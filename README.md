# slotfather

A machine-wide FIFO queue for expensive builds, test suites, and agent work. Projects sharing a computer use one capacity budget instead of starting independent worker pools.

The executable is a standalone Python 3 script using only the standard library. It requires POSIX process and file-lock facilities; it is exercised on macOS. macOS supplies the memory-pressure observations. Other hosts continue without observations their OS does not expose.

## Install

```sh
gh repo clone maxvyshniakov/slotfather
cd slotfather
mkdir -p ~/.local/bin
install -m 755 slotfather ~/.local/bin/slotfather
```

Put `~/.local/bin` on your `PATH`. All participating projects must use the same state directory; saved capacity is shared through that directory. Updating the executable preserves the queue and running leases.

## Use

```sh
slotfather run -n 1 --label tests -- python3 -m unittest discover
slotfather run -n 2 -- make build
slotfather run -n all -- ./benchmark.sh
slotfather status
slotfather available
slotfather advise --max 4
slotfather advise --exclusive
```

`run` waits for capacity, holds a lease while its command runs, and returns the command's exit status. The default request is one slot. `--wait SECONDS` bounds the wait and returns 75 if capacity does not arrive; a missing executable returns 127 and a signal returns 128 plus its number.

The automatic capacity is `max(1, cores // 3)`: four slots on a 12-core machine. A request larger than the machine's capacity takes every slot. Arrival order is strict: a queued all-slot request is never overtaken by later smaller requests. Use one slot for a suite or build, two for half this 12-core machine, and `all` for a benchmark requiring isolation.

Nested invocations inherit a live ancestor's lease rather than waiting for another one. Dead holders and reused PIDs are detected using process IDs and process start times. New leases wait while macOS reports low free swap or memory-pressure warning, except that an otherwise idle machine can start one request.

`advise` prints a JSON worker-budget decision. It uses the lower of the one- and five-minute load averages, subtracts the CPU share explained by held slots, and reserves the rest for unleased work. The 15-minute tail is recorded but does not shrink a new run after earlier work ended. `--exclusive` assumes the caller will wait for every slot; it is advice, not a lease.

State keeps its existing path `~/.local/state/machine-slots/` so active leases survive the command rename; `queue.log` records grants, releases, and reaping. `status` lists holders and waiters. The extracted version also reads leases in Straylight's earlier queue during migration; old clients do not see new leases, so retire old wrappers before relying on mutual isolation.

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
| `SLOTFATHER_MIN_FREE_SWAP_MB` | Free-swap floor; default 512 MB. |
| `SLOTFATHER_LEGACY_DIRS` | Colon-separated old queues; defaults to `~/.local/state/straylight-verification/slots`. Empty disables migration reads. |
| `SLOTFATHER_POLL` | Queue poll interval in seconds; default 2. |
| `SLOTFATHER_LOADAVG` | Advice test/diagnostic override: one number or `load1 load5 load15`. |
| `SLOTFATHER_MEMORY_FILE` | Memory test seam: file containing `free_swap_MB pressure_level`. |

A command receives `SLOTFATHER_LEASE` and `SLOTFATHER_COUNT` for its existing lease. Do not fabricate these values to skip queue admission.

## Verify

```sh
slotfather run -n 1 -- python3 -m unittest discover -s tests -v
```

The tests observe commands and queue admission using independent temporary state, controlled release files, and real child processes. They cover FIFO fairness, exclusive requests, nesting, stale leases, memory guard, CLI outcomes, legacy migration, load advice, and persistent shared capacity changes.
