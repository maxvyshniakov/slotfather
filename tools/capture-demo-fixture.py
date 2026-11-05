#!/usr/bin/env python3
"""Build a frozen navigation fixture from completed local queue history (no queue mutations)."""
import datetime as dt
import hashlib
import json
from pathlib import Path
import re

state=Path.home()/'.local/state/slotfather'
source=state/'history.jsonl'
rows=[json.loads(line) for line in source.read_text().splitlines()]

def stamp(value):
    return dt.datetime.fromisoformat(value.replace('Z','+00:00')).timestamp() if value else None

rows=[r for r in rows if r.get('granted') and r.get('ended') and r.get('capacity')==6]
for r in rows:
    r['_queued'],r['_granted'],r['_ended']=[stamp(r[k]) for k in ('queued','granted','ended')]
choices=[]
for r in rows:
    if r['waited']<10: continue
    t=r['_queued']+min(10,r['waited']/2)
    held=[h for h in rows if h['_granted']<=t<h['_ended']]
    waiting=[h for h in rows if h['_queued']<=t<h['_granted']]
    if 2<=len(held)<=5 and waiting and sum(h['count'] for h in held)<=6:
        score=(-abs(len(held)-3)-abs(len(waiting)-2),len(set(h['label'] for h in held+waiting)),t)
        choices.append((score,t,held,waiting))
if not choices: raise SystemExit('No completed historical interval with multiple holders and a waiter')
_,t,held,waiting=max(choices)
held.sort(key=lambda h:h['_granted']);waiting.sort(key=lambda h:h['_queued'])
machines=[json.loads(line) for line in (state/'machine.jsonl').read_text().splitlines()]
machine=min(machines,key=lambda m:abs(m['at']-t))

def iso(t):return dt.datetime.fromtimestamp(t,dt.timezone.utc).isoformat()
def clean(text):
    text=str(text or '').replace(str(Path.home()),'~')
    if re.search(r'(?:token|password|secret|authorization|api[_-]?key)',text,re.I):
        return '[redacted command]'
    return text

def item(r,index,waiting=False):
    return dict(label=clean(r['label']),count=r['count'],pid=10000+index,
      since=iso(r['_queued']),granted=None if waiting else iso(r['_granted']),
      cmd=clean(r['cmd']),meta=','.join(f'{k}={clean(v)}' for k,v in r.get('meta',{}).items() if k in ('lane','ticket','phase','class')),
      cpu=None if waiting else round(r.get('cpu',0)/max(1,r.get('wall',r['ran']))*100,1),
      rss=0 if waiting else round(r.get('rss_mean_mb',0)*1024),procs=0 if waiting else r.get('processes',0),
      tree=[],queue='',kind='waiting' if waiting else 'held',passable=False,asked='',yieldable=False,yields=0)

all_items=held+waiting
items=[item(r,i,i>=len(held)) for i,r in enumerate(all_items)]
events=[]
for i,r in enumerate(rows):
 for key,verb,delta in [('_granted','granted',r['count']),('_ended',r.get('outcome','released'),-r['count'])]:
  if t-7200<=r[key]<=t and verb in ('granted','released','reaped'):
   events.append(dict(when=iso(r[key]),verb=verb,count=r['count'],delta=delta,label=clean(r['label']),pid=10000+all_items.index(r) if r in all_items else 20000+i))
events.sort(key=lambda e:e['when'])
used=sum(h['count'] for h in held)
fixture=dict(provenance=dict(source='Completed local Slotfather history and nearest machine sample',
    historySha256=hashlib.sha256(source.read_bytes()).hexdigest(),capturedAt=iso(t),machineAt=iso(machine['at']),
    redactions='Home paths shortened, PIDs consistently remapped, secret-bearing commands omitted.',
    limitations='CPU and memory per lane are recorded run averages, not instantaneous samples. Historical process names and physical memory usage are unavailable; omitted.'),
  model=dict(directory='(frozen demo)',slots=6,saved=True,cores=12,averages=machine['load'],
    advice=dict(unleased_load=max(0,min(machine['load'][:2])-used*2)),holders=items[:len(held)],waiters=items[len(held):],
    used=used,free=6-used,guard=None,admission=0,order='backfill',start=None,limit=2700,
    memory=dict(level=machine['pressure'],total=None,used=None,swap_total=machine['swap_total_mb'],swap_free=machine['swap_free_mb']),
    events=events,updated=iso(t)))
baseline=max((m for m in machines if m['at']<=t),key=lambda m:m['at'])
fixture['machine_samples']=[dict(at=iso(m['at']),averages=m['load'],
    memory=dict(level=m['pressure'],swap_total=m['swap_total_mb'],swap_free=m['swap_free_mb']))
    for m in [baseline]+[m for m in machines if t<m['at']<=t+30]]
fixture['provenance']['machineSha256']=hashlib.sha256((state/'machine.jsonl').read_bytes()).hexdigest()
Path('tools/fixtures/navigation.json').write_text(json.dumps(fixture,indent=2)+'\n')
print(json.dumps({'held':[(i['label'],i['count']) for i in items[:len(held)]],'waiting':[(i['label'],i['count']) for i in items[len(held):]],'at':iso(t)}))
