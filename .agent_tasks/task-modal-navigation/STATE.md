# Task: task-modal-navigation

**Status:** in progress
**Started:** 2026-10-15   **Last touched:** 2026-10-15

## Objective

Prev/next navigation inside the agent detail modal on the :8773 dashboard,
walking the run's pipeline order (manifest step order), with position context
("task N of M") in a sticky bar at the top of the modal, plus Left/Right arrow
keys.

## Not in scope

- Navigation across runs/projects - the pipeline is the run's own step list.
- Reordering or launching steps from the bar.
- The run-details and request modals (no seq nav there).

## Decisions already made

- `seq` rides on each agent dict from `collect_dashboard`: manifest order is
  the pipeline; recorded steps missing from it fall back to record order;
  proposal previews use `proposed_steps` index; the `plan-proposal` planner
  row gets no `seq` (not a pipeline task).
- Sticky bar follows the `.topbar` trick: `position:sticky; top:0` +
  negative margins to cover `#detail`'s padding.
- `agentKey` switches to `run_key|step` (was `run_dir|step`) so nav targets
  and detailKey can't collide across unlaunched requests sharing a step name.

## Phases

### Phase 1 - data + UI

- [x] `collect_dashboard` emits `seq` per agent (run steps, pending rows,
      proposal previews)
- [x] Sticky `.dnav` bar in `showDetail`: prev/next buttons (sibling step
      names), "task N of M"
- [x] Click handler for `[data-nav]` + ArrowLeft/ArrowRight keydown
- [x] Tests: `seq` ordering in collect_dashboard + JS harness nav test
- [x] `python run_tests.py` green (200/200) + `conductor.py demo` green

## Where it stopped

Done. `seq` rides on every agent dict (manifest order; record-order
fallback for steps the manifest lost; `proposed_steps` index for previews;
planner row gets none). `showDetail` prepends a sticky `.dnav` bar with
sibling-named prev/next buttons + "task N of M" when the pipeline has >1
step; `navStep` shares the walk with `[data-nav]` clicks and
ArrowLeft/ArrowRight (skipped while a form field is focused); nav resets
`#detail` scrollTop, the 2s rebuild does not. `agentKey` now prefers
`run_key` over `run_dir` (proposal rows have no dir - same-named steps in
different requests could no longer alias).

## Verification

`python run_tests.py` (mock-only suite) + `python conductor.py demo`.
