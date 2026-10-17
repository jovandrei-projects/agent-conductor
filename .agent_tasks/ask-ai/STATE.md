# Task: ask-ai (dashboard assistant)

**Status:** in progress
**Started:** 2026-10-17   **Last touched:** 2026-10-17

## Objective

"Ask AI" assistant embedded in the :8773 dashboard: a launcher on the page
and inside every modal view, one persistent expandable chat surface,
conversation history, current-view context, citations that open modals,
images + dictation, model/effort/fast-mode settings with a read-only
default gate, controlled dashboard actions, and per-exchange/session usage
+ cost. Driven by manifest
`manifests/20261017-034530-cd55f6cc-ask-ai-context-chat.toml` (9 steps).
Reusable core with explicit host adapters - implementation lives in
agent-conductor, not a new project.

## Not in scope (this step)

- Any implementation - this file + `DESIGN.md` are the step's product.

## Decisions already made

See `DESIGN.md` for full reasoning, verified facts, and sources. Headlines:

- Provider = `devin` CLI itself. Baseline transport VERIFIED:
  `devin -p --prompt-file -r <session>` = multi-turn chat with cumulative
  ATIF export metrics (per-exchange usage = export delta). ACP
  (`devin acp`) advertises loadSession + image + embeddedContext but
  `session/new` stalls on this install (missing `V17__subagent_heads`
  session-db migration in devin 3000.6.2) - upgrade path, not baseline.
- User's "gpt-6luna, low thinking, fast mode" resolves to canonical uid
  `gpt-6-luna-low-priority` ($0.2/$0.02/$1 per 1M, TEAMS_TIER_PRO).
  Vision VERIFIED live (described the example PNG). "Fast" suffix naming
  is per-family (`-priority` for gpt-6/5.6, `-fast` for claude,
  `-lightning` for swe) - never hardcode one suffix.
- Read-only gate VERIFIED: `-p --permission-mode auto` hard-rejects
  mutation/exec/scope tools in non-interactive mode while read tools
  (incl. absolute-path reads outside cwd) still run. Mutating dashboard
  actions never touch the provider at all - model proposes action JSON,
  server validates + human confirms + executes via existing functions.
- Frontend: no framework, no npm - vanilla JS panel in the existing
  `_DASH_HTML` idiom; extend the `_summary_body` escape-first markdown
  subset (needs tables) with a JS twin. deep-chat (MIT) is the documented
  fallback if scope grows; nlux/assistant-ui/WebChat/ChatKit rejected
  (license/staleness/framework/provider-lock).
- Storage: `sqlite3` (stdlib) at `data/assistant/assistant.db`, additive
  `PRAGMA user_version` migrations; writes take `_assert_unwatched` like
  request writes (data/ sits in a self-run's watched cwd).
- Chat surface: fixed element OUTSIDE `#detail`, z between backdrop (50)
  and askwrap (70); launcher inside `.dnav` (covers all modal views) +
  page-level floating button.

## Phases

### Phase 1 - inspect + design (this step)

- [x] Map dashboard, modals, nav, persistence, provider, auth, registry,
      reports, tests
- [x] Inspect user example image + raw request record
- [x] Verify gpt-6luna id/effort/fast/vision/pricing + live transport
      probes (-p -r, permission gate, ACP handshake)
- [x] Research libraries + design precedents, compare against stack
- [x] Produce DESIGN.md: module boundaries, contracts, modal coverage,
      acceptance criteria, blockers, already-satisfied items

### Phase 2 - implementation (manifest steps, later sessions)

- [x] persistent-chat-core - sqlite schema + conversation/exchange store
      (shipped `assistant/` + `assistant_host/conductor_adapters.py`;
      contracts below)
- [x] view-context-and-retrieval - host context + retrieval adapters
      (shipped `assistant_host/context.py` + `retrieval.py` +
      `host_conductor_static.md`; core seams landed in the same step:
      schema v2 exchanges.sources, set_enriched, enricher/retrieval
      ctor args; 25 new tests; verified on real records - see
      'Implemented contracts' below)
- [x] assistant-runtime - provider adapter + gate + usage capture
      (shipped `assistant/providers/devin_print.py` + `assistant/
      runtime.py` + `assistant/calc.py` + `assistant_host/modelpick.py`;
      schema v3 exchanges.requests; live-verified `devin -p -r` smoke
      2026-10-17 incl. resume + cumulative delta; contracts below)
- [x] persistent-chat-interface - surface, launchers, settings, history
      (assistant/static/{chat.js,md.js,chat.css} + #dask on #detail shell
      + globalThis.aiView/aiNav bridge + /api/assistant/static/* serving)
- [ ] images-and-dictation - attachments + webSpeech composer
      (server side done: POST /api/assistant/attachments + ref binding;
      client composer + SpeechRecognition belong here)
- [x] guarded-dashboard-actions - action proposals + confirmations
      (ConductorActions adapter: 28-kind whitelist over the same
      conductor.py functions, launch-tier armed confirms, readonly
      recheck, failed-proposal audit, action cards; see 'Implemented
      contracts (guarded-dashboard-actions)' below)
- [x] usage-and-price-visibility - per-exchange + session rollups
      (provider capture, dispatch-time price stamping, priced
      fail/cancel, extra-counter passthrough, by-model rollup, expandable
      per-exchange + conversation UI, settings price provenance; see
      'Implemented contracts (usage-and-price-visibility)' below)
- [x] integration-and-design-review
- [ ] follow-up-plan

## Implemented contracts (persistent-chat-core)

- `assistant/` package, stdlib-only, never imports conductor. Host seams
  are injected callables: `guard` (trap-5 `_assert_unwatched`),
  `attachment_saver`, `attachments_dir` confinement, `dispatcher`
  (launch/cancel), `actions` (ActionAdapter), `catalog`, `now` clock.
- `assistant/store.py` - `Store(path, now=, migrations=)`: sqlite WAL db,
  `PRAGMA user_version` additive migrations (v1 = conversations/messages/
  exchanges/actions/attachments/settings + indexes). Entities use
  `<type>-YYYYMMDD-HHMMSS-<8hex>` ids (ID_RE is the lookup boundary).
  Multi-row writes go through `store.txn()` (BEGIN IMMEDIATE).
- `AssistantCore` interface: `create_conversation` (origin envelope +
  client_ref dedupe), `list_conversations` (keyset before=cursor on
  last_active_at+id), `get_conversation`, `open_conversation` (bump +
  message page), `last_active_conversation` (settings KV pointer +
  recency fallback), `list_messages` (seq-cursor pages, chronological
  items), `post_message` (user msg + queued exchange + pending assistant
  placeholder atomically; client_msg_id resend dedupes - an
  error/cancelled prior exchange re-queues a fresh turn on the SAME user
  message), `begin/update_partial/complete/fail/cancel_exchange`
  (terminal states sticky - usage can never double-land),
  `claim_request` (UNIQUE provider_session_id+request_id dedupe),
  `exchange_view` (poll payload incl. partial text), `usage` (per-exchange
  rows + rollup with `unpriced` count - NULL usd never reads as $0),
  `model_history` (bounded chronological tail; store keeps everything,
  `truncated` flag is honest), `update_settings`/`set_defaults`/
  `defaults` (snapshot lands on each message's settings_json),
  `save_upload` (+client_ref dedupe, attachments-dir confinement),
  `propose/decide/execute_action` + `list_actions` (audit rows; readonly
  re-checked AT execute time - a refusal keeps the proposal).
- `assistant/context.py` - ContextEnvelope {view, window, refs, filters,
  selection, enriched, at}: sanitized recursively (credential-shaped keys
  dropped), selection <=2KB, frozen envelope <=16KB stored on
  messages.ctx_ref. Per-turn frozen provenance; origin on the conv row.
- `assistant/http_api.py` - `dispatch(core, method, path, body, query)`
  -> (status, payload); ROUTES table per DESIGN 6.2; JSON 400 /
  AssistantError status / IntegrityError->409 mapping.
- `assistant/actions.py` / `retrieval.py` / `providers/__init__.py` -
  ActionSpec/ActionAdapter, Source+RetrievalAdapter (12 sources/64KB),
  ProviderAdapter/ProviderHandle/PollResult/UsageDelta + `usage_delta`
  (cumulative ATIF counters -> per-exchange delta).
- `assistant_host/conductor_adapters.py` - `open_core()`: db at
  data/assistant/assistant.db, workspace/ + spool/ dirs created (the db
  is a sibling of the provider session cwd, per trap 5), guard =
  conductor._assert_unwatched, saver = save_attachment, catalog =
  model_options()+price_table() merge, enricher = ConductorContext,
  retrieval = ConductorRetrieval, `static_preamble()` loads
  `host_conductor_static.md`.

## Implemented contracts (view-context-and-retrieval)

- `assistant/context.py` - `set_enriched(env, dict)` REPLACES any
  client-supplied `enriched` with the server's (trust boundary);
  sensitive-key/depth rules inherited, larger leaves for excerpts
  (ENRICHED_MAX_BYTES 12KB / ENRICHED_LEAF_MAX 6KB, envelope still
  <=16KB total).
- `assistant/core.py` - ctor `enricher=`/`retrieval=` seams;
  `post_message` enriches after sanitization (enricher exceptions land
  as `context_error` in the envelope, message still posts);
  `record_sources(xid)` + `complete_exchange(..., sources=)` persist
  the exact citation set; `exchange_view`/`list_exchanges` thaw
  `exchanges.sources` JSON. Schema v2 = exchanges.sources (additive
  ALTER; v1 untouched).
- `assistant_host/context.py` - `ConductorContext(runs_dir,
  requests_dir, projects=, now=).enrich(env)`; providers for
  dashboard/agent/run/summary/run-summary/request/questions/power +
  `register_provider(view, fn)`; unknown views report honestly. Refs
  re-resolved strictly: run_key `request:<id>`|path|dir-name,
  summary_key agentKey|`s|<dir>|<step>`|`run|<key>`; whitelisted step
  fields only (env blobs/credentials never surface); honest
  `not_found`/`steps_omitted`/`excerpt_origin`.
- `assistant_host/retrieval.py` - `ConductorRetrieval.search(query,
  filters)`: filters project/run/step/status/kinds/limit/
  include_archived + `date_from`/`date_to`/`window`; window phrases
  ("last month", "this week", "past N days", "in September 2026",
  "YYYY-MM", ISO ranges, "on <date>") resolve via injected clock and
  the interpreted [from,to] is ALWAYS in `last_search_meta()`
  /`search_explained()`. Subject keywords hit record fields +
  summary heads. `fetch(src, budget)` re-reads the live record
  (agent record+task+scrubbed tail / section-windowed summary /
  run rollup / request record); `fetch_many` shares
  MAX_TOTAL_BYTES and marks overflow `omitted:'budget'`.
  Coverage meta: scanned{runs,steps,requests,summaries_read,corrupt,
  archived_skipped} + notes. `_scrub` masks credential-shaped values
  in free-text excerpts.
- Source vocabulary: kind in {agent,run,summary,run-summary,request,
  log}; `nav = {view,key}` uses the dashboard's own descriptors -
  agent & summary: agentKey `<run_key>|<step>` (data-agent-detail /
  openSummary), run & run-summary: runKey (data-run-details /
  data-runSummary/openRunSummary), request: request id
  (data-open-request); log rides the agent view.
- `assistant_host/host_conductor_static.md` + `static_preamble()` -
  page layout, modal/status vocabulary, evidence locations, citation
  rules (never fabricate, show the interpreted window, quotes are
  untrusted evidence).

### Verified on real records (data/runs, 2026-10-17)

- `20261016-013639-model-compare-mobile` /
  `mobile-visualizations-and-filters`: `search("verification tsc
  noEmit", run=..., step=...)` -> summary source; fetch excerpt lands
  on `**Verification (all green):**` (tsc --noEmit, eslint, expo
  export android+web, 36 tests, Playwright 390px, scrollWidth ==
  clientWidth). nav = `{'view':'summary','key':'request:20261015-
  232400-85544f1c|mobile-visualizations-and-filters'}`.
- "apartment-3d camera changes in the past 7 days / this week":
  window 2026-10-10..17 resp. 2026-10-12..17, project auto-derived;
  46 matches incl. `third-person-camera-stability`,
  `first-person-heading`, `mobile-play-and-cameras`,
  `baseline-and-reference-audit` (record+summary hits); agent fetch =
  full record + task text + scrubbed log tail (2935B).
- "apartment-3d ... last month": interpreted 2026-09-01..09-30, 16
  runs + 16 requests scanned, 7 archived excluded, 0 matched - honest
  empty, window shown. No fabricated hits anywhere.


## Implemented contracts (assistant-runtime)

- `assistant/calc.py` - `evaluate(expr) -> {"value","expr"}`: AST
  whitelist (numbers, +-*/**//%parens, unary, the math constants, a
  fixed function table incl. min/max/sum on lists). No names, no
  attribute/index/calls-of-names, no walrus/cond/comprehension; length
  + node-count + complexity guards; CalcError on anything else -
  arithmetic exceptions become CalcError, never propagate.
- `assistant/providers/devin_print.py` - `DevinPrintProvider(argv, env=,
  spawn=, spawn_kwargs=, kill=, alive=, run_list=)`: one dispatch = one
  `devin --print --prompt-file <f> [-r sid] [--model uid]
  --permission-mode auto --export <f> --respect-workspace-trust false`
  child in the assistant workspace (`devin list` there is
  assistant-only). `--model` is emitted ONLY on fresh spawns (ignored +
  warned on `-r`). Poll: export first (sid/agent.model_name/
  final_metrics/answer tail), else bounded stdout narration tail with
  \r-frame collapse; done = process exit, nonzero = error, missing
  reply = error, auth-shaped tail = 'provider is not authenticated'.
  `cancel` = kill_tree via injected seam (taskkill /T /F); `sessions()`
  parses `devin list --format json`; `attach(exchange row)` +
  `alive_pid(pid)` back reconcile adoption. All process seams injected -
  the package never imports conductor.
- `assistant/runtime.py` - `AssistantRuntime(core, provider, workspace=,
  spool_dir=, resolve=, price_for=, preamble=, retrieval=, calculate=,
  max_hops=5, hop_timeout=900, poll_s=0.8, max_concurrent=4)`: the
  dispatcher core calls (launch/cancel/reconcile). Per exchange: resolve
  settings server-side (failure = error answer, never a dispatch); the
  vision gate refuses image posts to unverified families with concrete
  verified alternatives; a resolved-uid change forces a FRESH session
  (`--model` is ignored on `-r`); prompts = [preamble + contract] on
  fresh only + session settings + frozen context envelope +
  bounded history digest (fresh only - a resumed session carries its
  own) + user text + attachment paths. Tool contract: ONE fenced
  ```tool_call {search|fetch|calc} per turn; results land as fenced
  untrusted ```tool_result; search/fetch run through the host
  RetrievalAdapter server-side (bounded, filter-whitelisted,
  kind+key-only echo); calc never evals. ```proposed_action blocks in
  the reply -> core.propose_action rows (inert while readonly; only a
  human confirm can execute). Usage: export counters are CUMULATIVE -
  per-exchange usage = delta vs the session's last recorded cumulative
  (store.last_session_totals); absent counters record NULL (unknown,
  never zero). Every dispatch lands an exchanges.requests[] boundary
  {n,kind(fresh/resume/tool/adopted),pid,session_id,request_id sid#hN,
  prompt/export paths,started/ended,exit_code,error,usage delta,cum}.
  Reconcile(): inflight exchanges re-arm on startup - live provider pid
  = ADOPTED via attach() (polls its recorded export to the finish, never
  re-spawns), queued/dead-pid = re-dispatched; terminal finalize dedupes
  through claim_request(sid#xid).
- Schema v3: `exchanges.requests TEXT` (additive ALTER; migration test
  pattern moved to v4). `core.record_request` appends under txn;
  `note_dispatch` rewrites pid/paths per hop; `note_provider_session`
  stamps exchange + conversation resume handle; `session_totals` reads
  the newest earlier `cum` snapshot for deltas; `inflight_exchanges`
  feeds reconcile; `settings_validator` seam validates EFFECTIVE
  merged settings on conv create + settings updates.
- `assistant_host/modelpick.py` - catalog-aware resolution against the
  LIVE `model_options` map: family parse (uid = fam[-suffix]), effort
  validation, fast = family's own marker suffix after the effort segment
  (`-priority` gpt-6/5.6, `-fast` claude, `-lightning` swe - NEVER a
  universal flag; fast-BRANDED families like swe-1.6-fast satisfy
  fast=true bare), `VISION = {'gpt-6-luna': True}` only-verified map,
  errors carry concrete verified alternative uids - never a silent
  substitution. `make_validator(options_fn)` feeds core;
  `resolve_settings(snap, attachments_present)` feeds the runtime;
  `annotate_catalog(models, options, defaults)` adds
  vision/fast_variant flags + `default_resolution` (honest ok/error).
  Empty catalog (CLI down/unauthed): save-time validation defers,
  dispatch fails with 'catalog unavailable'.
- `conductor_adapters.open_runtime(data_dir=, runs_dir=, requests_dir=,
  devin_argv=, provider=, reconcile=True) -> (core, runtime)`: real
  provider (clean_devin_env + hidden flags + kill_tree + pid_alive),
  runtime dispatcher, settings validator, catalog annotation, seeded
  default `gpt-6-luna/low/fast` (preference only - dispatch re-verifies),
  startup reconcile. `open_core()` unchanged for tests.
- `conductor.py` serve: `/api/assistant/*` registered for GET+POST (raw
  body through - attachment route carries image bytes; JSON routes
  parse inside dispatch); lazy `assistant_open()` under STATE_LOCK -
  init failure = 503 `assistant unavailable`, dashboard unaffected.

### Verified live (devin 3000.x, 2026-10-17)

- `devin auth status` -> logged in; `model_options` -> 54 families incl.
  `gpt-6-luna` with low/medium/high/xhigh/max + `-priority` fast + `none`.
- DevinPrintProvider spawn in %TEMP% scratch, uid
  `gpt-6-luna-low-priority`: session `candied-scissor`, label
  `GPT-6 Luna Low Thinking Fast`, exit 0, export metrics
  14495p/7c/0cached/9steps, answer 'SMOKE OK'.
- Resume `-r candied-scissor` (no --model emitted): same session, cum
  29010p/14c/11steps -> delta math correct; `sessions()` listed it.
- Suite: 37 new test_assistant_runtime tests (scripted provider +
  tests/fake_devin_chat.py real-subprocess binary + e2e) + modelpick +
  calc; full suite 632/632 green, 8 shards.

## Implemented contracts (persistent-chat-interface)

- `assistant/static/chat.js` (self-invoking, defer-loaded): injects the
  launcher + panel DOM once - `#aifab` (page FAB) + `#aichat` (fixed,
  non-modal dialog at z 62, above `.backdrop` 50, below askwrap 70) +
  `#aisetwrap` (settings). The injected `<div id="dask">` stub already
  inside `#detail` (sibling of `.x` and `#dbody`) gets revealed and
  delegated through one document-level `[data-ask-ai]` click hook -
  every modal variant (agent/run/summary/run-summary/request/questions)
  is covered by the shell, never repainted, no per-view injection, and
  duplicates cannot exist.
- Modes: closed (FAB) / compact 380x560 / expanded ~920-wide x ~80vh,
  persisted via `ac-ai-open`; last conversation via `ac-ai-conv`;
  composer drafts per conversation via `ac-ai-draft[:<id>]` (restore
  tested); browser window identity `ac-ai-win` in sessionStorage.
- Send flow: `POST /conversations` (client_ref dedupe) on first message,
  `POST .../messages` {text, client_msg_id (uid >=8 chars), client_ctx
  = frozen view envelope snapshot}. Pending assistant placeholder
  renders 'thinking'; exchange statuses queued/running/partial/done/
  error/cancelled map to meta badges; `stop` -> POST
  `/exchanges/<id>/cancel`. Exchange polling (1.1s, capped 10 min) is
  independent of the dashboard's stateReqSeq refresh and guarded by
  `S.pollXid` - only one poller, superseded polls die.
- `client_ctx` view comes from `window.aiView()` (the bridge in
  _DASH_HTML): agent/summary/run/run-summary/request/questions/power/
  dashboard + filters (only/facets/projs) + selectedRun + selection -
  the host enricher replaces it server-side anyway (trust boundary).
  `ai-ctx` chip shows "about: <view>" and re-syncs via `globalThis.
  aiSync` which the dashboard calls at the end of `applySel` - so modal
  open/close/navigate/re-render keeps it honest with zero polling.
- History rail (`#ai-hist` toggle, `.ai-rail` column inside `.ai-main`):
  GET `/conversations?limit=30` + `open_conv` restore; per-item
  origin-view chip + title/timestamp; `New chat` reuses the flow.
  Per-user-message ctx chips (`aim-ctxm`) render the frozen envelope's
  view label; per-reply model badge (`aim-modelb`, clickable -> settings)
  shows THIS turn's settings_json snapshot, never current settings.
- Citations: `exchanges.sources[]` render as `aim-src` chips;
  `[data-ai-nav]` clicks dispatch through `window.aiNav.{agent,summary,
  runSummary,run,request}` - the dashboard's own openers - with the
  source's frozen `nav` descriptor (opaque to the client). A citation
  click never resets the conversation.
- Settings modal (nested dialog, Esc-in-dialog does not leak): GET
  `/models` (annotated catalog) + `/settings` defaults; Model = family
  select only listing live families; Effort select scoped to that
  family's suffixes; Fast checkbox disabled (with note) when the family
  has no fast marker; resolved-uid preview mirrors modelpick's
  family+effort+marker composition; Save -> POST
  `/conversations/<id>/settings` (or global `/settings` when no conv);
  validation/catalog errors surface verbatim in `#ais-msg`.
- `assistant/static/md.js` - escape-first markdown twin of
  `_summary_body` plus tables: fence/heading/list/ol/ul/blockquote/hr/
  bold/italic/inline-code/links (allowlisted http/https/mailto +
  hash/slash anchors only; `javascript:` etc. never linkify), tables,
  paragraphs. Everything else is HTML-escaped - assistant text is never
  trusted markup.
- `conductor.py` wiring: `_DASH_HTML` head links chat.css + md.js +
  chat.js (defer); `#dask` launcher inside `#detail` before `#dbody`
  (repaint-proof); `.dnav` right padding 56->140px for launcher room;
  `globalThis.aiView/aiNav` bridge globals at the end of applySel's
  block; `Handler._AI_STATIC` whitelist (`chat.js`,`md.js`,`chat.css`)
  served from `assistant/static/` through `_check_origin` like every
  other route (404 on anything else, path traversal included) with NO
  core dependency (broken provider init can't take the shell down);
  `_assistant` forwards parse_qs queries (pagination, attachments);
  `Handler.assistant_close` staticmethod releases the lazily-opened
  sqlite store (needed for Windows test tempdirs).
- The chat surface itself is injected at `document.body` level -
  `#detail` innerHTML rewrites on modal navigation can never destroy it,
  and it stays interactive while a source modal is open.

### Verified (persistent-chat-interface)

- `node --check` on chat.js/md.js + the extracted inline script.
- `tests/test_assistant_ui.py` (16 tests): markup/static-route serving +
  whitelist + query forwarding; md.js escape/table/link tests; node-vm
  launcher toggle + modal coverage + Escape handling, navigation
  persistence (chat survives view changes + minimize/reopen with draft),
  history restoration (stored conv id + draft reloaded), settings dialog
  (catalog families, effort scoping, fast gating, POST shape, header
  relabel, per-message snapshot badge), citation routing through aiNav,
  send->queued->poll->done flow with client_msg_id + ctx envelope.
- Live serve smoke on :8806 with a temp runs dir: index serves with the
  chat assets; `/api/assistant/static/*` 200; `/api/assistant/
  conversations?limit=5` -> `{"items":[]}`; `/settings` -> seeded
  `gpt-6-luna/low/fast/readonly`; `/models` -> real 800-uid catalog with
  `default_resolution` `gpt-6-luna-low-priority` + `vision:true`.
- Full test_conductor suite needs a rerun (bridge initially used
  `window.` which vm harnesses lack - now `globalThis.`).

## Implemented contracts (images-and-dictation)

- `assistant/core.py` - `save_upload(conv_id, bytes, client_ref=None)`:
  validates conv exists, dedupes on client_ref, caps 200 rows/conv
  (`ATTACH_PER_CONV_MAX`), runs the unwatched-dir guard, calls the
  injected saver (returns `{name,path,ctype}`), verifies the saved path
  stays under `attachments_dir`, stores ctype+bytes+client_ref. Host
  `RequestError`s re-raise as `AssistantError` keeping their HTTP
  status. `_bind_attachments` refuses foreign-conv ids (404), already-
  bound ids (409) and >20 ids (400). `read_upload(att_id)` validates
  the id format, checks conv existence + dir confinement, returns
  `(bytes, image/* ctype or octet-stream)` - 404 on bad ids/missing
  files.
- `conductor.py` - `save_attachment` returns `{name,path,ctype}` (the
  magic-sniffed type); `_attachment_fields` preserves `ctype` when a
  stored upload row is echoed back so request records still round-trip
  byte-for-byte. `GET /api/assistant/attachments/<id>` serves row bytes
  through `core.read_upload` - a row id is the only client key, never
  a path (e.g. `/att-abc`, not `?path=...`). POST body cap: 20MB scoped
  to `/api/assistant/attachments` only; other `/api/assistant/*` POSTs
  keep the 512KB JSON bound.
- `assistant/static/chat.js` - composer: `.ai-cwrap` (strip + textarea)
  beside `.ai-cbtns` (attach, mic, send). Paste: `clipboardData` file
  items route through `attachFiles` (image/* + 20MB + 20-count
  preflight with 'Skipped: X' notes - server magic-byte validation is
  authoritative); a clipboard without files falls through to normal
  text paste untouched. `#ai-file` (hidden, `accept="image/*"`,
  multiple) opened by `#ai-attach`. `uploadAtt` pushes a pending slot,
  `ensureConv`s, POSTs raw bytes to
  `/attachments?conversation=<id>&ref=<client-ref>`; slots carry
  `conv` - a mid-load/switch race migrates the slot to its OWN conv's
  `S.pendAtt` stash (loadConv re-parks foreign slots before restoring
  and filters them out on restore). `paintAtt` shows uploading/failed
  chips + preview thumbs from `/api/assistant/attachments/<id>`;
  corner `x` removes the pending slot. `send` gates on
  uploading/failed slots, requires text-or-attachments, posts
  `attachments:[ids]` (only ids under the current conv), clears the
  strip on success. Message render: `aim-ats` thumbs on user messages
  from `msg_attachments` JSON (path never enters markup). Vision
  refusals surface 'no verified image support' + 'settings -> pick a
  vision-capable model'.
- Dictation (same file): `micCtor` = `SpeechRecognition ||
  webkitSpeechRecognition` - absent = `.na` inert button + explanatory
  tooltip + click note (Firefox case), never a fake control.
  `startDict` toggles start/stop; continuous+interim; results write
  `base + final + interim` (boundary-space join) into the textarea -
  typed text preserved, transcript fully editable, nothing
  auto-submits. onerror maps not-allowed/service-not-allowed/audio-
  capture/network/language-not-supported/no-speech/aborted to inline
  notes; the once-per-tab disclosure states the browser/vendor speech
  service processes audio and no recording is stored. `teardownDict`
  aborts + invalidates callbacks on send, minimize, and every conv
  switch; `aria-pressed` + `.rec` paint the recording state.
- `assistant/static/chat.css` - `.ai-cbtn` controls (attach/mic),
  `.na`/`aria-pressed`/`.rec`+`airec` pulse states, `.ai-ats` pending
  strip, `.aim-ats` message thumbs, `.ai-cwrap`/`.ai-cbtns` composer
  columns.

### Verified (images-and-dictation)

- 134 assistant tests green (test_assistant 46 + runtime 40 + host 24 +
  ui 24): real-saver magic-byte/empty/oversize paths, per-conv cap,
  bound-id refusal, `read_upload` round-trip + tampered-ctype/path/
  missing-file gates, raw upload route incl. dedupe/404/415, runtime
  vision refusal + attachment-path-in-prompt, static route upload +
  byte serve + JSON-cap split, node-vm paste/file/remove/send-ids/
  thumbs/mic-unavailable/dictation-lifecycle/vision-note tests.
- Full suite: `python run_tests.py --quick --jobs 8` -> 667 tests,
  8/8 shards ok.
- Live probe on :8806 (headless Chromium): composer controls present;
  real file-picker upload -> 201 + thumbnail `naturalWidth>0` via the
  GET route; mid-load race parks the slot under its own conv
  (`S.pendAtt`); synthetic image paste pushes a pending slot; text
  paste untouched; `webkitSpeechRecognition` present -> mic click
  painted `.rec` + disclosure note.

### Still requires a real-browser/human check

- Live `devin -p` multimodal turn: the vision gate + path-in-prompt
  delivery are test-covered, but a real session reading a real image
  (DESIGN.md's 2026-10-17 probe pattern) was not re-run here - no real
  provider spawn was launched in verification.
- Real mic permission grant/deny + speech: needs a physical mic +
  headed browser; headless Chromium never exercises the real prompt.
- Real OS clipboard paste (image from Paint/screenshot): the synthetic
  ClipboardEvent path is verified; OS-level paste keeps the same
  `clipboardData.items` contract but is unverified end-to-end.

## Implemented contracts (guarded-dashboard-actions)

- `assistant/actions.py` - `ActionAdapter` is a concrete host seam (not
  ABC): `spec_for`/`validate`/`execute` required, `expected_ack(row)`
  + `describe()` optional. `LAUNCH_TIER_KINDS` = launch_request,
  generate_request, answer_request, discard_request, stop_generation,
  resume_run, merge_run, summarize_run, stop_run, stop_step,
  batch_step_stop, interrupt_step, delete_run, empty_trash,
  serve_power - kinds that spawn work, kill processes, merge, delete,
  or bounce the server. KIND_RE is a compiled regex (2-41 chars).
- `assistant/store.py` - schema v4: `actions.explain TEXT` (the model's
  one-line reason, shown on the confirm card). Migration test pattern
  moved to v5.
- `assistant/core.py` - `_record_action()` is the single bounded audit
  insert (payload JSON-capped at 150KB - the largest legit payload is
  an edit_request carrying a republished TOML). `propose_action(...,
  explain=, record_failure=)`: with record_failure a rejected proposal
  lands as a terminal `failed` row (audited, visible, never executes)
  instead of raising. `decide_action`: proposed->confirmed/denied,
  confirmed->denied retraction, terminal rows are no-ops.
  `execute_action(aid, ack=)`: readonly is re-read on the CURRENT
  conversation row immediately before the adapter call; `failed` is a
  terminal no-op; launch-tier kinds must echo `expected_ack` (the
  adapter's exact-target string) or 409. `post_message` drops a
  per-message `settings.readonly` claim and stamps the CONVERSATION's
  readonly into the stored settings_json - a message can never claim a
  capability the conversation lacks. `expected_ack(aid)` +
  `action_catalog()` are the runtime/UI reads.
- `assistant/runtime.py` - `_proposals` parses ```proposed_action
  blocks ONLY from the model's final reply (never from retrieved
  tool_result text), caps at _MAX_PROPOSALS, persists `explain`, and
  records malformed blocks as `failed` rows with kind `(malformed)`.
  The fresh-turn prompt appends `core.action_catalog()` under
  "Whitelisted actions you may propose" so the model only ever names
  real kinds.
- `assistant_host/actions.py` - `ConductorActions(runs_dir,
  requests_dir=, manifests_dir=, trash_dir=, devin_argv=, power=)`.
  `_KINDS` maps 28 kinds to {text (card effect line), help (prompt
  catalog line), validate (propose-time gate), call (execute), target
  (ack fragment)}. Validators resolve stable ids to real records -
  run dir names via `_run_dir_checked`, step names against state.json
  OR the run's manifest names (`manifest_ok` for state-free controls),
  request ids via `_read_request`, trash entries via `_trash_entry` -
  then run the host's own shape gates (`_request_fields`,
  `validate_proposal`, `_attachment_fields`, bound checks). `serve_power`
  exists ONLY when `power` is wired (spec_for/describe hide it
  otherwise). Executors call the same functions the dashboard POST
  endpoints call - `_assert_unwatched`, stage gates, liveness, merge
  claims, retry-safety all inherited. `_batch` helpers give per-item
  results/errors for batch_* kinds.
- `conductor.py` - `_serve_power(server, act)` extracted from the
  /api/serve/power endpoint so the endpoint and the action share one
  implementation; `assistant_open(server=)` forwards manifests_dir +
  trash_dir + the power seam into `open_runtime`.
- `assistant/static/chat.js` - `S.actions[xid]` rows render `.aim-card`
  under the exchange's message: kind/effect/params/explain/result +
  state badge. proposed+readonly -> disabled controls + 'read-only is
  on' hint (cards never hide). confirm tier -> Run/Deny. launch tier ->
  `Review & run` arms a second confirm block spelling the verbatim
  `expected_ack` target + effect; `Confirm & run` posts
  {approve, execute, ack} to `/actions/<id>/confirm`. Decisions write
  the returned row back into `S.actions` before the actions reload.
- `assistant/static/chat.css` - `.aim-card` + `.st-*` state styles,
  `.act-arm` armed block, `.act-params`, `.act-btns`.
- POST `/conversations/<id>/actions/<aid>/confirm` body gains `ack`
  (http_api -> execute_action); GET `/conversations/<id>/actions`
  rows include `expected_ack` + `explain`.

### Verified (guarded-dashboard-actions)

- 37 new tests: `tests/test_assistant_actions.py` (33) - whitelist
  coverage/serve_power seam; validators (wrong-target 404s, manifest-
  declared steps, request/trash gates, extra+malformed params, injection
  text staying data); ack naming + the full no-ack path (proposed,
  confirmed-no-ack, wrong-ack all refuse before conductor.stop_run is
  ever called); forged confirmed row -> failed at the adapter boundary;
  rejected proposals as failed rows; duplicate-decide idempotence;
  readonly deny + flip invalidation; per-message settings can't claim
  permission; fixture executions (archive stamp, review marks, batch
  partial errors, create/discard-to-trash, delete-to-trash, executor-
  error -> failed row, full HTTP flow with ack, deny over HTTP).
  `test_assistant_runtime.py` (+3): rejected/malformed proposals
  audited as failed rows; action catalog reaches the fresh prompt;
  (pre-existing: proposal inert under readonly, retrieval evidence
  can't flip readonly). `test_assistant_ui.py` (+1): card proposed/
  armed/executed rendering, verbatim ack POST, deny body, readonly
  inert state, injection escaping.
- `python run_tests.py` -> 702 tests, OK. `node --check` on chat.js +
  md.js clean.
- No test launches real work: launch-tier executors either never reach
  the conductor call (ack gate) or the `conductor.*` function is
  mocked at the boundary.

## Implemented contracts (usage-and-price-visibility)

- `assistant/core.py` - `USAGE_COUNTERS` is the one counter list
  (prompt/completion/cached/llm_steps). `_merged_usage(x, usage)` =
  terminal usage merged over already-persisted partial counters
  (explicit None never erases a landed partial); `_row_price(x)`
  rebuilds the rate map from the row's own snapshot; `complete_exchange`
  uses `eff_price` = passed rates else the row's stamped snapshot, so
  cost is always computed against the rates the exchange was stamped
  under - never re-looked-up. `fail_exchange`/`cancel_exchange` take
  `price=` and compute `usd = price_usage(_merged_usage, _row_price)`
  when unset - billable failures/cancels are priced, never free.
  `set_price_snapshot(xid, price)` is first-write-wins (a mid-flight
  table refresh can't rewrite dispatch-time inputs). `_usage_extra`
  sums numeric `usage.extra` fields across the requests JSON;
  `exchange_view`/`list_exchanges` thaw requests+sources and carry
  `usage_extra`. `usage()` returns `{exchanges[], totals}` with
  `by_model` groups (per-uid exchanges/tokens/usd/unpriced - a
  mixed-model conversation never blends rates), `currency:'USD'`,
  `cost_kind:'calculated'`, latest `price_fetched_at`/`price_source`;
  `unpriced` counts only terminal rows that actually REPORTED usage
  (a no-usage cancel is nothing-to-price, not unpriced).
- `assistant/runtime.py` - `_delta_dict` diffs the canonical counters
  AND every other numeric `total_*` key into `usage.extra` (cache
  writes, image/summarization billing land verbatim when a provider
  reports them - no invented price semantics). `_run_exchange` stamps
  `set_price_snapshot` at dispatch (before the hop loop) so a
  fail/cancel can price; `_finish` passes `price` into `fail_exchange`
  for the died-without-text path. `exchanges.requests[]` keeps
  `{kind: adopted}` and per-hop `usage.extra` for the expandable
  breakdown.
- `assistant_host/conductor_adapters.py` - `_pricing_meta()` =
  `{currency,unit,tier,fetched_at,source}` from `_PRICE_CACHE`, merged
  into the `/models` catalog payload - the settings modal's
  rate-provenance line comes from the same snapshot the exchanges
  store.
- `assistant/static/chat.js` - per-terminal-exchange
  `<details class="aim-use">` (native details = keyboard/screen-reader
  accessible): compact summary (in/cached/out/turns/est.$/'usage not
  reported'/'cost unknown' - never $0.00) expands to model+settings
  snapshot+status+session, the per-hop `internal requests` rows
  (`#n kind · request_id · Ns · exit · in/out/cached/turns · extras`,
  `error:` line on failed hops), `usage_extra` under 'also reported
  (no price semantics attached)', the rate snapshot (rates / source /
  fetched date) and the 'calculated USD estimate - not an invoice'
  line; an attachment-bearing turn notes 'no separate image charge;
  image processing lands inside the token counters'. `fmtUsd` shows
  `<$0.0001` for real-but-sub-precision costs (a nonzero cost never
  reads as free). Header `#ai-usage` is now a `<details>`: summary
  keeps the compact `in/out tok · ~$` chip; `#ai-useview` panel lists
  `by model` groups (multi-model only), `N unpriced` badges, the
  grand-total line, the unpriced 'unknown, not zero' note and the
  calculated-USD + rate-provenance line.
- `assistant/static/chat.js` settings modal - `#ais-price` shows the
  resolved uid + its own rates (family fallback when unpriced), a
  `vs standard <effort>: ~$X/$Y` comparison when the fast variant
  reprices (skipped for fast-branded families), image support, and
  `estimates in USD · rates fetched <date> (<source>) · not an
  invoice` from `catalog.pricing`. The dialog never changes the
  selection by itself.
- `assistant/static/chat.css` - `.aim-use` details/summary/`.aim-udet`/
  `.um-l`/`.um-r`(+`.err`)/`.um-h`/`.um-warn`; `.ai-usage` details +
  absolute `.ai-useview` dropdown panel.

### Verified (usage-and-price-visibility)

- `tests/test_assistant.py` +10 (`UsageAccountingTests`): by-model
  mixed-model rollup; stored snapshot survives a later rate-table
  change (old row's usd/price_in/fetched_at untouched, new exchange
  reprices); dispatch first-write-wins vs terminal refinement;
  failed-exchange priced (`usd` from usage+snapshot, not free);
  cancelled-with-usage priced from stored snapshot; no-usage cancel is
  not 'unpriced'; unpriced-model cancel counted; all-None usage =
  `usd=None` + unpriced (never $0); extra `total_*` counters roll up
  through `usage_extra`; store reopen keeps usage/price/requests
  verbatim.
- `tests/test_assistant_runtime.py` +5: provider `total_*` extras land
  in `usage.extra` + `usage_extra`; failed dispatch priced end-to-end;
  cancelled-mid-run priced from the dispatch stamp; failed+retry are
  two separately-priced exchanges sharing the session's cumulative
  baseline; two exchanges on different uids price against their own
  rate sets and roll up per-model.
- `tests/test_assistant_ui.py` +3: the expandable per-exchange block
  (summary, internal requests, extras, rate line, estimate label,
  image-charge note, unknown-not-zero honesty, cancelled row);
  `#ai-useview` (by-model split, total, unpriced honesty, USD/provenance);
  settings price line (resolved uid, rates, standard-vs-fast compare
  gone when fast unchecked, fetched/source/estimate wording).
- `python -m unittest tests.test_assistant tests.test_assistant_runtime
  tests.test_assistant_ui` -> 130 tests OK; `node --check` chat.js
  clean; `python run_tests.py` full suite re-run pending.

## Where it stopped

`integration-and-design-review` is done: the assembled branch was
audited end-to-end, a dozen real defects fixed, verified against a
live provider on isolated fixture data, browser-exercised
(46 deterministic checks), and security-reviewed. Findings below;
commits land in this worktree's branch.

## Implemented (integration-and-design-review)

Defects found in review and fixed:

- `assistant_host/retrieval.py` `_resolve_src` + `_run_dir_for_key`:
  the fetch tool contract hands back only `{kind,key}` echoes (search
  stubs never expose `run_dir`), so fetched excerpts failed with
  "run dir ? not found" and stored chips could not navigate. The key
  is now re-resolved server-side (`request:<id>` -> run dir map,
  `RUN_DIR_RE` names, display-name fallback) and a missing `nav`
  descriptor + human-readable `label` are synthesized so stored
  citations navigate and read `run / step - summary`. Confirmed
  end-to-end: live re-fetch now lands real excerpts with working
  nav descriptors.
- `retrieval.search` `kinds` filter accepted a string and iterated it
  character-wise; normalized to a list. Step `status` filters now use
  `conductor.effective_statuses` (the modal's own semantics) with a
  recorded-status fallback.
- `assistant_host/actions.py` batch step-review entries accept
  `{name, seq}` objects (not just names) - per-step completion
  `seq` now propagates to `conductor.set_step_review`.
- `assistant_host/modelpick.resolve`: `effort=''` + `fast=True` now
  searches the family's actual fast-marked suffixes (`-priority`,
  `-fast`, `-lightning`) instead of failing, and derives the effort
  from the suffix picked.
- `assistant/calc.py`: `_safe_pow` bounds integer-power evaluation by
  estimated result bit-length (`MAX_POW_BITS = 65536`) - `9**9**9`
  no longer hangs the evaluator.
- `assistant/core.py`: `record_text_progress`/`cancel_exchange`
  re-check exchange status inside the write txn so a raced terminal
  (poll finished while cancelling) is never clobbered; attachment
  upload dedupes on `client_ref` inside the txn and unlinks orphan
  bytes on insert failure; `read_upload` refuses oversized reads;
  `decide_action`/`execute_action` accept `conv_id` scoping; the
  launch-tier ack gate fails closed when `expected_ack` errors or
  returns empty instead of comparing against None.
- `assistant/http_api.py`: decide/execute routes forward the
  conversation id so cross-conversation action ids 404 instead of
  resolving; request `cursor` shape validated.
- `conductor.py` `assistant_open`: permanent init errors no longer
  503 forever - a stale failure retires after 60s; the `power` seam
  is omitted entirely when no server exists (a bare lambda used to
  list `serve_power` as a no-op action kind).
- `assistant/static/chat.js`: attachment slots are conversation-scoped
  (`conv`), marked `dead` on removal, parked per-conversation
  (`S.pendAtt`) when the visible thread moves during upload, and
  `send()` filters by current conversation; paint preserves open
  per-exchange usage `<details>` via `data-xu`; settings dialog has
  a real focus trap + Escape + `aria-modal`, focuses the model
  select on open, disables Save + says so when the model catalog is
  unreachable; `effortsFor` accepts only known effort heads; pasted
  text survives alongside an image; Escape closes settings first,
  otherwise minimizes; citation chips derive a nav descriptor
  client-side from `kind+key` when a legacy stored row lacks `nav`
  (`srcNav`) and fall back to the step name as their label; in-text
  `nav:<view>/<key>` citation links render as delegated
  `[data-ai-nav]` anchors (in md.js) instead of literal markdown.
- `assistant/static/chat.css`: launcher/panel z-index 58 - above the
  modal backdrop (50/51) but below row context menus (60) and
  alert/confirm/power dialogs (70).

New regression tests (all green):

- core: price snapshot first-write-wins; explicit `None` does not
  clobber price fields; partial terminal sticky; cancel-race keeps
  terminal; cursor shape validation; attachment ref dedupe + row-id
  reads; decide/execute conv scoping 404.
- actions: launch ack fails closed (empty expected ack), read-only
  re-check at execute, batch `{name, seq}` propagation, rejected
  proposals audited as failed.
- host: fetch-by-key resolution, nav+label synthesis, effective-
  status filter, kinds string normalization.
- ui: usage-details repaint preservation, conv-scoped attachment
  send filtering, paste keeps text, settings focus trap / outage
  state, nav-link affordances incl. `%22` escape safety.

### Verified (integration-and-design-review)

Deterministic, on isolated fixture data (`--runs-dir
%TEMP%\askai-data\runs`, real `devin` provider, served :8806
loopback):

- `python run_tests.py`: 734 tests, 1 transient
  `PermissionError` on a temp `state.json` (Windows file-lock race in
  `CumulativeElapsedTests.test_stop_then_restart_banks_first_dispatch`;
  passes standalone in 47.9s). Earlier port-bound run's 3
  `ServicePortLeaseTests` failures were my probe server holding
  :8806 - the module is green standalone.
- `node --check assistant/static/chat.js`, `md.js`: clean;
  `python -m compileall assistant assistant_host tests`: clean.
- Playwright headless Chromium (46 checks, screenshots in
  `%TEMP%\askai-probe\shots`): FAB open/minimize/reopen; expand/
  compact; `#dask` toggle inside a run modal; ctx chip follows
  dashboard->run modal->close; draft survives nav + minimize +
  reload; settings dialog real round-trip (effort medium -> server
  persisted -> back to low), catalog 54 families from the live
  `devin` list, Escape close, price provenance line; attach PNG ->
  thumbnail -> remove; mic toggles dictation on (real browser speech
  transcription itself stays unverified); history rail; usage chip
  expands; Escape minimizes; reload restores open chat + last
  conversation; citation chips + in-text `nav:` links open the right
  dashboard modal while the chat survives; chip labels readable.
- Live provider, 2 exchanges on one conversation (~$0.034 total,
  recorded): the `tsc --noEmit` question answered correctly
  (TypeScript flag = type-check without emit; the report's "clean"
  is a worker report, not independent verification) with a cited,
  navigable step-summary source; the apartment-3d camera question
  returned four dated steps (`bird-eye-and-plan-views`,
  `first-person-heading`, `third-person-camera-stability`,
  `mobile-play-and-cameras`, all Oct 15) with evidence + working
  `nav:summary` links and an honest coverage note (archived runs
  excluded).
- Security review: loopback bind verified (`127.0.0.1` listener);
  `_check_origin` gates every `/api/assistant/*` call (host-local +
  Origin match); read-only is enforced server-side at execute and
  per-message settings cannot bypass it; action whitelist + schema
  validation at propose + target revalidation + exact-target ack at
  execute; retrieved content is quoted evidence - action proposals
  come only from model-owned final text; md.js escapes all markup
  (`javascript:` rejected, `%22` can't break attributes); attachment
  route is id-gated, image-only, 20MB-capped, traversal 404'd;
  calculator + retrieval + requests all bounded; durable SQLite
  history verified by reload restore.
- Split audit: `assistant/` keeps zero conductor imports;
  `assistant_host/` holds all conductor-specific adapters.

### Still unverified / honest gaps

- Real speech transcription (browser SpeechRecognition audio path) -
  UI states verified, actual recognition needs a mic-enabled real
  browser profile.
- A real image attached to a sent message (vision turn): prior phase
  verified vision capability on the provider itself; an end-to-end
  attachment-on-send exchange was not re-spent this step.
- ACP `session/new` remains blocked (devin 3000.6.2 session-db
  migration) - `-p -r` is the shipped transport.

## Where it stopped

`usage-and-price-visibility` is implemented and tested (18 new tests):
every terminal exchange carries provider-reported token deltas,
extra `total_*` counters verbatim (never priced), and a first-write-wins
rate snapshot that survives later table refreshes; failed/cancelled/
partial exchanges are priced from the stored snapshot (billable, never
free, never $0-guessed); `GET .../usage` rolls up per-model groups +
unpriced + USD/estimate provenance; the chat renders a compact chip per
exchange that expands to internal-request hops, rate provenance and
'calculated estimate - not an invoice'; the header chip expands to the
conversation grand total + by-model split; settings shows resolved rates,
standard-vs-fast comparison and fetched-date provenance pre-selection.
130 assistant tests + node --check green; full `run_tests.py` rerun in
flight. Remaining per the manifest: `integration-and-design-review`,
then `follow-up-plan`.

## Where it stopped

`runtime-and-settings` (wave step of run 20261017-164321-assistant-fast-
transport): the per-conversation `transport` setting, the fast-readonly
routing rule, and the automatic CLI fallback are implemented on the
runtime/settings layer; the sibling `provider-adapter` step supplies the
concrete fast adapter - this branch wires its discovery but does not
merge it. Contract + schema v5 documented below; new `TransportTests`
(16) cover selection, read-only routing, init-failure fallback,
snapshot freezing, back-compat NULL rows, per-transport session namespacing
and reconcile adoption. Commit bf2000a.

### Verified (runtime-and-settings)

- `tests.test_assistant` 67 OK, `tests.test_assistant_runtime` 65 OK
  (49 prior + 16 TransportTests), `tests.test_assistant_host` +
  `tests.test_assistant_ui` 63 OK, `tests.test_assistant_actions` 38 OK -
  all standalone green.
- `python run_tests.py --jobs 8`: 868 tests, 5 shards failed - ALL
  failures are conductor/worktrees run-control races under a saturated
  shared box (this live wave's driver held 4 of the machine-global
  `%TEMP%/agent-conductor-slots` cap=8 and ~5 parallel `run_tests.py`
  gates from sibling worktree agents ran concurrently; steps sat
  'queued' for slots and stop/drain flags landed mid-queue -> 'skipped'
  not 'stopped', or wave-overlap timing assertions lost their overlap).
  Every failing test re-run standalone on this branch PASSES:
  StopControlTests(3), PauseRunTests(2), StepControl skip-resume,
  CumulativeElapsed driver-span, WorkerContextE2E ports. Zero failures
  in assistant code; no leaked provider processes (tests use fakes).

## Implemented contracts (fast-transport runtime + settings)

- Schema v5 (additive `ALTER`s, `PRAGMA user_version=5`):
  `conversations.transport TEXT NULL`, `exchanges.transport TEXT NULL`,
  `exchanges.fallback TEXT NULL`. NULL everywhere means "pre-feature row"
  and reads/dispatches as `cli` - no backfill, old rows keep working.
- `assistant/providers/__init__.py` - transport registry:
  `register_transport(name, factory)` + `registered_transports()`;
  `devin_print` registers `"cli"`. Adapters arriving from the sibling
  provider-adapter branch register at import - no hardcoded module name.
- `assistant/core.py` - `TRANSPORTS = ("cli","fast")`,
  `DEFAULT_TRANSPORT = "fast"`; `_clean_settings` validates/normalizes
  transport (unknown -> AssistantError 400); `defaults()`/`set_defaults()`
  carry `default.transport`; `create_conversation`/`update_settings`
  persist it; `post_message` freezes `"transport": conv.get("transport")
  or "cli"` into the per-message `settings_json` snapshot alongside
  model/effort/fast/readonly (per-message override accepted, `readonly`
  stays conversation-owned); `begin_exchange`/`note_dispatch` take
  `transport`+`fallback` kwargs and stamp the row; `transport_options()`
  asks the dispatcher for `transport_status()` and falls back to
  cli-available/fast-unavailable when none is bound;
  `exchange_view`/`list_exchanges` thaw `fallback` JSON like sources.
- `assistant/runtime.py` - `AssistantRuntime(..., providers=None)`:
  `self._providers` always has `"cli"` (positional provider), extra
  transports land by name; `self.provider` stays the CLI baseline.
  `_select_transport(snap, attachments)` runs per exchange from the
  FROZEN snapshot: NULL/missing/`"cli"` -> CLI; `"fast"` ->
  the registered fast adapter when present and `readonly` is true and
  the adapter carries image support when attachments exist - else CLI
  with a `{from,to,reason}` note persisted on `exchanges.fallback`.
  A `readonly=false` conversation asking for fast is answered by CLI -
  the fast lane is structurally read-only and the action whitelist is
  never bypassed. Init-class failures on hop 1 (send() raising - spawn
  failure/credential/endpoint - or a terminal error with no answer text)
  re-dispatch through `_to_cli()` onto a CLI session compatible with the
  conversation (`_session_for` now checks `model_uid` AND `transport`,
  so fast session ids never resume on the CLI lane and vice-versa) and
  stamp `fallback={from:fast,to:cli,reason}`; a failed first hop is also
  appended to `exchanges.requests` with `transport:"fast"`, `usage:None`
  so the usage details show the spent attempt honestly. An error AFTER
  text landed is a real failure, not an init problem - it stays
  `partial` on the fast transport (never silently re-asked). Adopted
  reconcile jobs re-dispatch on the transport recorded on the row even
  if settings moved since. `_Job.adapter` tracks the live provider so
  `_poll`/`cancel` use the answering transport. `transport_status()`
  -> per-transport `{id,name,available,note}` for the settings payload.
- `assistant/http_api.py` - `GET /api/assistant/settings` payload gains
  `transports: [{id,name,available,note}]` beside `defaults`.
- `assistant_host/conductor_adapters.py` - `open_runtime(...,
  providers=, fast_provider=)`: tests inject transports directly; on the
  default provider path `_fast_provider()` best-effort discovers a fast
  adapter - imports `assistant.providers/*` modules besides
  `devin_print`, honors `register_transport` entries other than `cli`,
  falls back to a module-level `build()/create()/provider` factory or a
  concrete `ProviderAdapter` subclass, constructing with only the seams
  it declares (argv/env/spawn_kwargs/kill/alive/workspace/spool/data
  dirs/now/log). Every failure degrades to None -> runtime routes fast
  conversations onto CLI with the reroute recorded.
- `assistant_host/modelpick.py` - `DEFAULT_SETTINGS` gains
  `transport: "fast"` so fresh installs seed the preferred lane;
  resolution still routes by capability per exchange.

### Tests (TransportTests, 16)

- fast lane answers a readonly conv on the fast adapter (exchange +
  per-request `transport` stamps, cli untouched); explicit `cli` and
  legacy NULL rows never touch fast; `readonly=false` + `transport=fast`
  lands on CLI with a recorded reason and the proposed-action flow
  lands its action row unchanged (whitelist intact); fast send() raise
  and hop-1 error both re-dispatch on CLI (`requests[]` carries the
  failed fast hop, `fallback` object thawed on the view); a mid-answer
  fast error stays `partial` on fast (no silent re-ask); no adapter
  wired -> CLI answers with 'not wired' reason; the per-message
  snapshot stays `fast` after the conv settings flip to `cli`
  (frozen-per-exchange); fast session ids never leak into the CLI lane
  and fast resume works inside the lane; a planted `running`/`fast`
  exchange re-adopts through `attach()` on the fast adapter, not the
  CLI; unknown transport values 400 on conv + defaults paths;
  `transport_status()` reports wiring honestly.
- `tests/test_assistant.py` `test_settings_transport_field`: GET
  settings returns `defaults.transport` + `transports` vocabulary;
  POST rejects an unknown transport (400), accepts `cli`/`fast` on
  defaults, conv create, and conv settings update.
- `tests/test_assistant.py` demo migration bumped to v6 (v5 is the real
  transport schema now).

## Deferred or blocked

- ACP `session/new` stalls (missing session-db migration in devin
  3000.6.2) - blocks true token streaming; `-p -r` baseline carries the
  feature. Re-check after a `devin update`.
- Whether the serve process should host a long-lived acp server child vs
  per-exchange `devin -p` spawns - DESIGN.md recommends per-exchange.
- The default seeded `fast=true` is a preference: dispatch re-verifies
  per family (swe-2 has no fast variant; resolution errors name verified
  alternatives, e.g. `gpt-6-luna-<effort>-priority`).
- Phone access is out of scope (loopback-only dashboard boundary).
