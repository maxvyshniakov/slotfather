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
| ↑ / ↓ or j / k | Select a running or waiting request. |
| Click a request | Select that row. |
| Mouse wheel | Scroll the table under the pointer. |
| g / G, Home / End | First or last request. |
| p or Space | Pause/resume updates. |
| r | Refresh. |
| m | Toggle native text selection; copying freezes updates and disables app mouse handling. |
| ? | Open scrollable help. |
| q | Quit. |

Only the selected request has a triangle and a soft background. The blank separator
between running and waiting requests is not selectable. Overflow counts appear in
the tables’ top and bottom padding rows.

With help open, arrows / j / k and the wheel scroll its text; PgUp / PgDown or
Space scroll a page, and Home / End or g / G jump to the ends. `?` / Esc close
help; q still quits the dashboard. Help navigation preserves request selection and
the tables' scroll positions.

## Requests and details

Time is elapsed run time for a running request and elapsed wait time for a pending
one. CPU use/share is **cores used / cores represented by the lease’s slots**;
it is an observation, not a hard CPU limit. Yellow indicates a nearly idle
holder; red indicates use well above its share. After sustained low activity,
status includes `idle for …`; this is not a diagnosis of a hung process.

`owner pid` is the process holding the lease. The process table includes that
owner and its descendants, busiest first. Each row’s CPU is a percentage:
100% means one core. Memory is summed resident memory, so shared pages can be
counted in more than one process.

For the first waiting request:

- `start est` is its estimated start. Later work may backfill only if its
  predicted finish, including the safety margin, is before this time, or it
  uses only slots the protected request will not need.
- `pass until` is the end of the current overtaking window. With `start est`
  shown, no later request may pass after this deadline. Without it, stricter
  backfill rules may follow the initial window.

For example, `start est 10:30` and `pass until 10:20` allow a later request at
10:10 only if it is expected to finish before 10:30 with the safety margin.
At 10:21 it cannot pass. These are policy deadlines and estimates, not a promised
start. The help explains them alongside CPU, load, memory, admission and history.

## Machine

- **memory / swap status:** white is normal, red is a condition that can block
  additional leases. The memory bar also reflects memory health.
- **unleased load:** estimated load not explained by the CPU share of held slots.
  It uses the lower of the one- and five-minute load averages.
- **admission:** slots a generic new request can get immediately, considering
  queue order and memory checks. It is not a count of worker processes.
- **load 1m:** one-minute system load, shown beside the core count.
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

## Lane context and details scrolling

REQUEST DETAILS has one scroll position for its facts, lane context and process
rows. Wheel anywhere inside the widget to scroll; the process table has no
independent viewport. Selecting another request resets the details to the top.

When the caller supplies `meta.lane` and `meta.lanefather.state`, the selected
request can show lane fields: project, phase, lane stage, branch, base,
ticket, receipt, review, claims and dependencies. Lane stage and request
status describe different lifecycles. Ticket numbers use OSC 8 hyperlinks;
open them with the terminal's link gesture, usually Cmd-click on macOS.
The worktree path appears before command as a muted folder link. Wrapped path
lines share the same destination. Plain-text snapshots contain labels and paths
without escape sequences.

Slotfather queries the installed `lanefather show ID --state-dir DIR --json`
only for selected work and caches the response for five seconds. The state
directory and lane ID together identify the lane. Older requests with only a
lane ID can resolve their project from the recorded working directory; lanefather
verifies that the resulting lane belongs to that repository. Missing references, an older
CLI, timeouts and unsupported responses appear as unavailable lane details;
they do not prevent viewing the request or its processes. No GitHub network
request is needed: ticket URLs come from the repository's GitHub origin.

Priority and optional ticket are request fields, displayed separately from caller
metadata. Known context gets named rows: phase, kind, check, revision, run on,
and session. Only custom fields remain in `meta`. A lane's `stage` is its
workflow lifecycle; a request's `phase` describes the current execution.
`receipt` shows the saved verification result and whether its tree is current;
`review` is the independent code review and its unresolved findings. The
occupancy histogram is five terminal rows high.
