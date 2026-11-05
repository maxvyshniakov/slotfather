#!/usr/bin/env python3
"""Refresh frozen lane context from saved records, projected to the queue scene's time."""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
from pathlib import Path
import subprocess
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


def timestamp(value: str | None) -> dt.datetime | None:
    return dt.datetime.fromisoformat(value.replace('Z', '+00:00')) if value else None


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state-dir', type=Path, default=Path.home() / '.local/state/straylight-orchestration/lanes')
    parser.add_argument('--project-dir', type=Path, default=Path.home() / 'straylight')
    args = parser.parse_args()
    fixture = ROOT / 'tools/fixtures/navigation.json'
    data: dict[str, Any] = json.loads(fixture.read_text())
    moment = timestamp(data['model']['updated'])
    assert moment is not None
    origin = subprocess.check_output(['git', '-C', str(args.project_dir), 'remote', 'get-url', 'origin'], text=True).strip()
    repository = origin.removesuffix('.git').replace('git@github.com:', 'https://github.com/')
    if not repository.startswith('https://github.com/'):
        raise SystemExit('Demo ticket links require a GitHub project origin')
    sources: dict[str, str] = {}

    def read(name: str) -> dict[str, Any]:
        path = args.state_dir / name
        if not path.is_file():
            return {}
        raw = path.read_bytes()
        sources[name] = hashlib.sha256(raw).hexdigest()
        return json.loads(raw)

    for item in data['model']['holders'] + data['model']['waiters']:
        # This is a finite conversion of the archived scene, not a runtime API alias.
        tags = dict(pair.split('=', 1) for pair in item.get('meta', '').split(',') if '=' in pair)
        item['priority'] = item.get('priority', tags.pop('class', 'normal'))
        ticket = item.get('ticket') or tags.pop('ticket', None)
        tags.pop('class', None)
        tags.pop('ticket', None)
        if ticket:
            item['ticket'] = str(ticket)
        item['context'] = tags
        item['meta'] = ','.join(f'{key}={value}' for key, value in tags.items())
        lane_id = tags.get('lane')
        if not lane_id:
            continue
        record = read(lane_id + '.json')
        if not record:
            item['lane'] = {'error': 'historical lane record unavailable'}
            continue
        landed: dict[str, Any] = record.get('landed') or {}
        stage = 'planned'
        for value, when in [('active', record.get('adopted')),
                            ('landed', landed.get('at')),
                            ('done', record.get('done'))]:
            at = timestamp(when)
            if at and at <= moment:
                stage = value
        receipt = read(lane_id + '.receipt.json')
        started, finished = timestamp(receipt.get('started')), timestamp(receipt.get('finished'))
        receipt_status = 'unavailable'
        if finished and finished <= moment:
            receipt_status = receipt.get('status', 'unknown')
        elif started and started <= moment:
            receipt_status = 'in progress'
        review = read(lane_id + '.review.json')
        review_started, review_finished = timestamp(review.get('started')), timestamp(review.get('finished'))
        review_status = 'not started' if stage in ('planned', 'active') else 'unavailable'
        if review_finished and review_finished <= moment:
            review_status = review.get('status', 'unknown')
        elif review_started and review_started <= moment:
            review_status = 'in progress'
        # A completed lane may have removed its worktree. The recorded request
        # command still names the actual historical verify.sh location.
        worktree = record.get('worktree')
        if not worktree and '/verify.sh' in item['cmd']:
            worktree = item['cmd'].split('/verify.sh', 1)[0]
        worktree = str(worktree or '').replace(str(Path.home()), '~')
        number = str(ticket or record.get('ticket') or '')
        item['lane'] = dict(version=1, id=lane_id, project='Straylight', repository=repository,
                            ticket=dict(number=number, url=f'{repository}/issues/{number}' if number.isdigit() else None),
                            status=stage, branch=record.get('branch'), base=record.get('base'), worktree=worktree,
                            claims=record.get('claims', []), after=record.get('after', []),
                            checks=dict(status=receipt_status, current=None),
                            review=dict(status=review_status, findings=0, unresolved=0))
    data['provenance']['laneContext'] = dict(
        source='Saved lanefather records, projected to the queue scene timestamp',
        records=sources,
        limitations='Later receipt/review outcomes are excluded. Historical receipt tree currentness and process details are unavailable. Worktree paths are shortened for public media.')
    fixture.write_text(json.dumps(data, indent=2) + '\n')
    print(f'Refreshed frozen lane context from {len(sources)} saved records')


if __name__ == '__main__':
    main()
