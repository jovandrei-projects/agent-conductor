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
| 5 | Dashboard request drafts + proposal review/start, run filters/token help, silent Windows probes | done | — | 98/98 mock tests + demo green; live planner not dispatched |
| 6 | Task prompt editing: pre-dispatch Edit prompt, resume notes for ran steps, full prompt text in modal | done | task-prompt-editing/ | 172/172 mock tests + demo green; prompt-/rnote- sentinels survive resume |
| 7 | Planner 'ask me questions' checkbox: questionnaire modal before manifest, answers feed a refine pass | done | planner-questions/ | 193/193 mock tests + demo green; live planner not dispatched |
| 8 | Stop proposal during preparation: stopgen sentinel + _GEN_PROCS kill + per-attempt gen_id scoping, restores draft/ready | done | stop-proposal-preparation/ | 216/216 mock tests + demo + real-proc smoke green |

## Not yet filed as tasks

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
  steps are deliberately not duplicated here.
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
  name themselves - `fmtT` renders `in X · out Y · cached Z · N turns`
  (cached always shown; turns = LLM steps, not manifest steps) instead
  of `X/Y+Zc ·Nst`; run rows read `planner:`/`steps:`; `steps` column
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
