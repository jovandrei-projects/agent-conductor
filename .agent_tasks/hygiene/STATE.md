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
rows. Clutter cleanup done same day: `sessions --rm --keep-hours 0` removed
the 6 recorded spawned sessions, 26 unrecorded spawned orphans removed by
`devin rm` (34 -> 2 sidebar entries, the 2 human sessions kept); `data/runs`
pruned to 2 run dirs (kept `20261013-093553-smoke` + `20261013-182727-demo`);
agent-conductor registered as a managed service in project-hub
`services.json` (commit a71a194 there). Second cleanup pass: `data/` pruned
of repro files + ~50 empty worktree dirs; `manifests/` created as the
convention for real manifests (its README documents location + run syntax).
Doc fix: `devin list` interactive picker is GLOBAL ("all sessions"); only
`--format json|csv` is cwd-scoped.

2026-10-13 ~23:30 follow-ups (same task, reopened):
- Demo sandbox REVERTED on user call: `demo` writes to `data/runs` again -
  all projects share the convention; task column disambiguates demo rows.
- Dashboard: agents split into per-project tables; new `task` column
  (step's prompt text; old records fall back to extracting the `## Task`
  body of the rendered prompt file) + `effort` column.
- `effort` manifest field: fuses as a model-variant suffix on `--model`
  (`model="gpt-6-luna"` + `effort="min"` -> `gpt-6-luna-min`); unknown
  variant errors loudly at spawn. Docs say check `devin models list` for
  exact names (needs auth).
- Session cleanup AUTOMATED per user approval: `sweep_sessions` runs the
  same 48h whitelist policy inside `sweep` and once at `serve` startup.
- Fixtures: `data/demo1` (books.csv catalog) + `data/demo2` (notes/) via
  `examples/make_demo_projects.py`, registered as slugs in projects.toml;
  manifests `examples/demo1.toml` / `demo2.toml` / `demo-mixed.toml` (mixed
  wave-1 parallel across projects). Live run pending auth.
- BLOCKER: `devin auth status` reports not logged in again (~14h after the
  morning login) - real demo runs queued on `devin auth logout && login`.
  Verified `devin -p` is genuinely blocked (auth status isn't lying) and the
  CLI has NO refresh command - interactive login is the only renewal.
- Dashboard now probes `auth status` (2min cache) and shows a red
  `AUTH: NOT LOGGED IN` banner + `[log in]` (pops a real terminal running
  `devin auth logout & devin auth login` - POST /api/auth/login) and
  `[recheck]` (POST /api/auth/recheck busts the cache) controls.
- Dashboard: project tables now show the latest 10 agents each with a
  "show N older" toggle - history stays in state.json, just not rendered.
- New manifests: examples/demo-wt-merge.toml (same-project wave, different
  files -> clean merge) and demo-wt-conflict.toml (same-file wave -> second
  merge conflicts, step failed, worktree preserved). Regression tests lock
  both paths + resume-of-lost-step; mock lines are now session-unique so
  parallel same-file writes actually diverge.
- Token TTL answered by docs: CLI tokens do NOT expire by default - dead
  means revoked/rejected server-side. Two observed cases; watch the next
  login for a pattern.

## Verification

`python run_tests.py` 34/34 green; `python conductor.py demo` done/done;
`python conductor.py sweep` on `data/runs` re-marks stale rows;
`python conductor.py sessions` lists the 6 real spawned sessions.
