# Task: favicon

Conductor-dispatched one-off step (manifest `manifests/agent-conductor-favicon.toml`,
run `favicon2`). This was the *second* dispatch - the first run's icon change was
swallowed by a sibling session's mid-run `git add -A` (see AGENTS.md trap 10) and
never landed as a coherent commit. A *third* dispatch (run `20261014-024259-favicon2`,
worktree `new-favicon`) spawned after the merge and found the work already
landed - a no-op re-verification only.

**Status:** done
**Started:** 2026-10-14   **Last touched:** 2026-10-14

## Objective

Replace the dashboard page icon (`<link rel="icon">` data-URI inside `_DASH_HTML`
in `conductor.py`) with a cleaner conductor glyph; keep viewBox 64x64, the
data-URI mechanics and the dark `#13161f` tile.

## Decisions already made

- Glyph = baton (top, `#e6edf3`, thin diagonal + hand knob) over four
  "musicians" - a head circle + rounded body bar each, on a shared y=52
  baseline, heights arcing up toward center. Reads as both an orchestra row
  and a bar chart at 16px.
- Colors reuse the dashboard status palette: `#58a6ff`, `#bc8cff`, `#d29922`,
  `#3fb950`.
- Verified visually: rasterized old-vs-new side by side at 128/32/16px via
  headless Edge (`--headless=new --screenshot`) - scratch/preview.png.

## Phases

### Phase 1 - done

- [x] Design new glyph, preview at favicon sizes
- [x] Swap data-URI in `_DASH_HTML`
- [x] `python run_tests.py` -> 48 tests, OK
- [x] Third dispatch (run `20261014-024259-favicon2`): glyph confirmed in
  place, `python run_tests.py` -> 50 tests, OK. No code change made.

## Where it stopped

Done; nothing to resume. Third dispatch was a no-op - the manifest was
re-run after the second run's merge had already landed the glyph. Icon line
is `conductor.py` `_DASH_HTML` (the `<link rel="icon" ...>` line).

## Verification

`python run_tests.py` (50/50 green 2026-10-14, third dispatch; 48/48 at
glyph landing).
