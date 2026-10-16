# Session cost

Appended at the end of every session, by the agent, before it stops. One row
per session. The purpose is to notice if the task workspace is costing more
context than it saves - if new input tokens climb while the work per session
does not, say so rather than just recording it.

Cached input is not free but is roughly an order of magnitude cheaper than
new input, so the column to watch is **new input**.

| Project / Task | Agent Messages | New Input Tokens | Cached Input Tokens | Output Tokens |
|---|---|---|---|---|
| agent-conductor / bootstrap | n/a | n/a | n/a | n/a |
| agent-conductor / bootstrap (overnight + resource cap, registry, :8773 dashboard) | n/a | n/a | n/a | n/a |
| agent-conductor / bootstrap (auth diagnosis, abort-path fix) | n/a | n/a | n/a | n/a |
| agent-conductor / bootstrap (smoke readme step; found -p detaches) | n/a | n/a | n/a | n/a |
| agent-conductor / bootstrap (verified README task already landed; no-op) | n/a | n/a | n/a | n/a |
| agent-conductor / bootstrap (nightly smoke: tests+demo green, docs updated) | n/a | n/a | n/a | n/a |
| agent-conductor / bootstrap (README task again; re-verified vs argparse, no-op) | n/a | n/a | n/a | n/a |
| agent-conductor / bootstrap (nightly smoke: tests+demo green, docs updated) | n/a | n/a | n/a | n/a |
| agent-conductor / bootstrap (README task 3rd dispatch; still a no-op - trap-5 respawn) | n/a | n/a | n/a | n/a |
| agent-conductor / bootstrap (nightly smoke: tests+demo green, docs updated) | n/a | n/a | n/a | n/a |
| agent-conductor / bootstrap (README task 4th dispatch; still a no-op - trap-5 respawn) | n/a | n/a | n/a | n/a |
| agent-conductor / bootstrap (nightly smoke: tests+demo green, docs updated) | n/a | n/a | n/a | n/a |
| agent-conductor / bootstrap (README task 5th dispatch; still a no-op - trap-5 respawn) | n/a | n/a | n/a | n/a |
| agent-conductor / bootstrap (nightly smoke: tests+demo green, docs updated) | n/a | n/a | n/a | n/a |
| agent-conductor / bootstrap (trap-5 root cause: mid-spawn state.json write killed workers; fixed, live smoke green, bootstrap done) | n/a | n/a | n/a | n/a |
| agent-conductor / hygiene (pid_alive was killing workers via os.kill; sweep/sessions commands; cloud marks) | n/a | n/a | n/a | n/a |

If the figures are not available at the end of a session, write the row with
the counts left as `n/a` rather than skipping it, so the session is still
accounted for.
| agent-conductor / hygiene (demo un-sandboxed; per-project dashboard + task/effort cols; auto 48h session sweep; demo1/demo2 fixtures+manifests pending auth) | n/a | n/a | n/a | n/a |
| agent-conductor / favicon (new baton-over-orchestra glyph; rode in sibling session's commit; 48/48 green) | n/a | n/a | n/a | n/a |
| agent-conductor / favicon2 (baton-over-musicians glyph, headless-Edge previewed; 48/48 green) | n/a | n/a | n/a | n/a |
| agent-conductor / favicon2 3rd dispatch (no-op: glyph already merged; re-verified 50/50 green) | n/a | n/a | n/a | n/a |
| agent-conductor / wt-matrix (dev/merge-tests: merge_resolver + resume-finalize + token metrics + 25-test worktree matrix; live lab run resolving real conflicts; 75/75 green) | n/a | n/a | n/a | n/a |
| agent-conductor / dashboard requests (draft/review/start, filters, token help, headless probes; 98/98 mock tests and demo green) | n/a | n/a | n/a | n/a |
| agent-conductor / run-control-taxonomy (audit -> graceful-pause merge, stopped no longer trips on_failure, durable skip sentinel, manifest pending rows; 164/164 + demo green) | n/a | n/a | n/a | n/a |
| agent-conductor / task-prompt-editing (pre-dispatch Edit prompt + resume notes sentinels, full-text modal task field; 172/172 + demo green) | n/a | n/a | n/a | n/a |
| agent-conductor / steps-counters-clarity (token counters self-label: in/out/cached/turns + planner:/steps: prefixes; 171/172 - unrelated stop-flag flake, demo green) | n/a | n/a | n/a | n/a |
| agent-conductor / run-lifecycle-cleanup (auto-archive stamp + delete-run endpoint + archived view chip; 182/182 + demo green) | n/a | n/a | n/a | n/a |
| agent-conductor / planner-questions (opt-in 'ask' box: planner emits BEGIN_QUESTIONS, request parks at 'questions' stage, one-question-per-modal answers feed a refine pass, 3-round cap; 193/193 + demo green) | n/a | n/a | n/a | n/a |
| agent-conductor / run-link-affordance (run names always link-styled; focus ring insets so td clipping can't cut it; 199/199 green) | n/a | n/a | n/a | n/a |
| agent-conductor / follow-up-plan (run review + drafted ux-controls-followup.toml; 2 parked branches + 600s verify-cap + bookkeeping-conflict findings) | n/a | n/a | n/a | n/a |
| agent-conductor / image-attachments (request-form drop/paste image upload to data/attachments + thumbnail strip + rec.attachments -> planner IMAGE ATTACHMENTS block; 199/199 + demo green) | n/a | n/a | n/a | n/a |
| agent-conductor / task-modal-navigation (sticky .dnav prev/next bar walking pipeline seq order + arrow keys; agentKey->run_key; 200/200 + demo green) | n/a | n/a | n/a | n/a |
| agent-conductor / resolve-task-modal-navigation (merge resolution: both-append conflicts on QUEUE.md + efficiency_stats.md; merge e8c79e9) | n/a | n/a | n/a | n/a |
| agent-conductor / stop-proposal-preparation (stopgen sentinel + _GEN_PROCS kill + gen_id attempt scoping, Stop proposal on 3 modals; 216/216 + demo + real-proc smoke green) | n/a | n/a | n/a | n/a |
| agent-conductor / compact-live-output (proposal run rows get a .genline last-output line under compact density, .fresh flash via prevTail incl. the full-view .sub; 211/211 green) | n/a | n/a | n/a | n/a |
| agent-conductor / follow-up-plan (proposal-ui-fixes run review: 1/2 steps landed, compact-live-output parked on flaky-verify false-fail; drafted proposal-ui-fixes-followup.toml - merge parked branch + harden _flag_soon family + live smoke) | n/a | n/a | n/a | n/a |
| agent-conductor / resolve-compact-live-output (merge resolution: both-append conflicts on QUEUE.md + efficiency_stats.md; merge 7e5f3cf) | n/a | n/a | n/a | n/a |
| agent-conductor / isolate-proposal-planning (planner output spooled outside watched cwds; publish-only waits on merge/watch; spool drain survives restart; 215/215 + demo green) | n/a | n/a | n/a | n/a |
| agent-conductor / resolve-isolate-proposal-planning (semantic merge: gen_id attempt scoping x spool overlay + gen_id ownership guards in publish/flush/drain; merge c2a8c6c + fix 400124b; 224/224 green) | n/a | n/a | n/a | n/a |
| agent-conductor / retain-proposal-session (plan_sessions + plan_gen_id per attempt; step-0 history rows on ready+launched plans; 221/221 + demo green) | n/a | n/a | n/a | n/a |
| agent-conductor / fix-compact-labels (.rlabels block keeps run-row chips visible in compact; .toksub unhides planner:/steps:/est labels; stage pills ellipsize; render sig includes density; Playwright screenshots; 225 tests - 2 pre-existing StopProposalTests fails stash-verified at base) | n/a | n/a | n/a | n/a |
| agent-conductor / verify-proposal-flow (merged retain+compact parked branches; fixed same-second updated_at publish race via mtime_ns+stopped_at, stale-overlay masking via overlay delete, drain plan_sessions, compact chip wrap; +3 integration tests; 232/232 + demo + live Playwright probe green) | n/a | n/a | n/a | n/a |
| agent-conductor / follow-up-plan (proposal-flow-fixes run review: all 3 requests delivered on master; residuals = half-navigable retained history, transitively-merged steps still marked failed/unmerged, no live exercise; drafted + plan-validated proposal-flow-fixes-followup.toml) | n/a | n/a | n/a | n/a |
| agent-conductor / resolve-follow-up-plan (merge unblocked: untracked byte-identical copy of the branch's followup manifest blocked merge start - no real conflict; FF to 65cae4a) | n/a | n/a | n/a | n/a |
| agent-conductor / deferred-merge-engine (merge=deferred: retain verified worktree work as pending merges via per-run/per-project integration baseline; explicit land is staged+verified+atomic onto recorded target, serialized by per-repo merge lock; resolver honored in-stage; 250/250 tests + demo green) | n/a | n/a | n/a | n/a |
| agent-conductor / summary-purpose-parsing (head-of-body conductor-purpose marker now parses to `purpose` field + label; task/summary/modal render real text; marker still ships in prompt; 262/262 + demo green) | n/a | n/a | n/a | n/a |
| agent-conductor / modal-detail-navigation (summary opens in-app via ?frag=1 fragment + modalHist Back stack for task->request->run chains; scroll restore, missing-item notices, no new windows; 264/264 + demo green) | n/a | n/a | n/a | n/a |
| agent-conductor / full-trajectory-view (arc entries expand to full ATIF text - lazy /trajectory endpoints + trajOpen/trajDetail caches; honest 404s for step-less/mock exports; per-entry copy verbatim; 272/272 + demo green) | n/a | n/a | n/a | n/a |
| agent-conductor / row-context-menus (right-click/Shift+F10 #ctxmenu on run+task rows reusing modal data-* hooks via shared runCtl; native menu kept for selections/editable/Shift+rc; Esc/scroll/outside-swallow dismissal, focus restore; 276/276 + demo + 25-check Playwright green) | n/a | n/a | n/a | n/a |
| agent-conductor / proposal-action-state (semantic pending: gen_inputs publish baseline + TOML-encoded option diff replace sticky stale; live revert-clears note, Apply neutralized when nothing/gen pending, launch gate, Regenerate secondary+confirm naming unsaved TOML edits; followup-step worktree-apply gap fixed; +18 tests, 294/294 + demo green) | n/a | n/a | n/a | n/a |
| agent-conductor / request-image-picker (paperclip btn + hidden input[type=file] feed the existing attachFiles upload path; cancel = no-op, value reset for re-pick, name dedupe same as drop/paste, aria labels + live strip; 295/295 + demo green) | n/a | n/a | n/a | n/a |
| agent-conductor / model-choice-lists (planner+step model fields: datalist -> combobox popup, recs-over-scrollable-all, flat activedescendant over dup entries; 297/297 + demo + 35-check Playwright probe green) | n/a | n/a | n/a | n/a |
| agent-conductor / task-outcome-summary (OUTCOME: third contract line - worker-reported result summary w/ outcome+outcomes history on the rec; modal outcome/earlier rows + honest 'not reported', result column, summary-md, cmd_status; 307/307 + demo green) | n/a | n/a | n/a | n/a |
| agent-conductor / resolve-task-outcome-summary (merge resolution: both-append on efficiency_stats.md + two-class collision in test_conductor.py, ModelComboboxTests + OutcomeSummaryTests each kept whole with own node harness; merge d57e9c2; 320/320 green) | n/a | n/a | n/a | n/a |
| agent-conductor / interrupt-with-instructions (intrq JSON sentinel -> rnote fold -> same-step respawn under INTERRUPT_NOTE; no-attempt-spent, late/stop-wins bank, restart exactly-once, intrq-capability stamp; composer+ctx UI w/ keyed drafts+attach reuse+progress+tombstone; +6 tests, 303/303 + demo green) | n/a | n/a | n/a | n/a |
| agent-conductor / resolve-interrupt-with-instructions (merge resolution: both-append on efficiency_stats.md + state-dict collision in cmd_run's new-run record - kept branch's intrq stamp AND HEAD's summarize_model; merge 821dfa4; 326/326 green) | n/a | n/a | n/a | n/a |
| agent-conductor / task-table-navigation (agents-table click targets: step-name data-agent-detail button opens modal, run-name data-filter-run applies run filter via run_key; tr.agen row click-catch-all removed; .dsel row mark keyed by agentKey/runKey via selKeys/applySel on all modal open/close+render; TaskTableNavTests x2 + edit-form test repointed; 322/322 + demo green) | n/a | n/a | n/a | n/a |
| agent-conductor / resolve-task-table-navigation (merge resolution: both-append on efficiency_stats.md + step-cell collision in conductor.py - kept HEAD's intr_pending/interrupts status spans AND the branch's step-name data-agent-detail button; merge 12fbe2b; 328/328 green) | n/a | n/a | n/a | n/a |
| agent-conductor / recoverable-trash (delete-run + request-discard now move the whole bundle to data/trash/<ts>-<rand> w/ meta+payload - 48h retention, Undo bar + Trash view w/ restore + confirmed empty, collision-renaming restores, bounded expired-only sweep, legacy tombstones; +20 tests, 346/346 + demo green) | n/a | n/a | n/a | n/a |
| agent-conductor / resolve-recoverable-trash (merge resolution: both-append on efficiency_stats.md only - kept HEAD's task-table-navigation + resolve-task-table-navigation rows AND the branch's recoverable-trash row; merge 8cd2552; 348/348 green) | n/a | n/a | n/a | n/a |
| agent-conductor / modal-refresh-stability (task-modal scroll stability: in-place sameView repaint + landmark scroll anchor + dedupe, focus/caret/input carry, deliberate-switch top reset, /api/state seq guard; measured root cause via live Chromium probes - RESET pre-fix, STABLE post-fix; +3 ModalScrollStabilityTests, 331/331 + demo green) | n/a | n/a | n/a | n/a |
| agent-conductor / resolve-modal-refresh-stability (merge resolution: both-append on QUEUE.md + efficiency_stats.md + refresh()/tick() collision in conductor.py - kept branch's stateReqSeq guard but routed it through HEAD's stateUrl() so showTrash's trash=1 param survives; merge 8cb3988; 351/351 green) | n/a | n/a | n/a | n/a |
| agent-conductor / followup-run-discovery (followup output -> persisted Ready-to-run request: followups.index sha-idempotent registration + classification (ready/empty/error/missing/absent), run_end+serve+sweep+60s-dashboard hooks, owner-dir scoping (no cross-tree imports), Discard veto + explicit import override, fold-in-place + human-edit-beats-rewrite, run-row followup chips; +15 tests, suite green) | n/a | n/a | n/a | n/a |
| agent-conductor / resolve-followup-run-discovery (merge resolution: both-append on efficiency_stats.md only - kept HEAD's modal-refresh-stability + resolve-modal-refresh-stability rows AND the branch's followup-run-discovery row; merge 9794ec5; 366/366 green) | n/a | n/a | n/a | n/a |
| agent-conductor / emulator-resource-ownership (evidence: workers shared adb serials ad hoc + emu kill'd a shared emulator; implemented machine-wide serial leases - explicit spec, cross-run ownership, uncapped queue on busy device, bounded block on absent, AVD boot on free port where tooling allows, ownership-scoped cleanup; ANDROID_SERIAL env to worker+verify; +14 DeviceOwnershipTests, 380/380 + demo green) | n/a | n/a | n/a | n/a |
| agent-conductor / execution-context-labels (run-row 'wt' chip from configured isolation - _run_isolation manifest->proposed_steps->record-wt fallback, 'wt M/N' for mixed; trap-5 watcher 409 now names run:step+slug+session via _watchers_of/_watcher_identity, same identity in sweep/archive/trash dry-runs + launch overlap + deferred-merge refusal; +13 tests) | n/a | n/a | n/a | n/a |
