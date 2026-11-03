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
from contextlib import ExitStack, contextmanager
import ctypes
import fcntl
import json
import os
from pathlib import Path
import pwd
import subprocess
import sys
import tempfile
import uuid

MIN_PYTHON = (3, 9)
ENV_SHEBANG = b"#!/usr/bin/env python3\n"
COMMANDS = ("slotfather", "slotfather-dashboard", "slotfather-stats")
USAGE = "usage: python3 install.py [--python /path/to/python3] [--user NAME] [--bin DIR] [--cgroup /A.slice/B.slice]"
STATE_LINE = b'DEFAULT_DIR = "~/.local/state/slotfather"\n'  # in slotfather; --user replaces it
CGROUP_LINE = b"DEFAULT_CGROUP = None  "  # in slotfather; --cgroup replaces the None
GUARD = b"Retired queue: use slotfather with ~/.local/state/slotfather. Update this checkout before running heavy work.\n"


class InstallError(Exception):
    pass


@contextmanager
def lock(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise InstallError(f"queue operation in progress at {path}; retry after it finishes") from None
        yield


def live_processes():
    # Linux: the C locale, as slotfather's own ps (procps writes lstart in the locale's words).
    env = {**os.environ, "LC_ALL": "C"} if sys.platform.startswith("linux") else None
    result = subprocess.run(["ps", "-axo", "pid=,stat=,lstart="], capture_output=True, text=True, env=env)
    if result.returncode:
        raise InstallError("cannot establish whether old queue processes are live: ps failed")
    table = {}
    for row in result.stdout.splitlines():
        parts = row.split(None, 2)
        if len(parts) == 3 and parts[0].isdigit() and not parts[1].startswith("Z"):
            table[int(parts[0])] = parts[2].strip()
    if os.getpid() not in table:
        raise InstallError("cannot establish old queue idleness: ps did not identify this installer")
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


def make_guard(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as handle:
        handle.write(GUARD)
    return path.stat().st_ino


def options(argv):
    """--python, --user, --bin and --cgroup, each at most once with a value."""
    found = {}
    while argv:
        if argv[0] not in ("--python", "--user", "--bin", "--cgroup") or len(argv) < 2 or argv[0] in found:
            raise InstallError(USAGE)
        found[argv[0]], argv = argv[1], argv[2:]
    return found


def interpreter(python=None):
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


def install(home, source, python, bin_dir=None, pinned=False, cgroup=None):
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
    created_guards = []
    changing = False
    with lock(state / ".slotfather-install.lock"), ExitStack() as locks:
        try:
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
            if destination.is_symlink():
                raise InstallError(f"new queue must be a real directory: {destination}")
            new_guard = False
            if not destination.exists():
                try:
                    inode = make_guard(destination)
                    created_guards.append((destination, inode))
                    new_guard = True
                except FileExistsError:
                    pass
            if not new_guard and not destination.is_dir():
                raise InstallError(f"new queue path is not a directory; preserve it and recover before retrying: {destination}")
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
                exchange(destination, empty)
                changing = True
                empty.unlink()  # This is now the temporary guard, never queue data.
            changing = True
            # The companions (dashboard, stats) first: an executable that names a missing one is the shorter-lived
            # mismatch.
            for name in reversed(COMMANDS):
                main_command = name == "slotfather"
                install_binary(source.with_name(name), bin_dir / name, python,
                               destination if pinned and main_command else None, cgroup if main_command else None)
            old_command.unlink(missing_ok=True)
            print(f"Installed {bin_dir / 'slotfather'}, slotfather-dashboard and slotfather-stats for "
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
