# Task: deferred-merge-controls

**Status:** in progress
**Started:** 2026-10-16   **Last touched:** 2026-10-16

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
  target, baseline notes: baseline_stuck/stage_kept/moved_reason/busy),
  per-entry checkbox (disabled unless `deferred_verified`), kind chip for
  `alternative`s - checking one alternative unchecks its siblings
  (never silently combined). Legacy parked branches list as a plain group.
- `merge_busy` on the run payload (`_MERGE_BUSY`) + per-group
  `_repo_merge_busy` flag; run modal re-renders on payload change
  (`modalRunKey`), so a refresh mid-merge shows the in-flight state.
- Agent rows carry deferred/merge_kind/target_branch/deferred_verified;
  a `deferred` chip + facet marks held work in the agents table.

## Phases

### Phase 1 - request field end to end

- [ ] `_request_fields` + stale keys + `requestFields()`
- [ ] `_apply_fields_to_toml` merge handling (strip step keys, run key,
      cloud override, worktree implication)
- [ ] `_guarantee_isolation`/`_publish_proposal` + planner prompt
- [ ] form select + worktrees coupling (force check+disable on deferred)

### Phase 2 - run-details pending panel

- [ ] collect_dashboard payload (groups, legacy, busy, agent fields)
- [ ] pending-merge UI + selective merge POST + result notice
- [ ] deferred chip/facet + modal merge-policy row + modal live refresh

### Phase 3 - tests + docs

- [ ] python tests (fields/apply/publish/payload) + node harness tests
      (form, panel, disabled/busy, repeated clicks, alt single-pick)
- [ ] run_tests.py + demo green; AGENTS.md/QUEUE.md/efficiency_stats

## Where it stopped

(just started)

## Verification

`python run_tests.py` (mock-only) + `python conductor.py demo`.
