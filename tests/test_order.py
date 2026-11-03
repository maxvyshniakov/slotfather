#!/usr/bin/env python3
"""Queue order (#643): which waiters start now. plan() on fixed queues, the run-time estimates it reads from the
history, the order through real processes, and an older slotfather sharing the queue."""
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import tempfile
import time
import unittest
from unittest import mock

from test_slotfather import ROOT, TOOL, SlotCase, WORK
from test_usage import load

sf = load(TOOL, "slotfather_order_core")

M = 1_800_000_000.0  # the moment plan() decides at
SETTINGS = {"SLOTFATHER_OVERTAKE_WINDOW": "600"}  # every other knob at its default unless a test sets it


def stamp(offset: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(M + offset))


def waiter(label: str, count: int, queued: float) -> dict:
    """A waiter that queued `queued` seconds before M (negative: earlier)."""
    return {"label": label, "count": count, "cmd": label, "since": stamp(queued), "state": "waiting"}


def holder(label: str, count: int, granted: float) -> dict:
    return {"label": label, "count": count, "cmd": label, "since": stamp(granted), "granted": stamp(granted),
            "state": "held"}


def runs(**estimates):
    """estimate(entry) from label -> seconds; other labels are unknown."""
    return lambda entry: estimates.get(entry["label"])


class PlanCase(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ, SETTINGS)
        patcher.start()
        self.addCleanup(patcher.stop)
        for key in [k for k in os.environ if k.startswith("SLOTFATHER_") and k not in SETTINGS]:
            os.environ.pop(key)

    def started(self, waiters, held, order=None, slots=6, **estimates):
        found = sf.plan(waiters, held, slots, M, order, runs(**estimates))
        return [e["label"] for e in found.grants]


class WindowOrderTests(PlanCase):
    def test_a_waiter_that_does_not_fit_lets_later_ones_pass_until_its_window_ends(self):
        held = [holder("one", 1, -60)]
        self.assertEqual(["small"], self.started([waiter("gate", 6, -100), waiter("small", 1, -50)], held, "window"))
        self.assertEqual([], self.started([waiter("gate", 6, -700), waiter("small", 1, -50)], held, "window"))

    def test_its_reason_names_the_waiter_and_the_end_of_the_window(self):
        found = sf.plan([waiter("gate", 6, -100)], [holder("one", 1, -60)], 6, M, "window")
        self.assertIn("5 of 6 slots free; requests that fit may pass gate until", found.reason)
        found = sf.plan([waiter("gate", 6, -700)], [holder("one", 1, -60)], 6, M, "window")
        self.assertEqual("5 of 6 slots free", found.reason)

    def test_the_queue_setting_chooses_the_order_and_anything_unknown_is_the_window(self):
        for value, expected in ((None, "backfill"), ("backfill", "backfill"), ("window", "window"),
                                ("easy", "window")):
            with self.subTest(value=value), mock.patch.dict(os.environ, {} if value is None else
                                                            {"SLOTFATHER_QUEUE": value}):
                self.assertEqual(expected, sf.queue_order())


class BackfillTests(PlanCase):
    """A gate (all six slots) waits past its window behind `one`, which holds one slot and should run an hour."""

    def setUp(self):
        super().setUp()
        self.held = [holder("one", 1, -600)]  # 3000 s of its expected 3600 left
        os.environ["SLOTFATHER_BACKFILL_LIMIT"] = "0"  # its own test below

    def test_a_later_request_starts_only_if_it_ends_before_the_head_can_start(self):
        queue = [waiter("gate", 6, -700), waiter("short", 1, -10), waiter("long", 1, -5)]
        self.assertEqual(["short"], self.started(queue, self.held, one=3600, short=900, long=1200))

    def test_its_estimate_counts_three_times_by_default(self):
        queue = [waiter("gate", 6, -700), waiter("short", 1, -10)]
        self.assertEqual([], self.started(queue, self.held, one=3600, short=1001))
        with mock.patch.dict(os.environ, {"SLOTFATHER_BACKFILL_SAFETY": "1"}):
            self.assertEqual(["short"], self.started(queue, self.held, one=3600, short=2999))

    def test_an_unknown_run_counts_the_fallback(self):
        queue = [waiter("gate", 6, -700), waiter("new", 1, -10)]
        self.assertEqual([], self.started(queue, self.held, one=3600))
        with mock.patch.dict(os.environ, {"SLOTFATHER_BACKFILL_FALLBACK": "60"}):
            self.assertEqual(["new"], self.started(queue, self.held, one=3600))

    def test_an_unknown_holder_counts_the_fallback_too(self):
        queue = [waiter("gate", 6, -700), waiter("short", 1, -10)]
        self.assertEqual(["short"], self.started(queue, self.held, short=900))  # one: 3600 - 600 s left
        self.assertEqual([], self.started(queue, self.held, short=1001))

    def test_a_holder_past_its_estimate_is_expected_to_run_on_for_half_what_it_ran(self):
        held = [holder("one", 1, -3000)]  # expected 600 s, so 1500 s more
        queue = [waiter("gate", 6, -700), waiter("short", 1, -10)]
        self.assertEqual(["short"], self.started(queue, held, one=600, short=500))
        self.assertEqual([], self.started(queue, held, one=600, short=501))
        with mock.patch.dict(os.environ, {"SLOTFATHER_BACKFILL_OVERRUN": "0"}):
            self.assertEqual([], self.started(queue, held, one=600, short=10), "it could end any moment")

    def test_the_head_starts_when_the_held_lease_it_waits_for_ends_whatever_passes_it(self):
        found = sf.plan([waiter("gate", 6, -700), waiter("short", 1, -10)], self.held, 6, M, None,
                        runs(one=3600, short=60), reserve_head=True)
        self.assertEqual(("gate", M + 3000), (found.head["label"], found.start))
        self.assertIn("gate is next, and a later request that fits starts first only if it ends before gate can",
                      found.reason)

    def test_nothing_passes_a_head_that_has_waited_the_limit_and_a_pass_must_end_before_it(self):
        del os.environ["SLOTFATHER_BACKFILL_LIMIT"]  # the default, 2700 s
        queue = [waiter("gate", 6, -2700), waiter("short", 1, -10)]
        self.assertEqual([], self.started(queue, self.held, one=3600, short=1))
        queue = [waiter("gate", 6, -2000), waiter("short", 1, -10)]  # 700 s to the limit
        self.assertEqual(["short"], self.started(queue, self.held, one=3600, short=233))
        self.assertEqual([], self.started(queue, self.held, one=3600, short=234))
        with mock.patch.dict(os.environ, {"SLOTFATHER_BACKFILL_LIMIT": "0"}):
            self.assertEqual(["short"], self.started(queue, self.held, one=3600, short=900))

    def test_slots_the_head_will_not_need_take_any_request(self):
        held = [holder("three", 3, -10), holder("two", 2, -10)]  # three ends first: then 4 slots are free
        queue = [waiter("head", 3, -700), waiter("long", 1, -10), waiter("long2", 1, -5)]
        self.assertEqual(["long"], self.started(queue, held, three=600, two=6000, long=99999, long2=99999))

    def test_within_its_window_a_waiter_lets_requests_that_fit_pass_as_before(self):
        queue = [waiter("gate", 6, -100), waiter("long", 1, -10)]
        self.assertEqual(["long"], self.started(queue, self.held, one=3600, long=99999))

    def test_requests_that_fit_are_granted_in_order_before_any_head(self):
        queue = [waiter("a", 2, -30), waiter("b", 3, -20), waiter("c", 1, -10)]
        self.assertEqual(["a", "b"], self.started(queue, self.held))

    def test_slots_held_in_an_earlier_queue_have_no_known_end_and_nothing_passes(self):
        found = sf.plan([waiter("gate", 6, -700), waiter("short", 1, -10)], [], 6, M, None, runs(short=1), other=2)
        self.assertEqual([], found.grants)

    def test_a_new_request_of_unknown_run_gets_no_slots_behind_the_head(self):
        waiters = [waiter("gate", 6, -700)]
        with mock.patch.object(sf, "now", return_value=stamp(0)), mock.patch.object(sf.time, "time", return_value=M):
            self.assertEqual(0, sf.room(waiters, 5, self.held, runs(one=3600)))
            self.assertEqual(0, sf.room(waiters, 5))
            self.assertEqual(5, sf.room([waiter("gate", 6, -100)], 5, self.held, runs(one=3600)))
            with mock.patch.dict(os.environ, {"SLOTFATHER_BACKFILL_FALLBACK": "60"}):
                self.assertEqual(5, sf.room(waiters, 5, self.held, runs(one=3600)))


class EstimateTests(unittest.TestCase):
    def test_a_label_loses_its_ticket_or_agent_id(self):
        for label, expected in (("lane-636-measure-seeded", "lane"), ("post-merge-638-plan-narrow", "post-merge"),
                                ("agent-a17b708a4e705e421", "agent"), ("sid-kit-wt-646", "sid-kit-wt"),
                                ("sid-kit-wt-button-spinner", "sid-kit-wt-button-spinner"), ("", ""), (None, "")):
            with self.subTest(label=label):
                self.assertEqual(expected, sf.family(label))

    def test_the_closest_kind_that_ran_gives_the_median_of_its_recent_runs(self):
        checks = lambda n: "/w/verify.sh --checks " + ",".join(f"c{i}" for i in range(n)) + " --jobs 1"
        found = sf.Estimates([("lane-1-a", checks(140), 4000), ("lane-1-a", checks(140), 5000),
                              ("lane-2-b", checks(130), 4600), ("lane-3-c", checks(4), 60),
                              ("post-merge-1-a", checks(140), 700), ("sid-kit", "bun x.mjs", 30)])
        self.assertEqual(4500, found.of("lane-1-a", checks(140)), "same label and kind")
        self.assertEqual(4000, found.of("lane-9-new", checks(140)), "same kind under any label: 4000, 5000, 700")
        self.assertEqual(4600, found.of("lane-9-new", checks(150)), "a lane check of about that size")
        self.assertEqual(60, found.of("lane-9-new", checks(5)))
        self.assertEqual(700, found.of("post-merge-9-x", checks(129)), "the same family, about that size")
        self.assertEqual(4300, found.of("smoke", checks(129)), "about that size under any label")
        self.assertEqual(30, found.of("sid-kit", "node other.mjs"), "same label")
        self.assertIsNone(found.of("unheard", "make all"))

    def test_only_the_last_runs_of_a_kind_count(self):
        found = sf.Estimates([("a", "make x", 1000)] * 30 + [("a", "make x", 10)] * sf.ESTIMATE_RUNS)
        self.assertEqual(10, found.of("a", "make x"))

    def test_the_history_gives_every_run_and_skips_requests_that_never_ran(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory)
            v1 = {"label": "old", "cmd": "make build", "count": 1, "queued": "2026-11-02T10:00:00Z",
                  "granted": "2026-11-02T10:01:00Z", "ended": "2026-11-02T10:11:00Z", "outcome": "released",
                  "waited": 60, "ran": 600}
            left = {**v1, "label": "gave-up", "granted": None, "ran": None, "outcome": "left"}
            v2 = {**v1, "v": 2, "label": "new", "ran": 30, "cpu": 12.0}
            (path / "history.jsonl.1").write_text(json.dumps(v1) + "\n")
            (path / "history.jsonl").write_text(json.dumps(left) + "\n" + "torn {\n" + json.dumps(v2) + "\n")
            found = sf.history_estimates(path)
            self.assertEqual((600, 30, None), (found.of("old", "make build"), found.of("new", "other"),
                                               found.of("gave-up", "x y")))


class OrderTests(SlotCase):
    """The order through real processes: a gate waits for every slot behind `one`, which the history says runs an
    hour; `short` has run in a second before."""

    def seed(self, **ran):
        self.state.mkdir(parents=True, exist_ok=True)
        lines = [{"label": label, "cmd": " ".join(("sh", "-c", WORK, "work", label)), "count": 1,
                  "queued": "2026-11-02T10:00:00Z", "granted": "2026-11-02T10:00:00Z",
                  "ended": "2026-11-02T11:00:00Z", "outcome": "released", "waited": 0, "ran": seconds}
                 for label, seconds in ran.items()]
        (self.state / "history.jsonl").write_text("".join(json.dumps(line) + "\n" for line in lines))

    def gate_behind_one(self):
        self.seed(one=3600, short=1)
        one = self.request("one", 1)
        self.started("one")
        gate = self.request("gate", "all")
        self.assertIn("gate is next, and a later request that fits starts first only if it ends before gate",
                      self.output_line(gate))
        return one, gate

    def test_a_short_request_starts_before_a_gate_that_waits_past_its_window(self):
        one, gate = self.gate_behind_one()
        unknown = self.request("unknown", 1, "env", "sh", "-c", WORK, "work", "unknown")  # a kind never run
        self.wait_for(lambda: "waiting: 1 slot(s) unknown" in self.status(), "the unknown request to queue")
        short = self.request("short", 1)
        self.started("short")
        self.release("short")
        self.finish(short)
        self.assertEqual("0", self.tool("available").stdout.strip(), "a request of unknown run waits")
        self.assertNotIn("start unknown", self.events())
        self.release("one")
        self.started("gate")
        self.release("gate")
        self.started("unknown")
        self.release("unknown")
        for process in (one, gate, unknown):
            self.finish(process)
        self.assertEqual(["start one", "start short", "end short", "end one", "start gate", "end gate",
                          "start unknown", "end unknown"], self.events())

    def test_the_window_order_holds_the_short_request_back(self):
        self.env["SLOTFATHER_QUEUE"] = "window"
        self.seed(one=3600, short=1)
        one = self.request("one", 1)
        self.started("one")
        gate = self.request("gate", "all")
        self.wait_for(lambda: "waiting: 4 slot(s) gate" in self.status(), "the gate to queue")
        short = self.request("short", 1)
        self.wait_for(lambda: "waiting: 1 slot(s) short" in self.status(), "the short request to queue")
        time.sleep(0.6)  # a few looks at the queue
        self.assertNotIn("start short", self.events())
        self.release("one")
        self.started("gate")
        self.release("gate")
        self.started("short")
        self.release("short")
        for process in (one, gate, short):
            self.finish(process)


@unittest.skipUnless((ROOT / ".git").exists(), "needs the git history for the previous slotfather")
class MixedVersionTests(SlotCase):
    """An older slotfather (bfa114b, the window order) on the same queue: its entries and outputs stay as they were,
    and a waiter it queued starts as soon as a new client looks at the queue."""

    OLD = "bfa114b"

    def setUp(self):
        super().setUp()
        found = subprocess.run(["git", "-C", str(ROOT), "show", f"{self.OLD}:slotfather"], capture_output=True)
        if found.returncode:
            self.skipTest(f"{self.OLD} is not in this checkout")
        self.old = self.tmp / "slotfather-old"
        self.old.write_bytes(found.stdout)
        self.old.chmod(0o755)

    def old_request(self, label, count):
        argv = [str(self.old), "run", "-n", str(count), "--label", label, "--", "sh", "-c", WORK, "work", label]
        process = subprocess.Popen(argv, env=self.env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                   start_new_session=True)
        self.processes.append(process)
        return process

    def test_a_waiter_of_the_old_version_is_backfilled_and_both_versions_read_the_queue_alike(self):
        OrderTests.seed(self, one=3600, short=1)
        one = self.old_request("one", 1)
        self.started("one")
        gate = self.request("gate", "all")
        self.wait_for(lambda: "waiting: 4 slot(s) gate" in self.status(), "the gate to queue")
        short = self.old_request("short", 1)
        self.started("short")  # the old client alone would hold it behind the gate
        for tool in (TOOL, str(self.old)):
            listing = subprocess.run([tool, "status"], env=self.env, capture_output=True, text=True).stdout
            self.assertRegex(listing, r"held: 1 slot\(s\) one pid=\d+ since=\S+ cmd=sh -c")
            self.assertIn("waiting: 4 slot(s) gate", listing)
            self.assertIn("2 of 4 slots held", listing)
            self.assertEqual("0", subprocess.run([tool, "available"], env=self.env, capture_output=True,
                                                 text=True).stdout.strip())
            advice = json.loads(subprocess.run([tool, "advise"], env=self.env, capture_output=True, text=True).stdout)
            self.assertEqual((2, True), (advice["held"], advice["waiting"]))
        for label in ("short", "one"):
            self.release(label)
        self.started("gate")
        self.release("gate")
        for process in (one, gate, short):
            self.finish(process)
        history = [json.loads(line)["label"] for line in (self.state / "history.jsonl").read_text().splitlines()]
        self.assertEqual(["one", "short"], history[:2], "the seed")
        self.assertEqual(["gate", "one", "short"], sorted(history[2:]), "both versions record their requests")


class DashboardOrderTests(SlotCase):
    def test_a_waiting_gate_says_when_it_should_start_and_until_when_others_pass(self):
        OrderTests.seed(self, one=3600)
        one = self.request("one", 1)
        self.started("one")
        gate = self.request("gate", "all")
        self.wait_for(lambda: "waiting: 4 slot(s) gate" in self.status(), "the gate to queue")
        text = self.tool("dashboard", "--once", "--width", "200", "--height", "40").stdout
        self.assertRegex(text, r"next is gate, which needs 4 slots with 3 free · it should start by "
                               r"\d\d:\d\d:\d\d; one that ends by then starts first until \d\d:\d\d:\d\d")
        for label in ("one", "gate"):
            self.release(label)
        for process in (one, gate):
            self.finish(process)


if __name__ == "__main__":
    unittest.main()
