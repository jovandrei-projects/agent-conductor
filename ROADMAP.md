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
- [ ] Live `devin -p` smoke test (blocked on `devin auth login`)
- [ ] Live run on one real project's queue (cheap model)

## Phase 2 - verified live behavior

- [ ] Confirm `-p` runs a full agentic loop (not a single turn) and sessions appear in `devin list` / Desktop
- [ ] Confirm `--export` ATIF output; use it for richer step summaries if useful
- [ ] Cloud step on a `cloud_ok` project end-to-end (origin visibility caveat documented)
- [ ] First real overnight manifest on a real project

## Phase 3 - parallelism & polish

- [ ] `wave`/`parallel` groups: concurrent steps in separate git worktrees + merge step
- [ ] Planner step pattern validated (re-slices QUEUE.md mid-run)
- [ ] hub.db: per-step status rows (not just end-of-run), maybe a runs view in project-hub
- [ ] Transcript-extract integration: distilled handoff notes into run dir
- [ ] Desktop visibility of spawned sessions confirmed/documented

---

## Explicitly not doing

- No UI automation of Devin Desktop (clicking "new session" etc.) - the CLI is the interface.
- No orchestrator-in-LLM loop that stays alive all night (that is the bug being fixed).
- No auto-push. Ever. Cloud steps see what was already pushed; conductor warns.
- No `0.0.0.0` anything, no server - if a dashboard appears it binds loopback and takes the next free 87xx port in the root registry.
