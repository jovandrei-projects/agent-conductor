# Task: deferred-merge-controls

**Status:** done
**Started:** 2026-10-16   **Last touched:** 2026-10-17

## Objective

Expose worktree isolation and merge timing as separate proposal options,
riding the landed `merge = "deferred"` backend (deferred-merge-engine, done
250/250). Run details show pending merges per project/step with targets,
verify status and alternatives; a confirmed selective merge runs the
existing `merge_run(only=)` op - no git logic in the UI.

## Decisions already made

- New request field `merge` ("auto"|"deferred") - a select (like
  on_failure), not a checkbox, so the two timings explain themselves.
  deferred implies worktree=true on non-cloud steps; cloud steps get a
  per-step `merge = "auto"` override ([run]+cloud+deferred is a manifest
  error). Same guarantee path as worktrees/merge_resolver
  (`_guarantee_isolation`, one-directional).
- Run modal: `pending_merges` groups rendered per project (slug, recorded
  target, baseline notes: baseline_stuck/stage_kept/moved_reason/busy/
  resolving), per-entry checkbox (disabled unless `deferred_verified` and
  not live), kind chip for `alternative`s - checking one alternative
  unchecks its siblings (never silently combined). Legacy parked branches
  list as a plain group, multi-project-aware via slug.
- `merge_busy` on the run payload (`_MERGE_BUSY`) + per-group
  `_repo_merge_busy` flag; run modal re-renders on payload change
  (`modalRunKey`), so a refresh mid-merge shows the in-flight state.
  `mergeSel` keeps ticks across refreshes; `mergeResult` keeps the last
  outcome visible after re-render.
- Agent rows carry deferred/merge_kind/target_branch/deferred_verified;
  a `deferred` chip + facet marks held work in the agents table; the
  agent modal shows a merge-policy row.
- Merge POST sends `{"steps": [ticked]}` to `/api/runs/<dir>/merge`
  (endpoint already threads `only=`). Button disabled while any run step
  is live or a merge is in flight; an explicit confirm spells out what
  lands and that nothing else is touched (no new run, no auto-combining).

## Phases

### Phase 1 - request field end to end

- [x] `_request_fields` + stale keys + `requestFields()`
- [x] `_apply_fields_to_toml` merge handling (strip step keys, run key,
      cloud override, worktree implication)
- [x] `_guarantee_isolation`/`_publish_proposal` + planner prompt
- [x] form select + worktrees coupling (force check+disable on deferred)

### Phase 2 - run-details pending panel

- [x] collect_dashboard payload (groups, legacy, busy, agent fields)
- [x] pending-merge UI + selective merge POST + result notice
- [x] deferred chip/facet + modal merge-policy row + modal live refresh

### Phase 3 - tests + docs

- [x] python tests (fields/apply/publish/payload): 4 apply/field tests in
      ApplyRequestTests, 2 payload tests in new PendingMergeViewTests
- [x] node harness tests: merge-timing select + worktrees coupling;
      pending panel render/tick/alt-single-pick/POST/busy/live/empty
- [x] run_tests.py + demo green; AGENTS.md/QUEUE.md/ROADMAP.md +
      efficiency_stats updated

## Where it stopped

Done: 258/258 tests + demo green, committed (a649a85 + docs commit). One
bug the suite caught and fixed: `syncMergeTiming` assumed `.dataset`
exists - older minimal node stubs crash; guarded.

Merged onto master as d7a1cc9 via resolve-deferred-merge-controls
(2026-10-17): ~14-region union keeping merge_target/summarize alongside
merge timing and the Pending merges panel atop the runCtl lifecycle. The
resolution fixed three seams: the ctx-menu merge item routes to the
run-details panel (ticking happens there), pending-merge ticks share the
single 'change' listener with the file pickers (stub DOMs keep only the
last-registered handler), and a null merge baseline reads 'auto'.
Post-merge suite: 508 ran, 4 pre-existing date-boundary planner failures
(default_model_effort flips swe-2 -> gpt-6-luna on 2026-10-17; identical
on pre-merge HEAD), 1 StopProposal flake green on rerun.

## Verification

`python run_tests.py` (mock-only) 258/258 + `python conductor.py demo` 2/2
(20261016-070212-demo).
