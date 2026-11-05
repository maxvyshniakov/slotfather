# Checks

Installed commands use only Python's standard library. Ruff and Pyright are development
checks, pinned in `requirements-dev.txt`; they are not installed on runner hosts.

## Gate

Run each command through one machine slot:

```sh
slotfather run -n 1 -- uv run --no-project --with-requirements requirements-dev.txt python tools/check.py
slotfather run -n 1 -- python3 -m unittest discover -s tests -v
```

The first command runs Ruff's error, import and bug-risk rules and Pyright in strict mode.
`tools/type-baseline.json` records existing diagnostics by relative file, tool, rule and message.
An increased count or a diagnostic in another file fails. A moved line does not consume a new
budget. The checker also rejects baseline growth relative to committed history. The baseline is
legacy debt, not a claim that the entire codebase is fully typed. New process-query and quality-gate
modules start without debt. Do not suppress a new error, expand the baseline or weaken the
configuration to make a change pass. CI must fetch full Git history (`fetch-depth: 0`);
shallow checkouts are refused because they can hide earlier budget reductions.

After fixing existing diagnostics, remove their budget:

```sh
slotfather run -n 1 -- uv run --no-project --with-requirements requirements-dev.txt python tools/check.py --shrink-baseline
```

This refresh refuses new diagnostics. `--initialize-baseline` is only for initial adoption;
it refuses an existing baseline, including one present in Git history.

For a focused change, run its test file before the full gate. Tests use isolated directories
and child processes; they must not modify the live queue. See [testing](../testing.md) for
Linux-container and media acceptance. Remote installation acceptance must compare local and
remote `slotfather version` and preserve active leases.

## Review

Review the issue's behavior and compatibility contract separately from coding standards.
The architecture is described in [architecture](../architecture.md). Keep command parsing in
`cli`, filesystem mutation in `state`/`leases`, OS inspection in `platform`, and scheduling
decisions in `schedule`. No shared mutable execution namespace or wildcard-import coupling.

New process queries must validate process start times, distinguish unavailable inspection
from an empty result, and leave queue files unchanged. Installer changes must prove old
clients can finish active leases during an upgrade. Do not rerun heavy gates inside review.
