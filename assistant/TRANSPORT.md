# Assistant provider transport - decision record

Produced by the `transport-investigation` step of run
`20261017-164321-assistant-fast-transport` (manifest
`manifests/20261017-164112-692a99ef-assistant-fast-transport.toml`).
Status: DECIDED. Supersedes the "ACP blocked" notes in
`.agent_tasks/ask-ai/DESIGN.md` sections 3.4, 6.7 and 9 - every claim
below was re-verified live on devin 3000.6.2 on 2026-10-17.

## Decision

**Chosen: warm ACP session transport** - `devin acp`, the CLI's own
stdio JSON-RPC (Agent Client Protocol) server, with one long-lived
server process per active conversation, prompts delivered by
`session/prompt` on a persistent session (`session/new` once,
`session/load` to re-attach across restarts). The earlier blocker
(`session/new` stalling on the `V17__subagent_heads` session-db
migration, DESIGN.md 3.4) is **gone in practice**: the migration error
still logs at session-db open on this build, but `session/new`,
`session/list`, `session/load`, `session/prompt`, `session/set_mode`,
`session/set_config_option` and `session/cancel` all completed in
repeated live probes today. New module: `assistant/providers/devin_acp.py`.

**Rejected (A): direct in-process HTTP to the model backend.** The
CLI's inference channel exists and is reachable -
`https://server.codeium.com/exa.api_server_pb.ApiServerService/GetChatMessage`,
a Connect-RPC server-streaming call whose `chatModelUid` accepts the
same uids (`gpt-6-luna-low-priority` proven in-session; the binary's
own retry log line shows the exact URL). Third-party captures
(public `devin-2api` / windsurf-reverse docs) show streamed
`deltaText`/`deltaTokens`/`usage`. Rejected because it is an
undocumented internal protocol: the request schema lives only as
protobuf descriptors embedded in a 155MB binary, calls carry
versioned metadata gates (ide-version floor, request-type enum,
uuid cascade/prompt ids), the response usage fields are not contract
guaranteed, and every CLI update can silently change any of it -
with the added ToS/maintenance risk of hand-rolling protobuf +
Connect envelope framing in stdlib Python. If ACP ever breaks, the
fallback is `devin -p`, not this.

**Rejected (B): `https://api.devin.ai` v3 REST.** Documented and
stable, but a different product surface: it creates whole Devin Cloud
sessions (remote agent, ACU billing, `devin_mode` vocabulary
normal/fast/lite/ultra/fusion - not our model uids), needs a
credential the CLI does not store (service-user `cog_` token or a
personal access token minted in the web app), and sees cloud repos,
not local state. A chat turn it is not.

`devin -p -r` (`devin_print.py`) stays shipped, untouched, as the
fallback transport and - per the manifest - the only transport allowed
on conversations where read-only is off (its action-proposal flow is
unchanged).

## Verified facts behind the decision (probes 2026-10-17, devin 3000.6.2)

Auth & credentials:
- `devin auth status`: credentials at
  `%APPDATA%\devin\credentials.toml` - a plaintext TOML
  `CredentialsFile{windsurf_api_key, api_server_url, devin_webapp_host,
  devin_api_url}` (field names confirmed in the binary). Not keychain/
  DPAPI-locked - readable by any user-context process, which is why a
  direct-HTTP adapter *could* technically reuse it; it is still the
  CLI's private protocol. `devin acp` logs its credential policy at
  startup: `ACP_BACKEND` env, else host-supplied credentials, else
  "env vars and stored CLI credentials" - the warm adapter needs
  **zero credential code**: the spawned process authenticates itself
  exactly like `devin -p` does today. Nothing is read by us, nothing
  is written to the repo.
- Honored env vars (binary strings + `--help`): `DEVIN_MODEL`,
  `DEVIN_PERMISSION_MODE`, `DEVIN_SANDBOX`, `DEVIN_API_URL`,
  `WINDSURF_API_SERVER_URL`, `ACP_BACKEND`,
  `CHISEL_MOCK_BACKEND_ADDR` (test seam).

Reachable model API & uid addressing:
- `devin models list` (54 families) is the catalog authority; the ACP
  session's `configOptions` `model` select exposes the same uid list,
  and `session/set_config_option {configId:"model",
  value:"gpt-6-luna-low-priority"}` switched a live session mid-flight
  (`currentValue` flipped; later `usage_update.size` moved 262000 ->
  1000000, matching luna's context). Model changes no longer force a
  new session - unlike `-r`, where `--model` is ignored.
- Model options carry `_meta.cognition.ai/supportsImages` flags -
  a live vision-capability source to prefer over the hardcoded
  `modelpick.VISION` map when on this transport.

Usage & streaming (per `session/prompt` response + notifications):
- Response: `{stopReason, usage:{totalTokens, inputTokens,
  outputTokens, cachedReadTokens?, cachedWriteTokens?},
  _meta:{"cognition.ai/userMessageId"}}` - **per-turn** numbers, not
  cumulative (turn 1: 14520 in/34 out/384 cachedRead; turn 2 on the
  same session reported its own 14585/22).
- Notifications on `session/update`: `agent_message_chunk` (streamed
  reply deltas - real token streaming), `agent_thought_chunk`,
  `tool_call`/`tool_call_update` (kind/title/locations/rawInput - e.g.
  `"Read file"` on `marker.txt`), `session_info_update`,
  `usage_update` (`used`,`size`, input/output/cachedRead in
  `cognition.ai/*` _meta).

Read-only gate (structural):
- `session/set_mode {modeId:"ask"}` -> the model answered a
  file-creation demand "I can't create files in Ask mode." - no
  `tool_call` of any mutating kind, no permission request, and the
  file never existed. Read tools still run under `ask` (verified in
  a separate session: a `read`-kind `tool_call` opened `marker.txt`
  and the model answered its contents). This is a stronger gate
  than `--permission-mode auto`: mutation tools are absent, not
  merely auto-rejected. Modes available: `accept-edits` (Code),
  `smart`, `ask`, `plan`, `bypass` - the adapter always sets `ask`
  on session create/load and re-asserts it per dispatch.

Cancel:
- `session/cancel` is a **notification** (no `id`; as a request it
  returns "Method not found"). Sent mid-prompt -> the pending
  `session/prompt` resolved `{stopReason:"cancelled"}`. Verified.

Session lifecycle:
- `session/load` on a **fresh `devin acp` process** re-attached an
  existing session with mode and history intact - a follow-up prompt
  answered correctly from prior-turn memory. Crash/serve-restart
  recovery is therefore real. **Caveat:** the load flow replays prior
  turns' `session/update` notifications (earlier `agent_message_chunk`
  text arrives again) - the adapter MUST drain/ignore updates until
  after `session/prompt` is issued, or replayed history will masquerade
  as live partial text.
- `devin list --format json` in the session's cwd lists ACP-created
  sessions identically to `-p` ones (`full-frill`, `prickly-exhaust`,
  `swamp-bobble` all listed; titles, timestamps, `isLocked`). `devin
  rm <id>` and `session/delete` remove them - the existing
  `conductor.py sessions` whitelist hygiene applies unchanged.
- `session/list` shows `cognition.ai/isLocked` for in-use sessions -
  the "already open elsewhere" signal (my own driving session showed
  it). A locked session must not be loaded by the adapter - skip and
  treat as session-gone.
- Residual caveat: the `V17__subagent_heads` migration error still
  logs on session-db open; it proved non-fatal on every call today
  (fresh dirs and this worktree's older db alike) but may go fatal
  again on some db state - the init-failure fallback below covers it.

## Adapter contract - `assistant/providers/devin_acp.py`

Same `ProviderAdapter` ABC (`providers/__init__.py`); `runtime.py`
needs no changes beyond provider construction.

- `send(prompt_path, *, session_id, model_uid, export_path,
  workspace)` -> `ProviderHandle`:
  - Read the prompt file the runtime already writes per hop
    (`<xid>.hN.prompt.md`); its fenced ```tool_call / ```tool_result /
    ```proposed_action contract passes through unchanged as ONE
    `{"type":"text"}` prompt block - the runtime's server-side tool
    loop is oblivious to the transport.
  - Ensure a warm session for the handle's conversation: reuse the
    live entry, else `session/load` the recorded `provider_session_id`,
    else `session/new`. After (re)attach: `session/set_mode ask`
    (read-only gate) and `session/set_config_option model=<uid>`
    when the session's current model differs.
  - Issue `session/prompt`; return `ProviderHandle{pid: acp_proc.pid,
    session_id, request_id: sid + "#h" + n, prompt_path, export_path:
    None}`.
- `poll(handle)` -> `PollResult`: drain buffered JSON-RPC lines for
  the session - concatenate `agent_message_chunk` text deltas into
  `res.text` (progressive text, a real stream - vs the 0.8s export
  tail today). Accumulate only chunks arriving AFTER this dispatch's
  `session/prompt` was issued - `session/load` replays historical
  chunks (verified), so the attach path drains before prompting.
  `done` when the prompt's JSON-RPC response arrives
  (`stopReason` end_turn/cancelled), when the acp process dies, or
  on the runtime's hop timeout. `tool_call*` updates feed an optional
  "using tools" indicator in the exchange view - informational only.
- Usage mapping (per-turn response `usage` -> cumulative machinery):
  the adapter keeps a running per-session cumulative map and reports
  it via `PollResult.raw_metrics` so `_delta_dict` keeps working
  untouched: `total_prompt_tokens += inputTokens`,
  `total_completion_tokens += outputTokens`,
  `total_cached_tokens += cachedReadTokens or 0`,
  `total_cached_write_tokens += cachedWriteTokens or 0` (lands in
  `usage.extra`, verbatim, unpriced - as designed). `llm_steps`:
  **not reported by this channel** - leave the counter absent
  (records as `None`/unknown, never a fabricated number); the
  exchanges.requests hop list remains the iteration record.
  `model_label`: the session's configured uid (configOptions
  currentValue) - there is no export `agent.model_name`; record the
  uid itself and let pricing resolve by uid.
- `cancel(handle)`: `session/cancel` notification (graceful
  `stopReason:"cancelled"`; the probe's cancelled response carried a
  zeroed usage block - partial usage is not guaranteed, record what
  arrives). Do NOT kill the acp process - the session stays warm;
  kill only when the entry is being reaped or the process is wedged.
- `sessions(workspace)`: `devin list --format json` in the workspace
  (identical set to `session/list`); `None` on failure - same
  contract as today.
- `attach(exchange)` / `alive_pid(pid)`: reconcile adoption - the
  provider pid is the acp server pid. If it died mid-prompt, the
  in-flight turn is lost (no mid-turn replay exists): the exchange
  is re-armed, `session/load` reattaches, and the prompt is re-issued
  once; `request_id` `sid#hN` + `claim_request` dedupe keep a doubly-
  landed reply from double-recording. A user message may persist
  twice inside the provider session after such a replay - visible in
  the provider transcript only, harmless to the store (record it in
  the requests row).

### Warm-pool lifecycle

- Pool: `conv_id -> {proc, session_id, model_uid, last_used}` inside
  the adapter (per serve process). One `devin acp` child per ACTIVE
  conversation - per-conversation processes sidestep cross-session
  response interleaving and bound blast radius; pool size is capped
  by the runtime's `max_concurrent` (extra exchanges queue exactly
  as today).
- Idle reap: entries unused > `IDLE_S` (30 min default) have their
  acp process terminated - session-db persists on disk, so the next
  turn re-`session/load`s with history intact. No `session/delete`
  on reap - the conversation's provider session is its resume handle.
- Serve shutdown: terminate all pool children (daemon threads +
  kill_tree on exit paths); they respawn lazily.
- Orphaned sessions: created sessions register in the workspace's
  `devin list` automatically - `exchanges.provider_session_id` +
  `conductor.py sessions` (and `devin rm`/`session/delete`) cover
  cleanup, unchanged.
- `isLocked`: a `session/list` entry locked by another owner is
  skipped - the exchange starts fresh with a digest, like
  "session-gone" today.

### Read-only guarantee

`ask` mode = no mutation tool channel exists in the provider session
at all (verified: demand to write a file got a refusal with zero
mutating tool calls; read tools still ran - needed for attachment
paths and its own file reads). Belt-and-suspenders: any inbound
`session/request_permission` (should never fire under `ask`) is
auto-answered denied. Dashboard mutations remain exclusively the
```proposed_action card flow - model text only, server-validated,
human-confirmed; the transport change touches none of it. Per the
manifest, conversations with read-only OFF route through `devin -p`
instead (selection rule, not a transport property).

### Credential handling

None. The adapter spawns `devin acp` under the same cleaned env as
`devin -p` (`clean_devin_env`); the child loads
`credentials.toml` itself. No file reads, no env capture, no
secrets in logs, prompts, exports, or the repo. An unauthenticated
child surfaces an auth-shaped JSON-RPC/stderr error -> mapped to
"provider is not authenticated" like `_AUTH_FAIL_RE` today.

### Attachments

Native: `session/prompt` accepts image blocks `{type:"image",
data:<b64>, mimeType}` - verified live (1x1 PNG answered correctly).
Send image attachments as image blocks (read bytes via the core's
upload store; cap e.g. 10MB/5 images, base64 inflation accounted),
AND keep the existing path line in the prompt text as the fallback
for oversized/unsupported payloads - the session's read tool can
still open absolute paths under `ask` mode (verified: it read
`marker.txt` outside any injected file block).

### Fallback to CLI

At `open_runtime`: spawn `devin acp` + `initialize` with a bounded
timeout (10s) - on spawn failure, handshake timeout, or an erroring
first `session/new`, swap the provider for `DevinPrintProvider`
(existing code path, untouched) and record `transport_fallback`
on the exchange row. Mid-flight acp death retries once on a
respawned server + `session/load`; a second failure fails the
exchange honestly - never a silent mid-exchange transport switch.

## Why it is faster

- No per-turn `devin -p` spawn: CLI init + credential load +
  session-db open + agent bootstrap (~1.3s observed for
  `session/new` alone, plus model/team-settings/plugin prefetch that
  `-p` pays every exchange) is paid once per conversation, not once
  per hop. Warm `session/prompt` round-trips start answering
  immediately.
- True streaming: `agent_message_chunk` deltas reach the exchange
  partial-text channel as they arrive - the UI repaints on each
  `pollX` tick with real progressive text instead of an export-file
  snapshot.
- Cheaper resume: `-r` re-loads and replays the session each spawn;
  `session/load` does it once per process lifetime.
- Richer accounting for free: cached read/write split per turn,
  per-model `supportsImages`, mid-session model switching.

## Risks & mitigations

| Risk | Mitigation |
|---|---|
| ACP is a newer, less-battered surface; the V17 migration defect already shipped once | `devin -p` fallback at init and per-exchange retry; the migration error is tolerated-but-logged today - a regression makes init fail loudly, never silently |
| Auth expiry mid-run | auth-shaped errors map to the existing "provider is not authenticated" exchange error; `devin auth status` recheck path unchanged |
| Warm-session staleness / context bloat over a long conversation | provider-side compaction is the CLI's problem; idle reap bounds process count; `session/load` failure or `isLocked` -> fresh session + history digest (existing path) |
| Rate limits / backend outage | JSON-RPC error or failed prompt surfaces as an exchange error with the real reason; `CheckUserMessageRateLimit` exists server-side; retry guidance unchanged |
| `session/cancel` is fire-and-forget (a race can still end_turn) | exchange cancel semantics already tolerate a terminal write racing a cancel (sticky terminal states); a landed reply after cancel is recorded, not lost |
| One prompt per session at a time | the runtime's per-conversation lock already serializes hops; a second concurrent `session/prompt` is never issued |
| Response/notification interleaving if the pool ever shares one acp process across convs | per-conversation processes (this design); sessionId filters in the drain loop as belt |
| Windows pipe deadlock on a verbose session | dedicated stdout drain thread (same pattern as `_OutBuffer`); stderr merged into stdout |
| CLI version drift (`configOptions` shape, method names, _meta keys) | defensive parsing - unknown update kinds ignored, usage fields defaulted; init handshake asserts `initialize` + a real `session/new`; failure falls back, never half-works |
| Session-db (`session/list`) fails on some future db state | `sessions()` returns None ("listing failed", not "gone") - same honesty rule as devin_print |
| Provider pid adoption semantics differ (pid = acp server, not a per-turn child) | reconcile adopts only when the live pid owns the exchange's recorded session; otherwise the exchange re-dispatches through `session/load` |

## What this does NOT change

- `assistant/runtime.py` hop loop, tool contract, proposals flow,
  price snapshots, request-boundary bookkeeping - identical.
- `devin_print.py` - shipped, untouched, remains the fallback and the
  only transport for non-readonly conversations.
- chat.js pollX rendering - partial text arrives through the same
  `exchange_view` channel; streaming needs no client change beyond
  the planned transport indicator.
- The read-only story: `--permission-mode auto` on `-p` and
  `set_mode ask` on ACP are the same product-level gate from two
  angles; the second layer (action whitelist + human confirm) is
  transport-independent.
