# Task: project-merge-targets

**Status:** done (merged to master as 6a40935; awaiting wave merge no more)
**Started:** 2026-10-16   **Last touched:** 2026-10-16

## Objective

`merge_target = "<branch>"` ([run] / [defaults] / [[step]]) makes worktree
steps branch off and merge back into that existing local branch instead of
implicitly using the checkout's current branch. The dashboard request form
exposes it via a branch selector fed by the selected project's real branches.
The selected target is persisted per step in state.json so retries, merge
resolution and manual merges never fall back silently.

## Decisions already made

- Per-step `[defaults]`/[[step]] `merge_target` requires `worktree = true`
  (ManifestError - silent ignore is a footgun). `[run].merge_target` is a
  DEFAULT for worktree steps only - a non-worktree step just ignores it, so
  the request form's field never breaks a plan that keeps direct steps.
- Merge into a non-checked-out target runs in a conductor-owned "merge
  worktree" at `data/worktrees/<run>/<step>.merge` - the main checkout's
  HEAD never moves. Target checked out in another (foreign) worktree =
  actionable failure; the conductor's own merge worktree is exempt.
- `git worktree add` gets the PLAIN branch name - `refs/heads/x` is a
  commit-ish and checks out DETACHED, which silently dropped merges
  (caught by test, fixed + post-merge `merge-base --is-ancestor` tripwire).
  Registered merge worktrees are verified to sit on the target; stale ones
  are recycled only when conductor-owned.
- Unset merge_target keeps byte-identical legacy behavior (merge into the
  checkout HEAD); the effective target (explicit or current branch) is still
  recorded in state at materialization. Prior records win over the manifest
  on resume (`prior.get("merge_target", step.merge_target)`); `merge_dir`
  is persisted too so the resolver re-enters the merge where it happened.
- Resolver sessions spawn only on a real conflict (`merr is None`); a bad
  location/vanished target/foreign merge is an actionable step failure, not
  a wasted session.
- Form: datalist from `GET /api/projects/<slug>/branches` (local branches,
  conductor/* filtered); untouched field gets the implied default (checkout
  branch, or editable `main` when project inferred); a typed value is never
  clobbered by project switches (module vars `mtTouched`/`mtImplied` -
  node:vm test stubs have no `dataset`). Server re-validates on save;
  inferred-project existence is checked per-step at proposal validation.
- Apply: project selected -> per-step keys on that project's post-apply
  worktree steps (run-level key removed - it would leak across projects);
  inferred -> `[run].merge_target`. `""` clears. Stale-flag on change.
- Planner prompt documents merge_target (existing local branch, requires
  worktree, never substituted).

## Delivered

- conductor.py: Step/Manifest `merge_target`; git helpers
  (`current_branch`, `branch_name_ok` via check-ref-format,
  `local_branch_exists` via refs/heads only, `list_local_branches`,
  `worktree_list`, `merge_target_error`, `validate_merge_targets`,
  `merge_wt_for`, `_merge_loc`, `merge_step_branch`); worktree_add(start);
  resolve_merge_conflict(merge_dir,target); run/resume/merge_run honor the
  recorded target; dead `worktree_merge` removed; plan prints
  `[merge -> x]`; proposal `mt`; request-field/apply/guarantee/stale;
  `/api/projects/<slug>/branches`; form datalist+hint+implied default;
  agent modal `merge target` row; step summary shows target.
- tests: 12 MergeTargetTests (test_worktrees.py) + 11
  MergeTargetRequestTests (test_conductor.py); 2 harness assertions updated
  for the new read-only branches fetch.
- AGENTS.md: merge-target paragraph + form-field note + resolver-location
  wording fix.

## Where it stopped

DONE - merged into master as `6a40935` by a resolver session
(2026-10-16). The merge unioned this task's merge_target work with
master's deferred-merge / pending-state / run-review changes: combined
the merge pipeline into `merge_step_branch` + `_RepoMergeLock`,
`resolve_merge_conflict` gained `merge_dir`/`target` while keeping
`remove_on_success`/`dirty_guard`, deferred staging honors recorded
targets, both `PendingStateTests` and `MergeTargetRequestTests` classes
kept whole (shared setUp/proposal/ready split per class), dashboard JS
init (`refreshPending` + `loadBranches`) unioned, `merge_target` added
to `OPTION_FIELDS`/client `OPT_KEYS`/`PEND_LABEL`, and `updateMtHint`
made stub-tolerant (`inp.value` may be absent on node:vm stubs). Two
pre-merge test fixtures gained `merge_target` in their baselines/fill.

## Verification

`python run_tests.py` -> 247/247 OK (~428s); `python conductor.py demo`
green. New coverage: lands-on-target, baselines-off-target,
worktree-required, run-level default scope, missing-branch actionable on
plan+run, checked-out-elsewhere refusal, multi-project distinct targets,
conflict-park -> merge_run/--resume on recorded target, resolver session in
the merge worktree, backcompat recording, branch helpers; request-side
format/existence validation, apply scoping, publish guarantee, stale,
proposal validation, branches endpoint.

Post-merge (6a40935): full suite 497 tests - 2 node-harness failures
(updateMtHint stub read, baseline fixture missing merge_target) fixed in
the resolution, 3 StopProposal flakes green on rerun; focused rerun of
the merged merge-path classes (MergeTarget/DeferredMerge/MergeRun/
ManualMergeGuard/MergeTargetRequest/PendingState) = 72/72 green.
