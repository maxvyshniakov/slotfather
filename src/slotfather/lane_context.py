"""Optional read-only lanefather summaries; no dependency on its storage schema."""
from __future__ import annotations

import json
from pathlib import Path
import re
import shutil
import subprocess
import time
from typing import Any, cast

CACHE_SECONDS = 5.0
CACHE_LIMIT = 128
_CACHE: dict[tuple[str, str, str], tuple[float, dict[str, Any]]] = {}


def fetch(context: dict[str, str], cwd: str = "") -> dict[str, Any]:
    lane, directory = context.get('lane', ''), context.get('lanefather.state', '')
    location = directory or cwd
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,63}', lane) or not Path(location).is_absolute():
        return {'error': 'lane reference missing'}
    command = shutil.which('lanefather') or ''
    # The command location is not part of identity: an already cached response survives a transient PATH change.
    key = (location, lane, 'state-v1' if directory else 'project-v1')
    cached = _CACHE.get(key)
    if cached and time.monotonic() - cached[0] < CACHE_SECONDS:
        return cached[1]
    result: dict[str, Any]
    if not command:
        result = {'error': 'lanefather not installed'}
    else:
        try:
            reference = ['--state-dir', directory] if directory else ['--project-dir', cwd]
            reply = subprocess.run([command, 'show', lane, *reference, '--json'],
                                   capture_output=True, text=True, timeout=1.5)
            if reply.returncode or len(reply.stdout) > 65536:
                result = {'error': 'lanefather query failed'}
            else:
                raw = json.loads(reply.stdout)
                data = cast(dict[str, Any], raw) if isinstance(raw, dict) else {}
                if data.get('version') != 1 or data.get('id') != lane:
                    result = {'error': 'unsupported lane summary'}
                elif (any(not isinstance(data.get(key, ''), (str, type(None)))
                          for key in ('project', 'status', 'branch', 'base'))
                      or any(not isinstance(data.get(key, {}), dict) for key in ('ticket', 'checks', 'review'))
                      or any(not isinstance(data.get(key, []), list) for key in ('claims', 'after'))):
                    result = {'error': 'invalid lane summary'}
                else:
                    result = data
        except (OSError, ValueError, subprocess.TimeoutExpired):
            result = {'error': 'lanefather unavailable'}
    if len(_CACHE) >= CACHE_LIMIT:
        _CACHE.pop(next(iter(_CACHE)))
    _CACHE[key] = (time.monotonic(), result)
    return result
