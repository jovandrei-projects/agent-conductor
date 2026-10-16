#!/usr/bin/env python3
"""Mock devin CLI for conductor tests - no auth, no credits, deterministic.

Implements the surface conductor.py uses:
    mock_devin.py auth status
    mock_devin.py list --format json          (per-cwd registry)
    mock_devin.py rm <id> [--force]           (drop a registered session)
    mock_devin.py [--cloud] --print --prompt-file F [--model M]
                  [--permission-mode M] [--export P] [--respect-workspace-trust B]

A "session" appends one line to WORK.txt in the cwd, registers itself in
.mock_sessions.json there, writes the export file, and prints STATUS: done.
Set MOCK_FAIL=1 to make the session exit 3 instead.

Prompt keywords (deterministic work shapes for the merge matrix):
    mockfail        exit 3, no work - fails THIS step only (unlike the
                    process-wide MOCK_FAIL env)
    prepend         write the line at the TOP of the file, not the end -
                    vs an append step this edits a different region, so two
                    parallel branches touching the SAME file merge cleanly
    sleep=N         sleep N seconds before working (out-of-order finishes)
    sleep_after=N   sleep N seconds AFTER writing - the work product exists
                    but the session never reports (timeout-with-work shape)
    skipwork        register the session, print STATUS, write nothing - a
                    'done' step whose branch has zero commits
    resolve-merge   resolve an in-progress git merge in the cwd: rewrite
                    every file holding <<<<<<< markers as ours-then-theirs
                    without markers, `git add -A`, `git commit --no-edit`.
                    Exercises conductor's merge_resolver path for real.
"""
import json
import os
import random
import re
import string
import subprocess
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
        dead = os.environ.get("MOCK_AUTH_FILE")
        if dead and Path(dead).is_file():
            print("Not logged in (mock)")
            return 1
        print("Logged in (mock)")
        return 0
    if args[:1] == ["list"]:
        print(json.dumps(_registry(cwd)))
        return 0
    if args[:1] == ["rm"]:
        target = next((a for a in args[1:] if not a.startswith("-")), None)
        sessions = [s for s in _registry(cwd) if s.get("id") != target]
        _save_registry(cwd, sessions)
        print(f"deleted session {target} (mock)")
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
    if "mockfail" in body:
        print("STATUS: failed")
        return 3

    sid = "mock-" + "".join(random.choices(string.ascii_lowercase, k=6))
    # prompt "killauth" trips the MOCK_AUTH_FILE flag: the NEXT step's
    # auth probe then fails -> simulates the token dying mid-run.
    if "killauth" in body and os.environ.get("MOCK_AUTH_FILE"):
        Path(os.environ["MOCK_AUTH_FILE"]).write_text("x")

    if "resolve-merge" in body:
        if os.environ.get("MOCK_RESOLVE_FAIL"):
            print("STATUS: failed")
            return 3
        return resolve_merge(cwd, sid, export)

    m = re.search(r"([A-Za-z0-9_]+\.txt)", body)
    # prompt "sleep=N" simulates a slow worker so waves can finish
    # out of manifest order; "sleep_after=N" works first, then stalls.
    slow = re.search(r"sleep=([0-9.]+)", body)
    slow_after = re.search(r"sleep_after=([0-9.]+)", body)
    if slow:
        time.sleep(float(slow.group(1)))
    work = cwd / (m.group(1) if m else "WORK.txt")
    n = sum(1 for _ in open(work, encoding="utf-8")) if work.is_file() else 0
    # sid in the line: two parallel worktrees writing the same file produce
    # different content -> a real merge conflict, not a no-op merge.
    if "skipwork" not in body:
        line = f"mock-step-{n + 1}-{sid}\n"
        if "prepend" in body and work.is_file():
            work.write_text(line + work.read_text(encoding="utf-8"),
                            encoding="utf-8")
        else:
            with open(work, "a", encoding="utf-8") as f:
                f.write(line)
    sessions = _registry(cwd)
    sessions.append({"id": sid, "short_id": sid,
                     "working_directory": str(cwd),
                     "last_activity_at": int(time.time()),
                     "title": (body.splitlines() or ["mock"])[0][:60]})
    _save_registry(cwd, sessions)
    if slow_after:
        time.sleep(float(slow_after.group(1)))

    if export:
        doc = {"mock": True, "session": sid, "cloud": cloud}
        if "conductor-run-summary" in body:
            # the run summarizer's prompt marker - answer with an ATIF
            # closing message so conductor's final_report() has prose
            # to lift into run.summary.md
            doc["steps"] = [{"step_id": 1, "source": "agent",
                             "tool_calls": [], "model_name": "mock",
                             "message": "Executive summary (mock): the "
                                        "run's steps reported done; "
                                        "everything landed."}]
        Path(export).write_text(json.dumps(doc), encoding="utf-8")

    print(f"mock worker ran: prompt={len(body)} chars model={model} "
          f"cloud={cloud} session={sid}")
    print("STATUS: done")
    print("NEXT: nothing - mock")
    return 0


def resolve_merge(cwd: Path, sid: str, export: str | None) -> int:
    """Union-resolve every unmerged file in cwd, then commit the merge.

    Resolution keeps BOTH sides (ours lines, then theirs lines) - correct for
    the append-style conflicts the matrix exercises and deterministic.
    """
    unmerged = subprocess.run(
        ["git", "-C", str(cwd), "diff", "--name-only", "--diff-filter=U"],
        capture_output=True, text=True).stdout.split()
    for rel in unmerged:
        p = cwd / rel
        out: list[str] = []
        mode = "common"                # common|ours|base|theirs
        for ln in p.read_text(encoding="utf-8").splitlines(keepends=True):
            if ln.startswith("<<<<<<<"):
                mode = "ours"
            elif ln.startswith("|||||||"):
                mode = "base"
            elif ln.startswith("=======") and mode == "ours":
                mode = "theirs"
            elif ln.startswith(">>>>>>>"):
                mode = "common"
            elif mode in ("common", "ours", "theirs"):
                out.append(ln)
        p.write_text("".join(out), encoding="utf-8")
    subprocess.run(["git", "-C", str(cwd), "add", "-A"], capture_output=True)
    subprocess.run(["git", "-C", str(cwd), "commit", "--no-edit"],
                   capture_output=True)
    sessions = _registry(cwd)
    sessions.append({"id": sid, "short_id": sid,
                     "working_directory": str(cwd),
                     "last_activity_at": int(time.time()),
                     "title": "resolve-merge"})
    _save_registry(cwd, sessions)
    if export:
        Path(export).write_text(
            json.dumps({"mock": True, "session": sid, "resolver": True}),
            encoding="utf-8")
    print(f"mock resolver ran: {len(unmerged)} file(s) resolved session={sid}")
    print("STATUS: done")
    print("NEXT: merge resolved - mock")
    return 0


if __name__ == "__main__":
    sys.exit(main())
