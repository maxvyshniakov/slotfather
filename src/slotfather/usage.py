"""Slotfather usage responsibilities."""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
from pathlib import Path
import signal
import sys
import threading
import time

from . import config as _config
from . import platform as _platform
from . import state as _state


class TreeUsage:
    """CPU and memory of one command's process tree, from snapshots of the process table.

    A process is the lease's while it descends from the command (attached), and stays the lease's after its parent
    exits and launchd adopts it (detached); a child of launchd started during the lease joins it when its
    environment carries the lease (`marked`). CPU time of every process an attached parent reaps reaches the
    command's own rusage at its exit, so it is not summed here; a detached process is reaped by launchd, so its CPU
    is the last sample of its own time (Linux: init adopts and reaps, as launchd does; an orphan adopted by another
    child subreaper, such as `systemd --user`, is followed only once seen in the tree). Processes born and gone
    between two samples outside the attached tree are not seen, nor is a child that exits unwaited-for before its parent exits (launchd reaps that zombie). Resident
    memory is the sum over the tree's processes (shared pages count once per process)."""

    def __init__(self, root: int, since: float, marked=None, uid: int | None = None):
        self.root, self.since = root, since
        self.marked = marked or (lambda pids: set())
        self.uid = os.getuid() if uid is None else uid
        self.root_start = None
        self.root_gone = False  # set once wait4 has reaped the command: its pid may then be another process
        self.members = {}  # (pid, start) -> [own CPU seconds at the last sample, attached]
        self.checked = set()  # (pid, start) of launchd children already considered
        self.detached_gone = 0.0
        self.processes = self.samples = 0
        self.rss_peak = 0  # KB
        self.rss_area = self.span = 0.0  # KB x seconds, seconds
        self.last = None  # (moment, resident KB) of the previous sample

    def candidates(self, table: dict) -> list[int]:
        """Children of launchd started during the lease that have not been considered yet; each is returned once,
        for `marked` to say whether it carries the lease."""
        fresh = []
        for pid, row in table.items():
            key = (pid, row[1])
            if row[0] == 1 and row[2] == self.uid and key not in self.members and key not in self.checked:
                self.checked.add(key)
                begun = _platform.started_at(row[1])
                if begun is not None and begun >= self.since - 1:
                    fresh.append(pid)
        return fresh

    def observe(self, moment: float, table: dict, adopted: set[int] | None = None) -> None:
        """Takes in one process table. `adopted` are the candidates() of this table that carry the lease; without
        it observe asks `marked` itself."""
        if adopted is None:
            fresh = self.candidates(table)
            adopted = self.marked(fresh) if fresh else set()
        for key in list(self.members):
            row = table.get(key[0])
            if row is None or row[1] != key[1]:
                cpu, attached = self.members.pop(key)
                if not attached:
                    self.detached_gone += cpu
        children = {}
        for pid, row in table.items():
            children.setdefault(row[0], []).append(pid)

        def tree(roots) -> set[int]:
            found, stack = set(), [p for p in roots if p in table]
            while stack:
                pid = stack.pop()
                if pid not in found:
                    found.add(pid)
                    stack.extend(children.get(pid, ()))
            return found

        if self.root_start is None and self.root in table and not self.root_gone:
            self.root_start = table[self.root][1]
        attached = set() if self.root_gone or self.root not in table or table[self.root][1] != self.root_start \
            else tree([self.root])
        adopted = {pid for pid in adopted if pid in table}
        detached = tree([pid for pid, _ in self.members if pid not in attached] + list(adopted)) - attached
        resident = 0
        for pid in attached | detached:
            row = table[pid]
            key = (pid, row[1])
            if key not in self.members:
                self.processes += 1
            self.members[key] = [row[4], pid in attached]
            resident += row[3]
        if self.last is not None:
            self.rss_area += self.last[1] * (moment - self.last[0])
            self.span += moment - self.last[0]
        self.last = (moment, resident)
        self.rss_peak = max(self.rss_peak, resident)
        self.samples += 1

    def detached(self) -> float:
        """CPU seconds of processes that left the attached tree: gone, and still running at the last sample."""
        return self.detached_gone + sum(cpu for cpu, attached in self.members.values() if not attached)

    def result(self, reaped_cpu: float, reaped_peak_kb: int = 0, wall: float | None = None) -> dict:
        """Usage for the history, once the command has exited and the last sample was taken after its exit:
        `reaped_cpu` and `reaped_peak_kb` come from the command's rusage."""
        detached = self.detached()
        mean = self.rss_area / self.span if self.span > 0 else (self.last[1] if self.last else 0)
        found = {"cpu": round(reaped_cpu + detached, 2), "cpu_detached": round(detached, 2),
                 "rss_peak_mb": round(max(self.rss_peak, reaped_peak_kb) / 1024, 1),
                 "rss_mean_mb": round(mean / 1024, 1), "processes": self.processes, "samples": self.samples}
        if wall is not None:
            found["wall"] = round(wall, 2)
        return found


class Watch(threading.Thread):
    """The holder's sampler: looks at the command's tree, first after SAMPLE_FIRST seconds and then half as long
    again each time up to SLOTFATHER_SAMPLE_MAX, and samples the machine when no holder has lately. A fault here
    never reaches the lease: the history then lacks what could not be measured."""

    def __init__(self, usage: TreeUsage, directory: Path):
        super().__init__(daemon=True, name="slotfather-watch")
        self.usage, self.directory = usage, directory
        try:
            self.most = max(0.05, float(_config.setting("SAMPLE_MAX") or _config.SAMPLE_MAX))
        except ValueError:
            self.most = _config.SAMPLE_MAX
        self.stopped = threading.Event()
        self.lock = threading.Lock()

    def observe(self) -> None:
        """One look at the tree. The ps calls run outside the lock, so finish() never waits on a slow ps for it."""
        moment, table = time.time(), _platform.usage_table()
        with self.lock:
            fresh = self.usage.candidates(table)
        adopted = self.usage.marked(fresh) if fresh else set()
        with self.lock:
            self.usage.observe(moment, table, adopted)

    def look(self) -> None:
        with contextlib.suppress(Exception):
            self.observe()
        with contextlib.suppress(Exception):
            sample_machine(self.directory)

    def run(self) -> None:
        # Signals for the holder (forwarded to its command) must interrupt the holder's wait4, not this thread.
        with contextlib.suppress(AttributeError, OSError, ValueError):
            signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGTERM, signal.SIGHUP, signal.SIGINT})
        interval = min(_config.SAMPLE_FIRST, self.most)
        while not self.stopped.wait(interval):
            self.look()
            interval = min(interval * 1.5, self.most)

    def finish(self, rusage, wall: float) -> dict | None:
        """Stops sampling, takes the last look (after the command exited) and returns the usage. Waits at most
        WATCH_JOIN seconds for the sampler, and skips the last look when it is still stuck: the release goes on."""
        self.usage.root_gone = True
        self.stopped.set()
        self.join(timeout=_config.WATCH_JOIN)
        if not self.is_alive():
            with contextlib.suppress(Exception):
                self.observe()
        if rusage is None or not self.lock.acquire(timeout=_config.WATCH_JOIN):
            return None
        try:
            peak = rusage.ru_maxrss // 1024 if sys.platform == "darwin" else rusage.ru_maxrss  # bytes on macOS
            return self.usage.result(rusage.ru_utime + rusage.ru_stime, peak, wall)
        except Exception:  # noqa: BLE001 - measuring must never cost the lease its release
            return None
        finally:
            self.lock.release()


def machine_facts(directory: Path) -> dict:
    """One machine sample: load, cumulative CPU ticks (busy is the change between two samples), memory pressure,
    swap and the slots held."""
    held = [e for _, e in _state.entries(directory) if e.get("state") == "held"]
    linux_facts = _platform.linux_memory() if _platform.linux() else None
    if linux_facts is None:
        swap = _platform.swap_usage()
        level = _platform.pressure_level()
        free = _platform.sysctl("kern.memorystatus_level")
        free = int(free) if free and free.isdigit() else None
    else:  # Linux: free memory is MemAvailable's share; no pressure level, PSI instead
        swap = None if linux_facts["swap_total"] is None else (linux_facts["swap_total"], linux_facts["swap_free"] or 0)
        level = None
        free = round(100 * linux_facts["available"] / linux_facts["total"]) \
            if linux_facts["available"] is not None and linux_facts["total"] else None
    item = {"v": _config.MACHINE_VERSION, "t": _config.now(), "at": round(time.time(), 2),
            "load": [round(v, 2) for v in os.getloadavg()], "ticks": _platform.cpu_ticks(), "cores": _config.cores(),
            "pressure": level, "memory_free_pct": free,
            "swap_total_mb": None if swap is None else round(swap[0]),
            "swap_free_mb": None if swap is None else round(swap[1]),
            "held": sum(e["count"] for e in held), "leases": len(held)}
    if linux_facts is not None:
        item.update(memory_available_mb=None if linux_facts["available"] is None else round(linux_facts["available"]),
                    psi_some=linux_facts["psi_some"], psi_full=linux_facts["psi_full"])
        if linux_facts["cgroup"]:
            item.update(cgroup=linux_facts["cgroup"], cgroup_max_mb=round(linux_facts["cgroup_max"]),
                        cgroup_available_mb=None if linux_facts["cgroup_available"] is None
                        else round(linux_facts["cgroup_available"]))
    with contextlib.suppress(_config.SlotError):
        item["capacity"] = _config.total()
    return item


def sample_machine(directory: Path) -> bool:
    """Appends a machine sample unless one was taken in the last SLOTFATHER_MACHINE_EVERY seconds; holders share
    the duty through machine.jsonl's age and a lock of its own, never the queue's."""
    path = directory / "machine.jsonl"
    try:
        every = float(_config.setting("MACHINE_EVERY") or _config.MACHINE_EVERY)
    except ValueError:
        every = _config.MACHINE_EVERY

    def due() -> bool:
        try:
            return time.time() - path.stat().st_mtime >= every
        except FileNotFoundError:
            return True
    if not due():
        return False
    with open(directory / ".machine.lock", "a") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        if not due():
            return False
        _state.append_bounded(path, json.dumps(machine_facts(directory)) + "\n", _config.MACHINE_MAX)
        return True
