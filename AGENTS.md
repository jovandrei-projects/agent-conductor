# Working notes for agents

Read this first, then `.agent_tasks/QUEUE.md` for what to pick up next, then
`ROADMAP.md` for where the work stands.

## What this is

`conductor.py` runs a TOML manifest of steps as a sequence of **fresh Devin
CLI sessions** - one `devin -p` subprocess per step, in the target project's
directory. It automates the loop a human otherwise drives by hand (new
session -> paste prompt -> wait -> repeat), so multi-phase work can run
overnight without one bloated context window or per-message cost growth.

State handoff between sessions is deliberately *not* a conversation: each
worker reads the project's `AGENTS.md` + `.agent_tasks/` and must leave them
current enough for the next session. The conductor only adds git checkpoints,
a run log, verify gates, and a `STATUS:` line contract.

`ROADMAP.md` owns the phase list and its status. `.agent_tasks/QUEUE.md` owns
which task is next, and each `.agent_tasks/<task>/STATE.md` owns the steps of
work in flight. This file owns the purpose, ground rules and traps. Do not
restate a phase's status here.

## Ground rules

The failure mode is an unattended agent doing damage overnight, so:

1. **Every step starts from a committed checkpoint.** Conductor commits the
   project before *and* after each step (`git add -A && commit`). Damage is
   always one `git reset` away. Steps in non-repo dirs fail at validation.
2. **Workers run `bypass` locally.** There is no human to answer prompts;
   bypass is safe *only* because of rule 1. `smart`/`normal` are available
   per step but will stall overnight.
3. **`cloud_ok=false` projects never go to Devin Cloud.** `projects.toml` is
   the gate; `wifi-network-monitor`, `chrome-bookmarks`, `money-plan` stay
   false. A `cloud=true` step on them fails validation, not just warns.
   Cloud workers see *origin*, not local state - unpushed work is invisible
   to them.
4. **A stalled step dies at `timeout_min`.** No hanging till morning.
5. **Run state lives in `data/runs/<ts>-<name>/`** (gitignored): `state.json`
   for resume, `events.jsonl`, per-step `*.stdout.log`, `*.export.json`,
   `prompts/*.md`. Never write run artifacts into the target project.
6. **Verify gates run via `cmd.exe`** (`shell=True`) - manifest `verify`
   strings are cmd syntax, not bash.

## Running things

```
python conductor.py plan <manifest.toml>    validate + show resolved steps
python conductor.py run  <manifest.toml>    execute -> data/runs/<ts>-<name>/
python conductor.py run  <manifest> --resume [dir]   skip done steps
python conductor.py status [run_dir]        step table of latest run
python conductor.py serve [--port 8773]     live dashboard of all agents
python conductor.py demo                    end-to-end vs tests/mock_devin.py
python run_tests.py                         unittest suite (mock-only, no auth)
```

Run flags: `--devin "<cmd>"` worker binary override, `--no-hub` skip the
hub.db row, `--no-keep-awake` allow system sleep (runs request
`SetThreadExecutionState` by default so overnight work survives),
`--max-parallel N` concurrency cap (overrides `run.max_parallel`).

Concurrency: `run.max_parallel` caps how many agents run at once across the
whole run; wave members beyond the cap wait in a queue (marked `queued` in
state). Default is conservative: ~1 agent per 4 logical cores, max 4 -
each agent is a full Devin session with tool subprocesses, not a thread.
The run records the effective cap + free RAM in `state.json`.

Tracking: `state.json` is the agent registry (status, pid, started/ended,
elapsed, session id, work dir, paths to prompt/stdout/export). `serve`
renders it as a live page at `http://127.0.0.1:8773` (loopback only,
auto-refreshes, shows elapsed time ticking for running agents). The same
run row is live-posted to `hub.db`, so `http://127.0.0.1:8762/tasks-ui`
shows coarse progress on the phone without any new wiring.

Parallel: adjacent `[[step]]` entries sharing `wave = N` run concurrently,
bounded by the cap. Steps on the *same* project in a wave need
`worktree = true` - each runs in `data/worktrees/<run>/<step>` on branch
`conductor/<run>/<step>`, merged back in manifest order after the wave; a
merge conflict fails the step but keeps the worktree for manual merge.

`examples/night.example.toml` is the reference manifest. `projects.toml`
registers slug -> path + `cloud_ok`. `--devin "..."` or `CONDUCTOR_DEVIN`
overrides the spawned binary (shlex-split; use forward slashes or quotes -
posix shlex eats unquoted backslashes).

Requires `devin` on PATH and `devin auth login` to have been run once
(`devin auth status` is checked at run start; conductor aborts cleanly if
not logged in).

## Traps

Append as they are hit.

1. **`devin -p`'s prompt arg is optional and greedy-adjacent.** A positional
   after `-p` is parsed as a workspace PATH, not a prompt, and errors out.
   Conductor never passes an inline prompt - always `--prompt-file`.
2. **Mock sessions register in `.mock_sessions.json`** inside the target
   project - fixture projects must gitignore it or conductor's `add -A`
   will commit it.
3. **Desktop auth != CLI auth.** Logging in inside Devin Desktop feeds the
   app's host credentials to `devin acp` over the protocol - it never writes
   `%APPDATA%\devin\credentials.toml`, which is the ONLY file the standalone
   CLI (`devin -p`, `auth status`) reads. Spawning workers therefore needs a
   real terminal `devin auth login` (browser flow or
   `--force-manual-token-flow` + pasted code from app.devin.ai). The login
   TUI reads the console directly - piped stdin can't drive it. Env vars
   `WINDSURF_API_KEY`/`DEVIN_API_KEY` are NOT honored.
4. **`auth login` trusts the file, `auth status` trusts the server.** A
   stale `credentials.toml` makes login short-circuit with "You are already
   logged in" while `auth status` still says "Not logged in" (the stored
   token is rejected). Fix: `devin auth logout` FIRST, then `devin auth
   login`. Hit 2026-10-13: Aug-26-era token; an expired app.devin.ai/auth
   browser tab is leftover and can just be closed.
5. **Never write into the worker's watched workspace after `Popen`.**
   `cli-user-edit-awareness` treats an external file change in the cwd as a
   user edit and *ends the session itself* - clean `rc 0`, logs stop cold,
   the ACP child gets `session/end` mid-first-delta. Conductor's
   post-spawn `save_state(state.json)` did exactly this on self-hosted
   runs (run_dir `data/runs/...` sits inside `agent-conductor`'s own cwd),
   so every live smoke worker died in 0.1-3s and the step was recorded
   `done` with "no STATUS line". Diagnosed 2026-10-13 by bisecting
   `run_step` inside `cmd_run`: bare `Popen`+`wait` ran 76s of real work;
   adding only the mid-spawn `save_state` killed it in 0.2s. Fix: `rec`
   keeps `pid` in memory; `state.json`/`events.jsonl` are written only
   *before* spawn and *after* wait. (Earlier theory "the subprocess
   detaches while the session runs on" was wrong - `devin list` showed a
   session entry but it was already dead; the README that appeared later
   came from a manually spawned session.)
   - Parallel safety: same-project waves are forced onto worktrees whose
     cwd is `data/worktrees/<run>/<step>` - `data/runs` writes are outside
     every worker's watch dir. Cross-project runs write outside the
     worker's cwd too. The poisoned case was only non-worktree self-runs.
6. **`rc 0` proves nothing.** A `devin -p` worker can exit 0 before the ACP
   turn starts (see trap 5) or print `STATUS` only at the end of a real
   turn. `run_step` now marks `rc==0` + missing `STATUS` as `failed`
   (note: "no-op run"), never `done`.

## The task workspace - read before starting work

`.agent_tasks/` holds the state of work in progress, so the plan does not die
with the session. The full contract is `.agent_tasks/README.md`, the template
`.agent_tasks/_template/STATE.md`. The short version:

1. **Read `.agent_tasks/QUEUE.md` first**, then the `STATE.md` of any task
   that is not `done`. If the queue is empty, fall back to `ROADMAP.md`. Do
   not open a second task while one is `in progress` unless the user asks.
2. **Progress is the checklist.** Tick a step only when it is finished *and
   verified*. Never delete a step: tick it, or move it to *Deferred or
   blocked* with the reason.
3. **Long output goes to `.agent_tasks/<task>/scratch/`**, which is
   gitignored, so any conclusion drawn from it must end up in `STATE.md`, or
   in `ROADMAP.md` if it is a measurement.
4. **Update `STATE.md` as state changes**, especially its *Where it stopped*
   line. Append a row to `.agent_tasks/efficiency_stats.md` before stopping.

A `STATE.md` step is a step of the work; a roadmap item is product state.
When a step lands and moves a roadmap item, tick both in the same commit.

## Assume this is the last prompt of the session

Sessions end without warning. Work accordingly:

- **Update `ROADMAP.md` when a phase item changes state**, in the same commit
  as the change.
- **When a measurement is taken, write the number down** where the roadmap
  keeps its findings. A superseded number is useful; a missing one is not.
- **Commit in coherent local groups** and never push without the user's
  explicit request.
- **If about to run low, stop and write rather than start something new.**
  Half-finished unverified code is worse than a documented gap.

## First run on a new machine

- Python 3.11+ (tomllib), git on PATH, `devin` CLI on PATH + `devin auth login`.
- Nothing else: stdlib only. `data/` is all generated output; `logs/` regenerates.
- `python conductor.py demo` is the no-auth proof that the install works.

## Verifying a change

```
python run_tests.py            # unit + mock integration, ~7s
python conductor.py demo       # user-visible end-to-end
python conductor.py plan examples/night.example.toml   # manifest validation
```
