# Task: run-control-taxonomy

**Status:** done
**Started:** 2026-10-15   **Last touched:** 2026-10-15

## Objective

One consistent naming system for the dashboard run/step controls, covering:
(a) graceful pause + resume, (b) resume after ungraceful interruption,
(c) skip/unskip a pending step in place, (d) stop the running step while the
run proceeds. Every visible action gets a distinct self-explanatory name plus
tooltip/confirm text saying exactly what it does.

## Audit findings (what existed)

- `Pause run` = hard-kill of live steps + `aborted='paused'` - identical
  mechanics to `Stop run now`, only the label differed.
- `Drain & stop` = graceful halt (`aborted='drained'`) - the actual "pause
  gracefully" semantics under a jargon name.
- `Stop this agent` killed a step AND halted the whole run, because
  `stopped` tripped `on_failure='stop'`.
- No skip/unskip existed; `skipped` was only a halt byproduct. Pending
  steps had dashboard rows only for request-launched runs.

## Final names (shipped)

- **Pause run** (live run): in-flight steps finish, nothing new launches,
  `aborted='paused'`. = old Drain mechanics + paused label.
- **Stop run now** (live run): kill live steps, nothing new launches,
  `aborted='stopped'`. Unchanged.
- **Resume unfinished steps** (ended/interrupted run): unchanged action;
  confirm text now says it covers paused/stopped/interrupted.
- **Stop this step** (running step): kills the agent; the run proceeds -
  `stopped` no longer trips on_failure (post-loop flag check keeps the
  final-wave bookkeeping working).
- **Skip step** / **Unskip step** (queued/waiting/user-skipped): new
  persistent `skip-<key>-<step>` tempdir sentinel, durable across resume
  (excluded from clear_run_flags), honored in run_step's pre-spawn check.
- `Drain & stop` button merged into Pause run; `POST /stop {drain:true}`
  kept for API back-compat (records `drained`). `/pause` on a legacy
  (pre-flags-v2) run also raises drain flags - old drivers only poll
  stop/drain.
- CLI-launched runs now render manifest-declared pending rows
  (waiting/skipped) so a not-yet-spawned step is visible and skippable;
  `skipped_by_user` marks user-skips so the modal offers Unskip.

## Where it stopped

Done. conductor.py + tests + docs committed; 164/164 mock tests and demo
green.

## Verification

`python run_tests.py` -> 164 tests OK (~290s); `python conductor.py demo`
-> {'done': 2}.
