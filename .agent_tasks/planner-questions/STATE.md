# Task: planner-questions

**Status:** in progress
**Started:** 2026-10-15   **Last touched:** 2026-10-15

## Objective

The request form gains an "ask me questions if needed" checkbox. When checked
the planner MAY reply `BEGIN_QUESTIONS…END_QUESTIONS` (JSON) instead of a
manifest; generation then pauses at stage `questions`, the dashboard asks the
human one question per modal (option buttons + free text + skip, like the
ask_question tool), and a second planner pass refines the TOML with the
answers. Unchecked / no questions = today's behavior, unchanged.

## Not in scope

- Questions during a *run* (workers stay unattended; this is planner-only).
- Multi-round questionnaires are allowed but capped (MAX_QUESTION_ROUNDS).

## Decisions already made

- Two planner passes, never an in-session wait: `devin -p` cannot block on a
  human (stall_min would kill it), and "state handoff is not a conversation"
  is the architecture. Pass 1 emits manifest XOR questions; pass 2 gets the
  Q&A block and may still emit questions (permission lives on `rec.ask`),
  capped at 3 rounds.
- `pending_questions`/`qa`/`qa_rounds` persist in the request JSON - the
  questionnaire survives server restarts and closed browsers.
- `ask` is generation-time permission only: no TOML key, never marks a
  proposal stale (same class as plan_model/plan_effort).
- Manifest wins if a planner emits both BEGIN_MANIFEST and BEGIN_QUESTIONS.

## Phases

### Phase 1 - server side

- [x] `ask` bool in `_request_fields`; `PlannerQuestions`; `_extract_questions`
      sanitizer; `_planner_response(qa=)` prompt blocks
- [x] `_finish_generation` catches PlannerQuestions -> status `questions`;
      `answer_request` endpoint + `/api/requests/<id>/answer` route;
      stage gates (save/apply/generate/discard); `start_generation` clears
      Q&A state; `_publish_proposal` clears pending; run row gets
      `questions` count

### Phase 2 - dashboard

- [x] `rq-ask` checkbox + requestFields; STAGES/COLORS/.stage.questions;
      run-row "waiting on answers" sub-line
- [x] questionnaire modal (options + free text + Skip + Skip all), auto-open
      once per question set, Answer button on the request form, qa summary

### Phase 3 - verify + docs

- [x] mock tests for the whole flow + Node harness modal test
      (10 RequestWorkflowTests + 1 Node harness)
- [x] `python run_tests.py` 193/193 + `python conductor.py demo` green;
      AGENTS.md paragraph; commits

## Where it stopped

Feature complete and verified. Pending: commits + efficiency_stats row.

## Verification

`python run_tests.py` and `python conductor.py demo`
