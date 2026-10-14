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
- DONE 2026-10-14: Record session_id ~20s after spawn, not just at step
  end - `_capture_session_id` thread probes `devin list` up to 5x25s while
  the step runs (safe inside watched cwd: list is read-only, verified
  live). Falls back to the post-wait diff. Context that motivated it: a
  crashed/orphaned run leaves a session id never written to state.json,
  invisible to the `sessions`/`sweep_sessions` whitelist - manual cleanup.
- DONE 2026-10-14 (trap 12): Post-STATUS hang - `devin -p` does not exit
  while a session-spawned child is alive (views-floorplan run). Workers'
  playwright probes spawn `vite` (:8805) that outlives the script; the
  CLI then idles until timeout_min - 3 steps printed STATUS: done then
  burned 52/52/85min. camera-stability exited clean (1004s) ONLY because
  its agent `netstat|grep 8805` + `taskkill /F`ed the leftover server.
  Shipped: (a) `wait_worker` polls tail_status during wait - a STATUS
  line collapses remaining wait to STATUS_GRACE_S then tree-kill, counts
  as turn end, verify still runs; (b) `kill_tree` = taskkill /T so
  grandchildren die too; (c) open: worker-side convention (kill spawned
  servers before STATUS) - camera-stability's agent did it from
  apartment-3d's own trap note. floorplan-fixes was the different case:
  killed mid-explore-test with no STATUS - 90min was genuinely too short
  for that phase's calibration workload.
