#!/usr/bin/env python3
"""The replay (#643): recorded requests through the queue orders. tests/fixtures/replay is the queue of
2026-11-02 22:10 to 2026-11-03 01:48 UTC (commands cut to their kind): the 6-slot measurements and the incident of
#643, where one 6-slot request held back twelve others with five slots idle."""
from __future__ import annotations

import json
import pathlib
import shutil
import tempfile
import unittest

from test_slotfather import ROOT
from test_usage import load

replay = load(str(ROOT / "tools" / "replay.py"), "slotfather_replay_under_test")
sf = replay.sf

FIXTURE = ROOT / "tests" / "fixtures" / "replay"
NOW = sf.seconds("2026-11-03T01:48:36Z")  # when the live queue in the fixture was copied
INCIDENT = (sf.seconds("2026-11-03T00:48:50Z"), sf.seconds("2026-11-03T01:40:00Z"))
SHIPPED, CURRENT = "backfill (shipped)", "window (current)"
CLASSES = "classes + yields (#643 shipped)"


class RecordedReplayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        policies = {name: replay.POLICIES[name] for name in (CURRENT, SHIPPED, "fifo (window 0)",
                                                              "backfill, estimates noisy", CLASSES,
                                                              "  estimates noisy")}
        cls.whole = replay.compare(FIXTURE, NOW, 6, policies)
        cls.incident = replay.compare(FIXTURE, NOW, 6, policies, since=INCIDENT[0], until=INCIDENT[1])

    def test_the_current_order_replays_the_recorded_waits(self):
        found = self.whole["validation"]
        self.assertEqual(57, found["requests"])
        self.assertGreaterEqual(found["within_60s"], 0.95)
        self.assertLess(found["mean_abs_error_s"], 30)
        self.assertAlmostEqual(found["recorded_h"], found["replayed_h"], delta=0.5)

    def test_backfill_waits_less_without_holding_large_requests_back_longer(self):
        current, shipped = (self.whole["policies"][name] for name in (CURRENT, SHIPPED))
        self.assertLess(shipped["total_h"], 0.9 * current["total_h"])
        self.assertLessEqual(shipped["max_s"], current["max_s"])
        self.assertLessEqual(shipped["large_max_s"], current["large_max_s"])
        self.assertLess(shipped["blocked_slot_h"], current["blocked_slot_h"])

    def test_wrong_estimates_do_not_hold_large_requests_back_longer(self):
        current, noisy = (self.whole["policies"][name] for name in (CURRENT, "backfill, estimates noisy"))
        self.assertLessEqual(noisy["large_max_s"], current["large_max_s"])

    def test_classes_wait_less_and_serve_interactive_work_first_without_starving_batch_work(self):
        backfill, classes = (self.whole["policies"][name] for name in (SHIPPED, CLASSES))
        self.assertLess(classes["total_h"], 0.8 * backfill["total_h"])
        self.assertLess(classes["classes"]["interactive"]["p90_s"], 0.5 * backfill["classes"]["interactive"]["p90_s"])
        self.assertLessEqual(classes["large_max_s"], backfill["large_max_s"])
        self.assertLessEqual(classes["classes"]["batch"]["delay_max_s"],
                             1.1 * backfill["classes"]["batch"]["delay_max_s"])
        noisy, before = (self.whole["policies"][name] for name in ("  estimates noisy", "backfill, estimates noisy"))
        bound = replay.CLASSES["interactive"] + replay.CLASSES["batch"]  # the most a class moves a request
        self.assertLessEqual(noisy["classes"]["batch"]["delay_max_s"], before["classes"]["batch"]["delay_max_s"] + bound)
        incident = self.incident["policies"]
        self.assertLessEqual(incident[CLASSES]["total_h"], incident[SHIPPED]["total_h"])

    def test_the_incident_waits_far_less(self):
        current, shipped = (self.incident["policies"][name] for name in (CURRENT, SHIPPED))
        self.assertLess(shipped["total_h"], 0.7 * current["total_h"])
        self.assertLessEqual(shipped["blocked_slot_h"], current["blocked_slot_h"])


class YieldTraceTests(unittest.TestCase):
    """A Straylight gate (batch, yieldable) holds five of six slots and a lane check the sixth, both for an hour; a
    pre-push check (interactive) comes ten minutes in."""

    def requests(self):
        checks = "/w/verify.sh --checks " + ",".join(f"c{n}" for n in range(60)) + " --jobs 4"
        found = [{"label": "lane-1-gate", "cmd": checks, "count": 5, "arrival": 0.0, "run": 3600.0},
                 {"label": "lane-2", "cmd": "/w/verify.sh --checks a,b", "count": 1, "arrival": 0.0, "run": 3600.0},
                 {"label": "sid-kit-wt-1", "cmd": "node scripts/pre-push.mjs origin", "count": 1, "arrival": 600.0,
                  "run": 60.0}]
        for number, request in enumerate(found):
            request.update(id=number, patience=None, waited=None, source="history",
                           arrival=sf.seconds("2026-11-02T10:00:00Z") + request["arrival"])
        return found

    def test_the_gate_yields_to_the_pre_push_check_and_resumes(self):
        found = self.requests()
        self.assertEqual(["batch", "normal", "interactive"], [replay.classify(r, 6) for r in found])
        seed = [("lane-1-gate", found[0]["cmd"], 3600.0), ("lane-2", found[1]["cmd"], 3600.0)]
        both = replay.Replay(found, 6, replay.POLICIES[CLASSES], seed)
        gate, _, check = both.simulate()
        waited = check["granted"] - found[2]["arrival"]
        self.assertGreaterEqual(waited, 120, "it asks once it has waited yield_after")
        self.assertLessEqual(waited, 120 + 300 + replay.POLL, "the gate yields at the end of its step")
        self.assertEqual(1, gate["yields"])
        self.assertAlmostEqual(60, gate["yielded"], delta=replay.POLL + 1, msg="back once the check is done")
        self.assertAlmostEqual(found[0]["arrival"] + 3600 + gate["yielded"], gate["ended"], delta=1)
        alone = replay.Replay(found, 6, replay.POLICIES["  classes only"], seed).simulate()
        self.assertAlmostEqual(3600 - 600, alone[2]["granted"] - found[2]["arrival"], delta=replay.POLL,
                               msg="without yields it waits for the first holder to end")


class AllSlotTraceTests(unittest.TestCase):
    """Review P2-2: a batch `-n all` measurement under a steady stream of interactive checks (one a minute, five
    minutes each, for four hours). Requests that rank ahead of it start first, so it is not bound by its limit;
    it starts once those that came within both class offsets after it have run."""

    def test_an_all_slot_batch_request_starts_after_the_class_offsets_and_the_runs_ahead_of_it(self):
        start = sf.seconds("2026-11-02T10:00:00Z")
        found = [{"label": "lane-1", "cmd": "make", "count": 1, "arrival": 0.0, "run": 600.0},
                 {"label": "lane-2-measure", "cmd": "measure.sh", "count": 6, "arrival": 10.0, "run": 600.0}]
        found += [{"label": "sid-kit-wt-1", "cmd": "node scripts/pre-push.mjs origin", "count": 1,
                   "arrival": 20.0 + 60 * n, "run": 300.0} for n in range(240)]
        for number, request in enumerate(found):
            request.update(id=number, patience=None, waited=None, source="history",
                           arrival=start + request["arrival"])
        self.assertEqual("batch", replay.classify(found[1], 6))
        seed = [("sid-kit-wt-1", found[2]["cmd"], 300.0), ("lane-1", "make", 600.0)]
        offsets = replay.CLASSES["interactive"] + replay.CLASSES["batch"]
        head = replay.Replay(found, 6, replay.POLICIES[CLASSES], seed).simulate()[1]
        waited = head["granted"] - found[1]["arrival"]
        self.assertGreater(waited, sf.ORDER_DEFAULTS["limit"], "the limit alone no longer bounds it")
        self.assertLessEqual(waited, offsets + 300 + 2 * replay.POLL, "the offsets and the runs ahead of it do")
        before = replay.Replay(found, 6, replay.POLICIES[SHIPPED], seed).simulate()[1]
        self.assertLessEqual(before["granted"] - found[1]["arrival"], sf.ORDER_DEFAULTS["limit"] + 600 + replay.POLL)


class TraceTests(unittest.TestCase):
    """Requests a slotfather without history left only in queue.log are recovered by their queue sequence."""

    def setUp(self):
        self.dir = pathlib.Path(tempfile.mkdtemp(prefix="slotfather-replay-"))
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)

    def history(self, label, count, queued, granted, ended, pid, outcome="released"):
        at = lambda clock: None if clock is None else f"2026-11-02T{clock}Z"
        ran = None if granted is None else int(sf.seconds(at(ended)) - sf.seconds(at(granted)))
        return {"label": label, "cmd": f"make {label}", "count": count, "pid": pid, "queued": at(queued),
                "granted": at(granted), "ended": at(ended), "outcome": outcome, "ran": ran,
                "waited": int(sf.seconds(at(granted or ended)) - sf.seconds(at(queued)))}

    def test_recorded_and_unrecorded_requests_join_in_queue_order(self):
        items = [self.history("first", 1, "10:00:00", "10:00:00", "10:10:00", 11),
                 self.history("last", 1, "10:20:00", "10:30:00", "10:31:00", 14)]
        (self.dir / "history.jsonl").write_text("".join(json.dumps(i) + "\n" for i in items))
        (self.dir / "queue.log").write_text(
            "2026-11-02T10:00:00Z granted 1 to first (pid 11)\n"
            "2026-11-02T10:05:00Z granted 5 to old-gate (pid 12)\n"  # an old client: no history line
            "2026-11-02T10:10:00Z released 1 (0000000001-11.json)\n"
            "2026-11-02T10:25:00Z released 5 (0000000002-12.json)\n"
            "2026-11-02T10:26:00Z released 6 (0000000003-13.json)\n"  # an old client's waiter that gave up
            "2026-11-02T10:30:00Z granted 1 to last (pid 14)\n"
            "2026-11-02T10:31:00Z released 1 (0000000004-14.json)\n")
        found = replay.requests(self.dir, sf.seconds("2026-11-02T11:00:00Z"))
        self.assertEqual([("first", "history", None), ("old-gate", "log", None),
                          ("(left, unrecorded)", "log", 960.0), ("last", "history", None)],
                         [(r["label"], r["source"], r["patience"]) for r in found])
        gate = found[1]
        self.assertEqual((sf.seconds("2026-11-02T10:05:00Z"), 1200), (gate["arrival"], gate["run"]),
                         "granted before the next known arrival: taken to arrive when granted")
        self.assertEqual(sf.seconds("2026-11-02T10:10:00Z"), found[2]["arrival"], "half way between its neighbours")
        # The gate's slots free at 10:25, before the waiter gave up: in the replay it runs (ASSUMED_RUN), and past
        # its window it held `last` back.
        replayed = replay.Replay(found, 6, replay.POLICIES[CURRENT]).simulate()
        self.assertEqual([0, 0, 900, 900], [r["granted"] - q["arrival"] for r, q in zip(replayed, found)])

    def test_a_reaped_lease_holds_its_slots_but_is_no_run_of_its_kind(self):
        """A reaped held lease (queue.log only, or a history line) keeps its slots in the replay for as long as it
        did, as the queue saw it; the estimates, the replay's and the seed's, leave its time out as the queue's do."""
        (self.dir / "queue.log").write_text(
            "2026-11-02T08:00:00Z granted 1 to ci (pid 21)\n"
            "2026-11-02T08:05:00Z released 1 (0000000001-21.json)\n"
            "2026-11-02T08:10:00Z granted 1 to ci (pid 22)\n"
            "2026-11-02T08:56:40Z reaped held lease of 1 for ci (pid 22 exited)\n"
            "2026-11-02T09:30:00Z granted 1 to orphan (pid 23)\n"
            "2026-11-02T10:16:40Z reaped held lease of 1 for orphan (pid 23 exited)\n")
        items = [self.history("ci", 1, "10:00:00", "10:00:00", "10:05:00", 24),
                 self.history("ci", 1, "10:20:00", "10:20:00", "11:06:40", 25, outcome="reaped")]
        for item in items:
            item["cmd"] = None
        (self.dir / "history.jsonl").write_text("".join(json.dumps(i) + "\n" for i in items))
        logged = replay.log_requests(self.dir / "queue.log")
        self.assertEqual([("ci", 1), ("ci", None), ("orphan", None)], [(r["label"], r["seq"]) for r in logged])
        self.assertEqual([("ci", None, 300)], replay.seed_runs(self.dir, sf.seconds("2026-11-02T09:00:00Z")))
        found = replay.requests(self.dir, sf.seconds("2026-11-02T12:00:00Z"))
        self.assertEqual([("orphan", "log", "reaped", 2800), ("ci", "history", "released", 300),
                          ("ci", "history", "reaped", 2800)],
                         [(r["label"], r["source"], r["outcome"], r["run"]) for r in found])
        run = replay.Replay(found, 1, replay.POLICIES[SHIPPED])
        results = run.simulate()
        self.assertEqual(sf.seconds("2026-11-02T10:16:40Z"), results[1]["granted"], "the orphan held the slot")
        self.assertEqual([300.0], run.estimates.runs[("label", "ci")])
        self.assertIsNone(run.estimates.of("orphan", None))

    def test_a_long_request_does_not_take_a_slot_the_head_needs_when_leases_end_out_of_order(self):
        # #643 review: x and z were expected to end within ten minutes and y in two hours, so by the expected order
        # one slot was spare for `long`; y ends early and z overruns, and the head would have waited for z.
        (self.dir / "queue.log").write_text("".join(
            f"2026-11-02T{start}Z granted {count} to {label} (pid {pid})\n"
            f"2026-11-02T{end}Z released {count} ({pid:010d}-{pid}.json)\n"
            for pid, (label, count, start, end) in enumerate((("x", 2, "06:00:00", "06:05:00"),
                                                             ("z", 2, "06:00:00", "06:10:00"),
                                                             ("y", 1, "06:00:00", "08:00:00"),
                                                             ("long", 1, "08:00:00", "10:00:00")), start=1)))
        items = [self.history("x", 2, "10:00:00", "10:00:00", "10:05:00", 11),
                 self.history("z", 2, "10:00:00", "10:00:00", "10:50:00", 12),
                 self.history("y", 1, "10:00:00", "10:00:00", "10:15:00", 13),
                 self.history("head", 4, "10:00:30", "10:50:00", "10:51:00", 14),
                 self.history("long", 1, "10:11:00", "10:51:00", "12:51:00", 15)]
        for item in items:
            item["cmd"] = None  # known by label, as the seed runs
        (self.dir / "history.jsonl").write_text("".join(json.dumps(i) + "\n" for i in items))
        found = replay.requests(self.dir, sf.seconds("2026-11-02T13:00:00Z"))
        replayed = {q["label"]: r for q, r in zip(found, replay.Replay(
            found, 6, replay.POLICIES[SHIPPED], replay.seed_runs(self.dir, found[0]["arrival"])).simulate())}
        self.assertEqual(sf.seconds("2026-11-02T10:15:00Z"), replayed["head"]["granted"], "it starts when y ends")
        self.assertGreaterEqual(replayed["long"]["granted"], replayed["head"]["granted"])


if __name__ == "__main__":
    unittest.main()
