# Scheduler experiments

Historical measurements and tuning decisions; current behavior is documented in [scheduling](scheduling.md).

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
| backfill, estimates scattered | 29.8 | 1560 | 3911 | 382 | 884 | 3911 | 136 | 852 | 3729 | 0 |
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

The same replay, with a class for each recorded request from its label and command (`classify()`: interactive for pre-push checks, one test file or name pattern, a single tool test; batch for gates of 50 or more checks, post-merge smokes, Sid-Kit's `verify.mjs`, old-client lane gates, `-n all` and labels that say measure, baseline, profile, stress, repro or probe; normal otherwise) and gates of Straylight and Sid-Kit taken as yieldable. An asked holder yields at the end of its step, a uniform time up to 300 s. Queue of 2026-11-02 14:21 to 2026-11-03 03:30 UTC (328 requests on 6 slots); the current order replays the recorded waits within 60 s for 89% of them. Waits in seconds; *batch max* includes the time a batch request yielded.

| Order | total h | p90 | ≥3 max | interactive mean | p90 | max | normal mean | batch mean | batch max | yields |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| window (before #643) | 40.0 | 1881 | 3911 | 631 | 2132 | 3911 | 244 | 944 | 3729 | 0 |
| backfill (`3e3a621`) | 30.5 | 1560 | 3911 | 382 | 884 | 3911 | 147 | 861 | 3729 | 0 |
| **classes 1800/1800 + yields after 120 s (shipped)** | **26.3** | **1324** | **3880** | **157** | **358** | **1895** | **93** | **926** | **3880** | **6** |
| classes only | 26.9 | 1324 | 3880 | 186 | 596 | 1895 | 98 | 926 | 3880 | 0 |
| yields only | 29.9 | 1560 | 3911 | 361 | 884 | 3911 | 142 | 861 | 3729 | 5 |
| interactive ahead only | 33.1 | 1597 | 3704 | 275 | 864 | 2386 | 184 | 978 | 3849 | 0 |
| batch behind only | 28.3 | 1464 | 3880 | 269 | 828 | 1895 | 99 | 932 | 3880 | 0 |
| offsets 600 s | 30.1 | 1597 | 3957 | 236 | 884 | 1895 | 133 | 967 | 3957 | 6 |
| batch 3600 s behind | 27.5 | 1378 | 4829 | 159 | 354 | 1895 | 79 | 1008 | 4829 | 6 |
| normal waiters ask too | 25.8 | 1192 | 3880 | 154 | 358 | 1895 | 81 | 926 | 3880 | 9 |
| steps up to 600 s | 26.6 | 1324 | 3880 | 167 | 495 | 1895 | 97 | 926 | 3880 | 5 |
| shipped, estimates scattered | 27.5 | 1373 | 4428 | 171 | 612 | 1895 | 97 | 965 | 4428 | 5 |
| backfill, estimates scattered | 29.8 | 1560 | 3911 | 382 | 884 | 3911 | 136 | 852 | 3729 | 0 |
| a short class by estimate (≤ 5 min, 30 min ahead) | 28.8 | 1507 | 3520 | 348 | 884 | 3247 | 112 | 876 | 3770 | 0 |

Classes with yields cut the total wait by 14% (30.5 to 26.3 h) and interactive waits by more than half (mean 382 to 157 s, p90 884 to 358 s, longest 3911 to 1895 s); normal work waits less too. Batch work pays for it, inside the hour the offsets bound: its mean wait grows by 8% (861 to 926 s) and its longest by 4% (3729 to 3880 s), by 19% with scattered estimates. Most of the gain comes from holding batch work back; yields add little to the total but halve what classes leave of the interactive p90 (596 to 358 s), at 6 yields in 13 hours that gave back 0.07 slot-hours. In the #643 incident window the total wait stays at 4.5 h: there the head was a measurement that every class waits behind (in the fixture copied during the incident, with the runs then still going estimated, it falls from 4.6 to 3.2 h).

Rejected:

- **Interactive ahead only** waits longer than backfill alone (33.0 h): checks that jump ahead of gates' heads delay those heads and everyone behind them. **Smaller offsets** (600 s) gain nothing (30.0 h); **batch 3600 s behind** lets the longest batch wait grow to 4829 s and waits longer in total (27.5 h).
- **Normal waiters asking too**: 0.5 h less in total for half again as many yields, of gates that nobody waits on at a terminal.
- **A short class by estimate** (no client change needed; the rejected row of the backfill table): 28.8 h and an interactive mean twice the shipped one; a kind's first run is classed wrongly, and old clients ignore it as they ignore classes.
- **Shrinking instead of yielding** (`yield --keep N`): the replay cannot tell how long a gate runs on fewer slots (leases used about 0.4 cores per slot in #635's sample, so a shrink might cost nothing or the gate's whole parallelism), so it is not offered.
- **A signal to the holder** (SIGUSR1, SIGTERM) instead of a file: a holder that installed no handler dies of it; a file asks only clients that look. **Stopping** a holder (SIGSTOP) or killing it: never.
- **Batch and normal waiters asking**, and asks for `-n all`: preemption is for work someone is waiting on.

#### Waiters that give up, and the safety factor (2026-11-03)

The queue of 2026-11-02 14:21 to 2026-11-03 14:14 UTC (667 requests on 6 slots: 630 from the history), and the part of it from 01:41, when backfill had been installed (no client sent a class, so the rows without classes are what ran). Twice that day a Straylight lane's `-n all --wait 1800` rerun waited behind a 1-slot gate that ran on for nearly two hours; as the head it held fitting smokes back for twenty minutes each, then gave up without running.

The history before `until` does not say which requests had a wait, and a replay that gives one only to those that gave up never shows a head given up on that would have started. So `--families` gives every request of a label family that ever gave up that family's longest give-up as its wait (unless it waited longer and so had none), and it leaves then if not granted. Totals in hours, the longest wait of a request of three or more slots in seconds; *scattered* is the mean (and range) over six random errors per request (e^N(0,1)); halved and doubled scale every estimate.

| Order (`--families`) | from 01:41: exact | scattered | halved | doubled | over the day: exact | scattered | halved | doubled | ≥3 max, halved |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `1708378` (installed) | 17.4 | 18.3 (16.8–20.9) | 20.9 | 18.3 | 44.2 | 44.1 (42.8–46.3) | 47.2 | 45.1 | 3911 |
| reaped runs left out | 17.4 | 18.3 (16.8–20.9) | 20.9 | 18.3 | 44.2 | 44.1 (42.8–46.3) | 47.2 | 45.1 | 3911 |
| **and a head that gives up first holds nobody (shipped)** | **16.7** | **17.8 (15.8–20.2)** | **20.9** | **17.1** | **43.5** | **43.5 (41.1–45.6)** | **47.2** | **43.9** | **3911** |
| the same, safety 2 | 18.2 | 16.6 (15.1–19.5) | 17.4 | 16.3 | 43.9 | 41.1 (39.7–42.9) | 41.7 | 42.5 | 4187 |

The Mac's history holds no reaped run with a time, so leaving them out changes nothing here. A head that gives up first saves 2–4% and never adds a give-up (8 from 01:41 and 29 over the day in every row). Safety 2 waits less whenever the estimates are off (5–12%), more with the medians as they were (18.2 against 16.7 h from 01:41), and with estimates halved lets the longest wait of a large request grow (4187 against 3911 s). It is not saved as the default; `slotfather order --safety 2` sets it.

Re-run after more history: `python3 tools/replay.py` (or `--since/--until` for a period, `--families`, `--json`).
