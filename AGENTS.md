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
python conductor.py demo                    end-to-end vs tests/mock_devin.py
python run_tests.py                         unittest suite (mock-only, no auth)
```

Run flags: `--devin "<cmd>"` worker binary override, `--no-hub` skip the
hub.db row, `--no-keep-awake` allow system sleep (runs request
`SetThreadExecutionState` by default so overnight work survives).

Parallel: adjacent `[[step]]` entries sharing `wave = N` run concurrently.
Steps on the *same* project in a wave need `worktree = true` - each runs in
`data/worktrees/<run>/<step>` on branch `conductor/<run>/<step>`, merged back
in manifest order after the wave; a merge conflict fails the step but keeps
the worktree for manual merge.

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
