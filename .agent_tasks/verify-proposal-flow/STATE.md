# Task: verify-proposal-flow

**Status:** done
**Started:** 2026-10-16   **Last touched:** 2026-10-16
Slice of manifest `manifests/proposal-flow-fixes.toml` (verify/merge step).

## Objective

Verify the three proposal-flow changes together in the merged state:
cancellable preparation (stop-proposal-preparation, merged a91fa43),
retained proposal session (parked branch), compact run-table labels
(parked branch). Sequence: prepare -> stop while preparing -> inputs
editable -> add info -> prepare again -> completed create-proposal
session stays on the plan's related list -> Start runs real step 1.
Plus late-completion, reload persistence, and labels in both densities.

## Prior-step review (what was actually delivered vs parked)

- `stop-proposal-preparation`: merged (checkpoint a91fa43 includes it).
- `retain-proposal-session`: DONE on its branch but conductor-recorded
  `failed` (merge gate) - merged here as 1c1f1d5 with conflicts resolved
  semantically (plan_sessions rides the spool architecture: attempts
  recorded into the overlay on publish/questions/fail, into the restored
  file record on stop; `_publish_from_spool` gained `gen_sids` +
  `plan_gen_id`).
- `fix-compact-labels`: DONE on its branch (its own UI probes green) but
  conductor-recorded `failed` via the two same-second StopProposalTests -
  merged here as c3e19b4 (code auto-merged; bookkeeping conflict only).

## Integration regressions found + fixed (aed7b98)

1. **Same-second stop/publish race** (the exact failure the compact
   branch's suite caught): `_disk_untouched` compared only
   `updated_at` - second granularity. A `stop_generation` restore in the
   same second passed the gate and the killed attempt's TOML published
   over the restored record. Now compares `generation_stopped_at` AND
   file `st_mtime_ns` (job.json records `base_stopped_at`/`base_mtime_ns`
   at attempt start).
2. **Stale 'generating' overlay masked the restore:** `stop_generation`
   rewrote the file but left the spooled `request.json` overlay, which
   outranks it for every `_read_request` caller - the request still read
   'generating' to the UI/tests and a drain could still publish the
   stopped draft. `stop_generation` now unlinks the owned overlay FIRST
   (a newer attempt's overlay is untouched).
3. **Drain outcomes recorded no plan_sessions:** publish-failure and
   restart-interruption paths in `_drain_generating` now record the
   attempt (session id falls back to the spooled export).
4. **Second run-row chip clipped at td edge in compact:** `td .stage`
   `max-width:100%` only caps the FIRST chip on the nowrap line; a chip
   starting mid-line still overflowed. `.rlabels` now gets
   `white-space:normal` so a following chip wraps to its own line;
   `.genline` became inline-block+ellipsis to stay single-line.

## Acceptance criteria - verified

- [x] Prepare -> Stop while `generating` -> record restores `draft` (no
  prior proposal) / `ready` (prior proposal), immediately readable as
  such (overlay dropped, not just out-voted)
- [x] Inputs (raw/fields/qa) editable + preserved after stop
- [x] Edit + immediate reprepare proceeds; stopped attempt cannot
  overwrite it (gen_id + overlay + disk gates)
- [x] Late TOML completion from stopped attempt cannot publish or alter
  the newer attempt - incl. the SAME-SECOND case (frozen now_iso test)
- [x] Stopped attempt banks its own `plan_sessions` entry (status
  stopped, its own session id); producer attribution moves to the
  successful attempt (`plan_gen_id`)
- [x] Completed create-proposal session stays on the plan's related
  list - `plan-proposal` row, seq -1, real session id, on disk
- [x] Association survives reload (fresh `request_details` +
  `collect_dashboard` rebuild rows from request json)
- [x] History is step-0 only: no run_dir, not in counts, never runnable;
  launch + resume operate on manifest steps only
- [x] Start runs the real step 1 (state.json `{organize: done}`, no
  planner respawn - spawn count asserted)
- [x] Compact + normal: every run-row label visible at 1400/900/700px
  (Playwright probe, stubbed /api/state): `.stage` chips on their own
  `.rlabels` line, wrap when needed, ellipsize in-pill; `.toksub`
  planner:/steps:/not-planned lines render in compact too
- [x] `python run_tests.py` 232/232 OK; `python conductor.py demo` green

## Checks not performed

- No real Devin planner session (mocked per instructions - the earlier
  retain branch verified `devin list` capture shape separately).
- Live token refresh of a real run's `.toksub` values (fmtTc vs fmtT is
  asserted by the node harness instead).

## Where it stopped

Done: both parked branches merged (1c1f1d5, c3e19b4), 4 integration
regressions fixed (aed7b98), 232/232 tests + demo + live Playwright
probe green. Next session can merge this branch / close the run.

STATUS: done
NEXT: merge conductor/.../verify-proposal-flow into the base and record the wave outcome in run state.json
