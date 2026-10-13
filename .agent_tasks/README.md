# Agent task workspace

State that belongs to a piece of work in progress, rather than to the
repository. It exists so a session can end at any moment without the plan
dying with it, and so the next session can resume from a short file instead of
re-deriving everything from the code.

Read `QUEUE.md` to find out what to work on. Read that task's `STATE.md` to
find out where it stopped. That is the whole protocol.

## Layout

```
.agent_tasks/
  README.md              this contract
  QUEUE.md               every task, in intended order, with one-line state
  efficiency_stats.md    token cost per session, appended at the end of each
  <task-slug>/
    STATE.md             objective, phases, checklists. For humans. Short.
    scratch/             for agents only. Gitignored. Terminal dumps, logs,
                         raw measurements, anything long
```

`STATE.md` is the only file a task must have. Add more top-level files in a
task folder only when a human is meant to read them; everything else goes in
`scratch/`.

`scratch/` is gitignored on purpose - it is the same category as `data/`.
**A finding that only exists in `scratch/` is lost.** When something in there
turns out to matter, write the conclusion into `STATE.md`, or into
`ROADMAP.md` if it is a measurement.

## What this workspace does not own

Each fact lives in one place. This workspace adds a third kind of file and
does not displace the other two:

| Question | File |
|---|---|
| Which task should be picked up next at all? | `.agent_tasks/QUEUE.md` |
| What is the next step of the work I am doing now, and what is done? | `.agent_tasks/<slug>/STATE.md` |
| What is the phase list, its status, and what did each phase find? | `ROADMAP.md` |
| What are the project's purpose, ground rules and traps? | `AGENTS.md` |

So a `STATE.md` step is a step of the work, not a roadmap item. When a step
lands and it moves a roadmap checkbox, tick both, in the same commit. Do not
copy measurements into `STATE.md` - reference the roadmap section that holds
them.

## Rules

1. **Check `QUEUE.md` and the `STATE.md` of any task that is not `done`
   before starting.** Do not open a second task while one is `in progress`
   unless the user says so.
2. **Tick the box when the step is actually finished and verified.** Not when
   the code is written. If a step half-landed, leave it `[ ]` and say what
   remains underneath it.
3. **Work in order where the order is real.** It often is not. If you finish
   something that was filed under a later phase, tick it there now and note
   the date - a later phase that is already fully ticked is simply done,
   which is the intended outcome, not an accident to fix.
4. **Long output goes to `scratch/`**, then read the file. Do not paste stack
   traces, query dumps or full test output into the conversation.
5. **Update `STATE.md` before the session ends.** Also update it at the
   moment a step changes state or a decision is made, because the session may
   not get an orderly end.
6. **Append to `efficiency_stats.md` at the end of the session.**
7. **Create a task folder only for work that will outlive one session.** A
   contained fix does not need one, and an empty ceremony folder costs tokens
   in every future session that reads the queue.

## Keeping this cheap

The point of the workspace is to spend fewer tokens than re-reading the
codebase would. It stops paying if the files themselves grow large, because
these are the files every session reads first.

- `STATE.md` should stay at roughly one screen. Checklists, not narrative.
- A step is one line. Reasoning that runs past two lines belongs in
  `ROADMAP.md` or `scratch/`.
- Prune finished detail rather than accumulating it: once a phase is fully
  ticked, it can collapse to a single done line. The history is in git.
- `QUEUE.md` holds one line per task. Not a summary of each task.
- Do not restate anything already in `AGENTS.md` or `ROADMAP.md`.

If this structure starts costing more than it saves - which is a real
possibility and worth watching - say so and propose the change instead of
quietly following it.
