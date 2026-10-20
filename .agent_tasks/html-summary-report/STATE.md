# Task: html-summary-report

**Status:** in progress
**Started:** 2026-10-20   **Last touched:** 2026-10-20

## Objective

The end-of-run summarizer session produces a SECOND artifact,
`run.summary.html` (alongside `run.summary.md`), and the run-summary
modal appends its sanitized rendering once, at the END of the modal,
below the markdown sumdoc.

## Not in scope

- Deterministic markdown statistics block — owned by
  `summary-regressions`.
- Canonical `<step> - <status>` markdown heading instructions and
  `decorateRunSum` / review-toggle logic — owned by
  `summary-regressions`. This task only consumes them (the markdown
  view and its links/toggles are preserved untouched).
- The `lavish-axi` reference is aesthetic only — never start, contact,
  or serve through that whiteboard server.

## Decisions already made

- **Artifact contract:** the summarizer prompt (`_summary_prompt`,
  new `## Also write run.summary.html` block, additive) asks the
  session to write `run.summary.html` in its cwd (a tempdir) — a
  self-contained document with its own `<style>`, lavish
  organization (header stat strip `header > ul > li > strong`,
  `<section>` findings, per-step `<article>`s), dashboard palette
  (#0d1117/#161b22/#58a6ff/#c9d1d9/#e6edf3/#21262d), stats only from
  the embedded run data. `summarize_run` copies the produced file
  (0 < size <= 512 KiB) into the run dir after a successful session;
  `run.summary.md` and the closing-message contract are untouched.
- **Serving:** `GET /api/runs/<run>/summary.html` returns the artifact
  re-sanitized on EVERY read wrapped in `<div class="run-summary-report">`;
  missing/unrenderable -> 404. `_run_dir_checked` keeps run-identity
  and artifact-access protections; the stored file is never served raw.
- **Sanitizer:** `sanitize_report_html` re-serializes through
  `html.parser.HTMLParser` (a real parser, not regex): allowlist of
  inert elements; drop-subtree set (script/style/iframe/svg/head/…);
  drop-void set (img/embed/link/input/…); everything else unwraps.
  Attributes are allowlist-only (title, colspan/rowspan, scope,
  col[group] span, time datetime, details open) — no URLs (so no
  network access, no loopback actions), no id/name/for/class/style/
  on* (no DOM collisions, no dashboard classes). Output is always
  balanced (open-tag stack force-closed at EOF); input capped at
  512 KiB.
- **Style scope:** a fixed stylesheet in `_DASH_HTML` scoped under
  `#detail .run-summary-report` with element selectors only — report
  markup carries no classes/inline styles, so nothing a tampered
  artifact does can style outside the wrapper.
- **Modal append:** `openRunSummary` kicks `appendRunReport(key, dir)`
  after painting the sumdoc; it fetches the fragment and
  `insertAdjacentHTML('beforeend')`s it. Guards: `summaryView.key`
  must still equal `run|<key>` (stale fetch drops), backdrop visible,
  `runReportFor` records the appended run (repeated renders /
  racing twins append exactly once). 404/empty/fetch error append
  nothing — legacy runs stay readable.

## Phases

### Phase 1 - artifact + serving

- [x] `_summary_prompt` additive HTML-report instructions block
- [x] `summarize_run` collects `run.summary.html` into the run dir;
      `mock_devin.py` emits one; aux-row outcome names both files;
      run fsig watches the new artifact
- [x] `sanitize_report_html` + `run_summary_html` +
      `GET /api/runs/<run>/summary.html`

### Phase 2 - modal + styling

- [x] `appendRunReport` + `runReportFor` guard wired into
      `openRunSummary`
- [x] scoped `#detail .run-summary-report` stylesheet (element
      selectors only)

### Phase 3 - tests + docs

- [x] Python: artifact generation (mock), endpoint contract,
      adversarial sanitizer battery (scripts/handlers/styles/ids/
      URLs/foreign/rawtext/comments/malformed/cap/tampered stored)
- [x] Node harness: ordering below sumdoc, 404/whitespace skip,
      once-only + stale-response guard, markdown decoration preserved,
      scoped-stylesheet static check
- [x] `run_tests.py --jobs 8` full suite

## Where it stopped

Done pending merge — all implementation + tests landed in this
worktree; `python run_tests.py --jobs 8` green (see Verification).

## Deferred or blocked

- The artifact is best-effort: a summarizer that answers but never
  writes the file leaves a markdown-only run (404 path) — by design.
- `run.summary.html` is never served raw (only the sanitized fragment);
  opening the stored file directly uses the model's own `<style>` —
  fine, it is a local artifact a human chose to open.

## Verification

`python run_tests.py --jobs 8` — full suite incl. tour_* and the
node harness inside test_conductor.
