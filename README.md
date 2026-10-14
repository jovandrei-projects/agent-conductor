# agent-conductor

`conductor.py` runs a TOML manifest of steps as a sequence of fresh Devin
CLI sessions - one `devin -p` subprocess per step, in the target project's
directory. It automates the loop a human otherwise drives by hand (new
session, paste prompt, wait, repeat), so multi-phase work can run overnight
without one bloated context window or per-message cost growth. State hands
off through the project's own `AGENTS.md` + `.agent_tasks/` files, not a
conversation; conductor adds git checkpoints before and after each step, a
run log, verify gates, and a `STATUS:` line contract. Python stdlib only.

## Manifest

```toml
[run]
name = "night"               # run dir: data/runs/<ts>-<name>/
on_failure = "stop"          # "continue" keeps going past failed steps
checkpoint = true            # git commit the project before AND after each step
# max_hours = 8              # optional wall-clock cap for the whole run
# max_parallel = 2           # concurrent agents; default ~1 per 4 cores, max 4

[defaults]                   # inherited by every step, overridable per step
model = "swe"
mode = "bypass"              # unattended: nobody to answer prompts
timeout_min = 60

[[step]]
name = "next-queue-task"
project = "video-tools"      # slug -> path + cloud_ok, from projects.toml
prompt = "Pick up the next task in .agent_tasks/QUEUE.md and work it to done or blocked."

[[step]]
name = "test-sweep"
project = "video-tools"
prompt = "Run the test suite and fix failures."
verify = "python run_tests.py --quick"   # cmd.exe gate; nonzero exit = step fails
timeout_min = 90

# Adjacent steps sharing `wave = N` run in parallel up to the cap;
# same-project wave members need `worktree = true` (own branch, merged back).
# `cloud = true` spawns `devin --cloud -p`; requires cloud_ok in projects.toml.
```

Fully annotated manifest: `examples/night.example.toml`. Real manifests
live in `manifests/`: hand-written `<project>-<what>.toml` and dashboard-generated
`<YYYYMMDD-HHMMSS>-<random>-<run>.toml` files stay flat (see its README).

## Dashboard requests

Open `python conductor.py serve` on `http://127.0.0.1:8773` and choose
**New request**. Enter your raw task, optionally pick a registered project,
model and effort, then **Save draft** (no Devin session is started). **Generate
proposal** starts a planning-only local Devin CLI session; it produces a
reviewable TOML manifest with a run description and proposed agent steps.
The runs table distinguishes gray drafts from purple **Ready to run** plans;
planned agents have no start time, session or token counts. Once launched,
steps not reached yet remain visible as **waiting** (or **skipped** if the run
ended early), until real step records replace them. The default is SWE-2 Max
through 2026-10-16, then GPT-6 Luna Max; both are editable.

Review or edit the request and TOML in the modal, save changes, regenerate
steps, or discard the proposal and its planned steps. Editing the raw request
or model after generation marks the plan stale until regenerated or explicitly
edited and saved as TOML. **Nothing implements the request until you separately
click Start run**; afterward `state.json` drives the usual live run/agent
progress, and the original request remains viewable. Raw drafts live only in
gitignored `data/requests/`; accepted TOML lives in `manifests/`. Clicking a
run row filters both tables; click its filter chip × to clear it.

## Commands

```
python conductor.py plan <manifest.toml>           validate + show resolved steps
python conductor.py run  <manifest.toml>           execute -> data/runs/<ts>-<name>/
python conductor.py run  <manifest> --resume [dir] skip done steps (default: latest)
python conductor.py status [run_dir]               step table of the latest run
python conductor.py serve [--port 8773]            live dashboard, loopback only
python conductor.py sweep                          re-mark orphaned records 'lost'
python conductor.py sessions [--rm] [--keep-hours N] list/clean spawned sessions
python conductor.py demo                           self-test vs tests/mock_devin.py
                                                   (fixture under data/demo-project/;
                                                   run appears in data/runs/)
python run_tests.py                                unittest suite (mock, no auth)
```

Run flags: `--devin "<cmd>"` binary override, `--no-hub`, `--no-keep-awake`,
`--max-parallel N`.

Requires `devin` on PATH with `devin auth login` done once in a real
terminal. Ground rules and traps: `AGENTS.md`. Phase status: `ROADMAP.md`.
