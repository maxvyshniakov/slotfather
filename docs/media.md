# Dashboard media

The README animation uses the real dashboard in a PTY, driven by real key input.
The queue scene is historical. Resource bars replay separate measured local
validation workloads; they are not measurements of those historical processes.
The short chart interval preserves recorded slot heights and event spacing,
aligned to the demo clock. Provenance is stored with the fixtures.

The loop visits the third running request, pauses, selects a waiting request, pauses,
and returns to the beginning. The demo keeps queue membership fixed while clocks,
resource observations and the chart viewport advance.

## Refresh after an interface change

Install the screen-kit CLI from its checkout (`bun install && bun link`).
It needs Python, `agg` and `gif2webp` (`brew install agg webp` on macOS).

From this checkout:

```sh
slotfather run -n 1 -- python3 tools/dashboard-demo.py --animation
slotfather run -n 1 -- python3 tools/dashboard-demo.py --screenshot
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
