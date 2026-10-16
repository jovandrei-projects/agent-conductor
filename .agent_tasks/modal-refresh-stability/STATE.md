# modal-refresh-stability

Wave step of run 20261016-100852-run-review-ux. Task modal (`#detail` /
`#dbody` in `conductor.py`'s `_DASH_HTML`) must not lose scroll position
when the same task repaints on background refreshes.

## Where it stopped

DONE - fix landed, verified in real Chromium + node harness, 331/331
tests + demo green, committed on branch
conductor/20261016-100852-run-review-ux/modal-refresh-stability.
Merged to master via resolve-merge session (merge 8cb3988; seq-guard
refresh() rerouted through HEAD's stateUrl() so the trash=1 param
survives; 351/351 tests green post-merge).

## Root cause (measured, scratch/probe_scroll.py + probe_mech.py)

The initial guess (backdrop hidden round-trip destroys scroll) was
DISPROVEN live: `display:none` toggling preserves scrollTop in Chromium,
and same-content innerHTML swaps do too. The real mechanisms:

- Wholesale `dbody.innerHTML` swap leaves nothing to scroll-anchor. A
  SHORTER repaint clamps `#detail.scrollTop` permanently (it never
  recovers on regrowth - measured 1631 -> 802 stuck), and growth above
  the viewport pushes the text being read off-screen (measured: bottom-
  pinned 802 stays 802 while the tail slides ~830px down).
- `/api/state` fetches (tick + post-action refresh()) were unsequenced -
  an older response could land last and repaint a stale view.
- Bonus inconsistency found: task switches only reset scroll via
  navStep's explicit scrollTop=0 - a row/ctx open of a different task
  inherited the previous task's offset.

## What landed (conductor.py dashboard JS)

- sameView repaint path in showDetail: detailKey unchanged + no other
  modal slot open + backdrop visible -> repaint in place, skip
  closeDetail's hide/show entirely.
- detailHTML dedupe: a byte-identical repaint skips the DOM write - the
  common steady-state tick leaves scroll/selection/focus/hover alone.
- captureModalScroll/restoreModalScroll: first-viewport-visible landmark
  (id / data-asid / data-ag / .k label / h3 text + occurrence index) and
  its pixel dy -> after the swap the same landmark is put back at dy;
  numeric scrollTop fallback when the landmark is gone; #dtail's inner
  scroll carried.
- captureModalFocus/restoreModalFocus: focused control re-found by a
  stable data-* signature (FOCUS_ATTRS incl. reviewStep/reviewRun for
  the parked review branch) and refocused preventScroll; textarea caret
  carried via id; unsaved #d-timeout value carried only when the
  rendered default didn't change (server value wins).
- Deliberate-switch policy: fresh task open lands at the top
  (explicit - hidden round-trips do NOT reset scrollTop); sibling nav
  unchanged (already top); Back still restores its modalScroll
  snapshot; same-task re-click is a repaint, not a switch.
- stateReqSeq: only the latest-issued /api/state response paints; tick()
  now goes through the same refresh() (also drops the reload-on-JSON-
  error-body edge).

## Checks

- [x] probe_scroll.py against fixture serve: pre-fix RESET (clamped
  1631->802 permanent + growth left bottom-pinned user behind), post-fix
  STABLE (scrollTop follows content every cycle: 802/1631 alternating)
- [x] node-harness tests in test_conductor.py
  (ModalScrollStabilityTests x3: anchor+clamp+dedupe+focus+dtail,
  deliberate-switch policy, POST+refresh action + out-of-order drop)
- [x] python run_tests.py green (331/331) + demo green

## Notes for the merge

- FOCUS_ATTRS already names the parked review branch's data-review-step/
  data-review-run hooks, so review buttons keep focus across repaints
  once that branch lands. The 'review action' regression is covered by
  the save-notes POST+refresh round trip (identical call shape).
