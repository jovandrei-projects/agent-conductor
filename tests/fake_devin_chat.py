#!/usr/bin/env python3
"""Fake `devin` CLI for assistant-runtime tests - a REAL subprocess the
DevinPrintProvider spawns/polls/kills, no auth, deterministic.

Surface covered:
    fake_devin_chat.py list --format json          per-cwd registry
    fake_devin_chat.py --print --prompt-file F [-r S] [--model M]
                    --permission-mode M --export P
                    --respect-workspace-trust B

A session registers in .fake_assist_sessions.json in the cwd with
CUMULATIVE counters (ATIF-style: each dispatch adds FAKE_TOKENS_JSON's
{prompt,completion,cached,steps}, default 100/40/30/2). The export is
written at turn end with steps[], agent.model_name, final_metrics.

Env controls:
    FAKE_REPLY        reply text (default "fake reply")
    FAKE_REPLY_H<n>   reply for this session's nth dispatch (overrides
                      FAKE_REPLY) - drives multi-hop tool-call fixtures
    FAKE_MODEL_LABEL  agent.model_name (default "Fake GPT")
    FAKE_SLEEP        seconds to sleep before writing the export
    FAKE_NO_EXPORT    if set, never write the export (stall/failure shape)
    FAKE_AUTH_FAIL    if set, print "Not logged in (fake)" + exit 1
    FAKE_EXIT         exit code after normal work (default 0)

`fake_devin_chat.py acp` - the ACP (ndjson stdin/stdout) variant driven
by DevinAcpProvider tests. Speaks initialize/session-new/load/
set_config_option/prompt/cancel against the same .fake_assist_sessions
registry (ACP sessions register like print sessions; history replays on
session/load). Per-turn usage lands in the prompt result AND in
usage_update responseDimensions cumulative metrics.

Extra env controls:
    FAKE_ACP_SLEEP    seconds before a prompt turn resolves (cancel test)
    FAKE_ACP_DIE      process exits mid-turn (crash recovery)
    FAKE_ACP_AUTH_FAIL initialize returns a 401-shaped error
    FAKE_ACP_NO_DIMS  omit responseDimensions (adapter-side cum fallback)
    FAKE_ACP_HANG     session/new never responds (handshake timeout test)
    FAKE_ACP_INBOUND  emit a server->client fs/write_text_file request
                      mid-turn (must get a refusal, not a write)
    FAKE_ACP_SNAPSHOT chunks are cumulative snapshots, not deltas
    FAKE_ACP_IGNORE_CANCEL  session/cancel never resolves the turn
    Set-mode/model choices persist in each session record's "config".
"""
import json
import os
import sys
import time
from pathlib import Path

REG = ".fake_assist_sessions.json"


def _load(cwd):
    f = cwd / REG
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _save(cwd, data):
    (cwd / REG).write_text(json.dumps(data), encoding="utf-8")


def _pid_alive(pid):
    if os.name == "nt":
        import ctypes
        k32 = ctypes.windll.kernel32
        h = k32.OpenProcess(0x1000, False, int(pid))
        if not h:
            return False
        try:
            code = ctypes.c_ulong()
            if not k32.GetExitCodeProcess(h, ctypes.byref(code)):
                return False
            return code.value == 259       # STILL_ACTIVE
        finally:
            k32.CloseHandle(h)
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _lock_file(cwd, sid):
    return cwd / f".{sid}.acplock"


def _lock_take(cwd, sid):
    _lock_file(cwd, sid).write_text(str(os.getpid()), encoding="utf-8")


def _lock_drop(cwd, sid):
    try:
        _lock_file(cwd, sid).unlink()
    except OSError:
        pass


def _is_locked(cwd, sid):
    """isLocked: a lockfile owned by a LIVE pid (dead owners' locks are
    reaped on read - a killed holder never locks a session forever)."""
    f = _lock_file(cwd, sid)
    try:
        pid = int((f.read_text() or "").strip())
    except (OSError, ValueError):
        return False
    if _pid_alive(pid):
        return True
    _lock_drop(cwd, sid)
    return False


def _tokens():
    return json.loads(os.environ.get(
        "FAKE_TOKENS_JSON",
        '{"prompt":100,"completion":40,"cached":30,"steps":2}'))


def _acp_send(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def _acp_update(sid, update):
    _acp_send({"jsonrpc": "2.0", "method": "session/update",
               "params": {"sessionId": sid, "update": update}})


def _acp_ts(n):
    return f"2026-10-17T00:00:{n:02d}.000000000+00:00"


def _acp_dims(cum):
    def cm(label, value, tail):
        return {"CumulativeMetric": {"label": label, "value": float(value),
                                     "tail": tail,
                                     "plural_tail": tail + "s",
                                     "prefix": ""}}
    return [
        {"uid": "agent_messages", "group_title": "Response Statistics",
         "kind": cm("Agent messages", cum.get("total_steps", 0),
                    " message")},
        {"uid": "model", "group_title": "Response Statistics",
         "kind": {"Metric": {"label": "Model",
                             "value": os.environ.get(
                                 "FAKE_MODEL_LABEL", "Fake GPT")}}},
        {"uid": "input_tokens", "group_title": "Token Usage",
         "kind": cm("Input tokens", cum.get("total_prompt_tokens", 0),
                    " token")},
        {"uid": "output_tokens", "group_title": "Token Usage",
         "kind": cm("Output tokens",
                    cum.get("total_completion_tokens", 0), " token")},
        {"uid": "cached_input_tokens", "group_title": "Token Usage",
         "kind": cm("Cached input tokens",
                    cum.get("total_cached_tokens", 0), " token")}]


def _acp_config(current_mode, current_model):
    return [{"id": "mode", "name": "Session Mode", "category": "mode",
             "type": "select", "currentValue": current_mode,
             "options": [{"value": "accept-edits", "name": "Code"},
                         {"value": "ask", "name": "Ask"},
                         {"value": "bypass", "name": "Bypass"}]},
            {"id": "model", "name": "Model", "category": "model",
             "type": "select", "currentValue": current_model,
             "options": [{"value": current_model,
                          "name": os.environ.get("FAKE_MODEL_LABEL",
                                                 "Fake GPT")}]}]


def _acp_replay(cwd, sid, rec):
    """session/load replays the stored history as session/update noise,
    ending in a cumulative usage_update like a real session."""
    i = 0
    for user, agent in rec.get("history", []):
        i += 1
        _acp_update(sid, {"sessionUpdate": "user_message_chunk",
                          "content": {"type": "text", "text": user},
                          "_meta": {"cognition.ai/timestamp": _acp_ts(i)}})
        _acp_update(sid, {"sessionUpdate": "agent_message_chunk",
                          "content": {"type": "text", "text": agent},
                          "_meta": {"cognition.ai/timestamp": _acp_ts(i)}})
    cum = rec.get("cum") or {}
    meta = {"cognition.ai/responseDimensions": _acp_dims(cum)}
    _acp_update(sid, {"sessionUpdate": "usage_update",
                      "used": int(cum.get("total_prompt_tokens") or 0),
                      "size": 262000, "_meta": meta})


def _acp_turn_usage(rec, tok):
    per = {"totalTokens": int(tok["prompt"]) + int(tok["completion"]),
           "inputTokens": int(tok["prompt"]),
           "outputTokens": int(tok["completion"]),
           "cachedReadTokens": int(tok["cached"])}
    cum = rec["cum"]
    meta = {"cognition.ai/inputTokens": per["inputTokens"],
            "cognition.ai/outputTokens": per["outputTokens"],
            "cognition.ai/cachedReadTokens": per["cachedReadTokens"]}
    if not os.environ.get("FAKE_ACP_NO_DIMS"):
        meta["cognition.ai/responseDimensions"] = _acp_dims(cum)
    return per, {"sessionUpdate": "usage_update", "used": per["totalTokens"],
                 "size": 262000, "_meta": meta}


def acp_main():
    cwd = Path.cwd()
    sessions = {}          # sid -> registry record reference
    my_locks = []          # sids this process holds (isLocked source)
    pending = None         # in-flight prompt request id
    tick = [0]

    def next_ts():
        tick[0] += 1
        return _acp_ts(tick[0])

    def reply_for(rec):
        body_text = (rec.get("last_prompt") or "")
        n = rec.get("n", 0)
        r = os.environ.get(f"FAKE_REPLY_H{n}",
                           os.environ.get("FAKE_REPLY", "fake reply"))
        return body_text if "ECHO_PROMPT" in body_text else r

    def finish_prompt(rid, sid, rec, cancelled=False):
        tok = _tokens()
        cum = rec.setdefault("cum", {})
        if not cancelled:
            for k, v in (("total_prompt_tokens", "prompt"),
                         ("total_completion_tokens", "completion"),
                         ("total_cached_tokens", "cached"),
                         ("total_steps", "steps")):
                cum[k] = int(cum.get(k) or 0) + int(tok.get(v) or 0)
            rec.setdefault("history", []).append(
                [rec.get("last_prompt", "")[:200], rec["reply"]])
            _save(cwd, _data)
            per, upd = _acp_turn_usage(rec, tok)
            _acp_update(sid, upd)
            result = {"stopReason": "end_turn", "usage": per,
                      "_meta": {"cognition.ai/userMessageId":
                                f"um-{sid}-{rec['n']}"}}
        else:
            _save(cwd, _data)
            result = {"stopReason": "end_turn",
                      "_meta": {"cognition.ai/userMessageId":
                                f"um-{sid}-{rec['n']}-cancelled"}}
        _acp_send({"jsonrpc": "2.0", "id": rid, "result": result})

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        method = msg.get("method")
        rid = msg.get("id")
        params = msg.get("params") or {}

        if method == "initialize":
            if os.environ.get("FAKE_ACP_AUTH_FAIL"):
                _acp_send({"jsonrpc": "2.0", "id": rid,
                           "error": {"code": -32001,
                                     "message": "Not logged in (fake) - "
                                                "unauthorized 401"}})
                continue
            _acp_send({"jsonrpc": "2.0", "id": rid, "result": {
                "protocolVersion": 1,
                "agentCapabilities": {
                    "loadSession": True,
                    "promptCapabilities": {"image": True, "audio": False,
                                           "embeddedContext": True},
                    "sessionCapabilities": {"list": {}, "delete": {}}}}})
        elif method == "initialized":
            pass
        elif method == "session/new":
            if os.environ.get("FAKE_ACP_HANG"):
                continue             # never responds - timeout test
            _data = _load(cwd)
            sid = f"fake-acp-{len(_data) + 1}"
            rec = _data.setdefault(sid, {"cum": {}, "n": 0,
                                         "at": time.time(),
                                         "history": []})
            _save(cwd, _data)
            sessions[sid] = rec
            _lock_take(cwd, sid)
            my_locks.append(sid)
            _acp_send({"jsonrpc": "2.0", "id": rid, "result": {
                "sessionId": sid,
                "modes": {"currentModeId": "accept-edits"},
                "configOptions": _acp_config("accept-edits",
                                             "fake-model")}})
        elif method == "session/load":
            _data = _load(cwd)
            sid = params.get("sessionId")
            if sid not in _data:
                _acp_send({"jsonrpc": "2.0", "id": rid,
                           "error": {"code": -32002,
                                     "message": f"session {sid} not "
                                                "found"}})
                continue
            rec = _data[sid]
            sessions[sid] = rec
            _lock_take(cwd, sid)
            my_locks.append(sid)
            _acp_replay(cwd, sid, rec)
            cfg = rec.get("config") or {}
            _acp_send({"jsonrpc": "2.0", "id": rid, "result": {
                "sessionId": sid,
                "modes": {"currentModeId": "accept-edits"},
                "configOptions": _acp_config(
                    cfg.get("mode", "accept-edits"),
                    cfg.get("model", "fake-model"))}})
        elif method in ("session/set_mode", "session/set_config_option"):
            sid = params.get("sessionId")
            key = params.get("configId") or (
                "mode" if method == "session/set_mode" else None)
            val = params.get("value") or params.get("modeId")
            if sid and key:
                _data = _load(cwd)
                rec = _data.get(sid)
                if rec is not None:
                    rec.setdefault("config", {})[key] = val
                    rec["config_calls"] = rec.get("config_calls", 0) + 1
                    _save(cwd, _data)
                    sessions[sid] = rec
            # echo the session's stored config like a real apply
            cfg = (sessions.get(sid) or {}).get("config") or {}
            _acp_send({"jsonrpc": "2.0", "id": rid, "result": {
                "configOptions": _acp_config(
                    cfg.get("mode", "ask"),
                    cfg.get("model", "fake-model"))}})
        elif method == "session/prompt":
            sid = params.get("sessionId")
            rec = sessions.get(sid)
            if rec is None:
                _data = _load(cwd)
                rec = _data.get(sid)
                sessions[sid] = rec
            if rec is None:
                _acp_send({"jsonrpc": "2.0", "id": rid,
                           "error": {"code": -32002,
                                     "message": f"session {sid} "
                                                "not found"}})
                continue
            _data = _load(cwd)
            if sid not in _data:
                _data[sid] = rec
            rec = _data[sid]
            sessions[sid] = rec
            text = "".join(b.get("text", "")
                           for b in params.get("prompt", [])
                           if isinstance(b, dict))
            rec["n"] = rec.get("n", 0) + 1
            rec["at"] = time.time()
            rec["last_prompt"] = text
            rec["reply"] = reply_for(rec)
            _save(cwd, _data)
            _acp_update(sid, {"sessionUpdate": "user_message_chunk",
                              "content": {"type": "text", "text": text},
                              "_meta": {"cognition.ai/timestamp":
                                        next_ts()}})
            if os.environ.get("FAKE_ACP_DIE"):
                sys.stdout.flush()
                os._exit(3)
            pending = (rid, sid, rec)
            sleep_s = float(os.environ.get("FAKE_ACP_SLEEP") or 0)
            if sleep_s:
                deadline = time.time() + sleep_s
                while time.time() < deadline:
                    nxt = sys.stdin.readline()
                    if not nxt:
                        break
                    try:
                        im = json.loads(nxt)
                    except ValueError:
                        continue
                    if im.get("method") == "session/cancel":
                        if os.environ.get("FAKE_ACP_IGNORE_CANCEL"):
                            continue
                        rid2, sid2, rec2 = pending
                        finish_prompt(rid2, sid2, rec2, cancelled=True)
                        pending = None
                        break
                    # queue it back - only cancel is handled mid-sleep
            if pending is None:
                continue
            rid, sid, rec = pending
            pending = None
            reply = rec["reply"]
            # FAKE_ACP_NO_TS / FAKE_ACP_CONST_TS imitate hosts whose live
            # deltas carry no timestamp or a shared one - the dedup sig
            # collapses every repeated fragment unless the adapter only
            # dedups replay-stamped chunks. Replayed history always keeps
            # its real timestamps.
            live_meta = None
            if os.environ.get("FAKE_ACP_CONST_TS"):
                live_meta = {"cognition.ai/timestamp": "ts-turn"}
            elif not os.environ.get("FAKE_ACP_NO_TS"):
                def live_meta():  # noqa: F811 - per-chunk unique ts
                    return {"cognition.ai/timestamp": next_ts()}
            def _meta_of():
                return live_meta() if callable(live_meta) else live_meta
            chunks = None
            if os.environ.get("FAKE_CHUNKS"):
                # verbatim delta list - lets a test emit repeated
                # fragment texts (the live-dedup corruption case)
                chunks = json.loads(os.environ["FAKE_CHUNKS"])
            if chunks is None and os.environ.get("FAKE_ACP_SNAPSHOT"):
                chunks = [reply[:len(reply)//2], reply]
            if chunks is None:
                mid = max(1, len(reply) // 2)
                chunks = [reply[:mid], reply[mid:]]
            for part in chunks:
                _acp_update(sid, {"sessionUpdate":
                                  "agent_message_chunk",
                                  "content": {"type": "text",
                                              "text": part},
                                  "_meta": _meta_of()})
            _acp_update(sid, {"sessionUpdate": "agent_thought_chunk",
                              "content": {"type": "text",
                                          "text": "thoughts..."},
                              "_meta": {"cognition.ai/timestamp":
                                        next_ts()}})
            if os.environ.get("FAKE_ACP_INBOUND"):
                _acp_send({"jsonrpc": "2.0", "id": 9900,
                           "method": "fs/write_text_file",
                           "params": {"path": "evil.txt",
                                      "content": "x"}})
            finish_prompt(rid, sid, rec)
        elif method == "session/cancel":
            if pending is not None:
                rid, sid, rec = pending
                pending = None
                finish_prompt(rid, sid, rec, cancelled=True)
        # unknown methods: ignore
    for sid in my_locks:                    # clean exit: drop our locks
        _lock_drop(cwd, sid)
    return 0


def main():
    args = sys.argv[1:]
    cwd = Path.cwd()
    if args[:1] == ["acp"]:
        return acp_main()
    if args[:1] == ["list"]:
        data = _load(cwd)
        print(json.dumps([{"id": s, "short_id": s,
                           "working_directory": str(cwd),
                           "isLocked": _is_locked(cwd, s),
                           "last_activity_at": int(v.get("at") or 0)}
                          for s, v in data.items()]))
        return 0

    prompt_file = export = model = sid = None
    i = 0
    while i < len(args):
        a = args[i]
        if a in ("--prompt-file", "--export", "--model", "-r",
                 "--permission-mode", "--respect-workspace-trust"):
            v = args[i + 1]
            if a == "--prompt-file":
                prompt_file = v
            elif a == "--export":
                export = v
            elif a == "--model":
                model = v
            elif a == "-r":
                sid = v
            i += 2
        else:
            i += 1

    if os.environ.get("FAKE_AUTH_FAIL"):
        print("Not logged in (fake) - run `devin auth login`")
        return 1

    data = _load(cwd)
    sid = sid or f"fake-sess-{len(data) + 1}"
    rec = data.setdefault(sid, {"cum": {}, "n": 0, "at": time.time()})
    rec["n"] = rec.get("n", 0) + 1
    rec["at"] = time.time()

    body = Path(prompt_file).read_text(encoding="utf-8") \
        if prompt_file else ""
    n = rec["n"]
    reply = os.environ.get(f"FAKE_REPLY_H{n}",
                           os.environ.get("FAKE_REPLY", "fake reply"))
    # 'prompt' keyword lets a test verify the prompt reached the session
    if "ECHO_PROMPT" in body:
        reply = body

    if os.environ.get("FAKE_SLEEP"):
        time.sleep(float(os.environ["FAKE_SLEEP"]))

    tok = json.loads(os.environ.get(
        "FAKE_TOKENS_JSON",
        '{"prompt":100,"completion":40,"cached":30,"steps":2}'))
    cum = rec.setdefault("cum", {})
    for k, v in (("total_prompt_tokens", "prompt"),
                 ("total_completion_tokens", "completion"),
                 ("total_cached_tokens", "cached"),
                 ("total_steps", "steps")):
        cum[k] = int(cum.get(k) or 0) + int(tok.get(v) or 0)
    _save(cwd, data)

    print(reply)
    if export and not os.environ.get("FAKE_NO_EXPORT"):
        doc = {"schema_version": "atif-1", "session_id": sid,
               "agent": {"name": "devin", "version": "0.0",
                          "model_name": os.environ.get(
                              "FAKE_MODEL_LABEL", "Fake GPT")},
               "steps": [{"step_id": 1, "source": "user",
                          "message": body[:200]},
                         {"step_id": 2, "source": "agent",
                          "message": reply, "tool_calls": []}],
               "final_metrics": dict(cum)}
        Path(export).write_text(json.dumps(doc), encoding="utf-8")
    return int(os.environ.get("FAKE_EXIT") or 0)


if __name__ == "__main__":
    sys.exit(main())
