#!/usr/bin/env python3
"""Lease metadata (#650): `run --meta key=value` on the queue entry, in the history, `status` (and `status --json`
for programs such as a guard that must tell whose lease a process belongs to), the dashboard and `stats --by
meta.KEY`; an older slotfather on the same queue."""
from __future__ import annotations
import sys as _path_sys
from pathlib import Path as _Path
_path_sys.path.insert(0, str(_Path(__file__).resolve().parents[1] / "src"))
from slotfather import config as sf_config


import json
import os
import subprocess
import sys
import unittest

from test_slotfather import ROOT, TOOL, WORK, SlotCase
from test_usage import load


# Work that writes its own pid, its lease and its process group before it waits for its release file.
TELL = 'echo "$$ $SLOTFATHER_LEASE $(ps -o pgid= -p $$)" > "$RELEASE/$1.who"; ' + WORK


class MetaTests(SlotCase):
    def told(self, label):
        path = self.release_dir / f"{label}.who"
        self.wait_for(lambda: path.exists() and path.read_text().endswith("\n"), f"{label} to say who it is")
        pid, lease, pgid = path.read_text().split()
        return int(pid), lease, int(pgid)

    def test_meta_rides_on_the_entry_the_status_and_the_history(self):
        process = self.request("lane-650", 1, "sh", "-c", TELL, "work", "lane-650",
                               options=("--meta", "ticket=650", "--meta", "lane=lane-650", "--meta",
                                        "phase=verify checks", "--meta", "model=opus"))
        self.started("lane-650")
        child, lease, pgid = self.told("lane-650")
        meta = {"ticket": "650", "lane": "lane-650", "phase": "verify checks", "model": "opus"}
        self.assertEqual(meta, json.loads(pathlib_read(lease))["meta"])
        self.assertIn('meta=ticket=650,lane=lane-650,phase="verify checks",model=opus cmd=sh -c', self.status())

        found = json.loads(self.tool("status", "--json").stdout)
        self.assertEqual((4, 1, 3), (found["slots"], found["held"], found["free"]))
        [entry] = found["entries"]
        self.assertEqual(lease, entry["lease"], "the lease is what SLOTFATHER_LEASE holds")
        self.assertEqual(os.path.basename(lease)[:-5], entry["id"])
        self.assertEqual(process.pid, entry["pid"], "the holder is `slotfather run`")
        self.assertEqual(pgid, entry["pgid"])
        self.assertIn(child, entry["tree"])
        self.assertIn(process.pid, entry["tree"])
        self.assertEqual(("held", 1, "lane-650", meta), (entry["state"], entry["count"], entry["label"],
                                                          entry["meta"]))
        mine = json.loads(self.tool("status", "--json", "--pid", str(child)).stdout)
        self.assertEqual([lease], [e["lease"] for e in mine["entries"]], "the lease this process belongs to")
        nobody = json.loads(self.tool("status", "--json", "--pid", str(os.getpid())).stdout)
        self.assertEqual([], nobody["entries"])

        self.release("lane-650")
        self.finish(process)
        [line] = [json.loads(line) for line in (self.state / "history.jsonl").read_text().splitlines()]
        self.assertEqual(meta, line["meta"])
        self.assertIn("meta=ticket=650,", self.tool("history").stdout)

    def test_a_process_that_left_the_tree_is_found_by_the_lease_in_its_environment(self):
        # review P2-3: a double fork reparents the process to launchd; by the tree alone it is in no lease.
        # The orphan is python (not an Apple platform binary), whose environment ps shows.
        orphan = (f'sh -c \'"{sys.executable}" -c "import time; time.sleep(60)" >/dev/null 2>&1 </dev/null & echo $! > "$RELEASE/orphan.pid"\'; '
                  + WORK)
        process = self.request("lane", 1, "sh", "-c", orphan, "work", "lane")
        self.started("lane")
        path = self.release_dir / "orphan.pid"
        self.wait_for(lambda: path.exists() and path.read_text().strip(), "the orphan to start")
        pid = int(path.read_text())
        self.addCleanup(lambda: subprocess.run(["kill", str(pid)], capture_output=True))
        self.wait_for(lambda: subprocess.run(["ps", "-o", "ppid=", "-p", str(pid)], capture_output=True,
                                             text=True).stdout.strip() == "1", "launchd to adopt the orphan")
        found = json.loads(self.tool("status", "--json", "--pid", str(pid)).stdout)
        [entry] = found["entries"]
        self.assertEqual(("lane", "env"), (entry["label"], entry["via"]))
        self.assertNotIn(pid, entry["tree"])
        start = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True, text=True,
                               env=self.env).stdout.strip()
        self.assertEqual({"pid": pid, "start": start}, found["query"], "the start time, to re-check before acting")
        holder = json.loads(self.tool("status", "--json", "--pid", str(process.pid)).stdout)
        self.assertEqual(["tree"], [e["via"] for e in holder["entries"]])
        gone = json.loads(self.tool("status", "--json", "--pid", "99999999").stdout)
        self.assertEqual(([], {"pid": 99999999, "start": None}), (gone["entries"], gone["query"]))
        self.release("lane")
        self.finish(process)

    def test_a_nested_run_inherits_the_lease_and_adds_no_meta(self):
        nested = f'{TOOL} run --meta ticket=999 --meta lane=other -- sh -c "$WORK" work "$1"'
        self.env["WORK"] = WORK
        process = self.request("outer", 1, "sh", "-c", nested, "outer", "outer",
                               options=("--meta", "ticket=650"))
        self.started("outer")
        found = json.loads(self.tool("status", "--json").stdout)
        self.assertEqual([{"ticket": "650"}], [e["meta"] for e in found["entries"]])
        self.release("outer")
        self.finish(process)
        lines = (self.state / "history.jsonl").read_text().splitlines()
        self.assertEqual([{"ticket": "650"}], [json.loads(line).get("meta") for line in lines])

    def test_meta_that_is_not_a_short_key_and_printable_value_is_refused(self):
        for pair in ("ticket", "Ticket=1", "=1", "t" * 33 + "=1", "ticket=a\x1bb", "ticket=" + "x" * 201):
            result = self.tool("run", "--meta", pair, "--", "true")
            self.assertEqual(2, result.returncode, pair)
            self.assertIn("--meta", result.stderr)
        self.assertFalse((self.state / "history.jsonl").exists(), "nothing queued")
        result = self.tool("run", "--meta", "ticket=1", "--meta", "ticket=2", "--", "true")
        self.assertEqual(0, result.returncode, "a later key replaces an earlier one")
        self.assertEqual({"ticket": "2"}, json.loads((self.state / "history.jsonl").read_text())["meta"])
        with self.assertRaises(sf_config.SlotError):
            sf_config.checked_meta({f"k{n}": "v" for n in range(17)})

    def test_status_json_options_are_checked(self):
        for args in (("--json", "--pid"), ("--json", "--pid", "x"), ("--json", "extra"), ("--pid", "1")):
            self.assertEqual(2, self.tool("status", *args).returncode, args)
        found = json.loads(self.tool("status", "--json").stdout)
        self.assertEqual({"v": 1, "slots": 4, "held": 0, "free": 4, "entries": [], "legacy": []}, found)

    def test_entries_without_meta_read_as_before(self):
        pid, start = self.holder()
        self.queue_entry(self.state, pid, start, 1, "old")
        self.assertRegex(self.status(), r"held: 1 slot\(s\) old pid=\d+ since=- cmd=fake\n")
        self.assertEqual([{}], [e["meta"] for e in json.loads(self.tool("status", "--json").stdout)["entries"]])
        self.assertEqual({}, sf_config.meta_of({"meta": ["not", "a", "mapping"]}))
        self.assertEqual({"ticket": "1"}, sf_config.meta_of({"meta": {"ticket": "1", "Bad Key\x1b": "x", "": "y"}}),
                         "keys a foreign writer left that --meta would refuse are dropped")

    def test_the_dashboard_shows_the_selected_lease_meta(self):
        process = self.request("lane-650", 1, options=("--meta", "ticket=650"))
        self.started("lane-650")
        text = self.tool("dashboard", "--once", "--width", "160", "--height", "40").stdout
        self.assertRegex(text, r"meta +ticket=650")
        self.release("lane-650")
        self.finish(process)


class MetaStatsTests(SlotCase):
    def test_stats_group_by_a_meta_key(self):
        self.state.mkdir(parents=True)
        line = {"v": 2, "label": "x", "cmd": "make", "count": 1, "granted": "2026-11-02T11:00:00Z",
                "ended": "2026-11-02T11:10:00Z", "outcome": "released", "waited": 10, "ran": 600, "status": 0,
                "cpu": 300.0, "wall": 600}
        lines = [{**line, "meta": {"ticket": "650"}}, {**line, "meta": {"ticket": "650", "lane": "a"}},
                 {**line, "meta": {"lane": "a"}}, line]
        (self.state / "history.jsonl").write_text("".join(json.dumps(i) + "\n" for i in lines))
        result = self.tool("stats", "--json", "--by", "meta.ticket")
        self.assertEqual(0, result.returncode, result.stderr)
        groups = {g["meta"]["ticket"]: g["requests"] for g in json.loads(result.stdout)["groups"]}
        self.assertEqual({"650": 2, None: 2}, groups)
        text = self.tool("stats", "--by", "meta.ticket").stdout
        self.assertRegex(text, r"META\.TICKET +RUNS")
        self.assertRegex(text, r"\n650 +2 ")
        self.assertRegex(text, r"\n- +2 ")
        self.assertEqual(2, self.tool("stats", "--by", "meta.Bad").returncode)


@unittest.skipUnless((ROOT / ".git").exists(), "needs the git history for the previous slotfather")
class MixedMetaTests(SlotCase):
    """3e3a621, the slotfather before #650, on the same queue as this one."""

    OLD = "3e3a621"

    def setUp(self):
        super().setUp()
        found = subprocess.run(["git", "-C", str(ROOT), "show", f"{self.OLD}:slotfather"], capture_output=True)
        if found.returncode:
            self.skipTest(f"{self.OLD} is not in this checkout")
        self.old = self.tmp / "slotfather-old"
        self.old.write_bytes(found.stdout)
        self.old.chmod(0o755)

    def test_an_old_client_reads_entries_with_meta_and_keeps_it_when_it_grants(self):
        first = subprocess.Popen([str(self.old), "run", "-n", "all", "--label", "old-gate", "--", "sh", "-c", WORK,
                                  "work", "old-gate"], env=self.env, stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT, text=True, start_new_session=True)
        self.processes.append(first)
        self.started("old-gate")
        new = self.request("new", 1, options=("--meta", "ticket=650"))
        self.wait_for(lambda: "waiting: 1 slot(s) new" in self.status(), "the new request to queue")
        listing = subprocess.run([str(self.old), "status"], env=self.env, capture_output=True, text=True).stdout
        self.assertRegex(listing, r"waiting: 1 slot\(s\) new pid=\d+ since=\S+ cmd=sh -c")
        self.release("old-gate")  # the old holder's release grants the new waiter
        self.started("new")
        self.assertIn("meta=ticket=650", self.status(), "the old client's grant kept the meta")
        self.release("new")
        for process in (first, new):
            self.finish(process)
        history = {json.loads(line)["label"]: json.loads(line) for line in
                   (self.state / "history.jsonl").read_text().splitlines()}
        self.assertEqual({"ticket": "650"}, history["new"]["meta"])
        self.assertNotIn("meta", history["old-gate"])


def pathlib_read(path: str) -> str:
    with open(path) as handle:
        return handle.read()


if __name__ == "__main__":
    unittest.main()
