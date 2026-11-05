#!/usr/bin/env python3
"""Historical queue with recorded validation metrics in the real interactive dashboard.

Capture/export belongs to screen-kit; this file only supplies product-owned data.
"""

import sys as _path_sys
from pathlib import Path as _Path
_path_sys.path.insert(0, str(_Path(__file__).resolve().parents[1] / "src"))
import argparse
import copy
import datetime as dt
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import time
import types
from typing import Any

ROOT=Path(__file__).resolve().parents[1]
FIXTURE=ROOT/'tools/fixtures/navigation.json'
METRICS=ROOT/'tools/fixtures/metrics.json'
CHART_HISTORY=ROOT/'tools/fixtures/chart-history.json'


def load_scene(advance=False):
    data=json.loads(FIXTURE.read_text())
    profiles=json.loads(METRICS.read_text())['samples']
    chart=json.loads(CHART_HISTORY.read_text())['steps']
    model=data['model']
    for item in model['holders']+model['waiters']:
        for key in ('since','granted'):
            item[key]=dt.datetime.fromisoformat(item[key]) if item[key] else None
        item['key']=(item['pid'],item['since'].isoformat())
        item['style']='warn' if item['kind']=='waiting' else 'accent'
    for event in model['events']:
        event['when']=dt.datetime.fromisoformat(event['when'])
    model['updated']=dt.datetime.fromisoformat(model['updated'])
    import importlib
    dashboard: Any=importlib.reload(importlib.import_module('slotfather.dashboard'))
    dashboard.HISTORY_WINDOW=60  # short demo zoom; the legend reports seconds per column
    origin=model['updated']
    started=time.monotonic()
    history_anchor=origin+dt.timedelta(seconds=10 if advance else 0)
    def current():
        return origin+dt.timedelta(seconds=10+time.monotonic()-started) if advance else origin

    class Clock(dt.datetime):
        @classmethod
        def now(cls,tz=None):
            moment=current()
            return moment.astimezone(tz) if tz else moment.astimezone().replace(tzinfo=None)

    class Date(dt.date):
        @classmethod
        def today(cls):return current().astimezone().date()

    dashboard.datetime=types.SimpleNamespace(datetime=Clock,date=Date,timedelta=dt.timedelta,timezone=dt.timezone)
    model['history']=dashboard.held_history(model['events'],model['used'],origin)
    dashboard.dashboard_colours(model,{})
    def sample():
        fresh=copy.deepcopy(model)
        fresh['updated']=current()
        elapsed=max(0,time.monotonic()-started) if advance else 0
        for observation in profiles:
            if observation['offset']>elapsed+2:  # skip process-startup warmup
                break
            fresh['averages']=observation['averages']
            fresh['memory'].update(observation['memory'])
            for item,usage in zip(fresh['holders'],observation['lanes']):
                for key in ('cpu','rss','procs'):
                    item[key]=usage[key]
        fresh['history']=[(history_anchor+dt.timedelta(seconds=step['start']),
                           history_anchor+dt.timedelta(seconds=step['end']),step['held'])
                          for step in chart]
        fresh['history'].append((history_anchor,fresh['updated'],fresh['used']))
        fresh['advice']['unleased_load']=max(0,min(fresh['averages'][:2])-fresh['used']*2)
        return fresh
    def frozen_details(_item: dict[str, Any]) -> None:
        pass  # frozen lane summaries; never query live lanefather
    dashboard.enrich_details=frozen_details
    dashboard.dashboard_model=sample
    dashboard.process_cwd=lambda pid:'~/straylight' if pid in [h['pid'] for h in model['holders']] else None
    return dashboard,sample()


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    export=parser.add_mutually_exclusive_group()
    export.add_argument('--screenshot',action='store_true')
    export.add_argument('--animation',action='store_true')
    parser.add_argument('--once',action='store_true')
    parser.add_argument('--width',type=int,default=156)
    parser.add_argument('--height',type=int,default=43)
    args=parser.parse_args()
    if args.screenshot or args.animation:
        tool=shutil.which('screen-kit')
        if not tool:parser.error('Install the screen-kit CLI first (bun link in its checkout).')
        name='dashboard-navigation' if args.animation else 'dashboard'
        destination=ROOT/'assets/screens'/name
        result=subprocess.run([tool,'capture','--config',str(ROOT/'tools/screens.mjs'),'--only',name,
                        '--out',str(destination)],cwd=ROOT,text=True,capture_output=True)
        if result.returncode:
            raise SystemExit(result.stderr)
        extension='webp' if args.animation else 'png'
        shutil.copyfile(destination/f'{name}.{extension}',ROOT/f'assets/dashboard.{extension}')
        print(ROOT/f'assets/dashboard.{extension}')
        return
    os.environ['TZ']='Etc/GMT-2'
    time.tzset()
    dashboard,model=load_scene(advance=not args.once)
    if args.once:
        canvas=dashboard.Canvas(args.width,args.height)
        dashboard.dashboard_draw(canvas,model,cwd='~/straylight')
        print(canvas.render(True))
    else:
        dashboard.dashboard_live(1)


if __name__=='__main__':main()
