# Task: retain-proposal-session

**Status:** done
**Started:** 2026-10-16   **Last touched:** 2026-10-16

## Objective

The `plan-proposal` agent row was emitted by `collect_dashboard` only while
`stage == "generating"` - a transient construct with no session_id, export or
ended_at, so it vanished the moment `_publish_proposal` flipped the request
to `ready`. The real planner session is now retained: its id is captured,
each attempt lands a `plan_sessions` record + `plan_gen_id` on the request,
and `collect_dashboard` re-emits it as a 'step 0' preparation-history row on
the plan's task list (ready, launched and post-reload alike) - never runnable
work.

## Not in scope

- Per-attempt gen.log/gen.export.json files (attempts share `<id>.gen.*`,
  newest overwrites - only the newest completed attempt gets file links).
- Live session-id attribution while still `generating` (row shows '…' as
  before; the id lands at attempt end).
- Changing what runs: no manifest/state.json step for preparation; launch,
  retry and resume keep operating on manifest steps only.

## Decisions already made

- Disappearance = emission condition, not archiving: the row only existed
  for `stage == "generating"`; no record of the session was persisted.
- Session id capture: `devin list` diff inside the planner tempdir post-wait
  (covers killed passes), then export `session_id`/`session` and
  `tail_session_ref` fallbacks. Stored transiently as `rec["_gen_session_ids"]`.
- Association: `plan_gen_id` = gen_id of the attempt that produced the current
  manifest, set only at publish; `plan_sessions` = bounded (12) list of
  terminal attempt records (done/stopped/failed, incl. questions-parked).
- Labels: at rest the producer is `plan-proposal`; other attempts are
  `plan-proposal-N` (agentKey uniqueness). While a new pass generates, the
  live row keeps the bare name and every completed attempt is numbered.
- Rows never join `counts` - a 'stopped' planner row must not make a run look
  resumable. Producer gets `seq = -1` (nav-chain step 0); others seq=None.

## Phases

### Phase 1 - capture + persist

- [x] `tail_session_id(export_path)` helper (ATIF `session_id`, mock `session`)
- [x] `_planner_response`: before/after `devin list` diff in tempdir +
  fallbacks -> `rec["_gen_session_ids"]`
- [x] `_plan_attempt_entry` + `_record_plan_attempt` (bounded, dedup by gen_id)
- [x] `_finish_generation`: `gen_sids` accumulate; entries at publish /
  questions park / GenerationStopped / generic error; `plan_gen_id` at publish

### Phase 2 - dashboard rows

- [x] `plan_attempt_agents(rec, key, generating, log_path, prices)` row builder
- [x] `collect_dashboard`: emit for unlaunched requests AND launched runs
  (run_key association, no counts, deduped per request)

### Phase 3 - regression tests + verify

- [x] publish keeps planner row w/ session id, done, seq -1; persists on disk
- [x] stop + regenerate: producer = newest attempt's session, earlier attempt
  shows as its own numbered stopped row
- [x] launch executes step 1 only: no planner respawn, state.json steps have
  no plan-proposal, history row still listed on the run; resume same
- [x] `python run_tests.py` 221/221 green + `python conductor.py demo` green

## Where it stopped

Done: 221/221 mock tests + demo green. Wave can merge.

UPDATE 2026-10-16 (verify-proposal-flow): merged into the verify branch
as 1c1f1d5. The semantic merge moved attempt recording onto the spool
architecture (records land on the overlay for publish/questions/failed,
on the restored file record for stopped; `_publish_from_spool` owns
`plan_gen_id` + `gen_sids`). Drain-recovered generations record attempts
too. 232/232 green in the merged state.

## Deferred or blocked

- Live session-id attribution while `generating` (post-wait capture only;
  the running row shows '…' until the attempt ends, as before).
- Older attempts' gen.log/gen.export.json are overwritten by the newest
  attempt (shared filename, pre-existing) - non-newest history rows show
  session id + status + times but no file links.

## Verification

`python run_tests.py` (221/221 OK, 2026-10-16); `python conductor.py demo`
(green). One pre-existing test needed a FakeProc guard: planner session-id
probes (`devin list`) spawn around the planner, so its cmd recorder now
filters on `--prompt-file`.
