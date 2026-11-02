#!/usr/bin/env python3
"""Install slotfather and retire pre-rename queues once they are idle.

This explicit migration is outside normal queue admission. It preserves every old
queue as the new queue or a named backup, and blocks old clients with file guards.

The installed commands run on one named interpreter: `#!/usr/bin/env python3` becomes
the interpreter running this installer (or --python), so an agent inside a project's
virtual environment still runs slotfather on the Python it was installed with.
"""
from contextlib import ExitStack, contextmanager
import ctypes
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import uuid

MIN_PYTHON = (3, 9)
ENV_SHEBANG = b"#!/usr/bin/env python3\n"
COMMANDS = ("slotfather", "slotfather-dashboard", "slotfather-stats")
USAGE = "usage: python3 install.py [--python /path/to/python3]"
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
    result = subprocess.run(["ps", "-axo", "pid=,stat=,lstart="], capture_output=True, text=True)
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


def interpreter(argv):
    """The interpreter the commands will name: --python PATH, else this one unless it belongs to a virtual
    environment, whose interpreter disappears with the project. It must exist and be Python 3.9 or later."""
    if argv:
        if len(argv) != 2 or argv[0] != "--python":
            raise InstallError(USAGE)
        python = os.path.abspath(os.path.expanduser(argv[1]))
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


def install_binary(source, target, python):
    payload = source.read_bytes()
    if payload.startswith(ENV_SHEBANG):
        payload = b"#!" + os.fsencode(python) + b"\n" + payload[len(ENV_SHEBANG):]
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


def install(home, source, python):
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
                install_binary(source.with_name(name), home / ".local/bin" / name, python)
            old_command.unlink(missing_ok=True)
            print(f"Installed {home / '.local/bin/slotfather'}, slotfather-dashboard and slotfather-stats for "
                  f"{python}; old queue paths are file guards")
        finally:
            # An idleness refusal leaves original state and commands untouched.
            # Once exchanges begin, preserve every guard and backup even on error.
            if not changing:
                for path, inode in reversed(created_guards):
                    if path.is_file() and path.stat().st_ino == inode:
                        path.unlink()


def main():
    if sys.argv[1:] == ["--help"]:
        print(USAGE)
        return 0
    try:
        python = interpreter(sys.argv[1:])
        install(Path.home(), Path(__file__).resolve().with_name("slotfather"), python)
    except (InstallError, OSError) as error:
        print(f"slotfather install: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
