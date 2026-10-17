# Task: run-review-ux

**Status:** in progress
**Started:** 2026-10-16   **Last touched:** 2026-10-16

## Objective

Make runs and tasks easy to review after completion: a human review state
that is independent of execution outcome, truthful outcome summaries, stable
modal/table interactions, cumulative timing, a "wt" run label, identified
watcher warnings, and a manual merge that can't fire mid-run. Driven by
manifest `manifests/20261016-100358-0d289b96-run-review-ux.toml` (9 steps).

## Not in scope

- Implementing the features here - this file only records the design.
- `plan-proposal` / `plan-proposal-N` rows getting review indicators
  (decided: they are preparation history, not run work - see DESIGN.md).
- Changing STATUS:/NEXT: worker contract (kept; SUMMARY: may be added).

## Decisions already made

See `DESIGN.md` for full reasoning + sources. Headlines:

- Review state = per-run + per-task `{seq, at, reviewed}` mark vs a
  `completion_seq` counter stamped at each terminal transition. An ack only
  covers the seq it saw - a re-completion after resume needs review again.
- Storage = `review-<flagkey>[-<step>]` sentinel files in
  `%TEMP%/agent-conductor-flags` (trap-5 safe), folded into state.json on
  the driver's next save; dashboard overlays flags live like
  prompt/rnote/skip already do.
- Explicit buttons "Mark as reviewed"/"Mark as needing review"; opening a
  modal never acknowledges.
- One attention accent for both levels, text always present (WCAG 1.4.1).
  Suggested green-run accent REJECTED (collides with done/#3fb950 and
  would paint failed runs green); orange-task acceptable but collides with
  timed_out/#f0883e - recommend a free hue instead.
- Historical records get no `completion_seq` -> no indicator, no migration
  write. Legacy steps can still be manually marked either way.

## Phases

### Phase 1 - inspect + design (this step)

- [x] Inspect tables/modals, completion reporting, persistence,
      timing/resume, watcher warning, manual merge flow
- [x] Research public unread/review-ack precedents (URLs in DESIGN.md)
- [x] Choose model + evaluate suggested accents
- [x] Decide historical-record + resume-completion semantics
- [x] Record touchpoints + test gaps for the implementation steps

### Phase 2 - implementation (manifest steps, other sessions)

- [x] review-workflow - the model above (done 2026-10-16, see below)
- [x] task-outcome-summary - OUTCOME: line -> rec["outcome"]/["outcomes"]
      (bounded history x8) -> modal/table; STATUS:/NEXT: untouched
      (landed on master, merge d57e9c2)
- [x] task-table-navigation - name-targeted clicks + .dsel selection +
      run-name filter + prev/next sibling nav
      (landed on master, merge 12fbe2b)
- [x] modal-refresh-stability - landmark/scroll-anchored modal rebuilds
      (landed on master, merge 8cb3988)
- [x] cumulative-elapsed-time - exec_intervals on steps+run, _now() seam,
      elapsed column + live tick
      (landed on master, merge d99c4e4)
- [x] execution-context-labels - run-level wt chip (_run_isolation) +
      watcher identity strings
      (landed on master, merge 6b1418c)
- [x] manual-merge-guard - run_executing + merge-<runkey> claim + resume
      serialization
      (landed on master, merge e5b470d)
- [x] integration-verification - combined merge of the three parked
      branches + full verification (below)
      (landed on master, merge e6655ad)
- [ ] follow-up-plan

## Where it stopped

`integration-verification` done 2026-10-16. The three parked branches
(review-workflow, cumulative-elapsed-time, manual-merge-guard) were
merged into the integration worktree on top of the four already-merged
steps and verified TOGETHER in the combined state:

- `python run_tests.py`: 457/457 OK (~13min - over the 600s verify cap,
  see below). One defect found + fixed during merge: a
  tests/test_conductor.py splice read `git show` output through a
  PowerShell pipe, so non-cp1252 bytes became literal '?' and broke JS
  regex asserts (8 failures); rebuilt the file from af996d3 + branch
  class + manual-merge-guard diff with raw-byte subprocess reads.
- `python conductor.py demo`: green into this worktree's data/runs; the
  produced run dir exercised live: run+step needs_review=True ->
  set_step_review/set_run_review acks fold into state.json (sentinel +
  folded mark both verified), stale-seq ack 409s, 'Mark needing review'
  tombstone re-flags, a simulated re-completion (completion_seq bump)
  re-flags despite the older ack, re-ack at seq 2 quiets again.
- `python conductor.py serve` smoke on the worktree data: /api/state
  carries needs_review/completion_seq/elapsed_s/exec_start/executing;
  POST /api/runs/<d>/review + .../steps/<s>/review return 200 and flip
  needs_review end-to-end over HTTP.
- run_executing verified live: None on the ended demo run, driver-alive
  reason on a live-pid stamp, per-step reason on a live step with dead
  driver; merge guard + claim + resume-side serialization covered by
  the merged test classes (worktrees + test_conductor).
- `python conductor.py plan <manifest>` green.
- Outcome/status contract verified on the demo record: status/
  status_line/next intact beside outcome/outcomes - machine handoff
  fields untouched, human summary additive.
- STATUS/NEXT consumers: run_manifest/driver path + cmd_status exercised
  by demo + suite.

Prior merge notes (how the individual steps reached master):

`manual-merge-guard` merged to master 2026-10-16 (merge e5b470d) via
merge-resolver session — conflicts were QUEUE.md bookkeeping appends,
the clear_run_flags sentinel exemption ('review-' vs 'merge-', union
kept), the cmd_run resume stamp (claim try/finally + live-driver
refusal wrapping the exec_intervals/prev_end seeding), and the
dashboard 'alive' payload var; 22/22 touched-path tests green.
Details in `.agent_tasks/manual-merge-guard/STATE.md`.

`cumulative-elapsed-time` merged to master 2026-10-16 (merge d99c4e4)
via merge-resolver session — conflicts were QUEUE.md bookkeeping
appends, the ended_at/_run_iv_close stamp sites vs _bump_completion,
dashboard payload fields (elapsed_s/exec_start vs review marks), the
run-modal kv rows, and shared-harness class tails in tests; both sides
kept, 446/446 suite green on the merge. Details in
`.agent_tasks/cumulative-elapsed-time/STATE.md`.

`review-workflow` merged to master 2026-10-16 (merge bcade8f) via
merge-resolver session after the wave merge conflicted on bookkeeping
appends + shared-harness class tails (kept both sides; 437/437 suite
green on the merge). It was implemented + verified on the branch
(290/290 `python run_tests.py`, demo + `plan` green). What landed:

- `completion_seq` bumps at every terminal-outcome write: run_step
  terminal update, pre-spawn non-repo fail, resume merge-finalize flips,
  halt_remaining run close, auth aborts, end-of-run ended_at, sweep
  lost-mark + run close, _merge_run + _merge_deferred_group flips.
- `review-<flagkey>[-<step>]` sentinels (JSON `{reviewed, seq, at}`) in
  %TEMP% - trap-5 safe - folded into state.json by save_state's new
  `_fold_review_flags`, exempt from clear_run_flags, overlaid live in
  collect_dashboard (newer `at` wins).
- Endpoints: POST /api/runs/<d>/review and .../steps/<s>/review
  (`set_run_review`/`set_step_review`/`_set_review`). Body `{reviewed}`
  + optional `seq` guard: an ack against a stale view 409s; 'mark
  reviewed' with nothing to review (seq-less, no tombstone) also 409s.
  Run and task marks are fully independent.
- Dashboard: `completion_seq`/`review`/`needs_review` on run + task
  dicts; `.stage.unreviewed` cyan pill (#39c5cf - the free hue), left
  row inset + bolded task name + `· unreviewed` aria suffix; 'review'
  kv row + Mark-as-reviewed/needing-review button in BOTH modals and
  both ctx menus (same data-review-run/review-step hooks). Run-level
  seq mismatch is gated on `ended_at` so a resumed in-flight run does
  not resurrect the old flag; a LIVE step suppresses its flag rather
  than dropping seq (pop would let an old ack collide with the recycled
  seq - documented in collect_dashboard).
- Failure stays failure: the pill is a sibling of status, never a
  recolor; `ReviewWorkflowTests` pins failed+unreviewed coexisting.
- 14 new tests: seq stamping, ack/re-flag persistence, repeat saves,
  run/task independence, legacy no-autoflag + manual marks, stale-seq
  409, flag overlay + fold, clear_run_flags exemption, sweep lost bump,
  resume re-completion re-flag, HTTP endpoint statuses, Node JS badge/
  modal/ctx/dispatch regression.

Next session = follow-up-plan (the run's last step), then the umbrella
task can close.

## Deferred or blocked

Nothing parked by this step. Carried risk, now MEASURED: `run_tests.py`
takes ~783s vs the 600s hard verify cap in `run_step` - any step whose
manifest verify is `python run_tests.py` will now time out post-verify
even on a green suite. This step's own outer verify hits it. Follow-up
material: raise/config the cap, or shard the suite.

Manual checks not covered by tests: a real-browser pass over the
combined dashboard (node-harness tests cover row/modal behavior, not
rendered CSS). Watch out: `tests.log` is rotating - long runs overwrite
early lines.

## Verification

`python run_tests.py` -> 457/457 OK (combined state, ~783s);
`python conductor.py demo` green; `python conductor.py plan <manifest>`
green; live serve smoke on real run data (review POSTs + state payload);
isolated exercises of review ack/reversal/stale-seq/re-completion and
run_executing on the produced demo run dir.
