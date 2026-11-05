# Dashboard media

The README animation uses the real dashboard in a PTY, driven by real key input.
The queue scene is historical. Resource bars replay separate measured local
validation workloads; they are not measurements of those historical processes.
The short chart interval preserves recorded slot heights and event spacing,
aligned to the demo clock. Provenance is stored with the fixtures.

The loop pauses on the second running request, a lane with rich context and changing
metrics. It passes desktop and the first waiter quickly, then selects a waiting
lane and scrolls its details. It also scrolls a running lane before returning
to the beginning. Frozen lane context shows ticket, worktree, branch/base, stage,
receipt and review at the queue scene timestamp; later outcomes are excluded. The demo keeps queue membership fixed while clocks,
resource observations and the chart viewport advance.

## Refresh after an interface change

Install the screen-kit CLI from its checkout (`bun install && bun link`).
It needs Python, `agg` and `gif2webp` (`brew install agg webp` on macOS).

From this checkout:

```sh
slotfather run -n 1 --priority interactive -- python3 tools/dashboard-demo.py --animation
slotfather run -n 1 --priority interactive -- python3 tools/dashboard-demo.py --screenshot
```

Outputs are `assets/dashboard.webp` and `assets/dashboard.png`. Recipes live in
`tools/screens.mjs`; reusable takes and verification evidence stay under the
ignored `assets/screens/` directory. The animation is lossless WebP. The palette
is Ghostty’s built-in default; Screen Kit Mono preserves JetBrains Mono geometry
except for compact-square side bearings, so bar cells remain visibly separate.

The demo uses a short history zoom and five graph rows for six slots, exposing
fractional block heights. The normal dashboard retains its own history window
and available-space calculation. Open the interactive scene with
`python3 tools/dashboard-demo.py`; it never accesses the live queue.

See [fixture provenance and refresh commands](../tools/fixtures/README.md) before
replacing data. Inspect new media before committing it.

To refresh lane context while retaining the existing queue and metrics:

```sh
python3 tools/capture-demo-context.py
```

This reads archived lane records and writes their hashes and reconstruction limits
into fixture provenance. Capture uses only those frozen summaries and never queries
the live lanefather state. The snapshot is 176 × 50 terminal cells; the animation
uses the same layout at four frames per second with real keyboard and wheel input.
