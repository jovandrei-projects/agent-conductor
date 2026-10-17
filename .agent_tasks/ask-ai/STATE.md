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

- [ ] persistent-chat-core - sqlite schema + conversation/exchange store
- [ ] view-context-and-retrieval - host context + retrieval adapters
- [ ] assistant-runtime - provider adapter + gate + usage capture
- [ ] persistent-chat-interface - surface, launchers, settings, history
- [ ] images-and-dictation - attachments + webSpeech composer
- [ ] guarded-dashboard-actions - action proposals + confirmations
- [ ] usage-and-price-visibility - per-exchange + session rollups
- [ ] integration-and-design-review
- [ ] follow-up-plan

## Where it stopped

Research + design complete and recorded in `DESIGN.md`. Next session =
`persistent-chat-core`: build `assistant/` module skeleton + sqlite
store per DESIGN.md sections 6-8. First thing: confirm schema + the
`_assert_unwatched` write guard, then migrations + store tests.

## Deferred or blocked

- ACP `session/new` stalls (missing session-db migration in devin
  3000.6.2) - blocks true token streaming; `-p -r` baseline carries the
  feature. Re-check after a `devin update`.
- Whether the serve process should host a long-lived acp server child vs
  per-exchange `devin -p` spawns - DESIGN.md recommends per-exchange.
- Phone access is out of scope (loopback-only dashboard boundary).
