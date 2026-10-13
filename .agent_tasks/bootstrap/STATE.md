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
- [x] tests/mock_devin.py + test_conductor.py - 12 tests green
- [x] projects.toml + examples/night.example.toml
- [x] AGENTS.md / ROADMAP.md / QUEUE.md / this file
- [ ] Commit scaffold
- [ ] Live `devin -p` smoke test once authed
- [ ] One real task on a real project (low-risk pick)

### Phase 2 - stretch tonight

- [ ] Parallel `wave` steps via git worktrees (if v1 stays green)
- [ ] hub.db run rows verified against mobile-apps task.py

## Where it stopped

`python run_tests.py` green (12 tests), `conductor.py demo` works, `plan`
prints resolved steps. Next: commit scaffold; then poll `devin auth status`
for the user's login and run a real `devin -p` smoke step on a scratch dir.

## Deferred or blocked

- Live verification of `devin -p` behavior (full agentic loop? session visible
  in Desktop?) - blocked on `devin auth login` by the user.
- Cloud steps: `devin --cloud -p` repo selection behavior unknown - verify live.

## Verification

`python run_tests.py` green; `python conductor.py demo` prints done/done.
