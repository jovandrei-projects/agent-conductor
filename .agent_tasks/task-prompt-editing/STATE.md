# Task: task-prompt-editing

**Status:** in progress
**Started:** 2026-10-15   **Last touched:** 2026-10-15

## Objective

Dashboard agent modal: edit a step's prompt while it has never been
dispatched (waiting/queued/skipped); once running/done/dispatched the
control locks, incl. a form open mid-start. For ran-but-unfinished steps
(stopped/failed/timed_out/lost/blocked/partial) a 'resume notes' field
appends operator text to the next dispatched prompt instead of editing
the original. Modal 'task' field shows the full prompt text (was
160-char/regex-trimmed); 'prompt' field is relabeled 'prompt file'.

## Decisions already made

- Overrides/notes are tempdir sentinels (`prompt-`/`rnote-` prefix next
  to skip-/timeout- in %TEMP%/agent-conductor-flags) - trap-5 safe, and
  durable like skip- so `clear_run_flags` on resume does not erase them.
- run_step folds both into `body_src` before render: override replaces
  the manifest body, RESUME_NOTES block appends after it; retries and
  resumes reuse the same composed body; the dispatched prompts/*.md
  records exactly what was sent.
- Editability gate is STORED status queued|skipped (or no rec) - those
  are the never-dispatched states; the endpoint 409s post-dispatch so a
  mid-start save can't win a race.
- Modal drafts survive the 2s re-render (promptDrafts/notesDrafts +
  focus/selection restore); detailIdx re-resolves by run_dir|step key so
  the modal stays on the same agent when rows re-sort.

## Phases

### Phase 1 - backend

- [x] prompt_flag/rnote_flag helpers + clear_run_flags exemption
- [x] run_step: apply override + resume notes, provenance on the rec
- [x] task_label full mode: keep newlines, stop at the real trailer
- [x] _manifest_bodies + collect_dashboard fields (prompt_editable,
      notes_enabled, prompt_override, resume_notes, prompt_edited,
      task_full for pending rows)
- [x] set_step_prompt / set_step_notes endpoints + routes

### Phase 2 - dashboard UI

- [x] modal 'task' -> 'task prompt' full text in <pre>, 'prompt' ->
      'prompt file'
- [x] Edit prompt block + Revert; Resume notes block; mid-start lock
      notice keeping the unsaved draft visible
- [x] draft preservation + same-agent re-resolution across re-renders

### Phase 3 - tests + docs

- [x] 8 new tests (endpoint gating, dispatch composition, dashboard
      fields, task_label, Node-harness modal lock); 172/172 + demo green
- [x] AGENTS.md 'Running things' blurb + QUEUE.md row

## Where it stopped

Done - dispatched in worktree conductor/20261015-182057-ux-controls/
task-prompt-editing; conductor merges the wave branch.

## Verification

`python run_tests.py` (mock suite) and `python conductor.py demo`.
