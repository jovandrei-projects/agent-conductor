# cumulative-elapsed-time — state

Wave step of run `20261016-100852-run-review-ux` (see
`.agent_tasks/run-review-ux/DESIGN.md` §1.4 + per-step touchpoints in the
main repo). Objective: elapsed time must be cumulative across resumes —
each `run_step` dispatch and each driver session is its own recorded
interval; views show the sum (plus the live interval while running),
never just the last dispatch's span. Run totals use driver intervals,
never a sum of overlapping step spans.

Session window: 2026-10-16 ~13:58 → (running)

## Schema (decided)

- Step rec `exec_intervals`: `[{start, end|null}]`, one per dispatch —
  appended at dispatch stamp, closed at terminal write. `elapsed_s` stays
  the closed-cumulative cache written at terminal. `first_started_at`
  preserves the original start (`started_at` stays latest-dispatch for
  pid/liveness windows). Legacy rec seed once: `{start,end}` pair, or
  `{start, end=start+elapsed_s}`, or `{dur: span}` when endpoints are
  gone — never fabricated endpoints.
- Run state `exec_intervals`: driver intervals — opened at create/resume
  (`driver_started_at`), closed wherever `ended_at` is stamped; a stale
  open interval closes at last-witness (newest state/step/event stamp).
- `rec_elapsed_s(rec, now)` / `run_elapsed_s(st, now)` = the one
  authoritative calc; `_run_elapsed_split` → `(elapsed_s, exec_start)`
  for the payload (closed sum + live start so the client ticks without
  sig churn).
- `conductor._now()` is the clock seam: `now_iso()` + all elapsed math
  route through it; liveness probes (`step_live`, `pid_alive`,
  `wait_worker`) keep real `time.time()`.

## Steps

- [x] failing regression tests first (fail→resume, multiple resumes,
     stop→restart, completed, reload, concurrent run total, missing
     history, JS tick math) — `CumulativeElapsedTests`, 9 cases, all
     confirmed failing pre-implementation (elapsed_s reset to the last
     dispatch's span on resume)
- [x] helpers + `_now()` seam (`_iso_from`, `_step_seed_intervals`,
     `_step_last_write`, `_iv_open`/`_iv_close`, `rec_elapsed_s`,
     `_run_witness_ts`, `_run_iv_close`, `_run_elapsed_split`,
     `run_elapsed_s` — conductor.py ~1198-1468)
- [x] run_step dispatch/terminal + skip/fail + sweep-lost wiring; run
     interval open/close at create/resume/end/auth-abort/halt/sweep
- [x] views: cmd_status (run elapsed + rec_elapsed_s per step),
     step_summary_md (elapsed + first-started), _elapsed_md,
     run_report_md (run elapsed line), collect_dashboard (agent
     first_started_at + run elapsed_s/exec_start), hub_update
- [x] JS: cumulative `elapsed()` + `runElapsed()`, runs-table elapsed
     column + `data-rel` ticking via tickElapsed, run modal + task
     modal elapsed/first-started rows
- [x] suite + demo green; QUEUE.md DONE row; AGENTS.md schema note

## Where it stopped

DONE — merged to master 2026-10-16 (merge d99c4e4) via merge-resolver
session after the wave merge conflicted on QUEUE.md bookkeeping appends,
the ended_at/run_elapsed stamp sites, dashboard payload fields, and the
shared-harness class tails in tests (kept both sides; 446/446 suite
green on the merge).
