"""Slotfather state responsibilities."""
from __future__ import annotations

import contextlib
import fcntl
import json
import math
import os
from pathlib import Path
import pwd
import stat
import time
from typing import Any, cast

from . import config as _config


def foreign_owner(directory: Path) -> int | None:
    """The uid that owns the queue when it is not this process's (effective) one, else None. A queue not made yet
    belongs to the owner of the directory it would be made in, unless that is a sticky shared one such as /tmp.
    Another user's queue is read only (#653 review: root through the guest agent running the installed command
    must not leave root's files in the runner's queue)."""
    path = Path(directory)
    while not os.path.lexists(path) and path != path.parent:
        path = path.parent
    try:
        info = path.stat()
    except OSError:
        return None
    if info.st_uid == os.geteuid() or (path != Path(directory) and info.st_mode & stat.S_ISVTX):
        return None
    return info.st_uid


def require_own(directory: Path, what: str) -> None:
    """Refuses `what`, a change to the queue, from a user other than the queue's owner."""
    owner = foreign_owner(directory)
    if owner is None:
        return
    try:
        name = pwd.getpwuid(owner).pw_name
    except KeyError:
        name = str(owner)
    raise _config.SlotError(f"the queue {directory} belongs to uid {owner} ({name}): {what} by uid {os.geteuid()} would leave "
                    f"files that user cannot write; run it as that user: sudo -u {name} slotfather ...")


SCHEMA_VERSION = 1


def schema_version(directory: Path) -> int:
    """Unstamped queues are v1. Reject unknown formats before any filesystem mutation."""
    path = directory / "schema.json"
    try:
        value = json.loads(path.read_text())["v"]
    except FileNotFoundError:
        return SCHEMA_VERSION
    except (OSError, ValueError, TypeError, KeyError) as error:
        raise _config.SlotError(f"cannot read state schema at {path}: {error}") from None
    if type(value) is not int or value != SCHEMA_VERSION:
        raise _config.SlotError(f"unsupported state schema {value!r} at {path}; update slotfather")
    return value


@contextlib.contextmanager
def locked(directory: Path):
    """The queue's lock. In another user's queue (foreign_owner()) nothing is created: an existing lock is taken
    through a read-only handle, a missing queue is read as it is."""
    schema_version(directory)
    if foreign_owner(directory) is not None:
        try:
            handle = open(directory / ".lock", "r")
        except OSError:
            yield
            return
        with handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            yield
        return
    (directory / "queue").mkdir(parents=True, exist_ok=True)
    with open(directory / ".lock", "a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        schema_version(directory)
        if not (directory / "schema.json").exists():
            write(directory / "schema.json", {"v": SCHEMA_VERSION})
        yield


def entries(directory: Path) -> list[tuple[Path, dict[str, Any]]]:
    found: list[tuple[Path, dict[str, Any]]] = []
    for path in sorted((directory / "queue").glob("*.json")):
        try:
            entry = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if isinstance(entry, dict) and isinstance(entry.get("count"), int):
            found.append((path, cast(dict[str, Any], entry)))
    return found


def write(path: Path, entry: dict) -> None:
    pending = path.with_name(path.name + ".pending")
    pending.write_text(json.dumps(entry) + "\n")
    pending.replace(path)


def record(directory: Path, entry: dict, outcome: str, status: int | None = None, usage: dict | None = None) -> None:
    """Add a request that left the queue to history.jsonl: released (it ran), left (it gave up waiting) or reaped
    (its holder died), with what its process tree used where the holder measured it. Callers hold the queue lock."""
    ended = time.time()
    queued, granted = _config.seconds(entry.get("since")), _config.seconds(entry.get("granted"))
    item = {"v": _config.HISTORY_VERSION, "label": entry.get("label"), "cmd": entry.get("cmd"), "cwd": entry.get("cwd"),
            "count": entry.get("count"), "pid": entry.get("pid"), "queued": entry.get("since"),
            "granted": entry.get("granted"), "ended": _config.now(), "outcome": outcome,
            "waited": None if queued is None else round((granted or ended) - queued),
            "ran": None if granted is None else round(ended - granted), "status": status}
    with contextlib.suppress(_config.SlotError, OSError):
        item.update(cores=_config.cores(), capacity=_config.total())
    item.update(usage or {})
    if entry.get("yields"):
        yielded = float(entry.get("yielded") or 0)
        away = epoch(entry.get("yielded_at"))
        item.update(yields=int(entry["yields"]), yielded=round(yielded + (max(0.0, ended - away) if away else 0), 2))
    if entry.get("until"):
        item["until"] = entry["until"]  # when its client would have given up (run --wait): the replay's patience
    meta = _config.meta_of(entry)
    if meta:
        item["meta"] = meta
    with contextlib.suppress(OSError):
        append_bounded(directory / "history.jsonl", json.dumps(item) + "\n", _config.HISTORY_MAX)


def append_bounded(path: Path, line: str, limit: int) -> None:
    """Append a line; past `limit` bytes the file becomes <name>.1, replacing the previous one, so at most two
    files' worth stays. Readers read <name>.1 first, then <name>."""
    fd = os.open(path, os.O_RDWR | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        size = os.fstat(fd).st_size
        # A torn earlier write (a holder killed mid-line) must not swallow this line: start it on a line of its own.
        prefix = b"\n" if size and os.pread(fd, 1, size - 1) != b"\n" else b""
        os.write(fd, prefix + line.encode("utf-8"))  # one write, at the end even with other appenders
    finally:
        os.close(fd)
    if path.stat().st_size > limit:
        path.replace(path.with_name(path.name + ".1"))


def objects(lines) -> list[dict]:
    """The JSON objects among lines; lines that do not parse (a torn write, a foreign line) are skipped."""
    found = []
    for line in lines:
        with contextlib.suppress(ValueError, UnicodeDecodeError):
            item = json.loads(line)
            if isinstance(item, dict):
                found.append(item)
    return found


def journal(path: Path):
    """The JSON objects of a bounded file and its rotated predecessor, oldest first."""
    for part in (path.with_name(path.name + ".1"), path):
        try:
            data = part.read_bytes()
        except FileNotFoundError:
            continue
        yield from objects(data.splitlines())


def tail_objects(path: Path, limit: int) -> list[dict]:
    """The last `limit` JSON objects of one file, read backwards from its end in blocks."""
    try:
        handle = open(path, "rb")
    except FileNotFoundError:
        return []
    with handle:
        end = handle.seek(0, os.SEEK_END)
        block, data = 65536, b""
        while True:
            start = max(0, end - block)
            handle.seek(start)
            data = handle.read(end - start)
            lines = data.splitlines()
            found = objects(lines[1:] if start else lines)  # the first line of a block may be cut
            if len(found) >= limit or not start:
                return found[-limit:]
            block *= 4


def history_items(directory: Path | None = None) -> list[dict]:
    """Every request in history.jsonl (and its rotated part), oldest first. Version-1 lines read as they are: they
    lack "v" and the usage fields."""
    return list(journal((directory or _config.state_dir()) / "history.jsonl"))


def history_lines(limit: int, directory: Path | None = None) -> list[str]:
    """The last `limit` requests that left the queue (every one for 0), oldest first. The rotated part is read only
    when the current file holds fewer."""
    path = (directory or _config.state_dir()) / "history.jsonl"
    if limit:
        items = tail_objects(path, limit)
        if len(items) < limit:
            items = tail_objects(path.with_name(path.name + ".1"), limit - len(items)) + items
    else:
        items = list(journal(path))
    found = []
    for item in items:
        ended = _config.seconds(item.get("ended"))
        when = time.strftime("%m-%d %H:%M", time.localtime(ended)) if ended is not None else "?"
        waited = "?" if item.get("waited") is None else _config.duration(item["waited"])
        ran = "" if item.get("ran") is None else f" ran {_config.duration(item['ran'])}"
        status = "" if item.get("status") is None else f" exit {item['status']}"
        used = ""
        if isinstance(item.get("cpu"), (int, float)):
            wall = item.get("wall") or item.get("ran")
            cores_used = f"{item['cpu'] / wall:.1f} cores" if wall else f"{item['cpu']:.0f} s CPU"
            used = f" used {cores_used}, {item.get('rss_peak_mb', 0):.0f} MB"
        meta = _config.meta_of(item)
        tags = f" meta={_config.meta_text(meta)}" if meta else ""
        found.append(f"{when} {item.get('outcome')}: {item.get('count')} slot(s) {item.get('label')} waited "
                     f"{waited}{ran}{status}{used}{tags} cmd={item.get('cmd')}")
    return found


def marker(path: Path) -> Path:
    """The yield request beside a queue entry (SLOTFATHER_YIELD in a yieldable lease's command)."""
    return path.with_suffix(".yield")


def asked(path: Path) -> dict | None:
    """The yield request for the entry at `path`, or None."""
    try:
        found = json.loads(marker(path).read_text())
    except (OSError, ValueError):
        return None
    return found if isinstance(found, dict) else None


def epoch(value) -> float | None:
    """A moment stored as epoch seconds or as a now() stamp, as epoch seconds; None for anything else."""
    return float(value) if type(value) in (int, float) and math.isfinite(value) else _config.seconds(value)


def log(directory: Path, message: str) -> None:
    with contextlib.suppress(OSError):
        with open(directory / "queue.log", "a", encoding="utf-8") as handle:
            handle.write(f"{_config.now()} {message}\n")


def save_order(values: dict[str, Any] | None, directory: Path | None = None) -> None:
    """Publishes order settings for every client (None: back to the defaults); callers hold the queue lock."""
    from .schedule import checked_order
    path = (directory or _config.state_dir()) / "order.json"
    if values is None:
        path.unlink(missing_ok=True)
        return
    values = checked_order({k: v for k, v in values.items() if k in _config.ORDER_DEFAULTS})
    path.parent.mkdir(parents=True, exist_ok=True)
    # The keys before classes always; a class knob only when it differs from its default, since a slotfather before
    # them refuses a file with keys it does not know (and falls back to the window order).
    write(path, {k: values.get(k, _config.ORDER_DEFAULTS[k]) if k == "order" else float(values.get(k, _config.ORDER_DEFAULTS[k]))
                 for k in _config.ORDER_DEFAULTS if k in _config.ORDER_BASE or values.get(k, _config.ORDER_DEFAULTS[k]) != _config.ORDER_DEFAULTS[k]})
