"""slotfather: one queue of requests for the machine's capacity, shared by every project and agent (#599).

  slotfather run [-n N|all] [--label L] [--meta K=V]... [--class C] [--yieldable] [--wait S] [--] COMMAND...
                                                                     run COMMAND holding N slots (default 1)
  slotfather yield                                                   inside a yieldable lease: yield if asked
  slotfather status [--json [--pid PID]]                             running and waiting requests, in queue order
  slotfather procs --json [--cwd PATH] [--pid PID]                  read-only process, lease and session ancestry
  slotfather version [--json]                                        identify this runtime source
  slotfather history [N]                                             the last N requests that left the queue
  slotfather stats [--since W] [--label L] [--by B] [--json]         request resource use and machine occupancy
  slotfather slots [N|memory G|lower G|reset]                        show, save or reset shared capacity
  slotfather available [--label L] [-- COMMAND...]                   queue space before memory checks
  slotfather advise [--max N] [--exclusive] [--label L] [-- CMD...]  a recommended worker budget, as JSON
  slotfather order [backfill|window] [--safety X] [--limit S] ...    show, save or reset the queue's order
  slotfather sweep [--live [--dev-servers]] [--json] | config | --plist  find (and stop) processes left behind
  slotfather [dashboard [--interval S | --once]]                     live view of capacity, leases and queue

Run heavy builds and tests through `slotfather run -n <k> -- ...`: a test suite or build that keeps the
machine busy takes 1 to 2 slots, a full gate that must not share the machine takes `-n all`.

Slots: one per three cores, at least one, and no more than one per 4 GiB of memory, so
`total = min(max(1, cores // 3), max(1, memory_GiB // 4))`: 4 on a 12-core M2 Max with 32 GiB, 2 on a 6-core VM with
12 GiB (on Linux the memory is the limit of the queue's cgroup where it has one: SLOTFATHER_CGROUP, the one
install.py --cgroup wrote in, the one saved with the capacity, or the caller's outermost limited one). A request
above a capacity that shrank takes every slot at its grant, and -n all the capacity then. `slotfather slots N` saves shared capacity; `slots memory G` one slot per G GiB of memory, `slots lower G` the
lower of that and the CPU budget (#653); `slotfather slots reset` restores the automatic default. SLOTFATHER_CORES
replaces the core count, SLOTFATHER_MEMORY_GIB the memory. A request above the total takes every slot.

Requests are served in arrival order. A waiter that does not fit lets later requests that fit start before it until
it has waited SLOTFATHER_OVERTAKE_WINDOW seconds (default 600). Past that it is the head (#643, the backfill order):
a later request that fits still starts first if, by the run times of earlier requests of its kind in the history,
three times its expected run ends before the head can start (when the held leases are expected to end) and before
the head has waited 2700 s, its limit; past the limit nothing passes it. An all-slot request therefore starts at
most the limit plus the overrun of what passed it, or, with classes, once what ranks ahead of it has run: the
requests that came within the class offsets after it (up to 3600 s for a batch one), and their overrun. `slotfather order` shows and saves the order and these numbers
for every client (order.json beside the queue); a client's environment can only tighten them, and
SLOTFATHER_QUEUE=window falls back to the earlier order, where the head holds back everyone behind it. Classes
(`--class interactive|normal|batch`, normal by default): an interactive waiter queues as if it came 1800 s earlier,
a batch one 1800 s later; an interactive waiter that has waited 120 s asks holders of a lower class that took
`--yieldable` to yield, by a marker file their client checks between steps (SLOTFATHER_YIELD) before it runs
`slotfather yield`; nothing ever stops a holder. A process
that already runs under a lease (SLOTFATHER_LEASE, or a holder among its ancestors) runs without a second lease, so
nested wrappers neither deadlock nor count twice. A lease whose holder died (its pid gone, or reused by a process
with another start time) is reaped by the next queue operation. While memory is short (free swap under SLOTFATHER_MIN_FREE_SWAP_MB, default 512, where swap is on; on
macOS the kernel's memory-pressure level at warning or above; on Linux MemAvailable under SLOTFATHER_MIN_AVAILABLE_MB,
default 1024, or the queue's cgroup's memory.max less its working set under that, or PSI memory avg10, the cgroup's
or else the machine's, at SLOTFATHER_MAX_PSI_SOME percent, default 10, or _FULL, default 5), new leases
wait for running ones; with nothing running they start. A waiter checks its own entry every SLOTFATHER_POLL seconds (default 2) but looks over the queue, which runs
ps, less often the longer nothing changes, up to SLOTFATHER_POLL_MAX (default 10); a release hands its slots on at
once. `--wait S` gives up after S seconds with exit 75 and leaves the queue. `run` exits with the command's status
(128 + the signal when a signal ended it).

`advise` sizes work for processes that take no lease: with each slot `cores / total` cores, the current load,
min(load1, load5), less what the held slots account for, max(0, load - held x cores per slot), is load without a
lease. Not load15: it still holds work whose leases ended minutes ago, and min() ignores a one-minute spike.
The worker budget is the slots left after that load (and after the held slots, unless --exclusive: an all-slot
request waits for them), a slot counting while at least half its cores are idle, never above --max (default
the total) nor, without --exclusive, the free slots, and at least 1. SLOTFATHER_LOADAVG replaces the load
averages: "load1 load5 load15", or one number for all three.

State: ~/.local/state/slotfather (SLOTFATHER_DIR replaces it; another user's queue is read only: changes are
refused and reading creates nothing; queue.log records grants, releases and
reaps; history.jsonl, which `history` lists, has a line per request that left the queue: its command, directory,
wait, run and outcome, and for `run` what its process tree used; machine.jsonl samples the machine while leases
run; `stats` reads both). While its command runs, the holder looks at the command's process tree every
SLOTFATHER_SAMPLE_MAX seconds at most (default 10; the first look comes sooner): CPU time is the command's own
rusage, which holds every descendant reaped inside the tree, plus the sampled CPU of processes that left the tree
(orphans, and launchd children carrying the lease's SLOTFATHER_LEASE). Held leases in the queues listed in
SLOTFATHER_LEGACY_DIRS (colon-separated; default Straylight's earlier ~/.local/state/straylight-verification/slots;
empty to ignore) count as used, scaled from their 4 slots, until their holders exit. SLOTFATHER_POLICY: normal
(default), background (low QoS), or bypass (no lease; for a person who knows the machine is idle). Standard library
only, Python 3.9 or later: install.py puts thin entry points in ~/.local/bin and preserves their complete package in an immutable release."""
from __future__ import annotations

import contextlib
import json
import math
import os
import sys
from typing import Any

from . import config as _config


def companion(name: str, argv: list[str], title: str) -> int:
    """Load only the requested view; admission never imports optional terminal or cleanup code."""
    from importlib import import_module
    try:
        module = import_module(f"slotfather.{name}")
    except ModuleNotFoundError as error:
        if error.name != f"slotfather.{name}":
            raise
        raise _config.SlotError(f"{title} is not installed") from None
    return module.main(argv)



def dashboard(argv: list[str]) -> int:
    return companion("dashboard", argv, "the dashboard")


USAGE = ("usage: slotfather                     (in a terminal: the live dashboard)\n"
         "       slotfather run [-n N|all] [--label L] [--meta KEY=VALUE]... [--class interactive|normal|batch]\n"
         "                      [--yieldable] [--wait SECONDS] [--] COMMAND...      (inside a lease: slotfather yield)\n"
         "       slotfather status [--json [--pid PID]] | history [N] | available [--label L] [-- COMMAND...]\n"
         "       slotfather advise [--max N] [--exclusive] [--label L] [-- COMMAND...]\n"
         "       slotfather order [backfill|window] [--safety X] [--limit S] [--fallback S] [--overrun X]\n"
         "                        [--interactive S] [--batch S] [--yield-after S] | reset\n"
         "       slotfather stats [--since WHEN] [--label GLOB] [--by both|label|kind] [--top N] [--json]\n"
         "       slotfather slots [N|memory GIB|lower GIB|reset]\n"
         "       slotfather procs --json [--cwd PATH] [--pid PID] | version [--json]\n"
         "       slotfather sweep [--live [--dev-servers]] [--json] [--quiet] [--pid PID]... | sweep config [KEY VALUE]...\n"
         "                        | sweep --plist [--live [--dev-servers]] [--interval SECONDS]\n"
         "       slotfather dashboard [--interval SECONDS] | dashboard --once [--width N] [--height N] [--color]")


def parse_count(value: str) -> int:
    if value == "all":
        return _config.total()
    if not value.isdigit() or int(value) < 1:
        raise _config.SlotError(f"-n takes a positive count or all, not {value!r}")
    return int(value)


def run_command(argv: list[str]) -> int:
    from . import leases as _leases
    count, label, wait, pairs, yieldable = 1, os.path.basename(os.getcwd()), None, [], False
    while argv:
        if argv[0] in ("-n", "--slots") and len(argv) > 1:
            count, argv = ("all" if argv[1] == "all" else parse_count(argv[1])), argv[2:]
        elif argv[0] in ("-l", "--label") and len(argv) > 1:
            label, argv = argv[1], argv[2:]
        elif argv[0] == "--meta" and len(argv) > 1:
            pairs, argv = pairs + [argv[1]], argv[2:]
        elif argv[0] == "--class" and len(argv) > 1:
            if argv[1] not in _config.CLASSES:
                raise _config.SlotError(f"--class takes {', '.join(_config.CLASSES)}, not {argv[1]!r}")
            pairs, argv = pairs + [f"class={argv[1]}"], argv[2:]
        elif argv[0] == "--yieldable":
            yieldable, argv = True, argv[1:]
        elif argv[0] == "--wait" and len(argv) > 1:
            try:
                wait = float(argv[1])
            except ValueError:
                raise _config.SlotError(f"--wait takes seconds, not {argv[1]!r}") from None
            argv = argv[2:]
        elif argv[0] == "--":
            argv = argv[1:]
            break
        elif argv[0].startswith("-"):
            raise _config.SlotError(f"unknown option {argv[0]}")
        else:
            break
    if not argv:
        raise _config.SlotError("no command to run")
    return _leases.run(count, label, argv, wait, _config.parse_meta(pairs), yieldable)


def request_options(argv: list[str], what: str, flags: dict) -> tuple[dict, str, str]:
    """`flags` (option -> takes a value) and then `[--label L] [-- COMMAND...]`: (options, label, command). The
    label defaults to the directory's name, as for run."""
    found, label, command = {}, os.path.basename(os.getcwd()), ""
    while argv:
        if argv[0] == "--":
            command = " ".join(argv[1:])
            break
        if argv[0] in ("-l", "--label") and len(argv) > 1:
            label, argv = argv[1], argv[2:]
        elif argv[0] in flags and (not flags[argv[0]] or len(argv) > 1):
            found[argv[0]] = argv[1] if flags[argv[0]] else True
            argv = argv[2:] if flags[argv[0]] else argv[1:]
        else:
            raise _config.SlotError(f"unknown {what} option {argv[0]}")
    return found, label, command


def yield_command(argv: list[str]) -> int:
    """`slotfather yield`, from inside a yieldable lease between two steps: yields if asked and returns once the
    lease is held again; returns at once when nothing asked. Exit 0 either way."""
    from . import leases as _leases
    from . import platform as _platform
    from . import state as _state
    if argv:
        raise _config.SlotError("yield takes no arguments")
    _state.require_own(_config.state_dir(), "yield")
    found = _leases.inherited_lease(_platform.processes())
    if found is None or found[0].parent.parent != _config.state_dir():
        raise _config.SlotError("yield runs inside a lease of this queue (SLOTFATHER_LEASE, or a holder among its ancestors)")
    if not _leases.yield_lease(found[0]):
        print("slot: no yield requested", file=sys.stderr)
    return 0


def status_command(argv: list[str]) -> int:
    """`status --json [--pid PID]`."""
    from . import query as _query
    pid = None
    if argv[:1] == ["--pid"] and len(argv) == 2 and argv[1].isdigit():
        pid = int(argv[1])
    elif argv:
        raise _config.SlotError("status takes --json and then optionally --pid PID")
    print(json.dumps(_query.status_json(pid=pid), sort_keys=True))
    return 0


def advise_command(argv: list[str]) -> int:
    from . import query as _query
    found, label, command = request_options(argv, "advise", {"--max": True, "--exclusive": False})
    maximum = parse_count(found["--max"]) if "--max" in found else None
    print(json.dumps(_query.advise(maximum, "--exclusive" in found, label=label, command=command), sort_keys=True))
    return 0


def available_command(argv: list[str]) -> int:
    from . import query as _query
    _, label, command = request_options(argv, "available", {})
    print(_query.available(label=label, command=command))
    return 0


def order_line(found: dict) -> str:
    return (f"{found['order']}: safety {found['safety']:g}, limit {found['limit']:g} s, fallback "
            f"{found['fallback']:g} s, overrun {found['overrun']:g}; interactive {found['interactive']:g} s ahead, "
            f"batch {found['batch']:g} s behind, yield after {found['yield_after']:g} s "
            f"({'saved' if found['saved'] else 'default'})")


def order_command(argv: list[str]) -> int:
    """`slotfather order [backfill|window] [--safety X] [--limit S] [--fallback S] [--overrun X] | reset`."""
    from . import schedule as _schedule
    from . import state as _state
    directory = _config.state_dir()
    changes: dict[str, Any] = {}
    reset = False
    while argv:
        word = argv.pop(0)
        if word in _config.QUEUE_ORDERS:
            changes["order"] = word
        elif word == "reset" and not changes:
            reset = True
        elif word.startswith("--") and word[2:].replace("-", "_") in _config.ORDER_KNOBS and argv:
            try:
                changes[word[2:].replace("-", "_")] = float(argv.pop(0))
            except ValueError:
                raise _config.SlotError(f"{word} takes a number") from None
        else:
            raise _config.SlotError(f"order takes backfill or window, --safety, --limit, --fallback, --overrun, "
                            f"--interactive, --batch, --yield-after, or reset; not {word!r}")
    if reset and changes:
        raise _config.SlotError("order reset takes nothing else")
    if changes or reset:
        _state.require_own(directory, "an order change")
        with _state.locked(directory):
            saved = dict(_config.ORDER_DEFAULTS)  # what is saved, without this client's environment
            with contextlib.suppress(OSError, ValueError, TypeError, AttributeError):
                saved.update(_schedule.checked_order(json.loads((directory / "order.json").read_text())))
            values = None if reset else {**saved, **changes}
            try:
                _state.save_order(values, directory)
            except ValueError as error:
                raise _config.SlotError(str(error)) from None
            except OSError as error:
                raise _config.SlotError(f"cannot save the order: {error}") from None
    saved = _schedule.order_settings(directory, environment=False)
    moved = [k for k in _config.ORDER_DEFAULTS if k not in _config.ORDER_BASE and saved[k] != _config.ORDER_DEFAULTS[k]]
    if (changes or reset) and moved:
        print(f"slotfather: note: order.json now holds {', '.join(moved)}, which slotfather 3e3a621 and older do not "
              f"know: their looks fall back to the window order until they are updated", file=sys.stderr)
    print(order_line(_schedule.order_settings(directory)))
    return 0


def slots_value(argv: list[str]) -> dict | None:
    """What `slots` saves: {"slots": N}, {"mode", "gib_per_slot", "slots"} or None for reset. The memory modes also
    save the count they give now as "slots", which slotfather 0af7546 and older read alone."""
    from . import platform as _platform
    if len(argv) == 1 and argv[0] == "reset":
        return None
    if len(argv) == 1 and argv[0].isascii() and argv[0].isdigit() and int(argv[0]) >= 1:
        return {"slots": int(argv[0])}
    if len(argv) == 2 and argv[0] in _config.CAPACITY_MODES:
        try:
            per_slot = float(argv[1])
        except ValueError:
            per_slot = 0.0
        if math.isfinite(per_slot) and per_slot > 0:
            per_slot = int(per_slot) if per_slot.is_integer() else per_slot
            # The cgroup is the saver's (SLOTFATHER_CGROUP, or its outermost limited one), not the one saved before.
            group = (_config.setting("CGROUP") or _config.DEFAULT_CGROUP or _platform.limited_cgroup()) if _platform.linux() else None
            if _platform.linux() and _platform.cgroup_limit(group) is None:
                print("slotfather: note: no cgroup with a memory limit here or in SLOTFATHER_CGROUP: the count "
                      "follows MemTotal, which a VM's balloon moves and which differs from what clients inside a "
                      "limited cgroup see; name the cgroup the leases share (SLOTFATHER_CGROUP=/a.slice/b.slice, "
                      "or install.py --cgroup)", file=sys.stderr)
            by_memory = _config.memory_slots(_platform.memory_gib(group or ""), per_slot)
            if by_memory is None:
                raise _config.SlotError("the machine's memory is unknown here; save a count instead (slotfather slots N)")
            cpu = max(1, _config.cores() // _config.CORES_PER_SLOT)
            found = {"slots": by_memory if argv[0] == "memory" else min(cpu, by_memory), "mode": argv[0],
                     "gib_per_slot": per_slot}
            if _platform.cgroup_limit(group) is not None:
                found["cgroup"] = group
            return found
    raise _config.SlotError("slots takes a positive integer, memory GIB or lower GIB (GiB per slot), or reset")


def slots_command(argv: list[str]) -> int:
    from . import leases as _leases
    from . import platform as _platform
    from . import state as _state
    directory = _config.state_dir()
    if argv:
        _state.require_own(directory, "a capacity change")
    value = slots_value(argv) if argv else None
    with _state.locked(directory):
        if argv:
            table = _platform.processes()
            live = _leases.reap(directory, table)
            legacy_live = any(_platform.alive(entry, table) for old in _config.legacy_dirs() for _, entry in _state.entries(old))
            if live or legacy_live:
                raise _config.SlotError("queue is busy; capacity can change only after every holder and waiter leaves")
            path = directory / "capacity.json"
            try:
                if value is None:
                    path.unlink(missing_ok=True)
                else:
                    _state.write(path, value)
            except OSError as error:
                raise _config.SlotError(f"cannot save capacity: {error}") from None
        found = _config.capacity_basis()
    print(_config.capacity_text(found))
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    command, rest = (argv[0], argv[1:]) if argv else ("", [])
    # A person typing just `slotfather` wants to see the queue; a script or agent without a terminal gets the usage.
    if not argv and sys.stdin.isatty() and sys.stdout.isatty():
        command = "dashboard"
    try:
        if command == "version":
            from .version import info, text
            if rest == ["--json"]:
                print(json.dumps(info(), sort_keys=True))
            elif not rest:
                print(text())
            else:
                raise _config.SlotError("version takes only --json")
            return 0
        if command == "procs":
            from .procs import command as procs_command
            return procs_command(rest)
        if command == "run":
            return run_command(rest)
        if command == "slots":
            return slots_command(rest)
        if command == "status" and not rest:
            from . import query as _query
            print("\n".join(_query.status_lines()))
            return 0
        if command == "status" and rest[:1] == ["--json"]:
            return status_command(rest[1:])
        if command == "yield":
            return yield_command(rest)
        if command == "history" and (not rest or (len(rest) == 1 and rest[0].isdigit())):
            from . import state as _state
            print("\n".join(_state.history_lines(int(rest[0]) if rest else 20)))
            return 0
        if command == "available":
            return available_command(rest)
        if command == "order":
            return order_command(rest)
        if command == "advise":
            return advise_command(rest)
        if command == "dashboard":
            return dashboard(rest)
        if command == "stats":
            return companion("stats", rest, "slotfather-stats")
        if command == "sweep":
            return companion("sweep", rest, "slotfather-sweep")
        if command in ("-h", "--help", "help"):
            print(__doc__)
            return 0
    except _config.SlotError as error:
        print(f"slotfather: {error}\n{USAGE}", file=sys.stderr)
        return 2
    print(USAGE, file=sys.stderr)
    return 2
