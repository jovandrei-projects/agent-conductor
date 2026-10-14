# manifests/

Real run manifests live here - one TOML per job, flat, named
`<project>-<what>.toml` (e.g. `video-tools-cjk-subs.toml`,
`spt-batch-chapters.toml`). `examples/` is for tests and reference only;
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
