# Reading the dashboard

`slotfather` opens the dashboard when stdin and stdout are terminals. Otherwise it
prints usage; use `slotfather dashboard --once` for a scripted snapshot.

```sh
slotfather dashboard --interval 5
slotfather dashboard --once --width 156 --height 43 --color
```

## Navigation

| Input | Action |
| --- | --- |
| ↑ / ↓ or j / k | Select an active or waiting lane. |
| Click a lane | Select that row. |
| Mouse wheel | Scroll the table under the pointer. |
| g / G, Home / End | First or last lane. |
| p or Space | Pause/resume updates. |
| r | Refresh. |
| m | Toggle native text selection; copying freezes updates and disables app mouse handling. |
| ? | Help. |
| q | Quit. |

Only the selected lane has a triangle and a soft background. The blank separator
between active and waiting lanes is not selectable. Overflow counts appear in
the tables’ top and bottom padding rows.

## Lanes and details

Time is elapsed run time for an active lane and elapsed wait time for a pending
one. CPU use/share is **cores used / cores represented by the lease’s slots**;
it is an observation, not a hard CPU limit. Yellow indicates a nearly idle
holder; red indicates use well above its share. After sustained low activity,
status includes `idle for …`; this is not a diagnosis of a hung process.

`owner pid` is the process holding the lease. The process table includes that
owner and its descendants, busiest first. Each row’s CPU is a percentage:
100% means one core. Memory is summed resident memory, so shared pages can be
counted in more than one process.

For the waiting head, `start est` is an estimate, `backfill by` says when work
must finish to pass it, and `pass until` is the deadline for passing. These fields
appear only when the scheduler has the corresponding estimate.

## Machine

- **memory / swap status:** white is normal, red is a condition that can block
  additional leases. The memory bar also reflects memory health.
- **unleased cores:** estimated load not explained by the CPU share of held slots.
  It uses the lower of the one- and five-minute load averages.
- **admission:** slots a generic new request can get immediately, considering
  queue order and memory checks. It is not a count of worker processes.
- **loaded … of … cores:** one-minute load average relative to the core count.
  Load counts runnable and waiting tasks; it is not CPU busy percentage and can
  exceed the number of cores.
- **memory / swap bars:** observed used capacity. Missing observations are shown
  as unavailable rather than as zero.

The histogram shows peak occupied slots per fixed time bucket over roughly an
hour. Blue-to-orange colour follows the capacity scale; `s/col` gives the time
represented by each column. The newest bucket is incomplete. Its legend gives
average occupancy, peak slots and the share of time capacity was full. Whole
slots can still produce fractional block heights when graph height and capacity
differ. Historical over-capacity values are retained in the legend.

Refreshing can reap dead queue entries, like `status`. Selecting, pausing and
scrolling never grants a lease, cancels work or kills a process.
