#!/usr/bin/env python3
"""slotfather-sweep: find, and with --live stop, processes left behind on the machine (#662); `slotfather sweep`
runs it.

  slotfather sweep [--live [--dev-servers]] [--json] [--quiet] [--pid PID]... [--no-log]
  slotfather sweep config [KEY VALUE]... | config reset
  slotfather sweep --plist [--live [--dev-servers]] [--interval SECONDS]

Classes (the 2026-11-03 incident, straylight#661):
  removed     a process whose working directory, or a path in its command, is in a directory that no longer
              exists (a removed worktree or temporary directory), older than removed_age: --live stops it
  orphan      a process adopted by launchd/init (ppid 1) of a known dev kind (verify and test runners, vitest,
              Playwright, agent-browser, headless Chromium, cargo/swift test, `tail -f`), older than orphan_age:
              --live stops it
  dev-server  storybook/vite/next dev servers older than dev_server_age whose parent chain reaches no live
              session and in whose project no session works: reported; --live --dev-servers stops them
  container   containers in a restart loop: reported only
  memory      while swap or memory runs short, the largest resident processes with their class: reported only

A live session's child (an agent's background shell, a terminal's job) is reported, never stopped, whatever its
class, except a removed-class one whose working directory is gone and which is older than session_removed_age (6 h):
the worktree it waited on will not come back. A `pgrep -f PATTERN` wait loop whose own command line matches
PATTERN gets a note (Linux's pgrep finds the loop itself; macOS's finds other waits for the same pattern).

Never touched: a process inside a live lease of this queue (the holder's tree, or carrying the lease's
SLOTFATHER_LEASE, as `status --json --pid` decides), or one whose tree holds such a process or a session; other
users' processes; processes with a terminal; agent and terminal sessions (claude, codex, tmux, sshd); system
and app bundle processes; launchd jobs and systemd services' main processes; the sweep and its ancestors. A stop
sends SIGTERM to the process and its descendants and SIGKILL to those still there after `grace` seconds, each
time only to a pid whose start time is unchanged. Without --live nothing is signalled. Every sweep appends a line
to sweep.jsonl beside the queue (`slotfather stats` counts them); stops are also written to queue.log. Settings
(ages, grace, the enabled classes) live in sweep.json beside the queue: `slotfather sweep config`.
"""
from __future__ import annotations

import contextlib
import datetime
import fcntl
import json
import os
from pathlib import Path
import plistlib
import re
import signal
import stat
import subprocess
import sys
import time
from typing import NamedTuple


from . import config as _config
from . import leases as _leases
from . import platform as _platform
from . import state as _state


LINUX = sys.platform.startswith("linux")
VERSION = 1
CLASSES = ("removed", "orphan", "dev-server", "container", "memory")
STOPPING = ("removed", "orphan")  # what --live stops; dev-server only with --dev-servers
DEFAULTS = {"classes": list(CLASSES), "removed_age": 600.0, "orphan_age": 7200.0, "dev_server_age": 28800.0,
            "session_removed_age": 21600.0, "grace": 10.0, "container_minutes": 30.0, "container_restarts": 3.0, "swap_free_mb": 1024.0,
            "swap_free_pct": 20.0, "memory_top": 8.0}
DURATIONS = ("removed_age", "orphan_age", "dev_server_age", "session_removed_age", "grace", "container_minutes")
LOG_MAX = 4 << 20  # bytes of sweep.jsonl before it becomes sweep.jsonl.1
TIMEOUT = 10  # seconds a ps, lsof, launchctl or docker call may take
LABEL = "com.slotfather.sweep"
USAGE = ("usage: slotfather sweep [--live [--dev-servers]] [--json] [--quiet] [--pid PID]... [--no-log]\n"
         "       slotfather sweep config [KEY VALUE]... | config reset\n"
         "       slotfather sweep --plist [--live [--dev-servers]] [--interval SECONDS]")


class SweepError(Exception):
    pass


# What a process is

class Proc(NamedTuple):
    pid: int
    ppid: int
    uid: int
    rss: int  # KB
    tty: str  # "" without a controlling terminal
    start: str  # ps lstart in the C locale: with the pid, the process's identity
    started: float | None  # epoch seconds
    cmd: str


# (kind, pattern on the command line) for the orphan class; the first that matches names it.
ORPHAN_KINDS = (
    ("vitest", re.compile(r"(^|[\s/])vitest(\.m?js)?(\s|$)")),
    ("jest", re.compile(r"(^|[\s/])jest(\.js)?(\s|$)")),
    ("storybook-test", re.compile(r"(^|[\s/])(test-storybook|storybook\s+test)(\s|$)")),
    ("agent-browser", re.compile(r"agent-browser")),
    ("playwright", re.compile(r"playwright")),
    ("chromium", re.compile(r"(?i)(chrom(e|ium)[^/]*|headless_shell)(\s.*)?\s--(headless|remote-debugging-p\w+)")),
    ("puppeteer", re.compile(r"puppeteer")),
    ("cargo-test", re.compile(r"(^|[\s/])cargo\s+(test|nextest)(\s|$)|/target/(debug|release)/deps/[\w-]+-[0-9a-f]{16}(\s|$)")),
    ("rustc", re.compile(r"(^|/)rustc(\s|$)")),
    ("swift-test", re.compile(r"(^|[\s/])swift[\s-](test|build)(\s|$)|swiftpm-testing-helper|(^|/)xctest(\s|$)")),
    ("pytest", re.compile(r"(^|[\s/])pytest(\s|$)|\s-m\s+(pytest|unittest)(\s|$)")),
    ("esbuild", re.compile(r"(^|/)esbuild\s.*--service")),
)
SHELLS = ("sh", "bash", "zsh", "dash")
RUNNERS = ("node", "bun", "deno", "npm", "npx", "pnpm", "yarn", "tsx", "python", "uv", "make") + SHELLS
RUNNER_WORD = re.compile(r"^(verify|test|tests|run-tests|smoke|e2e|heavy|run-browser-checks|browser-checks)$")
DEV_SERVERS = (
    ("storybook-dev", re.compile(r"(^|[\s/])(storybook\s+dev|start-storybook)(\s|$)")),
    ("vite-dev", re.compile(r"(^|[\s/])vite(\.m?js)?(\s+(dev|serve|preview))?(\s|$)")),
    ("next-dev", re.compile(r"(^|[\s/])next\s+dev(\s|$)")),
    ("astro-dev", re.compile(r"(^|[\s/])astro\s+dev(\s|$)")),
    ("webpack-serve", re.compile(r"webpack-dev-server|(^|[\s/])webpack\s+serve(\s|$)")),
)
BROWSERS = ("agent-browser", "chromium", "playwright", "puppeteer")  # daemons a live session may still drive
SESSION = re.compile(r"(^|/)(claude|codex|CodexCLI|tmux|screen|zellij|mosh-server|sshd|sshd-session|login|"
                     r"Runner\.Listener|Runner\.Worker)$|/claude/versions/[^/]+$")
# GUI apps whose children outlive the conversation that started them: they are not a live session (#661's preview
# server ran 27 h under the desktop app).
AGENT_APPS = re.compile(r"/(Claude|ChatGPT|Codex)\.app/")
SYSTEM = ("/System/", "/usr/libexec/", "/usr/sbin/", "/sbin/", "/Library/Apple/", "/usr/lib/systemd/",
          "/lib/systemd/")


def program(cmd: str) -> str:
    """The command's executable as ps shows it: its first word, or for an app bundle the path through its
    .app/Contents/ part, spaces and all."""
    found = re.match(r"(/(?:[^ ]| (?![-/]))*?\.app/Contents/\S*)", cmd)
    return found[1] if found else (cmd.split() or [""])[0]


def words(cmd: str) -> list[str]:
    """The command's words; macOS ps writes a newline or tab inside an argument as \\012 or \\011."""
    return cmd.replace("\\012", " ").replace("\\011", " ").split()


def orphan_kind(cmd: str) -> str | None:
    for name, pattern in ORPHAN_KINDS:
        if pattern.search(cmd):
            return name
    parts = words(cmd)
    if not parts:
        return None
    base = os.path.basename(parts[0])
    if base == "tail" and any(re.fullmatch(r"-[A-Za-z0-9+]*[fF][A-Za-z0-9]*", w) for w in parts[1:]):
        return "tail-f"
    base = re.sub(r"[\d.]+$", "", base) or base
    if base in RUNNERS:
        for n, word in enumerate(runner_script(parts)):
            if word == "--test":
                return "runner"
            if base in SHELLS and ("/" not in word and "." not in word or n):
                continue  # a shell's `test` or `[` is no test runner; its script (./verify.sh) is
            stem = os.path.basename(word).split(".")[0]
            if RUNNER_WORD.match(stem) or re.search(r"\.(test|spec)\.[cm]?[jt]sx?$", word):
                return "runner"
    return None


VALUE_OPTIONS = {"--cwd", "-C", "--prefix", "--filter", "-F", "-r", "--require", "--import", "--loader", "-w",
                 "--workspace", "--dir", "--directory", "-p", "--project"}
SUBCOMMANDS = {"run", "exec", "x", "run-script"}


def runner_script(parts: list[str]) -> list[str]:
    """What a runner runs: its script (or `-m` module, or a shell's `-c` command) and the word after it when that
    is no option (`node cli.mjs verify`, `npm run test`), or ["--test"]. Options and their values are skipped, so
    `node server.js --root /srv/e2e` and `python3 -m http.server --directory ~/tests` name no test."""
    rest = [w.strip("'\";") for w in parts[1:]]
    at = 0
    while at < len(rest):
        word = rest[at]
        if word == "--test":
            return [word]
        if word in ("-m", "-c"):
            script = rest[at + 1:at + 2]
            after = rest[at + 2:at + 3]
            return script + ([after[0]] if script and after and not after[0].startswith("-") else [])
        if word.startswith("-"):
            at += 2 if word in VALUE_OPTIONS else 1
            continue
        if word in SUBCOMMANDS:
            at += 1
            continue
        after = rest[at + 1:at + 2]
        return [word] + ([after[0]] if after and not after[0].startswith("-") else [])
    return []


def dev_server_kind(cmd: str) -> str | None:
    if re.search(r"(^|[\s/])vitest", cmd):
        return None
    for name, pattern in DEV_SERVERS:
        if pattern.search(cmd):
            return name
    return None


def is_session(proc: Proc) -> bool:
    return bool(proc.tty) or bool(SESSION.search(program(proc.cmd)))


def is_app(proc: Proc) -> bool:
    return ".app/Contents/" in program(proc.cmd)


# Snapshots of the machine

class Cwd(NamedTuple):
    path: str
    removed: bool | None  # None: unknown


class Snapshot:
    """The process table and what the sweep needs to know about it. Tests build one from fake tables."""

    def __init__(self, procs: dict[int, Proc], cwds: dict[int, Cwd] | None = None, managed=(),
                 moment: float | None = None, uid: int | None = None, me=(), isdir=None, notes=(), foreign=()):
        self.procs, self.cwds, self.managed = procs, cwds or {}, set(managed)
        self.foreign = set(foreign)
        self.moment = time.time() if moment is None else moment
        self.uid = os.geteuid() if uid is None else uid
        self.me = set(me)
        self.isdir = isdir or dir_state  # True, False (gone) or None (unknown)
        self.notes = list(notes)
        self.children: dict[int, list[int]] = {}
        for proc in procs.values():
            self.children.setdefault(proc.ppid, []).append(proc.pid)

    def age(self, proc: Proc) -> float | None:
        return None if proc.started is None else max(0.0, self.moment - proc.started)

    def ancestors(self, pid: int) -> list[int]:
        found, seen = [], {pid}
        here = self.procs[pid].ppid if pid in self.procs else 0
        while here in self.procs and here not in seen and here > 1:
            found.append(here)
            seen.add(here)
            here = self.procs[here].ppid
        return found

    def tree(self, pid: int) -> list[int]:
        found, stack = [], [pid]
        while stack:
            here = stack.pop()
            if here in self.procs and here not in found:
                found.append(here)
                stack.extend(self.children.get(here, ()))
        return found

    def adopted(self, proc: Proc) -> bool:
        """Whether launchd or init (or, on Linux, a user's systemd manager) adopted it: an orphan."""
        if proc.ppid == 1:
            return True
        parent = self.procs.get(proc.ppid)
        return LINUX and parent is not None and os.path.basename(words(parent.cmd)[0] if parent.cmd else "") == \
            "systemd"


def table() -> dict[int, Proc]:
    """Every process: one ps over the machine in the C locale (lstart in the words every client writes)."""
    try:
        result = subprocess.run(["ps", "-ax", "-ww", "-o", "pid=,ppid=,uid=,rss=,tty=,lstart=,command="],
                                capture_output=True, text=True, env={**os.environ, "LC_ALL": "C"}, timeout=TIMEOUT)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise SweepError(f"ps: {error}") from None
    return parse_table(result.stdout)


def parse_table(text: str) -> dict[int, Proc]:
    found = {}
    for line in text.splitlines():
        parts = line.split(None, 10)
        if len(parts) < 10 or not all(p.isdigit() for p in parts[:4]):
            continue
        start = " ".join(parts[5:10])
        tty = "" if parts[4] in ("??", "?", "-") else parts[4]
        found[int(parts[0])] = Proc(int(parts[0]), int(parts[1]), int(parts[2]), int(parts[3]), tty, start,
                                    _platform.started_at(start), parts[10].strip() if len(parts) > 10 else "")
    return found


def dir_state(path: str) -> bool | None:
    """True: a directory is there; False: nothing is (or a file); None: unknown, as when the sweep may not look
    (launchd without Full Disk Access under ~/Documents, ~/Desktop, iCloud): unknown is never gone."""
    try:
        return stat.S_ISDIR(os.stat(path).st_mode)
    except (FileNotFoundError, NotADirectoryError):
        return False
    except OSError:
        return None


def removed_dir(path: str, inode: int | None, isdir=dir_state) -> bool | None:
    """Whether a working directory is gone: nothing at its path, or another directory there now; None: unknown."""
    if not path.startswith("/"):
        return None
    state = isdir(path)
    if state is not True:
        return None if state is None else True
    if inode is not None:
        try:
            return os.stat(path).st_ino != inode
        except (FileNotFoundError, NotADirectoryError):
            return True
        except OSError:
            return None
    return False


def working_dirs(procs: dict[int, Proc], uid: int) -> tuple[dict[int, Cwd], list[str]]:
    """The working directory of each of the user's processes, and notes on what could not be read. macOS: one
    lsof; Linux: /proc/PID/cwd, which says " (deleted)" for a removed one."""
    found, notes = {}, []
    if LINUX:
        found, notes, _ = proc_cwds(procs, uid)
        return found, notes
    try:
        result = subprocess.run(["lsof", "-a", "-d", "cwd", "-u", str(uid), "-w", "-Fpin"], capture_output=True,
                                text=True, timeout=TIMEOUT * 3)
    except (OSError, subprocess.TimeoutExpired) as error:
        return found, [f"working directories unknown (lsof: {error})"]
    pid = inode = None
    for line in result.stdout.splitlines():
        tag, value = line[:1], line[1:]
        if tag == "p":
            pid, inode = (int(value) if value.isdigit() else None), None
        elif tag == "i":
            inode = int(value) if value.isdigit() else None
        elif tag == "n" and pid is not None:
            found[pid] = Cwd(value, removed_dir(value, inode))
    return found, notes


def proc_cwds(procs: dict[int, Proc], uid: int) -> tuple[dict[int, Cwd], list[str], set[int]]:
    """Linux: the working directories from /proc/PID/cwd, notes, and the pids in another mount namespace (a
    container: rootless podman, `docker run --user $(id -u)`), whose paths mean nothing here; those are left out
    and never candidates. A directory is gone only when the kernel says so: " (deleted)" or no links left."""
    found, notes, foreign = {}, [], set()
    try:
        own = os.readlink(_platform.proc_path("self", "ns", "mnt"))
    except OSError:
        own = None
    for pid, proc in procs.items():
        if proc.uid != uid:
            continue
        try:
            if own is not None and os.readlink(_platform.proc_path(str(pid), "ns", "mnt")) != own:
                foreign.add(pid)
                continue
            link = os.readlink(_platform.proc_path(str(pid), "cwd"))
        except OSError:
            if own is not None:
                foreign.add(pid)  # its namespace cannot be read: do not judge it
            continue
        if link.endswith(" (deleted)"):
            found[pid] = Cwd(link[:-len(" (deleted)")], True)
            continue
        try:
            removed = os.stat(_platform.proc_path(str(pid), "cwd")).st_nlink == 0
        except OSError:
            removed = None
        found[pid] = Cwd(link, removed)
    if foreign:
        notes.append(f"{len(foreign)} process(es) in another mount namespace (a container) left out")
    return found, notes, foreign


def managed_jobs(procs: dict[int, Proc], uid: int) -> set[int]:
    """Processes a service manager runs on purpose: launchd jobs of this user (macOS), systemd services' main
    processes (Linux). They have ppid 1 like an orphan; a service is not one."""
    found = set()
    if not LINUX:
        # `launchctl list` answers for the caller's domain only (from ssh or cron not the GUI one), so the GUI
        # domain's services are asked for by name as well.
        for command in (["launchctl", "list"], ["launchctl", "print", f"gui/{uid}"]):
            with contextlib.suppress(OSError, subprocess.TimeoutExpired):
                result = subprocess.run(command, capture_output=True, text=True, timeout=TIMEOUT)
                found |= launchd_pids(result.stdout, command[1])
        return found
    snap = Snapshot(procs, uid=uid)
    units: dict[tuple[bool, str], int] = {}
    for pid, proc in procs.items():
        if proc.uid != uid or not snap.adopted(proc):
            continue
        try:
            lines = _platform.proc_path(str(pid), "cgroup").read_text().splitlines()
        except OSError:
            continue
        key = service_of(lines)
        if key is None:
            continue
        if key not in units:
            units[key] = main_pid(*key)
        if units[key] in (pid, -1):
            found.add(pid)
    return found


def launchd_pids(text: str, how: str) -> set[int]:
    """The pids of running jobs in `launchctl list` (PID first on each line) or `launchctl print gui/UID` (its
    services block: pid, status, label) output."""
    found = set()
    if how == "list":
        for line in text.splitlines()[1:]:
            first = line.split("\t", 1)[0].strip()
            if first.isdigit() and int(first) > 0:
                found.add(int(first))
        return found
    inside = False
    for line in text.splitlines():
        if line.strip() == "services = {":
            inside = True
        elif inside and line.strip() == "}":
            inside = False
        elif inside:
            first = (line.split() or [""])[0]
            if first.isdigit() and int(first) > 0:
                found.add(int(first))
    return found


def service_of(cgroup_lines: list[str]) -> tuple[bool, str] | None:
    """(in a user's manager, unit) of a process's cgroup v2 path when it is a .service, else None."""
    path = next((line.split(":", 2)[2] for line in cgroup_lines if line.startswith("0::")), "")
    unit = path.rstrip("/").rsplit("/", 1)[-1]
    if not unit.endswith(".service"):
        return None
    return ("/user@" in path, unit)


def main_pid(user: bool, unit: str, run=subprocess.run) -> int:
    """The unit's MainPID from its manager (`systemctl --user` for a user's), or -1 when it cannot be asked: then
    every process in the service is taken as the service's."""
    try:
        result = run(["systemctl", *(["--user"] if user else []), "show", "--property", "MainPID", "--value", unit],
                     capture_output=True, text=True, timeout=TIMEOUT)
        if result.returncode == 0:
            return int(result.stdout.strip() or 0)
    except (OSError, subprocess.TimeoutExpired, ValueError):
        pass
    return -1


def snapshot() -> Snapshot:
    procs = table()
    uid = os.geteuid()
    foreign = set()
    if LINUX:
        cwds, notes, foreign = proc_cwds(procs, uid)
    else:
        cwds, notes = working_dirs(procs, uid)
    me, here = set(), os.getpid()
    while here in procs and here not in me and here > 1:
        me.add(here)
        here = procs[here].ppid
    me.add(os.getpid())
    return Snapshot(procs, cwds, managed_jobs(procs, uid), uid=uid, me=me, notes=notes, foreign=foreign)


# Leases: never touch what runs inside one

def leases(directory: Path, snap: Snapshot) -> tuple[dict[int, str], list[tuple[str, str]]]:
    """pid -> the label of the live lease whose holder's tree it is in, and (lease path, label) of every live lease
    (for the SLOTFATHER_LEASE check of processes that left the tree), from the queue (reaped as `status` does) and
    the earlier queues. The same rule as `status --json --pid` and the lease guard."""
    processes = _platform.processes()
    with _state.locked(directory):
        live = _leases.reap(directory, processes)
    protected, paths = {}, []
    for path, entry in live:
        label = str(entry.get("label") or path.stem)
        paths.append((str(path), label))
        for pid in _platform.descendants(processes, entry.get("pid")):
            protected[pid] = label
    for path, entry, _ in _leases.legacy_holders(processes):
        label = str(entry.get("label") or path.stem)
        paths.append((str(path), label))
        for pid in _platform.descendants(processes, entry.get("pid")):
            protected[pid] = label
    return protected, paths


def carried(pids: list[int], paths: list[tuple[str, str]], carrying=None) -> dict[int, str]:
    """Those of `pids` whose environment carries a live lease's SLOTFATHER_LEASE: pid -> label."""
    carrying = carrying or _platform.carrying
    found = {}
    rest = list(pids)
    for path, label in paths:
        if not rest:
            break
        for pid in carrying(rest, path):
            found[pid] = label
        rest = [p for p in rest if p not in found]
    return found


# Deciding

def exempt(proc: Proc, snap: Snapshot) -> str | None:
    """Why a process is never a candidate, or None."""
    if proc.pid <= 1 or proc.pid in snap.me:
        return "the sweep itself" if proc.pid in snap.me else "init"
    if proc.uid != snap.uid:
        return "another user's"
    if proc.tty:
        return "has a terminal"
    if is_session(proc):
        return "a session"
    exe = program(proc.cmd)
    if exe.startswith(SYSTEM):
        return "a system process"
    if is_app(proc) and not orphan_kind(proc.cmd):
        return "an app"
    if proc.pid in snap.managed:
        return "a launchd or systemd job"
    if proc.pid in snap.foreign:
        return "in another mount namespace"
    return None


def roots() -> tuple[str, ...]:
    """Where a path in a command must lie to count as a removed directory: home and the temporary directories."""
    found = [os.path.expanduser("~"), "/tmp", "/private/tmp", "/var/folders", "/private/var/folders", "/var/tmp"]
    temp = os.environ.get("TMPDIR")
    if temp:
        found.append(temp.rstrip("/"))
    return tuple(p.rstrip("/") + "/" for p in found if p and p != "/")


def gone_paths(cmd: str, isdir, within: tuple[str, ...]) -> list[str]:
    """Absolute paths in a command (also an option's =value and the items of a `:`, `,` or `;` list) under `within`
    whose directory is certainly gone (not merely unreadable); words with a glob or a shell variable are no paths."""
    found = []
    for word in words(cmd):
        for part in re.split(r"[=:,;]", word):  # NODE_PATH=a:b, -cp a.jar:b.jar, --paths=a,b
            path = part.strip("'\"()[]")
            if not path.startswith("/") or len(path) < 2 or not path.startswith(within) or \
                    re.search(r"[*?\[\]{}$`]", path):
                continue  # a glob or a variable is no path the process is in
            parent = os.path.dirname(path.rstrip("/"))
            if parent and parent != "/" and parent.rstrip("/") + "/" not in within and isdir(parent) is False \
                    and path not in found:
                found.append(path)
    return found


def project_root(path: str, isdir=os.path.isdir) -> str:
    """The checkout a directory belongs to (the nearest one with .git), else the directory itself."""
    here = path.rstrip("/") or "/"
    home = os.path.expanduser("~")
    while here not in ("/", home) and here:
        if os.path.lexists(os.path.join(here, ".git")):
            return here
        here = os.path.dirname(here)
    return path.rstrip("/") or "/"


def session_roots(snap: Snapshot) -> set[str]:
    """The checkouts live sessions (a terminal, an agent) work in."""
    found = set()
    for pid, proc in snap.procs.items():
        if proc.uid == snap.uid and is_session(proc) and pid in snap.cwds and not snap.cwds[pid].removed:
            path = snap.cwds[pid].path
            if path not in ("/", os.path.expanduser("~")):
                found.add(project_root(path))
    return found


def reaches_session(snap: Snapshot, pid: int) -> int | None:
    """The first ancestor that is a live session (a terminal, an agent CLI, a GUI app other than the agents'),
    or None."""
    for here in snap.ancestors(pid):
        proc = snap.procs[here]
        if is_session(proc) or (is_app(proc) and not AGENT_APPS.search(program(proc.cmd))):
            return here
    return None


def plan(snap: Snapshot, settings: dict, protected: dict[int, str] | None = None, carriers=None,
         only: set[int] | None = None, live: bool = False, dev_servers: bool = False,
         gone_since: dict[str, float] | None = None) -> list[dict]:
    """The sweep's items: each candidate process with its class, kind, tree and action (would-stop or stop, report,
    spared). `protected` maps pids in a live lease's tree to its label; `carriers(pids)` gives those of pids that
    carry a lease (pid -> label). `only` limits the candidates to those pids. `gone_since` maps "pid start" to when
    a sweep first saw that process's working directory gone (sweep-state.json); a process not in it counts as seen
    gone now."""
    protected = protected or {}
    gone_since = gone_since or {}
    enabled = set(settings["classes"])
    within = roots()
    session_dirs = None
    found = []
    for pid, proc in sorted(snap.procs.items()):
        if only is not None and pid not in only:
            continue
        if exempt(proc, snap):
            continue
        age = snap.age(proc)
        if age is None:
            continue
        item = None
        cwd = snap.cwds.get(pid)
        if "removed" in enabled and age >= settings["removed_age"]:
            if cwd is not None and cwd.removed is True:
                first = gone_since.get(gone_key(proc))
                item = {"class": "removed", "reason": f"cwd {cwd.path} is gone", "cwd_gone": True,
                        "gone_for": round(max(0.0, snap.moment - first)) if first else 0}
            elif orphan_kind(proc.cmd) or dev_server_kind(proc.cmd):
                # A path in the command counts only for the dev kinds: any other program may name a directory it
                # is about to create (an output path) or a list the split above did not foresee.
                gone = gone_paths(proc.cmd, snap.isdir, within)
                if gone:
                    item = {"class": "removed", "reason": f"{gone[0]} is in a directory that is gone"}
        if item is None and "orphan" in enabled and snap.adopted(proc) and age >= settings["orphan_age"]:
            kind = orphan_kind(proc.cmd)
            if kind and not dev_server_kind(proc.cmd):
                item = {"class": "orphan", "kind": kind, "reason": f"{kind} adopted by pid {proc.ppid}"}
                if kind in BROWSERS and cwd and not cwd.removed:
                    session_dirs = session_roots(snap) if session_dirs is None else session_dirs
                    if project_root(cwd.path) in session_dirs:  # its browser may be in use
                        item.update(spare=f"a session works in its checkout {project_root(cwd.path)}")
        if item is None and "dev-server" in enabled and age >= settings["dev_server_age"]:
            kind = dev_server_kind(proc.cmd)
            if kind and not any(dev_server_kind(snap.procs[a].cmd) for a in snap.ancestors(pid)
                                if snap.procs[a].uid == snap.uid):
                if reaches_session(snap, pid) is None:
                    session_dirs = session_roots(snap) if session_dirs is None else session_dirs
                    if cwd is None or cwd.removed or project_root(cwd.path) not in session_dirs:
                        item = {"class": "dev-server", "kind": kind,
                                "reason": "no live session above it or in its checkout"}
        if item is None:
            continue
        item.setdefault("kind", orphan_kind(proc.cmd) or dev_server_kind(proc.cmd) or
                        os.path.basename(words(proc.cmd)[0]) if proc.cmd else "?")
        tree = [p for p in snap.tree(pid) if snap.procs[p].uid == snap.uid]
        item.update(pid=pid, ppid=proc.ppid, start=proc.start, age=round(age), cmd=proc.cmd[:300],
                    cwd=cwd.path if cwd else None, tree=sorted(tree),
                    rss_mb=round(sum(snap.procs[p].rss for p in tree) / 1024, 1))
        found.append(item)
    # Leases and sessions in the tree spare the whole tree.
    pending = sorted({p for item in found for p in item["tree"] if p not in protected})
    lease_env = carriers(pending) if carriers and pending else {}
    for item in found:
        held = next((protected.get(p) or lease_env.get(p) for p in item["tree"]
                     if p in protected or p in lease_env), None)
        session = next((p for p in item["tree"] if p != item["pid"] and (
            is_session(snap.procs[p]) or p in snap.me)), None)
        above = reaches_session(snap, item["pid"])
        if above is not None:
            item["session"] = above
            item["reason"] += f"; under live session pid {above} ({os.path.basename(program(snap.procs[above].cmd))})"
        if held is not None:
            item.update(action="spared", reason=f"{item['reason']}; inside the live lease {held}")
        elif item.get("spare"):
            item.update(action="spared", reason=f"{item['reason']}; {item.pop('spare')}")
        elif session is not None:
            item.update(action="spared", reason=f"{item['reason']}; a session (pid {session}) in its tree")
        elif item["class"] in STOPPING or (item["class"] == "dev-server" and dev_servers):
            beside = above is not None and same_gone_dir(snap, above, item)
            if above is not None and (beside or not (
                    item.get("cwd_gone") and item.get("gone_for", 0) >= settings["session_removed_age"])):
                # A live session's child is its owner's to stop; only one whose working directory a sweep has
                # seen gone for session_removed_age waits on a worktree that will not come back, and not when the
                # session itself works in that removed directory (its MCP servers, language servers).
                item.update(action="spared", under_session=True,
                            reason=f"{item['reason']}; report only: a live session's child"
                                   + ("; the session works in the same removed directory" if beside else ""))
            else:
                item["action"] = "stop" if live else "would-stop"
        else:
            item["action"] = "report"
    # A candidate inside another that will be stopped goes with it; one inside another reported under a session is
    # in that one's tree.
    stopping = {item["pid"] for item in found if item["action"] in ("stop", "would-stop")}
    reported = {item["pid"] for item in found if item.get("under_session")}
    kept = []
    for item in found:
        above = next((a for a in snap.ancestors(item["pid"]) if a in stopping), None)
        if above is not None and item["action"] != "spared":
            continue
        if item.get("under_session") and any(a in reported for a in snap.ancestors(item["pid"])):
            continue  # listed with the session's child above it, in whose tree it is
        kept.append(item)
    return kept


def gone_key(proc: Proc) -> str:
    return f"{proc.pid} {proc.start}"


def same_gone_dir(snap: Snapshot, session: int, item: dict) -> bool:
    """Whether the session itself sits in the removed checkout its child's working directory was in."""
    there = snap.cwds.get(session)
    if there is None or there.removed is not True or not item.get("cwd"):
        return False
    return project_root(there.path) == project_root(item["cwd"]) or \
        item["cwd"].startswith(there.path.rstrip("/") + "/")


def track_gone(snap: Snapshot, before: dict[str, float]) -> dict[str, float]:
    """sweep-state.json's "gone_since" after this look: every process of the user whose working directory is gone,
    with the moment a sweep first saw it so (kept from `before`), and nothing for processes no longer gone."""
    return {gone_key(proc): before.get(gone_key(proc), snap.moment) for pid, proc in snap.procs.items()
            if proc.uid == snap.uid and snap.cwds.get(pid) is not None and snap.cwds[pid].removed is True}


def read_state(directory: Path) -> dict:
    try:
        data = json.loads((directory / "sweep-state.json").read_text())
    except (OSError, ValueError):
        return {}
    gone = data.get("gone_since") if isinstance(data, dict) else None
    return {"gone_since": {str(k): float(v) for k, v in gone.items() if isinstance(v, (int, float))}} \
        if isinstance(gone, dict) else {}


# Waits that never end

PGREP = re.compile(r"\bpgrep\s+((?:-[A-Za-z]+\s+)*)(?:\"([^\"]+)\"|'([^']+)'|([^\s;|&'\"]+))")


def self_matching(cmd: str) -> str | None:
    """The pattern of a `pgrep -f PATTERN` loop (until/while) whose own command line PATTERN matches, else None.
    Linux's pgrep then finds the loop itself, which never ends; macOS's skips the loop (its ancestor) but finds every
    other process carrying the pattern, such as another wait for the same thing, and two such waits keep each other
    alive."""
    if not re.search(r"\b(until|while)\b", cmd):
        return None
    for found in PGREP.finditer(cmd):
        if "f" not in found[1].replace("-", ""):
            continue
        pattern = found[2] or found[3] or found[4]
        try:
            hit = re.search(pattern, cmd) is not None
        except re.error:
            hit = pattern in cmd
        if hit:
            return pattern
    return None


def self_matching_waits(snap: Snapshot, only: set[int] | None = None) -> list[dict]:
    """The user's processes that wait in a self-matching pgrep loop, outermost first (report only)."""
    found, seen = [], set()
    for pid, proc in sorted(snap.procs.items()):
        if proc.uid != snap.uid or (only is not None and pid not in only):
            continue
        pattern = self_matching(proc.cmd)
        if pattern is None or any(a in seen for a in snap.ancestors(pid)):
            continue
        seen.add(pid)
        age = snap.age(proc)
        found.append({"pid": pid, "pattern": pattern, "age": None if age is None else round(age),
                      "session": reaches_session(snap, pid), "cmd": loop_head(proc.cmd),
                      "matched_by": matching(snap, pid, pattern)})
    return found


def matching(snap: Snapshot, pid: int, pattern: str) -> list[int]:
    """Other processes `pgrep -f PATTERN` run by the wait `pid` finds: not the wait, its ancestors (macOS pgrep skips
    them; procps pgrep does not, so there the wait always finds itself) or its own children, nor pgrep runs."""
    try:
        test = re.compile(pattern).search
    except re.error:
        def test(cmd):
            return pattern in cmd
    skip = {pid, *snap.ancestors(pid), *snap.tree(pid)}
    return sorted(p for p, proc in snap.procs.items()
                  if p not in skip and os.path.basename(words(proc.cmd)[0] if proc.cmd else "") != "pgrep"
                  and test(proc.cmd))


def loop_head(cmd: str) -> str:
    """The command from its loop on (an agent's shell prefixes it with a long setup), cut to 160 characters."""
    at = re.search(r"\b(until|while)\b", cmd)
    return (cmd[at.start():] if at else cmd)[:160]


# Containers and memory: reports

def docker_time(text) -> float | None:
    try:
        return datetime.datetime.strptime(str(text)[:19], "%Y-%m-%dT%H:%M:%S").replace(
            tzinfo=datetime.timezone.utc).timestamp()
    except ValueError:
        return None


def restart_loops(inspected: list[dict], settings: dict, moment: float | None = None) -> list[dict]:
    """Containers that keep restarting: a restart policy, at least container_restarts restarts, created at least
    container_minutes ago, and restarting now or started again within that time."""
    moment = time.time() if moment is None else moment
    window = settings["container_minutes"] * 60
    found = []
    for item in inspected:
        state = item.get("State") or {}
        policy = ((item.get("HostConfig") or {}).get("RestartPolicy") or {}).get("Name") or "no"
        restarts = int(item.get("RestartCount") or 0)
        created, started = docker_time(item.get("Created")), docker_time(state.get("StartedAt"))
        if policy == "no" or restarts < settings["container_restarts"] or created is None or \
                moment - created < window:
            continue
        if not (state.get("Restarting") or (started is not None and moment - started < window)):
            continue
        labels = (item.get("Config") or {}).get("Labels") or {}
        found.append({"class": "container", "action": "report", "name": str(item.get("Name") or "").lstrip("/"),
                      "id": str(item.get("Id") or "")[:12], "project": labels.get("com.docker.compose.project"),
                      "status": state.get("Status"), "restarts": restarts, "policy": policy,
                      "exit_code": state.get("ExitCode"), "started": state.get("StartedAt"),
                      "since_created": round(moment - created)})
    return found


def containers(settings: dict) -> dict:
    """{"available": bool, "items": [...], "note"}: Docker is asked, never started."""
    try:
        listed = subprocess.run(["docker", "ps", "-aq", "--no-trunc"], capture_output=True, text=True,
                                timeout=TIMEOUT)
        if listed.returncode != 0:
            return {"available": False, "items": [], "note": (listed.stderr.strip().splitlines() or ["docker failed"])[0]}
        ids = listed.stdout.split()
        if not ids:
            return {"available": True, "items": []}
        inspected = subprocess.run(["docker", "inspect", *ids], capture_output=True, text=True, timeout=TIMEOUT)
        data = json.loads(inspected.stdout or "[]")
    except FileNotFoundError:
        return {"available": False, "items": [], "note": "no docker command"}
    except (OSError, subprocess.TimeoutExpired, ValueError) as error:
        return {"available": False, "items": [], "note": f"docker: {error}"}
    return {"available": True, "items": restart_loops(data if isinstance(data, list) else [], settings)}


def memory_facts() -> dict:
    """Swap and memory now: total and free swap in MB, and why memory is short (None: it is not)."""
    usage = None
    with contextlib.suppress(_config.SlotError, OSError):
        usage = _platform.swap_usage()
    found = {"swap_total_mb": None if usage is None else round(usage[0]),
             "swap_free_mb": None if usage is None else round(usage[1]), "short": None}
    with contextlib.suppress(_config.SlotError, OSError):
        found["short"] = _platform.memory_short()
    return found


def memory_report(facts: dict, snap: Snapshot, settings: dict, items: list[dict],
                  protected: dict[int, str]) -> dict:
    """The largest resident processes with their class while memory is short (`short`), else just the facts."""
    total, free = facts.get("swap_total_mb"), facts.get("swap_free_mb")
    short = facts.get("short")
    floor = max(settings["swap_free_mb"], (total or 0) * settings["swap_free_pct"] / 100)
    if short is None and total and free is not None and free < floor:
        short = f"free swap {free} of {total} MB is under {floor:.0f} MB ({settings['swap_free_mb']:g} MB or " \
                f"{settings['swap_free_pct']:g}%)"
    found = {**facts, "short": short, "top": []}
    if short is None:
        return found
    classed = {}
    for item in items:
        for pid in item.get("tree", ()):
            classed.setdefault(pid, item["class"])
    top = sorted(snap.procs.values(), key=lambda p: -p.rss)[:int(settings["memory_top"])]
    for proc in top:
        label = protected.get(proc.pid)
        entry = {"pid": proc.pid, "rss_mb": round(proc.rss / 1024), "cmd": proc.cmd[:200],
                 "class": f"lease {label}" if label else classed.get(proc.pid, "-"),
                 "age": None if snap.age(proc) is None else round(snap.age(proc))}
        if LINUX and proc.uid == snap.uid:
            with contextlib.suppress(OSError, ValueError):
                for line in _platform.proc_path(str(proc.pid), "status").read_text().splitlines():
                    if line.startswith("VmSwap:"):
                        entry["swap_mb"] = round(int(line.split()[1]) / 1024)
        found["top"].append(entry)
    return found


# Acting

def identities(pids: list[int]) -> dict[int, tuple[str, bool]]:
    """pid -> (lstart in the C locale, a zombie) for those of `pids` still in the process table."""
    if not pids:
        return {}
    try:
        result = subprocess.run(["ps", "-o", "pid=,stat=,lstart=", "-p", ",".join(map(str, pids))],
                                capture_output=True, text=True, env={**os.environ, "LC_ALL": "C"}, timeout=TIMEOUT)
    except (OSError, subprocess.TimeoutExpired):
        return {}
    found = {}
    for line in result.stdout.splitlines():
        parts = line.split(None, 2)
        if len(parts) == 3 and parts[0].isdigit():
            found[int(parts[0])] = (" ".join(parts[2].split()), parts[1].startswith("Z"))
    return found


def stop(item: dict, snap: Snapshot, grace: float, now=time.monotonic, pause=time.sleep, look=None,
         kill=os.kill) -> dict:
    """SIGTERM to the item's tree, SIGKILL to what is left after `grace` seconds; only to pids whose start time is
    the one the sweep saw. Returns {"result": stopped|killed|gone|failed, "signalled": [...], "left": [...]}."""
    look = look or identities
    expected = {p: " ".join(snap.procs[p].start.split()) for p in item["tree"] if p in snap.procs}
    seen = look(list(expected))
    targets = [p for p, start in expected.items() if p in seen and seen[p][0] == start and not seen[p][1]]
    if not targets:
        return {"result": "gone", "signalled": [], "left": []}
    signalled = []
    for pid in targets:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            kill(pid, signal.SIGTERM)
            signalled.append(pid)

    def remaining() -> list[int]:
        here = look(targets)
        return [p for p in targets if p in here and here[p][0] == expected[p] and not here[p][1]]
    deadline = now() + grace
    left = remaining()
    while left and now() < deadline:
        pause(0.2)
        left = remaining()
    if not left:
        return {"result": "stopped", "signalled": signalled, "left": []}
    for pid in left:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            kill(pid, signal.SIGKILL)
    deadline = now() + 2
    while left and now() < deadline:
        pause(0.1)
        left = remaining()
    return {"result": "failed" if left else "killed", "signalled": signalled, "left": left}


# Settings

def settings(directory: Path | None = None) -> dict:
    """sweep.json beside the queue over DEFAULTS. Unknown keys are ignored with a "warnings" entry (a newer
    slotfather may have written them); a file that cannot be read or holds a bad value gives the defaults with
    "error", and a live sweep then stops nothing."""
    found = dict(DEFAULTS, classes=list(DEFAULTS["classes"]), saved=False, warnings=[])
    try:
        data = json.loads(((directory or _config.state_dir()) / "sweep.json").read_text())
        if not isinstance(data, dict):
            raise ValueError(f"not sweep settings: {data!r}")
        unknown = sorted(set(data) - set(DEFAULTS))
        if unknown:
            found["warnings"].append(f"sweep.json: unknown setting(s) {', '.join(unknown)} ignored")
        found.update(checked({k: v for k, v in data.items() if k in DEFAULTS}), saved=True)
    except FileNotFoundError:
        pass
    except (OSError, ValueError, TypeError) as error:
        found["error"] = f"sweep.json ignored: {error}"
    return found


def seconds_of(text: str) -> float:
    found = re.fullmatch(r"(\d+(?:\.\d+)?)([smhd]?)", text.strip())
    if not found:
        raise ValueError(f"not a duration: {text!r} (seconds, or 90s, 30m, 2h, 1d)")
    return float(found[1]) * {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}[found[2]]


def checked(data) -> dict:
    if not isinstance(data, dict) or set(data) - set(DEFAULTS):
        raise ValueError(f"not sweep settings: {data!r}")
    found = {}
    for key, value in data.items():
        if key == "classes":
            if not isinstance(value, list) or set(value) - set(CLASSES):
                raise ValueError(f"classes are some of {', '.join(CLASSES)}, not {value!r}")
            found[key] = [c for c in CLASSES if c in value]
        elif type(value) not in (int, float) or value < 0 or (key == "memory_top" and value < 1):
            raise ValueError(f"{key} must be a number of at least {1 if key == 'memory_top' else 0}, not {value!r}")
        else:
            found[key] = float(value)
    return found


def config_command(argv: list[str], directory: Path) -> int:
    if argv == ["reset"]:
        _state.require_own(directory, "a sweep settings change")
        with _state.locked(directory):
            (directory / "sweep.json").unlink(missing_ok=True)
    elif argv:
        if len(argv) % 2:
            raise SweepError("config takes KEY VALUE pairs, or reset")
        changes = {}
        for key, value in zip(argv[::2], argv[1::2]):
            key = key.replace("-", "_")
            if key not in DEFAULTS:
                raise SweepError(f"unknown setting {key}; settings: {', '.join(DEFAULTS)}")
            try:
                if key == "classes":
                    changes[key] = [] if value in ("none", "") else [c.strip() for c in value.split(",") if c.strip()]
                else:
                    changes[key] = seconds_of(value) if key in DURATIONS else float(value)
            except ValueError as error:
                raise SweepError(str(error)) from None
        _state.require_own(directory, "a sweep settings change")
        with _state.locked(directory):
            current = settings(directory)
            if "error" in current:  # a broken file is replaced by the defaults and these changes
                current = dict(DEFAULTS, classes=list(DEFAULTS["classes"]))
            saved = {k: v for k, v in current.items() if k in DEFAULTS}
            try:
                values = checked({**saved, **changes})
            except ValueError as error:
                raise SweepError(str(error)) from None
            _state.write(directory / "sweep.json", values)
    found = settings(directory)
    for problem in found["warnings"] + ([found["error"]] if "error" in found else []):
        print(f"slotfather sweep: {problem}", file=sys.stderr)
    print(f"sweep settings ({'saved in ' + str(directory / 'sweep.json') if found['saved'] else 'defaults'}):")
    for key in DEFAULTS:
        value = found[key]
        text = ",".join(value) or "none" if key == "classes" else (
            _config.duration(value) if key in DURATIONS and key != "container_minutes" else f"{value:g}")
        print(f"  {key:<18} {text}")
    return 0


# The sweep

def sweep(directory: Path, live: bool = False, dev_servers: bool = False, only: set[int] | None = None,
          logged: bool = True) -> dict:
    found_settings = settings(directory)
    refused = live and "error" in found_settings
    if refused:
        live = False  # settings that cannot be read stop nothing: the sweep reports what it would do
    snap = snapshot()
    protected, paths = leases(directory, snap)
    carriers = (lambda pids: carried(pids, paths)) if paths else None
    before = read_state(directory).get("gone_since", {})
    items = plan(snap, found_settings, protected, carriers, only, live, dev_servers, before)
    if live:
        for item in items:
            if item["action"] == "stop":
                item.update(stop(item, snap, found_settings["grace"]))
                item["action"] = item.pop("result")
                _state.log(directory, f"sweep {item['action']} {item['class']} pid {item['pid']} ({item['kind']}, "
                                  f"age {_config.duration(item['age'])}, {len(item['tree'])} process(es)): "
                                  f"{item['cmd'][:120]}")
    enabled = set(found_settings["classes"])
    docker = containers(found_settings) if "container" in enabled and only is None else \
        {"available": None, "items": []}
    memory = memory_report(memory_facts(), snap, found_settings, items, protected) if "memory" in enabled else {}
    waits = self_matching_waits(snap, only)
    counts = {c: sum(i["class"] == c for i in items) for c in ("removed", "orphan", "dev-server")}
    counts["container"] = len(docker["items"])
    report = {"v": VERSION, "at": round(snap.moment, 3), "live": live, "dev_servers": dev_servers,
              "only": sorted(only) if only is not None else None,
              "settings": {k: found_settings[k] for k in DEFAULTS},
              "settings_error": found_settings.get("error"), "live_refused": refused,
              "notes": snap.notes + found_settings["warnings"] + (
                  [found_settings["error"] + ("; the live sweep stopped nothing" if refused else "")]
                  if "error" in found_settings else []),
              "items": items, "containers": docker, "memory": memory, "found": counts,
              "stopped": {c: sum(i["class"] == c and i["action"] in ("stopped", "killed") for i in items)
                          for c in ("removed", "orphan", "dev-server")},
              "would_stop": sum(i["action"] == "would-stop" for i in items),
              "spared": sum(i["action"] == "spared" for i in items),
              "under_session": sum(bool(i.get("under_session")) for i in items),
              "self_matching": waits,
              "failed": sum(i["action"] == "failed" for i in items),
              "freed_mb": round(sum(i["rss_mb"] for i in items if i["action"] in ("stopped", "killed")), 1)}
    if logged and _state.foreign_owner(directory) is None:
        with contextlib.suppress(OSError):
            directory.mkdir(parents=True, exist_ok=True)
            _state.write(directory / "sweep-state.json", {"v": VERSION, "gone_since": track_gone(snap, before)})
            _state.append_bounded(directory / "sweep.jsonl", json.dumps(report, sort_keys=True) + "\n", LOG_MAX)
    return report


ACTIONS = {"would-stop": "would stop", "stop": "stop", "stopped": "stopped", "killed": "killed", "gone": "gone",
           "failed": "FAILED", "spared": "spared", "report": "report"}


def text(report: dict, quiet: bool = False) -> str:
    lines = []
    items = report["items"]
    if quiet:
        return "\n".join(f"slotfather sweep: {ACTIONS[i['action']]} {i['class']} pid {i['pid']} {i['kind']}: "
                         f"{i['cmd'][:120]}" for i in items if i["action"] in ("stopped", "killed", "failed"))
    when = time.strftime("%Y-%m-%d %H:%M", time.localtime(report["at"]))
    mode = "live" + (" with dev servers" if report["dev_servers"] else "") if report["live"] else \
        ("dry run (live refused: settings)" if report.get("live_refused") else "dry run")
    found = report["found"]
    lines.append(f"slotfather sweep · {mode} · {when} · removed {found['removed']}, orphans {found['orphan']}, "
                 f"dev servers {found['dev-server']}, looping containers {found['container']}"
                 + (f" · only pids {','.join(map(str, report['only']))}" if report["only"] else ""))
    for item in items:
        if item.get("under_session"):
            continue
        lines.append(f"  {item['class']:<10} {ACTIONS[item['action']]:<10} pid {item['pid']:<6} "
                     f"{_config.duration(item['age']):>7} {item['rss_mb']:>7.0f} MB  {item['kind']:<14} "
                     f"{item['cmd'][:110]}")
        detail = item["reason"] + (f"; {len(item['tree'])} process(es)" if len(item["tree"]) > 1 else "")
        lines.append(f"  {'':<10} {'':<10} {detail}")
    if not items:
        lines.append("  no removed-directory processes, orphans or stale dev servers")
    sessions = {}
    for item in items:
        if item.get("under_session"):
            sessions.setdefault(item.get("session"), []).append(item)
    for session, group in sessions.items():
        lines.append(f"  under live session pid {session}: {len(group)} child(ren) reported only "
                     f"(stopped only once a sweep has seen their cwd gone {_config.duration(report['settings']['session_removed_age'])})")
        for item in group:
            gone = f" cwd gone {_config.duration(item['gone_for'])}" if item.get("cwd_gone") else ""
            lines.append(f"    {item['class']:<10} pid {item['pid']:<6} {_config.duration(item['age']):>7}{gone}  "
                         f"{loop_head(item['cmd'])[:100]}")
    patterns = {}
    for wait in report.get("self_matching") or []:
        patterns.setdefault(wait["pattern"], []).append(wait)
    for pattern, group in patterns.items():
        oldest = max((w["age"] or 0) for w in group)
        pids = [w["pid"] for w in group]
        lines.append(f"  note: self-matching wait: pgrep -f {pattern!r} matches its own command line; "
                     f"{len(group)} wait(s), oldest {_config.duration(oldest)}, pids {', '.join(map(str, pids[:8]))}"
                     + ("…" if len(pids) > 8 else "") + f": {group[0]['cmd'][:80]}")
    docker = report["containers"]
    if docker.get("available") is False:
        lines.append(f"  containers: not checked ({docker.get('note')})")
    for item in docker["items"]:
        lines.append(f"  container  report     {item['name']} ({item['project'] or '-'}): {item['status']}, "
                     f"{item['restarts']} restarts, policy {item['policy']}, exit {item['exit_code']}")
    memory = report["memory"]
    if memory:
        swap = "" if memory.get("swap_total_mb") is None else \
            f"swap {memory['swap_total_mb'] - memory['swap_free_mb']} of {memory['swap_total_mb']} MB used"
        if memory.get("short"):
            lines.append(f"  memory short: {memory['short']}" + (f"; {swap}" if swap else "") + "; largest:")
            for entry in memory["top"]:
                lines.append(f"    pid {entry['pid']:<6} {entry['rss_mb']:>6} MB  {entry['class']:<14} "
                             f"{entry['cmd'][:100]}")
        elif swap:
            lines.append(f"  memory: {swap}, not short")
    for note in report["notes"]:
        lines.append(f"  note: {note}")
    if report["live"]:
        stopped = sum(report["stopped"].values())
        lines.append(f"stopped {stopped} tree(s), {report['freed_mb']:.0f} MB resident; spared {report['spared']} "
                     f"({report.get('under_session', 0)} under a live session), failed {report['failed']}")
    else:
        lines.append(f"dry run: nothing was signalled; {report['would_stop']} would be stopped with --live"
                     + ("" if report["dev_servers"] else " (dev servers only with --live --dev-servers)")
                     + f"; spared {report['spared']} ({report.get('under_session', 0)} under a live session)")
    return "\n".join(lines)


def plist(live: bool, dev_servers: bool, interval: int, directory: Path) -> str:
    """A launchd agent that runs the sweep every `interval` seconds, dry-run unless `live`."""
    command = [sys.executable, str(Path(__file__).resolve().with_name("__main__.py")), "sweep", "--quiet"]
    if live:
        command.append("--live")
    if dev_servers:
        command.append("--dev-servers")
    environment = {"PATH": os.environ.get("PATH", "/usr/bin:/bin:/usr/sbin:/sbin")}
    if os.environ.get("SLOTFATHER_DIR"):
        environment["SLOTFATHER_DIR"] = os.environ["SLOTFATHER_DIR"]
    data = {"Label": LABEL, "ProgramArguments": command, "StartInterval": interval, "RunAtLoad": True,
            "ProcessType": "Background", "LowPriorityIO": True, "Nice": 10, "EnvironmentVariables": environment,
            "StandardOutPath": str(directory / "sweep.out"), "StandardErrorPath": str(directory / "sweep.out")}
    return plistlib.dumps(data).decode()


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    directory = _config.state_dir()
    try:
        if argv[:1] == ["config"]:
            return config_command(argv[1:], directory)
        live = dev = as_json = quiet = as_plist = False
        logged = True
        only, interval = None, 3600
        while argv:
            option = argv.pop(0)
            if option == "--live":
                live = True
            elif option == "--dev-servers":
                dev = True
            elif option == "--json":
                as_json = True
            elif option == "--quiet":
                quiet = True
            elif option == "--no-log":
                logged = False
            elif option == "--plist":
                as_plist = True
            elif option == "--interval" and argv and argv[0].isdigit() and int(argv[0]) >= 60:
                interval = int(argv.pop(0))
            elif option == "--pid" and argv and argv[0].isdigit():
                only = (only or set()) | {int(argv.pop(0))}
            elif option in ("-h", "--help"):
                print(__doc__)
                return 0
            else:
                raise SweepError(f"unknown or incomplete option {option}")
        if as_plist:
            print(plist(live, dev, interval, directory), end="")
            return 0
        if live:
            _state.require_own(directory, "a live sweep")
        lock_path = directory / "sweep.lock"
        handle = None
        if _state.foreign_owner(directory) is None:
            directory.mkdir(parents=True, exist_ok=True)
            handle = open(lock_path, "a")
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                print("slotfather sweep: another sweep is running", file=sys.stderr)
                return 0
        try:
            if live and not logged:
                raise SweepError("a live sweep is always logged")
            report = sweep(directory, live, dev, only, logged)
        finally:
            if handle is not None:
                handle.close()
    except (SweepError, _config.SlotError) as error:
        print(f"slotfather sweep: {error}\n{USAGE}", file=sys.stderr)
        return 2
    if as_json:
        print(json.dumps(report, sort_keys=True))
    else:
        output = text(report, quiet)
        if output:
            print(output)
    if report["live_refused"]:
        print(f"slotfather sweep: {report['settings_error']}; the live sweep stopped nothing (fix it with "
              f"`slotfather sweep config ...` or `config reset`)", file=sys.stderr)
        return 3
    return 1 if report["failed"] else 0
