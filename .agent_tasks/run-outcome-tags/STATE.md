# Task: run-outcome-tags

**Status:** done
**Started:** 2026-10-20   **Last touched:** 2026-10-20

Wave step of manifest run `20261020-030142-whats-new-polish`
(`manifests/20261020-025338-950fe3fc-whats-new-polish.toml`), on branch
`conductor/20261020-030142-whats-new-polish/run-outcome-tags`.

## Objective

Make the What's-new work-digest run tags proportionate: a run that
mostly finished must not wear the red `failed` word for a single lost
step. Display classification only - feed sectioning, ordering, grouping
and the persisted run/step verdicts are untouched.

## Not in scope

- Feed loading/collection, `_section`, folding, tour_state, tour_polish.
- Summary generation/rendering and chat typography (sibling steps).
- The persisted `state.json` step verdicts and the run modal's own
  status vocabulary - the tour tag is a display derivation only.

## Classification rule (tour_feeds._run_outcome)

- A recorded `aborted` word still wins verbatim (paused/stopped/
  drained/...) - a halt reason is the outcome, step verdicts never
  overrule it. A non-string flag falls through as before.
- `failed` / `timed_out` / `lost` are the **unsuccessful** verdicts;
  `done` + unsuccessful are the **decisive** verdicts
  (skipped/superseded/nonterminal don't count on either side).
- `failed` requires unsuccessful verdicts AND (zero done OR a strict
  unsuccessful majority of decisive verdicts, bad > done).
- `partial` = any unsuccessful verdict when at least one step is done
  and bad <= done - an even done/unsuccessful split is a tie, not a
  failure majority (29 done + 1 lost -> partial).
- `done` stays reserved for every recorded step in
  done/superseded/skipped; anything else (nonterminal leftovers,
  stopped/blocked-only mixes) stays `partial` as before.
- Empty/missing/all-non-dict `steps` stays `ended` - a never-started
  run is not failed just because done == 0 (the empty check precedes
  the failure check).

## Badge compatibility

No new styling: `partial` already existed in `TOUR_TAG_STYLE`
(#d29922 amber, same as `paused`) and work-section cards already paint
record-event words FILLED via `.ttag.ev` + `background:<hex>26` - the
partial tag is verbatim + amber + filled, visibly distinct from both
`done` (green) and `failed` (red) with zero CSS churn. Run/pipeline
cards still render `e.outcome` verbatim over `e.tag`.

## Files changed

- `tour_feeds.py` - `_run_outcome` reweighted (was: ANY unsuccessful
  verdict -> `failed`).
- `tests/test_tour_feeds.py` - new `RunOutcomeTests` (7 tests: aborted
  wins / empty-ended / mostly-done per unsuccessful status /
  unsuccessful-majority + no-done failures / ties / clean+nonterminal /
  feed-level 29+1 card asserting tag+outcome+section+tallies); two
  fixtures retargeted to real failure-majorities (1 done + 2 bad) so
  they still pin `failed` under the new rule.
- `tests/test_tour_e2e.py` - the lifecycle run fixture gained a third
  step (timed_out) keeping it a true failure (steps_total 2->3).
- `tests/test_tour_dash.py` - new node-harness test: the partial word
  renders verbatim in the filled amber chip inside the Work digest
  deck, per-section counters and tool-deck outline tag untouched.

## Verification

- `python -m unittest tests.test_tour_feeds.RunOutcomeTests` - 7/7.
- New node-harness case + neighbours in test_tour_dash - green.
- Full tour suite (`test_tour_state test_tour_feeds test_tour_api
  test_tour_dash test_tour_polish test_tour_e2e`) - 166/166.
- `python -m unittest tests.test_conductor` - 453 tests, 1 ERROR:
  `test_summarize_manifest_autospawns` hit a Windows tempdir-teardown
  race (the detached `conductor.py summarize` child still held
  `summarize.launch.log` at TemporaryDirectory cleanup). Passes in
  isolation (19.3s); pre-existing flake class, unrelated to this
  change - the summarizer spawn path is untouched.
- `python run_tests.py --jobs 8` (continuation attempt, log in
  `full-suite.log`) - 1055 tests over 8 shards; 4 shards each carried
  ONE error, all pre-existing load flakes under a box busy with
  parallel agent sessions, all green on isolated re-run:
  `test_tour_api.test_concurrent_transitions_coalesce_refresh` and
  `test_conductor.test_http_trash_routes` hit socket timeouts on
  `GET /api/state*`; `test_assistant_acp.test_attach_reaps_live_orphan_
  before_load` saw a still-None session on attach;
  `test_assistant_runtime.test_cancel_kills_and_keeps_partial` raced
  the cancel vs partial-write then hit the same WinError 32 tempdir
  teardown class as the summarize flake above. None touch
  `_run_outcome`, feed sectioning, or the tag rendering.

## Where it stopped

DONE - classification reweighted, badge reused. Attempt 2 finished
the leftover verification: focused tests re-green (29/29), full suite
recorded (4 unrelated load flakes, all pass in isolation). Nothing
carried forward.

## Deferred or blocked

None known.
