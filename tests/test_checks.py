"""The quality gate rejects new debt and permits only smaller saved baselines."""
import importlib.util
from pathlib import Path
from types import ModuleType
import unittest
import json
import os
import shutil
import subprocess
import sys
import tempfile

CHECK = Path(__file__).resolve().parents[1] / "tools/check.py"


class BaselineTests(unittest.TestCase):
    def checker(self) -> ModuleType:
        spec = importlib.util.spec_from_file_location("slotfather_check", CHECK)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_new_diagnostics_cannot_borrow_another_files_budget(self) -> None:
        check = self.checker()
        accepted = {"src/slotfather/leases.py|pyright|reportUnknownVariableType|Type is unknown": 2}
        current = {"src/slotfather/procs.py|pyright|reportUnknownVariableType|Type is unknown": 1}
        self.assertEqual(current, check.regressions(current, accepted))

    def test_shrink_removes_resolved_diagnostics_and_rejects_growth(self) -> None:
        check = self.checker()
        accepted = {"old|ruff|F401|Unused import": 2, "other|pyright|unknown|Unknown": 1}
        current = {"old|ruff|F401|Unused import": 1}
        self.assertEqual(current, check.shrink_baseline(current, accepted))
        with self.assertRaisesRegex(ValueError, "new diagnostics"):
            check.shrink_baseline({"old|ruff|F401|Unused import": 3}, accepted)
        self.assertEqual(accepted, {"old|ruff|F401|Unused import": 2, "other|pyright|unknown|Unknown": 1})

    def test_committed_budget_cannot_restore_previously_removed_debt(self) -> None:
        check = self.checker()
        history = [{"a": 2, "b": 1}, {"a": 1}, {"a": 1, "c": 2}]
        self.assertEqual({"a": 1}, check.smallest_budget(history))

    def test_deleting_and_restoring_a_committed_baseline_cannot_restore_its_budget(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "tools").mkdir()
            shutil.copyfile(CHECK, root / "tools/check.py")
            baseline = root / "tools/type-baseline.json"
            key = "runtime.py|ruff|F401|unused"
            payload = json.dumps({"version": 1, "diagnostics": {key: 1}})
            git = ["git", "-C", str(root), "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
                   "-c", "core.hooksPath=/dev/null"]
            subprocess.run([*git, "init", "-q"], check=True)
            baseline.write_text(payload)
            subprocess.run([*git, "add", "tools/type-baseline.json"], check=True)
            subprocess.run([*git, "commit", "-qm", "adopt baseline"], check=True)
            subprocess.run([*git, "rm", "-q", "tools/type-baseline.json"], check=True)
            subprocess.run([*git, "commit", "-qm", "remove baseline"], check=True)
            baseline.write_text(payload)
            subprocess.run([*git, "add", "tools/type-baseline.json"], check=True)
            subprocess.run([*git, "commit", "-qm", "restore baseline"], check=True)
            bins = root / "bin"
            bins.mkdir()
            reports: dict[str, object] = {"ruff": [{"filename": str(root / "runtime.py"), "code": "F401", "message": "unused"}],
                       "pyright": {"generalDiagnostics": []}}
            for name, report in reports.items():
                executable = bins / name
                executable.write_text(f"#!{sys.executable}\nimport json\nprint({json.dumps(json.dumps(report))})\n")
                executable.chmod(0o755)
            result = subprocess.run([sys.executable, str(root / "tools/check.py")],
                                    env={**os.environ, "PATH": str(bins) + os.pathsep + os.environ["PATH"]},
                                    capture_output=True, text=True)
            self.assertNotEqual(0, result.returncode, "deleted committed debt must never reappear")
            self.assertIn("baseline", result.stderr + result.stdout)

    def test_unrecognized_or_invalid_baselines_fail_closed(self) -> None:
        check = self.checker()
        for text in ('[]', '{}', '{"version": 2, "diagnostics": {}}',
                     '{"version": true, "diagnostics": {}}',
                     '{"version": 1, "diagnostics": {"some": -1}}',
                     '{"version": 1, "diagnostics": {"some": true}}'):
            with self.subTest(text=text), self.assertRaises(ValueError):
                check.read_baseline(text)


if __name__ == "__main__":
    unittest.main()
