#!/usr/bin/env python3
"""Record real local validation workload metrics for the illustrative navigation demo.

Run through slotfather with three slots. The queue itself is never read or changed.
"""

import sys as _path_sys
from pathlib import Path as _Path
_path_sys.path.insert(0, str(_Path(__file__).resolve().parents[1] / "src"))
from slotfather import platform as sf_platform
import importlib.machinery
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import signal
import sys
import tempfile
import time

ROOT=Path(__file__).resolve().parents[1]
from slotfather import dashboard as dashboard
worker='''import subprocess,sys,time,resource,json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
commands=__import__('json').loads(sys.argv[1])
destination=Path(sys.argv[2])
index=int(destination.stem)
end=time.monotonic()+18
iteration=0
while time.monotonic()<end:
 with ThreadPoolExecutor(max_workers=2 if index==2 else 1) as pool:
  def check(command):return subprocess.run(command,check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
  for result in pool.map(check,commands*(6+3*index)):
   usage=resource.getrusage(resource.RUSAGE_CHILDREN)
   own=resource.getrusage(resource.RUSAGE_SELF)
   temporary=destination.with_suffix('.tmp')
   temporary.write_text(json.dumps(dict(at=time.monotonic(),cpu=usage.ru_utime+usage.ru_stime+own.ru_utime+own.ru_stime)))
   temporary.replace(destination)
 iteration+=1
 time.sleep([.02,.3,.07,.6,.12][(iteration+index*2)%5])
'''
commands=[
 [[sys.executable,'-m','unittest','discover','-s','tests','-p','test_dashboard.py','-k','HistoryTests']],
 [[sys.executable,'-m','unittest','discover','-s','tests','-p','test_dashboard.py','-k','SlotBarTests']],
 [['node','--check','../screen-kit/src/terminal.mjs'],['node','--check','../screen-kit/src/browser.mjs']],
]
children=[]
samples=[]
temporary=tempfile.TemporaryDirectory(prefix='slotfather-demo-metrics-')
usage_files=[Path(temporary.name)/f'{index}.json' for index in range(3)]
previous={}
def remaining(group):
 rows=subprocess.check_output(['ps','-axo','pid=,pgid=,uid=,stat='],text=True)
 return [fields for line in rows.splitlines() if len(fields:=line.split())==4
         and int(fields[1])==group and 'Z' not in fields[3]]
try:
 for command,path in zip(commands,usage_files):
  children.append(subprocess.Popen([sys.executable,'-c',worker,json.dumps(command),str(path)],cwd=ROOT,start_new_session=True))
 for second in range(16):
  if second:time.sleep(1)
  own,descendants=dashboard.process_usage()
  requests=[]
  for index,child in enumerate(children):
   if child.poll() is not None:
    raise RuntimeError(f'Validation workload {index} exited before recording finished: {child.returncode}')
   tree=dashboard.process_tree(child.pid,own,descendants)
   usage=json.loads(usage_files[index].read_text()) if usage_files[index].exists() else dict(at=time.monotonic(),cpu=0)
   before=previous.get(index,usage)
   cpu=max(0,usage['cpu']-before['cpu'])/max(.001,usage['at']-before['at'])*100
   previous[index]=usage
   requests.append(dict(cpu=round(cpu,1),rss=sum(p[2] for p in tree),
                     procs=len(tree),tree=[[10000+i,p[1],p[2],Path(p[3]).name] for i,p in enumerate(tree)]))
  machine=dashboard.memory_facts()
  samples.append(dict(offset=second,lanes=requests,averages=list(sf_platform.loads()),memory=machine))
finally:
 for child in children:
  if child.poll() is None:
   try:os.killpg(child.pid,signal.SIGTERM)
   except ProcessLookupError:pass
 for child in children:
  try:child.wait(timeout=3)
  except subprocess.TimeoutExpired:child.kill();child.wait()
  members=remaining(child.pid)
  if members:
   if any(int(member[2])!=os.getuid() for member in members):
    raise RuntimeError('Unexpected owner in validation process group')
   try:os.killpg(child.pid,signal.SIGKILL)
   except ProcessLookupError:pass
 temporary.cleanup()
result=dict(provenance=dict(source='Real CPU/RSS/process and host samples from local validation checks',
 capturedAt=__import__('datetime').datetime.now(__import__('datetime').timezone.utc).isoformat(),
 commands=[[argument.replace(str(Path.home()),'~') for argument in command]
           for group in commands for command in group],
 limitations='Illustrative workload profiles are replayed over the separate historical queue scene; they are not measurements of those historical request processes.'),samples=samples)
path=ROOT/'tools/fixtures/metrics.json'
path.write_text(json.dumps(result,indent=2)+'\n')
print(path)
