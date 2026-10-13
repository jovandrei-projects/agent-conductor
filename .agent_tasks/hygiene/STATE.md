# Task: hygiene

**Status:** done
**Started:** 2026-10-13   **Last touched:** 2026-10-13

## Objective

Pre-live hardening: the dashboard/status surfaces must tell the truth about
what is alive, and the spawned-session litter in the Desktop sidebar must be
cleanable without risking live workers or human sessions.

## Not in scope

- The first real manifest itself (next queue item).
- Archiving interactive sessions - `devin rm` targets we use are whitelisted
  to conductor-spawned ids only.

## Decisions already made

- Orphaned `running`/`queued` records become **`lost`**, not `failed`: the
  outcome is unknown, and `lost` steps re-run on `--resume` like any
  non-done step.
- `sweep` writes are skipped entirely while any live step's work_dir
  contains `data/runs` (self-hosted run in flight = trap 5 territory).
- Sessions are rm-eligible only when their run has ended AND they are older
  than `--keep-hours` (default 48). The transcript survives in the run dir
  (export.json + stdout.log), so spawned sessions are disposable.

## Phases

### Phase 1 - liveness truth

- [x] Fix `pid_alive`: `os.kill(pid,0)` is TerminateProcess on Windows - it
      KILLED probed workers (verified empirically). Now OpenProcess +
      GetExitCodeProcess, plus a process-creation-time check against
      `started_at` to defeat pid reuse.
- [x] `step_live` + `effective_statuses`: `running` w/o live pid (or stale
      no-pid record past timeout+15min) displays as `lost`; `queued` is
      `lost` when the run ended or went quiet >10min with nothing live.
- [x] Dashboard renders `lost` (gray), header counts only truly-live
      running; `cmd_status` shows effective status and tolerates minimal
      state.json files (repro3 KeyError fixed).

### Phase 2 - cleanup tools

- [x] `conductor.py sweep` - re-marks orphaned records `lost`, closes runs
      with nothing alive; auto-runs at `serve` startup; ran once: 10 stale
      records from the trap-5-era smoke runs are now `lost` (dashboard went
      from "6 running" to 0).
- [x] `conductor.py sessions` - lists conductor-spawned `devin` sessions
      per work_dir (`devin list` is cwd-scoped); `--rm` deletes eligible
      ones via `devin rm --force`; `--keep-hours N` policy knob.
- [x] `data/runs/repro3` deleted; `cmd_status` hardened against its shape.
- [x] `projects.toml`: `cloud_ok = true` for agent-conductor (apartment-3d
      already had it). Caveat recorded: agent-conductor has NO git remote,
      so cloud steps still can't see it until the repo is pushed.

## Where it stopped

**Done.** 34/34 tests green, demo 2/2 green, live sweep cleared the phantom
rows. Next: the first real manifest (queue "not yet filed" item) - and the
human must decide whether to push agent-conductor to GitHub before a cloud
step can actually run on it.

## Verification

`python run_tests.py` 34/34 green; `python conductor.py demo` done/done;
`python conductor.py sweep` on `data/runs` re-marks stale rows;
`python conductor.py sessions` lists the 6 real spawned sessions.
