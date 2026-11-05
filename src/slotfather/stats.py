#!/usr/bin/env python3
"""slotfather-stats: request resource use and machine occupancy; `slotfather stats` runs it.

  slotfather stats [--since WHEN] [--label GLOB] [--by both|label|kind|meta.KEY] [--top N] [--json]

Reads history.jsonl (one line per request that left the queue; version 2 lines carry what a `run` used) and
machine.jsonl (samples of the machine while leases ran), each with its rotated .1 part. WHEN is an age (90m, 36h,
7d) or a local date or time (2026-11-02, 2026-11-02T15:00); GLOB matches labels (fnmatch). Requests group by label
and by command kind, the program and its first argument (`bun run-browser-checks.mjs`, `verify.sh --checks [95]`),
or by one key of the lease metadata (`--by meta.ticket`; requests without that key group under "-"):
per group the median and 90th percentile of wait and run, and the cores used per slot held, CPU seconds over wall
seconds times slots. The machine part gives the share of sampled time the one-minute load stood over the cores
(more runnable threads than cores) and under them, the CPU busy share, and how often a slot's worth of cores sat
idle. The sweeps (sweep.jsonl, `slotfather sweep`) add what was found and stopped. It only reads; it lives apart from the queue like the dashboard and loads the slotfather beside it.
"""
from __future__ import annotations

import datetime
import fnmatch
import json
from pathlib import Path
import re
import sys
import time


from . import config as _config
from . import schedule as _schedule
from . import state as _state


GAP = 60.0  # seconds; a longer gap between machine samples means no lease ran, and counts as this long
USAGE = "usage: slotfather stats [--since WHEN] [--label GLOB] [--by both|label|kind|meta.KEY] [--top N] [--json]"


class StatsError(Exception):
    pass


def percentile(values: list[float], q: float) -> float | None:
    """The q-th percentile (0-100) by linear interpolation, or None for no values."""
    ordered = sorted(v for v in values if v is not None)
    if not ordered:
        return None
    rank = (len(ordered) - 1) * q / 100
    low = int(rank)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (rank - low)


kind = _schedule.kind  # the command kind the queue's run-time estimates use too


def parse_since(text: str, moment: float | None = None) -> float:
    """An age (90m, 36h, 7d) before now, or a local ISO date or time, as epoch seconds."""
    moment = time.time() if moment is None else moment
    age = re.fullmatch(r"(\d+(?:\.\d+)?)([smhd])", text.strip())
    if age:
        return moment - float(age[1]) * {"s": 1, "m": 60, "h": 3600, "d": 86400}[age[2]]
    try:
        value = datetime.datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
    except ValueError:
        raise StatsError(f"--since takes an age like 36h or 7d or a date like 2026-11-02, not {text!r}") from None
    return value.timestamp()  # a naive value is local time


def wall_of(item: dict) -> float | None:
    """Seconds the lease held its slots: its wall (else its run) less the time it yielded them back (#643)."""
    value = item.get("wall")
    if not isinstance(value, (int, float)):
        value = item.get("ran")
    if not isinstance(value, (int, float)):
        return None
    away = item.get("yielded")
    return max(0.0, float(value) - (float(away) if isinstance(away, (int, float)) else 0.0))


def measured(item: dict) -> bool:
    return isinstance(item.get("cpu"), (int, float)) and bool(wall_of(item))


def usage_of(items: list[dict]) -> dict:
    """Waits, runs and use of a set of requests."""
    ran = [i for i in items if i.get("granted") and isinstance(i.get("ran"), (int, float))]
    counted = [i for i in items if measured(i)]
    cpu = sum(i["cpu"] for i in counted)
    wall = sum(wall_of(i) for i in counted)
    slot_wall = sum(wall_of(i) * (i.get("count") or 1) for i in counted)
    per_lease = [i["cpu"] / wall_of(i) for i in counted]
    found = {
        "requests": len(items),
        "released": sum(i.get("outcome") == "released" for i in items),
        "left": sum(i.get("outcome") == "left" for i in items),
        "reaped": sum(i.get("outcome") == "reaped" for i in items),
        "failed": sum(i.get("outcome") == "released" and i.get("status") not in (0, None) for i in items),
        "wait_p50": percentile([i.get("waited") for i in items], 50),
        "wait_p90": percentile([i.get("waited") for i in items], 90),
        "run_p50": percentile([i["ran"] for i in ran], 50),
        "run_p90": percentile([i["ran"] for i in ran], 90),
        "slots_p50": percentile([i.get("count") for i in items if isinstance(i.get("count"), int)], 50),
        "slot_hours": round(sum(i["ran"] * (i.get("count") or 1) for i in ran) / 3600, 3),
        "measured": len(counted),
        "cpu_hours": round(cpu / 3600, 3),
        "cores": round(cpu / wall, 2) if wall else None,
        "cores_p90": None if not per_lease else round(percentile(per_lease, 90), 2),
        "cores_per_slot": round(cpu / slot_wall, 2) if slot_wall else None,
        "rss_peak_mb": max((i["rss_peak_mb"] for i in counted if isinstance(i.get("rss_peak_mb"), (int, float))),
                           default=None),
        "rss_mean_mb_p50": percentile([i.get("rss_mean_mb") for i in counted
                                       if isinstance(i.get("rss_mean_mb"), (int, float))], 50),
    }
    return found


def recorded_capacity(items: list[dict], current: int) -> int:
    """The capacity most requests saw when they left (version 2 lines record it), else the current one."""
    seen = [i["capacity"] for i in items if isinstance(i.get("capacity"), int) and i["capacity"] > 0]
    return int(percentile(seen, 50)) if seen else current


def held_time(items: list[dict], since: float | None, until: float, capacity: int) -> dict:
    """Slots held over time, from the grant and end of every request that ran: while any lease ran, the mean held
    and the share of that time every slot was held."""
    steps = []
    for item in items:
        start, end = _config.seconds(item.get("granted")), _config.seconds(item.get("ended"))
        if start is None or end is None or end <= start:
            continue
        start = max(start, since) if since is not None else start
        if end > start:
            steps += [(start, item.get("count") or 1), (min(end, until), -(item.get("count") or 1))]
    steps.sort()
    busy = full = area = 0.0
    level, previous = 0, None
    for moment, delta in steps:
        if previous is not None and level > 0:
            span = moment - previous
            busy += span
            area += span * level
            full += span if level >= capacity else 0
        level, previous = level + delta, moment
    return {"busy_hours": round(busy / 3600, 3), "held_mean": round(area / busy, 2) if busy else None,
            "all_held": round(full / busy, 3) if busy else None}


def machine(samples: list[dict], cores: int, capacity: int) -> dict:
    """The machine while leases ran: each sample stands for the time to the next one, at most GAP seconds."""
    samples = sorted((s for s in samples if isinstance(s.get("at"), (int, float))), key=lambda s: s["at"])
    if not samples:
        return {"samples": 0}
    gaps = [b["at"] - a["at"] for a, b in zip(samples, samples[1:])]
    usual = min(percentile(gaps, 50) or _config.MACHINE_EVERY, GAP)
    weights = [min(g, GAP) for g in gaps] + [usual]
    total_weight = sum(weights) or 1.0
    load1 = [(s.get("load") or [None])[0] for s in samples]
    over = sum(w for w, l, s in zip(weights, load1, samples) if l is not None and l > (s.get("cores") or cores))
    under = sum(w for w, l, s in zip(weights, load1, samples) if l is not None and l < (s.get("cores") or cores))
    busy, busy_weight, busy_sum, idle_slot, held_area, busy_area = [], 0.0, 0.0, 0.0, 0.0, 0.0
    for (a, b), w in zip(zip(samples, samples[1:]), weights):
        if not (a.get("ticks") and b.get("ticks")) or len(a["ticks"]) != 4 or len(b["ticks"]) != 4:
            continue
        if b["at"] - a["at"] > GAP:
            continue  # no lease ran in between: the ticks would average idle time into the busy share
        delta = [(y - x) % (1 << 32) for x, y in zip(a["ticks"], b["ticks"])]
        if not sum(delta):
            continue
        share = 1 - delta[2] / sum(delta)
        here = a.get("cores") or cores
        busy.append(share)
        busy_weight += w
        busy_sum += w * share
        idle_slot += w if (1 - share) * here >= here / (a.get("capacity") or capacity) else 0
        if a.get("held"):
            held_area += w * a["held"]
            busy_area += w * share * here
    swap_free = [s.get("swap_free_mb") for s in samples if s.get("swap_total_mb")]
    memory_free = [s.get("memory_free_pct") for s in samples]
    available = [s.get("memory_available_mb") for s in samples]

    def elevated(sample: dict) -> bool:
        """macOS: the pressure level above normal; Linux: PSI memory avg10 at the guard's limits."""
        level, some, full = (sample.get(k) for k in ("pressure", "psi_some", "psi_full"))
        return (level is not None and level > 1) or (some is not None and some >= _config.MAX_PSI_SOME) or \
            (full is not None and full >= _config.MAX_PSI_FULL)
    return {
        "samples": len(samples), "hours": round(total_weight / 3600, 3),
        "load1_p50": percentile(load1, 50), "load1_p90": percentile(load1, 90),
        "over_cores": round(over / total_weight, 3), "under_cores": round(under / total_weight, 3),
        "busy_p50": None if not busy else round(percentile(busy, 50), 3),
        "busy_mean": None if not busy_weight else round(busy_sum / busy_weight, 3),
        "slot_idle": None if not busy_weight else round(idle_slot / busy_weight, 3),
        "busy_cores_per_held_slot": round(busy_area / held_area, 2) if held_area else None,
        "pressure_elevated": round(sum(w for w, s in zip(weights, samples) if elevated(s)) / total_weight, 3),
        "memory_free_pct_min": min((m for m in memory_free if m is not None), default=None),
        "swap_free_mb_min": min((f for f in swap_free if f is not None), default=None),
        "memory_available_mb_min": min((a for a in available if a is not None), default=None),
    }


SWEPT = ("removed", "orphan", "dev-server")


def sweeps(items: list[dict]) -> dict:
    """What `slotfather sweep` found and stopped (sweep.jsonl, #662): runs, live runs, processes stopped per class,
    stops that failed, resident memory freed, and what the latest run found."""
    stopped = {c: sum((i.get("stopped") or {}).get(c, 0) for i in items) for c in SWEPT}
    last = items[-1] if items else {}
    return {"runs": len(items), "live": sum(bool(i.get("live")) for i in items), "stopped": stopped,
            "failed": sum(i.get("failed") or 0 for i in items),
            "freed_mb": round(sum(i.get("freed_mb") or 0 for i in items), 1),
            "last_at": last.get("at"), "last_found": last.get("found"), "last_live": last.get("live")}


def summarize(directory: Path | None = None, since: float | None = None, label: str | None = None,
              by: str = "both", moment: float | None = None) -> dict:
    directory = directory or _config.state_dir()
    moment = time.time() if moment is None else moment
    items = []
    for item in _state.history_items(directory):
        ended = _config.seconds(item.get("ended"))
        if since is not None and (ended is None or ended < since):
            continue
        if label is not None and not fnmatch.fnmatchcase(str(item.get("label")), label):
            continue
        items.append(item)
    groups = {}
    meta_key = by[5:] if by.startswith("meta.") else None
    for item in items:
        if meta_key:
            key = (_config.meta_of(item).get(meta_key), None)
        else:
            key = (item.get("label") if by != "kind" else None, kind(item.get("cmd")) if by != "label" else None)
        groups.setdefault(key, []).append(item)
    rows = []
    for (name, what), members in groups.items():
        row = {"label": None, "kind": None, "meta": {meta_key: name}} if meta_key else {"label": name, "kind": what}
        rows.append({**row, **usage_of(members)})
    rows.sort(key=lambda r: (-r["slot_hours"], -r["requests"]))
    try:
        capacity, saved = _config.capacity()
    except _config.SlotError:
        capacity, saved = _config.automatic_capacity(), False
    cores = _config.cores()
    samples = [s for s in _state.journal(directory / "machine.jsonl")
               if since is None or (isinstance(s.get("at"), (int, float)) and s["at"] >= since)]
    overall = usage_of(items)
    per_slot = overall["cores_per_slot"]
    swept = [i for i in _state.journal(directory / "sweep.jsonl")
             if since is None or (isinstance(i.get("at"), (int, float)) and i["at"] >= since)]
    return {
        "since": since, "label": label, "by": by, "cores": cores, "capacity": capacity, "saved": saved,
        "automatic": _config.automatic_capacity(),
        "first": items[0].get("ended") if items else None, "last": items[-1].get("ended") if items else None,
        "versions": {str(v): sum((i.get("v") or 1) == v for i in items)
                     for v in sorted({i.get("v") or 1 for i in items})},
        "overall": overall, "held": held_time(items, since, moment, recorded_capacity(items, capacity)),
        "machine": machine(samples, cores, capacity),
        "fill_slots": round(cores / per_slot, 1) if per_slot else None,
        "sweeps": sweeps(swept),
        "groups": rows,
    }


def span(value) -> str:
    return "-" if value is None else _config.duration(value)


def number(value, digits: int = 1) -> str:
    return "-" if value is None else f"{value:.{digits}f}"


def share(value) -> str:
    return "-" if value is None else f"{value * 100:.0f}%"


def local(stamp) -> str:
    moment = _config.seconds(stamp)
    return "?" if moment is None else time.strftime("%m-%d %H:%M", time.localtime(moment))


def report(summary: dict, top: int = 25) -> str:
    o, h, m = summary["overall"], summary["held"], summary["machine"]
    lines = [f"slotfather stats · {o['requests']} requests"
             + (f" ended {local(summary['first'])} to {local(summary['last'])}" if summary["first"] else "")
             + f" · {summary['cores']} cores, {summary['capacity']} slots "
             + ("(saved; automatic " + str(summary["automatic"]) + ")" if summary["saved"] else "(automatic)")]
    lines.append(f"ran {o['released']}, gave up {o['left']}, reaped {o['reaped']}, failed {o['failed']} · "
                 f"wait p50 {span(o['wait_p50'])} p90 {span(o['wait_p90'])} · {o['slot_hours']:.1f} slot-hours held"
                 f" · usage measured for {o['measured']}")
    if h["busy_hours"]:
        lines.append(f"while any lease ran ({h['busy_hours']:.1f} h): {number(h['held_mean'])} slots held on average, "
                     f"every slot held {share(h['all_held'])} of the time")
    meta_key = summary["by"][5:] if summary["by"].startswith("meta.") else None

    def first(row) -> str:
        if meta_key:
            value = row["meta"].get(meta_key)
            return "-" if value is None else str(value)
        return str(row["label"])
    title = summary["by"].upper() if meta_key else "LABEL"
    width = max([len(first(r)) for r in summary["groups"][:top]] + [len(title)])
    width = min(width, 34)
    kinds = max([len(str(r["kind"] or "")) for r in summary["groups"][:top]] + [4])
    kinds = min(kinds, 40)
    head = []
    if summary["by"] != "kind":
        head.append(f"{title:<{width}}")
    if summary["by"] not in ("label",) and not meta_key:
        head.append(f"{'KIND':<{kinds}}")
    head.append(f"{'RUNS':>5} {'WAIT p50':>8} {'p90':>6} {'RUN p50':>7} {'p90':>6} {'SLOTS':>5} {'CORES':>5} "
                f"{'PER SLOT':>8} {'PEAK MB':>7}")
    lines += ["", "  ".join(head)]
    for row in summary["groups"][:top]:
        cells = []
        if summary["by"] != "kind":
            cells.append(f"{first(row)[:width]:<{width}}")
        if summary["by"] != "label" and not meta_key:
            cells.append(f"{str(row['kind'])[:kinds]:<{kinds}}")
        cells.append(f"{row['requests']:>5} {span(row['wait_p50']):>8} {span(row['wait_p90']):>6} "
                     f"{span(row['run_p50']):>7} {span(row['run_p90']):>6} {number(row['slots_p50'], 0):>5} "
                     f"{number(row['cores']):>5} {number(row['cores_per_slot']):>8} "
                     f"{number(row['rss_peak_mb'], 0):>7}")
        lines.append("  ".join(cells))
    if len(summary["groups"]) > top:
        lines.append(f"… {len(summary['groups']) - top} more groups (--top N, or --json)")
    lines.append("")
    if o["measured"]:
        lines.append(f"measured requests used {number(o['cores_per_slot'], 2)} cores per slot held "
                     f"({summary['cores']} cores / {summary['capacity']} slots = "
                     f"{summary['cores'] / summary['capacity']:.1f} per slot); the cores fill at "
                     f"{number(summary['fill_slots'])} slots of that use")
    else:
        lines.append("no request in range recorded its usage (history lines before version 2, or holders older "
                     "than this slotfather)")
    if m.get("samples"):
        lines.append(f"machine, {m['hours']:.1f} h sampled while leases ran: load1 p50 {number(m['load1_p50'])} "
                     f"p90 {number(m['load1_p90'])}; over {summary['cores']} cores {share(m['over_cores'])} of the "
                     f"time, under {share(m['under_cores'])}; CPU busy mean {share(m['busy_mean'])}, a slot's cores "
                     f"idle {share(m['slot_idle'])}; {number(m['busy_cores_per_held_slot'])} busy cores per held slot; "
                     f"memory pressure above normal {share(m['pressure_elevated'])}"
                     + ("" if m["swap_free_mb_min"] is None else f"; least free swap {m['swap_free_mb_min']} MB")
                     + ("" if m.get("memory_available_mb_min") is None
                        else f"; least available memory {m['memory_available_mb_min']} MB"))
    else:
        lines.append("no machine samples in range")
    w = summary.get("sweeps") or {}
    if w.get("runs"):
        found = w.get("last_found") or {}
        lines.append(f"sweeps: {w['runs']} ({w['live']} live); stopped "
                     + ", ".join(f"{w['stopped'][c]} {c}" for c in SWEPT)
                     + f", {w['failed']} failed, {w['freed_mb']:.0f} MB resident freed; the last "
                     + ("live " if w.get("last_live") else "dry ") + f"run at "
                     + time.strftime("%m-%d %H:%M", time.localtime(w["last_at"] or 0)) + " found "
                     + ", ".join(f"{found.get(c, 0)} {c}" for c in (*SWEPT, "container")))
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    since = label = None
    by, top, as_json = "both", 25, False
    try:
        while argv:
            option = argv.pop(0)
            if option == "--since" and argv:
                since = parse_since(argv.pop(0))
            elif option == "--label" and argv:
                label = argv.pop(0)
            elif option == "--by" and argv and (argv[0] in ("both", "label", "kind") or (
                    argv[0].startswith("meta.") and _config.META_KEY.fullmatch(argv[0][5:]))):
                by = argv.pop(0)
            elif option == "--top" and argv and argv[0].isdigit():
                top = int(argv.pop(0))
            elif option == "--json":
                as_json = True
            elif option in ("-h", "--help"):
                print(__doc__)
                return 0
            else:
                raise StatsError(f"unknown or incomplete option {option}")
        summary = summarize(since=since, label=label, by=by)
    except (StatsError, _config.SlotError) as error:
        print(f"slotfather stats: {error}\n{USAGE}", file=sys.stderr)
        return 2
    print(json.dumps(summary, sort_keys=True) if as_json else report(summary, top))
    return 0
