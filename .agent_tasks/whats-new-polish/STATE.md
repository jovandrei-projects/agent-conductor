# Whats-new polish landing

Run `20261020-030142-whats-new-polish` delivered all five features on
worktree branches but merged **none** of them: every step's registered
verify gate (`python run_tests.py --jobs 8`, ~8-14 min wall on this box)
hit the default `verify_timeout_min = 10` while four agents held the
machine, so all steps recorded `failed` and the merge pipeline kept each
worktree (`step_merge_kept`, events.jsonl 05:27:51-52). `run-outcome-tags`
was the only gate that finished inside the cap; its shard-5 errors were
the same load-flake family seen on pristine main.

## Where it stopped

2026-10-20 - DONE. All five branches landed on main via dashboard
resolver merges (`progressive-tour` 2e28604, `run-outcome-tags`
6ffb4e5, `summary-regressions` bee3f0b, `html-summary-report` c5778f3,
`chat-font` 53b6e0e; parked
`conductor/20261020-030142-whats-new-polish/*` branches deleted), and
the integrated-validation step verified the combined tree:

- all five requirements present in the tree: (a) progressive /api/tour
  snapshots (tour_feeds emit hook, ctx["snap"]/_tour_compose/
  _tour_snapshot, gen+seq guards, non-blocking tour_transition,
  /api/tour/progress + client poll/merge/narration); (b) proportionate
  tour_feeds._run_outcome (strict unsuccessful majority of decisive
  verdicts or zero done -> failed; aborted verbatim; empty -> ended;
  29+1 -> partial); (c) _run_stats_lines/_run_stats_md/
  _ensure_stats_block served + baked stats block, canonical
  '## <step-name> - <status>' prompt headings, .sumdoc code pill wrap;
  (d) run.summary.html second artifact, GET
  /api/runs/<run>/summary.html re-sanitized per read under
  .run-summary-report, appended once below the sumdoc, mock_devin
  emits it; (e) chat.css system-sans prose on #aichat/#ai-rail/#aiset
  with code/.mdpre/.act-params/.act-tgt pinned ui-monospace.
- full suite `python run_tests.py --jobs 8` on the combined tree:
  1093 tests, 8/8 shards ok, ZERO failures/errors - even the known
  shared-box flake family (test_tour_api /api/state timeouts,
  test_assistant_acp session race, test_conductor device/stop timing)
  did not reproduce this run.

- ~~progressive-tour   992cd26 - staged snapshot feed + guarded client~~
  MERGED 2e28604
- ~~run-outcome-tags   4c4469f - proportionate `_run_outcome` tags~~
  MERGED 6ffb4e5
- ~~summary-regressions 097a73a - stats block + canonical headings + pills~~
  MERGED bee3f0b
- ~~html-summary-report d19db2f - run.summary.html artifact + sanitizer~~
  MERGED c5778f3
- ~~chat-font          e3fb85e - sans prose / mono code in chat.css~~
  MERGED 53b6e0e

Simulated manifest-order merges: conflicts only in `.agent_tasks/
QUEUE.md`, `.agent_tasks/efficiency_stats.md`, `tests/test_tour_feeds.py`
(tags vs tour), `conductor.py` (html-summary-report) - resolver-scope.

## Next

Nothing - task complete. `manifests/whats-new-polish-followup.toml`
(auto-registered request `20261020-053816-aa91a839`) is now fully
consumed: its land-* steps ran via resolver sessions and this step was
its integrated-validation step.

Trap for future manifests: `verify_timeout_min` defaults to 10 min; any
parallel run keeping the registered `--jobs 8` gate must raise it (~30)
or every gate times out and every branch parks.
