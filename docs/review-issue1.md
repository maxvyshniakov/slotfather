# Issue 1: independent review and validation

Base: `556f912dd1354e6142b1204db9e0ca220495430e`. The owner excluded VM110
installation from this task on 2026-11-05; Linux acceptance is local in OrbStack.
The implementation remains an uncommitted working candidate.

The configured Claude Opus reviewer was unavailable because its weekly quota was
exhausted. Two separate fresh GPT-6.1 Sol contexts reviewed the staged candidate,
without the implementer's report, edits or test runs. These are same-family
fallback reviews, not cross-family approval. One correction round followed;
there was no second reviewer pass or convergence claim.

## Standards pass as returned

Explicit violations, all P2:

1. `macos_process.command()` returned an executable path or short name when
   KERN_PROCARGS2 failed, losing arguments and violating the unavailable-state
   contract. The staged regression already expected full-command fallback.
2. An empty stored lease start bypassed identity comparison and received the
   current PID's start time, allowing a recycled PID to inherit an old lease.
3. Baseline history silently skipped commits where its file was absent, so
   deleting and restoring the file could restore removed diagnostic budgets.
4. `schedule.save_order()` removed files and created directories despite the
   documented ownership of filesystem mutation by `state` / `leases`.

No judgment-call findings.

## Spec pass as returned

All P2:

1. Partial: the dashboard's fixed November test used today's fixed UTC offset,
   leaving a seasonal DST failure despite the requirement to fix flaky tests.
2. Wrong: a macOS `ps -wwE` PID line was treated as proof of visible environment,
   so protected detached processes could receive empty leases and no unknown flag.
3. Missing acceptance evidence: staged documentation did not establish the
   startup/status target below 50 ms or record final before/after numbers.

No additional missing, extra or incompatible behavior was found in extraction,
CLI dispatch, schema adoption, immutable installation, SSH transport or upgrade
fixture. Static comparison found no omitted original core function.

## Dispositions

All seven findings were fixed in the working candidate:

- Full arguments use one targeted `/bin/ps` call with native identity validation;
  the raw argv/environment decoder and short-name fallback were removed.
- `procs` rejects empty lease identity instead of inventing ownership. Missing
  or hidden macOS environment evidence remains explicitly unknown.
- The checker treats a committed baseline deletion as an empty budget. A real
  temporary Git history reproduces deletion/restoration through the public gate.
- Order persistence moved to `state.save_order`; scheduler validation remains
  in `schedule`. Existing order/coexistence tests still pass.
- The calendar test localizes its fixed date using that date's timezone rules.
- [Performance evidence](performance.md) records startup and status below the
  target, complete distributions and safe fallback limitations.

## Validation

The exact runtime and tests were copied to an immutable tree before final checks:

- macOS: 320 tests passed, four platform/root-only skips.
- Linux, local Docker/OrbStack, root with service-user acceptance enabled:
  320 tests passed, four platform/root-permission skips. A focused unprivileged
  run separately covers the unreadable-directory behavior.
- Ruff and strict Pyright: no new diagnostics. The legacy baseline shrank from
  5,889 to 5,353 diagnostics; this does not claim all legacy code is fully typed.
- A frozen real pre-package client keeps and releases its lease across upgrade.
- Terminal help and mouse navigation use real PTYs. Help previews retain their
  input takes and verified manifests outside the repository.

The owner subsequently requested dynamic detail-label spacing and scrollable,
structured help. Focused RED/GREEN tests and the final suites cover those changes.
No remote VM was started, installed or changed. No commit or push was performed.
