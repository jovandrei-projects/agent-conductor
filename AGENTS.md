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
a run log, verify gates, and a `STATUS:`/`NEXT:`/`OUTCOME:` report
contract (`OUTCOME:` is the worker's own one-to-two-sentence result
summary for the human reviewing the run - concrete accomplishments plus
material limits, never next-step instructions or unverified success).


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
4. **A stalled step dies; a working one isn't rushed.** `timeout_min` is
   a floor: past it the worker may keep running while its stdout log
   keeps growing, killed only after `stall_min` (default 20, per step or
   `[defaults]`, 0 disables) of silence or at the 4x hard ceiling. The
   dashboard timeout editor still moves the floor live.
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
python conductor.py merge <run_dir>         land pending (deferred/parked) branches
python conductor.py summarize <run_dir>     write the run's LLM executive summary
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
whole run; wave/pool members beyond the cap wait in a queue (marked
`queued` in state). Default is conservative: ~1 agent per 4 logical cores,
max 4 - each agent is a full Devin session with tool subprocesses, not a
thread. The run records the effective cap + free RAM in `state.json`.
A second, machine-wide ceiling sits under all of it: the AGENT SLOT POOL
in `%TEMP%/agent-conductor-slots` (`CONDUCTOR_SLOT_DIR` overrides),
`CONDUCTOR_MAX_AGENTS`-sized (default 4..8 scaled to cpu_count, never
below 4) O_EXCL leases like the device/port registries. Every dispatched
step acquires one slot after its device lease and before spawn, holds it
for the whole `run_step` life (attempts, interrupt respawns, resolver
sessions count too), and a step that finds no free slot waits `queued`
with `slot_wait`/`slot_waited_s` + a note naming the holders - queued
steps hold NO slot so the pool always drains. Slot events
(`slot_acquired`/`slot_wait`/`slot_released`) land in events.jsonl, the
dashboard meta line shows `slots N/M` plus the queued count, and
`sweep_agent_slots` (sweep + serve startup) reclaims dead-owner leases.
Parallel sessions get a PARALLEL_NOTE prompt block: share the box, own
your checkout/leases/ports, never fight a peer or the user over a shared
resource - if a genuinely exclusive resource is held, end `STATUS:
blocked` naming it instead of racing.

Android devices: parallel STEPS are not parallel DEVICES - adb serials are
shared machine state, and without `device` a mobile worker picks its own
target (two workers will happily race `emulator-5554`; one step's cleanup
`emu kill` has already killed a shared emulator in a real run). A
`[[step]] device = ...` (`"any"`, `"emulator"`, `"physical"`, or an exact
adb serial; also `true` = any; `[defaults]` and the project's
`projects.toml` entry can carry it like `verify`) leases ONE serial
machine-wide in `%TEMP%/agent-conductor-devices/` for the step's whole
life - every attempt, interrupt respawn and the verify gate get
`ANDROID_SERIAL` + `CONDUCTOR_DEVICE_SERIAL`, the prompt gets a device
note, and `state.json` records `device`/`device_spec`/`device_booted`.
A busy-but-attached device queues the step (`queued` + `device_wait`,
uncapped - it frees when the owner's step ends or its pids die); a device
that never attaches blocks the step after `device_wait_s` (default 600,
0 = wait forever). Leases are O_EXCL files with owner+worker pids - a
dead driver doesn't free a device its orphaned worker still uses, and a
fully-dead lease is broken once (a recorded `booted` emulator it left
behind is reaped by serial+pid, never a blanket kill). `device =
"emulator"` may BOOT a free AVD on a free even console port when nothing
matching is attached (skipped: `device_boot = false`; `"any"` boots only
with `device_boot = true`; AVD + port + `emu_pid` are recorded so release
stops exactly that instance via `emu kill` → pid-verified taskkill - a
user's emulator or phone is never stopped). `device` + `cloud` is a
manifest error - a cloud worker can't see local adb. adb/emulator binaries
are discovered via `CONDUCTOR_ADB`/`CONDUCTOR_EMULATOR`, PATH, or the SDK
dirs (`ANDROID_HOME`, `ANDROID_SDK_ROOT`, `%LOCALAPPDATA%\Android\Sdk`).
Known gap: leases own the SERIAL, not the shared adb server - workers
still mustn't reuse fixed host ports (`adb forward tcp:9222`) across
concurrent steps; pick a free port per session.

Intentional app restarts: a worker that reloads or restarts a WebView
app can hit the app's unsaved-changes guard - the WebView shows a NATIVE
'Confirm Navigation' dialog (Leave this Page / Stay on this Page) that
no CDP/JS channel can answer, so the navigation never completes and
automation behind it waits forever (burned 20261014-235630-
apartment-play-edit's mobile-parity-validation: its harness ran
`location.reload()` on a page dirty with its own test edits; the
emulator sat on the dialog while the step clock ran out). The shipped
`mobile_device.py` (next to conductor.py, stdlib-only) is the supported
path and the device note hands it to every leased step:
`relaunch <pkg>` = process restart the dialog cannot block (settles
first so completed saves flush; in-memory unsaved state is lost - use
only when disposable), `confirm-navigation --pkg <pkg> [--leave|--stay]`
= bounded poll for the known dialog after an in-page reload, tapping
only when the caller asserts the dirty state is disposable/saved
(`--leave`) or aborts it (`--stay`); no decision flag = refuse,
unexpected dialogs = uiautomator+screencap diag + actionable failure,
never an auto-accept, every call `-s <leased serial>` (a foreign
`--serial` under a lease is refused). App-side follow-ups recorded
(apartment-3d, not edited here): its `location.reload()`-based harnesses
should either save/flush before reload or call the helper, and an
automation-authorized guard bypass (intent extra / `__save.flushNow()`
await hook) would remove the dialog round-trip entirely - today only
uiautomator can answer it.

Tracking: `state.json` is the agent registry (status, pid, started/ended,
elapsed, session id, work dir, paths to prompt/stdout/export, `merged` +
`merged_at` for worktree branches, `outcome` - the step's newest
`OUTCOME:` report - plus `outcomes`, the bounded history of earlier
attempts'/dispatches' reports so a resume's fresh result never erases
what a previous attempt finished; and per-step
`tokens` - prompt/completion/cached sums plus LLM step count, read out of
the ATIF export's `final_metrics` at step end; they roll up per run on the
dashboard). The planner session also exports and its metrics accumulate on
the request as `plan_tokens` (across regenerations and the malformed-TOML
retry) - the runs table shows them as `planner:`/`steps:` token lines
when present, each field labeled (`in / out / cached / turns` - cached
is a share of `in`, never additive; turns = LLM steps, not manifest
steps). A run row also carries a compact `wt` chip from `iso` - the
run's CONFIGURED worktree coverage `{wt,local,cloud}` computed by
`_run_isolation`: the parsed manifest is authoritative (parse already
folds `merge='deferred'` into worktree and rejects worktree+cloud),
falling back to the request's `proposed_steps` snapshot, then the
per-record `wt`/`cloud` stamped on every step record at queue/dispatch -
never branch names or liveness, so an undispatched run still labels and
a stray branch on a legacy record invents nothing. Partial coverage
spells it out on the chip (`wt 2/3`); cloud-only or worktree-free runs
get none.
`serve`
renders it as a live page at `http://127.0.0.1:8773` (loopback only,
auto-refreshes, shows elapsed time ticking for running agents). The same
run row is live-posted to `hub.db`, so `http://127.0.0.1:8762/tasks-ui`
shows coarse progress on the phone without any new wiring.

Elapsed time is **cumulative across dispatches**: every `run_step`
dispatch appends `{start,end}` to the step's `exec_intervals` and every
driver session appends one to the run's own `exec_intervals`
(opened at create/resume alongside `driver_started_at`, closed wherever
`ended_at` is stamped). `elapsed_s` stays the closed-intervals cache;
`first_started_at` keeps the original start while `started_at` is the
live dispatch's (pid/liveness windows need it). A resume therefore
accumulates instead of overwriting, idle gaps between attempts never
count, and run elapsed is driver time - never a sum of overlapping step
spans. `rec_elapsed_s`/`run_elapsed_s` are the one authoritative calc
(dashboard payload, `cmd_status`, summaries all share it); every
recorded stamp routes through the `_now()` seam so tests run a
deterministic clock. A crashed dispatch's orphaned interval closes at
its last sign of life (log mtime / newest state stamp), not at
discovery. Pre-interval records seed once from their stored
started/ended/elapsed fields - `{'dur': span}` when endpoints are gone,
never fabricated endpoints; nothing recorded renders as `-`.

Parallel: adjacent `[[step]]` entries sharing `wave = N` run concurrently,
bounded by the cap - OR `[run] parallel = true` puts EVERY step in one
shared pool scheduled as slots free (`wave=` keys are rejected at load as
redundant; a trailing `follow-up-plan` step still runs last, in its own
group after the pool drains). Steps on the *same* project in a wave or a
parallel pool need `worktree = true` - each runs in
`data/worktrees/<run>/<step>` on branch
`conductor/<run>/<step>`. Merging is always the parent's job: workers
never merge; the conductor waits for the whole wave, then merges each
branch itself, sequentially, in manifest order. A conflict fails the step
but keeps the worktree for manual merge - or, with `merge_resolver =
true` ([run] or per-step), spawns a `resolve-<step>` session where the
merge is open *mid-merge* to finish it (the main checkout, or the step's
merge worktree for a non-checked-out merge_target; refused on a dirty
location; a resolver that can't finish -> same abort+keep path).
`--resume`
finalizes unmerged branches instead of re-running them (a conflicted
branch can't be fixed by re-running - it shares the merge base), and
aborts a stale `conductor/*` merge-in-progress first - a human's merge is
never touched. All merges - post-wave, resume-finalize, dashboard,
deferred staging - serialize on a per-repo `.git/conductor-merge.lock`
(pid + timestamp, broken when the holder pid is dead) since two drivers
have raced merges and stale-aborts on the same checkout before. Run paths
(post-wave, resume-finalize) QUEUE on the lock (`wait=`) instead of
failing: the step record carries `merge_queue`=<holder pid> + a note while
it waits, a `merge_wait` event lands, and halt sentinels still bail it
out; dashboard/deferred merge entry points keep the fail-fast 409 'busy'.
Every
merge outcome lands in `events.jsonl`: `step_merged` /
`step_merge_conflict` per step (`how` = wave/resume/manual, `was` = the
status it replaces, `resolver` when a resolve-* session saved it),
`step_merge_kept` when a failed step's branch is parked unmerged,
`worktree_removed` for no-product debris, the `merge_*` family for the
deferred pipeline, and `merged_at` on the step record for when the branch
landed.

Summaries: the step summary (`Summary` link beside the step name in the
task modal, `<step>.summary.md`) leads with `## Report` - the worker's
OWN closing report, lifted from the trailing agent messages of its ATIF
export with the `STATUS:`/`NEXT:` contract lines stripped
(`final_report`); a step whose turn ended mid-report shows the forensic
sections without it. `## Task` carries the full prompt body in a
fixed-size pre box (`task_display` strips the conductor-purpose marker;
`.taskpre` gives small tasks the same slot). The run-level equivalent
is `run.summary.md`: with `[run] summarize = true` (or `= "<model>"` to
pin a cheap model) the driver spawns a detached `conductor.py summarize`
session after `run_end` that reads run stats + every step's closing
report (never the projects themselves - cwd is a tempdir) and writes one
executive summary; the same op runs via `python conductor.py summarize
<run_dir>` or the run modal's **Generate LLM summary** (`POST
/api/runs/<dir>/summarize`, refused while a step is live or one is
already generating - `.summarizing` marker + `Run summary`/`has_summary`
in the runs payload). `summarize.stdout.log` / `summarize.export.json`
record the session, `run_summarized` / `run_summary_failed` land in
events.jsonl, `state.run_summary` carries the same bookkeeping a step
record does (status, started/ended/elapsed, session, model, tokens,
cost inputs, generated_at). The summarizer NEVER changes step or run
status - when it fails or was never run, GET `/api/runs/<dir>/summary`
serves the deterministic roll-up (`run_report_md`: stats + each step's
closing report verbatim). On the dashboard it surfaces as one muted,
italic `run-summary` aux row under its run (`summary_agent` - `seq` is
null so it never counts as pipeline work, the context menu offers no
step actions, and it stays hidden until the run is filtered into); its
detail modal is where the summarizer's tokens/elapsed/session live. The
request form's `summarize` checkbox maps to `run.summarize` like the
other guarantee checkboxes.

Modal chrome: one sticky `.dnav` bar - Back (`← Back`, destination on
the tooltip) pins left, the pipeline counter `task X of Y` centers on
the BAR (absolutely positioned so sibling names can't shift it), and
prev/next arrows pin right (arrow-only in the summary view, named in the
task modal at a fixed 130px). Every control that mutates task/run state
lives in the `.actzone` "Actions" block at the bottom of the modal;
view-only controls (trajectory expand/collapse) stay with their content.
The timeout Save is disabled until the field differs from the applied
value. The `run` kv row and the run modal's tasks table are
`data-run-details`/`data-agent-detail` links - modal-to-modal jumps push
the Back stack like everything else.

Review marks cascade parent <-> children (`_set_review` writes the
sentinels): a run-level 'Mark as reviewed'/'Mark as needing review'
propagates to every recorded step (steps that never completed - no
completion_seq, no `_REVIEWABLE_ST` outcome - are skipped so a tombstone
never invents work), and every step mark re-derives the run's mark:
reviewed only once ALL recorded steps are covered, tombstone otherwise.
`seed_review_flags` at `serve` start backfills completions that predate
completion_seq (a seq-less record only flags on an explicit tombstone);
it writes marks only where NONE exists, so it's idempotent and can
never undo an ack. Follow-up links are both ways: `state.followup`
links the run row forward to the proposal, `followup_src` (request
record AND launched-run payload) links back to the source run, and
`linkKeys()`/`applySel()` marks the related row `.dlinked` while
`.dsel` stays on the open view's own row. Changed request fields get an
amber `.pendchg` frame from `refreshPending` (the `PEND_EL` key->element
map), suppressed mid-planning like the pend-note itself.

`merge = "deferred"` (per-step or `[run]` default; implies
`worktree = true`, incompatible with `cloud`) separates isolation from
merging: nothing lands after the wave - branch, worktree, commits and
verify results stay pending in `state.json`'s `pending_merges` until a
human runs `python conductor.py merge <run_dir>` or ticks pending entries
in the run modal's Pending merges panel (`{"steps": [...]}` lands only
the ticked subset). Sequential deferred steps
on one project still see verified predecessors: each branches off the
run's integration baseline `conductor/<run>/baseline-<slug>`, which
fast-forwards over a step only after status=done AND a green verify, and
absorbs target-branch movement via `merge-tree` plumbing between steps.
Same-wave same-project members are `alternative`s - parked apart, never
auto-combined. The explicit merge stages the verified entries in an
isolated `stage-<hash>` worktree, re-runs their `verify` gates on the
combination (`verify_must_pass=false` gates skipped; a verify-red step
is never even staged), resolves conflicts via merge_resolver IN the
stage, then moves the recorded target atomically (`update-ref` expected
value when the target isn't checked out, `merge --ff-only` when it is -
refused while dirty or watched by a live agent). A red combined verify
or a mid-merge/dirty stage keeps everything pending (stage kept for
inspection); a moved target drops the scratch stage - pending branches
are always the durable state, never deleted by cleanup paths.

Worker services: every dispatched local step leases one loopback port
from `8806-8859` (just past the C:\Projects registry's last assignment,
so a worker never receives a registered project port) for its whole
lifetime. Leases are files in `%TEMP%/agent-conductor-ports` - O_EXCL
file claims arbitrate between concurrent conductors, a `127.0.0.1` bind
probe is the truth for lease-less listeners (a bound port is skipped,
never taken over or killed), and a recorded owner pid + creation stamp
makes a dead conductor's leases reclaimable (`sweep`, `serve` startup).
The worker learns its context through `CONDUCTOR_*` env vars
(`WORK_DIR`/`REPO_DIR`/`RUN`/`STEP`/`BRANCH`/`SERVICE_PORT`/`SERVICE_URL`)
plus a prompt block naming its isolated checkout and leased port;
`conductor.py serve` run inside a worker binds that leased port instead
of 8773. The step record keeps `service_port`/`service_url` for review
and notes when the port is still bound after the worker exits (a leaked
dev server - reported, never port-killed: a bound port doesn't prove who
owns it). Cloud steps get nothing - they bind nothing local.

Merge targets: `merge_target = "<branch>"` ([[step]], `[defaults]`, or
[run] as the worktree-steps default) makes the step branch fork from and
merge back into that existing local branch instead of the checkout's
HEAD. Merges into a branch the checkout doesn't have out run in a
conductor-owned merge worktree `data/worktrees/<run>/<step>.merge`, so
the main checkout never moves; a target checked out in another worktree
fails actionably (conductor never merges into a checkout it doesn't
own). The resolved target is recorded in state.json per step, so resume,
merge_resolver sessions (which run in the merge worktree, not the
checkout) and the dashboard's manual Merge all honor it. A step-level
target without `worktree = true` is a manifest error; a missing or
never-existing branch fails at plan/proposal validation - nothing is
substituted and nothing is created.

`manifests/` holds real manifests (hand-written `<project>-<what>.toml` and
UI-generated `<YYYYMMDD-HHMMSS>-<random>-<run>.toml`, flat; see its README);
`examples/` is reference-only. `projects.toml` registers slug -> path +
`cloud_ok` + optional `verify` (the project's known gate command, e.g.
`python run_tests.py` - steps on the slug inherit it unless they set
their own; `verify = ""` opts out; cloud steps never inherit) + optional
`device`/`device_wait_s`/`device_boot` (a mobile slug's adb serial class;
steps on it inherit unless they set their own - cloud steps never do).
Relative
`path`s resolve against the repo root, so a git
worktree checkout needs the untracked `projects.local.toml` next to it
(absolute paths) - it merges over the tracked entries. `--devin "..."` or `CONDUCTOR_DEVIN` overrides the spawned binary
(shlex-split; use forward slashes or quotes - posix shlex eats unquoted
backslashes). The :8773 dashboard's New request saves raw drafts in ignored
`data/requests/`, generates/validates a reviewable TOML with a planning-only
Devin CLI session, and shows its steps as `planned`. Generation is isolated
from repo traffic: the planner runs in a tempdir in its own process group
(a merge or a serve Ctrl+C cannot reach it - trap 15), and everything it
produces spools under `%TEMP%/agent-conductor-planner/<id>/` (`gen.log`,
`gen.export.json`, `draft.toml`, a `request.json` overlay, `job.json`) -
never a watched workspace. The overlay is the live request record until the
short publish lands; `_read_request`/`_request_status`/the dashboard consult
it, so generation may run while a `resolve-*` session or an open merge owns
the checkout - only the final publish waits on `_wait_writable`
(`_assert_unwatched` also treats MERGE_HEAD in a non-ignored destination's
repo as blocking). `drain_planner_spool` at `serve` startup (plus a throttled
pass in `collect_dashboard`) replays what a restart orphaned: publish a
spooled draft, extract a finished manifest from `gen.log`, wait out a
still-writing orphan, or land the interruption as `error` with artifacts
preserved under `data/requests/`. A draft is published only while its
`draft_started_at` matches the current generation - a superseded draft is
kept (`<id>.draft.toml`) but never auto-published, and the publish gate
(`_disk_untouched`) compares the request file's mtime + stop stamp, not just
its second-granularity `updated_at`. `stop_generation` also deletes the
stopped attempt's overlay, so the restored draft/ready state is what
readers see at once instead of a stale 'generating' overlay.
Every finished attempt (published, questions-parked, stopped or failed)
is banked on the request as a `plan_sessions` record - gen_id, captured
session id (`devin list` diff in the planner tempdir, then export
`session_id`/`session` + `tail_session_ref` fallbacks), status, tokens -
and `plan_gen_id` marks the attempt that produced the current manifest.
`collect_dashboard` re-emits them as `plan-proposal` (the producer;
`plan-proposal-N` for other attempts) 'step 0' history rows on the plan's
task list - after publish, after launch (same `request:<id>` run_key) and
after reload, with real status + session/log links but never a `run_dir`
or a `counts` share, so nothing about them is runnable. Saving or generating NEVER
launches implementation: only the separate Start run button does.
Staleness is a *computed* diff (`_pending_actions`), not a latched flag:
generation inputs (`raw`/`project`/`attachments` - `GEN_INPUT_FIELDS`)
are compared to the `gen_inputs` snapshot `_publish_proposal` stamps at
publish, and option fields (`OPTION_FIELDS`) to what the TOML itself
encodes (`_manifest_options`). A diverging generation input needs
Regenerate tasks (Apply refuses with 409 - step prompts can't be patched
deterministically); diverging options need Apply or Regenerate - both
pending means Regenerate first since it writes the options too. Reverting
a field to its baseline clears the mark; launch refuses while either
bucket is pending; a failed generation/apply keeps the pending state; a
hand-edited TOML moves the option baseline (the form then shows drift
until reconciled). A third `mixed` bucket holds option keys whose TOML
baseline is a per-step mix the single form control cannot express
(`worktrees`/`merge`/`merge_target` when `is None`): it frames the
control with an explanatory tooltip but is NOT drift - it never flags
`stale` and never blocks Start (Apply remains the explicit normalize-
every-step path, warned in the note as flattening a deliberate mix).
Records published before baselines existed keep the
old `stale` flag as an opaque `fields` gen marker until a publish
re-baselines them; `ask`/`plan_model`/`plan_effort`/`label` never stale.
The form's follow-up checkbox asks the planner for a
closing `follow-up-plan` step that drafts `<run>-followup.toml` into
`manifests/` for review; if the planner omits it, `_publish_proposal`
appends the step itself. The produced file is *intake, not execution*:
`discover_followups` runs at run_end, `serve` startup, `sweep` and a
60s-throttled pass in `collect_dashboard`, and registers it as a request
record (`Ready to run`, `source_run`/`source_manifest` linked - only a
human's Start run launches it). The file's own text is validated by
`validate_proposal` and copied to an owned `manifests/<id>-<name>.toml`;
the source stays read-only and is never trashed. One repair runs before
validation everywhere a proposal is published (`_publish_proposal` and
`_followup_apply` both route through `_clamp_description`): an overlong
`[run].description` (300-char cap, stated to the planner and the
follow-up writer in their prompts) is trimmed to 300 with an ellipsis
rather than dead-ending the record `error` - a writer that overshoots by
a sentence used to wedge Apply and Regenerate on the same line forever. Idempotency lives in
`data/requests/followups.index` (sha'd per source), so repeated scans,
restarts and rewrites reconcile one record instead of duplicating - a
changed source re-copies and revalidates unless the record was
human-edited (the source sha no longer matches the owned copy, so the
edit wins). A no-`[[step]]` TOML or a "nothing remains" report lands as
`empty` ("No work remains"), a finished step with no file as `missing`
("Follow-up missing" + the expected path and the import hint), malformed
or unvalidatable text as `error` - all request rows, all discardable,
never launchable; a failed/stopped step with no file is `absent` on the
run stamp only, no row. Discarding an imported row vetoes the source in
the index (rescans never resurrect it; Restore clears the veto), while
`python conductor.py import <file.toml>` / `POST /api/requests/import`
register a written file explicitly - the deliberate override that also
imports files predating the feature or orphaned by a foreign checkout.
Run rows carry the `followup` stamp (registered/empty/missing/invalid/
deferred/absent) with a chip linking the request; busy destinations
defer to the next pass instead of failing (`_assert_unwatched`). `On
step failure` and `Attempts/step` map to
`run.on_failure` and `[defaults].max_attempts`; generation fails closed
when the planner ignores them (edit the TOML directly to override).
`Step model/effort` feed `[defaults]` (the workers); `Planner
model/effort` feed only the `--model` of the planning session itself, so
an expensive planner can write a runbook cheap workers execute. The model
fields are free-text comboboxes fed by `devin models list` (cached 10min,
parsed into per-family effort suffixes - an empty effort picks the
family's bare variant): typing filters a Recommendations group over a
separated, scrollable All-models group (`.mall`), a catalogless value
stays valid, and a model present in both groups is two distinct options
so `aria-activedescendant` never aliases. The fixed fallback list is all
you get while logged out.
Discard removes only an unrun, app-owned proposal and its planned rows.
The `worktrees`/`merge resolver` checkboxes and the `Merge timing`
select (auto = merge after each step; deferred = hold everything pending
for a manual merge) reach the planner prompt and are guaranteed into the
TOML at publish (`_guarantee_isolation` - never applied to `cloud=true`
steps; wave-required members always keep `worktree = true`; `deferred`
implies `worktree = true` on non-cloud steps and writes a per-step
`merge = "auto"` on cloud steps since deferred+cloud is invalid).
Picking `deferred` force-checks + greys the worktrees box
(`syncMergeTiming`), restoring the pre-forced state on revert. Review's
`Apply` button is the deterministic path -
`_apply_fields_to_toml` syncs run-level keys + per-step overrides the
form owns, so an unchecked `worktrees` strips planner-added flags except
where a same-project wave requires them, and per-step `merge` keys are
stripped so `[run] merge` is the sole policy. The `Merge target`
field's datalist comes from `GET /api/projects/<slug>/branches` of the
SELECTED project's repo (conductor/* excluded); untouched it defaults to
the checkout's branch (`main` editable while the project is inferred);
Apply writes it project-scoped - per-step keys on that project's
worktree steps when a project is chosen, `[run].merge_target` as the
inferred default - and it joins the stale-on-change set with the other
guaranteed fields.
The `Ask me questions` checkbox (`ask`) is a generation-time permission,
never a TOML key: the planner may answer with `BEGIN_QUESTIONS`/JSON/
`END_QUESTIONS` instead of a manifest, which parks the request at stage
`questions` with the sanitized set in `pending_questions` (a manifest
always wins when both appear). The dashboard pops one question per modal
- option cards, free text, Skip/`Skip all` (`POST
/api/requests/<id>/answer`, `{skip_all:true}` bulk-skips) - and each
answer accumulates in `rec.qa` on disk, so the queue survives restarts
and closed browsers. Emptying `pending_questions` restarts generation as
a refine pass: the prompt carries the collected Q&A block and may ask a
fresh set, capped at `MAX_QUESTION_ROUNDS` (3) rounds before the request
errors out. A Regenerate, an edited `raw`/`project`, or any manifest
publish drops the parked set - the questions were drafted against the
old request. Unchecked, or a planner that simply has none, is byte-for-
byte the old flow.
Request image attachments: dropping an image file anywhere on the
request dialog or pasting a clipboard screenshot uploads the raw bytes
to `POST /api/attachments` (magic-byte sniffed - PNG/JPEG/GIF/WebP/BMP,
20MB cap; the declared Content-Type is never trusted), which saves it as
`data/attachments/<ts>-<rand>.<ext>`; the absolute path is inserted into
`rq-raw` at the caret and the `{name,path}` pair joins `rec.attachments`
(`_attachment_fields` shape-checks, cap 20). The thumbnail strip under
the textarea is served by `GET /api/attachments/<name>` (name-regex +
resolved-parent checks keep it inside the dir) and each image has an `x`
remove while the stage is editable (draft..questions - never after
launch). Attachments feed the planner prompt as an IMAGE ATTACHMENTS
block, and changing them marks an existing proposal stale. Uploads take
the same `_assert_unwatched` trap-5 guard as request writes, since
`data/` sits inside a self-hosted run's watched cwd. Removed/unsaved
uploads leave orphan files in `data/` - ignored, same class as runs/.
**Stop proposal** (`POST /api/requests/<id>/stop`, a button while the
stage is `generating` - request modal, request-run modal, plan-proposal
agent modal) cancels the in-flight planner pass: `stop_generation` raises
a `stopgen-<id>` sentinel (polled by the planner's own `wait_worker`
stop_flags, so the kill is scoped to that request) and tree-kills the
registered `_GEN_PROCS` Popen immediately, then restores the last
editable state - `ready` when a previous proposal exists, `draft`
otherwise, inputs and collected Q&A untouched. Every generation attempt
carries a `gen_id` (`_GENERATING` is the set of live attempt ids, and all
`_finish_generation` writes check it), so a stopped attempt's late TOML,
error callback or questionnaire can never overwrite restored state or a
newer attempt - and its `finally` can't discard the successor's
membership. Repeated stops and stopping a finished generation 409 like
`stop_run` on an ended run; stopping an orphaned `generating` record
clears the stuck stage early.
Real runs still use `state.json` as their registry. Run controls use one
naming system - each visible name a distinct semantics: **Pause run**
(POST `/api/runs/<dir>/pause`) is the graceful hold - in-flight steps
finish, nothing new launches, the run records `aborted="paused"` (the
deliberate machine-restart case); **Stop run now** (POST `/stop`)
tree-kills every live step and records `aborted="stopped"`; **Resume
unfinished steps** (POST `/resume` -> `run <manifest> --resume <dir>`,
offered once nothing is live) re-runs every non-done step in fresh
attempts - same semantics as the CLI flag, covering paused, stopped and
interrupted runs alike. `POST /stop {drain:true}` survives as the legacy
alias of the graceful halt (`aborted="drained"`), and a pause on a
pre-flags-v2 run raises drain flags too - that is the graceful-hold
shape an old-code driver polls. Step controls sit on the agent modal:
**Stop this step** (`POST .../steps/<step>/stop`) kills just that agent
while the run proceeds to the next queued step - `stopped` deliberately
does NOT trip `on_failure`, only run-level sentinels halt the pipeline -
and **Skip step**/**Unskip step** (`POST .../steps/<step>/skip`
`{skip:bool}`) marks a pending (queued/waiting) step so it records
`skipped` instead of spawning: a persistent `skip-<key>-<step>` sentinel
that keeps the step's queue position and stays in force across resume
until unskipped (if its turn already passed, unskip takes effect on the
next resume). Stop/drain/pause/timeout flags are polled by `wait_worker`
and the wave loop; skip flags are checked in run_step's pre-spawn gate
and are NOT consumed. Sentinels travel as empty files in
`%TEMP%/agent-conductor-flags` (never inside run_dir - trap 5); `resume`
clears the halt ones via `clear_run_flags` (skip marks survive on
purpose). Sentinels are path-keyed
(`<name>-<hash>`); runs launched by pre-namespacing code lack
`state["flags"]="v2"` so endpoints dual-write the legacy bare names too -
a new dashboard can still stop a run its older self spawned.
Unplanned-death recovery needs no button: `sweep` (and `effective_statuses`
live) mark dead-pid `running`/orphaned `queued` records `lost`, the run
modal shows an "Interrupted" notice when a run has no `ended_at` and
nothing alive, and plain Resume re-runs them with INTERRUPTED_NOTE. A live agent's modal also has a
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
`conductor/*` branches the run modal shows a **Pending merges** panel:
deferred `pending_merges` groups per project repo (recorded target
branch, baseline, stuck/kept-stage/moved residue, live `resolve-*`
state, repo merge-lock busy flag) plus legacy parked branches, each
entry tickable with its status/verify/`alternative` kind - alternatives
are mutually exclusive picks (ticking one unticks its siblings; they are
never combined silently), unverified deferred entries can't be ticked
(never auto-merged; merge by hand if wanted). **Merge selected pending
work** POSTs `{"steps": [...]}` to `/api/runs/<dir>/merge` ->
`merge_run`, disabled while any step is live or `_MERGE_BUSY` holds the
run; the modal re-renders on every state poll (selections survive in
`mergeSel`) so an in-flight merge's progress shows live, and the
per-bucket result (merged/conflicted/skipped/unverified/verify_failed/
moved/busy/kept) renders inline. For parked auto-policy branches it
replays the post-wave merge loop, spawns the `resolve-*` session when
the manifest opts in - the fix for "the conductor died/conflicted
before merging" without `--resume`. The manual merge is gated on the
run actually being done executing - `run_executing()` is the shared
predicate (live driver pid, or a step record still backed by something
alive), so a live conductor mid-verify/merge, between waves, or
mid-shutdown 409s even when no step reads 'running' at that instant; a
raised stop/pause/drain sentinel is only a request and changes nothing
until the driver is gone. The op also takes the run's merge claim -
`merge-<runkey>` in the flag dir, same O_EXCL+pid shape as the repo
merge lock but machine-global - holds it for the whole operation and
re-reads state under it, so a `run --resume` that already passed its
own checks is caught by the recheck instead of overlapping; the resume
path holds the same claim while it stamps driver_pid, keeping the two
mutually exclusive. `clear_run_flags`, `sweep`/`archive` and `delete`
respect the claim (exempted / skipped / 409), and the dashboard marks
executing runs via the payload's `executing` field so the button and
context item render disabled with the reason instead of hiding.

Run hygiene keeps the runs table short without touching files: a run
whose `ended_at` is older than `CONDUCTOR_ARCHIVE_HOURS` (default 48, 0
disables) is stamped `state["archived"]` by `archive_runs` - folded into
`sweep`/`serve` startup, plus a 30-min throttled pass inside
`collect_dashboard` so a long-lived server still catches up. The stamp
is only a default-view filter: `/api/state` omits archived runs (the
payload's `archived_runs` count drives the `view: N archived` chip,
which refetches `?archived=1` to list them), while resume/merge still
work and `--resume` clears the stamp so a re-entered run resurfaces.
The run modal's **Archive run**/**Unarchive** (`POST .../archive`
`{archived}`) flips it manually for finished runs, and **Delete run**
(`POST .../delete` `{confirm:true}`) `rmtree`s the record outright -
state, logs, prompts, exports; refused (409) while a step is live or a
resume/merge is in flight, and the JS button is disabled then too. A
deleted run's spawned devin sessions are NOT removed - they leave the
`sessions` whitelist and age into manual `devin rm <id>` (trap-13
shape), and its parked worktree branches outlive the record.

Prompt controls sit beside the lifecycle ones on the agent modal.
**Edit prompt** (`POST .../steps/<step>/prompt` `{prompt}`) writes a
`prompt-<key>-<step>` sentinel holding a replacement task body that
`run_step` folds in at render - allowed only while the step never
dispatched (no record, or `queued`/`skipped`; the endpoint 409s once a
prompt exists, and an open form locks on the next refresh). `{prompt:
null}` reverts to the manifest text. **Resume notes**
(`POST .../steps/<step>/notes` `{notes}`) on a ran-but-unfinished step
write `rnote-<key>-<step>` - appended to the next dispatched prompt
under the original body (INTERRUPTED_NOTE + body + notes), so the
operator can say "VPN is restored" without rewriting the original
prompt; empty notes clear it. Both are durable content sentinels that
survive `clear_run_flags` like `skip-` (the resume they target reads
them), and `rec.prompt_edited` + a `note` record when an override was
dispatched. The modal's `task prompt` row shows the full body
(`prompts/*.md` once dispatched, the manifest body while pending - the
160-char `task` is only a fallback), and `prompt file` is the on-disk
path. The planner history row (plan-proposal) has no prompt file, so
its `task_full` carries the full "Propose a TOML runbook for: <raw>"
text directly - `task` stays the clipped row label. Rows stay glued to their agent across re-sorts via a run_dir|step
key, and unsaved form drafts survive the 2s modal rebuild.

**Interrupt + instruct** (`POST .../steps/<step>/interrupt`
`{instructions, attachments?}`; task-modal composer and running-row
context menu) is the in-flight steering control Stop this step lacks:
the operator's text + image refs land in a durable JSON `intrq-`
sentinel `wait_worker` polls in the same slot as a stop flag, and only
the driver acts on it - the worker's tree dies, its product is committed
like any attempt's, the request folds into the `rnote-` channel (as an
`INTERRUPT <ts>` block carrying the text and attachment paths), and the
SAME step respawns under `INTERRUPT_NOTE` + the original prompt. It is
not a retry - `attempt`/`max_attempts` are untouched, a per-interrupt
`*.interruptN` prompt/log/export set records the dispatch, and
`rec.interrupts` counts the respawns. Races resolve toward never losing
the request: landing after the turn's STATUS, during verify/teardown,
or beside a real stop flag banks it into notes for the next dispatch
instead of respawning a finished or deliberately-stopped step (a real
stop still wins the terminal status). The sentinel is deleted only
after the notes write lands, so a driver restart mid-transition applies
exactly once - `clear_run_flags` preserves `intrq-` like `rnote-`, and a
pending flag folds at the next dispatch's pre-spawn read. The endpoint
refuses a second request while one is pending (409 - no 'latest wins'
queue), dead or finished steps, and runs whose driver predates the
sentinel (`state.intrq` is stamped by runs this code drives, same scheme
as `flags:"v2"`); the UI's composer is a view change until the explicit
confirm, stays open across the 2s rebuild via keyed drafts, tracks the
transition as requested -> stopping -> resuming -> done off the row's
`intr_pending`/`interrupts` fields, and degrades to a tombstone if the
step ends while it is open.

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
     The mirror guard for OUR writes is `_assert_unwatched` (`_watchers_of`
     collects every live step whose work_dir contains a destination); its
     409 names the owner - `<run-dir>:<step>` plus slug + short session id
     via `_watcher_identity`, sorted, capped at 3 + "+N more" - and the
     same label feeds the launch project-overlap guard and
     `_any_live_step_in`'s deferred-merge block.
6. **`rc 0` proves nothing.** A `devin -p` worker can exit 0 before the ACP
   turn starts (see trap 5) or print `STATUS` only at the end of a real
   turn. `run_step` now marks `rc==0` + missing `STATUS` as `failed`,
   never `done`. The note says "no-op run" only when the export shows
   zero tokens - a worker that ran 2h of LLM steps and committed real
   work can still end its turn mid-report without a STATUS line (hit
   2026-10-15, apartment-3d wall placement: committed dc5edab then the
   log truncated mid-sentence). `--resume` prepends the interrupted-
   state note to `failed` steps too - partial work may be committed.
   `tail_status`/`tail_next`/`tail_outcome` scan the WHOLE stdout log
   (once, at step end) - an 8KB tail window misses a `STATUS:` that
   precedes a long closing report. A `STATUS:` inside Markdown prose or
   a bullet still doesn't count - the regex is anchored `^STATUS:` at
   column zero. `OUTCOME:` shares the convention (column-zero, last
   match wins, missing never fails) - it lands on `rec['outcome']` for
   the dashboard/summary, not on `STATUS` parsing.
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
   - The verify/merge window: after a worker exits, the conductor spends
     minutes in `verify` + merge while the record still says `running`
     with a dead worker pid - it displayed `lost`, and a startup sweep
     wrote `lost` + `ended_at` over a LIVE run (ux-controls, 2026-10-15).
     Fix: `state.driver_pid`/`driver_started_at` records the conductor
     itself (restamped on resume); `driver_alive` keeps such records
     live, makes sweep skip the run entirely, and blocks resume/double
     drivers. Without it, `effective_statuses` falls back to pid-only.
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
    dashboard. The dashboard's power button (top bar) wraps this safely:
    **Stop** shuts `serve` down, **Restart** spawns a detached replacement
    on the same port (the new process retries the bind until the old one
    frees it) - live runs survive either way per the rule above. The
    confirmation dialog previews `GET /api/serve/power`: active merges
    (per-repo `.git/conductor-merge.lock` + machine-global claims) render
    RED since a restart mid-merge can strand a checkout, live runs/steps
    render as a note (they keep going; only in-flight launches or merges
    a serve driver was about to fire are delayed). The dialog never
    blocks - it warns. All dashboard confirms/alerts route through
    `askConfirm`/`askAlert` (themed overlays, not native `confirm()`/
    `alert()`); `closeDetail` stays synchronous for clean transitions and
    returns a Promise only when a dirty form needs the prompt, so
    callers use `closeThen`/`await` accordingly.
16. **Every backslash inside `_DASH_HTML` is double-trapped.** The dashboard
    HTML/JS lives in a plain Python `"""` string, so a `\\` written in the
    source reaches the browser as a single `\`. Hit 2026-10-15: the JS
    path splitter `/[\\/]/` arrived as `/[\/]/` - matched `/` only - so
    Windows run dirs POSTed their full `C:\...` path and every lifecycle
    button 404'd ('Invalid run dir'), while `git`/`pytest` happily passed
    the mangled-but-valid JS. When a JS/regex/CSS literal in
    `_DASH_HTML` needs a backslash, either write `\\\\` (browser sees `\\`)
    or dodge the literal entirely - `String.fromCharCode(92)` is what
    `base()` uses. The same rule eats `\'`: Python delivers a bare
    apostrophe into a single-quoted JS string (hit 2026-10-17 - one
    `\'` broke every node-harness test at once). Prefer double-quoted
    JS strings for prose with apostrophes, and always `node --check`
    the EXTRACTED `<script>` (parse `_DASH_HTML`, not the raw source).
    Verify with `grep -n '\\\\' conductor.py` and mentally
    halve what you see; the Node harness tests catch breakage only if a
    test exercises that code path.

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
