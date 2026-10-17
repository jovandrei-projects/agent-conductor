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
- [x] tour-llm-polish - optional cached LLM enrichment (off by default;
  tour_polish.py + 24 tests, conductor wiring, build_feed polish= hook)
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

## tour_polish contract (for tour-validation)

- Module `tour_polish.py` (stdlib-only, no conductor import). Off unless
  `CONDUCTOR_TOUR_POLISH` env is truthy; model = `MODEL` constant
  (`gpt-6-luna-low-priority`, env `CONDUCTOR_TOUR_POLISH_MODEL`), bounds:
  `MAX_COMMITS` 30, `PROMPT_MAX_CHARS` 32k, `MAX_CARDS` 24,
  `REPLY_MAX_CHARS` 16k, `TIMEOUT_S` 120 (`CONDUCTOR_TOUR_POLISH_TIMEOUT_S`).
- Provider = `devin -p` print session (same spawn pattern as the run
  summarizer, `--permission-mode auto`, tempdir cwd); credentials stay in
  the devin CLI's credentials.toml. Tests inject `provider=(prompt)->str`.
- `Polisher(cache_path, repo_dir, devin_argv=, provider=, enabled=, ...)`:
  `range_key(rng)` = `"<base.commit>..<tip.commit>"` (at-bounds excluded -
  model input is commits only); `attempt(rng)` synchronous coalesced
  generate+persist; `kick(rng, on_done=)` async; `cards_for(rng)` ->
  validated card list | None (fallback/miss => None); `record(rng)`,
  `generating(rng)`. Cache `data/tour-cache.json` = `{version, ranges:
  {key: {status: ok|fallback, cards, error, model, at}}}`, atomic
  tmp+os.replace, 40-range cap.
- Feed integration: `build_feed(..., polish=callable)` ->
  `_collect_window(polish=)`; polished cards REPLACE the deterministic
  area grouping for claimed commits, keep `area:<stop>:<newest12>` ids,
  get `"polished": True`; curated fold-ins still override generated text;
  runs/requests re-filtered against the window's real records; leftovers
  group deterministically. History re-materialization consults the same
  cache keyed by the descriptor's stored range.
- conductor: `tour_ctx(..., devin_argv=)` gains `"devin_argv"`;
  `_tour_polisher(ctx)` builds one Polisher per serve (None when module
  absent); `_tour_build` applies `cards_for` + kicks `kick(on_done=
  invalidate feed)` only when enabled+uncached; payload gains
  `enrichment: {enabled, status: ok|fallback|generating|pending}`.
- Everything degrades to deterministic commit-subject cards: disabled,
  no provider, timeout, malformed/invalid reply, or a persisted fallback
  record. GET paths never generate inline and never write tour state.

## Where it stopped

tour-llm-polish landed on this branch: tour_polish.py (provider seam,
range-keyed tour-cache.json, validation, coalescing), tour_feeds
`polish=` hook, conductor `_tour_polisher`/`_tour_build` wiring +
`enrichment` payload field, tests/test_tour_polish.py (24 tests;
96/96 tour tests green). Full --jobs 8 suite: 886 tests, 3 shards
failed ONLY on shared-box contention flakes (agent slot pool
starvation: StepControl/PauseRun 's1 never reached running', device/
port lease timing, a Windows tempdir file-lock) - every failing test
passes solo, none touch tour code. tour-dashboard is the sibling wave
step (its merge state is the conductor's business). Next:
tour-validation.

## Verification

`python run_tests.py --jobs 8` (manifest verify gate).
