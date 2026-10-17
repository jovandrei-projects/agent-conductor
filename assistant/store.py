"""Durable chat history for the dashboard assistant - sqlite3, stdlib only.

Design contract: .agent_tasks/ask-ai/DESIGN.md section 6.3. One database per
host project; the host adapter picks the path (agent-conductor uses
``data/assistant/assistant.db``). sqlite is used instead of the project's
JSON-record convention because conversations need indexed pagination,
aggregate usage rollups, UNIQUE dedupe for retry-safe writes, and
message+exchange+usage atomicity - none of which flat files give us.

Migrations are additive via ``PRAGMA user_version``: schema v1 is the
baseline; later versions ALTER/ADD only and must never drop or rewrite
existing rows. ``Store`` never imports conductor or any host module - it is
the generic half of the assistant core.
"""
import contextlib
import datetime as dt
import json
import re
import secrets
import sqlite3
import threading
import time
from pathlib import Path

SCHEMA_VERSION = 2

# Chat entity ids look like the project's request ids with a short type
# prefix: ``msg-20261017-051359-1ab2c3d4``. The regex is the authorization
# boundary for lookups - anything else is a 404 before it touches a query.
ID_RE = re.compile(r"[a-z]{2,5}-\d{8}-\d{6}-[0-9a-f]{4,16}\Z")


def iso_of(ts: float) -> str:
    """Epoch -> the naive local ISO shape conductor's now_iso() produces."""
    return dt.datetime.fromtimestamp(ts).isoformat(timespec="seconds")


def new_id(prefix: str, now: float | None = None) -> str:
    stamp = dt.datetime.fromtimestamp(
        time.time() if now is None else now)
    return f"{prefix}-{stamp:%Y%m%d-%H%M%S}-{secrets.token_hex(4)}"


# Schema v1. JSON columns hold frozen snapshots (context envelopes, settings,
# attachment lists) - they are audit data, read back verbatim, never queried
# into, so JSON-in-a-column beats a join table here.
_SCHEMA_V1 = [
    """
    CREATE TABLE conversations(
      seq INTEGER PRIMARY KEY AUTOINCREMENT,
      id TEXT NOT NULL UNIQUE,
      client_ref TEXT UNIQUE,            -- client uuid; retry-safe create
      title TEXT NOT NULL DEFAULT '',
      origin_view TEXT NOT NULL DEFAULT '{}',  -- frozen ContextEnvelope JSON
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL,
      last_active_at TEXT NOT NULL,
      model TEXT NOT NULL DEFAULT '',
      effort TEXT NOT NULL DEFAULT '',
      fast INTEGER NOT NULL DEFAULT 0,
      readonly INTEGER NOT NULL DEFAULT 1,
      provider_session_id TEXT           -- current provider session, if any
    )
    """,
    "CREATE INDEX ix_conv_active ON conversations(last_active_at DESC, id DESC)",
    """
    CREATE TABLE messages(
      seq INTEGER PRIMARY KEY AUTOINCREMENT,
      id TEXT NOT NULL UNIQUE,
      conversation_id TEXT NOT NULL REFERENCES conversations(id),
      client_msg_id TEXT,                -- client uuid; resend dedupe
      role TEXT NOT NULL CHECK(role IN ('user','assistant','system')),
      text TEXT NOT NULL DEFAULT '',
      attachments TEXT NOT NULL DEFAULT '[]',   -- JSON [{id,name,path,ctype,bytes}]
      ctx_ref TEXT,                      -- frozen per-turn ContextEnvelope JSON
      settings_json TEXT,                -- {model,effort,fast,readonly} snapshot
      status TEXT NOT NULL DEFAULT 'complete'
        CHECK(status IN ('complete','partial','pending','error','cancelled')),
      created_at TEXT NOT NULL,
      UNIQUE(conversation_id, client_msg_id)
    )
    """,
    "CREATE INDEX ix_msg_conv ON messages(conversation_id, seq)",
    """
    CREATE TABLE exchanges(
      seq INTEGER PRIMARY KEY AUTOINCREMENT,
      id TEXT NOT NULL UNIQUE,
      conversation_id TEXT NOT NULL REFERENCES conversations(id),
      user_msg_id TEXT NOT NULL REFERENCES messages(id),
      assistant_msg_id TEXT REFERENCES messages(id),
      provider_session_id TEXT,
      request_id TEXT,                   -- provider-side request id
      status TEXT NOT NULL DEFAULT 'queued'
        CHECK(status IN ('queued','running','done','partial','error',
                         'cancelled')),
      pid INTEGER,                       -- worker process, when spawned
      prompt_path TEXT, export_path TEXT,-- provider io files under data dir
      started_at TEXT, ended_at TEXT,
      prompt_t INTEGER, completion_t INTEGER, cached_t INTEGER,
      llm_steps INTEGER,
      model_uid TEXT, model_label TEXT,  -- resolved at dispatch time
      price_in REAL, price_cached REAL, price_out REAL,  -- per-1M snapshot
      price_fetched_at REAL, price_source TEXT,
      usd REAL,                          -- NULL = unknown, never 0-guess
      error TEXT,
      UNIQUE(provider_session_id, request_id)
    )
    """,
    "CREATE INDEX ix_xch_conv ON exchanges(conversation_id, seq)",
    "CREATE INDEX ix_xch_user ON exchanges(user_msg_id)",
    """
    CREATE TABLE actions(
      seq INTEGER PRIMARY KEY AUTOINCREMENT,
      id TEXT NOT NULL UNIQUE,
      conversation_id TEXT NOT NULL REFERENCES conversations(id),
      exchange_id TEXT REFERENCES exchanges(id),
      kind TEXT NOT NULL,
      payload TEXT NOT NULL DEFAULT '{}',
      state TEXT NOT NULL DEFAULT 'proposed'
        CHECK(state IN ('proposed','confirmed','denied','executed',
                        'failed')),
      requires_confirm INTEGER NOT NULL DEFAULT 1,
      confirm_text TEXT,
      result TEXT,                       -- JSON outcome or refusal note
      created_at TEXT NOT NULL,
      decided_at TEXT, executed_at TEXT
    )
    """,
    "CREATE INDEX ix_act_conv ON actions(conversation_id, seq)",
    "CREATE INDEX ix_act_xch ON actions(exchange_id, seq)",
    """
    CREATE TABLE attachments(
      seq INTEGER PRIMARY KEY AUTOINCREMENT,
      id TEXT NOT NULL UNIQUE,
      client_ref TEXT,
      conversation_id TEXT NOT NULL REFERENCES conversations(id),
      message_id TEXT REFERENCES messages(id),  -- NULL until sent
      name TEXT NOT NULL,                -- host attachment basename
      path TEXT NOT NULL,                -- absolute path under attachments dir
      bytes INTEGER, ctype TEXT,
      created_at TEXT NOT NULL,
      UNIQUE(conversation_id, client_ref)
    )
    """,
    "CREATE INDEX ix_att_msg ON attachments(message_id)",
    "CREATE INDEX ix_att_conv ON attachments(conversation_id, seq)",
    # Small KV table: global defaults + the last-active pointer the dashboard
    # restores after a reload.
    "CREATE TABLE settings(key TEXT PRIMARY KEY, value TEXT NOT NULL)",
]

# Schema v2: exchanges.sources - JSON list of the citation Source dicts the
# retrieval adapter attached to an exchange ({kind,key,label,nav,excerpt,
# bytes}); the UI renders them as modal-opening citation chips. Persisted,
# never recomputed - a citation must keep pointing at what it meant at the
# time it was recorded.
_SCHEMA_V2 = ["ALTER TABLE exchanges ADD COLUMN sources TEXT"]

# Ordered additive migrations: (new user_version, statements). v1 is the
# baseline above; append (3, [...]) for the next change - never edit v1/v2.
MIGRATIONS: tuple[tuple[int, list[str]], ...] = ((1, _SCHEMA_V1),
                                               (2, _SCHEMA_V2))


def _row(r: sqlite3.Row | None) -> dict | None:
    return dict(r) if r is not None else None


class Store:
    """One sqlite database of chat history. Thread-safe: a single guarded
    connection serializes writers; WAL keeps readers unblocked.

    `now` is the clock seam (epoch float callable) - tests inject a
    deterministic clock; the host passes conductor._now so recorded stamps
    share the project's time convention.
    `migrations` overrides MIGRATIONS for tests that need to prove an
    additive upgrade preserves rows.
    """

    def __init__(self, path, *, now=None, migrations=None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._now = now or time.time
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False,
                                     isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._migrate(migrations if migrations is not None else MIGRATIONS)

    def close(self):
        with self._lock:
            self._conn.close()

    def user_version(self) -> int:
        return self._conn.execute("PRAGMA user_version").fetchone()[0]

    def _migrate(self, migrations):
        ver = self.user_version()
        for target, stmts in sorted(migrations, key=lambda m: m[0]):
            if target <= ver:
                continue
            with self.txn() as conn:
                for s in stmts:
                    conn.execute(s)
                conn.execute(f"PRAGMA user_version={int(target)}")
            ver = target

    @contextlib.contextmanager
    def txn(self):
        """One BEGIN IMMEDIATE transaction - the unit every multi-row write
        (message+exchange+usage) commits through."""
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            else:
                self._conn.execute("COMMIT")

    def query(self, sql: str, params=()) -> list[dict]:
        with self._lock:
            return [dict(r) for r in
                    self._conn.execute(sql, params).fetchall()]

    def query_one(self, sql: str, params=()) -> dict | None:
        with self._lock:
            return _row(self._conn.execute(sql, params).fetchone())

    def execute(self, sql: str, params=()) -> int:
        """Single-statement write under the lock; returns lastrowid."""
        with self._lock:
            return self._conn.execute(sql, params).lastrowid or 0

    def iso(self) -> str:
        return iso_of(self._now())

    # ------------------------------------------------------ conversations
    def insert_conversation(self, conn, rec: dict) -> dict:
        conn.execute(
            "INSERT INTO conversations(id, client_ref, title, origin_view,"
            " created_at, updated_at, last_active_at, model, effort, fast,"
            " readonly, provider_session_id)"
            " VALUES(:id,:client_ref,:title,:origin_view,:created_at,"
            " :updated_at,:last_active_at,:model,:effort,:fast,:readonly,"
            " :provider_session_id)", rec)
        return rec

    def get_conversation(self, conv_id: str) -> dict | None:
        if not ID_RE.fullmatch(str(conv_id or "")):
            return None
        return self.query_one(
            "SELECT * FROM conversations WHERE id=?", (conv_id,))

    def get_conversation_by_ref(self, client_ref: str) -> dict | None:
        if not client_ref:
            return None
        return self.query_one(
            "SELECT * FROM conversations WHERE client_ref=?", (client_ref,))

    def list_conversations(self, limit: int = 50,
                           before: tuple | None = None) -> list[dict]:
        """Newest-first keyset page on (last_active_at, id). `before` is the
        (last_active_at, id) cursor of the last row of the previous page."""
        sql = "SELECT * FROM conversations"
        params: list = []
        if before:
            sql += (" WHERE last_active_at < ? OR"
                    " (last_active_at = ? AND id < ?)")
            params += [before[0], before[0], before[1]]
        sql += " ORDER BY last_active_at DESC, id DESC LIMIT ?"
        params.append(int(limit) + 1)      # +1 row detects the next page
        return self.query(sql, params)

    def update_conversation(self, conv_id: str, **fields) -> None:
        if not fields:
            return
        cols = ", ".join(f"{k}=?" for k in fields)
        self.execute(f"UPDATE conversations SET {cols} WHERE id=?",
                     (*fields.values(), conv_id))

    # ---------------------------------------------------------- messages
    def insert_message(self, conn, rec: dict) -> dict:
        conn.execute(
            "INSERT INTO messages(id, conversation_id, client_msg_id, role,"
            " text, attachments, ctx_ref, settings_json, status, created_at)"
            " VALUES(:id,:conversation_id,:client_msg_id,:role,:text,"
            " :attachments,:ctx_ref,:settings_json,:status,:created_at)", rec)
        return rec

    def get_message(self, msg_id: str) -> dict | None:
        if not ID_RE.fullmatch(str(msg_id or "")):
            return None
        return self.query_one("SELECT * FROM messages WHERE id=?", (msg_id,))

    def find_message(self, conn, conv_id: str,
                     client_msg_id: str) -> dict | None:
        if not client_msg_id:
            return None
        return _row(conn.execute(
            "SELECT * FROM messages WHERE conversation_id=? AND"
            " client_msg_id=?", (conv_id, client_msg_id)).fetchone())

    def list_messages(self, conv_id: str, limit: int = 50,
                      before: int | None = None) -> list[dict]:
        """Newest-first page of messages (limit+1 rows so the caller can
        detect an older page). `before` = exclusive seq cursor into older
        history; core reverses the kept rows for chronological display."""
        sql = "SELECT * FROM messages WHERE conversation_id=?"
        params: list = [conv_id]
        if before is not None:
            sql += " AND seq < ?"
            params.append(int(before))
        sql += " ORDER BY seq DESC LIMIT ?"
        params.append(int(limit) + 1)
        return self.query(sql, params)

    def update_message(self, conn, msg_id: str, **fields) -> None:
        if not fields:
            return
        cols = ", ".join(f"{k}=?" for k in fields)
        conn.execute(f"UPDATE messages SET {cols} WHERE id=?",
                     (*fields.values(), msg_id))

    def count_messages(self, conv_id: str) -> int:
        return self.query_one(
            "SELECT COUNT(*) c FROM messages WHERE conversation_id=?",
            (conv_id,))["c"]

    # ---------------------------------------------------------- exchanges
    def insert_exchange(self, conn, rec: dict) -> dict:
        conn.execute(
            "INSERT INTO exchanges(id, conversation_id, user_msg_id,"
            " assistant_msg_id, provider_session_id, request_id, status,"
            " pid, prompt_path, export_path, started_at, ended_at,"
            " prompt_t, completion_t, cached_t, llm_steps, model_uid,"
            " model_label, price_in, price_cached, price_out,"
            " price_fetched_at, price_source, usd, error, sources)"
            " VALUES(:id,:conversation_id,:user_msg_id,:assistant_msg_id,"
            " :provider_session_id,:request_id,:status,:pid,:prompt_path,"
            " :export_path,:started_at,:ended_at,:prompt_t,:completion_t,"
            " :cached_t,:llm_steps,:model_uid,:model_label,:price_in,"
            " :price_cached,:price_out,:price_fetched_at,:price_source,"
            " :usd,:error,:sources)", rec)
        return rec

    def get_exchange(self, xid: str) -> dict | None:
        if not ID_RE.fullmatch(str(xid or "")):
            return None
        return self.query_one("SELECT * FROM exchanges WHERE id=?", (xid,))

    def find_exchange_by_request(self, provider_session_id: str,
                                 request_id: str) -> dict | None:
        """The dedupe lookup: a provider (session, request) pair maps to at
        most one exchange, so a resumed provider callback joins the existing
        row instead of duplicating usage."""
        if not provider_session_id or not request_id:
            return None
        return self.query_one(
            "SELECT * FROM exchanges WHERE provider_session_id=? AND"
            " request_id=?", (provider_session_id, request_id))

    def latest_exchange_for(self, user_msg_id: str) -> dict | None:
        return self.query_one(
            "SELECT * FROM exchanges WHERE user_msg_id=? ORDER BY seq DESC"
            " LIMIT 1", (user_msg_id,))

    def list_exchanges(self, conv_id: str) -> list[dict]:
        return self.query(
            "SELECT * FROM exchanges WHERE conversation_id=? ORDER BY seq",
            (conv_id,))

    def update_exchange(self, conn, xid: str, **fields) -> None:
        if not fields:
            return
        cols = ", ".join(f"{k}=?" for k in fields)
        conn.execute(f"UPDATE exchanges SET {cols} WHERE id=?",
                     (*fields.values(), xid))

    # ------------------------------------------------------------ actions
    def insert_action(self, conn, rec: dict) -> dict:
        conn.execute(
            "INSERT INTO actions(id, conversation_id, exchange_id, kind,"
            " payload, state, requires_confirm, confirm_text, result,"
            " created_at, decided_at, executed_at)"
            " VALUES(:id,:conversation_id,:exchange_id,:kind,:payload,"
            " :state,:requires_confirm,:confirm_text,:result,:created_at,"
            " :decided_at,:executed_at)", rec)
        return rec

    def get_action(self, aid: str) -> dict | None:
        if not ID_RE.fullmatch(str(aid or "")):
            return None
        return self.query_one("SELECT * FROM actions WHERE id=?", (aid,))

    def update_action(self, conn, aid: str, **fields) -> None:
        if not fields:
            return
        cols = ", ".join(f"{k}=?" for k in fields)
        conn.execute(f"UPDATE actions SET {cols} WHERE id=?",
                     (*fields.values(), aid))

    def list_actions(self, conv_id: str) -> list[dict]:
        return self.query(
            "SELECT * FROM actions WHERE conversation_id=? ORDER BY seq",
            (conv_id,))

    # -------------------------------------------------------- attachments
    def insert_attachment(self, conn, rec: dict) -> dict:
        conn.execute(
            "INSERT INTO attachments(id, client_ref, conversation_id,"
            " message_id, name, path, bytes, ctype, created_at)"
            " VALUES(:id,:client_ref,:conversation_id,:message_id,:name,"
            " :path,:bytes,:ctype,:created_at)", rec)
        return rec

    def get_attachment(self, att_id: str) -> dict | None:
        if not ID_RE.fullmatch(str(att_id or "")):
            return None
        return self.query_one(
            "SELECT * FROM attachments WHERE id=?", (att_id,))

    def find_attachment_by_ref(self, conv_id: str,
                               client_ref: str) -> dict | None:
        if not client_ref:
            return None
        return self.query_one(
            "SELECT * FROM attachments WHERE conversation_id=? AND"
            " client_ref=?", (conv_id, client_ref))

    def list_attachments(self, conv_id: str | None = None,
                         message_id: str | None = None) -> list[dict]:
        sql, params = "SELECT * FROM attachments WHERE 1=1", []
        if conv_id:
            sql += " AND conversation_id=?"
            params.append(conv_id)
        if message_id:
            sql += " AND message_id=?"
            params.append(message_id)
        return self.query(sql + " ORDER BY seq", params)

    def bind_attachment(self, conn, att_id: str, message_id: str) -> None:
        conn.execute("UPDATE attachments SET message_id=? WHERE id=?",
                     (message_id, att_id))

    # ------------------------------------------------------------ settings
    def get_setting(self, key: str, default=None):
        row = self.query_one("SELECT value FROM settings WHERE key=?", (key,))
        return row["value"] if row else default

    def set_setting(self, key: str, value) -> None:
        self.execute(
            "INSERT INTO settings(key, value) VALUES(?,?)"
            " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value) if not isinstance(value, str) else value))

    def get_setting_json(self, key: str, default=None):
        raw = self.get_setting(key)
        if raw is None:
            return default
        try:
            return json.loads(raw)
        except ValueError:
            return default
