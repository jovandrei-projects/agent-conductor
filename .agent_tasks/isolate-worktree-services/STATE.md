# Task: isolate-worktree-services

**Status:** done
**Started:** 2026-10-16   **Last touched:** 2026-10-16

## Objective

A worktree worker is isolated on *files* but shares the port space - a
dev server it launches (vite, `npm run dev`, `conductor.py serve`) can
bind the main workspace's port, collide with a sibling worker, or get
killed by somebody else's cleanup. Each local step must use its real
checkout and a distinct leased loopback port, propagate that context to
the worker, keep it in the record for review, and clean up only what it
owns.

## What shipped (conductor.py)

- `alloc_service_port`/`release_service_port`/`sweep_service_ports` +
  `_port_free`/`_lease_owner_alive`/`_own_pid_stamp`: lease files
  `svc-<port>.json` under `%TEMP%/agent-conductor-ports` (same pattern as
  the flag sentinels - never inside run_dir, trap 5). Range 8806-8859 -
  just past the C:\Projects port registry's last assignment (8805), so a
  worker never receives a registered project port. O_EXCL file create is
  the atomic claim between concurrent conductors; a 127.0.0.1 bind probe
  skips foreign listeners (never taken over, never killed); a lease
  records `pid`+`pid_created` so `pid_alive`'s reuse window reclaims a
  dead owner's lease. A fresh unparseable lease is a claim in flight
  (LEASE_CLAIM_GRACE_S), not corruption.
- `run_step` gains `repo_dir` (defaults to work_dir); every non-cloud
  step leases a port before the prompt is rendered and releases it when
  the step ends. Rec keeps `service_port`/`service_url` for review; a
  still-bound port at release lands a note (a leaked dev server is
  reported, never port-killed - a bound port doesn't prove the owner).
- Prompt: PREAMBLE gains a `{service_note}` slot (empty when nothing to
  declare - byte-identical prompt for cloud steps and custom preambles).
  `_service_note` names the isolated worktree + the off-limits shared
  checkout, and the leased port + env vars + the "never take over/kill a
  listener you didn't spawn" rule.
- Env: the worker Popen gets `CONDUCTOR_RUN`/`STEP`/`RUN_DIR`/`WORK_DIR`
  /`REPO_DIR`, `CONDUCTOR_BRANCH` (worktree steps), and
  `CONDUCTOR_SERVICE_PORT`/`CONDUCTOR_SERVICE_URL` - inherited context
  vars are popped first so a nested run can't leak its parent's.
- `serve`: `--port` now defaults to the step's leased
  `CONDUCTOR_SERVICE_PORT` (`env_service_port`), else 8773 - a worktree
  worker's own dashboard no longer collides with the main one.
- `sweep`/`serve` startup also reclaim dead-owner port leases.
- Dashboard: agent records carry `service_port`/`service_url`; the task
  modal shows a `service` row (leased/released).
- `tests/mock_devin.py` echoes its `CONDUCTOR_*` env into stdout so tests
  can assert propagation from the step's stdout log.
- New `tests/test_services.py` (17 tests): lease alloc/races/contention/
  staleness/release/sweep, env parsing, and run_manifest e2e - prompt +
  env + state carry the port, wave members get distinct ports and their
  own worktree paths, retries keep the lease, a stopped step releases it,
  a skipped step never touches one, cloud steps get none.

## Where it stopped

DONE - 17/17 new tests green standalone. Full suite + demo pending at
session end; committed in this worktree, merge handled by the run.

## Deferred or blocked

- The lease is advisory: a worker may still bind a default port if it
  ignores the prompt/env (nothing prevents it); the note is the contract.
- Leaked listeners that survive kill_tree are only *reported* (state
  note), never port-killed - deliberate: a bound port doesn't prove
  ownership.
- The post-step checkpoint (`git add -A` in the worktree) could adopt
  debris a worktree server wrote into its own checkout; mitigated by the
  fact the server runs inside the worktree, not the main checkout.

## Verification

`python run_tests.py` and `python conductor.py demo` (mock-only, no auth).
