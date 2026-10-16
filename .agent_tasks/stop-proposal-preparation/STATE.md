# Task: stop-proposal-preparation

**Status:** done
**Started:** 2026-10-16   **Last touched:** 2026-10-16

## Objective

A visible "Stop proposal" action while the dashboard shows "Preparing
proposal" that cancels the planner `devin` process, restores the request's
previous editable state (draft inputs, selections, and any earlier valid
proposal kept), and lets the user edit + Generate again immediately - with
late planner output never overwriting restored or newer state.

## Not in scope

- Stopping `questions`-stage requests (already editable, nothing running).
- Deleting/cleaning planner session history - `*.gen.log`/`*.export.json`
  stay on disk.
- Changes to run-level stop/pause machinery (only reused: sentinel files,
  `wait_worker` stop_flags, `kill_tree`).

## Findings: the lifecycle (traced)

- `start_generation` / `answer_request` (refine pass) set `status="generating"`,
  add the id to `_GENERATING`, spawn `_finish_generation` on a daemon thread.
- `_finish_generation` -> `_planner_response` spawns `devin --print
  --prompt-file ... --export ...` in a tempdir and blocks in
  `wait_worker(proc, log, 600)` - **no stop_flags passed**: the only exits are
  process end or the 600s timeout. UI fields render disabled while
  `stage==='generating'` and there is no stop endpoint - that is the
  reproduction.
- Results are guarded by `if rec["status"] == "generating"` under
  REQUEST_LOCK at all three write points (questions park, publish, error) -
  a stop can restore state and every late write then no-ops, **except** the
  "stopped attempt + immediate regenerate" race needs an attempt id.

## Decisions already made

- Attempt identity: `rec["gen_id"]` minted per attempt (`secrets.token_hex`);
  `_GENERATING` became a set of gen_ids so a dying attempt can't discard
  its successor's membership; the "still mine?" checks compare gen_id.
- Cancellation reuses the sentinel mechanism: `stopgen-<request_id>` file in
  `%TEMP%/agent-conductor-flags`, polled via `wait_worker`'s existing
  `stop_flags` arg; `_GEN_PROCS[rid]` registers the live Popen so the
  endpoint also `kill_tree`s immediately (no 15s poll wait). Start/answer
  clear the flag before spawning (stale flag would kill a new attempt).
- Restore rule: `manifest` set -> `ready` (stale flag untouched), else
  `draft`; `error` cleared; `generation_stopped_at` recorded. Repeated stop
  or stop after completion -> 409, same as `stop_run` on an ended run.
- UI: Stop proposal button on the request modal (generating stage), the
  run-detail modal of a generating request row, and the plan-proposal agent
  modal - all POST `/api/requests/<id>/stop`.
- `stop_generation` keeps the `_assert_unwatched` guard like every other
  request mutation: a CLI-launched self-hosted run watching the repo
  refuses the stop (409) rather than tripping the run's trap-5
  edit-awareness.

## Phases

### Phase 1 - server-side cancellation

- [x] Trace lifecycle + confirm no stop path exists (above)
- [x] `gen_stop_flag`, `GenerationStopped`, `_GEN_PROCS`, gen_id plumbing
- [x] `stop_generation` + `/api/requests/<id>/stop` route
- [x] `_planner_response`: register proc, stop_flags, GenerationStopped
- [x] `_finish_generation`: gen_id-scoped writes + stopped handling +
  early bail before `_wait_writable` (superseded attempt can't spin hours
  against a self-hosted run's watch)

### Phase 2 - dashboard

- [x] Request modal: Stop proposal button + notice; `requestAction('stop')`
  with confirm, no field submission (nothing to clobber)
- [x] Run-detail modal + plan-proposal agent modal stop buttons
  (`data-stop-gen` delegated handler)

### Phase 3 - regression tests + verify

- [x] 7 new tests (`StopProposalTests`): draft restore + late output
  discarded, ready restore + byte-identical prior manifest, real
  `_planner_response` proc tree-kill via sentinel, stop->edit->regenerate
  race with both attempts live, repeated/idle/finished stop 409,
  GenerationStopped unit path, node-harness button + POST
- [x] `python run_tests.py` green: **216/216** (~296s)
- [x] `python conductor.py demo` green
- [x] Real-process smoke (`scratch/smoke_stop.py`): real sleeper proc
  tree-killed, record draft in 3.3s, thread unwound clean

## Where it stopped

Done - merged-ready state on `conductor/<run>/stop-proposal-preparation`.
Residual note: stopping an orphaned `generating` record (server died
mid-plan) intentionally works as an early stage-clear before the 660s
error fallback.

## Deferred or blocked

(none)

## Verification

`python run_tests.py` - 216/216 OK at 2026-10-16 ~02:11.
