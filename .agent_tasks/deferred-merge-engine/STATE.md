# Task: deferred-merge-engine

**Status:** done
**Started:** 2026-10-16   **Last touched:** 2026-10-16

## Objective

`merge = "deferred"` (run-level `[run]` or per-step) retains worktree
branches/commits/verify results as pending merges instead of landing them,
until a human runs the merge (`merge_run` / Merge pending branches). A
per-run, per-project integration baseline branch gives sequential deferred
steps their verified predecessors' work. Backward default stays `auto`
(today's merge-after-step), and this run's own merge policy is untouched.

## Decisions already made

- `merge="deferred"` implies `worktree=true`; explicit `worktree=false` +
  deferred and `cloud=true` + deferred are manifest errors.
- Baseline ref `conductor/<run>/baseline-<slug>` per (run, project dir),
  created at the recorded target tip (`target_branch` + `target_sha` in
  `state["pending_merges"][repo_key]`). Sequential members = the only
  same-project deferred member of their group; they FF the baseline to the
  step tip when status==done and verify_ok is not False. Same-project wave
  siblings are `alternative`s - parked, never folded into the baseline.
- `_baseline_sync` absorbs target movement (human commits / auto-mode
  merges) into the baseline via `git merge-tree --write-tree` plumbing
  (git 2.38+; absent -> absorb skipped, baseline stays self-consistent).
  Conflict -> `baseline_stuck`, work continues off old tip.
- Explicit merge stages into scratch worktree `worktrees/<run>/stage-<h>`
  on branch `conductor/<run>/stage-<h>` at current target tip, merges each
  verified entry in manifest order (merge_resolver honored, resolved inside
  the stage), reruns each merged step's distinct verify cmd there, then
  lands atomically: `update-ref target staged_tip staged_base` (target not
  checked out) or `merge --ff-only` (target checked out in main checkout,
  refused while dirty or a worker watches it). Conflicted/verify-failed
  attempts KEEP the stage for inspection; landed or moved targets drop it.
- Serialization: per-repo lockfile `<gitdir>/conductor-merge.lock`
  (pid+ts, stale-broken on dead pid) + existing `_MERGE_BUSY` per run.
- Verified = `status=="done"` and `verify_ok is not False`. Anything else
  (failed, partial, blocked, timed_out, red verify incl.
  `verify_must_pass=false`) stays pending under `skipped_unverified` -
  never auto-included, never deleted by cleanup paths.
- `merge_run` accepts `only=[steps]`; the POST `/merge` endpoint passes
  `body["steps"]`. Legacy (non-deferred) parked branches keep the existing
  direct-merge path.

## Phases

### Phase 1 - manifest + runtime

- [x] `Step.merge`/`run.merge` parsing + validation (deferred=>worktree,
      cloud/explicit-false errors, bad value)
- [x] `worktree_add` start-point param; baseline helpers
      (ensure/sync/advance/rebuild); pending_merges records
- [x] cmd_run: deferred prep off baseline tip; post-wave records pending
      instead of merging; resume skips deferred in the finalize block
      (deferred stamp happens AT PREP so a crash pre-postwave can't be
      misread as a legacy unmerged auto-merge)

### Phase 2 - explicit merge op

- [x] `resolve_merge_conflict` generalized (merge_dir, already_started,
      remove_on_success, no dirty-guard in stage)
- [x] `_merge_run`: partition deferred entries -> staged+verified+atomic
      land per target; `_RepoMergeLock` (gitdir lockfile, stale-break);
      results add `unverified`/`verify_failed`/`moved`/`busy`/`kept`
- [x] CLI `merge` verb (`--steps` pick); plan `[deferred]` tag; JS
      merge-result bits; endpoint `steps` passthrough
- [x] repo-merge lock now also wraps legacy post-wave/resume/merge_run
      merges; `abort_own_merge` refuses while a live lock-holder exists
      (the recorded cross-run merge-kill)

### Phase 3 - tests + docs

- [x] tests/test_worktrees.py DeferredMergeTests: continuity via
      verify-gate, alternatives, retries, restart/resume, non-main target,
      failed verify (step + staged-combine), unchanged auto mode, lock,
      land-moved, kept-stage, mixed run, resolver-in-stage
- [x] AGENTS.md paragraph + QUEUE.md line + efficiency_stats row

## Where it stopped

Complete. 250/250 tests + `demo` green. Shipped on this worktree's
branch as two commits (feature+docs, tests) plus this task-state
commit. The conductor holds this step's branch/worktree as a pending
merge - no further handoff needed beyond the run's own merge review.

## Verification

`python run_tests.py` (mock-only) + `python conductor.py demo`;
new tests in `tests/test_worktrees.py`.
