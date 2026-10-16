# Task: emulator-resource-ownership

Conductor wave step (run 20261016-013719-workflow-ux-safety). Determine how
concurrent mobile steps allocate/target Android devices, then implement the
minimum reliable coordination if unsafe: serial assignment, cross-run
ownership, queueing on a shared device, safe lifecycle/cleanup.

**Status:** in progress   **Started:** 2026-10-16   **Last touched:** 2026-10-16

## Findings (evidence, not assumptions)

- **conductor.py had zero device awareness** - no manifest key, no env
  propagation, no locks. Devices were entirely worker-side prompt business.
- **Workers pick devices ad hoc.** run `20261014-235630-apartment-play-edit`
  ran all mobile steps sequentially (chained timestamps) so no collision
  occurred *there*, but workers chose targets themselves: `adb -s
  emulator-5554` hardcoded + `$env:ANDROID_SERIAL='emulator-5554'`
  (mobile-parity-validation export, 71 adb-bearing tool calls), then the
  physical handset `RFGL11NN0QV` via `adb -s RFGL11NN0QV install`.
- **A worker killed the shared emulator as cleanup.** In
  `mobile-parity-validation.attempt2.export.json`:
  `adb -s emulator-5554 emu kill; Start-Sleep -Seconds 3; adb devices -l`.
  Any concurrent worker's session on that emulator would have died.
- **Host-port collisions are a second hazard** (documented, not solved):
  workers run `adb -s <serial> forward tcp:9222 localabstract:...` - a
  fixed host port on the shared adb server collides across workers even on
  different serials.
- **Concurrent mobile steps DID overlap across runs**: state.json shows
  `apt:mobile-editor-and-placement` 03:00-05:33 vs
  `mc:mobile-visualizations-and-filters` 02:10-03:07 +
  `mc:verify-and-polish` 03:07-03:36. No collision only because
  model-compare verified at compile level and never called adb.
- **Machine reality**: 4 AVDs (Galaxy_S25_Ultra_API35,
  Galaxy_S26_Ultra_API36, Pixel_3a_API34, Pixel_8a_API35); adb/emulator not
  on PATH - SDK at `%LOCALAPPDATA%\Android\Sdk`.
- **apartment-3d tooling already honors `ANDROID_SERIAL`** (and adb honors
  it natively) -> env propagation is the right mechanism.

## Concurrency guarantee (implemented)

- `device = "<serial>" | "any" | "emulator" | "physical"` (also `true` =
  any; `[defaults]` and a `projects.toml` entry can carry it like
  `verify`; `device + cloud` = manifest error): the driver leases one adb
  serial machine-wide in `%TEMP%/agent-conductor-devices/<serial>.json`
  (O_EXCL + token + owner/worker pid liveness - merge-lock shape;
  owner_started_at is the owner's own creation stamp so a slow
  interpreter start can't make a live owner look dead). Held for the
  whole step incl. attempts, interrupt respawns, verify.
- Busy-but-attached -> step queues `queued`/`device_wait` uncapped; never
  attached -> `blocked` after `device_wait_s` (600, 0 = forever). Halt
  sentinels (stop/pause/drain/skip) work while queued; prompt-edit /
  resume-notes / interrupt sentinels re-fold after the wait.
- Worker + verify get `ANDROID_SERIAL` + `CONDUCTOR_DEVICE_SERIAL`; prompt
  gets a CONDUCTOR DEVICE note (owns serial, don't touch others, never
  kill anything incl. own - conductor owns lifecycle).
- `device = "emulator"` (or `any`+`device_boot`) boots a free AVD on a
  free even console port where the emulator binary + AVDs exist;
  `avd-<name>` slot leases stop double-boots, `emu avd name` excludes
  already-running images; lease `booted`+`emu_pid` -> release does
  `emu kill` -> image-name-checked taskkill fallback. Stale-lease break
  reaps a recorded booted orphan the same way. Never stops what we
  didn't boot.
- events: device_wait/acquired/booted/released/blocked/reaped;
  state.json: device/device_spec/device_booted/device_wait/device_waited_s;
  dashboard agent row + modal `device` field; plan tag `[device=spec]`.
- adb/emulator discovery: `CONDUCTOR_ADB`/`CONDUCTOR_EMULATOR` env, PATH,
  then `ANDROID_HOME`/`ANDROID_SDK_ROOT`/`%LOCALAPPDATA%\Android\Sdk`.

## Phases

- [x] Investigation + evidence report (above)
- [x] Lease registry + adb/emulator discovery seams
- [x] run_step: acquire -> prompt/env -> spawn/verify -> release; wait queue
- [x] Manifest parse + validation (device/cloud conflict, serial shape,
      project-registry inherit)
- [x] Dashboard surface (rec.device/device_wait + modal kv + plan tag)
- [x] Tests: 14 DeviceOwnershipTests - competing projects, explicit
      targeting, cancellation, stale recovery, unavailable, boot+image-guard
      reap, verify env, manifest validation, no-device regression
- [ ] Full suite green + commit + notes

## Where it stopped

All code + tests landed; final full-suite run in flight, then commits.

## Deferred or blocked

- adb-server host-port namespaces (`forward tcp:9222`) NOT coordinated -
  workers on different serials must pick free host ports. Noted in
  AGENTS.md as a known gap.
- Fairness between same-machine waiters is mutex-order, not FIFO.
- No cloud/device provisioning (task rule).
- `device` is not a request-form/planner option field (manifest/registry
  only) - can be added to OPTION_FIELDS if operators want it in the UI.

## Verification

`python run_tests.py` (+ `python conductor.py demo` - passed 2026-10-16)
