# Configuration

`-n` selects how many slots one command requests; it does not change the total capacity:

```sh
slotfather run -n 3 -- make build
```

Use the shared capacity command:

```sh
slotfather slots             # show effective capacity and saved/automatic mode
slotfather slots 6           # save six slots for every future client
slotfather slots memory 5    # one slot per 5 GiB of memory
slotfather slots lower 4     # the lower of the CPU budget and one slot per 4 GiB
slotfather slots reset       # restore automatic capacity
```

A saved value persists across shells and process restarts in `capacity.json` under the shared state directory. No shell export or per-process total override is needed.

**Capacity modes** (#653). The CPU budget is `max(1, cores // 3)`; the memory budget for *G* GiB per slot is `max(1, floor(round(memory_GiB) / G))`, the memory counted in whole GiB (Linux's `MemTotal` is a little under the size a VM was given: 11.7 for 12). On Linux the memory is the limit of the queue's cgroup where it has one (see Linux), else `MemTotal`, which shrinks when a VM's balloon takes memory. A capacity that shrinks under waiters never strands them: a request larger than the capacity takes every slot when it starts (its holder's `SLOTFATHER_COUNT` and the history say how many), and `-n all` takes the capacity at its grant, also when it grew. The automatic default is the lower of the CPU budget and the memory budget at 4 GiB per slot: four slots on the 12-core, 32 GiB Mac (CPU-bound), two on a 6-core, 12 GiB VM, three on a 16-core machine with 12 GiB (memory-bound). Heavy leases on the Mac peaked at 2–3 GiB of summed resident memory per slot in the median and 6 GiB at most (the history of 2026-11-03; summed RSS counts shared pages once per process, so it overstates). `slots memory G` uses the memory budget alone, for a host where running out of memory kills jobs while too many runnable threads only slow them, such as a CI runner without swap; `slots lower G` the lower of both budgets. Both follow the machine: they save the mode, *G* and, on Linux, the saver's limited cgroup (`"cgroup"`), and every client works the count out again from that cgroup wherever it runs, so a VM given more memory gets more slots without a new save. They also save the count they give at the time as `slots`, which slotfather 0af7546 and older read alone (an older client keeps that count after the machine changes). Where the memory cannot be read the saved count holds (the automatic default: the CPU budget). `status`, `available`, `advise` and `run -n all` use the resulting count; `advise` adds `capacity_mode`, `gib_per_slot`, `memory_gib` and `memory_short` (why new leases would wait for memory now, or `null`; the job count does not shrink for it).

**VM110** (#653: 6 vCPU, 11.7 GiB, no swap, about 1 GiB in use with nine idle runner services and the guest agent; the nine services in `ci-runners.slice` with `MemoryMax=10G`, each with `MemoryMax=6G`): `slotfather slots memory 5` with the command installed `--cgroup /ci.slice/ci-runners.slice`: two slots of the slice's 10 GiB, whatever the balloon does to `MemTotal` (the guard still reads `MemAvailable`). The Sid-Kit job that ran it out of memory had three headless Chromium renderers of 0.5–1.4 GiB each, about 5 GiB with the browser and Bun processes; on the Mac a 2-slot Sid-Kit browser run peaked at 4.3–9.2 GiB and 1-slot Vitest runs at 3.6–6.2 GiB. One heavy job is therefore about 5 GiB and two just fit: with two slots, Sid-Kit's and Data Grid's 2- and 3-slot browser checks and verifies (`heavy.sh 2`, `3`) run alone and two 1-slot jobs share the VM, with the Linux guard (under 1 GiB available, or memory stalls) holding a third start and per-service `MemoryMax` as the hard stop. The automatic default also gives two there, but by the CPU budget (`6 // 3`); `memory 5` keeps the count tied to memory when vCPUs are added and gives three at 16 GiB. Raise it (`memory 4`, three slots) only once runs under the queue show leases peaking well under 4 GiB per slot (`slotfather stats` on the VM).

Capacity changes and reset are refused while any live holder or waiter exists, including a live entry in a migration queue. Let the queue drain and retry; existing work is never cancelled. Publication is atomic and uses the same lock as queue admission. `run -n all` resolves capacity when it joins the queue and again when it is granted (the entry's `all`), so it keeps its exclusive meaning across a settings change or a memory capacity that moved.

| Variable | Purpose |
| --- | --- |
| `SLOTFATHER_DIR` | Shared queue directory. |
| `SLOTFATHER_CORES` | Core-count override for capacity and advice. |
| `SLOTFATHER_MEMORY_GIB` | Memory-size override, in GiB, for the memory budget. |
| `SLOTFATHER_POLICY` | `normal` (default), `background` (low QoS), or `bypass` (runs without a lease). |
| `SLOTFATHER_MIN_FREE_SWAP_MB` | Free-swap floor; default 512 MB. Ignored while swap is off. |
| `SLOTFATHER_MIN_AVAILABLE_MB` | Linux: `MemAvailable` floor; default 1024 MB. |
| `SLOTFATHER_CGROUP` | Linux: the queue's cgroup v2 path (`/ci.slice/ci-runners.slice`); default the one `install.py --cgroup` wrote in, else the one saved with the capacity, else the caller's outermost limited cgroup. |
| `SLOTFATHER_MAX_PSI_SOME`, `_FULL` | Linux: PSI memory `avg10` limits in percent; defaults 10 and 5; `0` turns one off. |
| `SLOTFATHER_LEGACY_DIRS` | Colon-separated old queues; defaults to `~/.local/state/straylight-verification/slots`. Empty disables migration reads. |
| `SLOTFATHER_POLL` | How often a waiter checks its own entry, and its first interval between full looks at the queue, in seconds; default 2. |
| `SLOTFATHER_POLL_MAX` | Longest interval between a waiter's full looks (each runs `ps` under the queue lock); default 10. The interval grows by half while the reason to wait stays the same. A release grants waiters at once, so the backoff only delays reaping a crashed holder and recovery from memory pressure. |
| `SLOTFATHER_OVERTAKE_WINDOW` | Seconds after it queued during which a waiter that does not fit lets later requests that fit start first; default 600. With `window` and `0`, requests are served strictly in arrival order. |
| `SLOTFATHER_QUEUE` | `window`: this client grants by the order before #643 whatever `slotfather order` saved (see Ordering). |
| `SLOTFATHER_BACKFILL_SAFETY`, `_LIMIT`, `_FALLBACK`, `_OVERRUN` | Tighten this client's backfill below what `slotfather order` saved (higher safety or fallback, lower overrun or nonzero limit); looser values are ignored. |
| `SLOTFATHER_PRIORITY_INTERACTIVE`, `_BATCH` | Move this client's classes less than `slotfather order` saved (seconds; lower values only; 0: not at all). |
| `SLOTFATHER_YIELD_AFTER` | Ask holders to yield later than saved (seconds; higher values only), or `0`: this client's looks ask nobody. |
| `SLOTFATHER_SAMPLE_MAX` | Longest interval, in seconds, between a holder's looks at its command's process tree; default 10. |
| `SLOTFATHER_MACHINE_EVERY` | Seconds between machine samples while leases run; default 15. |
| `SLOTFATHER_LOADAVG` | Advice test/diagnostic override: one number or `load1 load5 load15`. |
| `SLOTFATHER_MEMORY_FILE` | Memory test seam: file containing `free_swap_MB pressure_level`; it replaces both the macOS and the Linux signals. |
| `SLOTFATHER_CGROUP_ROOT` | Linux test seam: the cgroup v2 mount, default `/sys/fs/cgroup`. |
| `SLOTFATHER_PROC` | Linux test seam: a directory read as `/proc` for `meminfo`, `pressure/memory` and `PID/environ` (and the Linux paths then run on any host). |

A command receives `SLOTFATHER_LEASE` and `SLOTFATHER_COUNT` for its existing lease, and a yieldable lease's command `SLOTFATHER_YIELD`, the path of its yield request (see Polite preemption). Do not fabricate these values to skip queue admission.
