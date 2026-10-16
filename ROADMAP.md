# agent-conductor - workplan

One phase at a time, in order. Mark items `[x]` as they land.

## Decisions already made

|| Question | Answer |
|---|---|
|| Orchestrator is an agent or a script? | **Script.** A conductor agent would itself burn context all night; a Python loop costs nothing between steps and survives crashes via `state.json`. |
|| How does state pass between sessions? | The project's own `.agent_tasks/` + `AGENTS.md` - the existing convention, not a new channel. Prompts point at files, not at embedded task text. |
|| Spawn primitive | `devin -p --prompt-file` subprocess per step (fresh session each). `devin --cloud -p` for cloud steps. `devin acp` JSON-RPC deferred - `-p` is enough for the phase model. |
|| Permission model | `bypass` per step + conductor-owned git checkpoints before and after. Revertibility is the safety boundary. |
|| Privacy | `cloud_ok` per project in `projects.toml`; false for wifi-network-monitor, chrome-bookmarks, money-plan. Enforced at resolve time. |
|| Manifest format | TOML (stdlib `tomllib`), not Markdown - QUEUE.md stays the task source, the manifest is the run skeleton. |
| First cloud_ok pair | `agent-conductor` + `apartment-3d` (human call, 2026-10-13). Caveat: agent-conductor has no git remote - a cloud step clones from origin, so it needs the repo pushed first. |
| Default model | `swe` (= SWE-2 Max) while it is free through 2026-10-16; after that `gpt-6-luna` at max thinking effort. Set per manifest via `[defaults] model`; thinking effort via `effort` which fuses as a variant suffix (`gpt-6-luna` + `min` -> `--model gpt-6-luna-min`). `devin models list` (2026-10-14): swe-2 variants are **high/medium/max only** - no min/low; all free. |
| Spawned-session hygiene | `conductor.py sessions` whitelists on ids recorded in state.json - human sessions can't be touched. `--rm` deletes sessions from runs ended >48h ago; the same cleanup is automated in `sweep` + `serve` startup (user-approved 2026-10-13); transcripts live in the run dir anyway. |

## Phase 1 - sequential core (this session)

- [x] `conductor.py`: manifest load/validate, per-step `devin -p` spawn, git checkpoints, verify gate, run dir + state.json + events.jsonl, resume, status, demo
- [x] `projects.toml` registry with `cloud_ok` privacy gate
- [x] `tests/mock_devin.py` + `tests/test_conductor.py` - loop proven without auth
- [x] Parallel `wave` groups in git worktrees + merge-back (landed early; `conductor/<run>/<step>` branches)
- [x] hub.db: one task row per run, live-updated per step (`--no-hub` opts out)
- [x] Cloud-session ref capture: `app.devin.ai/sessions/...` parsed from stdout (cloud sessions don't show in `devin list`)
- [x] `run.max_parallel` resource cap (default ~1 per 4 cores, max 4; wave members queue past the cap) + `--max-parallel` override; effective cap + free RAM recorded per run
- [x] Agent registry: each step record in `state.json` carries pid, work_dir, branch, started_at/ended_at/elapsed_s, session id, output paths - the durable session-work record
- [x] `conductor.py serve` dashboard on `127.0.0.1:8773`: every agent + run, live elapsed, status colors, `/api/state` JSON
- [x] Live `devin -p` smoke test - first authed runs died in 0.1-3s (rc 0, no STATUS); root-caused 2026-10-13: conductor's mid-spawn `save_state` wrote into the worker's watched cwd and the CLI's user-edit awareness killed the session (trap 5). Fixed + verified: `20261013-093553-smoke` ran readme 56.6s / sanity 96.6s, both `STATUS: done`, README.md + commits landed
- [x] Step-completion contract hardened: rc 0 without `STATUS:` is `failed`, never `done`; regression test locks the no-workspace-writes-while-alive invariant
- [ ] Live run on one real project's queue (cheap model) - unblocked; next up

## Phase 2 - verified live behavior

- [x] Confirm `-p` runs a full agentic loop (not a single turn) and sessions appear in `devin list` / Desktop - confirmed 2026-10-13 (corrected finding): `-p` DOES track the session to completion - the earlier "subprocess exits while the session runs on" observation was actually the CLI ending the session itself because conductor wrote `state.json` into the watched workspace mid-init (trap 5). Sessions now run to real completion and appear in `devin list`/Desktop sidebar
- [x] Confirm `--export` ATIF output - written normally now that sessions survive (see `data/runs/*/*.export.json`)
- [ ] Cloud step on a `cloud_ok` project end-to-end (origin visibility caveat documented)
- [ ] First real overnight manifest on a real project
- [x] Worktree/merge matrix proven: `tests/test_worktrees.py` - 3-member waves (clean + 3-way conflict), same-file different-region clean merge, mixed project+worktree waves, wave-2 branching off wave-1 merges, member-fail/timeout/no-commit edge cases, resume-after-conflict, stale-merge cleanup
- [x] `merge_resolver`: on merge conflict, a `resolve-<step>` session finishes the merge in the main checkout (mid-merge, opt-in [run]/per-step). Guards: refuses dirty checkout, marker+unmerged+ancestor checks, conductor commits only verified resolutions. Stale `conductor/*` merges aborted on resume.
- [x] `--resume` finalizes unmerged branches (done AND failed/blocked/partial) instead of re-running a conflicted branch pointlessly
- [x] Per-step token totals in `state.json` + dashboard column + per-run sums (from ATIF `final_metrics`: prompt/completion/cached + LLM step count)

Smoke-run log: 2026-10-13 ~09:23 - `run_tests.py --quick` 20/20 green,
`conductor.py demo` 2/2 done (run `20261013-092326-demo`). No regressions;
the mock/unit layer is unaffected by the `-p` detach finding - the real
`devin -p` session-completion polling (trap 5) remains the blocker for a
live manifest.

2026-10-13 ~09:27 - `run_tests.py --quick` 20/20 green, `conductor.py demo`
2/2 done (run `20261013-092727-demo`). No regressions; trap 5 blocker
unchanged.

2026-10-13 ~09:29 - `run_tests.py --quick` 20/20 green, `conductor.py demo`
2/2 done (run `20261013-092923-demo`). No regressions; trap 5 blocker
unchanged.

2026-10-13 ~09:31 - `run_tests.py --quick` 20/20 green, `conductor.py demo`
2/2 done (run `20261013-093107-demo`). No regressions; trap 5 blocker
unchanged.

2026-10-13 ~09:38 - `run_tests.py --quick` 22/22 green, `conductor.py demo`
2/2 done (run `20261013-093750-demo`). No regressions; trap 5 blocker
unchanged.

2026-10-13 ~09:55 - trap 5 resolved (was misdiagnosed as "detached
subprocess"; real cause: mid-spawn `state.json` write into the watched
workspace -> user-edit awareness ends the session). Live smoke green:
readme 56.6s + sanity 96.6s, real commits; 22/22 tests; bootstrap task done.

2026-10-13 ~18:30 - pre-live hygiene pass. Found a worse bug than the stale
dashboard rows that prompted it: `pid_alive` used `os.kill(pid, 0)`, which
is TerminateProcess on Windows - every dashboard refresh KILLED the workers
it probed. Fixed (OpenProcess + GetExitCodeProcess + creation-time pid-reuse
guard). Stale records now sweep to `lost` (10 phantom rows cleared; `lost`
re-runs on --resume); `sessions` command lists/cleans spawned devin
sessions; cloud_ok marked for agent-conductor + apartment-3d; 34/34 tests
green.

2026-10-13 ~23:30 - dashboard + demo upgrade. Agents now grouped into
per-project tables with a `task` column (prompt text, with a fallback that
extracts the `## Task` body of old records' prompt files) and an `effort`
column (`effort` manifest field -> model-variant suffix). Demo sandbox
reverted per user call: `demo` writes to `data/runs` like every project.
Two fixture projects (demo1 book catalog, demo2 notes) under `data/` +
`examples/make_demo_projects.py` + three real manifests (demo1-only,
demo2-only, mixed) pending a live run - blocked on `devin auth login`
(credentials lapsed again). 39/39 tests green.

2026-10-14 ~03:40 - worktree merge matrix (branch `dev/merge-tests` in
worktree `agent-conductor-wt`, parallel to the apartment-3d live run).
`tests/test_worktrees.py` +25 tests covering the wave/worktree/conflict
combination space; `merge_resolver` implemented + proven end-to-end
against real git conflicts via mock `resolve-merge`. Token metrics now
flow ATIF export -> state.json -> dashboard (per-agent `prompt/out` cell +
per-run totals). 75/75 green.

2026-10-14 ~04:00 - LIVE proof: `lab-merge-stress` ran all 15 steps +
6 resolver sessions alongside the apartment run, 21/21 done, zero
failures. 9 worktree branches merged; 8 real conflicts (shared
`.agent_tasks/QUEUE.md`) all resolved semantically by delegated resolver
sessions. Tokens: 3.03M prompt / 82% cached / 340 LLM steps for a trivial
15-file workload (~144k prompt per fresh session - session spin-up cost
is the dominant term, worth a keep-warm/prompt-cache look later).

## Phase 3 - polish

- [x] Dashboard request workflow (2026-10-14): raw drafts in `data/requests/`, planning-only Devin CLI conversion into timestamped reviewable TOML, description + planned agent rows (waiting/skipped until dispatched after launch), edit/regenerate/discard, explicit Start run with state handoff. No auto-implementation on save or generate; future default switches from swe-2/max to gpt-6-luna/max after 10/16.
- [x] Dashboard run-row click filters runs + agents with persistent highlight and removable run chip; status/tag facets include wt/merged/unmerged; tokens header corrected and modal help with a worked cost formula.
- [x] Windows dashboard console flashes: periodic auth/session probes spawned visible cmd windows from `pythonw.exe`. All background subprocesses are now headless; deliberate login console remains visible (trap 14). Mock suite 98/98 and demo 2/2 green on 2026-10-14; live planner was not dispatched during verification.
- [x] Dashboard cost + filter polish (2026-10-15): dynamic self-serve pricing parsed from `docs.devin.ai/desktop/chat/models.md` (`modelCostData`, 296 models, disk-cached in `data/pricing.json`, 24h refresh) -> per-agent cost column + per-run `est` rollup (planner included, unpriced steps counted separately, real $0 renders as $0). Status chips are now inclusive pick-one-or-more filters that scope BOTH the agents tables and the runs table; run rows show `N steps`, a per-status breakdown, token split, cost, and pulse while any member step runs. Session column no longer flashes `deleted by user` during `devin list` propagation lag (SESSION_GONE_GRACE_S). Manual `Merge pending branches` button (`POST /api/runs/<dir>/merge` -> `merge_run`) merges parked/unmerged worktree branches, spawns the resolver when the manifest opts in, and refuses live runs. Request form gained `worktrees` + `merge resolver` checkboxes: they reach the planner prompt, are guaranteed into the TOML at publish (worktree never applied to `cloud=true` steps; wave-required members always keep it), and Review's `Apply` patches the saved TOML deterministically. Mock suite 122/122 green.
- [x] Lifecycle controls + abrupt-death recovery (2026-10-15): `Stop run now` (per-step flag + `taskkill /T` on live pids), `Drain & stop` (in-flight finishes, no new launches), `Stop this agent` - all appear only while work is live. Stop/drain sentinels are `%TEMP%/agent-conductor-flags` files polled by `wait_worker` and the wave loop (never in run_dir - trap 5); killed steps record `stopped`, unlaunched `skipped`, `state.aborted` records `stopped|drained`. Resume clears the sentinels and prepends INTERRUPTED_NOTE for running/lost/timed_out/stopped priors so the fresh session recovers from the repo/checkpoints, not a transcript. `_launch_process` gives dashboard-spawned runs their own process group (trap 15): stopping `serve` no longer Ctrl+C-kills work; only the stop endpoints do. `model = "adaptive"` is selectable for planner + steps (no effort suffix); the export's `agent.model_name` lands as `model_actual`, shown as `adaptive → <model>` and priced at the resolved model's rate. Agent modal shows the full task (`task_full` re-read from the prompt file); the running row's result cell is the log TAIL (the CLI writes one long \r-redrawn line - naive tails read the head).
- [x] Merge observability + no-STATUS distinction (2026-10-16): merge outcomes now land in `events.jsonl` - `step_merged`/`step_merge_conflict` per step (`how` = wave/resume/manual, `was` = replaced status, `resolver` when a resolve-* session saved it), `step_merge_kept` for failed-step branches parked unmerged, `worktree_removed` for no-product debris; `merged_at` stamps when the branch landed (agent modal `merged` row + `merged` chip tooltip). Run modal exposes `resume.launch.log` so a resume driver's output is discoverable. A step that exited 0 without the `STATUS:` contract line renders `failed ·no STATUS` - the tooltip splits real-work-ran (tokens present; Resume finalizes the merge) from a true no-op. Motivation: a real `rc0`-no-STATUS step merged silently on resume and left no per-step trace.
- [x] Worker-authored + LLM run summaries (2026-10-16): the step summary's `## Report` section now leads with the worker's own closing message - the trailing agent prose in the ATIF export with `STATUS:`/`NEXT:` stripped (`final_report`), so the report is what the session actually said rather than only what the conductor observed. `run.summary.md` is the run-level counterpart: `[run] summarize = true` (or a `"<model>"` string) spawns a detached `conductor.py summarize` session after `run_end` that synthesizes one executive report from run stats + every step's closing report; also on demand via `POST /api/runs/<dir>/summarize` / the run modal's Generate LLM summary + Run summary buttons (`GET /api/runs/<dir>/summary` serves the LLM file, else the deterministic per-step roll-up). The summarizer cwd is a tempdir, it never alters run/step status, a `.summarizing` marker powers the dashboard's Generating... state, and `run_summarized`/`run_summary_failed` events + `state.run_summary` record the session. Request form gained a `summarize` checkbox (guaranteed into `run.summarize` at publish like the other flags).
- [ ] Planner step pattern validated (re-slices QUEUE.md mid-run) - the pattern is documented in `examples/night.example.toml`, needs a live run
- [ ] Phone access to the dashboard: either a lan-bridge proxy port for :8773 (needs a mobile-apps bridge.defaults.json change + app redeploy) or accept that `tasks-ui` hub rows are enough
- [ ] `run.min_free_gb` scheduling gate: pause new agents when RAM is scarce (free_ram_gb is already recorded; the gate itself is unbuilt)
- [ ] Maybe a runs view in project-hub (reads `data/runs/*/state.json`)
- [ ] Transcript-extract integration: distilled handoff notes into run dir
- [ ] Session-cleanup surface on :8773 (the `sessions` command's list/rm as a dashboard panel)
- [ ] Desktop visibility of spawned sessions confirmed/documented

---

## Explicitly not doing

- No UI automation of Devin Desktop (clicking "new session" etc.) - the CLI is the interface.
- No orchestrator-in-LLM loop that stays alive all night (that is the bug being fixed).
- No auto-push. Ever. Cloud steps see what was already pushed; conductor warns.
- No `0.0.0.0` anything, no server - if a dashboard appears it binds loopback and takes the next free 87xx port in the root registry.
