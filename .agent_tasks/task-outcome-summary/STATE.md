# Task: task-outcome-summary

**Status:** done
**Started:** 2026-10-16   **Last touched:** 2026-10-16

## Objective

Workers' completion report gains a third contract line, `OUTCOME:` - a
one-to-two-sentence human-facing summary of what THAT step actually did
(achievements + material limits). It is parsed like `NEXT:`, persisted on
the step record (`outcome` latest + `outcomes` bounded history), shown in
the task modal and the agents-table result column, and honest when absent
(historical steps: "not reported", never fabricated).

## Not in scope

- No external summarization service - the worker writes the line itself.
- The planner's own contract (BEGIN_MANIFEST..STATUS) is unchanged; planner
  rows carry `outcome: None`.
- Sibling run-review-ux steps (review indicators, nav, timing, merge
  guards) belong to other wave members - do not touch them.

## Decisions already made

- `OUTCOME:` single line, last-match whole-log scan, 300-char cap (same
  shape as `NEXT:` - one logical line that wraps to ~2 display lines).
- Missing OUTCOME never fails a step - only STATUS gates (compat).
- `rec["outcome"]` = newest report; `rec["outcomes"]` = list of
  {outcome, ended_at, status, attempt}, dedupe-on-repeat, cap 8. A
  dispatch reporting nothing keeps the prior outcome.
- Modal: `outcome` kv row + `earlier` rows for prior attempts; the old
  `result` label (which showed the NEXT line) becomes `next`.
- Agents-table last column header `note` -> `result`: outcome is the
  primary text, conductor `note` drops to a `.sub` second line; rows with
  no outcome show the note as before.
- `step_summary_md` gains `- **outcome:**` + earlier bullets; `cmd_status`
  prints it; mock_devin prints it (`outcome=`/`nooutcome`/`status=`
  keywords drive fixtures).

## Phases

### Phase 1 - reporting path

- [x] PREAMBLE requires OUTCOME (STATUS/NEXT unchanged); OUTCOME_RE +
      `tail_outcome()` + `_record_outcome()` in conductor.py
- [x] run_step: per-attempt capture, `rec["outcome"]`/`rec["outcomes"]`,
      note/merge paths untouched
- [x] mock_devin prints OUTCOME (+ keywords)

### Phase 2 - surfaces

- [x] collect_dashboard agent rows carry outcome/outcomes (None on
      synthetic rows)
- [x] Task modal outcome/earlier/next rows + muted "not reported" state;
      agents-table result column; step_summary_md + cmd_status +
      FOLLOWUP_STEP mention

### Phase 3 - tests + docs

- [x] New tests: parse, attribute, success/partial, missing, resume
      updates, merge-note preserve, summary bullet, escaped render
- [x] `python run_tests.py` + `conductor.py demo` green (307/307)
- [x] AGENTS.md/QUEUE.md/efficiency_stats updated; commits grouped

## Where it stopped

Done - committed on branch conductor/20261016-100852-run-review-ux/
task-outcome-summary; the parent merges the worktree.

## Verification

`python run_tests.py` (unittest suite, mock-only) and `python conductor.py demo`.
