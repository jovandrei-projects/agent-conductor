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

## Not yet filed as tasks

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
