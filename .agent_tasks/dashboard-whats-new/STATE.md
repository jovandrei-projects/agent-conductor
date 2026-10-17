# Task: dashboard-whats-new

**Status:** in progress
**Started:** 2026-10-17   **Last touched:** 2026-10-17

## Objective

Persistent "What's new" walkthrough on the conductor dashboard, per
manifest `manifests/20261017-133854-34c155ce-whats-new-tour.toml` (run
20261017-145726-dashboard-whats-new). Six manifest steps, one Devin
session each, each in its own worktree.

## Not in scope for tour-state (this step)

HTTP routes, /api/state fields, feed collection, LLM polish, dashboard
UI - those are the later manifest steps.

## Decisions already made

- State module: `tour_state.py` at repo root (stdlib-only, like
  `mobile_device.py`); conductor.py NOT modified yet - the tour-api step
  imports it into the HTTP handlers.
- File `data/tour-state.json`, shape exactly `{watermark, viewed[],
  snoozed[]}`. Module RLock serializes read-modify-write; tmp +
  os.replace (+ PermissionError retry) is the atomic write.
- Watermark `{commit, at, origin}`: commit = RESOLVED sha bounding
  `git log <commit>..<boundary.commit>`; at = ISO instant bounding record
  feeds. Never symbolic HEAD, never serve_boot. Init =
  `{commit:null, at:null, origin:"init"}` (nothing acknowledged - do NOT
  pin HEAD; corrupt files quarantine to `*.corrupt-<ts>`).
- `capture_boundary(repo)` snapshots {commit: HEAD sha, at: now}; entry
  descriptors carry `range={base,tip}` = the window that produced them,
  so viewed/snoozed/unviewed entries stay retrievable after the
  watermark advances past them.
- Only `mark_viewed(id, rng, boundary, remaining_new)` may advance, and
  only when `remaining_new` (caller's still-'new' ids) is empty; it lands
  at the captured boundary, never HEAD, and refuses on non-descendant /
  unresolvable / non-sha boundaries. `snooze` defers (never advances,
  doesn't block), `unview` flags `unviewed_at` back to 'new' keeping the
  range. Queries: `entry_state`, `descriptor`, `entries`,
  `watermark_status`, `feed_range`, `update(fn)` for compound
  locked ops.

## Steps (manifest order)

- [x] tour-state - persistence + watermark (this file; 26 tests in
  tests/test_tour_state.py). NOTE: the conductor recorded the step
  `failed` on the post-step verify gate (600s cap on the full suite),
  NOT on the work - the branch was parked unmerged; tour-feeds merged it
  into its own branch to build on it.
- [x] tour-feeds - deterministic feeds: `tour_feeds.py` + 28 tests in
  tests/test_tour_feeds.py. Entry contract for the API step is below.
- [x] tour-api - GET /api/tour + POST viewed/snooze/unview, ~60s cache,
  tiny unseen count in /api/state (conductor.py tour_ctx block before
  collect_dashboard + routes in make_dashboard_handler; 18 tests in
  tests/test_tour_api.py)
- [ ] tour-dashboard - vanilla-JS walkthrough in _DASH_HTML. The
  What's-new button MUST use id `tour-btn` - TOUR_STOPS["tour"] selects
  `#tour-btn, #power-btn` (degrades to the power button until it exists).
- [ ] tour-llm-polish - optional cached LLM enrichment (off by default)
- [ ] tour-validation - end-to-end verify

## tour_feeds contract (for tour-api)

`feed = tour_feeds.build_feed(state, boundary, repo_dir=, runs_dir=,
requests_dir=, manifests_dir=)` -> `{boundary, range, entries, history,
unseen_count, warnings}`. Callers: `state = tour_state.load(path)`,
`boundary = tour_state.capture_boundary(repo_dir)`.

- `entries[i]`: `{id, kind (change|request|run|followup|curated|unknown),
  stop, title, blurb, tag (new|improved|fixed), anchor (selector|None),
  stop_title, commits [{sha,subject}], runs [dashboard run keys],
  requests [rids], at (event iso), range {base,tip}, state
  (new|viewed|snoozed)}`. Sorted oldest -> newest.
- `history[i]`: viewed descriptors re-materialized over their stored
  range (same shape + `viewed_at`, `unavailable` when the range is gone).
- Pass `entry["range"]` to `tour_state.mark_viewed(id, rng, boundary,
  remaining_new)` - it is the range the descriptor must keep.
- `runs` uses the dashboard key form (`request:<rid>` or run dir path);
  `requests` uses request ids. A run card absorbs its
  `conductor/<dir>/<step>` merge commits + chains (dedup).
- Curated records: `manifests/whatsnew.jsonl` lines `{stop, blurb,
  title?, tag?, commits|since+until|requests|runs}` - commits sha
  prefixes; malformed lines land in `feed["warnings"]`, never break.
- `state` for unseen counting: only `new` counts (snoozed/unviewed are
  deferred/re-opened via the persisted descriptors).

## tour API surface (for tour-dashboard)

All tour state lives in a per-serve `tour` ctx
(`conductor.tour_ctx(runs_dir, requests_dir, manifests_dir, repo_dir)`;
`repo_dir` = the conductor checkout the feed runs `git log` on, ROOT in
prod, fixture in tests; `make_dashboard_handler` gained a `tour_repo=`
kwarg and exposes the ctx as `Handler.tour_ctx`).

- `GET /api/tour` -> `tour_feed(ctx)`: `{generated_at, boundary, range,
  entries, history, unseen_count, warnings}`; ~60s `TOUR_FEED_TTL_S`
  cache. Entries carry the manifest contract verbatim (id/title/blurb/
  tag/anchor/commits[{sha,subject}]/runs/requests/state) plus kind,
  stop, stop_title, at, range.
- `POST /api/tour/<id>/<viewed|snooze|unview>` (JSON body required,
  same `_body()`+origin protections): `tour_transition` validates the
  id against entries+history (one fresh-rebuild retry on a cache miss,
  else 404), runs tour_state's persisted transition, then drops BOTH
  caches and rebuilds once - responses + the count can never be
  pre-mutation. Result = tour_state's dict + `unseen_count`; `viewed`
  also returns `advanced`/`advance_blocked`/`watermark`.
- `/api/state` gains `tour_unseen` ONLY: `tour_unseen(ctx)` returns the
  last computed count (null = never computed) and re-arms ONE daemon
  `_tour_build` when the feed is stale/absent (`ctx["busy"]`, the
  _SESS_BUSY pattern) - the paint path never runs git log.
- GET paths are read-only end to end (`tour_state.load` never writes);
  the state file appears only on the first mutation.

## Where it stopped

tour-api landed: conductor.py `tour_ctx`/`_tour_build`/`tour_feed`/
`tour_unseen`/`tour_transition` + routes, tests/test_tour_api.py (18
tests; 72/72 tour tests green; full --jobs 8 suite: 1 unrelated
StopControl flake, green in isolation). Next: tour-dashboard - the
vanilla-JS walkthrough in _DASH_HTML. The What's-new button MUST use
id `tour-btn` (TOUR_STOPS['tour'] falls back to #power-btn); badge
reads `tour_unseen` from /api/state, never GET /api/tour for it.

## Verification

`python run_tests.py --jobs 8` (manifest verify gate).
