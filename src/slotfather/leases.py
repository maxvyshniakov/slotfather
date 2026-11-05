"""Slotfather leases responsibilities."""
from __future__ import annotations

import contextlib
import json
import math
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
from typing import Any, cast

from . import config as _config
from . import platform as _platform
from . import query as _query
from . import schedule as _schedule
from . import state as _state


def reap(directory: Path, table: dict) -> list[tuple[Path, dict]]:
    """Live entries in queue order; removes those whose holder exited or crashed (in another user's queue it only
    leaves them out)."""
    live = []
    readonly = _state.foreign_owner(directory) is not None
    for path, entry in _state.entries(directory):
        if _platform.alive(entry, table):
            live.append((path, entry))
        elif not readonly:
            path.unlink(missing_ok=True)
            _state.marker(path).unlink(missing_ok=True)
            _state.log(directory, f"reaped {entry.get('state')} lease of {entry.get('count')} for "
                           f"{entry.get('label')} (pid {entry.get('pid')} exited)")
            _state.record(directory, entry, "reaped")
    return live


def legacy_holders(table: dict) -> list[tuple[Path, dict, int]]:
    """Held leases with a live holder in the earlier queues: (path, entry, slots here). Read only: their own
    clients reap them."""
    found = []
    for directory in _config.legacy_dirs():
        for path, entry in _state.entries(directory):
            if entry.get("state") == "held" and _platform.alive(entry, table):
                found.append((path, entry, min(_config.total(), math.ceil(entry["count"] * _config.total() / _config.LEGACY_TOTAL))))
    return found


def legacy_used(table: dict) -> int:
    return sum(count for _, _, count in legacy_holders(table))


def resume(entry: dict, moment: float | None = None) -> None:
    """A yielded lease that was granted again (at `moment`, else its `granted` stamp): `granted` is its first grant
    again (waits and runs in the history stay its own), `resumed` this grant, `yielded` the seconds it spent back in
    the queue."""
    away = _state.epoch(entry.get("yielded_at"))
    if away is None:
        return
    again = entry.get("granted")
    back = moment if moment is not None else _config.seconds(again) or time.time()
    entry["yielded"] = round(float(entry.get("yielded") or 0) + max(0.0, back - away), 2)
    entry.update(resumed=again, granted=entry.get("first_granted") or again)
    for key in ("yielded_at", "first_granted", "rank"):
        entry.pop(key, None)


def ask(directory: Path, live: list[tuple[Path, dict]], held: list[dict], waiters: list[dict],
        estimate, other: int, settings: dict) -> None:
    """Writes the yield requests yield_asks() wants and withdraws the others. Under the queue lock. `settings` are
    the saved ones without this client's environment, so that every client's look asks alike (a waiter whose
    client asks later or never says so on its entry)."""
    paths = {id(e): path for path, e in live}
    wanted = _schedule.yield_asks(waiters, held, _config.total(), time.time(), estimate, other, settings,
                        asked=[e for e in held if _state.marker(paths[id(e)]).exists()])
    asks = {id(h): w for h, w in wanted}
    for path, entry in live:
        place = _state.marker(path)
        if id(entry) in asks:
            who = asks[id(entry)]
            if place.exists():
                continue
            _state.write(place, {"by": paths[id(who)].stem, "label": who.get("label"), "priority": _config.priority_of(cast(dict[str, Any], who)),
                          "count": who["count"], "at": _config.now()})
            _state.log(directory, f"asked {entry['label']} (pid {entry['pid']}) to yield {entry['count']} for "
                           f"{who.get('label')}")
        elif place.exists():
            place.unlink(missing_ok=True)
            _state.log(directory, f"withdrew the yield request to {entry['label']} (pid {entry['pid']})")
    for place in (directory / "queue").glob("*.yield"):  # requests left by entries gone meanwhile
        if not place.with_suffix(".json").exists():
            place.unlink(missing_ok=True)


def yield_lease(path: Path, out=sys.stderr) -> bool:
    """Yields the held lease at `path` if it was asked to and the waiter that asked still waits: the lease goes back
    among the waiters just behind that waiter, the queue grants at once, and this returns True when the lease is
    held again. Returns False at once when nothing asked."""
    directory = path.parent.parent
    with _state.locked(directory):
        try:
            entry = json.loads(path.read_text())
        except (OSError, ValueError):
            raise _config.SlotError("the lease vanished from the queue") from None
        request = _state.asked(path)
        if entry.get("state") != "held" or request is None:
            return False
        live = reap(directory, _platform.processes())
        asker = next((e for p, e in live if p.stem == request.get("by") and e.get("state") != "held"), None)
        settings = _schedule.order_settings(directory, environment=False)
        behind = _schedule.rank_time(asker, settings) + _config.YIELD_BEHIND if asker is not None else None
        if asker is not None:  # would the asker start? Things may have changed since it asked.
            table = _platform.processes()
            held = [e for p, e in live if e.get("state") == "held" and p != path]
            waiters = [e for p, e in live if e.get("state") != "held"] + [dict(entry, state="waiting", rank=behind)]
            trial = _schedule.plan(waiters, held, _config.total(), time.time(), estimate=_schedule.estimator(directory),
                         other=legacy_used(table), settings=settings)
            if not any(e is asker for e in trial.grants):
                asker = None
        if asker is None:  # it started, left or would still not start; the next look would withdraw the request
            _state.marker(path).unlink(missing_ok=True)
            _state.log(directory, f"withdrew the yield request to {entry.get('label')} (pid {entry.get('pid')})")
            return False
        entry.update(state="waiting", yields=int(entry.get("yields") or 0) + 1, yielded_at=round(time.time(), 3),
                     first_granted=entry.get("granted"), rank=behind)
        _state.write(path, entry)
        _state.marker(path).unlink(missing_ok=True)
        _state.log(directory, f"yielded {entry['count']} ({path.name}) to {asker.get('label')}")
        grant(directory, _platform.processes())
    print(f"slot: {entry.get('label')} yielded {entry['count']} slot(s) to {asker.get('label')}; it resumes when "
          f"they are free", file=out, flush=True)
    held_again(path)
    print(f"slot: {entry.get('label')} resumes with {entry['count']} slot(s)", file=out, flush=True)
    return True


def held_again(path: Path) -> None:
    """Waits until the yielded lease at `path` is granted again, looking at the queue as a waiter does."""
    directory = path.parent.parent
    poll = float(_config.setting("POLL") or 2)
    poll_max = max(poll, float(_config.setting("POLL_MAX") or _config.POLL_MAX))
    interval, look = poll, 0.0
    while True:
        if time.monotonic() >= look:
            with _state.locked(directory):
                grant(directory, _platform.processes())
                try:
                    entry = json.loads(path.read_text())
                except (OSError, ValueError):
                    raise _config.SlotError("the lease vanished from the queue") from None
                if entry.get("state") == "held":
                    if "yielded_at" in entry:  # an older client granted it
                        resume(entry)
                        _state.write(path, entry)
                    return
            interval = min(interval * 1.5, poll_max)
            look = time.monotonic() + interval
        try:
            if json.loads(path.read_text()).get("state") == "held":
                look = 0.0  # held: settle it under the lock at once
                continue
        except (OSError, ValueError):
            raise _config.SlotError("the lease vanished from the queue") from None
        time.sleep(min(poll, max(0.0, look - time.monotonic())))


def grant(directory: Path, table: dict) -> str | None:
    """Grant the waiters plan() starts, in their order, while memory allows; then ask yieldable holders to yield for
    interactive waiters that still wait (yield_asks()). Returns why the first that does not start still waits."""
    live = reap(directory, table)
    held = [e for _, e in live if e["state"] == "held"]
    waiters = [e for _, e in live if e["state"] != "held"]
    paths = {id(e): path for path, e in live}
    estimate = _schedule.estimator(directory)
    other = legacy_used(table)
    slots = _config.total()
    found = _schedule.plan(_schedule.fit(waiters, slots), held, slots, time.time(), estimate=estimate, other=other,
                 settings=_schedule.order_settings(directory))
    used = sum(e["count"] for e in held) + other
    short = None
    for entry in found.grants:
        if used:
            short = short or _platform.memory_short()
            if short:
                return short
        entry.update(state="held", granted=_config.now())
        resume(entry, time.time())
        _state.write(paths[id(entry)], entry)
        used += entry["count"]
        _state.log(directory, f"granted {entry['count']} to {entry['label']} (pid {entry['pid']})")
    rest = [e for e in waiters if not any(e is g for g in found.grants)]
    with contextlib.suppress(OSError, _config.SlotError):
        ask(directory, live, held + found.grants, rest, estimate, other, _schedule.order_settings(directory, environment=False))
    return found.reason


class Lease:
    """A held lease, an inherited one, or none (bypass). Release it once the work has finished."""

    def __init__(self, count: int, path: Path | None = None, inherited: bool = False, yieldable: bool = False):
        self.count, self.path, self.inherited, self.yieldable = count, path, inherited, yieldable
        self.status = None  # the command's exit status, for the history, where the holder knows it
        self.usage = None  # what the command's process tree used (TreeUsage.result), where the holder measured it

    def environment(self) -> dict[str, str]:
        if self.path is None:
            return {}
        env = {}
        for prefix in _config.LEASE_ENV_PREFIXES:
            env.update({prefix + "LEASE": str(self.path), prefix + "COUNT": str(self.count)})
            if self.yieldable and not self.inherited:
                env[prefix + "YIELD"] = str(_state.marker(self.path))
        return env

    def yield_requested(self) -> dict | None:
        """The yield request for this lease, or None: {"by", "label", "class", "count", "at"} of the waiter that
        asked. Cheap (one stat and a small read); call it between steps."""
        return None if self.path is None or self.inherited else _state.asked(self.path)

    def yield_slots(self, out=sys.stderr) -> bool:
        """Yields the lease if it was asked to: back among the waiters, just behind the waiter that asked, until it is
        granted again; returns True then, False at once when nothing asked (or the asker no longer waits). Call it
        only between steps, with none of the lease's own work running."""
        if self.path is None or self.inherited:
            return False
        return yield_lease(self.path, out=out)

    def release(self) -> None:
        if self.path is not None and not self.inherited:
            directory = self.path.parent.parent
            with _state.locked(directory):
                try:
                    entry = json.loads(self.path.read_text())
                except (OSError, ValueError):
                    entry = None
                self.path.unlink(missing_ok=True)
                _state.log(directory, f"released {self.count} ({self.path.name})")
                _state.marker(self.path).unlink(missing_ok=True)
                if isinstance(entry, dict):
                    ran = entry.get("state") == "held" or bool(entry.get("yields"))  # a yielded lease ran
                    _state.record(directory, entry, "released" if ran else "left", self.status, self.usage)
                # Hand the slots on now: waiters look at the queue less often the longer they wait.
                with contextlib.suppress(_config.SlotError, OSError):
                    grant(directory, _platform.processes())
            self.path = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.release()


def inherited_lease(table: dict) -> tuple[Path, dict, int] | None:
    """The held lease this process runs under, here or in an earlier queue: (path, entry, slots here)."""
    mine = _platform.ancestors(table, os.getpid())
    own = _config.setting("LEASE")
    for path, entry in _state.entries(_config.state_dir()):
        mine_now = entry.get("state") == "held" or "yielded_at" in entry  # a yielded lease is still this tree's
        if mine_now and _platform.alive(entry, table) and (own == str(path) or entry["pid"] in mine):
            return path, entry, entry["count"]
    for path, entry, count in legacy_holders(table):
        if own == str(path) or entry["pid"] in mine:
            return path, entry, count
    return None


def inherited(table: dict) -> Lease | None:
    found = inherited_lease(table)
    if found is None:
        return None
    path, entry, count = found
    if "yielded_at" in entry:
        held_again(path)  # it yielded: what runs under it waits until it is granted again
    # A lease in an earlier queue is honoured but not handed on: nested runs find its holder among their ancestors.
    return Lease(count, path if path.parent.parent == _config.state_dir() else None, inherited=True)


def policy() -> str:
    value = _config.setting("POLICY") or "normal"
    if value not in _config.POLICIES:
        raise _config.SlotError(f"invalid SLOTFATHER_POLICY {value!r}: use {', '.join(_config.POLICIES)}")
    return value


def acquire(count: int | str, label: str, command: str = "", out=sys.stderr, wait: float | None = None,
            note: str = "", meta: dict[str, str] | None = None, yieldable: bool = False, *,
            priority: str | None = None, ticket: str | int | None = None) -> Lease:
    """Wait in the queue for `count` slots (at most the total); the lease is held by this process until released.
    Raises SlotTimeout, having left the queue, when `wait` seconds pass first. `note` follows the first word that
    the request waits. `meta` (key -> text) goes on the queue entry and the history line; a process that inherits
    a lease keeps that lease's metadata and adds none. A `yieldable` lease may be asked to yield for interactive
    waiters (see Lease.yield_slots(); the README's client protocol); it never is otherwise."""
    if count != "all" and (type(count) is not int or count < 1):
        raise _config.SlotError("count must be a positive integer or all")
    meta = _config.checked_meta(meta)
    if priority is not None and priority not in _config.PRIORITIES:
        raise _config.SlotError(f"priority is one of {', '.join(_config.PRIORITIES)}")
    selected_priority = priority if priority is not None else "normal"
    selected_ticket = _config.checked_ticket(ticket)
    if policy() == "bypass":
        return Lease(_config.total())
    directory = _config.state_dir()
    table = _platform.processes()
    found = inherited(table)
    if found:
        return found
    poll = float(_config.setting("POLL") or 2)
    poll_max = max(poll, float(_config.setting("POLL_MAX") or _config.POLL_MAX))
    me = os.getpid()
    try:
        cwd = os.getcwd()
    except OSError:
        cwd = ""
    _state.require_own(directory, "a lease")
    with _state.locked(directory):
        # Admission and capacity changes share this lock; resolve all only now (and again at the grant: plan()).
        whole = count == "all"
        count = _config.total() if whole else min(count, _config.total())
        sequence_file = directory / "sequence"
        try:
            sequence = int(sequence_file.read_text() or 0) + 1
        except (OSError, ValueError):
            sequence = 1
        sequence_file.write_text(str(sequence))
        path = directory / "queue" / f"{sequence:010d}-{me}.json"
        entry: dict[str, Any] = {"pid": me, "start": table.get(me, (0, ""))[1], "count": count, "label": label, "cmd": command,
                 "cwd": cwd, "since": _config.now(), "state": "waiting", "priority": selected_priority}
        if selected_ticket is not None:
            entry["ticket"] = selected_ticket
        if whole:
            entry["all"] = True  # the capacity at the grant; older clients read past fields they do not know
        if meta:
            entry["meta"] = meta  # older clients read past fields they do not know
        if yieldable:
            entry["yieldable"] = True
        if wait is not None:
            # When this client gives up (plan(): a head that would give up before its start holds nobody back).
            entry["until"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + wait))
        own = _schedule.order_settings(directory)["yield_after"]
        if own != _schedule.order_settings(directory, environment=False)["yield_after"]:
            entry["yield_after"] = own  # this client asks later or never (SLOTFATHER_YIELD_AFTER); others' looks read it
        _state.write(path, entry)
    lease = Lease(count, path, yieldable=yieldable)
    deadline = None if wait is None else time.monotonic() + wait
    # A terminated waiter leaves the queue at once rather than at the next reap.
    main_thread = threading.current_thread() is threading.main_thread()
    previous_term = signal.signal(signal.SIGTERM, terminate) if main_thread else None
    try:
        said, interval, look = None, poll, 0.0
        while True:
            if time.monotonic() >= look:
                # A full look runs ps over the machine under the queue lock. While the reason to wait stays the same,
                # each pause is half as long again, up to poll_max: a release grants at once, so only reaping a
                # crashed holder and memory recovering wait for a look.
                with _state.locked(directory):
                    waiting = grant(directory, _platform.processes())
                interval = poll if waiting != said else min(interval * 1.5, poll_max)
                look = time.monotonic() + interval
            try:
                current = json.loads(path.read_text())  # written whole by rename: no lock needed
                state = current["state"]
            except (OSError, ValueError, KeyError, TypeError):
                raise _config.SlotError("the lease vanished from the queue") from None
            if state == "held":
                if isinstance(current.get("count"), int) and current["count"] >= 1:
                    count = lease.count = current["count"]  # the capacity changed while it waited (plan())
                if said:
                    print(f"slot: granted {count} slot(s) to {label}", file=out, flush=True)
                return lease
            if deadline is not None and time.monotonic() >= deadline:
                raise _config.SlotTimeout(f"no {count} slot(s) within {wait:g} s: {waiting or 'earlier requests first'}")
            if waiting != said:
                print(f"slot: {label} waits for {count} slot(s): {waiting or 'earlier requests first'}",
                      file=out, flush=True)
                for line in _query.status_lines(directory):
                    print(f"  {line}", file=out, flush=True)
                if note and said is None:
                    print(f"slot: {note}", file=out, flush=True)
                said = waiting
            pause = min(poll, max(0.0, look - time.monotonic()))
            time.sleep(pause if deadline is None else max(0.0, min(pause, deadline - time.monotonic())))
    except BaseException:
        lease.release()
        raise
    finally:
        if previous_term is not None:
            signal.signal(signal.SIGTERM, previous_term)


def terminate(signum, _frame):
    raise SystemExit(128 + signum)


def run(count: int | str, label: str, argv: list[str], wait: float | None = None, meta: dict[str, str] | None = None,
        yieldable: bool = False, *, priority: str | None = None, ticket: str | int | None = None) -> int:
    """Run argv under a lease of `count` slots; the command's exit status."""
    from . import usage as _usage
    try:
        background = policy() == "background"
        lease = acquire(count, label, " ".join(argv), wait=wait, note=_config.ALL_NOTE if count == "all" else "", meta=meta,
                        yieldable=yieldable, priority=priority, ticket=ticket)
    except _config.SlotTimeout as error:
        print(f"slot: {label}: {error}", file=sys.stderr)
        return _config.TIMED_OUT
    except KeyboardInterrupt:
        return 130
    except SystemExit as stop:
        return int(stop.code or 1)
    with lease:
        if background:
            prefix = ["/usr/sbin/taskpolicy", "-b", "/usr/bin/nice", "-n", "10"] if os.access(
                "/usr/sbin/taskpolicy", os.X_OK) else ["/usr/bin/nice", "-n", "10"]
            argv = prefix + argv
        try:
            child = subprocess.Popen(argv, env={**os.environ, **lease.environment()})
        except OSError as error:
            print(f"slot: {error}", file=sys.stderr)
            return 127
        began = time.monotonic()
        watch = None
        if lease.path is not None and not lease.inherited:
            marker = str(lease.path)
            watch = _usage.Watch(_usage.TreeUsage(child.pid, time.time() - 1, lambda pids: _platform.carrying(pids, marker)),
                          lease.path.parent.parent)
            with contextlib.suppress(RuntimeError):
                watch.start()
        reaped = []

        def forward(signum, _frame):
            # os.kill rather than child.send_signal, which polls and could reap the child before wait4 sees it;
            # until wait4 returns the child (a zombie at worst) keeps its pid.
            if not reaped:
                with contextlib.suppress(ProcessLookupError):
                    os.kill(child.pid, signum)
        # The terminal sends SIGINT to the child's process group itself; the lease outlives the child.
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        for signum in (signal.SIGTERM, signal.SIGHUP):
            signal.signal(signum, forward)
        try:
            _, raw, rusage = os.wait4(child.pid, 0)  # the rusage holds every descendant reaped inside the tree
            status = os.waitstatus_to_exitcode(raw)
        except ChildProcessError:
            status, rusage = child.wait(), None
        reaped.append(True)
        if watch is not None:
            watch.usage.root_gone = True  # before any later look: the pid may be reused from now on
        child.returncode = status
        if watch is not None and watch.is_alive():
            lease.usage = watch.finish(rusage, time.monotonic() - began)
        lease.status = 128 - status if status < 0 else status
    return lease.status
