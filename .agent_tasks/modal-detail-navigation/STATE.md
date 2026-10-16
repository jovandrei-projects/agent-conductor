# Task: modal-detail-navigation

**Status:** done
**Started:** 2026-10-16   **Last touched:** 2026-10-16

## Objective

Wave step of run `20261016-013719-workflow-ux-safety`. Stop opening the
step summary in a separate browser window: render it inside the dashboard's
shared modal with the same prev/next chain the task modal uses, and give
modal-to-modal jumps (task -> request/runbook, request -> run details) a
real Back trail that restores the originating view, scroll included.

## Decisions already made

- The server hands the modal just the report body: `?frag=1` on the
  existing `/api/runs/<run>/steps/<step>/summary` route returns
  `_summary_body(md)` (the inner markup, same escaping) - `?raw=1` markdown
  and the standalone page are untouched. No client-side markdown renderer.
- The summary link stays a real `<a target="_blank" href=...>`: the plain
  click is intercepted (`data-summary`), ctrl/middle-click still opens the
  page, and the modal keeps an "Open as page ↗" escape hatch. Direct
  /summary URLs keep working.
- One shared `#detail` modal + a `modalHist` stack of view snapshots
  (`{kind: agent|run|request|summary, key|id, scroll}`). Sibling walks
  (dnav arrows, summary prev/next, refresh re-render) replace the view
  without stacking; only modal-*type* jumps push. Escape/dx/backdrop still
  close everything and clear the stack.
- `openRequest`/`showRunDetails`/`openSummary` gained a `keepStack` flag;
  a non-null `snapshotModal()` implies a modal->modal jump and forces
  keepStack, so older Back entries survive nesting. The questionnaire stays
  out of history (transient sub-dialog, owns its own teardown).
- `modalScroll` carries the restore offset across async fetches; fresh
  opens land at top.

## Verification

`python run_tests.py` 264/264 green (was 262) + `python conductor.py demo`
green. New: `ModalDetailNavTests` - HTTP-level `?frag=1`/`?raw=1`/page/404
coverage and a node-VM harness walking summary prev/next, arrow keys,
end-of-list disables, task->request->run->Back->Back, missing-item notice,
scroll restore, and Escape teardown.

## Where it stopped

Done. Not merged - the wave's closing steps own merging back to master.
