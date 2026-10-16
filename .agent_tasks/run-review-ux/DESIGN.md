# run-review-ux - inspection + design notes

Produced by the `inspect-and-design` step of run
`20261016-100852-run-review-ux` (manifest
`manifests/20261016-100358-0d289b96-run-review-ux.toml`). Everything below
refers to `conductor.py` at commit `50d6273` (~10.1k lines; line numbers
approximate) and the dashboard it serves (`_DASH_HTML`, lines 7045-9759).

Verify gates: `python run_tests.py` (unittest discovery of `tests/`,
276 tests, ~460s at this commit), `python conductor.py demo`,
`python conductor.py plan <manifest>`. `run_tests.py --quick` exists;
`logs/tests.log` holds the full run. There is NO committed Playwright
suite - browser probes so far were ad-hoc scratch scripts (see
row-context-menus notes in QUEUE.md).

---

## 1. Current-state map

### 1.1 Run table + agent (task) tables

`collect_dashboard` (~line 6656) builds the `/api/state` payload every
~2s (JS `tick()` at ~9750): `runs[]` dicts (~6687, ~6924) and `agents[]`
dicts (~6778, proposal/pending/plan rows at ~6842-6999). `render(d)`
(~8785) rebuilds HTML only when a JSON sig changes; `tail` is excluded
from the sig and streams separately.

Runs table (~8861-9022): `tr.runrow` + `data-run-key`; run name is a
`linkbtn` (`data-run-details` for real runs, `data-open-request` for
proposal rows); `.rlabels` holds `.stage` chips (`STAGES` map ~7338;
`runStateChip` ~8894 for running/paused/interrupted/halted; the stale
`Needs review` chip at ~8937 - SEE NAME COLLISION in §3.4), counts cell
colors via `COLORS` (~7332).

Agents tables (one per slug, ~9024-9118): `tr.agen` + `data-i` index +
`data-run-key`; whole-row click opens the task modal (handler ~9576);
status = `.dot` + text in `COLORS`; step cell carries `wt`/`merged`/
`unmerged` facet chips (~9080-9090); `data-el` cell ticks elapsed.

Modals share ONE `#detail`/`#dbody`/`#backdrop` (markup ~7330):
- Task modal `showDetail` (~7828): kv rows incl. `status`, `result`
  (=a.next), `note`, `log tail`, `verify`, `attempt`, `elapsed`;
  `.actions`: Session summary, Stop this step, Skip/Unskip, timeout
  editor, Edit prompt, Resume notes, View request/runbook; stdout tail
  (`#dtail`) + trajectory arc. Rebuilt every sig-changing tick on the
  SAME agent (`detailKey` = run_key|step, ~9123-9133).
- Run modal `showRunDetails` (~8073), control logic in `runCtl` (~8053):
  paused/interrupted notices; Show-only/filter, View request, Stop
  proposal, Merge pending branches, Pause/Stop/Resume, Archive, Delete.
- Context menus `ctxRunItems`/`ctxAgentItems` (~8185-8278): plain
  buttons carrying the SAME `data-*` hooks as modal buttons through the
  one delegated click handler (~9187+). Any new action needs BOTH a
  modal button and a ctx item - that parity is a project convention.

Highlighting today: `tr.runrow.selected` = the run FILTER mark (~8940);
`tr.agen.hl` = hover/selected-run correlation via `applyHl` (~9740).
There is no "modal is open on this row" marker.

### 1.2 Completion reporting

Worker contract: `STATUS:`/`NEXT:` lines parsed by `tail_status`/
`tail_next` (1182-1205; anchored `^X:` column 0, whole log). Status
matrix at ~2403-2418 (rc/STATUS/verify_must_pass). Terminal record write
at ~2420-2470: `status, ended_at, status_line, verify_ok, next, note,
elapsed_s, attempts, session_id(s), exit_code, tokens, model_actual,
prompt_edited, timeout_extended_s`. `step_summary_md` (~1596) renders
`/api/runs/<r>/steps/<s>/summary` (?frag modal / ?raw markdown /
standalone page) and persists `<step>.summary.md` once nothing is live
(~1704). `resolve-*` steps go through `run_step` too - they are ordinary
task recs.

Run-level "ended" writes: normal end ~3068, `halt_remaining` ~2738,
auth aborts ~2703/2779, sweep's run-close ~3207. Resume POPS
`ended_at`/`aborted`/`archived` (~2664-2668) - a resumed run is
"in flight" again until it re-ends.

### 1.3 Persistence + the trap-5 channel

- `data/runs/<ts>-<name>/state.json` = the registry (top fields listed
  in §1.2 of AGENTS.md + `driver_pid`, `pending_merges`, `flags="v2"`).
- `data/requests/<id>.json` = request records.
- Sentinels: `%TEMP%/agent-conductor-flags/`, path-keyed
  `_flag_key(run_dir)` = `<dirname>-<sha256-of-resolved-path[:8]>`.
  Prefixes in use: stop/drain/pause/timeout/skip/prompt/rnote (run+step)
  and `stopgen-<reqid>`. `clear_run_flags` removes halt flags on resume
  but EXEMPTS `skip-`/`prompt-`/`rnote-` by prefix (~2027) - new durable
  flags must join that exemption. `delete_run` sweeps `*<flagkey>*`
  (~6023) - free cleanup for correctly-named flags.
- `save_state` (~1157): tmp+replace, WinError-5 retries. `log_event`
  appends `events.jsonl`.
- TRAP 5: ANY write inside a live worker's watched dir ends its session.
  Guards: `_assert_unwatched(runs_dir, *dests)` (~3753), `_wait_writable`
  (~3774, polls it), `_runs_watched` (~3143, sweep/archive dry-run),
  `_any_live_step_in` (~5374), `_merge_open_for` (~3730). Liveness:
  `step_live(rec, driver_up)` (~704), `driver_alive(state)` (~687,
  `driver_pid`+creation-time guard), `effective_statuses` (~731).

### 1.4 Timing / resume

- Step elapsed: `t0` at ~2281, `rec["elapsed_s"]` overwritten at ~2426
  (attempts share one interval - retry loop ~2292-2382). `started_at`
  re-stamped at every dispatch (~2254), `ended_at` cleared. -> On
  --resume the prior `elapsed_s` is LOST (the cumulative-elapsed bug);
  only the last interval survives. `ended_at`/`note`/`next` also get
  overwritten by the new completion (desired) - but nothing accumulates.
- Display: JS `elapsed(a,now)` (~7436) = `elapsed_s` else
  `now-started_at` while running/queued; `tickElapsed` writes
  `td[data-el]` (~8779). `cmd_status` computes the same span (~3126).
  `step_summary_md` uses `elapsed_s` (~1606).
- Run level: only `started_at`/`ended_at`; no run elapsed field or
  column exists. "Run execution time" must mean DRIVER wall time
  (intervals of the conductor process), not sum of step durations
  (waves overlap). `driver_started_at` is restamped per driver session
  (~2672) - prior driver intervals are not stored anywhere.
- No clock seam: `time.time()` is called directly at ~15 sites
  (`now_iso()` only covers ISO stamps). Deterministic-clock tests will
  need a `conductor._now()`-style wrapper or `mock.patch('time.time')`
  (acceptable for serial unit tests; mock runs are subprocesses whose
  wall time is unaffected).

### 1.5 Workspace-watcher warning

`_assert_unwatched` (~3753) raises
`RequestError("A live agent is watching this workspace; wait until it
finishes before changing requests or manifests", 409)` - no identity.
Callers: `create_request` (~3786), `_wait_writable` for the proposal
publish (~3774), attachment POST (~9890), `set_run_archived` (~5980),
`delete_run` (~6012); `save_request`/`apply_request`/`launch_request`/
`answer_request` reach it through their own write paths (grep
`_assert_unwatched|_wait_writable` for the full set). `_runs_watched`
(~3143) returns a bare bool for sweep/archive dry-run notices.
`_any_live_step_in` (~5374) already formats identity as
`"<rundir>:<step>"` (~5399) - reuse that shape.

### 1.6 Manual pending-branch merge flow

`POST /api/runs/<dir>/merge` -> `merge_run` (~5626) -> `_merge_run`
(~5654): guards today = RUN_DIR_RE + state.json + `any(step_live(...))`
(5667) + `_RESUME_PIDS` liveness (5670); then legacy parked-branch loop
(manifest order, `_RepoMergeLock` per repo ~5718, optional
`resolve_merge_conflict`), then `_merge_deferred_group` for
`pending_merges`; `merge_manual` event; status flips at ~5735
(failed->done "merged via dashboard").
`_RepoMergeLock` (~5305, `.git/conductor-merge.lock` pid file) already
serializes every merge path across processes; `_MERGE_BUSY` serializes
per-run merges in this process.

Gaps the `manual-merge-guard` step must close:
1. `step_live` only treats `status=="running"` records as live. A live
   DRIVER with no 'running' records (the window between resume launch /
   wave boundary and the first step marking running; also the post-wave
   verify/merge window ends when the driver stamps records) passes the
   check -> merge races a driver. `_merge_run` should refuse on
   `driver_alive(st)` directly, not only via step_live. Same hole in
   `resume_run` (~5270) and `delete_run` (~6002) - flag for the
   implementer whether to harden those too (same one-line shape).
2. `resume_run` does not check `_MERGE_BUSY` - Resume clicked mid-merge
   launches a driver whose own post-wave merges and state writes race
   the manual merge. Add the symmetric refusal.
3. UI: `runCtl.pendingMerge` (~8065) shows an ENABLED button whenever a
   parked branch's own step isn't live, even while sibling steps run -
   the 409 is the only message. Disable + explain instead (ctx items
   already support `dis:` + `title:`; modal button `disabled`+`title`).
4. "Stop request / paused / incomplete shutdown must not read as
   completed": `driver_alive` covers the post-stop drain window
   (driver still finishing its wave); a fully halted run (aborted set,
   driver dead, nothing live) is legitimately mergeable.
5. Conductor-managed merges (post-wave ~2974, resume-finalize ~2799,
   `_merge_deferred_group` staging ~5437) stay untouched - the guard is
   on the user-facing `merge_run` entry point only.

---

## 2. External precedents (sources)

| Source | What it does | Takeaway for us |
|---|---|---|
| GitLab MR "Viewed" checkbox - https://docs.gitlab.com/user/project/merge_requests/changes/ | "mark the file as viewed until it changes again"; clears on content change OR manual uncheck; per-user, server-persisted (was localStorage) | Ack binds to a content fingerprint; change invalidates; reversal is a first-class checkbox. |
| GitHub PR file "Viewed" - https://codewithhugo.com/github-review-reset-viewed/ | `.js-reviewed-toggle` / `.js-reviewed-checkbox` per file; resets when the file changes after marking | Same fingerprint model; also shows users WANT bulk reset - our "Mark as needing review" covers it per item. |
| GitHub stale-approval dismissal - https://docs.github.com/en/pull-requests/how-tos/review-pull-requests/approving-a-pull-request-with-required-reviews | approval is bound to a commit SHA; new commits dismiss it -> re-review needed | Completion identity must be a fingerprint of the RESULT, not the entity. |
| Gerrit attention set + Reviewed flag - https://gerrit-review.googlesource.com/Documentation/user-attention-set.html and https://gerrit-review.googlesource.com/Documentation/user-review-ui.html | bold rows = needs your action; new patchset resets everyone to unreviewed; manual mark/unmark via a change action; the "Mark Reviewed" preference makes VIEWING a file auto-mark it - OPT-IN, not default | Explicit who-acts-next state; re-completion resets review; auto-ack-on-view exists only as a user preference -> keep ack explicit (matches the requirement). |
| GitHub notifications inbox - https://docs.github.com/en/subscriptions-and-notifications/how-tos/viewing-and-triaging-notifications/managing-notifications-from-your-inbox | Read/Unread/Done triage; "Mark as unread" restores; `is:unread` filter; unread = bold + dot | Three-state triage language ("unread"/"read") and reversal as a peer action to ack. |
| Azure DevOps "Mark as reviewed" - https://learn.microsoft.com/en-us/azure/devops/repos/git/review-pull-requests (+ sprint-159 notes) | per-file/folder checkboxes: "only meant to track your progress... does not represent voting... only visible to the reviewer" | Review state is a PERSONAL tracking layer independent of the artifact's outcome - exactly our model. |
| WCAG 1.4.1 Use of Color - https://www.w3.org/WAI/WCAG22/Understanding/use-of-color; badge guidance https://ux.redhat.com/elements/badge/accessibility/, https://design.cms.gov/components/badge/ | color may never be the only carrier; badges need text or accessible name; dynamic changes need status roles | Our indicator must ship text (`unreviewed` chip) + non-color marks (bold/border); `title`/aria text on rows. |

## 3. The chosen model

### 3.1 Semantics

Two orthogonal marks per entity (run and task), separate from
`status`/`merged`/`archived`/`stale`:

- `completion_seq` (int, on `state` and on each step `rec`): bumped at
  every terminal transition that changes what a human would review.
  Absent on legacy records.
- `review` = `{seq, at, reviewed}`: the newest human mark. `seq` = the
  completion_seq it acknowledged (null = mark on a seq-less record).

Display rule (server computes a plain `needs_review` bool per row/run -
keep JS dumb):

    needs_review = mark.reviewed is False                     # tombstone
                   or (completion_seq is not None
                       and mark.seq != completion_seq)

`mark` = flag content if present else `rec["review"]`/`state["review"]`;
`reviewed is False` is the explicit "needs review" tombstone and wins.

### 3.2 Where completion_seq bumps

A shared `_bump_completion(rec)` (and run-level equivalent) at each
site that writes a terminal outcome AFTER work ran:

- `run_step` terminal update (~2462) - covers every status incl.
  `stopped`; `resolve-*` steps get their own seqs for free.
- resume merge-finalize status flips (~2824 done / ~2830 failed /
  ~2840 merged=False) - a status a human may have seen changes.
- `merge_run` "merged via dashboard" flip (~5735) and conflict mark
  (~5742).
- `sweep_runs` lost-mark (~3195) and run-close `ended_at` (~3207).
- Run level: every `ended_at` stamp (~3068, ~2738, ~2703, ~2779, ~3207).
- NOT: `halt_remaining`/`_flag` skipped records (never ran - nothing to
  review), `archived` stamps (view filter), note/timeout/prompt
  metadata edits (must not reset ack - seq doesn't move on them).
- Also pop `completion_seq` when a step RE-dispatches
  (`rec.update(status="running"...)` ~2252): the prior completion is
  superseded while running; the next terminal write stamps a new one.
  (Cheap alternative if preferred: gate the indicator on non-live
  effective status - pick one, document why.)

### 3.3 Storage + write path (trap 5)

Sentinel-first, fold-on-save - the established pattern:

- `review_flag(run_dir, step=None)` -> `review-<flagkey>` /
  `review-<flagkey>-<step>`; JSON body `{reviewed, seq, at}`.
- Endpoints: `POST /api/runs/<dir>/review` and
  `POST /api/runs/<dir>/steps/<step>/review`, `{reviewed: bool}`.
  Write the flag always (always safe); when the run has nothing live
  (same guard shape as `set_run_archived` ~5976) also fold into
  state.json immediately so a never-resumed run still carries the mark.
  Validation: 404 unknown run/step; 409 on `reviewed:true` when there
  is nothing to review (no completion_seq and no prior tombstone).
- `_fold_review_flags(run_dir, state)` called next to save_state sites
  (wave loop, step end, merge paths, or inside save_state itself -
  flag wins on `at` conflict); dashboard reads the flag LIVE via
  `_flag_text`-style overlay like `prompt_override`/`resume_notes`/
  `_skip_flagged` already do (~6812-6815).
- `clear_run_flags` exemption: add `"review-"` to the prefix list at
  ~2027 - resume must not wipe acks (their seq just stops matching).
- `delete_run` cleanup is automatic (`*<flagkey>*` glob ~6023).
- state.json copy = durability for a run dir moved between checkouts
  (flag keys are path-hashes - a moved dir orphans its flags).

### 3.4 Labels + accents (evaluating the suggested green/orange)

Name collision to handle first: `Needs review` already exists as the
stale-PROPOSAL chip (red `.stage error`, ~8937) meaning "manifest no
longer matches the request". Keep it (different domain); use a
different word for completions - recommendation: **`unreviewed`** chip
text at table density, button text exactly as the manifest names them
("Mark as reviewed" / "Mark as needing review").

Accent evaluation:
- Green for runs: REJECTED. `#3fb950` is `done`/`merged`/auth-ok in
  this palette; a failed run outlined green reads success-onto-failure
  and the human's own constraint says an unsuccessful outcome must not
  look successful. Green also can't encode anything - the mark means
  "look at this" at both levels.
- Orange for tasks: DEFENSIBLE but imprecise. `#f0883e` = `timed_out`
  and `#ffa657` = `questions` - an orange review pill next to an orange
  status leans on the text to disambiguate (legal per WCAG since text
  exists, but a free hue is cleaner).
- Recommendation: ONE attention accent for both levels from a hue the
  palette doesn't use - `#39c5cf` (cyan) or `#ff7bce`/`#db61c2` (pink)
  are free in the GitHub-dark set. If the human insists on the split,
  keep orange for tasks and use cyan - never green - for runs.

Non-color carriers (WCAG 1.4.1, always present): the `unreviewed` text
pill (`.stage.unreviewed`) inside `.rlabels` on runs and beside status
on tasks; bolded run/step name (`tr.unreviewed .run-name b` or
font-weight on the step cell); `tr.unreviewed td:first-child {
box-shadow: inset 3px 0 <accent> }` mirroring `.selected`; `title=`
explainer on the pill + `aria-label` mention on rows. Optional:
`role="status"` on the pill for screen-reader pickup when it appears
during a live refresh (WCAG 4.1.3).

Where it renders: `.rlabels` line on run rows; the status cell (or
`task` step cell) on agent rows; a `review` kv row in `showDetail`;
buttons in `.actions` of both modals; parity items in
`ctxRunItems`/`ctxAgentItems`; optional `review` facet in `FACETS`
(~7463) if a filter is wanted - note ORDER/`byStatus` are for `status`
values, so a tag facet is the right slot, not a status chip.

### 3.5 Historical records + resume semantics (decisions)

- Legacy records: no `completion_seq` -> never flagged, no migration
  write (don't present them as freshly completed; don't fabricate acks
  either). Manual marks still work on them (tombstone shows indicator;
  "mark reviewed" records an ack with seq=null).
- Resume: `completion_seq` stays through resume (run-level seq is not
  popped - `ended_at` is; gating the indicator on `ended_at` present
  hides it mid-run anyway). Each re-end bumps seq -> old ack stops
  matching -> indicator returns for the NEW completion only.
- Same-completion re-delivery (repeat save_state, note edits, archive
  toggles): seq unchanged -> ack survives.
- Race safety: an ack records the seq it saw; a completion landing
  after it can never be masked by it (inequality, not timestamp
  comparison).
- `plan-proposal` rows: OUT of scope - they are preparation history,
  not run work; the request's own stage/stale chips already signal
  "look at this plan". Revisit only if the human asks.

## 4. Per-step implementation touchpoints

**review-workflow**: everything in §3 - flags plumbing + 2 endpoints +
`collect_dashboard` row/run fields (`completion_seq`, `review`,
`needs_review`) + `.stage.unreviewed`/`tr.unreviewed` CSS + modal
buttons + ctx items + `clear_run_flags` exemption + `_bump_completion`
call sites. Interplay check: `runStateChip` (~8894) stays about
resumability - the review pill is a sibling, never folded into it.

**task-outcome-summary**: reporting path in §1.2. Add optional
`SUMMARY:` (1-2 lines) to the worker contract in `PREAMBLE` (~98) and
step-render text; parse via a `tail_summary` sibling of `tail_status`
(whole-log, last match, ~300-500 char cap) -> `rec["outcome"]` at the
terminal update; surface as an "Outcome" block in `showDetail` (above
result/note) and prefer it in the table `note` cell; keep
`STATUS:`/`NEXT:` semantics untouched (NEXT stays the machine handoff).
Historical: `outcome` absent -> render an explicit "no outcome summary
recorded" muted line, never synthesize one from note/next. `resolve-*`
records work unchanged. Safe rendering: esc() like every field.

**task-table-navigation**: row-click dispatch at ~9576 (`tr.agen` ->
`showDetail`) - remove the bare-row branch; step name becomes a
`data-agent-detail` button (delegated handler exists ~9276); run cell
~9091 becomes `data-filter-run`-style button reusing the existing
filter dispatch; run-name button on the RUN row switches to filter per
the step text (currently opens run modal - the run modal then needs
another affordance: ctx menu "Open details" exists; a details button in
the row or keep name=modal and add explicit filter control - re-read
the step prompt at implementation time, it says run name applies the
filter). Selected-row highlight: `detailKey`/`runModalKey` matched in
`render()` -> `tr.open`-style class distinct from `.selected` (filter);
cleared by `closeDetail`, moved by `navStep`. Guard: agents rows
without run_dir (planner/pending) still open modals - highlight keyed
by `agentKey`, not run.

**modal-refresh-stability**: `render()` -> `showDetail(j,true)`
(~9132) rebuilds `#dbody` innerHTML on every sig-changing tick; the
scroll container is `#detail` (~7156). `modalScroll`/`applyModalScroll`
(~7724) serve Back-nav only; `modalSelActive` (~7631) already defers
rebuilds mid-selection; `navStep` intentionally sets scrollTop=0
(~7675). Likely fix: snapshot `#detail.scrollTop` before the rebuild
and restore after (keyed by `detailKey`; reset to 0 on deliberate
sibling nav - already the behavior), or surgical DOM patching of the
changing fields. Stale-async: `openSummary`'s `summaryView.key!==key`
guard (~7784) is the pattern to copy for any new async write.
Harness note: the node stub has no real scroll/layout - this fix wants
a real-browser check (see gaps).

**cumulative-elapsed-time**: §1.4. Suggested shape: keep `elapsed_s` as
"current interval" OR accumulate into it on re-dispatch - pick ONE
display contract: `rec["exec_s"]` cumulative + `started_at`/`ended_at`
staying first/last-touch, plus `rec["first_started_at"]` to preserve
original start. Every resume interval adds, never double counts
(intervals are disjoint by construction: one run_step process, one t0).
Run level: `state["exec_s"]` accumulated at driver end (`time.time() -
run_t0` at ~2710), restarted drivers add theirs; live ticking = stored
+ current interval in `elapsed()`. New runs-table column = colgroup
change ~8862 + header + cell + JS tick for live runs. Historical:
absent `exec_s` -> fall back to `ended_at - started_at` for steps
(honest, matches old display), `-` for runs - never invent.

**execution-context-labels**: wt chip on run rows - derive from
`_manifest_cached(st["manifest"])` step.worktree + proposed_steps `wt`
for request rows (never from `rec.branch` - that's incidental). Mixed
runs: chip text stays `wt`, `title=` lists e.g. "3 of 5 steps run in
worktrees". Emit a `wt`/`wt_mixed` field on run dicts in
`collect_dashboard` (manifest cache already exists ~6717). Watcher
warning: `_assert_unwatched` (~3753) message gains identity - collect
matching `f"{rundir}:{step}"` (cap ~3, "+N more"), fall back to
work_dir path when name missing. Same identity line belongs in
`_runs_watched`'s dry-run output (change bool -> info string or add a
parallel collector). The "is the warning still reachable" question:
yes - every request write + publish + attachment + archive/delete
paths it, and self-hosted runs (agent-conductor itself) trip it
regularly; it is NOT dead code.

**manual-merge-guard**: §1.6 gaps 1-4. UI: `runCtl` (~8053) expose
`mergeAllowed`/`mergeWhyNot`; button `disabled` + `title` in modal and
`dis:`+`title` in ctx items. Server: `_merge_run` adds
`driver_alive(st)` refusal + `_RESUME_PIDS` (exists) + re-validate
inside `_RepoMergeLock`; `resume_run` gains the `_MERGE_BUSY` (and
arguably `driver_alive`) refusal. Tests: tempdir repos like
MergeRunTests, fabricated driver_pid (a sleeper subprocess), stale
client POSTs.

**integration-verification**: suite + demo + node-harness additions;
verify chips don't hide failure (a `failed` unreviewed task shows red
dot + pill); STATUS/NEXT consumers (`tail_status`/`tail_next`, modal
`result`) unchanged; conductor merges unaffected; keyboard path
(`tabindex`/Enter on name buttons); legacy state.json fixtures.

## 5. Test coverage gaps (what exists vs what's missing)

Existing tooling:
- unittest + `tests/mock_devin.py` (loop-level, real subprocesses,
  tempdir git repos) - `RunTests`, `MergeRunTests`, `DeferredMergeTests`.
- HTTP endpoint tests: `ThreadingHTTPServer` on port 0 + `http.client`
  (ModalDetailNavTests pattern) - good for the review endpoints.
- Node `vm` harness against `_DASH_HTML`'s `<script>` with a stub
  `document` (`@unittest.skipUnless(shutil.which("node"))`) - the UI
  regression vehicle (DashboardJsFilterTests, ContextMenuTests,
  ModalDetailNavTests). Rendered HTML asserted by regex.
- `conductor.py demo` end-to-end vs mock.

Gaps to note for implementers:
- The node stub has NO real DOM/layout: `scrollTop` is a plain property
  - it can assert code SETS it, not that a browser preserves position.
  Modal-scroll work needs a committed Playwright probe (none exists;
  prior probes lived in scratch/ and were never committed) or accept
  harness-level assertions + manual verification - decide explicitly.
- No clock seam (§1.4) - deterministic elapsed tests need a
  `conductor._now()`/injected clock or `mock.patch('time.time')`;
  add the seam when implementing, don't patch around it ad-hoc.
- Wall-clock sentinel tests are a known flaky family
  (`_flag_soon` helpers, test_worktrees ~884 flake recorded) - write
  review-flag tests as direct file writes, never sleeps.
- `_assert_unwatched` message text is untested - the identity change
  is a cheap assert to add.
- `run_tests.py` ~460s at this commit vs the 600s verify cap inside
  `run_step` (~2391) and AGENTS.md's stale "~7s" claim - budget verify
  accordingly; the 600s cap has already claimed a parked branch once.
- No tests around `clear_run_flags` prefix exemptions for NEW durable
  flag types - add `review-` coverage when the flag lands.
