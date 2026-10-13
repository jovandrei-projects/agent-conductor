# Task: bootstrap

**Status:** in progress
**Started:** 2026-10-13   **Last touched:** 2026-10-13

## Objective

`conductor.py` exists, is tested end-to-end against a mock devin, and can run
a real manifest the moment `devin auth login` has been done once.

## Not in scope

- Parallel worktrees, hub.db per-step rows, cloud live test (Phase 2/3).
- Devin Desktop UI automation.

## Decisions already made

- Script-not-agent orchestrator; handoff via project `.agent_tasks` files.
- `bypass` workers + conductor git checkpoints = the safety model.
- `projects.toml` `cloud_ok` gate protects the three private projects.
- Agent tracking: `state.json` step records are the registry (pid, times,
  paths); `conductor.py serve` renders them - no second store.

## Phases

### Phase 1 - build

- [x] Scaffold from _templates/new_project, git init
- [x] conductor.py: plan/run/status/demo, checkpoints, verify, resume
- [x] tests/mock_devin.py + test_conductor.py - 20 tests green
- [x] projects.toml + examples/night.example.toml + examples/smoke.toml
- [x] AGENTS.md / ROADMAP.md / QUEUE.md / this file
- [x] Parallel `wave` groups via git worktrees + merge-back
- [x] hub.db run row live-updated per step (`--no-hub` opt-out)
- [x] Cloud session URL capture from stdout
- [x] Commit everything (7 commits)
- [x] `[run] max_hours` wall-clock cap; keep-awake power request during runs
- [x] `[run] max_parallel` resource cap (auto ~1 per 4 cores, max 4) + `queued` marking; free RAM recorded per run
- [x] Agent registry in state.json (pid, work_dir, branch, started/ended/elapsed, session, output paths)
- [x] `conductor.py serve` live dashboard on 127.0.0.1:8773 (+ `/api/state`)
- [x] Live `devin -p` smoke test once authed - ran 2026-10-13 ~09:00; sessions spawn and do real work, but `devin -p` detaches so conductor misjudged completion (trap 5). README.md landed from the still-running readme session

### Phase 2 - stretch tonight

- [x] Parallel `wave` steps via git worktrees
- [x] hub.db run rows verified against mobile-apps task.py

## Where it stopped

Session ended **2026-10-13 ~02:35** (the "ending time" the user asked to
register - this line is it). Everything is built, tested (20 green) and
documented; dashboard verified live against demo runs.

**Auth is still the blocker, and the diagnosis changed:** the 1:36am login
went to *Devin Desktop* (host credentials passed to `devin acp`), NOT to the
standalone CLI's `%APPDATA%\devin\credentials.toml` (file untouched since
Aug 26; `auth status` still says not logged in; `devin -p` errors "Not
logged in"). The login TUI reads the console directly, so it can't be
driven non-interactively - **the user must run `devin auth login` in a real
terminal** (or `--force-manual-token-flow` + code from app.devin.ai).
Hub question #256 posted to the phone queue with these instructions.
Full details: AGENTS.md trap 3.

**Update 2026-10-13 ~08:30:** the 02:28 smoke run aborted at the auth gate
(`run_abort reason="auth"` in its events.jsonl) - correct behavior. New
wrinkle: `devin auth login` claimed "already logged in" because
credentials.toml EXISTS, while `auth status` still rejects the stored
Aug-26 token. The command to unblock is `devin auth logout` FIRST, then
`devin auth login` (AGENTS.md trap 4). Also fixed today: abort/validation
paths in cmd_run now write `ended_at` + drop the hub row, so aborted runs
no longer display as "running" forever on :8773. The stale smoke run's
state.json and hub row #257 were patched by hand.

**Update 2026-10-13 ~09:05 - auth fixed, smoke ran, new blocker found.**
`python conductor.py run examples/smoke.toml` executed real sessions
(runs 20261013-084732 and -085914). The smoke readme session wrote and
committed `README.md`. But `devin -p` exits ~0.1-1.8s after registering
the session: stdout logs empty, no export.json, step `done`+checkpoints
commit nothing, and the actual agents keep running detached (visible in
`devin list`; several stale "spawned unattended" sessions may still be
live). Step gating must track session completion, not subprocess exit -
see AGENTS.md trap 5. That is now the blocker for any real manifest.

**Update 2026-10-13 ~09:22 - readme step confirmed landed.** A later
spawned session re-checked `README.md`: present (62 lines), committed in
88d8ea3, and accurate against conductor.py (all manifest fields, flags,
and paths verified). No changes needed; the step stays done.

Next session's first job: find how to observe a spawned session's state
(`devin list` is a TUI; look for a non-interactive flag, `devin acp`
JSON-RPC, or the ATIF export appearing only at session end), then gate
`step_end` on it. Also worth checking: whether `-p` streams if stdout is
a TTY, and whether the captured `session_id` (`showy-headlight`) maps to
the display slug (`gleaming-magnolia`) at all.

Traps already hit and documented in AGENTS.md: `-p` positional-after-flag
is parsed as PATH not prompt (use --prompt-file); `is_git_repo` must
require repo ROOT (nested dirs inside a parent repo would checkpoint the
parent - happened, fixed, regression test added); `--permission-mode`
flag wants `dangerous`, not `bypass` (normalized via MODE_FLAG); Desktop
login != CLI credentials.toml.

**Update 2026-10-13 ~09:23 - nightly smoke run green.** `run_tests.py
--quick` 20/20, `conductor.py demo` 2/2 done (run `20261013-092326-demo`).
Outcome appended to ROADMAP.md Phase 2. The `-p` detach blocker is
unchanged - it lives above the mock layer, so unit/demo green does not
mean live manifests work yet.

## Deferred or blocked

- Session-completion tracking for `devin -p` steps - the subprocess
  detaches (trap 5); blocked on finding a non-interactive session-state
  channel (`devin acp`, a `devin list --json`-style flag, or ATIF).
- Cloud steps: `devin --cloud -p` repo selection behavior unknown - verify live.
- notes-ui relation: dashboard is standalone (:8773, loopback). Phone view
  of runs already works via hub.db rows on tasks-ui (:8762). A bridge proxy
  for :8773 is a Phase-3 option needing a mobile-apps change + redeploy.

## Verification

`python run_tests.py` green (20); `python conductor.py demo` prints
done/done; `curl http://127.0.0.1:8773/api/state` returns runs+agents JSON.
