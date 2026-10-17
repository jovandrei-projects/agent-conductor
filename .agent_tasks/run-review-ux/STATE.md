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

- [ ] review-workflow - the model above
- [ ] task-outcome-summary - SUMMARY: line -> rec["outcome"] -> modal/table
- [ ] task-table-navigation - name-targeted clicks + selection highlight
- [ ] modal-refresh-stability - scroll-stable modal rebuilds
- [ ] cumulative-elapsed-time - interval accumulation + runs elapsed col
- [ ] execution-context-labels - run-level wt chip + watcher identity
- [ ] manual-merge-guard - disable/refuse while run live (driver included)
- [ ] integration-verification + follow-up-plan

## Where it stopped

Design complete and verified (276/276 `python run_tests.py`). Next session
= `review-workflow` step: implement per DESIGN.md - `completion_seq` bumps
at the enumerated write sites, `review-*` sentinels + endpoints +
`collect_dashboard` fields, modal buttons + ctx-menu items, `.stage`
pill + row accent. Do NOT implement the other steps' scopes.

## Deferred or blocked

Nothing. Known risk carried to implementers: `run_tests.py` now ~460s vs
the 600s hard verify cap in `run_step` - a slow step's post-verify could
flirt with the cap (hit once before, see QUEUE.md compact-live-output).

## Verification

`python run_tests.py` (276 tests, ~7.7min), `python conductor.py demo`,
`python conductor.py plan <manifest>`. Design-only step: verified by
inspection + full suite green, no code changed.
