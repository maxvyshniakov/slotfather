"""Read-only process query through the public package and command interface."""
from __future__ import annotations

import importlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))


class ProcsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="slotfather-procs-")
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name) / "state"
        self.environment = mock.patch.dict(os.environ, {
            "SLOTFATHER_DIR": str(self.directory), "SLOTFATHER_LEGACY_DIRS": "",
        })
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.procs = importlib.import_module("slotfather.procs")
        platform = mock.patch("sys.platform", "linux")
        platform.start()
        self.addCleanup(platform.stop)

    def ps(self, *rows: str) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess([], 0, "\n".join(rows) + "\n", "")

    def test_pid_query_is_read_only_and_exposes_identity_without_cwd_scan(self):
        output = self.ps("51 1 501 ?? S Wed Nov  4 10:00:00 2026 /usr/local/bin/claude",
                         "52 51 501 ?? S Wed Nov  4 10:00:01 2026 python3 test.py")
        with mock.patch("subprocess.run", return_value=output) as run:
            found = self.procs.query(pid=52)
        self.assertEqual(2, found["v"])
        self.assertEqual({"pid": 52, "cwd": None, "start": "Wed Nov 4 10:00:01 2026"}, found["query"])
        self.assertEqual([52], [item["pid"] for item in found["processes"]])
        item = found["processes"][0]
        self.assertEqual([51], item["ancestors"])
        self.assertEqual([51], [session["pid"] for session in item["sessions"]])
        self.assertEqual([], item["leases"])
        self.assertFalse(self.directory.exists(), "queries must never create or reap queue state")
        self.assertEqual(1, run.call_count, "the fast PID query reads ps once and never runs lsof")

    def lease(self, name: str, *, pid: int = 51, start: str = "Wed Nov  4 10:00:00 2026", **extra: object) -> Path:
        queue = self.directory / "queue"
        queue.mkdir(parents=True, exist_ok=True)
        path = queue / f"{name}.json"
        path.write_text(json.dumps({"pid": pid, "start": start, "state": "held", "count": 2,
                                    "label": name, "priority": "batch", "meta": {"session": "session-a"}, **extra}))
        return path

    def test_live_leases_join_by_ancestry_and_reused_holders_are_excluded_without_reaping(self):
        valid = self.lease("active")
        stale = self.lease("stale", start="Tue Nov  3 10:00:00 2026")
        before = {path: path.read_bytes() for path in (valid, stale)}
        output = self.ps("51 1 501 ?? S Wed Nov  4 10:00:00 2026 /usr/local/bin/claude",
                         "52 51 501 ?? S Wed Nov  4 10:00:01 2026 python3 test.py")
        with mock.patch("subprocess.run", return_value=output):
            found = self.procs.query(pid=52)
        leases = found["processes"][0]["leases"]
        self.assertEqual(["active"], [item["id"] for item in leases])
        self.assertEqual((51, "batch", "tree", "session-a"),
                         (leases[0]["pid"], leases[0]["priority"], leases[0]["via"], leases[0]["meta"]["session"]))
        self.assertEqual(before, {path: path.read_bytes() for path in before})
        self.assertEqual({"queue"}, {path.name for path in self.directory.iterdir()})

    def test_cwd_filter_matches_directory_boundaries_and_omits_other_users_and_callers(self):
        uid = os.geteuid()
        output = self.ps(f"51 1 {uid} ?? S Wed Nov  4 10:00:00 2026 /bin/zsh",
                         f"52 51 {uid} ?? S Wed Nov  4 10:00:01 2026 python3 test.py",
                         f"53 51 {uid} ?? S Wed Nov  4 10:00:02 2026 node neighbor.js",
                         f"54 1 {uid + 1} ?? S Wed Nov  4 10:00:03 2026 node other.js",
                         f"9999 51 {uid} ?? S Wed Nov  4 10:00:04 2026 slotfather procs")
        cwds = subprocess.CompletedProcess([], 0,
            "p51\nn/work/lane\np52\nn/work/lane/tests\np53\nn/work/lane-other\np54\nn/work/lane\np9999\nn/work/lane\n", "")
        with mock.patch("sys.platform", "darwin"), mock.patch("os.getpid", return_value=9999), \
                mock.patch("subprocess.run", side_effect=[output, cwds]):
            found = self.procs.query(cwd="/work/lane")
        self.assertEqual([52], [item["pid"] for item in found["processes"]])
        self.assertEqual("/work/lane/tests", found["processes"][0]["cwd"])
        self.assertEqual("/work/lane", found["query"]["cwd"])

    def test_detached_process_matches_exact_lease_environment_without_exposing_environment(self):
        lease = self.lease("active")
        output = self.ps("51 1 501 ?? S Wed Nov  4 10:00:00 2026 python3 holder.py",
                         "53 1 501 ?? S Wed Nov  4 10:00:01 2026 node detached.js")
        environment = self.ps(f"53 node detached.js SECRET=never-print SLOTFATHER_LEASE={lease} NEXT=value")
        with mock.patch("sys.platform", "darwin"), mock.patch("subprocess.run", side_effect=[output, environment]):
            found = self.procs.query()
        self.assertEqual("env", found["processes"][-1]["leases"][0]["via"])
        self.assertNotIn("never-print", json.dumps(found))
        wrong = self.ps(f"53 node detached.js SLOTFATHER_LEASE={lease}-other")
        with mock.patch("sys.platform", "darwin"), mock.patch("subprocess.run", side_effect=[output, wrong]):
            found = self.procs.query()
        self.assertEqual([], found["processes"][-1]["leases"])
        self.assertEqual([53], found["lease_unknown"], "psE cannot prove that a negative environment was readable")

    def test_command_returns_versioned_json_and_rejects_ambiguous_options(self):
        output = self.ps("52 1 501 ?? S Wed Nov  4 10:00:01 2026 python3 test.py")
        stdout = io.StringIO()
        with mock.patch("subprocess.run", return_value=output), mock.patch("sys.stdout", stdout):
            self.assertEqual(0, self.procs.command(["--json", "--pid", "52"]))
        self.assertEqual(52, json.loads(stdout.getvalue())["query"]["pid"])
        for args in (["--pid", "52"], ["--json", "--pid", "0"], ["--json", "--pid", "52", "--pid", "53"],
                     ["--json", "--cwd"], ["--json", "--unknown"]):
            with self.subTest(args=args), self.assertRaises(self.procs.ProcsError):
                self.procs.command(args)

    def test_unreadable_process_or_cwd_tables_fail_closed(self):
        failed = subprocess.CompletedProcess([], 1, "", "permission denied")
        with mock.patch("subprocess.run", return_value=failed), self.assertRaises(self.procs.ProcsError):
            self.procs.query(pid=52)
        output = self.ps("52 1 501 ?? S Wed Nov  4 10:00:01 2026 python3 test.py")
        with mock.patch("sys.platform", "darwin"), mock.patch("subprocess.run", side_effect=[output, failed]), \
                self.assertRaises(self.procs.ProcsError):
            self.procs.query(cwd="/work/lane")
        with mock.patch("subprocess.run", return_value=self.ps()), self.assertRaises(self.procs.ProcsError):
            self.procs.query()

    def test_linux_cwd_uses_local_mount_namespace_and_reports_unknown_paths(self):
        uid = os.geteuid()
        proc = Path(self.tmp.name) / "proc"
        (proc / "self/ns").mkdir(parents=True)
        (proc / "self/ns/mnt").symlink_to("mnt:[111]")
        for number, namespace in ((52, "mnt:[111]"), (53, "mnt:[222]")):
            (proc / str(number) / "ns").mkdir(parents=True)
            (proc / str(number) / "ns/mnt").symlink_to(namespace)
            (proc / str(number) / "cwd").symlink_to("/work/lane/tests")
        output = self.ps(f"52 1 {uid} ?? S Wed Nov  4 10:00:01 2026 python3 test.py",
                         f"53 1 {uid} ?? S Wed Nov  4 10:00:02 2026 node container.js")
        with mock.patch("sys.platform", "linux"), mock.patch.dict(os.environ, {"SLOTFATHER_PROC": str(proc)}), \
                mock.patch("subprocess.run", return_value=output):
            found = self.procs.query(cwd="/work/lane")
        self.assertEqual([52], [item["pid"] for item in found["processes"]])
        self.assertEqual([53], found["cwd_unknown"], "foreign paths must never be treated as local directories")

    def test_missing_and_zombie_pid_return_no_live_identity(self):
        output = self.ps("51 1 501 ?? S Wed Nov  4 10:00:00 2026 /bin/sh",
                         "52 1 501 ?? Z Wed Nov  4 10:00:01 2026 dead")
        with mock.patch("subprocess.run", return_value=output):
            found = self.procs.query(pid=52)
        self.assertEqual([], found["processes"])
        self.assertIsNone(found["query"]["start"])

    def test_malformed_live_identity_is_unknown_and_never_an_unleased_answer(self):
        path = self.lease("active")
        path.write_text(json.dumps({"pid": 51, "state": "held", "count": 2}))
        output = self.ps("51 1 501 ?? S Wed Nov  4 10:00:00 2026 python3 holder.py")
        with mock.patch("subprocess.run", return_value=output), self.assertRaises(self.procs.ProcsError):
            self.procs.query(pid=51)
        self.assertTrue(path.exists())
        self.lease("active", start="")
        with mock.patch("subprocess.run", return_value=output), self.assertRaises(self.procs.ProcsError):
            self.procs.query(pid=51)

    def test_future_schema_is_refused_without_modifying_the_queue(self):
        self.directory.mkdir()
        path = self.directory / "schema.json"
        path.write_text('{"v": 999}\n')
        output = self.ps("51 1 501 ?? S Wed Nov  4 10:00:00 2026 python3 holder.py")
        errors = importlib.import_module("slotfather.config")
        with mock.patch("subprocess.run", return_value=output), self.assertRaisesRegex(errors.SlotError, "schema"):
            self.procs.query(pid=51)
        self.assertEqual('{"v": 999}\n', path.read_text())


if __name__ == "__main__":
    unittest.main()
