"""Slotfather query responsibilities."""
from __future__ import annotations

import math
import sys
from pathlib import Path
from typing import Any, cast

from . import config as _config
from . import leases as _leases
from . import platform as _platform
from . import schedule as _schedule
from . import state as _state


def snapshot(directory: Path | None = None) -> tuple[list[dict], list[tuple[Path, dict, int]], dict]:
    """The live queue entries, the earlier queues' holders and the process table."""
    directory = directory or _config.state_dir()
    table = _platform.processes()
    with _state.locked(directory):
        live = [entry for _, entry in _leases.reap(directory, table)]
    return live, _leases.legacy_holders(table), table


def status_lines(directory: Path | None = None) -> list[str]:
    directory = directory or _config.state_dir()
    table = _platform.processes()
    with _state.locked(directory):
        live = _leases.reap(directory, table)
    legacy = _leases.legacy_holders(table)
    lines = []
    for path, e in live:
        meta = _config.meta_of(e)
        tags = f" meta={_config.meta_text(meta)}" if meta else ""
        if e.get("yieldable"):
            request = _state.asked(path) if e.get("state") == "held" else None
            tags += " yieldable" + (f" asked-to-yield-for={request.get('label')}" if request else "") + \
                (f" yielded={e['yields']}" if e.get("yields") else "")
        lines.append(f"{e['state']}: {e['count']} slot(s) {e['label']} pid={e['pid']} since={e['since']}{tags} "
                     f"cmd={e['cmd']}")
    lines += [f"legacy: {count} slot(s) {e.get('label')} pid={e.get('pid')} (earlier queue {path.parent.parent})"
              for path, e, count in legacy]
    used = sum(e["count"] for _, e in live if e["state"] == "held") + sum(count for _, _, count in legacy)
    return lines + [f"{used} of {_config.total()} slots held"]


def status_processes(directory: Path, pid: int | None = None) -> dict[int, tuple[int, str]]:
    """The legacy identity format, using the native adapter only when every holder is observable."""
    if sys.platform == "darwin":
        from .macos_process import basic_snapshot
        sources = [directory, *_config.legacy_dirs()]
        required = {entry["pid"] for source in sources for _, entry in _state.entries(source)
                    if type(entry.get("pid")) is int}
        if pid is not None:
            required.add(pid)
        try:
            table = basic_snapshot(required)
            # Legacy clients use their own lock: a lease may appear during this observation.
            if any(entry["pid"] in table and not table[entry["pid"]][1]
                   for source in sources for _, entry in _state.entries(source) if type(entry.get("pid")) is int):
                return _platform.processes()
            return table
        except OSError:
            pass
    return _platform.processes()


def status_json(directory: Path | None = None, pid: int | None = None) -> dict:
    """The queue for programs (`status --json`): every live entry with its lease id (the entry's name), the lease
    path (what SLOTFATHER_LEASE holds), holder pid and process group, its live process tree and its metadata. With
    `pid`, only the entries that process belongs to, each with `via`: "tree" (the holder or a descendant) or "env"
    (it left the tree, by a double fork or setsid and launchd adopting it, but its environment carries the lease's
    SLOTFATHER_LEASE; macOS shows that only for the user's own processes that are not Apple platform binaries), and
    `query`: the pid and its start time as `ps -o lstart=` gives it (None: not running), for a guard to check again
    that it is the same process before it acts."""
    directory = directory or _config.state_dir()
    with _state.locked(directory):
        table = status_processes(directory, pid)
        live = _leases.reap(directory, table)
    entries_found: list[dict[str, Any]] = []
    for path, entry in live:
        tree = _platform.descendants(table, entry.get("pid"))
        priority = _config.priority_of(cast(dict[str, Any], entry))
        item: dict[str, Any] = {"id": path.stem, "lease": str(path), "state": entry.get("state"), "count": entry.get("count"),
                "label": entry.get("label"), "pid": entry.get("pid"), "pgid": _platform.process_group(entry.get("pid")),
                "start": entry.get("start"), "since": entry.get("since"), "granted": entry.get("granted"),
                "cmd": entry.get("cmd"), "cwd": entry.get("cwd"), "meta": _config.meta_of(entry), "tree": tree,
                "priority": priority, "ticket": _config.ticket_of(cast(dict[str, Any], entry)),
                "yieldable": bool(entry.get("yieldable")),
                "yields": int(entry.get("yields") or 0), "yield_asked": _state.asked(path), "position": None}
        entries_found.append(item)
    settings = _schedule.order_settings(directory)
    serving = _schedule.ranked([e for _, e in live if e.get("state") != "held"], settings) \
        if settings["order"] == "backfill" else [e for _, e in live if e.get("state") != "held"]
    places = {id(e): n for n, e in enumerate(serving, 1)}
    for (path, entry), item in zip(live, entries_found):
        item["position"] = places.get(id(entry))  # waiters: their place in the order the queue serves them
    legacy = []
    for path, entry, count in _leases.legacy_holders(table):
        legacy.append({"id": path.stem, "lease": str(path), "state": "held", "count": count,
                       "label": entry.get("label"), "pid": entry.get("pid"), "pgid": _platform.process_group(entry.get("pid")),
                       "tree": _platform.descendants(table, entry.get("pid"))})
    held = sum(e["count"] for e in entries_found if e["state"] == "held") + sum(e["count"] for e in legacy)
    found = {}
    if pid is not None:
        from .procs import environment_leases
        candidates = [item for item in entries_found if pid not in item["tree"]]
        marked, _ = environment_leases([pid] if pid in table else [], candidates)
        mine = []
        for item in entries_found:
            if pid in item["tree"]:
                mine.append(dict(item, via="tree"))
            elif item["lease"] in marked.get(pid, set()):
                mine.append(dict(item, via="env"))
        entries_found = mine
        legacy = [dict(e, via="tree") for e in legacy if pid in e["tree"]]
        found["query"] = {"pid": pid, "start": table[pid][1] if pid in table else None}
    slots = _config.total()
    return {"v": 2, "slots": slots, "held": held, "free": max(0, slots - held), "entries": entries_found,
            "legacy": legacy, **found}


def available(directory: Path | None = None, label: str = "", command: str = "") -> int:
    """How many slots a new request fits by queue order, before memory checks: the free ones less what waiters take first, or none while a
    waiter past the overtaking window holds back everyone behind it, unless (backfill) the request is expected to
    end before that waiter can start. Without `command` its run counts as unknown (the fallback)."""
    directory = directory or _config.state_dir()
    live, legacy, _ = snapshot(directory)
    held = [e for e in live if e["state"] == "held"]
    other = sum(count for _, _, count in legacy)
    return _schedule.room([e for e in live if e["state"] != "held"], max(0, _config.total() - sum(e["count"] for e in held) - other),
                held, _schedule.estimator(directory), other, label=label, command=command)


def size(maximum: int, cores: int, total: int, averages: tuple[float, float, float], held: int,
         waiting: bool = False, exclusive: bool = False) -> dict:
    """A job count for work that runs beside `held` leased slots (or after them, when `exclusive`) under load
    `averages` (1, 5 and 15 minutes) that include processes taking no lease. The module docstring has the formula."""
    per_slot = cores / total
    load1, load5, load15 = averages
    load = min(load1, load5)
    unleased = max(0.0, load - held * per_slot)
    busy = unleased + (0.0 if exclusive else held * per_slot)
    capacity = max(0, math.floor((cores - busy) / per_slot + 0.5))
    free = total if exclusive else 0 if waiting else max(0, total - held)
    worker_budget = max(1, min(maximum, capacity, free))
    return {"jobs": worker_budget, "maximum": maximum, "exclusive": exclusive, "cores": cores, "total": total,
            "cores_per_slot": round(per_slot, 2), "held": held, "waiting": waiting, "free": free,
            "load": round(load, 2), "load1": round(load1, 2), "load5": round(load5, 2), "load15": round(load15, 2),
            "unleased_load": round(unleased, 2), "capacity": capacity}


def advise(maximum: int | None = None, exclusive: bool = False, cores_now: int | None = None,
           averages: tuple[float, float, float] | None = None, label: str = "", command: str = "") -> dict:
    """size() for the queue as it is now: slots held by others (the caller's own lease is not one) and
    whether a request waits, a request for `command` under `label` when given (else one of unknown run), with
    "run_estimate", the run the history expects of it (None: unknown, counted as the fallback)."""
    live, legacy, table = snapshot()
    mine = _leases.inherited_lease(table)
    holders = [e for e in live if e["state"] == "held" and not (mine and e["pid"] == mine[1]["pid"])]
    other = sum(count for path, e, count in legacy if not (mine and path == mine[0]))
    held = sum(e["count"] for e in holders) + other
    basis = _config.capacity_basis()
    slots = basis["slots"]
    waiters = [e for e in live if e["state"] != "held"]
    estimate = _schedule.estimator(_config.state_dir())
    waiting = bool(waiters) and _schedule.room(waiters, max(0, slots - held), holders, estimate, other, label=label,
                                     command=command) == 0
    found = size(maximum or slots, cores_now or _config.cores(), slots, averages or _platform.loads(), min(held, slots), waiting,
                 exclusive)
    found["run_estimate"] = estimate({"label": label, "cmd": command}) if command else None
    # How the capacity was found (#653), and why new leases would wait for memory now (None: they would not); the
    # job count does not shrink for it: a caller that starts memory-heavy workers should.
    found.update(capacity_mode=basis["mode"], gib_per_slot=basis["gib_per_slot"], memory_gib=basis["memory_gib"],
                 memory_short=_platform.memory_short())
    return found
