"""`devin acp` warm-session provider transport.

One provider session = one long-lived `devin acp` child process the
adapter keeps pooled across turns. The exchange's ``send()`` maps to an
ACP ``session/prompt`` round on an already-initialized session, so a warm
turn skips CLI init/auth/model-load entirely (probes 2026-10-17: ~4s warm
turn vs ~15-30s for a fresh ``devin -p`` spawn).

Verified on devin 3000.6.2 (live probes, scratch dir):
- ``initialize`` + ``initialized`` handshake; client capabilities
  ``fs.read=false, fs.write=false, terminal=false`` - the client carries
  NO file/terminal channel, so the read-only guarantee is structural
  (``session/request_permission`` and every other inbound request are
  answered with a JSON-RPC refusal too).
- ``session/new`` / ``session/load`` return in ~1-3s. The old
  ``V17__subagent_heads`` migration ERROR still logs but is non-fatal -
  sessions create, load, and register in ``devin list`` normally.
- ``session/set_config_option {configId:"mode",value:"ask"}`` - Ask mode
  cannot write files; combined with the absent client write/exec
  channels the adapter is read-only by construction, not by flag.
- ``session/set_config_option {configId:"model",value:<uid>}`` takes the
  same uids ``devin models list`` publishes (gpt-6-luna-low-priority
  verified). Model is applied whenever the requested uid differs from
  the session's current config value - mid-session switching works on
  this transport, unlike ``-r`` where ``--model`` is ignored.
- ``session/prompt`` streams ``session/update`` notifications:
  ``agent_message_chunk`` (the reply stream - delta OR cumulative
  snapshot, both handled), ``agent_thought_chunk`` (reasoning - used as
  progressive text until the first message chunk), ``usage_update``
  (per-turn inputTokens/outputTokens/cachedReadTokens plus
  ``_meta.cognition.ai/responseDimensions`` CUMULATIVE metrics:
  input_tokens/output_tokens/cached_input_tokens/agent_messages and the
  Metric "model" label). The response carries
  ``{stopReason, usage{...}, _meta{userMessageId}}``.
- ``session/cancel`` is a notification; the in-flight prompt request then
  resolves quickly.
- ``session/load`` replays session history as session/update traffic -
  chunks carry ``_meta.cognition.ai/timestamp`` so replays dedupe cleanly.
- Sessions register in ``devin list`` under the spawn cwd, so the
  ``sessions()`` registry contract works unchanged. A session locked by
  another live process (``isLocked``) is never loaded - it is treated
  like session-gone and a fresh session starts.

Usage contract (assistant/TRANSPORT.md): PollResult.raw_metrics stays a
CUMULATIVE per-session map so the runtime's ``_delta_dict`` math is
unchanged - total_prompt_tokens/total_completion_tokens/
total_cached_tokens from per-turn ``usage{inputTokens, outputTokens,
cachedReadTokens, cachedWriteTokens}`` (cachedWrite lands as
total_cached_write_tokens in ``usage.extra``, unpriced), or the server's
own cumulative responseDimensions when present (they survive adapter
restarts). ``total_steps`` is deliberately ABSENT - ACP reports no LLM
step count and the contract is to record NULL, never fabricate.

Failure surfaces: spawn/handshake failures raise ``ProviderUnavailable``
(``AuthUnavailable`` for credential-shaped failures) so the runtime can
fall back to the ``devin -p`` transport; mid-turn process death marks the
turn done with an error and whatever text streamed. Killing a pooled
process is safe - the session persists in the session store and the next
send() ``session/load``s it back.

Attach: an exchange whose recorded pid is dead re-issues its prompt
after ``session/load`` (the decision record's "re-arm + re-issue once";
the provider transcript may carry the user message twice - harmless to
the store). A still-live recorded pid is OUR orphaned acp child - its
stdio is untalkable, so it is killed (never leaked) and the session
re-attached the same way.
"""
import base64
import json
import re
import subprocess
import threading
import time
from pathlib import Path

from . import (AuthUnavailable, PollResult, ProviderAdapter,
               ProviderHandle, ProviderUnavailable)

OUT_BUF_MAX = 262_144          # stderr narration cap kept per connection
HANDSHAKE_S = 20.0             # initialize round-trip budget
SESSION_S = 60.0               # session/new|load budget (first load ~2.5s)
CONFIG_S = 15.0                # set_config_option budget
CANCEL_GRACE_S = 4.0           # session/cancel -> kill escalation window
ADOPT_DRAIN_S = 3.0            # replay-drain window after session/load
IDLE_TTL_S = 1800.0            # idle pooled conns are reaped (session persists)
REAP_S = 30.0                  # reaper scan period
PROMPT_READ_MAX = 2_000_000    # prompt file bytes accepted into one turn


class AcpError(Exception):
    """A JSON-RPC error or protocol failure on the ACP channel."""

    def __init__(self, message, code=None):
        super().__init__(message)
        self.code = code


class _OutBuffer:
    """Bounded append-only text sink (stderr narration tail)."""

    def __init__(self, cap=OUT_BUF_MAX):
        self._lock = threading.Lock()
        self._parts: list[str] = []
        self._size = 0
        self._cap = cap

    def feed(self, chunk: str) -> None:
        if not chunk:
            return
        with self._lock:
            self._parts.append(chunk)
            self._size += len(chunk)
            while self._size > self._cap and self._parts:
                self._size -= len(self._parts.pop(0))

    def tail(self, n=8000) -> str:
        with self._lock:
            raw = "".join(self._parts)
        return raw[-n:]


class _AcpConn:
    """One `devin acp` process: ndjson stdio, pending-request table, and a
    notification callback. Inbound (server->client) requests are refused
    by default - the client grants no fs/terminal/permission capability,
    so any that still arrive get an explicit JSON-RPC error rather than
    silence."""

    def __init__(self, proc, err: _OutBuffer):
        self.proc = proc
        self.err = err
        self._wlock = threading.Lock()
        self._next_id = 0
        self._pending: dict[int, dict] = {}
        self._done: dict[int, dict] = {}   # responded, not yet consumed
        self.on_message = None            # cb(dict) for notifications
        self.on_request = None            # cb(dict) -> result|raises AcpError
        self.closed = threading.Event()

    # ---------------- plumbing
    def start(self) -> None:
        threading.Thread(target=self._read_loop, daemon=True,
                         name="devin-acp-read").start()
        if self.proc.stderr is not None:
            threading.Thread(target=self._err_loop, daemon=True,
                             name="devin-acp-err").start()

    def _read_loop(self) -> None:
        try:
            for ln in self.proc.stdout:
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    msg = json.loads(ln)
                except ValueError:
                    self.err.feed("[acp] non-json line: " + ln[:200] + "\n")
                    continue
                self._dispatch(msg)
        except (OSError, ValueError):
            pass
        finally:
            self._close_all()

    def _err_loop(self) -> None:
        try:
            for chunk in self.proc.stderr:
                self.err.feed(chunk)
        except (OSError, ValueError, AttributeError):
            pass

    def _dispatch(self, msg: dict) -> None:
        if msg.get("method") and msg.get("id") is not None:
            self._answer_request(msg)       # server->client request
            return
        if "id" in msg and ("result" in msg or "error" in msg):
            rid = msg.get("id")
            with self._wlock:
                st = self._pending.pop(rid, None)
                if st is not None:
                    st["msg"] = msg
                    self._done[rid] = msg
                    st["ev"].set()
            return
        cb = self.on_message
        if cb:
            try:
                cb(msg)
            except Exception:               # noqa: BLE001 - never kill reader
                pass

    def _answer_request(self, msg: dict) -> None:
        """Refuse every inbound request - structural read-only."""
        rid = msg.get("id")
        cb = self.on_request
        if cb is not None:
            try:
                result = cb(msg)
            except AcpError as exc:
                self._send_raw({"jsonrpc": "2.0", "id": rid,
                                "error": {"code": exc.code or -32603,
                                          "message": str(exc)}})
                return
            except Exception:               # noqa: BLE001
                self._send_raw({"jsonrpc": "2.0", "id": rid,
                                "error": {"code": -32603,
                                          "message": "client error"}})
                return
            self._send_raw({"jsonrpc": "2.0", "id": rid, "result": result})
            return
        self._send_raw({"jsonrpc": "2.0", "id": rid,
                        "error": {"code": -32601,
                                  "message": "client capability not "
                                             "granted"}})

    def _send_raw(self, obj: dict) -> None:
        data = json.dumps(obj) + "\n"
        try:
            with self._wlock:
                self.proc.stdin.write(data)
                self.proc.stdin.flush()
        except (OSError, ValueError, BrokenPipeError):
            self._close_all()

    # ---------------- rpc
    def request(self, method: str, params: dict | None,
                timeout: float) -> dict:
        rid = self.request_async(method, params)
        with self._wlock:
            st = self._pending[rid]
        if not st["ev"].wait(timeout):
            with self._wlock:
                self._pending.pop(rid, None)
            raise AcpError(f"{method} timed out after {timeout}s")
        msg = st["msg"] or {}
        if "error" in msg:
            err = msg["error"] or {}
            raise AcpError(f"{method} failed: {err.get('code')} "
                           f"{err.get('message')}", code=err.get("code"))
        return msg.get("result") or {}

    def request_async(self, method: str, params: dict | None) -> int:
        """Queue a request without waiting; poll_pending() reports the
        decoded response message (or None while in flight)."""
        with self._wlock:
            self._next_id += 1
            rid = self._next_id
            self._pending[rid] = {"ev": threading.Event(), "msg": None}
        self._send_raw({"jsonrpc": "2.0", "id": rid, "method": method,
                        "params": params or {}})
        if self.closed.is_set():
            with self._wlock:
                st = self._pending.get(rid)
                if st is not None:
                    st["msg"] = {"error": {"code": -32000,
                                           "message": "acp connection "
                                                      "closed"}}
                    st["ev"].set()
        return rid

    def poll_pending(self, rid: int) -> dict | None:
        """None while in flight; the decoded response once delivered."""
        with self._wlock:
            return self._done.pop(rid, None)

    def notify(self, method: str, params: dict | None) -> None:
        self._send_raw({"jsonrpc": "2.0", "method": method,
                        "params": params or {}})

    def _close_all(self) -> None:
        if self.closed.is_set():
            return
        self.closed.set()
        with self._wlock:
            pend = self._pending
            self._pending = {}
        for rid, st in pend.items():
            st["msg"] = {"error": {"code": -32000,
                                   "message": "acp connection closed"}}
            st["ev"].set()
            with self._wlock:
                self._done[rid] = st["msg"]


class _Turn:
    """One in-flight session/prompt round."""

    def __init__(self, rid: int = 0):
        self.rid = rid
        self.parts: list[str] = []          # concatenated agent text
        self.thought_tail = ""
        self.seen_chunks: set = set()       # (kind, ts, text) replay dedup
        self.done = threading.Event()
        self.result: dict | None = None     # prompt result (stopReason..)
        self.error: str | None = None       # terminal error text

    def text(self) -> str:
        return "".join(self.parts).strip()


def _chunk_text(content) -> str | None:
    if isinstance(content, dict) and content.get("type") == "text":
        t = content.get("text")
        return t if isinstance(t, str) else None
    return None


# responseDimensions cumulative metrics -> ATIF-keyed counters. No
# "agent messages" mapping: llm_steps has no ACP analog and the contract
# leaves it absent, never fabricated.
_DIM_MAP = {
    "input_tokens": "total_prompt_tokens",
    "output_tokens": "total_completion_tokens",
    "cached_input_tokens": "total_cached_tokens",
}

_IMG_MIMES = {"image/png", "image/jpeg", "image/gif", "image/webp"}
IMG_MAX_BYTES = 10_000_000     # per-image cap for native image blocks
IMG_MAX_COUNT = 5              # per-prompt cap


def _dims_to_cum(dims) -> dict | None:
    """responseDimensions -> cumulative ATIF-keyed counters, or None when
    the session reports no cumulative metrics."""
    out: dict | None = None
    for d in dims or []:
        uid = (d or {}).get("uid")
        key = _DIM_MAP.get(uid)
        if not key:
            continue
        cm = ((d or {}).get("kind") or {}).get("CumulativeMetric")
        if not isinstance(cm, dict):
            continue
        try:
            val = int(float(cm.get("value") or 0))
        except (TypeError, ValueError):
            continue
        if out is None:
            out = {}
        out[key] = val
    return out


def _config_model_uid(config_options) -> str | None:
    """The `model` select's currentValue (a model uid, per the decision
    record: record the uid itself, let pricing resolve by uid)."""
    for opt in config_options or []:
        if (opt or {}).get("id") == "model" and opt.get("currentValue"):
            return str(opt["currentValue"])
    return None


_AUTH_FAIL_RE = re.compile(
    r"not logged in|not authenticated|no credentials|unauthorized|"
    r"401\b|authentication required|devin auth", re.I)


class _WarmSession:
    """A pooled `devin acp` connection bound to one provider session."""

    def __init__(self, conn: _AcpConn, sid: str, workspace: str):
        self.conn = conn
        self.sid = sid
        self.workspace = workspace
        self.cum: dict[str, int] = {}       # cumulative ATIF-keyed counters
        self.model_label: str | None = None
        self.turn: _Turn | None = None      # live dispatch
        self.replay: _Turn | None = None    # session/load history catch
        self.last_active = 0.0
        self.adopted = False                # built via session/load

    def apply_update(self, msg: dict) -> None:
        params = msg.get("params") or {}
        if params.get("sessionId") != self.sid:
            return
        upd = params.get("update") or {}
        kind = upd.get("sessionUpdate")
        if kind == "usage_update":
            meta = upd.get("_meta") or {}
            dims = meta.get("cognition.ai/responseDimensions")
            cum = _dims_to_cum(dims)
            if cum:
                self.cum.update(cum)         # absolute cumulative values
            else:
                for src, dst in (("cognition.ai/inputTokens",
                                  "total_prompt_tokens"),
                                 ("cognition.ai/outputTokens",
                                  "total_completion_tokens"),
                                 ("cognition.ai/cachedReadTokens",
                                  "total_cached_tokens")):
                    v = meta.get(src)
                    if isinstance(v, (int, float)) and v:
                        # per-turn usage_update without dims: accumulate
                        self.cum[dst] = int(self.cum.get(dst) or 0) + int(v)
            return
        turn = self.turn or self.replay
        if turn is None:
            return
        if kind in ("agent_message_chunk", "agent_thought_chunk"):
            text = _chunk_text(upd.get("content"))
            if text is None:
                return
            ts = str(((upd.get("_meta") or {})
                      .get("cognition.ai/timestamp")) or "")
            sig = (kind, ts, text)
            # Only REPLAYED history dedupes: replayed chunks carry a
            # real timestamp and re-send verbatim, so a (kind,ts,text)
            # hit is a straggler. A live turn's own chunks are never
            # deduped against each other - identical fragments recur
            # legitimately inside one answer (markdown repeats
            # bullets/backticks constantly) and live deltas may carry
            # no timestamp or a shared one, which collapses distinct
            # repeats to the same sig and silently eats text (the
            # 'garbled answer' corruption).
            if ts and sig in turn.seen_chunks:
                return                       # replayed history
            if turn is self.replay:
                turn.seen_chunks.add(sig)
            if kind == "agent_thought_chunk":
                turn.thought_tail = text
                return
            cur = turn.text()
            # ACP sends either deltas or cumulative snapshots; cover both
            if cur and text.startswith(cur):
                turn.parts = [text]
            else:
                turn.parts.append(text)
        elif kind == "user_message_chunk":
            # a user turn boundary - during replay everything after the
            # last user message is the answer we adopt
            if turn is self.replay:
                turn.parts = []


class _AcpHandle(ProviderHandle):
    def __init__(self, *, session=None, turn=None, adopted_row=None, **kw):
        self.session = session              # _WarmSession | None
        self.turn = turn                    # _Turn | None
        self.adopted_row = adopted_row      # dict when attach()ed
        super().__init__(**kw)


class DevinAcpProvider(ProviderAdapter):
    """ProviderAdapter over pooled `devin acp` stdio processes.

    Injected seams (same convention as DevinPrintProvider):
    - argv: devin command prefix (["devin"], or a test's fake binary)
    - env(): child environment (host passes clean_devin_env)
    - spawn: Popen callable (host adds hidden_subprocess_kwargs)
    - kill(proc): tree kill; kill_pid(pid): orphan kill (conductor's)
    - alive(pid): liveness probe for ADOPTED handle bookkeeping
    - run_list(cwd): `devin list --format json` runner -> (rc, stdout)
    - clock/sleep: test seams
    - workspace: assistant workspace dir (attach() derives it from the
      exchange's export path when unset)
    """

    def __init__(self, argv=("devin",), *, env=None, spawn=None,
                 spawn_kwargs=None, kill=None, kill_pid=None, alive=None,
                 run_list=None, clock=time.time, sleep=time.sleep,
                 workspace=None, idle_ttl=IDLE_TTL_S,
                 cancel_grace=CANCEL_GRACE_S, handshake_s=HANDSHAKE_S,
                 session_s=SESSION_S, config_s=CONFIG_S,
                 adopt_drain_s=ADOPT_DRAIN_S):
        self._argv = list(argv)
        self._env = env
        self._spawn = spawn or subprocess.Popen
        self._spawn_kwargs = dict(spawn_kwargs or {})
        self._kill = kill or self._default_kill
        self._kill_pid = kill_pid or self._default_kill_pid
        self._alive = alive or self._default_alive
        self._run_list = run_list or self._default_list
        self._clock = clock
        self._sleep = sleep
        self._workspace = str(workspace) if workspace else None
        self._idle_ttl = float(idle_ttl)
        self._cancel_grace = float(cancel_grace)
        self._handshake_s = float(handshake_s)
        self._session_s = float(session_s)
        self._config_s = float(config_s)
        self._adopt_drain = float(adopt_drain_s)
        self._lock = threading.Lock()
        self._pool: dict[str, _WarmSession] = {}
        self._reaper_started = False

    # ============================================================ send
    def send(self, prompt_path: str, *, session_id: str | None,
             model_uid: str, export_path: str,
             workspace, attachments=None) -> ProviderHandle:
        blocks = self._prompt_blocks(prompt_path, attachments)
        ws = self._session_for(session_id, model_uid, str(workspace))
        turn = self._issue_prompt(ws, blocks)
        # export_path stays None on the handle - there is no ATIF export
        # on this transport (decision record); usage rides raw_metrics.
        return _AcpHandle(pid=ws.conn.proc.pid, session_id=ws.sid,
                          prompt_path=str(prompt_path), export_path=None,
                          session=ws, turn=turn)

    def _prompt_blocks(self, prompt_path, attachments) -> list[dict]:
        body = Path(prompt_path).read_text(encoding="utf-8",
                                           errors="replace")
        if len(body) > PROMPT_READ_MAX:
            body = body[:PROMPT_READ_MAX]
        blocks = [{"type": "text", "text": body}]
        # Image attachments go out as native ACP image blocks (the
        # prompt text already carries their absolute paths as the
        # fallback - the session's read tool opens them under `ask`).
        # Non-image or oversized payloads simply skip the native block;
        # the path line remains the refusal-free fallback.
        sent = 0
        for a in attachments or []:
            mime = str((a or {}).get("mime_type") or "")
            path = (a or {}).get("path")
            if mime not in _IMG_MIMES or sent >= IMG_MAX_COUNT or not path:
                continue
            try:
                raw = Path(path).read_bytes()
            except (OSError, TypeError):
                continue
            if len(raw) > IMG_MAX_BYTES:
                continue
            blocks.append({"type": "image",
                           "data": base64.b64encode(raw).decode("ascii"),
                           "mimeType": mime})
            sent += 1
        return blocks

    def _issue_prompt(self, ws: _WarmSession, blocks: list[dict]) -> _Turn:
        turn = _Turn()
        if ws.replay is not None:
            # replayed-history chunk sigs: a post-drain straggler must not
            # masquerade as this turn's streamed text
            turn.seen_chunks = set(ws.replay.seen_chunks)
        ws.turn = turn
        try:
            rid = ws.conn.request_async(
                "session/prompt",
                {"sessionId": ws.sid, "prompt": blocks})
        except Exception:
            ws.turn = None
            raise
        turn.rid = rid
        ws.last_active = self._clock()
        if ws.conn.closed.is_set():
            ws.turn = None
            raise ProviderUnavailable(
                "devin acp connection closed before the turn started")
        return turn

    def _session_for(self, sid: str | None, model_uid: str,
                     workspace: str) -> _WarmSession:
        with self._lock:
            ws = self._pool.get(sid) if sid else None
            if ws is not None and (ws.conn.closed.is_set()
                                   or ws.conn.proc.poll() is not None):
                self._drop(ws)
                ws = None
        if ws is not None:
            if ws.turn is not None and not ws.turn.done.is_set():
                raise ProviderUnavailable(
                    f"provider session {ws.sid} already has a turn in "
                    "flight")
            ws.last_active = self._clock()
            return ws
        return self._spawn_session(sid, model_uid, workspace)

    def _spawn_session(self, sid: str | None, model_uid: str,
                       workspace: str) -> _WarmSession:
        conn = self._open_conn(workspace)
        try:
            self._handshake(conn)
            ws = None
            adopted = False
            if sid and self._is_locked(workspace, sid):
                sid = None       # held by a foreign live process - skip
            if sid:
                # register before load so replayed history lands
                ws = _WarmSession(conn, sid, workspace)
                ws.replay = _Turn()
                with self._lock:
                    self._pool[sid] = ws
                try:
                    res = self._session_open(
                        conn, "session/load",
                        {"sessionId": sid, "cwd": workspace,
                         "mcpServers": []})
                    adopted = True
                except ProviderUnavailable as exc:
                    if isinstance(exc, AuthUnavailable):
                        raise
                    with self._lock:
                        self._drop(ws)
                    ws = None
                    # stale/deleted session id -> honest fresh session
                    sid = None
                else:
                    ws.model_label = _config_model_uid(
                        res.get("configOptions")) or ws.model_label
                    # session/load replays history - give stragglers a
                    # bounded drain before any prompt is issued
                    self._sleep(self._adopt_drain)
            if sid is None:
                res = self._session_open(conn, "session/new",
                                         {"cwd": workspace,
                                          "mcpServers": []})
                sid = res.get("sessionId")
                if not sid:
                    raise ProviderUnavailable(
                        "devin acp session/new returned no session id")
                ws = _WarmSession(conn, sid, workspace)
                with self._lock:
                    self._pool[sid] = ws
                ws.model_label = _config_model_uid(
                    res.get("configOptions")) or ws.model_label
            assert ws is not None
            ws.adopted = adopted
            self._session_open(
                conn, "session/set_config_option",
                {"sessionId": sid, "configId": "mode", "value": "ask"},
                timeout=self._config_s)
            if model_uid and model_uid != ws.model_label:
                res = self._session_open(
                    conn, "session/set_config_option",
                    {"sessionId": sid, "configId": "model",
                     "value": str(model_uid)}, timeout=self._config_s)
                ws.model_label = (_config_model_uid(
                    res.get("configOptions")) or str(model_uid))
        except Exception:
            try:
                self._kill(conn.proc)
            except Exception:               # noqa: BLE001
                pass
            raise
        ws.last_active = self._clock()
        self._start_reaper()
        return ws

    def _is_locked(self, workspace: str, sid: str) -> bool:
        """`devin list` isLocked = held by a live process. A locked
        session is never loaded - it is treated like session-gone."""
        rc, out = self._run_list(Path(workspace))
        if rc != 0 or not out:
            return False                       # can't tell -> try the load
        try:
            items = json.loads(out)
        except ValueError:
            return False
        for s in items if isinstance(items, list) else []:
            if str(s.get("id") or s.get("session_id")) == sid:
                return bool(s.get("isLocked") or s.get("is_locked"))
        return False

    def _open_conn(self, workspace: str) -> _AcpConn:
        kw = dict(self._spawn_kwargs)
        env = self._env() if self._env else None
        try:
            proc = self._spawn(
                self._argv + ["acp"], cwd=workspace,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, encoding="utf-8",
                errors="replace", bufsize=1, env=env, **kw)
        except OSError as exc:
            raise ProviderUnavailable(
                f"could not start `devin acp`: {exc}") from exc
        conn = _AcpConn(proc, _OutBuffer())
        conn.on_message = self._on_conn_message
        conn.start()
        return conn

    def _handshake(self, conn: _AcpConn) -> None:
        try:
            conn.request("initialize",
                         {"protocolVersion": 1,
                          "clientCapabilities": {
                              "fs": {"readTextFile": False,
                                     "writeTextFile": False},
                              "terminal": False}},
                         self._handshake_s)
        except AcpError as exc:
            if _AUTH_FAIL_RE.search(str(exc)) or \
                    _AUTH_FAIL_RE.search(conn.err.tail()):
                raise AuthUnavailable(
                    "provider is not authenticated (devin auth status)") \
                    from exc
            raise ProviderUnavailable(
                f"devin acp initialize: {exc}") from exc
        conn.notify("initialized", {})

    def _session_open(self, conn, method, params,
                      timeout=None) -> dict:
        try:
            return conn.request(method, params,
                                timeout or self._session_s)
        except AcpError as exc:
            if _AUTH_FAIL_RE.search(str(exc)) or \
                    _AUTH_FAIL_RE.search(conn.err.tail()):
                raise AuthUnavailable(
                    "provider is not authenticated (devin auth status)") \
                    from exc
            raise ProviderUnavailable(
                f"devin acp {method}: {exc}") from exc

    def _on_conn_message(self, msg: dict) -> None:
        if msg.get("method") != "session/update":
            return
        sid = (msg.get("params") or {}).get("sessionId")
        with self._lock:
            ws = self._pool.get(sid)
        if ws is not None:
            ws.apply_update(msg)

    # ============================================================ poll
    def poll(self, handle: ProviderHandle) -> PollResult:
        if getattr(handle, "adopted_row", None) is not None:
            return self._poll_adopted(handle)
        return self._poll_turn(handle)

    def _poll_turn(self, handle: ProviderHandle) -> PollResult:
        res = PollResult()
        ws = getattr(handle, "session", None)
        turn = getattr(handle, "turn", None)
        if ws is None or turn is None:
            res.done = True
            res.error = "provider handle has no live turn"
            return res
        res.session_id = ws.sid
        res.model_label = ws.model_label
        res.raw_metrics = dict(ws.cum) if ws.cum else None
        res.text = turn.text() or turn.thought_tail.strip()
        res.exit_code = ws.conn.proc.poll()

        if not turn.done.is_set():
            msg = ws.conn.poll_pending(turn.rid)
            if msg is not None:
                self._finish_turn(ws, turn, msg)
            elif ws.conn.closed.is_set() or res.exit_code is not None:
                self._fail_turn(ws, turn)

        if turn.done.is_set():
            res.done = True
            res.text = turn.text() or res.text
            res.model_label = ws.model_label
            if turn.result is not None:
                meta = turn.result.get("_meta") or {}
                umid = meta.get("cognition.ai/userMessageId")
                if umid:
                    res.request_id = str(umid)
                    handle.request_id = str(umid)
                self._fold_turn_usage(ws, turn.result.get("usage") or {})
            res.raw_metrics = dict(ws.cum) if ws.cum else None
            if turn.error:
                res.error = turn.error
            elif not res.text:
                res.error = "provider finished without a reply"
        return res

    def _finish_turn(self, ws: _WarmSession, turn: _Turn,
                     msg: dict) -> None:
        err = msg.get("error")
        if err is not None:
            etext = f"{err.get('code')} {err.get('message')}"
            turn.error = ("provider is not authenticated "
                          "(devin auth status)"
                          if _AUTH_FAIL_RE.search(etext)
                          else f"provider error: {etext}")
        else:
            turn.result = msg.get("result") or {}
        if ws.turn is turn:
            ws.turn = None
        turn.done.set()

    def _fail_turn(self, ws: _WarmSession, turn: _Turn) -> None:
        tail = ws.conn.err.tail()
        if _AUTH_FAIL_RE.search(tail):
            turn.error = ("provider is not authenticated "
                          "(devin auth status)")
        else:
            turn.error = "provider process ended the session mid-turn"
        if ws.turn is turn:
            ws.turn = None
        turn.done.set()

    def _fold_turn_usage(self, ws: _WarmSession, usage: dict) -> None:
        """Fold a prompt result's per-turn `usage` into the cumulative
        snapshot, but only for counters responseDimensions never gave us
        (dims carry absolute values and survive adapter restarts).
        cachedWriteTokens lands as total_cached_write_tokens -> usage.extra,
        unpriced. No total_steps - ACP reports none and NULL is honest."""
        if not isinstance(usage, dict):
            return
        for src, dst in (("inputTokens", "total_prompt_tokens"),
                         ("outputTokens", "total_completion_tokens"),
                         ("cachedReadTokens", "total_cached_tokens"),
                         ("cachedWriteTokens",
                          "total_cached_write_tokens")):
            v = usage.get(src)
            if isinstance(v, (int, float)) and dst not in ws.cum:
                ws.cum[dst] = int(ws.cum.get(dst) or 0) + int(v)

    # ---------------------------------------------------- adopt/attach
    def attach(self, exchange: dict) -> ProviderHandle:
        """Rebuild a handle for a dispatch whose parent serve died.

        A still-live recorded pid is OUR orphaned `devin acp` child - its
        stdio is untalkable, so it is killed (never leaked) and the
        session is re-loaded in a fresh process; the replayed history
        tail becomes the turn's recovered text."""
        return _AcpHandle(pid=exchange.get("pid"),
                          session_id=exchange.get("provider_session_id"),
                          prompt_path=exchange.get("prompt_path"),
                          export_path=exchange.get("export_path"),
                          adopted_row=dict(exchange))

    def alive_pid(self, pid) -> bool:
        try:
            return bool(pid) and self._alive(int(pid))
        except (TypeError, ValueError):
            return False

    def _poll_adopted(self, handle: _AcpHandle) -> PollResult:
        row = handle.adopted_row
        if not getattr(handle, "adopt_issued", False):
            handle.adopt_issued = True
            ws = handle.session
            if ws is None:
                pid = row.get("pid")
                if pid and self._alive(pid):
                    # our own orphaned acp child - reap before reloading
                    try:
                        self._kill_pid(int(pid))
                    except Exception:       # noqa: BLE001 - best effort
                        pass
                try:
                    ws = self._spawn_session(
                        row.get("provider_session_id"), None,
                        self._adopt_workspace(row))
                except Exception as exc:    # noqa: BLE001
                    res = PollResult(done=True, session_id=row.get(
                        "provider_session_id"))
                    res.error = (str(exc) if isinstance(
                        exc, ProviderUnavailable)
                        else f"adopt failed: {exc}")
                    return res
                handle.session = ws
                handle.pid = ws.conn.proc.pid
            # the in-flight turn is lost (no mid-turn replay exists):
            # re-issue the exchange's prompt once on the re-attached
            # session, like a re-dispatch (decision record)
            try:
                blocks = self._prompt_blocks(row.get("prompt_path"), None)
                handle.turn = self._issue_prompt(ws, blocks)
            except Exception as exc:        # noqa: BLE001
                res = PollResult(done=True, session_id=ws.sid)
                res.error = (str(exc) if isinstance(
                    exc, ProviderUnavailable)
                    else f"adopt failed: {exc}")
                return res
        return self._poll_turn(handle)

    def _adopt_workspace(self, row: dict) -> str:
        if self._workspace:
            return self._workspace
        p = row.get("export_path") or row.get("prompt_path") or ""
        # spool/ and workspace/ are siblings under the assistant data dir
        if p:
            return str(Path(p).parent.parent / "workspace")
        return "."

    # ========================================================== cancel
    def cancel(self, handle: ProviderHandle) -> None:
        ws = getattr(handle, "session", None)
        turn = getattr(handle, "turn", None)
        if ws is None:
            if handle.pid:
                try:
                    self._kill_pid(int(handle.pid))
                except Exception:           # noqa: BLE001
                    pass
            return
        try:
            ws.conn.notify("session/cancel", {"sessionId": ws.sid})
        except Exception:                   # noqa: BLE001
            pass
        if turn is not None:
            deadline = self._clock() + self._cancel_grace
            while not turn.done.is_set() and self._clock() < deadline:
                msg = ws.conn.poll_pending(turn.rid)
                if msg is not None:
                    self._finish_turn(ws, turn, msg)
                    break
                if ws.conn.closed.is_set():
                    self._fail_turn(ws, turn)
                    break
                self._sleep(0.05)
            if not turn.done.is_set():
                # session/cancel went unanswered - kill the process; the
                # session persists for the next send()/session-load
                try:
                    self._kill(ws.conn.proc)
                except Exception:           # noqa: BLE001
                    pass
                self._fail_turn(ws, turn)
                with self._lock:
                    self._drop(ws)

    # ======================================================== sessions
    def sessions(self, workspace) -> set[str] | None:
        rc, out = self._run_list(Path(workspace))
        if rc != 0 or not out:
            return None
        try:
            items = json.loads(out)
        except ValueError:
            return None
        ids = set()
        for s in items if isinstance(items, list) else []:
            sid = s.get("id") or s.get("session_id")
            if sid:
                ids.add(str(sid))
        return ids

    # -------------------------------------------------------- lifecycle
    def close(self) -> None:
        """Kill every pooled acp process (sessions persist in the store)."""
        with self._lock:
            sessions = list(self._pool.values())
            self._pool.clear()
        for ws in sessions:
            try:
                self._kill(ws.conn.proc)
            except Exception:               # noqa: BLE001
                pass

    def _drop(self, ws: _WarmSession) -> None:
        for k, v in list(self._pool.items()):
            if v is ws:
                self._pool.pop(k, None)

    def _start_reaper(self) -> None:
        with self._lock:
            if self._reaper_started:
                return
            self._reaper_started = True
        threading.Thread(target=self._reap_loop, daemon=True,
                         name="devin-acp-reaper").start()

    def _reap_loop(self) -> None:
        while True:
            self._sleep(REAP_S)
            with self._lock:
                if not self._pool:
                    self._reaper_started = False
                    return
                victims = [ws for ws in self._pool.values()
                           if ws.conn.closed.is_set()
                           or self._idle_expired(ws)]
            for ws in victims:
                try:
                    self._kill(ws.conn.proc)
                except Exception:           # noqa: BLE001
                    pass
                with self._lock:
                    self._drop(ws)

    def _idle_expired(self, ws: _WarmSession) -> bool:
        if ws.turn is not None and not ws.turn.done.is_set():
            return False                     # a live turn is never idle
        return (self._clock() - ws.last_active) > self._idle_ttl

    # -------------------------------------------------------- internals
    @staticmethod
    def _default_kill(proc) -> None:
        try:
            proc.kill()
            proc.wait(timeout=10)
        except (OSError, subprocess.SubprocessError):
            pass

    @staticmethod
    def _default_kill_pid(pid: int) -> None:
        if hasattr(subprocess, "run") and _is_windows():
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                           capture_output=True)
        else:
            try:
                import os
                os.kill(pid, 15)
            except OSError:
                pass

    @staticmethod
    def _default_alive(pid: int) -> bool:
        if _is_windows():
            import ctypes
            k32 = ctypes.windll.kernel32
            h = k32.OpenProcess(0x1000, False, int(pid))
            if not h:
                return False
            try:
                code = ctypes.c_ulong()
                if not k32.GetExitCodeProcess(h, ctypes.byref(code)):
                    return False
                return code.value == 259     # STILL_ACTIVE
            finally:
                k32.CloseHandle(h)
        import os
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True

    def _default_list(self, cwd: Path) -> tuple[int, str]:
        try:
            out = subprocess.run(
                self._argv + ["list", "--format", "json"], cwd=str(cwd),
                capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=30)
            return out.returncode, out.stdout or ""
        except (OSError, subprocess.SubprocessError):
            return 1, ""


def _is_windows() -> bool:
    import os
    return os.name == "nt"
