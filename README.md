# slotfather

A machine-wide FIFO queue for expensive builds, test suites, and agent work. Projects sharing a computer use one capacity budget instead of starting independent worker pools.

The executable is a standalone Python 3.9+ script using only the standard library; the optional live view is a second script, `slotfather-dashboard`, kept apart so the queue code stays small. It requires POSIX process and file-lock facilities; it is exercised on macOS. macOS supplies the memory-pressure observations. Other hosts continue without observations their OS does not expose.

## Install

```sh
gh repo clone maxvyshniakov/slotfather
cd slotfather
python3 install.py
```

The installed commands run on the interpreter that ran the installer: their `#!/usr/bin/env python3` line becomes that interpreter's path, as invoked, so an agent inside a project's virtual environment does not run slotfather on the project's Python. The installer refuses an interpreter inside a virtual environment, and one older than 3.9; name another with `python3 install.py --python /usr/bin/python3`. Re-run it to switch interpreters.

The installer also performs the one-time rename migration. It refuses live holders or waiters in either retired queue, preserves the entire earlier machine queue (including saved capacity) when the new queue is absent, and otherwise leaves the existing new queue untouched. Retired contents remain in named `~/.local/state/slotfather-migrations/` backups. Different old and new configurations are both preserved; the existing new configuration stays effective.

Both retired paths, `~/.local/state/machine-slots` and `~/.local/state/straylight-verification/slots`, become regular file guards. Atomic directory/file exchanges ensure old clients cannot recreate an uncoordinated queue between the move and guard creation. Existing intentional guards make a repeated install safe, including while the new queue has active leases. Unrelated files at those paths are refused and preserved. The installer uses the default state locations; coordinate custom queue paths separately.

The executable is replaced atomically and the old command name is removed last. If a migration stops after a directory exchange, retain its guards and printed backup paths: the queue contents remain preserved for recovery. This is an explicit installation step, not compatibility logic in normal command execution.

Put `~/.local/bin` on your `PATH`. All participating projects must use the same state directory; saved capacity is shared through that directory. Updating the executable preserves the queue and running leases.

## Use

```sh
slotfather run -n 1 --label tests -- python3 -m unittest discover
slotfather run -n 2 -- make build
slotfather run -n all -- ./benchmark.sh
slotfather status
slotfather history 50
slotfather available
slotfather advise --max 4
slotfather advise --exclusive
```

`run` waits for capacity, holds a lease while its command runs, and returns the command's exit status. The default request is one slot. `--wait SECONDS` bounds the wait and returns 75 if capacity does not arrive; a missing executable returns 127 and a signal returns 128 plus its number.

The automatic capacity is `max(1, cores // 3)`: four slots on a 12-core machine. A request larger than the machine's capacity takes every slot. Requests are served in arrival order with one exception: a waiter that does not fit lets later requests that fit start before it for `SLOTFATHER_OVERTAKE_WINDOW` seconds after it queued (default 600). After that it holds back everyone behind it, so an all-slot request starts no later than the window plus the longest run admitted during it. Use one slot for a suite or build, two for half this 12-core machine, and `all` for a benchmark requiring isolation; a waiting `-n all` request says so on stderr.

Nested invocations inherit a live ancestor's lease rather than waiting for another one. Dead holders and reused PIDs are detected using process IDs and process start times. New leases wait while macOS reports low free swap or memory-pressure warning, except that an otherwise idle machine can start one request. With swap turned off there is no swap to run short of, so only memory pressure holds leases back.

`advise` prints a JSON worker-budget decision. It uses the lower of the one- and five-minute load averages, subtracts the CPU share explained by held slots, and reserves the rest for unleased work. The 15-minute tail is recorded but does not shrink a new run after earlier work ended. `--exclusive` assumes the caller will wait for every slot; it is advice, not a lease.

State lives in `~/.local/state/slotfather/`; `queue.log` records grants, releases, and reaping. `history.jsonl` has one JSON line per request that left the queue: label, command, directory, slots, queue, grant and end times, seconds waited and run, the outcome (`released`, `left` after giving up, or `reaped`) and, for `run`, the exit status; past 4 MB its older half is dropped. `history [N]` lists the last N (default 20). `status` lists holders and waiters. The extracted version also reads leases in Straylight's earlier queue during migration; old clients do not see new leases, so retire old wrappers before relying on mutual isolation.

## Dashboard

```sh
slotfather                           # in a terminal: the live view; ? lists keys, q quits
slotfather dashboard --interval 5    # the same, refreshing every 5 s instead of every second
slotfather dashboard --once          # one snapshot as text (add --width/--height, --color)
```

Plain `slotfather` opens the dashboard only when both its input and output are a terminal; without one, as from a script or an agent, it prints the usage and exits 2 as before.

The top line says what happens to a request made now (open, full, queued behind a larger head, or held back by the memory guard) and why. Below it one bar shows the capacity: each lease's slots joined in its own colour, free slots hollow, queued demand after it. RUNNING lists leases with how long they ran, how long they waited and the cores their process tree uses against the cores their slots stand for; a lease holding slots while nearly idle turns yellow. QUEUE lists waiters in arrival order with the slots requested ahead of each. SELECTED shows the chosen entry's command, directory and busiest processes. MACHINE shows load, memory pressure, the guard, and slots held over the last hour, rebuilt from `queue.log`. EVENTS is the log's tail, newest first.

`slotfather dashboard` runs `slotfather-dashboard` from beside the `slotfather` executable with the same interpreter; nothing of it loads during queue admission. The dashboard only reads the queue (like `status`, a refresh reaps dead entries); it never grants, cancels or kills anything.

## Configuration

`-n` selects how many slots one command requests; it does not change the total capacity:

```sh
slotfather run -n 3 -- make build
```

Use the shared capacity command:

```sh
slotfather slots          # show effective capacity and saved/automatic mode
slotfather slots 6        # save six slots for every future client
slotfather slots reset    # restore automatic capacity
```

A saved value persists across shells and process restarts in `capacity.json` under the shared state directory. No shell export or per-process total override is needed. The automatic default is `max(1, cores // 3)`, so four slots on a 12-core Mac.

Capacity changes and reset are refused while any live holder or waiter exists, including a live entry in a migration queue. Let the queue drain and retry; existing work is never cancelled. Publication is atomic and uses the same lock as queue admission. `run -n all` resolves capacity only when it joins the queue, so it keeps its exclusive meaning across a concurrent settings change.

| Variable | Purpose |
| --- | --- |
| `SLOTFATHER_DIR` | Shared queue directory. |
| `SLOTFATHER_CORES` | Core-count override for capacity and advice. |
| `SLOTFATHER_POLICY` | `normal` (default), `background` (low QoS), or `bypass` (runs without a lease). |
| `SLOTFATHER_MIN_FREE_SWAP_MB` | Free-swap floor; default 512 MB. Ignored while swap is off. |
| `SLOTFATHER_LEGACY_DIRS` | Colon-separated old queues; defaults to `~/.local/state/straylight-verification/slots`. Empty disables migration reads. |
| `SLOTFATHER_POLL` | How often a waiter checks its own entry, and its first interval between full looks at the queue, in seconds; default 2. |
| `SLOTFATHER_POLL_MAX` | Longest interval between a waiter's full looks (each runs `ps` under the queue lock); default 10. The interval grows by half while the reason to wait stays the same. A release grants waiters at once, so the backoff only delays reaping a crashed holder and recovery from memory pressure. |
| `SLOTFATHER_OVERTAKE_WINDOW` | Seconds after it queued during which a waiter that does not fit lets later requests that fit start first; default 600. `0` serves strictly in arrival order. |
| `SLOTFATHER_LOADAVG` | Advice test/diagnostic override: one number or `load1 load5 load15`. |
| `SLOTFATHER_MEMORY_FILE` | Memory test seam: file containing `free_swap_MB pressure_level`. |

A command receives `SLOTFATHER_LEASE` and `SLOTFATHER_COUNT` for its existing lease. Do not fabricate these values to skip queue admission.

## Verify

```sh
slotfather run -n 1 -- python3 -m unittest discover -s tests -v
```

Installer tests use isolated home directories and real live-process entries to check refusal, preservation, file guards, and repeated installation. Queue tests observe commands and queue admission using independent temporary state, controlled release files, and real child processes. They cover FIFO fairness, the overtaking window, the run history, exclusive requests, nesting, stale leases, memory guard, CLI outcomes, legacy migration, load advice, and persistent shared capacity changes.
