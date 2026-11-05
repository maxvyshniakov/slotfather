"""Priority and ticket are request fields; retired metadata aliases are rejected."""
from __future__ import annotations

import json
import importlib
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
config = importlib.import_module('slotfather.config')


class PriorityTests(unittest.TestCase):
    def test_cli_priority_and_default_are_exported_and_preserved(self) -> None:
        for options, expected in ((['--priority', 'interactive'], 'interactive'),
                                  (['--priority', 'batch'], 'batch'), ([], 'normal')):
            with self.subTest(options=options), tempfile.TemporaryDirectory() as tmp:
                env = {key: value for key, value in os.environ.items()
                       if not key.startswith(('SLOTFATHER', 'STRAYLIGHT_SLOT'))}
                env.update(SLOTFATHER_DIR=tmp, SLOTFATHER_LEGACY_DIRS='', SLOTFATHER_CORES='8')
                child = ('import json,os,subprocess; '
                         'entry=json.load(open(os.environ["SLOTFATHER_LEASE"])); '
                         'status=json.loads(subprocess.check_output(' + repr([str(ROOT / 'slotfather'), 'status', '--json']) + ')); '
                         'print(json.dumps({"entry":entry,"status":status["entries"][0]}))')
                run = subprocess.run([str(ROOT / 'slotfather'), 'run', *options, '--ticket', '42',
                                      '--', sys.executable, '-c', child], env=env, text=True, capture_output=True, timeout=10)
                self.assertEqual(0, run.returncode, run.stderr)
                data = json.loads(run.stdout)
                self.assertEqual(expected, data['entry']['priority'])
                self.assertEqual(expected, data['status']['priority'])
                self.assertNotIn('class', data['status'])
                self.assertNotIn('class', data['entry'].get('meta', {}))
                history = json.loads((Path(tmp) / 'history.jsonl').read_text())
                self.assertEqual(expected, history['priority'])

    def test_ticket_is_an_optional_request_field_and_the_library_accepts_priority(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = {key: value for key, value in os.environ.items()
                   if not key.startswith(('SLOTFATHER', 'STRAYLIGHT_SLOT'))}
            env.update(SLOTFATHER_DIR=tmp, SLOTFATHER_LEGACY_DIRS='', SLOTFATHER_CORES='8',
                       PYTHONPATH=str(ROOT / 'src'))
            code = ('import json; from slotfather import leases; '
                    'lease=leases.acquire(1,"api",priority="interactive",ticket=42); '
                    'print(lease.path.read_text()); lease.release()')
            run = subprocess.run([sys.executable, '-c', code], env=env, text=True, capture_output=True, timeout=10)
            self.assertEqual(0, run.returncode, run.stderr)
            entry = json.loads(run.stdout)
            self.assertEqual(('interactive', '42'), (entry['priority'], entry['ticket']))
            run = subprocess.run([str(ROOT / 'slotfather'), 'run', '--ticket', '#99', '--',
                                  sys.executable, '-c', 'import os;print(open(os.environ["SLOTFATHER_LEASE"]).read())'],
                                 env=env, text=True, capture_output=True, timeout=10)
            self.assertEqual(0, run.returncode, run.stderr)
            self.assertEqual('99', json.loads(run.stdout)['ticket'])
            run = subprocess.run([str(ROOT / 'slotfather'), 'run', '--', sys.executable, '-c',
                                  'import os;print(open(os.environ["SLOTFATHER_LEASE"]).read())'],
                                 env=env, text=True, capture_output=True, timeout=10)
            self.assertEqual(0, run.returncode, run.stderr)
            self.assertNotIn('ticket', json.loads(run.stdout))

    def test_old_cli_and_metadata_inputs_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env = {key: value for key, value in os.environ.items()
                   if not key.startswith(('SLOTFATHER', 'STRAYLIGHT_SLOT'))}
            env.update(SLOTFATHER_DIR=tmp, SLOTFATHER_LEGACY_DIRS='')
            for args in (['--class', 'batch'], ['--meta', 'class=batch'],
                         ['--meta', 'priority=batch'], ['--meta', 'ticket=42']):
                run = subprocess.run([str(ROOT / 'slotfather'), 'run', *args, '--', 'true'],
                                     env=env, text=True, capture_output=True, timeout=5)
                self.assertEqual(2, run.returncode, str(args))
            self.assertFalse((Path(tmp) / 'history.jsonl').exists())

    def test_stats_group_by_request_ticket_and_priority(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = dict(v=2, label='job', cmd='true', count=1, queued='2026-11-05T10:00:00Z',
                        granted='2026-11-05T10:00:00Z', ended='2026-11-05T10:01:00Z',
                        outcome='released', waited=0, ran=60, status=0)
            rows = [{**base, 'priority': 'interactive', 'ticket': '42'},
                    {**base, 'priority': 'normal'}, {**base, 'priority': 'batch', 'ticket': '42'}]
            (Path(tmp) / 'history.jsonl').write_text(''.join(json.dumps(row) + '\n' for row in rows))
            env = {key: value for key, value in os.environ.items() if not key.startswith('SLOTFATHER')}
            env.update(SLOTFATHER_DIR=tmp, SLOTFATHER_LEGACY_DIRS='')
            for field, expected in [('ticket', {'42': 2, None: 1}),
                                    ('priority', {'interactive': 1, 'normal': 1, 'batch': 1})]:
                run = subprocess.run([str(ROOT / 'slotfather'), 'stats', '--json', '--by', field],
                                     env=env, text=True, capture_output=True, timeout=5)
                self.assertEqual(0, run.returncode, run.stderr)
                self.assertEqual(expected, {row[field]: row['requests'] for row in json.loads(run.stdout)['groups']})

    def test_scheduler_reads_only_request_priority(self) -> None:
        self.assertEqual('interactive', config.priority_of({'priority': 'interactive'}))
        self.assertEqual('normal', config.priority_of({'meta': {'class': 'batch'}}))
        self.assertEqual('normal', config.priority_of({}))


if __name__ == '__main__':
    unittest.main()
