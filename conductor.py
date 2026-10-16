#!/usr/bin/env python3
"""conductor.py - run a plan as a sequence of fresh Devin sessions.

The human-driven loop this replaces: split a project into phases, start a new
Devin session per phase so each gets a fresh context window, paste the prompt,
wait, repeat. The conductor does the same thing from a TOML manifest - one
`devin -p` subprocess per step, state handed off through the project's own
`.agent_tasks` files instead of conversation history.

    python conductor.py plan  night.toml      validate + show resolved steps
    python conductor.py run   night.toml      execute -> data/runs/<ts>-<name>/
    python conductor.py run   night.toml --resume [dir]   skip finished steps
    python conductor.py status [run_dir]      table from the latest run
    python conductor.py serve [--port 8773]   live dashboard of every agent
    python conductor.py sweep                 mark orphaned records 'lost'
    python conductor.py sessions [--rm]       list/clean spawned devin sessions
    python conductor.py demo                  end-to-end against a mock devin

Manifest: see examples/night.example.toml. Project registry: projects.toml.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import dataclasses
import datetime as dt
import hashlib
import html
import http.server
import json
import logging
import os
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
import urllib.parse
import urllib.request
from logging.handlers import RotatingFileHandler
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
RUNS_DIR = DATA_DIR / "runs"
REQUESTS_DIR = DATA_DIR / "requests"
ATTACHMENTS_DIR = DATA_DIR / "attachments"
MANIFESTS_DIR = ROOT / "manifests"
LOGS_DIR = ROOT / "logs"
PROJECTS_FILE = ROOT / "projects.toml"
TASK_PY = ROOT.parent / "mobile-apps" / "pc-bridge" / "task.py"

LOG = logging.getLogger("conductor")
STATE_LOCK = threading.RLock()
STATUS_RE = re.compile(r"^STATUS:\s*([A-Za-z_-]+)", re.M)
NEXT_RE = re.compile(r"^NEXT:\s*(.+)", re.M)
SESSION_URL_RE = re.compile(
    r"app\.devin\.ai/sessions/([A-Za-z0-9_-]+)|(devin-[0-9a-f]{32})")
VALID_MODES = {"normal", "accept-edits", "smart", "bypass", "dangerous",
               "yolo", "auto"}
# Canonical --permission-mode flag values (from `devin --help`); the manifest
# may use friendlier slash-command aliases.
MODE_FLAG = {"normal": "auto", "auto": "auto", "accept-edits": "accept-edits",
             "smart": "smart", "bypass": "dangerous", "yolo": "dangerous",
             "dangerous": "dangerous"}

# A `devin -p` worker stays alive while any session-spawned child lives -
# workers' playwright probes leave a vite dev server up, and the CLI then
# idles until timeout_min (trap 12: 3 steps burned 52/52/85min post-turn).
# Once a STATUS line reaches the log the turn is over, so the remaining
# wait collapses to this grace, the tree is killed, and the step counts as
# a completed turn (verify still runs) instead of a timeout.
STATUS_GRACE_S = 120.0
WAIT_POLL_S = 15.0        # wait() slice between STATUS checks on the log
# A recorded session_id can briefly vanish from `devin list` right after
# spawn (registration lags seconds-to-a-minute, and the dashboard's own
# listing cache is 120s stale) - don't read that as 'deleted' for the
# first few minutes of a step.
SESSION_GONE_GRACE_S = 300.0

# A finished run older than this is auto-archived: files kept on disk
# (state, logs, ATIF exports) but hidden from the default dashboard view -
# the 'archived' view chip (/api/state?archived=1) lists it and the run
# modal's Unarchive clears the stamp. CONDUCTOR_ARCHIVE_HOURS overrides;
# 0 disables auto-archive.
try:
    ARCHIVE_AFTER_S = float(
        os.environ.get("CONDUCTOR_ARCHIVE_HOURS") or 48) * 3600
except ValueError:
    ARCHIVE_AFTER_S = 48 * 3600

PREAMBLE = """\
This session was spawned unattended by agent-conductor
(C:\\Projects\\agent-conductor). No human is watching this run. Do NOT use
ask_user_question - if a human decision is required, you may post it to the
phone queue with `python C:\\Projects\\mobile-apps\\pc-bridge\\task.py ask
--project {project} --title "..." --body "..."` and then finish blocked.

You are working in {project_dir} (already your working directory). Read its
AGENTS.md first, then .agent_tasks/QUEUE.md and the STATE.md of any task that
is not `done` - those files are the shared memory between sessions; this
conversation has no history beyond them.

## Task

{body}

## Before you finish

- Update .agent_tasks so the next session resumes without you: tick verified
  steps in the STATE.md you worked from, refresh its "Where it stopped" line,
  update QUEUE.md if the task's status changed.
- Commit your work in logical groups (why-not-what messages). Never push.
- Kill any servers/watchers you spawned (vite, `npm run dev`, playwright)
  BEFORE printing STATUS - a live child keeps this CLI process from
  exiting and your step gets killed at the timeout regardless.
- End your final message with exactly these two lines, at the start of
  their own lines, as the LAST thing you print. They are read by a
  machine parsing your stdout - writing STATUS inside STATE.md, a commit
  message, or a Markdown bullet does NOT count and marks the step failed:

STATUS: done | partial | blocked
NEXT: <one line - what the next session should do first>
"""

# Prepended to the task body on retry attempts (max_attempts > 1): the
# previous session hit timeout_min, its work was committed, and this fresh
# session must continue from that state instead of starting over.
RETRY_NOTE = """\
NOTE FROM THE CONDUCTOR: a previous session attempt at this same step hit \
its {timeout:g}-minute limit and was stopped (attempt {prev} of {total}). \
Its work is already committed in this repo - do NOT redo it. Read the \
recent git log and .agent_tasks state, then CONTINUE from where it stopped.
"""

# Prepended when --resume re-runs a step whose previous session ended
# abruptly (user stop, conductor died, connection lost) - the new session
# is told the prior transcript vanished mid-work so it recovers from the
# committed state instead of trusting a clean start.
INTERRUPTED_NOTE = """\
NOTE FROM THE CONDUCTOR: a previous session attempt at this step ended \
abruptly (recorded {how}) - it was stopped by the user, the connection \
dropped, the conductor process died mid-turn, or the worker exited \
before reporting STATUS. Partial work may already \
be committed in this repo - inspect the recent git log and .agent_tasks \
state, then CONTINUE from where it stopped rather than redoing finished \
work.
"""

# Appended to the task body when the dashboard's 'resume notes' field
# carries text: operator context for the next dispatch ("VPN is back").
# The step's original prompt is never rewritten post-dispatch - notes
# ride along under it instead.
RESUME_NOTES = """\
NOTE FROM THE OPERATOR (saved on the conductor dashboard for this step):
{notes}
"""

# Closing review step appended when a request's follow-up checkbox is set -
# the planner is told to write it, and _publish_proposal appends this block
# itself when the planner forgot, so the checkbox is a guarantee.
FOLLOWUP_STEP = '''

[[step]]
name = "follow-up-plan"
project = "{slug}"
timeout_min = 30
prompt = 'This run just ended. Review what it accomplished against the request it ran from: per-step logs, STATUS/NEXT lines and notes are under {runs} (the newest dir ending in -{run}), and progress files live under this project .agent_tasks/. Then write a NEW conductor TOML manifest proposing ONLY the still-unfinished or newly discovered work, saved as {manifests}/{run}-followup.toml. Do not implement or launch that manifest - a human reviews it first.'
'''

RESOLVE_BODY = """\
<!-- conductor-purpose: resolve-merge -->
A git merge is in progress in this repo and needs a human-style
resolution. Conductor tried to merge branch `{branch}` - a parallel
agent's work, still checked out at {worktree} - back into the main
checkout, and it conflicts.

Conflicted files:
{files}

Resolve the merge:
1. `git status` shows the state. Open each conflicted file and resolve
   every <<<<<<< marker, keeping the intent of BOTH sides (the main
   checkout's content and the branch's work - when they merely append
   different lines, keep both).
2. `git add` each resolved file, then `git commit --no-edit` to finish
   the merge. Verify `git status` shows no unmerged paths.
3. Do NOT `git merge --abort`, do NOT rebase, and do NOT rewrite the
   branch's commits - its work must land through this merge.
4. Touch nothing outside the conflict resolution; the rest of the tree
   is other agents' committed work.

If the conflict cannot be resolved safely, leave the merge in progress
and end with STATUS: blocked - the branch and its worktree are kept
either way."""


# ---------------------------------------------------------------- manifest

@dataclasses.dataclass
class Step:
    name: str
    project: str
    prompt: str | None = None
    prompt_file: str | None = None
    model: str | None = None
    mode: str = "bypass"
    timeout_min: float = 60
    verify: str | None = None          # cmd.exe command run in the work dir
    verify_must_pass: bool = True
    cloud: bool = False
    wave: int | None = None            # adjacent steps sharing a wave run in parallel
    worktree: bool = False             # run in a temp git worktree + branch, merged after
    effort: str | None = None          # thinking-effort variant suffix fused onto
                                       # the model id ("min" -> "model-min")
    merge_resolver: bool = False       # on merge conflict, spawn a resolver
                                       # session in the main checkout to
                                       # finish the merge instead of only
                                       # parking the worktree for a human
    max_attempts: int = 1              # a step hitting timeout_min is committed
                                       # and retried in a fresh session until
                                       # this many total attempts ran
    stall_min: float = 20              # past timeout_min a still-talking log
                                       # auto-extends; this much stdout silence
                                       # (or the 4x ceiling) ends it. 0 = hard
                                       # deadline, no extension


@dataclasses.dataclass
class Manifest:
    path: Path
    name: str
    on_failure: str                    # stop | continue
    checkpoint: bool
    preamble: str | None
    max_hours: float | None            # wall-clock cap for the whole run
    max_parallel: int | None           # cap on concurrent agents; None = auto
    merge_resolver: bool               # run-wide default for step.merge_resolver
    steps: list[Step]
    description: str | None = None


class ManifestError(ValueError):
    pass


def load_manifest(path: Path, text: str | None = None) -> Manifest:
    doc = tomllib.loads(path.read_text(encoding="utf-8") if text is None else text)
    run = doc.get("run", {})
    defaults = doc.get("defaults", {})
    name = run.get("name") or path.stem
    description = run.get("description")
    if description is not None and not isinstance(description, str):
        raise ManifestError("run.description must be a string")
    on_failure = run.get("on_failure", "stop")
    if on_failure not in ("stop", "continue"):
        raise ManifestError(f"run.on_failure must be stop|continue, got {on_failure!r}")
    preamble = run.get("preamble")
    if run.get("preamble_file"):
        preamble = (path.parent / run["preamble_file"]).read_text(encoding="utf-8")

    steps: list[Step] = []
    seen: set[str] = set()
    registry: dict | None = None     # lazy - only loaded when a step
    for i, raw in enumerate(doc.get("step", [])):  #   omits verify
        merged = {**defaults, **raw}
        merged.setdefault("name", f"step-{i + 1}")
        if merged["name"] in seen:
            raise ManifestError(f"duplicate step name {merged['name']!r}")
        seen.add(merged["name"])
        if "project" not in merged:
            raise ManifestError(f"step {merged['name']!r} has no project")
        if bool(merged.get("prompt")) == bool(merged.get("prompt_file")):
            raise ManifestError(
                f"step {merged['name']!r}: set exactly one of prompt / prompt_file")
        mode = str(merged.get("mode", "bypass"))
        if mode not in VALID_MODES:
            raise ManifestError(f"step {merged['name']!r}: bad mode {mode!r}")
        if merged.get("cloud") and merged.get("worktree"):
            raise ManifestError(
                f"step {merged['name']!r}: worktree makes no sense with "
                f"cloud=true (cloud runs on a Devin VM)")
        mr = merged.get("merge_resolver")
        if mr is None:
            mr = run.get("merge_resolver", False)
        # A step without verify inherits the project's registered gate
        # (projects.toml `verify = "..."`). Explicit verify = "" opts out.
        # Cloud steps never inherit - a local verify of an untouched
        # checkout proves nothing about what ran on the VM.
        verify = merged.get("verify")
        if verify is None and not merged.get("cloud"):
            if registry is None:
                try:
                    registry = load_projects()
                except Exception:
                    registry = {}
            verify = (registry.get(merged["project"]) or {}).get("verify")
        try:
            ma = int(merged.get("max_attempts", 1))
        except (TypeError, ValueError):
            ma = 0
        if not 1 <= ma <= 10:
            raise ManifestError(
                f"step {merged['name']!r}: max_attempts must be 1-10")
        steps.append(Step(
            name=merged["name"], project=merged["project"],
            prompt=merged.get("prompt"),
            prompt_file=merged.get("prompt_file"),
            model=merged.get("model"), mode=mode,
            timeout_min=float(merged.get("timeout_min", 60)),
            verify=verify,
            verify_must_pass=bool(merged.get("verify_must_pass", True)),
            cloud=bool(merged.get("cloud", False)),
            wave=merged.get("wave"),
            worktree=bool(merged.get("worktree", False)),
            effort=merged.get("effort") or None,
            merge_resolver=bool(mr), max_attempts=ma,
            stall_min=float(merged.get("stall_min", 20))))
    if not steps:
        raise ManifestError("manifest has no [[step]] entries")
    max_parallel = run.get("max_parallel")
    if max_parallel is not None:
        max_parallel = int(max_parallel)
        if max_parallel < 1:
            raise ManifestError("run.max_parallel must be >= 1")
    return Manifest(path=path, name=name, on_failure=on_failure,
                    checkpoint=bool(run.get("checkpoint", True)),
                    preamble=preamble, max_hours=run.get("max_hours"),
                    max_parallel=max_parallel,
                    merge_resolver=bool(run.get("merge_resolver", False)),
                    steps=steps, description=description)


def load_projects() -> dict[str, dict]:
    """The project registry. `projects.toml` is tracked and its relative
    `path`s resolve against the repo root - which breaks in a git worktree
    checkout (../other-project lands outside the worktree). An untracked
    `projects.local.toml` next to it merges over the base so a worktree or
    machine can re-point slugs (typically with absolute paths) without
    touching the shared file."""
    out: dict[str, dict] = {}
    for f in (PROJECTS_FILE, PROJECTS_FILE.with_name("projects.local.toml")):
        if not f.is_file():
            continue
        doc = tomllib.loads(f.read_text(encoding="utf-8"))
        for slug, ent in doc.get("project", {}).items():
            p = Path(ent["path"])
            if not p.is_absolute():
                p = (f.parent / p).resolve()
            merged = dict(out.get(slug, {}))
            merged.update({"path": p,
                           "cloud_ok": bool(ent.get("cloud_ok", False)),
                           "verify": ent.get("verify")})
            out[slug] = merged
    return out


def resolve_project(step: Step, projects: dict, manifest_dir: Path) -> tuple[Path, str]:
    """Return (project_dir, slug). A step may name a projects.toml slug or a
    literal path relative to the manifest."""
    if step.project in projects:
        ent = projects[step.project]
        if step.cloud and not ent["cloud_ok"]:
            raise ManifestError(
                f"step {step.name!r}: project {step.project!r} has cloud_ok=false "
                f"in projects.toml - refusing to send it to Devin Cloud")
        return ent["path"], step.project
    p = Path(step.project)
    if not p.is_absolute():
        p = (manifest_dir / p).resolve()
    if not p.is_dir():
        raise ManifestError(
            f"step {step.name!r}: project {step.project!r} is neither a "
            f"projects.toml slug nor an existing directory ({p})")
    if step.cloud:
        raise ManifestError(
            f"step {step.name!r}: cloud steps require a projects.toml entry "
            f"with cloud_ok=true; literal paths default to local-only")
    return p, p.name


def group_waves(resolved: list[tuple[Path, str, Step]]) -> list[list]:
    """Adjacent steps sharing a non-None `wave` number form one parallel
    group; everything else is a singleton, in manifest order."""
    groups: list[list] = []
    cur: list = []
    cur_wave = None
    for item in resolved:
        w = item[2].wave
        if w is not None and cur and w == cur_wave:
            cur.append(item)
        else:
            if cur:
                groups.append(cur)
            cur = [item]
            cur_wave = w
    if cur:
        groups.append(cur)
    return groups


def validate_groups(groups: list[list]) -> None:
    """Parallel steps on the SAME project dir must each run in a worktree."""
    for members in groups:
        if len(members) < 2:
            continue
        by_dir: dict[str, list[Step]] = {}
        for pdir, _slug, step in members:
            by_dir.setdefault(str(pdir), []).append(step)
        for d, ss in by_dir.items():
            if len(ss) > 1 and not all(s.worktree for s in ss):
                names = ", ".join(s.name for s in ss)
                raise ManifestError(
                    f"wave collision: {len(ss)} parallel steps ({names}) share "
                    f"{d} - set worktree=true on all of them")


# ---------------------------------------------------------------- spawn

def devin_cmd(devin_argv: list[str], step: Step, prompt_path: Path,
              export_path: Path) -> list[str]:
    cmd = list(devin_argv)
    if step.cloud:
        cmd.append("--cloud")
    cmd += ["--print", "--prompt-file", str(prompt_path)]
    if step.model:
        # effort fuses as a model-variant suffix ("swe-1-6-fast" style); an
        # unknown variant makes devin error loudly at spawn instead of
        # silently running the wrong effort. 'adaptive' is a routing mode,
        # not a family - it takes no effort suffix.
        model = (f"{step.model}-{step.effort}"
                 if step.effort and step.model != "adaptive" else step.model)
        cmd += ["--model", model]
    cmd += ["--permission-mode", MODE_FLAG[step.mode],
            "--export", str(export_path),
            "--respect-workspace-trust", "false"]
    return cmd


def render_prompt(step: Step, slug: str, project_dir: Path,
                  manifest_dir: Path, preamble: str | None) -> str:
    if step.prompt_file:
        body = (manifest_dir / step.prompt_file).read_text(encoding="utf-8")
    else:
        body = step.prompt
    return (preamble or PREAMBLE).format(body=body, project=slug,
                                       project_dir=project_dir)


def clean_devin_env() -> dict:
    """Env for any spawned `devin` process. When conductor itself runs
    inside the Devin Desktop / VS-Code extension host, the inherited
    ACP_BACKEND + WINDSURF_* + VSCODE_* vars make the CLI expect
    host-supplied auth instead of reading credentials.toml, so every
    `auth status`/`-p` reports 'Not logged in' against a perfectly good
    token. Strip those prefixes from every devin child."""
    env = dict(os.environ)
    for k in list(env):
        if k.startswith(("ACP_", "WINDSURF_", "VSCODE_", "TERM_PROGRAM")):
            del env[k]
    return env


def hidden_subprocess_kwargs() -> dict:
    return {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}


def session_activity(devin_argv: list[str], cwd: Path) -> dict[str, float] | None:
    """Session id -> last_activity epoch `devin list` reports for cwd, or
    None when the listing itself failed (devin missing, bad dir) - callers
    must not treat a failed listing as 'no sessions'. The activity stamp is
    what lets the dashboard attribute a live session to the step that
    spawned it (the session appears seconds after the step starts)."""
    try:
        out = subprocess.run(devin_argv + ["list", "--format", "json"],
                             cwd=str(cwd), capture_output=True, text=True,
                             encoding="utf-8", errors="replace", timeout=60,
                             env=clean_devin_env(),
                             **hidden_subprocess_kwargs())
        if out.returncode != 0:
            return None
        return {s["id"]: float(s.get("last_activity_at") or 0)
                for s in json.loads(out.stdout or "[]")}
    except Exception:
        return None


def session_ids(devin_argv: list[str], cwd: Path) -> set[str] | None:
    """Session ids `devin list` reports for cwd, or None when the listing
    itself failed (devin missing, bad dir) - callers must not treat a
    failed listing as 'all sessions gone'."""
    act = session_activity(devin_argv, cwd)
    return set(act) if act is not None else None


def check_auth(devin_argv: list[str]) -> bool:
    try:
        out = subprocess.run(devin_argv + ["auth", "status"],
                             capture_output=True, text=True, timeout=60,
                             encoding="utf-8", errors="replace",
                             env=clean_devin_env(),
                             **hidden_subprocess_kwargs())
        return out.returncode == 0 and "not logged in" not in (
            out.stdout + out.stderr).lower()
    except Exception:
        return False


# ---------------------------------------------------------------- resources

def default_max_parallel() -> int:
    """Conservative concurrency ceiling when the manifest doesn't set one.

    Every worker is a full Devin agent session - a spawned CLI plus tool
    subprocesses, model streaming and whatever builds it runs. One agent per
    ~4 logical cores, never more than 4, keeps an ordinary desktop usable.
    Set run.max_parallel (or --max-parallel) to override; the box is yours.
    """
    return max(1, min(4, (os.cpu_count() or 4) // 4))


def effective_max_parallel(cfg: Manifest, override: int | None = None) -> int:
    if override is not None:
        if override < 1:
            raise ManifestError("--max-parallel must be >= 1")
        return override
    return cfg.max_parallel or default_max_parallel()


def _iso_ts(s: str | None) -> float | None:
    try:
        return dt.datetime.fromisoformat(s).timestamp() if s else None
    except (TypeError, ValueError):
        return None


def _proc_started_epoch(handle) -> float | None:
    """Process creation time from an OpenProcess handle (Windows)."""
    import ctypes
    ct = ctypes.c_ulonglong(0)
    buf = ctypes.c_ulonglong(0)
    if not ctypes.windll.kernel32.GetProcessTimes(
            handle, ctypes.byref(ct), ctypes.byref(buf),
            ctypes.byref(buf), ctypes.byref(buf)):
        return None
    return ct.value / 10_000_000 - 11644473600.0   # FILETIME -> unix epoch


def pid_alive(pid: int | None, since: str | None = None) -> bool:
    """Is this child pid still running? MUST be non-destructive.

    Never probe with os.kill(pid, 0) on Windows: there it means
    TerminateProcess(handle, 0) - it KILLS the process it checks (verified
    2026-10-13; os.kill is still fine on POSIX where sig 0 is a pure probe).
    Use OpenProcess + GetExitCodeProcess instead.

    `since` (the step's started_at) guards against pid reuse: Windows
    recycles pids, so a live pid may belong to an unrelated process. Its
    creation time must be within ~just-after the step start or it isn't
    our worker.
    """
    if not pid:
        return False
    if os.name == "nt":
        import ctypes
        k32 = ctypes.windll.kernel32
        h = k32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
        if not h:
            return False
        try:
            code = ctypes.c_ulong()
            if not k32.GetExitCodeProcess(h, ctypes.byref(code)) \
                    or code.value != 259:        # 259 = STILL_ACTIVE
                return False
            if since:
                t0, created = _iso_ts(since), _proc_started_epoch(h)
                if t0 is not None and created is not None \
                        and not (t0 - 5 <= created <= t0 + 900):
                    return False                 # recycled pid, not ours
            return True
        finally:
            k32.CloseHandle(h)
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True    # exists but we can't signal it
    except OSError:
        return False


def driver_alive(state: dict) -> bool:
    """Is the run's own conductor process still alive?

    state carries driver_pid/driver_started_at since 2026-10-15. The gap
    this closes: between worker exit and step_end the conductor spends
    minutes in verify+merge with a 'running' record pointing at a dead
    worker pid - the dashboard displayed 'lost', a startup sweep could
    WRITE 'lost'/ended_at over a live run, and the resume guard could let
    a second driver take over mid-verify. Runs recorded by older code
    lack the field and keep worker-pid-only semantics."""
    pid = state.get("driver_pid")
    if not isinstance(pid, int) or pid <= 0:
        return False
    return pid_alive(pid, state.get("driver_started_at")
                     or state.get("started_at"))


def step_live(rec: dict, driver_up: bool = False) -> bool:
    """Is a 'running' step record backed by something actually alive?

    Two live shapes: a live pid, or a fresh no-pid record. No-pid mid-run
    is normal - the pid is kept in memory only until the worker exits
    (trap 5) - but a no-pid 'running' record older than its own timeout
    means the conductor process died mid-step.

    driver_up (the run's conductor is alive): a 'running' record stays
    live while the driver is in the verify/merge window between worker
    exit and step_end - the worker pid is already dead there.
    """
    if rec.get("status") != "running":
        return False
    if rec.get("pid") and pid_alive(rec["pid"], rec.get("started_at")):
        return True
    if driver_up:
        return True
    if rec.get("pid"):
        return False
    t0 = _iso_ts(rec.get("started_at"))
    if t0 is None:
        return False
    timeout_s = float(rec.get("timeout_min") or 60) * 60
    return time.time() - t0 <= timeout_s + 900


def effective_statuses(state: dict) -> dict[str, str]:
    """Per-step status corrected for reality, without mutating state.json.

    'running' with nothing alive behind it displays as 'lost'. 'queued' is
    'lost' when the run ended or has been quiet >10min with nothing live -
    an orphaned queue entry can never start. A live driver (driver_pid)
    means neither applies - the conductor is mid-verify/merge, not dead.
    ('lost' steps re-run on --resume like any non-done step.)
    """
    steps = state.get("steps") or {}
    drv = driver_alive(state)
    live = {n: step_live(r, drv) for n, r in steps.items()}
    stamps = [_iso_ts(state.get(k)) for k in ("started_at", "ended_at")]
    stamps += [_iso_ts(r.get(k)) for r in steps.values()
               for k in ("started_at", "ended_at")]
    newest = max((t for t in stamps if t), default=None)
    quiet = time.time() - newest if newest else 0.0
    orphaned = not drv and (bool(state.get("ended_at"))
                            or (not any(live.values()) and quiet > 600))
    out = {}
    for n, r in steps.items():
        st = r.get("status", "?")
        if st == "running" and not live[n]:
            out[n] = "lost"
        elif st == "queued" and orphaned:
            out[n] = "lost"
        else:
            out[n] = st
    return out


def free_ram_gb() -> float | None:
    """Free physical memory via GlobalMemoryStatusEx (informational)."""
    try:
        import ctypes

        class MEMSTATUSEX(ctypes.Structure):
            _fields_ = [("dwLength", ctypes.c_ulong),
                        ("dwMemoryLoad", ctypes.c_ulong),
                        ("ullTotalPhys", ctypes.c_ulonglong),
                        ("ullAvailPhys", ctypes.c_ulonglong),
                        ("ullTotalPageFile", ctypes.c_ulonglong),
                        ("ullAvailPageFile", ctypes.c_ulonglong),
                        ("ullTotalVirtual", ctypes.c_ulonglong),
                        ("ullAvailVirtual", ctypes.c_ulonglong),
                        ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
        st = MEMSTATUSEX()
        st.dwLength = ctypes.sizeof(MEMSTATUSEX)
        ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st))
        return round(st.ullAvailPhys / 1e9, 1)
    except Exception:
        return None


# ---------------------------------------------------------------- power

def keep_awake() -> None:
    """Prevent system sleep for the duration of this process (Windows).
    ES_CONTINUOUS|ES_SYSTEM_REQUIRED; released automatically on exit."""
    try:
        import ctypes
        ctypes.windll.kernel32.SetThreadExecutionState(0x80000003)
        LOG.info("keep_awake: SetThreadExecutionState set")
    except Exception as exc:
        LOG.warning("keep_awake failed: %s", exc)


# ---------------------------------------------------------------- git

def _git(path: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(path), *args],
                          capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=120,
                          **hidden_subprocess_kwargs())


def is_git_repo(path: Path) -> bool:
    """True only if `path` is the ROOT of its own work tree.

    `rev-parse --is-inside-work-tree` is true for any dir nested inside a
    parent repo - and `git add -A` is repo-wide, not cwd-scoped, so treating a
    nested dir as "a repo" would checkpoint the parent. (Hit 2026-10-13: a
    demo fixture inside this repo got committed to agent-conductor itself.)
    """
    r = _git(path, "rev-parse", "--show-toplevel")
    if r.returncode != 0:
        return False
    top = os.path.normcase(str(Path(r.stdout.strip()).resolve()))
    return top == os.path.normcase(str(path.resolve()))


def git_dirty(path: Path) -> bool:
    return bool(_git(path, "status", "--porcelain").stdout.strip())


def git_dirty_tracked(path: Path) -> bool:
    """Modified TRACKED files only - the merge-resolver guard. Untracked
    files can never join a merge commit (git merge itself refuses the
    one collision case), so loose junk like a stray `nul` or a pending
    generated manifest must not refuse the resolver."""
    return bool(_git(path, "status", "--porcelain",
                     "--untracked-files=no").stdout.strip())


def git_commit(path: Path, msg: str, allow_empty: bool = False) -> bool:
    _git(path, "add", "-A")
    cmd = ["commit", "-m", msg]
    if allow_empty:
        cmd.append("--allow-empty")
    return _git(path, *cmd).returncode == 0


def sanitize_branch(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9._/-]", "-", s)


def worktree_add(repo_dir: Path, wt_path: Path, branch: str) -> bool:
    if wt_path.is_dir():
        return True   # resume: reuse the worktree left by an earlier attempt
    r = _git(repo_dir, "worktree", "add", str(wt_path), "-b", branch)
    if r.returncode != 0 and "already exists" in r.stderr:
        r = _git(repo_dir, "worktree", "add", str(wt_path), branch)
    return r.returncode == 0


def worktree_remove(repo_dir: Path, wt_path: Path, branch: str) -> None:
    _git(repo_dir, "worktree", "remove", "--force", str(wt_path))
    _git(repo_dir, "branch", "-D", branch)


def worktree_debris(repo_dir: Path, wt_path: Path, branch: str) -> bool:
    """A branch is debris when it carries no commits beyond the repo's HEAD
    (the step died before committing anything). Safe to delete."""
    r = _git(repo_dir, "rev-list", "--count", f"HEAD..{branch}")
    return r.returncode == 0 and r.stdout.strip() == "0"


def worktree_merge(repo_dir: Path, wt_path: Path, branch: str) -> bool:
    """Merge the step's branch into the main checkout. On conflict, abort the
    merge and LEAVE the worktree+branch for a human - the work is not lost.
    A merge already in progress (someone else's - abort_own_merge cleared
    ours) makes this refuse instead of `merge --abort`-ing a foreign merge."""
    if merge_in_progress(repo_dir):
        return False
    r = _git(repo_dir, "merge", "--no-edit", branch)
    if r.returncode != 0:
        _git(repo_dir, "merge", "--abort")
        return False
    worktree_remove(repo_dir, wt_path, branch)
    return True


def unmerged_paths(repo_dir: Path) -> list[str]:
    """Paths the index still marks conflicted (ls-files -u), deduped."""
    r = _git(repo_dir, "diff", "--name-only", "--diff-filter=U")
    return r.stdout.split() if r.returncode == 0 else []


def merge_in_progress(repo_dir: Path) -> bool:
    return _git(repo_dir, "rev-parse", "-q", "--verify",
                "MERGE_HEAD").returncode == 0


def has_conflict_markers(repo_dir: Path, rel: str) -> bool:
    try:
        return any(ln.startswith(("<<<<<<<", ">>>>>>>")) for ln in
                   (repo_dir / rel).read_text(
                       encoding="utf-8", errors="replace").splitlines())
    except OSError:
        return False


def abort_own_merge(repo_dir: Path) -> bool:
    """Abort an in-progress merge ONLY if it is conductor's own: MERGE_HEAD
    must sit on a conductor/* branch. A merge a human started (or one on an
    unrelated branch) is left alone. Covers 'conductor died mid-resolution':
    resume finds the stale merge and backs it out before re-running."""
    mh = _git(repo_dir, "rev-parse", "-q", "--verify", "MERGE_HEAD")
    if mh.returncode != 0:
        return False
    sha = mh.stdout.strip()
    brs = _git(repo_dir, "branch", "--contains", sha,
               "--list", "conductor/*")
    if "conductor/" not in brs.stdout:
        return False
    return _git(repo_dir, "merge", "--abort").returncode == 0


# ---------------------------------------------------------------- run state

def now_iso() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


def save_state(run_dir: Path, state: dict) -> None:
    with STATE_LOCK:
        tmp = run_dir / "state.json.tmp"
        tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
        # Windows: a reader or the AV scanner can hold either path open for
        # a few ms and the rename fails with WinError 5. Retry briefly -
        # losing a state write is far worse than a 100ms stall.
        for attempt in range(20):
            try:
                tmp.replace(run_dir / "state.json")
                return
            except PermissionError:
                if attempt == 19:
                    raise
                time.sleep(0.1)


def log_event(run_dir: Path, **kv) -> None:
    kv["ts"] = now_iso()
    line = json.dumps(kv, ensure_ascii=False)
    with STATE_LOCK:
        with open(run_dir / "events.jsonl", "a", encoding="utf-8") as f:
            f.write(line + "\n")


def tail_status(stdout_log: Path) -> str | None:
    """The worker's terminal STATUS: line. Reads the WHOLE log, not a
    tail: an agent that prints 'STATUS: done' then a long closing report
    (>8KB of prose) must not flip to 'failed - no STATUS line' - that
    exact false negative was hit on the live apartment run. Logs are
    small and this runs once per step end, not per poll."""
    try:
        data = stdout_log.read_bytes().decode("utf-8", "replace")
    except OSError:
        return None
    found = STATUS_RE.findall(data)
    return found[-1].lower() if found else None


def tail_next(stdout_log: Path) -> str | None:
    """The worker's NEXT: line - its one-line report to the next session
    (and to a human reading the dashboard). Whole-file like tail_status:
    a closing report can push NEXT: past any fixed tail window."""
    try:
        data = stdout_log.read_bytes().decode("utf-8", "replace")
    except OSError:
        return None
    found = NEXT_RE.findall(data)
    return found[-1].strip()[:300] if found else None


def tail_session_ref(stdout_log: Path) -> str | None:
    """Cloud sessions never show up in `devin list` (they live server-side);
    the CLI prints an app.devin.ai session URL to stdout instead."""
    try:
        data = stdout_log.read_bytes()[-65536:].decode("utf-8", "replace")
    except OSError:
        return None
    matches = SESSION_URL_RE.findall(data)
    if not matches:
        return None
    return matches[-1][0] or matches[-1][1]


def tail_metrics(export_path: Path) -> dict | None:
    """Token totals from the ATIF export's final_metrics (written by
    `devin --export` at session end): prompt/completion/cached token sums
    and the LLM step count. None for mocks and unfinished steps."""
    try:
        d = json.loads(export_path.read_text(encoding="utf-8",
                                             errors="replace"))
    except (OSError, ValueError):
        return None
    fm = d.get("final_metrics")
    if not isinstance(fm, dict):
        return None
    out = {}
    for src, dst in (("total_prompt_tokens", "prompt"),
                     ("total_completion_tokens", "completion"),
                     ("total_cached_tokens", "cached"),
                     ("total_steps", "llm_steps")):
        if isinstance(fm.get(src), (int, float)):
            out[dst] = int(fm[src])
    return out or None


def tail_agent_model(export_path: Path) -> str | None:
    """The model the session ACTUALLY ran (export's agent.model_name) -
    the resolved pick when a step requests `adaptive`. None for mocks and
    unfinished steps."""
    try:
        d = json.loads(export_path.read_text(encoding="utf-8",
                                             errors="replace"))
    except (OSError, ValueError):
        return None
    name = (d.get("agent") or {}).get("model_name")
    return name if isinstance(name, str) and name else None


# Trajectory arc: condense an ATIF export's `steps` array into labeled
# phase spans (setup -> audit -> implement -> test -> ... -> commit) so
# the task modal can show "what the session did" in ~15 lines instead of
# 77 raw LLM steps. Heuristic only - the phase tag is cosmetic.
_ARC_PHASE_RX = [
    ("commit", re.compile(r"commit|staged|git add|checkpoint", re.I)),
    ("test", re.compile(r"\btests?\b|suite|pytest|run_tests|unittest|"
                        r"\bgreen\b|fail(?:ed|ures?)?\b|verify", re.I)),
    ("docs", re.compile(r"agents\.md|readme|queue\.md|state\.md|roadmap|"
                        r"changelog|documentation|\bdocs\b", re.I)),
    ("audit", re.compile(r"check|audit|read|look|examine|inspect|grep|"
                         r"search|find|review|understand|see ", re.I)),
    ("implement", re.compile(r"edit|update|add|wire|endpoint|rename|"
                             r"refactor|\bfix|todo|now ", re.I)),
]
_ARC_WRITE_TOOLS = {"edit", "write", "notebook_edit"}
_ARC_READ_TOOLS = {"read", "grep", "code_search", "web_search", "webfetch",
                   "get_output", "notebook_read", "ls", "find_file_by_name",
                   "mcp_read_resource", "mcp_list_tools", "mcp_list_servers",
                   "skill", "read_subagent"}


def _arc_phase(step: dict) -> str:
    if step.get("source") != "agent":
        return "setup"
    calls = [c for c in (step.get("tool_calls") or [])
             if isinstance(c, dict)]
    names = [c.get("function_name") or "" for c in calls]
    msg = (step.get("message") or "").strip()
    if msg:
        hay = msg
    else:
        # Message-less step: classify on tool names + SHORT args only
        # (command, file_path). Edit payloads are file contents - pure
        # keyword noise - and reasoning_content is 4k of free text that
        # mentions every phase in passing, so neither belongs here.
        bits = list(names)
        for c in calls:
            a = c.get("arguments")
            if isinstance(a, dict):
                v = (a.get("command") or a.get("file_path")
                     or a.get("notebook_path") or a.get("pattern") or "")
                bits.append(str(v)[:300])
        hay = " ".join(bits)
    for phase, rx in _ARC_PHASE_RX:
        if rx.search(hay):
            return phase
    if any(n in _ARC_WRITE_TOOLS for n in names):
        return "implement"
    if calls and all(n in _ARC_READ_TOOLS for n in names):
        return "audit"
    return "work"


def _arc_head(step: dict) -> str:
    msg = " ".join((step.get("message") or "").split())
    if msg:
        return msg[:110]
    calls = [c.get("function_name") or "?"
             for c in (step.get("tool_calls") or [])
             if isinstance(c, dict)]
    if calls:
        seen: dict[str, int] = {}
        for n in calls:
            seen[n] = seen.get(n, 0) + 1
        return " + ".join(f"{n}x{k}" if k > 1 else n
                          for n, k in seen.items())
    rc = " ".join((step.get("reasoning_content") or "").split())
    return ("think: " + rc[:100]) if rc else "(no detail)"


def _arc_ts(s: dict) -> float | None:
    """Entry timestamp as epoch seconds (None if absent/unparseable)."""
    try:
        return dt.datetime.fromisoformat(str(s["timestamp"])).timestamp()
    except (KeyError, ValueError, TypeError):
        return None


def trajectory_arc(export_path) -> dict | None:
    """Phase-grouped timeline of an ATIF export for the task modal.
    None for missing/corrupt exports, mocks, and live steps (the export
    is written once when the session ends - there is nothing mid-run).
    Groups collapse in the UI; each carries its raw entries (timestamps,
    gap-to-next durations, REAL per-step token metrics - ATIF exports
    meter every LLM call, so nothing here is estimated)."""
    try:
        d = json.loads(Path(export_path).read_text(
            encoding="utf-8", errors="replace"))
    except (OSError, ValueError, TypeError):
        return None
    steps = d.get("steps")
    if not isinstance(steps, list) or not steps:
        return None
    groups: list[dict] = []
    for s in steps:
        if not isinstance(s, dict):
            continue
        phase = _arc_phase(s)
        m = s.get("metrics") if isinstance(s.get("metrics"), dict) else {}
        ent = {"sid": s.get("step_id"), "ts": s.get("timestamp"),
               "head": _arc_head(s),
               "epoch": _arc_ts(s)}
        for src, dst in (("prompt_tokens", "in"),
                         ("completion_tokens", "out"),
                         ("cached_tokens", "cached")):
            if isinstance(m.get(src), (int, float)):
                ent[dst] = int(m[src])
        if groups and groups[-1]["phase"] == phase:
            groups[-1]["entries"].append(ent)
        else:
            groups.append({"phase": phase, "entries": [ent]})
    if not groups:
        return None
    out = []
    for g in groups:
        ents = g["entries"]
        for i, e in enumerate(ents):
            nxt = ents[i + 1]["epoch"] if i + 1 < len(ents) else None
            if e["epoch"] is not None and nxt is not None:
                e["dur_s"] = round(nxt - e["epoch"])
            e.pop("epoch", None)
        span = (str(ents[0]["sid"]) if ents[0]["sid"] == ents[-1]["sid"]
                else f"{ents[0]['sid']}-{ents[-1]['sid']}")
        t0 = ents[0]["ts"]
        tin = sum(e.get("in", 0) for e in ents) or None
        tout = sum(e.get("out", 0) for e in ents) or None
        tcache = sum(e.get("cached", 0) for e in ents) or None
        out.append({"span": span, "phase": g["phase"], "n": len(ents),
                    "head": ents[0]["head"], "t0": t0,
                    "t1": ents[-1]["ts"], "tin": tin, "tout": tout,
                    "tcached": tcache,
                    "entries": [{k: v for k, v in e.items()
                                 if v is not None} for e in ents]})
    return {"steps": len(steps), "groups": out[:40]}


_ARC_CACHE: dict[str, tuple[float, dict | None]] = {}


def cached_arc(path) -> dict | None:
    """mtime-keyed memo over trajectory_arc - exports land once at
    session end and never change afterwards, and collect_dashboard runs
    every refresh, so parsing the JSON per poll would be pure waste."""
    if not path:
        return None
    try:
        m = os.path.getmtime(path)
    except OSError:
        return None
    key = str(path)
    hit = _ARC_CACHE.get(key)
    if hit and hit[0] == m:
        return hit[1]
    arc = trajectory_arc(path)
    _ARC_CACHE[key] = (m, arc)
    return arc


def _fmtK(n: int) -> str:
    """Python twin of the dashboard's fmtK: 10754521 -> 10.8M."""
    if n >= 1_000_000:
        s, u = f"{n / 1_000_000:.1f}", "M"
    elif n >= 1000:
        s, u = f"{n / 1000:.1f}", "k"
    else:
        return str(n)
    return s.rstrip("0").rstrip(".") + u


def _branch_commits(repo: str | None, branch: str | None) -> list[str]:
    """`git log --oneline <default>..<branch>` for a step's branch -
    best effort: gone branch or deleted worktree just yields no section."""
    if not repo or not branch:
        return []
    for base in ("master", "main"):
        try:
            r = subprocess.run(
                ["git", "-C", repo, "log", "--oneline",
                 f"{base}..{branch}"],
                capture_output=True, text=True, timeout=15,
                **hidden_subprocess_kwargs())
        except (OSError, subprocess.SubprocessError):
            return []
        if r.returncode == 0:
            return [ln for ln in r.stdout.splitlines() if ln.strip()]
    return []


def step_summary_md(rec: dict, st: dict, run_name: str) -> str:
    """Human-readable end-of-step report - the modal's 'Session summary'
    link. Built on demand from state.json + the cached ATIF arc, so it
    works retroactively for runs that predate the artifact."""
    name = rec.get("name") or "-"
    t = rec.get("tokens") or {}
    prompt_t = t.get("prompt") or 0
    cached_t = t.get("cached") or 0
    out_t = t.get("completion") or 0
    billed = max(0, prompt_t - cached_t)
    span = rec.get("elapsed_s")
    el = (f"{int(span // 60)}m{int(span % 60):02d}s"
          if isinstance(span, (int, float)) else "-")
    est = estimate_cost(t, rec.get("model"), rec.get("effort"),
                        price_table(), rec.get("model_actual"))
    cost_s = (f"${est['usd']:.3f}" if est and est.get("usd") is not None
              else "unpriced")
    status = rec.get("status") or "?"
    verify = rec.get("verify_ok")
    verify_s = ("pass" if verify is True else "fail" if verify is False
                else "n/a")
    branch = rec.get("branch")
    merged = rec.get("merged")
    branch_s = ""
    if branch:
        branch_s = f"`{branch}` - " + (
            "merged" if merged else "unmerged, kept at "
            f"`{rec.get('work_dir') or rec.get('project')}`")
    L = [f"# {run_name} / {name}", ""]
    L += [
        f"- **status:** {status} · **verify:** {verify_s} · "
        f"**exit:** {rec.get('exit_code', '-')}",
        f"- **started:** {rec.get('started_at') or '-'} · "
        f"**ended:** {rec.get('ended_at') or '-'} · **elapsed:** {el}",
        f"- **model:** {rec.get('model_actual') or rec.get('model') or '-'}"
        f" (effort {rec.get('effort') or '-'}) · "
        f"**mode:** {rec.get('mode') or '-'} · "
        f"**attempts:** {rec.get('attempts', 1)}/"
        f"{rec.get('max_attempts', 1)}",
        f"- **session:** {rec.get('session_id') or '-'}",
        f"- **tokens:** billed in {_fmtK(billed)} · out {_fmtK(out_t)} · "
        f"cached {_fmtK(cached_t)} · {t.get('llm_steps') or 0} turns · "
        f"est {cost_s} (full prompt sum {_fmtK(prompt_t)})",
    ]
    if branch_s:
        L.append(f"- **branch:** {branch_s}")
    if rec.get("note"):
        L.append(f"- **note:** {rec['note']}")
    if rec.get("next"):
        L.append(f"- **worker says next:** {rec['next']}")
    L.append("")
    if rec.get("task"):
        L += ["## Task", "", rec["task"], ""]
    arc = cached_arc(rec.get("export"))
    if arc and arc.get("groups"):
        L += [f"## Trajectory ({arc['steps']} turns, times UTC)", ""]
        for g in arc["groups"]:
            bits = [f"`{g['span']}` **{g['phase']}**"]
            t0, t1 = g.get("t0"), g.get("t1")
            if t0:
                bits.append(str(t0)[11:19] +
                            (f"-{str(t1)[11:19]}" if t1 and t1 != t0
                             else ""))
            if g["n"] > 1:
                bits.append(f"{g['n']} turns")
            if g.get("tin") or g.get("tout"):
                billed_g = max(0, (g["tin"] or 0) - (g["tcached"] or 0))
                bits.append(f"in {_fmtK(billed_g)} · out "
                            f"{_fmtK(g['tout'] or 0)} · cached "
                            f"{_fmtK(g['tcached'] or 0)}")
            head = (g.get("head") or "").strip()
            L.append("- " + " · ".join(bits) +
                     (f" — {head[:110]}" if head else ""))
        L.append("")
    commits = _branch_commits(rec.get("project") or rec.get("work_dir"),
                              branch)
    if commits:
        L += [f"## Commits ({len(commits)})", ""]
        L += [f"- {c}" for c in commits]
        L.append("")
    tail = stdout_tail(rec.get("stdout_log"), 15)
    if tail:
        L += ["## stdout tail", "", "```", tail, "```", ""]
    return "\n".join(L)


def step_summary(run_name: str, step_name: str,
                 runs_dir: Path = RUNS_DIR) -> str:
    """GET /api/runs/<run>/steps/<step>/summary - render the report and,
    once the step is no longer live, persist it as <step>.summary.md in
    the run dir. Writing while a worker runs could trip user-edit
    awareness on self-hosted runs (trap 5), so live steps serve a fresh
    render without touching disk."""
    if not RUN_DIR_RE.fullmatch(step_name):
        raise RequestError("Invalid step name", 404)
    d, st = _run_dir_checked(run_name, runs_dir)
    rec = (st.get("steps") or {}).get(step_name)
    if rec is None:
        raise RequestError("Step not in this run", 404)
    md = step_summary_md({**rec, "name": rec.get("name") or step_name},
                         st, run_name)
    drv = driver_alive(st)
    if not any(step_live(r, drv)
               for r in (st.get("steps") or {}).values()):
        try:
            (d / f"{step_name}.summary.md").write_text(md, encoding="utf-8")
        except OSError:
            pass
    return md


def summary_html(md: str, title: str) -> str:
    """Render our own generated summary markdown as a styled page.

    We only emit a narrow markdown subset (#, ##, - bullets, ``` fences,
    **bold**, `code`), and every line is HTML-escaped BEFORE inline
    markup is applied, so agent-authored content (task text, stdout,
    trajectory heads) can never inject markup."""
    def inline(s: str) -> str:
        s = html.escape(s)
        s = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s)
        return re.sub(r"`(.+?)`", r"<code>\1</code>", s)

    body, in_ul, in_pre = [], False, False

    def close_ul() -> None:
        nonlocal in_ul
        if in_ul:
            body.append("</ul>")
            in_ul = False

    for ln in md.splitlines():
        if in_pre:
            if ln.strip() == "```":
                body.append("</pre>")
                in_pre = False
            else:
                body.append(html.escape(ln))
            continue
        if ln.strip() == "```":
            close_ul()
            body.append("<pre>")
            in_pre = True
        elif ln.startswith("## "):
            close_ul()
            body.append(f"<h2>{inline(ln[3:])}</h2>")
        elif ln.startswith("# "):
            close_ul()
            body.append(f"<h1>{inline(ln[2:])}</h1>")
        elif ln.startswith("- "):
            if not in_ul:
                body.append("<ul>")
                in_ul = True
            body.append(f"<li>{inline(ln[2:])}</li>")
        elif not ln.strip():
            close_ul()
        else:
            close_ul()
            body.append(f"<p>{inline(ln)}</p>")
    close_ul()
    if in_pre:
        body.append("</pre>")
    return ("<!doctype html><html><head><meta charset=utf-8>"
            f"<title>{html.escape(title)}</title><style>"
            "body{background:#0d1117;color:#c9d1d9;font:14px/1.5 "
            "'Segoe UI',system-ui,sans-serif;max-width:900px;margin:"
            "24px auto;padding:0 16px}"
            "h1{font-size:20px;border-bottom:1px solid #21262d;"
            "padding-bottom:8px}h2{font-size:15px;color:#58a6ff;"
            "margin:20px 0 6px}ul{padding-left:20px}li{margin:3px 0}"
            "code{background:#161b22;border-radius:4px;padding:1px 5px;"
            "font-size:12px}pre{background:#161b22;border-radius:6px;"
            "padding:10px 12px;font-size:12px;overflow:auto;"
            "white-space:pre-wrap}strong{color:#e6edf3}</style></head>"
            f"<body>{''.join(body)}</body></html>")


def _merge_tokens(acc: dict[str, int], tok: dict | None) -> dict:
    for k, v in (tok or {}).items():
        if isinstance(v, (int, float)):
            acc[k] = acc.get(k, 0) + int(v)
    return acc


def kill_tree(proc) -> None:
    """Kill the worker AND its children. Bare proc.kill() orphans
    grandchildren - a spawned vite kept :8805 bound for hours after its
    run ended (hit 2026-10-14); taskkill /T walks the tree on Windows."""
    if proc.poll() is not None:
        return
    if os.name == "nt":
        r = subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                           capture_output=True, **hidden_subprocess_kwargs())
        if r.returncode != 0 and proc.poll() is None:
            proc.kill()
    else:
        proc.kill()


# ------------------------------------------------------------------ stops
# Stop/drain sentinels live in the OS temp dir - NEVER inside run_dir:
# data/runs sits inside the agent-conductor repo, so writing a flag file
# there during a self-hosted run would land in the worker's watched cwd
# and trip user-edit-awareness (trap 5). The temp dir is outside every
# project's watch set, so a dashboard stop cannot corrupt a live session.

def _flag_dir() -> Path:
    return Path(tempfile.gettempdir()) / "agent-conductor-flags"


def _safe_name(s: str) -> str:
    return re.sub(r"[^\w.-]", "_", s)


def _flag_key(run_dir: Path) -> str:
    """Sentinel names pin the run's resolved path, not just its dir name -
    the flag dir is machine-global, and the same `<ts>-<name>` can live in
    two checkouts' data/runs (or two test sandboxes) at once."""
    h = hashlib.sha256(
        os.path.normcase(str(run_dir.resolve())).encode()).hexdigest()[:8]
    return f"{_safe_name(run_dir.name)}-{h}"


def stop_flag(run_dir: Path, step: str | None = None) -> Path:
    n = "stop-" + _flag_key(run_dir)
    return _flag_dir() / (n + f"-{_safe_name(step)}" if step else n)


def drain_flag(run_dir: Path) -> Path:
    return _flag_dir() / f"drain-{_flag_key(run_dir)}"


def timeout_flag(run_dir: Path, step: str) -> Path:
    """Minutes override written by the dashboard's timeout editor;
    wait_worker re-reads it each poll and moves the deadline."""
    return _flag_dir() / f"timeout-{_flag_key(run_dir)}-{_safe_name(step)}"


def pause_flag(run_dir: Path) -> Path:
    """Dashboard 'Pause run' sentinel: a GRACEFUL hold - in-flight steps
    run to completion, then nothing new launches and the run records
    aborted='paused'. Resume continues where it left off (the deliberate
    machine-restart case, distinct from a hard stop or a crash)."""
    return _flag_dir() / f"pause-{_flag_key(run_dir)}"


def skip_flag(run_dir: Path, step: str) -> Path:
    """Dashboard 'Skip step' sentinel: the step records 'skipped' instead
    of spawning while the flag exists. Unlike the halt sentinels it is
    DELIBERATE and durable - clear_run_flags leaves it alone, so a skip
    survives --resume until the user clicks Unskip."""
    return _flag_dir() / f"skip-{_flag_key(run_dir)}-{_safe_name(step)}"


def _skip_flagged(run_dir: Path) -> set[str]:
    """_safe_name'd step names currently carrying a skip sentinel."""
    prefix = f"skip-{_flag_key(run_dir)}-"
    try:
        return {f.name[len(prefix):]
                for f in _flag_dir().glob(prefix + "*")}
    except OSError:
        return set()


def prompt_flag(run_dir: Path, step: str) -> Path:
    """Dashboard 'Edit prompt' override: while a step has never been
    dispatched, this file holds the replacement task body; run_step
    reads it right before rendering. Durable like skip- - it survives
    clear_run_flags so an edit placed on a pending step is still honored
    when a later resume dispatches it."""
    return _flag_dir() / f"prompt-{_flag_key(run_dir)}-{_safe_name(step)}"


def rnote_flag(run_dir: Path, step: str) -> Path:
    """Dashboard 'resume notes': operator text appended to the step's
    next dispatched prompt (after the task body), so a resumed session
    can be told things like 'VPN is restored'. For steps that already
    ran the original prompt is never edited - notes are the add-on."""
    return _flag_dir() / f"rnote-{_flag_key(run_dir)}-{_safe_name(step)}"


def gen_stop_flag(request_id: str) -> Path:
    """Dashboard 'Stop proposal' sentinel: polled by the planner's
    wait_worker while a request generates. Request ids are already
    filename-safe (YYYYMMDD-HHMMSS-<8hex>, REQUEST_ID_RE), and unlike run
    sentinels they cannot collide across checkouts - each id exists in
    exactly one requests dir."""
    return _flag_dir() / f"stopgen-{request_id}"


def _flag_text(flag: Path) -> str | None:
    try:
        return flag.read_text(encoding="utf-8") if flag.is_file() else None
    except OSError:
        return None


# Runs launched by an older conductor poll BARE-name sentinels
# (stop-<run>, drain-<run>, timeout-<run>-<step>) - the path-keyed names
# above only exist in new code. state["flags"]="v2" marks a run whose
# driver knows the new scheme; without it, flag writes dual-write both
# names so a new dashboard can still stop/drain/pause an old-code run
# (e.g. a run that outlives the dashboard that spawned it).
def _flag_paths(run_dir: Path, st: dict | None, new_p: Path,
                legacy: str) -> list[Path]:
    paths = [new_p]
    if (st or {}).get("flags") != "v2":
        paths.append(_flag_dir() /
                     f"{legacy}-{_safe_name(run_dir.name)}")
    return paths


def stop_flags_for(run_dir: Path, st: dict | None,
                   step: str | None = None) -> list[Path]:
    """Every sentinel that means 'halt' for the code version driving this
    run: path-keyed always, plus the bare-name variant for legacy runs."""
    paths = [stop_flag(run_dir, step)]
    if (st or {}).get("flags") != "v2":
        n = f"stop-{_safe_name(run_dir.name)}"
        paths.append(_flag_dir() /
                     (n + f"-{_safe_name(step)}" if step else n))
    return paths


def drain_flags_for(run_dir: Path, st: dict | None) -> list[Path]:
    return _flag_paths(run_dir, st, drain_flag(run_dir), "drain")


def pause_flags_for(run_dir: Path, st: dict | None) -> list[Path]:
    return _flag_paths(run_dir, st, pause_flag(run_dir), "pause")


def _flag_set(*flags: Path) -> Path | None:
    return next((f for f in flags if f.is_file()), None)


def _append_note(rec: dict, text: str) -> None:
    rec["note"] = (rec["note"] + "; " + text) if rec.get("note") else text


def _flag_raise(name: str) -> None:
    """Write a stop/drain sentinel. Best-effort: a mkdir/write failure only
    delays the stop by one poll, so callers don't crash on it."""
    try:
        _flag_dir().mkdir(exist_ok=True)
        (_flag_dir() / name).touch()
    except OSError:
        pass


def clear_run_flags(run_dir: Path) -> None:
    """Remove all HALT sentinels for a run - called on resume/merge so a
    previously stopped run can actually start work again. Clears both
    path-keyed names and legacy bare-name ones: a run paused under old
    code must be resumable by new code. Skip sentinels deliberately
    survive: a user's skip stays in force until they Unskip, or a resume
    would silently resurrect a step they removed from the queue. The
    prompt-edit and resume-notes sentinels survive too - the resume is
    exactly when run_step reads them."""
    name = _safe_name(run_dir.name)
    pats = [f"*{_flag_key(run_dir)}*",
            f"stop-{name}*", f"drain-{name}", f"pause-{name}",
            f"timeout-{name}-*"]
    try:
        for pat in pats:
            for f in _flag_dir().glob(pat):
                # skip- stays in force until Unskip; prompt-/rnote- are
                # the pending step's edited prompt and resume notes -
                # both are meant to survive the resume that is about to
                # read them, so none of the three are halt sentinels.
                if f.name.startswith(("skip-", "prompt-", "rnote-")):
                    continue
                try:
                    f.unlink()
                except OSError:
                    pass
    except OSError:
        pass


TIMEOUT_MAX_EXT = 3        # a live log auto-extends past timeout_min up to
                           # this many extra floor units: ceiling = 4x


def wait_worker(proc, stdout_log: Path, timeout_s: float,
                stop_flags: tuple[Path, ...] = (),
                timeout_flag: Path | None = None,
                stall_s: float | None = None
                ) -> tuple[int | None, bool, bool, bool, float]:
    """Wait for the worker; returns (rc, timed_out, hung_after_status,
    user_stopped, extended_s).

    Polls in short slices so a STATUS line in the log is noticed within
    WAIT_POLL_S: the turn is then over, so the remaining wait collapses to
    STATUS_GRACE_S before the tree is killed. hung_after_status tells the
    caller the work completed - run verify and honor the status line
    rather than reporting a timeout. A stop sentinel appearing mid-wait
    tree-kills the worker and reports user_stopped. timeout_flag, if it
    appears, carries minutes set from the dashboard - the deadline moves
    to start+that value (a value under elapsed kills next poll).

    timeout_s is a floor, not a guillotine, when stall_s is set: past the
    deadline the worker keeps running while the stdout log grew within the
    last stall_s - a live turn is never rushed - but silence >= stall_s or
    the hard ceiling at (1+TIMEOUT_MAX_EXT)x the floor ends it. extended_s
    reports how far past the floor the step actually ran."""
    t_start = time.time()
    floor_s = timeout_s
    deadline = t_start + floor_s
    ceiling = t_start + floor_s * (1 + TIMEOUT_MAX_EXT)
    grace_at = None
    last_change = t_start
    last_stat = None

    def ext() -> float:
        return max(0.0, time.time() - (t_start + floor_s))

    while True:
        try:
            rc = proc.wait(timeout=WAIT_POLL_S)
            # The stop endpoint can kill the pid directly between its
            # flag-write and our next poll - the sentinel must still win
            # over a bare nonzero rc, or a user stop records as 'failed'.
            if _flag_set(*stop_flags) is not None:
                return rc, False, False, True, ext()
            return rc, False, False, False, ext()
        except subprocess.TimeoutExpired:
            pass
        now = time.time()
        try:
            stat = stdout_log.stat()
            cur = (stat.st_size, stat.st_mtime_ns)
        except OSError:
            cur = None
        if cur is not None and cur != last_stat:
            last_stat, last_change = cur, now
        if timeout_flag is not None:
            try:
                floor_s = float(timeout_flag.read_text().strip()) * 60
                deadline = t_start + floor_s
                ceiling = t_start + floor_s * (1 + TIMEOUT_MAX_EXT)
            except (OSError, ValueError):
                pass
        if _flag_set(*stop_flags) is not None:
            kill_tree(proc)
            return proc.wait(), False, False, True, ext()
        if tail_status(stdout_log) is not None:
            if grace_at is None:
                grace_at = now + STATUS_GRACE_S
            if now >= grace_at:
                kill_tree(proc)
                return proc.wait(), False, True, False, ext()
        elif now >= deadline:
            if stall_s is not None and now < ceiling \
                    and now - last_change < stall_s:
                continue           # log still growing - silently extended
            kill_tree(proc)
            return proc.wait(), True, False, False, ext()


def _capture_session_id(rec: dict, devin_argv: list[str], work_dir: Path,
                        before: set[str], state: dict, run_dir: Path,
                        watched: bool, proc) -> None:
    """Record the spawned session id while the step is still running.

    The post-wait `devin list` diff already captures it, but a conductor
    that dies mid-step loses that write forever - the orphaned session is
    then indistinguishable from a human one and invisible to `sessions`
    (AGENTS.md 'Running things'). The id registers in `devin list` seconds
    after spawn; probe a few times, then stop. `devin list` only reads, so
    running it inside the worker's watched cwd is safe (verified live: a
    list call mid-step did not disturb the worker).

    Best-effort by design: any failure just leaves session_id for the
    post-wait diff.
    """
    try:
        for _ in range(5):
            try:
                proc.wait(timeout=25)
                return                   # step already over - diff wins
            except subprocess.TimeoutExpired:
                pass
            new = (session_ids(devin_argv, work_dir) or set()) - before
            if len(new) != 1:
                continue                 # not registered yet, or ambiguous
            with STATE_LOCK:
                if not rec.get("session_id"):
                    rec["session_id"] = sorted(new)[0]
                    if not watched:
                        save_state(run_dir, state)
            return
    except Exception:
        return


def run_step(step: Step, slug: str, work_dir: Path, cfg: Manifest,
             run_dir: Path, devin_argv: list[str], state: dict,
             do_checkpoint: bool = True, branch: str | None = None,
             post_commit: bool = True,
             resume_note: str | None = None) -> str:
    """Run one step; return final status: done|partial|blocked|failed|timed_out
    |stopped|skipped.

    work_dir is where the session runs and where checkpoints/verify happen -
    the project dir, or a git worktree for parallel steps. When a worktree is
    used the caller owns the pre-step checkpoint (on the main checkout), so
    do_checkpoint=False. post_commit=False skips the post-step `git add -A &&
    commit` sweep - required for the merge resolver, whose work_dir sits in
    an in-progress merge where a blind commit could adopt conflict markers.

    The step's record in state.json doubles as the agent registry entry:
    status/started_at are set while it queues, pid once the child process
    exists, and ended_at/elapsed_s/session_id when it finishes. The dashboard
    reads exactly this.

    A step that hits timeout_min is committed as-is and retried in a fresh
    session (up to max_attempts total) with a "continue from committed
    state" note prepended - an underestimated timeout continues instead of
    dying. Each attempt gets its own prompt/stdout/export artifacts.

    resume_note (an INTERRUPTED_NOTE render) is prepended to the task body -
    used when --resume restarts a step whose last session died abruptly.

    Stop sentinels (dashboard Stop buttons) are tempdir files polled here:
    one present before spawn -> 'skipped' (never ran); one appearing
    mid-wait -> tree-kill and 'stopped'. A 'Skip step' sentinel lands the
    same 'skipped' but is NOT consumed - it stays in force (even across
    --resume) until the user clicks Unskip.
    """
    halted = _flag_set(stop_flag(run_dir),
                       stop_flag(run_dir, step.name))
    user_skip = halted is None and skip_flag(run_dir, step.name).is_file()
    if halted is not None or user_skip:
        per_step = _flag_set(stop_flag(run_dir, step.name))
        if per_step is not None:
            per_step.unlink(missing_ok=True)
        if user_skip:
            note = "skipped by user (Skip step)"
        elif pause_flag(run_dir).is_file():
            note = "run paused"
        else:
            note = "stopped by user"
        with STATE_LOCK:
            state["steps"].setdefault(step.name, {}).update(
                name=step.name, slug=slug, status="skipped",
                ended_at=now_iso(),
                note=note + " before the session spawned")
            save_state(run_dir, state)
        log_event(run_dir, event="step_stopped", step=step.name,
                  phase="before_spawn", user_skip=user_skip)
        return "skipped"

    body_src = step.prompt
    if body_src is None and step.prompt_file:
        try:
            body_src = (cfg.path.parent / step.prompt_file).read_text(
                encoding="utf-8")
        except OSError:
            body_src = step.prompt_file

    # Dashboard prompt edits / resume notes are tempdir sentinels (trap-5
    # safe): an override replaces the task body for every dispatch while
    # it exists; notes append operator context under it. Both are durable
    # (clear_run_flags leaves them) so they also apply on resume.
    prompt_edited = False
    try:
        over = prompt_flag(run_dir, step.name)
        if over.is_file():
            body_src = over.read_text(encoding="utf-8")
            prompt_edited = True
    except OSError:
        pass
    rnotes = _flag_text(rnote_flag(run_dir, step.name))
    if rnotes and rnotes.strip():
        body_src = (body_src or "").rstrip() + "\n\n" + \
            RESUME_NOTES.format(notes=rnotes.strip())

    prompt_path = run_dir / "prompts" / f"{step.name}.md"
    prompt_path.parent.mkdir(exist_ok=True)
    # Always render from body_src: it is the resolved body - manifest
    # prompt or prompt_file contents - with any dashboard prompt override
    # and resume notes already folded in.
    render_step = dataclasses.replace(
        step, prompt=(resume_note or "") + (body_src or ""),
        prompt_file=None)
    prompt_path.write_text(
        render_prompt(render_step, slug, work_dir, cfg.path.parent,
                      cfg.preamble), encoding="utf-8")
    stdout_log = run_dir / f"{step.name}.stdout.log"
    export_path = run_dir / f"{step.name}.export.json"

    with STATE_LOCK:
        rec = state["steps"].setdefault(step.name, {})
        rec.update(name=step.name, slug=slug, project=str(work_dir),
                   work_dir=str(work_dir), branch=branch,
                   status="running", started_at=now_iso(), ended_at=None,
                   model=step.model, mode=step.mode, cloud=step.cloud,
                   effort=step.effort, timeout_min=step.timeout_min,
                   max_attempts=step.max_attempts,
                   task=" ".join((body_src or "").split())[:160],
                   prompt=str(prompt_path), stdout_log=str(stdout_log),
                   export=str(export_path))
        save_state(run_dir, state)
    log_event(run_dir, event="step_start", step=step.name, project=str(work_dir))

    if cfg.checkpoint and do_checkpoint:
        if not is_git_repo(work_dir):
            with STATE_LOCK:
                rec.update(status="failed",
                           note="not a git repo root; checkpoint required")
                save_state(run_dir, state)
            return "failed"
        git_commit(work_dir, f"conductor: checkpoint before {step.name}",
                   allow_empty=True)

    t0 = time.time()
    timed_out = False
    hung = False
    stopped = False
    ext_s = 0.0
    rc: int | None = None
    status_line = None
    new_sessions: set[str] = set()
    all_sessions: set[str] = set()
    tok_acc: dict[str, int] = {}
    attempt = 0
    while True:
        attempt += 1
        if attempt > 1:
            # The timed-out attempt's work was committed at the bottom of
            # this loop; the retry is a fresh session pointed at that state.
            # Per-attempt files keep every transcript and export.
            prompt_path = run_dir / "prompts" / \
                f"{step.name}.attempt{attempt}.md"
            stdout_log = run_dir / f"{step.name}.attempt{attempt}.stdout.log"
            export_path = run_dir / f"{step.name}.attempt{attempt}.export.json"
            retry_step = dataclasses.replace(
                step, prompt=RETRY_NOTE.format(
                    timeout=step.timeout_min, prev=attempt - 1,
                    total=step.max_attempts) + "\n" + (body_src or ""))
            prompt_path.write_text(
                render_prompt(retry_step, slug, work_dir, cfg.path.parent,
                              cfg.preamble), encoding="utf-8")
            log_event(run_dir, event="step_retry", step=step.name,
                      attempt=attempt)

        before = session_ids(devin_argv, work_dir) or set()
        cmd = devin_cmd(devin_argv, step, prompt_path, export_path)
        LOG.info("step %s attempt %d/%d: %s (cwd=%s)", step.name, attempt,
                 step.max_attempts, " ".join(cmd), work_dir)
        timed_out = hung = False
        rc = None
        spawn_failed = False
        with open(stdout_log, "wb") as out:
            try:
                proc = subprocess.Popen(cmd, cwd=str(work_dir), stdout=out,
                                        stderr=subprocess.STDOUT,
                                        stdin=subprocess.DEVNULL,
                                        env=clean_devin_env(),
                                        **hidden_subprocess_kwargs())
                # Writing state.json into run_dir mid-spawn lands inside the
                # worker's watched workspace on self-hosted runs, and the
                # CLI's user-edit awareness ends the fresh session mid-init
                # (trap 5) - there the pid stays memory-only until the final
                # save_state below. Anywhere else the write is invisible to
                # the worker, so persist now: a live pid is what lets the
                # dashboard and sweep tell 'running' from 'orphaned'.
                with STATE_LOCK:
                    rec["pid"] = proc.pid
                    rec["attempts"] = attempt
                    try:
                        watched = run_dir.resolve().is_relative_to(
                            Path(work_dir).resolve())
                    except OSError:
                        watched = True
                    if not watched:
                        save_state(run_dir, state)
                threading.Thread(
                    target=_capture_session_id,
                    args=(rec, devin_argv, work_dir, before, state, run_dir,
                          watched, proc),
                    daemon=True).start()
                try:
                    rc, timed_out, hung, stopped, ext_s = wait_worker(
                        proc, stdout_log, step.timeout_min * 60,
                        stop_flags=(stop_flag(run_dir),
                                    stop_flag(run_dir, step.name)),
                        timeout_flag=timeout_flag(run_dir, step.name),
                        stall_s=(step.stall_min * 60
                                 if step.stall_min > 0 else None))
                except subprocess.TimeoutExpired:      # unreachable; safety
                    kill_tree(proc)
                    proc.wait()
                    timed_out, ext_s = True, 0.0
                if stopped:
                    # consume the per-step sentinel; a run-level stop flag
                    # stays so later waves see it too
                    stop_flag(run_dir, step.name).unlink(
                        missing_ok=True)
                    break
            except OSError as exc:
                LOG.error("step %s spawn failed: %s", step.name, exc)
                log_event(run_dir, event="spawn_error", step=step.name,
                          error=str(exc))
                spawn_failed = True

        new_sessions = (session_ids(devin_argv, work_dir) or set()) - before
        all_sessions |= new_sessions
        status_line = tail_status(stdout_log)
        _merge_tokens(tok_acc, tail_metrics(export_path))
        # Commit each attempt's product BEFORE deciding to retry - the next
        # session resumes from committed state, not the working tree.
        if post_commit and cfg.checkpoint and is_git_repo(work_dir) \
                and git_dirty(work_dir):
            git_commit(work_dir, f"conductor: work from {step.name}")
        if spawn_failed or not timed_out or attempt >= step.max_attempts:
            break
    elapsed = round(time.time() - t0, 1)

    verify_ok = None
    if step.verify and not timed_out and not stopped:
        try:
            v = subprocess.run(step.verify, cwd=str(work_dir), shell=True,
                               capture_output=True, text=True,
                               encoding="utf-8", errors="replace",
                               timeout=600, **hidden_subprocess_kwargs())
            verify_ok = v.returncode == 0
            (run_dir / f"{step.name}.verify.log").write_text(
                (v.stdout or "") + (v.stderr or ""), encoding="utf-8",
                errors="replace")
        except subprocess.TimeoutExpired:
            # A hung gate must not crash the conductor - it is a failed
            # verify like any other red run.
            verify_ok = False
            (run_dir / f"{step.name}.verify.log").write_text(
                "verify timed out after 600s\n", encoding="utf-8")

    if stopped:
        status = "stopped"
    elif timed_out:
        status = "timed_out"
    elif (rc is None or rc != 0) and not hung:
        status = "failed"
    elif status_line in ("blocked", "partial"):
        status = status_line
    elif status_line is None:
        # rc 0 without STATUS is a no-op: the CLI can exit cleanly before the
        # ACP turn even starts (seen 2026-10-13). Never report that as done.
        status = "failed"
    elif verify_ok is False and step.verify_must_pass:
        status = "failed"
    else:
        status = "done"

    with STATE_LOCK:
        rec["session_id"] = (sorted(new_sessions)[-1] if new_sessions
                             else tail_session_ref(stdout_log))
        if all_sessions:
            rec["session_ids"] = sorted(all_sessions)
        rec["exit_code"] = rc
        rec["elapsed_s"] = elapsed
        rec["attempts"] = attempt
        notes = []
        if attempt > 1:
            notes.append(f"retried after timeout: {attempt} attempts total, "
                         "each attempt's work committed before the next")
        if timed_out and ext_s > 1:
            notes.append(f"log kept growing past the "
                         f"{step.timeout_min:g}min deadline so the step ran "
                         f"+{ext_s / 60:g}min extra before a "
                         f"{step.stall_min:g}min silence or the "
                         f"{1 + TIMEOUT_MAX_EXT}x cap ended it")
        if status_line is None and rc == 0:
            if tok_acc:
                notes.append("worker exited 0 without a STATUS: line - "
                             "turn ended before the report but tokens "
                             "show real work ran; check commits before "
                             "re-running")
            else:
                notes.append("worker exited 0 without a STATUS line "
                             "(no-op run - nothing was done)")
        if hung:
            notes.append("STATUS printed but the CLI stayed alive on a "
                         "spawned child (trap 12); tree-killed after "
                         "grace - counted as turn end")
        if stopped:
            notes.append("stopped by user via dashboard; the run "
                         "proceeds without it, --resume re-runs the step")
        if prompt_edited:
            notes.append("prompt body was overridden via the dashboard "
                         "edit control before dispatch")
        if rnotes and rnotes.strip():
            notes.append("dashboard resume notes were appended to the "
                         "prompt")
        if notes:
            rec["note"] = "; ".join(notes)
        rec.update(status=status, ended_at=now_iso(), status_line=status_line,
                   verify_ok=verify_ok, next=tail_next(stdout_log),
                   timeout_extended_s=round(ext_s) if ext_s > 1 else None,
                   tokens=tok_acc or None,
                   model_actual=tail_agent_model(export_path),
                   prompt_edited=prompt_edited or None,
                   prompt=str(prompt_path), stdout_log=str(stdout_log),
                   export=str(export_path))
        save_state(run_dir, state)
    log_event(run_dir, event="step_end", step=step.name, status=status,
              exit_code=rc, elapsed_s=elapsed, session=rec.get("session_id"),
              attempts=attempt)
    return status


def resolve_merge_conflict(step: Step, slug: str, repo_dir: Path,
                           work_dir: Path, branch: str, cfg: Manifest,
                           run_dir: Path, devin_argv: list[str],
                           state: dict) -> bool:
    """A worktree branch conflicted at merge-back: re-enter the merge in the
    MAIN checkout and hand the resolution to a fresh session (manifest
    `merge_resolver`). Returns True when the merge was completed.

    Safety shape: the resolver may stage a fix without committing - the
    conductor finishes `git commit --no-edit` itself once the index is
    conflict-free, marker-free, and the branch tip is an ancestor of HEAD
    (a resolver that quietly aborted the merge can never pass). Anything
    else -> merge --abort + the worktree/branch stay for a human.

    Refuses entirely on a dirty main checkout: another agent's uncommitted
    work sitting in the tree would get swept into the merge commit
    (trap 10 shape) - that is human territory.
    """
    if git_dirty_tracked(repo_dir):
        LOG.warning("%s: main checkout has modified tracked files; not "
                    "spawning a merge resolver", step.name)
        rec = state["steps"].get(step.name, {})
        _append_note(rec, "merge resolver refused: modified tracked files "
                          "in main checkout (trap 10/11 - commit or stash "
                          "them, then resume)")
        save_state(run_dir, state)
        return False
    r = _git(repo_dir, "merge", "--no-edit", branch)
    if r.returncode == 0:                    # raced clean after all
        worktree_remove(repo_dir, work_dir, branch)
        return True
    conflicted = unmerged_paths(repo_dir)
    body = RESOLVE_BODY.format(
        branch=branch, worktree=work_dir,
        files="\n".join(f"- {f}" for f in conflicted) or "- (see git status)")
    rstep = Step(name=f"resolve-{step.name}", project=slug, prompt=body,
                 model=step.model, mode=step.mode,
                 timeout_min=step.timeout_min)
    # Keep the step's recorded status (done/failed): what is pending is the
    # MERGE, tracked via merged=False - the resume path keys off exactly
    # that, so a conductor dying mid-resolution resumes through late-merge.
    rec = state["steps"].get(step.name, {})
    rec.update(note=f"merge conflict on {branch}; resolver session "
                    f"{rstep.name} spawned in the main checkout",
               merged=False)   # orange 'unmerged' chip while resolving
    save_state(run_dir, state)
    log_event(run_dir, event="resolve_start", step=step.name, branch=branch,
              files=conflicted)
    status = run_step(rstep, slug, repo_dir, cfg, run_dir, devin_argv,
                      state, do_checkpoint=False, post_commit=False)
    log_event(run_dir, event="resolve_end", step=step.name, status=status)

    # Adopt any resolved-but-unstaged file that carries no markers, then
    # decide. A file still holding markers is never staged.
    for f in conflicted:
        if not has_conflict_markers(repo_dir, f):
            _git(repo_dir, "add", f)
    if status == "done" and not unmerged_paths(repo_dir) \
            and not any(has_conflict_markers(repo_dir, f)
                        for f in conflicted) \
            and merge_in_progress(repo_dir):
        # Index clean, merge still open -> conductor commits the resolution.
        _git(repo_dir, "commit", "--no-edit")
    tip = _git(repo_dir, "rev-parse", branch).stdout.strip()
    landed = bool(tip) and _git(repo_dir, "merge-base", "--is-ancestor",
                                tip, "HEAD").returncode == 0
    if status == "done" and landed and not unmerged_paths(repo_dir):
        worktree_remove(repo_dir, work_dir, branch)
        return True
    if merge_in_progress(repo_dir):
        _git(repo_dir, "merge", "--abort")
    return False


def new_run_dir(cfg: Manifest, runs_dir: Path) -> Path:
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    d = runs_dir / f"{stamp}-{cfg.name}"
    d.mkdir(parents=True, exist_ok=False)
    (d / "prompts").mkdir(exist_ok=True)
    return d


def find_resume_dir(cfg: Manifest, runs_dir: Path, arg: str | None) -> Path:
    if arg and arg != "latest":
        d = Path(arg)
        if not d.is_absolute():
            d = (Path.cwd() / d).resolve()
        if (d / "state.json").is_file():
            return d
        raise SystemExit(f"--resume: no state.json in {d}")
    cands = sorted(runs_dir.glob(f"*-{cfg.name}"), reverse=True)
    for d in cands:
        if (d / "state.json").is_file():
            return d
    raise SystemExit(f"--resume: no prior run of {cfg.name!r} under {runs_dir}")


def _task_py(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(TASK_PY), *args],
                          capture_output=True, text=True, timeout=60,
                          encoding="utf-8", errors="replace",
                          **hidden_subprocess_kwargs())


def hub_start(cfg: Manifest, run_dir: Path) -> int | None:
    """Create the run's hub.db row; returns its id or None. The row is
    live-updated per step so the phone shows overnight progress."""
    if not TASK_PY.is_file():
        return None
    try:
        out = _task_py("add", "--project", "agent-conductor", "--kind", "task",
                       "--title", f"conductor run: {cfg.name}",
                       "--body", f"run dir: {run_dir}\nstarted {now_iso()}",
                       "--by", "conductor")
        m = re.search(r"#(\d+)", out.stdout or "")
        if not m:
            return None
        tid = int(m.group(1))
        _task_py("update", str(tid), "--status", "in_progress")
        return tid
    except Exception as exc:
        LOG.warning("hub.db row create failed: %s", exc)
        return None


def hub_update(tid: int | None, state: dict, final: bool = False,
               final_status: str = "done") -> None:
    if tid is None or not TASK_PY.is_file():
        return
    counts: dict[str, int] = {}
    for rec in state["steps"].values():
        counts[rec["status"]] = counts.get(rec["status"], 0) + 1
    body = ("run dir: " + state.get("run_dir", "?") + "\n"
            + "ended: " + (state.get("ended_at") or "still running") + "\n"
            + "\n".join(f"- {r.get('name', '?')}: {r.get('status', '?')}"
                        for r in state["steps"].values()))
    try:
        args_ = ["update", str(tid), "--body", body,
                 "--title", f"conductor run {state['run']}: {dict(counts)}"]
        if final:
            args_ += ["--status", final_status]
        _task_py(*args_)
    except Exception as exc:
        LOG.warning("hub.db update failed: %s", exc)


def cmd_run(args: argparse.Namespace) -> int:
    manifest_path = Path(args.manifest).resolve()
    cfg = load_manifest(manifest_path)
    projects = load_projects()
    devin_argv = shlex.split(args.devin or os.environ.get("CONDUCTOR_DEVIN")
                             or "devin")
    runs_dir = Path(args.runs_dir).resolve() if args.runs_dir else RUNS_DIR

    resolved = [(*resolve_project(s, projects, manifest_path.parent), s)
                for s in cfg.steps]
    # Validate before any run state exists: a bad wave must not leave a
    # state.json the dashboard will show as "running" forever.
    groups = group_waves(resolved)
    validate_groups(groups)
    cap = effective_max_parallel(cfg, getattr(args, "max_parallel", None))

    if getattr(args, "keep_awake", True):
        keep_awake()

    if args.resume is not None:
        run_dir = find_resume_dir(cfg, runs_dir, args.resume or "latest")
        state = json.loads((run_dir / "state.json").read_text(encoding="utf-8"))
        # The driver is now THIS code version: it polls the path-keyed
        # sentinels only, so the dashboard can stop writing legacy names.
        state["flags"] = "v2"
        # A resumed run is in flight again: the stale 'aborted'/'ended_at'
        # from the pause/crash would keep showing the old end-state notice
        # (e.g. 'Paused by user') until the loop re-stamps them.
        state.pop("aborted", None)
        state.pop("ended_at", None)
        # A re-entered run is in flight again - drop the auto-archive
        # stamp so it resurfaces on the default dashboard view.
        state.pop("archived", None)
        # The driver is THIS process now - restamp so driver_alive()
        # tracks the resumed conductor, not the dead original.
        state["driver_pid"] = os.getpid()
        state["driver_started_at"] = now_iso()
        save_state(run_dir, state)
        print(f"[resume] {run_dir.name}")
        # Resume must be able to start work again: drop any stop/drain
        # sentinels left by a dashboard Stop click.
        clear_run_flags(run_dir)
    else:
        run_dir = new_run_dir(cfg, runs_dir)
        state = {"run": cfg.name, "description": cfg.description,
                 "request_id": getattr(args, "request_id", None),
                 "manifest": str(manifest_path),
                 "started_at": now_iso(), "devin": " ".join(devin_argv),
                 "devin_argv": devin_argv,
                 "run_dir": str(run_dir), "max_parallel": cap,
                 "flags": "v2",
                 "driver_pid": os.getpid(),
                 "driver_started_at": now_iso(),
                 "free_ram_gb": free_ram_gb(), "steps": {}}
        save_state(run_dir, state)
    hub = getattr(args, "hub", True)
    tid = hub_start(cfg, run_dir) if hub else None
    log_event(run_dir, event="run_start", manifest=str(manifest_path),
              hub_task=tid)

    if "devin" in os.path.basename(devin_argv[-1]).lower():
        if not check_auth(devin_argv):
            print("devin is not logged in. Run `devin auth login` first.")
            state["ended_at"] = now_iso()
            state["aborted"] = "auth"
            save_state(run_dir, state)
            log_event(run_dir, event="run_abort", reason="auth")
            hub_update(tid, state, final=True, final_status="dropped")
            return 2

    run_t0 = time.time()
    stop = False

    def halt_remaining(from_gi: int) -> None:
        """A user pause/stop/drain ended the run mid-loop: every step that
        never launched is recorded 'skipped' (not just absent) so the
        dashboard shows the full plan and --resume knows what is left."""
        halted = stop_flag(run_dir).is_file()
        paused = pause_flag(run_dir).is_file()
        with STATE_LOCK:
            for grp in groups[from_gi:]:
                for _d, _s, s_ in grp:
                    r = state["steps"].get(s_.name) or {}
                    if r.get("status") == "done":
                        continue
                    if skip_flag(run_dir, s_.name).is_file():
                        why = "skipped by user (Skip step)"
                    elif paused:
                        why = "run paused"
                    elif halted:
                        why = "run stopped by user"
                    else:
                        why = "run drained"
                    state["steps"].setdefault(s_.name, {}).update(
                        name=s_.name, status="skipped",
                        note=why + " before this step ran")
            state["aborted"] = ("paused" if paused else
                                "stopped" if halted else "drained")
            state["ended_at"] = now_iso()
            save_state(run_dir, state)

    for gi, members in enumerate(groups):
        if _flag_set(stop_flag(run_dir), drain_flag(run_dir),
                     pause_flag(run_dir)) is not None:
            # Dashboard run controls: 'stop' also tree-kills live steps
            # via wait_worker's flag poll; 'drain'/'pause' let them
            # finish. Either way nothing NEW launches - remaining work
            # stays resumable.
            halted = stop_flag(run_dir).is_file()
            paused = pause_flag(run_dir).is_file()
            kind = "paused" if paused else "stopped" if halted else "drained"
            print(f"run {kind} by user - no further steps launch "
                  f"(resume with --resume {run_dir.name})")
            log_event(run_dir, event="run_stop", drain=not halted,
                      paused=paused)
            halt_remaining(gi)
            break
        if cfg.max_hours and time.time() - run_t0 > cfg.max_hours * 3600:
            print(f"stopping run: max_hours={cfg.max_hours} reached")
            log_event(run_dir, event="run_deadline", max_hours=cfg.max_hours)
            break
        if "devin" in os.path.basename(devin_argv[-1]).lower() \
                and not check_auth(devin_argv):
            # Token died mid-run: mark every remaining step blocked and
            # stop - spawning more workers would just burn failures.
            # --resume re-runs blocked steps after a fresh login.
            print("devin auth lost mid-run; remaining steps -> blocked "
                  "(fix auth, then --resume)")
            log_event(run_dir, event="run_abort", reason="auth_midrun")
            with STATE_LOCK:
                for grp in groups[gi:]:
                    for _d, _s, s_ in grp:
                        if state["steps"].get(s_.name, {}).get(
                                "status") != "done":
                            state["steps"].setdefault(s_.name, {}).update(
                                name=s_.name, status="blocked",
                                note="auth lost mid-run; --resume after "
                                     "devin auth login")
                state["aborted"] = "auth_midrun"
                state["ended_at"] = now_iso()
                save_state(run_dir, state)
            hub_update(tid, state, final=True, final_status="dropped")
            return 2
        # Materialize worktrees for members that want isolation.
        prepared: list[tuple[Step, str, Path, Path, str | None]] = []
        wave_ckpt_done = False
        for project_dir, slug, step in members:
            prior = state["steps"].get(step.name, {})
            unmerged = bool(prior.get("branch")) \
                and prior.get("merged") is not True
            if args.resume is not None and unmerged:
                # Finalize the merge before anything else: the step's product
                # is a branch that never landed. Covers 'done' (conductor
                # died pre-merge), 'failed' (earlier conflict), and
                # blocked/partial strays. Re-running on the same stale
                # branch cannot fix a conflict - it shares the merge base -
                # so a still-failing merge is left parked, not re-run.
                wt = Path(prior["work_dir"]) if prior.get(
                    "work_dir") else runs_dir.parent / "worktrees" / (
                    run_dir.name) / step.name
                if _git(project_dir, "rev-parse", "-q", "--verify",
                        prior["branch"]).returncode == 0:
                    abort_own_merge(project_dir)
                    ok = worktree_merge(project_dir, wt, prior["branch"])
                    if not ok and step.merge_resolver:
                        ok = resolve_merge_conflict(
                            step, slug, project_dir, wt, prior["branch"],
                            cfg, run_dir, devin_argv, state)
                    if ok:
                        was = prior.get('status')
                        prior.update(status="done", merged=True)
                        _append_note(prior, f"merged on resume (was {was})")
                        save_state(run_dir, state)
                        print(f"[merged] {step.name} (merge on resume)")
                    elif prior.get("status") == "done":
                        prior.update(status="failed", merged=False)
                        _append_note(prior, "merge conflict on resume; "
                                            f"worktree kept at {wt}")
                        save_state(run_dir, state)
                        print(f"[ failed] {step.name} (merge conflict "
                              "on resume)")
                        if cfg.on_failure == "stop":
                            stop = True
                            break
                    else:
                        prior["merged"] = False
                        _append_note(prior, "merge still conflicts; "
                                            "resolve the kept worktree by "
                                            "hand or enable merge_resolver "
                                            "- re-running the same branch "
                                            "cannot fix a conflict")
                        save_state(run_dir, state)
                        print(f"[conflict] {step.name} still unmerged")
                    continue
            if args.resume is not None and prior.get("status") == "done":
                print(f"[skip ] {step.name} (done)")
                continue
            # Re-running an interrupted step (killed, orphaned, crashed):
            # tell the fresh session it isn't starting clean.
            rnote = (INTERRUPTED_NOTE.format(how=prior["status"])
                     if args.resume is not None and prior.get("status") in
                     ("running", "lost", "timed_out", "stopped", "failed")
                     else None)
            if step.worktree:
                if cfg.checkpoint and not wave_ckpt_done \
                        and is_git_repo(project_dir):
                    tag = step.wave if step.wave is not None else step.name
                    git_commit(project_dir,
                               f"conductor: checkpoint before wave {tag}",
                               allow_empty=True)
                    wave_ckpt_done = True
                # A conductor merge left in progress (conductor died while a
                # resolver session ran) blocks every new merge in this repo.
                # Back it out - but only our own conductor/* merges, never a
                # human's merge in progress.
                if abort_own_merge(project_dir):
                    log_event(run_dir, event="merge_abort_stale",
                              step=step.name, project=str(project_dir))
                    print(f"[stale-merge aborted] {slug}")
                # Branch carries the RUN DIR name, not the manifest name:
                # re-running the same manifest can never collide with a
                # stale branch still checked out in an old run's worktree.
                branch = sanitize_branch(
                    f"conductor/{run_dir.name}/{step.name}")
                wt = runs_dir.parent / "worktrees" / run_dir.name / step.name
                if not worktree_add(project_dir, wt, branch):
                    with STATE_LOCK:
                        state["steps"].setdefault(step.name, {}).update(
                            name=step.name, slug=slug,
                            status="failed", note="worktree add failed")
                        save_state(run_dir, state)
                    print(f"[  failed] {step.name} (worktree add)")
                    if cfg.on_failure == "stop":
                        stop = True
                        break
                    continue
                prepared.append((step, slug, wt, project_dir, branch, rnote))
            else:
                prepared.append((step, slug, project_dir, project_dir,
                                 None, rnote))
        if stop:
            break
        if not prepared:
            continue   # every member of this group was already done (resume)

        # Mark members queued up front: the pool may hold them while a
        # max_parallel-sized set runs, and the dashboard shows the queue.
        with STATE_LOCK:
            for step, _s, _w, _r, _b, _n in prepared:
                state["steps"].setdefault(step.name, {}).update(
                    status="queued", name=step.name)
            save_state(run_dir, state)

        if len(prepared) == 1:
            step, slug, work_dir, _repo, branch, rnote = prepared[0]
            statuses = {step.name: run_step(step, slug, work_dir, cfg,
                                            run_dir, devin_argv, state,
                                            do_checkpoint=(branch is None),
                                            branch=branch,
                                            resume_note=rnote)}
            hub_update(tid, state)
        else:
            statuses = {}
            with concurrent.futures.ThreadPoolExecutor(
                    max_workers=min(len(prepared), cap)) as ex:
                futs = {ex.submit(run_step, step, slug, work_dir, cfg,
                                  run_dir, devin_argv, state,
                                  do_checkpoint=(branch is None),
                                  branch=branch,
                                  resume_note=rnote): step.name
                        for step, slug, work_dir, _r, branch, rnote
                        in prepared}
                for f in concurrent.futures.as_completed(futs):
                    statuses[futs[f]] = f.result()
                    hub_update(tid, state)

        # Merge worktree branches back into the main checkout, in order.
        for step, slug, work_dir, repo_dir, branch, _n in prepared:
            status = statuses.get(step.name)
            print(f"[{status or 'skipped':>8}] {step.name}")
            if branch:
                rec = state["steps"].get(step.name, {})
                if status in ("done", "partial", "blocked"):
                    if worktree_merge(repo_dir, work_dir, branch):
                        rec["merged"] = True
                    elif step.merge_resolver and resolve_merge_conflict(
                            step, slug, repo_dir, work_dir, branch, cfg,
                            run_dir, devin_argv, state):
                        rec["merged"] = True
                        _append_note(rec, "merge conflict resolved by "
                                          f"resolve-{step.name}")
                    else:
                        rec.update(status="failed", merged=False)
                        _append_note(rec, "merge conflict; worktree kept "
                                          f"at {work_dir}")
                        status = "failed"
                        statuses[step.name] = "failed"
                    save_state(run_dir, state)
                elif worktree_debris(repo_dir, work_dir, branch):
                    # step died before doing work and the branch carries
                    # nothing - remove the worktree+branch instead of
                    # leaving orphan debris in the target repo.
                    worktree_remove(repo_dir, work_dir, branch)
                    rec = state["steps"].get(step.name, {})
                    _append_note(rec, "worktree removed: step failed "
                                      "with no work product")
                    save_state(run_dir, state)
                else:
                    rec = state["steps"].get(step.name, {})
                    _append_note(rec, f"worktree kept at {work_dir} - "
                                      "branch has unmerged work")
                    save_state(run_dir, state)
        # Once every member merged or was removed, the per-run worktree
        # dir is an empty husk - drop it (kept worktrees keep it alive).
        try:
            (runs_dir.parent / "worktrees" / run_dir.name).rmdir()
        except OSError:
            pass
        if cfg.on_failure == "stop" and any(
                s in ("failed", "timed_out", "blocked")
                for s in statuses.values()):
            print("stopping run after wave")
            # 'stopped' is deliberately absent: a per-step 'Stop this
            # step' is the user vetoing ONE task, not a failure - the run
            # proceeds to the next queued step. Run-level stops still halt
            # regardless of on_failure because the stop/pause/drain
            # sentinel itself persists to the next top-of-loop check.
            if _flag_set(stop_flag(run_dir), drain_flag(run_dir),
                         pause_flag(run_dir)) is not None:
                halt_remaining(gi + 1)
            break

    # A halt sentinel landing in the FINAL wave never re-enters the
    # loop's flag check - stamp the same bookkeeping here so the run
    # still records aborted + skipped leftovers.
    if state.get("aborted") is None and _flag_set(
            stop_flag(run_dir), drain_flag(run_dir),
            pause_flag(run_dir)) is not None:
        halt_remaining(len(groups))

    state["ended_at"] = now_iso()
    save_state(run_dir, state)
    log_event(run_dir, event="run_end")
    hub_update(tid, state, final=True)
    counts: dict[str, int] = {}
    for rec in state["steps"].values():
        counts[rec["status"]] = counts.get(rec["status"], 0) + 1
    print(f"run {cfg.name}: {dict(counts)}  ({run_dir})")
    return 0 if set(counts) <= {"done", "partial"} else 1


# ---------------------------------------------------------------- plan/status

def cmd_plan(args: argparse.Namespace) -> int:
    manifest_path = Path(args.manifest).resolve()
    cfg = load_manifest(manifest_path)
    projects = load_projects()
    resolved = [(*resolve_project(s, projects, manifest_path.parent), s)
                for s in cfg.steps]
    validate_groups(group_waves(resolved))
    print(f"run {cfg.name!r}: {len(cfg.steps)} steps, on_failure={cfg.on_failure}, "
          f"checkpoint={cfg.checkpoint}, "
          f"max_parallel={effective_max_parallel(cfg)}")
    for pdir, slug, s in resolved:
        cloud = " [cloud]" if s.cloud else ""
        if s.cloud:
            dirty = "unknown"
            if is_git_repo(pdir):
                ahead = _git(pdir, "rev-list", "--count", "@{u}..HEAD").stdout.strip()
                dirty = f"unpushed_commits={ahead or '?'}"
            print(f"    note: cloud workers see origin, not local state ({dirty})")
        tags = cloud + (" [worktree]" if s.worktree else "") \
            + (f" [wave {s.wave}]" if s.wave is not None else "") \
            + (" [resolver]" if s.merge_resolver else "")
        tries = f" x{s.max_attempts}" if s.max_attempts > 1 else ""
        print(f"  {s.name:<20} {slug:<22} model={s.model or '-':<10} "
              f"mode={s.mode:<8} timeout={s.timeout_min:g}m{tries}{tags}")
        print(f"    verify: {s.verify or '-'}")
        print(f"    $ {' '.join(devin_cmd(shlex.split(args.devin or 'devin'), s, Path('<pf>'), Path('<exp>')))}")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    run_dir = Path(args.run_dir) if args.run_dir else None
    if run_dir is None:
        cands = sorted(RUNS_DIR.glob("*/state.json"), reverse=True)
        if not cands:
            print("no runs yet")
            return 0
        run_dir = cands[0].parent
    state = json.loads((run_dir / "state.json").read_text(encoding="utf-8"))
    eff = effective_statuses(state)
    ended = state.get("ended_at") or "still running"
    print(f"run {state.get('run', '?')}  started "
          f"{state.get('started_at') or '?'}  ended {ended}  ({run_dir})")
    for name, rec in (state.get("steps") or {}).items():
        sess = rec.get("session_id") or "-"
        span = rec.get("elapsed_s")
        if span is None and rec.get("started_at"):
            t0 = _iso_ts(rec["started_at"])
            span = round(time.time() - t0) if t0 else 0
        print(f"  {eff.get(name, '?'):>9}  {name:<20} {span or 0:>7.0f}s  "
              f"ended={rec.get('ended_at') or '-'}  "
              f"session={sess}  verify={rec.get('verify_ok')}")
        if rec.get("note"):
            print(f"             note: {rec['note']}")
        if sess not in (None, "-") and not str(sess).startswith("mock"):
            how = "devin --cloud -r" if "/" in str(sess) else "devin -r"
            print(f"             resume: cd {rec.get('project')} && {how} {sess}")
    return 0


# ---------------------------------------------------------------- hygiene

def _runs_watched(runs_dir: Path, states: list[tuple[Path, dict]]) -> bool:
    """Trap-5 write guard: True when a live step's work_dir contains
    runs_dir - a self-hosted run is in flight and ANY write under runs_dir
    lands in the worker's watched tree (an external edit ends its session)."""
    for _sj, st in states:
        for rec in (st.get("steps") or {}).values():
            if step_live(rec, driver_alive(st)):
                wd = rec.get("work_dir") or rec.get("project")
                try:
                    if wd and runs_dir.resolve().is_relative_to(
                            Path(wd).resolve()):
                        return True
                except OSError:
                    pass
    return False


def _load_run_states(runs_dir: Path) -> list[tuple[Path, dict]]:
    states: list[tuple[Path, dict]] = []
    for sj in sorted(runs_dir.glob("*/state.json")):
        try:
            states.append((sj, json.loads(sj.read_text(encoding="utf-8"))))
        except (OSError, ValueError):
            continue
    return states


def sweep_runs(runs_dir: Path, write: bool = True) -> list[str]:
    """Re-mark orphaned 'running'/'queued' records as 'lost', close runs
    with nothing alive, and archive finished runs older than
    ARCHIVE_AFTER_S. Returns human-readable lines about what changed.

    Write guard (trap 5): if a live step's work_dir contains runs_dir, a
    self-hosted run is in flight and ANY write under runs_dir lands in the
    worker's watched tree - so this degrades to a dry-run report.
    """
    states = _load_run_states(runs_dir)
    watched = _runs_watched(runs_dir, states)
    lines: list[str] = []
    for sj, st in states:
        if driver_alive(st):
            # The run's conductor is alive (possibly mid-verify/merge with
            # a dead worker pid). Orphan-marking is for dead drivers.
            continue
        eff = effective_statuses(st)
        steps = st.get("steps") or {}
        dirty = False
        for name, rec in steps.items():
            if eff.get(name) == "lost":
                lines.append(f"{sj.parent.name}/{name}: "
                             f"{rec.get('status')} -> lost")
                if write:
                    rec["status"] = "lost"
                    if not rec.get("ended_at"):
                        rec["ended_at"] = now_iso()
                    rec.setdefault(
                        "note", "marked lost by sweep: nothing alive behind "
                                "the record (conductor or worker died?)")
                    dirty = True
        if steps and not st.get("ended_at") and not any(
                r.get("status") in ("running", "queued")
                for r in steps.values()):
            lines.append(f"{sj.parent.name}: run closed (no live steps)")
            if write:
                st["ended_at"] = now_iso()
                dirty = True
        if dirty:
            if watched:
                lines.insert(0, f"DRY-RUN: a live worker watches {runs_dir} "
                                f"(self-hosted run) - not writing (trap 5)")
                break
            save_state(sj.parent, st)
    lines += archive_runs(runs_dir, write=write)
    return lines


def archive_runs(runs_dir: Path, write: bool = True,
                 now: float | None = None) -> list[str]:
    """Stamp state['archived'] on runs whose ended_at is older than
    ARCHIVE_AFTER_S - the answer to run-dir clutter: old temp runs stay on
    disk but drop out of the default dashboard view. A run only qualifies
    once it HAS an ended_at (the close pass above supplies it for
    interrupted runs), so a live or just-ended run is never touched. The
    stamp is a view filter, not a move - resume/merge keep working, and
    resuming a run clears it. Same trap-5 write guard as the sweep."""
    if ARCHIVE_AFTER_S <= 0:
        return []
    now = time.time() if now is None else now
    states = _load_run_states(runs_dir)
    watched = _runs_watched(runs_dir, states)
    lines: list[str] = []
    for sj, st in states:
        if st.get("archived"):
            continue
        ended = _iso_ts(st.get("ended_at"))
        if ended is None or now - ended < ARCHIVE_AFTER_S:
            continue
        lines.append(f"{sj.parent.name}: archived (ended {st['ended_at']}, "
                     f"over {ARCHIVE_AFTER_S / 3600:g}h old)")
        if write and not watched:
            st["archived"] = now_iso()
            save_state(sj.parent, st)
    if lines and write and watched:
        lines.insert(0, f"DRY-RUN: a live worker watches {runs_dir} "
                        f"(self-hosted run) - not writing (trap 5)")
    return lines


_ARCHIVE_TRIED = 0.0
_ARCHIVE_POLL_S = 1800.0    # /api/state is polled ~2s; scan twice an hour


def _auto_archive(runs_dir: Path) -> None:
    """serve's startup sweep archives once at boot; a dashboard left up for
    days would never hide runs that finished after boot without this
    throttled pass (failures just keep the current view)."""
    global _ARCHIVE_TRIED
    if time.time() - _ARCHIVE_TRIED < _ARCHIVE_POLL_S:
        return
    _ARCHIVE_TRIED = time.time()
    try:
        archive_runs(runs_dir)
    except Exception:
        LOG.exception("auto-archive sweep failed")


def cmd_sweep(args: argparse.Namespace) -> int:
    runs_dir = Path(args.runs_dir).resolve() if args.runs_dir else RUNS_DIR
    lines = sweep_runs(runs_dir)
    devin_argv = shlex.split(getattr(args, "devin", None)
                             or os.environ.get("CONDUCTOR_DEVIN") or "devin")
    lines += sweep_sessions(runs_dir, devin_argv,
                            getattr(args, "keep_hours", 48.0))
    print("\n".join(lines) if lines else "nothing stale")
    return 0


def devin_rm(devin_argv: list[str], cwd: Path, sid: str) -> int:
    try:
        if not cwd.is_dir():
            cwd = Path.cwd()   # recorded worktree may be gone post-merge;
            # rm only needs the sid, cwd just has to exist
        return subprocess.run(devin_argv + ["rm", sid, "--force"],
                              cwd=str(cwd), capture_output=True, text=True,
                              encoding="utf-8", errors="replace",
                              timeout=60, env=clean_devin_env(),
                              **hidden_subprocess_kwargs()).returncode
    except Exception:
        return 1


def _worktree_checkout_paths() -> list[Path]:
    """Registered git worktrees of THIS repo (ROOT), excluding ROOT itself
    - dev checkouts like agent-conductor-wt. Each keeps its own data/runs
    with its own spawned-session whitelist (trap 13)."""
    try:
        r = subprocess.run(["git", "worktree", "list", "--porcelain"],
                           cwd=str(ROOT), capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=30,
                           **hidden_subprocess_kwargs())
    except Exception:
        return []
    if r.returncode != 0:
        return []
    try:
        root = ROOT.resolve()
    except OSError:
        root = ROOT
    out = []
    for line in r.stdout.splitlines():
        if line.startswith("worktree "):
            p = Path(line[9:].strip())
            try:
                p = p.resolve()
            except OSError:
                pass
            if p != root:
                out.append(p)
    return out


def _session_runs_dirs(runs_dir: Path) -> list[Path]:
    """The runs dir being scanned, PLUS registered worktree checkouts'
    data/runs when the caller asked for the default RUNS_DIR - a dev
    worktree's spawned sessions are whitelisted only in ITS state.jsons,
    so skipping them leaves them manual-cleanup-only (trap 13)."""
    dirs = [runs_dir]
    try:
        if runs_dir.resolve() == RUNS_DIR.resolve():
            dirs += [wt / "data" / "runs" for wt in
                     _worktree_checkout_paths()
                     if (wt / "data" / "runs").is_dir()]
    except OSError:
        pass
    return dirs


def collect_sessions(runs_dir: Path, devin_argv: list[str]) -> list[dict]:
    """Conductor-spawned devin sessions still registered under their work_dir.

    Whitelist on ids recorded in run state.json - interactive sessions are
    never in that registry, so human sessions can never appear here. Each
    row carries age_s for --keep-hours decisions.
    """
    spawned: dict[str, dict] = {}
    state_files = sorted(s for d in _session_runs_dirs(runs_dir)
                         for s in d.glob("*/state.json"))
    for sj in state_files:
        try:
            st = json.loads(sj.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for name, rec in (st.get("steps") or {}).items():
            for sid in ([rec.get("session_id")]
                        + list(rec.get("session_ids") or [])):
                if not sid or "/" in str(sid):
                    continue        # cloud URL refs aren't local sessions
                spawned[str(sid)] = {
                    "run": st.get("run"), "step": name,
                    "status": rec.get("status"),
                    "ended": rec.get("ended_at") or st.get("ended_at"),
                    "run_ended": bool(st.get("ended_at")),
                    "work_dir": rec.get("work_dir") or rec.get("project")}
    by_dir: dict[str, list[str]] = {}
    gone: list[str] = []
    for sid, info in spawned.items():
        if info["work_dir"] and Path(info["work_dir"]).is_dir():
            by_dir.setdefault(info["work_dir"], []).append(sid)
        else:
            gone.append(sid)   # worktree removed post-merge: can't
            # enumerate there, but the recorded id is whitelist enough -
            # `devin rm <id>` does not need the dir.
    out: list[dict] = []
    for wd, sids in by_dir.items():
        live_ids = session_ids(devin_argv, Path(wd)) or set()
        for sid in sids:
            if sid in live_ids:
                info = dict(spawned[sid], sid=sid)
                t = _iso_ts(info["ended"])
                info["age_s"] = time.time() - t if t else None
                out.append(info)
    for sid in gone:
        info = dict(spawned[sid], sid=sid, dir_gone=True)
        t = _iso_ts(info["ended"])
        info["age_s"] = time.time() - t if t else None
        out.append(info)
    return out


def _session_eligible(info: dict, keep_hours: float) -> bool:
    return (info["run_ended"] and info["age_s"] is not None
            and info["age_s"] > keep_hours * 3600)


def sweep_sessions(runs_dir: Path, devin_argv: list[str],
                   keep_hours: float = 48.0) -> list[str]:
    """Automated half of `sessions --rm`: delete spawned sessions whose run
    ended >keep_hours ago. Same whitelist as `sessions` - human sessions are
    structurally unreachable. Transcripts stay in the run dir."""
    lines: list[str] = []
    for info in collect_sessions(runs_dir, devin_argv):
        if not _session_eligible(info, keep_hours):
            continue
        rc = devin_rm(devin_argv, Path(info["work_dir"]), info["sid"])
        lines.append(f"rm {info['sid']} ({info['run']}/{info['step']}): "
                     f"{'ok' if rc == 0 else f'rc={rc}'}")
    return lines


def cmd_sessions(args: argparse.Namespace) -> int:
    """List/remove conductor-spawned devin sessions (whitelist via
    collect_sessions). --rm deletes sessions whose run ended more than
    --keep-hours ago; the transcript survives in the run dir."""
    runs_dir = Path(args.runs_dir).resolve() if args.runs_dir else RUNS_DIR
    devin_argv = shlex.split(args.devin or os.environ.get("CONDUCTOR_DEVIN")
                             or "devin")
    present = collect_sessions(runs_dir, devin_argv)
    if not present:
        print("no conductor-spawned sessions in `devin list`")
        return 0
    removable: list[dict] = []
    print(f"{len(present)} conductor-spawned session(s) registered:")
    for info in present:
        age_s = info["age_s"]
        age_h = round(age_s / 3600, 1) if age_s is not None else None
        ok = _session_eligible(info, args.keep_hours)
        if ok:
            removable.append(info)
        print(f"  {info['sid']:<22} {info['step']:<16} "
              f"{info['status'] or '?':<9} "
              f"age={age_h if age_h is not None else '?'}h  "
              f"{'rm-eligible' if ok else 'kept'}  "
              f"{Path(info['work_dir']).name}")
    if not args.rm:
        if removable:
            print(f"{len(removable)} from runs ended >{args.keep_hours}h ago "
                  f"- `--rm` deletes them (transcripts stay in the run dir)")
        return 0
    for info in removable:
        rc = devin_rm(devin_argv, Path(info["work_dir"]), info["sid"])
        print(f"  rm {info['sid']}: {'ok' if rc == 0 else f'rc={rc}'}")
    return 0


REQUEST_LOCK = threading.RLock()
REQUEST_ID_RE = re.compile(r"^\d{8}-\d{6}-[0-9a-f]{8}$")
RUN_DIR_RE = re.compile(r"^[A-Za-z0-9][\w.-]*$")
_LAUNCH_PIDS: dict[str, int] = {}
_RESUME_PIDS: dict[str, int] = {}
# In-flight proposal generations, keyed by the attempt's gen_id (minted per
# attempt and stamped on the request record). Keyed by attempt, not request
# id, so a stopped attempt unwinding can never discard a newer attempt's
# membership or write over its state.
_GENERATING: set[str] = set()
# Live planner Popen per request id - lets 'Stop proposal' tree-kill the
# devin process now instead of waiting out the wait_worker poll.
_GEN_PROCS: dict = {}
_DRAINING: set[str] = set()

# Planner output spools OUTSIDE every project's watched cwd (same rule as
# the flag dir - trap 5): while a generation owns a request its stdout
# log, ATIF export, extracted draft and the live request overlay all sit
# in PLANNER_SPOOL/<id>/, and only the short publish is deferred until
# data/requests + manifests/ are safe to write. The spool also survives a
# serve restart - drain_planner_spool replays whatever it finds.
PLANNER_SPOOL = Path(tempfile.gettempdir()) / "agent-conductor-planner"
WATCH_POLL_S = 5.0            # _wait_writable poll interval
PLANNER_ORPHAN_GRACE_S = 60   # gen.log this fresh may still be written by
                              # an orphaned planner - drain waits it out
_DRAIN_POLL_S = 10.0          # collect_dashboard drain throttle
_DRAIN_TRIED = 0.0


class RequestError(ValueError):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


class GenerationStopped(Exception):
    """Raised inside _planner_response when the stopgen-<request_id>
    sentinel appears (dashboard 'Stop proposal'). _finish_generation banks
    the attempt's token usage and leaves the request exactly as
    stop_generation restored it - a stop is not an error."""


class PlannerQuestions(Exception):
    """The planner chose the questionnaire path over emitting a manifest
    (the request's 'ask me questions' box was checked). Carries the
    sanitized question list; _finish_generation parks it on the request
    record so the dashboard can ask the human, one modal per question."""
    def __init__(self, questions: list[dict]):
        super().__init__(f"planner asked {len(questions)} question(s)")
        self.questions = questions


def default_model_effort(today: dt.date | None = None) -> tuple[str, str]:
    return (("swe-2", "max") if (today or dt.date.today())
            <= dt.date(2026, 10, 16) else ("gpt-6-luna", "max"))


def _request_path(request_id: str, requests_dir: Path) -> Path:
    if not REQUEST_ID_RE.fullmatch(request_id):
        raise RequestError("Invalid request id", 404)
    return requests_dir / f"{request_id}.json"


def _spool_dir(request_id: str, root: Path | None = None) -> Path:
    if not REQUEST_ID_RE.fullmatch(str(request_id)):
        raise RequestError("Invalid request id", 404)
    return (root or PLANNER_SPOOL) / request_id


def _spool_rec(request_id: str) -> dict | None:
    """The generation thread's live request overlay, if one is parked in
    the spool. While it exists it is more current than the request file,
    which is only rewritten once the workspace is unwatched."""
    try:
        rec = json.loads((_spool_dir(request_id) / "request.json")
                         .read_text(encoding="utf-8"))
    except (OSError, ValueError, RequestError):
        return None
    return rec if isinstance(rec, dict) and rec.get("id") == request_id \
        else None


def _save_spool(rec: dict, spool: Path) -> None:
    """Mirror the request record into the spool - the overlay readers
    consult while the workspace write is still deferred."""
    spool.mkdir(parents=True, exist_ok=True)
    tmp = spool / "request.json.tmp"
    rec["updated_at"] = now_iso()
    tmp.write_text(json.dumps(rec, indent=2, ensure_ascii=False),
                   encoding="utf-8")
    tmp.replace(spool / "request.json")


def _gen_log_path(request_id: str, requests_dir: Path) -> Path:
    """Where the planner's stdout log lives right now: the spool while a
    generation owns the request, requests_dir once the flush copied it."""
    sp = _spool_dir(request_id) / "gen.log"
    return sp if sp.is_file() else requests_dir / f"{request_id}.gen.log"


def _read_request_file(request_id: str, requests_dir: Path) -> dict:
    """The request record exactly as it sits in requests_dir - callers
    deciding whether the human moved on while a spool was parked need the
    disk truth, not the overlay."""
    try:
        return json.loads(_request_path(request_id, requests_dir).read_text(
            encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RequestError("Request not found", 404) from exc
    except (OSError, ValueError) as exc:
        raise RequestError("Cannot read request", 500) from exc


def _read_request(request_id: str, requests_dir: Path) -> dict:
    overlay = _spool_rec(request_id)
    if overlay is not None:
        return overlay          # a generation in flight owns the record
    return _read_request_file(request_id, requests_dir)


def _save_request(rec: dict, requests_dir: Path) -> None:
    requests_dir.mkdir(parents=True, exist_ok=True)
    p = _request_path(rec["id"], requests_dir)
    tmp = p.with_suffix(".json.tmp")
    rec["updated_at"] = now_iso()
    tmp.write_text(json.dumps(rec, indent=2, ensure_ascii=False),
                   encoding="utf-8")
    tmp.replace(p)
    # An authoritative request-file write obsoletes the parked overlay -
    # delete request.json so the shadow drops, but leave the spool's
    # artifacts for the generation thread's flush to rescue.
    try:
        (_spool_dir(rec["id"]) / "request.json").unlink(missing_ok=True)
    except OSError:
        pass


ATTACH_MAX_BYTES = 20 * 1024 * 1024   # clipboard screenshots run MBs
ATTACH_MAX_PER_REQUEST = 20
ATTACH_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
# Magic-byte signatures for the formats a browser can paste/drop. The
# declared Content-Type is never trusted - the bytes decide both the stored
# extension and the type thumbnails are served with.
ATTACH_MAGIC = [
    (b"\x89PNG\r\n\x1a\n", ".png", "image/png"),
    (b"\xff\xd8\xff", ".jpg", "image/jpeg"),
    (b"GIF87a", ".gif", "image/gif"),
    (b"GIF89a", ".gif", "image/gif"),
    (b"BM", ".bmp", "image/bmp"),
]


def _image_sig(data: bytes) -> tuple[str, str] | None:
    for magic, ext, ctype in ATTACH_MAGIC:
        if data.startswith(magic):
            return ext, ctype
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp", "image/webp"
    return None


def save_attachment(data: bytes,
                    attachments_dir: Path = ATTACHMENTS_DIR) -> dict:
    """Persist a pasted/dropped image under data/attachments and return the
    {name, path} record the request JSON stores. `path` is absolute - it is
    what lands in the request text and later in step prompts, so it must
    resolve from any work_dir."""
    if not isinstance(data, (bytes, bytearray)) or not data:
        raise RequestError("Empty attachment")
    if len(data) > ATTACH_MAX_BYTES:
        raise RequestError(
            f"Images over {ATTACH_MAX_BYTES // 1048576} MB are not "
            "supported", 413)
    sig = _image_sig(bytes(data[:16]))
    if sig is None:
        raise RequestError(
            "Only PNG/JPEG/GIF/WebP/BMP images can be attached", 415)
    attachments_dir.mkdir(parents=True, exist_ok=True)
    name = (f"{dt.datetime.now():%Y%m%d-%H%M%S}-{secrets.token_hex(4)}"
            f"{sig[0]}")
    p = attachments_dir / name
    p.write_bytes(bytes(data))
    return {"name": name, "path": str(p.resolve())}


def read_attachment(name: str,
                    attachments_dir: Path = ATTACHMENTS_DIR
                    ) -> tuple[bytes, str]:
    """Serve a saved attachment back to the dashboard for thumbnails. The
    name regex plus resolved-parent check keep the endpoint inside the
    attachments dir."""
    p = attachments_dir / name
    if not ATTACH_NAME_RE.fullmatch(name) or \
            p.resolve().parent != attachments_dir.resolve() or \
            not p.is_file():
        raise RequestError("Attachment not found", 404)
    data = p.read_bytes()
    sig = _image_sig(data[:16])
    return data, (sig[1] if sig else "application/octet-stream")


def _attachment_fields(value) -> list[dict]:
    """Shape-check the [{name, path}] list the dashboard sends. `name` is
    the data/attachments basename (it drives the thumbnail URL); `path` is
    the absolute path inserted into the request text."""
    if value in (None, ""):
        return []
    if not isinstance(value, list) or len(value) > ATTACH_MAX_PER_REQUEST:
        raise RequestError(
            f"At most {ATTACH_MAX_PER_REQUEST} attachments per request")
    out = []
    for a in value:
        if not isinstance(a, dict):
            raise RequestError("Invalid attachment")
        name, path = str(a.get("name") or ""), str(a.get("path") or "")
        if not ATTACH_NAME_RE.fullmatch(name) or not 1 <= len(path) <= 600:
            raise RequestError("Invalid attachment")
        out.append({"name": name, "path": path})
    return out


def _request_fields(payload: dict, rec: dict | None = None) -> dict:
    if not isinstance(payload, dict) or set(payload) - {
            "raw", "label", "project", "model", "effort", "manifest",
            "followup", "on_failure", "max_attempts",
            "plan_model", "plan_effort",
            "worktrees", "merge_resolver", "ask", "attachments"}:
        raise RequestError("Invalid request fields")
    model, effort = default_model_effort()
    fields = {k: (rec or {}).get(k, v) for k, v in (
        ("raw", ""), ("label", ""), ("project", ""), ("on_failure", "continue"),
        ("model", model), ("effort", effort),
        ("plan_model", model), ("plan_effort", effort))}
    for key in fields:
        if key in payload:
            if not isinstance(payload[key], str):
                raise RequestError(f"{key} must be text")
            fields[key] = payload[key].strip()
    for bkey in ("followup", "worktrees", "merge_resolver", "ask"):
        fields[bkey] = bool((rec or {}).get(bkey))
        if bkey in payload:
            v = payload[bkey]
            fields[bkey] = (v.strip().lower() in ("1", "true", "on", "yes")
                            if isinstance(v, str) else bool(v))
    try:
        fields["max_attempts"] = int(payload.get("max_attempts") or
                                     (rec or {}).get("max_attempts") or 3)
    except (TypeError, ValueError):
        raise RequestError("max_attempts must be a number 1-10") from None
    if not 1 <= fields["max_attempts"] <= 10:
        raise RequestError("max_attempts must be 1-10")
    if fields["on_failure"] not in ("stop", "continue"):
        raise RequestError("on_failure must be stop or continue")
    if not fields["raw"] or len(fields["raw"]) > 100_000:
        raise RequestError("Request text must be 1–100,000 characters")
    if len(fields["label"]) > 80:
        raise RequestError("Label must be at most 80 characters")
    if fields["project"] and fields["project"] not in load_projects():
        raise RequestError("Choose a registered project from projects.toml")
    for key in ("model", "plan_model"):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", fields[key]):
            raise RequestError(f"Invalid {key}")
    for key in ("effort", "plan_effort"):   # '' = the family's bare variant
        if fields[key] and not re.fullmatch(
                r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", fields[key]):
            raise RequestError(f"Invalid {key}")
    for mk, ek in (("model", "effort"), ("plan_model", "plan_effort")):
        if (fields[mk] or "").lower() == "adaptive":
            fields[ek] = ""          # adaptive is a routing mode - no effort
        if fields[mk] == "swe-2" and fields[ek] not in {
                "medium", "high", "max"}:
            raise RequestError("SWE-2 supports medium, high or max effort")
    fields["attachments"] = _attachment_fields(
        payload["attachments"] if "attachments" in payload
        else (rec or {}).get("attachments"))
    fields["label"] = fields["label"] or " ".join(fields["raw"].split())[:80]
    return fields


def _merge_open_for(path: Path) -> bool:
    """True when `path` is tracked land inside a repo with an unresolved
    merge (MERGE_HEAD). A write there can be swept into the merge commit
    by the next `git add -A` (trap 11), so publishes defer while a merge
    is open. gitignored destinations (data/, logs/) can never join a
    commit - they stay writable mid-merge."""
    probe = next((a for a in (path, *path.parents) if a.is_dir()), None)
    if probe is None:
        return False
    top = _git(probe, "rev-parse", "--show-toplevel")
    if top.returncode != 0:
        return False
    repo = Path(top.stdout.strip())
    try:
        rel = path.resolve().relative_to(repo.resolve())
    except (OSError, ValueError):
        return False
    if _git(repo, "check-ignore", "-q",
            rel.as_posix() or ".").returncode == 0:
        return False                    # ignored - a merge can't adopt it
    return merge_in_progress(repo)


def _assert_unwatched(runs_dir: Path, *destinations: Path) -> None:
    for sj in runs_dir.glob("*/state.json"):
        try:
            st = json.loads(sj.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for step in (st.get("steps") or {}).values():
            wd = step.get("work_dir") or step.get("project")
            if wd and step_live(step, driver_alive(st)) and any(
                    p.resolve().is_relative_to(Path(wd).resolve())
                    for p in destinations):
                raise RequestError("A live agent is watching this workspace; "
                                   "wait until it finishes before changing "
                                   "requests or manifests", 409)
    for p in destinations:
        if _merge_open_for(p):
            raise RequestError("A merge is in progress in this checkout; "
                               "wait until it finishes before changing "
                               "manifests", 409)


def _wait_writable(runs_dir: Path, *destinations: Path) -> None:
    while True:
        try:
            _assert_unwatched(runs_dir, *destinations)
            return
        except RequestError:
            time.sleep(WATCH_POLL_S)


def create_request(payload: dict, requests_dir: Path = REQUESTS_DIR,
                   runs_dir: Path = RUNS_DIR) -> dict:
    with REQUEST_LOCK:
        _assert_unwatched(runs_dir, requests_dir)
        fields = _request_fields(payload)
        stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        rec = {"id": f"{stamp}-{secrets.token_hex(4)}", "status": "draft",
               "created_at": now_iso(), "manifest": None, "description": "",
               "run": None, "proposed_steps": [], "stale": False,
               "error": None, **fields}
        _save_request(rec, requests_dir)
        return dict(rec)


def _manifest_path(rec: dict, manifests_dir: Path) -> Path:
    p = Path(rec.get("manifest") or "")
    if not rec.get("manifest") or p.parent.resolve() != manifests_dir.resolve() \
            or not p.name.startswith(f"{rec['id']}-") \
            or p.suffix != ".toml":
        raise RequestError("Proposal manifest is not owned by this request", 409)
    return p


def validate_proposal(text: str, manifests_dir: Path = MANIFESTS_DIR) -> Manifest:
    if not isinstance(text, str) or not 1 <= len(text) <= 150_000:
        raise RequestError("Manifest must be 1–150,000 characters")
    try:
        doc = tomllib.loads(text)
        run = doc.get("run", {})
        if not isinstance(run, dict) or run.get("preamble") \
                or run.get("preamble_file"):
            raise RequestError("A proposal cannot replace the worker preamble")
        cfg = load_manifest(manifests_dir / "proposal.toml", text)
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,59}", cfg.name):
            raise RequestError("[run].name must be a short, safe run name")
        if not cfg.description or not cfg.description.strip() \
                or len(cfg.description) > 300:
            raise RequestError("[run].description must be 1–300 characters")
        if not cfg.checkpoint:
            raise RequestError("Proposals must keep checkpoint = true")
        projects = load_projects()
        for step in cfg.steps:
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,59}",
                                step.name):
                raise RequestError(f"Invalid step name: {step.name!r}")
            if step.project not in projects:
                raise RequestError(f"Use a projects.toml slug, not a path: "
                                   f"{step.project!r}")
            if step.prompt_file or not isinstance(step.prompt, str) \
                    or not step.prompt.strip():
                raise RequestError(f"{step.name}: include the task as prompt text")
            if not 0 < step.timeout_min <= 1440:
                raise RequestError(f"{step.name}: invalid timeout_min")
            for val in (step.model, step.effort):
                if val and not re.fullmatch(
                        r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", val):
                    raise RequestError(f"{step.name}: invalid model or effort")
            if step.model == "swe-2" and step.effort not in (
                    None, "medium", "high", "max"):
                raise RequestError(f"{step.name}: SWE-2 supports medium, "
                                   "high or max effort")
            project_dir, _slug = resolve_project(step, projects, manifests_dir)
            if not is_git_repo(project_dir):
                raise RequestError(f"{step.project}: not a git repository root")
        validate_groups(group_waves([
            (*resolve_project(s, projects, manifests_dir), s)
            for s in cfg.steps]))
        return cfg
    except (tomllib.TOMLDecodeError, ManifestError, TypeError, KeyError,
            AttributeError) as exc:
        raise RequestError(f"Invalid proposal: {exc}") from exc


# ------------------------------------------------ deterministic TOML edits
# The request form's option fields (model/effort/attempts/on_failure/
# worktrees/merge_resolver/followup) map to known TOML keys, so review-time
# changes can be patched into the proposal without a planner round-trip.
# Everything written here is re-validated by _publish_proposal before it
# touches disk, so a bad patch can never produce a bad manifest.

def _toml_tables(lines: list[str]) -> list[tuple[str, int, int]]:
    """(kind, header_idx, end_idx) for the three proposal tables.
    Other-looking bracket lines inside a multiline prompt never match a
    bare header line."""
    heads: list[tuple[str, int]] = []
    for i, ln in enumerate(lines):
        s = ln.strip()
        if re.fullmatch(r"\[\[\s*step\s*\]\]", s):
            heads.append(("step", i))
        elif re.fullmatch(r"\[\s*run\s*\]", s):
            heads.append(("run", i))
        elif re.fullmatch(r"\[\s*defaults\s*\]", s):
            heads.append(("defaults", i))
    return [(k, i, heads[j + 1][1] if j + 1 < len(heads) else len(lines))
            for j, (k, i) in enumerate(heads)]


def _toml_keys(lines: list[str], start: int, end: int) -> list[tuple[int, str]]:
    """(line_idx, key) for 'key =' lines in a table, skipping lines inside
    a multiline ''' or \"\"\" literal (a prompt could contain 'worktree =')."""
    out: list[tuple[int, str]] = []
    in_str = None
    for i in range(start + 1, end):
        s = lines[i]
        if in_str:
            if in_str in s:
                in_str = None
            continue
        m = re.match(r"^\s*([A-Za-z_][\w-]*)\s*=\s*(.*)$", s)
        if not m:
            continue
        val = m.group(2)
        for q in ("'''", '"""'):
            if val.startswith(q) and s.count(q) == 1:
                in_str = q
        out.append((i, m.group(1)))
    return out


def _toml_key_at(lines: list[str], start: int, end: int,
                 key: str) -> int | None:
    return next((i for i, k in _toml_keys(lines, start, end) if k == key),
                None)


def _toml_set(lines: list[str], start: int, end: int, key: str,
              value_src: str) -> None:
    i = _toml_key_at(lines, start, end, key)
    if i is None:
        lines.insert(start + 1, f"{key} = {value_src}")
    else:
        lines[i] = f"{key} = {value_src}"


def _wave_worktree_required(cfg: Manifest, manifests_dir: Path) -> set[str]:
    """Step names that MUST keep worktree=true: members of a same-project
    multi-step wave (validation rejects the manifest otherwise)."""
    projects = load_projects()
    resolved = []
    for s in cfg.steps:
        try:
            resolved.append((*resolve_project(s, projects, manifests_dir), s))
        except ManifestError:
            resolved.append((Path(s.project), s.project, s))
    need: set[str] = set()
    for members in group_waves(resolved):
        by_dir: dict[str, list[Step]] = {}
        for pdir, _slug, s in members:
            by_dir.setdefault(str(pdir), []).append(s)
        for ss in by_dir.values():
            if len(ss) > 1:
                need.update(s.name for s in ss)
    return need


def _apply_fields_to_toml(text: str, fields: dict,
                          manifests_dir: Path = MANIFESTS_DIR) -> str:
    """Patch a proposal's option fields in place - the 'Apply to TOML' path.
    Only keys present in `fields` are touched: {on_failure, merge_resolver}
    write [run]; {model, effort, max_attempts} write [defaults] and are
    stripped from [[step]] sections so the form value wins; {worktrees}
    sets/clears per-step worktree (kept on same-project wave members and
    never added to cloud steps - both would fail validation);
    {followup} adds/removes the closing follow-up-plan step. Request text
    and project are NOT applyable - those need regeneration."""
    try:
        cfg = load_manifest(manifests_dir / "applied.toml", text)
    except (ManifestError, tomllib.TOMLDecodeError) as exc:
        raise RequestError(f"Existing proposal does not parse - fix it or "
                           f"regenerate first ({exc})", 409)
    lines = text.splitlines()

    def sect(kind: str) -> tuple[int | None, int | None]:
        for k, s, e in _toml_tables(lines):
            if k == kind:
                return s, e
        return None, None

    def ensure(kind: str) -> tuple[int, int]:
        s, e = sect(kind)
        if s is None:
            if lines and lines[-1].strip():
                lines.append("")
            lines.append(f"[{kind}]")
            s, e = sect(kind)
        return s, e

    def step_sect(name: str) -> tuple[int | None, int | None]:
        for k, s, e in _toml_tables(lines):
            if k != "step":
                continue
            i = _toml_key_at(lines, s, e, "name")
            if i is None:
                continue
            try:
                if tomllib.loads(lines[i]).get("name") == name:
                    return s, e
            except (tomllib.TOMLDecodeError, ValueError):
                continue
        return None, None

    if fields.get("on_failure") in ("stop", "continue"):
        _toml_set(lines, *ensure("run"), "on_failure",
                  json.dumps(fields["on_failure"]))
    if "merge_resolver" in fields:
        _toml_set(lines, *ensure("run"), "merge_resolver",
                  "true" if fields["merge_resolver"] else "false")
    # Per-step overrides of fields the form owns would silently fight the
    # applied value - strip them so defaults/run keys are the sole source.
    drop_keys = ({"merge_resolver"} if "merge_resolver" in fields else
                 set()) | (
        {"model", "effort"}
        if any(fields.get(k) is not None for k in ("model", "effort"))
        else set()) | (
        {"max_attempts"} if fields.get("max_attempts") is not None else set())
    if drop_keys:
        drop = sorted((i for k, s, e in _toml_tables(lines) if k == "step"
                       for i, key in _toml_keys(lines, s, e)
                       if key in drop_keys), reverse=True)
        for i in drop:
            del lines[i]
    if any(fields.get(k) is not None for k in ("model", "effort")) or \
            fields.get("max_attempts") is not None:
        s, e = ensure("defaults")
        for key in ("model", "effort"):
            if fields.get(key) is not None:
                _toml_set(lines, s, e, key, json.dumps(fields[key]))
        if fields.get("max_attempts") is not None:
            _toml_set(lines, s, e, "max_attempts",
                      str(int(fields["max_attempts"])))
    if "worktrees" in fields:
        want = bool(fields["worktrees"])
        required = _wave_worktree_required(cfg, manifests_dir)
        cloud_steps = {s.name for s in cfg.steps if s.cloud}
        for s in cfg.steps:
            ss, se = step_sect(s.name)
            if ss is None:
                continue
            if want and s.name not in cloud_steps:
                _toml_set(lines, ss, se, "worktree", "true")
            elif not want and s.name not in required:
                i = _toml_key_at(lines, ss, se, "worktree")
                if i is not None:
                    del lines[i]
    if "followup" in fields:
        ss, se = step_sect("follow-up-plan")
        if fields["followup"] and ss is None and cfg.steps:
            slug = fields.get("project") or cfg.steps[-1].project
            lines.extend(FOLLOWUP_STEP.format(
                slug=slug, run=cfg.name, runs=RUNS_DIR.as_posix(),
                manifests=MANIFESTS_DIR.as_posix()).splitlines())
        elif not fields["followup"] and ss is not None:
            del lines[ss:se]
            if ss and ss - 1 < len(lines) and not lines[ss - 1].strip():
                del lines[ss - 1]
    return "\n".join(lines) + ("\n" if text.endswith("\n") else "")


def _guarantee_isolation(text: str, rec: dict,
                         manifests_dir: Path = MANIFESTS_DIR) -> str:
    """The worktrees/merge_resolver checkboxes are guarantees, not hints:
    add the flags when the planner omitted them. One-directional - an
    unchecked box never strips a deliberate worktree/merge_resolver."""
    fields = {}
    if rec.get("worktrees"):
        fields["worktrees"] = True
    if rec.get("merge_resolver"):
        fields["merge_resolver"] = True
    if not fields:
        return text
    try:
        return _apply_fields_to_toml(text, fields, manifests_dir)
    except RequestError:
        return text   # unparseable planner output fails validation anyway


def _publish_proposal(rec: dict, text: str, requests_dir: Path,
                      manifests_dir: Path, runs_dir: Path) -> dict:
    cfg = validate_proposal(text, manifests_dir)
    if rec.get("followup") and not any(
            s.name == "follow-up-plan" for s in cfg.steps):
        # The planner was told to close with a review step but forgot -
        # append it so the checkbox is a guarantee, not a hint.
        slug = rec.get("project") or cfg.steps[-1].project
        text = text.rstrip() + FOLLOWUP_STEP.format(
            slug=slug, run=cfg.name, runs=RUNS_DIR.as_posix(),
            manifests=MANIFESTS_DIR.as_posix())
        cfg = validate_proposal(text, manifests_dir)
    if rec.get("worktrees") or rec.get("merge_resolver"):
        # The checkboxes are guarantees: add the flags the planner omitted.
        text = _guarantee_isolation(text, rec, manifests_dir)
        cfg = validate_proposal(text, manifests_dir)
    if not manifests_dir.is_dir():
        raise RequestError(f"Missing manifests directory: {manifests_dir}", 409)
    _assert_unwatched(runs_dir, requests_dir, manifests_dir)
    p = (_manifest_path(rec, manifests_dir) if rec.get("manifest") else
         manifests_dir / f"{rec['id']}-{cfg.name}.toml")
    tmp = p.with_suffix(".toml.tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(p)
    rec.update(status="ready", manifest=str(p), run=cfg.name,
               description=cfg.description.strip(), error=None, stale=False,
               pending_questions=[],
               proposed_steps=[{
                   "step": s.name, "slug": s.project, "model": s.model,
                   "effort": s.effort, "mode": s.mode, "task": " ".join(
                       s.prompt.split())[:160], "wt": s.worktree,
                   "cloud": s.cloud} for s in cfg.steps])
    _save_request(rec, requests_dir)
    return dict(rec)


def save_request(request_id: str, payload: dict,
                 requests_dir: Path = REQUESTS_DIR,
                 manifests_dir: Path = MANIFESTS_DIR,
                 runs_dir: Path = RUNS_DIR) -> dict:
    with REQUEST_LOCK:
        _assert_unwatched(runs_dir, requests_dir, manifests_dir)
        rec = _read_request(request_id, requests_dir)
        stage = _request_status(rec, runs_dir)
        if stage not in ("draft", "ready", "error", "launch_failed",
                         "questions"):
            raise RequestError("This request is being processed or already run", 409)
        rec["status"] = stage
        fields = _request_fields(payload, rec)
        # Planner model/effort never enter the TOML, so they cannot stale a
        # proposal; the rest diverge from the on-disk runbook until the
        # user applies them to the TOML or regenerates.
        changed = any(fields[k] != rec.get(k) for k in
                      ("raw", "project", "model", "effort", "followup",
                       "worktrees", "merge_resolver",
                       "on_failure", "max_attempts")) \
            or fields["attachments"] != (rec.get("attachments") or [])
        raw_moved = (fields["raw"] != rec.get("raw")
                     or fields["project"] != rec.get("project"))
        rec.update(fields)
        if "manifest" in payload:
            return _publish_proposal(rec, payload["manifest"],
                                     requests_dir, manifests_dir, runs_dir)
        if stage == "questions" and raw_moved:
            # The parked questionnaire was drafted against the old request
            # text - drop it; Regenerate re-plans from the new one.
            rec.update(status="draft", pending_questions=[], qa=[],
                       qa_rounds=0)
        if rec.get("manifest") and changed:
            rec["stale"] = True
        _save_request(rec, requests_dir)
        return dict(rec)


def apply_request(request_id: str, payload: dict,
                  requests_dir: Path = REQUESTS_DIR,
                  manifests_dir: Path = MANIFESTS_DIR,
                  runs_dir: Path = RUNS_DIR) -> dict:
    """Save the form fields AND rewrite the proposal TOML to match - the
    deterministic alternative to regeneration. Refused when the request
    text or target project changed: those shape the step prompts, which a
    deterministic patch cannot rewrite."""
    with REQUEST_LOCK:
        _assert_unwatched(runs_dir, requests_dir, manifests_dir)
        rec = _read_request(request_id, requests_dir)
        stage = _request_status(rec, runs_dir)
        if stage not in ("draft", "ready", "error", "launch_failed",
                         "questions"):
            raise RequestError("This request is being processed or already run",
                               409)
        if not rec.get("manifest"):
            raise RequestError("No proposal yet - generate one first", 409)
        rec["status"] = stage
        fields = _request_fields(payload, rec)
        if fields["raw"] != rec.get("raw") or \
                fields["project"] != rec.get("project"):
            raise RequestError("Request text or project changed - the step "
                               "prompts depend on them; regenerate the "
                               "proposal instead", 409)
        text = payload.get("manifest")
        if not isinstance(text, str):
            p = _manifest_path(rec, manifests_dir)
            try:
                text = p.read_text(encoding="utf-8")
            except OSError as exc:
                raise RequestError("Cannot read the proposal TOML", 500) from exc
        rec.update(fields)
        patched = _apply_fields_to_toml(text, fields, manifests_dir)
        return _publish_proposal(rec, patched, requests_dir, manifests_dir,
                                 runs_dir)


def request_details(request_id: str, requests_dir: Path = REQUESTS_DIR,
                    manifests_dir: Path = MANIFESTS_DIR,
                    runs_dir: Path | None = None) -> dict:
    with REQUEST_LOCK:
        rec = dict(_read_request(request_id, requests_dir))
        if rec.get("manifest"):
            p = _manifest_path(rec, manifests_dir)
            rec["manifest_text"] = (p.read_text(encoding="utf-8")
                                    if p.is_file() else "")
        else:
            rec["manifest_text"] = ""
        log = _gen_log_path(request_id, requests_dir)
        rec["gen_tail"] = (stdout_tail(str(log), 40)
                           if log.is_file() else None)
        if runs_dir:
            status = _request_status(rec, runs_dir)
            if status == "error" and rec["status"] == "generating":
                rec["error"] = "Planning was interrupted. Regenerate to retry."
            rec["status"] = status
        return rec


MAX_QUESTIONS = 8          # per questionnaire round
MAX_QUESTION_ROUNDS = 3    # rounds of questions per generation


def _extract_questions(text: str) -> list[dict]:
    """Parse the planner's BEGIN_QUESTIONS/END_QUESTIONS block into the
    sanitized list the questionnaire modal renders - the planner's JSON is
    untrusted, so every field is re-shaped and capped. Raises RequestError
    when a block exists but is unusable (fails closed like a bad TOML)."""
    matches = re.findall(r"^BEGIN_QUESTIONS\s*\n(.*?)^END_QUESTIONS\s*$",
                         text, re.M | re.S)
    if not matches:
        return None
    try:
        items = json.loads(matches[-1])
    except ValueError as exc:
        raise RequestError("Planner's questions were malformed "
                           f"({exc}); try regenerating", 502) from exc
    if not isinstance(items, list) or not items:
        raise RequestError("Planner's questions were malformed; "
                           "try regenerating", 502)
    out: list[dict] = []
    for it in items:
        if len(out) >= MAX_QUESTIONS:
            break
        if not isinstance(it, dict):
            continue
        q = str(it.get("question") or "").strip()[:500]
        if not q:
            continue
        opts = []
        for o in it.get("options") or []:
            if not isinstance(o, dict):
                continue
            lbl = str(o.get("label") or "").strip()[:80]
            if lbl:
                opts.append({"label": lbl,
                             "description": str(o.get("description")
                                                or "")[:200]})
            if len(opts) >= 6:
                break
        out.append({"qid": f"q{len(out)}", "question": q,
                    "header": str(it.get("header") or "")[:40],
                    "options": opts,
                    "multi_select": bool(it.get("multi_select"))})
    if not out:
        raise RequestError("Planner's questions were malformed; "
                           "try regenerating", 502)
    return out


def _planner_response(rec: dict, projects: dict,
                      devin_argv: list[str],
                      log_path: Path | None = None,
                      fix_note: str | None = None,
                      qa: list[dict] | None = None) -> str:
    slugs = ", ".join(sorted(projects))
    fix = (f"A previous draft of this proposal failed TOML parsing: "
           f"{fix_note}. Re-emit the full corrected TOML.\n\n"
           if fix_note else "")
    followup = ""
    if rec.get("followup"):
        followup = (
            "The human also asked for a closing review step: append a final "
            "sequential [[step]] named 'follow-up-plan' on the main project "
            "slug, with a modest timeout_min. Its prompt must direct the "
            "worker to review what this run accomplished against the human "
            "request - per-step logs, STATUS/NEXT lines and notes will be "
            f"under {RUNS_DIR.as_posix()} in the newest dir ending in your "
            "run's name, alongside each project's .agent_tasks files - then "
            "write a NEW conductor TOML manifest proposing only the "
            "still-unfinished or newly discovered work, saved as "
            f"'{MANIFESTS_DIR.as_posix()}/<your run name>-followup.toml' "
            "using forward slashes. The step must NOT launch or implement "
            "the follow-up - a human reviews it.\n\n")
    isolate = ""
    if rec.get("worktrees"):
        isolate += ("The human asked for worktree isolation: set "
                    "worktree = true on EVERY [[step]] (except a cloud = "
                    "true step, where worktrees are meaningless). Each "
                    "step then runs on a conductor/<run>/<step> branch in "
                    "its own checkout and merges back after its wave.\n\n")
    if rec.get("merge_resolver"):
        isolate += ("The human asked for automatic conflict resolution: "
                    "set merge_resolver = true in [run], so a conflicted "
                    "worktree merge is finished by a resolve-<step> "
                    "session instead of only being parked.\n\n")
    ask_block = ""
    if rec.get("ask"):
        ask_block = (
            "The human opted in to clarifying questions. ONLY if a decision "
            "in the request genuinely changes the runbook and cannot be "
            "sensibly inferred, you may reply with a questionnaire INSTEAD "
            "of the manifest - otherwise go straight to the TOML. To ask, "
            "reply with only:\n"
            "BEGIN_QUESTIONS\n"
            '[{"question": "full question text", "header": "short label", '
            '"options": [{"label": "choice", "description": "why"}], '
            '"multi_select": false}]\n'
            "END_QUESTIONS\n"
            "then a final STATUS: done line. A JSON array of 1-6 questions; "
            "2-4 options each where sensible (omit options when free text "
            "is the more natural answer); multi_select = true when several "
            "choices may apply at once. The human can always skip a "
            "question or write their own answer, so never ask what you can "
            "reasonably decide yourself.\n\n")
    attach_block = ""
    if rec.get("attachments"):
        attach_block = (
            "IMAGE ATTACHMENTS - the human attached these images to the "
            "request (absolute paths saved under data/attachments). Read an "
            "image when the request depends on what it shows, and pass the "
            "path through to the prompt of any step that needs it:\n"
            + "".join(f"- {a.get('path')}\n" for a in rec["attachments"])
            + "\n")
    qa_block = ""
    if qa:
        lines = []
        for i, a in enumerate(qa, 1):
            bits = []
            if a.get("answer"):
                bits.append("chose " + ", ".join(
                    json.dumps(x) for x in a["answer"]))
            if a.get("text"):
                bits.append("wrote " + json.dumps(a["text"]))
            lines.append(f"{i}. {a.get('question', '')}\n   A: "
                         + ("; ".join(bits)
                            if bits else "(skipped - your call)"))
        qa_block = (
            "CLARIFYING Q&A - the human answered your earlier questions; "
            "let the answers steer scope, project choice and sequencing:\n"
            + "\n".join(lines) +
            "\nAnswer round complete - emit the manifest unless a decisive "
            "gap still remains.\n\n")
    known_verify = {s: p["verify"] for s, p in projects.items()
                    if p.get("verify")}
    verify_hint = (f"Registered verify commands: {known_verify} - set "
                   "verify under [defaults] (or per [[step]]) to the "
                   "command for each step's project. A failed verify "
                   "keeps the step's worktree parked instead of merging "
                   "broken work, so it is worth having.\n"
                   if known_verify else
                   "Do not guess verification commands; include verify "
                   "only when known.\n")
    prompt = f"""You are writing a proposed agent-conductor TOML runbook, not doing the work.
Do not edit files, invoke tools, run commands, inspect projects, or start a run.
Reply with exactly one TOML document between BEGIN_MANIFEST and END_MANIFEST,
then a final STATUS: done line. No Markdown fences or other text.
The ONLY task specification is the human request below; do not invent repo details.
Registered project slugs (use these, never paths): {slugs}
Preferred project (if supplied): {rec['project'] or '(infer from request)'}
[run] needs name (short safe slug), description (one useful sentence),
on_failure = {json.dumps(rec.get('on_failure') or 'continue')}, and
checkpoint = true.
[defaults] needs model = {json.dumps(rec['model'])}, effort =
{json.dumps(rec['effort'])}, mode = "bypass", and timeout_min.
Set max_attempts = {int(rec.get('max_attempts') or 3)} in [defaults]: a step
that underestimates its timeout_min is then continued by a fresh session
instead of ending early (1 = no retries).
Each [[step]] needs a unique name, a registered project slug, and a concrete
prompt describing only that step. Prefer sequential steps. If using waves on
one project, every member needs worktree = true. Cloud steps require explicit
user intent and a project permitted for cloud. No prompt_file or preamble.
{verify_hint}Do not reproduce credentials or private data in the tracked manifest; refer
agents to the project files containing those details instead.
The proposal must NOT start work; the human will edit it and launch later.
Windows paths belong in TOML literal strings (single quotes) or use forward
slashes - a backslash inside a double-quoted string is a TOML escape and
breaks parsing.
{followup}
{isolate}{ask_block}{attach_block}{fix}HUMAN REQUEST:
{rec['raw']}
{qa_block}

BEGIN_MANIFEST
...your TOML here...
END_MANIFEST
STATUS: done
"""
    with tempfile.TemporaryDirectory(prefix="conductor-planner-") as wd, \
            tempfile.TemporaryDirectory(prefix="conductor-planner-log-") as ld:
        prompt_path = Path(wd) / "request.md"
        prompt_path.write_text(prompt, encoding="utf-8")
        log = log_path or Path(ld) / "planner.stdout.log"
        export_path = log.with_suffix(".export.json")
        pm = rec.get("plan_model") or rec["model"]
        pe = rec.get("plan_effort")          # '' = bare variant, no suffix
        if pe is None:
            pe = rec["effort"]
        cmd = devin_argv + [
            "--print", "--prompt-file", str(prompt_path),
            "--model", f"{pm}-{pe}"
            if pe and pm != "adaptive" else pm,
            "--permission-mode", "normal", "--export", str(export_path),
            "--respect-workspace-trust", "false"]
        rid = rec.get("id")
        sflag = gen_stop_flag(rid) if rid else None
        with open(log, "ab" if fix_note else "wb") as out:
            # The planner gets its own process group like run workers
            # (trap 15): a merge elsewhere must not reach it, and a serve
            # restart leaves it free to finish - the spool keeps whatever
            # it writes for drain_planner_spool to recover.
            proc = subprocess.Popen(cmd, cwd=wd, stdout=out,
                                    stderr=subprocess.STDOUT,
                                    stdin=subprocess.DEVNULL,
                                    env=clean_devin_env(),
                                    **_detached_kwargs())
            if rid:
                with REQUEST_LOCK:
                    _GEN_PROCS[rid] = proc
            try:
                rc, timed_out, _hung, stopped, _ext = wait_worker(
                    proc, log, 600,
                    stop_flags=(sflag,) if sflag is not None else ())
            finally:
                if rid:
                    with REQUEST_LOCK:
                        if _GEN_PROCS.get(rid) is proc:
                            _GEN_PROCS.pop(rid, None)
        if stopped or (sflag is not None and sflag.is_file()):
            raise GenerationStopped(f"request {rid} proposal stopped")
        if timed_out or rc != 0:
            raise RequestError("Planner did not finish; check CLI auth and "
                               "retry generation", 502)
        text = log.read_text(encoding="utf-8", errors="replace")
    matches = re.findall(r"^BEGIN_MANIFEST\s*\n(.*?)^END_MANIFEST\s*$",
                         text, re.M | re.S)
    if not matches:
        # The 'ask me questions' contract: a questionnaire INSTEAD of the
        # TOML parks the generation until the human answers (or skips).
        if rec.get("ask") and "BEGIN_QUESTIONS" in text:
            raise PlannerQuestions(_extract_questions(text))
        raise RequestError("Planner returned no TOML proposal; try regenerating",
                           502)
    return matches[-1].strip() + "\n"


def _rotate_log(log_path: Path) -> None:
    """gen.log -> gen.prev.log so a regeneration keeps the previous
    planner run's output instead of truncating it."""
    try:
        if log_path.is_file():
            log_path.replace(log_path.with_name("gen.prev.log"))
    except OSError:
        pass


def _save_draft(rec: dict, spool: Path, text: str) -> None:
    """The extracted TOML lands in the spool BEFORE any publish wait, so
    the generated proposal is durable through cancellation and restart.
    draft_started_at pins it to this generation - a superseded draft
    stays on disk but is never auto-published."""
    tmp = spool / "draft.toml.tmp"
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(spool / "draft.toml")
    rec["draft_started_at"] = rec.get("generation_started_at")
    _save_spool(rec, spool)


def _job_meta(spool: Path) -> dict:
    try:
        job = json.loads((spool / "job.json").read_text(encoding="utf-8"))
        return job if isinstance(job, dict) else {}
    except (OSError, ValueError):
        return {}


def _disk_untouched(request_id: str, requests_dir: Path, job: dict) -> bool:
    """job.json recorded the request file's stamp when the generation
    started; any other value means a human write landed while the
    generation was parked, and the parked state must not overwrite it."""
    if job.get("base_updated_at") is None:
        return False
    try:
        return _read_request_file(request_id, requests_dir).get(
            "updated_at") == job["base_updated_at"]
    except RequestError:
        return False                   # the request was deleted outright


def _copy_spool_artifacts(request_id: str, spool: Path, requests_dir: Path,
                        include_draft: bool = True) -> None:
    """The durable half of a generation: planner stdout, ATIF export,
    the previous round's log, and the extracted draft, copied to
    data/requests/<id>.* - call only inside an unwatched window."""
    names = [("gen.log", "gen.log"), ("gen.export.json", "gen.export.json"),
             ("gen.prev.log", "gen.prev.log")]
    if include_draft:
        names.append(("draft.toml", "draft.toml"))
    for src_name, suffix in names:
        src = spool / src_name
        if src.is_file():
            try:
                shutil.copy2(src, requests_dir / f"{request_id}.{suffix}")
            except OSError:
                pass


def _flush_spool(request_id: str, spool: Path, requests_dir: Path,
                 runs_dir: Path, gen_id: str | None = None) -> None:
    """Land a parked generation state (questions / error) - deferred until
    data/requests is unwatched, then the overlay becomes the request file
    and the artifacts are copied out of the spool. If the human moved the
    request on while the flush waited, only the artifacts survive; a spool
    a newer generation attempt claimed (job.json carries its gen_id) is
    left to that attempt entirely."""
    while True:
        _wait_writable(runs_dir, requests_dir)
        with REQUEST_LOCK:
            try:
                _assert_unwatched(runs_dir, requests_dir)
            except RequestError:
                continue               # a watcher arrived in the gap
            if gen_id is not None and _job_meta(spool).get(
                    "gen_id") not in (None, gen_id):
                return                 # a newer attempt owns this spool
            overlay = _spool_rec(request_id)
            _copy_spool_artifacts(request_id, spool, requests_dir)
            if overlay is not None and _disk_untouched(
                    request_id, requests_dir, _job_meta(spool)):
                _save_request(overlay, requests_dir)
            shutil.rmtree(spool, ignore_errors=True)
            return


def _publish_from_spool(request_id: str, text: str, spool: Path,
                        requests_dir: Path, manifests_dir: Path,
                        runs_dir: Path, tok_plan: dict | None = None,
                        gen_id: str | None = None) -> None:
    """The deferred publish - the ONLY part of generation that waits on
    the repo. The planner's whole output is already durable in the spool;
    this just validates and lands it once no watcher or open merge owns
    the checkout. A request the human moved on mid-wait gets artifact
    rescue instead of an overwrite; a spool a newer attempt claimed
    (job.json or overlay gen_id no longer matches) is left untouched."""
    while True:
        _wait_writable(runs_dir, requests_dir, manifests_dir)
        try:
            with REQUEST_LOCK:
                _assert_unwatched(runs_dir, requests_dir, manifests_dir)
                overlay = _spool_rec(request_id)
                job = _job_meta(spool)
                if gen_id is not None and (
                        job.get("gen_id") not in (None, gen_id)
                        or (overlay or {}).get("gen_id") not in
                            (None, gen_id)):
                    return             # a newer attempt owns this spool
                if overlay is not None and overlay.get("status") == \
                        "generating" and _disk_untouched(
                            request_id, requests_dir, job):
                    if tok_plan:
                        plan_tok = _merge_tokens(
                            dict(overlay.get("plan_tokens") or {}), tok_plan)
                        if plan_tok:
                            overlay["plan_tokens"] = plan_tok
                    _copy_spool_artifacts(request_id, spool, requests_dir,
                                          include_draft=False)
                    _publish_proposal(overlay, text, requests_dir,
                                      manifests_dir, runs_dir)
                else:
                    # moved on mid-wait - keep the draft + log as rescue
                    _copy_spool_artifacts(request_id, spool, requests_dir)
                shutil.rmtree(spool, ignore_errors=True)
                return
        except RequestError:
            try:
                _assert_unwatched(runs_dir, requests_dir, manifests_dir)
            except RequestError:
                continue
            raise


def _finish_generation(request_id: str, requests_dir: Path,
                       manifests_dir: Path, runs_dir: Path,
                       devin_argv: list[str], gen_id: str) -> None:
    """One planning attempt: plan into the SPOOL (outside every project's
    watched cwd), then defer only the short publish until the repo is
    writable. Every state write re-checks BOTH status=='generating' AND
    gen_id - stop_generation restores the record while the planner
    unwinds, and an immediate Regenerate already owns the 'generating'
    slot then, so a stale attempt must not touch the record. Killing this
    thread (restart, shutdown) loses nothing - drain_planner_spool
    replays whatever the spool holds."""
    tok_plan: dict[str, int] = {}
    spool = _spool_dir(request_id)

    def still_mine(rec: dict) -> bool:
        return rec.get("status") == "generating" \
            and rec.get("gen_id") == gen_id

    def save_draft(text: str) -> bool:
        """Draft is durable in the spool before any publish wait - but
        only while this attempt still owns the record. A stop or a
        Regenerate drops the overlay; resurrecting it here would let the
        stale attempt publish as if it had never been stopped."""
        with REQUEST_LOCK:
            if not still_mine(_read_request(request_id, requests_dir)):
                return False
            _save_draft(rec, spool, text)
        return True

    try:
        rec = _read_request(request_id, requests_dir)
        spool.mkdir(parents=True, exist_ok=True)
        base = _read_request_file(request_id, requests_dir)
        (spool / "job.json").write_text(json.dumps({
            "request_id": request_id, "requests_dir": str(requests_dir),
            "manifests_dir": str(manifests_dir), "runs_dir": str(runs_dir),
            "gen_id": gen_id,
            "base_updated_at": base.get("updated_at"),
            "base_status": base.get("status"),
            "created_at": now_iso()}), encoding="utf-8")
        log_path = spool / "gen.log"
        _rotate_log(log_path)
        export_path = log_path.with_suffix(".export.json")
        projects = load_projects()
        qa = rec.get("qa") or None    # set -> this pass refines on answers
        try:
            text = _planner_response(rec, projects, devin_argv, log_path,
                                     qa=qa)
            _merge_tokens(tok_plan, tail_metrics(export_path))
            if not save_draft(text):
                return              # stopped/superseded - nothing to publish
            try:
                defaults = tomllib.loads(text).get("defaults", {})
            except tomllib.TOMLDecodeError as exc:
                # A malformed draft (e.g. a Windows path's \U inside a
                # double-quoted string) is trivially fixable - retry once
                # with the parse error fed back.
                text = _planner_response(rec, projects, devin_argv,
                                         log_path, fix_note=str(exc), qa=qa)
                _merge_tokens(tok_plan, tail_metrics(export_path))
                if not save_draft(text):
                    return          # stopped/superseded mid-retry
                defaults = tomllib.loads(text).get("defaults", {})
        except PlannerQuestions as q:
            # The planner asked instead of drafting - park the sanitized
            # questionnaire on the request. The dashboard poses it one
            # modal at a time; answer_request restarts this function as the
            # refine pass once every question has an answer or a skip.
            # The parked overlay is crash-recoverable: drain lands it.
            _merge_tokens(tok_plan, tail_metrics(export_path))
            with REQUEST_LOCK:
                rec = _read_request(request_id, requests_dir)
                if not still_mine(rec):
                    return      # stopped/superseded - nothing to park
                rounds = int(rec.get("qa_rounds") or 0) + 1
                plan_tok = _merge_tokens(
                    dict(rec.get("plan_tokens") or {}), tok_plan)
                if plan_tok:
                    rec["plan_tokens"] = plan_tok
                if rounds > MAX_QUESTION_ROUNDS:
                    rec.update(
                        status="error", pending_questions=[],
                        error=f"The planner still has questions after "
                              f"{MAX_QUESTION_ROUNDS} rounds - "
                              "regenerate, or make the request more "
                              "specific")
                else:
                    rec.update(status="questions", qa_rounds=rounds,
                               pending_questions=q.questions,
                               error=None)
                _save_spool(rec, spool)
            _flush_spool(request_id, spool, requests_dir, runs_dir,
                         gen_id=gen_id)
            return
        if defaults.get("model") != rec["model"] or \
                defaults.get("effort") != rec["effort"]:
            raise RequestError("Planner ignored the selected model or effort; "
                               "regenerate or edit the TOML directly")
        run_doc = tomllib.loads(text).get("run", {})
        if run_doc.get("on_failure") != (rec.get("on_failure") or "continue"):
            raise RequestError("Planner ignored the selected on_failure; "
                               "regenerate or edit the TOML directly")
        try:
            planner_attempts = int(defaults.get("max_attempts") or 0)
        except (TypeError, ValueError):
            planner_attempts = 0
        if planner_attempts != int(rec.get("max_attempts") or 3):
            raise RequestError("Planner ignored the selected max_attempts; "
                               "regenerate or edit the TOML directly")
        _publish_from_spool(request_id, text, spool, requests_dir,
                            manifests_dir, runs_dir, tok_plan,
                            gen_id=gen_id)
    except GenerationStopped:
        # stop_generation already restored the record (draft, or ready with
        # the previous proposal). Bank the killed attempt's token usage -
        # plan_tokens accumulates across attempts - and touch nothing else;
        # a newer attempt may already own the 'generating' slot.
        try:
            _assert_unwatched(runs_dir, requests_dir)
            with REQUEST_LOCK:
                rec = _read_request(request_id, requests_dir)
                if rec.get("status") == "generating":
                    return      # superseded or stop write pending - the
                                # newer/restoring write owns the record
                plan_tok = _merge_tokens(
                    dict(rec.get("plan_tokens") or {}), tok_plan)
                if plan_tok:
                    rec["plan_tokens"] = plan_tok
                    _save_request(rec, requests_dir)
        except RequestError:
            pass
    except Exception as exc:
        _wait_writable(runs_dir, requests_dir, LOGS_DIR)
        LOG.warning("request %s generation failed: %s", request_id, exc)
        msg = (f"Planner returned malformed TOML ({exc})"
               if isinstance(exc, tomllib.TOMLDecodeError) else str(exc))
        overlay = _spool_rec(request_id)
        if overlay is not None and still_mine(overlay):
            plan_tok = _merge_tokens(
                dict(overlay.get("plan_tokens") or {}), tok_plan)
            if plan_tok:
                overlay["plan_tokens"] = plan_tok
            overlay.update(status="error", error=msg[:300])
            _save_spool(overlay, spool)
        # Flush only while this attempt still owns the spool - a newer
        # generation rewrote job.json with its own gen_id and its parked
        # state is not ours to land (or delete).
        if _job_meta(spool).get("gen_id") in (None, gen_id):
            _flush_spool(request_id, spool, requests_dir, runs_dir,
                         gen_id=gen_id)
    finally:
        with REQUEST_LOCK:
            _GENERATING.discard(gen_id)


def drain_planner_spool(root: Path | None = None) -> list[str]:
    """Adopt generations whose owner died (server restart, killed thread).
    Each spool's job.json names its own request/manifest/run dirs, so the
    restarted serve replays deferred publishes for whichever checkout
    spawned them. Returns the request ids it took over."""
    root = root or PLANNER_SPOOL
    adopted = []
    try:
        dirs = sorted(root.iterdir())
    except OSError:
        return []
    for d in dirs:
        if not d.is_dir() or not REQUEST_ID_RE.fullmatch(d.name) \
                or not (d / "job.json").is_file():
            continue
        # _GENERATING is keyed by attempt gen_id - a live generation's
        # spool carries it in job.json (and the overlay once written).
        live = {_job_meta(d).get("gen_id"),
                (_spool_rec(d.name) or {}).get("gen_id")}
        if _GENERATING.intersection(live - {None}) \
                or d.name in _DRAINING:
            continue
        _DRAINING.add(d.name)
        threading.Thread(target=_drain_spool, args=(d,), daemon=True).start()
        adopted.append(d.name)
    return adopted


def _drain_spool(spool: Path) -> None:
    try:
        job = _job_meta(spool)
        try:
            rid = str(job["request_id"])
            requests_dir = Path(job["requests_dir"])
            manifests_dir = Path(job["manifests_dir"])
            runs_dir = Path(job["runs_dir"])
        except (KeyError, TypeError):
            return                  # unreadable job - leave for a human
        overlay = _spool_rec(rid)
        if (overlay or {}).get("status") == "generating":
            _drain_generating(rid, overlay, spool, requests_dir,
                              manifests_dir, runs_dir)
        else:
            # parked questions/error/ready interrupted mid-flush, or a
            # stray spool whose overlay an interleaved save dropped -
            # either way the flush rescues the artifacts
            _flush_spool(rid, spool, requests_dir, runs_dir)
    finally:
        _DRAINING.discard(spool.name)


def _drain_generating(rid: str, overlay: dict, spool: Path,
                      requests_dir: Path, manifests_dir: Path,
                      runs_dir: Path) -> None:
    """A generation that never finished: publish a spooled draft, extract
    one from a finished-but-uncaptured planner log, wait out an orphan
    that is still writing, else land the interruption as an error."""
    draft = spool / "draft.toml"
    current = overlay.get("draft_started_at") == overlay.get(
        "generation_started_at") and overlay.get("draft_started_at")
    draft_ok = draft.is_file() and bool(current)
    log = spool / "gen.log"
    if not draft_ok and log.is_file():
        # A detached planner may have outlived the server: while its log
        # is still being written, leave the spool for the next drain pass.
        try:
            fresh = (time.time() - log.stat().st_mtime
                     < PLANNER_ORPHAN_GRACE_S)
        except OSError:
            fresh = False
        if fresh:
            return
        # The writer is gone - recover the manifest straight from the log.
        try:
            text = log.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        matches = re.findall(r"^BEGIN_MANIFEST\s*\n(.*?)^END_MANIFEST\s*$",
                             text, re.M | re.S)
        if matches and overlay.get("generation_started_at"):
            overlay = dict(overlay)
            overlay["draft_started_at"] = overlay["generation_started_at"]
            _save_spool(overlay, spool)
            (spool / "draft.toml").write_text(matches[-1].strip() + "\n",
                                             encoding="utf-8")
            draft_ok = True
    gid = overlay.get("gen_id")
    if draft_ok:
        _GENERATING.add(gid or rid)
        try:
            _publish_from_spool(rid, draft.read_text(encoding="utf-8"),
                                spool, requests_dir, manifests_dir,
                                runs_dir, gen_id=gid)
        except Exception as exc:
            LOG.exception("spool drain publish failed for %s", rid)
            cur = _spool_rec(rid)
            # Only write the failure while the spool still belongs to the
            # attempt being drained - a Regenerate may have claimed it.
            if (cur or overlay).get("gen_id") == gid:
                cur = dict(cur or overlay)
                cur.update(
                    status="error",
                    error=f"Recovered draft failed to publish: {exc}"[:300])
                _save_spool(cur, spool)
                _flush_spool(rid, spool, requests_dir, runs_dir,
                             gen_id=gid)
        finally:
            _GENERATING.discard(gid or rid)
        return
    # Interrupted mid-planning (or the draft belongs to a superseded
    # generation): land an error; the artifacts, including any draft,
    # are preserved under data/requests by the flush.
    if overlay.get("status") == "generating" and \
            (_spool_rec(rid) or {}).get("gen_id") == gid:
        overlay = dict(overlay)
        overlay.update(
            status="error",
            error="Planning was interrupted by a server restart - the "
                  "draft and log were preserved; regenerate to retry.")
        _save_spool(overlay, spool)
    _flush_spool(rid, spool, requests_dir, runs_dir, gen_id=gid)


def _auto_drain() -> None:
    """Throttled spool recovery for a long-lived serve - parked
    generations left by a crash/restart get replayed without anyone
    pressing Regenerate."""
    global _DRAIN_TRIED
    if time.time() - _DRAIN_TRIED < _DRAIN_POLL_S:
        return
    _DRAIN_TRIED = time.time()
    try:
        drain_planner_spool()
    except Exception:
        LOG.exception("planner spool drain failed")


def start_generation(request_id: str,
                     requests_dir: Path = REQUESTS_DIR,
                     manifests_dir: Path = MANIFESTS_DIR,
                     runs_dir: Path = RUNS_DIR,
                     devin_argv: list[str] | None = None,
                     background: bool = True) -> dict:
    with REQUEST_LOCK:
        # NO _assert_unwatched here: the planner runs in its own tempdir
        # and every byte it produces lands in the spool (outside every
        # watched cwd), so a live worker or an in-progress merge must not
        # block generation. Only the deferred publish waits on the repo.
        rec = _read_request(request_id, requests_dir)
        if _request_status(rec, runs_dir) not in (
                "draft", "ready", "error", "launch_failed", "questions"):
            raise RequestError("Only drafts or unrun proposals can be generated", 409)
        # A leftover sentinel from a stopped attempt would kill this new
        # pass on the first wait_worker poll - clear before marking.
        gen_stop_flag(request_id).unlink(missing_ok=True)
        gen_id = secrets.token_hex(4)
        # A manual (Re)generate starts planning from scratch - any parked
        # questionnaire and its collected answers are dropped on purpose.
        rec.update(status="generating", error=None, gen_id=gen_id,
                   generation_started_at=now_iso(), pending_questions=[],
                   qa=[], qa_rounds=0)
        rec.pop("draft_started_at", None)
        # The overlay - not the request file - marks the generation: the
        # file is only safe to write once the workspace is unwatched.
        _save_spool(rec, _spool_dir(request_id))
        _GENERATING.add(gen_id)
    args = (request_id, requests_dir, manifests_dir, runs_dir,
            devin_argv or ["devin"], gen_id)
    if background:
        threading.Thread(target=_finish_generation, args=args,
                         daemon=True).start()
        return rec
    _finish_generation(*args)
    return request_details(request_id, requests_dir, manifests_dir)


def answer_request(request_id: str, payload: dict,
                   requests_dir: Path = REQUESTS_DIR,
                   manifests_dir: Path = MANIFESTS_DIR,
                   runs_dir: Path = RUNS_DIR,
                   devin_argv: list[str] | None = None,
                   background: bool = True) -> dict:
    """Record one answer (or skip_all) to the parked planner questionnaire.
    Once pending_questions is empty the refine pass starts automatically -
    same _finish_generation path, except rec['qa'] survives (a manual
    Regenerate via start_generation clears it instead)."""
    with REQUEST_LOCK:
        _assert_unwatched(runs_dir, requests_dir)
        rec = _read_request(request_id, requests_dir)
        if rec.get("status") != "questions":
            raise RequestError("This request is not waiting on answers", 409)
        pend = list(rec.get("pending_questions") or [])
        if not pend:
            raise RequestError("No questions are pending", 409)
        rec["qa"] = list(rec.get("qa") or [])
        if payload.get("skip_all") is True:
            for q in pend:
                rec["qa"].append({"qid": q["qid"], "header": q["header"],
                                  "question": q["question"], "answer": [],
                                  "text": "", "skipped": True})
            pend = []
        else:
            qid = payload.get("qid")
            q = next((x for x in pend if x.get("qid") == qid), None)
            if q is None:
                raise RequestError("That question is not pending", 409)
            skipped = bool(payload.get("skipped"))
            sel = payload.get("selected") or []
            if not isinstance(sel, list) or any(
                    not isinstance(s, str) for s in sel):
                raise RequestError("selected must be a list of labels")
            labels = [o["label"] for o in q.get("options") or []]
            if any(s not in labels for s in sel):
                raise RequestError("Unknown option label")
            if len(sel) > 1 and not q.get("multi_select"):
                raise RequestError("This question takes a single option")
            text = str(payload.get("text") or "").strip()[:2000]
            if not skipped and not sel and not text:
                raise RequestError("Pick an option, write an answer, "
                                   "or skip")
            rec["qa"].append({"qid": qid, "header": q["header"],
                              "question": q["question"],
                              "answer": [] if skipped else sel,
                              "text": "" if skipped else text,
                              "skipped": skipped})
            pend = [x for x in pend if x["qid"] != qid]
        rec["pending_questions"] = pend
        if pend:
            _save_request(rec, requests_dir)
            return dict(rec)
        gen_stop_flag(request_id).unlink(missing_ok=True)
        gen_id = secrets.token_hex(4)
        rec.update(status="generating", error=None, gen_id=gen_id,
                   generation_started_at=now_iso())
        _save_request(rec, requests_dir)
        _GENERATING.add(gen_id)
    args = (request_id, requests_dir, manifests_dir, runs_dir,
            devin_argv or ["devin"], gen_id)
    if background:
        threading.Thread(target=_finish_generation, args=args,
                         daemon=True).start()
        return dict(rec)
    _finish_generation(*args)
    return request_details(request_id, requests_dir, manifests_dir)


def stop_generation(request_id: str,
                    requests_dir: Path = REQUESTS_DIR,
                    runs_dir: Path = RUNS_DIR) -> dict:
    """Dashboard 'Stop proposal': cancel the in-flight planning pass and
    hand the request back to editing.

    The sentinel does the scoped kill - only this request's planner polls
    it, so unrelated generations and runs are untouched - and the live
    Popen registry makes it immediate rather than next-poll. The record is
    restored to the last editable state: 'ready' when a previous proposal
    exists (stale flag and TOML untouched), 'draft' otherwise; inputs and
    collected Q&A were never modified by generation, so nothing else needs
    restoring. _finish_generation's gen_id check keeps late output or an
    error callback from overwriting this state, so the user can edit and
    Generate again at once.

    A stop on an orphaned 'generating' record (server died mid-plan, no
    thread left) works the same way and clears the stuck stage early; a
    stop after the attempt already finished 409s like stop_run on an ended
    run."""
    with REQUEST_LOCK:
        _assert_unwatched(runs_dir, requests_dir)
        rec = _read_request(request_id, requests_dir)
        if _request_status(rec, runs_dir) != "generating":
            raise RequestError(
                "This request is not preparing a proposal", 409)
        _flag_raise(gen_stop_flag(request_id).name)
        rec["status"] = "ready" if rec.get("manifest") else "draft"
        rec["error"] = None
        rec["generation_stopped_at"] = now_iso()
        _save_request(rec, requests_dir)
        proc = _GEN_PROCS.get(request_id)
    if proc is not None:
        kill_tree(proc)
    return dict(rec)


def _request_run_dir(request_id: str, runs_dir: Path) -> Path | None:
    for sj in sorted(runs_dir.glob("*/state.json"), reverse=True):
        try:
            if json.loads(sj.read_text(encoding="utf-8")).get(
                    "request_id") == request_id:
                return sj.parent
        except (OSError, ValueError):
            continue
    return None


def _request_status(rec: dict, runs_dir: Path) -> str:
    if _request_run_dir(rec["id"], runs_dir):
        return "launched"
    # A spooled overlay outranks the request file until the deferred
    # publish/flush lands - after a crash it is the only place the
    # interrupted generation's truth survives.
    eff = _spool_rec(rec["id"]) or rec
    if eff.get("status") == "generating":
        if eff.get("gen_id") in _GENERATING:
            return "generating"
        age = time.time() - (_iso_ts(eff.get("generation_started_at"))
                             or time.time())
        return "generating" if age <= 660 else "error"
    status = eff.get("status") or "draft"
    if status != "launching":
        return status
    age = time.time() - (_iso_ts(rec.get("launched_at")) or time.time())
    if age > 90 and (rec["id"] not in _LAUNCH_PIDS or not pid_alive(
            _LAUNCH_PIDS[rec["id"]], rec.get("launched_at"))):
        return "launch_failed"
    return "launching"


def _detached_kwargs() -> dict:
    """Process-group isolation for children that must outlive their
    spawner's console: CREATE_NEW_PROCESS_GROUP (nt) / start_new_session
    (posix) keeps the child out of the server's Ctrl+C/SIGHUP dispatch -
    closing or interrupting `serve` leaves runs and planners alone (trap
    15); only the explicit stop endpoints kill run work."""
    kw = dict(hidden_subprocess_kwargs())
    if os.name == "nt":
        kw["creationflags"] = (kw.get("creationflags", 0)
                               | subprocess.CREATE_NEW_PROCESS_GROUP)
    else:
        kw["start_new_session"] = True
    return kw


def _launch_process(cmd: list[str], log_path: Path) -> int:
    """Spawn a conductor `run`/`resume` subprocess from the dashboard."""
    with open(log_path, "wb") as out:
        proc = subprocess.Popen(cmd, cwd=str(ROOT), stdout=out,
                                stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                env=clean_devin_env(), **_detached_kwargs())
    return proc.pid


def launch_request(request_id: str,
                   requests_dir: Path = REQUESTS_DIR,
                   manifests_dir: Path = MANIFESTS_DIR,
                   runs_dir: Path = RUNS_DIR,
                   devin_argv: list[str] | None = None) -> dict:
    with REQUEST_LOCK:
        _assert_unwatched(runs_dir, requests_dir)
        rec = _read_request(request_id, requests_dir)
        if _request_status(rec, runs_dir) not in ("ready", "launch_failed") \
                or rec.get("stale"):
            raise RequestError("Review and save a current proposal before "
                               "launching; already launched requests cannot "
                               "be started again", 409)
        p = _manifest_path(rec, manifests_dir)
        try:
            cfg = validate_proposal(p.read_text(encoding="utf-8"),
                                    manifests_dir)
        except OSError as exc:
            raise RequestError("Proposal TOML is missing", 409) from exc
        projects = load_projects()
        targets = {resolve_project(s, projects, manifests_dir)[0].resolve()
                   for s in cfg.steps}
        for sj in runs_dir.glob("*/state.json"):
            try:
                st = json.loads(sj.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if any(not step.get("branch") and step_live(step, driver_alive(st)) and
                   Path(step.get("work_dir") or step.get("project") or "").resolve()
                   in targets for step in (st.get("steps") or {}).values()):
                raise RequestError("A live agent already works in this project; "
                                   "wait or isolate work in a worktree", 409)
        if ROOT.resolve() in targets and any(
                _request_status(r, runs_dir) == "generating" for f in
                requests_dir.glob("*.json") for r in [json.loads(
                    f.read_text(encoding="utf-8"))]):
            raise RequestError("Wait for proposal generation to finish before "
                               "running work in agent-conductor", 409)
        rec.update(status="launching", launched_at=now_iso(), error=None)
        _save_request(rec, requests_dir)
        cmd = [sys.executable, str(ROOT / "conductor.py"), "run", str(p),
               "--request-id", request_id, "--runs-dir", str(runs_dir)]
        if devin_argv and devin_argv != ["devin"]:
            cmd += ["--devin", shlex.join(devin_argv)]
        try:
            _LAUNCH_PIDS[request_id] = _launch_process(
                cmd, requests_dir / f"{request_id}.run.log")
        except OSError as exc:
            rec.update(status="launch_failed", error=str(exc)[:300])
            _save_request(rec, requests_dir)
            raise RequestError("Could not start the conductor process", 500) from exc
        return dict(rec)


def resume_run(name: str, runs_dir: Path = RUNS_DIR,
               devin_argv: list[str] | None = None) -> dict:
    """Re-enter an ended run: `conductor.py run <manifest> --resume <dir>`
    skips done steps and re-runs the rest, in fresh sessions with fresh
    timeout budgets. Refused while anything in the run is still live."""
    if not RUN_DIR_RE.fullmatch(name):
        raise RequestError("Invalid run dir", 404)
    d = (runs_dir / name).resolve()
    if d.parent != runs_dir.resolve() or not (d / "state.json").is_file():
        raise RequestError("Run not found", 404)
    try:
        st = json.loads((d / "state.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RequestError("Cannot read run state", 500) from exc
    if any(step_live(r, driver_alive(st)) for r in (st.get("steps") or {}).values()):
        raise RequestError("This run still has a live agent - wait for it "
                           "or let it die first", 409)
    pid = _RESUME_PIDS.get(name)
    if pid and pid_alive(pid):
        raise RequestError("A resume is already starting for this run", 409)
    manifest = st.get("manifest")
    if not manifest or not Path(manifest).is_file():
        raise RequestError("Run's manifest is gone; resume from the CLI: "
                           "conductor.py run <manifest> --resume", 409)
    cmd = [sys.executable, str(ROOT / "conductor.py"), "run", manifest,
           "--resume", str(d), "--runs-dir", str(runs_dir)]
    # The run's own worker binary wins over the server's env default - a
    # run launched with --devin <mock> must resume on the same binary.
    dv = (st.get("devin_argv")
          or (shlex.split(st["devin"]) if st.get("devin") else None)
          or devin_argv)
    if dv and dv != ["devin"]:
        cmd += ["--devin", shlex.join(dv)]
    clear_run_flags(d)     # else a stopped run re-stops instantly
    try:
        _RESUME_PIDS[name] = _launch_process(cmd, d / "resume.launch.log")
    except OSError as exc:
        raise RequestError("Could not start the conductor process", 500) from exc
    return {"resumed": name, "pid": _RESUME_PIDS[name]}


_MERGE_BUSY: set[str] = set()
_MERGE_LOCK = threading.Lock()


def merge_run(name: str, runs_dir: Path = RUNS_DIR,
              devin_argv: list[str] | None = None) -> dict:
    """Merge a finished run's pending worktree branches back into their main
    checkouts - the dashboard's manual counterpart to the post-wave merge.
    Same rules: a clean merge lands and removes the worktree; a conflict
    parks it (or, when the step has merge_resolver, spawns a resolve-*
    session). Refused while the run has a live step, an in-flight resume,
    or another merge already running - a resolve-* session can take
    minutes and two callers must never race the same merge."""
    with _MERGE_LOCK:
        if name in _MERGE_BUSY:
            raise RequestError("A merge is already running for this run",
                               409)
        _MERGE_BUSY.add(name)
    try:
        return _merge_run(name, runs_dir, devin_argv)
    finally:
        with _MERGE_LOCK:
            _MERGE_BUSY.discard(name)


def _merge_run(name: str, runs_dir: Path,
               devin_argv: list[str] | None) -> dict:
    if not RUN_DIR_RE.fullmatch(name):
        raise RequestError("Invalid run dir", 404)
    d = (runs_dir / name).resolve()
    if d.parent != runs_dir.resolve() or not (d / "state.json").is_file():
        raise RequestError("Run not found", 404)
    try:
        st = json.loads((d / "state.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RequestError("Cannot read run state", 500) from exc
    steps = st.get("steps") or {}
    if any(step_live(r, driver_alive(st)) for r in steps.values()):
        raise RequestError("This run still has a live agent - wait for it "
                           "or let it die first", 409)
    pid = _RESUME_PIDS.get(name)
    if pid and pid_alive(pid):
        raise RequestError("A resume is already running for this run", 409)
    pending = {n for n, r in steps.items()
               if r.get("branch") and r.get("merged") is not True}
    if not pending:
        return {"dir": name, "merged": [], "conflicted": [], "skipped": []}
    manifest = st.get("manifest")
    if not manifest or not Path(manifest).is_file():
        raise RequestError("Run's manifest is gone; merge the branches "
                           "by hand from each kept worktree", 409)
    cfg = load_manifest(Path(manifest))
    projects = load_projects()
    results = {"dir": name, "merged": [], "conflicted": [], "skipped": []}
    for step in cfg.steps:                    # manifest order, like cmd_run
        rec = steps.get(step.name) or {}
        branch = rec.get("branch")
        if not branch or rec.get("merged") is True:
            continue
        try:
            repo_dir, slug = resolve_project(step, projects,
                                             Path(manifest).parent)
        except ManifestError as exc:
            rec["note"] = f"merge skipped: {exc}"
            results["skipped"].append(step.name)
            continue
        wt = (Path(rec["work_dir"]) if rec.get("work_dir")
              else runs_dir.parent / "worktrees" / d.name / step.name)
        if _git(repo_dir, "rev-parse", "-q", "--verify",
                branch).returncode != 0:
            rec["note"] = "merge skipped: branch is gone"
            results["skipped"].append(step.name)
            continue
        # Clear a stale conductor merge left by a dead run before trying
        # ours; a human's merge in progress makes worktree_merge refuse.
        if abort_own_merge(repo_dir):
            log_event(d, event="merge_abort_stale", step=step.name,
                      project=str(repo_dir))
        ok = worktree_merge(repo_dir, wt, branch)
        if not ok and step.merge_resolver:
            dv = (st.get("devin_argv")
                  or (shlex.split(st["devin"]) if st.get("devin") else None)
                  or devin_argv or ["devin"])
            ok = resolve_merge_conflict(step, slug, repo_dir, wt, branch,
                                        cfg, d, dv, st)
        if ok:
            rec.update(status="done" if rec.get("status") == "failed"
                       else rec.get("status"),
                       merged=True,
                       note="merged via dashboard "
                            f"(was {rec.get('status')})")
            results["merged"].append(step.name)
        else:
            rec.update(merged=False,
                       note="merge still conflicts; the worktree is kept "
                            f"at {wt} for manual resolution")
            results["conflicted"].append(step.name)
    pending -= set(results["merged"])
    for n in pending - set(results["conflicted"]) - set(results["skipped"]):
        # recorded branch belongs to a step the manifest no longer names
        steps[n]["note"] = "merge skipped: step not in the manifest anymore"
        results["skipped"].append(n)
    save_state(d, st)
    log_event(d, event="merge_manual", merged=results["merged"],
              conflicted=results["conflicted"], skipped=results["skipped"])
    try:
        (runs_dir.parent / "worktrees" / d.name).rmdir()
    except OSError:
        pass
    return results


def _run_dir_checked(name: str, runs_dir: Path) -> tuple[Path, dict]:
    """Resolve + load a run dir for dashboard mutation endpoints."""
    if not RUN_DIR_RE.fullmatch(name):
        raise RequestError("Invalid run dir", 404)
    d = (runs_dir / name).resolve()
    if d.parent != runs_dir.resolve() or not (d / "state.json").is_file():
        raise RequestError("Run not found", 404)
    try:
        return d, json.loads((d / "state.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RequestError("Cannot read run state", 500) from exc


def _kill_pid(pid: int | None, since: str | None) -> None:
    """taskkill /T /F a recorded worker pid, guarded by the same
    creation-time check pid_alive uses (a recycled pid is never ours)."""
    if not pid or not pid_alive(pid, since):
        return
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                       capture_output=True, **hidden_subprocess_kwargs())
    else:
        try:
            os.kill(pid, 15)
        except OSError:
            pass


def stop_step(run_name: str, step_name: str,
              runs_dir: Path = RUNS_DIR) -> dict:
    """Dashboard 'Stop this step': drop a per-step sentinel, then - when
    the pid is visible - tree-kill it now rather than waiting out the
    poll. The running conductor notices either way and records 'stopped';
    a queued-but-unspawned step lands 'skipped'. The RUN proceeds to the
    next queued step - only run-level controls halt the pipeline."""
    if not RUN_DIR_RE.fullmatch(step_name):
        raise RequestError("Invalid step name", 404)
    d, st = _run_dir_checked(run_name, runs_dir)
    rec = (st.get("steps") or {}).get(step_name)
    if rec is None:
        raise RequestError("Step not in this run", 404)
    if rec.get("status") not in ("running", "queued"):
        raise RequestError(f"Step is {rec.get('status')}, not running", 409)
    if rec.get("status") == "running" and not step_live(rec, driver_alive(st)):
        raise RequestError("This step is already dead (its record is "
                           "stale - sweep will mark it lost)", 409)
    for f in stop_flags_for(d, st, step_name):
        _flag_raise(f.name)
    if rec.get("pid"):
        _kill_pid(rec["pid"], rec.get("started_at"))
    # NB no log_event here: while a step is live, run_dir can sit inside
    # the worker's watched cwd (self-hosted run) - writing events.jsonl
    # would itself trip user-edit-awareness (trap 5). The conductor logs
    # step_stopped/run_stop itself.
    return {"stopping": step_name}


def skip_step(run_name: str, step_name: str, body: dict,
              runs_dir: Path = RUNS_DIR) -> dict:
    """Dashboard 'Skip step'/'Unskip step': a persistent per-step sentinel
    the conductor checks right before spawn. Skipping keeps the step in
    the run but it never launches while marked ('skipped' when its turn
    comes); unskipping restores it in its original queue position - if
    the turn already passed, it re-runs on --resume like any skipped
    step. The flag survives resume on purpose (clear_run_flags leaves
    it), so a skip is undone only by an explicit Unskip."""
    if not RUN_DIR_RE.fullmatch(step_name):
        raise RequestError("Invalid step name", 404)
    d, st = _run_dir_checked(run_name, runs_dir)
    steps = st.get("steps") or {}
    rec = steps.get(step_name)
    if rec is None and step_name not in _manifest_step_names(st):
        raise RequestError("Step not in this run", 404)
    if body.get("skip", True):
        status = (rec or {}).get("status")
        if status == "running":
            raise RequestError(
                "Step already launched - use Stop this step to end it", 409)
        if status not in (None, "queued", "skipped", "lost"):
            raise RequestError(
                f"Step already finished ({status}) - skipping only "
                "works on steps that have not run", 409)
        _flag_raise(skip_flag(d, step_name).name)
        # Same trap-5 rule as stop_step: flag files only, never state.json.
        return {"skipped": step_name}
    skip_flag(d, step_name).unlink(missing_ok=True)
    return {"unskipped": step_name,
            "turn_passed": (rec or {}).get("status") == "skipped"}


_MANIFEST_CACHE: dict[str, tuple[float, "Manifest"]] = {}


def _manifest_cached(path_str: str) -> "Manifest | None":
    """load_manifest keyed on (path, mtime) - collect_dashboard calls it
    once per run per refresh and manifests are small, but the parse is
    still pointless work when nothing changed."""
    try:
        mt = Path(path_str).stat().st_mtime
    except OSError:
        return None
    ent = _MANIFEST_CACHE.get(path_str)
    if ent and ent[0] == mt:
        return ent[1]
    try:
        cfg = load_manifest(Path(path_str))
    except (OSError, ManifestError, ValueError):
        # TOMLDecodeError is a ValueError - a corrupt manifest just means
        # no pending rows, never a dead dashboard refresh.
        return None
    _MANIFEST_CACHE[path_str] = (mt, cfg)
    return cfg


def _manifest_step_names(st: dict) -> set[str]:
    """Step names declared by the run's manifest - for validating a
    Skip on a step that has no state.json record yet."""
    cfg = _manifest_cached(st.get("manifest") or "")
    return {s.name for s in cfg.steps} if cfg else set()


def stop_run(run_name: str, body: dict,
             runs_dir: Path = RUNS_DIR) -> dict:
    """Dashboard run-level controls.

    {"pause": true}  - 'Pause run': graceful hold. In-flight steps run to
    completion, nothing new launches, the run records aborted='paused'
    (the deliberate-hold / machine-restart case; resume continues it).
    {"drain": true}  - legacy alias of the same graceful halt, recorded
    aborted='drained'. Otherwise - 'Stop run now': every live step's
    process tree is killed now, nothing new launches, aborted='stopped'.
    The wave loop honors the sentinels between waves, so no later step
    launches regardless of on_failure."""
    d, st = _run_dir_checked(run_name, runs_dir)
    if st.get("ended_at"):
        raise RequestError("This run already ended", 409)
    if body.get("pause"):
        for f in pause_flags_for(d, st):
            _flag_raise(f.name)
        if st.get("flags") != "v2":
            # Old-code drivers only poll stop/drain at the wave boundary -
            # the graceful-hold shape they understand is drain.
            for f in drain_flags_for(d, st):
                _flag_raise(f.name)
        live = [n for n, r in (st.get("steps") or {}).items()
                if r.get("status") == "running" and step_live(r, driver_alive(st))]
        return {"pausing": live}
    if body.get("drain"):
        for f in drain_flags_for(d, st):
            _flag_raise(f.name)
        return {"draining": run_name}
    for f in stop_flags_for(d, st):
        _flag_raise(f.name)
    live = [n for n, r in (st.get("steps") or {}).items()
            if r.get("status") == "running" and step_live(r, driver_alive(st))]
    for n in live:                       # immediate kill, not just the poll
        _kill_pid((st["steps"][n]).get("pid"),
                  st["steps"][n].get("started_at"))
        for f in stop_flags_for(d, st, n):
            _flag_raise(f.name)
    return {"stopping": live}


def set_run_archived(run_name: str, body: dict,
                     runs_dir: Path = RUNS_DIR) -> dict:
    """Dashboard 'Archive run'/'Unarchive': the manual half of the
    auto-archive pass. The stamp is only a default-view filter - the run
    dir stays on disk and resume/merge keep working (a resumed run clears
    the stamp itself). Refused while a step is live: archiving is for
    finished work, and hiding live work would hide its controls."""
    d, st = _run_dir_checked(run_name, runs_dir)
    archived = body.get("archived") is not False    # default: archive
    if archived and any(
            step_live(r, driver_alive(st)) for r in (st.get("steps") or {}).values()):
        raise RequestError("This run still has a live agent - wait for it "
                           "or let it die first", 409)
    _assert_unwatched(runs_dir, d)
    if archived:
        st["archived"] = now_iso()
    else:
        st.pop("archived", None)
    save_state(d, st)
    return {"run": run_name, "archived": archived}


def delete_run(run_name: str, body: dict,
               runs_dir: Path = RUNS_DIR) -> dict:
    """Dashboard 'Delete run': remove the run's whole on-disk record -
    state, events, prompts, stdout logs, ATIF exports. Requires
    {"confirm": true} and is refused while any step is live or a
    resume/merge is in flight (same gates resume_run/merge_run use).
    The devin sessions it spawned are NOT deleted - once the state.json
    naming them is gone they leave the `sessions` whitelist and become
    manual `devin rm <id>` cleanup, same as trap-13 orphans. Parked
    worktree branches outlive the record."""
    if body.get("confirm") is not True:
        raise RequestError("Confirm delete explicitly")
    d, st = _run_dir_checked(run_name, runs_dir)
    if any(step_live(r, driver_alive(st)) for r in (st.get("steps") or {}).values()):
        raise RequestError("This run still has a live agent - wait for it "
                           "or let it die first", 409)
    pid = _RESUME_PIDS.get(run_name)
    if pid and pid_alive(pid):
        raise RequestError("A resume is already running for this run", 409)
    with _MERGE_LOCK:
        if run_name in _MERGE_BUSY:
            raise RequestError("A merge is already running for this run",
                               409)
    _assert_unwatched(runs_dir, d)
    try:
        shutil.rmtree(d)
    except OSError as exc:
        raise RequestError(f"Could not delete the run dir: {exc}",
                           500) from exc
    # Sentinels in %TEMP% key on the run's resolved path - sweep the
    # path-keyed set (stop/skip/prompt/rnote/timeout) plus the bare
    # run-level legacy names. Step-level legacy names (stop-<run>-<step>)
    # can't be told apart from a same-named run's keyed flags - left in
    # place, they are inert empty files.
    for f in _flag_dir().glob(f"*{_flag_key(d)}*"):
        try:
            f.unlink()
        except OSError:
            pass
    for prefix in ("stop", "drain", "pause"):
        try:
            (_flag_dir() / f"{prefix}-{_safe_name(d.name)}").unlink(
                missing_ok=True)
        except OSError:
            pass
    return {"deleted": run_name}


def set_step_timeout(run_name: str, step_name: str, body: dict,
                     runs_dir: Path = RUNS_DIR) -> dict:
    """Dashboard timeout editor: a minutes value in a tempdir sentinel the
    conductor's wait_worker polls, so the deadline moves live - grow it
    past the default, or shrink it under elapsed to end the step early.
    Applies to the current attempt and every retry of this step."""
    if not RUN_DIR_RE.fullmatch(step_name):
        raise RequestError("Invalid step name", 404)
    d, st = _run_dir_checked(run_name, runs_dir)
    rec = (st.get("steps") or {}).get(step_name)
    if rec is None:
        raise RequestError("Step not in this run", 404)
    if rec.get("status") not in ("running", "queued"):
        raise RequestError(
            f"Step is {rec.get('status')} - only live steps take a "
            "timeout override (edit the manifest for future runs)", 409)
    try:
        minutes = float(body.get("minutes"))
    except (TypeError, ValueError):
        raise RequestError("minutes must be a number") from None
    if not 1 <= minutes <= 1440:
        raise RequestError("minutes must be 1-1440", 422)
    try:
        _flag_dir().mkdir(exist_ok=True)
        timeout_flag(d, step_name).write_text(
            str(minutes), encoding="utf-8")
    except OSError as exc:
        raise RequestError(f"Could not write the timeout flag: {exc}",
                           500) from exc
    return {"timeout_min": minutes}


def set_step_prompt(run_name: str, step_name: str, body: dict,
                    runs_dir: Path = RUNS_DIR) -> dict:
    """Dashboard 'Edit prompt': the replacement task body lives in a
    tempdir sentinel that run_step reads just before rendering - the
    manifest file stays untouched and the dispatched prompts/*.md records
    exactly what was sent. Only a step that never dispatched is editable
    (pending manifest steps have no record; 'queued'/'skipped' records
    are pre-spawn); once the prompt is dispatched the control locks and
    resume notes are the follow-up channel. {"prompt": null} reverts to
    the manifest text."""
    if not RUN_DIR_RE.fullmatch(step_name):
        raise RequestError("Invalid step name", 404)
    d, st = _run_dir_checked(run_name, runs_dir)
    rec = (st.get("steps") or {}).get(step_name)
    if rec is None and step_name not in _manifest_step_names(st):
        raise RequestError("Step not in this run", 404)
    if rec is not None and rec.get("status") not in ("queued", "skipped"):
        raise RequestError(
            f"Step is {rec.get('status')} - its prompt was already "
            "dispatched; leave resume notes instead", 409)
    flag = prompt_flag(d, step_name)
    try:
        _flag_dir().mkdir(exist_ok=True)
        if body.get("prompt") is None:
            flag.unlink(missing_ok=True)
            return {"prompt_override": None}
        if not isinstance(body["prompt"], str):
            raise RequestError("prompt must be text")
        if not 1 <= len(body["prompt"]) <= 100_000:
            raise RequestError("prompt must be 1-100,000 characters", 422)
        flag.write_text(body["prompt"], encoding="utf-8")
    except OSError as exc:
        raise RequestError(f"Could not write the prompt flag: {exc}",
                           500) from exc
    return {"prompt_override": body["prompt"]}


def set_step_notes(run_name: str, step_name: str, body: dict,
                   runs_dir: Path = RUNS_DIR) -> dict:
    """Dashboard 'resume notes': operator text in a tempdir sentinel that
    run_step appends to the step's next dispatched prompt - the original
    prompt itself is never edited post-dispatch. For steps that ran and
    will re-run (stopped/failed/timed_out/lost/blocked/partial); a queued
    orphan counts too, though a never-dispatched step wants Edit prompt.
    {"notes": ""} clears the field."""
    if not RUN_DIR_RE.fullmatch(step_name):
        raise RequestError("Invalid step name", 404)
    d, st = _run_dir_checked(run_name, runs_dir)
    rec = (st.get("steps") or {}).get(step_name)
    if rec is None:
        raise RequestError("Step not in this run", 404)
    eff = effective_statuses(st).get(step_name, rec.get("status"))
    if eff not in ("blocked", "partial", "failed", "timed_out",
                   "stopped", "lost"):
        raise RequestError(
            f"Step is {eff} - resume notes apply to steps that already "
            "ran; a pending step takes Edit prompt instead", 409)
    flag = rnote_flag(d, step_name)
    try:
        _flag_dir().mkdir(exist_ok=True)
        if body.get("notes") in (None, ""):
            flag.unlink(missing_ok=True)
            return {"resume_notes": None}
        if not isinstance(body["notes"], str):
            raise RequestError("notes must be text")
        if len(body["notes"]) > 20_000:
            raise RequestError("notes must be at most 20,000 characters",
                               422)
        flag.write_text(body["notes"], encoding="utf-8")
    except OSError as exc:
        raise RequestError(f"Could not write the notes flag: {exc}",
                           500) from exc
    return {"resume_notes": body["notes"]}


def discard_request(request_id: str,
                    requests_dir: Path = REQUESTS_DIR,
                    manifests_dir: Path = MANIFESTS_DIR,
                    runs_dir: Path = RUNS_DIR) -> dict:
    with REQUEST_LOCK:
        _assert_unwatched(runs_dir, requests_dir, manifests_dir)
        rec = _read_request(request_id, requests_dir)
        if _request_status(rec, runs_dir) not in (
                "draft", "ready", "error", "launch_failed", "questions"):
            raise RequestError("A running or launched proposal cannot be discarded",
                               409)
        if rec.get("manifest"):
            _manifest_path(rec, manifests_dir).unlink(missing_ok=True)
        rec.update(status="discarded", proposed_steps=[], manifest=None)
        _save_request(rec, requests_dir)
        return dict(rec)


# ---------------------------------------------------------------- dashboard

SERVE_PORT = 8773    # registered in C:\Projects\AGENTS.md

_STATUS_COLOR = {"done": "#3fb950", "partial": "#d29922", "blocked": "#d29922",
                 "failed": "#f85149", "timed_out": "#f0883e",
                 "running": "#58a6ff", "queued": "#8b949e",
                 "waiting": "#8b949e", "skipped": "#6e7681",
                 "planned": "#bc8cff", "ready": "#bc8cff",
                 "generating": "#bc8cff", "draft": "#d29922",
                 "launching": "#58a6ff", "launch_failed": "#f85149",
                 "lost": "#6e7681"}

_AUTH_CACHE = [0.0, "unknown"]        # [checked_at, label]
_SESS_CACHE = [0.0, {}]               # [checked_at, {work_dir: set|None}]
_MODELS_CACHE = [0.0, {}]             # [checked_at, {family: [suffixes]}]
_EFFORT_ORDER = ("", "min", "low", "medium", "high", "xhigh", "max")


def _effort_rank(suffix: str) -> tuple[int, str]:
    """Sort effort suffixes by level ('low-fast' with 'low'), unknowns last."""
    base = suffix.partition("-")[0]
    try:
        return (_EFFORT_ORDER.index(base), suffix)
    except ValueError:
        return (len(_EFFORT_ORDER), suffix)


def auth_status(devin_argv: list[str]) -> str:
    """`devin auth status`, cached 2min so the 2s dashboard poll doesn't
    spawn a probe per refresh."""
    if time.time() - _AUTH_CACHE[0] < 120:
        return _AUTH_CACHE[1]
    try:
        out = subprocess.run(devin_argv + ["auth", "status"],
                             capture_output=True, text=True,
                             encoding="utf-8", errors="replace", timeout=30,
                             env=clean_devin_env(),
                             **hidden_subprocess_kwargs())
        txt = (out.stdout + out.stderr).lower()
        label = ("NOT LOGGED IN" if out.returncode != 0
                 or "not logged in" in txt else "ok")
    except (OSError, subprocess.SubprocessError):
        label = "devin missing"
    _AUTH_CACHE[0], _AUTH_CACHE[1] = time.time(), label
    return label


def model_options(devin_argv: list[str]) -> dict[str, list[str]]:
    """`devin models list` parsed into {family slug: [effort suffixes]},
    cached 10min - the suffix list is what `-<effort>` may legitimately be
    fused onto the family (e.g. swe-2 -> high/medium/max). A bare variant
    id yields the '' suffix meaning "no effort flag". Empty when the CLI is
    unavailable or logged out - the dashboard then falls back to a fixed
    list."""
    if time.time() - _MODELS_CACHE[0] < 600:
        return _MODELS_CACHE[1]
    fams: dict[str, list[str]] = {}
    try:
        out = subprocess.run(devin_argv + ["models", "list"],
                             capture_output=True, text=True,
                             encoding="utf-8", errors="replace", timeout=30,
                             env=clean_devin_env(),
                             **hidden_subprocess_kwargs())
        if out.returncode == 0:
            fam = None
            for line in out.stdout.splitlines():
                m = re.match(r"^\S.*\(([a-z0-9][a-z0-9._-]*)\)\s*$", line)
                if m:
                    # variant ids dash where the printed family slug dots
                    # (swe-1.7-lightning -> swe-1-7-lightning-*)
                    fam = m.group(1).replace(".", "-")
                    fams.setdefault(fam, [])
                    continue
                if fam is None:
                    continue
                v = re.match(r"^  ([a-z0-9][a-z0-9._-]*)\s{2,}", line)
                if v:
                    vid = v.group(1)
                    suffix = (vid[len(fam) + 1:]
                              if vid.startswith(fam + "-") else "")
                    if suffix not in fams[fam]:
                        fams[fam].append(suffix)
            for suffixes in fams.values():
                suffixes.sort(key=_effort_rank)
    except (OSError, subprocess.SubprocessError):
        pass
    _MODELS_CACHE[0], _MODELS_CACHE[1] = time.time(), fams
    return fams


# Per-token rates: docs.devin.ai publishes the chat-models table as an
# embedded JS array (modelCostData) in the page's MDX source - valid JSON
# once extracted. Fetched in the background, mirrored to
# <data>/pricing.json so restarts/offline still price old rows. We use the
# Self-serve tab's tier (TEAMS_TIER_PRO).
_PRICES_URL = "https://docs.devin.ai/desktop/chat/models.md"
_PRICES_TIER = "TEAMS_TIER_PRO"
_PRICES_TTL_S = 6 * 3600          # refetch a good table ~4x/day
_PRICES_RETRY_S = 600             # a failed fetch retries in ~10min
_PRICE_CACHE = {"fetched": 0.0, "table": {}, "source": ""}
_PRICE_BUSY = False
_PRICE_TRIED = 0.0


def _parse_price_table(text: str) -> dict:
    """modelCostData -> {model_uid: {label, in, out, cr}} for the self-serve
    tier; rates are USD per million tokens ('cr' = cache-read input)."""
    m = re.search(r"modelCostData\s*=\s*(\[.*?\])\s*;", text, re.S)
    if not m:
        return {}
    try:
        data = json.loads(m.group(1))
    except ValueError:
        return {}
    table = {}
    for ent in data:
        if not isinstance(ent, dict) or ent.get("tier") != _PRICES_TIER:
            continue
        uid = ent.get("model_uid")
        if not isinstance(uid, str) or not uid:
            continue
        table[uid] = {
            "label": ent.get("label") or uid,
            "in": ent.get("input_cost_per_million_usd"),
            "out": ent.get("output_cost_per_million_usd"),
            "cr": ent.get("cache_read_cost_per_million_usd")}
    return table


def price_table(runs_dir: Path | None = None) -> dict:
    """Best-known self-serve rate table. Memory first, then the pricing.json
    disk mirror; the refresh is a background thread so this never blocks a
    dashboard render."""
    if _PRICE_CACHE["table"]:
        return _PRICE_CACHE["table"]
    pf = (runs_dir.parent if runs_dir else DATA_DIR) / "pricing.json"
    try:
        d = json.loads(pf.read_text(encoding="utf-8"))
        if isinstance(d.get("table"), dict):
            _PRICE_CACHE["fetched"] = float(d.get("fetched_at") or 0)
            _PRICE_CACHE["table"] = d["table"]
            _PRICE_CACHE["source"] = d.get("url") or pf.name
    except (OSError, ValueError, TypeError):
        pass
    return _PRICE_CACHE["table"]


def refresh_prices(runs_dir: Path) -> None:
    """Background re-fetch of the docs rate table. A populated table refetches
    every 6h; an empty one (offline boot, docs down) retries every 10min.
    Failures keep whatever table we had."""
    global _PRICE_BUSY, _PRICE_TRIED
    interval = _PRICES_TTL_S if _PRICE_CACHE["table"] else _PRICES_RETRY_S
    if _PRICE_BUSY or time.time() - _PRICE_TRIED < interval:
        return
    _PRICE_BUSY = True
    _PRICE_TRIED = time.time()

    def work() -> None:
        global _PRICE_BUSY
        try:
            req = urllib.request.Request(
                _PRICES_URL, headers={"User-Agent": "agent-conductor",
                                      "Accept": "text/markdown,*/*"})
            with urllib.request.urlopen(req, timeout=15) as r:
                table = _parse_price_table(
                    r.read().decode("utf-8", "replace"))
            if table:
                _PRICE_CACHE.update(fetched=time.time(), table=table,
                                    source=_PRICES_URL)
                try:
                    (runs_dir.parent / "pricing.json").write_text(
                        json.dumps({"fetched_at": _PRICE_CACHE["fetched"],
                                    "tier": _PRICES_TIER, "url": _PRICES_URL,
                                    "table": table}), encoding="utf-8")
                except OSError:
                    pass
        except Exception:
            LOG.exception("pricing refresh failed")
        _PRICE_BUSY = False

    threading.Thread(target=work, daemon=True).start()


def estimate_cost(tokens: dict | None, model: str | None,
                  effort: str | None, table: dict,
                  model_actual: str | None = None) -> dict | None:
    """USD estimate for one session's token totals at self-serve rates.

    `cached` prices at the cache-read rate and `prompt - cached` is the
    uncached input share (ATIF doesn't split cache write vs read; docs'
    cache-write column is 0 or near-0 for most models anyway). The uid is
    the fused model id the CLI actually ran (model + '-' + effort), falling
    back to the bare model; for model='adaptive' the export's recorded
    model_name is resolved by table label so the rate matches what really
    ran. Returns {'usd': None} for unpriced models."""
    if not tokens:
        return None
    uid = f"{model}-{effort}" if model and effort else model
    ent = table.get(uid) or table.get(model or "")
    used = uid if uid in table else model
    if ent is None and model_actual:
        for cand, e in table.items():
            if (e.get("label") or "").lower() == model_actual.lower():
                ent, used = e, cand
                break
    if not ent or ent.get("in") is None or ent.get("out") is None:
        return {"usd": None, "uid": uid}
    p = tokens.get("prompt") or 0
    c = tokens.get("cached") or 0
    o = tokens.get("completion") or 0
    usd = (max(p - c, 0) * ent["in"] + c * (ent.get("cr") or 0)
           + o * ent["out"]) / 1_000_000
    return {"usd": round(usd, 4), "uid": used, "label": ent.get("label")}


def live_sessions_by_dir(runs_dir: Path,
                         devin_argv: list[str]) -> dict[str, dict | None]:
    """work_dir -> {session id: last_activity epoch} from `devin list`
    there, cached 2min. A dir maps to None when listing failed (devin
    down/missing) - never interpret None as 'all sessions gone'.

    Dirs are collected for two uses: records with a session_id (gone
    checks) and steps still plausibly alive (live attribution of the
    not-yet-recorded session id)."""
    if _SESS_CACHE[1] and time.time() - _SESS_CACHE[0] < 120:
        return _SESS_CACHE[1]
    dirs: set[str] = set()
    for sj in runs_dir.glob("*/state.json"):
        try:
            st = json.loads(sj.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for rec in (st.get("steps") or {}).values():
            wd = rec.get("work_dir") or rec.get("project")
            if wd and (rec.get("session_id") or step_live(rec, driver_alive(st))) \
                    and Path(wd).is_dir():
                dirs.add(wd)
    out = {d: session_activity(devin_argv, Path(d)) for d in dirs}
    _SESS_CACHE[0], _SESS_CACHE[1] = time.time(), out
    return out


def task_label(rec: dict, full: bool = False) -> str | None:
    """What the agent was asked to do. New records store `task` directly
    (160-char summary); `full` goes back to the prompt file for the
    untruncated body the detail modal wants - falling back to the stored
    summary when the file is gone."""
    if rec.get("task") and not full:
        return rec["task"]
    pf = rec.get("prompt")
    if not pf:
        return rec.get("task")
    try:
        text = Path(pf).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return rec.get("task")
    # The prompt file is PREAMBLE-shaped: '## Task' ... '## Before you
    # finish'. Anchor the body capture on the trailer heading itself - a
    # task that embeds its own '## ' sections must not be cut at the
    # first one; when the trailer is absent (custom preamble) take the
    # rest of the file so nothing is ever trimmed.
    m = re.search(r"## Task[^\n]*\n+(.*?)(?:\n+## Before you finish|\Z)",
                  text, re.S)
    body = m.group(1) if m else text
    if full:
        return body.strip() or rec.get("task")
    one = " ".join(body.split())
    return one[:160] or rec.get("task")


def stdout_tail(path: str | None, lines: int = 15) -> str | None:
    """Last `lines` of a step's stdout log - the modal's 'how it ended'.

    The CLI writes narration as one continuous line - no \n at all - so a
    naive splitlines returns a single mega-line and the 'tail' reads as
    the head. Split on lone \r too (progress redraws) so a line-oriented
    tail still picks the freshest text.
    """
    if not path:
        return None
    try:
        data = Path(path).read_bytes()[-8192:]
    except OSError:
        return None
    return "\n".join(re.split(r"\r\n|\r|\n",
                              data.decode("utf-8", "replace"))[-lines:])


def _state_md(work_dir: str | None, step: str | None,
              project_dir: str | None = None) -> str | None:
    """The project's own task-state file for this step, when present.

    .agent_tasks/<task>/STATE.md is the project's memory - written BY the
    worker for the NEXT session, not by the conductor. Step names
    conventionally match task folder names, so probe both the step's
    work_dir (a worktree's copy while it still exists) and the
    registered project path (the main checkout, post-merge).
    """
    if not step:
        return None
    for base in (work_dir, project_dir):
        if not base:
            continue
        try:
            p = Path(base) / ".agent_tasks" / step / "STATE.md"
            if p.is_file():
                return str(p)
        except OSError:
            continue
    return None


# serve process boot id - embedded in every payload so an open tab notices
# a restart and reloads itself to get fresh JS/HTML.
SERVE_BOOT = now_iso()


def proposal_agent(step: dict, rec: dict, projects: dict, key: str,
                   status: str, run_dir: str | None = None,
                   sort_at: str | None = None) -> dict:
    slug = step["slug"]
    return {"run": rec.get("run"), "run_dir": run_dir,
            "run_key": key, "request_id": rec["id"],
            "step": step["step"], "slug": slug,
            "work_dir": str(projects.get(slug, {}).get("path") or slug),
            "status": status,
            "created_at": sort_at or rec.get("updated_at") or rec.get("created_at"),
            "started_at": None, "ended_at": None, "elapsed_s": None,
            "session_id": None, "model": step.get("model"),
            "effort": step.get("effort"), "mode": step.get("mode"),
            "task": step.get("task"), "task_full": None,
            "model_actual": None, "wt": step.get("wt"),
            "timeout_min": step.get("timeout_min"),
            "attempts": None, "max_attempts": None,
            "merged": None, "cloud": step.get("cloud"), "tokens": None,
            "cost": None,
            "alive": False,
            "note": "Run ended before this step started" if status == "skipped"
            else None}


def _manifest_pending(st: dict, projects: dict) -> list[dict]:
    """Steps the run's manifest declares but state.json has no record for -
    CLI-launched runs get the same pending placeholder rows request runs
    already had, so a not-yet-spawned step is visible (and skippable)."""
    cfg = _manifest_cached(st.get("manifest") or "")
    if cfg is None:
        return []
    have = st.get("steps") or {}
    out = []
    manifest_dir = Path(st["manifest"]).parent
    for step in cfg.steps:
        if step.name in have:
            continue
        try:
            pdir, slug = resolve_project(step, projects, manifest_dir)
            wd = str(pdir)
        except ManifestError:
            slug = wd = step.project
        task = " ".join((step.prompt or "").split())[:160]
        if not task and step.prompt_file:
            task = step.prompt_file
        out.append({"name": step.name, "slug": slug, "work_dir": wd,
                    "task": task, "model": step.model,
                    "effort": step.effort, "mode": step.mode,
                    "cloud": step.cloud, "wt": step.worktree,
                    "timeout_min": step.timeout_min})
    return out


def _manifest_bodies(st: dict) -> dict[str, str]:
    """Full task bodies from the run's manifest, keyed by step name.

    state.json only stores a 160-char summary, so the detail modal and
    the prompt edit form read the real text from the manifest - the same
    prompt/prompt_file resolution run_step applies at dispatch."""
    cfg = _manifest_cached(st.get("manifest") or "")
    if cfg is None:
        return {}
    out = {}
    for s in cfg.steps:
        b = s.prompt
        if b is None and s.prompt_file:
            try:
                b = (cfg.path.parent / s.prompt_file).read_text(
                    encoding="utf-8")
            except OSError:
                b = s.prompt_file
        out[s.name] = b or ""
    return out


def _pending_agent(st: dict, sd: dict, key: str, run_dir: str,
                   sort_at: str | None) -> dict:
    """Placeholder row for a manifest step with no state.json record yet -
    same shape proposal_agent returns so the UI treats both alike."""
    return {"run": st.get("run"), "run_dir": run_dir, "run_key": key,
            "request_id": st.get("request_id"),
            "step": sd["name"], "slug": sd["slug"],
            "work_dir": sd["work_dir"], "status": "waiting",
            "created_at": sort_at or st.get("started_at"),
            "started_at": None, "ended_at": None, "elapsed_s": None,
            "session_id": None, "model": sd.get("model"),
            "effort": sd.get("effort"), "mode": sd.get("mode"),
            "task": sd.get("task"), "task_full": None,
            "model_actual": None, "wt": sd.get("wt"),
            "timeout_min": sd.get("timeout_min"),
            "attempts": None, "max_attempts": None,
            "merged": None, "cloud": sd.get("cloud"), "tokens": None,
            "cost": None, "alive": False, "note": None}


def collect_dashboard(runs_dir: Path,
                      devin_argv: list[str] | None = None,
                      requests_dir: Path | None = None,
                      include_archived: bool = False) -> dict:
    """Show executed run states alongside saved, not-yet-executed requests.
    A launched request uses state.json for its rows, not its proposal
    preview. Runs stamped state['archived'] (auto-archive, or the modal's
    Archive button) are omitted unless include_archived - they stay on
    disk either way, and the 'archived_runs' count always reflects them."""
    requests_dir = requests_dir or runs_dir.parent / "requests"
    _auto_archive(runs_dir)
    _auto_drain()
    sess_by_dir = live_sessions_by_dir(runs_dir, devin_argv or ["devin"])
    projects = load_projects()
    refresh_prices(runs_dir)
    prices = price_table(runs_dir)
    requests: dict[str, dict] = {}
    for rf in requests_dir.glob("*.json"):
        try:
            rec = json.loads(rf.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(rec, dict) and REQUEST_ID_RE.fullmatch(
                str(rec.get("id") or "")) and rec.get("status") != "discarded":
            # a parked planner overlay is more current than the file
            requests[rec["id"]] = _spool_rec(rec["id"]) or rec
    launched: set[str] = set()
    archived_runs = 0
    runs: list[dict] = []
    agents: list[dict] = []
    for sj in sorted(runs_dir.glob("*/state.json"), reverse=True):
        try:
            st = json.loads(sj.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if st.get("request_id"):
            launched.add(st["request_id"])
        if st.get("archived"):
            archived_runs += 1
            if not include_archived:
                continue
        key = (f"request:{st['request_id']}" if st.get("request_id")
               else str(sj.parent))
        counts: dict[str, int] = {}
        tok_run: dict[str, int] = {}
        run_cost = 0.0
        run_priced = 0
        run_unpriced = 0
        request = requests.get(st.get("request_id"))
        proposed = {s["step"]: s for s in request.get("proposed_steps", [])} \
            if request else {}
        eff = effective_statuses(st)
        flagged = _skip_flagged(sj.parent)     # 'Skip step' marks in place
        mbodies = _manifest_bodies(st)         # full text per step name
        # Pipeline order for the task modal's prev/next: the manifest's
        # step order IS the pipeline. Recorded steps the manifest no
        # longer names (renamed, file gone) keep their record order after
        # it; a missing manifest leaves record order alone.
        cfg = _manifest_cached(st.get("manifest") or "")
        pipe = [s.name for s in cfg.steps] if cfg else []
        pipe += [n for n in (st.get("steps") or {}) if n not in set(pipe)]
        seq_of = {n: i for i, n in enumerate(pipe)}
        prop_seq = {name: i for i, name in enumerate(proposed)}
        for name, rec in st.get("steps", {}).items():
            plan = proposed.get(name) or {}
            t = rec.get("tokens")
            if isinstance(t, dict):
                for k in ("prompt", "completion", "cached", "llm_steps"):
                    if isinstance(t.get(k), int):
                        tok_run[k] = tok_run.get(k, 0) + t[k]
            status = eff.get(name, rec.get("status", "?"))
            counts[status] = counts.get(status, 0) + 1
            sid = rec.get("session_id")
            slug = rec.get("slug") or plan.get("slug")
            wd = (rec.get("work_dir") or rec.get("project") or
                  str(projects.get(slug, {}).get("path") or ""))
            live = sess_by_dir.get(wd)
            model = rec.get("model") or plan.get("model")
            effort = rec.get("effort") or plan.get("effort")
            cost = estimate_cost(t, model, effort, prices,
                                 rec.get("model_actual"))
            if isinstance(t, dict):
                if cost and cost.get("usd") is not None:
                    run_cost += cost["usd"]
                    run_priced += 1
                else:
                    run_unpriced += 1
            # A recorded id missing from `devin list` minutes after spawn is
            # propagation/cache lag, not deletion. While the worker pid is
            # provably alive the session must exist (list is just stale), so
            # 'gone' is suppressed entirely; once the pid is dead (user
            # deleted the session -> CLI child reaped) show it immediately.
            # No-pid records (self-hosted runs keep the pid memory-only,
            # trap 5) still wait out the grace window.
            start_ts = _iso_ts(rec.get("started_at"))
            rec_pid = rec.get("pid")
            # The modal's full 'task' text: the dispatched prompt body
            # when a prompt file exists, else the manifest's untruncated
            # body (state.json's 160-char summary is only the fallback).
            tfull = task_label(rec, full=True)
            if tfull == rec.get("task"):
                tfull = mbodies.get(name) or tfull
            worker_alive = bool(rec_pid) and pid_alive(
                rec_pid, rec.get("started_at"))
            session_gone = bool(
                sid and live is not None and str(sid) not in live
                and not worker_alive and (
                    rec.get("ended_at") or rec_pid or
                    (start_ts or 0) < time.time() - SESSION_GONE_GRACE_S))
            # A dashboard-edited timeout overrides the manifest value for
            # display - wait_worker applies the same flag on its next poll.
            try:
                tf = timeout_flag(sj.parent, rec.get("name") or name)
                timeout_min = (float(tf.read_text().strip())
                               if tf.is_file() else rec.get("timeout_min"))
            except (OSError, ValueError):
                timeout_min = rec.get("timeout_min")
            agents.append({
                "run": st.get("run"), "run_dir": st.get("run_dir") or str(sj.parent),
                "run_key": key, "request_id": st.get("request_id"),
                "step": rec.get("name") or name, "slug": slug,
                "seq": seq_of.get(rec.get("name") or name,
                                  prop_seq.get(rec.get("name") or name)),
                "work_dir": wd,
                "status": status, "started_at": rec.get("started_at"),
                "ended_at": rec.get("ended_at"),
                "elapsed_s": rec.get("elapsed_s"),
                "alive": step_live(rec, driver_alive(st)),
                "pid": rec.get("pid"), "session_id": rec.get("session_id"),
                "model": model,
                "model_actual": rec.get("model_actual"),
                "mode": rec.get("mode") or plan.get("mode"),
                "effort": effort,
                "task": task_label(rec) or plan.get("task"),
                "task_full": tfull or plan.get("task"),
                # The edit control exists only while the step never
                # dispatched (queued/skipped records are pre-spawn);
                # anything that ran takes resume notes instead - the
                # original prompt is left untouched on purpose.
                "prompt_editable": rec.get("status") in
                ("queued", "skipped"),
                "notes_enabled": status in
                ("blocked", "partial", "failed", "timed_out", "stopped",
                 "lost"),
                "prompt_override": _flag_text(
                    prompt_flag(sj.parent, rec.get("name") or name)),
                "resume_notes": _flag_text(
                    rnote_flag(sj.parent, rec.get("name") or name)),
                "prompt_edited": bool(rec.get("prompt_edited")),
                "cloud": rec.get("cloud", plan.get("cloud")),
                "verify_ok": rec.get("verify_ok"),
                "skipped_by_user": _safe_name(
                    rec.get("name") or name) in flagged,
                "note": rec.get("note"), "next": rec.get("next"),
                "session_gone": session_gone,
                "exit_code": rec.get("exit_code"),
                "branch": rec.get("branch"), "merged": rec.get("merged"),
                "timeout_min": timeout_min,
                "timeout_ext_s": rec.get("timeout_extended_s"),
                "attempts": rec.get("attempts"),
                "max_attempts": rec.get("max_attempts"),
                "tokens": rec.get("tokens"), "cost": cost,
                "wt": bool(rec.get("branch") or plan.get("wt")),
                "stdout_log": rec.get("stdout_log"),
                "tail": stdout_tail(rec.get("stdout_log")),
                "export": rec.get("export"), "prompt": rec.get("prompt"),
                "arc": cached_arc(rec.get("export")),
                "state_md": _state_md(
                    wd, rec.get("name") or name,
                    str(projects.get(slug, {}).get("path") or "") or None)})
        # Pending rows: declared steps with no record yet. Request runs
        # use the proposal's richer step dicts; CLI runs fall back to the
        # manifest itself. A live run shows them 'waiting', an ended one
        # 'skipped'; a user-placed Skip mark forces 'skipped' either way.
        pend = ([proposal_agent(step, request, projects, key, "waiting",
                                str(sj.parent), st.get("started_at"))
                 for step in request.get("proposed_steps") or []
                 if step["step"] not in (st.get("steps") or {})]
                if request else
                [_pending_agent(st, sd, key, str(sj.parent),
                                st.get("started_at"))
                 for sd in _manifest_pending(st, projects)])
        for row in pend:
            if _safe_name(row["step"]) in flagged:
                row["status"] = "skipped"
                row["skipped_by_user"] = True
                row["note"] = ("skipped by user (Skip step) - Unskip "
                               "restores its queue position")
            else:
                row["status"] = "skipped" if st.get("ended_at") else "waiting"
                if row["status"] == "skipped":
                    row["note"] = "Run ended before this step started"
            # Never dispatched: the prompt edit control is live, and the
            # manifest body - not the 160-char summary - is the modal's
            # full text and the form's prefill.
            row["task_full"] = mbodies.get(row["step"]) or row.get("task")
            row["prompt_editable"] = True
            row["notes_enabled"] = False
            row["prompt_override"] = _flag_text(
                prompt_flag(sj.parent, row["step"]))
            row["resume_notes"] = _flag_text(
                rnote_flag(sj.parent, row["step"]))
            row["prompt_edited"] = row["prompt_override"] is not None
            row["seq"] = seq_of.get(row["step"], prop_seq.get(row["step"]))
            counts[row["status"]] = counts.get(row["status"], 0) + 1
            agents.append(row)
        runs.append({"run": st.get("run"), "dir": str(sj.parent),
                     "key": key, "request_id": st.get("request_id"),
                     "archived": st.get("archived"),
                     "description": st.get("description") or
                     (request or {}).get("description"),
                     "started_at": st.get("started_at"),
                     "ended_at": st.get("ended_at"), "counts": counts,
                     "aborted": st.get("aborted"),
                     "tokens": tok_run or None,
                     "cost": ({"usd": round(run_cost, 4) if run_priced
                               else None, "unpriced": run_unpriced}
                              if run_priced or run_unpriced else None),
                     "plan_tokens": (request or {}).get("plan_tokens"),
                     "plan_cost": estimate_cost(
                         (request or {}).get("plan_tokens"),
                         (request or {}).get("plan_model")
                         or (request or {}).get("model"),
                         (request or {}).get("plan_effort"), prices),
                     "max_parallel": st.get("max_parallel"),
                     "projects": sorted({r.get("slug") for r in
                                         st.get("steps", {}).values()
                                         if r.get("slug")} |
                                        {s["slug"] for s in proposed.values()})})
    for rid, rec in requests.items():
        if rid in launched:
            continue
        stage = _request_status(rec, runs_dir)
        if stage == "launched":
            continue
        steps = rec.get("proposed_steps") or []
        key = f"request:{rid}"
        log_p = _gen_log_path(rid, requests_dir)
        gen_tail = (stdout_tail(str(log_p))
                    if stage in ("generating", "error") and log_p.is_file()
                    else None)
        gen_last = next((ln for ln in reversed((gen_tail or "").splitlines())
                         if ln.strip()), None)
        runs.append({"run": rec.get("run") or rec.get("label"),
                     "key": key, "request_id": rid,
                     "description": rec.get("description"),
                     "dir": rec.get("manifest") or "",
                     "created_at": rec.get("created_at"),
                     "updated_at": rec.get("updated_at"),
                     "started_at": (rec.get("generation_started_at")
                                    if stage == "generating" else None),
                     "ended_at": None,
                     "counts": {"planned": len(steps)} if steps else {},
                     "tokens": None,
                     "plan_tokens": rec.get("plan_tokens"),
                     "plan_cost": estimate_cost(
                         rec.get("plan_tokens"),
                         rec.get("plan_model") or rec.get("model"),
                         rec.get("plan_effort"), prices),
                     "max_parallel": None,
                     "stage": stage, "error": rec.get("error"),
                     "stale": rec.get("stale"), "gen_last": gen_last,
                     # pending questionnaire (stage 'questions'): enough for
                     # the row chip + auto-popup signature; the modal loads
                     # the full objects via GET /api/requests/<id>
                     "questions": [{"qid": q.get("qid"),
                                    "header": q.get("header"),
                                    "question": (q.get("question")
                                                 or "")[:160]}
                                   for q in rec.get("pending_questions")
                                   or []],
                     "projects": sorted({s["slug"] for s in steps})
                     or ([rec["project"]] if rec.get("project") else [])})
        if stage == "generating":
            # The planner IS an agent while it runs - surface it like a
            # worker row so its live stdout streams into the agents table
            # and detail modal. work_dir is a tempdir, never a watched dir.
            agents.append({
                "run": rec.get("run") or rec.get("label"),
                "run_dir": None, "run_key": key, "request_id": rid,
                "step": "plan-proposal",
                "slug": rec.get("project") or "agent-conductor",
                "work_dir": "planner tempdir (auto-cleaned)",
                "status": "running",
                "created_at": rec.get("generation_started_at")
                or rec.get("updated_at"),
                "started_at": rec.get("generation_started_at"),
                "ended_at": None, "elapsed_s": None, "alive": False,
                "pid": None, "session_id": None, "session_gone": False,
                "model": rec.get("plan_model") or rec.get("model"),
                "effort": (rec.get("plan_effort")
                           if rec.get("plan_effort") is not None
                           else rec.get("effort")),
                "mode": "planner", "cloud": False, "verify_ok": None,
                "task": "Propose a TOML runbook for: " + " ".join(
                    (rec.get("raw") or "").split())[:120],
                "note": None, "next": None, "exit_code": None,
                "branch": None, "merged": None, "tokens": None, "wt": False,
                "cost": None,
                "stdout_log": str(log_p) if log_p.is_file() else None,
                "tail": gen_tail, "export": None, "prompt": None})
        pbodies = _manifest_bodies(rec)
        for i, s in enumerate(steps):
            a = proposal_agent(s, rec, projects, key, "planned")
            a["task_full"] = pbodies.get(s["step"]) or a.get("task")
            a["prompt_editable"] = False      # edited via the TOML review
            a["notes_enabled"] = False
            a["seq"] = i               # proposal order is its pipeline
            agents.append(a)
    # session_id is discovered post-spawn (diff of `devin list`); while a
    # step runs the cell is empty. Attribute it live instead: among the
    # unclaimed ids in the step's work_dir, the spawned one is the session
    # that went active when the step started (older human sessions in the
    # same dir have stale activity stamps). Ambiguous -> leave blank.
    claimed = {a["session_id"] for a in agents if a.get("session_id")}
    for a in agents:
        if a["status"] == "running" and not a.get("session_id"):
            live = sess_by_dir.get(a.get("work_dir")) or {}
            started = (_iso_ts(a.get("started_at")) or 0) - 5
            cand = [s for s, act in live.items()
                    if str(s) not in claimed and act >= started]
            if len(cand) == 1:
                a["session_id"], a["session_live"] = cand[0], True
        tail_last = next(
            (ln.strip() for ln in
             reversed((a.get("tail") or "").splitlines())
             if ln.strip()), None)
        if tail_last and len(tail_last) > 200:
            # CLI logs stream narration without newlines - the 'last line'
            # can be an 8KB blob whose START is stale. The freshest text
            # is at the END.
            tail_last = "…" + tail_last[-200:]
        a["tail_last"] = tail_last
    agents.sort(key=lambda a: a.get("started_at") or a.get("created_at") or "",
                reverse=True)
    runs.sort(key=lambda r: r.get("started_at") or r.get("updated_at") or
              r.get("created_at") or "", reverse=True)
    return {"generated_at": now_iso(), "serve_boot": SERVE_BOOT,
            "cpu_count": os.cpu_count(),
            "free_ram_gb": free_ram_gb(), "runs": runs, "agents": agents,
            "archived_runs": archived_runs,
            "project_options": [{"slug": slug, "cloud_ok": p["cloud_ok"]}
                                for slug, p in sorted(projects.items())],
            "pricing": {"tier": "self-serve", "models": len(prices),
                        "fetched": (dt.datetime.fromtimestamp(
                            _PRICE_CACHE["fetched"]).isoformat(
                                timespec="seconds")
                            if _PRICE_CACHE["fetched"] else None),
                        "source": _PRICE_CACHE["source"]},
            "defaults": dict(zip(("model", "effort"), default_model_effort())),
            "model_options": model_options(devin_argv or ["devin"]),
            "auth": auth_status(devin_argv or ["devin"])}


_DASH_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>agent-conductor</title>
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'%3E%3Crect width='64' height='64' rx='14' fill='%2313161f'/%3E%3Cline x1='13' y1='19' x2='49' y2='7' stroke='%23e6edf3' stroke-width='2.6' stroke-linecap='round'/%3E%3Ccircle cx='13' cy='19' r='3' fill='%23e6edf3'/%3E%3Ccircle cx='12' cy='37' r='3' fill='%2358a6ff'/%3E%3Crect x='8.5' y='41' width='7' height='11' rx='2' fill='%2358a6ff'/%3E%3Ccircle cx='23.5' cy='31' r='3' fill='%23bc8cff'/%3E%3Crect x='20' y='35' width='7' height='17' rx='2' fill='%23bc8cff'/%3E%3Ccircle cx='35' cy='26' r='3' fill='%23d29922'/%3E%3Crect x='31.5' y='30' width='7' height='22' rx='2' fill='%23d29922'/%3E%3Ccircle cx='46.5' cy='34' r='3' fill='%233fb950'/%3E%3Crect x='43' y='38' width='7' height='14' rx='2' fill='%233fb950'/%3E%3C/svg%3E">
<style>
:root { color-scheme: dark; }
body { background:#0d1117; color:#e6edf3; font:14px/1.45 ui-monospace,Consolas,monospace; margin:0; padding:16px; }
h1 { font-size:18px; margin:0; }
.sub { color:#8b949e; font-size:12px; margin-bottom:14px; }
h2 { font-size:13px; text-transform:uppercase; letter-spacing:.08em; color:#8b949e; margin:22px 0 8px; }
h3.proj { font-size:13px; color:#e6edf3; margin:16px 0 4px; }
h3.proj .sub { margin-bottom:0; font-weight:400; }
/* Sticky chrome: the topbar (title/meta/actions/density/filters) stays
   pinned; every table header pins just under it (--topbar-h is measured
   in JS since the filters row can wrap). .wrapw must NOT set overflow -
   any overflow ancestor becomes a scroll container and th would stick to
   it instead of the viewport. */
.topbar { position:sticky; top:0; z-index:40; background:#0d1117;
  margin:-16px -16px 0; padding:10px 16px 8px; border-bottom:1px solid #21262d; }
.tb-row { display:flex; align-items:center; gap:14px; flex-wrap:wrap; }
.tb-row .sub { margin:0; }
.tb-gap { flex:1; }
.tb-flow { margin:6px 0 0; }
#filters { margin-top:8px; }
.seg { display:inline-flex; border:1px solid #30363d; border-radius:7px;
  overflow:hidden; }
.seg button { background:transparent; border:0; color:#8b949e;
  font:inherit; font-size:12px; padding:5px 11px; cursor:pointer; }
.seg button.on { background:#1f6feb; color:#fff; }
table { border-collapse:collapse; width:100%; table-layout:fixed; }
th { position:sticky; top:var(--topbar-h,110px); z-index:7;
  background:#0d1117; text-align:left; font-weight:600; color:#8b949e;
  font-size:11px; text-transform:uppercase; padding:4px 10px 4px 0;
  border-bottom:1px solid #21262d; }
td { padding:6px 10px 6px 0; border-bottom:1px solid #161b22; vertical-align:top; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
td.wrap { white-space:normal; color:#8b949e; font-size:12px; }
td.step { white-space:normal; }  /* wt/merged chips wrap under a long name */
tr.agen, tr.runrow { cursor:pointer; }
tr.runrow:hover td { background:#172532; }
tr.runrow.selected td { background:#1c3041; }
tr.runrow.selected td:first-child { box-shadow:inset 3px 0 #58a6ff; }
.dots span { animation:blink 1.2s infinite; color:#58a6ff; }
.dots span:nth-child(2) { animation-delay:.4s; }
.dots span:nth-child(3) { animation-delay:.8s; }
@keyframes blink { 0%,100%{opacity:.15} 50%{opacity:1} }
tr.agen td { transition:background .12s; }
tr.agen:hover td { background:#26405a; }
tr.hl td { background:#1c2833; }
tr.agen.hl:hover td { background:#2a4a68; }
.gone { opacity:.45; text-decoration:line-through; }
td.res { color:#8b949e; font-size:12px; }
td.res.fresh { animation:fresh 1.2s ease-out; }
@keyframes fresh { 0% { background:rgba(88,166,255,.25); } 100% { background:transparent; } }
.chip { display:inline-block; border:1px solid #30363d; border-radius:10px; padding:1px 9px; margin:0 5px 5px 0; font-size:12px; cursor:pointer; user-select:none; }
.chip.off { opacity:.35; }
.chip.active { background:#1c3041; border-color:#58a6ff; }
button.chip { background:transparent; font:inherit; color:inherit; }
button.chip.active { background:#1c3041; }
button.chip:focus-visible, button:focus-visible, tr.runrow:focus-visible { outline:2px solid #58a6ff; outline-offset:2px; }
.filter-group { display:inline-block; margin:0 14px 5px 0; }
.filter-label { color:#8b949e; margin-right:6px; }
.dot { display:inline-block; width:9px; height:9px; border-radius:50%; margin-right:6px; }
.dot.run { animation:pulse 1.2s ease-in-out infinite; }
@keyframes pulse { 50% { opacity:.35; } }
.wrapw { }  /* overflow here would create a scroll container and break
   the sticky th - tables are width:100% fixed, nothing overflows */
/* compact density: one line per row - wrap cells ellipsize, sub-lines
   (dir path, plan tokens) hide */
body.compact td { padding-top:2px; padding-bottom:2px; }
body.compact td.wrap, body.compact td.res {
  white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
body.compact td .sub { display:none; }
body.compact td br { display:none; }
/* .genline: a proposal row's compact live-output line - display:none at
   full density (the .sub line under the stage chip shows the same
   gen_last there), inline on the row's one line under compact, ellipsized
   by the td. .fresh flashes it on new output - the same animation the
   agent result cells use, on the .sub line too. */
.genline { display:none; }
body.compact td .genline { display:inline; color:#8b949e;
  font-size:12px; margin-left:6px; }
.genline.fresh, td .sub.fresh { animation:fresh 1.2s ease-out; }
.counts span { background:#161b22; border-radius:8px; padding:1px 8px; margin-right:5px; font-size:12px; }
a { color:#58a6ff; text-decoration:none; }
[hidden] { display:none !important; }
.tok-help { display:inline-block; position:relative; margin-left:6px; vertical-align:middle; }
.tok-help summary { display:inline-grid; place-items:center; width:18px; height:18px; border:1px solid #6e7681; border-radius:50%; color:#58a6ff; cursor:pointer; list-style:none; }
.tok-help summary::-webkit-details-marker { display:none; }
.tok-help .explain { padding:10px 12px; margin-top:7px; border:1px solid #30363d; background:#0d1117; border-radius:6px; font-size:12px; line-height:1.6; }
#backdrop { position:fixed; inset:0; background:rgba(0,0,0,.55); z-index:50; }
#detail { position:fixed; left:50%; top:50%; transform:translate(-50%,-50%); width:min(780px,92vw); max-height:82vh; background:#161b22; border:1px solid #30363d; border-radius:10px; padding:18px 22px; overflow:auto; z-index:51; font-size:13px; box-sizing:border-box; }
#detail h3 { margin:0 80px 10px 0; font-size:14px; }
#detail .kv { display:grid; grid-template-columns:96px 1fr; gap:4px 12px; }
#detail .k { color:#8b949e; }
#detail pre { white-space:pre-wrap; color:#8b949e; background:#0d1117; border-radius:6px; padding:8px 10px; font-size:12px; max-height:180px; overflow:auto; }
#detail .x { position:absolute; top:10px; right:16px; cursor:pointer; color:#8b949e; background:none; border:0; font:inherit; }
/* prev/next task bar - sticky like .topbar: top:0 plus margins that undo
   #detail's padding so it pins flush over the scroll gap. The right pad
   leaves room for the Close X which overlays the bar. */
#detail .dnav { position:sticky; top:0; z-index:6; display:flex; align-items:center; gap:10px; margin:-18px -22px 12px; padding:7px 56px 7px 22px; background:#161b22; border-bottom:1px solid #30363d; border-radius:9px 9px 0 0; }
#detail .dnav-btn { display:inline-flex; align-items:center; gap:6px; max-width:40%; background:#21262d; border:1px solid #30363d; color:#e6edf3; border-radius:6px; padding:4px 10px; font:inherit; font-size:12px; cursor:pointer; }
#detail .dnav-btn span { overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
#detail .dnav-btn:hover:not(:disabled) { border-color:#58a6ff; }
#detail .dnav-btn:disabled { opacity:.4; cursor:default; }
#detail .dnav-pos { margin:0 auto; color:#8b949e; font-size:12px; white-space:nowrap; }
.arc { font-size:12px; border:1px solid #21262d; border-radius:6px; padding:6px 8px; max-height:280px; overflow-y:auto; background:#0d1117; }
.arcrow { display:flex; gap:8px; padding:1px 0; }
.arcspan { color:#8b949e; min-width:44px; text-align:right; flex:none; }
.arcphase { flex:none; width:78px; }
.archead { color:#8b949e; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
.arcrow:hover .archead { color:#e6edf3; }
details.arcgrp > summary { display:flex; gap:8px; padding:1px 0; cursor:pointer; list-style:none; }
details.arcgrp > summary::-webkit-details-marker { display:none; }
details.arcgrp > summary::before { content:'▸'; color:#6e7681; flex:none; }
details.arcgrp[open] > summary::before { content:'▾'; }
.arcdetail { margin:2px 0 6px 18px; border-left:1px solid #21262d; padding-left:8px; }
.arcdetail .arcrow { font-size:11px; }
.arctime { color:#6e7681; min-width:60px; flex:none; }
.arcdur { color:#6e7681; min-width:44px; text-align:right; flex:none; }
.arctok { color:#6e7681; min-width:96px; text-align:right; flex:none; }
.arc-setup, .arc-work { color:#8b949e; }
.arc-audit { color:#79c0ff; }
.arc-implement { color:#d2a8ff; }
.arc-test { color:#ffa657; }
.arc-docs { color:#7ee787; }
.arc-commit { color:#3fb950; }

.stage { display:inline-block; border-radius:9px; padding:1px 7px; font-size:11px; border:1px solid #30363d; white-space:nowrap; }
.stage.draft { color:#e3b341; border-color:#6b5a1f; background:#2a2317; }
.stage.ready { color:#bc8cff; border-color:#614b84; background:#251d35; }
.stage.generating { color:#58a6ff; background:#172638;
  animation:pulse 1.2s ease-in-out infinite; }
.stage.launching { color:#58a6ff; background:#172638; }
.stage.error, .stage.launch_failed { color:#f85149; background:#321c20; }
.stage.live { color:#58a6ff; background:#172638; border-color:#1f6feb;
  animation:pulse 1.2s ease-in-out infinite; }
.stage.paused { color:#e3b341; border-color:#6b5a1f; background:#2a2317; }
.stage.interrupted { color:#f0883e; border-color:#7a3d12; background:#2d1f14; }
.stage.questions { color:#ffa657; border-color:#7a3d12; background:#2d1f14; }
.stage.halted { color:#8b949e; }
.stage.archived { color:#8b949e; border-color:#21262d; }
.linkbtn { background:none; border:0; color:#58a6ff; cursor:pointer; font:inherit; padding:0; text-decoration:underline; }
.run-name { color:#58a6ff; text-decoration:underline; text-align:left; }
.run-name:hover { color:#79c0ff; }
/* td has padding-left:0 + overflow:hidden, so an outset focus ring is
   clipped at the cell edge - draw it inside the element instead */
td :is(a,button):focus-visible { outline-offset:-2px; }
.btn { border:1px solid #388bfd; border-radius:6px; background:#1f6feb; color:#fff; padding:7px 12px; cursor:pointer; font:inherit; }
.btn.secondary { color:#e6edf3; background:#21262d; border-color:#30363d; }
.btn.danger { color:#ffb2aa; background:transparent; border-color:#9f3535; }
.btn.attn { border-color:#d29922; color:#e3b341; }
.btn:disabled { cursor:not-allowed; opacity:.45; }
#detail label { display:block; color:#8b949e; margin:12px 0 5px; }
#detail input, #detail textarea, #detail select { box-sizing:border-box; width:100%; padding:8px 10px; border:1px solid #30363d; border-radius:6px; background:#0d1117; color:#e6edf3; font:inherit; }
/* image attachments on the request form: a thumbnail strip under the
   request textarea, fed by drop/paste (saved to data/attachments) */
.attach-strip { display:flex; flex-wrap:wrap; gap:8px; margin-top:8px; }
.attach-item { position:relative; width:104px; height:70px; border:1px solid #30363d; border-radius:6px; overflow:hidden; background:#0d1117; }
.attach-item img { width:100%; height:100%; object-fit:cover; display:block; }
.attach-x { position:absolute; top:2px; right:2px; width:20px; height:20px; border-radius:50%; border:1px solid #30363d; background:rgba(13,17,23,.9); color:#ffb2aa; cursor:pointer; font-size:14px; line-height:1; padding:0; }
.attach-x:hover { border-color:#9f3535; }
#detail textarea.drop { border-color:#58a6ff; }
#detail label.check { display:flex; align-items:flex-start; gap:8px; margin:12px 0 4px; font-size:13px; cursor:pointer; }
#detail label.check input[type=checkbox] { width:auto; margin-top:2px; }
#detail textarea { min-height:145px; resize:vertical; }
#detail textarea.toml { min-height:270px; font:12px/1.5 ui-monospace,Consolas,monospace; }
#detail input:disabled, #detail textarea:disabled, #detail select:disabled { opacity:.65; }
#detail .field-row { display:grid; grid-template-columns:2fr 1fr 1fr; gap:10px; }
#detail .actions { display:flex; gap:8px; flex-wrap:wrap; align-items:center; margin:16px 0 0; }
#detail .notice { color:#8b949e; background:#0d1117; border-left:3px solid #30363d; padding:8px 10px; margin:10px 0; }
#detail .notice.warn { border-color:#d29922; color:#e6edf3; }
#detail .notice.error { border-color:#f85149; color:#ffb2aa; }
#detail details.proposal { margin-top:14px; }
#detail details.proposal summary { cursor:pointer; color:#58a6ff; }
#detail .proposal-list { margin-top:12px; padding-left:20px; }
#detail .proposal-list li { margin:8px 0; }
#detail .proposal-list small { color:#8b949e; }
/* questionnaire modal (planner 'ask me questions' flow) */
.qopts { display:flex; flex-direction:column; gap:6px; margin:10px 0 4px; }
.qopt { text-align:left; border:1px solid #30363d; border-radius:6px;
  background:#0d1117; color:#e6edf3; padding:8px 10px; cursor:pointer;
  font:inherit; }
.qopt:hover { border-color:#58a6ff; }
.qopt.on { border-color:#58a6ff; background:#1c3041; }
.qopt .sub { display:block; margin-top:2px; }
@media (max-width:640px) { .hide-s { display:none; } #detail { width:92vw; } #detail .field-row { grid-template-columns:1fr; } }
</style></head><body>
<div class="topbar">
  <div class="tb-row">
    <h1>agent-conductor</h1>
    <span class="sub" id="meta">loading&hellip;</span>
    <span class="tb-gap"></span>
    <button type="button" class="btn" id="new-request">New request</button>
    <div class="seg" id="density" role="group" aria-label="Row density">
      <button type="button" data-density="compact">Compact</button>
      <button type="button" data-density="full" class="on">Full</button>
    </div>
  </div>
  <div class="sub tb-flow">Save draft &rarr; generate proposal &rarr; review &rarr; start when ready. Generating never runs tasks.</div>
  <div class="sub" id="filters"></div>
</div>
<h2>runs</h2><div class="wrapw"><table id="runs"></table></div>
<h2>agents by project</h2><div id="agents"></div>
<div id="backdrop" hidden><div id="detail" role="dialog" aria-modal="true" aria-labelledby="modal-title"><button type="button" class="x" id="dx">Close ×</button><div id="dbody"></div></div></div>
<script>
const COLORS = {done:'#3fb950',partial:'#d29922',blocked:'#d29922',
stopped:'#d29922',
failed:'#f85149',timed_out:'#f0883e',running:'#58a6ff',queued:'#8b949e',
waiting:'#8b949e',skipped:'#6e7681',planned:'#bc8cff',ready:'#bc8cff',
generating:'#bc8cff',draft:'#d29922',questions:'#ffa657',
launching:'#58a6ff',launch_failed:'#f85149',lost:'#6e7681'};
const STAGES = {draft:'Draft · unsent',generating:'Preparing proposal',
  questions:'Needs your answers',
  ready:'Ready to run',launching:'Starting run',launch_failed:'Start failed',
  error:'Planning failed'};
const esc = s => (s==null?'':String(s)).replace(/[&<>"]/g,
  c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
// basename for win/posix paths. fromCharCode(92) is a backslash - a '\\'
// literal in this Python-served string is an escaping trap (a '\\' in
// source reaches JS as '\', silently turning [\\/] into [/]-only).
const base = p => { const s=String(p||'');
  const i=Math.max(s.lastIndexOf('/')+1, s.lastIndexOf(String.fromCharCode(92))+1);
  return s.slice(i); };
const today = new Date().toDateString();
const loc = iso => { if(!iso) return '-';
  const d=new Date(iso);
  return d.toDateString()===today
    ? d.toLocaleTimeString([], {hour:'2-digit',minute:'2-digit',second:'2-digit'})
    : d.toLocaleDateString([], {month:'2-digit',day:'2-digit'}) + ' ' +
      d.toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'}); };
const fmtS = s => { if(s==null) return '-';
  s=Math.round(s); const h=Math.floor(s/3600), m=Math.floor(s%3600/60);
  return (h? h+'h':'') + (m? m+'m':'') + (s%60)+'s'; };
const kfmt = n => n==null?'-':(n>=1000
  ? (n/1000).toFixed(n>=100000?0:1).replace(/\.0$/,'')+'k' : String(n));
// tokens render as labeled fields so every counter names itself:
// in = BILLED input (full prompt minus the cached share - same number
// Devin Desktop's card reports), out = completion, cached = the share
// of the prompt served from cache (always shown so no value can hide),
// turns = LLM steps/model turns, NOT manifest steps. Full prompt per
// turn = in + cached (the tooltip carries it).
const billed = t => t && t.prompt!=null
  ? Math.max(t.prompt-(t.cached||0),0) : null;
const fmtT = t => (!t||t.prompt==null) ? '-' :
  `in ${kfmt(billed(t))} · out ${kfmt(t.completion)}` +
  ` · cached ${kfmt(t.cached||0)}` +
  (t.llm_steps ? ` · ${t.llm_steps} turns` : '');
// compact density: bare in/out/cached + LLM turns - order is fixed and
// spelled out in TOK_HELP + the cell title
const fmtTc = t => (!t||t.prompt==null) ? '-' :
  `${kfmt(billed(t))}/${kfmt(t.completion)}/${kfmt(t.cached||0)}` +
  (t.llm_steps ? ` ${t.llm_steps}L` : '');
const tokTitle = t => !t ? '' :
  `billed in ${t.prompt-(t.cached||0)} ` +
  `(full prompt ${t.prompt}, of which cached ${t.cached}) · ` +
  `out ${t.completion} · ${t.llm_steps??'?'} LLM steps (model turns)`;
const fmtC = c => !c ? '-' : c.usd==null ? 'n/a' :
  '$'+(c.usd===0 ? '0' : c.usd<0.01 ? c.usd.toFixed(4) : c.usd.toFixed(2));
const costTitle = c => !c ? 'no token data yet' :
  (c.label ? `${c.label} (${c.uid||''})` : (c.uid||'')) +
  (c.usd==null ? ' - no self-serve rate for this model'
              : ' - est. self-serve cost');
// row density + sticky-topbar height (test harness stubs `document`
// only - guard every browser global)
const store = (typeof localStorage!=='undefined') ? localStorage :
  {getItem:()=>null, setItem:()=>{}};
let density = store.getItem('ac-density') || 'full';
// 'archived' view chip: archived runs stay on disk but are hidden from the
// default listing - the toggle refetches /api/state?archived=1 to list them.
let showArchived = store.getItem('ac-arch') === '1';
function syncTopbar(){
  const tb = document.querySelector && document.querySelector('.topbar');
  if (tb && document.documentElement)
    document.documentElement.style.setProperty(
      '--topbar-h', tb.offsetHeight+'px');
}
function setDensity(d){
  density = d; store.setItem('ac-density', d);
  if (document.body)
    document.body.classList.toggle('compact', d==='compact');
  (document.querySelectorAll ? document.querySelectorAll('#density button')
    : []).forEach(b=>b.classList.toggle('on', b.dataset.density===d));
  syncTopbar();
}
const TOK_HELP = 'tokens: in (billed, uncached) / out / cached ' +
  '(cache-served share) · turns = LLM steps; compact view reads ' +
  '"in/out/cached NL"; select an agent for details';
const tokenHelp = `<details class="tok-help"><summary aria-label="Explain tokens and cost">?</summary>` +
  `<div class="explain"><b>What the numbers mean</b><br>` +
  `<b>in</b>: uncached input tokens - the part billed at the input ` +
  `rate, same number Devin Desktop's card reports. The raw export ` +
  `counts the WHOLE prompt each turn (in + cached); the cell tooltip ` +
  `still shows it.<br>` +
  `<b>out</b>: output (completion) tokens.<br>` +
  `<b>cached</b>: the share of the prompt served from cache - billed ` +
  `at the (cheaper) cache-read rate, never added on top of ` +
  `<b>in</b>.<br>` +
  `<b>turns</b>: LLM steps - model turns, not manifest steps. ` +
  `<b>M</b> in prices means one million tokens.<br><br>` +
  `<b>Estimated cost</b> = (prompt - cached) × input rate + ` +
  `cached × cache rate + completion × output rate, each rate per token ` +
  `for that model and effort.<br><br>` +
  `<b>Example (illustrative rates, not a quote):</b> 10,000 prompt, ` +
  `6,000 cached, 1,000 completion; at $2/M input, $0.50/M cached, ` +
  `$8/M output: 4,000 × $2/M + 6,000 × $0.50/M + 1,000 × $8/M ` +
  `= $0.008 + $0.003 + $0.008 = <b>$0.019</b>. ` +
  `Rates come from the self-serve table at docs.devin.ai/desktop/chat/` +
  `models, refetched every few hours; a model with no listed rate ` +
  `shows n/a.</div></details>`;
function elapsed(a, now){
  if (a.elapsed_s != null) return fmtS(a.elapsed_s);
  if (a.started_at && (a.status==='running'||a.status==='queued'))
    return fmtS((now - new Date(a.started_at))/1000);
  return '-'; }
// While a request generates, its planner row carries the live stdout tail -
// stream it into the open request modal's <pre id="gtail">.
function streamGenTail(){
  const gel = document.getElementById('gtail');
  if (!gel || !modalRequest) return;
  const pa = AGENTS.find(a=>a.request_id===modalRequest.id &&
                          a.step==='plan-proposal');
  const t = (pa && pa.tail) || '';
  if (gel.textContent !== t){ gel.textContent = t; gel.scrollTop = gel.scrollHeight; }
}
const expanded = {};      // slug -> true: show all rows, not just latest 10
const only = new Set();   // status chips: empty = all, else show ONLY these
const facets = new Set();
let selectedRun = null;
const prevTail = {};      // run|step -> last tail_last shown; a change
                          // flashes the result cell (.fresh) as logs stream.
                          // 'genline|<runkey>' keys the same diff for a
                          // proposal row's compact live-output line
const SHOW_N = 10;
let runsShown = SHOW_N;   // visible run-table rows; active runs never hide
const ORDER = ['running','queued','waiting','planned','done','partial',
               'blocked','stopped','timed_out','failed','skipped','lost'];
const FACETS = ['wt','merged','unmerged'];
const matchesFacet = (a,f) => f==='wt' ? a.wt :
  f==='merged' ? a.merged===true : a.merged===false;
const runKey = r => r.key||r.dir;
let lastData = null;
let AGENTS = [];          // last payload, for the detail modal

// column layout shared by every per-project table (fixed layout -> the
// same widths line up across tables)
const AGENT_COLS = '<colgroup>' +
  '<col style="width:8%"><col style="width:20%">' +
  '<col style="width:8%"><col class="hide-s" style="width:6%">' +
  '<col class="hide-s" style="width:5%"><col class="hide-s" style="width:5%">' +
  '<col style="width:6%"><col style="width:6%">' +
  '<col class="hide-s" style="width:10%"><col class="hide-s" style="width:5%">' +
  '<col style="width:6%"><col class="hide-s" style="width:5%">' +
  '<col style="width:10%">' +
  '</colgroup>';

let detailIdx = null;
let detailKey = null;       // run_key|step of the open agent modal - the
                            // payload re-sorts each tick, so the index is
                            // re-resolved by key before every re-render
let modalRequest = null;
let modalQA = null;         // {id, rec, sel:Set, free:''} while the planner
                            // questionnaire modal is open
let rqAttachments = [];     // [{name,path}] on the open request form
const qDismissed = new Set();   // request|qids sigs the human closed - the
                              // auto-popup never re-nags for the same set
const promptDrafts = {};    // key -> unsaved 'Edit prompt' text
const notesDrafts = {};     // key -> unsaved 'resume notes' text
// run_key namespaces per run/request - run_dir alone collides for
// unlaunched proposals (their dir is null), which would let the detail
// modal (and a nav click) land on another request's same-named step.
const agentKey = a => (a.run_key||a.run_dir||'') + '|' + a.step;
const qSig = (id, pend) => id + '|' + (pend||[]).map(q=>q.qid).join(',');
function closeDetail(force=false){
  if (!force && modalRequest?.dirty && !confirm('Discard unsaved edits?')) return false;
  // closing the questionnaire mid-way dismisses the auto-popup for THIS
  // question set - the request's Answer button still re-opens it, and a
  // fresh question round pops again (its qids differ)
  if (modalQA && modalQA.rec)
    qDismissed.add(qSig(modalQA.id, modalQA.rec.pending_questions));
  detailIdx = null; detailKey = null; modalRequest = null; modalQA = null;
  document.getElementById('backdrop').hidden = true;
  return true;
}
// The steps of one run (or proposal) ARE the pipeline - collect_dashboard
// stamps each row with `seq` = its manifest/proposal position, so prev/next
// walks execution order rather than the time-sorted table. Rows without a
// seq (the plan-proposal planner) sit out of the chain.
const pipeSibs = a => {
  const rk = a && (a.run_key||a.run_dir||'');
  if (!rk) return [];
  return AGENTS.filter(x=>(x.run_key||x.run_dir||'')===rk && x.seq!=null)
    .sort((x,y)=>x.seq-y.seq);
};
function navStep(d){
  const a = AGENTS.find(x=>agentKey(x)===detailKey);
  if (!a) return;
  const sibs = pipeSibs(a);
  const pos = sibs.findIndex(x=>agentKey(x)===detailKey);
  const b = pos>=0 && sibs[pos+d];
  if (b){
    const j = AGENTS.findIndex(x=>agentKey(x)===agentKey(b));
    if (j>=0){
      showDetail(j);
      const d = document.getElementById('detail');
      if (d) d.scrollTop = 0;      // land on the task top, not mid-scroll
    }
  }
}
function showDetail(i){
  const a = AGENTS[i]; if(!a || !closeDetail()) return;
  detailIdx = i; detailKey = agentKey(a);
  // The modal is rebuilt on every refresh tick - carry the focused
  // textarea's draft text and caret across the rebuild so typing in
  // Edit prompt / resume notes isn't eaten.
  const f0 = document.activeElement;
  const fid = f0 && f0.id;
  const fsel = (fid==='d-prompt'||fid==='d-rnotes')
    ? [f0.selectionStart, f0.selectionEnd] : null;
  const pk = detailKey;
  const pd = promptDrafts[pk];   // unsaved edit (undefined = none open)
  const nd = notesDrafts[pk];
  const kv = [['step',a.step],['run',a.run],['project',a.slug],
    ['status',a.status],['task prompt',a.task_full||a.task],
    ['result',a.next],['note',a.note],['log tail',a.tail_last],
    ['model',(a.model||'-')+(a.effort?' (effort '+a.effort+')':'')+
      (a.model_actual?' → '+a.model_actual:'')],
    ['mode',(a.mode||'-')+(a.cloud?' cloud':'')],
    ['started',a.started_at],['ended',a.ended_at],
    ['elapsed',a.elapsed_s!=null?fmtS(a.elapsed_s):'-'],
    ['timeout',a.timeout_min!=null?a.timeout_min+' min'+
      (a.timeout_ext_s?` (+${fmtS(a.timeout_ext_s)} active past it)`:
      ''):'-'],
    ['attempt',a.attempts!=null?`${a.attempts} of ${a.max_attempts||'?'}`:'-'],
    ['tokens',tokTitle(a.tokens)||'-'],
    ['est cost',(fmtC(a.cost)!=='-'?fmtC(a.cost)+' - ':'')+costTitle(a.cost)],
    ['session',(a.session_id||'-')+(a.session_gone?' (deleted from sidebar)':'')],
    ['verify',a.verify_ok==null?'-':(a.verify_ok?'pass':'FAIL')],
    ['exit',a.exit_code],['work dir',a.work_dir],
    ['branch',a.branch],['merged',a.merged],
    ['prompt file',a.prompt],['stdout',a.stdout_log],['export',a.export],
    ['state file',a.state_md]];
  // prev/next walks this step's own pipeline - sibling buttons name their
  // target, the counter gives position. Hidden for a lone/seq-less row.
  const sibs = pipeSibs(a);
  const pos = sibs.findIndex(x=>agentKey(x)===detailKey);
  const navBtn = (b,dir)=> b
    ? `<button type="button" class="dnav-btn" ` +
      `data-nav="${esc(agentKey(b))}" title="${esc(b.step)}">` +
      (dir<0 ? `&larr; <span>${esc(b.step)}</span>`
             : `<span>${esc(b.step)}</span> &rarr;`) + `</button>`
    : `<button type="button" class="dnav-btn" disabled>` +
      (dir<0 ? '&larr;' : '&rarr;') + `</button>`;
  const navBar = (pos>=0 && sibs.length>1)
    ? `<div class="dnav">${navBtn(sibs[pos-1],-1)}` +
      `<span class="dnav-pos">task ${pos+1} of ${sibs.length}</span>` +
      `${navBtn(sibs[pos+1],1)}</div>` : '';
  document.getElementById('dbody').innerHTML =
    navBar +
    `<h3 id="modal-title">${esc(a.slug)} / ${esc(a.step)}</h3><div class="kv">` +
    kv.map(([k,v])=>`<div class="k">${k}</div><div class="wrap">` +
      (k==='task prompt' ? `<pre>${esc(v??'-')}</pre>` : esc(v??'-')) +
      (k==='tokens'?tokenHelp:'') + '</div>').join('') +
    `</div>` +
    (a.run_dir
      ? (`<div class="actions"><a class="btn secondary" target="_blank" ` +
        `href="/api/runs/${encodeURIComponent(base(a.run_dir))}/steps/` +
        `${encodeURIComponent(a.step)}/summary" title="human-readable ` +
        `end-of-step report, generated from the ATIF export - also saved ` +
        `as <step>.summary.md in the run dir once the step ends">` +
        `Session summary ↗</a><span class="sub" style="margin-left:10px">` +
        `status, elapsed, tokens, trajectory, commits, stdout tail - ` +
        `readable markdown instead of a raw log.</span></div>` +
        (a.status==='running'
          ? `<div class="actions"><button type="button" class="btn danger" ` +
            `data-stop-agent="${esc(base(a.run_dir))}" ` +
            `data-step="${esc(a.step)}" ` +
            `title="kill this agent's process tree now - the run ` +
            `proceeds to the next step">Stop this step</button>` +
            `<span class="sub" style="margin-left:10px">tree-kills the ` +
            `devin process; committed work stays. The run continues; ` +
            `Resume re-runs the step.</span></div>` : '') +
        (((a.status==='queued'||a.status==='waiting') && !a.skipped_by_user)
          ? `<div class="actions"><button type="button" ` +
            `class="btn secondary" ` +
            `data-skip-step="${esc(base(a.run_dir))}" ` +
            `data-step="${esc(a.step)}" ` +
            `title="never launch this step while marked - it keeps its ` +
            `queue position and stays skipped (even across resume) ` +
            `until you Unskip">Skip step</button>` +
            `<span class="sub" style="margin-left:10px">the step records ` +
            `'skipped' when its turn comes - nothing is launched.</span>` +
            `</div>` : '') +
        (a.skipped_by_user
          ? `<div class="actions"><button type="button" class="btn" ` +
            `data-unskip-step="${esc(base(a.run_dir))}" ` +
            `data-step="${esc(a.step)}" ` +
            `title="remove the skip mark - the step runs in its original ` +
            `queue position (if its turn already passed, on the next ` +
            `resume)">Unskip step</button></div>` : '') +
        ((a.status==='running'||a.status==='queued')
          ? `<div class="actions"><label for="d-timeout" ` +
            `style="margin:0">timeout (min)</label>` +
            `<input id="d-timeout" type="number" min="1" max="1440" ` +
            `value="${a.timeout_min??''}" style="width:90px">` +
            `<button type="button" class="btn secondary" ` +
            `data-set-timeout="${esc(base(a.run_dir))}" ` +
            `data-step="${esc(a.step)}">Save</button>` +
            `<span class="sub">moves the live deadline on the next poll ` +
            `(&lt;15s); a value under elapsed ends the step early, ` +
            `retries use it too.</span></div>` : '') +
        (a.prompt_editable
          ? `<h3 style="margin-top:14px">Edit prompt` +
            (a.prompt_edited || a.prompt_override!=null
              ? ` <span class="sub">(override saved - the manifest ` +
                `itself is unchanged)</span>` : '') + `</h3>` +
            `<textarea id="d-prompt" style="min-height:120px" ` +
            `spellcheck="false">${esc(pd!==undefined?pd:
              (a.prompt_override ?? a.task_full ?? a.task ?? ''))}` +
            `</textarea><div class="actions">` +
            `<button type="button" class="btn secondary" ` +
            `data-save-prompt="${esc(base(a.run_dir))}" ` +
            `data-step="${esc(a.step)}">Save prompt</button>` +
            (a.prompt_override!=null
              ? `<button type="button" class="btn secondary" ` +
                `data-revert-prompt="${esc(base(a.run_dir))}" ` +
                `data-step="${esc(a.step)}">Revert to manifest</button>` : '') +
            `<span class="sub">what this step's next session is ` +
            `dispatched with; locks the moment the step starts.</span>` +
            `</div>` : '') +
        (!a.prompt_editable && pd!==undefined
          ? `<div class="notice warn">This step started while the edit ` +
            `was open - its prompt was already dispatched, so the edit ` +
            `was NOT applied. The unsaved text is kept below` +
            (a.notes_enabled ? ` - put follow-up context in resume ` +
              `notes instead.` : ` - copy it out before closing.`) +
            `</div><textarea disabled style="min-height:70px">` +
            `${esc(pd)}</textarea>` : '') +
        (a.notes_enabled
          ? `<h3 style="margin-top:14px">Resume notes</h3>` +
            `<textarea id="d-rnotes" style="min-height:70px" ` +
            `spellcheck="false" placeholder="context for the resume, ` +
            `e.g. VPN is restored">${esc(nd!==undefined?nd:
              (a.resume_notes||''))}</textarea><div class="actions">` +
            `<button type="button" class="btn secondary" ` +
            `data-save-notes="${esc(base(a.run_dir))}" ` +
            `data-step="${esc(a.step)}">Save notes</button>` +
            `<span class="sub">appended to this step's next dispatched ` +
            `prompt - the original prompt is never edited. Empty + ` +
            `Save clears stored notes.</span></div>` : '')) : '') +
    (a.request_id ? `<div class="actions">` +
      (a.step==='plan-proposal' && a.status==='running'
        ? `<button type="button" class="btn danger" ` +
          `data-stop-gen="${esc(a.request_id)}" ` +
          `title="cancel this planning pass - the request stays editable ` +
          `and any previous proposal is kept">Stop proposal</button>` : '') +
      `<button type="button" class="btn secondary" ` +
      `data-open-request="${esc(a.request_id)}">View request / runbook</button></div>` : '') +
    (a.tail ? `<h3 style="margin-top:14px">stdout tail</h3><pre id="dtail">${esc(a.tail)}</pre>` : '') +
    (a.arc && a.arc.groups && a.arc.groups.length
      ? `<h3 style="margin-top:14px">trajectory` +
        ` <span class="sub" style="margin:0">${a.arc.steps} LLM steps, ` +
        `grouped by phase - expand a row for per-step times & real ` +
        `per-turn tokens</span></h3>` +
        `<div class="arc">` + a.arc.groups.map(g =>
          `<details class="arcgrp"><summary><span class="arcspan">` +
          `${esc(g.span)}</span><span class="arcphase arc-` +
          `${esc(g.phase)}">${esc(g.phase)}</span>` +
          `<span class="arctime">${loc(g.t0)}</span>` +
          `<span class="arctok" title="billed in / out / cached ` +
          `(per-turn ATIF metrics, not estimates)">${g.tin!=null
            ? kfmt(Math.max(g.tin-(g.tcached||0),0))+'/'+
              kfmt(g.tout||0)+'/'+kfmt(g.tcached||0)
            : ''}</span>` +
          `<span class="archead">${esc(g.head)}</span></summary>` +
          `<div class="arcdetail">` + (g.entries||[]).map(e =>
            `<div class="arcrow"><span class="arcspan">${esc(e.sid)}` +
            `</span><span class="arctime">${loc(e.ts)}</span>` +
            `<span class="arcdur">${e.dur_s!=null?fmtS(e.dur_s):''}` +
            `</span><span class="arctok" title="billed in / out / ` +
            `cached (full prompt = in+cached: ${e.in||'-'})">` +
            `${e.in!=null
              ? kfmt(Math.max(e.in-(e.cached||0),0))+'/'+
                kfmt(e.out||0)+'/'+kfmt(e.cached||0)
              : ''}</span>` +
            `<span class="archead" title="${esc(e.head)}">` +
            `${esc(e.head)}</span></div>`).join('') +
          `</div></details>`).join('') + `</div>` : '');
  if (fsel){
    const el = document.getElementById(fid);
    if (el && el.focus){ el.focus();
      try{ el.setSelectionRange(fsel[0],fsel[1]); } catch(e){} }
  }
  document.getElementById('backdrop').hidden = false;
}

function showRunDetails(key){
  const r = lastData?.runs.find(x=>runKey(x)===key);
  if (!r || !closeDetail()) return;
  const total = Object.values(r.counts||{}).reduce((s,n)=>s+n,0);
  const runUsd = (r.cost&&r.cost.usd!=null)||(r.plan_cost&&r.plan_cost.usd!=null)
    ? (r.cost&&r.cost.usd||0)+(r.plan_cost&&r.plan_cost.usd||0) : null;
  const kv = [['description',r.description||'-'],['projects',(r.projects||[]).join(', ')],
    ['started',r.started_at],['ended',r.ended_at],
    ['steps',(total?`${total} total · `:'')+
      Object.entries(r.counts||{}).map(([s,n])=>`${n} ${s}`).join(', ')],
    ['tokens',tokTitle(r.tokens)||'-'],
    ['est cost',runUsd!=null?fmtC({usd:runUsd})+
      (r.cost&&r.cost.unpriced?` (+${r.cost.unpriced} step(s) unpriced)`:'')+
      ` (self-serve rates, plan + steps)`:'-'],
    ['manifest / dir',r.dir],
    ['sessions', AGENTS.filter(a=>(a.run_key||a.run_dir)===key &&
      a.session_id).map(a=>`${a.step}: ${a.session_id}`).join(' \u00b7 ')
      ||'-']];
  const canResume = Object.keys(r.counts||{}).length &&
    !Object.keys(r.counts).some(s=>['running','queued'].includes(s)) &&
    Object.keys(r.counts).some(s=>s!=='done');
  // Steps whose worktree branch never landed (conflict parked the worktree,
  // or the conductor died between the wave and the merge) - mergeable from
  // here once nothing in the run is live.
  const pendingMerge = AGENTS.some(a=>a.branch && a.merged!==true &&
    (a.run_key||a.run_dir)===key &&
    !['running','queued'].includes(a.status));
  const liveSteps = AGENTS.filter(a=>(a.run_key||a.run_dir)===key &&
    ['running','queued'].includes(a.status));
  // Paused vs interrupted: 'paused' means someone clicked Pause run (a
  // graceful hold - in-flight steps finished first); a run with no
  // ended_at and nothing live means the conductor itself died (crash or
  // reboot). Both resume from committed state.
  const interrupted = !r.ended_at && !r.aborted && !liveSteps.length &&
    Object.keys(r.counts||{}).some(s=>s!=='done');
  const notice = (r.aborted==='paused' && canResume)
    ? `<div class="notice warn">Paused - in-flight steps finished and ` +
      `nothing new launched. Resume continues where it left off ` +
      `(safe across a restart).</div>`
    : interrupted
    ? `<div class="notice warn">Interrupted - the conductor process is ` +
      `gone (crash or machine restart). Resume picks up unfinished ` +
      `steps from committed state.</div>` : '';
  document.getElementById('dbody').innerHTML =
    `<h3 id="modal-title">${esc(r.run)}</h3>` +
    (r.stage ? `<span class="stage ${esc(r.stage)}">${esc(STAGES[r.stage]||r.stage)}</span>` : '') +
    notice +
    `<div class="kv" style="margin-top:12px">` +
    kv.map(([k,v])=>`<div class="k">${k}</div><div class="wrap">${esc(v??'-')}` +
      (k==='tokens'?tokenHelp:'') + `</div>`).join('') + '</div>' +
    `<div class="actions"><button type="button" class="btn secondary" ` +
    `data-filter-run="${esc(key)}">Show only this run</button>` +
    (r.request_id ? `<button type="button" class="btn secondary" ` +
      `data-open-request="${esc(r.request_id)}">View original request / TOML</button>` : '') +
    (r.stage==='generating' && r.request_id
      ? `<button type="button" class="btn danger" ` +
        `data-stop-gen="${esc(r.request_id)}" ` +
        `title="cancel this planning pass - the request stays editable ` +
        `and any previous proposal is kept">Stop proposal</button>` : '') +
    (pendingMerge ? `<button type="button" class="btn" ` +
      `data-merge-run="${esc(base(r.dir))}" ` +
      `data-rk="${esc(key)}">Merge pending branches</button>` : '') +
    (liveSteps.length ? `<button type="button" class="btn" ` +
      `data-pause-run="${esc(base(r.dir))}" ` +
      `title="let in-flight steps finish, then hold the run - nothing ` +
      `new launches until Resume">Pause run</button>` +
      `<button type="button" class="btn danger" ` +
      `data-stop-run="${esc(base(r.dir))}" ` +
      `title="kill every live step's process tree now - committed work ` +
      `stays; nothing new launches; Resume restarts unfinished steps">` +
      `Stop run now</button>` : '') +
    (canResume ? `<button type="button" class="btn" ` +
      `data-resume-run="${esc(base(r.dir))}" ` +
      `title="re-run every non-done step in a fresh session - covers ` +
      `paused, stopped and interrupted runs">` +
      `Resume unfinished steps</button>` : '') +
    (r.stage ? '' : `<button type="button" class="btn secondary" ` +
      `data-arch-run="${esc(base(r.dir))}" ` +
      `data-arch-set="${r.archived?0:1}" ` +
      `title="${r.archived
        ? 'remove the archived stamp - the run returns to the default view'
        : 'hide this run from the default view (auto-archive does this ' +
          'after 48h finished; files stay on disk)'}">` +
      `${r.archived?'Unarchive':'Archive run'}</button>` +
      `<button type="button" class="btn danger" ` +
      `data-delete-run="${esc(base(r.dir))}"` +
      `${liveSteps.length?' disabled':''} ` +
      `title="${liveSteps.length
        ? 'disabled while a step is still live - stop the run first'
        : 'permanently delete this run record - state, logs, prompts, ' +
          'exports'}">Delete run</button>`) +
    '</div>';
  document.getElementById('backdrop').hidden = false;
}
async function api(method,path,body){
  const opt = method==='GET' ? {} : {method,
    headers:{'Content-Type':'application/json'}, body:JSON.stringify(body||{})};
  const res=await fetch(path,opt);
  const out=await res.json();
  if (!res.ok) throw new Error(out.error||`HTTP ${res.status}`);
  return out;
}
async function refresh(){ render(await api('GET','/api/state' +
  (showArchived?'?archived=1':''))); }
function effortOptionsFor(model){
  const e=(lastData?.model_options||{})[model];
  return (e && e.length ? e : ['min','low','medium','high','max'])
    .map(x=>`<option value="${esc(x)}">`).join('');
}
// 'adaptive' is a routing mode with no effort variant - the effort field is
// cleared and greyed out. Any other model switch re-checks the current
// effort against that family's suffixes and drops a value it no longer has.
function syncEffort(modelInp, inputId, listId){
  const inp=document.getElementById(inputId), dl=document.getElementById(listId);
  const model=(modelInp?.value||'').trim();
  if (!inp) return;
  if (model.toLowerCase()==='adaptive'){
    inp.value=''; inp.disabled=true;
    inp.placeholder='n/a - adaptive chooses its own effort';
    if (dl) dl.innerHTML='';
    return;
  }
  inp.disabled=modelInp?modelInp.disabled:false;
  inp.placeholder='';
  const opts=(lastData?.model_options||{})[model]||[];
  if (dl) dl.innerHTML=effortOptionsFor(model);
  if (inp.value && opts.length && !opts.includes(inp.value)) inp.value='';
}
function openNew(){
  if (!closeDetail()) return;
  const defaults=lastData?.defaults||{model:'swe-2',effort:'max'};
  modalRequest={id:null,status:'new',dirty:false,originalManifest:''};
  showRequestForm({status:'new',raw:'',label:'',project:'',...defaults});
}
function showRequestForm(rec){
  const stage=rec.status, editable=!['generating','launching','launched'].includes(stage);
  const disabled=editable?'':' disabled';
  rqAttachments=Array.isArray(rec.attachments)?rec.attachments.slice():[];
  const choices=lastData?.project_options||[];
  const projectOptions=`<option value="">Infer from request</option>` +
    choices.map(p=>`<option value="${esc(p.slug)}"${p.slug===rec.project?' selected':''}>` +
      `${esc(p.slug)}${p.cloud_ok?'':' (local only)'}</option>`).join('');
  const modelList=(lastData?.model_options &&
    Object.keys(lastData.model_options).length)
    ? Object.keys(lastData.model_options) : ['swe-2','gpt-6-luna'];
  if (!modelList.includes('adaptive')) modelList.push('adaptive');
  const modelOptions=modelList.map(m=>`<option value="${esc(m)}">`).join('');
  const steps=rec.proposed_steps||[];
  const canEdit=editable && stage!=='new';
  document.getElementById('dbody').innerHTML =
    `<h3 id="modal-title">${stage==='new'?'New request':
      stage==='ready'?'Review proposed run':
      stage==='launched'?'Original request': 'Request details'}</h3>` +
    (stage==='new'?'':`<span class="stage ${esc(stage)}">` +
      `${esc(STAGES[stage]||stage)}</span>`) +
    `<div class="notice">Save keeps your request as a draft. Generate asks Devin ` +
      `to propose a TOML runbook only; no implementation starts. ` +
      `Starting the run requires a separate, explicit click. Generated TOML ` +
      `is a tracked file: review it before sharing.</div>` +
    (rec.error ? `<div class="notice error">${esc(rec.error)}</div>`:'') +
    // no 'fields changed' nag while planning is in flight - the change is
    // being regenerated/answered already
    (rec.stale && stage!=='generating' && stage!=='questions'
      ? `<div class="notice warn">Request fields changed since ` +
      `the proposal. Regenerate tasks, or use <b>Apply options to TOML</b> ` +
      `to write the model/effort/attempts/failure/worktree options into ` +
      `the manifest without regenerating (request text and project ` +
      `changes need regeneration).</div>`:'') +
    (stage==='generating'?`<div class="notice warn">Planning in progress. ` +
      `The previous proposal, if any, remains available if generation ` +
      `fails. <b>Stop proposal</b> cancels this pass and returns the form ` +
      `to editing.</div>`:'') +
    (stage==='questions'?`<div class="notice warn">The planner paused for ` +
      `your answers before writing the TOML ` +
      `(${(rec.pending_questions||[]).length} pending). Answer them via the ` +
      `button below, or edit the request and Regenerate.</div>`:'') +
    (rec.gen_tail && (stage==='generating'||stage==='error'||
                      stage==='questions')
      ? `<label>Planner output${stage==='generating'?' (live)':''}</label>` +
        `<pre id="gtail">${esc(rec.gen_tail)}</pre>`:'') +
    (stage==='launching'||stage==='launched'?`<div class="notice">This ` +
      `request has already been started; editing and discard are disabled.</div>`:'') +
    `<label for="rq-label">Short label (optional until planned)</label>` +
    `<input id="rq-label" maxlength="80" value="${esc(rec.label||'')}"${disabled}>` +
    `<div class="field-row"><div><label for="rq-project">Project</label>` +
    `<select id="rq-project"${disabled}>${projectOptions}</select></div>` +
    `<div><label for="rq-plan_model">Planner model</label>` +
    `<input id="rq-plan_model" list="rq-models" ` +
    `value="${esc(rec.plan_model||rec.model||'')}"${disabled}></div>` +
    `<div><label for="rq-plan_effort">Planner effort</label>` +
    `<input id="rq-plan_effort" list="rq-plan_efforts" ` +
    `value="${esc(rec.plan_effort||rec.effort||'')}"${disabled}>` +
    `<datalist id="rq-plan_efforts">` +
    `${effortOptionsFor(rec.plan_model||rec.model)}</datalist>` +
    `</div></div>` +
    `<div class="field-row"><div><label for="rq-model">Step model</label>` +
    `<input id="rq-model" list="rq-models" ` +
    `value="${esc(rec.model||'')}"${disabled}>` +
    `<datalist id="rq-models">${modelOptions}</datalist></div>` +
    `<div><label for="rq-effort">Step effort</label>` +
    `<input id="rq-effort" list="rq-efforts" ` +
    `value="${esc(rec.effort||'')}"${disabled}>` +
    `<datalist id="rq-efforts">${effortOptionsFor(rec.model)}</datalist>` +
    `</div><div></div></div>` +
    `<div class="field-row"><div><label for="rq-on_failure">On step failure</label>` +
    `<select id="rq-on_failure"${disabled}>` +
      `<option value="continue"${rec.on_failure!=='stop'?' selected':''}>` +
      `continue - keep the run going</option>` +
      `<option value="stop"${rec.on_failure==='stop'?' selected':''}>` +
      `stop - halt the run</option></select></div>` +
    `<div><label for="rq-attempts">Attempts/step</label>` +
    `<input id="rq-attempts" type="number" min="1" max="10" ` +
      `value="${esc(rec.max_attempts??3)}"${disabled}></div><div></div></div>` +
    `<label for="rq-raw">Your request</label><textarea id="rq-raw"${disabled} ` +
    `placeholder="Describe the work in your own words…">${esc(rec.raw||'')}</textarea>` +
    `<div id="rq-attach" class="attach-strip" hidden></div>` +
    (editable?`<div class="sub">Attach images by dropping image files ` +
      `anywhere on this dialog or pasting a screenshot - each is saved ` +
      `under data/attachments and its path is inserted into the request. ` +
      `Remove one with its &times; any time before generating or ` +
      `starting.</div>`:'') +
    `<label class="check"><input type="checkbox" id="rq-followup"` +
      `${rec.followup?' checked':''}${disabled}><span>End the run with a ` +
      `planning step that drafts a follow-up manifest into manifests/ ` +
      `for anything unfinished</span></label>` +
    `<label class="check"><input type="checkbox" id="rq-worktrees"` +
      `${rec.worktrees?' checked':''}${disabled}><span>Run every step in ` +
      `its own git worktree (<code>worktree = true</code>): changes land ` +
      `on a conductor/&lt;run&gt;/&lt;step&gt; branch and merge back ` +
      `after each wave - required anyway for same-project parallel ` +
      `steps</span></label>` +
    `<label class="check"><input type="checkbox" id="rq-merge_resolver"` +
      `${rec.merge_resolver?' checked':''}${disabled}><span>On a ` +
      `worktree merge conflict, spawn a resolve-* session to finish the ` +
      `merge (<code>merge_resolver = true</code>) instead of only ` +
      `parking the worktree for a human</span></label>` +
    `<label class="check"><input type="checkbox" id="rq-ask"` +
      `${rec.ask?' checked':''}${disabled}><span>Ask me questions if ` +
      `needed: when the planner hits a genuine ambiguity while drafting, ` +
      `it may pause with a questionnaire (one question per dialog - ` +
      `suggested options, free text, skip) and refine the TOML with your ` +
      `answers. Unchecked = it decides everything itself.</span></label>` +
    (rec.qa&&rec.qa.length?`<details class="proposal"><summary>Planner ` +
      `Q&amp;A (${rec.qa.length}) - the answers the current draft was ` +
      `refined with</summary><ol class="proposal-list">` +
      rec.qa.map(a=>`<li>${esc(a.question)}<br><small>` +
        `${esc(a.skipped?'(skipped)':
          ((a.answer||[]).concat(a.text?[a.text]:[]).join('; ')||'-'))}` +
        `</small></li>`).join('') + `</ol></details>`:'') +
    (steps.length?`<h3 style="margin:16px 0 0">Proposed steps (${steps.length})</h3>` +
      `<ol class="proposal-list">${steps.map(s=>`<li><b>${esc(s.step)}</b> ` +
        `<small>${esc(s.slug)} · ${esc(s.model||'-')} / ${esc(s.effort||'-')}` +
        `${s.wt?' · worktree':''}</small><br>${esc(s.task||'')}</li>`).join('')}</ol>`:'') +
    (rec.manifest ? `<details class="proposal"><summary>Edit proposed TOML ` +
      `(steps, verify, model, effort and description)</summary>` +
      `<div class="notice">The TOML is the plan that will run. Changes to ` +
      `the request fields above need regeneration, or an explicit TOML ` +
      `edit and save. No task executes while you edit.</div>` +
      `<label for="rq-manifest">Runbook · ${esc(rec.manifest)}</label>` +
      `<textarea id="rq-manifest" class="toml" spellcheck="false"${disabled}>` +
      `${esc(rec.manifest_text||'')}</textarea></details>`:'') +
    `<div id="form-msg" class="notice" hidden></div>` +
    `<div class="actions">` +
    (stage==='generating'
      ? `<button type="button" class="btn danger" data-action="stop" ` +
        `title="cancel this planning pass - the request fields stay as ` +
        `they were and any previous proposal is kept">` +
        `Stop proposal</button>`:'') +
    (stage==='questions'&&(rec.pending_questions||[]).length
      ? `<button type="button" class="btn attn" ` +
        `data-open-questions="${esc(rec.id)}">Answer questions (` +
        `${(rec.pending_questions||[]).length})</button>`:'') +
    (editable?`<button type="button" class="btn secondary" data-action="save">` +
      `${stage==='new'||stage==='draft'?'Save draft':'Save edits'}</button>` +
      `<button type="button" class="btn${rec.stale?' attn':''}" ` +
      `data-action="generate">` +
      `${rec.manifest?'Regenerate tasks':'Generate proposal'}</button>` +
      (rec.manifest?`<button type="button" ` +
        `class="btn secondary${rec.stale?' attn':''}" data-action="apply" ` +
        `title="Rewrite the TOML to match the fields above (model, effort, ` +
        `attempts, failure policy, worktrees, resolver) without a planner ` +
        `call - request text and project changes still need Regenerate">` +
        `Apply options to TOML</button>`:''):'') +
    ((stage==='error'||stage==='questions')&&rec.manifest_text
      ? `<button type="button" class="btn secondary" data-action="restore"` +
        ` title="republish the previous proposal and drop the pending ` +
        `questions">Keep previous plan</button>`:'') +
    (stage==='ready'||stage==='launch_failed' ? `<button type="button" ` +
      `class="btn" data-action="run"${rec.stale?' disabled':''}>` +
      `${stage==='launch_failed'?'Retry start':'Start run'}</button>`:'') +
    (canEdit?`<button type="button" class="btn danger" data-action="discard">` +
      `Discard</button>`:'') +
    (stage==='launched'?`<button type="button" class="btn secondary" ` +
      `data-run-details="request:${esc(rec.id)}">View run details</button>`:'') +
    `</div>`;
  document.getElementById('backdrop').hidden = false;
  renderAttach(editable);
  syncEffort(document.getElementById('rq-model'),'rq-effort','rq-efforts');
  syncEffort(document.getElementById('rq-plan_model'),
             'rq-plan_effort','rq-plan_efforts');
}
// --- request image attachments -----------------------------------------
// Thumbnail strip under 'Your request' fed by file drops and clipboard
// pastes: each image uploads to POST /api/attachments (saved under
// data/attachments) and its absolute path lands in the text at the caret.
// The [{name,path}] list persists on the request JSON - chips are
// removable until the request reaches a non-editable stage.
function renderAttach(editable){
  const strip=document.getElementById('rq-attach');
  if(!strip) return;
  strip.innerHTML=rqAttachments.map(a=>
    `<span class="attach-item"><a href="/api/attachments/` +
    `${encodeURIComponent(a.name)}" target="_blank" ` +
    `title="${esc(a.path)}"><img src="/api/attachments/` +
    `${encodeURIComponent(a.name)}" alt="${esc(a.name)}"></a>` +
    (editable?`<button type="button" class="attach-x" ` +
      `data-attach-rm="${esc(a.name)}" title="Remove this attachment">` +
      `\u00d7</button>`:'') +
    `</span>`).join('');
  strip.hidden=!rqAttachments.length;
}
// The editable request textarea gates every drop/paste listener - other
// modals (agent detail, questionnaire) reuse the same backdrop, so a bare
// document-level bind must bail when rq-raw is absent or disabled.
function attachTarget(){
  const ta=document.getElementById('rq-raw');
  return (ta && !ta.disabled && !modalQA) ? ta : null;
}
function insertAtCaret(ta,text){
  // NL via charCode: a backslash-n written into this Python string would
  // arrive in the browser as a real newline mid-literal - trap 16.
  const NL=String.fromCharCode(10);
  const s=ta.selectionStart==null?ta.value.length:ta.selectionStart;
  const e=ta.selectionEnd==null?s:ta.selectionEnd;
  const before=ta.value.slice(0,s), after=ta.value.slice(e);
  const pad=(before && !before.endsWith(NL)?NL:'') + text + NL +
    (after && !after.startsWith(NL)?NL:'');
  ta.value=before+pad+after;
  ta.selectionStart=ta.selectionEnd=before.length+pad.length;
}
async function attachFiles(files){
  const ta=attachTarget();
  if(!ta || !files) return;
  const imgs=[...files].filter(f=>(f.type||'').startsWith('image/'));
  if(!imgs.length){ formError('Only image files can be attached.'); return; }
  for (const f of imgs){
    try {
      const res=await fetch('/api/attachments',{method:'POST',
        headers:{'Content-Type':f.type||'application/octet-stream'},
        body:f});
      const out=await res.json().catch(()=>({}));
      if(!res.ok) throw new Error(out.error||('HTTP '+res.status));
      if(!rqAttachments.some(x=>x.name===out.name)) rqAttachments.push(out);
      renderAttach(true);
      insertAtCaret(ta,out.path);
      if(modalRequest) modalRequest.dirty=true;
      try{ta.dispatchEvent(new Event('input',{bubbles:true}));}catch(e){}
    } catch(err){ formError('Attach failed: '+err.message); }
  }
}
async function openRequest(id){
  if (!closeDetail()) return;
  modalRequest={id,status:'loading',dirty:false,originalManifest:''};
  document.getElementById('dbody').innerHTML =
    '<h3 id="modal-title">Loading request…</h3>';
  document.getElementById('backdrop').hidden = false;
  try {
    const rec=await api('GET',`/api/requests/${encodeURIComponent(id)}`);
    if (!modalRequest || modalRequest.id!==id || modalRequest.dirty) return;
    modalRequest={id,status:rec.status,dirty:false,
      originalManifest:rec.manifest_text||'',record:rec};
    showRequestForm(rec);
  } catch(err){
    document.getElementById('dbody').innerHTML =
      `<h3 id="modal-title">Request unavailable</h3><div class="notice error">` +
      `${esc(err.message)}</div>`;
  }
}
function requestFields(){
  const f=Object.fromEntries(
    ['raw','label','project','model','effort','on_failure',
     'plan_model','plan_effort'].map(k=>
    [k,document.getElementById(`rq-${k}`).value]));
  for (const k of ['followup','worktrees','merge_resolver','ask']){
    const el=document.getElementById(`rq-${k}`);
    if (el) f[k]=el.checked;
  }
  const att=document.getElementById('rq-attempts');
  if (att) f.max_attempts=att.value;
  f.attachments=rqAttachments.map(a=>({name:a.name,path:a.path}));
  return f;
}
function formError(message){
  const el=document.getElementById('form-msg');
  if (el){el.hidden=false;el.className='notice error';el.textContent=message;}
}
async function requestAction(action){
  if (!modalRequest) return;
  const id=modalRequest.id, rec=modalRequest.record||{};
  const manifestEl=document.getElementById('rq-manifest');
  const editedManifest=!!manifestEl &&
    manifestEl.value!==modalRequest.originalManifest;
  if (action==='discard' && !confirm('Discard this request, its proposal TOML, and all planned steps?')) return;
  if (action==='generate' && rec.manifest && !confirm(
      'Regenerate the proposal? Your current TOML will be replaced only if generation succeeds.')) return;
  if (action==='stop' && !confirm(
      'Stop preparing this proposal? The planner session is cancelled - ' +
      'your request and any previous proposal are kept.')) return;
  if (action==='run'){
    if (modalRequest.dirty){ formError('Save and review edits before starting.'); return; }
    if (!confirm(`Start ${rec.run} now? This will execute ${rec.proposed_steps?.length||0} ` +
                 `agent steps and make git checkpoints.`)) return;
  }
  const buttons=document.querySelectorAll('#detail [data-action]');
  buttons.forEach(b=>b.disabled=true);
  try {
    let rid=id;
    if (action==='apply' && !rid){ formError('Nothing to apply yet - ' +
      'generate a proposal first.'); return; }
    if (action==='save'||action==='generate'||action==='restore'||
        action==='apply'){
      const fields=requestFields();
      if (!rid){
        const saved=await api('POST','/api/requests',fields);
        rid=saved.id; modalRequest.id=rid;
      } else if (action==='apply'){
        // Apply patches the proposal on top of the textarea's TOML when it
        // has unsaved edits, so a hand edit is never silently replaced.
        if (editedManifest) fields.manifest=manifestEl.value;
        await api('POST',`/api/requests/${rid}/apply`,fields);
      } else {
        if (action==='restore'||action==='save'&&editedManifest)
          fields.manifest=manifestEl.value;
        await api('POST',`/api/requests/${rid}/save`,fields);
      }
      if (action==='generate')
        await api('POST',`/api/requests/${rid}/generate`,{});
    } else if (action==='run'){
      await api('POST',`/api/requests/${rid}/run`,{});
    } else if (action==='stop'){
      if (!rid) return;
      await api('POST',`/api/requests/${rid}/stop`,{});
    } else if (action==='discard'){
      await api('POST',`/api/requests/${rid}/discard`,{confirm:true});
      selectedRun=null;
      closeDetail(true);
      await refresh();
      return;
    }
    modalRequest.dirty=false;
    await refresh();
    await openRequest(rid);
  } catch(err){ formError(err.message); }
  finally { buttons.forEach(b=>b.disabled=false); }
}

// --- planner questionnaire ('ask me questions' checkbox) ---------------
// One question per modal, mirroring the ask_question tool: suggested
// options as toggle cards + a free-text field + Skip. Each answer POSTs
// immediately (progress survives a closed tab); when the last pending
// question is answered the server restarts generation as the refine pass.
async function openQuestions(id){
  if (modalQA && modalQA.id===id && modalQA.rec){ showQuestion(); return; }
  modalQA={id,rec:null};                 // claim the slot during the fetch
  let rec;
  try { rec=await api('GET',`/api/requests/${encodeURIComponent(id)}`); }
  catch(e){ modalQA=null; return; }
  if (!(rec.pending_questions||[]).length){ modalQA=null;
    openRequest(id); return; }           // answered meanwhile -> normal form
  if (!closeDetail()){ modalQA=null; return; }
  modalQA={id,rec,sel:new Set(),free:''};
  showQuestion();
  document.getElementById('backdrop').hidden=false;
}
function showQuestion(){
  const rec=modalQA.rec, pend=rec.pending_questions||[], q=pend[0];
  if (!q){ modalQA=null; closeDetail(true); refresh(); return; }
  const done=(rec.qa||[]).length, total=done+pend.length;
  document.getElementById('dbody').innerHTML =
    `<h3 id="modal-title">Planner question ${done+1} of ${total}</h3>` +
    `<div class="notice">The planner hit a decision it wants you to make ` +
      `before writing the TOML${esc(rec.label?` - ${rec.label}`:'')}. ` +
      `Your answers feed a second planning pass; skipping leaves the call ` +
      `to the planner.</div>` +
    (q.header?`<span class="stage questions">${esc(q.header)}</span>`:'') +
    `<p style="font-size:14px;margin:12px 0 4px">${esc(q.question)}</p>` +
    ((q.options||[]).length
      ? `<div class="qopts">` + q.options.map(o=>
          `<button type="button" ` +
          `class="qopt${modalQA.sel.has(o.label)?' on':''}" ` +
          `data-qopt="${esc(o.label)}"><b>${esc(o.label)}</b>` +
          (o.description?`<span class="sub">${esc(o.description)}</span>`:'') +
          `</button>`).join('') + `</div>` +
        `<div class="sub">${q.multi_select
          ? 'pick any that apply, or write your own below'
          : 'pick one, or write your own below'}</div>` : '') +
    `<input id="q-free" maxlength="2000" ` +
      `placeholder="your own answer (optional)" value="${esc(modalQA.free)}">` +
    `<div id="form-msg" class="notice" hidden></div>` +
    `<div class="actions">` +
    `<button type="button" class="btn" data-qsend>Send answer</button>` +
    `<button type="button" class="btn secondary" data-qskip ` +
      `title="no preference - the planner decides">Skip</button>` +
    `<button type="button" class="btn secondary" data-qskipall ` +
      `title="skip this and every remaining question, then generate">` +
      `Skip all</button>` +
    `<span class="sub">closing keeps questions pending on the request - ` +
      `they are not lost.</span>` +
    `</div>`;
  const fi=document.getElementById('q-free');
  if (fi && fi.focus) fi.focus();
}
async function answerQuestion(body){
  const qbtns=document.querySelectorAll(
    '#detail [data-qsend],#detail [data-qskip],#detail [data-qskipall]');
  qbtns.forEach(b=>b.disabled=true);
  try {
    const out=await api('POST',
      `/api/requests/${encodeURIComponent(modalQA.id)}/answer`, body);
    if ((out.pending_questions||[]).length && out.status==='questions'){
      modalQA.rec=out; modalQA.sel=new Set(); modalQA.free='';
      showQuestion();
    } else {
      modalQA=null; closeDetail(true); await refresh();
    }
  } catch(err){ formError(err.message); qbtns.forEach(b=>b.disabled=false); }
}

let lastSig = null, BOOT = null;
function tickElapsed(now){
  for (const a of AGENTS){
    const td = document.querySelector(`td[data-el="${a._i}"]`);
    if (td) td.textContent = elapsed(a, now);
  }
}
function render(d){
  if (BOOT===null) BOOT = d.serve_boot;
  else if (d.serve_boot !== BOOT){ location.reload(); return; }
  lastData = d; AGENTS = d.agents;
  AGENTS.forEach((a,i)=>a._i=i);   // stable cell ids before the sig check
  if (selectedRun && !d.runs.some(r=>runKey(r)===selectedRun)) selectedRun=null;
  const now = Date.now();
  const live = d.agents.filter(a=>a.status==='running');
  const planned = d.agents.filter(a=>a.status==='planned').length;
  document.getElementById('meta').innerHTML =
    `${d.agents.length-planned} agents | ${planned} planned | ${live.length} running | ` +
    `cpu ${d.cpu_count||'?'} cores | free ram ${d.free_ram_gb||'?'} GB | ` +
    `refreshed ${loc(d.generated_at)}` +
    (d.pricing && d.pricing.models
      ? ` | ${d.pricing.models} model rates` +
        (d.pricing.fetched ? ` @ ${loc(d.pricing.fetched)}` : '')
      : ' | rates unavailable') +
    (d.auth && d.auth!=='ok'
      ? ` <b style="color:#f85149">AUTH: ${esc(d.auth)}</b>` +
        ` <a href="#" data-auth="login">[log in]</a>` +
        ` <a href="#" data-auth="recheck">[recheck]</a>`
      : ` <span style="color:#3fb950">auth ok</span>`);
  // Only rebuild the tables when the payload actually changes - rebuilding
  // innerHTML every 2s resets animations/hover and looks like a reload.
  // `tail` is excluded: live log growth streams into the open modal instead.
  const sig = JSON.stringify([d.runs, d.agents.map(({tail,...r})=>r),
    selectedRun, [...only].sort(), [...facets].sort(), expanded,
    runsShown===Infinity?'all':runsShown]);
  const tailEl = document.getElementById('dtail');
  if (sig === lastSig){
    tickElapsed(now);
    if (tailEl && detailIdx!=null && AGENTS[detailIdx]
        && tailEl.textContent !== AGENTS[detailIdx].tail)
      tailEl.textContent = AGENTS[detailIdx].tail;   // live stdout stream
    streamGenTail();
    return;
  }
  lastSig = sig;

  // status filter chips (click toggles)
  const scope = d.agents.filter(a=>!selectedRun ||
    (a.run_key||a.run_dir)===selectedRun);
  const byStatus = {};
  for (const a of scope) byStatus[a.status]=(byStatus[a.status]||0)+1;
  const selected = d.runs.find(r=>runKey(r)===selectedRun);
  const facetCounts = Object.fromEntries(FACETS.map(f=>
    [f,scope.filter(a=>matchesFacet(a,f)).length]));
  document.getElementById('filters').innerHTML =
    (selected?`<span class="filter-group"><span class="filter-label">run:</span>` +
      `<button type="button" class="chip active" data-clear-run ` +
      `aria-label="Clear run filter">${esc(selected.run)} ×</button></span>`:'') +
    `<span class="filter-group"><span class="filter-label">status:</span>` +
    ORDER.filter(s=>byStatus[s]).map(s=>
      `<button type="button" class="chip${only.has(s)?' active':''}` +
      `${only.size&&!only.has(s)?' off':''}" data-st="${s}" ` +
      `aria-pressed="${only.has(s)}" style="color:${COLORS[s]||'#8b949e'}" ` +
      `title="show only ${s}">${byStatus[s]} ${s}</button>`).join('') +
    '</span>' +
    `<span class="filter-group"><span class="filter-label">tags:</span>` +
    FACETS.filter(f=>facetCounts[f]||facets.has(f)).map(f=>
      `<button type="button" class="chip${facets.has(f)?' active':''}" ` +
      `data-facet="${f}" aria-pressed="${facets.has(f)}">` +
      `${facetCounts[f]} ${f}${facets.has(f)?' ×':''}</button>`).join('') +
    '</span>' +
    `<span class="filter-group"><span class="filter-label">view:</span>` +
    `<button type="button" class="chip${showArchived?' active':''}" ` +
    `data-arch aria-pressed="${showArchived}" ` +
    `title="list archived runs - finished runs are auto-archived after ` +
    `48h (CONDUCTOR_ARCHIVE_HOURS); files stay on disk, Unarchive in the ` +
    `run modal brings one back">${d.archived_runs||0} archived` +
    `${showArchived?' ×':''}</button></span>` +
    (only.size||facets.size?` <button type="button" class="chip" ` +
      `data-reset>clear filters</button>`:'');

  // runs table first - a run can span projects (chips show which)
  let h = '<colgroup><col style="width:13%"><col style="width:19%">' +
    '<col style="width:15%"><col style="width:9%"><col style="width:9%">' +
    '<col style="width:17%"><col style="width:5%">' +
    '<col class="hide-s" style="width:13%"></colgroup>' +
    '<tr><th>run</th><th>description</th><th>projects</th><th>started</th>' +
    '<th>ended</th><th title="per-status step counts, then token sums ' +
    'for the planner session and the worker steps, then estimated ' +
    'cost">steps</th>' +
    '<th title="parallel-agent cap for this run (run.max_parallel)">cap</th>' +
    '<th class="hide-s">dir</th></tr>';
  // rows are newest-first; active runs and unlaunched requests always show,
  // finished history is capped at SHOW_N until "show more"/"show all".
  // The status/tag chips also scope the runs table: a run stays visible
  // while at least one of its agents matches the active filters.
  const matchAgent = a => (!only.size||only.has(a.status)) &&
    [...facets].every(f=>matchesFacet(a,f));
  const allRuns = d.runs.filter(r=>!selectedRun||runKey(r)===selectedRun)
    .filter(r=>{
      if (!only.size && !facets.size) return true;
      const kids = d.agents.filter(a=>(a.run_key||a.run_dir)===runKey(r));
      return kids.length ? kids.some(matchAgent) : false;
    });
  const actRuns = allRuns.filter(r=>!r.ended_at);
  const finRuns = allRuns.filter(r=>r.ended_at);
  const finShow = Math.max(runsShown - actRuns.length, 0);
  const shownRuns = actRuns.concat(finRuns.slice(0, finShow));
  const hiddenRuns = finRuns.length - Math.min(finShow, finRuns.length);
  // Every real run gets a state label under its name: live, held
  // (paused/stopped/drained - all resumable), or interrupted (conductor
  // died - crash or machine restart). These chips all say "resume me" -
  // when every step is done there is nothing to resume, so a stale
  // 'aborted' left by a pre-fix resume must not keep labeling the row.
  const runStateChip = r => {
    const c = r.counts||{};
    const live = (c.running||0)+(c.queued||0);
    if (live) return `<span class="stage live" ` +
      `title="a step in this run is live - click the name for controls">` +
      `running</span>`;
    const left = Object.keys(c).some(s=>s!=='done');
    if (!left) return '';
    if (r.aborted==='paused')
      return `<span class="stage paused" title="deliberate hold after ` +
        `in-flight steps finished - Resume continues it">paused - ` +
        `resume ready</span>`;
    if (!r.ended_at && Object.keys(c).length)
      return `<span class="stage interrupted" title="conductor process ` +
        `died (crash/reboot) - Resume continues it">interrupted - ` +
        `resume ready</span>`;
    if (r.aborted) return `<span class="stage halted">${esc(r.aborted)}` +
      ` - resumable</span>`;
    return '';
  };
  for (const r of shownRuns){
    const counts = Object.entries(r.counts||{})
      .map(([k,v])=>`<span style="color:${COLORS[k]||'#8b949e'}">${v} ${k}</span>`)
      .join('');
    const runUsd = (r.cost&&r.cost.usd!=null)||(r.plan_cost&&r.plan_cost.usd!=null)
      ? (r.cost&&r.cost.usd||0)+(r.plan_cost&&r.plan_cost.usd||0) : null;
    // Compact density hides the .sub planner-output line entirely, so the
    // same gen_last also rides inline on the row as .genline (display:none
    // at full density). The prevTail diff drives the .fresh flash - the
    // same mechanism agent tail cells use. gen_last can be one giant
    // newline-less blob - keep its END, where the fresh text is.
    const gk = 'genline|' + runKey(r);
    const gfresh = r.stage && gk in prevTail && prevTail[gk] !== r.gen_last;
    if (r.stage) prevTail[gk] = r.gen_last;
    const glast = r.gen_last && r.gen_last.length > 160
      ? '…' + r.gen_last.slice(-160) : r.gen_last;
    const cgen = !r.stage ? '' :
      r.stage==='questions' && (r.questions||[]).length
        ? `planner waits on ${r.questions.length} answer(s)`
        : glast || (r.stage==='generating' ? 'planner warming up' : '');
    h += `<tr class="runrow${runKey(r)===selectedRun?' selected':''}" ` +
      `data-run-key="${esc(runKey(r))}" tabindex="0" ` +
      `aria-label="Filter to run ${esc(r.run)}"><td class="wrap">` +
      (r.counts&&r.counts.running
        ? `<span class="dot run" style="background:#58a6ff" ` +
          `title="a step in this run is running"></span>` : '') +
      // the run name itself is the details affordance (requests open the
      // proposal modal instead) - the separate Details link is gone; a
      // click on the rest of the row still filters.
      (r.stage
        ? `<button type="button" class="linkbtn run-name" ` +
          `data-open-request="${esc(r.request_id)}" ` +
          `title="${r.stage==='draft'?'Edit draft':'Review proposal'}">` +
          `<b>${esc(r.run)}</b></button>`
        : `<button type="button" class="linkbtn run-name" ` +
          `data-run-details="${esc(runKey(r))}" title="run details">` +
          `<b>${esc(r.run)}</b></button>`) + `<br>` +
      (r.stage ? `<span class="stage ${esc(r.stage)}">` +
        `${esc(STAGES[r.stage]||r.stage)}` +
        (r.stage==='generating'
          ? `<span class="dots"><span>.</span><span>.</span><span>.</span></span>`
          : '') + `</span>` +
        (cgen ? ` <span class="genline${gfresh?' fresh':''}" ` +
          `title="${esc(cgen)}">${esc(cgen)}</span>` : '') +
        (r.stage==='questions' && (r.questions||[]).length
          ? `<br><span class="sub">planner waits on ` +
            `${r.questions.length} answer(s) - open to reply</span>`
          : r.gen_last ? `<br><span class="sub${gfresh?' fresh':''}">` +
            `${esc(r.gen_last)}</span>` :
          r.stage==='generating'
            ? `<br><span class="sub">planner warming up - ` +
              `devin stays silent for the first minutes; watch the ` +
              `plan-proposal row below</span>` : '') :
        (r.archived ? `<span class="stage archived" ` +
          `title="auto-archived run - hidden from the default view; ` +
          `files stay on disk">archived</span>` : '') +
        runStateChip(r) + `<br>` +
        `<span class="sub">${esc(base(r.dir))}</span>`) +
      // stale survives until _publish_proposal clears it, so mid-planning
      // stages would double-label the row ('Preparing proposal' + 'Needs
      // review'); the flag only means something once planning has stopped
      (r.stale && r.stage!=='generating' && r.stage!=='questions'
        ? ` <span class="stage error">Needs review</span>`:'') +
      `</td>` +
      `<td class="wrap">${r.description?esc(r.description):
        r.stage?'<span class="sub">Awaiting proposal</span>':'-'}</td>` +
      `<td>${(r.projects||[]).map(p=>`<span class="chip">${esc(p)}</span>`).join('')}</td>` +
      `<td>${loc(r.started_at)}</td>` +
      `<td>${r.ended_at?loc(r.ended_at):(r.started_at?'<b>running</b>':'-')}</td>` +
      `<td class="counts">` +
      `${counts||'<span class="sub">not planned</span>'}` +
      (r.plan_tokens ? `<br><span class="sub" ` +
        `title="${esc(tokTitle(r.plan_tokens))}">planner: ` +
        `${density==='compact' ? fmtTc(r.plan_tokens) : fmtT(r.plan_tokens)}` +
        (r.plan_cost?` · ${fmtC(r.plan_cost)}`:'') + `</span>` : '') +
      (r.tokens ? `<br><span class="sub" ` +
        `title="${esc(tokTitle(r.tokens))}">steps: ` +
        `${density==='compact' ? fmtTc(r.tokens) : fmtT(r.tokens)}` +
        (r.cost?` · ${fmtC(r.cost)}`:'') + `</span>` : '') +
      (runUsd!=null ? `<br><span class="sub" title="estimated self-serve ` +
        `cost, plan + steps${r.cost&&r.cost.unpriced
          ?`; ${r.cost.unpriced} step(s) have no rate`:''}">` +
        `est ${fmtC({usd:runUsd})}</span>` : '') +
      `</td><td>${r.max_parallel||'-'}</td>` +
      `<td class="hide-s wrap">${esc(r.dir)}</td></tr>`;
  }
  if (hiddenRuns > 0)
    h += `<tr><td colspan="8"><a href="#" data-rmore>show more ` +
      `(${hiddenRuns} finished run${hiddenRuns===1?'':'s'})</a> · ` +
      `<a href="#" data-rall>show all ${allRuns.length}</a></td></tr>`;
  else if (runsShown > SHOW_N)
    h += `<tr><td colspan="8"><a href="#" data-rless>` +
      `show latest ${SHOW_N} only</a></td></tr>`;
  document.getElementById('runs').innerHTML = h;

  // one fixed-width table per project slug, ordered by newest activity
  const groups = {};
  scope.forEach(a=>{ (groups[a.slug||'?'] ??= []).push(a); });
  const slugs = Object.keys(groups).sort((x,y)=>
    (groups[y][0].started_at||groups[y][0].created_at||'') <
    (groups[x][0].started_at||groups[x][0].created_at||'') ? -1 : 1);
  h = '';
  for (const slug of slugs){
    const rows = groups[slug].filter(matchAgent);
    if (!rows.length) continue;
    const all = groups[slug];
    const nRun = all.filter(r=>r.status==='running').length;
    const show = expanded[slug] ? rows : rows.slice(0, SHOW_N);
    h += `<h3 class="proj">${esc(slug)} <span class="sub">` +
      `${all.length} agent(s)${nRun?`, ${nRun} running`:''}` +
      `${rows.length<all.length?`, ${all.length-rows.length} filtered`:''}` +
      `${show.length<rows.length?`, showing latest ${show.length}`:''}` +
      `</span></h3>` +
      '<div class="wrapw"><table>' + AGENT_COLS +
      '<tr><th></th><th>step</th><th>run</th>' +
      '<th class="hide-s">model</th><th class="hide-s">effort</th>' +
      '<th class="hide-s">mode</th><th>started</th><th>elapsed</th>' +
      '<th class="hide-s" title="' + TOK_HELP + '">tokens</th>' +
      '<th class="hide-s" title="estimated cost at self-serve rates ' +
      '(docs.devin.ai/desktop/chat/models), recomputed as the rate ' +
      'table updates">est $</th>' +
      '<th>ended</th><th class="hide-s">session</th><th>note</th></tr>';
    for (const a of show){
      const c = COLORS[a.status]||'#8b949e';
      const pulse = a.status==='running' ? ' run' : '';
      const tk = (a.run_dir||'') + '|' + a.step;
      const fresh = a.status==='running' && tk in prevTail &&
                    prevTail[tk] !== a.tail_last;
      if (a.status==='running') prevTail[tk] = a.tail_last;
      const sess = a.session_id ? String(a.session_id).slice(0,14)
        : (a.status==='running' ? '…' : '-');
      const sessTd = a.session_gone
        ? `<td class="hide-s gone" title="session deleted from devin list; ` +
          `transcript stays in the run dir">${esc(sess)}</td>`
        : a.session_live
        ? `<td class="hide-s" style="color:#58a6ff" ` +
          `title="live session (inferred - recorded at step end)">` +
          `${esc(sess)}…</td>`
        : `<td class="hide-s"${sess==='…'?' title="recorded when the step ends"':''}>${esc(sess)}</td>`;
      h += `<tr class="agen" data-i="${a._i}" data-run-key="${esc(a.run_key||a.run_dir||'')}">` +
        `<td><span class="dot${pulse}" style="background:${c}" ` +
        `title="${a.status}${a.alive?' (pid '+a.pid+')':''}"></span>` +
        `${a.status}` +
        (a.attempts>1 ? `<span class="sub" title="ran ` +
          `${a.attempts} session attempts (timeout retries)"> ` +
          `×${a.attempts}</span>` : '') +
        (a.timeout_ext_s ? `<span class="sub" title="stayed active ` +
          `${fmtS(a.timeout_ext_s)} past the timeout floor"> +ext</span>`
          : '') + `</td>` +
        `<td class="step"><span>${esc(a.step)}</span>${a.wt
          ? ` <button type="button" class="chip" data-facet="wt" ` +
            `style="color:#58a6ff" title="${esc(a.branch||'worktree')}">wt</button>` +
            (a.merged===true
             ? ` <button type="button" class="chip" data-facet="merged" ` +
               `style="color:#3fb950">merged</button>`
             : a.merged===false
             ? ` <button type="button" class="chip" data-facet="unmerged" ` +
               `style="color:#f0883e">unmerged</button>`
             : '')
          : ''}</td>` +
        `<td>${esc(a.run)}</td>` +
        `<td class="hide-s" title="${a.model==='adaptive'&&a.model_actual
          ?esc(`adaptive resolved to ${a.model_actual}`):''}">` +
        `${esc(a.model||'-')}${a.model==='adaptive'&&a.model_actual
          ?` → ${esc(a.model_actual)}`:''}</td>` +
        `<td class="hide-s">${esc(a.effort||'-')}</td>` +
        `<td class="hide-s">${esc(a.mode||'-')}${a.cloud?' cloud':''}</td>` +
        `<td>${loc(a.started_at)}</td>` +
        `<td data-el="${a._i}">${elapsed(a,now)}</td>` +
        `<td class="hide-s" title="${esc(tokTitle(a.tokens))}">` +
        `${density==='compact' ? fmtTc(a.tokens) : fmtT(a.tokens)}</td>` +
        `<td class="hide-s" title="${esc(costTitle(a.cost))}">` +
        `${fmtC(a.cost)}</td>` +
        `<td>${loc(a.ended_at)}</td>` + sessTd +
        (a.status==='running'
          ? `<td class="res${fresh?' fresh':''}" ` +
            `title="${esc(a.tail_last||'')}">${esc(a.tail_last||'')}` +
            `<span class="dots"><span>.</span><span>.</span><span>.</span>` +
            `</span></td>`
          : `<td class="wrap">${esc(a.note||'-')}</td>`) + `</tr>`;
    }
    if (rows.length > SHOW_N)
      h += `<tr><td colspan="13"><a href="#" data-slug="${esc(slug)}">` +
        (expanded[slug] ? `show latest ${SHOW_N} only` :
          `show ${rows.length - SHOW_N} older agent(s)`)+`</a></td></tr>`;
    h += '</table></div>';
  }
  document.getElementById('agents').innerHTML = h ||
    `<span class="sub">${scope.length?'no agents match these filters':
      selectedRun?'no steps yet; generate the proposal to preview agents':'no agents yet'}</span>`;
  applyHl();
  streamGenTail();
  if (detailIdx != null && !document.getElementById('backdrop').hidden){
    // agent rows re-sort as steps start/finish - keep the modal (and
    // its prompt form) on the SAME agent, not whatever row now sits at
    // the old index
    const j = detailKey==null ? detailIdx
      : AGENTS.findIndex(x=>agentKey(x)===detailKey);
    if (j >= 0 && AGENTS[j]) showDetail(j);
    else closeDetail(true);
  }
  const modalRun = modalRequest?.id && d.runs.find(r=>r.request_id===modalRequest.id);
  const modalStage = modalRun && (modalRun.stage||'launched');
  // an open request modal follows its stage - 'questions' swaps it for
  // the questionnaire (the form stays reachable via the run-name click)
  if (modalStage && modalRequest.status!=='loading' && !modalRequest.dirty &&
      modalRequest.status!==modalStage)
    (modalStage==='questions' ? openQuestions : openRequest)(modalRequest.id);
  // A planner questionnaire is blocked on a human - pop it once per
  // question set when nothing else is modal (closing it marks the set
  // dismissed; the request's Answer button re-opens it).
  const qr = !modalQA && document.getElementById('backdrop').hidden &&
    d.runs.find(r=>r.stage==='questions' && (r.questions||[]).length);
  if (qr && !qDismissed.has(
      qr.request_id+'|'+qr.questions.map(q=>q.qid).join(',')))
    openQuestions(qr.request_id);
  syncTopbar();      // filter rows can wrap and change the bar height
}
document.addEventListener('click', async e=>{
  const t = e.target;
  if (t.id==='dx' || t.id==='backdrop'){
    closeDetail(); return; }
  if (t.id==='new-request'){
    openNew(); return; }
  if (t.dataset && t.dataset.density){
    setDensity(t.dataset.density); return; }
  if (t.dataset && t.dataset.action){
    await requestAction(t.dataset.action); return; }
  // closest(), not t.dataset - the name button wraps a <b>, so the
  // actual click target can be an inner element with an empty dataset.
  const openReqEl = t.closest && t.closest('[data-open-request]');
  if (openReqEl){
    await openRequest(openReqEl.dataset.openRequest); return; }
  const openQEl = t.closest && t.closest('[data-open-questions]');
  if (openQEl){
    await openQuestions(openQEl.dataset.openQuestions); return; }
  const rmAtt = t.closest && t.closest('[data-attach-rm]');
  if (rmAtt){
    e.preventDefault();
    rqAttachments=rqAttachments.filter(
      a=>a.name!==rmAtt.dataset.attachRm);
    renderAttach(true);
    if (modalRequest) modalRequest.dirty=true;
    return; }
  if (modalQA && t.dataset && t.dataset.qopt!==undefined){
    // toggle a suggested option - single-select replaces, multi toggles;
    // re-clicking the sole pick deselects it
    const l=t.dataset.qopt, q=(modalQA.rec.pending_questions||[])[0];
    if (!q) return;
    if (q.multi_select){
      modalQA.sel.has(l)?modalQA.sel.delete(l):modalQA.sel.add(l);
    } else {
      modalQA.sel = modalQA.sel.has(l) ? new Set() : new Set([l]);
    }
    showQuestion(); return; }
  if (modalQA && t.dataset &&
      (t.dataset.qsend!==undefined||t.dataset.qskip!==undefined||
       t.dataset.qskipall!==undefined)){
    const pend=modalQA.rec.pending_questions||[];
    let body;
    if (t.dataset.qskipall!==undefined) body={skip_all:true};
    else if (t.dataset.qskip!==undefined)
      body={qid:pend[0]&&pend[0].qid, skipped:true};
    else {
      const fi=document.getElementById('q-free');
      body={qid:pend[0]&&pend[0].qid, selected:[...modalQA.sel],
            text:(fi?fi.value:modalQA.free)||''};
      if (!body.selected.length && !body.text.trim()){
        formError('Pick an option, write an answer, or Skip.'); return; }
    }
    if (!body.skip_all && !body.qid) return;
    await answerQuestion(body); return; }
  const runDetEl = t.closest && t.closest('[data-run-details]');
  if (runDetEl){
    showRunDetails(runDetEl.dataset.runDetails); return; }
  const navEl = t.closest && t.closest('[data-nav]');
  if (navEl){
    const j = AGENTS.findIndex(x=>agentKey(x)===navEl.dataset.nav);
    if (j>=0){
      showDetail(j);
      const d = document.getElementById('detail');
      if (d) d.scrollTop = 0;
    }
    return; }
  if (t.dataset && t.dataset.resumeRun){
    if(!confirm('Resume this run? Every non-done step re-runs in a fresh ' +
                'session with a fresh timeout - covers paused, stopped ' +
                'and interrupted (crashed) runs alike.')) return;
    t.disabled=true; t.textContent='Resuming…';
    try {
      await api('POST',`/api/runs/${encodeURIComponent(t.dataset.resumeRun)}/resume`,{});
      await refresh();
    } catch(err){ alert('Resume failed: '+err.message); t.disabled=false;
      t.textContent='Resume unfinished steps'; }
    return; }
  if (t.dataset && t.dataset.mergeRun){
    if(!confirm('Merge the pending worktree branches of this run into ' +
                'their ' +
                'main checkouts? Clean merges land and drop the worktree; ' +
                'conflicts stay parked for manual resolution (or a ' +
                'resolve-* session when merge_resolver is set).')) return;
    const rk = t.dataset.rk;
    t.disabled=true; t.textContent='Merging…';
    try {
      const out=await api('POST',
        `/api/runs/${encodeURIComponent(t.dataset.mergeRun)}/merge`,{});
      const bits=[];
      if (out.merged?.length) bits.push(`merged: ${out.merged.join(', ')}`);
      if (out.conflicted?.length)
        bits.push(`still conflicted (worktree kept): ${out.conflicted.join(', ')}`);
      if (out.skipped?.length) bits.push(`skipped: ${out.skipped.join(', ')}`);
      await refresh();
      if (rk) showRunDetails(rk);
      alert(bits.length ? bits.join(' | ') : 'Nothing pending to merge.');
    } catch(err){ alert('Merge failed: '+err.message); t.disabled=false;
      t.textContent='Merge pending branches'; }
    return; }
  if (t.dataset && (t.dataset.skipStep || t.dataset.unskipStep)){
    const un = !!t.dataset.unskipStep;
    const dir = t.dataset.skipStep || t.dataset.unskipStep;
    if(!confirm(un
        ? `Unskip '${t.dataset.step}'? It runs in its original queue ` +
          `position - if its turn already passed this run, on the ` +
          `next resume.`
        : `Skip '${t.dataset.step}'? It keeps its place in the run but ` +
          `never launches while marked - the mark stays in force even ` +
          `across resume until you Unskip.`)) return;
    t.disabled=true;
    try {
      const out=await api('POST',
        `/api/runs/${encodeURIComponent(dir)}/steps/` +
        `${encodeURIComponent(t.dataset.step)}/skip`,{skip:!un});
      closeDetail(true); await refresh();
      if (un && out.turn_passed)
        alert(`Unskipped - '${t.dataset.step}' already passed its turn ` +
              `this run, so it re-runs on the next resume.`);
    } catch(err){ alert((un?'Unskip':'Skip')+' failed: '+err.message);
      t.disabled=false; }
    return; }
  if (t.dataset && t.dataset.stopAgent){
    if(!confirm(`Stop step '${t.dataset.step}'? Its devin process tree ` +
                `is killed (committed work stays) and the step records ` +
                `'stopped'. The run proceeds to the next step; resume ` +
                `re-runs it.`)) return;
    t.disabled=true;
    try {
      await api('POST',
        `/api/runs/${encodeURIComponent(t.dataset.stopAgent)}/steps/` +
        `${encodeURIComponent(t.dataset.step)}/stop`,{});
      closeDetail(true); await refresh();
    } catch(err){ alert('Stop failed: '+err.message); t.disabled=false; }
    return; }
  if (t.dataset && t.dataset.stopGen){
    if(!confirm('Stop preparing this proposal? The planner session is ' +
                'cancelled - the request stays editable and any previous ' +
                'proposal is kept.')) return;
    t.disabled=true;
    try {
      await api('POST',
        `/api/requests/${encodeURIComponent(t.dataset.stopGen)}/stop`,{});
      closeDetail(true); await refresh();
    } catch(err){ alert('Stop failed: '+err.message); t.disabled=false; }
    return; }
  if (t.dataset && t.dataset.setTimeout){
    const inp=document.getElementById('d-timeout');
    const mins=parseFloat(inp?.value);
    if(!(mins>=1)){ alert('Enter a timeout in minutes (1-1440).'); return; }
    t.disabled=true;
    try {
      await api('POST',
        `/api/runs/${encodeURIComponent(t.dataset.setTimeout)}/steps/` +
        `${encodeURIComponent(t.dataset.step)}/timeout`,{minutes:mins});
      await refresh();
    } catch(err){ alert('Timeout update failed: '+err.message);
      t.disabled=false; }
    return; }
  if (t.dataset && (t.dataset.savePrompt || t.dataset.revertPrompt)){
    const dir=t.dataset.savePrompt||t.dataset.revertPrompt;
    const ta=document.getElementById('d-prompt');
    const revert = !!t.dataset.revertPrompt;
    t.disabled=true;
    try {
      await api('POST',
        `/api/runs/${encodeURIComponent(dir)}/steps/` +
        `${encodeURIComponent(t.dataset.step)}/prompt`,
        {prompt:revert?null:(ta?ta.value:null)});
      delete promptDrafts[detailKey];
      await refresh();
    } catch(err){ alert((revert?'Revert':'Prompt save')+' failed: ' +
      err.message); t.disabled=false; }
    return; }
  if (t.dataset && t.dataset.saveNotes){
    const ta=document.getElementById('d-rnotes');
    t.disabled=true;
    try {
      await api('POST',
        `/api/runs/${encodeURIComponent(t.dataset.saveNotes)}/steps/` +
        `${encodeURIComponent(t.dataset.step)}/notes`,
        {notes:ta?ta.value:''});
      delete notesDrafts[detailKey];
      await refresh();
    } catch(err){ alert('Notes save failed: '+err.message);
      t.disabled=false; }
    return; }
  if (t.dataset && t.dataset.pauseRun){
    if(!confirm('Pause this run? In-flight steps run to completion, ' +
                'then the run holds - NO further step launches ' +
                'regardless of on_failure. It stays resumable across a ' +
                'dashboard or machine restart. To kill live work ' +
                'immediately, use Stop run now instead.')) return;
    t.disabled=true;
    try {
      const out=await api('POST',
        `/api/runs/${encodeURIComponent(t.dataset.pauseRun)}/pause`,{});
      closeDetail(true); await refresh();
      alert(out.pausing?.length
        ? `Pause set: ${out.pausing.join(', ')} finishes first, ` +
          `then the run holds.` :
          'Pause set - nothing was in flight; the run is held.');
    } catch(err){ alert('Pause failed: '+err.message); t.disabled=false; }
    return; }
  if (t.dataset && (t.dataset.stopRun || t.dataset.drainRun)){
    const drain=!!t.dataset.drainRun, dir=t.dataset.stopRun||t.dataset.drainRun;
    if(!confirm(drain
        ? 'Drain this run: let in-flight steps finish, then end the run ' +
          'without launching more?'
        : 'Stop this run NOW? Every live step is tree-killed (committed ' +
          'work stays) and nothing further launches; Resume restarts ' +
          'unfinished steps. To hold only after in-flight steps finish, ' +
          'use Pause run instead.')) return;
    t.disabled=true;
    try {
      const out=await api('POST',
        `/api/runs/${encodeURIComponent(dir)}/stop`,{drain});
      closeDetail(true); await refresh();
      if (!drain) alert(out.stopping?.length
        ? `Stopping: ${out.stopping.join(', ')}` :
          'Stop requested - nothing was live.');
    } catch(err){ alert('Stop failed: '+err.message); t.disabled=false; }
    return; }
  if (t.dataset && t.dataset.archRun){
    const on=t.dataset.archSet==='1';
    t.disabled=true;
    try {
      await api('POST',
        `/api/runs/${encodeURIComponent(t.dataset.archRun)}/archive`,
        {archived:on});
      closeDetail(true); await refresh();
    } catch(err){ alert((on?'Archive':'Unarchive')+' failed: ' +
      err.message); t.disabled=false; }
    return; }
  if (t.dataset && t.dataset.deleteRun){
    const dir=t.dataset.deleteRun;
    const r=(lastData?.runs||[]).find(x=>base(x.dir)===dir);
    const unmerged=AGENTS.filter(a=>a.branch && a.merged!==true &&
      (a.run_key||a.run_dir)===(r?runKey(r):'')).length;
    if(!confirm(`Delete run '${r?r.run:dir}'? Its whole record - state, ` +
      `logs, prompts, session exports - is removed from disk. There is ` +
      `no undo.` +
      (unmerged?` ${unmerged} worktree branch(es) are still unmerged - ` +
        `merge them first or they stay parked.`:''))) return;
    t.disabled=true;
    try {
      await api('POST',`/api/runs/${encodeURIComponent(dir)}/delete`,
                {confirm:true});
      closeDetail(true); await refresh();
    } catch(err){ alert('Delete failed: '+err.message); t.disabled=false; }
    return; }
  if (t.dataset && t.dataset.filterRun){
    selectedRun=t.dataset.filterRun;
    closeDetail(true); if (lastData) render(lastData); return; }
  if (t.dataset && t.dataset.clearRun!==undefined){
    selectedRun=null; if (lastData) render(lastData); return; }
  if (t.dataset && t.dataset.reset!==undefined){
    only.clear(); facets.clear(); if (lastData) render(lastData); return; }
  if (t.dataset && t.dataset.arch!==undefined){   // 'N archived' view chip
    showArchived=!showArchived;
    store.setItem('ac-arch', showArchived?'1':'0');
    await refresh(); return;
  }
  if (t.dataset && t.dataset.st){            // status filter chip: show only
    only.has(t.dataset.st) ? only.delete(t.dataset.st)
                           : only.add(t.dataset.st);
    if (lastData) render(lastData); return;
  }
  if (t.dataset && t.dataset.facet){
    const f=t.dataset.facet;
    if (facets.has(f)) facets.delete(f);
    else {
      if (f==='merged') facets.delete('unmerged');
      if (f==='unmerged') facets.delete('merged');
      facets.add(f);
    }
    if (lastData) render(lastData); return;
  }
  if (t.dataset && t.dataset.slug){          // expand/collapse a table
    e.preventDefault();
    expanded[t.dataset.slug] = !expanded[t.dataset.slug];
    if (lastData) render(lastData); return;
  }
  if (t.dataset && t.dataset.rmore!==undefined){   // runs list paging
    e.preventDefault(); runsShown += SHOW_N;
    if (lastData) render(lastData); return; }
  if (t.dataset && t.dataset.rall!==undefined){
    e.preventDefault(); runsShown = Infinity;
    if (lastData) render(lastData); return; }
  if (t.dataset && t.dataset.rless!==undefined){
    e.preventDefault(); runsShown = SHOW_N;
    if (lastData) render(lastData); return; }
  if (t.dataset && t.dataset.auth){
    e.preventDefault();
    if (t.dataset.auth==='login'){
      await fetch('/api/auth/login', {method:'POST', headers:{'Content-Type':'application/json'}, body:'{}'});
      t.outerHTML = ' <span style="color:#8b949e">a terminal opened - ' +
        'sign in at the link, paste the code back; this clears itself</span>';
    } else {
      await fetch('/api/auth/recheck', {method:'POST', headers:{'Content-Type':'application/json'}, body:'{}'});
    }
    return;
  }
  const runRow = t.closest && t.closest('tr.runrow');
  if (runRow){
    selectedRun = selectedRun===runRow.dataset.runKey ? null : runRow.dataset.runKey;
    if (lastData) render(lastData);
    return;
  }
  const row = t.closest && t.closest('tr.agen');
  if (row) showDetail(+row.dataset.i);
});
document.addEventListener('input', e=>{
  // typed questionnaire text survives an option-toggle re-render
  if (modalQA && e.target.id==='q-free') modalQA.free=e.target.value;
  // unsaved prompt/notes text survives the 2s modal rebuild - keyed by
  // run_dir|step so a re-sorted row never inherits another step's draft
  if ((e.target.id==='d-prompt'||e.target.id==='d-rnotes')
      && detailKey!=null)
    (e.target.id==='d-prompt'?promptDrafts:notesDrafts)[detailKey]=
      e.target.value;
  if (modalRequest && e.target.id?.startsWith('rq-')){
    modalRequest.dirty=true;
    const msg=document.getElementById('form-msg');
    if (msg) msg.hidden=true;
    // Edited fields diverge from the proposal TOML until applied or
    // regenerated - flag the buttons that reconcile them.
    if (modalRequest.record?.manifest)
      document.querySelectorAll(
        '#detail [data-action=generate],#detail [data-action=apply]')
        .forEach(b=>b.classList.add('attn'));
    if (e.target.id==='rq-model' || e.target.id==='rq-plan_model')
      syncEffort(e.target,
                 e.target.id==='rq-model'?'rq-effort':'rq-plan_effort',
                 e.target.id==='rq-model'?'rq-efforts':'rq-plan_efforts');
  }
});
// attachment drop/paste: bound once at document level (the modal's
// innerHTML is rebuilt per open, element listeners would stack or die);
// attachTarget() limits them to an editable request form.
document.addEventListener('dragover', e=>{
  const ta=attachTarget(); if(!ta) return;
  if(e.dataTransfer && [...(e.dataTransfer.types||[])].includes('Files')){
    e.preventDefault(); ta.classList.add('drop'); }
});
document.addEventListener('dragleave', ()=>{
  const ta=document.getElementById('rq-raw');
  if(ta) ta.classList.remove('drop');
});
document.addEventListener('drop', e=>{
  const ta=attachTarget();
  if(ta) ta.classList.remove('drop');
  if(!ta || !e.dataTransfer) return;
  const files=e.dataTransfer.files;
  if(files && files.length){ e.preventDefault(); attachFiles(files); }
});
document.addEventListener('paste', e=>{
  const ta=attachTarget(); if(!ta) return;
  const items=[...(e.clipboardData?e.clipboardData.items||[]:[])];
  const files=items
    .filter(i=>i.kind==='file'&&(i.type||'').startsWith('image/'))
    .map(i=>i.getAsFile()).filter(Boolean);
  if(files.length){ e.preventDefault(); attachFiles(files); }
});
document.addEventListener('keydown', e=>{
  if (e.key==='Enter' && modalQA && e.target.id==='q-free'){
    e.preventDefault();
    const pend=(modalQA.rec.pending_questions||[]);
    const body={qid:pend[0]&&pend[0].qid, selected:[...modalQA.sel],
                text:e.target.value||''};
    if (!body.selected.length && !body.text.trim())
      { formError('Pick an option, write an answer, or Skip.'); return; }
    if (body.qid) answerQuestion(body);
    return;
  }
  if ((e.key==='Enter'||e.key===' ') &&
      e.target.matches && e.target.matches('tr.runrow')){
    e.preventDefault();
    selectedRun = selectedRun===e.target.dataset.runKey ? null : e.target.dataset.runKey;
    if (lastData) render(lastData);
  }
  // Left/Right walks the open task's pipeline (same as the .dnav arrows)
  // - but never while typing in a form field.
  if ((e.key==='ArrowLeft'||e.key==='ArrowRight') && detailKey!=null &&
      !document.getElementById('backdrop').hidden){
    const ae = document.activeElement;
    if (!(ae && /^(INPUT|TEXTAREA|SELECT)$/.test(ae.tagName||''))){
      e.preventDefault();
      navStep(e.key==='ArrowLeft'?-1:1);
      return;
    }
  }
  if (e.key==='Escape' && !document.getElementById('backdrop').hidden)
    closeDetail();
});
// hovering a run row highlights its agent rows across all project tables.
// hoverRun persists across the 2s re-render so the highlight doesn't flicker.
let hoverRun = null;
function applyHl(){
  document.querySelectorAll('tr[data-run-key]').forEach(x=>
    x.classList.toggle('hl', x.dataset.runKey===selectedRun ||
      !!hoverRun && x.dataset.runKey===hoverRun));
}
document.addEventListener('mouseover', e=>{
  const tr = e.target.closest && e.target.closest('tr[data-run-key]');
  hoverRun = tr && tr.dataset.runKey || null;
  applyHl();
});
async function tick(){
  try { render(await (await fetch('/api/state' +
    (showArchived?'?archived=1':''))).json()); }
  catch(e){} finally { setTimeout(tick, 2000); } }
setDensity(density);
if (typeof window!=='undefined' && window.addEventListener)
  window.addEventListener('resize', syncTopbar);
tick();
</script></body></html>"""


def make_dashboard_handler(runs_dir: Path, devin_argv: list[str],
                           requests_dir: Path | None = None,
                           manifests_dir: Path = MANIFESTS_DIR,
                           attachments_dir: Path | None = None):
    requests_dir = requests_dir or runs_dir.parent / "requests"
    attachments_dir = attachments_dir or runs_dir.parent / "attachments"

    class Handler(http.server.BaseHTTPRequestHandler):
        def _send(self, body: bytes, ctype: str, status: int = 200) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _json(self, value: dict, status: int = 200) -> None:
            self._send(json.dumps(value, ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8", status)

        def _check_origin(self) -> None:
            host = self.headers.get("Host", "")
            try:
                parsed = urllib.parse.urlsplit("http://" + host)
                local = (parsed.hostname in ("127.0.0.1", "localhost")
                         and parsed.port == self.server.server_port)
            except ValueError as exc:
                raise RequestError("Invalid local dashboard host", 403) from exc
            if not local:
                raise RequestError("Only local dashboard requests are allowed", 403)
            origin = self.headers.get("Origin")
            if origin and origin != f"http://{host}":
                raise RequestError("Cross-origin requests are not allowed", 403)

        def _body(self) -> dict:
            if self.headers.get("Content-Type", "").split(";", 1)[0] \
                    != "application/json":
                raise RequestError("Send application/json", 415)
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 524288:
                    raise RequestError("Request body is too large or empty")
                value = json.loads(self.rfile.read(size).decode("utf-8"))
                if not isinstance(value, dict):
                    raise RequestError("Expected a JSON object")
                return value
            except (ValueError, UnicodeError) as exc:
                if isinstance(exc, RequestError):
                    raise
                raise RequestError("Invalid JSON request body") from exc

        def do_GET(self) -> None:                    # noqa: N802 - stdlib name
            path = urllib.parse.urlsplit(self.path).path
            try:
                self._check_origin()
                if path == "/api/state":
                    q = urllib.parse.parse_qs(
                        urllib.parse.urlsplit(self.path).query)
                    self._json(collect_dashboard(
                        runs_dir, devin_argv, requests_dir,
                        include_archived=q.get("archived", [""])[0]
                        in ("1", "true", "yes")))
                elif (m := re.fullmatch(r"/api/requests/([^/]+)", path)):
                    rec = request_details(m[1], requests_dir,
                                          manifests_dir, runs_dir)
                    if rec["status"] == "discarded":
                        raise RequestError("Request was discarded", 404)
                    self._json(rec)
                elif (m := re.fullmatch(
                        r"/api/runs/([^/]+)/steps/([^/]+)/summary", path)):
                    md = step_summary(m.group(1), m.group(2), runs_dir)
                    q = urllib.parse.parse_qs(
                        urllib.parse.urlsplit(self.path).query)
                    if q.get("raw", [""])[0] in ("1", "true"):
                        self._send(md.encode("utf-8"),
                                   "text/markdown; charset=utf-8")
                    else:
                        self._send(summary_html(
                            md, f"{m.group(1)} / {m.group(2)}")
                            .encode("utf-8"),
                            "text/html; charset=utf-8")
                elif (m := re.fullmatch(r"/api/attachments/([^/]+)", path)):
                    data, ctype = read_attachment(
                        urllib.parse.unquote(m[1]), attachments_dir)
                    self._send(data, ctype)
                elif path in ("/", "/index.html"):
                    self._send(_DASH_HTML.encode("utf-8"),
                               "text/html; charset=utf-8")
                else:
                    self.send_error(404)
            except RequestError as exc:
                self._json({"error": str(exc)}, exc.status)
            except Exception:
                LOG.exception("dashboard GET failed")
                self._json({"error": "Dashboard request failed"}, 500)

        def do_POST(self) -> None:                   # noqa: N802 - stdlib name
            path = urllib.parse.urlsplit(self.path).path
            try:
                self._check_origin()
                if path == "/api/attachments":
                    # Raw image bytes (screenshot paste / file drop), NOT the
                    # JSON envelope _body() expects. data/attachments can sit
                    # inside a self-hosted run's watched cwd, so uploads take
                    # the same trap-5 guard as request writes.
                    try:
                        size = int(
                            self.headers.get("Content-Length") or 0)
                    except ValueError:
                        raise RequestError("Missing image body") from None
                    if not 0 < size <= ATTACH_MAX_BYTES:
                        raise RequestError(
                            "Image body is too large or empty", 413)
                    _assert_unwatched(runs_dir, attachments_dir)
                    self._json(save_attachment(self.rfile.read(size),
                                               attachments_dir), 201)
                    return
                body = self._body()
                if path == "/api/requests":
                    self._json(create_request(body, requests_dir, runs_dir), 201)
                elif (m := re.fullmatch(r"/api/requests/([^/]+)/"
                                         r"(save|generate|run|discard|apply"
                                         r"|answer|stop)", path)):
                    request_id, action = m.groups()
                    if action == "save":
                        self._json(save_request(request_id, body, requests_dir,
                                                manifests_dir, runs_dir))
                    elif action == "generate":
                        self._json(start_generation(request_id, requests_dir,
                                                    manifests_dir, runs_dir,
                                                    devin_argv), 202)
                    elif action == "stop":
                        self._json(stop_generation(request_id, requests_dir,
                                                   runs_dir), 202)
                    elif action == "run":
                        self._json(launch_request(request_id, requests_dir,
                                                  manifests_dir, runs_dir,
                                                  devin_argv), 202)
                    elif action == "apply":
                        self._json(apply_request(request_id, body, requests_dir,
                                                 manifests_dir, runs_dir))
                    elif action == "answer":
                        self._json(answer_request(request_id, body,
                                                  requests_dir, manifests_dir,
                                                  runs_dir, devin_argv))
                    else:
                        if body.get("confirm") is not True:
                            raise RequestError("Confirm discard explicitly")
                        self._json(discard_request(request_id, requests_dir,
                                                   manifests_dir, runs_dir))
                elif (m := re.fullmatch(r"/api/runs/([^/]+)/resume", path)):
                    self._json(resume_run(m.group(1), runs_dir,
                                          devin_argv), 202)
                elif (m := re.fullmatch(r"/api/runs/([^/]+)/merge", path)):
                    self._json(merge_run(m.group(1), runs_dir, devin_argv))
                elif (m := re.fullmatch(r"/api/runs/([^/]+)/stop", path)):
                    self._json(stop_run(m.group(1), body, runs_dir), 202)
                elif (m := re.fullmatch(r"/api/runs/([^/]+)/pause", path)):
                    self._json(stop_run(m.group(1),
                                        {**body, "pause": True},
                                        runs_dir), 202)
                elif (m := re.fullmatch(r"/api/runs/([^/]+)/archive", path)):
                    self._json(set_run_archived(m.group(1), body,
                                                runs_dir))
                elif (m := re.fullmatch(r"/api/runs/([^/]+)/delete", path)):
                    self._json(delete_run(m.group(1), body, runs_dir))
                elif (m := re.fullmatch(
                        r"/api/runs/([^/]+)/steps/([^/]+)/stop", path)):
                    self._json(stop_step(m.group(1), m.group(2), runs_dir),
                               202)
                elif (m := re.fullmatch(
                        r"/api/runs/([^/]+)/steps/([^/]+)/skip", path)):
                    self._json(skip_step(m.group(1), m.group(2), body,
                                         runs_dir), 202)
                elif (m := re.fullmatch(
                        r"/api/runs/([^/]+)/steps/([^/]+)/timeout", path)):
                    self._json(set_step_timeout(
                        m.group(1), m.group(2), body, runs_dir), 202)
                elif (m := re.fullmatch(
                        r"/api/runs/([^/]+)/steps/([^/]+)/prompt", path)):
                    self._json(set_step_prompt(
                        m.group(1), m.group(2), body, runs_dir), 202)
                elif (m := re.fullmatch(
                        r"/api/runs/([^/]+)/steps/([^/]+)/notes", path)):
                    self._json(set_step_notes(
                        m.group(1), m.group(2), body, runs_dir), 202)
                elif path == "/api/auth/login":
                    # Login is an interactive console flow (trap 3) - the most a
                    # dashboard button can do is pop a REAL terminal for it.
                    # logout first: a stale credentials.toml makes plain `login`
                    # short-circuit with "already logged in" (trap 4).
                    subprocess.Popen(
                        'start "" cmd /k "devin auth logout & '
                        'devin auth login --force-manual-token-flow"',
                        shell=True, env=clean_devin_env())
                    _AUTH_CACHE[0] = 0.0      # next tick re-probes
                    self._json({})
                elif path == "/api/auth/recheck":
                    _AUTH_CACHE[0] = 0.0
                    self._json({"auth": auth_status(devin_argv)})
                else:
                    self.send_error(404)
            except RequestError as exc:
                self._json({"error": str(exc)}, exc.status)
            except Exception:
                LOG.exception("dashboard POST failed")
                self._json({"error": "Dashboard request failed"}, 500)

        def log_message(self, fmt: str, *a) -> None:
            LOG.debug("http %s", fmt % a)

    return Handler


def cmd_serve(args: argparse.Namespace) -> int:
    runs_dir = Path(args.runs_dir).resolve() if args.runs_dir else RUNS_DIR
    for line in sweep_runs(runs_dir):       # one-shot cleanup at startup;
        print(f"sweep: {line}")             # display stays honest after
    # 48h session cleanup also runs here: spawned sessions are disposable
    # bookkeeping (transcripts live in the run dir) and the whitelist makes
    # human sessions unreachable, so it is safe unattended.
    devin_argv = shlex.split(os.environ.get("CONDUCTOR_DEVIN") or "devin")
    for line in sweep_sessions(runs_dir, devin_argv):
        print(f"sweep: {line}")
    # A restarted serve adopts generations whose owner died: the spool
    # replays deferred publishes and lands parked questions/errors.
    for rid in drain_planner_spool():
        print(f"sweep: adopted planner spool for request {rid}")
    handler = make_dashboard_handler(runs_dir, devin_argv)
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", args.port), handler)
    print(f"conductor dashboard: http://127.0.0.1:{args.port}  "
          f"(watching {runs_dir})")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


def cmd_demo(args: argparse.Namespace) -> int:
    """End-to-end against tests/mock_devin.py - no auth, no credits."""
    proj = DATA_DIR / "demo-project"
    proj.mkdir(parents=True, exist_ok=True)
    if not is_git_repo(proj):
        subprocess.run(["git", "init"], cwd=str(proj), capture_output=True,
                       **hidden_subprocess_kwargs())
    (proj / ".gitignore").write_text(".mock_sessions.json\n", encoding="utf-8")
    (proj / "WORK.txt").write_text("", encoding="utf-8")
    (proj / "AGENTS.md").write_text("# demo project\n", encoding="utf-8")
    tasks = proj / ".agent_tasks"
    tasks.mkdir(exist_ok=True)
    (tasks / "QUEUE.md").write_text("# queue\n\n| # | Task | Status |\n",
                                    encoding="utf-8")
    manifest = DATA_DIR / "demo.toml"
    manifest.write_text(f"""\
[run]
name = "demo"

[[step]]
name = "alpha"
project = "{proj.as_posix()}"
prompt = "Append a line to WORK.txt."

[[step]]
name = "beta"
project = "{proj.as_posix()}"
prompt = "Append a line to WORK.txt."
verify = "python -c \\"import pathlib;assert pathlib.Path('WORK.txt').read_text().strip()\\""
""", encoding="utf-8")
    mock = (ROOT / "tests" / "mock_devin.py").as_posix()
    py = Path(sys.executable).as_posix()
    ns = argparse.Namespace(manifest=str(manifest), resume=None,
                            devin=f"{py} {mock}", runs_dir=args.runs_dir,
                            hub=False, keep_awake=False)
    return cmd_run(ns)


# ---------------------------------------------------------------- main

def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="backslashreplace", line_buffering=True)
        except AttributeError:
            pass
    LOGS_DIR.mkdir(exist_ok=True)
    handler = RotatingFileHandler(LOGS_DIR / "conductor.log", maxBytes=1_000_000,
                                  backupCount=5, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    LOG.addHandler(handler)
    LOG.setLevel(logging.INFO)

    ap = argparse.ArgumentParser(prog="conductor",
                                 description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, fn, helptext in (
            ("plan", cmd_plan, "validate a manifest and show resolved steps"),
            ("run", cmd_run, "execute a manifest"),
            ("status", cmd_status, "show a run's step table"),
            ("serve", cmd_serve, "live dashboard of all runs + agents"),
            ("sweep", cmd_sweep, "mark orphaned running/queued records lost"),
            ("sessions", cmd_sessions,
             "list / clean up conductor-spawned devin sessions"),
            ("demo", cmd_demo, "self-test against a mock devin")):
        p = sub.add_parser(name, help=helptext)
        if name in ("plan", "run"):
            p.add_argument("manifest")
        if name == "run":
            p.add_argument("--request-id", default=None, help=argparse.SUPPRESS)
            p.add_argument("--resume", nargs="?", const="latest", default=None,
                           help="resume a run dir (default: latest for this manifest)")
            p.add_argument("--max-parallel", type=int, default=None,
                           help="cap concurrent agents (overrides the "
                                "manifest's run.max_parallel)")
        if name in ("plan", "run", "demo", "sessions", "sweep"):
            p.add_argument("--devin", default=None,
                           help="devin command override (e.g. mock shim). "
                                "Split with shlex - quote or use forward "
                                "slashes in Windows paths")
        if name in ("run", "demo", "serve", "sweep", "sessions"):
            p.add_argument("--runs-dir", default=None, help=argparse.SUPPRESS)
        if name in ("sessions", "sweep"):
            p.add_argument("--keep-hours", type=float, default=48,
                           help="grace period after a run ends before its "
                                "sessions are rm-eligible (default 48)")
        if name == "sessions":
            p.add_argument("--rm", action="store_true",
                           help="delete sessions from runs ended more than "
                                "--keep-hours ago (devin rm --force)")
        if name in ("run", "demo"):
            p.add_argument("--no-hub", dest="hub", action="store_false",
                           default=True, help="don't create/update a hub.db row")
            p.add_argument("--no-keep-awake", dest="keep_awake",
                           action="store_false", default=True,
                           help="allow the machine to sleep during the run")
        if name == "serve":
            p.add_argument("--port", type=int, default=SERVE_PORT)
        if name == "status":
            p.add_argument("run_dir", nargs="?", default=None)
    args = ap.parse_args(argv)
    return {"plan": cmd_plan, "run": cmd_run, "status": cmd_status,
            "serve": cmd_serve, "sweep": cmd_sweep,
            "sessions": cmd_sessions, "demo": cmd_demo}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
