# Statistics

```sh
slotfather stats                      # every request in the history, grouped by label and command kind
slotfather stats --since 24h --by kind
slotfather stats --since 2026-11-02 --label 'request-*' --json
```

While `run`'s command runs, its holder looks at the command's process tree: first after half a second, then each interval half as long again, up to `SLOTFATHER_SAMPLE_MAX` seconds (default 10), and once more after the command exits. One `ps` over the machine per look; nothing per second.

- **CPU** (`cpu`, seconds of user plus system time) is the command's own rusage from `wait4`, which already holds every descendant that a parent inside the tree reaped, short-lived ones included, plus `cpu_detached`: processes that left the tree. A child whose parent exits is adopted by launchd and its CPU never reaches the command's rusage, so it stays the lease's and counts its last sampled CPU time; so does a child of launchd started during the lease whose environment carries the lease's `SLOTFATHER_LEASE` (macOS shows the environment of the user's own processes that are not Apple platform binaries). Processes born and gone between two looks outside the reaped tree are missed, as is a child that exits while its parent never waits for it and the parent then exits (launchd reaps the zombie), and work done by daemons (Docker's VM, launchd services) is not the lease's.
- **Memory**: `rss_peak_mb` and `rss_mean_mb`, the sum of resident memory over the tree at each look (time-weighted mean; shared pages count once per process). The peak is at least the largest single process from rusage.
- `wall` (seconds, monotonic), `processes` seen, `samples` taken, and the machine's `cores` and `capacity` at release. Lines carry `"v": 2`; lines written before (no `v`, no usage) still read and count in waits and runs. Nested runs record nothing of their own; leases taken by an older slotfather or through the library `acquire()` record no usage.

While leases run, holders also append a machine sample to `machine.jsonl` at most every `SLOTFATHER_MACHINE_EVERY` seconds (default 15; shared between holders by the file's age and a lock of its own, never the queue lock): load averages, the cumulative CPU ticks (busy is the change between samples), the memory-pressure level and free-memory percentage, swap, and the slots held. It rotates like the history.

`stats` groups requests by label and by command kind (or, with `--by meta.KEY`, by that metadata key; requests without it group under `-`) (program and first argument: `bun run-browser-checks.mjs`, `verify.sh --checks [95]` for a list of 95 checks) and gives, per group, the median and 90th-percentile wait and run, typical slots, mean cores used (CPU seconds over wall seconds), **cores used per slot held** (CPU seconds over wall seconds times slots) and the peak resident memory. Below it: the same over every measured lease, and the slots at which that use would fill the cores; the mean slots held while any lease ran and how often all were held; and from the machine samples, the share of time the one-minute load stood over the cores (more runnable threads than cores) and under them, the mean CPU busy share, how often a slot's worth of cores sat idle, busy cores per held slot (includes work without a lease), memory pressure and the least free swap. `--json` prints all of it; `--top N` limits the table.

**Choosing capacity.** `slotfather slots N` should make the slots' use fill the cores without holding the load over them: with *c* cores used per slot held, *cores / c* slots fill the machine. Check the machine part too: much time over the cores with all slots held means fewer slots; a slot's cores often idle while requests wait means more.

Use `--by ticket` or `--by priority` to group by request fields. `--by meta.KEY`
groups only custom caller tags; the retired built-in metadata names are refused.
