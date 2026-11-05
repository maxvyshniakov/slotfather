"""The navigation scene advances recorded observations without inventing queue events."""
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch


def load_demo():
    path=Path(__file__).resolve().parents[1]/'tools/dashboard-demo.py'
    spec=importlib.util.spec_from_file_location('dashboard_demo_fixture',path)
    demo=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(demo)
    return demo


class NavigationDemoTests(unittest.TestCase):
    def test_clock_and_recorded_samples_advance_while_queue_members_stay_fixed(self):
        demo=load_demo()
        with patch.object(demo.time,'monotonic',return_value=100) as clock:
            dashboard,initial=demo.load_scene(advance=True)
            first=dashboard.dashboard_model()
            clock.return_value=105
            later=dashboard.dashboard_model()
        self.assertEqual(5,(later['updated']-first['updated']).total_seconds())
        for key in ('holders','waiters'):
            self.assertEqual([(x['key'],x['count']) for x in first[key]],
                             [(x['key'],x['count']) for x in later[key]])
        self.assertEqual(first['events'],later['events'])
        self.assertNotEqual([h['cpu'] for h in first['holders']],
                            [h['cpu'] for h in later['holders']])
        self.assertNotEqual(dashboard.history_buckets(first['history'],56),
                            dashboard.history_buckets(later['history'],56))

    def test_recorded_chart_has_peaks_and_fractional_vertical_cells(self):
        demo=load_demo()
        with patch.object(demo.time,'monotonic',return_value=100):
            dashboard,model=demo.load_scene(advance=True)
        canvas=dashboard.Canvas(156,43)
        dashboard.dashboard_draw(canvas,model,cwd='~/straylight')
        text=canvas.render(False)
        self.assertTrue(any(glyph in text for glyph in '▁▂▃▄▅▆▇'))
        values=dashboard.history_buckets(model['history'],56)
        self.assertGreaterEqual(sum(a!=b for a,b in zip(values,values[1:])),5)
        self.assertGreaterEqual(max(values)-min(values),3)
