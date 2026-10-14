# manifests/

Real run manifests live here, flat: hand-written files use
`<project>-<what>.toml` (e.g. `video-tools-cjk-subs.toml`), while dashboard
proposals use `<YYYYMMDD-HHMMSS>-<random>-<run>.toml`. The timestamp prefix
sorts by creation; the random suffix prevents same-second collisions. Existing
manifest names do not change. `examples/` is for tests and reference only;
nothing durable goes there.

## Running one

Always from the agent-conductor root, never from the target project:

```
cd C:\Projects\agent-conductor
python conductor.py plan manifests\<name>.toml   # validate + show steps
python conductor.py run  manifests\<name>.toml   # execute
```

Inside the manifest, each `[[step]]` names a `project` slug from
`../projects.toml` (or a path relative to the manifest). The slug decides
WHERE the worker session runs - the worker's working directory is the
target project, and it reads THAT project's `AGENTS.md` + `.agent_tasks/`
per the preamble. conductor's own rules only impose the session contract:
commit work, update the project's `.agent_tasks`, end with `STATUS:`.

## Writing one

Copy `../examples/night.example.toml` - every field is annotated there.
`state.json` in each `data/runs/<ts>-<run>/` tracks what finished;
`conductor.py status` and the :8773 dashboard render it.

The dashboard's **New request** flow saves raw text in gitignored
`data/requests/`. **Generate proposal** runs only a planner in a temporary
workspace; after validation it writes a timestamped TOML here with a short
`[run].description` and shows its steps as *planned*, not *queued* or
*running*. Review/edit the TOML or regenerate it before clicking **Start
run** explicitly. Discard removes only that request's generated TOML and
planned steps; it cannot discard an already-started run. UI proposals must
use registered project slugs, a checkpoint, inline prompts, and the ordinary
cloud privacy gate. Hand-authored manifests may still use the full format.

## A concrete request -> manifest

"Overnight, have money-plan work get three features into the mobile app."
You write `manifests\mobile-apps-three-features.toml`:

```toml
[run]
name = "mobile-3features"
checkpoint = true

[defaults]
model = "swe"          # free through 2026-10-16; then gpt-6-luna
mode = "bypass"
timeout_min = 60

[[step]]
name = "plan"
project = "mobile-apps"      # slug from projects.toml - never a raw path
prompt = "Read .agent_tasks/QUEUE.md and the money-plan feature request in
docs/. Write a plan file docs/plan-3features.md breaking it into 3 steps."

[[step]]
name = "feature-1"
project = "mobile-apps"
prompt = "Implement item 1 of docs/plan-3features.md. Verify with the
project's own test command."
verify = "npm test"          # cmd.exe syntax - it runs inside the project

[[step]]
name = "feature-2"
project = "mobile-apps"
prompt = "Implement item 2 of docs/plan-3features.md."

[[step]]
name = "feature-3"
project = "mobile-apps"
prompt = "Implement item 3 of docs/plan-3features.md."
```

The worker session lands in `C:\Projects\mobile-apps` and follows THAT
project's AGENTS.md/.agent_tasks - you never tell it conductor exists.
A step in a different project is just another `project = "..."` line in the
same manifest (a run can span projects; the dashboard groups agents per
project and the runs table shows chips for every project it touched).

To trigger from a plain chat ("activate overnight mode for X"), the
`agent-conductor` skill is installed globally
(%APPDATA%\devin\skills\agent-conductor\SKILL.md) - any session can write
the manifest and launch it from the conductor root.

## Parallel runs and merges

Adjacent `[[step]]`s sharing `wave = N` run in parallel; same-project wave
members need `worktree = true`. The conductor waits for the whole wave,
then merges each branch back in manifest order. On a conflict it parks
the worktree and fails the step - unless `merge_resolver = true` ([run]
or per-step), which spawns a `resolve-<step>` session in the main
checkout mid-merge to finish it. See `lab-merge-stress.toml` for a
4-wave manifest exercising every merge case against lab-a/b/c fixtures.
