# Task: run-lifecycle-cleanup

**Status:** done
**Started:** 2026-10-15   **Last touched:** 2026-10-15

## Objective

Finished runs older than a threshold (default 48h, env
`CONDUCTOR_ARCHIVE_HOURS`) get stamped `archived` in state.json and are
hidden from the default dashboard view but kept on disk; a view toggle
lists them. The run modal gains a confirmed 'Delete run' that removes
the run's on-disk record, disabled/refused while the run is live.

## Decisions already made

- `state["archived"] = <iso>` stamp, not a directory move: `*/state.json`
  globs (sweep, sessions whitelist, `_request_run_dir`) keep working.
- Auto-archive runs in `sweep_runs` (sweep + serve startup) plus a 30-min
  throttled pass inside `collect_dashboard` (`_auto_archive`) so a
  long-lived server catches runs that finish after boot.
- Archived run's `request_id` still registers in `launched` - its proposal
  row stays suppressed. `--resume` pops the stamp (run resurfaces).
- Delete = `shutil.rmtree` of the run dir after `{confirm:true}` +
  live/resume/merge guards + `_assert_unwatched`; path-keyed %TEMP%
  sentinels for the run are swept, spawned devin sessions are not.

## Phases - all landed 2026-10-15

- Backend: `archive_runs` + `ARCHIVE_AFTER_S`, sweep_runs fold-in,
  `collect_dashboard(include_archived)` + `archived_runs` count,
  `POST .../archive` + `POST .../delete`, resume clears stamp.
- Dashboard: `view: N archived` chip (persisted `ac-arch`,
  `?archived=1`), `archived` stage chip on rows, Archive/Unarchive +
  Delete run buttons in the run modal.
- Tests: `RunLifecycleCleanupTests` 8 tests; suite 182/182, demo green.
- Live-verified: serve on a temp runs dir - default view hides a 50h
  run, `?archived=1` lists it, archive/unarchive/delete round-trip.

## Where it stopped

Done - nothing to resume. Feature documented in AGENTS.md "Run hygiene".

## Verification

`python run_tests.py` (182/182) + `python conductor.py demo` green;
endpoints exercised against a live `serve` on a fixture runs dir.
