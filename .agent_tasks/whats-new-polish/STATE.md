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

2026-10-20 - Closing review (follow-up-plan) complete.
`progressive-tour` landed on main via resolver merge 2e28604 (conflicts
were .agent_tasks tail-appends only; tour code byte-identical to branch;
112 feeds/api/dash + 8/8 e2e tests green on the merged tree). Four
branches still parked, all merge-clean vs main individually
(`conductor/20261020-030142-whats-new-polish/<step>`):

- ~~progressive-tour   992cd26 - staged snapshot feed + guarded client~~
  MERGED 2e28604
- run-outcome-tags   4c4469f - proportionate `_run_outcome` tags
- summary-regressions 097a73a - stats block + canonical headings + pills
- html-summary-report d19db2f - run.summary.html artifact + sanitizer
- chat-font          e3fb85e - sans prose / mono code in chat.css

Simulated manifest-order merges: conflicts only in `.agent_tasks/
QUEUE.md`, `.agent_tasks/efficiency_stats.md`, `tests/test_tour_feeds.py`
(tags vs tour), `conductor.py` (html-summary-report) - resolver-scope.

Baseline verify on the merged result (main 0d42d39 = this worktree):
`python run_tests.py --jobs 8` -> 1047 tests, 3 errors, all known
load-flake family (test_tour_api `/api/state` client timeouts x2,
test_assistant_acp session race); node-VM harness (test_tour_dash.py) is
inside the suite. Zero of the five requirements are on main.

## Next

Proposal saved for human review: `manifests/whats-new-polish-followup.toml`
(auto-registered as request `20261020-053816-aa91a839`). It replays the
five features as parallel `land-*` steps (each merges its parked branch -
all clean vs main today) with `verify_timeout_min = 30`, merge_resolver
on, plus a dependent `integrated-validation` step. A human launches it
with `python conductor.py run manifests/whats-new-polish-followup.toml`;
the lighter alternative is `python conductor.py merge
data/runs/20261020-030142-whats-new-polish` (same serialized merges +
resolver, no integrated verify - run the suite after by hand).

Trap for future manifests: `verify_timeout_min` defaults to 10 min; any
parallel run keeping the registered `--jobs 8` gate must raise it (~30)
or every gate times out and every branch parks.
