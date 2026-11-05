"""The macOS native process adapter against the real kernel and the ps contract."""
from __future__ import annotations

import importlib
import os
from pathlib import Path
import subprocess
import sys
import time
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))


class ArgumentProcessTests(unittest.TestCase):
    def test_argument_read_failure_is_not_a_short_name_fallback(self):
        native = importlib.import_module("slotfather.macos_process")
        with mock.patch("subprocess.run", side_effect=subprocess.TimeoutExpired("ps", 5)), \
                self.assertRaises(OSError):
            native.argument_processes({52})

    def test_legacy_single_digit_day_has_the_exact_ps_spacing(self):
        native = importlib.import_module("slotfather.macos_process")
        clock = time.struct_time((2026, 11, 4, 10, 0, 1, 2, 308, -1))
        with mock.patch("time.localtime", return_value=clock):
            self.assertEqual("Wed Nov  4 10:00:01 2026", native.start_text(0, padded_day=True))
            self.assertEqual("Wed Nov 4 10:00:01 2026", native.start_text(0))


@unittest.skipUnless(sys.platform == "darwin", "macOS libproc adapter")
class MacosProcessTests(unittest.TestCase):
    def test_native_identity_matches_ps_for_the_live_caller(self):
        native = importlib.import_module("slotfather.macos_process")
        number = os.getpid()
        with mock.patch("subprocess.run", wraps=subprocess.run) as run:
            observed = native.snapshot(number)
        self.assertEqual(1, run.call_count)
        self.assertNotIn("-E", run.call_args.args[0])
        process = observed[number]
        output = subprocess.check_output(["ps", "-ww", "-p", str(number), "-o", "pid=,ppid=,uid=,lstart=,command="],
                                         text=True, env={**os.environ, "LC_ALL": "C"})
        fields = output.split(None, 8)
        self.assertEqual((number, os.getppid(), os.geteuid()), process[:3])
        self.assertEqual(" ".join(fields[3:8]), process[4])
        self.assertIn("unittest", process[5])
        self.assertEqual(fields[8].strip(), process[5])
        self.assertNotIn("SLOTFATHER_", process[5])

    def test_basic_snapshot_preserves_legacy_ps_start_spacing(self):
        native = importlib.import_module("slotfather.macos_process")
        number = os.getpid()
        observed = native.basic_snapshot({number})
        output = subprocess.check_output(["ps", "-p", str(number), "-o", "ppid=,lstart="],
                                         text=True, env={**os.environ, "LC_ALL": "C"}).strip()
        parent, start = output.split(None, 1)
        self.assertEqual((int(parent), start), observed[number])

    def test_short_bsd_identity_matches_protected_init_parent(self):
        native = importlib.import_module("slotfather.macos_process")
        libproc, _ = native.libraries()
        observed = native.short_info(libproc, 1)
        output = subprocess.check_output(["/bin/ps", "-p", "1", "-o", "pid=,ppid="], text=True)
        self.assertEqual(tuple(map(int, output.split())), (observed.pbsi_pid, observed.pbsi_ppid))
        self.assertNotEqual(5, observed.pbsi_status)


if __name__ == "__main__":
    unittest.main()
