# Task: mobile-relaunch-handling

**Status:** done
**Started:** 2026-10-16   **Last touched:** 2026-10-16

## Objective

An intentional, authorized app restart/reload on a device-leased step can
never again wait indefinitely on the WebView 'Confirm Navigation' (Leave
this Page / Stay on this Page) dialog: the conductor ships a bounded
helper + worker guidance that handles the known confirmation, fails
actionably on unexpected dialogs, and stays scoped to the leased serial.

## Not in scope

- Editing apartment-3d (the app project) — follow-ups for it are recorded
  in AGENTS.md + below, not implemented.
- Generic dialog handling beyond the known beforeunload confirmation and
  foreign-package modal detection.
- Changing the app's `beforeunload` guard itself — it is a legitimate
  protection, never to be disabled from automation.

## Decisions already made

- Responsible party (traced): the worker's own harness `location.reload()`
  on a page dirty with its own test edits (apartment-3d
  `tools/device-persist-test.mjs reload()`/clean-slate), not an app
  framework. `src/saveui.js:405` `beforeunload` preventDefault is the
  legit guard; WebView renders it as a native modal that CDP cannot
  answer. Evidence: run 20261014-235630-apartment-play-edit
  mobile-parity-validation (attempt 1 burned its 180-min timeout;
  screenshot 20261016-005550-bdf92e96.png shows the dialog up at 00:55).
- Fix lives in conductor: `mobile_device.py` (stdlib-only, repo root) +
  DEVICE_NOTE paragraph + AGENTS.md trap doc. `am force-stop`+start is
  the supported restart because the dialog dies with the process.
- Consent is explicit: `--leave` asserts the dirty state is disposable
  test data or already saved; no flag = refuse with instructions; a
  foreign-owned signature or unexpected modal = diag + fail, never a tap.

## Phases

### Phase 1 - trace

- [x] Read attachment PNG + run 20261014-235630-apartment-play-edit state,
  events, stdout logs, ATIF export for mobile-parity-validation
- [x] Establish the stall mechanism (worker `location.reload()` while
  dirty -> native beforeunload dialog -> CDP dead-end; `am force-stop`
  recovery is what eventually unwedged it)
- [x] Identify the app-side guard (`src/saveui.js` beforeunload +
  pagehide stash) and confirm it must not be bypassed globally

### Phase 2 - conductor-side fix

- [x] `mobile_device.py`: serial scoping (lease env > flag, foreign
  serial refused), uiautomator parse, `confirm-navigation` (known-dialog
  handling with explicit --leave/--stay consent), `relaunch`
  (settle->force-stop->LAUNCHER start->bounded foreground wait->post
  scan), `surface` diagnostics, diag capture (uiautomator.xml +
  screen.png), exit codes
- [x] DEVICE_NOTE: helper path + the don't-leave-it-hanging guidance for
  every device-leased step
- [x] tests/test_mobile_device.py: 30 tests (known dialog accept/stay,
  no-decision refuse, no-dialog success/require, foreign-owned,
  unexpected ANR, bounded timeouts, tap failure, serial ownership/CLI)
- [x] AGENTS.md trap doc + app-side follow-up record

## Where it stopped

`python run_tests.py` green — 410/410 (30 new MobileDeviceTests + 2 new
prompt assertions in DeviceOwnershipTests). Conductor merged the branch
to master as 2e6888e (one both-append conflict on efficiency_stats.md;
both rows kept). Nothing left.

## Deferred or blocked

- App-side follow-ups for apartment-3d (recorded, not implemented): its
  `location.reload()`-based device harnesses should save/flush before
  reload or call the conductor helper; an automation-authorized guard
  bypass (launch intent extra or an awaited `__save.flushNow()`-style
  hook) would remove the dialog round-trip entirely.
- In-app (same-package) dialogs that are not the known confirmation are
  not detectable generically — documented limitation; the helper covers
  the known signature + foreign-package surfaces.

## Verification

`python run_tests.py` (adds 30 MobileDeviceTests + 2 prompt assertions
in DeviceOwnershipTests). Helper CLI is import-safe and mock-free; real
device behavior was matched from the apartment-3d run's evidence, not
re-run live.
