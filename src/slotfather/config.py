"""Slotfather config responsibilities."""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
from typing import Mapping, cast
import re

from . import platform as _platform


MIN_PYTHON = (3, 9)


DEFAULT_DIR = "~/.local/state/slotfather"


DEFAULT_CGROUP = None  # install.py --cgroup writes the queue's cgroup here, so every client of the queue names it


DEFAULT_LEGACY_DIRS = "~/.local/state/straylight-verification/slots"


LEGACY_TOTAL = 4  # the earlier queue's fixed slot count


CORES_PER_SLOT = 3


AUTO_GIB_PER_SLOT = 4.0


CAPACITY_MODES = ("memory", "lower")  # saved in capacity.json beside "slots", which older clients read alone


MIN_FREE_SWAP_MB = 512


MAX_PRESSURE_LEVEL = 1  # kern.memorystatus_vm_pressure_level: 1 normal, 2 warning, 4 critical


MIN_AVAILABLE_MB = 1024


MAX_PSI_SOME = 10.0


MAX_PSI_FULL = 5.0


POLICIES = ("normal", "background", "bypass")


TIMED_OUT = 75  # EX_TEMPFAIL


POLL_MAX = 10  # seconds between a long waiter's full looks at the queue


OVERTAKE_WINDOW = 600  # seconds a waiter that does not fit lets later requests that fit start first


QUEUE_ORDERS = ("backfill", "window")  # `slotfather order`; the first is the default


ORDER_DEFAULTS = {"order": "backfill", "safety": 3.0, "limit": 2700.0, "fallback": 3600.0, "overrun": 0.5,
                  "interactive": 1800.0, "batch": 1800.0, "yield_after": 120.0}


ORDER_KNOBS = {"safety": ("BACKFILL_SAFETY", 1.0, "up"), "limit": ("BACKFILL_LIMIT", 0.0, "limit"),
               "fallback": ("BACKFILL_FALLBACK", 0.0, "up"), "overrun": ("BACKFILL_OVERRUN", 0.0, "down"),
               "interactive": ("PRIORITY_INTERACTIVE", 0.0, "down"), "batch": ("PRIORITY_BATCH", 0.0, "down"),
               "yield_after": ("YIELD_AFTER", 0.0, "delay")}


ORDER_BASE = ("order", "safety", "limit", "fallback", "overrun")  # always saved; the rest only when not the default


CLASSES = ("interactive", "normal", "batch")  # `run --class`, or the lease metadata's `class`; normal by default


YIELDS_MAX = 2  # times one lease is asked to yield


ASKERS = ("interactive",)  # the classes whose waiters ask (the replay found nothing in letting normal ones ask)


YIELD_BEHIND = 0.001  # seconds behind the waiter it yields to that a yielded lease queues


SHELLS = ("sh", "bash", "zsh", "dash", "fish")


ESTIMATE_RUNS = 20  # recent runs of a kind its estimate takes the median of


ESTIMATE_BYTES = 1 << 20  # of the history's end (and of its rotated part) read for estimates


UNFINISHED = ("reaped", "left")  # history outcomes whose time is no run of their kind: left out of the estimates


ESTIMATES_VERSION = 2  # estimates.json: 2 leaves UNFINISHED out; a file without it (or older) is worked out again


HISTORY_MAX = 4 << 20  # bytes of history.jsonl; past it the file becomes history.jsonl.1, replacing the older one


HISTORY_VERSION = 2


MACHINE_VERSION = 1  # machine.jsonl lines


MACHINE_MAX = 4 << 20  # bytes of machine.jsonl before it becomes machine.jsonl.1


MACHINE_EVERY = 15  # seconds between machine samples while any lease runs


SAMPLE_FIRST = 0.5  # seconds before a holder's first look at its command's process tree


SAMPLE_MAX = 10  # longest interval between those looks; each is half as long again as the one before


PS_TIMEOUT = 5  # seconds a ps or sysctl of the sampler may take before its answer is given up


WATCH_JOIN = 5  # seconds a release waits for the sampler to stop, and for its lock; then it goes without


ALL_NOTE = ("-n all holds every later request behind it until the whole machine is free; use it only for work that "
            "needs the machine to itself, such as a performance measurement, and otherwise ask for the slots the "
            "command uses")


LEASE_ENV_PREFIXES = ["SLOTFATHER_"]


META_KEY = re.compile(r"[a-z][a-z0-9_.-]{0,31}")


META_VALUE_MAX = 200


META_KEYS_MAX = 16


class SlotError(Exception):
    pass


class SlotTimeout(SlotError):
    pass


def setting(name: str, default: str | None = None) -> str | None:
    """SLOTFATHER_<name>, read when needed; a client that loads this file may replace this function to add its own
    spellings (tools/slot_lease.py)."""
    value = os.environ.get("SLOTFATHER_" + name)
    return default if value is None else value


def state_dir() -> Path:
    return Path(os.path.expanduser(setting("DIR") or DEFAULT_DIR))


def cores() -> int:
    value = setting("CORES") or ""
    return int(value) if value.isdigit() and int(value) > 0 else os.cpu_count() or 1


def memory_slots(gib: float | None, per_slot: float) -> int | None:
    """One slot per `per_slot` GiB of `gib`, at least one; None when the memory is unknown. The memory counts in whole
    GiB, rounded: Linux's MemTotal is a little under the size a VM was given (11.7 for 12)."""
    if gib is None:
        return None
    return max(1, math.floor(round(gib) / per_slot + 1e-9))


def capacity_basis() -> dict:
    """The capacity and how it was found: {"slots", "saved", "mode", "cores", "cpu_slots", "memory_gib",
    "gib_per_slot", "memory_slots"}. Mode "fixed" is a saved count, "memory" one slot per gib_per_slot GiB, "lower"
    the lower of the CPU and memory budgets, "automatic" the lower at AUTO_GIB_PER_SLOT; atomic publication keeps
    readers consistent."""
    path = state_dir() / "capacity.json"
    machine = cores()
    cpu = max(1, machine // CORES_PER_SLOT)
    found = {"saved": True, "cores": machine, "cpu_slots": cpu, "memory_gib": None, "gib_per_slot": None,
             "memory_slots": None, "cgroup": None}
    try:
        data = json.loads(path.read_text())
        value = data["slots"]
    except FileNotFoundError:
        data, value = {"mode": "automatic", "gib_per_slot": AUTO_GIB_PER_SLOT}, None
        found["saved"] = False
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise SlotError(f"cannot read capacity from {path}: {error}") from None
    invalid = SlotError(f"invalid saved capacity in {path}; use slotfather slots reset while the queue is idle")
    if value is not None and (type(value) is not int or value < 1):
        raise invalid
    mode = data.get("mode", "fixed")
    if mode == "fixed":
        return {**found, "slots": value, "mode": mode}
    per_slot = data.get("gib_per_slot")
    if mode not in CAPACITY_MODES + ("automatic",) or type(per_slot) not in (int, float) or not per_slot > 0:
        raise invalid
    group = data.get("cgroup") if isinstance(data.get("cgroup"), str) else None
    gib = _platform.memory_gib(group)
    by_memory = memory_slots(gib, per_slot)
    found.update(mode=mode, gib_per_slot=per_slot, memory_slots=by_memory,
                 memory_gib=None if gib is None else round(gib, 1), cgroup=group or _platform.memory_cgroup())
    if by_memory is None:  # memory unknown: the count saved with the mode, or the CPU budget
        slots = value or cpu
    else:
        slots = by_memory if mode == "memory" else min(cpu, by_memory)
    return {**found, "slots": slots}


def automatic_capacity() -> int:
    """The capacity with nothing saved: the lower of the CPU budget and one slot per AUTO_GIB_PER_SLOT GiB."""
    by_memory = memory_slots(_platform.memory_gib(), AUTO_GIB_PER_SLOT)
    cpu = max(1, cores() // CORES_PER_SLOT)
    return cpu if by_memory is None else min(cpu, by_memory)


def capacity() -> tuple[int, bool]:
    """The shared saved capacity, or the automatic default: (slots, saved)."""
    found = capacity_basis()
    return found["slots"], found["saved"]


def capacity_text(found: dict) -> str:
    """`slotfather slots`' line: "N slots (saved)", "N slots (automatic)", with the budgets where memory counts."""
    slots, mode = found["slots"], found["mode"]
    if mode == "fixed":
        return f"{slots} slots (saved)"
    memory = f"{found['memory_gib']:g} GiB" if found["memory_gib"] is not None else "unknown memory"
    if found.get("cgroup") and found["memory_gib"] is not None and not setting("MEMORY_GIB"):
        memory += f" in {found['cgroup']}"
    per = f"one per {found['gib_per_slot']:g} GiB of {memory}"
    cpu = f"{found['cpu_slots']} by {found['cores']} cores"
    if mode == "automatic":
        if found["memory_slots"] is None or found["memory_slots"] >= found["cpu_slots"]:
            return f"{slots} slots (automatic)"
        return f"{slots} slots (automatic, by memory: {per}; {cpu})"
    if mode == "memory":
        return f"{slots} slots (saved: {per})"
    return f"{slots} slots (saved: the lower of {cpu} and {found['memory_slots'] or '?'} by memory, {per})"


def total() -> int:
    return capacity()[0]


def overtake_window() -> float:
    """SLOTFATHER_OVERTAKE_WINDOW in seconds; a value that is not a number serves strictly in order."""
    try:
        return max(0.0, float(setting("OVERTAKE_WINDOW") or OVERTAKE_WINDOW))
    except ValueError:
        return 0.0


def legacy_dirs() -> list[Path]:
    value = setting("LEGACY_DIRS", DEFAULT_LEGACY_DIRS)
    own = state_dir().resolve()
    paths = [Path(os.path.expanduser(p)) for p in value.split(":") if p]
    return [p for p in paths if p.resolve() != own]


def now() -> str:
    import datetime
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def seconds(stamp) -> float | None:
    """A UTC queue timestamp as epoch seconds, including earlier unpadded spellings."""
    if not isinstance(stamp, str):
        return None
    import datetime
    try:
        if len(stamp) == 20 and stamp[4] == stamp[7] == "-" and stamp[10] == "T" and stamp[13] == stamp[16] == ":" and stamp[-1] == "Z":
            return datetime.datetime.fromisoformat(stamp[:-1]).replace(tzinfo=datetime.timezone.utc).timestamp()
        # Old clients/readers accepted unpadded months and days. Keep that fallback.
        return datetime.datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(
            tzinfo=datetime.timezone.utc).timestamp()
    except ValueError:
        return None


def duration(value: float) -> str:
    value = int(value)
    if value >= 3600:
        return f"{value // 3600}h{value % 3600 // 60:02d}m"
    return f"{value // 60}m{value % 60:02d}s" if value >= 60 else f"{value}s"


def number_setting(name: str, default: float) -> float:
    try:
        return float(setting(name) or default)
    except ValueError:
        return default


_host_port = None


def checked_meta(meta: object) -> dict[str, str]:
    """Lease metadata, validated: raises SlotError for a key that is not a short lowercase word, a value that is
    not short printable text, or too many keys."""
    if meta is None:
        return {}
    if not isinstance(meta, dict):
        raise SlotError("meta must be a mapping of key to value")
    if len(meta) > META_KEYS_MAX:
        raise SlotError(f"at most {META_KEYS_MAX} --meta keys")
    found = {}
    for key, value in cast(dict[object, object], meta).items():
        if not isinstance(key, str) or not META_KEY.fullmatch(key):
            raise SlotError(f"--meta key {key!r} must be a lowercase word (a-z, 0-9, _ . -), at most 32 characters")
        if not isinstance(value, str) or len(value) > META_VALUE_MAX or not value.isprintable():
            raise SlotError(f"--meta {key} takes printable text of at most {META_VALUE_MAX} characters")
        if key == "class" and value not in CLASSES:
            raise SlotError(f"the class is one of {', '.join(CLASSES)}, not {value!r}")
        found[key] = value
    return found


def parse_meta(pairs: list[str]) -> dict[str, str]:
    """`key=value` words as lease metadata; a later key replaces an earlier one."""
    found = {}
    for pair in pairs:
        key, equals, value = pair.partition("=")
        if not equals:
            raise SlotError(f"--meta takes key=value, not {pair!r}")
        found[key] = value
    return checked_meta(found)


def meta_of(entry: Mapping[str, object]) -> dict[str, str]:
    """An entry's or history line's metadata: {} where it has none (an older client) or it is not a mapping of
    text, as a foreign writer could leave."""
    meta = entry.get("meta")
    if not isinstance(meta, dict):
        return {}
    return {k: str(v) for k, v in cast(dict[object, object], meta).items()
            if isinstance(k, str) and META_KEY.fullmatch(k) and isinstance(v, (str, int, float))}


def meta_text(meta: dict) -> str:
    """Metadata for one line: key=value pairs joined by commas, a value quoted where it holds a space, comma, quote
    or equals sign."""
    plain = re.compile(r'[^\s,="]+')
    return ",".join(f"{k}={v if plain.fullmatch(v) else json.dumps(v)}" for k, v in meta.items())
