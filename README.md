# slotfather

A machine-wide queue for expensive builds, test suites, and agent work. Projects sharing a computer use one capacity budget instead of starting independent worker pools.

The executable is a standalone Python 3.9+ script using only the standard library; the optional live view (`slotfather-dashboard`) and the statistics (`slotfather-stats`) are separate scripts, kept apart so the queue code stays small. It requires POSIX process and file-lock facilities; it is exercised on macOS. macOS supplies the memory-pressure observations. Other hosts continue without observations their OS does not expose.

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
slotfather run -n 1 --label lane-650 --meta ticket=650 --meta lane=lane-650 --meta phase=verify -- ./verify.sh
slotfather run -n 1 --class interactive -- node scripts/pre-push.mjs
slotfather run -n 4 --class batch --yieldable -- ./verify.sh --checks ...   # its client runs `slotfather yield`
slotfather status
slotfather status --json --pid 4242
slotfather history 50
slotfather stats --since 24h
slotfather available
slotfather available --label tests -- python3 -m unittest discover
slotfather advise --max 4
slotfather advise --exclusive
slotfather order
```

`run` waits for capacity, holds a lease while its command runs, and returns the command's exit status. The default request is one slot. `--wait SECONDS` bounds the wait and returns 75 if capacity does not arrive; a missing executable returns 127 and a signal returns 128 plus its number.

The automatic capacity is `max(1, cores // 3)`: four slots on a 12-core machine. A request larger than the machine's capacity takes every slot. Requests are served in arrival order, except that a later request that fits may start before a waiter that does not (see Ordering): freely for `SLOTFATHER_OVERTAKE_WINDOW` seconds after that waiter queued (default 600), and after that only when the history says it will be done before the waiter can start. An all-slot request starts no later than its limit (45 minutes) plus the overrun of what passed it. Use one slot for a suite or build, two for half this 12-core machine, and `all` for a benchmark requiring isolation; a waiting `-n all` request says so on stderr.

Nested invocations inherit a live ancestor's lease rather than waiting for another one. Dead holders and reused PIDs are detected using process IDs and process start times. New leases wait while macOS reports low free swap or memory-pressure warning, except that an otherwise idle machine can start one request. With swap turned off there is no swap to run short of, so only memory pressure holds leases back.

`advise` prints a JSON worker-budget decision. It uses the lower of the one- and five-minute load averages, subtracts the CPU share explained by held slots, and reserves the rest for unleased work. The 15-minute tail is recorded but does not shrink a new run after earlier work ended. `--exclusive` assumes the caller will wait for every slot; it is advice, not a lease.

State lives in `~/.local/state/slotfather/`; `queue.log` records grants, releases, and reaping. `history.jsonl` has one JSON line per request that left the queue: label, command, directory, slots, queue, grant and end times, seconds waited and run, the outcome (`released`, `left` after giving up, or `reaped`) and, for `run`, the exit status and what the command used (see Statistics). Past 4 MB it becomes `history.jsonl.1`, replacing the previous one, so at most two files' worth is kept; readers take `.1` first. `history [N]` lists the last N (default 20). `status` lists holders and waiters.

**Lease metadata.** `run --meta key=value` (repeatable; the library's `acquire(..., meta={...})`) says who asked and why: `ticket`, `lane`, `phase`, `model`, a `session`, whatever a client finds useful. Keys are lowercase words of at most 32 characters (`a-z`, digits, `_ . -`), values printable text of at most 200, at most 16 keys; a later key replaces an earlier one. The metadata is stored on the queue entry and in the request's history line (`"meta": {...}`), and shown by `status` and `history` (`meta=ticket=650,lane=lane-650` before `cmd=`), in the dashboard's SELECTED panel, and grouped by `stats --by meta.KEY`. A nested `run` that inherits a lease runs under that lease's metadata and adds none of its own. Older clients read past the field: they list, grant (keeping it on the entry) and release such entries as before; only a request an older client reaps leaves a history line without its metadata.

**`status --json`** is the queue for programs, such as a guard that must not kill another holder's lease: `{"v": 1, "slots", "held", "free", "entries": [...], "legacy": [...]}`, each entry `{"id", "lease", "state", "count", "label", "pid", "pgid", "start", "since", "granted", "cmd", "cwd", "meta", "tree", "class", "yieldable", "yields", "yield_asked", "position"}` (the last five: see Polite preemption). `id` is the entry's name, `lease` its path (the value of `SLOTFATHER_LEASE` in the lease's command), `pid` the holder (`slotfather run`, or the process that called `acquire()`), `pgid` its process group, `tree` the holder and its live descendants. `status --json --pid PID` keeps only the entries whose tree holds PID: an empty list means the process belongs to no lease of this queue, unless launchd adopted it, in which case its environment still carries `SLOTFATHER_LEASE`. The extracted version also reads leases in Straylight's earlier queue during migration; old clients do not see new leases, so retire old wrappers before relying on mutual isolation.

## Dashboard

```sh
slotfather                           # in a terminal: the live view; ? lists keys, q quits
slotfather dashboard --interval 5    # the same, refreshing every 5 s instead of every second
slotfather dashboard --once          # one snapshot as text (add --width/--height, --color)
```

Plain `slotfather` opens the dashboard only when both its input and output are a terminal; without one, as from a script or an agent, it prints the usage and exits 2 as before.

The top line says what happens to a request made now (open, full, queued behind a larger head, or held back by the memory guard) and why. Below it one bar shows the capacity: each lease's slots joined in its own colour, free slots hollow, queued demand after it. RUNNING lists leases with how long they ran, how long they waited and the cores their process tree uses against the cores their slots stand for; a lease holding slots while nearly idle turns yellow. QUEUE lists waiters in arrival order with the slots requested ahead of each. SELECTED shows the chosen entry's command, directory and busiest processes. MACHINE shows load, memory pressure, the guard, and slots held over the last hour, rebuilt from `queue.log`. EVENTS is the log's tail, newest first.

`slotfather dashboard` runs `slotfather-dashboard` from beside the `slotfather` executable with the same interpreter; nothing of it loads during queue admission. The dashboard only reads the queue (like `status`, a refresh reaps dead entries); it never grants, cancels or kills anything.

## Statistics

```sh
slotfather stats                      # every request in the history, grouped by label and command kind
slotfather stats --since 24h --by kind
slotfather stats --since 2026-11-02 --label 'lane-*' --json
```

While `run`'s command runs, its holder looks at the command's process tree: first after half a second, then each interval half as long again, up to `SLOTFATHER_SAMPLE_MAX` seconds (default 10), and once more after the command exits. One `ps` over the machine per look; nothing per second.

- **CPU** (`cpu`, seconds of user plus system time) is the command's own rusage from `wait4`, which already holds every descendant that a parent inside the tree reaped, short-lived ones included, plus `cpu_detached`: processes that left the tree. A child whose parent exits is adopted by launchd and its CPU never reaches the command's rusage, so it stays the lease's and counts its last sampled CPU time; so does a child of launchd started during the lease whose environment carries the lease's `SLOTFATHER_LEASE` (macOS shows the environment of the user's own processes that are not Apple platform binaries). Processes born and gone between two looks outside the reaped tree are missed, as is a child that exits while its parent never waits for it and the parent then exits (launchd reaps the zombie), and work done by daemons (Docker's VM, launchd services) is not the lease's.
- **Memory**: `rss_peak_mb` and `rss_mean_mb`, the sum of resident memory over the tree at each look (time-weighted mean; shared pages count once per process). The peak is at least the largest single process from rusage.
- `wall` (seconds, monotonic), `processes` seen, `samples` taken, and the machine's `cores` and `capacity` at release. Lines carry `"v": 2`; lines written before (no `v`, no usage) still read and count in waits and runs. Nested runs record nothing of their own; leases taken by an older slotfather or through the library `acquire()` record no usage.

While leases run, holders also append a machine sample to `machine.jsonl` at most every `SLOTFATHER_MACHINE_EVERY` seconds (default 15; shared between holders by the file's age and a lock of its own, never the queue lock): load averages, the cumulative CPU ticks (busy is the change between samples), the memory-pressure level and free-memory percentage, swap, and the slots held. It rotates like the history.

`stats` groups requests by label and by command kind (or, with `--by meta.KEY`, by that metadata key; requests without it group under `-`) (program and first argument: `bun run-browser-checks.mjs`, `verify.sh --checks [95]` for a list of 95 checks) and gives, per group, the median and 90th-percentile wait and run, typical slots, mean cores used (CPU seconds over wall seconds), **cores used per slot held** (CPU seconds over wall seconds times slots) and the peak resident memory. Below it: the same over every measured lease, and the slots at which that use would fill the cores; the mean slots held while any lease ran and how often all were held; and from the machine samples, the share of time the one-minute load stood over the cores (more runnable threads than cores) and under them, the mean CPU busy share, how often a slot's worth of cores sat idle, busy cores per held slot (includes work without a lease), memory pressure and the least free swap. `--json` prints all of it; `--top N` limits the table.

**Choosing capacity.** `slotfather slots N` should make the slots' use fill the cores without holding the load over them: with *c* cores used per slot held, *cores / c* slots fill the machine. Check the machine part too: much time over the cores with all slots held means fewer slots; a slot's cores often idle while requests wait means more.

## Ordering

`grant()` runs in every client that looks at the queue (each waiter, every few seconds, and each release), and it starts the waiters `plan()` picks:

1. Waiters are taken in arrival order, moved by their class (see Classes below); each that fits starts.
2. A waiter that does not fit lets later requests that fit start first for `SLOTFATHER_OVERTAKE_WINDOW` seconds after it queued (600), as before.
3. The first waiter past its window that does not fit is the **head**. Its start is reserved: the moment the held leases' expected ends free enough slots for it. A later request that fits starts before it only if three times (*safety*) its expected run ends by that moment and by the time the head will have waited its *limit* (2700 s), or if it takes only spare slots: those the head will not need at that moment even if the leases expected to run on past it are the largest ones (EASY backfill, made independent of which lease ends first). From the limit on nothing passes: the head holds everyone back as the window order did.

Expected runs come from `history.jsonl` (its last megabyte, read only when a request fits behind a head and kept in `estimates.json` beside it until the history changes, so other processes, releases and `available` read the small file): the median of the last 20 runs of the closest earlier kind of request, trying in turn the same label and command kind (`stats`' kind: program and first argument, with a runner's `run` target, a shell's `-c` command and no `env VAR=…`), the same label family (the label up to its ticket or agent id: `lane`, `post-merge`) and kind, that family with a check list of about the same size (`verify.sh --checks` of 128 to 255 checks), the kind under any label, about that size under any label, the same label, and the same family. A request like nothing before counts as running the *fallback* (3600 s), so with the default limit it passes a head only into spare slots. A held lease past its expected run is expected to run on for the *overrun* share (half) of what it has run so far, because a run that overran its kind tends to be a long one.

`available` and `advise` answer for a request of unknown run (the fallback) unless given the command: `slotfather available --label L -- COMMAND...`, `slotfather advise [--max N] --label L -- COMMAND...`; `advise` reports the run it expected as `run_estimate` (`null`: unknown).

**Settings.** `slotfather order` shows the order and its numbers; `slotfather order backfill|window`, `--safety X` (at least 1), `--limit S` (0: no limit), `--fallback S`, `--overrun X`, and the classes' `--interactive S`, `--batch S` and `--yield-after S` (see below) save them for every client in `order.json` beside the queue, like `slots` saves capacity, and `slotfather order reset` restores the defaults. They take effect at the next look at the queue, also while it is busy. Every client's look grants for the whole queue, so a client's environment can only tighten them: `SLOTFATHER_QUEUE=window`, or a higher `SLOTFATHER_BACKFILL_SAFETY` or `_FALLBACK`, a lower `_OVERRUN` or a lower nonzero `_LIMIT`, a lower `SLOTFATHER_PRIORITY_INTERACTIVE` or `_BATCH` (classes move requests less), a higher `SLOTFATHER_YIELD_AFTER` or `0` (nobody is asked); looser values are ignored. A saved file that cannot be read gives the window order. The class knobs are written to `order.json` only when they differ from their defaults: a slotfather before them refuses a file with keys it does not know and falls back to the window order.

**When an estimate is wrong.** A request that runs longer than expected delays the head by the difference. The limit bounds it: no request passes a head after the limit, and none that is expected to end after it, so beyond the leases it waited for, the head waits only for the overrun of what passed it. An overestimate only keeps a request behind the head, as the window order did. The replays below with estimates halved, doubled and scattered (each one off by a random factor, e^N(0,1)) keep the longest wait of large requests at the current order's.

**Mixed versions.** Queue entries, `history.jsonl` and every output (`status`, `available`, `advise`, `slots`, `history`, the lease variables and nesting) are unchanged, so old and new clients share one queue. An older client grants by its own order whenever it looks, so until every client is updated the queue admits what either order allows: a new client's look starts a short request an old one would hold back, and an old client's look keeps today's behaviour for what the new order would refuse. Straylight's vendored `tools/slotfather` predates the history, so its runs are not in the estimates until it uses this one.

**Falling back.** `slotfather order window` restores the order before #643 for every client of this version at their next look, without classes or yield requests; `SLOTFATHER_QUEUE=window` does it for one client. `slotfather order --yield-after 0` keeps the classes and asks nobody to yield; `--interactive 0 --batch 0` keeps backfill without classes. Installing the previous version from a checkout of `bfa114b` (or `3e3a621`, backfill without classes) with `python3 install.py` also works; the queue and history need nothing.

### Classes

`run --class interactive|normal|batch` (the same as `--meta class=...`; the library's `acquire(meta={"class": ...})`) says how much someone waits for the request. **interactive** is work a person or agent waits on at once: a pre-push check, a single test. **batch** is work nobody waits on minute by minute: full gates, post-merge smokes, measurements (`-n all` gets no class of its own: say `--class batch`). Everything else, and every request that names no class, is **normal**.

The class moves where a waiter stands in the order, by a fixed offset that is also its ageing: an interactive waiter queues as if it came *interactive* seconds (1800) earlier, a batch one as if it came *batch* seconds (1800) later. So a batch request is ahead of every normal request that comes more than 30 minutes after it and of every interactive one that comes more than an hour after it; nothing waits behind newer work for longer than that. The window order (`slotfather order window`) has no classes.

Classes change only the order in which `plan()` takes the waiters; the window, the head and its reservation then work as before on that order, and the window and the limit count from when a request really queued. The head is the first waiter in class order that is past its window and does not fit; a request that ranks ahead of it is not passing it, so a batch head (a `-n all` measurement) can start after its limit by the runs of the requests that rank ahead of it, those that came within 30 minutes after it (normal) or an hour (interactive). An interactive request that does not fit becomes the head in its turn and gets the head's reservation.

### Polite preemption

A holder that opted in (`run --yieldable`, `acquire(..., yieldable=True)`) can be asked to give its slots back for a while; it is never stopped, killed or signalled, and a holder that did not opt in is never asked. An **interactive** waiter asks when it has waited *yield_after* seconds (120), does not fit, and by the held leases' expected ends would not get its slots within another *yield_after*. It asks only yieldable holders of a lower class that have held their slots *yield_after* seconds since their last grant and have yielded fewer than two times; only as many as it needs (first those already asked, then batch before normal, then the one expected to run longest); and only when the queue would then start it, with the asked holders back among the waiters. Every look decides again: a request no longer needed (the waiter started or left) is withdrawn.

The request is a file beside the holder's queue entry, `<entry>.yield`, whose path the lease's command gets in `SLOTFATHER_YIELD`; it holds `{"by", "label", "class", "count", "at"}` of the waiter that asked. `slotfather yield` from inside the lease (the library's `lease.yield_slots()`) yields if asked and the waiter still waits: the lease goes back to the queue (the same entry and holder, state `waiting`) just behind that waiter, the queue grants at once so the waiter starts, and the command returns when the lease is granted again with its slots. Without a request it returns at once, saying `no yield requested`; it exits 0 either way. A nested `slotfather run` under a yielded lease waits until the lease is back. `status` shows `yieldable`, `asked-to-yield-for=LABEL` and `yielded=N`; `status --json` adds `class`, `yieldable`, `yields`, `yield_asked` (the request or `null`) and `position` (a waiter's place in the order the queue serves); the dashboard's SELECTED panel shows the request; `queue.log` records asks, withdrawals and yields. The history line of a lease that yielded carries `yields` and `yielded` (seconds back in the queue); its wait and run count from its first grant, and `stats` leaves the yielded time out of cores per slot.

#### What a client implements to be preemptible

Straylight's `tools/verify.py` and `tools/heavy.py` and Sid-Kit's `scripts/heavy.sh` and `scripts/verify.mjs` are the intended clients; they are not changed here.

1. **Opt in when taking the lease**: `slotfather run --yieldable --class batch ... -- COMMAND` (Sid-Kit's `heavy.sh`), or, for a client that holds its lease in-process (Straylight's `verify.py` through `tools/slot_lease.py`), `acquire(count, label, command, yieldable=True, meta={"class": "batch", ...})`.
2. **Look for a request between steps**, whenever a step ends and none of the lease's own work runs: Straylight between checks, Sid-Kit between `verify.mjs` steps. The look is one `stat`: `[ -e "$SLOTFATHER_YIELD" ]` in a shell, `os.path.exists(os.environ.get("SLOTFATHER_YIELD", ""))` in Python, `lease.yield_requested()` in-process. With parallel jobs (`--jobs N`), stop starting new steps once a request is seen and let the running ones finish first.
3. **Yield**: run `slotfather yield` (in-process `lease.yield_slots()`); it blocks while the lease is back in the queue. Then go on with the next step, with the same slots. Nothing has to be undone: a yield happens between steps.
4. **Never** yield in the middle of a step or a measurement (a measurement does not opt in), never kill or signal a process to make room, and do not start new work under the lease while yielding.

A client may ignore a request: the waiter then waits as it would have, and the request is withdrawn once it is no longer needed.

**Mixed versions.** A slotfather before classes (`3e3a621` and earlier, Straylight's vendored copy) ignores them and the requests: its looks grant in arrival order, so until every client runs this version a waiter may start as either order allows. It lists a yielded lease as a waiter and may grant it again before the waiter it yielded to; `slotfather yield` then returns as usual. Since the yield itself grants at once, the waiter normally starts before an older client looks.

### How the order was chosen

`tools/replay.py` replays the recorded queue through each order in an event simulation: arrivals, ends, requests that gave up (after the wait they had), and a look every 10 s while anyone waits; the memory guard is ignored. `window` and `backfill` run the queue's own `plan()`. Requests whose slotfather wrote no history (Straylight's vendored copy) are taken from `queue.log` and placed by their queue sequence number; one that gave up and starts in a replay runs the median of its kind.

Recorded queue of 2026-11-02 14:21 to 2026-11-03 01:47 UTC (232 requests on 6 slots: 189 from the history, 32 only in `queue.log`, 11 live). Replayed through the current order, the 189 recorded waits come out at 26.0 h against 27.7 h recorded, mean absolute error 50 s, 94% within 60 s; the misses are periods when the memory guard held the queue. Waits in seconds; *≥3* is requests of three or more slots; *idle* is slot-hours free while someone waited, *fit-idle* while a waiter would have fitted them.

| Order | total h | mean | p90 | max | ≥3 mean | ≥3 max | idle | fit-idle |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| window 600 s (before #643) | 33.4 | 525 | 2143 | 4083 | 1670 | 3911 | 13.7 | 12.3 |
| strict FIFO (window 0) | 53.9 | 847 | 3314 | 5043 | 1990 | 4871 | 14.1 | 13.7 |
| **backfill (shipped)** | **26.5** | **417** | **1911** | **4083** | **1569** | **3911** | **12.4** | **9.2** |
| backfill without the window | 37.9 | 595 | 2885 | 4160 | 1595 | 3988 | 12.7 | 9.8 |
| backfill, safety 1.5 | 28.0 | 440 | 2080 | 4481 | 1626 | 4156 | 12.2 | 8.0 |
| backfill without the limit | 25.1 | 395 | 1915 | 4570 | 1676 | 4438 | 12.2 | 8.0 |
| backfill for `-n all` heads only | 26.7 | 419 | 1911 | 4083 | 1569 | 3911 | 12.4 | 9.3 |
| short class (≤ 5 min jumps 30 min ahead) + window | 26.7 | 420 | 1886 | 4059 | 1527 | 3520 | 13.5 | 10.2 |
| short class + backfill | 24.6 | 386 | 1882 | 4059 | 1461 | 3520 | 12.5 | 9.1 |
| backfill knowing every run exactly | 22.3 | 351 | 1806 | 4029 | 1571 | 3857 | 12.7 | 9.0 |
| backfill, estimates halved | 25.8 | 405 | 1911 | 4083 | 1569 | 3911 | 12.0 | 8.0 |
| backfill, estimates doubled | 26.5 | 417 | 1911 | 4083 | 1569 | 3911 | 12.4 | 9.2 |
| backfill, estimates scattered | 25.8 | 406 | 1887 | 3911 | 1570 | 3911 | 12.3 | 8.8 |
| the same without the limit | 26.4 | 416 | 1845 | 6054 | 1657 | 6054 | 12.6 | 8.3 |

Backfill cuts the total wait by a fifth and the time slots sat idle while a waiter fitted them by a quarter, without a longer wait for any large request. Requests that gave up waiting drop from 38 to 20. In the #643 incident (requests that arrived 00:48 to 01:40, behind a 6-slot request blocked by one overrunning 1-slot lane check) the total wait falls from 7.3 h to 4.2 h; with exact run times it would have been 2.4 h, so better estimates (more v2 history, Straylight on this version) are the next gain.

Rejected:

- **Backfill without the overtaking window** (pure EASY): every waiter that does not fit becomes a head at once and long requests stop passing it; worse than today (37.9 h).
- **No limit** waits slightly less on this trace (25.1 h) but lets the wait of a large request grow with every misestimated run (6054 s with scattered estimates). A **lower safety factor** (1.5) admits more of those overruns (4481 s).
- **Backfill only around `-n all`**: the same numbers as for every head, with a special case more.
- **A short-job class with ageing** (a request expected under 5 minutes queues as if it came 30 minutes earlier): on top of backfill it saves a further 7% and the longest large wait, but it reorders the queue that `status` shows, and old clients ignore the class, so its effect would depend on which client looks. Worth revisiting once every client runs this version.
- **Booking by measured use** (packing slots by the cores a kind uses) needs a day of v2 usage lines; leases used 0.4 cores per slot in #635's first sample, so capacity, not order, is the lever there.
- A **per-command `--estimate`** flag: callers do not know their run time better than the history.

#### How the classes were chosen

The same replay, with a class for each recorded request from its label and command (`classify()`: interactive for pre-push checks, one test file or name pattern, a single tool test; batch for gates of 50 or more checks, post-merge smokes, Sid-Kit's `verify.mjs`, old-client lane gates, `-n all` and labels that say measure, baseline, profile, stress, repro or probe; normal otherwise) and gates of Straylight and Sid-Kit taken as yieldable. An asked holder yields at the end of its step, a uniform time up to 300 s. Queue of 2026-11-02 14:21 to 2026-11-03 03:30 UTC (295 requests on 6 slots; 51 interactive, 156 normal, 78 batch); the current order replays the recorded waits within 60 s for 89% of them. Waits in seconds; *batch max* includes the time a batch request yielded.

| Order | total h | p90 | ≥3 max | interactive mean | p90 | max | normal mean | batch mean | batch max | yields |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| window (before #643) | 40.0 | 1881 | 3911 | 631 | 2132 | 3911 | 244 | 944 | 3729 | 0 |
| backfill (`3e3a621`) | 30.3 | 1560 | 3911 | 381 | 884 | 3911 | 145 | 860 | 3729 | 0 |
| **classes 1800/1800 + yields after 120 s (shipped)** | **26.2** | **1324** | **3880** | **155** | **358** | **1895** | **92** | **925** | **3880** | **6** |
| classes only | 26.8 | 1324 | 3880 | 184 | 596 | 1895 | 97 | 925 | 3880 | 0 |
| yields only | 29.9 | 1560 | 3911 | 361 | 884 | 3911 | 141 | 860 | 3729 | 5 |
| interactive ahead only | 33.0 | 1597 | 3704 | 275 | 864 | 2386 | 181 | 978 | 3849 | 0 |
| batch behind only | 28.2 | 1464 | 3880 | 267 | 828 | 1895 | 98 | 932 | 3880 | 0 |
| offsets 600 s | 30.0 | 1597 | 3957 | 235 | 884 | 1895 | 133 | 966 | 3957 | 6 |
| batch 3600 s behind | 26.6 | 1378 | 4829 | 152 | 296 | 1895 | 60 | 1007 | 4829 | 5 |
| normal waiters ask too | 25.7 | 1192 | 3880 | 152 | 358 | 1895 | 81 | 925 | 3880 | 9 |
| steps up to 600 s | 26.6 | 1324 | 3880 | 166 | 495 | 1895 | 97 | 925 | 3880 | 5 |
| shipped, estimates scattered | 27.5 | 1373 | 4428 | 171 | 612 | 1895 | 97 | 965 | 4428 | 5 |
| backfill, estimates scattered | 29.6 | 1560 | 3911 | 380 | 884 | 3911 | 134 | 850 | 3729 | 0 |
| a short class by estimate (≤ 5 min, 30 min ahead) | 28.3 | 1507 | 3520 | 322 | 823 | 3247 | 110 | 875 | 3770 | 0 |

Classes with yields cut the total wait by 14% and interactive waits by more than half (mean 381 to 155 s, p90 884 to 358 s, longest 3911 to 1895 s); normal work waits less too. Batch work pays for it, inside the hour the offsets bound: its mean wait grows by 8% (860 to 925 s) and its longest by 4% (3729 to 3880 s), by 19% with scattered estimates. Most of the gain comes from holding batch work back; yields add little to the total but halve what classes leave of the interactive p90 (596 to 358 s), at 6 yields in 13 hours that gave back 0.07 slot-hours. In the #643 incident window the total wait stays at 4.5 h: there the head was a measurement that every class waits behind (in the fixture copied during the incident, with the runs then still going estimated, it falls from 4.6 to 3.2 h).

Rejected:

- **Interactive ahead only** waits longer than backfill alone (33.0 h): checks that jump ahead of gates' heads delay those heads and everyone behind them. **Smaller offsets** (600 s) gain nothing (30.0 h); **batch 3600 s behind** lets the longest batch wait grow to 4829 s for 0.4 h less in total.
- **Normal waiters asking too**: 0.5 h less in total for half again as many yields, of gates that nobody waits on at a terminal.
- **A short class by estimate** (no client change needed; the rejected row of the backfill table): 28.3 h and an interactive mean twice the shipped one; a kind's first run is classed wrongly, and old clients ignore it as they ignore classes.
- **Shrinking instead of yielding** (`yield --keep N`): the replay cannot tell how long a gate runs on fewer slots (leases used about 0.4 cores per slot in #635's sample, so a shrink might cost nothing or the gate's whole parallelism), so it is not offered.
- **A signal to the holder** (SIGUSR1, SIGTERM) instead of a file: a holder that installed no handler dies of it; a file asks only clients that look. **Stopping** a holder (SIGSTOP) or killing it: never.
- **Batch and normal waiters asking**, and asks for `-n all`: preemption is for work someone is waiting on.

Re-run after more history: `python3 tools/replay.py` (or `--since/--until` for a period, `--json`).

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
| `SLOTFATHER_OVERTAKE_WINDOW` | Seconds after it queued during which a waiter that does not fit lets later requests that fit start first; default 600. With `window` and `0`, requests are served strictly in arrival order. |
| `SLOTFATHER_QUEUE` | `window`: this client grants by the order before #643 whatever `slotfather order` saved (see Ordering). |
| `SLOTFATHER_BACKFILL_SAFETY`, `_LIMIT`, `_FALLBACK`, `_OVERRUN` | Tighten this client's backfill below what `slotfather order` saved (higher safety or fallback, lower overrun or nonzero limit); looser values are ignored. |
| `SLOTFATHER_PRIORITY_INTERACTIVE`, `_BATCH` | Move this client's classes less than `slotfather order` saved (seconds; lower values only; 0: not at all). |
| `SLOTFATHER_YIELD_AFTER` | Ask holders to yield later than saved (seconds; higher values only), or `0`: this client's looks ask nobody. |
| `SLOTFATHER_SAMPLE_MAX` | Longest interval, in seconds, between a holder's looks at its command's process tree; default 10. |
| `SLOTFATHER_MACHINE_EVERY` | Seconds between machine samples while leases run; default 15. |
| `SLOTFATHER_LOADAVG` | Advice test/diagnostic override: one number or `load1 load5 load15`. |
| `SLOTFATHER_MEMORY_FILE` | Memory test seam: file containing `free_swap_MB pressure_level`. |

A command receives `SLOTFATHER_LEASE` and `SLOTFATHER_COUNT` for its existing lease, and a yieldable lease's command `SLOTFATHER_YIELD`, the path of its yield request (see Polite preemption). Do not fabricate these values to skip queue admission.

## Verify

```sh
slotfather run -n 1 -- python3 -m unittest discover -s tests -v
```

Installer tests use isolated home directories and real live-process entries to check refusal, preservation, file guards, and repeated installation. Queue tests observe commands and queue admission using independent temporary state, controlled release files, and real child processes. They cover FIFO fairness, the overtaking window, backfill past a head (through real processes, and with a client of the previous version on the same queue), the run history and its rotation, exclusive requests, nesting, stale leases, memory guard, CLI outcomes, legacy migration, load advice, and persistent shared capacity changes. Class tests decide `plan()` and `yield_asks()` on fixed queues (offsets and ageing, a yielded lease behind its asker, who asks whom and when not), check the class settings and what a client's environment may change, run the whole protocol through real processes (a yieldable gate yields between steps and resumes, a holder that did not opt in is never asked, a request is shown and withdrawn when its waiter leaves), and run the previous version beside it; the replay test checks that classes with yields wait less on the recorded queue without holding batch work back beyond the offsets, and that a gate yields to a pre-push check and resumes in a small trace. Metadata tests run requests with `--meta` through status, `status --json` (lease, process group, tree, `--pid`), the history, a nested run, the dashboard, `stats --by meta.KEY`, and the previous version on the same queue. Usage tests account fake process tables (reaped children, orphans that outlive their parent, launchd children carrying the lease, reused pids, memory), measure a real tree with a burning orphan under `run`, and check `stats` over old and new history lines and machine samples. Order tests decide `plan()` on fixed queues (window, reservation, fallback, overrun, limit, spare slots when leases end out of order), check the estimates, their cache and command kinds, the saved settings and what a client's environment may change, and `available`/`advise` for a given command; the replay test runs the recorded queue of 2026-11-02/03 in `tests/fixtures/replay` (commands cut to their kind) and checks that the current order reproduces the recorded waits and that backfill waits less without a longer wait for large requests, also with scattered estimates; a small trace checks that a long request does not take a slot the head needs when leases end out of order.
