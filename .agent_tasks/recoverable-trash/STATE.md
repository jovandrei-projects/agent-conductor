# Task: recoverable-trash

**Status:** done
**Started:** 2026-10-16   **Last touched:** 2026-10-16

## Objective

Deleting a run record or discarding a proposal draft becomes a recoverable
move to `data/trash/<entry>/` (48h retention, `CONDUCTOR_TRASH_HOURS`), not
`rmtree`. Lists lose the entry instantly; an Undo action and a Trash view
(Restore + confirmed Empty trash) recover or finally purge it.

## Not in scope

- Deleting the things a run spawned: devin sessions, parked worktree
  branches/worktrees, attachments. They outlive trash by design (meta notes
  them; restore re-whitelists sessions by bringing state.json back).
- Per-entry manual purge - Empty trash is the only irreversible op.
- CLI flags for trash (dashboard-only surface; sweep purges expired).

## Decisions already made

- `data/trash/<ts>-<rand6>/{meta.json,payload/}`; meta written LAST (commit
  point) so a crash mid-move leaves ignorable non-entry debris.
- Run payload = the whole run dir moved; request payload = `<id>.json` +
  owned `<id>-*.toml` manifest + `<id>.*` artifacts + planner spool dir
  (must move - else drain_planner_spool would resurrect the record).
- Restore never overwrites: occupied run-dir dest -> `<name>-restored[-N]`
  + state.json internal paths rewritten; manifest dest -> `-restored` name
  + `rec["manifest"]` repointed; artifacts/spool occupied -> keep existing;
  record dest occupied -> refuse whole op (409).
- Legacy `status=="discarded"` request tombstones list as `legacy-<id>`
  trash entries (deleted_at = updated_at, so old ones expire on sight);
  restore lands them as `draft` (manifest was already unlinked).
- Trash endpoints: `POST /api/trash/<id>/restore`, `POST /api/trash/empty`
  ({confirm:true}); `/api/state?trash=1` adds full `trash` list; payload
  always carries `trash_count`/`trash_ids` for the chip + Undo liveness.
- `sweep_trash` (bounded, TRASH_SWEEP_MAX/pass, expired-only, trap-5
  watched guard) folds into `sweep_runs` -> `sweep` CLI + `serve` startup +
  throttled `_auto_archive` poll get it free.

## Phases

### Phase 1 - backend engine

- [x] `_trash_*` engine: entry mint/list/read, meta write, item moves,
  legacy tombstone listing, restore (+path rewrite/collision), empty,
  bounded sweep.
- [x] Rewire `delete_run` + `discard_request` to move-to-trash; keep all
  live/resume/merge/unwatched guards, add summarizer guard for runs.
- [x] `collect_dashboard` + routes + sweep/serve/auto wiring.

### Phase 2 - dashboard UI

- [x] `N trash` view chip + trash section (name/kind/deleted/purges-in/
  contents/Restore + Empty trash), persisted toggle.
- [x] Undo bar after delete/discard (`lastTrash`, verified against
  `trash_ids`, dismiss + open-trash buttons).
- [x] Updated delete/discard confirm text + titles (48h recovery wording).

### Phase 3 - tests + bookkeeping

- [x] `RecoverableTrashTests` in tests/test_conductor.py: temp fixtures
  only - trash/restore run+request, collisions, expiry via `now=`,
  bounded sweep, partial-restore failure, legacy tombstones, watched
  refusal, HTTP routes, empty-trash confirm, `_DASH_HTML` markers.
- [x] Update existing delete/discard test expectations.
- [x] `python run_tests.py` + `python conductor.py demo` green.
- [x] QUEUE.md + efficiency_stats.md entries; commit in logical groups.

## Where it stopped

Complete. One implementation wrinkle worth remembering: in
`discard_request` the spool-overlay decision (`_spool_rec`) must be taken
BEFORE the spool dir moves into the payload - a reorder once made the
record payload pick up the stale on-disk shadow instead.

## Verification

`python run_tests.py` and `python conductor.py demo`.
