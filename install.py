#!/usr/bin/env python3
"""Install slotfather and retire pre-rename queues once they are idle.

This explicit migration is outside normal queue admission. It preserves every old
queue as the new queue or a named backup, and blocks old clients with file guards.

The installed commands run on one named interpreter: `#!/usr/bin/env python3` becomes
the interpreter running this installer (or --python), so an agent inside a project's
virtual environment still runs slotfather on the Python it was installed with.

`--user NAME` installs for another user (root only) or pins the queue for this one: the
queue is NAME's ~/.local/state/slotfather, written into the installed slotfather as its
default state directory, so every process that runs these commands shares that one queue
whatever its HOME (several CI runner services of one user, #653). Paths it creates under
NAME's home are given to NAME. `--bin DIR` puts the commands in DIR instead of
~/.local/bin, such as /usr/local/bin, which service PATHs already hold. `--cgroup PATH`
writes in the cgroup v2 path the leases share (their memory limit and pressure), so a
shell outside it, such as root's through the guest agent, counts the same capacity:

    sudo python3 install.py --user runner --bin /usr/local/bin --cgroup /ci.slice/ci-runners.slice
"""
from __future__ import annotations

from contextlib import ExitStack, contextmanager
import ctypes
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import pwd
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import uuid
from typing import Any, Iterator, cast

MIN_PYTHON = (3, 9)
ENV_SHEBANG = b"#!/usr/bin/env python3\n"
COMMANDS = ("slotfather", "slotfather-dashboard", "slotfather-stats", "slotfather-sweep")
USAGE = "usage: python3 install.py [--python /path/to/python3] [--user NAME] [--bin DIR] [--cgroup /A.slice/B.slice] [--host SSH_ALIAS]"
STATE_LINE = b'DEFAULT_DIR = "~/.local/state/slotfather"\n'  # in slotfather; --user replaces it
CGROUP_LINE = b"DEFAULT_CGROUP = None  "  # in slotfather; --cgroup replaces the None
GUARD = b"Retired queue: use slotfather with ~/.local/state/slotfather. Update this checkout before running heavy work.\n"


class InstallError(Exception):
    pass


@contextmanager
def lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise InstallError(f"queue operation in progress at {path}; retry after it finishes") from None
        yield


def live_processes() -> dict[int, str]:
    # Linux: the C locale, as slotfather's own ps (procps writes lstart in the locale's words).
    env = {**os.environ, "LC_ALL": "C"} if sys.platform.startswith("linux") else None
    result = subprocess.run(["ps", "-axo", "pid=,stat=,lstart="], capture_output=True, text=True, env=env)
    if result.returncode:
        raise InstallError("cannot establish whether queue processes are live: ps failed")
    table: dict[int, str] = {}
    for row in result.stdout.splitlines():
        parts = row.split(None, 2)
        if len(parts) == 3 and parts[0].isdigit() and not parts[1].startswith("Z"):
            table[int(parts[0])] = parts[2].strip()
    if os.getpid() not in table:
        raise InstallError("cannot establish queue idleness: ps did not identify this installer")
    return table


def require_idle(directory, table):
    for path in (directory / "queue").glob("*.json"):
        try:
            entry = json.loads(path.read_text())
            pid, start, state = entry["pid"], entry["start"], entry["state"]
            if type(pid) is not int or not isinstance(start, str) or state not in ("held", "waiting"):
                raise ValueError("unrecognized queue entry")
        except (OSError, ValueError, KeyError, TypeError) as error:
            raise InstallError(f"cannot establish old queue idleness from {path}: {error}") from None
        if pid in table and (not start or table[pid] == start):
            raise InstallError(f"live old {state} entry at {path}; let holders and waiters drain before installing")


def require_current_format(directory: Path, table: dict[int, str]) -> None:
    """The caller holds the queue lock: live clients must already use canonical request fields."""
    for path in (directory / "queue").glob("*.json"):
        try:
            entry: dict[str, Any] = json.loads(path.read_text())
            pid, start, state = entry["pid"], entry["start"], entry["state"]
            if type(pid) is not int or pid <= 0 or not isinstance(start, str) or state not in ("held", "waiting"):
                raise ValueError("unrecognized queue entry")
        except (OSError, ValueError, KeyError, TypeError) as error:
            raise InstallError(f"cannot establish current queue format from {path}: {error}") from None
        live_start = table.get(pid)
        if live_start is None:
            continue
        if start and " ".join(start.split()) != " ".join(live_start.split()):
            continue  # The PID was reused; this stale record is not the process now running.
        if not start:
            raise InstallError(f"cannot establish process identity at {path}; let holders and waiters drain "
                               "before installing")
        raw_meta = entry.get("meta", {})
        if not isinstance(raw_meta, dict):
            raise InstallError(f"cannot establish current queue format from {path}: metadata is not a mapping")
        meta = cast(dict[str, Any], raw_meta)
        priority = entry.get("priority")
        if (not isinstance(priority, str) or priority not in ("interactive", "normal", "batch")
                or any(key in meta for key in ("class", "priority", "ticket"))):
            raise InstallError(f"live old-format {state} entry at {path}; update its caller and let old-format "
                               "holders and waiters drain before installing; no running work was stopped")


def reserve_current_queue(directory: Path, locks: ExitStack, created_guards: list[tuple[Path, int]]) -> bool:
    """Lock an existing queue through publication, or reserve an absent path against concurrent creation."""
    while True:
        if directory.is_symlink():
            raise InstallError(f"new queue must be a real directory: {directory}")
        if directory.is_dir():
            locks.enter_context(lock(directory / ".lock"))
            require_current_format(directory, live_processes())
            return False
        if directory.exists():
            raise InstallError(f"new queue path is not a directory; preserve it and recover before retrying: {directory}")
        try:
            inode = make_guard(directory)
            created_guards.append((directory, inode))
            return True
        except FileExistsError:
            continue  # A client won creation: lock and inspect its queue before any other installation work.


def history_ticket(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, int) and not isinstance(value, bool):
        value = str(value)
    if not isinstance(value, str) or len(value) > 200 or not value.isprintable():
        raise ValueError("ticket is not a short printable identifier")
    value = value.strip()
    if not value or value.lower() == "none":
        return None
    return value[1:] if value.startswith("#") and value[1:].isdigit() else value


def history_bytes(path: Path) -> bytes:
    """Migration reads only regular files, never follows a history symlink."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                raise InstallError(f"history migration requires a regular file: {path}")
            return handle.read()
    except OSError as error:
        raise InstallError(f"cannot read history for migration at {path}: {error}") from None


def history_updates(directory: Path | None) -> list[tuple[str, bytes, bytes]]:
    """Plan the complete finite conversion before any history or release is changed."""
    updates: list[tuple[str, bytes, bytes]] = []
    if directory is None:
        return updates
    for name in ("history.jsonl", "history.jsonl.1"):
        path = directory / name
        if path.is_symlink():
            raise InstallError(f"history migration refuses a symlink: {path}")
        if not path.exists():
            continue
        original = history_bytes(path)
        try:
            lines = original.decode("utf-8").splitlines(keepends=True)
        except UnicodeError as error:
            raise InstallError(f"cannot migrate {path}: {error}") from None
        converted: list[str] = []
        for number, line in enumerate(lines, 1):
            if not line.strip():
                converted.append(line)
                continue
            try:
                raw = json.loads(line)
                if not isinstance(raw, dict):
                    raise ValueError("history record is not an object")
                record = cast(dict[str, Any], raw)
                updated = dict(record)
                raw_meta = record.get("meta")
                if raw_meta is not None and not isinstance(raw_meta, dict):
                    raise ValueError("history metadata is not a mapping")
                meta = cast(dict[str, Any], raw_meta) if raw_meta is not None else {}
                priority = record["priority"] if "priority" in record else meta.get("priority", meta.get("class", "normal"))
                if priority not in ("interactive", "normal", "batch"):
                    raise ValueError(f"unsupported priority {priority!r}")
                updated["priority"] = priority
                if "ticket" in record:
                    updated["ticket"] = history_ticket(record["ticket"])
                elif "ticket" in meta:
                    ticket = history_ticket(meta["ticket"])
                    if ticket is not None:
                        updated["ticket"] = ticket
                if isinstance(raw_meta, dict):
                    updated["meta"] = {key: value for key, value in meta.items()
                                       if key not in ("class", "priority", "ticket")}
                if updated == record:
                    converted.append(line)
                else:
                    ending = "\r\n" if line.endswith("\r\n") else "\n" if line.endswith("\n") else ""
                    converted.append(json.dumps(updated, ensure_ascii=False, separators=(",", ":")) + ending)
            except (ValueError, TypeError) as error:
                raise InstallError(f"cannot migrate {path} line {number}: {error}") from None
        payload = "".join(converted).encode("utf-8")
        if payload != original:
            updates.append((name, original, payload))
    return updates


def migrate_history(directory: Path, updates: list[tuple[str, bytes, bytes]], backup: Path) -> None:
    """Back up all originals before replacing any file; the current queue lock remains held."""
    if not updates:
        return
    for name, original, _ in updates:
        if history_bytes(directory / name) != original:
            raise InstallError(f"history changed during installation at {directory / name}; retry")
    saved = backup / "history"
    pending: list[tuple[Path, Path]] = []
    try:
        saved.mkdir(parents=True, mode=0o700)
        for name, original, payload in updates:
            with (saved / name).open("xb") as handle:
                os.fchmod(handle.fileno(), 0o600)
                handle.write(original)
                handle.flush()
                os.fsync(handle.fileno())
            target = directory / name
            with tempfile.NamedTemporaryFile(dir=directory, prefix=".history-migration-", delete=False) as handle:
                path = Path(handle.name)
                pending.append((path, target))
                handle.write(payload)
                handle.flush()
                os.fchmod(handle.fileno(), target.stat().st_mode & 0o777)
                os.fsync(handle.fileno())
        for path, target in pending:
            os.replace(path, target)
    except OSError as error:
        raise InstallError(f"history conversion stopped; original files are preserved at {saved}: {error}") from None
    finally:
        for path, _ in pending:
            path.unlink(missing_ok=True)
    print(f"Migrated request fields in {', '.join(name for name, _, _ in updates)}; originals saved at {saved}")


def exchange(first, second):
    """Swap a directory with a guard atomically; never expose a missing old path."""
    libc = ctypes.CDLL(None, use_errno=True)
    if sys.platform == "darwin":
        function = libc.renamex_np
        function.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint]
        result = function(os.fsencode(first), os.fsencode(second), 2)  # RENAME_SWAP
    elif sys.platform.startswith("linux") and hasattr(libc, "renameat2"):
        function = libc.renameat2
        function.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        result = function(-100, os.fsencode(first), -100, os.fsencode(second), 2)  # RENAME_EXCHANGE
    else:
        raise InstallError("this installer requires atomic rename exchange on macOS or Linux")
    if result:
        number = ctypes.get_errno()
        raise InstallError(f"atomic queue retirement failed for {first}: {os.strerror(number)}")


def make_guard(path: Path) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as handle:
        handle.write(GUARD)
    return path.stat().st_ino


def options(argv: list[str]) -> dict[str, str]:
    """Local install options and an optional SSH destination, each once with a value."""
    found: dict[str, str] = {}
    while argv:
        if argv[0] not in ("--python", "--user", "--bin", "--cgroup", "--host") or len(argv) < 2 or argv[0] in found:
            raise InstallError(USAGE)
        found[argv[0]], argv = argv[1], argv[2:]
    return found


def interpreter(python: str | None = None) -> str:
    """The interpreter the commands will name: --python PATH, else this one unless it belongs to a virtual
    environment, whose interpreter disappears with the project. It must exist and be Python 3.9 or later."""
    if python:
        python = os.path.abspath(os.path.expanduser(python))
    elif sys.prefix != sys.base_prefix:
        raise InstallError("this Python belongs to a virtual environment; run install.py with a Python outside it, "
                           "or name one with --python")
    else:
        python = sys.executable  # as invoked, not resolved: a version-manager link keeps following upgrades
    try:
        found = subprocess.run([python, "-c", "import sys; print('%d.%d' % sys.version_info[:2])"],
                               capture_output=True, text=True, timeout=20).stdout.strip()
        version = tuple(int(part) for part in found.split("."))
    except (OSError, ValueError, subprocess.TimeoutExpired):
        raise InstallError(f"cannot run {python} as Python") from None
    if version < MIN_PYTHON:
        raise InstallError(f"{python} is Python {found}; slotfather needs {MIN_PYTHON[0]}.{MIN_PYTHON[1]} or later")
    return python


def install_binary(source, target, python, state=None, cgroup=None):
    payload = source.read_bytes()
    if payload.startswith(ENV_SHEBANG):
        payload = b"#!" + os.fsencode(python) + b"\n" + payload[len(ENV_SHEBANG):]
    if state is not None:
        if payload.count(STATE_LINE) != 1:
            raise InstallError(f"cannot pin the state directory in {source}: its DEFAULT_DIR line is not the expected one")
        payload = payload.replace(STATE_LINE, b"DEFAULT_DIR = " + json.dumps(str(state)).encode() + b"\n")
    if cgroup is not None:
        if payload.count(CGROUP_LINE) != 1:
            raise InstallError(f"cannot pin the cgroup in {source}: its DEFAULT_CGROUP line is not the expected one")
        payload = payload.replace(CGROUP_LINE, b"DEFAULT_CGROUP = " + json.dumps(cgroup).encode() + b"  ")
    target.parent.mkdir(parents=True, exist_ok=True)
    pending = None
    try:
        with tempfile.NamedTemporaryFile(dir=target.parent, prefix=".slotfather-", delete=False) as handle:
            pending = Path(handle.name)
            handle.write(payload)
            handle.flush()
            os.fchmod(handle.fileno(), 0o755)
            os.fsync(handle.fileno())
        os.replace(pending, target)
    finally:
        if pending is not None:
            pending.unlink(missing_ok=True)


def source_files(root: Path) -> list[Path]:
    """The portable runtime payload, excluding bytecode, git data and unrelated checkout files."""
    files = [root / "install.py", *(root / name for name in COMMANDS)]
    package = root / "src" / "slotfather"
    if package.is_dir():
        files.extend(sorted(package.rglob("*.py")))
    return files


def source_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in source_files(root):
        if not path.is_file():
            raise InstallError(f"source file missing: {path}")
        name = str(path.relative_to(root)).encode()
        payload = path.read_bytes()
        digest.update(len(name).to_bytes(8, "big") + name)
        digest.update(len(payload).to_bytes(8, "big") + payload)
    return digest.hexdigest()


def source_identity(root: Path) -> dict[str, Any]:
    """Installed and streamed checkouts identify exactly the same runtime bytes."""
    digest = source_digest(root)
    supplied = root / ".slotfather-source.json"
    if supplied.is_file():
        data = json.loads(supplied.read_text())
        if data.get("source_sha256") != digest:
            raise InstallError("streamed source does not match its recorded digest")
        return data
    result = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True)
    commit = result.stdout.strip() if result.returncode == 0 else None
    changed = subprocess.run(["git", "-C", str(root), "status", "--porcelain", "--untracked-files=normal", "--",
                              "install.py", *COMMANDS, "src"], capture_output=True, text=True)
    return {"v": 1, "commit": commit, "dirty": bool(changed.stdout.strip()) or commit is None,
            "source_sha256": digest}


def install_package(home: Path, root: Path, state: Path | None = None, cgroup: str | None = None,
                    bin_dir: Path | None = None) -> Path:
    """Prepare a complete immutable release before any command can reference it."""
    identity = source_identity(root)
    defaults = {"state": str(state) if state is not None else None, "cgroup": cgroup}
    descriptor = {"identity": identity, "defaults": defaults}
    suffix = hashlib.sha256(json.dumps(descriptor, sort_keys=True).encode()).hexdigest()[:12]
    # System entry points must never load runner-writable modules during a root status query.
    prefix = home / ".local" if bin_dir is None or home in bin_dir.parents else bin_dir.parent
    releases = prefix / "lib/slotfather/releases"
    releases.mkdir(parents=True, exist_ok=True)
    releases.parent.chmod(0o755)
    releases.chmod(0o755)
    release = releases / (identity["source_sha256"] + "-" + suffix)
    pending = Path(tempfile.mkdtemp(prefix=".pending-", dir=releases))
    try:
        package = pending / "slotfather"
        package.mkdir()
        source_package = root / "src/slotfather"
        for path in sorted(source_package.rglob("*.py")):
            target = package / path.relative_to(source_package)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target)
        config = package / "config.py"
        payload = config.read_bytes()
        if state is not None:
            if payload.count(STATE_LINE) != 1:
                raise InstallError(f"cannot pin the state directory in {config}: unexpected DEFAULT_DIR")
            payload = payload.replace(STATE_LINE, b"DEFAULT_DIR = " + json.dumps(str(state)).encode() + b"\n")
        if cgroup is not None:
            if payload.count(CGROUP_LINE) != 1:
                raise InstallError(f"cannot pin the cgroup in {config}: unexpected DEFAULT_CGROUP")
            payload = payload.replace(CGROUP_LINE, b"DEFAULT_CGROUP = " + json.dumps(cgroup).encode() + b"  ")
        config.write_bytes(payload)
        (package / "version.json").write_text(json.dumps(identity, sort_keys=True) + "\n")
        # mkdtemp is private while preparing; published root-owned code must be readable by runner.
        for path in pending.rglob("*"):
            path.chmod(0o755 if path.is_dir() else 0o644)
        pending.chmod(0o755)
        if source_digest(root) != identity["source_sha256"]:
            raise InstallError("source changed during install; retry from a stable checkout")
        if release.exists():
            expected = {str(p.relative_to(pending)): p.read_bytes() for p in pending.rglob("*") if p.is_file()}
            actual = {str(p.relative_to(release)): p.read_bytes() for p in release.rglob("*")
                      if p.is_file() and "__pycache__" not in p.parts}
            if actual != expected:
                raise InstallError(f"immutable installed release differs from source: {release}")
        else:
            os.replace(pending, release)
        return release
    finally:
        if pending.exists():
            shutil.rmtree(pending)


def package_wrapper(release: Path, name: str) -> bytes:
    module = "cli" if name == "slotfather" else name.removeprefix("slotfather-")
    return ("#!/usr/bin/env python3\nimport sys\nsys.path.insert(0, " + repr(str(release)) + ")\n"
            "from slotfather." + module + " import main\nif __name__ == '__main__':\n    sys.exit(main())\n").encode()


def install_remote(root: Path, host: str, argv: list[str]) -> None:
    """Send one verified runtime snapshot to the named SSH destination; no remote git checkout needed."""
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.@:-]*", host):
        raise InstallError("--host takes an SSH alias or user@hostname, not options or shell syntax")
    identity = source_identity(root)
    data = io.BytesIO()
    with tarfile.open(fileobj=data, mode="w") as archive:
        for path in source_files(root):
            archive.add(path, arcname=str(path.relative_to(root)), recursive=False)
        payload = (json.dumps(identity, sort_keys=True) + "\n").encode()
        info = tarfile.TarInfo(".slotfather-source.json")
        info.size = len(payload)
        info.mode = 0o600
        archive.addfile(info, io.BytesIO(payload))
    command = 'python3 "$staging/install.py"' + "".join(" " + shlex.quote(part) for part in argv)
    script = ('set -eu; staging=$(mktemp -d "${TMPDIR:-/tmp}/slotfather-install.XXXXXXXX"); '
              "trap 'rm -rf -- \"$staging\"' EXIT HUP INT TERM; "
              'tar -xf - -C "$staging"; ' + command)
    result = subprocess.run(["ssh", "--", host, script], input=data.getvalue())
    if result.returncode:
        raise InstallError(f"remote install on {host} failed (exit {result.returncode})")


def install(home: Path, source: Path, python: str, bin_dir: Path | None = None,
            pinned: bool = False, cgroup: str | None = None) -> None:
    """Install into `bin_dir` (default HOME/.local/bin) for the queue in HOME/.local/state/slotfather; `pinned`
    writes that queue into slotfather as its default, `cgroup` the cgroup v2 path its leases share."""
    bin_dir = bin_dir or home / ".local/bin"
    state = home / ".local/state"
    destination = state / "slotfather"
    old_paths = (state / "machine-slots", state / "straylight-verification/slots")
    old_command = home / ".local/bin/machine-slot"
    for name in COMMANDS:
        if not source.with_name(name).is_file():
            raise InstallError(f"source executable missing: {source.with_name(name)}")
    if old_command.is_dir() and not old_command.is_symlink():
        raise InstallError(f"old command is a directory; leaving it unchanged: {old_command}")
    release = None
    created_guards: list[tuple[Path, int]] = []
    changing = False
    with lock(state / ".slotfather-install.lock"), ExitStack() as locks:
        try:
            new_guard = reserve_current_queue(destination, locks, created_guards)
            old_directories = []
            # Reserve absent old paths before checking idleness. An old client can
            # no longer create a queue while the other paths are being checked.
            for old in old_paths:
                while True:
                    if old.is_symlink():
                        raise InstallError(f"old queue is a symlink; retire it explicitly first: {old}")
                    if old.is_dir():
                        locks.enter_context(lock(old / ".lock"))
                        old_directories.append(old)
                        break
                    if old.exists():
                        if not old.is_file():
                            raise InstallError(f"unrecognized old queue path: {old}")
                        if old.read_bytes() != GUARD:
                            raise InstallError(f"unrecognized file at old queue path; leaving it unchanged: {old}")
                        break  # An intentional retirement guard already blocks old clients.
                    try:
                        inode = make_guard(old)
                        created_guards.append((old, inode))
                        break
                    except FileExistsError:
                        continue  # A client created it; inspect and lock its queue.
            table = live_processes()
            for old in old_directories:
                require_idle(old, table)
            history_source = destination if not new_guard else old_paths[0] if old_paths[0] in old_directories else None
            updates = history_updates(history_source)
            if (source.parent / "src/slotfather").is_dir():
                release = install_package(home, source.parent, destination if pinned else None, cgroup, bin_dir)
            backup = state / "slotfather-migrations" / uuid.uuid4().hex
            for old in old_directories:
                if new_guard and old == old_paths[0]:
                    exchange(old, destination)
                    changing = True
                    new_guard = False
                    print(f"Migrated queue and configuration to {destination}")
                else:
                    saved = backup / ("machine-slots" if old == old_paths[0] else "straylight-verification-slots")
                    make_guard(saved)
                    exchange(old, saved)
                    changing = True
                    print(f"Old queue preserved at {saved}; existing new queue is unchanged")
            if new_guard:
                empty = backup / "new-queue"
                empty.mkdir(parents=True)
                locks.enter_context(lock(empty / ".lock"))
                exchange(destination, empty)
                changing = True
                empty.unlink()  # This is now the temporary guard, never queue data.
            changing = True
            migrate_history(destination, updates, backup)
            # The companions (dashboard, stats, sweep) first: an executable that names a missing one is the shorter-lived
            # mismatch.
            for name in reversed(COMMANDS):
                main_command = name == "slotfather"
                if release is None:
                    install_binary(source.with_name(name), bin_dir / name, python,
                                   destination if pinned and main_command else None, cgroup if main_command else None)
                else:
                    with tempfile.NamedTemporaryFile(dir=release, prefix=".wrapper-", delete=False) as handle:
                        wrapper = Path(handle.name)
                        handle.write(package_wrapper(release, name))
                    try:
                        install_binary(wrapper, bin_dir / name, python)
                    finally:
                        wrapper.unlink(missing_ok=True)
            old_command.unlink(missing_ok=True)
            print(f"Installed {bin_dir / 'slotfather'}, slotfather-dashboard, slotfather-stats and slotfather-sweep for "
                  f"{python}; old queue paths are file guards" + (f"; queue {destination}" if pinned else "")
                  + (f"; cgroup {cgroup}" if cgroup else ""))
        finally:
            # An idleness refusal leaves original state and commands untouched.
            # Once exchanges begin, preserve every guard and backup even on error.
            if not changing:
                for path, inode in reversed(created_guards):
                    if path.is_file() and path.stat().st_ino == inode:
                        path.unlink()


def give(home, user, bin_dir, owner_of=lambda path: os.lstat(path).st_uid, chown=os.lchown):
    """Hands what this (root) run created under `home` to `user`: the state paths the installer writes, and the
    commands when they live in the user's home. Only root-owned paths change hands; commands elsewhere stay root's."""
    state = home / ".local/state"
    paths = [home / ".local", state, home / ".local/bin", state / ".slotfather-install.lock"]
    for name in ("slotfather", "machine-slots", "straylight-verification", "slotfather-migrations"):
        top = state / name
        paths.append(top)
        if top.is_dir() and not top.is_symlink():
            paths += sorted(top.rglob("*"))
    library = home / ".local/lib/slotfather"
    paths.extend([home / ".local/lib", library])
    if library.is_dir() and not library.is_symlink():
        paths.extend(sorted(library.rglob("*")))
    if home in bin_dir.parents or bin_dir == home:
        paths += [bin_dir] + [bin_dir / name for name in COMMANDS]
    for path in paths:
        if os.path.lexists(path) and owner_of(path) == 0:
            chown(path, user.pw_uid, user.pw_gid)


def main():
    if sys.argv[1:] == ["--help"]:
        print(USAGE)
        return 0
    try:
        found = options(sys.argv[1:])
        if "--host" in found:
            host = found.pop("--host")
            forwarded = [value for pair in found.items() for value in pair]
            install_remote(Path(__file__).resolve().parent, host, forwarded)
            return 0
        python = interpreter(found.get("--python"))
        home, user = Path.home(), None
        if "--user" in found:
            try:
                user = pwd.getpwnam(found["--user"])
            except KeyError:
                raise InstallError(f"no user {found['--user']!r} on this machine") from None
            if user.pw_uid != os.getuid() and os.getuid() != 0:
                raise InstallError(f"installing for {user.pw_name} needs root (sudo)")
            home = Path(user.pw_dir)
        bin_dir = Path(os.path.expanduser(found["--bin"])) if "--bin" in found else None
        if bin_dir is not None and not bin_dir.is_absolute():
            raise InstallError("--bin takes an absolute directory")
        cgroup = found.get("--cgroup")
        if cgroup is not None and (not cgroup.startswith("/") or "\n" in cgroup or ".." in cgroup.split("/")):
            raise InstallError("--cgroup takes a cgroup v2 path such as /ci.slice/ci-runners.slice")
        try:
            install(home, Path(__file__).resolve().with_name("slotfather"), python, bin_dir, pinned=user is not None,
                    cgroup=cgroup)
        finally:
            # Also after a failure part-way: what it created under the user's home is the user's.
            if user is not None and os.getuid() == 0 and user.pw_uid != 0:
                give(home, user, bin_dir or home / ".local/bin")
    except (InstallError, OSError) as error:
        print(f"slotfather install: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
