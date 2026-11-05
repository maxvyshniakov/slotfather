#!/usr/bin/env python3
"""Choose a varied, unmodified short interval from completed local queue history."""
import datetime as dt
import hashlib
import json
from pathlib import Path

root=Path(__file__).resolve().parents[1]
scene=json.loads((root/'tools/fixtures/navigation.json').read_text())['model']
source=Path.home()/'.local/state/slotfather/history.jsonl'
rows=[json.loads(line) for line in source.read_text().splitlines()]
def stamp(value):return dt.datetime.fromisoformat(value.replace('Z','+00:00')).timestamp()
events=[]
for row in rows:
    if row.get('granted') and row.get('ended') and row.get('capacity')==scene['slots']:
        events.extend([(stamp(row['granted']),row['count']),(stamp(row['ended']),-row['count'])])
if not events:raise SystemExit('No completed queue history')
events.sort()
steps=[]
level=0
previous=events[0][0]
for when,delta in events:
    if when>previous:steps.append((previous,when,level))
    level+=delta
    previous=when
best=None
for low,high,level in steps:
    if level!=scene['used'] or high-low<12:continue
    end=low+4
    values=[max((n for a,b,n in steps if a<end-56+i+1 and b>end-56+i),default=0) for i in range(56)]
    if max(values)>scene['slots']:continue
    changes=sum(a!=b for a,b in zip(values,values[1:]))
    spread=max(values)-min(values)
    score=(changes+spread*2,changes,spread)
    if best is None or score>best[0]:best=(score,end)
if best is None:raise SystemExit('No historical interval matching the scene occupancy')
_,end=best
result=dict(provenance=dict(source='Completed Slotfather history.jsonl grant/end events',
    sourceSha256=hashlib.sha256(source.read_bytes()).hexdigest(),
    sourceEnd=dt.datetime.fromtimestamp(end,dt.timezone.utc).isoformat(),
    note='A recorded short interval is time-aligned with the illustrative demo scene. Heights and event spacing are preserved.'),
    steps=[dict(start=round(a-end,6),end=round(min(b,end)-end,6),held=n)
           for a,b,n in steps if a<end and b>end-180])
path=root/'tools/fixtures/chart-history.json'
path.write_text(json.dumps(result,indent=2)+'\n')
print(path)
