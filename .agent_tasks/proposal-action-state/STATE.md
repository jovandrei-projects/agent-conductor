# Task: proposal-action-state

**Status:** done
**Started:** 2026-10-16   **Last touched:** 2026-10-16

## Objective

Proposal-draft staleness is computed from real diffs, not the sticky
`stale` flag: request inputs that require Regenerate (raw, project,
attachments) are diffed against a `gen_inputs` baseline stamped at
publish; applyable option fields (model/effort/max_attempts/on_failure/
worktrees/merge_resolver/followup) are diffed against what the manifest
TOML itself encodes. Reverting a field to baseline clears its indication.
The modal explains which action resolves which pending set, disables
Apply when there is nothing to apply, styles Regenerate as a neutral
secondary (Start run stays primary), and launch is blocked while either
set is pending.

## Decisions already made

- `gen_inputs = {raw, project, attachments}` stamped inside
  `_publish_proposal` only after the manifest write succeeds - that one
  funnel covers save-with-manifest, Apply and generation publish.
- `ask`, `plan_model`, `plan_effort`, `label` are next-generation-only
  or cosmetic - they never stale a proposal (same as today).
- worktrees/merge_resolver/followup are dual (planner prompt AND TOML
  keys) but Apply resolves them deterministically -> they sit in the
  options bucket only; Regenerate covers them too.
- Option diff compares the FORM value to the TOML encoding (defaults/run
  tables + worktree coverage + follow-up-plan step), not to a stored
  snapshot - a hand-edited TOML moves the baseline by itself.
  Per-step overrides are beyond the form's vocabulary - not drift.
- worktrees baseline: True = all non-cloud steps isolated, False = none
  beyond wave-required, null = mixed (checkbox can't express; drifts
  either way until reconciled).
- Legacy records (no gen_inputs): sticky `stale` stays as an opaque
  "request fields" gen marker; a not-stale legacy record materializes
  gen_inputs from stored fields on next save.
- Apply refuses (409) when gen inputs diverge from the baseline - the
  old code only compared to the saved record, so a saved raw change
  slipped through.

## Phases

### Phase 1 - server semantics

- [x] `_gen_inputs_of`/`_manifest_options`/`_pending_actions` helpers
- [x] `_publish_proposal` stamps gen_inputs; `save_request` materializes
  legacy baselines + stamps computed stale (no sticky set)
- [x] `apply_request` refuses on baseline gen-diff (raw/project/attachments)
- [x] `launch_request` gates on computed pending
- [x] `request_details` + `collect_dashboard` emit `pending`/`baseline`/
  computed `stale`

### Phase 2 - dashboard UI

- [x] `pendingNow`/`pendNote`/`refreshPending` + `#pend-note` div; input
  handler re-diffs live (revert clears without saving)
- [x] Apply disabled + explanatory title when nothing applies / gen
  pending / manifest broken; Regenerate `btn secondary` + confirm text
  warns about unsaved TOML edits; no `.attn` colour-only signal
- [x] runs-table chip keeps 'Needs review' + gains pending-detail title

### Phase 3 - tests

- [x] python: revert-clears, independent buckets, both-pending order,
  save/reopen, failed apply/regen, launch gate, manual TOML baseline,
  attachments, planner-only fields, legacy opaque/materialized baselines
  (PendingStateTests, 16 tests)
- [x] node harness: notice text per bucket, Apply disabled states,
  Regenerate confirm cancel + secondary class, revert-clears-live
  (2 new DashboardJsFilterTests)

## Where it stopped

Done - all phases ticked, 294/294 suite + `conductor.py demo` green.
Also fixed a real Apply bug found en route: `follow-up-plan` appended
after the worktree pass never got `worktree`, leaving a permanently
mixed state; it now inherits the checked box inside
`_apply_fields_to_toml`.

## Verification

```
python run_tests.py            # unittest suite incl. node harness
python conductor.py demo       # end-to-end vs tests/mock_devin.py
```
