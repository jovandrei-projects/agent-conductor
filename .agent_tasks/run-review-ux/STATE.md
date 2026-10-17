# Task: run-review-ux

**Status:** in progress
**Started:** 2026-10-16   **Last touched:** 2026-10-16

## Objective

Make runs and tasks easy to review after completion: a human review state
that is independent of execution outcome, truthful outcome summaries, stable
modal/table interactions, cumulative timing, a "wt" run label, identified
watcher warnings, and a manual merge that can't fire mid-run. Driven by
manifest `manifests/20261016-100358-0d289b96-run-review-ux.toml` (9 steps).

## Not in scope

- Implementing the features here - this file only records the design.
- `plan-proposal` / `plan-proposal-N` rows getting review indicators
  (decided: they are preparation history, not run work - see DESIGN.md).
- Changing STATUS:/NEXT: worker contract (kept; SUMMARY: may be added).

## Decisions already made

See `DESIGN.md` for full reasoning + sources. Headlines:

- Review state = per-run + per-task `{seq, at, reviewed}` mark vs a
  `completion_seq` counter stamped at each terminal transition. An ack only
  covers the seq it saw - a re-completion after resume needs review again.
- Storage = `review-<flagkey>[-<step>]` sentinel files in
  `%TEMP%/agent-conductor-flags` (trap-5 safe), folded into state.json on
  the driver's next save; dashboard overlays flags live like
  prompt/rnote/skip already do.
- Explicit buttons "Mark as reviewed"/"Mark as needing review"; opening a
  modal never acknowledges.
- One attention accent for both levels, text always present (WCAG 1.4.1).
  Suggested green-run accent REJECTED (collides with done/#3fb950 and
  would paint failed runs green); orange-task acceptable but collides with
  timed_out/#f0883e - recommend a free hue instead.
- Historical records get no `completion_seq` -> no indicator, no migration
  write. Legacy steps can still be manually marked either way.

## Phases

### Phase 1 - inspect + design (this step)

- [x] Inspect tables/modals, completion reporting, persistence,
      timing/resume, watcher warning, manual merge flow
- [x] Research public unread/review-ack precedents (URLs in DESIGN.md)
- [x] Choose model + evaluate suggested accents
- [x] Decide historical-record + resume-completion semantics
- [x] Record touchpoints + test gaps for the implementation steps

### Phase 2 - implementation (manifest steps, other sessions)

- [x] review-workflow - the model above (done 2026-10-16, see below)
- [x] task-outcome-summary - SUMMARY: line -> rec["outcome"] -> modal/table
      (landed on master, merge d57e9c2)
- [x] task-table-navigation - name-targeted clicks + selection highlight
      (landed on master, merge 12fbe2b)
- [x] modal-refresh-stability - scroll-stable modal rebuilds
      (landed on master, merge 8cb3988)
- [ ] cumulative-elapsed-time - interval accumulation + runs elapsed col
- [x] execution-context-labels - run-level wt chip + watcher identity
      (landed on master, merge 6b1418c)
- [ ] manual-merge-guard - disable/refuse while run live (driver included)
- [ ] integration-verification + follow-up-plan

## Where it stopped

`review-workflow` merged to master 2026-10-16 via merge-resolver
session after the wave merge conflicted on bookkeeping + test/class
tails (kept both sides). It was implemented + verified on the branch
(290/290 `python run_tests.py`, demo + `plan` green). What landed:

- `completion_seq` bumps at every terminal-outcome write: run_step
  terminal update, pre-spawn non-repo fail, resume merge-finalize flips,
  halt_remaining run close, auth aborts, end-of-run ended_at, sweep
  lost-mark + run close, _merge_run + _merge_deferred_group flips.
- `review-<flagkey>[-<step>]` sentinels (JSON `{reviewed, seq, at}`) in
  %TEMP% - trap-5 safe - folded into state.json by save_state's new
  `_fold_review_flags`, exempt from clear_run_flags, overlaid live in
  collect_dashboard (newer `at` wins).
- Endpoints: POST /api/runs/<d>/review and .../steps/<s>/review
  (`set_run_review`/`set_step_review`/`_set_review`). Body `{reviewed}`
  + optional `seq` guard: an ack against a stale view 409s; 'mark
  reviewed' with nothing to review (seq-less, no tombstone) also 409s.
  Run and task marks are fully independent.
- Dashboard: `completion_seq`/`review`/`needs_review` on run + task
  dicts; `.stage.unreviewed` cyan pill (#39c5cf - the free hue), left
  row inset + bolded task name + `· unreviewed` aria suffix; 'review'
  kv row + Mark-as-reviewed/needing-review button in BOTH modals and
  both ctx menus (same data-review-run/review-step hooks). Run-level
  seq mismatch is gated on `ended_at` so a resumed in-flight run does
  not resurrect the old flag; a LIVE step suppresses its flag rather
  than dropping seq (pop would let an old ack collide with the recycled
  seq - documented in collect_dashboard).
- Failure stays failure: the pill is a sibling of status, never a
  recolor; `ReviewWorkflowTests` pins failed+unreviewed coexisting.
- 14 new tests: seq stamping, ack/re-flag persistence, repeat saves,
  run/task independence, legacy no-autoflag + manual marks, stale-seq
  409, flag overlay + fold, clear_run_flags exemption, sweep lost bump,
  resume re-completion re-flag, HTTP endpoint statuses, Node JS badge/
  modal/ctx/dispatch regression.

Next session = any other Phase-2 step (none started). Do NOT implement
their scopes here.

## Deferred or blocked

Nothing. Known risk carried to implementers: `run_tests.py` now ~460s vs
the 600s hard verify cap in `run_step` - a slow step's post-verify could
flirt with the cap (hit once before, see QUEUE.md compact-live-output).

## Verification

`python run_tests.py` (276 tests, ~7.7min), `python conductor.py demo`,
`python conductor.py plan <manifest>`. Design-only step: verified by
inspection + full suite green, no code changed.
