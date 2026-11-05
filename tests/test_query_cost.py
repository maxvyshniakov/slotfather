"""The compatible status query batches detached lease-marker inspection."""
from __future__ import annotations

import importlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


class StatusInspectionTests(unittest.TestCase):
    def test_one_environment_observation_matches_all_candidate_leases(self) -> None:
        query = importlib.import_module("slotfather.query")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            queue = root / "queue"
            queue.mkdir()
            born = "Wed Nov  4 10:00:00 2026"
            for number in (51, 52, 53):
                (queue / f"{number:010d}-{number}.json").write_text(json.dumps({
                    "pid": number, "start": born, "count": 1, "state": "held", "label": str(number),
                    "since": "2026-11-04T10:00:00Z", "cmd": "work"}))
            marker = str(queue / "0000000052-52.json")
            table = "\n".join(f"{number} 1 S {born}" for number in (51, 52, 53, 200))
            def run(argv: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
                output = f"200 worker SLOTFATHER_LEASE={marker}\n" if "-wwE" in argv else table
                return subprocess.CompletedProcess(argv, 0, output, "")
            with mock.patch.dict(os.environ, {"SLOTFATHER_DIR": str(root), "SLOTFATHER_LEGACY_DIRS": "",
                                              "SLOTFATHER_CORES": "12", "SLOTFATHER_MEMORY_GIB": "64"}), \
                    mock.patch("sys.platform", "darwin"), mock.patch("ctypes.CDLL", side_effect=OSError("native fixture boundary")), \
                    mock.patch("subprocess.run", side_effect=run) as calls:
                found = query.status_json(pid=200)
            self.assertEqual(["0000000052-52"], [item["id"] for item in found["entries"]])
            self.assertEqual("env", found["entries"][0]["via"])
            self.assertEqual(1, sum("-wwE" in call.args[0] for call in calls.call_args_list),
                             "status must inspect a PID's environment once, regardless of queue size")


if __name__ == "__main__":
    unittest.main()
