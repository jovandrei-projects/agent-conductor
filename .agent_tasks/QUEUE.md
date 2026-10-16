# Task queue

The master ordered list. One line per task. This answers "what next"; the
task's own `STATE.md` answers "where in it".

Status is one of `next`, `in progress`, `blocked`, `paused`, `done`. Keep the
order meaningful, but see rule 3 in `README.md`: finishing something early is
fine, it just has to be ticked where it lives.

Conventions for `STATE.md` are in `README.md`; the template is in `_template/`.

| # | Task | Status | Folder | Waits on |
|---|---|---|---|---|
| 1 | Bootstrap: conductor.py v1 + mock tests + docs | done | bootstrap/ | trap 5 root-caused + fixed (no workspace writes while a worker is alive); auth solved; live smoke green |
| 2 | Pre-live hygiene: liveness probe, lost/sweep, sessions cleanup, cloud marks | done | hygiene/ | os.kill probe bug found + fixed (was killing live workers) |
| 3 | Dashboard favicon: baton-over-orchestra glyph | done | favicon/ | second dispatch; first was eaten by trap-10 commit swallow |
| 4 | Worktree merge matrix: exhaustive tests + merge_resolver + lab fixtures | done | wt-matrix/ | landed on dev/merge-tests (commits 5ba30c4, 7f7dcd9); live lab run 21/21, 8 real conflicts resolved |
| 5 | Dashboard request drafts + proposal review/start, run filters/token help, silent Windows probes | done | â€” | 98/98 mock tests + demo green; live planner not dispatched |
| 6 | Task prompt editing: pre-dispatch Edit prompt, resume notes for ran steps, full prompt text in modal | done | task-prompt-editing/ | 172/172 mock tests + demo green; prompt-/rnote- sentinels survive resume |
| 7 | Planner 'ask me questions' checkbox: questionnaire modal before manifest, answers feed a refine pass | done | planner-questions/ | 193/193 mock tests + demo green; live planner not dispatched |
| 8 | Stop proposal during preparation: stopgen sentinel + _GEN_PROCS kill + per-attempt gen_id scoping, restores draft/ready | done | stop-proposal-preparation/ | 216/216 mock tests + demo + real-proc smoke green |
| 9 | Retain the create-proposal session as step-0 preparation history on the plan's task list | done | retain-proposal-session/ | 221/221 mock tests + demo green; plan_sessions + plan_gen_id persisted per attempt |
| 10 | Verify the three proposal-flow changes together (stop / retain-session / compact labels) in the merged state | done | verify-proposal-flow/ | both parked branches merged, 4 integration regressions fixed (same-second publish race, stale overlay, drain records, chip wrap); 232/232 + demo + live probe green |
| 11 | Interrupt + instruct: stop+resume a running step with added instructions/screenshots, exactly-once across races/restarts | done | interrupt-with-instructions/ | intrq sentinel -> rnote fold -> same-step respawn (not a retry); late/stop-wins banking, restart exactly-once; composer+ctx UI, attach reuse; 303/303 + demo green |

## Not yet filed as tasks

- DONE 2026-10-16 (followup-run-discovery, wave step of run
  20261016-013719-workflow-ux-safety): a produced `<run>-followup.toml`
  is intake, not execution - `discover_followups` registers it as a
  persisted proposal request (`Ready to run`, linked to its run via
  `source_run`/`source_manifest`, only a human's Start run launches it).
  The source file is validated by `validate_proposal` and copied to an
  owned `manifests/<id>-<name>.toml`; the source stays read-only.
  `data/requests/followups.index` makes it idempotent across run_end /
  serve-startup / sweep / 60s-throttled dashboard passes, retries and
  rewrites (sha'd; a changed source re-copies unless a human edited the
  owned copy - the edit wins; a missing/empty row folds in place when the
  file lands). Outcomes that aren't launchable work are honest request
  rows: `empty` ("No work remains"), `missing` ("Follow-up missing" +
  expected path + import hint), `error` (malformed/unvalidatable - the
  broken text stays inspectable via the owned copy); a failed step with
  no file is `absent` on the run stamp only. Discard vetoes the source in
  the index (Restore clears it); `conductor.py import <file.toml>` /
  `POST /api/requests/import` is the explicit recovery path - also the
  override for files predating the feature or orphaned by a foreign
  checkout. Run rows carry the `followup` stamp chip (registered/empty/
  missing/invalid/deferred/absent); busy destinations defer to the next
  pass via `_assert_unwatched`. The manifests dir a tree owns is derived
  (`_followup_owner_dir`) so a dashboard never reaches into a tree it
  doesn't serve. New request stages render via STAGES/COLORS; empty/
  missing joined the editable/discardable set and are launch-gated like
  draft. Registration never spawns work (tested with _launch_process
  mocked). 15 new FollowupDiscoveryTests; suite + AGENTS.md updated.
- DONE 2026-10-16 (recoverable-trash, wave step of run
  20261016-013719-workflow-ux-safety): 'Delete run' and proposal 'Discard'
  are recoverable now - both move their whole bundle to
  `data/trash/<ts>-<8hex>` (next to data/runs; `data/trash/events.jsonl`
  is its audit log) instead of rmtree-unlinking. Each entry's `meta.json`
  persists kind, ref, origin, deleted_at and expires_at (48h retention,
  `TRASH_RETAIN_S` = 172800, `CONDUCTOR_TRASH_HOURS` boot knob) plus an
  item list of {slot,payload,dest,name,dir} pairs so restore is a pure
  file move with no hidden state. Runs keep every prior protection -
  confirm gate, live steps/summarizer/resume/_MERGE_BUSY, in-watched-dir
  refusal (destination too: trap 5), matching `devin rm`s after the move;
  sessions were never deleted anyway and parked branches/worktrees are
  untouched (the meta note says so). Requests take record + owned manifest
  + ALL `<id>.*` side files + the parked planner spool (moved inside the
  entry so a pending flush cannot resurrect the request); when a spool
  overlay exists it - not the stale on-disk shadow - becomes the record
  payload; pre-trash 'discarded' tombstones list as `legacy-<id>`, restore
  to `draft`, and expire off the recorded timestamp. Restore never
  overwrites: a busy run name lands `-restored`, a busy request record
  refuses the whole restore (409), a busy manifest lands `<stem>-restored`
  with `rec.manifest` repointed, occupied side-file/spool destinations
  keep the newer copy; a run restored under a fresh name gets its
  state.json self-paths rewritten and a `run_restored` event. Partial
  failures keep the failed items in the entry for retry; every move is
  validated direct-child-of-managed-root and every payload direct-child-
  of-payload/ so a crafted meta cannot escape. `sweep_trash` (folded into
  sweep_runs, cmd_serve + serve loop) purges ONLY expired entries, capped
  at TRASH_SWEEP_LIMIT=64/pass, and dry-runs entirely while a live
  self-hosted worker watches the data tree. Endpoints: `GET
  /api/state?trash=1` (trash_count/trash_ids always, entries on demand),
  `POST /api/trash/<id>/restore`, `POST /api/trash/empty`
  ({confirm:true} - the one permanent delete). UI: `view:` gains an 'N
  trash' chip, a #trashwrap table between runs and agents shows kind /
  deleted / purges-in / notes / Restore + a plainly-confirmed Empty
  trash, and #undobar offers Undo for the entry you just trashed until
  dismissed or restored elsewhere. 346/346 tests + demo green; 20 new
  RecoverableTrashTests (metadata+retention round-trips, collision-safe
  restores incl. repointed state paths, attachment/followup + spool
  preservation, watched/merge guards, bounded+expired-only sweeps,
  partial-failure retry, crafted-meta escapes, legacy tombstones, HTTP
  routes, dashboard hooks).
- DONE 2026-10-16 (modal-refresh-stability, wave step of run
  20261016-100852-run-review-ux): the task modal no longer loses its
  place during the 2s background repaint. Measured root cause (the
  display:none suspicion was disproven live): the wholesale dbody
  innerHTML swap leaves nothing to scroll-anchor, so a shorter repaint
  clamps #detail.scrollTop permanently and growth above the viewport
  pushes the text being read off-screen. showDetail now repaints in
  place while detailKey is unchanged (no closeDetail hide/show), a
  byte-identical repaint skips the DOM write entirely, and real changes
  restore a landmark scroll anchor (first viewport-visible id/data-asid/
  data-ag/.k/h3 element + occurrence index, pixel dy preserved, #dtail's
  own inner scroll carried), restore focus via a stable data-*
  signature + textarea caret + unsaved #d-timeout value (server value
  wins on conflict). Deliberate task switches land at the top as an
  explicit policy (row opens previously inherited the old offset since
  hidden round-trips don't reset scrollTop); Back keeps its own
  modalScroll snapshot; /api/state responses are seq-guarded so an
  older body landing last can't repaint a stale view. Reproduced and
  verified in real Chromium (probe_scroll.py: RESET pre-fix, STABLE
  post-fix); 331/331 tests + demo green; 3 new
  ModalScrollStabilityTests node-harness tests.
- DONE 2026-10-16 (model-choice-lists, wave step of run
  20261016-013719-workflow-ux-safety): the Planner/Step model fields moved
  off the shared native datalist onto a real combobox (role=combobox +
  listbox popup per field). Typing filters a Recommendations group
  (substring hits, prefix first) that sits above a separated, scrollable
  .mall 'All models' group - both read modelCatalog(), the same
  `devin models list` parse (+adaptive, +2-slug fallback while logged
  out). The full list always renders, even at zero matches ('no matches'
  note); free text is untouched (the input keeps any catalogless value).
  A model in BOTH groups is two options with distinct ids
  (-opt-r<i>/-opt-a<i>) sharing one flat items order, so
  aria-activedescendant/Arrow nav is unambiguous across the seam;
  aria-selected marks both twins. Esc closes only the popup (modal stays),
  Enter picks the active option (or just closes when none - free text
  survives), ArrowDown opens+activates from closed, click on the focused
  input re-opens, outside click/focusout dismiss, mousedown inside the
  popup is preventDefaulted so the input never blurs mid-pick, hover
  tracks active. Picks dispatch a synthetic input event so dirty/syncEffort
  /pending-diff stay the single typing path; popup flips .up near the
  modal's bottom edge. 297/297 mock tests + demo green; new
  ModelComboboxTests (markup + node-harness interactions) and a 35-check
  Playwright probe against real Chromium + the live 54-family catalog.
- DONE 2026-10-16 (request-image-picker, wave step of run
  20261016-013719-workflow-ux-safety): the request form's "Attach images"
  hint gains a paperclip button - an icon-sized .btn.secondary look
  carrying an inline SVG stroke glyph (aria-label + title, global
  focus-visible ring covers it; no emoji). It drives a hidden
  `<input type="file" id="rq-file" accept="image/*" multiple>` via a
  delegated `[data-attach-pick]` click (attachTarget()-gated like the
  drop/paste listeners), and a document-level `change` listener feeds
  the selection into the SAME attachFiles upload path - POST
  /api/attachments, thumbnail strip, caret path insert, dirty mark,
  refreshPending. Cancel delivers no files and changes nothing; the
  input value resets after each pick so re-choosing the same file still
  fires change; name-dedupe matches drop/paste. The strip got
  aria-live=polite + aria-label. Non-editable stages render neither the
  button nor the input. 295/295 mock tests + demo green; new
  node-harness test covers markup/aria, picker click, cancel no-op,
  invalid type, select+dedupe, draft reopen, and remove.
- DONE 2026-10-16 (proposal-action-state, wave step of run
  20261016-013719-workflow-ux-safety): proposal staleness is a computed
  field-vs-baseline diff, not a latched `stale` flag
  (`_pending_actions`). Generation inputs (request text, project,
  attachments) diff against the `gen_inputs` snapshot stamped by
  `_publish_proposal`; option fields (model/effort/attempts/failure
  policy/worktrees/resolver/followup) diff against what the manifest
  TOML itself encodes (`_manifest_options`), so a hand-edited TOML moves
  its own baseline. The modal's `#pend-note` re-diffs the live form on
  every input - reverting a field to its baseline clears the mark
  without a save - and names which action reconciles which bucket
  (Regenerate for generation inputs, Apply options for the rest, both =>
  Regenerate first since it writes the options too). Apply is disabled
  with an explanatory title when there is nothing to apply, and refuses
  (409) when generation inputs diverged from baseline - including a
  divergence saved earlier, which the old record-compare let through.
  Launch refuses while either bucket is pending; failed generation or
  apply keeps the pending state; Regenerate is a neutral `btn
  secondary` (Start run keeps the primary look), always confirms, and
  now names unsaved TOML edits it would drop. Legacy pre-baseline
  records keep the old flag as an opaque 'fields' gen marker; a
  never-stale legacy record materializes `gen_inputs` from its stored
  fields on next save. En route fix: a follow-up-plan step appended by
  Apply missed the worktree pass (mixed state the checkbox can't say) -
  it now inherits a checked `worktrees`. 294/294 tests + demo green;
  16 new PendingStateTests + 2 new DashboardJsFilterTests.
- DONE 2026-10-16 (run/session summaries - hand-implemented, same
  session as obs-merge-telemetry): `final_report()` lifts the worker's
  own closing prose from the ATIF export (trailing agent messages with
  no tool calls, STATUS:/NEXT: stripped) and `step_summary_md` leads
  with it as `## Report`; the run-level counterpart is `run.summary.md`
  written by an opt-in detached `conductor.py summarize` session
  (`[run] summarize = true|"<model>"`, dashboard Generate LLM summary
  button, `POST /api/runs/<dir>/summarize`) fed run stats + each step's
  closing report via `_summary_prompt` (`conductor-run-summary`
  marker; mock answers it). Served by `GET /api/runs/<dir>/summary`
  (LLM file else `run_report_md` deterministic roll-up), rendered
  in-app via `openRunSummary` (`run|<key>` summaryView keys ride the
  existing Back stack). `_summary_body` widened to ###/####, ordered
  lists, `---`, italics - all still escaped. `.summarizing` pid marker
  drives the dashboard state; `run_summarized`/`run_summary_failed`
  events + `state.run_summary`/`state.summarize_model` record it.
  Refused while any step is live (trap 5). Request form `summarize`
  checkbox -> `run.summarize` via `_apply_fields_to_toml` +
  `_guarantee_isolation`. 10 new RunSummaryTests + manifest-parse +
  renderer coverage.
- DONE 2026-10-16 (row-context-menus, wave step of run
  20261016-013719-workflow-ux-safety): right-click (or Shift+F10 / the
  Menu key on a focused row) opens a `#ctxmenu` action list on run and
  task rows. Items are plain buttons carrying the SAME data-* hooks the
  modal action buttons use - one delegated click handler, one set of
  sentinel endpoints, server-side state validation unchanged. Run menu:
  Open details (request modal for proposal rows, run modal otherwise),
  View original request / TOML, Filter by this item / Clear run filter,
  Pause run / Stop run now / Resume unfinished steps / Merge pending
  branches, Archive, Delete (disabled while live). Task menu: Open
  details, Session summary, Filter by this item, View request / runbook,
  Stop this step / Skip step / Unskip step / Stop proposal / Edit prompt
  - availability mirrors the modal, both now computed by the shared
  `runCtl()` helper extracted from `showRunDetails`. New delegated attrs:
  data-agent-detail + data-edit-prompt keyed by agentKey. Native menu is
  preserved on editable fields, a live text selection, non-row targets,
  and via Shift+right-click as the documented escape (hint lives in the
  menu footer + the topbar flow line). Dismissal: outside click is
  swallowed (the row under it never fires - no accidental filter/detail
  jump), Esc closes and returns focus to the row, scroll/resize close
  it, the questions auto-popup clears it, Tab is trapped while arrows
  move focus. tr.agen rows are now focusable (tabindex + Enter/Space
  opens details; aria-haspopup on both row types). Position clamps to
  the viewport; keyboard-opened menus anchor to the row rect. 276/276
  mock tests + demo green; 4 new ContextMenuTests (run/task state items
  + dispatch to endpoints, filter, disabled/unavailable actions,
  outside-click swallow, Esc/scroll dismissal, editable/selection/
  Shift+right-click native preservation, ContextMenu/Shift+F10 open,
  focus restore) + a 25-check Playwright probe against real Chromium.
- DONE 2026-10-16 (full-trajectory-view, wave step of run
  20261016-013719-workflow-ux-safety): arc entries in the task modal are
  now nested `<details class="arcent">` that expand to the step's FULL
  retained text - the arc's 110-char `head` was truncation at
  `trajectory_arc()`, not a CSS ellipsis, so expansion lazy-fetches
  `GET /api/runs/<r>/steps/<s>/trajectory` (and
  `/api/requests/<id>/trajectory` for the planner session), which
  re-reads the step's ATIF export for message, reasoning_content,
  tool-call args (rendered `key: value` so multiline edit payloads stay
  literal), and tool results paired by source_call_id (orphan results
  still surface as `extra_obs`). Header gains Expand all / Collapse
  all; `trajOpen` (agent-keyed Sets) keeps open groups+entries across
  the 2s modal rebuild - which previously collapsed even groups - and
  `trajDetail` caches detail by export-path+mtime (resume rewrites
  refetch). Per-entry Copy ships verbatim `arcPlainText`, never HTML.
  `.arcpre` wraps long lines + stays selectable; a live in-modal text
  selection pauses the tick rebuild (`modalSelActive`). 200k/field cap
  is recorded as `trunc` and labeled; mock/step-less exports 404 with
  the reason - nothing is fabricated from the head. Groups past the
  40-group cap announce their count. `/api/state` stays compact (only
  arc.em + groups_total added). 272/272 mock tests + demo green; 8 new
  TrajectoryDetailTests incl. node-harness expand/copy/run-switch/XSS.
- DONE 2026-10-16 (modal-detail-navigation, wave step of run
  20261016-013719-workflow-ux-safety): the task modal's 'Session summary'
  no longer opens a separate browser window - it renders the report inside
  the shared `#detail` modal via a new `?frag=1` branch on the existing
  `/summary` route (server-side `_summary_body` fragment, same escaping;
  `?raw=1` markdown and the standalone page are unchanged, so direct URLs
  stay usable). The in-modal view gets the same `.dnav` chrome as the task
  nav - sibling-named arrows + `task N of M`, disabled at the pipeline
  ends, ArrowLeft/Right shared - and an 'Open as page â†—' escape hatch.
  New `modalHist` Back stack: task -> request/runbook and request -> run
  details push the view they leave ({kind,key|id,scroll}); Back restores
  the exact task/request/run incl. scroll offset, skipping entries whose
  rows vanished, while sibling walks and the refresh re-render preserve
  the stack instead of clearing it. The 'View request / runbook' link in
  a task modal now returns to THAT task on Back instead of dropping the
  modal state. Escape/dx/backdrop still tear the whole dialog down.
  264/264 mock tests + demo green; 2 new ModalDetailNavTests (HTTP-level
  frag/raw/404 + node-harness prev/next/Back/missing/switch-runs).
- DONE 2026-10-16 (summary-purpose-parsing, wave step of run
  20261016-013719-workflow-ux-safety): the reported
  `/summary` page for resolve-task-modal-navigation rendered the raw
  `<!-- conductor-purpose: resolve-merge -->` marker as its Task text
  (and the comment ate ~45 of the 160-char summary budget). Root cause
  was task-description selection treating conductor metadata as prose,
  not the comment token itself. New `split_purpose`/`purpose_label`/
  `task_display`/`task_summary` helpers recognize the marker ONLY at the
  head of a body - a mid-text mention (like this step's own prompt) or
  an unrelated HTML comment in task text is never stripped. Dispatched
  records now store `purpose` as its own machine-readable field with a
  marker-free `task`; old marker-bearing records are parsed back at read
  time so existing runs render clean retroactively. Marker-only tasks
  display `conductor task: <label>` (resolve-merge -> "merge conflict
  resolution"). The marker still ships verbatim in the dispatched prompt
  file - mock_devin's resolve-merge trigger relies on it - and pending
  rows keep `task_full` literal since it doubles as the Edit-prompt
  prefill (a save round-trip must not drop embedded metadata). Summary
  gains a `- **purpose:** `token` (label)` bullet; the modal gains a
  `purpose` kv row; agents rows carry `purpose`. HTML sanitization
  unchanged - escaping tests + new injection test green. 262/262 mock
  tests + demo green; verified against the real run's state.json.
- DONE 2026-10-16 (deferred-merge-engine, wave step of run
  20261016-013719-workflow-ux-safety): `merge = "deferred"` (run-level
  or per-step; implies worktree) separates worktree isolation from
  merging - branches, worktrees, commits and verify results stay pending
  in `state.json`'s `pending_merges` until a human runs
  `conductor.py merge <run_dir>` (new CLI verb; `--steps` picks a
  subset) or clicks Merge pending branches. Sequential deferred steps
  on one project branch off a per-run integration baseline
  (`conductor/<run>/baseline-<slug>`) that FFs only over verified tips
  and absorbs target movement via merge-tree plumbing; same-wave
  same-project members stay `alternative`s, never auto-combined. The
  explicit merge stages verified entries in an isolated `stage-<hash>`
  worktree, re-runs their verify gates on the combination, resolves
  conflicts via merge_resolver IN the stage, and lands the recorded
  target atomically (ff-only merge when checked out, expected-value
  update-ref otherwise) - a red combined verify or busy repo keeps
  everything pending; retained work is never deleted. Bonus fix: ALL
  merge paths (post-wave, resume-finalize, merge_run, deferred staging)
  now serialize on `.git/conductor-merge.lock` + `abort_own_merge`
  refuses while a live lock-holder exists - the cross-run merge-kill
  recorded by isolate-proposal-planning. 250/250 tests + demo green;
  13 new DeferredMergeTests.
- DONE 2026-10-16 (follow-up-plan, closing step of run
  20261016-013711-proposal-flow-fixes): reviewed the run against the
  human's three requests - ALL THREE DELIVERED on master (stop at
  a91fa43; retain + compact-labels landed transitively via the verify
  branch, merge 00722cc; verify fixed 4 real integration regressions,
  232/232 + demo + live Playwright probe green). Residuals only, none
  blocking: (a) retained planner history is half-navigable - attempts
  share `<id>.gen.log`/`.export.json` so non-newest rows lose file
  links, and the in-flight row shows '...' as session id until attempt
  end (both were the retain step's own deferred items); (b) run-record
  truthfulness bug found by this review - state.json still marks the
  two transitively-merged steps `failed`/`merged:false` though their
  commits are on master, which also falsely offers them as
  merge-pending (merged flag only set on directly-merged branches);
  (c) the whole flow is mock-verified only - no real planner has been
  stopped/reprepared/retained live. Drafted
  manifests/proposal-flow-fixes-followup.toml (validated by `plan`):
  plan-history-completeness (per-attempt spool artifacts + live sid
  probe), merged-record-accuracy (ancestor-check backfill + cleanup of
  this run's two merged parked worktrees), proposal-flow-live-smoke
  (tempdir-copy serve - trap-5-safe), closing follow-up-plan. NOT
  launched - human review. Deliberately excluded: concurrent-run
  merge/checkout protection (owned by the still-live
  workflow-ux-safety run's own follow-up; its parked
  isolate-worktree-services branch is not this manifest's business)
  and anything already drafted in ux-controls-followup.toml /
  proposal-ui-fixes-followup.toml. UPDATE 2026-10-16: merged via
  resolve-follow-up-plan - was never a real conflict: conductor's
  merge never started (events logged `files: []`) because the step's
  own draft manifests/proposal-flow-fixes-followup.toml sat untracked
  in the main checkout, byte-identical to the branch's copy - git
  refuses to overwrite untracked files even then. Cleared it and the
  branch fast-forwarded to 65cae4a (no merge commit). New trap class:
  a same-path untracked file blocks merge start and reads as a
  conflict to the driver.
- DONE 2026-10-16 (fix-compact-labels, wave step of run
  20261016-013711-proposal-flow-fixes): "the labels under the run table,
  sometimes are not visible in the 'compact' view" - two losses, both
  reproduced with Playwright probes (evidence in
  fix-compact-labels/STATE.md + scratch/). (a) `td br { display:none }`
  in compact collapsed the `.stage` chips (running / paused - resume
  ready / Preparing proposal / archived / Needs review) INLINE after the
  run name where `td.wrap`'s ellipsis clipped them depending on name
  length + column width - the "sometimes". The chips + `.genline` + dir
  `.sub` now live in a `<div class="rlabels">` block that keeps its own
  line under the name in both densities; `Needs review` groups there too.
  (b) `planner:`/`steps:`/`est`/`not planned` were `.sub` spans hidden
  by the blanket compact `.sub` rule despite `fmtTc()` being written as
  their compact form (dead branch since efec536) - they're `.toksub`
  now (same look, exempt from the hide, block-stacked in compact).
  Also: `td .stage` ellipsizes inside the pill when wider than the
  column (+`title=` tooltip); `render()` sig now includes `density` so a
  Compact|Full flip re-renders the fmtT/fmtTc swap even when the payload
  is unchanged (previously the format never updated until data changed).
  New node-harness test `test_compact_run_row_labels_stay_visible` green.
  Suite: 225 tests, 2 PRE-EXISTING failures - StopProposalTests x2
  (stash-verified on the base commit): `stop_generation` restores the
  record but `_publish_from_spool` only gates on `_disk_untouched`
  (`updated_at`, SECOND granularity) - a same-second Stop still gets its
  late TOML published as `ready` + replaces an earlier manifest.
  Interaction of merged stop-proposal-preparation x
  isolate-proposal-planning; owner = verify-proposal-flow / a follow-up
  (suggested: publish gate should re-read the file's status+gen_id, or
  clear gen_id in the stop write).
- DONE 2026-10-16 (follow-up-plan, closing step of run
  20261016-013703-proposal-ui-fixes): reviewed the run - 1/2 worker steps
  landed. `fix-regenerate-label` done + merged (e1eaa7c): 'Needs review'
  chip + modal nag now suppress while stage is generating/questions.
  `compact-live-output` work is COMPLETE but parked unmerged (branch
  conductor/20261016-013703-proposal-ui-fixes/compact-live-output,
  fd86225 + a7a3da9, 211/211 green in-worktree) - it recorded `failed`
  only because the post-step verify gate hit a flaky sentinel test:
  `test_queued_step_flag_skips_without_spawning` (test_worktrees.py:884,
  rc 0 != 1 at ~306s; green standalone). New issue: SECOND flake in the
  `_flag_soon` wall-clock family (same class as the ux-controls
  stop-flag flake) - possibly a real race where a flag landing between
  the pre-spawn check and subprocess spawn is dropped. Drafted
  manifests/proposal-ui-fixes-followup.toml (validated by `plan`): merge
  the parked branch (main checkout, union-merge bookkeeping), harden the
  flag-timing test family + close the pre-spawn flag window, live-smoke
  both UI fixes, closing follow-up-plan step. NOT launched - human
  review. Overlap note inside the file: supersedes the still-unlaunched
  ux-controls-followup flake step; its merge/verify-timeout/ask-smoke
  steps are deliberately not duplicated here. UPDATE 2026-10-16:
  compact-live-output merged via resolve-compact-live-output (merge
  commit 7e5f3cf; both conflicts were both-append bookkeeping on
  QUEUE.md + efficiency_stats.md - kept both sides, chronological
  order).
- DONE 2026-10-16 (compact-live-output): compact density hid every
  `.sub`/`<br>` in the proposal run row, so a generating request showed
  only the 'Preparing proposal' chip with no live output. The same
  `gen_last` value now also renders inline as `.genline` right after the
  stage chip - `display:none` at full density (the `.sub` line is
  unchanged there), a single ellipsized line under `body.compact`,
  truncated to the last 160 chars (fresh end) of newline-less blobs.
  The existing `prevTail` diff keys it as `genline|<runkey>` and gives it
  the `.fresh` flash on new output; the flash now applies to the
  full-view `.sub` line too. `questions` rows show 'planner waits on N
  answer(s)' in the same slot; pre-output shows 'planner warming up'.
  211/211 mock tests green.
- DONE 2026-10-16 (fix-regenerate-label): clicking Regenerate on a stale
  proposal showed 'Preparing proposal...' AND 'Needs review' at once -
  `stale` is only cleared by `_publish_proposal` on success, so it
  outlives the whole planning pass. Both stale surfaces (the runs-table
  chip and the modal's 'Request fields changed' nag) now gate on the
  stage not being `generating`/`questions`: the in-flight planning chip
  owns the row while the planner runs, and 'Needs review' returns if
  generation fails or the proposal still mismatches once planning stops.
  The `stale` flag itself is untouched - it still gates launch. 210/210
  mock tests green.
- DONE 2026-10-16 (isolate-proposal-planning, wave step of run
  20261016-013719-workflow-ux-safety): proposal generation no longer
  fights repo traffic. The planner writes everything to a per-request
  spool at %TEMP%/agent-conductor-planner/<id>/ (gen.log, export,
  draft.toml, request.json overlay, job.json) - never a watched cwd - so
  a live resolve-* session or open merge can't block or be interrupted
  by generation; only the short publish waits on _wait_writable, which
  now also treats an open MERGE_HEAD as blocking for non-ignored dests.
  Planner runs in its own process group; drain_planner_spool replays
  spooled state at serve startup and on a throttled poll (publish draft,
  extract from a finished log, or error with artifacts preserved).
  215/215 mock tests + demo green. UPDATE 2026-10-16: merged via
  resolve-isolate-proposal-planning (merge c2a8c6c + fix 400124b;
  224/224 green). Two cross-run race findings: a concurrent run's
  abort_own_merge killed the first in-progress merge mid-resolution
  (re-merged cleanly), and a foreign driver's `git add -A` checkpoint
  committed the second merge under a 'checkpoint' message - merges are
  NOT protected from concurrent runs in the same checkout. The merge
  itself was semantic: stop-proposal's gen_id attempt scoping now
  composes with the spool - job.json carries gen_id, _publish_from_spool
  /_flush_spool take an ownership guard, drain skips spools whose gen_id
  is live, and _save_draft can't resurrect a stopped attempt's overlay.
- DONE 2026-10-15 (follow-up-plan, closing step of run
  20261015-182057-ux-controls): reviewed the run - 6/8 steps merged
  clean; `image-attachments` + `task-modal-navigation` were done work
  parked unmerged (conflicts only on .agent_tasks/QUEUE.md +
  efficiency_stats.md; the resolver's untracked-files guard refused
  silently - fixed since f261937). UPDATE 2026-10-15: image-attachments
  merged via resolve-image-attachments (merge commit 07915de; all three
  conflicts were both-append - kept both sides, normalized a stray `||`
  row prefix in efficiency_stats.md; 206/206 mock tests green).
  task-modal-navigation still parked - its verify hit the hard-coded
  600s gate despite a green suite. Drafted
  manifests/ux-controls-followup.toml: merge the remaining parked
  branch, configurable verify_timeout_s, .agent_tasks merge-safety,
  flaky stop-flag test, live planner-ask smoke. NOT launched - human
  review. UPDATE 2026-10-15: task-modal-navigation merged via
  resolve-task-modal-navigation - same both-append conflicts on
  QUEUE.md + efficiency_stats.md, kept both sides.
- DONE 2026-10-15 (run-link-affordance): run names in the runs table
  always render as links now (blue + underline, lighter blue on hover) -
  they are the run-modal affordance and previously looked like plain
  text until hovered. Focus fix: after Esc-close the still-focused
  button kept an outline clipped at the cell's left edge (td is
  padding-left:0 + overflow:hidden and the shared focus rule offsets
  the ring outward 2px) - `td :is(a,button):focus-visible` now draws
  the ring inside the element (`outline-offset:-2px`), which also
  covers the show-more links. 199/199 mock tests green.
- DONE 2026-10-15 (task-modal-navigation): the task detail modal gets a
  sticky `.dnav` bar - prev/next buttons naming their sibling step and a
  `task N of M` counter - walking the run's pipeline in manifest order
  (`seq` stamped per agent by collect_dashboard; record-order fallback
  for unmanifested steps, proposed_steps index for previews, none for the
  plan-proposal row or 1-step pipelines). `[data-nav]` clicks and
  ArrowLeft/ArrowRight share `navStep`; nav resets modal scroll, the 2s
  rebuild doesn't. `agentKey` prefers run_key over run_dir so proposal
  steps of different requests can't alias. 200/200 mock tests + demo
  green.
- DONE 2026-10-15 (trajectory drill-down + compact counters): each arc
  group is now an expandable `<details>` - per-entry timestamp,
  gap-to-next duration, and REAL per-turn in/out/cached tokens read
  from the ATIF step `metrics` (every LLM call is metered; nothing is
  estimated). Agents table drops the `task` column (still full in the
  modal) and gains a compact token form `in/out/cached NL` under
  compact density; run rows' planner:/steps: follow suit. Token
  findings so far: `total_prompt_tokens` is the FULL context re-sent
  per turn (cached is a share of it), so a 77-turn session sums ~11M
  even though ~98% was cache-served - the Desktop card reports only
  the uncached delta instead. First LLM call on a fresh agent-conductor
  session already carries ~107k prompt tokens.
- DONE 2026-10-15 (image-attachments): request-form image attachments -
  drop image files anywhere on the request dialog or paste a clipboard
  screenshot; each uploads raw bytes to `POST /api/attachments`
  (magic-byte sniffed PNG/JPEG/GIF/WebP/BMP, 20MB cap, same
  `_assert_unwatched` trap-5 guard as request writes), saves under
  `data/attachments/<ts>-<rand>.<ext>`, and its absolute path is
  inserted into `rq-raw` at the caret. The `[{name,path}]` list rides
  `attachments` on the request JSON (`_attachment_fields`, cap 20,
  changes mark a proposal stale), renders as a thumbnail strip via
  `GET /api/attachments/<name>` with a per-image `x` remove while the
  stage is editable, and lands in the planner prompt as an IMAGE
  ATTACHMENTS block. 199/199 mock tests + demo green.
- DONE 2026-10-15 (run-lifecycle-cleanup): finished runs auto-archive
  after `CONDUCTOR_ARCHIVE_HOURS` (default 48) - a `state["archived"]`
  stamp hides them from the default `/api/state` while files stay on
  disk; `sweep`/`serve` startup apply it plus a 30min throttled pass in
  `collect_dashboard`, the `view: N archived` chip lists them via
  `?archived=1`, the modal's Archive/Unarchive flips it manually, and
  `--resume` clears it. **Delete run** in the run modal `rmtree`s the
  record (confirm + live/resume/merge guards); spawned devin sessions
  become trap-13-style manual cleanup. 182/182 mock tests + demo green.
- DONE 2026-10-15 (steps-counters-clarity): run/agent token counters
  name themselves - `fmtT` renders `in X Â· out Y Â· cached Z Â· N turns`
  (cached always shown; turns = LLM steps, not manifest steps) instead
  of `X/Y+Zc Â·Nst`; run rows read `planner:`/`steps:`; `steps` column
  header + tooltips + the `?` explainer use the same words.
- DONE 2026-10-15 (run-control-taxonomy): one name per lifecycle
  action. `Pause run` is now the graceful hold (in-flight steps finish,
  nothing new launches, `aborted='paused'` - the machine-restart case);
  `Stop run now` stays the hard kill (`aborted='stopped'`); `Drain &
  stop` merged into Pause (`POST /stop {drain:true}` kept for
  back-compat, `/pause` raises drain flags for pre-v2 drivers); `Resume
  unfinished steps` covers paused/stopped/interrupted. Step-level: `Stop
  this step` kills one agent while the run proceeds (`stopped` no longer
  trips on_failure), and `Skip step`/`Unskip step` is a new durable
  `skip-<key>-<step>` sentinel - pending steps keep their queue
  position, stays in force across resume until unskipped, takes effect
  on the next resume if the turn already passed. CLI-launched runs now
  show manifest-declared pending rows (waiting/skipped) like request
  runs already did.
- DONE 2026-10-15: Lifecycle controls + recovery. Run modal: `Stop run
  now` (flags + pid-targeted taskkill of live steps) and `Drain & stop`
  (finish in-flight, launch nothing new); agent modal: `Stop this agent`.
  Sentinels live in %TEMP%/agent-conductor-flags (trap-5-safe), polled by
  wait_worker + the wave loop; killed steps record `stopped`, unlaunched
  `skipped`, state.aborted=`stopped|drained`. Resume clears sentinels and
  prepends INTERRUPTED_NOTE for running/lost/timed_out/stopped priors so
  a fresh session recovers from committed state. Dashboard-launched
  runs now use CREATE_NEW_PROCESS_GROUP so stopping the server never
  interrupts work (trap 15). `model = "adaptive"` supported (no effort
  suffix; export's agent.model_name -> model_actual shown + priced).
  Modal shows the full task (task_full from the prompt file); running
  agent 'result' cell is the log TAIL (\r-aware), not the head.
  Follow-up same day: live timeout editor per step (minutes sentinel
  polled by wait_worker), attempt N/max in the modal, `projects.local.toml`
  override for worktree checkouts, session 'gone' suppressed while the
  worker pid is alive (dead pid shows deletion immediately, no grace),
  save_state retries the rename (WinError 5 flake), runs-table steps
  column shows per-status counts only. Later: rc-0-no-STATUS note now
  distinguishes a token-bearing export (real work ran - check commits)
  from a true no-op; agent modal gains separate 'note' + 'log tail'
  rows (a set `next` no longer masks the failure note); --resume
  prepends the interrupted-state note to `failed` steps too. Next:
  `timeout_min` is now a floor with activity extension (`stall_min`,
  4x ceiling, `timeout_extended_s` recorded); lifecycle sentinels are
  keyed by run-path hash (cross-checkout collisions gone); resume/merge
  prefer the run's recorded worker binary; `adaptive` clears/disables
  the effort field (form + server-side normalization). Later still:
  run-level `Pause run` (`/pause` -> pause+stop sentinels; live step
  killed, nothing new launches regardless of on_failure, run records
  aborted='paused' and resumes clean); endpoints dual-write legacy
  bare-name sentinels for runs launched before flag-namespacing
  (state['flags']='v2' marks new-code drivers); run modal warns on
  'Interrupted' (no ended_at, nothing alive = conductor died);
  tail_status/tail_next now scan the whole log (8KB tail could miss a
  STATUS: before a long closing report); STATUS contract text hardened
  ("print it to stdout - filing it in STATE.md does not count" after a
  live worker did exactly that); run rows carry a state chip
  (running/paused/interrupted/halted - the resume-worthy ones say so);
  agent rows mark xN retry attempts and +ext timeout extensions; agent
  modal links the project's .agent_tasks/<step>/STATE.md.
- First real overnight manifest on a real project (Phase 2 in ROADMAP.md; live smoke already green)
- Push agent-conductor to GitHub if cloud steps on it are wanted - it has NO remote, and cloud workers clone from origin (human decision: repo name + private/public)
- Phone access to the :8773 dashboard via lan-bridge proxy (needs mobile-apps change; tasks-ui rows already give coarse progress)
- DONE 2026-10-14: Record session_id ~20s after spawn, not just at step
  end - `_capture_session_id` thread probes `devin list` up to 5x25s while
  the step runs (safe inside watched cwd: list is read-only, verified
  live). Falls back to the post-wait diff. Context that motivated it: a
  crashed/orphaned run leaves a session id never written to state.json,
  invisible to the `sessions`/`sweep_sessions` whitelist - manual cleanup.
- DONE 2026-10-14 (trap 12): Post-STATUS hang - `devin -p` does not exit
  while a session-spawned child is alive (views-floorplan run). Workers'
  playwright probes spawn `vite` (:8805) that outlives the script; the
  CLI then idles until timeout_min - 3 steps printed STATUS: done then
  burned 52/52/85min. camera-stability exited clean (1004s) ONLY because
  its agent `netstat|grep 8805` + `taskkill /F`ed the leftover server.
  Shipped: (a) `wait_worker` polls tail_status during wait - a STATUS
  line collapses remaining wait to STATUS_GRACE_S then tree-kill, counts
  as turn end, verify still runs; (b) `kill_tree` = taskkill /T so
  grandchildren die too; (c) open: worker-side convention (kill spawned
  servers before STATUS) - camera-stability's agent did it from
  apartment-3d's own trap note. floorplan-fixes was the different case:
  killed mid-explore-test with no STATUS - 90min was genuinely too short
  for that phase's calibration workload.
- DONE 2026-10-15: Sticky dashboard chrome. `.topbar` (title/meta/New
  request/density seg/filters) pins at top; every table `th` pins just
  under it via `--topbar-h` measured at render (filters wrap). `.wrapw`
  dropped `overflow-x` - an overflow ancestor becomes the scroll
  container and breaks `th` stickiness. Compact|Full toggle flips
  `body.compact`: wrap cells ellipsize to one line, `.sub` and `<br>`
  hide; persists in `localStorage['ac-density']`. Modal z-index bumped
  above the bar (50/51).
- DONE 2026-10-15: Run name is the details affordance (the separate
  Details link is gone; compact mode hid it on the .sub line anyway).
  Clicking the name opens the run/proposal modal; clicking elsewhere on
  the row still filters. Resume now pops stale 'aborted'/'ended_at' from
  state.json - a finished-then-resumed run no longer shows the 'Paused
  by user' notice forever.
- DONE 2026-10-15: Task-modal trajectory arc - `trajectory_arc` condenses
  a finished step's ATIF export (agent messages -> tool-call summaries)
  into phase-grouped spans (setup/audit/implement/test/docs/commit),
  rendered after the stdout tail. Retroactive for every past run.
- DONE 2026-10-15 (verify/merge-window false 'lost'): worker exits ->
  conductor runs ~5min verify+merge while the record still says 'running'
  on a dead worker pid -> dashboard flashed 'lost'/'interrupted-resume
  ready', and a serve-startup sweep stamped 'lost'+ended_at on a LIVE
  run. Fix: state.json now carries driver_pid/driver_started_at (the
  conductor's own pid, restamped on resume); driver_alive keeps the
  step live, sweep skips live-driver runs, and resume/archive/delete
  correctly refuse mid-verify. Old runs lack the field -> old semantics.
- DONE 2026-10-16 (task-outcome-summary, wave step of run
  20261016-100852-run-review-ux): the worker report gains a third contract
  line OUTCOME: - the human-facing one-to-two-sentence summary of what
  THAT step actually did (concrete results + material limits), printed
  beside STATUS:/NEXT: at column zero, last-match whole-log parse
  (tail_outcome, 300-char cap, missing never fails). Persisted as
  rec['outcome'] (newest) + rec['outcomes'] (bounded attempt/dispatch
  history, so a resume's fresh result replaces the display without
  erasing earlier accomplishments; a dispatch reporting nothing keeps
  the last real one). Surfaced as the task modal's outcome row (+earlier
  rows, honest muted 'not reported' for records that predate the
  contract - NEXT: is a handoff, never a stand-in), the agents-table
  result column (outcome leads, conductor note drops to a .sub line),
  summary-md bullets, cmd_status, and the follow-up plan's review
  sources. mock_devin prints it (outcome=/nooutcome/status= keywords);
  12 new tests, 307/307 + demo green.
- DONE 2026-10-16 (task-table-navigation, wave step of run
  20261016-100852-run-review-ux): the agents table got real click targets
  - the step name is a `linkbtn step-name` button (`data-agent-detail`,
  agentKey-keyed) that opens the task modal, and the run name a
  `linkbtn run-link` (`data-filter-run`, run_key before run_dir) that
  applies the existing run filter; the bare `tr.agen` click->modal
  catch-all is gone, so misclicks and text selection can't pop a dialog
  (the row stays focusable for Shift+F10 + Enter/Space = details, and
  dropped `cursor:pointer` since it no longer acts). `data-filter-run`
  dispatch moved to `closest()` like the other attrs. New `.dsel` mark
  (same palette as run `.selected`, hover-proof) highlights the row
  whose modal is open - `selKeys()`/`applySel()` key it by
  agentKey/runKey via a new `data-akey` on `tr.agen` (never row index),
  so re-sorts/filters/refreshes can't paint the wrong row; task nav +
  ArrowLeft/Right move the mark, closeDetail clears it, and run
  details/request/questionnaire/summary views mark their run row the
  same way. A task with no run association renders a plain-text run
  cell. 2 new TaskTableNavTests (click targets, keyboard, filtering,
  nav, highlight lifecycle) + test_modal_edit_form_locks updated for
  the new entry point; 322/322 + demo green.
- DONE 2026-10-16 (emulator-resource-ownership, wave step of run
  20261016-013719-workflow-ux-safety): Android devices were entirely
  worker-side prompt business - workers hardcoded `adb -s emulator-5554`,
  switched to the physical handset ad hoc, and one step's cleanup ran
  `emu kill` on a serial a sibling could have been using (evidence in
  20261014-235630-apartment-play-edit artifacts; mobile steps also
  overlapped across runs 20261014/20261016 by wall clock). Now a
  `[[step]] device = ...` ("any"|"emulator"|"physical"|exact serial;
  `true` = any; inherited from [defaults] or the project's
  `projects.toml` entry like `verify`; device+cloud is a manifest
  error) leases ONE adb serial machine-wide in
  `%TEMP%/agent-conductor-devices/` for the step's whole life -
  attempts, interrupt respawns and the verify gate get
  `ANDROID_SERIAL` + `CONDUCTOR_DEVICE_SERIAL` and the prompt gets a
  device note. Lease files are O_EXCL + token + owner/worker-pid
  liveness (merge-lock shape; owner_started_at is the owner's own
  process-creation stamp so a slow interpreter start can't mark a
  live owner dead); a fresh-but-tokenless file counts as held so an
  O_EXCL winner's half-written lease can't be unlinked under it
  (Windows PermissionError). Busy-but-attached queues uncapped
  (`queued` + `device_wait`, halt sentinels honored, prompt-edit /
  rnote / intrq re-folded after the wait); never-attached ends
  `blocked` after `device_wait_s` (600, 0 = forever).
  `device = "emulator"` (or `any`+`device_boot`) boots a free AVD on
  a free even console port where the emulator binary + AVDs exist
  (`avd-<name>` slot lease stops double-boots, `emu avd name`
  excludes already-running images); lease `booted`+`emu_pid` makes
  release kill exactly that instance (`emu kill` -> image-name-
  checked taskkill) and a stale-lease break reaps a recorded orphan
  the same way - never a user's device or someone else's emulator.
  adb/emulator discovery: CONDUCTOR_ADB/_EMULATOR, PATH, then SDK
  dirs. Surface: device/device_spec/device_booted/device_wait(_s)
  on the rec, dashboard row + modal field, `[device=]` plan tag,
  device_{wait,acquired,booted,released,blocked,reaped} events.
  Known gap documented in AGENTS.md: the lease owns the SERIAL, not
  the shared adb server - workers must still pick free host ports
  for `adb forward tcp:NNNN` rather than reusing fixed ones. 14 new
  DeviceOwnershipTests (competing projects queue, explicit serial,
  stop-while-queued, live/stale owner recovery, booted-emu reap
  with image-name guard, verify env, manifest validation);
  380/380 + demo green.
- DONE 2026-10-16 (execution-context-labels, wave step of run
  20261016-100852-run-review-ux): two label truths. (a) Run rows now
  carry a compact 'wt' chip from `iso` {wt,local,cloud} - the run's
  CONFIGURED worktree coverage computed by `_run_isolation`: parsed
  manifest first (merge='deferred' already reads as worktree, cloud
  steps never count), then the request's proposed_steps snapshot, then
  the `wt`/`cloud` fields now stamped on every step record at
  queue/dispatch/pre-spawn-terminal - branch names and liveness never
  consulted, so undispatched runs label correctly and a parked branch
  on a legacy record invents nothing. Partial coverage spells it out on
  the chip itself ('wt 1/2' + title explaining the rest run in the
  checkout). (b) The trap-5 'A live agent is watching this workspace'
  409 is still reachable (data/ sits under ROOT - a main-checkout or
  resolve-* step watches every managed dir) and now names its owner:
  `_watchers_of` collects ALL live steps whose work_dir contains a
  destination (sorted, dedup-stable), `_watcher_identity` renders
  '<run-dir>:<step> (slug, session <id8>)' with missing fields dropped
  rather than 'None', capped at 3 + '+N more'. The same identity now
  feeds the sweep/archive/trash-purge DRY-RUN lines (via
  `_runs_watched` returning the identity list), `_any_live_step_in`'s
  deferred-merge refusal, and the launch-time project-overlap 409.
  Identifiers only - task text never enters an error string. 13 new
  tests (iso derivation matrix + watcher identity/multi/fallback +
  node chip render).
