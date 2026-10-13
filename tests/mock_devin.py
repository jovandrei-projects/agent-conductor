#!/usr/bin/env python3
"""Mock devin CLI for conductor tests - no auth, no credits, deterministic.

Implements the surface conductor.py uses:
    mock_devin.py auth status
    mock_devin.py list --format json          (per-cwd registry)
    mock_devin.py [--cloud] --print --prompt-file F [--model M]
                  [--permission-mode M] [--export P] [--respect-workspace-trust B]

A "session" appends one line to WORK.txt in the cwd, registers itself in
.mock_sessions.json there, writes the export file, and prints STATUS: done.
Set MOCK_FAIL=1 to make the session exit 3 instead.
"""
import json
import os
import random
import string
import sys
import time
from pathlib import Path

REG = ".mock_sessions.json"


def _registry(cwd: Path) -> list:
    f = cwd / REG
    return json.loads(f.read_text(encoding="utf-8")) if f.is_file() else []


def _save_registry(cwd: Path, sessions: list) -> None:
    (cwd / REG).write_text(json.dumps(sessions), encoding="utf-8")


def main() -> int:
    args = sys.argv[1:]
    cwd = Path.cwd()

    if args[:1] == ["auth"]:
        print("Logged in (mock)")
        return 0
    if args[:1] == ["list"]:
        print(json.dumps(_registry(cwd)))
        return 0
    if args[:1] == ["models"]:
        print(json.dumps({"families": {"swe": ["swe-1-6"], "claude": ["opus"]}}))
        return 0

    prompt_file = export = model = None
    cloud = False
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--cloud":
            cloud = True
            i += 1
        elif a in ("--print", "-p"):
            i += 1
        elif a in ("--prompt-file", "--model", "--permission-mode",
                   "--export", "--respect-workspace-trust"):
            val = args[i + 1]
            if a == "--prompt-file":
                prompt_file = val
            elif a == "--model":
                model = val
            elif a == "--export":
                export = val
            i += 2
        else:
            i += 1

    if os.environ.get("MOCK_FAIL") == "1":
        print("STATUS: failed")
        return 3

    body = ""
    if prompt_file:
        body = Path(prompt_file).read_text(encoding="utf-8")

    work = cwd / "WORK.txt"
    n = sum(1 for _ in open(work, encoding="utf-8")) if work.is_file() else 0
    with open(work, "a", encoding="utf-8") as f:
        f.write(f"mock-step-{n + 1}\n")

    sid = "mock-" + "".join(random.choices(string.ascii_lowercase, k=6))
    sessions = _registry(cwd)
    sessions.append({"id": sid, "short_id": sid,
                     "working_directory": str(cwd),
                     "last_activity_at": int(time.time()),
                     "title": (body.splitlines() or ["mock"])[0][:60]})
    _save_registry(cwd, sessions)

    if export:
        Path(export).write_text(
            json.dumps({"mock": True, "session": sid, "cloud": cloud}),
            encoding="utf-8")

    print(f"mock worker ran: prompt={len(body)} chars model={model} "
          f"cloud={cloud} session={sid}")
    print("STATUS: done")
    print("NEXT: nothing - mock")
    return 0


if __name__ == "__main__":
    sys.exit(main())
