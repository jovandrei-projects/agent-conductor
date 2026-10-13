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
- [ ] Live `devin -p` smoke test once authed (manifest ready: examples/smoke.toml, targets agent-conductor itself)

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

Once authed:

    cd C:\Projects\agent-conductor
    python conductor.py run examples/smoke.toml
    python conductor.py serve     # then http://127.0.0.1:8773

Then check `python conductor.py status`, `devin list` in the project dir,
and `data/runs/*/*.export.json`. Open questions the live run answers: does
`-p` run the full agentic loop, do spawned sessions appear in `devin
list`/Desktop, what does ATIF export contain. Fallback if `-p` proves
single-turn-only: drive `devin acp` (JSON-RPC stdio) - not built yet.

Traps already hit and documented in AGENTS.md: `-p` positional-after-flag
is parsed as PATH not prompt (use --prompt-file); `is_git_repo` must
require repo ROOT (nested dirs inside a parent repo would checkpoint the
parent - happened, fixed, regression test added); `--permission-mode`
flag wants `dangerous`, not `bypass` (normalized via MODE_FLAG); Desktop
login != CLI credentials.toml.

## Deferred or blocked

- Live verification of `devin -p` behavior - blocked on terminal-side
  `devin auth login` (Desktop-side login does not count; see trap 3).
- Cloud steps: `devin --cloud -p` repo selection behavior unknown - verify live.
- notes-ui relation: dashboard is standalone (:8773, loopback). Phone view
  of runs already works via hub.db rows on tasks-ui (:8762). A bridge proxy
  for :8773 is a Phase-3 option needing a mobile-apps change + redeploy.

## Verification

`python run_tests.py` green (20); `python conductor.py demo` prints
done/done; `curl http://127.0.0.1:8773/api/state` returns runs+agents JSON.
