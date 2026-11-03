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


class RecordedReplayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        policies = {name: replay.POLICIES[name] for name in (CURRENT, SHIPPED, "fifo (window 0)",
                                                              "backfill, estimates noisy")}
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

    def test_the_incident_waits_far_less(self):
        current, shipped = (self.incident["policies"][name] for name in (CURRENT, SHIPPED))
        self.assertLess(shipped["total_h"], 0.7 * current["total_h"])
        self.assertLessEqual(shipped["blocked_slot_h"], current["blocked_slot_h"])


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


if __name__ == "__main__":
    unittest.main()
