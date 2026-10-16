# Task: isolate-proposal-planning

**Status:** done
**Started:** 2026-10-16   **Last touched:** 2026-10-16

## Objective

Proposal generation must be able to run to completion while a worktree
merge into agent-conductor is pending or in progress (incl. a live
`resolve-*` session in the main checkout). A merge must not terminate an
unrelated planner, planning must not write into a watched workspace, only
the short final publish may wait for the repo, and generated output must
survive cancellation/retry/server restart.

## Investigation findings (conductor.py)

- `start_generation` used `_assert_unwatched(runs_dir, requests_dir)` ->
  409 while any live step's `work_dir` covered `data/requests` - a
  `resolve-*` session records `work_dir=<repo root>`, so a merge resolver
  blocked generation at the gate for its whole (minutes-long) life.
- The guard was *necessary* because generation wrote into the repo the
  whole time (`<id>.gen.log` streamed stdout, `<id>.gen.export.json`,
  `<id>.json` status flips) - those writes would kill a repo-root watcher
  (trap 5): planning interrupted merges.
- The publish parked in `_wait_writable` with the TOML only in RAM - a
  serve restart lost it; the rec sat at "generating" until the 660s stale
  rule.
- The planner `devin -p` shared serve's console process group - Ctrl+C on
  the server killed it (trap-15 shape).
- `_assert_unwatched` only saw live step records - a human merge
  (MERGE_HEAD) was invisible and a publish could land mid-merge.
- `launch_request`'s generating-scan read raw disk recs, which no longer
  carry "generating" once marking moved to the overlay.

## Decisions already made

- Spool = `%TEMP%/agent-conductor-planner/<id>/` (same pattern as
  `agent-conductor-flags`): `request.json` overlay, `job.json` (owning
  dirs + the request file's base stamp), `gen.log`/`gen.prev.log`,
  `gen.export.json`, `draft.toml`. Outside every watched cwd; survives
  restarts.
- `_read_request` returns the overlay while a spool exists; every
  mutating endpoint gates on `_request_status`, which prefers the
  overlay, so no torn writes. `_save_request` deletes `request.json`
  (drops the shadow) but leaves spool artifacts for the flush to rescue.
- Start of generation never touches `requests_dir`/`manifests_dir`; the
  whole publish (manifest + rec + artifact copy + spool cleanup) is
  deferred inside `_publish_from_spool` behind `_wait_writable` +
  REQUEST_LOCK - only that short op waits on the repo.
- `_assert_unwatched` also treats an open merge (MERGE_HEAD) as blocking,
  but only for non-gitignored destinations (`git check-ignore`: `data/`
  stays writable mid-merge, `manifests/` waits).
- The planner runs detached (`_detached_kwargs`, shared with
  `_launch_process`).
- A draft is auto-published only while its `draft_started_at` matches the
  current `generation_started_at`; a superseded draft is preserved as
  `<id>.draft.toml`, never published.
- `drain_planner_spool` at `cmd_serve` startup + `_auto_drain` (10s
  throttle) inside `collect_dashboard`: lands parked questions/error
  overlays, publishes a spooled draft, extracts a finished manifest from
  a dead-but-complete `gen.log`, waits out a still-writing orphan
  (`PLANNER_ORPHAN_GRACE_S` = 60s mtime freshness), else lands the
  interruption as `error` preserving artifacts.
- `answer_request` keeps its entry guard (interactive; human retries).
- `WATCH_POLL_S` module const (was hardcoded 5s); tests patch it down.

## Phases

### Phase 1 - reproduce

- [x] Reproduced: `start_generation` 409s while a watcher covers
  `data/requests` (scratch/repro.py); regression tests written first.

### Phase 2 - implement

- [x] Spool helpers (`_spool_dir`, `_spool_rec`, `_save_spool`,
  `_gen_log_path`, `job.json`) + `_read_request`/`_request_status`
  overlay.
- [x] `_planner_response` log/export -> spool; detached process group;
  gen.log rotation on regenerate.
- [x] `_finish_generation`: overlay writes, draft.toml before any wait,
  deferred publish + artifact copy + spool cleanup under REQUEST_LOCK.
- [x] `_assert_unwatched` merge-in-progress check for non-ignored dests.
- [x] `drain_planner_spool` + serve-startup + `_auto_drain` hook.
- [x] `launch_request` generating-scan via `_request_status`;
  `request_details`/`collect_dashboard` read spooled log/overlay.

### Phase 3 - coverage

- [x] Generation finishes while a resolver is live; publish lands after
  the watcher clears; zero writes into watched dirs meanwhile.
- [x] Publish defers on an open MERGE_HEAD in a real temp repo, lands
  after `merge --abort`; a parked branch doesn't block anything.
- [x] Drain recovers: spooled draft -> ready; orphaned log -> extract +
  publish; interrupted -> error + artifacts preserved; superseded draft
  never republishes.
- [x] `python run_tests.py`: 215/215 green. `python conductor.py demo`
  green.

## Where it stopped

DONE - merged to master. The branch was committed in-worktree; the merge
into the main checkout was handled by resolve-isolate-proposal-planning
(merge c2a8c6c, ownership-gate fix 400124b). 224/224 mock tests green on
the merged tree. Nothing left in this task.

## Deferred or blocked

- Planner session ids are still not recorded for `sessions` whitelisting
  (pre-existing gap); a detached planner that outlives the server cleans
  itself up at exit, and drain recovers its output.
- `create_request`/`save_request`/`apply`/`discard`/attachments keep
  their `_assert_unwatched` 409 while a watcher is live - interactive
  writes the human can retry; not part of this task.
- While a deferred publish waits, the dashboard shows the request as
  `generating` (accurate - the pipeline is still in flight) and the
  questionnaire's answer endpoints stay guarded until the flush lands.

## Verification

`python run_tests.py` and `python conductor.py demo` (mock-only, no auth).
