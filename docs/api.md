# State, metadata and JSON status

[Domain definitions](../GLOSSARY.md) · [Compatibility aliases](terminology.md)

State lives in `~/.local/state/slotfather/`; `queue.log` records grants, releases, and reaping. `history.jsonl` has one JSON line per request that left the queue: label, command, directory, slots, queue, grant and end times, seconds waited and run, the outcome (`released`, `left` after giving up, or `reaped`), `until` for a request with `--wait` (when it would give up) and, for `run`, the exit status and what the command used (see Statistics). Past 4 MB it becomes `history.jsonl.1`, replacing the previous one, so at most two files' worth is kept; readers take `.1` first. `history [N]` lists the last N (default 20). `status` lists holders and waiters.

**Request fields.** `run --priority interactive|normal|batch` (library:
`acquire(..., priority="interactive")`) sets scheduling priority; omitted priority
is `normal`. `--ticket ID` / `ticket=...` sets an optional issue identifier.
Both are top-level queue and history fields and appear in JSON status/process
responses (`ticket` is null when absent in query responses). Inherited leases
keep the outer request's fields. Built-in fields cannot be passed through metadata;
`--class`, `meta.class`, `meta.priority` and `meta.ticket` are rejected.

**Lease metadata.** `run --meta key=value` (repeatable; the library's `acquire(..., meta={...})`) adds caller context: `lane`, `phase`, `model`, a `session`, and custom tags. Keys are lowercase words of at most 32 characters (`a-z`, digits, `_ . -`), values printable text of at most 200, at most 16 keys; a later key replaces an earlier one. The metadata is stored on the queue entry and in the request's history line (`"meta": {...}`), and shown by `status` and `history` (`meta=lane=lane-650,phase=check` before `cmd=`), in the dashboard's REQUEST DETAILS panel, and grouped by `stats --by meta.KEY`. A nested `run` that inherits a lease runs under that lease's metadata and adds none of its own. Priority and ticket use dedicated request fields rather than metadata.

**`status --json`** is the queue for programs, such as a guard that must not kill another holder's lease: `{"v": 2, "slots", "held", "free", "entries": [...], "legacy": [...]}`, each entry `{"id", "lease", "state", "count", "label", "pid", "pgid", "start", "since", "granted", "cmd", "cwd", "meta", "tree", "priority", "ticket", "yieldable", "yields", "yield_asked", "position"}` (the last five: see Polite preemption). `id` is the entry's name, `lease` its path (the value of `SLOTFATHER_LEASE` in the lease's command), `pid` the holder (`slotfather run`, or the process that called `acquire()`), `pgid` its process group, `tree` the holder and its live descendants. `status --json --pid PID` keeps only the entries PID belongs to, each with `via`: `"tree"` (the holder or one of its descendants) or `"env"` (it left the tree, by a double fork or `setsid` and launchd adopting it, but its environment carries the lease's `SLOTFATHER_LEASE`; macOS shows the environment only of the user's own processes that are not Apple platform binaries), and adds `"query": {"pid", "start"}`, the process's start time as `ps -o lstart=` prints it (`null`: not running), so a guard can check that the pid was not reused before it acts. An empty list means the process belongs to no lease of this queue as far as can be seen. The extracted version also reads leases in Straylight's earlier queue during migration; old clients do not see new leases, so retire old wrappers before relying on mutual isolation.

## Process queries

Use `procs` for process ownership checks and worktree inspection:

```sh
slotfather procs --json --pid 4242
slotfather procs --json --cwd /path/to/worktree
```

Unlike `status`, which can reap abandoned leases while reporting the queue,
`procs` is strictly read-only. It never creates state, takes the queue lock,
reaps an entry or appends history. Unknown future state schemas and failed
process-table reads produce an error, not an empty ownership answer.

The versioned response has these fields:

| Field | Meaning |
| --- | --- |
| `v` | Response schema version, currently `2`. |
| `query` | Requested `pid` and canonical `cwd`; `start` identifies the requested PID, or is `null` when it is not live. |
| `processes` | Matching live process records, in PID order. |
| `cwd_unknown` | Own-user PIDs whose working directories could not be read as local paths. Relevant when `--cwd` is requested. |
| `lease_unknown` | PIDs for which the detached-process environment check could not be completed. |

Each process contains `pid`, `ppid`, `uid`, `tty`, `start`, `cmd`, `cwd`,
`ancestors`, `sessions` and `leases`. `start` is the C-locale `ps lstart`
identity with whitespace normalized. Zombies are excluded. `ancestors` lists
parents nearest first, without PID 1. `sessions` contains the process itself
and ancestors recognized as terminal, agent, remote-login or CI sessions;
each record contains `pid`, `start` and `cmd`. This is observed ancestry,
not an assertion that an agent owns another process.

A `leases` record contains `id`, `lease` (path), `state`, `count`, `label`,
`pid` (holder), `start` (holder identity), `priority`, `ticket`, `meta`, `legacy` and `via`.
Priority is `interactive`, `normal` or `batch`. `via` is `tree`
for the holder or its descendants, or `env` for a detached process carrying
that exact `SLOTFATHER_LEASE`. Stale holder identities never match. Legacy
counts use that older queue's capacity units. No environment contents are
returned.

`--pid` selects exactly that PID, not its descendants. It reads one process
table and skips the working-directory scan; `cwd` in its process record is
therefore `null` unless `--cwd` is also supplied. Without either filter all
live processes are reported, also without a working-directory scan.

On macOS, exact-PID queries use standard-library `ctypes` with libproc for
process identities, then one targeted `ps` read for the requested process's
and its ancestors' full arguments. That command never requests environments.
The adapters' identities must agree. Unsupported native interfaces or an
unavailable required identity fall back to the portable process-table adapter.

`--cwd` selects this user's processes whose canonical working directory is
the root or a descendant directory: `/work/lane-other` does not match
`/work/lane`. The query process and its ancestors are omitted. Both filters
may be combined and intersect. Linux processes in a different or unreadable
mount namespace are listed in `cwd_unknown`, since their pathnames cannot
be interpreted safely in the caller's filesystem.

Consumers that decide whether to signal a process or remove a worktree must
fail closed on query errors and relevant `cwd_unknown` or `lease_unknown`
entries. An empty `leases` list is only the membership visible to this
process: macOS and Linux permissions can hide detached processes'
environments. A negative macOS `ps -wwE` observation therefore remains in
`lease_unknown`, even when ps printed the process's command. Only a positive
lease marker proves detached membership there; readable Linux `/proc` bytes
can also prove the absence of that marker. It is not permission to stop a process. Recheck `(pid, start)`
immediately before any action, because PIDs can be reused after the query.
