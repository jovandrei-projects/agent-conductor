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
- [x] Live `devin -p` smoke test (ran 2026-10-13 ~09:00; real sessions spawned and do the work - but `devin -p` detaches, see AGENTS.md trap 5 and the Phase 2 finding below)
- [ ] Live run on one real project's queue (cheap model) - blocked until step completion tracks the session, not the subprocess

## Phase 2 - verified live behavior

- [x] Confirm `-p` runs a full agentic loop (not a single turn) and sessions appear in `devin list` / Desktop - confirmed 2026-10-13: the smoke readme session kept working ~5min and committed README.md after its subprocess exited; it shows in `devin list`. **Finding: `devin -p` exits after ~0.1-1.8s once the session is registered - the session is a detached managed object, the subprocess is not its lifetime.** Exit code, elapsed_s, stdout log and export are all empty/premature; step gating must poll session state instead (trap 5)
- [ ] Confirm `--export` ATIF output; use it for richer step summaries if useful - no export.json was written in the detached runs; needs a session-completion-aware fetch
- [ ] Cloud step on a `cloud_ok` project end-to-end (origin visibility caveat documented)
- [ ] First real overnight manifest on a real project

Smoke-run log: 2026-10-13 ~09:23 - `run_tests.py --quick` 20/20 green,
`conductor.py demo` 2/2 done (run `20261013-092326-demo`). No regressions;
the mock/unit layer is unaffected by the `-p` detach finding - the real
`devin -p` session-completion polling (trap 5) remains the blocker for a
live manifest.

## Phase 3 - polish

- [ ] Planner step pattern validated (re-slices QUEUE.md mid-run) - the pattern is documented in `examples/night.example.toml`, needs a live run
- [ ] Phone access to the dashboard: either a lan-bridge proxy port for :8773 (needs a mobile-apps bridge.defaults.json change + app redeploy) or accept that `tasks-ui` hub rows are enough
- [ ] `run.min_free_gb` scheduling gate: pause new agents when RAM is scarce (free_ram_gb is already recorded; the gate itself is unbuilt)
- [ ] Maybe a runs view in project-hub (reads `data/runs/*/state.json`)
- [ ] Transcript-extract integration: distilled handoff notes into run dir
- [ ] Desktop visibility of spawned sessions confirmed/documented

---

## Explicitly not doing

- No UI automation of Devin Desktop (clicking "new session" etc.) - the CLI is the interface.
- No orchestrator-in-LLM loop that stays alive all night (that is the bug being fixed).
- No auto-push. Ever. Cloud steps see what was already pushed; conductor warns.
- No `0.0.0.0` anything, no server - if a dashboard appears it binds loopback and takes the next free 87xx port in the root registry.
