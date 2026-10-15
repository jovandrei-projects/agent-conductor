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
import http.server
import json
import logging
import os
import re
import secrets
import shlex
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
import urllib.parse
from logging.handlers import RotatingFileHandler
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
RUNS_DIR = DATA_DIR / "runs"
REQUESTS_DIR = DATA_DIR / "requests"
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
- End your final message with exactly these two lines:

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
    for i, raw in enumerate(doc.get("step", [])):
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
            verify=merged.get("verify"),
            verify_must_pass=bool(merged.get("verify_must_pass", True)),
            cloud=bool(merged.get("cloud", False)),
            wave=merged.get("wave"),
            worktree=bool(merged.get("worktree", False)),
            effort=merged.get("effort") or None,
            merge_resolver=bool(mr), max_attempts=ma))
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
    if not PROJECTS_FILE.is_file():
        return {}
    doc = tomllib.loads(PROJECTS_FILE.read_text(encoding="utf-8"))
    out = {}
    for slug, ent in doc.get("project", {}).items():
        p = Path(ent["path"])
        if not p.is_absolute():
            p = (ROOT / p).resolve()
        out[slug] = {"path": p, "cloud_ok": bool(ent.get("cloud_ok", False))}
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
        # silently running the wrong effort.
        model = f"{step.model}-{step.effort}" if step.effort else step.model
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


def step_live(rec: dict) -> bool:
    """Is a 'running' step record backed by something actually alive?

    Two live shapes: a live pid, or a fresh no-pid record. No-pid mid-run
    is normal - the pid is kept in memory only until the worker exits
    (trap 5) - but a no-pid 'running' record older than its own timeout
    means the conductor process died mid-step.
    """
    if rec.get("status") != "running":
        return False
    if rec.get("pid"):
        return pid_alive(rec["pid"], rec.get("started_at"))
    t0 = _iso_ts(rec.get("started_at"))
    if t0 is None:
        return False
    timeout_s = float(rec.get("timeout_min") or 60) * 60
    return time.time() - t0 <= timeout_s + 900


def effective_statuses(state: dict) -> dict[str, str]:
    """Per-step status corrected for reality, without mutating state.json.

    'running' with nothing alive behind it displays as 'lost'. 'queued' is
    'lost' when the run ended or has been quiet >10min with nothing live -
    an orphaned queue entry can never start. ('lost' steps re-run on
    --resume like any non-done step.)
    """
    steps = state.get("steps") or {}
    live = {n: step_live(r) for n, r in steps.items()}
    stamps = [_iso_ts(state.get(k)) for k in ("started_at", "ended_at")]
    stamps += [_iso_ts(r.get(k)) for r in steps.values()
               for k in ("started_at", "ended_at")]
    newest = max((t for t in stamps if t), default=None)
    quiet = time.time() - newest if newest else 0.0
    orphaned = bool(state.get("ended_at")) \
        or (not any(live.values()) and quiet > 600)
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
    merge and LEAVE the worktree+branch for a human - the work is not lost."""
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
        tmp.replace(run_dir / "state.json")


def log_event(run_dir: Path, **kv) -> None:
    kv["ts"] = now_iso()
    line = json.dumps(kv, ensure_ascii=False)
    with STATE_LOCK:
        with open(run_dir / "events.jsonl", "a", encoding="utf-8") as f:
            f.write(line + "\n")


def tail_status(stdout_log: Path) -> str | None:
    try:
        data = stdout_log.read_bytes()[-8192:].decode("utf-8", "replace")
    except OSError:
        return None
    found = STATUS_RE.findall(data)
    return found[-1].lower() if found else None


def tail_next(stdout_log: Path) -> str | None:
    """The worker's NEXT: line - its one-line report to the next session
    (and to a human reading the dashboard)."""
    try:
        data = stdout_log.read_bytes()[-8192:].decode("utf-8", "replace")
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


def wait_worker(proc, stdout_log: Path,
                timeout_s: float) -> tuple[int | None, bool, bool]:
    """Wait for the worker; returns (rc, timed_out, hung_after_status).

    Polls in short slices so a STATUS line in the log is noticed within
    WAIT_POLL_S: the turn is then over, so the remaining wait collapses to
    STATUS_GRACE_S before the tree is killed. hung_after_status tells the
    caller the work completed - run verify and honor the status line
    rather than reporting a timeout.
    """
    deadline = time.time() + timeout_s
    grace_at = None
    while True:
        try:
            return proc.wait(timeout=WAIT_POLL_S), False, False
        except subprocess.TimeoutExpired:
            pass
        if tail_status(stdout_log) is not None:
            if grace_at is None:
                grace_at = time.time() + STATUS_GRACE_S
            if time.time() >= grace_at:
                kill_tree(proc)
                return proc.wait(), False, True
        elif time.time() >= deadline:
            kill_tree(proc)
            return proc.wait(), True, False


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
             post_commit: bool = True) -> str:
    """Run one step; return final status: done|partial|blocked|failed|timed_out.

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
    """
    prompt_path = run_dir / "prompts" / f"{step.name}.md"
    prompt_path.parent.mkdir(exist_ok=True)
    prompt_path.write_text(
        render_prompt(step, slug, work_dir, cfg.path.parent, cfg.preamble),
        encoding="utf-8")
    stdout_log = run_dir / f"{step.name}.stdout.log"
    export_path = run_dir / f"{step.name}.export.json"

    body_src = step.prompt
    if body_src is None and step.prompt_file:
        try:
            body_src = (cfg.path.parent / step.prompt_file).read_text(
                encoding="utf-8")
        except OSError:
            body_src = step.prompt_file
    with STATE_LOCK:
        rec = state["steps"].setdefault(step.name, {})
        rec.update(name=step.name, slug=slug, project=str(work_dir),
                   work_dir=str(work_dir), branch=branch,
                   status="running", started_at=now_iso(), ended_at=None,
                   model=step.model, mode=step.mode, cloud=step.cloud,
                   effort=step.effort, timeout_min=step.timeout_min,
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
                    rc, timed_out, hung = wait_worker(
                        proc, stdout_log, step.timeout_min * 60)
                except subprocess.TimeoutExpired:      # unreachable; safety
                    kill_tree(proc)
                    proc.wait()
                    timed_out = True
            except OSError as exc:
                LOG.error("step %s spawn failed: %s", step.name, exc)
                log_event(run_dir, event="spawn_error", step=step.name,
                          error=str(exc))
                spawn_failed = True

        new_sessions = (session_ids(devin_argv, work_dir) or set()) - before
        all_sessions |= new_sessions
        status_line = tail_status(stdout_log)
        for k, v in (tail_metrics(export_path) or {}).items():
            tok_acc[k] = tok_acc.get(k, 0) + int(v)
        # Commit each attempt's product BEFORE deciding to retry - the next
        # session resumes from committed state, not the working tree.
        if post_commit and cfg.checkpoint and is_git_repo(work_dir) \
                and git_dirty(work_dir):
            git_commit(work_dir, f"conductor: work from {step.name}")
        if spawn_failed or not timed_out or attempt >= step.max_attempts:
            break
    elapsed = round(time.time() - t0, 1)

    verify_ok = None
    if step.verify and not timed_out:
        v = subprocess.run(step.verify, cwd=str(work_dir), shell=True,
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=600,
                           **hidden_subprocess_kwargs())
        verify_ok = v.returncode == 0
        (run_dir / f"{step.name}.verify.log").write_text(
            (v.stdout or "") + (v.stderr or ""), encoding="utf-8",
            errors="replace")

    if timed_out:
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
        if status_line is None and rc == 0:
            notes.append("worker exited 0 without a STATUS line "
                         "(no-op run - nothing was done)")
        if hung:
            notes.append("STATUS printed but the CLI stayed alive on a "
                         "spawned child (trap 12); tree-killed after "
                         "grace - counted as turn end")
        if notes:
            rec["note"] = "; ".join(notes)
        rec.update(status=status, ended_at=now_iso(), status_line=status_line,
                   verify_ok=verify_ok, next=tail_next(stdout_log),
                   tokens=tok_acc or None,
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
    if git_dirty(repo_dir):
        LOG.warning("%s: main checkout dirty; not spawning a merge resolver",
                    step.name)
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
        print(f"[resume] {run_dir.name}")
    else:
        run_dir = new_run_dir(cfg, runs_dir)
        state = {"run": cfg.name, "description": cfg.description,
                 "request_id": getattr(args, "request_id", None),
                 "manifest": str(manifest_path),
                 "started_at": now_iso(), "devin": " ".join(devin_argv),
                 "run_dir": str(run_dir), "max_parallel": cap,
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
    for gi, members in enumerate(groups):
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
                        prior.update(status="done", merged=True,
                                     note="merged on resume (was "
                                          f"{prior.get('status')})")
                        save_state(run_dir, state)
                        print(f"[merged] {step.name} (merge on resume)")
                    elif prior.get("status") == "done":
                        prior.update(status="failed", merged=False,
                                     note="merge conflict on resume; "
                                          f"worktree kept at {wt}")
                        save_state(run_dir, state)
                        print(f"[ failed] {step.name} (merge conflict "
                              "on resume)")
                        if cfg.on_failure == "stop":
                            stop = True
                            break
                    else:
                        prior.update(
                            merged=False,
                            note="merge still conflicts; resolve the kept "
                                 "worktree by hand or enable merge_resolver "
                                 "- re-running the same branch cannot fix "
                                 "a conflict")
                        save_state(run_dir, state)
                        print(f"[conflict] {step.name} still unmerged")
                    continue
            if args.resume is not None and prior.get("status") == "done":
                print(f"[skip ] {step.name} (done)")
                continue
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
                prepared.append((step, slug, wt, project_dir, branch))
            else:
                prepared.append((step, slug, project_dir, project_dir, None))
        if stop:
            break
        if not prepared:
            continue   # every member of this group was already done (resume)

        # Mark members queued up front: the pool may hold them while a
        # max_parallel-sized set runs, and the dashboard shows the queue.
        with STATE_LOCK:
            for step, _s, _w, _r, _b in prepared:
                state["steps"].setdefault(step.name, {}).update(
                    status="queued", name=step.name)
            save_state(run_dir, state)

        if len(prepared) == 1:
            step, slug, work_dir, _repo, branch = prepared[0]
            statuses = {step.name: run_step(step, slug, work_dir, cfg,
                                            run_dir, devin_argv, state,
                                            do_checkpoint=(branch is None),
                                            branch=branch)}
            hub_update(tid, state)
        else:
            statuses = {}
            with concurrent.futures.ThreadPoolExecutor(
                    max_workers=min(len(prepared), cap)) as ex:
                futs = {ex.submit(run_step, step, slug, work_dir, cfg,
                                  run_dir, devin_argv, state,
                                  do_checkpoint=(branch is None),
                                  branch=branch): step.name
                        for step, slug, work_dir, _r, branch in prepared}
                for f in concurrent.futures.as_completed(futs):
                    statuses[futs[f]] = f.result()
                    hub_update(tid, state)

        # Merge worktree branches back into the main checkout, in order.
        for step, slug, work_dir, repo_dir, branch in prepared:
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
                        rec.update(merged=True,
                                   note="merge conflict resolved by "
                                        f"resolve-{step.name}")
                    else:
                        rec.update(status="failed",
                                   note="merge conflict; worktree kept at "
                                        f"{work_dir}", merged=False)
                        status = "failed"
                        statuses[step.name] = "failed"
                    save_state(run_dir, state)
                elif worktree_debris(repo_dir, work_dir, branch):
                    # step died before doing work and the branch carries
                    # nothing - remove the worktree+branch instead of
                    # leaving orphan debris in the target repo.
                    worktree_remove(repo_dir, work_dir, branch)
                    rec = state["steps"].get(step.name, {})
                    rec.setdefault("note", "worktree removed: step failed "
                                           "with no work product")
                    save_state(run_dir, state)
                else:
                    rec = state["steps"].get(step.name, {})
                    rec.setdefault("note", "worktree kept at "
                                           f"{work_dir} - branch has "
                                           "unmerged work")
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
            break

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

def sweep_runs(runs_dir: Path, write: bool = True) -> list[str]:
    """Re-mark orphaned 'running'/'queued' records as 'lost' and close runs
    with nothing alive. Returns human-readable lines about what changed.

    Write guard (trap 5): if a live step's work_dir contains runs_dir, a
    self-hosted run is in flight and ANY write under runs_dir lands in the
    worker's watched tree - so this degrades to a dry-run report.
    """
    states: list[tuple[Path, dict]] = []
    watched = False
    for sj in sorted(runs_dir.glob("*/state.json")):
        try:
            st = json.loads(sj.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        states.append((sj, st))
        for rec in (st.get("steps") or {}).values():
            if step_live(rec):
                wd = rec.get("work_dir") or rec.get("project")
                try:
                    if wd and runs_dir.resolve().is_relative_to(
                            Path(wd).resolve()):
                        watched = True
                except OSError:
                    pass
    lines: list[str] = []
    for sj, st in states:
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
                return lines
            save_state(sj.parent, st)
    return lines


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
_LAUNCH_PIDS: dict[str, int] = {}
_GENERATING: set[str] = set()


class RequestError(ValueError):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def default_model_effort(today: dt.date | None = None) -> tuple[str, str]:
    return (("swe-2", "max") if (today or dt.date.today())
            <= dt.date(2026, 10, 16) else ("gpt-6-luna", "max"))


def _request_path(request_id: str, requests_dir: Path) -> Path:
    if not REQUEST_ID_RE.fullmatch(request_id):
        raise RequestError("Invalid request id", 404)
    return requests_dir / f"{request_id}.json"


def _read_request(request_id: str, requests_dir: Path) -> dict:
    try:
        return json.loads(_request_path(request_id, requests_dir).read_text(
            encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RequestError("Request not found", 404) from exc
    except (OSError, ValueError) as exc:
        raise RequestError("Cannot read request", 500) from exc


def _save_request(rec: dict, requests_dir: Path) -> None:
    requests_dir.mkdir(parents=True, exist_ok=True)
    p = _request_path(rec["id"], requests_dir)
    tmp = p.with_suffix(".json.tmp")
    rec["updated_at"] = now_iso()
    tmp.write_text(json.dumps(rec, indent=2, ensure_ascii=False),
                   encoding="utf-8")
    tmp.replace(p)


def _request_fields(payload: dict, rec: dict | None = None) -> dict:
    if not isinstance(payload, dict) or set(payload) - {
            "raw", "label", "project", "model", "effort", "manifest",
            "followup"}:
        raise RequestError("Invalid request fields")
    model, effort = default_model_effort()
    fields = {k: (rec or {}).get(k, v) for k, v in (
        ("raw", ""), ("label", ""), ("project", ""),
        ("model", model), ("effort", effort))}
    for key in fields:
        if key in payload:
            if not isinstance(payload[key], str):
                raise RequestError(f"{key} must be text")
            fields[key] = payload[key].strip()
    fields["followup"] = bool((rec or {}).get("followup"))
    if "followup" in payload:
        v = payload["followup"]
        fields["followup"] = (v.strip().lower() in ("1", "true", "on", "yes")
                              if isinstance(v, str) else bool(v))
    if not fields["raw"] or len(fields["raw"]) > 100_000:
        raise RequestError("Request text must be 1–100,000 characters")
    if len(fields["label"]) > 80:
        raise RequestError("Label must be at most 80 characters")
    if fields["project"] and fields["project"] not in load_projects():
        raise RequestError("Choose a registered project from projects.toml")
    for key in ("model", "effort"):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", fields[key]):
            raise RequestError(f"Invalid {key}")
    if fields["model"] == "swe-2" and fields["effort"] not in {
            "medium", "high", "max"}:
        raise RequestError("SWE-2 supports medium, high or max effort")
    fields["label"] = fields["label"] or " ".join(fields["raw"].split())[:80]
    return fields


def _assert_unwatched(runs_dir: Path, *destinations: Path) -> None:
    for sj in runs_dir.glob("*/state.json"):
        try:
            st = json.loads(sj.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for step in (st.get("steps") or {}).values():
            wd = step.get("work_dir") or step.get("project")
            if wd and step_live(step) and any(
                    p.resolve().is_relative_to(Path(wd).resolve())
                    for p in destinations):
                raise RequestError("A live agent is watching this workspace; "
                                   "wait until it finishes before changing "
                                   "requests or manifests", 409)


def _wait_writable(runs_dir: Path, *destinations: Path) -> None:
    while True:
        try:
            _assert_unwatched(runs_dir, *destinations)
            return
        except RequestError:
            time.sleep(5)


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
                if val is not None and not re.fullmatch(
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
        if stage not in ("draft", "ready", "error", "launch_failed"):
            raise RequestError("This request is being processed or already run", 409)
        rec["status"] = stage
        fields = _request_fields(payload, rec)
        changed = any(fields[k] != rec.get(k) for k in
                      ("raw", "project", "model", "effort", "followup"))
        rec.update(fields)
        if "manifest" in payload:
            return _publish_proposal(rec, payload["manifest"],
                                     requests_dir, manifests_dir, runs_dir)
        if rec.get("manifest") and changed:
            rec["stale"] = True
        _save_request(rec, requests_dir)
        return dict(rec)


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
        log = requests_dir / f"{request_id}.gen.log"
        rec["gen_tail"] = (stdout_tail(str(log), 40)
                           if log.is_file() else None)
        if runs_dir:
            status = _request_status(rec, runs_dir)
            if status == "error" and rec["status"] == "generating":
                rec["error"] = "Planning was interrupted. Regenerate to retry."
            rec["status"] = status
        return rec


def _planner_response(rec: dict, projects: dict,
                      devin_argv: list[str],
                      log_path: Path | None = None,
                      fix_note: str | None = None) -> str:
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
    prompt = f"""You are writing a proposed agent-conductor TOML runbook, not doing the work.
Do not edit files, invoke tools, run commands, inspect projects, or start a run.
Reply with exactly one TOML document between BEGIN_MANIFEST and END_MANIFEST,
then a final STATUS: done line. No Markdown fences or other text.
The ONLY task specification is the human request below; do not invent repo details.
Registered project slugs (use these, never paths): {slugs}
Preferred project (if supplied): {rec['project'] or '(infer from request)'}
[run] needs name (short safe slug), description (one useful sentence),
on_failure (stop or continue), and checkpoint = true.
[defaults] needs model = {json.dumps(rec['model'])}, effort =
{json.dumps(rec['effort'])}, mode = "bypass", and timeout_min.
Set max_attempts = 3 in [defaults]: a step that underestimates its
timeout_min is then continued by a fresh session instead of ending early.
Each [[step]] needs a unique name, a registered project slug, and a concrete
prompt describing only that step. Prefer sequential steps. If using waves on
one project, every member needs worktree = true. Cloud steps require explicit
user intent and a project permitted for cloud. No prompt_file or preamble.
Do not guess verification commands; include verify only when known.
Do not reproduce credentials or private data in the tracked manifest; refer
agents to the project files containing those details instead.
The proposal must NOT start work; the human will edit it and launch later.
Windows paths belong in TOML literal strings (single quotes) or use forward
slashes - a backslash inside a double-quoted string is a TOML escape and
breaks parsing.
{followup}
{fix}HUMAN REQUEST:
{rec['raw']}

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
        cmd = devin_argv + [
            "--print", "--prompt-file", str(prompt_path),
            "--model", f"{rec['model']}-{rec['effort']}",
            "--permission-mode", "normal", "--respect-workspace-trust", "false"]
        with open(log, "ab" if fix_note else "wb") as out:
            proc = subprocess.Popen(cmd, cwd=wd, stdout=out,
                                    stderr=subprocess.STDOUT,
                                    stdin=subprocess.DEVNULL,
                                    env=clean_devin_env(),
                                    **hidden_subprocess_kwargs())
            rc, timed_out, _hung = wait_worker(proc, log, 600)
        if timed_out or rc != 0:
            raise RequestError("Planner did not finish; check CLI auth and "
                               "retry generation", 502)
        text = log.read_text(encoding="utf-8", errors="replace")
    matches = re.findall(r"^BEGIN_MANIFEST\s*\n(.*?)^END_MANIFEST\s*$",
                         text, re.M | re.S)
    if not matches:
        raise RequestError("Planner returned no TOML proposal; try regenerating",
                           502)
    return matches[-1].strip() + "\n"


def _finish_generation(request_id: str, requests_dir: Path,
                       manifests_dir: Path, runs_dir: Path,
                       devin_argv: list[str]) -> None:
    try:
        rec = _read_request(request_id, requests_dir)
        log_path = requests_dir / f"{request_id}.gen.log"
        projects = load_projects()
        text = _planner_response(rec, projects, devin_argv, log_path)
        try:
            defaults = tomllib.loads(text).get("defaults", {})
        except tomllib.TOMLDecodeError as exc:
            # A malformed draft (e.g. a Windows path's \U inside a double-
            # quoted string) is trivially fixable - retry once with the
            # parse error fed back.
            text = _planner_response(rec, projects, devin_argv, log_path,
                                     fix_note=str(exc))
            defaults = tomllib.loads(text).get("defaults", {})
        if defaults.get("model") != rec["model"] or \
                defaults.get("effort") != rec["effort"]:
            raise RequestError("Planner ignored the selected model or effort; "
                               "regenerate or edit the TOML directly")
        while True:
            _wait_writable(runs_dir, requests_dir, manifests_dir)
            try:
                with REQUEST_LOCK:
                    rec = _read_request(request_id, requests_dir)
                    if rec["status"] == "generating":
                        _publish_proposal(rec, text, requests_dir,
                                          manifests_dir, runs_dir)
                break
            except RequestError:
                try:
                    _assert_unwatched(runs_dir, requests_dir, manifests_dir)
                except RequestError:
                    continue
                raise
    except Exception as exc:
        _wait_writable(runs_dir, requests_dir, LOGS_DIR)
        LOG.warning("request %s generation failed: %s", request_id, exc)
        msg = (f"Planner returned malformed TOML ({exc})"
               if isinstance(exc, tomllib.TOMLDecodeError) else str(exc))
        with REQUEST_LOCK:
            rec = _read_request(request_id, requests_dir)
            if rec["status"] == "generating":
                rec.update(status="error", error=msg[:300])
                _save_request(rec, requests_dir)
    finally:
        with REQUEST_LOCK:
            _GENERATING.discard(request_id)


def start_generation(request_id: str,
                     requests_dir: Path = REQUESTS_DIR,
                     manifests_dir: Path = MANIFESTS_DIR,
                     runs_dir: Path = RUNS_DIR,
                     devin_argv: list[str] | None = None,
                     background: bool = True) -> dict:
    with REQUEST_LOCK:
        _assert_unwatched(runs_dir, requests_dir)
        rec = _read_request(request_id, requests_dir)
        if _request_status(rec, runs_dir) not in (
                "draft", "ready", "error", "launch_failed"):
            raise RequestError("Only drafts or unrun proposals can be generated", 409)
        rec.update(status="generating", error=None, generation_started_at=now_iso())
        _save_request(rec, requests_dir)
        _GENERATING.add(request_id)
    args = (request_id, requests_dir, manifests_dir, runs_dir,
            devin_argv or ["devin"])
    if background:
        threading.Thread(target=_finish_generation, args=args,
                         daemon=True).start()
        return rec
    _finish_generation(*args)
    return request_details(request_id, requests_dir, manifests_dir)


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
    if rec["status"] == "generating" and rec["id"] not in _GENERATING \
            and time.time() - (_iso_ts(rec.get("generation_started_at"))
                               or time.time()) > 660:
        return "error"
    if rec["status"] != "launching":
        return rec["status"]
    age = time.time() - (_iso_ts(rec.get("launched_at")) or time.time())
    if age > 90 and (rec["id"] not in _LAUNCH_PIDS or not pid_alive(
            _LAUNCH_PIDS[rec["id"]], rec.get("launched_at"))):
        return "launch_failed"
    return "launching"


def _launch_process(cmd: list[str], log_path: Path) -> int:
    with open(log_path, "wb") as out:
        proc = subprocess.Popen(cmd, cwd=str(ROOT), stdout=out,
                                stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                env=clean_devin_env(),
                                **hidden_subprocess_kwargs())
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
            if any(not step.get("branch") and step_live(step) and
                   Path(step.get("work_dir") or step.get("project") or "").resolve()
                   in targets for step in (st.get("steps") or {}).values()):
                raise RequestError("A live agent already works in this project; "
                                   "wait or isolate work in a worktree", 409)
        if ROOT.resolve() in targets and any(
                r.get("status") == "generating" for f in
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


def discard_request(request_id: str,
                    requests_dir: Path = REQUESTS_DIR,
                    manifests_dir: Path = MANIFESTS_DIR,
                    runs_dir: Path = RUNS_DIR) -> dict:
    with REQUEST_LOCK:
        _assert_unwatched(runs_dir, requests_dir, manifests_dir)
        rec = _read_request(request_id, requests_dir)
        if _request_status(rec, runs_dir) not in (
                "draft", "ready", "error", "launch_failed"):
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
            if wd and (rec.get("session_id") or step_live(rec)) \
                    and Path(wd).is_dir():
                dirs.add(wd)
    out = {d: session_activity(devin_argv, Path(d)) for d in dirs}
    _SESS_CACHE[0], _SESS_CACHE[1] = time.time(), out
    return out


def task_label(rec: dict) -> str | None:
    """What the agent was asked to do. New records store `task` directly;
    for pre-field records, dig the body out of the rendered prompt file
    (between the preamble's '## Task' and '## Before you finish' markers)."""
    if rec.get("task"):
        return rec["task"]
    pf = rec.get("prompt")
    if not pf:
        return None
    try:
        text = Path(pf).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    m = re.search(r"## Task\s*\n+(.*?)\n*## ", text, re.S)
    body = m.group(1) if m else text
    return " ".join(body.split())[:160] or None


def stdout_tail(path: str | None, lines: int = 15) -> str | None:
    """Last `lines` of a step's stdout log - the modal's 'how it ended'."""
    if not path:
        return None
    try:
        data = Path(path).read_bytes()[-8192:]
    except OSError:
        return None
    return "\n".join(data.decode("utf-8", "replace").splitlines()[-lines:])


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
            "task": step.get("task"), "wt": step.get("wt"),
            "merged": None, "cloud": step.get("cloud"), "tokens": None,
            "alive": False,
            "note": "Run ended before this step started" if status == "skipped"
            else None}


def collect_dashboard(runs_dir: Path,
                      devin_argv: list[str] | None = None,
                      requests_dir: Path | None = None) -> dict:
    """Show executed run states alongside saved, not-yet-executed requests.
    A launched request uses state.json for its rows, not its proposal preview."""
    requests_dir = requests_dir or runs_dir.parent / "requests"
    sess_by_dir = live_sessions_by_dir(runs_dir, devin_argv or ["devin"])
    projects = load_projects()
    requests: dict[str, dict] = {}
    for rf in requests_dir.glob("*.json"):
        try:
            rec = json.loads(rf.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(rec, dict) and REQUEST_ID_RE.fullmatch(
                str(rec.get("id") or "")) and rec.get("status") != "discarded":
            requests[rec["id"]] = rec
    launched: set[str] = set()
    runs: list[dict] = []
    agents: list[dict] = []
    for sj in sorted(runs_dir.glob("*/state.json"), reverse=True):
        try:
            st = json.loads(sj.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if st.get("request_id"):
            launched.add(st["request_id"])
        key = (f"request:{st['request_id']}" if st.get("request_id")
               else str(sj.parent))
        counts: dict[str, int] = {}
        tok_run: dict[str, int] = {}
        request = requests.get(st.get("request_id"))
        proposed = {s["step"]: s for s in request.get("proposed_steps", [])} \
            if request else {}
        eff = effective_statuses(st)
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
            agents.append({
                "run": st.get("run"), "run_dir": st.get("run_dir") or str(sj.parent),
                "run_key": key, "request_id": st.get("request_id"),
                "step": rec.get("name") or name, "slug": slug,
                "work_dir": wd,
                "status": status, "started_at": rec.get("started_at"),
                "ended_at": rec.get("ended_at"),
                "elapsed_s": rec.get("elapsed_s"),
                "alive": step_live(rec),
                "pid": rec.get("pid"), "session_id": rec.get("session_id"),
                "model": rec.get("model") or plan.get("model"),
                "mode": rec.get("mode") or plan.get("mode"),
                "effort": rec.get("effort") or plan.get("effort"),
                "task": task_label(rec) or plan.get("task"),
                "cloud": rec.get("cloud", plan.get("cloud")),
                "verify_ok": rec.get("verify_ok"),
                "note": rec.get("note"), "next": rec.get("next"),
                "session_gone": bool(sid and live is not None
                                     and str(sid) not in live),
                "exit_code": rec.get("exit_code"),
                "branch": rec.get("branch"), "merged": rec.get("merged"),
                "tokens": rec.get("tokens"),
                "wt": bool(rec.get("branch") or plan.get("wt")),
                "stdout_log": rec.get("stdout_log"),
                "tail": stdout_tail(rec.get("stdout_log")),
                "export": rec.get("export"), "prompt": rec.get("prompt")})
        if request:
            pending = "skipped" if st.get("ended_at") else "waiting"
            for step in request.get("proposed_steps") or []:
                if step["step"] in (st.get("steps") or {}):
                    continue
                counts[pending] = counts.get(pending, 0) + 1
                agents.append(proposal_agent(
                    step, request, projects, key, pending, str(sj.parent),
                    st.get("started_at")))
        runs.append({"run": st.get("run"), "dir": str(sj.parent),
                     "key": key, "request_id": st.get("request_id"),
                     "description": st.get("description") or
                     (request or {}).get("description"),
                     "started_at": st.get("started_at"),
                     "ended_at": st.get("ended_at"), "counts": counts,
                     "tokens": tok_run or None,
                     "max_parallel": st.get("max_parallel"),
                     "projects": sorted({r.get("slug") for r in
                                         st.get("steps", {}).values()
                                         if r.get("slug")} |
                                        {s["slug"] for s in proposed.values()})})
    for rid, rec in requests.items():
        if rid in launched:
            continue
        stage = (_request_status(rec, runs_dir)
                 if rec.get("status") in ("launching", "generating")
                 else rec.get("status"))
        if stage == "launched":
            continue
        steps = rec.get("proposed_steps") or []
        key = f"request:{rid}"
        log_p = requests_dir / f"{rid}.gen.log"
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
                     "started_at": None, "ended_at": None,
                     "counts": {"planned": len(steps)} if steps else {},
                     "tokens": None, "max_parallel": None,
                     "stage": stage, "error": rec.get("error"),
                     "stale": rec.get("stale"), "gen_last": gen_last,
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
                "model": rec.get("model"), "effort": rec.get("effort"),
                "mode": "planner", "cloud": False, "verify_ok": None,
                "task": "Propose a TOML runbook for: " + " ".join(
                    (rec.get("raw") or "").split())[:120],
                "note": None, "next": None, "exit_code": None,
                "branch": None, "merged": None, "tokens": None, "wt": False,
                "stdout_log": str(log_p) if log_p.is_file() else None,
                "tail": gen_tail, "export": None, "prompt": None})
        for s in steps:
            agents.append(proposal_agent(s, rec, projects, key, "planned"))
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
        a["tail_last"] = next(
            (ln for ln in reversed((a.get("tail") or "").splitlines())
             if ln.strip()), None)
    agents.sort(key=lambda a: a.get("started_at") or a.get("created_at") or "",
                reverse=True)
    runs.sort(key=lambda r: r.get("started_at") or r.get("updated_at") or
              r.get("created_at") or "", reverse=True)
    return {"generated_at": now_iso(), "serve_boot": SERVE_BOOT,
            "cpu_count": os.cpu_count(),
            "free_ram_gb": free_ram_gb(), "runs": runs, "agents": agents,
            "project_options": [{"slug": slug, "cloud_ok": p["cloud_ok"]}
                                for slug, p in sorted(projects.items())],
            "defaults": dict(zip(("model", "effort"), default_model_effort())),
            "auth": auth_status(devin_argv or ["devin"])}


_DASH_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>agent-conductor</title>
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'%3E%3Crect width='64' height='64' rx='14' fill='%2313161f'/%3E%3Cline x1='13' y1='19' x2='49' y2='7' stroke='%23e6edf3' stroke-width='2.6' stroke-linecap='round'/%3E%3Ccircle cx='13' cy='19' r='3' fill='%23e6edf3'/%3E%3Ccircle cx='12' cy='37' r='3' fill='%2358a6ff'/%3E%3Crect x='8.5' y='41' width='7' height='11' rx='2' fill='%2358a6ff'/%3E%3Ccircle cx='23.5' cy='31' r='3' fill='%23bc8cff'/%3E%3Crect x='20' y='35' width='7' height='17' rx='2' fill='%23bc8cff'/%3E%3Ccircle cx='35' cy='26' r='3' fill='%23d29922'/%3E%3Crect x='31.5' y='30' width='7' height='22' rx='2' fill='%23d29922'/%3E%3Ccircle cx='46.5' cy='34' r='3' fill='%233fb950'/%3E%3Crect x='43' y='38' width='7' height='14' rx='2' fill='%233fb950'/%3E%3C/svg%3E">
<style>
:root { color-scheme: dark; }
body { background:#0d1117; color:#e6edf3; font:14px/1.45 ui-monospace,Consolas,monospace; margin:0; padding:16px; }
h1 { font-size:18px; margin:0 0 4px; }
.sub { color:#8b949e; font-size:12px; margin-bottom:14px; }
h2 { font-size:13px; text-transform:uppercase; letter-spacing:.08em; color:#8b949e; margin:22px 0 8px; }
h3.proj { font-size:13px; color:#e6edf3; margin:16px 0 4px; }
h3.proj .sub { margin-bottom:0; font-weight:400; }
table { border-collapse:collapse; width:100%; table-layout:fixed; }
th { text-align:left; font-weight:600; color:#8b949e; font-size:11px; text-transform:uppercase; padding:4px 10px 4px 0; border-bottom:1px solid #21262d; }
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
.wrapw { overflow-x:auto; }
.counts span { background:#161b22; border-radius:8px; padding:1px 8px; margin-right:5px; font-size:12px; }
a { color:#58a6ff; text-decoration:none; }
[hidden] { display:none !important; }
.tok-help { display:inline-block; position:relative; margin-left:6px; vertical-align:middle; }
.tok-help summary { display:inline-grid; place-items:center; width:18px; height:18px; border:1px solid #6e7681; border-radius:50%; color:#58a6ff; cursor:pointer; list-style:none; }
.tok-help summary::-webkit-details-marker { display:none; }
.tok-help .explain { padding:10px 12px; margin-top:7px; border:1px solid #30363d; background:#0d1117; border-radius:6px; font-size:12px; line-height:1.6; }
#backdrop { position:fixed; inset:0; background:rgba(0,0,0,.55); z-index:8; }
#detail { position:fixed; left:50%; top:50%; transform:translate(-50%,-50%); width:min(780px,92vw); max-height:82vh; background:#161b22; border:1px solid #30363d; border-radius:10px; padding:18px 22px; overflow:auto; z-index:9; font-size:13px; box-sizing:border-box; }
#detail h3 { margin:0 80px 10px 0; font-size:14px; }
#detail .kv { display:grid; grid-template-columns:96px 1fr; gap:4px 12px; }
#detail .k { color:#8b949e; }
#detail pre { white-space:pre-wrap; color:#8b949e; background:#0d1117; border-radius:6px; padding:8px 10px; font-size:12px; max-height:180px; overflow:auto; }
#detail .x { position:absolute; top:10px; right:16px; cursor:pointer; color:#8b949e; background:none; border:0; font:inherit; }
.toolbar { display:flex; align-items:center; gap:12px; flex-wrap:wrap; }
.toolbar .sub { margin:0; }
.stage { display:inline-block; border-radius:9px; padding:1px 7px; font-size:11px; border:1px solid #30363d; white-space:nowrap; }
.stage.draft { color:#e3b341; border-color:#6b5a1f; background:#2a2317; }
.stage.ready { color:#bc8cff; border-color:#614b84; background:#251d35; }
.stage.generating { color:#58a6ff; background:#172638;
  animation:pulse 1.2s ease-in-out infinite; }
.stage.launching { color:#58a6ff; background:#172638; }
.stage.error, .stage.launch_failed { color:#f85149; background:#321c20; }
.linkbtn { background:none; border:0; color:#58a6ff; cursor:pointer; font:inherit; padding:0; text-decoration:underline; }
.btn { border:1px solid #388bfd; border-radius:6px; background:#1f6feb; color:#fff; padding:7px 12px; cursor:pointer; font:inherit; }
.btn.secondary { color:#e6edf3; background:#21262d; border-color:#30363d; }
.btn.danger { color:#ffb2aa; background:transparent; border-color:#9f3535; }
.btn:disabled { cursor:not-allowed; opacity:.45; }
#detail label { display:block; color:#8b949e; margin:12px 0 5px; }
#detail input, #detail textarea, #detail select { box-sizing:border-box; width:100%; padding:8px 10px; border:1px solid #30363d; border-radius:6px; background:#0d1117; color:#e6edf3; font:inherit; }
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
@media (max-width:640px) { .hide-s { display:none; } #detail { width:92vw; } #detail .field-row { grid-template-columns:1fr; } }
</style></head><body>
<h1>agent-conductor</h1>
<div class="sub" id="meta">loading&hellip;</div>
<div class="toolbar"><button type="button" class="btn" id="new-request">New request</button><span class="sub">Save draft → generate proposal → review → start when ready. Generating never runs tasks.</span></div>
<div class="sub" id="filters"></div>
<h2>runs</h2><div class="wrapw"><table id="runs"></table></div>
<h2>agents by project</h2><div id="agents"></div>
<div id="backdrop" hidden><div id="detail" role="dialog" aria-modal="true" aria-labelledby="modal-title"><button type="button" class="x" id="dx">Close ×</button><div id="dbody"></div></div></div>
<script>
const COLORS = {done:'#3fb950',partial:'#d29922',blocked:'#d29922',
failed:'#f85149',timed_out:'#f0883e',running:'#58a6ff',queued:'#8b949e',
waiting:'#8b949e',skipped:'#6e7681',planned:'#bc8cff',ready:'#bc8cff',
generating:'#bc8cff',draft:'#d29922',
launching:'#58a6ff',launch_failed:'#f85149',lost:'#6e7681'};
const STAGES = {draft:'Draft · unsent',generating:'Preparing proposal',
  ready:'Ready to run',launching:'Starting run',launch_failed:'Start failed',
  error:'Planning failed'};
const esc = s => (s==null?'':String(s)).replace(/[&<>"]/g,
  c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
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
const fmtT = t => (!t||t.prompt==null) ? '-' :
  `${kfmt(t.prompt)}/${kfmt(t.completion)}/${kfmt(t.cached)}`;
const tokTitle = t => !t ? '' :
  `prompt ${t.prompt} · completion ${t.completion} · ` +
  `cached ${t.cached} · ${t.llm_steps??'?'} LLM steps`;
const TOK_HELP = 'prompt / completion / cached; select an agent for details';
const tokenHelp = `<details class="tok-help"><summary aria-label="Explain tokens and cost">?</summary>` +
  `<div class="explain"><b>What the numbers mean</b><br>` +
  `<b>Prompt</b>: all input tokens (instructions, tools, context, history).<br>` +
  `<b>Cached</b>: the part of prompt served from cache; do not add it to prompt.<br>` +
  `<b>Completion</b>: output tokens. <b>LLM steps</b>: model turns. ` +
  `<b>M</b> in prices means one million tokens.<br><br>` +
  `<b>Estimated cost</b> = (prompt - cached) × input rate + ` +
  `cached × cache rate + completion × output rate, each rate per token ` +
  `for that model and effort.<br><br>` +
  `<b>Example (illustrative rates, not a quote):</b> 10,000 prompt, ` +
  `6,000 cached, 1,000 completion; at $2/M input, $0.50/M cached, ` +
  `$8/M output: 4,000 × $2/M + 6,000 × $0.50/M + 1,000 × $8/M ` +
  `= $0.008 + $0.003 + $0.008 = <b>$0.019</b>. ` +
  `Actual rates vary; priced totals await model-compare.</div></details>`;
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
const hidden = new Set(); // statuses filtered out via chips
const facets = new Set();
let selectedRun = null;
const prevTail = {};      // run|step -> last tail_last shown; a change
                          // flashes the result cell (.fresh) as logs stream
const SHOW_N = 10;
let runsShown = SHOW_N;   // visible run-table rows; active runs never hide
const ORDER = ['running','queued','waiting','planned','done','partial',
               'blocked','timed_out','failed','skipped','lost'];
const FACETS = ['wt','merged','unmerged'];
const matchesFacet = (a,f) => f==='wt' ? a.wt :
  f==='merged' ? a.merged===true : a.merged===false;
const runKey = r => r.key||r.dir;
let lastData = null;
let AGENTS = [];          // last payload, for the detail modal

// column layout shared by every per-project table (fixed layout -> the
// same widths line up across tables)
const AGENT_COLS = '<colgroup>' +
  '<col style="width:7%"><col style="width:12%"><col style="width:18%">' +
  '<col style="width:7%"><col class="hide-s" style="width:7%">' +
  '<col class="hide-s" style="width:5%"><col class="hide-s" style="width:6%">' +
  '<col style="width:6%"><col style="width:5%">' +
  '<col class="hide-s" style="width:7%"><col style="width:6%">' +
  '<col class="hide-s" style="width:6%"><col style="width:8%">' +
  '</colgroup>';

let detailIdx = null;
let modalRequest = null;
function closeDetail(force=false){
  if (!force && modalRequest?.dirty && !confirm('Discard unsaved edits?')) return false;
  detailIdx = null; modalRequest = null;
  document.getElementById('backdrop').hidden = true;
  return true;
}
function showDetail(i){
  const a = AGENTS[i]; if(!a || !closeDetail()) return;
  detailIdx = i;
  const kv = [['step',a.step],['run',a.run],['project',a.slug],
    ['status',a.status],['task',a.task],['result',a.next||a.note],
    ['model',(a.model||'-')+(a.effort?' (effort '+a.effort+')':'')],
    ['mode',(a.mode||'-')+(a.cloud?' cloud':'')],
    ['started',a.started_at],['ended',a.ended_at],
    ['elapsed',a.elapsed_s!=null?fmtS(a.elapsed_s):'-'],
    ['tokens',tokTitle(a.tokens)||'-'],
    ['session',(a.session_id||'-')+(a.session_gone?' (deleted from sidebar)':'')],
    ['verify',a.verify_ok==null?'-':(a.verify_ok?'pass':'FAIL')],
    ['exit',a.exit_code],['work dir',a.work_dir],
    ['branch',a.branch],['merged',a.merged],
    ['prompt',a.prompt],['stdout',a.stdout_log],['export',a.export]];
  document.getElementById('dbody').innerHTML =
    `<h3 id="modal-title">${esc(a.slug)} / ${esc(a.step)}</h3><div class="kv">` +
    kv.map(([k,v])=>`<div class="k">${k}</div><div class="wrap">${esc(v??'-')}` +
      (k==='tokens'?tokenHelp:'') + '</div>').join('') +
    `</div>` +
    (a.request_id ? `<div class="actions"><button type="button" class="btn secondary" ` +
      `data-open-request="${esc(a.request_id)}">View request / runbook</button></div>` : '') +
    (a.tail ? `<h3 style="margin-top:14px">stdout tail</h3><pre id="dtail">${esc(a.tail)}</pre>` : '');
  document.getElementById('backdrop').hidden = false;
}

function showRunDetails(key){
  const r = lastData?.runs.find(x=>runKey(x)===key);
  if (!r || !closeDetail()) return;
  const kv = [['description',r.description||'-'],['projects',(r.projects||[]).join(', ')],
    ['started',r.started_at],['ended',r.ended_at],
    ['steps',Object.entries(r.counts||{}).map(([s,n])=>`${n} ${s}`).join(', ')],
    ['tokens',tokTitle(r.tokens)||'-'],['manifest / dir',r.dir]];
  document.getElementById('dbody').innerHTML =
    `<h3 id="modal-title">${esc(r.run)}</h3>` +
    (r.stage ? `<span class="stage ${esc(r.stage)}">${esc(STAGES[r.stage]||r.stage)}</span>` : '') +
    `<div class="kv" style="margin-top:12px">` +
    kv.map(([k,v])=>`<div class="k">${k}</div><div class="wrap">${esc(v??'-')}` +
      (k==='tokens'?tokenHelp:'') + `</div>`).join('') + '</div>' +
    `<div class="actions"><button type="button" class="btn secondary" ` +
    `data-filter-run="${esc(key)}">Show only this run</button>` +
    (r.request_id ? `<button type="button" class="btn secondary" ` +
      `data-open-request="${esc(r.request_id)}">View original request / TOML</button>` : '') +
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
async function refresh(){ render(await api('GET','/api/state')); }
function openNew(){
  if (!closeDetail()) return;
  const defaults=lastData?.defaults||{model:'swe-2',effort:'max'};
  modalRequest={id:null,status:'new',dirty:false,originalManifest:''};
  showRequestForm({status:'new',raw:'',label:'',project:'',...defaults});
}
function showRequestForm(rec){
  const stage=rec.status, editable=!['generating','launching','launched'].includes(stage);
  const disabled=editable?'':' disabled';
  const choices=lastData?.project_options||[];
  const projectOptions=`<option value="">Infer from request</option>` +
    choices.map(p=>`<option value="${esc(p.slug)}"${p.slug===rec.project?' selected':''}>` +
      `${esc(p.slug)}${p.cloud_ok?'':' (local only)'}</option>`).join('');
  const effortOptions=['min','low','medium','high','max'].map(x=>
    `<option value="${x}"${x===rec.effort?' selected':''}>${x}</option>`).join('');
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
    (rec.stale ? `<div class="notice warn">Request or model changed since ` +
      `the proposal. Regenerate tasks, or edit and save the TOML before ` +
      `starting a run.</div>`:'') +
    (stage==='generating'?`<div class="notice warn">Planning in progress. ` +
      `The previous proposal, if any, remains available if generation fails.</div>`:'') +
    (rec.gen_tail && (stage==='generating'||stage==='error')
      ? `<label>Planner output${stage==='generating'?' (live)':''}</label>` +
        `<pre id="gtail">${esc(rec.gen_tail)}</pre>`:'') +
    (stage==='launching'||stage==='launched'?`<div class="notice">This ` +
      `request has already been started; editing and discard are disabled.</div>`:'') +
    `<label for="rq-label">Short label (optional until planned)</label>` +
    `<input id="rq-label" maxlength="80" value="${esc(rec.label||'')}"${disabled}>` +
    `<div class="field-row"><div><label for="rq-project">Project</label>` +
    `<select id="rq-project"${disabled}>${projectOptions}</select></div>` +
    `<div><label for="rq-model">Model</label><input id="rq-model" ` +
    `list="rq-models" value="${esc(rec.model||'')}"${disabled}>` +
    `<datalist id="rq-models"><option value="swe-2"><option value="gpt-6-luna">` +
    `</datalist></div><div><label for="rq-effort">Effort</label>` +
    `<select id="rq-effort"${disabled}>${effortOptions}</select></div></div>` +
    `<label for="rq-raw">Your request</label><textarea id="rq-raw"${disabled} ` +
    `placeholder="Describe the work in your own words…">${esc(rec.raw||'')}</textarea>` +
    `<label class="check"><input type="checkbox" id="rq-followup"` +
      `${rec.followup?' checked':''}${disabled}> End the run with a ` +
      `planning step that drafts a follow-up manifest into manifests/ ` +
      `for anything unfinished</label>` +
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
    (editable?`<button type="button" class="btn secondary" data-action="save">` +
      `${stage==='new'||stage==='draft'?'Save draft':'Save edits'}</button>` +
      `<button type="button" class="btn" data-action="generate">` +
      `${rec.manifest?'Regenerate tasks':'Generate proposal'}</button>`:'') +
    (stage==='error'&&rec.manifest_text ? `<button type="button" ` +
      `class="btn secondary" data-action="restore">Keep previous plan</button>`:'') +
    (stage==='ready'||stage==='launch_failed' ? `<button type="button" ` +
      `class="btn" data-action="run"${rec.stale?' disabled':''}>` +
      `${stage==='launch_failed'?'Retry start':'Start run'}</button>`:'') +
    (canEdit?`<button type="button" class="btn danger" data-action="discard">` +
      `Discard</button>`:'') +
    (stage==='launched'?`<button type="button" class="btn secondary" ` +
      `data-run-details="request:${esc(rec.id)}">View run details</button>`:'') +
    `</div>`;
  document.getElementById('backdrop').hidden = false;
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
  const f=Object.fromEntries(['raw','label','project','model','effort'].map(k=>
    [k,document.getElementById(`rq-${k}`).value]));
  const fup=document.getElementById('rq-followup');
  if (fup) f.followup=fup.checked;
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
  if (action==='run'){
    if (modalRequest.dirty){ formError('Save and review edits before starting.'); return; }
    if (!confirm(`Start ${rec.run} now? This will execute ${rec.proposed_steps?.length||0} ` +
                 `agent steps and make git checkpoints.`)) return;
  }
  const buttons=document.querySelectorAll('#detail [data-action]');
  buttons.forEach(b=>b.disabled=true);
  try {
    let rid=id;
    if (action==='save'||action==='generate'||action==='restore'){
      const fields=requestFields();
      if (!rid){
        const saved=await api('POST','/api/requests',fields);
        rid=saved.id; modalRequest.id=rid;
      } else {
        if (action==='restore'||action==='save'&&editedManifest)
          fields.manifest=manifestEl.value;
        await api('POST',`/api/requests/${rid}/save`,fields);
      }
      if (action==='generate')
        await api('POST',`/api/requests/${rid}/generate`,{});
    } else if (action==='run'){
      await api('POST',`/api/requests/${rid}/run`,{});
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
    (d.auth && d.auth!=='ok'
      ? ` <b style="color:#f85149">AUTH: ${esc(d.auth)}</b>` +
        ` <a href="#" data-auth="login">[log in]</a>` +
        ` <a href="#" data-auth="recheck">[recheck]</a>`
      : ` <span style="color:#3fb950">auth ok</span>`);
  // Only rebuild the tables when the payload actually changes - rebuilding
  // innerHTML every 2s resets animations/hover and looks like a reload.
  // `tail` is excluded: live log growth streams into the open modal instead.
  const sig = JSON.stringify([d.runs, d.agents.map(({tail,...r})=>r),
    selectedRun, [...hidden].sort(), [...facets].sort(), expanded,
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
      `<button type="button" class="chip${hidden.has(s)?' off':''}" data-st="${s}" ` +
      `aria-pressed="${!hidden.has(s)}" style="color:${COLORS[s]||'#8b949e'}">` +
      `${byStatus[s]} ${s}</button>`).join('') + '</span>' +
    `<span class="filter-group"><span class="filter-label">tags:</span>` +
    FACETS.filter(f=>facetCounts[f]||facets.has(f)).map(f=>
      `<button type="button" class="chip${facets.has(f)?' active':''}" ` +
      `data-facet="${f}" aria-pressed="${facets.has(f)}">` +
      `${facetCounts[f]} ${f}${facets.has(f)?' ×':''}</button>`).join('') +
    '</span>' +
    (hidden.size||facets.size?` <button type="button" class="chip" ` +
      `data-reset>clear filters</button>`:'');

  // runs table first - a run can span projects (chips show which)
  let h = '<colgroup><col style="width:13%"><col style="width:19%">' +
    '<col style="width:15%"><col style="width:9%"><col style="width:9%">' +
    '<col style="width:17%"><col style="width:5%">' +
    '<col class="hide-s" style="width:13%"></colgroup>' +
    '<tr><th>run</th><th>description</th><th>projects</th><th>started</th>' +
    '<th>ended</th><th>steps</th>' +
    '<th title="parallel-agent cap for this run (run.max_parallel)">cap</th>' +
    '<th class="hide-s">dir</th></tr>';
  // rows are newest-first; active runs and unlaunched requests always show,
  // finished history is capped at SHOW_N until "show more"/"show all"
  const allRuns = d.runs.filter(r=>!selectedRun||runKey(r)===selectedRun);
  const actRuns = allRuns.filter(r=>!r.ended_at);
  const finRuns = allRuns.filter(r=>r.ended_at);
  const finShow = Math.max(runsShown - actRuns.length, 0);
  const shownRuns = actRuns.concat(finRuns.slice(0, finShow));
  const hiddenRuns = finRuns.length - Math.min(finShow, finRuns.length);
  for (const r of shownRuns){
    const counts = Object.entries(r.counts||{})
      .map(([k,v])=>`<span style="color:${COLORS[k]||'#8b949e'}">${v} ${k}</span>`)
      .join('');
    h += `<tr class="runrow${runKey(r)===selectedRun?' selected':''}" ` +
      `data-run-key="${esc(runKey(r))}" tabindex="0" ` +
      `aria-label="Filter to run ${esc(r.run)}"><td class="wrap">` +
      `<b>${esc(r.run)}</b><br>` +
      (r.stage ? `<span class="stage ${esc(r.stage)}">` +
        `${esc(STAGES[r.stage]||r.stage)}` +
        (r.stage==='generating'
          ? `<span class="dots"><span>.</span><span>.</span><span>.</span></span>`
          : '') + `</span>` +
        (r.gen_last ? `<br><span class="sub">${esc(r.gen_last)}</span>` : '') :
        `<span class="sub">${esc((r.dir||'').split(/[\\/]/).pop())}</span>`) +
      (r.stale?` <span class="stage error">Needs review</span>`:'') +
      `<br><button type="button" class="linkbtn" ` +
      (r.stage ? `data-open-request="${esc(r.request_id)}">${r.stage==='draft'?'Edit':'Review'}` :
        `data-run-details="${esc(runKey(r))}">Details`) + `</button></td>` +
      `<td class="wrap">${r.description?esc(r.description):
        r.stage?'<span class="sub">Awaiting proposal</span>':'-'}</td>` +
      `<td>${(r.projects||[]).map(p=>`<span class="chip">${esc(p)}</span>`).join('')}</td>` +
      `<td>${loc(r.started_at)}</td>` +
      `<td>${r.ended_at?loc(r.ended_at):(r.started_at?'<b>running</b>':'-')}</td>` +
      `<td class="counts">${counts||'<span class="sub">not planned</span>'}` +
      (r.tokens ? `<br><span class="sub" title="${TOK_HELP}">tok ` +
        `${kfmt(r.tokens.prompt)}/${kfmt(r.tokens.completion)}/` +
        `${kfmt(r.tokens.cached)}</span>` : '') +
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
    const rows = groups[slug].filter(a=>!hidden.has(a.status) &&
      [...facets].every(f=>matchesFacet(a,f)));
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
      '<tr><th></th><th>step</th><th>task</th><th>run</th>' +
      '<th class="hide-s">model</th><th class="hide-s">effort</th>' +
      '<th class="hide-s">mode</th><th>started</th><th>elapsed</th>' +
      '<th class="hide-s" title="' + TOK_HELP + '">tokens</th>' +
      '<th>ended</th><th class="hide-s">session</th><th>result</th></tr>';
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
        `${a.status}</td>` +
        `<td class="step"><span ` +
        `style="white-space:nowrap">${esc(a.step)}</span>${a.wt
          ? ` <button type="button" class="chip" data-facet="wt" ` +
            `style="color:#58a6ff" title="${esc(a.branch||'worktree')}">wt</button>` +
            (a.merged===true
             ? ` <button type="button" class="chip" data-facet="merged" ` +
               `style="color:#3fb950">merged</button>`
             : a.merged===false
             ? ` <button type="button" class="chip" data-facet="unmerged" ` +
               `style="color:#f0883e">unmerged</button>`
             : '')
          : ''}</td><td class="wrap">${esc(a.task||'')}</td>` +
        `<td>${esc(a.run)}</td>` +
        `<td class="hide-s">${esc(a.model||'-')}</td>` +
        `<td class="hide-s">${esc(a.effort||'-')}</td>` +
        `<td class="hide-s">${esc(a.mode||'-')}${a.cloud?' cloud':''}</td>` +
        `<td>${loc(a.started_at)}</td>` +
        `<td data-el="${a._i}">${elapsed(a,now)}</td>` +
        `<td class="hide-s" title="${esc(tokTitle(a.tokens))}">` +
        `${fmtT(a.tokens)}</td>` +
        `<td>${loc(a.ended_at)}</td>` + sessTd +
        (a.status==='running'
          ? `<td class="res${fresh?' fresh':''}" ` +
            `title="${esc(a.tail_last||'')}">${esc(a.tail_last||'')}` +
            `<span class="dots"><span>.</span><span>.</span><span>.</span>` +
            `</span></td>`
          : `<td class="wrap">${esc(a.next||a.note||'')}</td>`) + `</tr>`;
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
  if (detailIdx != null && !document.getElementById('backdrop').hidden)
    showDetail(detailIdx);        // refresh open modal on real changes
  const modalRun = modalRequest?.id && d.runs.find(r=>r.request_id===modalRequest.id);
  const modalStage = modalRun && (modalRun.stage||'launched');
  if (modalStage && modalRequest.status!=='loading' && !modalRequest.dirty &&
      modalRequest.status!==modalStage) openRequest(modalRequest.id);
}
document.addEventListener('click', async e=>{
  const t = e.target;
  if (t.id==='dx' || t.id==='backdrop'){
    closeDetail(); return; }
  if (t.id==='new-request'){
    openNew(); return; }
  if (t.dataset && t.dataset.action){
    await requestAction(t.dataset.action); return; }
  if (t.dataset && t.dataset.openRequest){
    await openRequest(t.dataset.openRequest); return; }
  if (t.dataset && t.dataset.runDetails){
    showRunDetails(t.dataset.runDetails); return; }
  if (t.dataset && t.dataset.filterRun){
    selectedRun=t.dataset.filterRun;
    closeDetail(true); if (lastData) render(lastData); return; }
  if (t.dataset && t.dataset.clearRun!==undefined){
    selectedRun=null; if (lastData) render(lastData); return; }
  if (t.dataset && t.dataset.reset!==undefined){
    hidden.clear(); facets.clear(); if (lastData) render(lastData); return; }
  if (t.dataset && t.dataset.st){            // status filter chip
    hidden.has(t.dataset.st) ? hidden.delete(t.dataset.st)
                             : hidden.add(t.dataset.st);
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
  if (modalRequest && e.target.id?.startsWith('rq-')){
    modalRequest.dirty=true;
    const msg=document.getElementById('form-msg');
    if (msg) msg.hidden=true;
  }
});
document.addEventListener('keydown', e=>{
  if ((e.key==='Enter'||e.key===' ') &&
      e.target.matches && e.target.matches('tr.runrow')){
    e.preventDefault();
    selectedRun = selectedRun===e.target.dataset.runKey ? null : e.target.dataset.runKey;
    if (lastData) render(lastData);
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
  try { render(await (await fetch('/api/state')).json()); }
  catch(e){} finally { setTimeout(tick, 2000); } }
tick();
</script></body></html>"""


def make_dashboard_handler(runs_dir: Path, devin_argv: list[str],
                           requests_dir: Path | None = None,
                           manifests_dir: Path = MANIFESTS_DIR):
    requests_dir = requests_dir or runs_dir.parent / "requests"

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
                    self._json(collect_dashboard(runs_dir, devin_argv,
                                                 requests_dir))
                elif (m := re.fullmatch(r"/api/requests/([^/]+)", path)):
                    rec = request_details(m[1], requests_dir,
                                          manifests_dir, runs_dir)
                    if rec["status"] == "discarded":
                        raise RequestError("Request was discarded", 404)
                    self._json(rec)
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
                body = self._body()
                if path == "/api/requests":
                    self._json(create_request(body, requests_dir, runs_dir), 201)
                elif (m := re.fullmatch(r"/api/requests/([^/]+)/"
                                         r"(save|generate|run|discard)", path)):
                    request_id, action = m.groups()
                    if action == "save":
                        self._json(save_request(request_id, body, requests_dir,
                                                manifests_dir, runs_dir))
                    elif action == "generate":
                        self._json(start_generation(request_id, requests_dir,
                                                    manifests_dir, runs_dir,
                                                    devin_argv), 202)
                    elif action == "run":
                        self._json(launch_request(request_id, requests_dir,
                                                  manifests_dir, runs_dir,
                                                  devin_argv), 202)
                    else:
                        if body.get("confirm") is not True:
                            raise RequestError("Confirm discard explicitly")
                        self._json(discard_request(request_id, requests_dir,
                                                   manifests_dir, runs_dir))
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
