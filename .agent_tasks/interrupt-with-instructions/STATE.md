# Task: interrupt-with-instructions

Wave step of run 20261016-013719-workflow-ux-safety. Conventions: `../README.md`.

**Status:** done
**Started:** 2026-10-16   **Last touched:** 2026-10-16

## Objective

A running step can be steered without losing its work: the operator types
supplemental instructions (+screenshots), confirms once, and the SAME step
resumes in a fresh session with its worktree/commits/context intact - never
a retry, never a manual stop+resume+notes sequence.

## Not in scope

- Queueing a second request while one is pending (refused 409 by design).
- Editing the original prompt post-dispatch (notes channel only, same as
  resume notes).
- A generic "send message to running session" channel - this is strictly
  stop+respawn-with-additions.

## Decisions already made

- `intrq-<key>-<step>` JSON sentinel polled by wait_worker in the same slot
  as a stop flag - submission and the kill land under one name, and only
  the driver acts (no respawn from the endpoint, so a dead driver never
  orphans a replacement).
- Fold into the `rnote-` channel as an `INTERRUPT <ts>` block, then delete
  the sentinel: instructions apply exactly once across driver restart
  (clear_run_flags preserves intrq- like rnote-; a leftover folds at the
  next dispatch's pre-spawn read).
- Respawn is a loop iteration of run_step, not a retry: `attempt`/
  `max_attempts` untouched, `rec.interrupts` counts respawns, artifacts
  are `*.interruptN.*` like `*.attemptN.*`.
- Late arrivals (STATUS already in log, verify/teardown window, or a real
  stop flag in the same slice) bank into notes instead of respawning -
  natural completion and user stop both win their terminal status.
- `state["intrq"]=True` stamps drivers that poll the sentinel (same
  capability-mark scheme as `flags:"v2"`); old-driver runs get 409 + the
  stop+notes workaround.

## Phases

### Phase 1 - backend

- [x] intrq_flag sentinel + _intrq_to_notes fold + INTERRUPT_NOTE +
      clear_run_flags preservation
- [x] run_step respawn loop: kill -> commit product -> fold -> respawn
      same step; late/stop-wins banking; post-loop straggler sweep
- [x] POST /api/runs/<dir>/steps/<step>/interrupt (gates: running+live+
      intrq-capable, payload validation, pending dedupe, atomic write)
- [x] collect_dashboard agent fields intr_pending + interrupts

### Phase 2 - dashboard

- [x] Task-modal composer (open = view change only, Cancel free, explicit
      confirm on send, error surface in-place)
- [x] Attachment reuse (intrAttach per-agent buckets, paperclip + drop +
      paste, refs ride the POST body)
- [x] Progress view (pending -> stopping -> resuming -> done off
      intr_pending/interrupts) + row badges (·interrupting, ·intrxN)
- [x] Running-row context menu entry + tombstone when the step ends
      mid-compose + d-instr draft/caret survival across the 2s rebuild

### Phase 3 - tests + docs

- [x] 5 python tests (e2e respawn+attachments, endpoint gates+dedupe,
      restart-exactly-once, late-arrival bank, stop-beats-interrupt)
- [x] 1 node harness test (menu gating, open/cancel free, send body,
      pending disable, tombstone)
- [x] AGENTS.md feature paragraph
- [x] Full suite + demo green (303/303 + demo 2/2); commits

## Where it stopped

Done - 303/303 suite + demo green, 1 regression found+fixed on the way
(attachErr mis-routed to the composer's notice when the request form was
the target; the picker change-listener also needed identity compare,
not .id, for the harness's id-less stub nodes). Landed with the wave's
commit series; merge back is the wave's job.

## Deferred or blocked

- Worktree-path e2e (respawn-in-same-worktree is by construction - the
  loop never re-runs worktree setup and commits land on the step branch).

## Verification

`python run_tests.py` + `python conductor.py demo`; focused:
`python -m unittest tests.test_conductor.InterruptInstructTests -v`
