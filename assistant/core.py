"""Application interface over the chat store (DESIGN.md sections 6.1-6.3).

`AssistantCore` is the reusable conversation engine: create/list/reopen
conversations, append exchanges, page history, restore last-active,
snapshot settings, record usage, audit actions, reference attachments.
It is transport-agnostic - provider dispatch comes through the injected
`dispatcher` (the assistant-runtime step), action execution through an
`ActionAdapter` (guarded-dashboard-actions step), and every mutating write
runs the injected `guard` (the host's live-workspace/trap-5 check) first.

Retry-safety contract:
- `client_msg_id` dedupes user messages (and, while an exchange is
  queued/running/done, the exchange itself) - a resumed POST cannot
  duplicate either.
- `claim_request` maps (provider_session_id, request_id) to at most one
  exchange, so a replayed provider callback can't double-count usage.
- `complete_exchange`/`fail_exchange`/`cancel_exchange` are terminal-state
  no-ops on repeat calls - usage lands exactly once, on the row.
"""
import json
import re
import time
from pathlib import Path

from . import context as ctxmod
from .actions import KIND_RE, LAUNCH_TIER_KINDS
from .store import Store, iso_of, new_id

TEXT_MAX = 100_000            # same cap family as request bodies
TITLE_MAX = 120
HISTORY_PAGE_MAX = 200
CONV_PAGE_MAX = 100
MODEL_HISTORY_CHARS = 24_000  # default digest budget; storage keeps all
ATTACH_PER_MESSAGE_MAX = 20   # same bound as the host request form
ATTACH_PER_CONV_MAX = 200     # uploads are never deleted; bound the pile

SETTINGS_KEYS = ("model", "effort", "fast", "readonly")
SLUG_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
EXCHANGE_LIVE = ("queued", "running")
EXCHANGE_TERMINAL = ("done", "partial", "error", "cancelled")
MSG_TERMINAL = ("complete", "partial", "error", "cancelled")


class AssistantError(ValueError):
    """ValueError so it slots beside the host's RequestError handling;
    `status` maps straight onto HTTP."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


USAGE_COUNTERS = ("prompt_t", "completion_t", "cached_t", "llm_steps")
PRICE_COUNTERS = ("prompt_t", "completion_t", "cached_t")


def price_usage(usage: dict, rates: dict | None) -> float | None:
    """Same formula as conductor.estimate_cost: cached is a share of input
    priced at the cache-read rate. Returns None = unknown (never a
    zero-guess): no rates -> None, and a usage dict with NO token counter
    at all -> None (a missing report is not a free request)."""
    if not usage or not rates:
        return None
    rin, rcr, rout = rates.get("in"), rates.get("cached"), rates.get("out")
    if rin is None or rout is None:
        return None
    if all(usage.get(k) is None for k in PRICE_COUNTERS):
        return None
    p = usage.get("prompt_t") or 0
    c = usage.get("cached_t") or 0
    o = usage.get("completion_t") or 0
    return round((max(p - c, 0) * rin + c * (rcr or 0) + o * rout)
                 / 1_000_000, 9)        # micro-dollar precision kept


def _clean_settings(patch: dict) -> dict:
    """Validate a {model,effort,fast,readonly} partial update."""
    if not isinstance(patch, dict):
        raise AssistantError("settings must be an object")
    unknown = set(patch) - set(SETTINGS_KEYS)
    if unknown:
        raise AssistantError(f"unknown settings: {sorted(unknown)}")
    out = {}
    for k in ("model", "effort"):
        if k in patch:
            v = str(patch[k] or "").strip()
            if v and not SLUG_RE.fullmatch(v):
                raise AssistantError(f"invalid {k}")
            out[k] = v
    for k in ("fast", "readonly"):
        if k in patch:
            out[k] = bool(patch[k])
    return out


class AssistantCore:
    """The store plus the host's seams. `store` is required; everything
    else is an adapter the host supplies:

    - guard(*paths): raises AssistantError/RequestError(409) when a live
      agent watches a workspace containing the paths (trap-5 guard); the
      assistant db dir is always passed. None = standalone/testing.
    - attachment_saver(bytes) -> {name, path}: host's image validator.
    - attachments_dir: Path the stored `path` must resolve under.
    - dispatcher: object with .launch(exchange) / .cancel(exchange) the
      runtime provides; None leaves exchanges 'queued'.
    - actions: ActionAdapter for proposal validation/execution.
    - retrieval: RetrievalAdapter for bounded evidence lookups; None = the
      runtime answers without retrieval. Stored, not called here - the
      dispatcher drives search()/fetch() during prompt assembly.
    - enricher: callable(envelope dict) -> dict producing the server-side
      `enriched` block for each posted message; a failure is recorded
      honestly in the envelope, never silently dropped or fatal.
    - catalog: callable -> {models:[{uid,label,prices...}]} for /models.
    - settings_validator: callable(effective {model,effort,fast,readonly})
      that raises AssistantError when the combination cannot be dispatched
      (unknown family, effort/fast variant the family never published,
      vision-less model + images). Server-side validation per the manifest;
      None = shape-only checking (tests, standalone).
    - now: epoch clock seam (tests inject; host passes conductor._now).
    """

    def __init__(self, store: Store, *, guard=None, attachment_saver=None,
                 attachments_dir=None, dispatcher=None, actions=None,
                 retrieval=None, enricher=None, catalog=None,
                 settings_validator=None, now=None):
        self.store = store
        self._guard = guard
        self._save_attach = attachment_saver
        self._attach_dir = (Path(attachments_dir) if attachments_dir
                            else None)
        self._dispatcher = dispatcher
        self._actions = actions
        self.retrieval = retrieval
        self._enricher = enricher
        self._catalog = catalog
        self._validator = settings_validator
        self._now = now or time.time

    # ---------------------------------------------------------- plumbing
    def _iso(self) -> str:
        return iso_of(self._now())

    def _guarded(self, *paths: Path) -> None:
        if self._guard:
            self._guard(*paths)

    def guard_paths(self, *paths) -> None:
        """Public guard entry for the runtime: spool/workspace writes under
        the data dir take the same trap-5 check as row writes."""
        self._guarded(*paths)

    def _validate_settings(self, effective: dict) -> None:
        """Server-side settings check against the host's live catalog.
        Runs on EFFECTIVE settings (stored + patch merged), so a patch
        that only flips `fast` still validates the real combination."""
        if self._validator:
            self._validator(dict(effective))

    def _conv(self, conv_id: str) -> dict:
        conv = self.store.get_conversation(conv_id)
        if conv is None:
            raise AssistantError("Conversation not found", 404)
        return conv

    def _xid(self, xid: str) -> dict:
        x = self.store.get_exchange(xid)
        if x is None:
            raise AssistantError("Exchange not found", 404)
        return x

    # ----------------------------------------------------- conversations
    def defaults(self) -> dict:
        """Global settings defaults new conversations inherit."""
        return {"model": self.store.get_setting("default.model", ""),
                "effort": self.store.get_setting("default.effort", ""),
                "fast": self.store.get_setting_json("default.fast", False),
                "readonly":
                self.store.get_setting_json("default.readonly", True)}

    def set_defaults(self, patch: dict) -> dict:
        self._guarded(self.store.path.parent)
        vals = _clean_settings(patch)
        self._validate_settings({**self.defaults(), **vals})
        with self.store.txn():
            for k, v in vals.items():
                self.store.set_setting(f"default.{k}", v)
        return self.defaults()

    def create_conversation(self, origin=None, *, title="", client_ref=None,
                            settings=None) -> dict:
        """New conversation. `origin` is the frozen origin envelope (view +
        window identity + refs) - dict or pre-built envelope. `client_ref`
        makes a retried POST return the existing row instead of a dup."""
        try:
            env = ctxmod.envelope(
                (origin or {}).get("view", "dashboard"),
                refs=(origin or {}).get("refs"),
                filters=(origin or {}).get("filters"),
                window=(origin or {}).get("window"),
                at=(origin or {}).get("at") or self._iso())
        except ctxmod.ContextError as exc:
            raise AssistantError(str(exc)) from exc
        vals = {**self.defaults(), **_clean_settings(settings or {})}
        self._validate_settings(vals)
        title = str(title or "").strip()[:TITLE_MAX]
        now = self._iso()
        rec = {"id": new_id("conv", self._now()),
               "client_ref": str(client_ref)[:128] if client_ref else None,
               "title": title, "origin_view": ctxmod.freeze(env),
               "created_at": now, "updated_at": now, "last_active_at": now,
               "model": vals["model"], "effort": vals["effort"],
               "fast": int(vals["fast"]),
               "readonly": int(vals["readonly"]),
               "provider_session_id": None}
        self._guarded(self.store.path.parent)
        with self.store.txn() as conn:
            if client_ref:
                have = conn.execute(
                    "SELECT * FROM conversations WHERE client_ref=?",
                    (rec["client_ref"],)).fetchone()
                if have:
                    return {**self.get_conversation(have["id"]),
                            "deduped": True}
            self.store.insert_conversation(conn, rec)
            self.store.set_setting("last_active", rec["id"])
        return self.get_conversation(rec["id"])

    def list_conversations(self, limit: int = 50,
                           before: tuple | None = None) -> dict:
        limit = max(1, min(int(limit or 50), CONV_PAGE_MAX))
        rows = self.store.list_conversations(limit, before)
        nxt = None
        if len(rows) > limit:
            rows, last = rows[:limit], rows[limit - 1]
            nxt = [last["last_active_at"], last["id"]]
        for r in rows:
            r["origin_view"] = ctxmod.thaw(r.get("origin_view"))
        return {"items": rows, "next_before": nxt}

    def get_conversation(self, conv_id: str) -> dict:
        conv = dict(self._conv(conv_id))
        conv["origin_view"] = ctxmod.thaw(conv.get("origin_view"))
        return conv

    def open_conversation(self, conv_id: str, *, limit: int = 50,
                          before: int | None = None) -> dict:
        """Reopen a conversation: bumps last_active (restore-on-reload
        ordering) and returns the record plus the newest message page."""
        conv = self._conv(conv_id)
        self._guarded(self.store.path.parent)
        now = self._iso()
        with self.store.txn() as conn:
            conn.execute(
                "UPDATE conversations SET last_active_at=?, updated_at=?"
                " WHERE id=?", (now, now, conv_id))
            self.store.set_setting("last_active", conv_id)
        return {"conversation": self.get_conversation(conv_id),
                **self.list_messages(conv_id, limit=limit, before=before)}

    def last_active_conversation(self) -> dict | None:
        cid = self.store.get_setting("last_active")
        if cid:
            conv = self.store.get_conversation(cid)
            if conv:
                return self.get_conversation(cid)
        row = self.store.query_one(
            "SELECT id FROM conversations ORDER BY last_active_at DESC,"
            " id DESC LIMIT 1")
        return self.get_conversation(row["id"]) if row else None

    def update_settings(self, conv_id: str, patch: dict) -> dict:
        """Per-conversation settings (model/effort/fast/readonly). Applies
        to the NEXT exchange only - history keeps its per-message
        snapshots."""
        conv = self._conv(conv_id)
        vals = _clean_settings(patch)
        self._validate_settings(
            {"model": conv["model"], "effort": conv["effort"],
             "fast": bool(conv["fast"]), "readonly": bool(conv["readonly"]),
             **vals})
        self._guarded(self.store.path.parent)
        now = self._iso()
        fields = {"updated_at": now, "last_active_at": now}
        for k, v in vals.items():
            fields[k] = int(v) if k in ("fast", "readonly") else v
        with self.store.txn() as conn:
            conn.execute(
                "UPDATE conversations SET " +
                ", ".join(f"{k}=?" for k in fields) + " WHERE id=?",
                (*fields.values(), conv_id))
        return self.get_conversation(conv["id"])

    # -------------------------------------------------------- messages
    def list_messages(self, conv_id: str, *, limit: int = 50,
                      before: int | None = None) -> dict:
        self._conv(conv_id)
        limit = max(1, min(int(limit or 50), HISTORY_PAGE_MAX))
        # store rows come newest-first; the +1 row detects an older page
        rows = self.store.list_messages(conv_id, limit, before)
        nxt = None
        if len(rows) > limit:
            rows = rows[:limit]
            nxt = rows[-1]["seq"]      # oldest seq kept on this page
        rows.reverse()                 # chronological for the client
        return {"items": rows, "next_before": nxt}

    def post_message(self, conv_id: str, text: str, *, client_msg_id,
                     client_ctx=None, attachments=None,
                     settings=None) -> dict:
        """Append a user message and queue its exchange (202 semantics).

        Retry-safe: a `client_msg_id` already stored returns the existing
        message + its live/done exchange; when that exchange ended in
        error/cancelled the resend queues a FRESH assistant turn on the
        same user message - never a duplicate message.
        """
        conv = self._conv(conv_id)
        text = str(text or "")
        has_files = bool(attachments)
        if not (text.strip() or has_files):
            raise AssistantError("Message text required")
        if len(text) > TEXT_MAX:
            raise AssistantError(f"Message over {TEXT_MAX} characters")
        if client_msg_id and not isinstance(client_msg_id, str):
            raise AssistantError("client_msg_id must be a string")
        if client_msg_id and not re.fullmatch(
                r"[A-Za-z0-9_-]{8,64}", client_msg_id):
            raise AssistantError("Invalid client_msg_id")

        try:
            env = ctxmod.new_envelope_from_client(client_ctx or {},
                                                  at=self._iso())
            if self._enricher:
                # Server-side enrichment replaces whatever `enriched` the
                # client claimed - its refs are re-resolved against real
                # state, its text never becomes authority. A failed lookup
                # is recorded honestly instead of killing the question.
                try:
                    ctxmod.set_enriched(env, self._enricher(dict(env)))
                except Exception as exc:      # noqa: BLE001 - degrade, note
                    ctxmod.set_enriched(
                        env, {"context_error":
                              f"view-context enrichment failed: "
                              f"{type(exc).__name__}: {exc}"[:300]})
            frozen_ctx = ctxmod.freeze(env)
        except ctxmod.ContextError as exc:
            raise AssistantError(str(exc)) from exc
        per_msg = _clean_settings(settings or {})
        # `readonly` is a conversation-level capability - a message can
        # override the model snapshot but must never claim a permission
        # the conversation does not have (the stored snapshot feeds the
        # reply badge AND the runtime's read-only prompt line).
        per_msg.pop("readonly", None)
        snap = {**{k: conv[k] for k in ("model", "effort")},
                "fast": bool(conv["fast"]),
                "readonly": bool(conv["readonly"]),
                **per_msg}
        self._validate_settings(snap)
        now = self._iso()

        self._guarded(self.store.path.parent)
        with self.store.txn() as conn:
            msg = self.store.find_message(conn, conv_id, client_msg_id)
            if msg is not None:
                x = self.store.latest_exchange_for(msg["id"])
                if x is not None and x["status"] not in ("error",
                                                         "cancelled"):
                    return {"message": msg, "exchange": x, "deduped": True}
            else:
                att_rows = self._bind_attachments(
                    conn, conv_id, attachments or [])
                msg = self.store.insert_message(conn, {
                    "id": new_id("msg", self._now()),
                    "conversation_id": conv_id,
                    "client_msg_id": client_msg_id,
                    "role": "user", "text": text,
                    "attachments": json.dumps(att_rows,
                                              ensure_ascii=False),
                    "ctx_ref": frozen_ctx,
                    "settings_json": json.dumps(snap, sort_keys=True),
                    "status": "complete", "created_at": now})
                for a in att_rows:       # link after the msg id exists
                    self.store.bind_attachment(conn, a["id"], msg["id"])

            reply = self.store.insert_message(conn, {
                "id": new_id("msg", self._now()),
                "conversation_id": conv_id, "client_msg_id": None,
                "role": "assistant", "text": "", "attachments": "[]",
                "ctx_ref": frozen_ctx,
                "settings_json": json.dumps(snap, sort_keys=True),
                "status": "pending", "created_at": now})
            x = self.store.insert_exchange(conn, {
                "id": new_id("xch", self._now()),
                "conversation_id": conv_id, "user_msg_id": msg["id"],
                "assistant_msg_id": reply["id"],
                "provider_session_id": conv.get("provider_session_id"),
                "request_id": None, "status": "queued", "pid": None,
                "prompt_path": None, "export_path": None,
                "started_at": None, "ended_at": None,
                "prompt_t": None, "completion_t": None, "cached_t": None,
                "llm_steps": None, "model_uid": None, "model_label": None,
                "price_in": None, "price_cached": None, "price_out": None,
                "price_fetched_at": None, "price_source": None,
                "usd": None, "error": None, "sources": None,
                "requests": None})
            conn.execute(
                "UPDATE conversations SET last_active_at=?, updated_at=?"
                " WHERE id=?", (now, now, conv_id))
            self.store.set_setting("last_active", conv_id)

        result = {"message": self.store.get_message(msg["id"]),
                  "exchange": self.store.get_exchange(x["id"]),
                  "assistant_message": self.store.get_message(reply["id"]),
                  "deduped": False}
        if self._dispatcher:
            try:
                self._dispatcher.launch(dict(result["exchange"]))
            except Exception as exc:      # noqa: BLE001 - record, stay queued
                self.mark_launch_failed(x["id"], str(exc)[:400])
        return result

    def _bind_attachments(self, conn, conv_id: str, ids: list) -> list[dict]:
        """Resolve client-supplied attachment ids to row refs for the
        message's attachments JSON."""
        if not isinstance(ids, list) or len(ids) > ATTACH_PER_MESSAGE_MAX:
            raise AssistantError(
                f"attachments must be a list of at most "
                f"{ATTACH_PER_MESSAGE_MAX} ids")
        rows = []
        for aid in ids:
            att = self.store.get_attachment(str(aid))
            if att is None or att["conversation_id"] != conv_id:
                raise AssistantError("Attachment not found", 404)
            if att["message_id"]:
                raise AssistantError(
                    "Attachment is already bound to a message", 409)
            rows.append({"id": att["id"], "name": att["name"],
                         "path": att["path"], "ctype": att["ctype"],
                         "bytes": att["bytes"]})
        return rows

    # -------------------------------------------------------- exchanges
    def begin_exchange(self, xid: str, *, pid=None, provider_session_id=None,
                       prompt_path=None, export_path=None,
                       model_uid=None) -> dict:
        x = self._xid(xid)
        if x["status"] != "queued":
            return x
        self._guarded(self.store.path.parent)
        with self.store.txn() as conn:
            self.store.update_exchange(
                conn, xid, status="running", started_at=self._iso(),
                pid=pid,
                provider_session_id=provider_session_id
                or x.get("provider_session_id"),
                prompt_path=prompt_path, export_path=export_path,
                model_uid=model_uid or x.get("model_uid"))
        return self.store.get_exchange(xid)

    def claim_request(self, xid: str, provider_session_id: str,
                      request_id: str) -> dict:
        """Associate a provider (session, request) pair with an exchange.
        UNIQUE(session, request) makes a replayed callback return the
        already-owning exchange - the caller treats `dup=True` as 'usage
        already counted elsewhere'."""
        x = self._xid(xid)
        owner = self.store.find_exchange_by_request(provider_session_id,
                                                    request_id)
        if owner is not None:
            return {**owner, "dup": owner["id"] != xid}
        self._guarded(self.store.path.parent)
        with self.store.txn() as conn:
            self.store.update_exchange(
                conn, xid, provider_session_id=provider_session_id,
                request_id=request_id)
        return {**self.store.get_exchange(xid), "dup": False}

    def note_dispatch(self, xid: str, *, pid=None, prompt_path=None,
                      export_path=None, model_uid=None) -> dict:
        """Record the live dispatch of a later hop (begin_exchange covers
        the queued->running first hop only). The resolved model_uid is
        stamped at dispatch so session-continuity checks compare against
        what actually launched, not only completed rows."""
        x = self._xid(xid)
        self._guarded(self.store.path.parent)
        with self.store.txn() as conn:
            self.store.update_exchange(
                conn, xid, pid=pid, prompt_path=prompt_path,
                export_path=export_path,
                model_uid=model_uid or x.get("model_uid"))
        return self.store.get_exchange(xid)

    def note_provider_session(self, xid: str, session_id: str) -> dict:
        """Record the provider session a dispatch landed on - on the
        exchange AND the conversation (its resume handle for later turns).
        Called as soon as the provider reports the id, live or terminal."""
        x = self._xid(xid)
        if not session_id:
            return x
        self._guarded(self.store.path.parent)
        with self.store.txn() as conn:
            self.store.update_exchange(
                conn, xid, provider_session_id=session_id)
            conn.execute(
                "UPDATE conversations SET provider_session_id=? WHERE id=?",
                (session_id, x["conversation_id"]))
        return self.store.get_exchange(xid)

    def set_price_snapshot(self, xid: str, price: dict | None) -> dict:
        """Stamp the dispatch-time pricing inputs on the exchange -
        FIRST WRITE WINS per column so a later rate-table update (or a
        re-armed reconcile pass) can never rewrite what this dispatch
        was priced against. The terminal write (complete/fail) may
        still overwrite with a label-matched set - that's the final
        accounting, not history drift."""
        x = self._xid(xid)
        if not isinstance(price, dict) or not price:
            return x
        pairs = {"price_in": "in", "price_cached": "cached",
                 "price_out": "out", "price_fetched_at": "fetched_at",
                 "price_source": "source"}
        fields = {col: price.get(src) for col, src in pairs.items()
                  if x.get(col) is None and price.get(src) is not None}
        if not fields:
            return x
        self._guarded(self.store.path.parent)
        with self.store.txn() as conn:
            self.store.update_exchange(conn, xid, **fields)
        return self.store.get_exchange(xid)

    def record_request(self, xid: str, entry: dict) -> dict:
        """Append one internal request-boundary record (a provider dispatch
        hop: spawn ids, timing, per-hop usage delta + cumulative snapshot)
        to exchanges.requests. Audit data - appended even on terminal rows
        so a crash mid-finalize never loses a boundary."""
        x = self._xid(xid)
        self._guarded(self.store.path.parent)
        with self.store.txn() as conn:
            cur = self.store.get_exchange(xid) or {}
            try:
                reqs = json.loads(cur["requests"]) \
                    if isinstance(cur.get("requests"), str) else []
            except ValueError:
                reqs = []
            reqs.append(entry)
            self.store.update_exchange(
                conn, xid, requests=json.dumps(reqs, ensure_ascii=False))
        return self.store.get_exchange(xid)

    def session_totals(self, provider_session_id: str,
                       exclude_xid: str | None = None) -> dict | None:
        """Last recorded cumulative provider counters for a session - the
        baseline a resumed dispatch's usage delta is measured against."""
        return self.store.last_session_totals(provider_session_id,
                                              exclude_xid)

    def inflight_exchanges(self) -> list[dict]:
        """Exchanges stuck in a live state - the reconcile pass on runtime
        start re-owns them (adopt a live pid, re-dispatch a dead one)."""
        return self.store.query(
            "SELECT * FROM exchanges WHERE status IN ('queued','running')"
            " ORDER BY seq")

    def update_partial(self, xid: str, text: str,
                       usage: dict | None = None) -> dict:
        """Progressive text while the provider runs (export-poll
        transport). Stored on the assistant message so reloads see it."""
        x = self._xid(xid)
        if x["status"] not in EXCHANGE_LIVE:
            return self.exchange_view(xid)
        self._guarded(self.store.path.parent)
        fields = {"text": str(text or "")[:TEXT_MAX], "status": "partial"}
        with self.store.txn() as conn:
            self.store.update_message(conn, x["assistant_msg_id"], **fields)
            uf = _usage_fields(usage)
            if uf:
                self.store.update_exchange(conn, xid, **uf)
        return self.exchange_view(xid)

    def record_sources(self, xid: str, sources: list) -> dict:
        """Attach the retrieval adapter's Source list to a live exchange -
        the citation set the answer draws on. Sources are evidence, not
        instructions; they're stored verbatim (already host-bounded)."""
        x = self._xid(xid)
        if x["status"] in EXCHANGE_TERMINAL:
            return x
        if not isinstance(sources, list):
            raise AssistantError("sources must be a list")
        self._guarded(self.store.path.parent)
        with self.store.txn() as conn:
            self.store.update_exchange(
                conn, xid,
                sources=json.dumps(sources, ensure_ascii=False))
        return self.store.get_exchange(xid)

    def complete_exchange(self, xid: str, text: str, *, usage=None,
                          model=None, price=None, usd="auto",
                          request_id=None, sources=None, error=None,
                          status="done") -> dict:
        """Terminal write: assistant text + token/price snapshot, one
        transaction. Repeating a completion is a no-op - usage can never
        double-land."""
        x = self._xid(xid)
        if status not in ("done", "partial"):
            raise AssistantError(f"invalid completion status {status!r}")
        if x["status"] in ("done", "partial", "error", "cancelled"):
            return self.exchange_view(xid)          # terminal is sticky
        self._guarded(self.store.path.parent)
        now = self._iso()
        usage = _merged_usage(x, usage)
        price = price or {}
        eff_price = {src: (price.get(src) if price.get(src) is not None
                           else x.get(col))
                     for src, col in (("in", "price_in"),
                                      ("cached", "price_cached"),
                                      ("out", "price_out"))}
        if usd == "auto":
            usd = price_usage(usage, eff_price)
        with self.store.txn() as conn:
            self.store.update_message(
                conn, x["assistant_msg_id"],
                text=str(text or "")[:TEXT_MAX],
                status=("complete" if status == "done" else status))
            self.store.update_exchange(
                conn, xid, status=status, ended_at=now,
                request_id=request_id or x.get("request_id"),
                prompt_t=usage["prompt_t"],
                completion_t=usage["completion_t"],
                cached_t=usage["cached_t"],
                llm_steps=usage["llm_steps"],
                model_uid=(model or {}).get("uid") or x.get("model_uid"),
                model_label=(model or {}).get("label")
                or x.get("model_label"),
                price_in=price.get("in", x.get("price_in")),
                price_cached=price.get("cached", x.get("price_cached")),
                price_out=price.get("out", x.get("price_out")),
                price_fetched_at=price.get("fetched_at",
                                           x.get("price_fetched_at")),
                price_source=price.get("source", x.get("price_source")),
                sources=(json.dumps(sources, ensure_ascii=False)
                         if sources is not None else x.get("sources")),
                error=(str(error)[:800] if error else x.get("error")),
                usd=usd)
            conn.execute(
                "UPDATE conversations SET last_active_at=?, updated_at=? "
                "WHERE id=?", (now, now, x["conversation_id"]))
        return self.exchange_view(xid)

    def fail_exchange(self, xid: str, error: str, *, partial_text=None,
                      usage=None, price=None, usd="auto") -> dict:
        """Terminal 'error' write. A failed dispatch still consumed
        provider tokens, so its usage lands (billable) and, when a price
        snapshot is available, its calculated usd too - 'failed' never
        means 'free' or 'invisible'."""
        x = self._xid(xid)
        if x["status"] in ("done", "error", "cancelled"):
            return self.exchange_view(xid)
        self._guarded(self.store.path.parent)
        now = self._iso()
        fields = _usage_fields(usage)
        if isinstance(price, dict):
            fields.update({col: v for col, v in (
                ("price_in", price.get("in")),
                ("price_cached", price.get("cached")),
                ("price_out", price.get("out")),
                ("price_fetched_at", price.get("fetched_at")),
                ("price_source", price.get("source")))
                if v is not None})
        if usd == "auto":
            usd = price_usage(_merged_usage(x, usage),
                              _row_price({**x, **fields}))
        if usd is not None:
            fields["usd"] = usd
        with self.store.txn() as conn:
            self.store.update_message(
                conn, x["assistant_msg_id"], status="error",
                text=(str(partial_text)[:TEXT_MAX]
                      if partial_text else
                      self.store.get_message(
                          x["assistant_msg_id"])["text"]))
            self.store.update_exchange(
                conn, xid, status="error", ended_at=now,
                error=str(error or "failed")[:800], **fields)
            conn.execute(
                "UPDATE conversations SET last_active_at=?, updated_at=? "
                "WHERE id=?", (now, now, x["conversation_id"]))
        return self.exchange_view(xid)

    def cancel_exchange(self, xid: str) -> dict:
        """Cancel a live exchange: dispatcher kill first (so a completed
        provider can't race the cancel), then the terminal row update.
        Usage-so-far was landed by the dispatcher's partial update; when
        a price snapshot exists the partial consumption is priced too -
        a cancelled request can still be billed."""
        x = self._xid(xid)
        if x["status"] not in EXCHANGE_LIVE:
            return self.exchange_view(xid)
        self._guarded(self.store.path.parent)
        if self._dispatcher:
            try:
                self._dispatcher.cancel(dict(x))
            except Exception:            # noqa: BLE001 - kill is best-effort
                pass
        now = self._iso()
        fields = {"status": "cancelled", "ended_at": now}
        usd = price_usage(_merged_usage(x, None), _row_price(x))
        if usd is not None:
            fields["usd"] = usd
        with self.store.txn() as conn:
            self.store.update_message(conn, x["assistant_msg_id"],
                                      status="cancelled")
            self.store.update_exchange(conn, xid, **fields)
        return self.exchange_view(xid)

    def mark_launch_failed(self, xid: str, error: str) -> dict:
        with self.store.txn() as conn:
            self.store.update_exchange(conn, xid, error=str(error)[:800])
        return self.store.get_exchange(xid)

    def exchange_view(self, xid: str) -> dict:
        """Poller payload: exchange state + the assistant message's current
        text (partial while running)."""
        x = self._xid(xid)
        out = dict(x)
        reply = (self.store.get_message(x["assistant_msg_id"])
                 if x.get("assistant_msg_id") else None)
        out["text"] = (reply or {}).get("text", "")
        out["message_status"] = (reply or {}).get("status")
        for col in ("sources", "requests"):
            try:    # stored JSON lists -> ready-to-render dicts
                out[col] = (json.loads(out[col])
                            if isinstance(out.get(col), str)
                            else (out.get(col) or []))
            except ValueError:
                out[col] = []
        out["usage_extra"] = _usage_extra(out["requests"])
        return out

    def list_exchanges(self, conv_id: str) -> list[dict]:
        self._conv(conv_id)
        rows = self.store.list_exchanges(conv_id)
        for r in rows:      # citation + request-boundary JSON thaws
            for col in ("sources", "requests"):
                try:
                    r[col] = (json.loads(r[col])
                              if isinstance(r.get(col), str)
                              else (r.get(col) or []))
                except ValueError:
                    r[col] = []
            r["usage_extra"] = _usage_extra(r["requests"])
        return rows

    def usage(self, conv_id: str) -> dict:
        """Per-exchange usage rows + conversation rollup. The rows carry
        their internal request records (thawed) so the breakdown can
        expand hops server-side-identically; `by_model` keeps multi-model
        conversations honest (a mixed total would blend rates); unpriced
        exchanges are COUNTED separately - never silently zero. A
        cancelled row only counts as unpriced when it actually consumed
        tokens (nothing to price = nothing reported missing)."""
        rows = self.list_exchanges(conv_id)
        tot = {"prompt_t": 0, "completion_t": 0, "cached_t": 0,
               "llm_steps": 0, "usd": 0.0, "exchanges": len(rows),
               "unpriced": 0}
        by_model = {}
        price_seen = {"fetched_at": 0.0, "source": None}
        for r in rows:
            for k in USAGE_COUNTERS:
                tot[k] += r.get(k) or 0
            terminal = r["status"] in EXCHANGE_TERMINAL
            has_usage = any(r.get(k) is not None for k in PRICE_COUNTERS)
            key = (r.get("model_uid") or r.get("model_label")
                   or "(unresolved)")
            grp = by_model.setdefault(key, {
                "model_uid": r.get("model_uid"),
                "model_label": r.get("model_label"), "exchanges": 0,
                "prompt_t": 0, "completion_t": 0, "cached_t": 0,
                "llm_steps": 0, "usd": 0.0, "unpriced": 0})
            grp["exchanges"] += 1
            for k in USAGE_COUNTERS:
                grp[k] += r.get(k) or 0
            if r.get("usd") is None:
                if terminal and (r["status"] != "cancelled" or has_usage):
                    tot["unpriced"] += 1
                    grp["unpriced"] += 1
            else:
                tot["usd"] += r["usd"]
                grp["usd"] += r["usd"]
            if r.get("price_fetched_at") and \
                    r["price_fetched_at"] > price_seen["fetched_at"]:
                price_seen = {"fetched_at": r["price_fetched_at"],
                              "source": r.get("price_source")}
            grp["usd"] = round(grp["usd"], 9)
        tot["usd"] = round(tot["usd"], 9)
        tot["by_model"] = by_model
        tot["currency"] = "USD"            # rates are USD/1M tokens
        tot["cost_kind"] = "calculated"    # estimate, never an invoice
        if price_seen["fetched_at"]:
            tot["price_fetched_at"] = price_seen["fetched_at"]
            tot["price_source"] = price_seen["source"]
        return {"exchanges": rows, "totals": tot}

    def model_history(self, conv_id: str,
                      max_chars: int = MODEL_HISTORY_CHARS) -> dict:
        """The bounded tail supplied to a model: complete exchanges,
        chronological, newest-first-then-reversed until the char budget.
        The store always keeps full history; `truncated` says how much was
        left out so the runtime can note it honestly."""
        self._conv(conv_id)
        rows = self.store.query(
            "SELECT role, text, status, seq FROM messages"
            " WHERE conversation_id=? AND role IN ('user','assistant')"
            " AND status != 'pending' ORDER BY seq DESC", (conv_id,))
        picked, used = [], 0
        for r in rows:
            t = r["text"] or ""
            if used + len(t) > max_chars and picked:
                break
            picked.append(r)
            used += len(t)
            if used >= max_chars:
                break
        picked.reverse()
        return {"items": [{"role": r["role"], "text": r["text"]}
                          for r in picked],
                "truncated": len(picked) < len(rows),
                "total_messages": len(rows)}

    # -------------------------------------------------------- attachments
    def save_upload(self, conv_id: str, data: bytes,
                    client_ref=None) -> dict:
        """Validate + persist image bytes through the host's saver, then
        record the reference. `client_ref` dedupes a retried upload; the
        stored path must resolve under the host's attachments dir. The
        saver is authoritative on empties/size/magic bytes - its errors
        keep their HTTP status through the AssistantError translation so
        the host's RequestError never leaks across the seam."""
        self._conv(conv_id)
        if self._save_attach is None:
            raise AssistantError("Attachments not configured", 409)
        if client_ref:
            have = self.store.find_attachment_by_ref(conv_id,
                                                     str(client_ref)[:128])
            if have:
                return dict(have, deduped=True)
        if len(self.store.list_attachments(conv_id)) \
                >= ATTACH_PER_CONV_MAX:
            raise AssistantError(
                f"This conversation already has {ATTACH_PER_CONV_MAX} "
                "attachments", 409)
        self._guarded(self.store.path.parent)
        try:
            saved = self._save_attach(bytes(data))  # host validates bytes
        except AssistantError:
            raise
        except Exception as exc:                 # noqa: BLE001 - host's own
            raise AssistantError(                # error type stays outside
                str(exc), getattr(exc, "status", 400)) from exc
        path = Path(saved["path"]).resolve()
        if self._attach_dir and not path.is_relative_to(
                self._attach_dir.resolve()):
            raise AssistantError("Attachment path outside attachments dir")
        rec = {"id": new_id("att", self._now()),
               "client_ref": str(client_ref)[:128] if client_ref else None,
               "conversation_id": conv_id, "message_id": None,
               "name": saved["name"], "path": str(path),
               "bytes": len(data), "ctype": saved.get("ctype"),
               "created_at": self._iso()}
        with self.store.txn() as conn:
            self.store.insert_attachment(conn, rec)
        return self.store.get_attachment(rec["id"])

    def read_upload(self, att_id: str) -> tuple[bytes, str]:
        """Serve stored attachment bytes back for previews. Authorization
        is the row itself: the id must exist (ID_RE is the lookup
        boundary), its conversation must still exist, and the recorded
        path must resolve under the host's attachments dir - the client
        never hands us a filesystem path. Only image/* (or octet-stream
        for rows that predate ctype recording) is ever returned, so a
        tampered row can't turn this into an HTML-serving endpoint."""
        att = self.store.get_attachment(att_id)
        if att is None:
            raise AssistantError("Attachment not found", 404)
        self._conv(att["conversation_id"])
        path = Path(att["path"]).resolve()
        if self._attach_dir and not path.is_relative_to(
                self._attach_dir.resolve()):
            raise AssistantError("Attachment path outside attachments dir")
        try:
            data = path.read_bytes()
        except OSError:
            raise AssistantError("Attachment data is missing", 404)
        ctype = str(att.get("ctype") or "")
        if not ctype.startswith("image/"):
            ctype = "application/octet-stream"
        return data, ctype

    # ------------------------------------------------------------ actions
    def _record_action(self, conv_id: str, kind: str, payload,
                       exchange_id, *, explain=None, state="proposed",
                       requires_confirm=1, confirm_text=None,
                       result=None, decided_at=None) -> dict:
        """Insert one audited action row. `payload` is JSON-capped so a
        rejected proposal can't stash an unbounded blob as 'audit'."""
        try:
            packed = json.dumps(payload or {}, ensure_ascii=False)
        except (TypeError, ValueError):
            packed = json.dumps({"unserializable": True})
        # 150KB: the largest legit payload is edit_request carrying a
        # republished proposal TOML (validate caps it at 120KB + fields).
        if len(packed) > 150_000:
            packed = json.dumps({"truncated": True, "bytes": len(packed)})
        now = self._iso()
        rec = {"id": new_id("act", self._now()),
               "conversation_id": conv_id, "exchange_id": exchange_id,
               "kind": kind, "payload": packed, "state": state,
               "requires_confirm": int(requires_confirm),
               "confirm_text": confirm_text,
               "result": (json.dumps(result, ensure_ascii=False)
                          if result is not None else None),
               "explain": (str(explain)[:500] if explain else None),
               "created_at": now, "decided_at": decided_at,
               "executed_at": None}
        self._guarded(self.store.path.parent)
        with self.store.txn() as conn:
            self.store.insert_action(conn, rec)
        return self.store.get_action(rec["id"])

    def propose_action(self, conv_id: str, kind: str, payload: dict, *,
                       exchange_id=None, explain=None,
                       record_failure: bool = False) -> dict:
        """Record a model-emitted proposal. The adapter whitelist decides
        if the kind exists and what confirmation it needs - no adapter,
        no proposals. ``record_failure`` is the runtime's proposal path:
        instead of raising, a rejected proposal lands as a 'failed' row
        so it stays visible + audited and can never be executed."""
        self._conv(conv_id)
        if self._actions is None:
            raise AssistantError("Actions not enabled", 409)
        kind = str(kind or "").strip()
        try:
            if not re.fullmatch(KIND_RE, kind):
                raise AssistantError("Invalid action kind")
            spec = self._actions.spec_for(kind)
            if spec is None:
                raise AssistantError(
                    f"Action kind {kind!r} is not allowed", 403)
            clean = self._actions.validate(kind, payload or {})
        except Exception as exc:                  # noqa: BLE001 - audit
            if not record_failure:
                if isinstance(exc, AssistantError):
                    raise
                # duck-typed status passthrough - the host's RequestError
                # carries one; this module never imports the host.
                raise AssistantError(
                    str(exc)[:400],
                    getattr(exc, "status", None) or 400) from exc
            return self._record_action(
                conv_id, kind or "(malformed)", payload, exchange_id,
                explain=explain, state="failed", requires_confirm=1,
                result={"error": f"{type(exc).__name__}: {exc}"[:400],
                        "refused": "proposal rejected at validation"},
                decided_at=self._iso())
        return self._record_action(
            conv_id, kind, clean, exchange_id, explain=explain,
            requires_confirm=int(spec.requires_confirm
                                 or kind in LAUNCH_TIER_KINDS),
            confirm_text=spec.confirm_text or None)

    def decide_action(self, aid: str, approve: bool) -> dict:
        """Human decision on the confirm card: proposed -> confirmed or
        denied. A 'confirmed' row can still be retracted (nothing has
        executed yet) but never goes back to proposed; terminal rows are
        idempotent no-ops. A confirmed action still re-checks read-only
        at execute time - 'turn read-only back on' must invalidate
        pending writes."""
        a = self._action(aid)
        if a["state"] == "confirmed" and approve:
            return a
        if a["state"] not in ("proposed", "confirmed"):
            return a
        self._guarded(self.store.path.parent)
        with self.store.txn() as conn:
            self.store.update_action(
                conn, aid, state="confirmed" if approve else "denied",
                decided_at=self._iso())
        return self.store.get_action(aid)

    def execute_action(self, aid: str, *, ack: str | None = None) -> dict:
        """Run a proposed/confirmed action through the host adapter.
        Read-only is re-read HERE - never earlier - and model output,
        retrieved text, and prompt text are not in its authority.

        Launch-tier kinds additionally demand `ack`: the adapter's
        expected exact-target string, echoed by the client from the
        separate confirmation step that showed target + effect. A bare
        execute on a launch class always refuses."""
        a = self._action(aid)
        if a["state"] in ("denied", "executed", "failed"):
            return a
        self._guarded(self.store.path.parent)
        conv = self._conv(a["conversation_id"])
        if conv["readonly"]:
            with self.store.txn() as conn:
                self.store.update_action(
                    conn, aid,
                    result=json.dumps({"refused": "read-only enabled"}))
            raise AssistantError(
                "Conversation is read-only; enable actions in settings "
                "to run this", 409)
        if self._actions is None:
            raise AssistantError("Actions not enabled", 409)
        if a["requires_confirm"] and a["state"] != "confirmed":
            raise AssistantError("Action needs confirmation first", 409)
        if a["kind"] in LAUNCH_TIER_KINDS:
            expected = self.expected_ack(aid)
            if expected is not None and ack != expected:
                raise AssistantError(
                    "Confirm the exact target first - this action needs "
                    "a separate explicit confirmation", 409)
        try:
            result = self._actions.execute(a)
        except Exception as exc:                 # noqa: BLE001 - audit it
            with self.store.txn() as conn:
                self.store.update_action(
                    conn, aid, state="failed",
                    result=json.dumps({"error": str(exc)[:800]}),
                    executed_at=self._iso())
            raise AssistantError(f"Action failed: {exc}", 500) from exc
        with self.store.txn() as conn:
            self.store.update_action(
                conn, aid, state="executed",
                result=json.dumps(result or {}, ensure_ascii=False),
                executed_at=self._iso())
        return self.store.get_action(aid)

    def _action(self, aid: str) -> dict:
        a = self.store.get_action(aid)
        if a is None:
            raise AssistantError("Action not found", 404)
        return a

    def _expected_ack_for(self, a: dict) -> str | None:
        if a["kind"] not in LAUNCH_TIER_KINDS or self._actions is None:
            return None
        try:
            return self._actions.expected_ack(dict(a))
        except Exception:                          # noqa: BLE001
            return None

    def expected_ack(self, aid: str) -> str | None:
        """The exact-target string a launch-tier action must see echoed
        at execute time; the client's second confirmation step displays
        it verbatim. None for non-launch kinds / missing adapter."""
        a = self.store.get_action(aid)
        return None if a is None else self._expected_ack_for(a)

    def action_catalog(self) -> str:
        """The adapter's prompt-facing whitelist text ('' = none)."""
        if self._actions is None:
            return ""
        try:
            return self._actions.describe() or ""
        except Exception:                          # noqa: BLE001
            return ""

    def list_actions(self, conv_id: str) -> list[dict]:
        self._conv(conv_id)
        rows = self.store.list_actions(conv_id)
        for a in rows:
            a["expected_ack"] = self._expected_ack_for(a)
        return rows

    # ------------------------------------------------------------- misc
    def catalog(self) -> dict:
        """Model/price data for the settings modal via the host's catalog
        adapter (model_options + price_table merge)."""
        if self._catalog is None:
            return {"models": []}
        c = self._catalog()
        # the host may return the full annotated payload (models +
        # default_resolution) or a bare models list
        return c if isinstance(c, dict) else {"models": c}

    def close(self):
        self.store.close()


def _usage_fields(usage: dict | None) -> dict:
    """Only the counter keys actually present - an absent key must never
    overwrite a stored value with NULL."""
    if not usage:
        return {}
    return {k: usage[k] for k in USAGE_COUNTERS
            if usage.get(k) is not None}


def _merged_usage(x: dict, usage: dict | None) -> dict:
    """Effective usage for a terminal pricing write: the explicit `usage`
    delta wins per counter, falling back to counters already landed on
    the row (partial updates stamp usage mid-run). An explicit None is
    'not reported', not a zero - it never overrides stored data."""
    usage = usage or {}
    return {k: (usage.get(k) if usage.get(k) is not None else x.get(k))
            for k in USAGE_COUNTERS}


def _row_price(row: dict) -> dict:
    """The price snapshot stored on the exchange row -> the rates dict
    price_usage understands."""
    return {"in": row.get("price_in"), "cached": row.get("price_cached"),
            "out": row.get("price_out")}


def _usage_extra(requests) -> dict:
    """Sum the provider's NON-canonical counters (cache writes, image or
    summarization billing when a provider reports them under extra
    `total_*` keys) across a row's internal request records - kept raw
    (no pricing semantics invented) so the UI can show them verbatim."""
    out = {}
    for ent in requests or []:
        ex = (ent.get("usage") or {}).get("extra") \
            if isinstance(ent, dict) else None
        if isinstance(ex, dict):
            for k, v in ex.items():
                if isinstance(v, (int, float)):
                    out[k] = out.get(k, 0) + v
    return out
