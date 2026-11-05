#!/usr/bin/env python3
"""Run Ruff and strict Pyright, rejecting diagnostics beyond the saved debt budget."""
from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Literal, TypedDict, cast

ROOT = Path(__file__).resolve().parents[1]
BASELINE = ROOT / "tools/type-baseline.json"


class RuffDiagnostic(TypedDict):
    filename: str
    code: str
    message: str


class PyrightDiagnostic(TypedDict):
    file: str
    rule: str
    severity: str
    message: str


class PyrightReport(TypedDict):
    generalDiagnostics: list[PyrightDiagnostic]


class Baseline(TypedDict):
    version: int
    diagnostics: dict[str, int]


def regressions(current: dict[str, int], accepted: dict[str, int]) -> dict[str, int]:
    """Diagnostics whose count exceeds the budget for that exact file, tool, rule and message."""
    return {key: count - accepted.get(key, 0) for key, count in current.items()
            if count > accepted.get(key, 0)}


def shrink_baseline(current: dict[str, int], accepted: dict[str, int]) -> dict[str, int]:
    """Return a smaller debt budget; never accept a new diagnostic during refresh."""
    if regressions(current, accepted):
        raise ValueError("baseline cannot grow: fix new diagnostics before shrinking")
    return dict(current)


def key(path: str, tool: str, rule: str, message: str) -> str:
    """Moving an existing diagnostic does not give another file any of its budget."""
    file = Path(path).resolve().relative_to(ROOT).as_posix()
    return "|".join((file, tool, rule, message))


def tool_report(tool: Literal["ruff", "pyright"], arguments: list[str]) -> object:
    executable = shutil.which(tool)
    if executable is None:
        raise RuntimeError(f"{tool} is missing; run with the pinned requirements-dev.txt tools")
    result = subprocess.run([executable, *arguments], cwd=ROOT, capture_output=True, text=True)
    if result.returncode not in (0, 1):
        raise RuntimeError(f"{tool} failed ({result.returncode}): {result.stderr or result.stdout}")
    try:
        return cast(object, json.loads(result.stdout))
    except ValueError as error:
        raise RuntimeError(f"{tool} did not produce a JSON report: {result.stderr or result.stdout}") from error


def collect() -> dict[str, int]:
    """Run both real tools. JSON casts cross their documented report boundary only."""
    ruff = cast(list[RuffDiagnostic], tool_report("ruff", ["check", "--output-format", "json", "."]))
    pyright = cast(PyrightReport, tool_report("pyright", ["--outputjson"]))
    counts: Counter[str] = Counter()
    for item in ruff:
        if item["code"] in ("F821", "F822", "F823", "E999", "invalid-syntax"):
            raise RuntimeError(f"cannot baseline broken code: {item['filename']}: {item['message']}")
        counts[key(item["filename"], "ruff", item["code"], item["message"])] += 1
    for item in pyright["generalDiagnostics"]:
        if (item["severity"] == "error" and not item.get("rule")) or item.get("rule") in (
                "reportUndefinedVariable", "reportMissingImports"):
            raise RuntimeError(f"cannot baseline broken code: {item['file']}: {item['message']}")
        if item["severity"] in ("error", "warning"):
            counts[key(item["file"], "pyright", item.get("rule", "error"), item["message"])] += 1
    return dict(sorted(counts.items()))


def read_baseline(text: str) -> dict[str, int]:
    raw = cast(object, json.loads(text))
    if not isinstance(raw, dict):
        raise ValueError("unrecognized baseline format")
    data = cast(dict[str, object], raw)
    diagnostics = data.get("diagnostics")
    if type(data.get("version")) is not int or data.get("version") != 1 or not isinstance(diagnostics, dict):
        raise ValueError("unrecognized baseline format")
    counts = cast(dict[object, object], diagnostics)
    if any(not isinstance(name, str) or type(count) is not int or count < 1
           for name, count in counts.items()):
        raise ValueError("baseline counts must be named positive integers")
    return cast(dict[str, int], diagnostics)


def smallest_budget(history: list[dict[str, int]]) -> dict[str, int]:
    """Once committed debt disappears, a later change cannot resurrect its budget."""
    if not history:
        return {}
    budget = dict(history[0])
    for previous in history[1:]:
        budget = {name: min(count, previous[name]) for name, count in budget.items() if name in previous}
    return budget


def committed_baseline() -> dict[str, int] | None:
    """A baseline cannot grow even if a previous commit skipped the local gate."""
    shallow = subprocess.run(["git", "rev-parse", "--is-shallow-repository"], cwd=ROOT,
                             capture_output=True, text=True)
    if shallow.returncode == 0 and shallow.stdout.strip() == "true":
        raise RuntimeError("baseline validation requires full Git history; fetch without a depth limit")
    commits = subprocess.run(["git", "log", "--format=%H", "--follow", "--", "tools/type-baseline.json"],
                             cwd=ROOT, capture_output=True, text=True)
    if commits.returncode:
        return None  # exported source tree without Git metadata
    history: list[dict[str, int]] = []
    for revision in commits.stdout.splitlines():
        result = subprocess.run(["git", "show", f"{revision}:tools/type-baseline.json"], cwd=ROOT,
                                capture_output=True, text=True)
        # A committed deletion after adoption is an empty budget, not missing evidence.
        # The path's log includes its first addition, so earlier unrelated commits are absent.
        history.append(read_baseline(result.stdout) if result.returncode == 0 else {})
    return smallest_budget(history) if history else None


def write_baseline(counts: dict[str, int]) -> None:
    payload: Baseline = {"version": 1, "diagnostics": dict(sorted(counts.items()))}
    BASELINE.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--initialize-baseline", action="store_true",
                       help="initial adoption only: refuses to replace an existing baseline")
    modes.add_argument("--shrink-baseline", action="store_true",
                       help="remove resolved debt; refuses to add any diagnostic")
    args = parser.parse_args(argv)
    try:
        current = collect()
        if args.initialize_baseline:
            if BASELINE.exists() or committed_baseline() is not None:
                raise ValueError("baseline already exists; use --shrink-baseline after fixing diagnostics")
            write_baseline(current)
            print(f"Initialized {sum(current.values())} existing diagnostics")
            return 0
        accepted = read_baseline(BASELINE.read_text())
        committed = committed_baseline()
        if committed is not None and regressions(accepted, committed):
            raise ValueError("saved baseline grew beyond committed history; restore its budget and fix new diagnostics")
        added = regressions(current, accepted)
        if added:
            for diagnostic, count in sorted(added.items()):
                print(f"+{count} {diagnostic}", file=sys.stderr)
            print(f"Quality gate failed: {sum(added.values())} new diagnostics", file=sys.stderr)
            return 1
        if args.shrink_baseline:
            write_baseline(shrink_baseline(current, accepted))
        print(f"Ruff + strict Pyright: no new diagnostics; "
              f"{sum(current.values())} existing, {sum(accepted.values()) - sum(current.values())} resolved")
    except (OSError, RuntimeError, ValueError, KeyError, TypeError) as error:
        print(f"quality gate: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
