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

## Phases

### Phase 1 - build

- [x] Scaffold from _templates/new_project, git init
- [x] conductor.py: plan/run/status/demo, checkpoints, verify, resume
- [x] tests/mock_devin.py + test_conductor.py - 15 tests green
- [x] projects.toml + examples/night.example.toml + examples/smoke.toml
- [x] AGENTS.md / ROADMAP.md / QUEUE.md / this file
- [x] Parallel `wave` groups via git worktrees + merge-back
- [x] hub.db run row live-updated per step (`--no-hub` opt-out)
- [x] Cloud session URL capture from stdout
- [x] Commit everything (7 commits)
- [x] `[run] max_hours` wall-clock cap; keep-awake power request during runs
- [ ] Live `devin -p` smoke test once authed (manifest ready: examples/smoke.toml, targets agent-conductor itself)

### Phase 2 - stretch tonight

- [x] Parallel `wave` steps via git worktrees
- [x] hub.db run rows verified against mobile-apps task.py

## Where it stopped

Everything is built and mock-tested (15 tests green). **Blocked on
`devin auth login`** - the user said they'd run it tonight. Once live:
`python conductor.py run examples/smoke.toml` runs 2 real `devin -p`
workers against agent-conductor itself (write README.md, run tests).
A background watcher in the building session polls `devin auth status`
every 60s.

Traps already hit and documented in AGENTS.md: `-p` positional-after-flag
is parsed as PATH not prompt (use --prompt-file); `is_git_repo` must
require repo ROOT (nested dirs inside a parent repo would checkpoint the
parent - happened, fixed, regression test added); `--permission-mode`
flag wants `dangerous`, not `bypass` (normalized via MODE_FLAG).

## Deferred or blocked

- Live verification of `devin -p` behavior (full agentic loop? session visible
  in Desktop?) - blocked on `devin auth login` by the user.
- Cloud steps: `devin --cloud -p` repo selection behavior unknown - verify live.

## Verification

`python run_tests.py` green; `python conductor.py demo` prints done/done.
