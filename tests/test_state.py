"""State schema upgrades preserve old entries and refuse future writers."""
import json
from pathlib import Path
import tempfile
import unittest

from test_slotfather import ROOT
import sys
sys.path.insert(0, str(ROOT / "src"))
from slotfather import config, state


class SchemaTests(unittest.TestCase):
    def test_unversioned_state_is_adopted_without_rewriting_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'queue').mkdir()
            entry = root / 'queue/0000000001-1.json'
            entry.write_text('{"pid":1,"state":"held","count":2}\n')
            before = entry.read_bytes()
            with state.locked(root):
                self.assertEqual(1, state.schema_version(root))
            self.assertEqual(before, entry.read_bytes())
            self.assertEqual({'v': 1}, json.loads((root / 'schema.json').read_text()))

    def test_future_schema_is_refused_without_touching_the_queue(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'schema.json').write_text('{"v":999}\n')
            before = {p.name: p.read_bytes() for p in root.iterdir()}
            with self.assertRaisesRegex(config.SlotError, 'schema.*999'):
                with state.locked(root):
                    self.fail('future state must never be used')
            self.assertEqual(before, {p.name: p.read_bytes() for p in root.iterdir()})


if __name__ == '__main__':
    unittest.main()
