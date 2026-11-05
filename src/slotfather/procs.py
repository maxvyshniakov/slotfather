"""Read-only process identities, session ancestry and lease membership.

A query is an observation, never a queue operation: no lock, directory creation,
reaping or history append is performed. Consumers must revalidate ``pid,start``
before acting on a process because an observation cannot prevent PID reuse.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Any, NamedTuple, cast

from . import config, state


class ProcsError(config.SlotError):
    """Process membership could not be established."""


class Process(NamedTuple):
    pid: int
    ppid: int
    uid: int
    tty: str
    start: str
    cmd: str


_SESSION = re.compile(r"(^|/)(claude|codex|CodexCLI|tmux|screen|zellij|mosh-server|sshd|sshd-session|login|"
                      r"Runner\.Listener|Runner\.Worker)$|/claude/versions/[^/]+$")


def read_processes(pid: int | None = None, required: set[int] | None = None) -> dict[int, Process]:
    """One locale-stable process table; failure is never an empty answer."""
    if sys.platform == "darwin" and pid is not None:
        try:
            from .macos_process import snapshot
            return {number: Process(*process) for number, process in snapshot(pid, required or set()).items()}
        except (OSError, ValueError, AttributeError):
            pass  # Unsupported native ABI or required identity: use the full ps adapter.
    argv = ["ps", "-ax", "-ww", "-o", "pid=,ppid=,uid=,tty=,stat=,lstart=,command="]
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=5,
                                env={**os.environ, "LC_ALL": "C"})
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ProcsError(f"cannot read processes: {error}") from None
    if result.returncode:
        raise ProcsError(f"ps exited {result.returncode}: {result.stderr.strip()[:200]}")
    found: dict[int, Process] = {}
    for line in result.stdout.splitlines():
        parts = line.split(None, 10)
        if len(parts) < 10 or not all(value.isdigit() for value in parts[:3]):
            continue
        if parts[4].startswith("Z"):
            continue
        pid, ppid, uid = map(int, parts[:3])
        found[pid] = Process(pid, ppid, uid, "" if parts[3] in ("?", "??", "-") else parts[3],
                             " ".join(parts[5:10]), parts[10] if len(parts) > 10 else "")
    if not found:
        raise ProcsError("ps listed no live processes")
    return found


def ancestors(processes: dict[int, Process], pid: int) -> list[int]:
    """Nearest parent first; cycles and PID 1 end the chain."""
    found: list[int] = []
    seen = {pid}
    parent = processes[pid].ppid if pid in processes else 0
    while parent > 1 and parent in processes and parent not in seen:
        seen.add(parent)
        found.append(parent)
        parent = processes[parent].ppid
    return found


def session(process: Process) -> bool:
    executable = (process.cmd.split() or [""])[0]
    return bool(process.tty or _SESSION.search(executable))


def lease_entries(directory: Path | None = None) -> list[tuple[Path, dict[str, Any], bool]]:
    """Validate raw queue entries once before observing the process identities they require."""
    directory = directory or config.state_dir()
    sources = [(directory, False), *[(path, True) for path in config.legacy_dirs()]]
    found: list[tuple[Path, dict[str, Any], bool]] = []
    for source, old in sources:
        if old and source.is_file():
            continue  # Installers retire legacy queues with file guards.
        state.schema_version(source)
        try:
            with os.scandir(source / "queue") as queue:
                paths = sorted(Path(item.path) for item in queue if item.name.endswith(".json"))
        except (FileNotFoundError, NotADirectoryError):
            continue
        except OSError as error:
            raise ProcsError(f"cannot read queue {source}: {error}") from None
        for path in paths:
            try:
                raw = json.loads(path.read_text())
                entry = cast(dict[str, Any], raw) if isinstance(raw, dict) else None
            except FileNotFoundError:
                continue  # A holder released while this observation was being made.
            except (OSError, ValueError) as error:
                raise ProcsError(f"cannot read lease {path}: {error}") from None
            if entry is None or type(entry.get("pid")) is not int or entry["pid"] <= 0 or \
                    (not isinstance(entry.get("start"), str) or not entry["start"].strip()) or entry.get("state") not in ("held", "waiting") or \
                    type(entry.get("count")) is not int or entry["count"] <= 0:
                raise ProcsError(f"invalid lease identity at {path}")
            if old and entry["state"] != "held":
                continue
            found.append((path, entry, old))
    return found


def read_leases(processes: dict[int, Process], entries: list[tuple[Path, dict[str, Any], bool]]) -> list[dict[str, Any]]:
    """Join live identities without changing stale entries on disk."""
    leases: list[dict[str, Any]] = []
    for path, entry, old in entries:
        holder = processes.get(entry["pid"])
        start = str(entry["start"])
        if holder is None or start and " ".join(start.split()) != holder.start:
            continue
        meta = config.meta_of(entry)
        priority = config.priority_of(entry)
        leases.append({"id": path.stem, "lease": str(path), "state": entry["state"],
                       "count": entry["count"], "label": entry.get("label"), "pid": holder.pid,
                       "start": holder.start, "priority": priority, "ticket": config.ticket_of(entry),
                       "meta": meta, "legacy": old})
    return leases


def read_cwds(processes: dict[int, Process], uid: int) -> dict[int, str]:
    """Read local working directories; inaccessible processes remain explicitly unknown."""
    found: dict[int, str] = {}
    if sys.platform.startswith("linux"):
        proc = Path(os.environ.get("SLOTFATHER_PROC", "/proc"))
        try:
            namespace = os.readlink(proc / "self/ns/mnt")
        except OSError as error:
            raise ProcsError(f"cannot read own mount namespace: {error}") from None
        for pid, process in processes.items():
            if process.uid != uid:
                continue
            try:
                if os.readlink(proc / str(pid) / "ns/mnt") != namespace:
                    continue
                path = os.readlink(proc / str(pid) / "cwd")
            except OSError:
                continue
            if path.endswith(" (deleted)"):
                path = path[:-len(" (deleted)")]
            found[pid] = os.path.realpath(path)
        return found
    try:
        result = subprocess.run(["lsof", "-a", "-d", "cwd", "-u", str(uid), "-w", "-Fpn"],
                                capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ProcsError(f"cannot read working directories: {error}") from None
    if result.returncode not in (0, 1) or not result.stdout.strip():
        raise ProcsError(f"lsof could not read working directories (exit {result.returncode})")
    current: int | None = None
    for line in result.stdout.splitlines():
        if line.startswith("p") and line[1:].isdigit():
            current = int(line[1:])
        elif line.startswith("n") and current in processes and processes[current].uid == uid:
            found[current] = os.path.realpath(line[1:])
    return found


def environment_leases(pids: list[int], leases: list[dict[str, Any]]) -> tuple[dict[int, set[str]], list[int]]:
    """Read only lease markers; environment contents are never retained or returned."""
    found: dict[int, set[str]] = {}
    if not pids or not leases:
        return found, []
    paths = {str(lease["lease"]) for lease in leases}
    if sys.platform.startswith("linux"):
        proc = Path(os.environ.get("SLOTFATHER_PROC", "/proc"))
        unknown: list[int] = []
        for number in pids:
            try:
                fields = (proc / str(number) / "environ").read_bytes().split(b"\0")
            except OSError:
                unknown.append(number)
                continue
            found[number] = {path for path in paths if os.fsencode("SLOTFATHER_LEASE=" + path) in fields}
        return found, unknown
    try:
        result = subprocess.run(["ps", "-wwE", "-o", "pid=,command=", "-p", ",".join(map(str, pids))],
                                capture_output=True, text=True, env={**os.environ, "LC_ALL": "C"}, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return found, pids
    if result.returncode:
        return found, pids
    for line in result.stdout.splitlines():
        number, _, rest = line.strip().partition(" ")
        if number.isdigit() and int(number) in pids:
            found[int(number)] = {path for path in paths if f" SLOTFATHER_LEASE={path} " in f" {rest} "}
    # ps may print command text while suppressing a protected process's entire
    # environment. No matched marker is an unknown result, never proof of absence.
    return found, sorted(number for number in pids if not found.get(number))


def query(pid: int | None = None, cwd: str | None = None, directory: Path | None = None) -> dict[str, Any]:
    """Observe processes; ``--pid`` is exact and does not scan working directories."""
    entries = lease_entries(directory)
    processes = read_processes(pid, {entry["pid"] for _, entry, _ in entries})
    leases = read_leases(processes, entries)
    selected = sorted(processes) if pid is None else ([pid] if pid in processes else [])
    cwds = read_cwds(processes, os.geteuid()) if cwd is not None else {}
    unknown: list[int] = []
    if cwd is not None:
        cwd = os.path.realpath(os.path.expanduser(cwd))
        own = {os.getpid(), *ancestors(processes, os.getpid())}
        unknown = sorted(number for number, process in processes.items()
                         if process.uid == os.geteuid() and number not in cwds and number not in own)
        selected = [number for number in selected if processes[number].uid == os.geteuid() and number not in own
                    and number in cwds and (cwds[number] == cwd or cwds[number].startswith(cwd.rstrip("/") + "/"))]
    chains = {number: ancestors(processes, number) for number in selected}
    detached = [number for number in selected if not any(lease["pid"] in [number, *chains[number]] for lease in leases)]
    marked, lease_unknown = environment_leases(detached, leases)
    records: list[dict[str, Any]] = []
    for number in selected:
        process = processes[number]
        chain = chains[number]
        records.append({**process._asdict(), "cwd": cwds.get(number), "ancestors": chain,
                        "sessions": [{"pid": item, "start": processes[item].start, "cmd": processes[item].cmd}
                                     for item in [number, *chain] if session(processes[item])],
                        "leases": [{**lease, "via": "tree" if lease["pid"] in [number, *chain] else "env"}
                                   for lease in leases if lease["pid"] in [number, *chain]
                                   or lease["lease"] in marked.get(number, set())]})
    return {"v": 2, "query": {"pid": pid, "cwd": cwd, "start": processes[pid].start if pid in processes else None},
            "cwd_unknown": unknown, "lease_unknown": lease_unknown, "processes": records}


USAGE = "usage: slotfather procs --json [--pid PID] [--cwd PATH]"


def command(argv: list[str]) -> int:
    """CLI adapter; only explicit JSON queries are accepted."""
    found: dict[str, str | bool] = {}
    at = 0
    while at < len(argv):
        option = argv[at]
        if option in found or option not in ("--json", "--pid", "--cwd"):
            raise ProcsError(USAGE)
        if option == "--json":
            found[option] = True
            at += 1
        elif at + 1 < len(argv) and argv[at + 1] and not argv[at + 1].startswith("--"):
            found[option] = argv[at + 1]
            at += 2
        else:
            raise ProcsError(USAGE)
    if "--json" not in found:
        raise ProcsError(USAGE)
    pid: int | None = None
    if "--pid" in found:
        value = str(found["--pid"])
        if not value.isascii() or not value.isdigit() or int(value) <= 0:
            raise ProcsError(USAGE)
        pid = int(value)
    cwd = str(found["--cwd"]) if "--cwd" in found else None
    print(json.dumps(query(pid=pid, cwd=cwd)))
    return 0
