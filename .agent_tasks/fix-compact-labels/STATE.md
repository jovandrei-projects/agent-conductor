# Task: fix-compact-labels

Status: done - pending merge (this session)
Slice of manifest `manifests/proposal-flow-fixes.toml` (`steps[2]`).

## Goal

From the request record: "the labels under the run table, sometimes are
not visible in the 'compact' view". Labels must remain visible and
readable in compact, after density toggles, viewport resizes, and table
updates - without redesigning anything else.

## What "the labels" actually were (measured, not guessed)

Two distinct losses in the runs table, confirmed with Playwright against
`python conductor.py serve --port 8791` fed a stubbed `/api/state`:

1. **Chip labels clipped inline after the run name.** In compact,
   `body.compact td br { display:none }` removed the `<br>` between the
   run-name button and the `.stage` chips (`running`,
   `paused - resume ready`, `Preparing proposal...`, `archived`,
   `Needs review`), so the chips collapsed onto the name's single
   ellipsized `td.wrap` line. Whether they clipped depended on name
   length and column width - the "sometimes". Evidence
   (`scratch/probe_clip.py`, viewport 1400, compact): `running` chip
   right edge 215px vs cell edge 194px; `Preparing proposal...` 265px;
   at 900px the name alone overflowed and chips vanished entirely.

2. **Labeled token lines hidden.** `planner:`/`steps:`/`est`/`not
   planned` were `.sub` spans, killed by `body.compact td .sub {
   display:none }` - even though `fmtTc()` was written specifically as
   their compact format (dead branch since efec536).

## Fix (conductor.py `_DASH_HTML` only)

- Run-name cell: the chips + `.genline` + `.sub` lines now live in a
  `<div class="rlabels">` - a plain block box keeps them on their own
  line under the name in BOTH densities, replacing the `<br>` that
  compact hid. `Needs review` moved inside, grouped with the other chips
  (it used to render last, after the dir `.sub`).
- `planner:`/`steps:`/`est`/`not planned` are now `.toksub` - same look
  as `.sub` but exempt from the compact hide; `display:block` in compact
  because their `<br>` is hidden there.
- `td .stage` gets `max-width:100%; overflow:hidden;
  text-overflow:ellipsis` so a pill wider than the column (900px
  viewport) ellipsizes inside the pill instead of clipping invisibly;
  `title=` on the stage chip carries the full label for hover.
- `render()` sig now includes `density`: the fmtT/fmtTc text differs
  per density, so a Compact|Full flip must re-render even when the
  payload is identical (previously the format never updated until data
  changed - stale long text in compact indefinitely).

## Evidence

- `scratch/probe_labels.py` + `scratch/probe_clip.py` - Playwright
  probes against the live serve, stubbed `/api/state`.
- `scratch/shot-full.png` / `shot-compact.png` /
  `shot-compact-narrow.png` - after-fix screenshots: chips on their own
  line, token labels visible as block lines in compact, ellipsized
  `Preparing propos…` at 900px.
- New node-harness test `test_compact_run_row_labels_stay_visible`
  (tests/test_conductor.py) asserts: `.rlabels` block markup, chips
  inside it, `.toksub` on the four labels, `fmtTc` short form after
  `setDensity('compact')`, long form back after `setDensity('full')`,
  `Needs review` grouped before the hidden dir `.sub`.

## Checks not performed

- No real browser session with a logged-in Devin backend (probe used a
  stubbed `/api/state` only).
- No unattended agent run (not needed for a CSS/markup change).

## Pre-existing failure found (for verify-proposal-flow)

`python run_tests.py`: 225 tests, **2 failures - both in
`StopProposalTests` and both reproducible on the worktree's base commit
(stash-verified), i.e. NOT caused by this change:**

- `test_stop_restores_draft_and_late_output_is_discarded`
- `test_stop_with_previous_proposal_returns_to_ready`

Root cause (stop-proposal-preparation x isolate-proposal-planning
interaction): `stop_generation` restores the record file to
`draft`/`ready` but keeps `gen_id` and relies on `_disk_untouched`
(file `updated_at` vs job.json's `base_updated_at`, SECOND granularity)
to block the late publish. `stop` within the same second as
`start_generation` collides -> `_publish_from_spool` sees `untouched`,
the killed attempt's TOML publishes, status flips to `ready` (test 2
shows the replacement plan overwriting the earlier proposal). The
overlay check (`overlay.status=='generating'`) is also vacuous - the
overlay is written by `_save_draft` before stop lands. Suggested fix
for the owner: gate the publish on the on-disk record, e.g. file
`status=='generating' && file.gen_id==gen_id`, and/or clear `gen_id` in
`stop_generation`'s restore write.

## Where it stopped

Implemented + verified in this worktree; committed locally (see git
log), pending the run's merge/verify step. Serve process on :8791 was
killed before finishing.
