# Task: followup-run-discovery

**Status:** done
**Started:** 2026-10-16   **Last touched:** 2026-10-16

## Objective

A `<run>-followup.toml` written by a run's closing `follow-up-plan` step
becomes a persisted request record shown as `Ready to run` in the dashboard's
Runs section - registered, never launched. Malformed/missing/empty outputs
surface as honest, dismissible statuses; an explicit import path recovers
already-written files.

## Not in scope

- Launching/scheduling the follow-up run (Start run stays the human's button).
- The merge-back of THIS step's own branch (conductor's job).
- Re-running a follow-up-plan step that failed before writing anything.

## Decisions already made

- Registration reuses the proposal path: the file's text is validated by
  `validate_proposal` and copied to an owned `manifests/<id>-<name>.toml`,
  so `_manifest_path`/apply/discard/launch semantics stay unchanged. The
  original path is kept as `source_manifest` (never owned, never trashed).
- Idempotency + the Discard veto live in `data/requests/followups.index`
  (JSON map: file path or run outcome key -> {request, sha, discarded});
  named `.index`, not `.json`, so `requests_dir.glob("*.json")` never
  mistakes it for a request record.
- New request statuses: `empty` (TOML parsed, no [[step]], or the step
  reported nothing remains) and `missing` (finished step, no file); both
  render via STAGES like `ready`, are Discard-able, and can never launch
  (`launch_request` still accepts only ready/launch_failed).
- Discovery runs at run_end (driver), serve startup, sweep, and a 60s
  throttled pass in collect_dashboard - all idempotent, all deferring on
  `_assert_unwatched` (trap 5).
- The manifests dir a data tree owns is derived (`_followup_owner_dir`):
  ROOT/data/runs -> the checkout's manifests/, a foreign checkout's
  data/runs -> <checkout>/manifests, a flat sandbox -> the sibling dir.
  Discovery never globs a manifests tree it doesn't serve (the leak the
  first suite run caught: real files imported into test sandboxes).
- Source candidates per run: owner manifests first (the prompt's
  `{manifests}` target is the DRIVER's checkout), then the run's own
  manifest's parent dir (a driver in another checkout), then the step's
  worktree/manifests (worktree producer commits on-branch).
- A source rewrite re-copies+revalidates UNLESS the owned copy's sha no
  longer equals the last-imported sha - a human edit wins over a rewrite.
- A missing/empty outcome record folds in place when the file later
  appears (same request id flips to its real status; no twin rows).

## Phases

### Phase 1 - core machinery

- [x] index helpers + classify (ready/empty/error) + `_register_source`/`_register_outcome` + `import_followup_file` + `discover_followups`
- [x] wire: run_end, serve startup, sweep, `_auto_followups` throttle, discard/restore index marks, `import` CLI verb, POST /api/requests/import
- [x] gates: empty/missing added to save/apply/discard editable stages
- [x] UI: STAGES/COLORS/CSS for empty+missing, `followup` on run rows, `follow-up of <run>` on request rows

### Phase 2 - tests + docs

- [x] FollowupDiscoveryTests: registration, durable listing, dup prevention, malformed, missing, worktree source, empty, no-launch (15 tests, incl. fold-in-place + human-edit-beats-rewrite + run_end hook)
- [x] suite green; AGENTS.md paragraph; STATE/QUEUE/efficiency_stats

## Where it stopped

All phases done. Full suite re-run clean after the owner-dir scoping fix;
15/15 FollowupDiscoveryTests green. Branch merged to master via
resolve-followup-run-discovery (merge 9794ec5; both-append on
efficiency_stats.md only; 366/366 post-merge suite green).

## Deferred or blocked

- A `deferred` stamp (busy checkout at registration time) retries on the
  next scheduled pass - intended behavior, not a follow-up item.

## Verification

`python run_tests.py` - 15/15 new tests green, previously-failing
dashboard/pending/request tests re-verified in isolation; full-suite
re-run confirmed clean (one timing flake unrelated: SessionGoneGraceTests
test_grace_and_expiry passes in isolation).
