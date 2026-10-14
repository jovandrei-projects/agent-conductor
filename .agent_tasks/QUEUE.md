# Task queue

The master ordered list. One line per task. This answers "what next"; the
task's own `STATE.md` answers "where in it".

Status is one of `next`, `in progress`, `blocked`, `paused`, `done`. Keep the
order meaningful, but see rule 3 in `README.md`: finishing something early is
fine, it just has to be ticked where it lives.

Conventions for `STATE.md` are in `README.md`; the template is in `_template/`.

| # | Task | Status | Folder | Waits on |
|---|---|---|---|---|
| 1 | Bootstrap: conductor.py v1 + mock tests + docs | done | bootstrap/ | trap 5 root-caused + fixed (no workspace writes while a worker is alive); auth solved; live smoke green |
| 2 | Pre-live hygiene: liveness probe, lost/sweep, sessions cleanup, cloud marks | done | hygiene/ | os.kill probe bug found + fixed (was killing live workers) |
| 3 | Dashboard favicon: baton-over-orchestra glyph | done | favicon/ | second dispatch; first was eaten by trap-10 commit swallow |
| 4 | Worktree merge matrix: exhaustive tests + merge_resolver + lab fixtures | done | wt-matrix/ | landed on dev/merge-tests (commits 5ba30c4, 7f7dcd9); live lab run 21/21, 8 real conflicts resolved |

## Not yet filed as tasks

- First real overnight manifest on a real project (Phase 2 in ROADMAP.md; live smoke already green)
- Push agent-conductor to GitHub if cloud steps on it are wanted - it has NO remote, and cloud workers clone from origin (human decision: repo name + private/public)
- Phone access to the :8773 dashboard via lan-bridge proxy (needs mobile-apps change; tasks-ui rows already give coarse progress)
- Record session_id ~20s after spawn, not just at step end (2026-10-14):
  a crashed/orphaned run leaves a session id never written to state.json,
  which is invisible to `sessions`/`sweep_sessions` whitelist - manual
  cleanup only. Poll `devin list` once post-spawn (before wait), set
  rec["session_id"] under the same trap-5-guarded conditional save as pid.
  Careful: `devin list` runs with cwd=work_dir (watched) - appeared safe in
  the live_sessions_by_dir path, but verify no file writes land in cwd.
- Post-STATUS hang: `devin -p` does not exit while a session-spawned child
  is alive (2026-10-14, views-floorplan run). Workers' playwright probes
  spawn `vite` (port 8805) that outlives the script; the CLI then sits
  idle until timeout_min kills it - 3 steps printed STATUS: done then
  burned 52/52/85min. camera-stability exited clean (1004s) ONLY because
  its agent explicitly `netstat|grep 8805` + `taskkill /F`ed the leftover
  server. Fixes, cheapest first: (a) run_step polls tail_status(stdout_log)
  during wait; after a STATUS line appears, grace ~90-120s then kill and
  still run verify (work is complete - the hang is CLI teardown, not the
  turn); (b) timeout kill should take the process tree (taskkill /T) not
  just devin.exe; (c) prompt/AGENTS convention: workers kill servers they
  spawn before STATUS. floorplan-fixes is the different case: killed
  mid-explore-test with no STATUS - 90min was genuinely too short for that
  phase's calibration workload.
