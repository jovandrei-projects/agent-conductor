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
