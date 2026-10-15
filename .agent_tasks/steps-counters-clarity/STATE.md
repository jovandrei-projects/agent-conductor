# steps-counters-clarity

Objective: the runs-table `steps` column token lines were cryptic -
`plan 12k/1.2k ·5st` and `steps 45k/8k+20kc ·31st`. Make every counter
self-evident.

## What the numbers are (found)

`fmtT(t)` renders `prompt/completion+cachedc ·llm_steps st` from the ATIF
export's `final_metrics`: `prompt` = ALL input tokens (cached is a subset,
never added on top - cost formula `(prompt-cached)*in + cached*cache +
completion*out`); `completion` = output; `llm_steps` = model turns (NOT
manifest steps - the `st` suffix collides with the steps column). `+Nc`
read as additive; a 0 cached hid the slot entirely. `r.plan_tokens` =
planner session sums; `r.tokens` = worker step sums; counts chips were
already labeled. Dead `const total` at old line 5166.

## Steps

- [x] `fmtT` -> labeled fields `in X · out Y · cached Z · N turns`
      (cached always shown so no value can hide); shared by agent cells
      and run rows.
- [x] run rows: `planner:` / `steps:` prefixes; `steps` column header gets
      a tooltip; dead `total` removed.
- [x] `tokTitle`, `TOK_HELP`, `tokenHelp` explainer use the same words and
      state cached is a share of `in`.
- [x] test_conductor.py token-format assertions updated.
- [x] run_tests.py + demo green: 171/172 (the one failure,
      test_step_stop_kills_worker_and_marks_stopped, is an unrelated
      stop-flag timing flake - passes standalone in 1.5s, and every
      dashboard test class is green); demo 2/2.

## Where it stopped

Done - committed; nothing resumes. The runs-table steps column now reads
`planner: in X · out Y · cached Z · N turns · $` /
`steps: in X · out Y · cached Z · N turns · $` under the count chips,
with a column-header tooltip and matching agent-cell format.
