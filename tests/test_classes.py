#!/usr/bin/env python3
"""Classes and polite preemption (#643): interactive waiters queue ahead and batch ones behind, by a bounded offset
(their ageing); an interactive waiter asks yieldable holders of a lower class to yield, and a holder yields only
through its own client (`slotfather yield` between steps). plan() and yield_asks() on fixed queues, the settings,
the whole protocol through real processes, and the previous version on the same queue."""
from __future__ import annotations
import sys as _path_sys
from pathlib import Path as _Path
_path_sys.path.insert(0, str(_Path(__file__).resolve().parents[1] / "src"))
from slotfather import config as sf_config
from slotfather import leases as sf_leases
from slotfather import schedule as sf_schedule
from slotfather import state as sf_state


import json
import os
import pathlib
import time
import unittest
from unittest import mock
from typing import Any

from test_order import M, PlanCase, holder, runs, stamp, waiter
from test_slotfather import TOOL, WORK, SlotCase
from test_usage import load


# A client that keeps the protocol: between steps (every 50 ms) it looks for its yield request and yields.
YIELDER = ('echo "start $1" >> "$LOG"; while [ ! -e "$RELEASE/$1" ]; do if [ -e "$SLOTFATHER_YIELD" ]; then '
           'echo "yield $1" >> "$LOG"; "$TOOL" yield; echo "resume $1" >> "$LOG"; fi; sleep 0.05; done; '
           'echo "end $1" >> "$LOG"')


def classed(entry: dict, cls: str, **extra) -> dict:
    return {**entry, "priority": cls, **extra}


class RankTests(PlanCase):
    def setUp(self):
        super().setUp()
        self.held = [holder("one", 5, -60)]  # one slot free

    def test_an_interactive_waiter_queues_interactive_seconds_earlier(self):
        queue = [waiter("normal", 1, -1000), classed(waiter("test", 1, -100), "interactive")]
        self.assertEqual(["test"], self.started(queue, self.held))
        queue = [waiter("normal", 1, -2000), classed(waiter("test", 1, -100), "interactive")]
        self.assertEqual(["normal"], self.started(queue, self.held), "more than 1800 s earlier: still first")

    def test_a_batch_waiter_queues_batch_seconds_later_and_ages_ahead_of_everyone(self):
        queue = [classed(waiter("gate", 1, -1000), "batch"), waiter("normal", 1, -100)]
        self.assertEqual(["normal"], self.started(queue, self.held))
        queue = [classed(waiter("gate", 1, -3700), "batch"), classed(waiter("test", 1, -10), "interactive")]
        self.assertEqual(["gate"], self.started(queue, self.held),
                         "a batch request that waited longer than both offsets is ahead of a new interactive one")

    def test_the_window_order_ignores_classes(self):
        queue = [waiter("normal", 1, -1000), classed(waiter("test", 1, -100), "interactive")]
        self.assertEqual(["normal"], self.started(queue, self.held, "window"))

    def test_an_interactive_request_goes_ahead_of_a_batch_head_instead_of_passing_it(self):
        held, queue = [holder("one", 1, -600)], [classed(waiter("gate", 6, -700), "batch"),
                                                  classed(waiter("test", 1, -10), "interactive")]
        self.settings.update(interactive=0.0, batch=0.0)
        self.assertEqual([], self.started(queue, held), "unknown run: it would end after the gate can start")
        self.settings.update(interactive=1800.0, batch=1800.0)
        self.assertEqual(["test"], self.started(queue, held))

    def test_a_yielded_lease_queues_behind_the_waiter_it_yielded_to(self):
        test = classed(waiter("test", 1, -10), "interactive")
        back = classed(waiter("gate", 1, -3000), "batch", rank=sf_schedule.rank_time(test, self.settings) + sf_config.YIELD_BEHIND)
        self.assertEqual(["test"], self.started([back, test], self.held))

    def test_a_pinned_rank_wins_alone_so_later_arrivals_queue_behind_the_yielded_lease(self):
        # review P2-1: a batch lease granted at once and asked soon after would otherwise rank at since + 1800,
        # behind every normal request of the next half hour, while its job sits half done.
        test = classed(waiter("test", 1, -150), "interactive")
        back = classed(waiter("gate", 1, -200), "batch", rank=sf_schedule.rank_time(test, self.settings) + sf_config.YIELD_BEHIND)
        later = waiter("later", 1, -10)
        self.assertEqual(["gate"], self.started([back, later], self.held))
        self.assertLess(sf_schedule.rank_time(back, self.settings), sf_schedule.rank_time(later, self.settings))

    def test_a_yielded_head_counts_its_limit_from_when_it_yielded(self):
        # review P3-5: by its first arrival the limit is long past, and nothing could pass it at all.
        held = [holder("one", 1, -600)]
        head = classed(waiter("gate", 6, -4000), "normal", yielded_at=M - 100, yields=1)
        queue = [head, waiter("short", 1, -10)]
        self.assertEqual(["short"], self.started(queue, held, one=3600, short=60))
        self.assertEqual([], self.started([{k: v for k, v in head.items() if k != "yielded_at"}, queue[1]], held,
                                          one=3600, short=60))

    def test_an_unknown_class_is_normal(self):
        self.assertEqual("normal", sf_config.priority_of({"priority": "urgent"}))
        self.assertEqual("normal", sf_config.priority_of({}))


class YieldAskTests(PlanCase):
    """An interactive test waits behind six held slots; `gate` (batch, yieldable) has held four for ten minutes and
    should run an hour."""

    def setUp(self):
        super().setUp()
        self.gate = classed(holder("gate", 4, -600), "batch", yieldable=True)
        self.other = holder("other", 2, -600)
        self.test = classed(waiter("test", 1, -300), "interactive")
        self.estimates = runs(gate=3600, other=3600)

    def asks(self, waiters, held, **estimates):
        found = sf_schedule.yield_asks(waiters, held, 6, M, runs(**{"gate": 3600, "other": 3600, **estimates}),
                              settings=self.settings)
        return [(h["label"], w["label"]) for h, w in found]

    def test_an_interactive_waiter_asks_a_yieldable_batch_holder(self):
        self.assertEqual([("gate", "test")], self.asks([self.test], [self.gate, self.other]))

    def test_nobody_asks_without_reason(self):
        cases: dict[str, Any] = {
            "it has waited less than yield_after": ([classed(waiter("test", 1, -60), "interactive")], None),
            "a normal waiter does not ask": ([waiter("test", 1, -300)], None),
            "the holder did not opt in": ([self.test], {"yieldable": False}),
            "the holder is not of a lower class": ([self.test], {"priority": "interactive"}),
            "the holder was granted less than yield_after ago": ([self.test], {"granted": stamp(-60)}),
            "the holder yielded YIELDS_MAX times": ([self.test], {"yields": sf_config.YIELDS_MAX}),
        }
        for why, (waiters, change) in cases.items():
            with self.subTest(why):
                self.assertEqual([], self.asks(waiters, [{**self.gate, **(change or {})}, self.other]))
        with self.subTest("its slots are expected within yield_after"):
            ending = holder("ending", 2, -100)  # 50 s left of its 150 (and half its run so far)
            self.assertEqual([], self.asks([self.test], [self.gate, ending], ending=150))
        with self.subTest("polite preemption is off"):
            self.settings["yield_after"] = 0.0
            self.assertEqual([], self.asks([self.test], [self.gate, self.other]))
        with self.subTest("the window order"):
            self.settings.update(yield_after=120.0, order="window")
            self.assertEqual([], self.asks([self.test], [self.gate, self.other]))

    def test_it_asks_only_when_it_would_then_start(self):
        head = waiter("measure", 6, -4000)  # ranks ahead of the test: the gate's slots would go to it
        self.assertEqual([], self.asks([head, self.test], [self.gate, self.other]))

    def test_it_asks_as_few_holders_as_it_needs_the_longest_running_first(self):
        held = [classed(holder("a", 2, -600), "batch", yieldable=True),
                classed(holder("b", 2, -600), "batch", yieldable=True), holder("c", 2, -600)]
        self.assertEqual([("b", "test")], self.asks([self.test], held, a=1800, b=7200, c=3600))
        three = classed(waiter("test", 3, -300), "interactive")
        self.assertEqual({("a", "test"), ("b", "test")}, set(self.asks([three], held, a=1800, b=7200, c=3600)))

    def test_a_waiter_that_asks_later_or_never_says_so_on_its_entry(self):
        # review P3: every client's look asks with the saved yield_after; a client's tighter environment rides on
        # its own entry, so looks of differently configured clients agree.
        never = classed(waiter("test", 1, -300), "interactive", yield_after=0)
        self.assertEqual([], self.asks([never], [self.gate, self.other]))
        later = classed(waiter("test", 1, -300), "interactive", yield_after=600)
        self.assertEqual([], self.asks([later], [self.gate, self.other]))
        sooner = classed(waiter("test", 1, -300), "interactive", yield_after=30)  # never looser than saved
        self.assertEqual([("gate", "test")], self.asks([sooner], [self.gate, self.other]))

    def test_a_holder_already_asked_is_asked_again_before_another(self):
        a, b = (classed(holder(n, 3, -600), "batch", yieldable=True) for n in ("a", "b"))
        found = sf_schedule.yield_asks([self.test], [a, b], 6, M, runs(a=1800, b=7200), settings=self.settings, asked=[a])
        self.assertEqual(["a"], [h["label"] for h, _ in found])


class SettingsTests(unittest.TestCase):
    def setUp(self):
        self.dir = pathlib.Path(__import__("tempfile").mkdtemp(prefix="slotfather-classes-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(self.dir, ignore_errors=True))
        patcher = mock.patch.dict(os.environ, {"SLOTFATHER_DIR": str(self.dir)})
        patcher.start()
        self.addCleanup(patcher.stop)
        for key in [k for k in os.environ if k.startswith("SLOTFATHER_") and k != "SLOTFATHER_DIR"]:
            os.environ.pop(key)

    def test_the_environment_only_moves_classes_less_and_asks_later_or_never(self):
        loose = {"SLOTFATHER_PRIORITY_INTERACTIVE": "3600", "SLOTFATHER_PRIORITY_BATCH": "3600",
                 "SLOTFATHER_YIELD_AFTER": "30"}
        with mock.patch.dict(os.environ, loose):
            found = sf_schedule.order_settings()
        self.assertEqual((1800, 1800, 120), (found["interactive"], found["batch"], found["yield_after"]))
        tight = {"SLOTFATHER_PRIORITY_INTERACTIVE": "600", "SLOTFATHER_PRIORITY_BATCH": "0",
                 "SLOTFATHER_YIELD_AFTER": "300"}
        with mock.patch.dict(os.environ, tight):
            found = sf_schedule.order_settings()
        self.assertEqual((600, 0, 300), (found["interactive"], found["batch"], found["yield_after"]))
        with mock.patch.dict(os.environ, {"SLOTFATHER_YIELD_AFTER": "0"}):
            self.assertEqual(0, sf_schedule.order_settings()["yield_after"])
        sf_state.save_order({**sf_config.ORDER_DEFAULTS, "yield_after": 0})
        with mock.patch.dict(os.environ, {"SLOTFATHER_YIELD_AFTER": "60"}):
            self.assertEqual(0, sf_schedule.order_settings()["yield_after"], "nothing asks earlier than never")

    def test_the_saved_settings_alone_are_every_clients_view_for_asking(self):
        with mock.patch.dict(os.environ, {"SLOTFATHER_YIELD_AFTER": "0", "SLOTFATHER_QUEUE": "window"}):
            self.assertEqual((0, "window"), (sf_schedule.order_settings()["yield_after"], sf_schedule.order_settings()["order"]))
            saved = sf_schedule.order_settings(environment=False)
        self.assertEqual((120, "backfill"), (saved["yield_after"], saved["order"]))

    def test_class_knobs_are_saved_only_when_they_differ_from_their_defaults(self):
        sf_state.save_order(dict(sf_config.ORDER_DEFAULTS))
        self.assertEqual(set(sf_config.ORDER_BASE), set(json.loads((self.dir / "order.json").read_text())))
        sf_state.save_order({**sf_config.ORDER_DEFAULTS, "batch": 600})
        self.assertEqual(600, json.loads((self.dir / "order.json").read_text())["batch"])
        self.assertEqual(600, sf_schedule.order_settings()["batch"])


class ProtocolTests(SlotCase):
    """Real processes on four slots: `gate` (batch) holds all four, an interactive `quick` asks."""

    def setUp(self):
        super().setUp()
        self.env["TOOL"] = TOOL
        self.assertEqual(0, self.tool("order", "--yield-after", "1").returncode)

    def gate(self, *options, script=YIELDER):
        return self.request("gate", 4, "sh", "-c", script, "work", "gate", options=("--priority", "batch", *options))

    def test_a_yieldable_holder_yields_between_steps_and_resumes_after_the_interactive_request(self):
        gate = self.gate("--yieldable")
        self.started("gate")
        time.sleep(1.1)  # a holder is asked only after it held its slots yield_after seconds
        quick = self.request("quick", 1, options=("--priority", "interactive"))
        self.started("quick")
        self.assertEqual(["start gate", "yield gate", "start quick"], self.events())
        found = {e["label"]: e for e in json.loads(self.tool("status", "--json").stdout)["entries"]}
        self.assertEqual(("waiting", "batch", True, 1, 1), (found["gate"]["state"], found["gate"]["priority"],
                                                             found["gate"]["yieldable"], found["gate"]["yields"],
                                                             found["gate"]["position"]))
        self.assertEqual(("held", "interactive"), (found["quick"]["state"], found["quick"]["priority"]))
        self.assertRegex(self.status(), r"waiting: 4 slot\(s\) gate .*yieldable yielded=1 cmd=")
        self.release("quick")
        self.wait_for(lambda: "resume gate" in self.events(), "the gate to resume")
        self.release("gate")
        for process in (gate, quick):
            self.finish(process)
        self.assertEqual(["start gate", "yield gate", "start quick", "end quick", "resume gate", "end gate"],
                         self.events())
        lines = {json.loads(line)["label"]: json.loads(line) for line in
                 (self.state / "history.jsonl").read_text().splitlines()}
        line = lines["gate"]
        self.assertEqual(("released", 1), (line["outcome"], line["yields"]))
        self.assertGreater(line["yielded"], 0)
        self.assertLess(line["waited"], 2, "its wait is the wait before its first grant")
        self.assertGreaterEqual(line["ran"], line["yielded"], "its run counts from its first grant")
        log = (self.state / "queue.log").read_text()
        self.assertIn("asked gate", log)
        self.assertIn("yielded 4", log)
        self.assertEqual([], list((self.state / "queue").glob("*.yield")))

    def test_a_holder_that_did_not_opt_in_is_never_asked(self):
        gate = self.gate()
        self.started("gate")
        quick = self.request("quick", 1, options=("--priority", "interactive"))
        self.wait_for(lambda: "waiting: 1 slot(s) quick" in self.status(), "quick to queue")
        time.sleep(2.5)  # past yield_after, several looks
        self.assertEqual([], list((self.state / "queue").glob("*.yield")))
        self.assertEqual(["start gate"], self.events())
        self.release("gate")
        self.started("quick")
        self.release("quick")
        for process in (gate, quick):
            self.finish(process)

    def test_a_request_is_shown_until_it_is_withdrawn_when_the_asker_leaves(self):
        gate = self.gate("--yieldable", script=WORK.replace("$1", "gate"))  # opted in, but never looks
        self.started("gate")
        time.sleep(1.1)
        quick = self.request("quick", 1, options=("--priority", "interactive", "--wait", "4"))
        self.wait_for(lambda: list((self.state / "queue").glob("*.yield")), "the yield request")
        self.assertIn("yieldable asked-to-yield-for=quick", self.status())
        [entry] = [e for e in json.loads(self.tool("status", "--json").stdout)["entries"] if e["label"] == "gate"]
        self.assertEqual(("quick", "interactive", 1), (entry["yield_asked"]["label"], entry["yield_asked"]["priority"],
                                                       entry["yield_asked"]["count"]))
        text = self.tool("dashboard", "--once", "--width", "160", "--height", "40").stdout
        self.assertRegex(text, r"yield +asked to yield for quick")
        self.assertEqual(75, quick.wait(timeout=20), "it gave up waiting")
        self.assertEqual([], list((self.state / "queue").glob("*.yield")), "its leaving withdrew the request")
        self.assertIn("withdrew the yield request to gate", (self.state / "queue.log").read_text())
        self.release("gate")
        self.finish(gate)

    def test_a_client_that_asks_later_says_so_on_its_entry(self):
        self.env["SLOTFATHER_YIELD_AFTER"] = "0"
        process = self.request("quick", 1, options=("--priority", "interactive"))
        self.started("quick")
        [entry] = [json.loads(p.read_text()) for p in (self.state / "queue").glob("*.json")]
        self.assertEqual(0, entry["yield_after"])
        self.release("quick")
        self.finish(process)

    def test_the_order_warns_that_a_class_knob_makes_older_clients_use_the_window_order(self):
        quiet = self.tool("order", "--yield-after", "120")
        self.assertNotIn("3e3a621", quiet.stderr)
        loud = self.tool("order", "--batch", "600")
        self.assertEqual(0, loud.returncode)
        self.assertIn("3e3a621", loud.stderr)
        self.assertIn("window order", loud.stderr)

    def test_a_holder_does_not_yield_when_the_asker_would_still_not_start(self):
        # review P3-6: `gate` holds 2 of 4, `other` 2; the asker wants all 4, so the gate's 2 would idle.
        (gate_pid, gate_start), (other_pid, other_start), (asker_pid, asker_start) = (self.holder() for _ in "abc")
        queue = self.state / "queue"
        queue.mkdir(parents=True, exist_ok=True)
        base = {"cmd": "fake", "since": "2026-11-03T00:00:00Z", "granted": "2026-11-03T00:00:00Z", "state": "held"}
        gate = queue / f"{1:010d}-{gate_pid}.json"
        gate.write_text(json.dumps({**base, "pid": gate_pid, "start": gate_start, "count": 2, "label": "gate",
                                    "yieldable": True, "priority": "batch"}))
        (queue / f"{2:010d}-{other_pid}.json").write_text(json.dumps(
            {**base, "pid": other_pid, "start": other_start, "count": 2, "label": "other"}))
        asker = queue / f"{3:010d}-{asker_pid}.json"
        asker.write_text(json.dumps({"pid": asker_pid, "start": asker_start, "count": 4, "label": "quick",
                                     "cmd": "fake", "since": "2026-11-03T00:10:00Z", "state": "waiting",
                                     "priority": "interactive"}))
        sf_state.write(sf_state.marker(gate), {"by": asker.stem, "label": "quick", "priority": "interactive", "count": 4,
                                   "at": "2026-11-03T00:12:00Z"})
        with mock.patch.dict(os.environ, {"SLOTFATHER_DIR": str(self.state), "SLOTFATHER_LEGACY_DIRS": "",
                                          "SLOTFATHER_CORES": "12", "SLOTFATHER_MEMORY_GIB": "64",
                                          "SLOTFATHER_MEMORY_FILE": str(self.memory)}):
            self.assertFalse(sf_leases.yield_lease(gate))
        self.assertEqual("held", json.loads(gate.read_text())["state"])
        self.assertFalse(sf_state.marker(gate).exists(), "the request is withdrawn")

    def test_yield_returns_at_once_when_nothing_asked_and_needs_a_lease(self):
        result = self.tool("run", "--yieldable", "--", TOOL, "yield")
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("no yield requested", result.stderr)
        self.assertEqual(2, self.tool("yield").returncode)
        self.assertEqual(2, self.tool("run", "--priority", "urgent", "--", "true").returncode)
        self.assertEqual(2, self.tool("run", "--meta", "class=urgent", "--", "true").returncode)
        self.assertEqual(2, self.tool("run", "--meta", "class=batch", "--", "true").returncode)

    def test_a_lease_granted_again_by_an_older_client_resumes(self):
        pid, start = self.holder()
        (self.state / "queue").mkdir(parents=True, exist_ok=True)
        path = self.state / "queue" / f"{1:010d}-{pid}.json"
        path.write_text(json.dumps({"pid": pid, "start": start, "count": 1, "label": "gate", "cmd": "fake",
                                    "since": "2026-11-03T00:00:00Z", "state": "held",
                                    "granted": "2026-11-03T00:20:00Z", "first_granted": "2026-11-03T00:01:00Z",
                                    "yielded_at": "2026-11-03T00:10:00Z", "yields": 1, "rank": 1.0}))
        with mock.patch.dict(os.environ, {"SLOTFATHER_DIR": str(self.state), "SLOTFATHER_LEGACY_DIRS": ""}):
            sf_leases.held_again(path)
        entry = json.loads(path.read_text())
        self.assertEqual(("2026-11-03T00:01:00Z", "2026-11-03T00:20:00Z", 600), (entry["granted"], entry["resumed"],
                                                                               entry["yielded"]))
        self.assertFalse({"yielded_at", "first_granted", "rank"} & set(entry))




class StatsTests(unittest.TestCase):
    def test_cores_per_slot_leave_out_the_time_a_lease_yielded(self):
        stats = load(TOOL + "-stats", "slotfather_stats_classes")
        self.assertEqual(600.0, stats.wall_of({"wall": 900.0, "yielded": 300}))
        self.assertEqual(900.0, stats.wall_of({"wall": 900.0}))


if __name__ == "__main__":
    unittest.main()
