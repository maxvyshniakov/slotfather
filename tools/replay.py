#!/usr/bin/env python3
"""Replay the recorded queue through queue orders and compare the waits (#643).

  python3 tools/replay.py [--dir STATE] [--now ISO] [--slots N] [--since ISO] [--until ISO] [--json]

Reads history.jsonl (arrival, slots, run and label of every request that left the queue), queue.log (grants and
releases: they also give the requests whose slotfather recorded no history, placed in arrival order by their queue
sequence) and the live queue (holders still running count as running until --now; the runs of waiters, and of
requests that gave up but start in a replay, are the median of their kind). Every order runs on the same requests
in an event simulation: arrivals, ends, give-ups (a request that left the queue gives up after the wait it had)
and a look every POLL seconds while anyone waits, as waiters do. The memory guard is ignored. `window` and
`backfill` call the queue's own plan(), and yields its yield_asks(); the other orders are written here and were
not shipped. Recorded requests carry no class, so classify() gives one from the label and command, and yieldable()
says whose client would yield (Straylight and Sid-Kit gates); an asked holder yields at the end of its step, a
uniform time up to the policy's `step` seconds, and queues again with the rest of its run. It prints the
current order's replay against the recorded waits, then per order the waits of the requests that arrived from
--since (default the first request in the history) to --until, and the slot-hours idle meanwhile while someone
waited, and while a waiter fitted the idle slots. Reads only; run it from a checkout.
"""
from __future__ import annotations

import argparse
import bisect
import heapq
import importlib.machinery
import importlib.util
import json
import math
import os
from pathlib import Path
import random
import re
import sys
import time


def load_core():
    path = Path(__file__).resolve().parent.parent / "slotfather"
    loader = importlib.machinery.SourceFileLoader("slotfather_core", str(path))
    module = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
    loader.exec_module(module)
    return module


sf = load_core()
POLL = 10  # seconds between looks while anyone waits (SLOTFATHER_POLL_MAX)
ASSUMED_RUN = 600  # seconds a request that never ran and is like nothing else runs once the replay grants it


def stamp(moment: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(moment))


# The recorded requests

def log_requests(path: Path) -> list[dict]:
    """The requests queue.log saw leave, by queue sequence: {seq, pid, label, count, granted, ended}; `granted` is None
    for a waiter that left without a grant (its label is then unknown). Reaped leases have no sequence."""
    grants, found = {}, []
    try:
        lines = path.read_text(errors="replace").splitlines()
    except FileNotFoundError:
        return []
    for line in lines:
        when, rest = sf.seconds(line[:20]), line[21:]
        if when is None:
            continue
        match = re.match(r"granted (\d+) to (.*) \(pid (\d+)\)$", rest)
        if match:
            grants[int(match[3])] = (when, int(match[1]), match[2])
            continue
        match = re.match(r"released (\d+) \((\d+)-(\d+)\.json\)$", rest)
        if match:
            pid = int(match[3])
            granted, _, label = grants.pop(pid, (None, None, None))
            found.append({"seq": int(match[2]), "pid": pid, "label": label, "count": int(match[1]),
                          "granted": granted, "ended": when})
            continue
        match = re.match(r"reaped held lease of (\d+) for (.*) \(pid (\d+) exited\)$", rest)
        if match and int(match[3]) in grants:
            granted, count, label = grants.pop(int(match[3]))
            found.append({"seq": None, "pid": int(match[3]), "label": label, "count": count, "granted": granted,
                          "ended": when})
    return found


def requests(directory: Path, moment: float) -> list[dict]:
    """The recorded requests, in arrival order: {label, cmd, count, arrival, run, patience, waited, source, outcome}.
    `run` is None where it never ran (estimated in the replay); `patience` is how long a request that left the queue
    waited before it gave up; `waited` is the recorded wait where known; `outcome` is the history's (a reaped lease
    holds its slots in the replay as it did, but its time is no run of its kind: the estimates leave it out).

    history.jsonl gives arrivals. A request it lacks (its holder ran a slotfather that records no history) is
    known from queue.log by its queue sequence: it arrived after the request before it in the sequence and before
    the one after it; one that was granted is taken to arrive at the latest such moment (its wait is a lower
    bound), one that left without a grant half way between."""
    found, by_pid = [], {}
    for item in sf.journal(directory / "history.jsonl"):
        arrival = sf.seconds(item.get("queued"))
        if arrival is None or not isinstance(item.get("count"), int):
            continue
        granted = sf.seconds(item.get("granted"))
        request = {"label": item.get("label"), "cmd": item.get("cmd"), "count": item["count"], "arrival": arrival,
                   "run": item.get("ran") if granted is not None else None,
                   "patience": item.get("waited") if granted is None else None,
                   "waited": item.get("waited"), "source": "history", "outcome": item.get("outcome")}
        found.append(request)
        by_pid.setdefault(item.get("pid"), []).append((sf.seconds(item.get("ended")), request))
    start = min((r["arrival"] for r in found), default=moment)
    arrivals, unknown = {}, []
    for left in log_requests(directory / "queue.log"):
        known = [r for ended, r in by_pid.get(left["pid"], ()) if ended is not None and abs(ended - left["ended"]) <= 2]
        if known:
            if left["seq"] is not None:
                arrivals[left["seq"]] = known[0]["arrival"]
        elif left["ended"] >= start:
            unknown.append(left)
    live = []
    if (directory / "queue").is_dir():
        for path, entry in sf.entries(directory):
            arrival = sf.seconds(entry.get("since"))
            if arrival is None:
                continue
            granted = sf.seconds(entry.get("granted"))
            with_seq = path.name.split("-")[0]
            if with_seq.isdigit():
                arrivals[int(with_seq)] = arrival
            live.append({"label": entry.get("label"), "cmd": entry.get("cmd"), "count": entry["count"],
                         "arrival": arrival, "run": None if granted is None else max(0.0, moment - granted),
                         "patience": None, "waited": None if granted is None else granted - arrival,
                         "source": "live", "outcome": None})
    known_seqs = sorted(arrivals)

    def bounds(seq):
        at = bisect.bisect_left(known_seqs, seq)
        low = arrivals[known_seqs[at - 1]] if at else None
        high = arrivals[known_seqs[at]] if at < len(known_seqs) else None
        return low, high

    for left in unknown:
        low, high = bounds(left["seq"]) if left["seq"] is not None else (None, None)
        if left["granted"] is not None:
            arrival = left["granted"] if high is None else min(high, left["granted"])
            found.append({"label": left["label"], "cmd": None, "count": left["count"], "arrival": arrival,
                          "run": left["ended"] - left["granted"], "patience": None, "waited": None, "source": "log",
                          "outcome": "released" if left["seq"] is not None else "reaped"})
        else:
            high = left["ended"] if high is None else min(high, left["ended"])
            arrival = high if low is None else (low + high) / 2
            found.append({"label": "(left, unrecorded)", "cmd": None, "count": left["count"], "arrival": arrival,
                          "run": None, "patience": left["ended"] - arrival, "waited": None, "source": "log",
                          "outcome": "left"})
    found += live
    found.sort(key=lambda r: r["arrival"])
    for number, request in enumerate(found):
        request["id"] = number
    return found


def seed_runs(directory: Path, before: float) -> list[tuple]:
    """Runs that ended before the replay starts, for the estimates: queue.log holds (label only); reaped leases
    (no sequence) are left out, as the queue's estimates leave them out."""
    return [(r["label"], None, r["ended"] - r["granted"]) for r in log_requests(directory / "queue.log")
            if r["granted"] is not None and r["seq"] is not None and r["ended"] < before]


# Classes for the replay: no recorded request carried one, so they come from its label and command

SINGLE_TEST = re.compile(r"(--test-name-pattern|-p test_\S+\.py|\s-k\s|\S+::\S+)")
TEST_RUNNERS = ("node --test", "npx vitest", "bunx vitest", "vitest", "bun test", "pytest", "cargo test")
MEASURING = re.compile(r"measure|baseline|bench|profile|stress|repro|probe|-alone\b|mem\b")


def classify(request: dict, slots: int) -> str:
    """interactive: a pre-push check or a single test (one test file, a name pattern, one tool test); batch: a full
    gate (verify with a check list of 50 or more, a post-merge smoke, Sid-Kit's verify.mjs, a lane check of an old
    client, which ran full gates), and measurements (every slot, or a label that says so); normal otherwise."""
    label, command = request.get("label") or "", request.get("cmd") or ""
    what = sf.kind(command)
    if "pre-push" in command or label.startswith("publish"):
        return "interactive"
    if request["count"] >= slots or MEASURING.search(label):
        return "batch"
    if SINGLE_TEST.search(command):
        return "interactive"
    words = command.split()
    files = [w for w in words if re.search(r"\.(test|spec)\.[cm]?[jt]sx?$|(^|/)test[-_]\S+\.py$", w)]
    if any(what.startswith(r) for r in TEST_RUNNERS) and len(files) == 1:
        return "interactive"
    if re.fullmatch(r"python3 (tools/)?test[-_]\S+\.py", what):
        return "interactive"
    checks = re.search(r"--checks \[(\d+)\]", what)
    if (checks and int(checks[1]) >= 50) or "--suites" in what or "verify.mjs" in what:
        return "batch"
    family = sf.family(label)
    if family in ("post-merge", "post-merge-pin-ui-libs", "ci-baseline-cold", "ci-baseline-warm"):
        return "batch"
    if family.startswith("lane") and not command:
        return "batch"  # a lane check through Straylight's vendored client: its full gate
    return "normal"


def yieldable(request: dict, cls: str, slots: int) -> bool:
    """Whether the request's client would implement the yield protocol: a batch gate of Straylight's verify or
    Sid-Kit's verify.mjs, not a measurement (which must not be interrupted)."""
    label = request.get("label") or ""
    return cls == "batch" and request["count"] < slots and not MEASURING.search(label)


# Orders written only here (rejected)

def short_first(limit: float, bonus: float):
    """A short class by estimate: a waiter expected to run under `limit` seconds queues as if it came `bonus` seconds
    earlier, so long work that has waited longer than the bonus is still ahead of it (ageing). Returns the waiters
    in that order (for the window order, which serves them as given) and the bonus; under the backfill order the
    short ones go to plan() as interactive, `bonus` seconds ahead."""
    def order(waiters, estimate):
        def short(entry):
            run = estimate(entry)
            return run is not None and run <= limit
        ordered = sorted(waiters, key=lambda e: (sf.seconds(e["since"]) - (bonus if short(e) else 0), e["id"]))
        return [dict(e, meta={"class": "interactive" if short(e) else "normal"}) for e in ordered], bonus
    return order


def plan_all_only(waiters, held, slots, moment, estimate, settings):
    """Backfill around an all-slot waiter only; any other waiter that does not fit keeps the window order."""
    first = sf.plan(waiters, held, slots, moment, "window", settings=settings)
    if not any(e["count"] >= slots for e in waiters):
        return first
    backfill = sf.plan(waiters, held, slots, moment, "backfill", estimate, settings=settings)
    if backfill.head is not None and backfill.head["count"] >= slots:
        return backfill
    return first


# The simulation

class Replay:
    def __init__(self, requests: list[dict], slots: int, policy: dict, seed: list[tuple] = (),
                 assumed: dict | None = None):
        self.requests, self.slots, self.policy = requests, slots, policy
        self.estimates = sf.Estimates(seed)
        self.assumed = assumed or {}
        self.classes = [classify(r, slots) for r in requests]
        self.asks = self.yields = 0

    def run_of(self, request: dict) -> float:
        """The run a request has in the replay: recorded, or for one that never ran the median of its kind over the
        whole history, else ASSUMED_RUN seconds."""
        if request["run"] is not None:
            return float(request["run"])
        found = self.assumed.get("estimates").of(request["label"], request["cmd"]) if self.assumed else None
        return float(found if found is not None else ASSUMED_RUN)

    def estimate(self, entry: dict) -> float | None:
        if self.policy.get("oracle"):
            return self.run_of(self.requests[entry["id"]])
        found = self.estimates.of(entry.get("label"), entry.get("cmd"))
        bias = self.policy.get("bias", 1.0)
        if found is not None and self.policy.get("noise"):  # one error per request, as a kind's estimate would be
            error = random.Random(f"{self.policy.get('seed', 1)}-{entry['id']}").gauss(0, self.policy["noise"])
            found *= math.exp(error)
        return None if found is None else found * bias

    def settings(self) -> dict:
        """The queue's defaults before classes (no class moves, nobody asked), and what the policy changes."""
        return {**sf.ORDER_DEFAULTS, "interactive": 0.0, "batch": 0.0, "yield_after": 0.0,
                **self.policy.get("settings", {})}

    def step(self, number: int) -> float:
        """How long the holder takes to reach the end of its current step once asked: uniform up to the policy's
        `step` seconds, fixed per request."""
        return random.Random(f"step-{number}").uniform(0, self.policy.get("step", 120))

    def decide(self, waiting: list[dict], held: list[dict], moment: float) -> list[dict]:
        """The grants of the policy's order with its settings (the queue's defaults, and what it changes)."""
        name = self.policy["order"]
        settings = self.settings()
        if name == "all-only":
            return plan_all_only(waiting, held, self.slots, moment, self.estimate, settings).grants
        if name.startswith("short"):
            ordered, bonus = self.policy["reorder"](waiting, self.estimate)
            inner = "backfill" if name == "short+backfill" else "window"
            found = sf.plan(ordered, held, self.slots, moment, inner, self.estimate,
                            settings={**settings, "interactive": bonus, "batch": 0.0}).grants
            by_id = {e["id"]: e for e in waiting}
            return [by_id[e["id"]] for e in found]
        return sf.plan(waiting, held, self.slots, moment, name, self.estimate, settings=settings).grants

    def simulate(self) -> list[dict]:
        environment = {k: os.environ.get(k) for k in self.policy.get("env", {})}
        os.environ.update(self.policy.get("env", {}))
        askers, sf.ASKERS = sf.ASKERS, self.policy.get("askers", sf.ASKERS)
        try:
            return self._simulate()
        finally:
            sf.ASKERS = askers
            for key, value in environment.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    def _simulate(self) -> list[dict]:
        events, order = [], 0
        for request in self.requests:
            heapq.heappush(events, (request["arrival"], 0, order, "arrive", request["id"]))
            order += 1
        waiting, held = {}, {}
        result = {r["id"]: {"id": r["id"], "granted": None, "ended": None, "left": None, "yields": 0,
                            "yielded": 0.0, "class": self.classes[r["id"]]} for r in self.requests}
        remaining = {}  # id -> seconds of its run still to go when it was last granted
        began = {}  # id -> moment of its last grant
        generation = {}  # id -> grant count, so an end scheduled before a yield is ignored
        asked = {}  # holder id -> moment it was asked
        segments = []  # (from, to, slots held, someone waits, a waiter fits the free slots)
        last = None
        ticking = False
        while events:
            moment, _, _, kind, number, *rest = heapq.heappop(events)
            extra = rest[0] if rest else None
            if last is not None and moment > last:
                used = sum(e["count"] for e in held.values())
                segments.append((last, moment, used, bool(waiting),
                                 any(e["count"] <= self.slots - used for e in waiting.values())))
            last = moment
            request = self.requests[number] if number is not None else None
            if kind == "arrive":
                cls = self.classes[number]
                waiting[number] = {"id": number, "label": request["label"], "cmd": request["cmd"],
                                   "count": min(request["count"], self.slots), "since": stamp(request["arrival"]),
                                   "state": "waiting", "meta": {"class": cls},
                                   "yieldable": yieldable(request, cls, self.slots)}
                remaining[number] = self.run_of(request)
                if request["patience"] is not None:
                    heapq.heappush(events, (moment + request["patience"], 1, order, "leave", number))
                    order += 1
            elif kind == "leave":
                if number in waiting:
                    del waiting[number]
                    result[number]["left"] = moment
            elif kind == "end":
                if generation.get(number) != extra or number not in held:
                    continue  # it yielded before this end
                entry = held.pop(number)
                asked.pop(number, None)
                if request.get("outcome") not in sf.UNFINISHED:  # as history_estimates() leaves them out
                    self.estimates.add(entry["label"], entry["cmd"], self.run_of(request))
                result[number]["ended"] = moment
            elif kind == "yield":
                if number not in held or number not in asked or generation.get(number) != extra or \
                        asked[number] not in waiting:
                    continue  # the ask was withdrawn, or it ended first, or the asker started or left meanwhile
                entry = held.pop(number)
                entry["rank"] = sf.rank_time(waiting[asked.pop(number)], self.settings()) + sf.YIELD_BEHIND
                remaining[number] -= moment - began[number]
                entry.update(state="waiting", yields=int(entry.get("yields") or 0) + 1, yielded_at=moment)
                waiting[number] = entry
                result[number]["yields"] += 1
                self.yields += 1
            elif kind == "tick":
                ticking = False
            if waiting:
                ordered = sorted(waiting.values(), key=lambda e: (sf.seconds(e["since"]), e["id"]))
                for entry in self.decide(ordered, list(held.values()), moment):
                    number = entry["id"]
                    del waiting[number]
                    if "yielded_at" in entry:  # back after a yield: as the queue keeps it, granted stays the first
                        result[number]["yielded"] += moment - entry.pop("yielded_at")
                        entry.update(yielded=result[number]["yielded"], resumed=stamp(moment),
                                     granted=stamp(result[number]["granted"]))
                    else:
                        entry.update(granted=stamp(moment))
                    entry["state"] = "held"
                    held[number] = entry
                    began[number] = moment
                    generation[number] = generation.get(number, 0) + 1
                    if result[number]["granted"] is None:
                        result[number]["granted"] = moment
                    heapq.heappush(events, (moment + remaining[number], -1, order, "end", number,
                                            generation[number]))
                    order += 1
                settings = self.settings()
                if settings["yield_after"] and waiting:
                    ordered = sorted(waiting.values(), key=lambda e: (sf.seconds(e["since"]), e["id"]))
                    holders = list(held.values())
                    wanted = sf.yield_asks(ordered, holders, self.slots, moment, self.estimate, settings=settings,
                                           asked=[held[n] for n in asked])
                    chosen = {h["id"]: w["id"] for h, w in wanted}
                    for number in [n for n in asked if n not in chosen]:
                        del asked[number]  # withdrawn
                    for number, asker in chosen.items():
                        known = number in asked
                        asked[number] = asker
                        if known:
                            continue
                        self.asks += 1
                        delay = min(self.step(number), max(0.0, remaining[number] - (moment - began[number])))
                        heapq.heappush(events, (moment + delay, 0, order, "yield", number, generation[number]))
                        order += 1
            if waiting and not ticking:
                heapq.heappush(events, (moment + POLL, 2, order, "tick", None))
                order += 1
                ticking = True
        self.segments = segments
        return [result[r["id"]] for r in self.requests]


# Comparison

def percentile(values: list[float], q: float) -> float:
    values = sorted(values)
    if not values:
        return 0.0
    at = (len(values) - 1) * q / 100
    low = math.floor(at)
    high = min(low + 1, len(values) - 1)
    return values[low] + (values[high] - values[low]) * (at - low)


def measure(requests: list[dict], results: list[dict], start: float, until: float, replay: Replay) -> dict:
    waits, large, left = [], [], 0
    classes = {name: {"waits": [], "delays": []} for name in sf.CLASSES}
    yielded = yields = 0.0
    for request, result in zip(requests, results):
        if request["arrival"] < start or request["arrival"] > until:
            continue
        end = result["granted"] if result["granted"] is not None else result["left"]
        if end is None:
            continue
        wait = end - request["arrival"]
        waits.append(wait)
        classes[result["class"]]["waits"].append(wait)
        classes[result["class"]]["delays"].append(wait + result["yielded"])  # every second it was not running
        yielded += result["yielded"] * request["count"]
        yields += result["yields"]
        if request["count"] >= 3:
            large.append(wait)
        left += result["granted"] is None
    idle = blocked = held = 0.0
    for begin, end, used, someone, fits in replay.segments:
        span = min(end, until) - max(begin, start)
        if span > 0:
            held += span * used
            idle += span * (replay.slots - used) * someone
            blocked += span * (replay.slots - used) * fits
    per_class = {name: {"requests": len(c["waits"]), "total_h": sum(c["waits"]) / 3600,
                        "mean_s": sum(c["waits"]) / max(1, len(c["waits"])), "p90_s": percentile(c["waits"], 90),
                        "max_s": max(c["waits"], default=0), "delay_max_s": max(c["delays"], default=0),
                        "delay_total_h": sum(c["delays"]) / 3600}
                 for name, c in classes.items()}
    return {"classes": per_class, "yields": int(yields), "yielded_slot_h": yielded / 3600,
            "requests": len(waits), "total_h": sum(waits) / 3600, "mean_s": sum(waits) / max(1, len(waits)),
            "p90_s": percentile(waits, 90), "max_s": max(waits, default=0),
            "large_mean_s": sum(large) / max(1, len(large)), "large_max_s": max(large, default=0),
            "idle_slot_h": idle / 3600, "blocked_slot_h": blocked / 3600,
            "held_mean": held / max(1.0, until - start), "never_ran": left}


WINDOW = {"SLOTFATHER_OVERTAKE_WINDOW": "600"}
CLASSES = {"interactive": 1800, "batch": 1800}  # the shipped class offsets
SHIPPED = {**CLASSES, "yield_after": 120}
POLICIES = {
    "window (current)": {"order": "window", "env": WINDOW},
    "fifo (window 0)": {"order": "window", "env": {"SLOTFATHER_OVERTAKE_WINDOW": "0"}},
    "backfill (shipped)": {"order": "backfill", "env": WINDOW},
    "classes + yields (#643 shipped)": {"order": "backfill", "env": WINDOW, "settings": SHIPPED, "step": 300},
    "  classes only": {"order": "backfill", "env": WINDOW, "settings": CLASSES},
    "  yields only": {"order": "backfill", "env": WINDOW, "settings": {"yield_after": 120}, "step": 300},
    "  interactive ahead only": {"order": "backfill", "env": WINDOW, "settings": {"interactive": 1800}},
    "  batch behind only": {"order": "backfill", "env": WINDOW, "settings": {"batch": 1800}},
    "  offsets 600 s": {"order": "backfill", "env": WINDOW, "step": 300,
                        "settings": {"interactive": 600, "batch": 600, "yield_after": 120}},
    "  batch 3600 s behind": {"order": "backfill", "env": WINDOW, "step": 300,
                              "settings": {**SHIPPED, "batch": 3600}},
    "  normal waiters ask too": {"order": "backfill", "env": WINDOW, "settings": SHIPPED, "step": 300,
                                 "askers": ("interactive", "normal")},
    "  steps up to 600 s": {"order": "backfill", "env": WINDOW, "settings": SHIPPED, "step": 600},
    "  estimates noisy": {"order": "backfill", "env": WINDOW, "settings": SHIPPED, "step": 300, "noise": 1.0},
    "backfill, no window": {"order": "backfill", "env": {"SLOTFATHER_OVERTAKE_WINDOW": "0"}},
    "backfill, safety 1.5": {"order": "backfill", "env": WINDOW, "settings": {"safety": 1.5}},
    "backfill, safety 2": {"order": "backfill", "env": WINDOW, "settings": {"safety": 2}},
    "backfill, safety 2.5": {"order": "backfill", "env": WINDOW, "settings": {"safety": 2.5}},
    "  safety 2, estimates noisy": {"order": "backfill", "env": WINDOW, "noise": 1.0, "settings": {"safety": 2}},
    "  safety 2, estimates x0.5": {"order": "backfill", "env": WINDOW, "bias": 0.5, "settings": {"safety": 2}},
    "classes + yields, safety 2": {"order": "backfill", "env": WINDOW, "settings": {**SHIPPED, "safety": 2},
                                   "step": 300},
    "  same, estimates noisy": {"order": "backfill", "env": WINDOW, "settings": {**SHIPPED, "safety": 2},
                                "step": 300, "noise": 1.0},
    "backfill, no head limit": {"order": "backfill", "env": WINDOW, "settings": {"limit": 0}},
    "backfill, -n all only": {"order": "all-only", "env": WINDOW},
    "short class + window": {"order": "short", "reorder": short_first(300, 1800), "env": WINDOW},
    "short class + backfill": {"order": "short+backfill", "reorder": short_first(300, 1800), "env": WINDOW},
    "backfill, exact runs": {"order": "backfill", "oracle": True, "env": WINDOW},
    "backfill, estimates x0.5": {"order": "backfill", "bias": 0.5, "env": WINDOW},
    "backfill, estimates x2": {"order": "backfill", "bias": 2.0, "env": WINDOW},
    "backfill, estimates noisy": {"order": "backfill", "noise": 1.0, "env": WINDOW},
    "  same, no head limit": {"order": "backfill", "noise": 1.0, "env": WINDOW, "settings": {"limit": 0}},
}


def validate(requests: list[dict], results: list[dict], start: float, until: float = math.inf) -> dict:
    """The current order's replay against the recorded waits of the requests whose arrival the history holds."""
    errors, recorded, replayed = [], 0.0, 0.0
    for request, result in zip(requests, results):
        if request["source"] != "history" or not start <= request["arrival"] <= until or request["waited"] is None:
            continue
        end = result["granted"] if result["granted"] is not None else result["left"]
        wait = (end or request["arrival"]) - request["arrival"]
        errors.append(abs(wait - request["waited"]))
        recorded += request["waited"]
        replayed += wait
    return {"requests": len(errors), "recorded_h": recorded / 3600, "replayed_h": replayed / 3600,
            "mean_abs_error_s": sum(errors) / max(1, len(errors)), "p90_abs_error_s": percentile(errors, 90),
            "within_60s": sum(e <= 60 for e in errors) / max(1, len(errors))}


def compare(directory: Path, moment: float, slots: int, policies: dict = POLICIES, seed_log: bool = True,
            since: float | None = None, until: float | None = None) -> dict:
    """Every order over the same requests; waits count for requests that arrived between `since` (default the first
    request in the history) and `until` (default the last)."""
    found = requests(directory, moment)
    start = min((r["arrival"] for r in found if r["source"] == "history"), default=moment)
    until = max((r["arrival"] for r in found), default=moment) if until is None else until
    first = start if since is None else since
    whole = sf.Estimates((r["label"], r["cmd"], r["run"]) for r in found
                         if r["run"] is not None and r.get("outcome") not in sf.UNFINISHED)
    seed = seed_runs(directory, start) if seed_log else []
    table, validation = {}, None
    for name, policy in policies.items():
        replay = Replay(found, slots, policy, seed, {"estimates": whole})
        results = replay.simulate()
        table[name] = measure(found, results, first, until, replay)
        if name == "window (current)":
            validation = validate(found, results, first, until)
    return {"start": stamp(first), "until": stamp(until), "requests": len(found), "slots": slots,
            "sources": {s: sum(r["source"] == s for r in found) for s in ("history", "log", "live")},
            "validation": validation, "policies": table}


def report(summary: dict) -> str:
    lines = [f"{summary['requests']} requests {summary['start']} .. {summary['until']} on {summary['slots']} slots "
             f"({', '.join(f'{v} from {k}' for k, v in summary['sources'].items())})"]
    v = summary["validation"]
    if v:
        lines.append(f"current order vs recorded waits ({v['requests']} requests): recorded {v['recorded_h']:.1f} h, "
                     f"replayed {v['replayed_h']:.1f} h, mean abs error {v['mean_abs_error_s']:.0f} s, p90 "
                     f"{v['p90_abs_error_s']:.0f} s, {v['within_60s']:.0%} within 60 s")
    head = f"{'order':<32} {'total h':>7} {'mean s':>7} {'p90 s':>7} {'max s':>7} {'≥3 mean':>8} {'≥3 max':>7} " \
           f"{'idle sh':>7} {'fit-idle':>8} {'held':>5}"
    lines += ["", head]
    for name, m in summary["policies"].items():
        lines.append(f"{name:<32} {m['total_h']:>7.1f} {m['mean_s']:>7.0f} {m['p90_s']:>7.0f} {m['max_s']:>7.0f} "
                     f"{m['large_mean_s']:>8.0f} {m['large_max_s']:>7.0f} {m['idle_slot_h']:>7.1f} "
                     f"{m['blocked_slot_h']:>8.1f} {m['held_mean']:>5.2f}")
    counts = next(iter(summary["policies"].values()))["classes"]
    sizes = ", ".join(f"{n} {c['requests']}" for n, c in counts.items())
    lines += ["", f"by class ({sizes}; batch delay: its wait plus the time it yielded)",
              f"{'order':<32} {'int mean':>8} {'int p90':>7} {'int max':>7} {'norm mean':>9} {'norm p90':>8} "
              f"{'bat mean':>8} {'bat max':>7} {'delay max':>9} {'delay h':>7} {'yields':>6} {'yield sh':>8}"]
    for name, m in summary["policies"].items():
        i, n, b = (m["classes"][c] for c in ("interactive", "normal", "batch"))
        lines.append(f"{name:<32} {i['mean_s']:>8.0f} {i['p90_s']:>7.0f} {i['max_s']:>7.0f} {n['mean_s']:>9.0f} "
                     f"{n['p90_s']:>8.0f} {b['mean_s']:>8.0f} {b['max_s']:>7.0f} {b['delay_max_s']:>9.0f} "
                     f"{b['delay_total_h']:>7.1f} {m['yields']:>6} {m['yielded_slot_h']:>8.2f}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--dir", default=str(sf.state_dir()))
    parser.add_argument("--now", help="the moment live holders are taken to run until (ISO, UTC); default now")
    parser.add_argument("--slots", type=int, help="capacity; default the saved or automatic one")
    parser.add_argument("--since", help="count waits of requests that arrived from then (ISO, UTC)")
    parser.add_argument("--until", help="... and until then")
    parser.add_argument("--json", action="store_true")
    options = parser.parse_args(argv)
    directory = Path(options.dir).expanduser()
    moment = sf.seconds(options.now) if options.now else time.time()
    os.environ["SLOTFATHER_DIR"] = str(directory)
    slots = options.slots or sf.total()
    summary = compare(directory, moment, slots, since=sf.seconds(options.since) if options.since else None,
                      until=sf.seconds(options.until) if options.until else None)
    print(json.dumps(summary, indent=1) if options.json else report(summary))
    return 0


if __name__ == "__main__":
    sys.exit(main())
