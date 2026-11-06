"""macOS admission uses live memory pressure, not free space in allocated swap."""
from __future__ import annotations

import importlib
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
platform = importlib.import_module("slotfather.platform")


class MacosMemoryGuardTests(unittest.TestCase):
    def short(self, level: str | None, free: float = 460.81) -> str | None:
        environment = {k: v for k, v in os.environ.items() if not k.startswith("SLOTFATHER_")}

        def sysctl(name: str) -> str | None:
            return {
                "vm.swapusage": f"total = 3072.00M  used = 2611.19M  free = {free}M  (encrypted)",
                "kern.memorystatus_vm_pressure_level": level,
            }.get(name)

        with mock.patch.dict(os.environ, environment, clear=True), \
                mock.patch.object(sys, "platform", "darwin"), \
                mock.patch.object(platform, "sysctl", side_effect=sysctl):
            return platform.memory_short()

    def test_normal_pressure_does_not_treat_current_swap_allocation_as_a_limit(self):
        self.assertIsNone(self.short("1"))
        self.assertIsNone(self.short("1", free=0))

    def test_warning_and_critical_still_block_even_with_free_swap(self):
        for level in ("2", "4"):
            for free in (0, 460.81, 8000):
                with self.subTest(level=level, free=free):
                    self.assertEqual(f"memory pressure level {level} (warning or critical)",
                                     self.short(level, free))

    def test_unknown_pressure_keeps_the_conservative_swap_fallback(self):
        for level in (None, "unavailable", "0"):
            with self.subTest(level=level):
                self.assertEqual("free swap 461 MB is under 512 MB", self.short(level))
                self.assertIsNone(self.short(level, free=8000))

    def test_explicit_diagnostic_seam_keeps_its_swap_floor(self):
        with tempfile.TemporaryDirectory() as directory:
            memory = Path(directory) / "memory"
            memory.write_text("460.81 1\n")
            with mock.patch.dict(os.environ, {"SLOTFATHER_MEMORY_FILE": str(memory)}), \
                    mock.patch.object(sys, "platform", "darwin"):
                self.assertEqual("free swap 461 MB is under 512 MB", platform.memory_short())


if __name__ == "__main__":
    unittest.main()
