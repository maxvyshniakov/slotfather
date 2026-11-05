# Navigation scene

`navigation.json` reconstructs an actual queue interval from completed local Slotfather
history and the nearest machine sample. See its `provenance` for dates, hashes and limits.
Home paths are shortened and PIDs remapped. No live queue is read or modified by the demo.

The queue stays fixed so navigation is the focus. Clocks advance normally and the histogram
uses a short zoom (2 seconds per column) with a moving history window. The chart
is five rows tall for six slots, so the real renderer uses fractional block heights.
`chart-history.json` is a varied recorded interval from completed queue history;
its timestamps are aligned to the demo clock without changing heights or spacing.
Refresh it with `slotfather run -n 1 -- python3 tools/capture-chart-history.py`.

`metrics.json` contains measured CPU, RSS, process counts and host observations from separate
local validation workloads. The demo replays these profiles over the historical queue; they
are illustrative and are not measurements of the historical request processes. CPU uses real
elapsed CPU-time deltas, including completed validation children. No synthetic CPU waveform
or invented completion events are used. Process details are omitted because these workload
profiles do not belong to the historical process trees.

To replace the metrics recording:

```sh
slotfather run -n 3 -- python3 tools/record-demo-metrics.py
```

It runs three local check workloads in isolated process groups, captures about 16 seconds of
observations and terminates its workers. It never enqueues or modifies the real queue.

To replace the fixture from your own completed local queue history, run
`python3 tools/capture-demo-fixture.py` from the checkout and review the resulting JSON
before using it in public media. The capture tool excludes secret-bearing commands.

Presentation names follow the current glossary. Caller labels, metadata, command arguments
and recorded observations keep their source values; version-1 metrics retain the `lanes`
collection key as a compatibility alias.
