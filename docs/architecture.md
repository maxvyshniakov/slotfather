# Architecture and compatibility

Slotfather is a Python package under `src/slotfather/`. The four repository executables are
entry points; installation copies a self-contained package, without pip or runtime packages.
The source package lives under `src` because the root `slotfather` name is the executable.

Definitions: [glossary](../GLOSSARY.md). Naming and wire aliases: [terminology](terminology.md).

## Modules

| Module | Owns |
| --- | --- |
| `config` | Environment settings, installation defaults, constants and shared errors. |
| `platform` | macOS/Linux inspection: process identity and ancestry, memory, swap, PSI and cgroups. |
| `state` | Queue locking, atomic files, bounded journals and persisted schema contracts. |
| `schedule` | Queue ranking, backfill, runtime estimates and cooperative-yield decisions. |
| `leases` | Taking, granting, releasing, reaping and inheriting leases; command execution. |
| `usage` | Process-tree accounting and machine sampling while work runs. |
| `query` | Compatible queue snapshots and status output. |
| `procs` | Read-only process queries for guards and other tools. |
| `cli` | Argument validation and command dispatch. |
| `dashboard`, `stats`, `sweep` | Presentation, historical analysis and guarded leftover-process cleanup. |

Scheduling decisions operate on entry data; OS inspection and file mutation stay outside
those decisions. Optional views load separately from admission. Tests cross the same package
interfaces as callers and replace OS/time inputs at their boundaries.

## State on disk

The queue directory defaults to `~/.local/state/slotfather`; `SLOTFATHER_DIR` overrides it.
A pinned installation shares one user's directory even when a service changes `HOME`.

| File | Format and compatibility |
| --- | --- |
| `schema.json` | State schema 1. Unstamped queues are adopted without rewriting entries; unknown versions are refused before mutation. Old clients ignore the stamp and can release existing leases. |
| `queue/*.json`, `sequence`, `.lock` | Existing queue format: atomic JSON entries and a shared advisory lock. PID plus process start time identifies a holder. |
| `capacity.json`, `order.json` | Shared settings. Keep old keys and meanings; older clients ignore added fields. |
| `queue/*.yield` | Cooperative requests, never permission to stop a holder. |
| `queue.log` | Timestamped grants, releases and reaping; retained event syntax supports existing history readers. |
| `history.jsonl[.1]` | Current record version 2. Unversioned version-1 history remains readable. |
| `machine.jsonl[.1]` | Record version 1; unavailable facts remain unavailable. |
| `estimates.json` | Disposable cache version 2. Rebuilt when its version or history stamp changes. |
| `sweep.json`, `sweep.jsonl[.1]` | Cleanup settings and version-1 results. Cleanup never owns lease admission. |

A structural migration must preserve these files and active leases. Adding data is safe only
when old readers can ignore it and old writers can still release their leases. A breaking
schema needs a distinct migration, validation and coexistence test; moving Python code is
not a reason to rewrite queue state. Derived caches may be rebuilt rather than migrated.

## Stable surfaces

- Command names, options, exit codes and signal forwarding remain compatible.
- `status --json` stays at version 1. Process queries have a separate versioned response.
- Process identity is a PID plus its start time. A recycled PID must never inherit a lease
  or permission to stop a process.
- Installed commands bind to an immutable package release; replacement entry points do not
  change code underneath an already running old client. Releases are retained while old
  clients may still use them.
- `slotfather version` identifies the installed source so local and remote installations can
  be compared. Installation does not take over queue admission or cancel running work.

Executable-file internals are not the package API. External tools should use documented JSON
commands; Python integrations should import the module that owns the behavior they use.
