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
- [ ] persistent-chat-interface - surface, launchers, settings, history
      (assistant/static/chat.js + md.js + route registration in
      make_dashboard_handler -> assistant.http_api.dispatch)
- [ ] images-and-dictation - attachments + webSpeech composer
      (server side done: POST /api/assistant/attachments + ref binding;
      client composer + SpeechRecognition belong here)
- [ ] guarded-dashboard-actions - action proposals + confirmations
      (audit flow + readonly recheck done; implement ActionAdapter over
      conductor functions + proposal parsing + confirm cards)
- [ ] usage-and-price-visibility - per-exchange + session rollups
      (usage/price snapshot columns + rollup endpoint exist; needs
      provider-side capture + UI)
- [ ] integration-and-design-review
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

## Where it stopped

`assistant-runtime` is implemented, tested and live-smoke-verified:
the `devin -p [-r]` provider, the dispatcher runtime (tool loop,
proposals, usage deltas, reconcile/adopt), catalog-aware settings
resolution with verified defaults, and `/api/assistant/*` route
registration in `make_dashboard_handler` are all landed. Exchanges now
drive real provider turns end to end. Remaining per the manifest:
`persistent-chat-interface` (assistant/static/chat.js + md.js UI -
launchers, composer, bubble/expand, settings modal, citations, exchange
poll wiring), `images-and-dictation` (client composer + SpeechRecognition;
server side is done), `guarded-dashboard-actions` (conductor
ActionAdapter over conductor functions + confirm cards), then
`usage-and-price-visibility` UI + `integration-and-design-review` +
`follow-up-plan`.

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
