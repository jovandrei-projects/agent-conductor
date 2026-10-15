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
python conductor.py sweep                   re-mark orphaned records 'lost'
python conductor.py sessions [--rm]         list/clean spawned devin sessions
python conductor.py demo                    end-to-end vs tests/mock_devin.py
python run_tests.py                         unittest suite (mock-only, no auth)
```

Session hygiene: `sessions` whitelists on ids recorded in run state.json -
interactive/human sessions can never be listed or removed. `--rm` deletes
sessions whose run ended more than `--keep-hours` ago (default 48); the
ATIF transcript survives in the run dir, so spawned sessions are
disposable bookkeeping. The same 48h cleanup runs automatically inside
`sweep` and once at `serve` startup (`sweep_sessions`) - still
whitelist-only, so automation is safe. `devin list --format json|csv` is cwd-scoped -
sessions are only visible under the work_dir that spawned them (the
interactive `devin list` picker is GLOBAL, "all sessions" - it ignores
cwd, so the sidebar-style list mixes every workspace's sessions).
Sessions from merged worktrees can't be enumerated (`devin rm <id>`
still works if you know the id - it is in state.json). Also invisible
to `sessions`: spawned
sessions whose id was never recorded (the trap-5-era dead runs) - they
are indistinguishable from human sessions in `devin list`, so they stay
manual cleanup.

Run flags: `--devin "<cmd>"` worker binary override, `--no-hub` skip the
hub.db row, `--no-keep-awake` allow system sleep (runs request
`SetThreadExecutionState` by default so overnight work survives),
`--max-parallel N` concurrency cap (overrides `run.max_parallel`).

Retries: `max_attempts = N` (per step or `[defaults]`, 1-10, default 1)
re-runs a step that hits `timeout_min` in a FRESH session - each attempt's
work is committed before the next spawns and the retry prompt is prepended
a "continue from committed state" note, so an underestimated timeout
continues instead of ending the step. Attempts get their own
`prompts/*.attemptN.md`, `*.attemptN.stdout.log`, `*.attemptN.export.json`;
`elapsed_s` covers all attempts, `attempts`/`session_ids` land in the rec
(every attempt's session joins the whitelist, not just the last). Retries
fire on `timed_out` only - a post-STATUS hang (trap 12) already counts as
turn end, and `failed`/`blocked` stay terminal.

Concurrency: `run.max_parallel` caps how many agents run at once across the
whole run; wave members beyond the cap wait in a queue (marked `queued` in
state). Default is conservative: ~1 agent per 4 logical cores, max 4 -
each agent is a full Devin session with tool subprocesses, not a thread.
The run records the effective cap + free RAM in `state.json`.

Tracking: `state.json` is the agent registry (status, pid, started/ended,
elapsed, session id, work dir, paths to prompt/stdout/export, and per-step
`tokens` - prompt/completion/cached sums plus LLM step count, read out of
the ATIF export's `final_metrics` at step end; they roll up per run on the
dashboard). The planner session also exports and its metrics accumulate on
the request as `plan_tokens` (across regenerations and the malformed-TOML
retry) - the runs table shows them as `plan`/`steps` labels when present.
`serve`
renders it as a live page at `http://127.0.0.1:8773` (loopback only,
auto-refreshes, shows elapsed time ticking for running agents). The same
run row is live-posted to `hub.db`, so `http://127.0.0.1:8762/tasks-ui`
shows coarse progress on the phone without any new wiring.

Parallel: adjacent `[[step]]` entries sharing `wave = N` run concurrently,
bounded by the cap. Steps on the *same* project in a wave need
`worktree = true` - each runs in `data/worktrees/<run>/<step>` on branch
`conductor/<run>/<step>`. Merging is always the parent's job: workers
never merge; the conductor waits for the whole wave, then merges each
branch itself, sequentially, in manifest order. A conflict fails the step
but keeps the worktree for manual merge - or, with `merge_resolver =
true` ([run] or per-step), spawns a `resolve-<step>` session in the main
checkout *mid-merge* to finish it (refused on a dirty checkout; a
resolver that can't finish -> same abort+keep path). `--resume`
finalizes unmerged branches instead of re-running them (a conflicted
branch can't be fixed by re-running - it shares the merge base), and
aborts a stale `conductor/*` merge-in-progress first - a human's merge is
never touched.

`manifests/` holds real manifests (hand-written `<project>-<what>.toml` and
UI-generated `<YYYYMMDD-HHMMSS>-<random>-<run>.toml`, flat; see its README);
`examples/` is reference-only. `projects.toml` registers slug -> path +
`cloud_ok`; relative `path`s resolve against the repo root, so a git
worktree checkout needs the untracked `projects.local.toml` next to it
(absolute paths) - it merges over the tracked entries. `--devin "..."` or `CONDUCTOR_DEVIN` overrides the spawned binary
(shlex-split; use forward slashes or quotes - posix shlex eats unquoted
backslashes). The :8773 dashboard's New request saves raw drafts in ignored
`data/requests/`, generates/validates a reviewable TOML with a planning-only
Devin CLI session, and shows its steps as `planned`. The planner's own stdout
streams to `data/requests/<id>.gen.log` (outside its tempdir cwd, so trap-5
safe) and appears live as a `plan-proposal` agent row while generating. Saving or generating NEVER
launches implementation: only the separate Start run button does. Editing
request/model after generation marks the plan stale; regenerate or edit+save
TOML before launching. The form's follow-up checkbox asks the planner for a
closing `follow-up-plan` step that drafts `<run>-followup.toml` into
`manifests/` for review; if the planner omits it, `_publish_proposal`
appends the step itself. `On step failure` and `Attempts/step` map to
`run.on_failure` and `[defaults].max_attempts`; generation fails closed
when the planner ignores them (edit the TOML directly to override).
`Step model/effort` feed `[defaults]` (the workers); `Planner
model/effort` feed only the `--model` of the planning session itself, so
an expensive planner can write a runbook cheap workers execute. The model
fields autocomplete from `devin models list` (cached 10min, parsed into
per-family effort suffixes - an empty effort picks the family's bare
variant); the fixed fallback list is all you get while logged out.
Discard removes only an unrun, app-owned proposal and its planned rows.
The `worktrees`/`merge resolver` checkboxes reach the planner prompt and
are guaranteed into the TOML at publish (`_guarantee_isolation` - never
applied to `cloud=true` steps; wave-required members always keep
`worktree = true`); Review's `Apply` button is the deterministic path -
`_apply_fields_to_toml` syncs run-level keys + per-step overrides the
form owns, so an unchecked `worktrees` strips planner-added flags except
where a same-project wave requires them.
Real runs still use `state.json` as their registry. A run's detail modal
has a Resume button (`POST /api/runs/<dir>/resume` -> `run <manifest>
--resume <dir>`) once nothing in it is live - same semantics as the CLI
flag, fresh attempts for every non-done step. While a run IS live the
modal instead offers `Stop run now` (POST `/stop` - flags + tree-kills
every live step) and `Drain & stop` (`/stop` `{drain:true}` - live steps
finish, nothing new launches); an agent modal on a live step gets `Stop
this agent` (POST `/api/runs/<dir>/steps/<step>/stop`). Stop/drain travel
as empty sentinel files in `%TEMP%/agent-conductor-flags` (never inside
run_dir - trap 5), polled by `wait_worker` and the wave loop; `resume`
clears them via `clear_run_flags` first. A live agent's modal also has a
timeout editor (`POST .../steps/<step>/timeout` {minutes}) - the same
sentinel mechanism: `wait_worker` re-reads it each poll and moves the
deadline to spawn+minutes (under elapsed = early kill; retries inherit).
A step killed by a stop is
`stopped` (vs `skipped` = never spawned); both plus `lost`/`timed_out`
get INTERRUPTED_NOTE prepended on resume so the fresh session recovers
from committed state instead of trusting a clean start. `model =
"adaptive"` is a valid step/planner model - it takes no effort suffix,
and the export's `agent.model_name` lands in `rec.model_actual` so the
dashboard shows which real model adaptive picked and prices against it. When a run has unmerged
`conductor/*` branches a `Merge pending branches` button appears
(`POST /api/runs/<dir>/merge` -> `merge_run`): it replays the post-wave
merge loop for parked branches, spawns the `resolve-*` session when the
manifest opts in, and 409s while any step is live - the fix for "the
conductor died/conflicted before merging" without `--resume`.

Cost display: `serve` fetches self-serve rates from
`docs.devin.ai/desktop/chat/models.md` (the Mintlify page embeds a
`modelCostData` JS array; `TEAMS_TIER_PRO` is the self-serve tier),
disk-caches them in `data/pricing.json` beside `runs/`, and refetches in
a thread every 24h - offline/failed fetches just keep the last good
table, and an unpriced model renders `-` while a real $0 (promo) renders
`$0`. Formula: `(prompt - cached) * in_rate + cached * cache_rate +
completion * out_rate` (cached is a component of prompt, never added on
top). The session column also tolerates `devin list` propagation lag: a
recorded id missing from the listing is `deleted by user` only after the
step ended or `SESSION_GONE_GRACE_S` (5min) passed.

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
   Docs say CLI tokens do NOT expire by default - a dead token was
   rejected/revoked server-side, not timed out. Hit twice (Aug-26 era, and
   again ~14h after the 2026-10-13 morning login). If a fresh login dies
   quickly too, that's an account-side problem for Devin support, not a
   conductor constraint. The dashboard banner shows the state; its
   `[log in]` button pops a real terminal running logout+login (the TUI
   still can't be driven from a pipe - the button just opens the right
   window for the human).
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
7. **`os.kill(pid, 0)` is TerminateProcess on Windows.** It KILLS the
   process it probes - verified by spawning a sleeper and watching it die.
   The dashboard's liveness check used it on every `running` record per
   `/api/state` refresh, so an open dashboard during a run murdered the
   workers (their pids reach disk via sibling save_state calls in waves).
   Probe with OpenProcess + GetExitCodeProcess (259=STILL_ACTIVE) instead.
   Windows also recycles pids: `pid_alive` compares the process's creation
   time to the step's `started_at`, or a reused pid reads as alive.
8. **`running`/`queued` records can be orphans.** A conductor that dies
   mid-run leaves live-looking rows forever (the dashboard showed "6
   running" 9h after the last worker died). `sweep` re-marks them `lost`
   (dead pid, or no-pid record older than timeout+15min; queued is lost
   when the run ended or went quiet >10min). `serve` sweeps once at
   startup; `status`/dashboard compute the same `lost` display live.
   `sweep` refuses to write while a live step's work_dir contains
   `data/runs` - a self-hosted run in flight (trap 5).
9. **IDE env vars poison `devin` auth.** When conductor runs inside the
   Devin Desktop / VS-Code extension host (this repo's dev session is),
   inherited `ACP_BACKEND`, `WINDSURF_*`, `VSCODE_*`, `TERM_PROGRAM` make
   the CLI expect host-supplied auth instead of reading
   `credentials.toml` - `auth status` and even `-p` report "Not logged
   in" while a plain console says logged in against the SAME file.
   Cost a full day of "token died in minutes" false diagnoses
   (2026-10-14). `clean_devin_env()` strips those prefixes from EVERY
   spawned devin child: workers, probes, `devin list`, `devin rm`, and
   the dashboard login terminal.
10. **Don't commit in a repo with a live non-worktree self-run.** A
    second agent's `git add -A` mid-run sweeps the worker's in-flight
    edits into the WRONG commit (hit 2026-10-14: a dashboard commit
    swallowed the favicon worker's icon change; the worker noticed, kept
    the tree consistent, finished clean). Same-repo concurrency is
    exactly what `worktree = true` is for - use it whenever another agent
    (human or not) may commit to the project while a step runs.
11. **Never auto-`git add -A && commit` around a merge in progress.** The
    merge resolver deliberately runs a session mid-merge in the main
    checkout; a blind checkpoint there would finalize whatever is staged -
    conflict markers included. `run_step(post_commit=False)` exists for
    exactly this; the conductor only completes the merge itself once the
    index is conflict-free AND marker-free AND the branch tip is an
    ancestor of HEAD (a resolver that quietly `merge --abort`s can never
    fake 'resolved'). Same reasoning: resolver is refused on a dirty main
    checkout - someone else's uncommitted work would get swept into the
    merge commit (trap 10 shape).
12. **A `devin -p` worker that leaves a spawned server alive never
    exits.** The CLI's process stays up while any session-spawned child
    lives (workers' playwright probes boot vite :8805 and leave it
    running) - the turn can be fully over (STATUS printed, export.json
    written) yet the process idles until timeout_min. 2026-10-14
    views-floorplan run: 3 of 5 steps printed `STATUS: done` then burned
    52/52/85min; the one clean exit (camera-stability) had explicitly
    `taskkill /F`ed its leftover server first. Fixes in place:
    `wait_worker` polls the log for a STATUS line and collapses the
    remaining wait to `STATUS_GRACE_S` before killing (counts as turn
    end - verify still runs, status_line honored); `kill_tree` uses
    `taskkill /T` because bare `proc.kill()` orphans grandchildren - a
    stray vite kept :8805 bound 3h after that run. Worker-side
    convention still helps: workers should kill servers they spawn
    before STATUS (camera-stability's agent knew via apartment-3d's own
    trap notes; a `devin -p` exit-hang on live children is arguably a
    CLI bug - worth reporting upstream if it persists).
13. **A dev worktree's session whitelist dies with the checkout.**
    `sessions`/`sweep_sessions` whitelist on ids recorded in
    `data/runs/*/state.json` - and every checkout has its OWN data/runs.
    Work done in a worktree of this repo (`git worktree add`, e.g.
    agent-conductor-wt) spawns sessions that only ITS data/runs knows
    about. `sessions` on the default runs dir does scan registered
    worktrees' data/runs too - but `git worktree remove` / `rm -rf` of
    the checkout erases the whitelist itself, after which spawned
    sessions are manual `devin rm <id>` only. Rule: before deleting a
    worktree checkout of THIS repo, either run `sessions --rm
    --keep-hours 0 --runs-dir <wt>/data/runs` or copy its run dirs into
    the main checkout's data/runs (that also puts the runs on :8773).
    Hit 2026-10-14: the dev/merge-tests worktree's lab run left 21
    sessions in the sidebar; cleaned just before the checkout went away.
14. **`pythonw.exe serve` flashes background console windows on Windows.**
    The dashboard probes `devin list` for work dirs and `devin auth status`
    every 120s. Under `pythonw.exe` those console subprocesses open brief
    blank windows (multiple work dirs -> multiple flashes per refresh), even
    with captured stdout. All non-interactive subprocesses now use
    `CREATE_NO_WINDOW` on Windows (`hidden_subprocess_kwargs`); keep the
    intentionally interactive `[log in]` button's terminal visible. A
    running `pythonw.exe` server must be restarted to load this fix.
15. **Stopping `serve` must not stop work - but the shared console betrays
    that.** Dashboard-launched `run`/`resume` conductors are children of
    `serve`, and `CREATE_NO_WINDOW` children still share the parent's
    console: Ctrl+C on the server's console broadcasts CTRL_C_EVENT to
    EVERY attached process, runs and their workers included. `_launch_process`
    therefore adds `CREATE_NEW_PROCESS_GROUP` (posix: `start_new_session`)
    - a console Ctrl+C or a plain server exit leaves runs running and the
    restarted dashboard picks them up from state.json/pids. Only the stop
    endpoints (pid-targeted `taskkill /T`) or Ctrl+C inside a terminal that
    ran `conductor.py run` directly should kill work. Caveat: `taskkill /T`
    on the SERVER pid still walks into its children - do not tree-kill the
    dashboard.

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
