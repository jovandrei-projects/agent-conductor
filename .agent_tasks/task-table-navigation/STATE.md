# Task: task-table-navigation

**Status:** done
**Started:** 2026-10-16   **Last touched:** 2026-10-16

## Objective

Agent-task table (the per-project `#agents` tables on :8773): the run name
became a filter button (existing `selectedRun` filter, keyed by
`run_key||run_dir`), the step name is the task-modal button, and the
whole-row click no longer opens the modal. The row whose details are on
screen keeps a `.dsel` highlight mark (task modal, run details, request /
summary views), moved by modal prev/next and cleared on close.

## Decisions

- New targets reuse existing delegated hooks: `data-agent-detail` (opens
  task modal by agentKey) and `data-filter-run` (sets selectedRun). The
  `data-filter-run` dispatch moved to `closest()` like the other attrs so
  inner markup can't miss it.
- Row stays focusable (Shift+F10 menu, Enter/Space = details) - that is a
  deliberate row activation, not the arbitrary-click affordance removed.
  `cursor:pointer` dropped off `tr.agen` (row itself no longer clickable);
  `tr.agen:focus-visible` joined the shared focus-ring rule.
- `.dsel` is keyed by agentKey/runKey via new `data-akey` on `tr.agen` -
  never row index - so re-sorts/filters/refreshes can't mark the wrong row.
  `selKeys()`/`applySel()` run at render end and on every modal open/close
  (closeDetail clears; showDetail/showRunDetails/openRequest/openQuestions/
  openSummary/openRunSummary mark). Agent rows mark on `a:<agentKey>`, run
  rows on `r:<runKey>`; request/questionnaire modals mark `r:request:<id>`,
  run summaries `r:<runKey>` (summaryView 'run|' prefix).
- A task row with no run association renders a plain-text run cell - there
  is no filter target to apply.

## Phases

### Phase 1 - implementation

- [x] CSS: `.dsel` styles + `tr.agen` loses pointer cursor
- [x] `tr.agen` markup: `data-akey`, step-name `data-agent-detail` button,
      run-name `data-filter-run` button (plain text when no run assoc)
- [x] Click handler: `data-filter-run` -> closest(); deleted `tr.agen`
      click->showDetail catch-all; ctx 'Open details' title reworded
- [x] `selKeys`/`applySel` + call sites (closeDetail, showDetail,
      showRunDetails, openRequest, openQuestions, openSummary,
      openRunSummary, render)
- [x] Updated `test_modal_edit_form_locks_when_step_starts` (was row-click)
- [x] New `TaskTableNavTests` (2 tests): click targets + no-row-click,
      closest() dispatch on inner markup, missing-run plain cell,
      keyboard row activation, nav-moves-mark, re-sort/filter/unfilter,
      close clears, run-details mark
- [x] `python run_tests.py` 322/322 green + `conductor.py demo` green

## Where it stopped

Done. `tr.agen` carries `data-akey`; the step name is a
`linkbtn step-name` (`data-agent-detail`), the run name a
`linkbtn run-link` (`data-filter-run` = run_key||run_dir). The delegated
click handler's `tr.agen` catch-all is gone and `data-filter-run` dispatches
by `closest()`. `.dsel` (same palette as run `.selected`) marks the row
whose modal is open, keyed not indexed. 322/322 mock tests + demo green.

## Verification

`python run_tests.py` (mock-only suite) + `python conductor.py demo`.
