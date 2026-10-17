# Task: manual-merge-guard

Wave step of run 20261016-100852-run-review-ux. Withhold the manual
'Merge pending branches' action while its run is still executing, in UI
and in the operation itself, with the start/resume race closed.

**Status:** done
**Started:** 2026-10-16   **Last touched:** 2026-10-16

## Objective

`merge_run` (dashboard button + `conductor.py merge`) refuses a run whose
driver is still alive - including the between-wave, verify/merge and
stop/pause-shutdown windows where no step reads 'running' - and a resume
starting concurrently can never overlap a merge in flight.

## Not in scope

- Conductor-managed merges (post-wave, resume-finalize, deferred staging)
  and `resolve-*` resolver sessions - untouched paths.
- Redesigning the other run-review UX signals.

## Decisions already made

- `run_executing(state)` is the shared predicate: live `driver_pid` is
  authoritative (covers every no-running-step window; a halt sentinel is
  a request, not a completed stop); pre-driver_pid runs fall back to
  live step records.
- Run-level claim `merge-<runkey>` in the flag dir (O_EXCL + pid, same
  shape as `conductor-merge.lock` but machine-global) serializes merge vs
  resume-stamp and merge vs CLI merge; `_merge_run` re-reads state UNDER
  the claim so a resume mid-stamp is caught at execution time.
- `cmd_run --resume` holds the claim while it stamps driver_pid and now
  refuses a live driver outright (the double-driver hole); `resume_run`
  refuses a live driver or a held claim before spawning.
- `clear_run_flags` exempts `merge-`; `sweep`/`archive` skip claimed
  runs; `delete_run` 409s on a held claim.
- UI: run rows carry server-computed `executing`; `runCtl` consumes it,
  the modal button + context item render disabled with the reason.
  Stale pages are covered by the server-side 409.
- `run_manifest` (test helper) restamps a dead driver_pid after the
  in-process cmd_run exits - the test-runner pid reads alive forever on
  POSIX and can sit inside the creation-window early in the suite.

## Where it stopped

Complete; regression tests in `ManualMergeGuardTests`
(tests/test_worktrees.py) + `test_merge_pending_disabled_while_run_executing`
(ContextMenuTests). Verification: `python run_tests.py`.

## Deferred or blocked

Nothing.

## Verification

`python run_tests.py` (mock-only, no auth).
