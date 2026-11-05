"""The details panel scrolls as one document and retains terminal links."""
from __future__ import annotations

import datetime
import importlib
import json
import os
import tempfile
from unittest import mock
from pathlib import Path
import sys
from typing import Any
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
view = importlib.import_module('slotfather.dashboard')


def example() -> tuple[dict[str, Any], datetime.datetime]:
    now = datetime.datetime.now(datetime.timezone.utc)
    item: dict[str, Any] = dict(key=(123, 'now'), label='compiler', kind='held', count=1, pid=123,
        style='h0', cpu=20, rss=1024, procs=20, granted=now, since=now, queue='', cmd='build', meta='',
        tree=[(1000 + i, 0, 1024, f'worker-{i:02}') for i in range(20)])
    return dict(holders=[item], waiters=[], cores=12, slots=6), now


class DetailsTests(unittest.TestCase):
    def test_wheel_anywhere_in_details_scrolls_the_whole_document(self) -> None:
        model, now = example()
        scroll: dict[str, int] = {}
        def draw() -> Any:
            canvas = view.Canvas(80, 12)
            canvas.scroll = scroll
            view.draw_detail(canvas, 0, 0, 12, 80, model, 0, '/project', now)
            return canvas
        canvas = draw()
        self.assertIn('compiler', canvas.render(False))
        self.assertNotIn('processes', canvas.tables)
        view.mouse_action(canvas, ('mouse', 65, 25, 3, True), scroll, 0)
        canvas = draw()
        self.assertNotIn('compiler', canvas.render(False))
        self.assertIn('above', canvas.render(False))
        for _ in range(20):
            view.mouse_action(canvas, ('mouse', 65, 25, 3, True), scroll, 0)
            canvas = draw()
        self.assertIn('worker-19', canvas.render(False))
        self.assertNotIn('more', canvas.render(False))
        self.assertIn('REQUEST DETAILS', canvas.render(False).splitlines()[0])
        view.mouse_action(canvas, ('mouse', 64, 25, 3, True), scroll, 0)
        self.assertLess(scroll['details'], canvas.tables['details']['offset'])

    def test_lane_context_has_a_scrolling_ticket_link_and_no_escape_in_plain_text(self) -> None:
        model, now = example()
        model['holders'][0]['lane'] = dict(id='architecture', project='project', status='active',
            branch='lane/architecture', base='dev', claims=['src/**'], after=[],
            ticket=dict(number='42', url='https://github.com/example/project/issues/42'),
            checks=dict(status='green', current=True), review=dict(status='not started', findings=0, unresolved=0))
        model['holders'][0]['context'] = dict(lane='architecture', phase='check')
        canvas = view.Canvas(100, 80)
        view.draw_detail(canvas, 0, 0, 80, 100, model, 0, '/project', now)
        plain = canvas.render(False)
        self.assertNotIn('LANE', plain)
        self.assertRegex(plain, r'lane\s+architecture')
        self.assertRegex(plain, r'phase\s+check')
        self.assertIn('lane/architecture', plain)
        self.assertNotIn('\x1b', plain)
        self.assertIn('\x1b]8;;https://github.com/example/project/issues/42\x1b\\', canvas.render(True))
        self.assertIn('#42\x1b]8;;\x1b\\', canvas.render(True))
        canvas.scroll = {'details': 20}
        short = view.Canvas(100, 12)
        short.scroll = canvas.scroll
        view.draw_detail(short, 0, 0, 12, 100, model, 0, '/project', now)
        self.assertNotIn('https://github.com', short.render(True), 'off-screen links must not leak onto other rows')

    def test_context_query_uses_explicit_identity_and_caches_the_read_only_response(self) -> None:
        model, _ = example()
        item = model['holders'][0]
        item['context'] = {'lane': 'compiler', 'lanefather.state': '/project/state'}
        with tempfile.TemporaryDirectory() as tmp:
            command = Path(tmp) / 'lanefather'
            calls = Path(tmp) / 'calls'
            command.write_text('#!' + sys.executable + '\nimport json,sys\n'
                + 'from pathlib import Path\n'
                + 'Path(' + repr(str(calls)) + ').write_text(json.dumps(sys.argv[1:]))\n'
                + 'print(json.dumps({"version":1,"id":"compiler","project":"source-project","status":"active"}))\n')
            command.chmod(0o755)
            with mock.patch.dict(os.environ, {'PATH': tmp + os.pathsep + os.environ['PATH']}):
                view.enrich_details(item)
                self.assertEqual('source-project', item['lane']['project'])
                self.assertEqual(['show', 'compiler', '--state-dir', '/project/state', '--json'], json.loads(calls.read_text()))
                command.unlink()
                view.enrich_details(item)
                self.assertEqual('source-project', item['lane']['project'], 'unchanged identity should reuse the cached response')
        item['context'] = {'lane': 'compiler', 'lanefather.state': '/another-project/state'}
        with mock.patch.dict(os.environ, {'PATH': '/does-not-exist'}):
            view.enrich_details(item)
        self.assertIn('error', item['lane'], 'another project must not inherit the cached lane')

    def test_ticket_urls_cannot_inject_terminal_control_sequences(self) -> None:
        canvas = view.Canvas(40, 3)
        canvas.hyperlink(0, 0, '#42', 'https://github.com/project/issues/42\x1b]52;clipboard', 20)
        self.assertNotIn('\x1b]8;', canvas.render(True))
        self.assertNotIn('clipboard', canvas.render(True))
        canvas.hyperlink(1, 0, '#42', 'javascript:alert(1)', 20)
        self.assertNotIn('javascript:', canvas.render(True))

    def test_worktree_is_a_muted_folder_link_before_command(self) -> None:
        model, now = example()
        folder = '/tmp/Project #1/' + 'long-directory-' * 4 + '/agent worktree'
        model['holders'][0]['lane'] = dict(id='compiler', worktree=folder)
        model['holders'][0]['context'] = dict(lane='compiler')
        canvas = view.Canvas(64, 80)
        view.draw_detail(canvas, 0, 0, 80, 64, model, 0, '/project', now)
        plain = canvas.render(False)
        self.assertLess(plain.index('worktree'), plain.index('command'))
        url = Path(folder).as_uri()
        self.assertIn('%20', url)
        self.assertIn('%23', url)
        self.assertIn('\x1b]8;;' + url + '\x1b\\', canvas.render(True))
        self.assertNotIn('file://', plain)
        linked = [(row, column) for (row, column), target in canvas.links.items() if target == url]
        self.assertGreater(len({row for row, _ in linked}), 1, 'wrapped path lines remain linked')
        self.assertTrue(all(canvas.cells[row][column][1] == 'dim' for row, column in linked))
        self.assertFalse(view.safe_link('file://another-host/private/path'))

    def test_known_context_is_structured_and_only_custom_fields_remain_in_meta(self) -> None:
        model, now = example()
        item = model['holders'][0]
        item['priority'] = 'interactive'
        item['ticket'] = '99'
        item['context'] = dict(lane='compiler', ticket='42', phase='check', kind='verify',
            session='session-a', check='syntax', sha='abc123', on='base', model='test-model',
            **{'class': 'batch', 'lanefather.state': '/private/state'})
        item['meta'] = 'class=batch,kind=verify,model=test-model'
        item['lane'] = dict(id='compiler', status='active', project='project', repository='https://github.com/example/project',
            ticket=dict(number='42', url='https://github.com/example/project/issues/42'),
            checks=dict(status='green', current=True), review=dict(status='not started'))
        canvas = view.Canvas(110, 80)
        view.draw_detail(canvas, 0, 0, 80, 110, model, 0, '/project', now)
        text = canvas.render(False)
        for label, value in [('priority', 'interactive'), ('ticket', '#99'), ('phase', 'check'),
                             ('kind', 'verify'), ('revision', 'abc123'), ('run on', 'base'),
                             ('session', 'session-a'), ('stage', 'active'), ('receipt', 'green · current')]:
            self.assertRegex(text, label + r'\s+' + value)
        meta = next(line for line in text.splitlines() if 'meta ' in line)
        self.assertIn('model=test-model', meta)
        self.assertNotIn('class=', meta)
        self.assertNotIn('kind=', meta)
        self.assertNotIn('/private/state', text)
        self.assertIn('https://github.com/example/project/issues/99', canvas.render(True))
        self.assertNotIn('https://github.com/example/project/issues/42', canvas.render(True))


if __name__ == '__main__':
    unittest.main()
