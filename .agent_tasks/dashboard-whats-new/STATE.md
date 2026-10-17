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
  tests/test_tour_state.py)
- [ ] tour-feeds - deterministic git/request/run/followup feeds +
  TOUR_STOPS + manifests/whatsnew.jsonl curated records
- [ ] tour-api - GET /api/tour + POST viewed/snooze/unview, ~60s cache,
  tiny unseen count in /api/state
- [ ] tour-dashboard - vanilla-JS walkthrough in _DASH_HTML
- [ ] tour-llm-polish - optional cached LLM enrichment (off by default)
- [ ] tour-validation - end-to-end verify

## Where it stopped

tour-state landed: `tour_state.py` + `tests/test_tour_state.py`, suite
589/589 green at --jobs 8. Next session (tour-feeds): build the feed
collector on top of `tour_state` - use `feed_range`/`capture_boundary`
windows and `git log <wm.commit>..<boundary.commit>`.

## Verification

`python run_tests.py --jobs 8` (manifest verify gate).
