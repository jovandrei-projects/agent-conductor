# test-perf — suite speed, flakes, and merge-lifecycle hardening

Branch: test-perf (worktree: C:\Projects\_worktrees\ac-test-perf)

## Goal

The ~15min serial suite made every merge/verify cycle painful and the
dashboard's synchronous `merge_run` let a hub `taskkill /F /T` orphan a
mid-merge resolver (the original MERGE_HEAD wedge incident).

## Steps

- [x] Profile the suite: mobile 0.4s / services 17s / worktrees 7m17s /
      conductor 7m16s — slowness is real e2e work, not a stuck process.
- [x] Fix the 2026-10-17 date bomb: planner tests assumed swe-2;
      `default_model_effort` extended per user (promo runs to 11/01):
      swe-2 while `today < 2026-11-01`, gpt-6-luna after.
- [x] Wire the dead `--quick` flag: `@slow` marker + runner filter on
      QUICK_TESTS; marked the measured-heaviest classes
      (worktrees: ManualMergeGuard/DeferredMerge/MergeTarget/StopControl;
      conductor: CumulativeElapsed/PauseRun/Run/ReviewWorkflow/
      DeviceOwnership/StepControl). quick ≈ 6min serial.
- [x] `--jobs N` parallel sharding in run_tests.py: class-granular LPT
      bin-pack, per-shard CONDUCTOR_PLANNER_SPOOL (new env override on
      PLANNER_SPOOL), per-shard logs, both `--jobs=N` and `--jobs N`.
      `--jobs 6 --quick` ≈ 2min wall (510 tests).
- [x] Detach dashboard merge_run -> `merge_run_start` spawns
      `conductor.py merge` like resume/summarize; `_MERGE_PIDS` +
      existing claim file keep merges serialized; UI shows "merge
      running in background"; merge_busy covers detached children.
- [x] ResourceWarning: `_launch_process` dropped Popen handles -> GC
      warned on live children. `_DETACHED_PROCS` registry retains them;
      /api/auth/login spawn routed through the same helper. Prune is
      fake-Popen tolerant (a mocked spawn broke every later launch).
- [x] StopProposal flake family root-caused + fixed:
      * `_spool_rec` swallowed transient OSError -> None; a mid-stop or
        mid-save_draft miss orphaned the 'generating' overlay (wedge for
        the 660s orphan grace) or made stop 409. Now retries OSError
        (4x/50ms; FileNotFoundError/ValueError stay immediate).
      * `_finish_generation` finally drops a 'generating' overlay still
        bearing the exiting attempt's gen_id.
      * test fakes: `fake_wait`/`stop_aware_wait` had 5s/10s deadlines -
        under CPU starvation the "planner" self-failed 'error' BEFORE
        the test could raise the stop flag -> the real 409 flake
        signature. Deadlines raised (poll 5s->60s, stop_aware_wait
        10s->60s, gates 30s->120s, wait_* 10s->30s).
- [ ] Full `--jobs 8` gate + merge back to main.

## Where it stopped

Fixes verified: StopProposal+ProposalFlow 11/11; MergeRunTests+
FollowupDiscovery 22/22; transient-read regression test added.
Parallel quick run: 6 shards ~2.5min, only failure was the fake-Popen
prune bug (fixed). Full `--jobs 8` run in flight.
