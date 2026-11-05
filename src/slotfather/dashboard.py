#!/usr/bin/env python3
"""slotfather-dashboard: a read-only live view of the slotfather queue; `slotfather dashboard` runs it.

  slotfather dashboard [--interval S]                        live view: ? lists the keys, q quits
  slotfather dashboard --once [--width N] [--height N] [--color]   one snapshot as text

It lives apart from the queue so the lease code stays small: it reads what `slotfather status` reads, and a fault
here ends the view, never admission. It loads the slotfather executable beside it (symlinks resolved) as a module.
"""
from __future__ import annotations

import contextlib
import datetime
import json
import math
import os
from pathlib import Path
import re
import select
import shutil
import subprocess
import sys
import termios
import textwrap
import time
import tty
import unicodedata
from typing import Any, NamedTuple, cast


from . import config as _config
from . import platform as _platform
from . import query as _query
from . import schedule as _schedule


# dashboard_model() gathers plain data and dashboard_draw() lays it out on a Canvas of styled cells; the live view
# and --once write that canvas as the same escape sequences, so they are the same picture.

SPARK = "▁▂▃▄▅▆▇█"
CHART_ROWS = 6
MACHINE_HEIGHT = 12 + CHART_ROWS  # status, resource bars, gaps, legend and frame
HISTORY_WINDOW = 3600  # seconds of queue.log the utilisation strip rebuilds
LOG_TAIL = 262144  # bytes of queue.log read per refresh
WIDE = 110  # columns from which the panels sit side by side
# style -> (256-colour index or RGB foreground, attributes: b bold, d dim, r reverse). None keeps the terminal's own foreground,
# so text reads on light and dark themes alike.
STYLES = {
    "text": (None, ""), "bold": (None, "b"), "dim": (245, "d"), "frame": (239, "d"),
    "title": (None, "b"), "key": (255, "b"), "accent": (75, ""), "cursor": (255, "b"), "status-normal": (255, ""),
    "ok": (114, ""), "warn": (179, ""), "bad": (167, ""), "legacy": (176, ""),
    "badge-ok": (114, "br"), "badge-warn": (179, "br"), "badge-bad": (167, "br"),
    "h0": (75, ""), "h1": (79, ""), "h2": (216, ""), "h3": (141, ""),
    "h4": (110, ""), "h5": (186, ""),
}
# Fixed colours by chart height, so orange always means near capacity, even for short bars.
CHART_COLOURS = [(95, 175, 255), (105, 155, 255), (135, 145, 255), (170, 135, 240),
                 (200, 140, 210), (225, 150, 170), (245, 160, 130), (255, 175, 95)]
STYLES.update({f"chart{i}": (rgb, "") for i, rgb in enumerate(CHART_COLOURS)})
HOLDER_STYLES = 6
ASCII = str.maketrans({"🮅": "#", "■": "#", "▶": ">", "▉": "#", "▌": "+", "▐": "|", "█": "#", "▓": "%", "▒": "=", "░": ".", "·": ".", "●": "*", "…": "~", "─": "-", "│": "|",
                       "╭": "+", "╮": "+", "╰": "+", "╯": "+", "↑": "^", "↓": "v", "↗": "/", "↘": "\\", "→": ">",
                       "←": "<", "≈": "~", "×": "x", "—": "-", "⏸": "=", "▁": "_", "▂": "_", "▃": "-", "▄": "-",
                       "▅": "=", "▆": "=", "▇": "#"})
GRANTED = re.compile(r"granted (\d+) to (.*) \(pid (\d+)\)$")
RELEASED = re.compile(r"released (\d+) \(\d+-(\d+)\.json\)$")
REAPED = re.compile(r"reaped (\S+) lease of (\d+) for (.*) \(pid (\d+) exited\)$")


def dashboard_text(value) -> str:
    """A label or command as plain text: any process writes queue entries, so control bytes are blanked."""
    return "".join(char if char.isprintable() else " " for char in str(value))


def cell_width(char: str) -> int:
    if unicodedata.combining(char):
        return 0
    return 2 if unicodedata.east_asian_width(char) in "WF" else 1


def text_width(value) -> int:
    return sum(cell_width(char) for char in dashboard_text(value))


def clip(value, width: int) -> str:
    """value cut to `width` cells, ending in … when cut."""
    text = dashboard_text(value)
    if text_width(text) <= width:
        return text
    kept, used = [], 0
    for char in text:
        if used + cell_width(char) > width - 1:
            break
        kept.append(char)
        used += cell_width(char)
    return "".join(kept) + "…" if width > 0 else ""


def span(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m{seconds % 60:02}s"
    if seconds < 86400:
        return f"{seconds // 3600}h{seconds // 60 % 60:02}m"
    return f"{seconds // 86400}d{seconds // 3600 % 24:02}h"


def today() -> datetime.date:
    return datetime.date.today()


def clock(moment: datetime.datetime | None) -> str:
    if moment is None:
        return "--:--:--"
    local = moment.astimezone()
    return local.strftime("%H:%M:%S" if local.date() == today() else "%a %H:%M")


def megabytes(value: float | None) -> str:
    if value is None:
        return "?"
    return f"{value / 1024:.1f} GB" if value >= 1024 else f"{value:.0f} MB"


def short(value: float) -> str:
    return f"{value:.1f}".rstrip("0").rstrip(".")


def tilde(path: str) -> str:
    home = os.path.expanduser("~")
    return "~" + path[len(home):] if path == home or path.startswith(home + os.sep) else path


def parse_utc(value) -> datetime.datetime | None:
    try:
        return datetime.datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=datetime.timezone.utc)
    except (TypeError, ValueError):
        return None


class Canvas:
    """A grid of [character, style, selected] cells; a wide character takes its cell and an empty one after it."""

    def __init__(self, width: int, height: int):
        self.scroll: dict[str, int] = {}
        self.tables = {}
        self.help_view: HelpViewport | None = None
        self.width, self.height = max(0, width), max(0, height)
        self.cells = [[[" ", "text", False] for _ in range(self.width)] for _ in range(self.height)]

    def put(self, y: int, x: int, value: Any, style: str = "text", width: int | None = None) -> int:
        """Writes value from column x, cut to `width` cells and to the edge; the column after it."""
        text = clip(value, width) if width is not None else dashboard_text(value)
        if not 0 <= y < self.height:
            return x
        for char in text:
            size = cell_width(char)
            if size == 0:
                continue
            if x + size > self.width:
                break
            if x >= 0:
                self.cells[y][x][:2] = [char, style]
                if size == 2:
                    self.cells[y][x + 1][:2] = ["", style]
            x += size
        return x

    def right(self, y: int, end: int, value, style: str = "text") -> int:
        """Writes value ending just before column `end`; the column it starts at."""
        start = end - text_width(value)
        self.put(y, start, value, style)
        return start

    def segments(self, y: int, x: int, parts, width: int | None = None) -> int:
        """Writes (text, style) parts one after another, all within `width` cells from x."""
        end = self.width if width is None else x + width
        for text, style in parts:
            if x >= end:
                break
            x = self.put(y, x, text, style, end - x)
        return x

    def fill(self, y: int, x: int, height: int, width: int) -> None:
        for row in range(max(0, y), min(self.height, y + height)):
            for column in range(max(0, x), min(self.width, x + width)):
                self.cells[row][column] = [" ", "text", False]

    def select(self, y: int, start: int, end: int) -> None:
        if 0 <= y < self.height:
            for column in range(max(0, start), min(self.width, end)):
                self.cells[y][column][2] = True

    def box(self, y: int, x: int, height: int, width: int, title: str = "", note: str = "") -> None:
        if height < 2 or width < 6:
            return
        self.put(y, x, "╭" + "─" * (width - 2) + "╮", "frame")
        for row in range(y + 1, y + height - 1):
            self.put(row, x, "│", "frame")
            self.put(row, x + width - 1, "│", "frame")
        self.put(y + height - 1, x, "╰" + "─" * (width - 2) + "╯", "frame")
        after = self.put(y, x + 2, f" {title} ", "title", width - 4) if title else x + 2
        if note and x + width - 2 - text_width(note) - 2 > after:
            self.right(y, x + width - 2, f" {note} ", "dim")

    def runs(self, y: int) -> list[list]:
        """Row y as [text, style, selected] runs."""
        found = []
        for char, style, selected in self.cells[y]:
            if found and found[-1][1] == style and found[-1][2] == selected:
                found[-1][0] += char
            else:
                found.append([char, style, selected])
        return found

    def lines(self, colour: bool, ascii_only: bool = False) -> list[str]:
        """Every row at full width, with SGR colour codes when `colour`."""
        found = []
        for y in range(self.height):
            if colour:
                line = "".join(sgr(style, selected) + text + "\x1b[0m" for text, style, selected in self.runs(y))
            else:
                line = "".join(char for char, _, _ in self.cells[y])
            found.append(line.translate(ASCII) if ascii_only else line)
        return found

    def render(self, colour: bool, ascii_only: bool = False) -> str:
        return "\n".join(line if colour else line.rstrip() for line in self.lines(colour, ascii_only))


def sgr(style: str, selected: bool) -> str:
    fore, flags = STYLES[style]
    if selected and fore is None:
        fore = 252  # readable neutral text over the soft selection background
    codes = ["0"] + (["1"] if "b" in flags else []) + (["2"] if "d" in flags and fore is None else [])
    if isinstance(fore, tuple):
        codes.append("38;2;" + ";".join(str(channel) for channel in fore))
    elif fore is not None:
        codes.append(f"38;5;{fore}")
    if selected:
        codes.append("48;2;56;62;74")
    codes += ["7"] if "r" in flags else []
    return "\x1b[" + ";".join(codes) + "m"


# Dashboard data

def process_usage() -> tuple[dict, dict]:
    """pid -> (CPU percent, resident KB, command name) for every process, and ppid -> its child pids."""
    result = subprocess.run(["ps", "-axo", "pid=,ppid=,%cpu=,rss=,comm="], capture_output=True, text=True)
    if result.returncode:
        raise OSError(f"CPU process sample failed: ps exited {result.returncode}")
    own, children = {}, {}
    for line in result.stdout.splitlines():
        parts = line.split(None, 4)
        if len(parts) == 5 and parts[0].isdigit() and parts[1].isdigit():
            with contextlib.suppress(ValueError):
                own[int(parts[0])] = (float(parts[2].replace(",", ".")), int(parts[3]), os.path.basename(parts[4]))
                children.setdefault(int(parts[1]), []).append(int(parts[0]))
    return own, children


def process_tree(pid, own: dict, children: dict) -> list[tuple[int, float, int, str]]:
    """(pid, CPU percent, resident KB, name) for pid and all its descendants: a lease's whole command."""
    found, stack, seen = [], [pid], set()
    while stack:
        current = stack.pop()
        if current in seen or current not in own:
            continue
        seen.add(current)
        found.append((current, *own[current]))
        stack.extend(children.get(current, ()))
    return found


def process_cwd(pid) -> str | None:
    """The directory a holder was started in, which names its project better than most labels."""
    with contextlib.suppress(OSError):
        return os.readlink(f"/proc/{pid}/cwd")
    try:
        result = subprocess.run(["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"], capture_output=True, text=True,
                                timeout=2)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return next((line[1:] for line in result.stdout.splitlines() if line.startswith("n")), None)


def macos_used_memory(text: str) -> float | None:
    """Physical MB in active, wired and compressor pages; inactive cache is excluded."""
    page = re.search(r"page size of (\d+) bytes", text)
    fields = dict(re.findall(r"^(Pages [^:]+):\s+(\d+)\.", text, re.MULTILINE))
    names = ("Pages active", "Pages wired down", "Pages occupied by compressor")
    if not page or any(name not in fields for name in names):
        return None
    return sum(int(fields[name]) for name in names) * int(page[1]) / (1024 * 1024)


def memory_facts() -> dict:
    """Free and total swap in MB and the kernel's memory-pressure level, as far as this host reports them."""
    seam = _config.setting("MEMORY_FILE")
    if seam:
        with contextlib.suppress(OSError, ValueError):
            free, level = (float(v) for v in Path(seam).read_text().split()[:2])
            return {"swap_free": free, "swap_total": None, "level": level}
        return {"swap_free": None, "swap_total": None, "level": None}
    if _platform.linux():  # no pressure level: MemAvailable and PSI, as the memory policy reads them
        facts = _platform.linux_memory()
        return {"swap_total": facts["swap_total"], "swap_free": facts["swap_free"], "level": None,
                "available": facts["available"], "total": facts["total"], "psi_some": facts["psi_some"],
                "psi_full": facts["psi_full"], "cgroup_available": facts.get("cgroup_available"),
                "cgroup": facts.get("cgroup")}
    total, free = _platform.swap_usage() or (None, None)
    facts = {"swap_total": total, "swap_free": free, "level": _platform.pressure_level()}
    with contextlib.suppress(OSError, ValueError, subprocess.TimeoutExpired):
        physical = _platform.sysctl("hw.memsize")
        stats = subprocess.run(["vm_stat"], capture_output=True, text=True, timeout=2)
        facts.update(total=int(physical) / (1024 * 1024), used=macos_used_memory(stats.stdout))
    return facts


def queue_events(directory: Path) -> list[dict]:
    """The grants, releases and reaps at the end of queue.log, oldest first. Each event carries `delta`, the change
    it made to held slots. A release whose grant precedes the part read is taken to end a held lease; one whose
    pid was never granted in a fully read log is a waiter that gave up."""
    try:
        with open(directory / "queue.log", "rb") as handle:
            start = max(0, handle.seek(0, os.SEEK_END) - LOG_TAIL)
            handle.seek(start)
            lines = handle.read().decode("utf-8", "replace").splitlines()[1 if start else 0:]
    except OSError:
        return []
    events, granted = [], {}
    for line in lines:
        moment, _, message = line.partition(" ")
        when = parse_utc(moment)
        if when is None:
            continue
        if found := GRANTED.match(message):
            count, label, pid = int(found[1]), found[2], int(found[3])
            granted[pid] = label
            events.append({"when": when, "verb": "granted", "count": count, "label": label, "pid": pid,
                           "delta": count})
        elif found := RELEASED.match(message):
            count, pid = int(found[1]), int(found[2])
            held = pid in granted or start > 0
            events.append({"when": when, "verb": "released" if held else "left", "count": count,
                           "label": granted.pop(pid, ""), "pid": pid, "delta": -count if held else 0})
        elif found := REAPED.match(message):
            state, count, label, pid = found[1], int(found[2]), found[3], int(found[4])
            granted.pop(pid, None)
            events.append({"when": when, "verb": "reaped", "count": count, "label": label, "pid": pid,
                           "delta": -count if state == "held" else 0, "state": state})
    return events


def held_history(events: list[dict], held_now: int, now_moment: datetime.datetime,
                 window: float = HISTORY_WINDOW * 2) -> list[tuple[datetime.datetime, datetime.datetime, int]]:
    """Held slots over the last `window` seconds as (start, end, slots) steps, walked back from held_now."""
    begin = now_moment - datetime.timedelta(seconds=window)
    steps, level, end = [], held_now, now_moment
    for event in reversed(events):
        if event["when"] <= begin:
            break
        if event["delta"] and event["when"] < end:
            steps.append((event["when"], end, max(0, level)))
            end = event["when"]
        level -= event["delta"]
    steps.append((begin, end, max(0, level)))
    return steps[::-1]


def history_buckets(steps: list, columns: int) -> list[int]:
    """Peak slots per fixed wall-clock bucket; only the newest bucket is incomplete.

    Anchoring buckets to the clock keeps old peaks stable between scroll steps.
    """
    if not steps or columns <= 0:
        return []
    begin, end = steps[0][0], steps[-1][1]
    seconds = max(1, math.ceil(HISTORY_WINDOW / columns))
    edge = math.ceil(end.timestamp() / seconds) * seconds
    end = datetime.datetime.fromtimestamp(edge, end.tzinfo)
    width = datetime.timedelta(seconds=seconds)
    begin = end - width * columns
    buckets = []
    for column in range(columns):
        low, high = begin + width * column, begin + width * (column + 1)
        buckets.append(max((level for start, stop, level in steps if start < high and stop > low), default=0))
    return buckets


def history_summary(steps: list, slots: int) -> dict:
    # Retain extra history for whole clock buckets, but summarise only the trailing hour.
    if steps:
        begin = steps[-1][1] - datetime.timedelta(seconds=HISTORY_WINDOW)
        steps = [(max(start, begin), stop, level) for start, stop, level in steps if stop > begin]
    total_seconds = sum((stop - start).total_seconds() for start, stop, _ in steps) or 1
    held = sum((stop - start).total_seconds() * min(level, slots) for start, stop, level in steps)
    full = sum((stop - start).total_seconds() for start, stop, level in steps if level >= slots)
    return {"average": held / total_seconds / slots, "full": full / total_seconds,
            "peak": max((level for _, _, level in steps), default=0)}


def dashboard_admission(waiters: list, held: list, slots: int, other: int, memory_block: str | None,
                        estimate, settings: dict) -> int:
    """Largest generic request grant() would admit, including earlier grants consuming the memory exemption."""
    used = sum(entry["count"] for entry in held) + other
    free = max(0, slots - used)
    if not free or memory_block and used:
        return 0
    if not waiters:
        return free
    if memory_block:
        # On an idle host only the first planned grant bypasses the memory gate.
        # A new generic request may rank ahead of a lower-priority existing waiter.
        probe = dict(count=free, label="", cmd="", since=_config.now())
        found = _schedule.plan(waiters + [probe], held, slots, time.time(), estimate=estimate,
                        other=other, settings=settings)
        return free if found.grants and found.grants[0] is probe else 0
    return _schedule.room(waiters, free, held, estimate, other, settings=settings)


def dashboard_model() -> dict:
    directory = _config.state_dir()
    live, legacy, _ = _query.snapshot(directory)
    slots, saved = _config.capacity()
    own, children = process_usage()
    asks = {}  # holder pid -> the label of the waiter that asked it to yield
    for place in (directory / "queue").glob("*.yield"):
        with contextlib.suppress(OSError, ValueError, AttributeError):
            asks[int(place.stem.split("-")[-1])] = str(json.loads(place.read_text()).get("label") or "?")

    def item(entry: dict, kind: str, count: int, queue: str = "") -> dict:
        tree = process_tree(entry.get("pid"), own, children) if kind != "waiting" else []
        return {"tree": sorted(tree, key=lambda process: -process[1]), "cpu": sum(p[1] for p in tree),
                "rss": sum(p[2] for p in tree), "procs": len(tree),"key": (entry.get("pid"), entry.get("since")), "kind": kind, "count": count, "queue": queue,
                "label": dashboard_text(entry.get("label", "")), "pid": entry.get("pid"),
                "cmd": dashboard_text(entry.get("cmd", "")), "since": parse_utc(entry.get("since")),
                "meta": dashboard_text(_config.meta_text(_config.meta_of(entry))),
                "yieldable": bool(entry.get("yieldable")), "yields": int(entry.get("yields") or 0),
                "asked": dashboard_text(asks.get(entry.get("pid"), "")),
                "granted": parse_utc(entry.get("granted")), "passable": kind == "waiting" and _schedule.overtakable(entry),
                "style": "warn" if kind == "waiting" else "legacy"}

    holders = [item(e, "held", e["count"]) for e in live if e["state"] == "held"]
    holders += [item(e, "legacy", count, str(path.parent.parent)) for path, e, count in legacy]
    settings, start = _schedule.order_settings(directory), None
    order = settings["order"]
    live_waiters = [e for e in live if e["state"] != "held"]
    _schedule.fit(live_waiters, slots)  # as the queue grants them when the capacity shrank under them
    if order == "backfill":
        live_waiters = _schedule.ranked(live_waiters, settings)  # the order the queue serves them in
    waiters = [item(e, "waiting", e["count"]) for e in live_waiters]
    used = sum(h["count"] for h in holders)
    if order == "backfill" and live_waiters:
        with contextlib.suppress(Exception):  # an estimate that cannot be read leaves the sentence without a time
            found = _schedule.plan(live_waiters, [e for e in live if e["state"] == "held"], slots, time.time(), order,
                            _schedule.estimator(directory), sum(count for _, _, count in legacy), reserve_head=True,
                            settings=settings)
            if found.head is not None and found.start is not None:
                start = datetime.datetime.fromtimestamp(found.start, datetime.timezone.utc)
    memory_block, machine, averages = _platform.memory_short(), _config.cores(), _platform.loads()
    events = queue_events(directory)
    moment = datetime.datetime.now(datetime.timezone.utc)
    free = max(0, slots - used)
    admission = dashboard_admission(live_waiters, [e for e in live if e["state"] == "held"], slots,
                    sum(count for _, _, count in legacy), memory_block,
                    _schedule.estimator(directory) if live_waiters else None, settings)
    own_held = sum(h["count"] for h in holders if h["kind"] == "held")
    return {"directory": str(directory), "slots": slots, "saved": saved, "cores": machine, "averages": averages,
            "advice": _query.size(slots, machine, slots, averages, min(used, slots),
                              bool(waiters) and admission == 0),
            "holders": holders, "waiters": waiters, "used": used, "free": free, "memory_block": memory_block,
            "admission": admission,
            "order": order, "start": start, "limit": settings["limit"],
            "memory": memory_facts(), "history": held_history(events, own_held, moment), "events": events[-60:],
            "updated": moment}


def dashboard_colours(model: dict, assigned: dict) -> None:
    """Gives each running lease a colour it keeps until it ends, preferring colours no other lease shows, so the
    slot bar, the table swatch and the detail heading stay linked as the queue moves."""
    keys = {h["key"] for h in model["holders"]}
    for key in [key for key in assigned if key not in keys]:
        del assigned[key]
    for holder in model["holders"]:
        if holder["kind"] != "held":
            continue
        if holder["key"] not in assigned:
            taken = list(assigned.values())
            assigned[holder["key"]] = min(range(HOLDER_STYLES), key=lambda n: (taken.count(n), n))
        holder["style"] = f"h{assigned[holder['key']]}"


# Dashboard layout

def bar(fraction: float, width: int, style: str) -> list[tuple[str, str]]:
    """Compact squares leave vertical breathing room between adjacent bar rows."""
    width = max(0, width)
    full = max(0, min(width, math.floor(fraction * width + 0.5)))
    return [("■" * full, style), ("■" * (width - full), "frame")]


def share(item: dict, model: dict) -> tuple[float, float]:
    """(cores the lease's commands use, cores its slots stand for)."""
    return item["cpu"] / 100, item["count"] * model["cores"] / model["slots"]


def dashboard_idle(model: dict, tracked: dict) -> None:
    """Track observed low CPU per grant, never infer idle time before the dashboard saw it."""
    live = set()
    for item in model["holders"]:
        key = (item["key"], item["granted"])
        live.add(key)
        used, allotted = share(item, model)
        if allotted > 0 and used < 0.1 * allotted:
            item["idle_since"] = tracked.setdefault(key, model["updated"])
        else:
            tracked.pop(key, None)
            item["idle_since"] = None
    for key in list(tracked):
        if key not in live:
            del tracked[key]


def clear_idle(model: dict | None, tracked: dict) -> None:
    """A pause or sampling failure breaks the observed idle interval."""
    tracked.clear()
    for item in model["holders"] if model else []:
        item.pop("idle_since", None)


def share_style(item: dict, model: dict, moment: datetime.datetime) -> str:
    used, allotted = share(item, model)
    running = (moment - item["granted"]).total_seconds() if item["granted"] else 0
    if running > 120 and used < 0.1 * allotted:
        return "warn"  # holds slots while nearly idle
    return "bad" if used > 1.5 * allotted else "accent"


def columns(width: int, spec: list[tuple]) -> list[tuple]:
    """spec: (key, title, width, right-aligned, priority) with width 0 for the one flexible column. Drops the
    columns of the highest priority number until the rest leave the flexible one 10 cells; (key, title, x, width,
    right-aligned)."""
    keep = list(spec)
    while sum(c[2] for c in keep) + 2 * (len(keep) - 1) + 10 > width and any(c[2] and c[4] for c in keep):
        keep.remove(max((c for c in keep if c[2] and c[4]), key=lambda c: c[4]))
    flexible = max(4, width - sum(c[2] for c in keep) - 2 * (len(keep) - 1))
    placed, x = [], 0
    for key, title, size, right, _ in keep:
        placed.append((key, title, x, size or flexible, right))
        x += (size or flexible) + 2
    return placed


def draw_row(canvas: Canvas, y: int, x: int, placed: list, values: dict) -> None:
    for key, _, column, size, right in placed:
        value = values.get(key, "")
        parts = value if isinstance(value, list) else [(value, "text")] if isinstance(value, str) else [value]
        start = x + column
        if right:
            start += max(0, size - sum(text_width(text) for text, _ in parts))
        canvas.segments(y, start, parts, x + column + size - start)


def table_view(canvas: Canvas, name: str, y: int, x: int, height: int, width: int,
               top: int, room: int, count: int, selected: int | None = None,
               base: int | None = None, row_items: list | None = None) -> tuple[int, int]:
    """One viewport shared by painting, hit testing and wheel scrolling."""
    room = max(0, room)
    visible = room
    automatic = max(0, (selected or 0) - visible + 1) if selected is not None else 0
    offset = max(0, min(canvas.scroll.get(name, automatic), max(0, count - visible)))
    canvas.tables[name] = dict(x=x, y=y, width=width, height=height, top=top,
                              visible=visible, count=count, offset=offset, base=base, row_items=row_items)
    if room:
        above = sum(index is not None for index in row_items[:offset]) if row_items is not None else offset
        if above:
            canvas.put(top - 1, x + 3, f"↑ {above} above", "dim", width - 6)
        below = (sum(index is not None for index in row_items[offset + visible:]) if row_items is not None
                 else max(0, count - offset - visible))
        if below:
            canvas.put(top + visible, x + 3, f"↓ {below} more", "dim", width - 6)
    return offset, visible


def mouse_action(canvas: Canvas, event: tuple[str, int, int, int, bool],
                 scroll: dict[str, int], selected: int) -> int:
    if canvas.help_view is not None:
        help_mouse(canvas, event, scroll)
        return selected
    _, button, mx, my, pressed = event
    if not pressed:
        return selected
    for name, view in reversed(list(canvas.tables.items())):
        if not (view["x"] < mx < view["x"] + view["width"] - 1
                and view["y"] < my < view["y"] + view["height"] - 1):
            continue
        code = button & ~28  # Shift / Alt / Ctrl do not change the wheel direction.
        if code in (64, 65):
            scroll[name] = max(0, min(scroll.get(name, cast(int, view["offset"])) + (3 if code == 65 else -3),
                                      max(0, view["count"] - view["visible"])))
        elif code == 0:
            row = my - view["top"]
            if 0 <= row < min(view["visible"], view["count"] - view["offset"]):
                position = view["offset"] + row
                if view["row_items"] is not None:
                    item = view["row_items"][position]
                    return selected if item is None else item
                if view["base"] is not None:
                    return view["base"] + position
        return selected
    return selected


def table_header(canvas: Canvas, y: int, x: int, height: int, width: int, title: str, placed: list, gutter: int = 0) -> None:
    """The panel's top border doubles as its table header, aligned with the data."""
    canvas.box(y, x, height, width)
    for index, (key, label, column, size, right) in enumerate(placed):
        value = title if index == 0 else label.lower()
        start = x + 3 + gutter + column + (max(0, size - text_width(value)) if right else 0)
        canvas.put(y, start - 1, " ", "frame")
        canvas.put(y, start, value, "title" if index == 0 else "dim", size)
        canvas.put(y, min(x + width - 2, start + min(size, text_width(value))), " ", "frame")


def draw_header(canvas: Canvas, model: dict | None, paused: bool, error: str | None, copying: bool = False) -> tuple[int, int]:
    """App/keys on the left; last update followed by live/pause/copy state on the right."""
    y = canvas.height - 1
    x = canvas.put(y, 1, "slotfather", "title", max(0, canvas.width - 2))
    status, style = (("● stale", "bad") if error else ("● text selection", "accent") if copying
                     else ("⏸ paused", "warn") if paused else ("● live", "ok"))
    room = canvas.width - 1 - x - 2
    right = canvas.width - 1
    if text_width(status) <= room:
        right = canvas.right(y, right, status, style)
        if model:
            stamp = clock(model["updated"])
            if text_width("updated " + stamp) + text_width(status) + 2 <= room:
                stamp = "updated " + stamp
            if text_width(stamp) + text_width(status) + 2 <= room:
                right = canvas.right(y, right - 2, stamp, "dim")
    return x + 3, right - 2


def draw_slots(canvas: Canvas, y: int, x: int, width: int, model: dict) -> None:
    """Solid slots spread across the widget, with two-cell gaps and right-aligned free label."""
    slots = model["slots"]
    area = max(0, width)
    gap = 2 if area >= slots + 2 * (slots - 1) else 1 if area >= 2 * slots - 1 else 0
    cell, remainder = divmod(max(0, area - gap * (slots - 1)), slots)
    widths = [cell + (index < remainder) for index in range(slots)]
    starts, cursor = [], x
    for size in widths:
        starts.append(cursor)
        cursor += size + gap
    labels, filled = [], 0
    for holder in model["holders"]:
        count = min(holder["count"], slots - filled)
        if count <= 0:
            break
        labels.append((starts[filled], holder["label"], holder["style"]))
        for index in range(filled, filled + count):
            canvas.put(y, starts[index], "🮅" * widths[index], holder["style"])
        filled += count
    for index in range(filled, slots):
        canvas.put(y, starts[index], "🮅" * widths[index], "frame")
    label_end = x + area
    if filled < slots and area >= 4:
        free_start = x + area - 4
        canvas.put(y + 1, free_start, "free", "dim")
        label_end = free_start - 2
    for index, (label_x, label, style) in enumerate(labels):
        limit = labels[index + 1][0] - 2 if index + 1 < len(labels) else label_end
        canvas.put(y + 1, label_x, label, style, max(0, limit - label_x))


def draw_requests(canvas: Canvas, y: int, x: int, height: int, width: int, model: dict, selected: int,
               moment: datetime.datetime) -> None:
    holders, waiters = model["holders"], model["waiters"]
    items = holders + waiters
    row_items = list(range(len(holders)))
    if holders and waiters:
        row_items.append(None)  # visual separator, never a selectable request
    row_items.extend(range(len(holders), len(items)))
    placed = columns(width - 6, [("label", "label", 0, False, 0), ("status", "status", 10, False, 0),
                                 ("slots", "slots", 5, True, 1), ("time", "time", 8, True, 2),
                                 ("cpu", "cpu use/share", 15, True, 3), ("pid", "pid", 6, True, 4)])
    table_header(canvas, y, x, height, width, "REQUESTS", placed)
    if not items:
        canvas.put(y + 2, x + 3, f"No requests · all {model['slots']} slots free", "dim", width - 6)
    visual_selected = row_items.index(selected) if selected in row_items else None
    offset, rows = table_view(canvas, "requests", y, x, height, width, y + 2, height - 4,
                              len(row_items), visual_selected, row_items=row_items)
    for visual, index in enumerate(row_items[offset:offset + rows], offset):
        if index is None:
            continue
        item = items[index]
        row = y + 2 + visual - offset
        waiting = index >= len(holders)
        if waiting:
            status = (f"waiting #{index - len(holders) + 1}", "warn")
            age = (moment - item["since"]).total_seconds() if item["since"] else None
            cpu = ("—", "dim")
        else:
            status = ("legacy" if item["kind"] == "legacy" else "running", "text")
            age = (moment - item["granted"]).total_seconds() if item["granted"] else None
            used, allotted = share(item, model)
            cpu = bar(used / allotted if allotted else 0, 5, share_style(item, model, moment)) + [
                (f" {used:.1f}/{short(allotted)}", "text")]
        draw_row(canvas, row, x + 3, placed, {
            "label": (item["label"], item["style"]), "status": status, "slots": str(item["count"]),
            "time": span(age) if age is not None else "—", "cpu": cpu, "pid": str(item["pid"])})
        if index == selected:
            canvas.put(row, x + 2, "▶", "cursor")
            canvas.select(row, x + 3, x + width - 3)


def queue_policy(model: dict) -> dict[str, str]:
    """Admission estimates and deadlines for the head, exposed as table fields."""
    if not model["waiters"]:
        return {}
    head = model["waiters"][0]
    if head["count"] <= model["free"]:
        return {}
    fields = {}
    if model["free"] and head.get("passable") and head.get("since"):
        fields["pass until"] = clock(head["since"] + datetime.timedelta(seconds=_config.overtake_window()))
    if model.get("order") == "backfill" and model.get("start") is not None and head.get("since"):
        fields["start est"] = clock(model["start"])
        if model.get("limit"):
            fields["pass until"] = clock(head["since"] + datetime.timedelta(seconds=model["limit"]))
    return fields



def draw_detail(canvas: Canvas, y: int, x: int, height: int, width: int, model: dict, selected: int,
                cwd: str | None, moment: datetime.datetime) -> None:
    canvas.box(y, x, height, width, "REQUEST DETAILS")
    items = model["holders"] + model["waiters"]
    inner, bottom = width - 6, y + height - 2
    if not items:
        canvas.put(y + 2, x + 3, "Nothing to select · the queue is idle", "dim", inner)
        return
    item = items[selected]
    if item["kind"] == "waiting":
        position = selected - len(model["holders"])
        heading = f"waiting · #{position + 1} · wants {item['count']}"
    else:
        heading = ("earlier queue" if item["kind"] == "legacy" else "running") + f" · {item['count']} slot{'s' if item['count'] != 1 else ''}"
    if item["kind"] != "waiting" and item.get("idle_since") is not None and share_style(item, model, moment) == "warn":
        minutes, seconds = divmod(max(0, int((moment - item["idle_since"]).total_seconds())), 60)
        duration = f"{minutes}m {seconds:02}s" if minutes else f"{seconds}s"
        heading += f" · idle for {duration}"
    facts = [("label", [(item["label"] or "(no label)", "bold")]), ("status", [(heading, "text")])]
    if item["kind"] == "waiting":
        facts.append(("queued", [(f"{clock(item['since'])} · waiting "
                                  f"{span((moment - item['since']).total_seconds()) if item['since'] else '?'}", "text")]))
        if position == 0:
            facts.append(("free", [(f"{model['free']} of {model['slots']} slots", "text")]))
            facts.extend((name, [(value, "text")]) for name, value in queue_policy(model).items())
            if model["used"] and model["memory_block"]:
                facts.append(("blocked", [(model["memory_block"], "warn")]))
        else:
            facts.append(("ahead", [(f"{position} requests · {sum(w['count'] for w in model['waiters'][:position])} slots", "text")]))
    else:
        used, allotted = share(item, model)
        style = share_style(item, model, moment)
        note = " · above its share" if style == "bad" else ""
        facts.append(("cpu", bar(used / allotted if allotted else 0, 10, style) +
                      [(f" {used:.1f} of {short(allotted)} cores{note}", "text")]))
        if item["granted"]:
            waited = f" · waited {span((item['granted'] - item['since']).total_seconds())}" if item["since"] else ""
            facts.append(("granted", [(f"{clock(item['granted'])}{waited} · running "
                                       f"{span((moment - item['granted']).total_seconds())}", "text")]))
        facts.append(("memory", [(f"{megabytes(item['rss'] / 1024)} in {item['procs']} processes", "text")]))
    if item.get("asked"):
        facts.append(("yield", [(f"asked to yield for {item['asked']}", "warn")]))
    elif item.get("yieldable") or item.get("yields"):
        facts.append(("yield", [("yields between steps when asked" + (f" · yielded {item['yields']}×"
                                                                       if item.get("yields") else ""), "dim")]))
    facts.append(("owner pid", [(str(item["pid"]), "text")]))
    where = tilde(item["queue"] or cwd or "")
    label_width = max(text_width(name) for name in [*(name for name, _ in facts), "command", "meta", "cwd"]) + 2
    value_width = max(1, inner - label_width)
    if text_width(where) > value_width:
        where = "…" + where[-max(0, value_width - 1):]
    blocks = [("command", item["cmd"] or "—")]
    if item.get("meta"):
        blocks.append(("meta", item["meta"]))
    available = max(0, bottom - (y + 2) - len(facts) - bool(where))
    process_room = 5 if item["tree"] and available >= len(blocks) + 5 else 0
    budget = max(len(blocks), available - process_room)
    wrapped = [textwrap.wrap(text, value_width, break_on_hyphens=False) or ["—"] for _, text in blocks]
    sizes = [1] * len(blocks)
    # Expand command and metadata together, each to at most three rows.
    for _ in range(2):
        for index, lines in enumerate(wrapped):
            if sum(sizes) < budget and sizes[index] < min(3, len(lines)):
                sizes[index] += 1
    for (name, _), lines, count in zip(blocks, wrapped, sizes):
        for index, line in enumerate(lines[:count]):
            if index == count - 1 and len(lines) > count:
                line = clip(line + " …", value_width)
            facts.append((name if index == 0 else "", [(line, "dim")]))
    if where:
        facts.append(("cwd", [(where, "dim")]))
    row = y + 2
    for name, parts in facts:
        if row >= bottom:
            break
        canvas.put(row, x + 3, name, "dim")
        canvas.segments(row, x + 3 + label_width, parts, value_width)
        row += 1
    if item["tree"] and bottom - row >= 5:
        placed = columns(inner, [("name", "process", 0, False, 0), ("cpu", "cpu", 6, True, 1),
                                 ("rss", "memory", 8, True, 2), ("pid", "pid", 6, True, 0)])
        draw_row(canvas, row + 1, x + 3, placed, {key: (title, "dim") for key, title, *_ in placed})
        offset, visible = table_view(canvas, "processes", row + 1, x, bottom - row, width,
                                     row + 3, bottom - row - 3, len(item["tree"]))
        for process_row, (pid, cpu, rss, name) in enumerate(item["tree"][offset:offset + visible], row + 3):
            draw_row(canvas, process_row, x + 3, placed, {"pid": str(pid), "cpu": f"{cpu:.0f}%",
                     "rss": megabytes(rss / 1024), "name": name})


def machine_memory_status(memory: dict) -> tuple[str, str]:
    """RAM/pressure health only; swap has a separate status and never colours RAM by itself."""
    level = memory.get("level")
    if level is not None:
        return ("memory low", "bad") if level > _config.MAX_PRESSURE_LEVEL else ("memory ok", "status-normal")
    if any(memory.get(key) is not None for key in ("available", "cgroup_available", "psi_some", "psi_full")):
        facts = dict(memory, swap_total=0, swap_free=None)
        for key in ("available", "psi_some", "psi_full"):
            facts.setdefault(key, None)
        return ("memory low", "bad") if _platform.linux_short(facts) else ("memory ok", "status-normal")
    return "memory unknown", "dim"


def machine_status(model: dict, width: int) -> list[tuple[str, str]]:
    memory = model["memory"]
    ram = machine_memory_status(memory)
    if memory.get("swap_total") == 0:
        swap = ("swap off", "status-normal")
    elif memory.get("swap_free") is None:
        swap = ("swap unknown", "dim")
    elif memory["swap_free"] < _config.number_setting("MIN_FREE_SWAP_MB", _config.MIN_FREE_SWAP_MB):
        swap = ("swap low", "bad")
    else:
        swap = ("swap ok", "status-normal")
    admission = model.get("admission")
    permit = (("admission: ?", "dim") if admission is None else ("no admission", "bad") if not admission
              else (f"admission: {admission} slots", "status-normal"))
    outside = model["advice"]["unleased_load"]
    fields = [ram, swap, (f"unleased load: {outside:.1f}", "status-normal"), permit]
    separator = " · "
    if sum(text_width(text) for text, _ in fields) + 9 > width:
        fields[0] = (ram[0].replace("memory", "mem").replace("unknown", "?"), ram[1])
        fields[1] = (swap[0].replace("unknown", "?"), swap[1])
        fields[2] = (f"unleased {outside:.1f}", "status-normal")
        separator = "  "
        if sum(text_width(text) for text, _ in fields) + 6 > width:
            separator = " "
    parts = []
    for index, field in enumerate(fields):
        if index:
            parts.append((separator, "dim"))
        parts.append(field)
    return parts


def draw_machine(canvas: Canvas, y: int, x: int, height: int, width: int, model: dict) -> None:
    canvas.box(y, x, height, width, "MACHINE")
    inner, slots = width - 6, model["slots"]
    load1 = model["averages"][0]
    memory = model["memory"]
    ram_status = machine_memory_status(memory)
    def usage_parts(used: float, total: float, style: str) -> list:
        label = f"used {megabytes(used)} of {megabytes(total)}"
        bar_width = min(12, max(0, inner - 10 - text_width(label) - 1))
        return bar(used / total, bar_width, style) + [(" " + label if bar_width else label, "text")]

    if memory["swap_total"] == 0:
        swap = ("off", "text")
    else:
        total = f" of {megabytes(memory['swap_total'])}" if memory["swap_total"] else ""
        swap = (f"{megabytes(memory['swap_free'])} free{total}", "text")
    swap_parts = [swap]
    if memory.get("swap_total") and memory.get("swap_free") is not None:
        used_swap = max(0, memory["swap_total"] - memory["swap_free"])
        swap_parts = usage_parts(used_swap, memory["swap_total"], "accent")
    used_memory, total_memory = memory.get("used"), memory.get("total")
    if used_memory is None and total_memory and memory.get("available") is not None:
        used_memory = max(0, total_memory - memory["available"])
    memory_parts = [("usage unavailable", "dim")]
    if total_memory and used_memory is not None:
        memory_style = {"status-normal": "accent", "bad": "bad"}.get(ram_status[1], "dim")
        memory_parts = usage_parts(used_memory, total_memory, memory_style)
    chart_width = max(1, inner)
    buckets = history_buckets(model["history"], chart_width)
    summary = history_summary(model["history"], slots)
    # (priority, name, parts): the lines a short panel drops first have the highest priority number.
    lines = [
        (0, "load 1m", bar(load1 / model["cores"], 12, "accent") + [(f" {load1:.1f} · {model['cores']} cores",
                                                                "text")]),
        (1, "memory", memory_parts),
        (3, "swap", swap_parts),
    ]
    room = height - 4
    while len(lines) + 6 > room and len(lines) > 1:
        lines.remove(max(lines, key=lambda line: line[0]))
    # Reserve a gap before/after the chart and a one-line summary.
    chart_rows = max(0, min(CHART_ROWS, room - len(lines) - 5))
    canvas.segments(y + 2, x + 3, machine_status(model, inner), inner)
    row = y + 4  # one gap between the status row and resource bars
    for _, name, parts in lines:
        canvas.put(row, x + 3, name, "dim")
        canvas.segments(row, x + 13, parts, inner - 10)
        row += 1
    if chart_rows:
        row += 1  # gap above the histogram
    # Eighth-block heights; the same blue-to-orange palette is tied to the capacity scale.
    for line in range(chart_rows):
        floor = (chart_rows - 1 - line) * 8
        for column, level in enumerate(buckets):
            eighths = math.ceil(level / slots * chart_rows * 8) - floor if level else 0
            glyph = SPARK[min(8, eighths) - 1] if eighths > 0 else " "
            height_fraction = min(1, level / slots) if chart_rows == 1 else (chart_rows - 1 - line) / (chart_rows - 1)
            colour = round(height_fraction * (len(CHART_COLOURS) - 1))
            canvas.put(row, x + 3 + column, glyph, f"chart{colour}" if eighths > 0 else "frame")
        row += 1
    if chart_rows:
        row += 1  # one gap row between histogram and legend
    if chart_rows and row < y + height - 2:
        canvas.put(row, x + 3, f"held/{slots}  {max(1, math.ceil(HISTORY_WINDOW / chart_width))}s/col  "
                                f"avg {summary['average']:.0%}  peak {summary['peak']}/{slots}  "
                                f"full {summary['full']:.0%}", "dim", inner)


def draw_events(canvas: Canvas, y: int, x: int, height: int, width: int, model: dict) -> None:
    placed = columns(width - 6, [("time", "time", 10, False, 0), ("event", "event", 8, False, 1),
                                 ("slots", "slots", 5, True, 2), ("detail", "detail", 12, False, 3),
                                 ("label", "label", 0, False, 0), ("pid", "pid", 6, True, 4)])
    table_header(canvas, y, x, height, width, "EVENTS", placed)
    styles = {"granted": "ok", "released": "accent", "left": "dim", "reaped": "bad"}
    events = model["events"][::-1]
    offset, visible = table_view(canvas, "events", y, x, height, width, y + 2, height - 4, len(events))
    if not events:
        canvas.put(y + 2, x + 3, "No grants, releases or reaps logged yet", "dim", width - 6)
    for row, event in enumerate(events[offset:offset + visible], y + 2):
        detail = "wait ended" if event["verb"] == "left" else "owner exited" if event["verb"] == "reaped" else ""
        draw_row(canvas, row, x + 3, placed, {
            "time": (clock(event["when"]), "dim"), "event": (event["verb"], styles[event["verb"]]),
            "label": event["label"] or "?", "slots": str(event["count"]),
            "detail": (detail, "dim"), "pid": str(event["pid"])})


HELP_CONTENT: list[tuple[str, str]] = [
    ("KEYS", "title"),
    ("In the dashboard", "dim"),
    ("↑/↓ or j/k: select an running or waiting request. Click a row to select it.", "text"),
    ("Home/End or g/G: first/last request. Wheel: scroll the table under the pointer.", "text"),
    ("p or space: pause updates. r: refresh. m: native text selection; updates freeze while copying.", "text"),
    ("In this help", "dim"),
    ("↑/↓ or j/k: scroll. PgUp/PgDown or space: page. Home/End or g/G: top/bottom.", "text"),
    ("Wheel: scroll help. ? or Esc: close help. q: quit the dashboard.", "text"),
    ("SLOTS", "title"),
    ("Each block is one slot: a unit of shared work capacity. Running requests hold slots; free slots are dim. Slots are a scheduling budget, not a CPU or memory limit.", "text"),
    ("One colour identifies one running request across its slots and row. Labels below the blocks name their owners; free marks unused capacity.", "text"),
    ("The header shows machine cores and shared slot capacity. manual means a saved capacity setting; auto means capacity calculated from the machine.", "text"),
    ("REQUESTS", "title"),
    ("Running requests come first, followed by a blank separator and waiting requests. ▶ and a soft background identify the selected row. Selection opens its REQUEST DETAILS.", "text"),
    ("status: running, legacy from an earlier queue, or waiting #N. N is the request's current place in the waiting queue.", "text"),
    ("slots is the held or requested slot count. time is elapsed run time for running work and elapsed wait time for waiting work. pid is the lease owner.", "text"),
    ("cpu use/share: observed cores used by the request / the core budget represented by its slots. A request can exceed its share; it is not a hard limit. Waiting requests show no CPU reading.", "text"),
    ("Yellow CPU indicates nearly idle work holding slots; red means use well above its share.", "text"),
    ("↑ above and ↓ more count rows outside the visible table. Scroll with the wheel or move the selection to reveal them.", "text"),
    ("REQUEST DETAILS", "title"),
    ("label is the caller-supplied name. status gives the run or wait state and slot count. idle for means sustained near-zero observed CPU while still holding slots.", "text"),
    ("For running work, cpu uses the same core scale as REQUESTS. granted shows when the lease started, how long it waited and how long it has run. memory is the process tree's resident memory.", "text"),
    ("owner pid is the process holding the lease. The process table includes it and its descendants, busiest first. Process CPU uses percent: 100% is one core.", "text"),
    ("command is the launched work; meta is caller-supplied context; cwd is its working directory.", "text"),
    ("For waiting work, queued shows when the request joined and how long it has waited. free is currently unused slot capacity; ahead counts earlier waiting requests and their requested slots.", "text"),
    ("start est is the first waiting request's estimated start. Later work may backfill if its predicted finish, including a safety margin, is before this time, or if it uses only slots the protected request will not need.", "text"),
    ("pass until: end of the current overtaking window. With start est shown, no later request may pass after this time. Without start est, stricter backfill rules may follow.", "text"),
    ("Example: start est 10:30, pass until 10:20. At 10:10, a later request may pass if it can finish before 10:30 with a margin. At 10:21, it cannot pass at all.", "text"),
    ("These are scheduling estimates and rules, not a promised start. Deadlines can change as the queue advances.", "text"),
    ("EVENTS", "title"),
    ("Recent queue events, newest first. time is the event time; slots is its slot count; label and pid identify the request. Scroll to read older events.", "text"),
    ("granted: the request received slots. released: it returned them. left: it stopped waiting. reaped: its owner exited and the stale request was removed.", "text"),
    ("detail adds context such as wait ended or owner exited. It is normally empty for grants and releases.", "text"),
    ("MACHINE", "title"),
    ("The status row summarizes memory, swap, estimated unleased demand and admission. White is normal; red identifies a condition limiting additional work.", "text"),
    ("load 1m: the system load averaged over one minute, shown beside the logical CPU count. The bar compares the two; load is not CPU utilization.", "text"),
    ("unleased load: estimated demand outside leased work. This is not the number of free cores.", "text"),
    ("admission: how many slots a generic new request can get now, after the queue and memory checks. It is not a worker count; free slots can still have no admission.", "text"),
    ("memory and swap bars show used capacity. Low memory or swap blocks additional leases; an idle machine may still start its first request.", "text"),
    ("The held chart shows occupied slots over time, newest on the right. Each column preserves its interval's peak; fractional heights show more than whole rows.", "text"),
    ("held/N is the slot scale; s/col is seconds per column. Blue is the bottom, orange is full capacity. avg is average occupancy; peak is the most held; full is time at capacity.", "text"),
    ("The dashboard only observes. It never grants, cancels or kills work.", "dim"),
]


def help_lines(width: int) -> list[tuple[str, str]]:
    """Wrap the same help document to the modal's available text width."""
    lines: list[tuple[str, str]] = []
    for text, style in HELP_CONTENT:
        if not text:
            continue
        if lines:
            lines.extend([("", "text")] * (2 if style == "title" else 1))
        wrapped = textwrap.wrap(text, max(1, width), break_long_words=True, break_on_hyphens=False)
        lines.extend((line, style) for line in wrapped)
    return lines


class HelpViewport(NamedTuple):
    x: int
    y: int
    width: int
    height: int
    offset: int
    visible: int
    total: int


def help_key(canvas: Canvas, key: str, scroll: dict[str, int]) -> str:
    """Help owns navigation while open; dashboard actions cannot leak through it."""
    if key in ("?", "esc"):
        return "close"
    if key in ("q", "Q"):
        return "quit"
    info = canvas.help_view
    if info is None:
        return "ignored"
    maximum = max(0, info.total - info.visible)
    offset = scroll.get("help", info.offset)
    page = max(1, info.visible - 1)
    if key in ("j", "down"):
        offset += 1
    elif key in ("k", "up"):
        offset -= 1
    elif key in ("page_down", " "):
        offset += page
    elif key == "page_up":
        offset -= page
    elif key in ("g", "home"):
        offset = 0
    elif key in ("G", "end"):
        offset = maximum
    else:
        return "ignored"
    scroll["help"] = max(0, min(offset, maximum))
    return "scroll"


def help_mouse(canvas: Canvas, event: tuple[str, int, int, int, bool], scroll: dict[str, int]) -> None:
    info = canvas.help_view
    if info is None:
        return
    _, button, x, y, pressed = event
    if not pressed or not (info.x < x < info.x + info.width - 1 and info.y < y < info.y + info.height - 1):
        return
    code = button & ~28
    if code in (64, 65):
        offset = scroll.get("help", info.offset) + (3 if code == 65 else -3)
        scroll["help"] = max(0, min(offset, max(0, info.total - info.visible)))


def draw_help(canvas: Canvas) -> None:
    width = min(max(0, canvas.width - 2), 70)
    lines = help_lines(width - 4)
    height = min(max(0, canvas.height - 2), len(lines) + 5)
    y, x = (canvas.height - height) // 2, (canvas.width - width) // 2
    visible = max(0, height - 5)
    offset = max(0, min(canvas.scroll.get("help", 0), max(0, len(lines) - visible)))
    canvas.scroll["help"] = offset
    canvas.help_view = HelpViewport(x, y, width, height, offset, visible, len(lines))
    canvas.fill(y, x, height, width)
    canvas.box(y, x, height, width, "HELP")
    inner = max(0, width - 4)
    if height < 6:
        canvas.put(y + 1, x + 2, "Enlarge terminal", "dim", inner)
        return
    if offset:
        canvas.put(y + 1, x + 2, f"↑ {offset} above", "dim", inner)
    for row, (text, style) in enumerate(lines[offset:offset + visible], y + 2):
        canvas.put(row, x + 2, text, style, inner)
    below = max(0, len(lines) - offset - visible)
    if below:
        canvas.put(y + height - 3, x + 2, f"↓ {below} more", "dim", inner)
    hint = ("↑↓ scroll  PgUp/PgDown page  Home/End  ?/Esc close  q quit" if inner >= 58 else
            "↑↓ scroll · ?/Esc close" if inner >= 23 else "↑↓  ?/Esc close" if inner >= 15 else "Esc close")
    canvas.put(y + height - 2, x + 2, hint, "key", inner)


def allocate(available: int, panels: list[tuple[str, int, int]]) -> dict[str, int]:
    """panels: (name, minimum, natural height) by priority. Each gets its minimum while room lasts, then grows
    towards its natural height; the last panel placed takes what remains."""
    heights = {}
    for name, minimum, _ in panels:
        if minimum <= available:
            heights[name], available = minimum, available - minimum
    for name, _, natural in panels:
        if name in heights:
            extra = min(available, max(0, natural - heights[name]))
            heights[name], available = heights[name] + extra, available - extra
    if heights:
        heights[list(heights)[-1]] += available
    return heights


def dashboard_draw(canvas: Canvas, model: dict | None, selected: int = 0, paused: bool = False,
                   help_open: bool = False, error: str | None = None, cwd: str | None = None,
                   scroll: dict | None = None, mouse_enabled: bool = True) -> None:
    canvas.scroll = scroll if scroll is not None else {}
    width, height = canvas.width, canvas.height
    x, status_end = draw_header(canvas, model, paused, error, copying=not mouse_enabled)
    keys = [("↑↓", "select"), ("p", "resume" if paused else "pause"), ("r", "refresh"),
            ("m", "select text" if mouse_enabled else "mouse on"), ("?", "help"), ("q", "quit")]
    for key, action in keys:
        if x + text_width(key + action) + 2 > status_end:
            break
        x = canvas.segments(height - 1, x, [(key, "key"), (f" {action}  ", "dim")])
    if error:
        canvas.put(height - 1, x + 1, f"refresh failed: {error}", "bad", max(0, status_end - x - 1))
    if model is None:
        canvas.put(2, 3, f"Queue unavailable: {error}" if error else "Reading the queue…",
                   "bad" if error else "dim", width - 6)
        if help_open:
            draw_help(canvas)
        return
    moment = datetime.datetime.now(datetime.timezone.utc)
    if width < 50 or height < 14:
        draw_slots(canvas, min(2, height - 4), 3, width - 6, model)
        if 6 < height - 1:
            canvas.put(6, 3, "Enlarge the terminal for leases and the queue", "dim", width - 6)
        if help_open:
            draw_help(canvas)
        return
    left = width - max(52, width * 2 // 5) - 1 if width >= WIDE else width
    slot_height = 5
    canvas.box(0, 0, slot_height, left, "SLOTS", f"{model['cores']} cores · {model['slots']} slots "
               + ("(manual)" if model["saved"] else "(auto)"))
    draw_slots(canvas, 2, 3, left - 6, model)
    top, available = slot_height, height - 1 - slot_height
    holders, waiters = model["holders"], model["waiters"]
    requests = max(1, len(holders) + len(waiters)) + bool(holders and waiters) + 4
    if width >= WIDE:
        heights = allocate(available, [("requests", min(requests, 10), requests), ("events", 5, available)])
        y = top
        for name in ("requests", "events"):
            if name in heights:
                draw_panel(canvas, name, y, 0, heights[name], left, model, selected, cwd, moment)
                y += heights[name]
        right_available = height - 1
        machine = min(MACHINE_HEIGHT, right_available - 10) if right_available >= 16 else 0
        right = {"detail": right_available - machine, "machine": machine}
        y = 0
        for name in ("detail", "machine"):
            if right[name]:
                draw_panel(canvas, name, y, left + 1, right[name], width - left - 1, model, selected, cwd, moment)
                y += right[name]
    else:
        heights = allocate(available, [("requests", min(requests, 10), requests),
                                       ("detail", 5, 10), ("machine", 6, MACHINE_HEIGHT),
                                       ("events", 5, available)])
        y = top
        for name in ("requests", "detail", "machine", "events"):
            if name in heights:
                draw_panel(canvas, name, y, 0, heights[name], width, model, selected, cwd, moment)
                y += heights[name]
    if help_open:
        draw_help(canvas)


def draw_panel(canvas: Canvas, name: str, y: int, x: int, height: int, width: int, model: dict, selected: int,
               cwd: str | None, moment: datetime.datetime) -> None:
    if name == "requests":
        draw_requests(canvas, y, x, height, width, model, selected, moment)
    elif name == "detail":
        draw_detail(canvas, y, x, height, width, model, selected, cwd, moment)
    elif name == "machine":
        draw_machine(canvas, y, x, height, width, model)
    else:
        draw_events(canvas, y, x, height, width, model)


# Dashboard terminal: plain escape sequences, not curses. The ncurses that Python links (6.0 here) repeats a
# multibyte character with REP by sending only its last byte, and terminals with REP in their terminfo, such as
# Ghostty and cmux, then draw each run of █ or ─ as garbage. Writing the canvas ourselves avoids every such quirk.

KEYS = {"\x1b[A": "up", "\x1bOA": "up", "\x1b[B": "down", "\x1bOB": "down", "\x1b[H": "home", "\x1bOH": "home",
        "\x1b[1~": "home", "\x1b[F": "end", "\x1bOF": "end", "\x1b[4~": "end", "\x1b[5~": "page_up", "\x1b[6~": "page_down"}
KEY_PENDING: dict[int, str] = {}
MOUSE = re.compile(r"\x1b\[<(\d+);(\d+);(\d+)([Mm])")
SEQUENCE = re.compile(r"\x1b(\[[0-9;?<]*[ -/]*[@-~]|O.)")
PARTIAL = re.compile(r"\x1b(\[(?:<[0-9;]*|[0-9;?]*[ -/]*)|O)?$")


def read_keys(fd: int) -> list[str | tuple[str, int, int, int, bool]]:
    """Keys waiting on fd: printable characters, the names in KEYS, or esc. A sequence split across reads is
    completed first; one this view does not know is dropped rather than read as Esc and its letters."""
    data = KEY_PENDING.pop(fd, "") + os.read(fd, 1024).decode("utf-8", "ignore")
    while PARTIAL.search(data) and select.select([fd], [], [], 0.03)[0]:
        chunk = os.read(fd, 1024)
        if not chunk:
            break
        data += chunk.decode("utf-8", "ignore")
    partial = PARTIAL.search(data)
    if partial and partial.group() != "\x1b":
        KEY_PENDING[fd] = partial.group()
        data = data[:partial.start()]
    found: list[str | tuple[str, int, int, int, bool]] = []
    index = 0
    while index < len(data):
        mouse = MOUSE.match(data, index)
        if mouse:
            button, x, y, action = mouse.groups()
            found.append(("mouse", int(button), int(x) - 1, int(y) - 1, action == "M"))
            index = mouse.end()
            continue
        name = next((KEYS[seq] for seq in KEYS if data.startswith(seq, index)), None)
        sequence = SEQUENCE.match(data, index)
        if name or sequence:
            if name:
                found.append(name)
            index = sequence.end() if sequence else index + 1
        else:
            found.append("esc" if data[index] == "\x1b" else data[index])
            index += 1
    return found


@contextlib.contextmanager
def full_screen(fd_in: int, fd_out: int):
    """The alternate screen with the cursor hidden, line wrap off and keys read one at a time without echo; Ctrl-C
    still interrupts. Everything is put back on the way out."""
    saved = termios.tcgetattr(fd_in)
    try:
        tty.setcbreak(fd_in)
        os.write(fd_out, b"\x1b[?1049h\x1b[?25l\x1b[?7l\x1b[?1000h\x1b[?1006h\x1b[2J")
        yield
    finally:
        KEY_PENDING.pop(fd_in, None)
        os.write(fd_out, b"\x1b[?1006l\x1b[?1000l\x1b[0m\x1b[?7h\x1b[?25h\x1b[?1049l")
        termios.tcsetattr(fd_in, termios.TCSADRAIN, saved)


def dashboard_live(interval: float) -> None:
    fd_in, fd_out = sys.stdin.fileno(), sys.stdout.fileno()
    colour = not os.environ.get("NO_COLOR")
    ascii_only = "utf" not in (sys.stdout.encoding or "").lower()
    model = error = selected_key = None
    index, colours, cwds, shown, drawn_size = 0, {}, {}, [], None
    scroll: dict[str, int] = {}
    idle_seen = {}
    mouse_enabled = True
    paused = help_open = False
    fetched, drawn, whole, force, dirty = 0.0, 0.0, 0.0, True, True
    canvas = Canvas(0, 0)
    with full_screen(fd_in, fd_out):
        while True:
            if force or (mouse_enabled and not paused and time.monotonic() - fetched >= interval):
                try:
                    fresh = dashboard_model()
                    dashboard_colours(fresh, colours)
                    dashboard_idle(fresh, idle_seen)
                    model, error = fresh, None
                except (_config.SlotError, OSError) as failure:
                    clear_idle(model, idle_seen)
                    error = dashboard_text(failure)
                fetched, force, dirty = time.monotonic(), False, True
            items = (model["holders"] + model["waiters"]) if model else []
            keys = [item["key"] for item in items]
            index = keys.index(selected_key) if selected_key in keys else max(0, min(index, len(items) - 1))
            next_key = keys[index] if items else None
            if next_key != selected_key:
                scroll.pop("processes", None)
            selected_key = next_key
            if items and items[index]["kind"] != "waiting" and items[index]["pid"] not in cwds:
                cwds[items[index]["pid"]] = process_cwd(items[index]["pid"])
            size = os.get_terminal_size(fd_out)  # read each pass: at most 0.25 s behind a resize
            if size != drawn_size:
                # A terminal reflows its own screen on resize: clear it and draw every row.
                os.write(fd_out, b"\x1b[2J")
                shown, drawn_size, dirty = [], size, True
            elif mouse_enabled and time.monotonic() - whole >= 5:
                shown = []  # rewrite every row now and then, in place, so a stray line cannot stay wrong
            if dirty or (mouse_enabled and time.monotonic() - drawn >= 1) or not shown:
                canvas = Canvas(size.columns, size.lines)
                dashboard_draw(canvas, model, index, paused, help_open, error,
                               cwds.get(items[index]["pid"]) if items else None, scroll, mouse_enabled)
                lines = canvas.lines(colour, ascii_only)
                # Only rows that changed are written: a second's tick usually touches the clock and a few ages.
                frame = "".join(f"\x1b[{row + 1};1H{line}" for row, line in enumerate(lines)
                                if row >= len(shown) or shown[row] != line)
                if frame:
                    os.write(fd_out, frame.encode("utf-8", "replace"))
                whole = time.monotonic() if not shown else whole
                shown, drawn, dirty = lines, time.monotonic(), False
            if not select.select([fd_in], [], [], 0.25)[0]:
                continue
            for key in read_keys(fd_in):
                dirty = True
                if isinstance(key, tuple):
                    if mouse_enabled:
                        previous = index
                        index = mouse_action(canvas, key, scroll, index)
                        if index != previous:
                            scroll.pop("processes", None)
                        selected_key = keys[index] if items else None
                    continue
                if help_open:
                    action = help_key(canvas, key, scroll)
                    if action == "quit":
                        return
                    if action == "close":
                        help_open = False
                        canvas.help_view = None
                    continue
                if key in ("j", "down", "k", "up", "g", "home", "G", "end", "page_up", "page_down"):
                    scroll.pop("requests", None)
                    scroll.pop("processes", None)
                if key in ("q", "Q"):
                    return
                if key == "esc":
                    help_open = False
                elif key == "?":
                    help_open = True
                    scroll.pop("help", None)
                    draw_help(canvas)
                elif key in ("m", "M"):
                    clear_idle(model, idle_seen)
                    mouse_enabled = not mouse_enabled
                    os.write(fd_out, b"\x1b[?1000h\x1b[?1006h" if mouse_enabled
                             else b"\x1b[?1006l\x1b[?1000l")
                    force = mouse_enabled
                elif key in ("p", " "):
                    clear_idle(model, idle_seen)
                    paused = not paused
                elif key in ("r", "R"):
                    shown, force = [], True
                    os.write(fd_out, b"\x1b[2J")
                elif key in ("j", "down"):
                    index += 1
                elif key in ("k", "up"):
                    index -= 1
                elif key in ("g", "home", "page_up"):
                    index = 0
                elif key in ("G", "end", "page_down"):
                    index = len(items) - 1
                index = max(0, min(index, len(items) - 1))
                selected_key = keys[index] if items else None


def dashboard_command(argv: list[str]) -> int:
    interval, once, colour, width, height = 1.0, False, None, None, None
    while argv:
        if argv[0] == "--once":
            once, argv = True, argv[1:]
        elif argv[0] == "--color":
            colour, argv = True, argv[1:]
        elif argv[0] in ("--interval", "--width", "--height") and len(argv) > 1:
            try:
                value = float(argv[1])
            except ValueError:
                value = math.nan
            if argv[0] == "--interval":
                if not 0.1 <= value <= 60:
                    raise _config.SlotError("dashboard --interval takes seconds from 0.1 to 60")
                interval = value
            elif not (value.is_integer() and 5 <= value <= 1000):
                raise _config.SlotError(f"dashboard {argv[0]} takes a whole number of cells from 5 to 1000")
            elif argv[0] == "--width":
                width = int(value)
            else:
                height = int(value)
            argv = argv[2:]
        else:
            raise _config.SlotError("dashboard takes [--interval SECONDS] or --once [--width N] [--height N] [--color]")
    if once:
        model = dashboard_model()
        dashboard_colours(model, {})
        items = model["holders"] + model["waiters"]
        size_now = shutil.get_terminal_size((120, 40))
        canvas = Canvas(width or size_now.columns, height or size_now.lines)
        dashboard_draw(canvas, model, cwd=process_cwd(items[0]["pid"]) if items and items[0]["kind"] != "waiting"
                       else None)
        tty = sys.stdout.isatty() and not os.environ.get("NO_COLOR")
        print(canvas.render(colour if colour is not None else tty,
                            "utf" not in (sys.stdout.encoding or "").lower()))
        return 0
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise _config.SlotError("dashboard needs an interactive terminal; dashboard --once prints one snapshot")
    try:
        dashboard_live(interval)
    except KeyboardInterrupt:
        pass
    return 0


def main(argv: list[str] | None = None) -> int:
    try:
        return dashboard_command(list(sys.argv[1:] if argv is None else argv))
    except _config.SlotError as error:
        print(f"slotfather dashboard: {error}", file=sys.stderr)
        return 2
