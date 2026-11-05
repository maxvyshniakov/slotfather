# CLI latency

Measured on 2026-11-05, macOS arm64, Python 3.13.14. The baseline is the
standalone executable at `556f912dd1354e6142b1204db9e0ca220495430e`;
the candidate is the package whose source hashes accompany the
[measurement data](performance/2026-11-05-cli-macos.json).

| Fresh process | Before median / p95 | After median / p95 |
| --- | --- | --- |
| `--help` startup | 46.25 / 49.04 ms | 30.50 / 31.97 ms |
| `status --json --pid PID` | 75.14 / 76.90 ms | 39.62 / 41.77 ms |
| `procs --json --pid PID` | — | 50.29 / 56.05 ms |

Startup and the existing status query are below the issue's 50 ms target in
both median and p95. The new process query is around 50 ms; its slower samples
are retained in the data. Supplementary `python -S` runs measured 30.06 / 31.02 ms
for startup, 38.51 / 40.35 ms for status and 48.80 / 51.43 ms for procs.

## Method

The benchmark ran through `slotfather run -n all` against the real shared queue.
It copied the candidate launcher and package into a separate directory before
waiting for the machine, so concurrent checkout edits could not change a run.
SHA-256 checks before and after confirmed that those sources stayed fixed.
The baseline executable came directly from Git.

Each command started a fresh interpreter 25 times: five warm-up runs, then
20 measured runs. `time.perf_counter()` measured subprocess wall time through
exit and captured output. JSON validation followed the timer. PID queries
selected the benchmark process, a live descendant of its all-slot lease.
The artifact records sample counts, minimum, maximum, median, empirical p95,
interpreter, baseline hash and every candidate package-source hash.

To repeat the comparison, freeze the launcher and `src/slotfather`, export the
baseline executable with `git show <baseline>:slotfather`, and use the same
interpreter and PID for both commands under an exclusive machine lease. Repeat
five warm-ups and 20 measured subprocesses per command; validate their exit
status and JSON, and reject a run if its source hashes change.

## Scope and fallback

The macOS status path reads native BSD identities and uses the smaller BSD
record for otherwise protected parent links. Every lease holder and queried
PID still requires a full identity. Legacy `ps lstart` spacing is preserved.
The new process query reads only its required identities and obtains full
ancestry arguments in one targeted `ps` call. Command environments are never
requested by that arguments call.

These are warmed-filesystem measurements from this Mac, not real-time bounds.
Permission failures, unsupported native interfaces and changing identities
fall back to the portable `ps` path and may exceed 50 ms. Linux latency was not
measured in this capture. Process visibility and fail-closed consumer rules are
part of the [query API](api.md#process-queries).
