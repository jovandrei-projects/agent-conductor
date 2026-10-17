# ask-ai assistant - inspection + design notes

Produced by the `research-and-design` step of run
`20261017-051357-dashboard-ask-ai` (manifest
`manifests/20261017-034530-cd55f6cc-ask-ai-context-chat.toml`, request
`data/requests/20261017-034530-cd55f6cc.json`). Refers to `conductor.py`
at this worktree's HEAD (~18.1k lines; line numbers approximate) and the
dashboard it serves (`_DASH_HTML`, lines ~12364-17588).

Verify gates: `python run_tests.py` (unittest, ~500 tests, ~13min -
over the 600s run_step verify cap, see run-review-ux STATE.md),
`python conductor.py demo`, `python conductor.py plan <manifest>`,
`run_tests.py --quick` / `--jobs N`. No committed Playwright suite;
browser probes are ad-hoc scratch scripts. Node-harness tests extract the
`<script>` block of `_DASH_HTML` and run it under `node:vm` with stub
DOMs (see `DashboardTests`/`*JsFilterTests` in tests/test_conductor.py).

---

## 1. What the user asked for (raw request summary)

Request `20261017-034530-cd55f6cc`, distilled:

1. An "ask AI" symbol button on every modal + the main dashboard, lower
   right; opens a small chat window; expandable; conversation-history
   area on the right where space allows. "Transparent sticky circle" was
   a suggestion, not a contract - aesthetics/ergonomics over literalness.
2. Default agent `gpt-6luna`, low thinking, fast mode; user is open to
   better-config suggestions and wants price awareness at selection time.
   Settings modal opens by clicking the displayed agent name:
   model/effort/fast-mode + read-only toggle (default ON).
3. Context of the current view: the assistant knows what page/modal/
   summary is open, field meanings, flows, and where logs/reports live -
   "a reduced version of the state of things", not the whole codebase.
4. Clipboard-paste and file-attach images.
5. Dictation into the textbox (explicit, editable, not auto-sent).
6. Full chat history in a database incl. which window it was opened in;
   back-and-forth conversation; persists across modal navigation and
   reload; citations/links that open the relevant modals.
7. Read-only by default; when disabled the assistant may trigger real
   dashboard mechanisms: create bug-fix proposals, edit proposals, mark
   items reviewed/unreviewed, batch edits on tasks/runs, other relevant
   triggers; also tables and calculations.
8. Token/cost stats per prompt-exchange and per chat total
   (input/output/cache, model, price).
9. Reusable design - "might be reused for other projects later, maybe a
   separate project adapted to each workspace". Keep implementation in
   agent-conductor with separable core.
10. Retrieval examples: ask about a line in
    `20261016-013639-model-compare-mobile / mobile-visualizations-and-filters`
    ("is `--noEmit` a flag?"); "tasks related to apartment-3d last month
    where there were modifications to the camera" -> specific steps +
    what to look at.

Example image `data/attachments/20261017-034141-9004b812.png`: the in-app
step-summary view (`openSummary`) of
`mobile-visualizations-and-filters` - `.dnav` bar ("<- Back", "task 3 of
5", prev/next), kv fact sheet, TASK pre box, REPORT + WHAT HAPPENED THIS
SESSION sections. The cited line sits under "Verification (all green)".

---

## 2. Current-state map

### 2.1 Stack facts that constrain the design

- Single-file app: all HTML/CSS/JS lives in `_DASH_HTML`
  (~5.2k lines inside a Python `"""` string). No build step, no npm, no
  frameworks - vanilla JS + GitHub-dark palette (`#0d1117` bg,
  `#161b22` panels, `#58a6ff` accent, Segoe UI stack). Python is
  stdlib-only (`tomllib`, `http.server`, `threading`, no sqlite yet,
  no third-party imports anywhere in conductor.py).
- Server: `http.server.ThreadingHTTPServer` on 127.0.0.1:8773
  (`cmd_serve` ~17916). Threaded - a long-poll/SSE response occupies one
  thread, not the server. Handler = `make_dashboard_handler` (~17589);
  `_send`/`_json` helpers, `do_GET`/`do_POST` with regex fullmatch
  routing. `protocol_version` is default HTTP/1.1-capable BaseHTTPRequestHandler
  - `wfile` flushes support SSE, but every current response is one-shot
  `Content-Length` JSON/HTML.
- Auth boundary: `_check_origin` (~17610) - Host must be
  127.0.0.1/localhost at the server port and Origin (when present) must
  match it. No user accounts, no tokens; "the application's existing
  user/workspace authorization boundary" IS loopback + origin check +
  OS-file permissions on data/. Anything the assistant does rides the
  same gate.
- `_body()` caps JSON at 512KB (17624). Image uploads bypass it as raw
  bytes (ATTACH_MAX_BYTES = 20MB, magic-byte sniffed PNG/JPEG/GIF/WebP/
  BMP, ~6321-6402).
- Trap 16: `_DASH_HTML` is a plain Python string - `\\` halves, `\'`
  breaks JS single-quoted strings. Prefer double-quoted JS or separate
  static assets (recommended for the chat module - see 6.2).
- Trap 5: writes inside a live self-run's watched cwd kill its session.
  `data/` sits inside the agent-conductor checkout; every write path that
  lands there takes `_assert_unwatched(runs_dir, *dests)` (409 + owner
  name) or `_wait_writable` for publishes. Assistant DB writes and chat
  attachments must do the same.

### 2.2 Modal inventory (what the launcher must cover)

ONE shared shell: `#backdrop` (fixed, z 50) > `#detail`
(role=dialog aria-modal) > `#dbody` (markup ~12815). ALL modal views
render into `#dbody`:

| View | Opener | Identity state | Content |
|---|---|---|---|
| Task/agent detail | `showDetail(i)` ~13966 | `detailKey` = run_key\|step | kv fact sheet, task prompt, stdout tail `#dtail`, trajectory arc, Actions zone |
| Run details | `showRunDetails(key)` ~14345 | `runModalKey`/`modalRunKey` | run kv, pending-merges panel, tasks table, Actions |
| Step summary | `openSummary(key)` ~13637 | `summaryView={key}` | fetched `?frag=1` markup of `step_summary_md` |
| Run summary | `openRunSummary(key)` ~13752 | `summaryView={key:'run\|<rk>'}` | LLM run.summary.md else `run_report_md` |
| Request/proposal | `openNew()`/`openRequest(id)` ~15044/15682 | `modalRequest={id,rec,dirty}` | draft/generate/review form, attachments strip, model comboboxes, pend-note, Actions |
| Planner questions | `openQuestions(id)`/`showQuestion()` ~15806 | `modalQA={id,rec,sel,free}` | one question + option cards + free text, skip |

Separate overlays (NOT #dbody): `#ctxmenu` row context menu (z 60),
`#askwrap` askConfirm/askAlert alertdialog (z 70), `#powerwrap` power
dialog (z 70). `#trashwrap` is an inline table region, not a modal.

Navigation: `modalHist` Back stack of `{kind,key|id,scroll}` snapshots;
`.dnav` sticky bar renders inside each view's markup (Back left,
"task/run X of Y" centered, prev/next right). 2s `tick()` -> `refresh()`
-> `api('GET','/api/state')` -> `render(d)` rebuilds `#dbody` on any
sig change - in-place repaint with landmark scroll/focus/draft restore
(modal-refresh-stability). The chat surface MUST live outside `#detail`
so rebuilds and modal navigation never touch it (this is also exactly
how it survives "navigate to a different modal, conversation stays").

View-state globals the context adapter reads: `detailKey`,
`runModalKey`, `modalRequest`, `modalQA`, `summaryView`, `modalHist`,
plus filters `only`/`facets`/`projs`/`selectedRun`, payload `lastData`,
row array `AGENTS`, `agentKey()`/`runKey()`.

### 2.3 Data/persistence layout (what the assistant may cite/retrieve)

- `data/runs/<ts>-<name>/state.json` - step registry
  (status/times/intervals/session/tokens/model/mode/effort/branch/
  merged/outcome/outcomes/review/completion_seq/exec_intervals/
  pending_merges/run_summary/exec/followup...), `events.jsonl`,
  `prompts/*.md`, `*.stdout.log`, `*.export.json` (ATIF: `steps[]`,
  `final_metrics{total_prompt_tokens,total_completion_tokens,
  total_cached_tokens,total_steps}`, `agent{model_name,
  tool_definitions}`, `session_id`), `<step>.summary.md`,
  `run.summary.md`, `run_report_md`-equivalent served live.
- `data/requests/<id>.json` - proposal records (fields enumerated in
  QUEUE research: raw/label/project/model/effort/plan_model/plan_effort/
  followup/on_failure/max_attempts/merge/merge_target/worktrees/
  merge_resolver/summarize/ask/attachments/proposed_steps/gen_inputs/
  qa/pending_questions/plan_sessions/stale/status...).
- `data/attachments/<ts>-<rand>.<ext>` - image bytes + serve endpoint.
- `data/pricing.json` - disk mirror of the docs rate table.
- `data/trash/<ts>-<hex>/` - recoverable deletes w/ meta.json.
- `manifests/*.toml`, `manifests/README.md`, `projects.toml`
  (+ `projects.local.toml` overlay on worktrees).
- Flag sentinels in `%TEMP%/agent-conductor-flags` (stop/drain/pause/
  timeout/skip/prompt/rnote/intrq/review/merge claims/stopgen).
- `hub.db` is EXTERNAL (mobile-apps pc-bridge `task.py` subprocess
  `hub_start`/`hub_update` ~5084) - not a conductor store; do not reuse
  for chat.
- Summaries: `step_summary_md` (~2334) deterministic per-step report;
  `run_report_md` (~2556) deterministic run roll-up; `run.summary.md`
  LLM-authored; `final_report(export)` lifts the worker's closing prose
  (~1974); `trajectory_arc`/`trajectory_detail` (~2083/2218) mine ATIF
  steps; `stdout_tail` (~11344).

### 2.4 Dashboard API (the controlled-actions surface)

Read: `/api/state` (+?archived=1,?trash=1), `/api/requests/<id>`,
run/step `/summary` (?raw/?frag), step/request `/trajectory`,
`/api/attachments/<name>`, `/api/projects/<slug>/branches`,
`/api/serve/power` (GET).

Mutations (POST JSON, all gated by `_check_origin`):
requests - create / import / save / generate / stop / apply / answer /
run(launch) / discard(confirm); runs - resume / summarize / merge{steps}
/ stop / pause / archive / delete{confirm} / review; steps - review /
interrupt{instructions,attachments} / stop / skip / timeout / prompt /
notes; trash - restore / empty{confirm}; auth - login / recheck;
serve power - stop|restart.

Assistant action whitelist should map onto the existing functions
(`save_request`, `create_request`, `apply_request`, `set_*_review`,
`set_run_archived`, `skip_step`, `set_step_notes`, `set_step_timeout`,
`discard_request`...) - reusing their validation, never re-implementing.
Launch-capable (`requests/<id>/run`, `runs/<d>/resume`, `merge`) and
destructive (`delete`, `empty trash`, `serve power`) classes need the
extra confirmation tier per the manifest.

### 2.5 Provider integration (what actually calls a model)

Everything goes through the `devin` binary (`devin_argv`, env-cleaned by
`clean_devin_env()`, headless via `hidden_subprocess_kwargs()`):
- Workers: `devin -p --prompt-file <f> --model <fam[-effort]>
  --permission-mode bypass --export <atif>` (~714).
- `devin list --format json` (cwd-scoped session registry),
  `devin rm <id>`, `devin auth status/login`,
  `devin models list` -> `model_options()` families + effort suffixes.
- Planner: detached `devin -p` in a tempdir, spool replay.
- Summarizer: detached `conductor.py summarize` -> `devin -p`.
- Pricing: `price_table()`/`refresh_prices()` scrape
  `https://docs.devin.ai/desktop/chat/models.md` `modelCostData`,
  `TEAMS_TIER_PRO`, cached to `data/pricing.json` (296 rows, 6h TTL);
  `estimate_cost()` prices prompt/cached/completion; `model_actual`
  resolves via label match.
- `devin acp` exists (stdio JSON-RPC server) - see 3.4 for the probe.

Renderer reuse: `_summary_body` (~2844) - escape-first narrow markdown
(#####/bold/italic/`code`/```fences/ul/ol/hr) - agent-authored content
can never inject markup. It has NO tables or links - the chat renderer
extends this grammar (see 6.4).

Image pipeline reuse: `save_attachment`/`read_attachment`/
`_attachment_fields` (~6345-6402) - magic-byte validation, 20MB cap,
name-regex + resolved-parent checks on serve. Chat attachments can
share `data/attachments/` + endpoints.

Model combobox reuse: `model_options()` parse + the accessible
combobox pattern (role=combobox, Recommendations + `.mall` groups,
aria-activedescendant) in `showRequestForm` (~15200+) - the settings
modal should reuse the same data + interaction, not a datalist.

---

## 3. Verified provider facts (probes run 2026-10-17, ~$0.001 spent)

All in `data/assistant-probe/` (gitignored scratch; sessions removed
from `devin list` afterwards where possible).

### 3.1 gpt-6luna resolution - VERIFIED

`devin models list` (54 families, live): family **GPT-6 Luna
(`gpt-6-luna`)** exists with variants
`{none,low,medium,high,xhigh,max}` and `-priority` fast variants.
The user's "gpt-6luna, low thinking, fast mode" -> canonical uid
**`gpt-6-luna-low-priority`** (label "GPT-6 Luna Low Thinking Fast",
1M context). `--model` accepts family slug/alias/uid; the fused form
`{family}-{effort}` is exactly what conductor already builds.

Pricing (docs.devin.ai modelCostData, TEAMS_TIER_PRO, mirrored
data/pricing.json):
- `gpt-6-luna-low`: $0.10/1M in, $0.01/1M cached-in, $0.50/1M out.
- `gpt-6-luna-low-priority` (fast): $0.20 / $0.02 / $1.00 - 2x.
- Cheaper alternatives: `gpt-5.6-luna-*` exists (~2x luna-6 price:
  $0.2/$1.2, fast $0.4/$2.4); adaptive = $0.5/$0.1/$2 router.
- For comparison: gpt-6-sol-low = $2/$0.2/$10 (10x luna);
  claude-sonnet-5.5-low = $2/$0.1/$10.

"Fast mode" suffix naming is FAMILY-SPECIFIC - never hardcode:
`-priority` (gpt-5.6/gpt-6 families), `-fast` (claude-*) or
`-fast` in uid for some, `-lightning` (swe-2), separate family
`swe-1.6-fast`, `none` effort exists only on some families. The
settings UI must read `model_options()` and only offer suffixes the
selected family actually publishes.

Effort ladder per catalog: `none,low,medium,high,xhigh,max`
(`_EFFORT_ORDER` order may vary per family - read the family's own
suffix list).

Vision: **VERIFIED live** - `gpt-6-luna-low-priority` correctly
described the user's PNG (dark session-summary modal, run/status/
timing/tokens, task prompt, report) when handed the absolute path and
asked to look at it. Mechanism: the agent's own read tool renders the
image - "attach image" = save bytes + pass the path (+ instruction).
The `session/prompt` image-block path (ACP) is advertised
(`promptCapabilities.image:true`) but unexercised (see 3.4).

### 3.2 Multi-turn - VERIFIED

- `devin -p --prompt-file q.txt --model gpt-6-luna-low-priority`
  created session `wholesale-browser` (cwd-scoped).
- `devin -p -r wholesale-browser --prompt-file q2.txt` resumed it and
  answered from memory correctly.
- Caveat: stderr warns "**--model is ignored when resuming a session;
  the session's saved model is used**" - model/effort/fast changes do
  NOT apply mid-session. Design consequence: a settings change starts a
  NEW provider session for the next exchange; continuity is carried by
  our context digest (see 6.7). Also `-r` keeps the original cwd's
  session - assistant sessions need a dedicated workspace dir (see 6.1).
- `-c`/`--continue` exists too (latest session) - `-r <id>` is the
  precise primitive.

### 3.3 Usage metrics - VERIFIED

`--export` ATIF per dispatch: `session_id`,
`final_metrics{total_prompt_tokens, total_completion_tokens,
total_cached_tokens, total_steps}` CUMULATIVE across resumed turns
(18723+8+9 steps -> 37478+36+11 on the second exchange) - per-exchange
usage = delta of consecutive exports; `agent.model_name` =
"GPT-6 Luna Low Thinking Fast" resolves to the pricing-table label
(exactly like `model_actual` pricing today). `agent.tool_definitions`
lists the session's real tools (read/write/edit/exec/run_subagent/
browser/mcp/... - the full worker toolset).

### 3.4 ACP transport - PARTIALLY VERIFIED, BLOCKED TODAY

`devin acp` (stdio JSON-RPC, ACP v1) answers `initialize` with:
`loadSession:true`, `promptCapabilities{image:true, audio:false,
embeddedContext:true}`, `sessionCapabilities{list,delete,
additionalDirectories}`, Cognition `_meta` extensions
(multiRootWorkspace, terminalLifecycle, userEdits, userConfig,
userShellCommand, sessionShare, sessionRename, documentLifecycle, ...).

BUT `session/new` produced no response in 60-120s across cwd/capability
variants (incl. after an `initialized` notification). Cause found in
`%APPDATA%\devin\cli\logs\devin_*.log`:
`ERROR ... session_db_job:open: refinery_core::traits::missing:
migration V17__subagent_heads is missing from the filesystem`
- the ACP session store's migration set is incomplete on this install
  (devin 3000.6.2). `devin list` still works (different path/tolerance).
This blocks the ACP path TODAY: no `session/new` -> no session/prompt
streaming. Re-check after `devin update` or a fixed CLI build; not a
conductor-side bug. If unblocked, ACP gives real `session/update`
streaming, `session/cancel`, `session/load`, image blocks, embedded
context, and `session/request_permission` callbacks that would be the
cleanest possible capability gate (our client answers).

### 3.5 Permission/capability gate - VERIFIED

`devin -p --prompt-file q4.txt --permission-mode auto`: the session
tried its exec/write tools and the CLI logged
"**rejected a tool call that requires confirmation. Running in
non-interactive mode. Use --permission-mode dangerous to
auto-approve all tools.**" - no `gate-write-test.txt` was created.
Reads (grep/read/find incl. absolute paths outside cwd) DID run - the
PNG read in 3.1 proves it, and `request_scope`-style escalation cannot
be granted non-interactively.

=> Under `-p --permission-mode auto` the provider session is
structurally read-only: read tools free, every mutation/exec/scope tool
auto-rejected. This is a real gate the model cannot talk its way past
(the CLI enforces, not the model). Dashboard mutations are an entirely
separate channel (model emits proposal JSON in text; our server
validates + confirms + executes) so they never depend on provider
permissions at all.

Residual: `auto` lets the agent READ any absolute path - including
`credentials.toml`-class secrets, whose content could then leak into
chat text (local-only display, still undesirable). The preamble should
denylist sensitive paths, and the retrieval adapter does bounded
server-side reads instead of trusting the agent's roaming reads for
primary context. Honest residual risk, mitigations documented in 6.8.

### 3.6 Streaming story

- `devin -p`: stdout carries the final answer at turn end (with
  \r-redrawn progress lines mid-run - AGENTS.md documents naive tails
  reading the head). The `--export` file updates "after each turn" -
  polling it mid-flight can surface intermediate agent text = chunked
  progressive updates, not token streaming.
- True token streaming needs `session/update` (ACP) - currently blocked
  (3.4). Design ships progressive-updates-via-export-poll with an
  honest "thinking..." state; upgrade to SSE on ACP when the CLI fix
  lands. The manifest's "streaming where available" allows this.

---

## 4. External research - assistants + libraries

### 4.1 Design precedents worth copying

- **Intercom Fin / Spotlight Messenger** (ideas.fin.ai/p/designing-the-
  front-door): discreet launcher, expectations set by entry point;
  "show, don't tell" - cards/tables/navigation inline; a dismissable
  floating bar when the composer itself is the surface. Their lesson
  that a pure-text thread tires fast argues for our citation chips +
  action cards + tables, not prose-only replies.
- **ServiceTitan Anvil2 Agentic Experience Shell**
  (anvil.servicetitan.com/docs/kits/ai-kit/patterns/agentic-experience-
  shell): floating window shell vs fullscreen dialog shell, both
  user-switchable; chat history in the left rail (they put it right -
  either side, user asked right); artifact panel fullscreen-only.
  Maps to our compact/expanded states.
- **GitHub Copilot / ChatGPT/Claude web**: persistent conversation
  sidebar + history list; context attachment chips ("current file");
  per-message model badge + token/cost display (Copilot shows model +
  usage on premium requests).
- **Linear/Notion/Slack AI panels**: right-side docked assistant that
  coexists with the working pane - same shape as our expanded mode.

### 4.2 Embeddable library comparison (vs our stack: single-file vanilla
JS inside a Python string, stdlib server, loopback, no build)

| Library | Fit for us | License | Maintained | Verdict |
|---|---|---|---|---|
| `deep-chat` (deepchat.dev) web component | Framework-agnostic `<deep-chat>`; custom `connect` to our endpoints; markdown, attachments, webSpeech STT built-in, streaming, avatars | MIT | active (v2.4.2 Jan 2026, v2.5.1 ~Aug 2026, ~20k dl/wk) | Viable fallback: would give the surface cheaply, but 6.5MB unpacked, own shadow-DOM theme, and our needs (view-context envelope, citation->modal nav, per-exchange usage, settings modal, read-only gate, action cards) are all bespoke intercepts fighting its model. Reuse its UX conventions; adopt if the custom surface balloons. Pin a >7-day-old version if ever used (2.4.2). |
| `@nlux/core` | vanilla-JS chat lib w/ adapter API | MPL-2.0 + AI-training/code-translation addenda | LAST PUBLISH Aug 2024 (2.17.1) - stale | Rejected (nonstandard license addenda + unmaintained) |
| `assistant-ui` | React/TS primitives + runtimes | MIT | active | Rejected - requires adopting React/build for one feature |
| MS BotFramework-WebChat | React internals, Direct Line/Copilot protocol; embed build only supports unauthenticated Copilot agents | MIT | active | Rejected - protocol/framework mismatch, huge |
| OpenAI ChatKit | hosted OpenAI sessions only | - | - | Rejected - provider-locked |
| Vercel AI SDK (`ai` + UI) | React/Next-centric | Apache-2.0 | active | Rejected - framework |
| `_summary_body` extension (in-house) | Already the project's escape-first renderer; extend with tables + citation anchors; JS twin `mdBody()` for live chat | ours | - | **Recommended** - zero deps, exact security posture, small |
| marked + DOMPurify vendored | would give full CommonMark | MIT / Apache-2.0+MPL dual | active | Unneeded weight for a narrow grammar; revisit only if real rendering gaps appear |

Dictation: Web Speech API `SpeechRecognition`/`webkitSpeechRecognition`
- Chrome/Edge yes; Safari 14.1+ prefixed (partial); Firefox disabled
  (flag only). Chrome routes audio to Google's service - must be
  disclosed in-UI (manifest requirement). `localhost` IS a secure
  context so it works on :8773. Requires a user gesture + mic
  permission; interim results stream into the editable textarea;
  never auto-send. No working fallback exists in this stack (no STT
  provider); the honest answer is feature-detect + hide-with-tooltip,
  with an optional MediaRecorder->server-STT adapter left as a
  provider seam.

### 4.3 What other products do for the same hard bits

- Context attachment: Copilot/ChatGPT show an explicit "context" chip
  per message rather than silently reading the page - mirrors our
  per-turn `ctx` provenance.
- Citations: web assistants render source cards; ours should render
  modal-opening chips keyed by stable ids (run_key|step etc.).
- Usage visibility: Copilot premium-request counter, ChatGPT usage
  pages - per-exchange usage popover + session rollup is normal UX.

---

## 5. Requirements -> already-satisfied inventory

Things later steps should VALIDATE, not rebuild:

- **Image upload validation/storage/serve** - `save_attachment` +
  `read_attachment` + `/api/attachments` GET/POST (magic bytes, 20MB,
  name/parent checks). Chat only adds conversation/message scoping.
- **Safe markdown render** - `_summary_body` escape-first subset
  (needs table support + a JS twin for live chat).
- **Model catalog + effort parsing** - `model_options()` family->suffix
  map + the accessible combobox implementation in the request form.
- **Pricing table + cost math** - `price_table`, `refresh_prices`,
  `estimate_cost` (same cached-share formula the usage step needs).
- **Provider session registry** - `devin list` cwd-scoped (assistant
  workspace isolates its sessions), `devin rm` cleanup,
  `tail_session_id`/`tail_agent_model`/`tail_metrics` export readers.
- **Report retrieval** - `step_summary_md`, `run_report_md`,
  `final_report`, `trajectory_arc/detail`, `stdout_tail`,
  `task_label`, `_state_md` locator.
- **Request record surface** - `create_request`/`save_request`/
  `apply_request`/`start_generation`/`answer_request` cover
  "create bug-fix proposal / edit proposal" actions.
- **Review/archive/skip/notes/timeout** - existing endpoints ARE the
  controlled-action set; reuse functions, never reimplement.
- **View-state identity** - `detailKey`/`runModalKey`/`modalRequest`/
  `modalQA`/`summaryView`/`modalHist` + `agentKey`/`runKey` uniquely
  name every surface; `lastData` payload backs enrichment.
- **Modal chrome conventions** - `.dnav` slots in every modal view =
  the launcher mount point covering ALL views at once; `esc()`,
  `askConfirm`/`askAlert`/`closeThen`, STCLASS chips, modal drafts maps.
- **Authorization** - loopback + `_check_origin` + `_assert_unwatched`
  + `_body` caps already form the server-side gate framework.
- **Persistence conventions** - JSON records, flag sentinels,
  events.jsonl audit logs, additive-file discipline; sqlite3 stdlib
  available for the heavier chat store (decision in 6.1).

---

## 6. Proposed architecture

### 6.1 Module boundaries (extraction-ready)

New package `assistant/` inside agent-conductor, wired in by thin
conductor call-sites. Every conductor-specific lookup lives behind a
host adapter so the core ports to another project by swapping adapters:

```
assistant/
  __init__.py           # package marker + public re-exports
  core.py               # conversation/exchange engine - transport-agnostic
  store.py              # sqlite3 schema + migrations + queries (stdlib)
  context.py            # ContextEnvelope types + builders (generic)
  providers/
    __init__.py         # ProviderAdapter ABC: send()/resume()/cancel()/usage()
    devin_print.py      # devin -p [-r] transport (VERIFIED baseline)
    devin_acp.py        # ACP stdio transport (future; blocked today)
  actions.py            # ActionAdapter ABC + validation + audit records
  retrieval.py          # RetrievalAdapter ABC: search()/fetch() bounded reads
  http_api.py           # route table -> core calls; returns dicts/bytes
  static/
    chat.js             # the whole client (served verbatim)
    chat.css            # scoped styles
    md.js               # escape-first markdown twin of _summary_body + tables
assistant_host/
  conductor_adapters.py # THE host wiring: view->envelope, retrieval vs
                        # runs_dir/requests_dir/manifests/projects.toml,
                        # actions->existing conductor functions,
                        # persistence->data/assistant/, pricing->price_table
```

Core rules: `assistant/` never imports `conductor`; adapters take
injected callables (runs_dir, projects map, price_table, action fns).
`assistant/static/*` is served as static assets (NEW thing for the
dashboard, which has none today - keeps the module self-contained AND
avoids trap-16 backslash issues in `_DASH_HTML`; the page loads it via
`<script src="/api/assistant/static/chat.js">` injected in _DASH_HTML).

Assistant workspace: `data/assistant/workspace/` is the cwd every
provider session runs in (empty dir; `devin list` there enumerates only
assistant sessions). The sqlite db lives at
`data/assistant/assistant.db` - a SIBLING of workspace/, outside the
session's watched dir (trap 5). `data/assistant/` is under gitignored
`data/` like runs/requests/attachments.

### 6.2 HTTP surface (same Handler, same gates)

Add routes under `/api/assistant/` inside `make_dashboard_handler`
(delegating to `assistant.http_api`), keeping `_check_origin` +
`_assert_unwatched` on writes:

- `GET  /api/assistant/conversations?limit&before` - history list
- `POST /api/assistant/conversations {origin_view, title?}` - create
- `GET  /api/assistant/conversations/<id>` - record + message page
- `POST /api/assistant/conversations/<id>/messages` {text, attachments,
  client_ctx, client_msg_id} - queue an exchange; 202 + exchange_id
- `GET  /api/assistant/exchanges/<xid>` - poll exchange state (partial
  text, status, usage when done) - progressive-update transport
- `POST /api/assistant/exchanges/<xid>/cancel` - cancel in-flight
- `GET  /api/assistant/conversations/<id>/usage` - per-exchange rows +
  session rollup
- `POST /api/assistant/attachments` - wraps save_attachment (same magic-
  byte/20MB rules) + links the file to conversation/message
- `GET  /api/assistant/settings` / `PUT /api/assistant/settings` -
  defaults {model, effort, fast, readonly} + catalog snapshot
- `GET  /api/assistant/models` - model_options() + pricing merged
  (settings modal data)
- `POST /api/assistant/conversations/<id>/actions/<aid>/confirm` -
  human confirms/rejects a proposed action -> executes via ActionAdapter
- `GET  /api/assistant/static/<name>` - chat.js/chat.css/md.js
- `GET  /api/assistant/last` - last-active conversation for reload
  restore (also tracked client-side)

All POSTs idempotent: `client_msg_id` (uuid) + exchange id make retries
dedupe via UNIQUE constraints. `_assert_unwatched(runs_dir,
assistant_dir)` guards every write.

### 6.3 Storage schema (sqlite3, `data/assistant/assistant.db`)

```sql
conversations(id TEXT PK, created_at, updated_at, title,
              origin_view TEXT,            -- {kind,key/ids} JSON
              provider_session_id TEXT,    -- current devin session
              model TEXT, effort TEXT, fast INT, readonly INT DEFAULT 1,
              last_active_at)
messages(id TEXT PK, conversation_id FK, role TEXT, -- user/assistant/system
         text TEXT, attachments JSON, created_at,
         ctx_ref TEXT,                     -- frozen view envelope JSON
         settings_json TEXT,               -- model/effort/fast/readonly snap
         status TEXT)                      -- complete/partial/error/cancelled
exchanges(id TEXT PK, conversation_id FK, user_msg_id, assistant_msg_id,
          provider_session_id, request_id TEXT, -- provider's id when known
          status TEXT, started_at, ended_at,
          prompt_t INT, completion_t INT, cached_t INT, llm_steps INT,
          model_uid TEXT, model_label TEXT, price_fetched_at REAL,
          usd REAL,                          -- NULL = unknown, never 0-guess
          error TEXT)
actions(id TEXT PK, exchange_id FK, kind TEXT, payload JSON,
        state TEXT,            -- proposed/confirmed/denied/executed/failed
        requires_confirm INT, confirm_text TEXT, result TEXT, created_at,
        decided_at, executed_at)
attachments(id TEXT PK, message_id FK, name TEXT, path TEXT,
            bytes INT, ctype TEXT)
UNIQUE(exchanges.provider_session_id, request_id)  -- retry dedupe
UNIQUE(messages.conversation_id, client_msg_id)    -- resend dedupe
PRAGMA user_version -> additive migrations (v1 tables; later ALTERs)
```

Why sqlite over the project's JSON convention: conversations need
indexed pagination (list by recency, page messages), partial
aggregation (usage rollups), UNIQUE dedupe for retry-safety, and
transactions for message+exchange+usage atomicity. sqlite3 is stdlib -
no new dependency. JSON alternative noted; loses every one of those.

### 6.4 Client surface (chat.js, outside modal lifecycle)

One fixed-position panel `#aichat`, z-index ~62 (above `#backdrop` 50,
below `#askwrap` 70 - so it coexists with any modal but themed
confirm/alert dialogs still top it; `#ctxmenu` 60 stays under it -
acceptable, menus are transient pointer tools at the cursor).

States: `collapsed` (floating launcher only), `compact` (~380x560
bottom-right window), `expanded` (right-docked wide panel ~min(720px,
55vw) with history rail on the right per the request - rail shows
conversations + usage summary; on <900px screens rail collapses to a
header drawer). `minimized` returns to launcher WITHOUT losing state.
Page reload restores last-active conversation (localStorage hint +
`GET /api/assistant/last`).

Launchers (one surface, many entry points - never two chats):
- Page-level: fixed bottom-right button on the dashboard body
  (`#aifab`, aria-label "Ask AI", inline SVG glyph like the paperclip -
  subtle but >=3:1 contrast, NOT literally transparent per the request's
  own softening).
- In-modal: `.dnav` right zone gets `[data-ask-ai]` button injected at
  render - covers task/run/summary/run-summary/request/questions views
  uniformly because every view renders `.dnav` (power dialog + askwrap
  get none - dialogs are transient confirmations).
- Clicking a launcher OPENS/FOCUSES the shared surface carrying the
  view it was clicked from (sets pending ctx). If chat is already open
  it stays put and only its "next question uses <view>" chip updates.

Composer: textarea + attach (paperclip, reuse attachFiles-style upload)
+ image-thumb strip + dictation mic (feature-detected) + send.
Attachments upload eagerly on pick/paste (same POST path), previews
removable pre-send; text paste unaffected.

Messages: bubbles user-right/assistant-left, `md.js` escape-first
render (tables + code fences + citation anchors), per-message:
model badge (label + effort + fast) clickable -> settings modal;
ctx chip ("about: task mobile-visualizations-and-filters") hover ->
envelope detail; usage chip (in/out/cached + $) -> expandable
breakdown; citation chips `[#n]` from the structured sources the
server attached to the exchange.

Settings modal (opens from the agent-name badge): combobox-backed
model selector reusing `model_options()` + prices (shows $/1M per
variant + capability notes like "vision verified" where known),
effort select limited to the family's real suffixes, fast-mode toggle
(only where the family publishes a fast variant - `-priority`/`-fast`/
`-lightning` naming read from the catalog, never assumed), read-only
toggle (default ON per conversation; server-owned), explanations of
each control. Persisted per exchange so history never relabels old
replies.

Dictation: `SpeechRecognition||webkitSpeechRecognition` - hidden with
tooltip on unsupported (Firefox); explicit start/stop button + visible
recording state; interim results append into the editable textarea
(never auto-sent); permission failure -> inline note; disclosure line
that Chrome-family audio is processed by the vendor's service. No
lib needed (~60 lines); MediaRecorder->server-STT adapter noted as a
future seam, out of scope (no STT provider configured).

Focus/a11y: role=dialog aria-modal=false for the panel (non-modal
coexistence is the point), roving tabindex history, Escape minimizes
CHAT only (must not fire the modal's Escape - stopPropagation when
focus is inside chat), focus returns to the launcher on close,
aria-live=polite on incoming streamed text, keyboard-operable
launchers (`/` or a hotkey TBD? skip - click/Tab suffice), citation
chips are real buttons. Screen-reader labels on icon-only buttons.

### 6.5 Context envelope (compact current-view context)

Client collects (never trusting client text as authority - server
re-resolves ids against state):
```
client_ctx = {view:'dashboard|agent|run|summary|run-summary|request|
                   questions|power',
              run_key, run_dir, step, request_id, summary_key,
              filters:{only,facets,projs,selectedRun},
              selection:<user text selection, capped ~2KB>,
              at:<iso>}
```
Server enriches (bounded): the record's kv fields (status/times/model/
tokens/outcome/note), task text (<=1.5KB like _summary_prompt), a
bounded excerpt of the open summary body or stdout tail (<=8KB),
pending-merges summary for run view, request stage + step names for
request view. Frozen onto the exchange (`ctx_ref`) - later questions
show their original view; new questions pick up the now-current view
(manifest requirement).

Static preamble (small, curated - the "page design, flows, terminology,
where evidence lives" the user asked for): dashboard section map, modal
kinds, status vocabulary, sentinel/flag concepts, run-dir layout,
"STATUS/NEXT/OUTCOME contract", verify/merge vocabulary, where logs/
summaries/prompts live by convention. Written ONCE as
`assistant_host/host_conductor_static.md` (adapter-owned - shipped),
exposed via `conductor_adapters.static_preamble()` and injected per
session start.

### 6.6 Retrieval adapter (bounded, authorized, cited)

`search(query, filters)->[source]`: glob/scan runs_dir state.json +
requests_dir records (dates, project slugs, step/run names, status,
subject keywords over task text + outcome + note + report heads);
`fetch(source, budget)->excerpt`: state.json rec fields,
`step_summary_md` (bounded), stdout tail N lines, manifest prompt
body, request record fields, trajectory arc heads. Budgets: <=12
sources, <=64KB total, sensitive-field filter (never dump env,
credentials, full raw logs without request), dedupe by identity.
Sources return `{kind:'agent'|'run'|'summary'|'run-summary'|'request'|
'log', key, label, nav}` where `nav = {view, key}` is the host
descriptor the client turns into a citation chip (showDetail /
openRunDetails / openSummary / openRunSummary / openRequest - all
exist; `log` rides the agent view's own key since the task modal is
the log's home). Implemented: see `assistant_host/retrieval.py`.
Content treated as untrusted evidence: never
copied into system role, marked as quoted material in the prompt
assembly (prompt-injection posture in 6.8).

The date/subject example ("apartment-3d tasks last month with camera
changes") maps to: search slug=apartment-3d, date window derived
deterministically server-side (record the interpreted range in the
answer envelope - the manifest requires showing it), keyword match
camera|lens|fov over task/outcome/note/report, fetch top matches'
state+summary excerpts. Honest no-results message when coverage is
partial (archived runs omitted unless flagged in).

### 6.7 Provider adapter + exchange flow

Baseline `devin_print` (VERIFIED):
1. Exchange POST -> create exchange row (status=queued), resolve
   settings snapshot (conversation's unless user changed - changes
   BEFORE send).
2. Provider session = conversation.provider_session_id IF settings
   unchanged AND session still exists; else fresh `devin -p` in
   `data/assistant/workspace/` (new session id recorded on the
   exchange + conversation; context carried via compact digest of
   prior exchanges - honest provenance note, not silent loss).
3. Prompt assembly: system-style preamble (assistant role + safety
   rules + dashboard static context) + context envelope + bounded
   retrieved evidence + prior-exchange digest (when session is new)
   + user text + attachment paths. Written to a prompt file under
   `data/assistant/spool/` (like planner spool; outside workspace/).
4. Spawn `devin -p [-r sid] --prompt-file --model <uid>
   --permission-mode auto --export <atif>` detached
   (`hidden_subprocess_kwargs` + CREATE_NEW_PROCESS_GROUP +
   `clean_devin_env`); pid recorded on the exchange.
5. Poll worker: parse export for newest agent text -> update exchange
   partial text (client polls `/api/assistant/exchanges/<xid>` ~1s);
   completion: read `final_metrics` delta vs session's previous
   exchange (cumulative counters), `model_name`, STATUS-equivalent
   (process exit + reply present), persist exchange+message+usage
   transactionally.
6. Cancel: `POST .../cancel` -> taskkill /T the pid (kill_tree),
   exchange status=cancelled (partial text + usage-so-far kept).
7. Retry: resend creates a new exchange w/ same client_msg_id path on
   the USER message only (dedupe) - assistant turn is fresh.
8. Settings change -> next exchange spawns a new session (model can't
   change on -r); prior session id kept on the conversation row for
   `devin list` continuity/debugging; old sessions are ordinary devin
   sessions visible in the assistant workspace's `devin list`
   (documented cleanup seam - `devin rm` whitelist like sessions cmd
   is a follow-up nicety, not required).

Future `devin_acp` adapter: same ProviderAdapter contract; unblocks
real token streaming (session/update->SSE) + image blocks + embedded
context + permission-callback gating. Blocked by the 3000.6.2
session-db migration defect (3.4) - do not write around it; retry
after a CLI update.

### 6.8 Read-only gate + controlled actions

Two-layer enforcement (each independently sufficient):
- Provider layer: `--permission-mode auto` => mutation/exec/scope
  tools auto-rejected non-interactively (VERIFIED 3.5); only reads
  run. So even a prompt-injected reply can't make the SESSION mutate
  anything.
- Application layer: dashboard mutations never go through the
  provider. The model MAY emit an action proposal as structured JSON
  in its reply (`proposed_action{kind, params, explain}`); the server
  validates kind against the ActionAdapter whitelist + params against
  the real functions' schemas, checks `conversation.readonly` at
  EXECUTION time (not proposal), requires human confirmation for
  anything mutating, and an extra confirm tier for launch/destructive
  ops (run/resume/merge/delete/power/discard/generate). Executed via
  the existing functions under `_check_origin` + `_assert_unwatched`
  exactly as the human's own calls; result + audit row recorded on the
  exchange; affected views refresh via the normal 2s tick.
- `readonly` lives server-side on the conversation (default 1).
  Changing it = PUT settings with explicit body; it is read at action-
  dispatch time so "turn read-only back on" invalidates pending writes
  instantly (manifest requirement). Model output, retrieved text,
  attachments, or prompt text can NEVER flip it (it's not in the
  model's authority at all).
- Read-only mode still allows: questions, bounded retrieval, tables,
  calculations (deterministic server-side calculator or plain LLM
  math in text - no eval tool), citations, action PROPOSALS rendered
  as inert cards with "enable actions in settings to run this".
- Whitelist v1 (mapped to existing functions): create_request
  (bug-fix proposal draft), save_request/apply_request (edit
  proposals), set_run_review/set_step_review, set_run_archived,
  skip_step, set_step_notes, set_step_timeout, discard_request
  (confirm-tier). Launch-tier (deferred to guarded step's exact list):
  launch_request, resume_run, merge_run_start, delete_run,
  empty_trash, summarize_run_start, serve power.
- Prompt-injection posture: retrieved evidence + log text + summaries
  are marked "untrusted quoted material" in prompt assembly; the
  static preamble states the only authorities (settings flag,
  whitelist, confirmations). Action proposals found inside retrieved
  text are never parsed as actions (only the assistant message's own
  proposal block, schema-validated). Forged tool calls = plain text,
  no effect. Residual: the session's own read tools may read
  arbitrary absolute paths - sensitive-path denylist in the preamble
  + bounded server-side retrieval as the primary channel; document
  the residual honestly.

### 6.9 Usage + price visibility

Per exchange: token deltas (3.3), model uid/label resolved via
estimate_cost-style label match, price inputs snapshot (price table
fetched_at + per-1M rates copied onto the exchange row so later table
updates never rewrite history), usd = computed (rounded, currency
labeled) or NULL=unknown when unpriced - never zero-guessed. Rollups
at conversation level (sum over exchanges, separate counted for
unpriced). Display: per-message compact chip + expandable breakdown;
chat header session total; settings modal shows live variant prices
pre-selection. Failed/cancelled/partial exchanges keep their partial
usage (manifest: include them honestly). Multimodal/image charges:
none exist in the current table - recorded as unknown if they ever
appear (pricing source noted per row).

### 6.10 Test plan (matching existing conventions)

- Python unittest in tests/test_conductor.py style + tests/mock_devin.py
  extension: mock acp/-p reply files; store tests (migrations,
  pagination, dedupe, readonly default, settings snapshots);
  retrieval tests vs fixture run dirs; action-adapter validation +
  forged-call/rejection tests; gate tests (readonly blocks,
  confirm-required, flag recheck).
- Node-harness JS tests: extract chat.js (or md.js) for `node --check`
  + vm-based DOM stubs - launcher presence per modal kind, persistence
  of draft/open state, citation-click nav call, settings gating,
  Escape scoping. NOTE: chat.js as a separate static file makes it
  directly testable without parsing _DASH_HTML - a free win.
- Live verification deferred per manifest to integration step: real
  gpt-6-luna-low-priority exchange cost + adequacy for the two named
  questions; vision exchange; dictation manual check (hardware).
- Probe data to reuse: `data/assistant-probe/` exports (real ATIF
  examples incl. resume deltas) - copy into test fixtures if needed.

---

## 7. Modal coverage matrix (acceptance source)

| Surface | Launcher | Context envelope | Notes |
|---|---|---|---|
| Dashboard page | floating `#aifab` | filters, visible run/agent counts, selected run | |
| Task modal | `.dnav` button | agent rec + tail/summary excerpt | citation target: `showDetail` |
| Run modal | `.dnav` button | run rec + step list + pending merges | `showRunDetails` |
| Step summary view | `.dnav` button | summary key + bounded md body | `openSummary` |
| Run summary view | `.dnav` button | run key + bounded md body | `openRunSummary` |
| Request/proposal modal | `.dnav` button | request rec + stage + step names + pend state | `openRequest` |
| Planner questions | `.dnav` button | request id + current question | modalQA |
| askConfirm/askAlert | none | - | transient confirm dialogs stay clean |
| Power dialog | none | - | transient; page launcher still visible |
| Context menu | none | - | pointer-transient |

Duplicate-assistant guard: launchers only ever focus/open the ONE
panel; citation clicks open host modals while chat stays usable
(z-order design); clicking a citation does not push the chat's own
state onto modalHist (chat is outside the stack by design).

## 8. Acceptance criteria (draft for the integration step)

1. Launcher reachable by keyboard + pointer on every row of section 7;
   chat opens near lower-right, expands, minimizes, restores after
   reload with the same conversation + draft.
2. A question asked while the step-summary view is open answers with
   that view's content (verification: the user's --noEmit example gets
   a correct explanation of the flag), cites the summary, and clicking
   the citation re-opens that modal without losing the thread.
3. Conversation history survives serve restart (sqlite) + browser
   reload; prior exchanges show their frozen ctx chip + model badge.
4. Attach: clipboard-paste + file-pick images preview/remove/send;
   the assistant answer demonstrably sees the image (path-based);
   non-image payloads rejected by the same magic-byte rules; text
   paste unaffected.
5. Dictation: mic button only where SpeechRecognition exists, explicit
   start/stop + editable result; hidden-explained on Firefox.
6. Settings: model combobox offers only real suffixes per family; fast
   toggle only on fast-capable variants; price shown pre-select;
   changes apply to the NEXT exchange only (new provider session
   spawned; history badges unchanged); read-only default ON.
7. Read-only: all mutation proposals render inert; with read-only off,
   a whitelisted action executes only after its confirmation card and
   lands exactly like the UI equivalent (verified vs direct endpoint).
8. Usage: each exchange shows in/out/cached/model/$ (or "unknown");
   conversation total sums; failed/cancelled rows still report;
   pricing date/source visible; no estimate presented as invoice.
9. Retrieval: the apartment-3d camera query returns dated steps +
   cited evidence + working modal links, or an honest partial-coverage
   answer showing the interpreted date range.
10. Zero new runtime deps beyond Python stdlib + vendored static
    assets; `python run_tests.py` stays green.

## 9. Open items / blockers

- **ACP `session/new` stalls** - devin 3000.6.2 session-db missing
  `V17__subagent_heads` migration (found in cli logs). Blocks true
  streaming + image blocks + permission-callback gate. Workaround:
  `-p -r` baseline (verified). Recheck after `devin update`; report
  upstream if it persists. Does NOT block the feature.
- **`session/load` on -r keeps original model** - model changes start
  a new provider session (verified warning). Digest-based continuity;
  flag for user if they expect same-session switching.
- **Agent read-tools can roam absolute paths** under `auto` (verified
  image read outside cwd) - preamble denylist + server-side retrieval
  primary; residual documented.
- **Suite ~13min > 600s verify cap** - pre-existing (run-review-ux
  notes); each manifest step's own verify will hit it. Follow-up
  material exists; implementation steps should use
  `run_tests.py --quick` or shard per the test-perf work.
- **Dictation on Firefox** - no support; hide-with-explanation only.
- **Phone access** - out of scope (loopback boundary); hub.db stays
  untouched.
- **`devin acp` permission-mode** - no `--permission-mode` flag on
  `devin acp --help`; gate for ACP would rely on answering
  `session/request_permission` - unverified until session/new works.

## 10. References

- ACP spec: https://agentclientprotocol.com/protocol/overview
  (session/new, /load, /prompt, /cancel, session/update stream,
  request_permission client hook)
- Devin models doc (pricing source): https://docs.devin.ai/desktop/chat/models.md
- deep-chat: https://deepchat.dev , https://www.npmjs.com/package/deep-chat
- nlux: https://www.npmjs.com/package/@nlux/core (+ repo LICENSE MPL+AI)
- assistant-ui: https://github.com/assistant-ui/assistant-ui
- WebChat embed: https://github.com/microsoft/botframework-webchat-embed
- Intercom Fin design: https://ideas.fin.ai/p/designing-the-front-door
- Anvil2 agentic shell: https://anvil.servicetitan.com/docs/kits/ai-kit/patterns/agentic-experience-shell
- Web Speech support: https://caniuse.com/speech-recognition
