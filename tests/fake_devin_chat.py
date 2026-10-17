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


def main():
    args = sys.argv[1:]
    cwd = Path.cwd()
    if args[:1] == ["list"]:
        data = _load(cwd)
        print(json.dumps([{"id": s, "short_id": s,
                           "working_directory": str(cwd),
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
