# Task: integration-regressions

**Status:** done
**Started:** 2026-10-16   **Last touched:** 2026-10-16

## Objective

Verify the landed workflow-ux-safety wave features together against an
isolated conductor instance + synthetic runs - existing suite, mock demo,
live dashboard probe, and merge-pipeline e2e - without touching real runs.

## What was verified

- Full suite: 423 tests green (`python run_tests.py`), built-in mock demo
  two steps done.
- Browser probe (Playwright, isolated `serve` + synthetic run dir, scratch/
  e2e_ui_probe.py): 60/60 checks - followup chip on run row, task modal
  prev/next + Arrow keys, in-modal session summary with its own dnav,
  Back chain task<->summary<->request, full trajectory + Expand all +
  per-entry Copy, ctx menu items + disabled-Delete-while-live + Esc +
  native menu preserved on editable fields, request modal dirty pend-note
  show/revert, paperclip + hidden file input + thumbnail + remove,
  Regenerate confirm naming unsaved TOML edits, model combobox groups,
  Interrupt+instruct composer (instructions + attach + sentinel POST
  carrying both), trash table + restore.
- Deferred merge e2e (scratch/e2e_deferred.py): 22/22 - sequential
  deferred continuity on non-main target (baseline-<slug> FF only over
  verified tips), alternatives parked separately, manual `merge` stages
  + re-verifies + lands atomically, conflict recovery via merge_resolver
  inside the stage worktree, pending entries retained on failure.
- Gen-during-merge e2e (scratch/e2e_gen_during_merge.py): 10/10 -
  request created while MERGE_HEAD open (data/ ignored writes OK),
  planner completed mid-merge into spool, publish deferred to merge end,
  owned manifest + plan_sessions + plan_gen_id populated.
- Apartment evidence (data/runs/20261014-235630-apartment-play-edit
  state.json): mobile-parity-validation done on attempt 2 (dialog-burned
  attempt 1 committed before respawn); mobile-* steps carry honest
  notes/next; no step claims device-lease coverage it never had.

## Regressions found + fixed

- `intrComposerHTML()` emitted the composer attach-strip without
  `id="d-instr-attach"` - `renderIntrAttach()` could never find it, so
  images attached in the Interrupt+instruct composer uploaded but never
  displayed and could not be removed (POST still carried them). Fixed in
  conductor.py; regression assert added to InterruptInstructTests
  (JS harness, `id="d-instr-attach"`).

## Deliberately not fixed (limitations, not regressions)

- `mobile_device.py` post-tap verification can report RC_TIMEOUT if
  uiautomator returns a persistently stale dump after a successful tap.
  Bounded and fail-safe (diag capture + honest error); never reports a
  false OK. A cross-check against `dumpsys window` focus is possible
  follow-up but out of scope.
- Three parked branches hold real unlanded work (verified not in HEAD):
  `isolate-worktree-services` (per-worker loopback port lease +
  CONDUCTOR_* env), `project-merge-targets` (manifest `merge_target` key
  + merge worktree + branches API), `deferred-merge-controls` (request
  form Merge timing + run-modal pending panel). They are the run's
  pending_merges - retained by design, await human `conductor.py merge`.

## Where it stopped

Task complete; see QUEUE.md tail entry.

## Verification

`python run_tests.py` + `python conductor.py demo`; probes under
`.agent_tasks/integration-regressions/scratch/`.
