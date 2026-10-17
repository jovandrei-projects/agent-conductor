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
- [ ] assistant-runtime - provider adapter + gate + usage capture
      (implement ProviderAdapter as `assistant/providers/devin_print.py`;
      wire dispatcher.launch/cancel; `usage_delta` + price snapshot
      helpers already exist; `/exchanges/<id>` poll returns partial text)
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


## Where it stopped

`view-context-and-retrieval` is implemented and green: generic seams
(set_enriched, schema v2 exchanges.sources, enricher/retrieval ctor args,
exchanges route) + host adapters (ConductorContext 8 view providers +
register hook, ConductorRetrieval search/fetch/fetch_many with
interpreted windows + coverage meta, host_conductor_static.md wired
through open_core). 37+25 focused tests, full suite 595 green (8
shards), `conductor.py demo` green, both required real-record checks
passed (verification-section fetch + apartment-3d camera windows;
honest zero-match for September). Still NOT wired into `serve` -
make_dashboard_handler doesn't register the assistant routes, no
dispatcher/provider exists, exchanges stay `queued`. Next session =
`assistant-runtime` per the manifest: `providers/devin_print.py` +
dispatcher seam into open_core (retrieval/enricher already default
there; static_preamble() is the system-prompt input).

## Deferred or blocked

- ACP `session/new` stalls (missing session-db migration in devin
  3000.6.2) - blocks true token streaming; `-p -r` baseline carries the
  feature. Re-check after a `devin update`.
- Whether the serve process should host a long-lived acp server child vs
  per-exchange `devin -p` spawns - DESIGN.md recommends per-exchange.
- Phone access is out of scope (loopback-only dashboard boundary).
