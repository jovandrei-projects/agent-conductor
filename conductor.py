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
import shlex
import subprocess
import sys
import threading
import time
import tomllib
from logging.handlers import RotatingFileHandler
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
RUNS_DIR = DATA_DIR / "runs"
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
- End your final message with exactly these two lines:

STATUS: done | partial | blocked
NEXT: <one line - what the next session should do first>
"""


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


@dataclasses.dataclass
class Manifest:
    path: Path
    name: str
    on_failure: str                    # stop | continue
    checkpoint: bool
    preamble: str | None
    max_hours: float | None            # wall-clock cap for the whole run
    max_parallel: int | None           # cap on concurrent agents; None = auto
    steps: list[Step]


class ManifestError(ValueError):
    pass


def load_manifest(path: Path) -> Manifest:
    doc = tomllib.loads(path.read_text(encoding="utf-8"))
    run = doc.get("run", {})
    defaults = doc.get("defaults", {})
    name = run.get("name") or path.stem
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
            effort=merged.get("effort") or None))
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
                    max_parallel=max_parallel, steps=steps)


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


def session_ids(devin_argv: list[str], cwd: Path) -> set[str] | None:
    """Session ids `devin list` reports for cwd, or None when the listing
    itself failed (devin missing, bad dir) - callers must not treat a
    failed listing as 'all sessions gone'."""
    try:
        out = subprocess.run(devin_argv + ["list", "--format", "json"],
                             cwd=str(cwd), capture_output=True, text=True,
                             encoding="utf-8", errors="replace", timeout=60,
                             env=clean_devin_env())
        if out.returncode != 0:
            return None
        return {s["id"] for s in json.loads(out.stdout or "[]")}
    except Exception:
        return None


def check_auth(devin_argv: list[str]) -> bool:
    try:
        out = subprocess.run(devin_argv + ["auth", "status"],
                             capture_output=True, text=True, timeout=60,
                             encoding="utf-8", errors="replace",
                             env=clean_devin_env())
        return "not logged in" not in (out.stdout + out.stderr).lower()
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
                          encoding="utf-8", errors="replace", timeout=120)


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


def run_step(step: Step, slug: str, work_dir: Path, cfg: Manifest,
             run_dir: Path, devin_argv: list[str], state: dict,
             do_checkpoint: bool = True, branch: str | None = None) -> str:
    """Run one step; return final status: done|partial|blocked|failed|timed_out.

    work_dir is where the session runs and where checkpoints/verify happen -
    the project dir, or a git worktree for parallel steps. When a worktree is
    used the caller owns the pre-step checkpoint (on the main checkout), so
    do_checkpoint=False.

    The step's record in state.json doubles as the agent registry entry:
    status/started_at are set while it queues, pid once the child process
    exists, and ended_at/elapsed_s/session_id when it finishes. The dashboard
    reads exactly this.
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

    before = session_ids(devin_argv, work_dir) or set()
    cmd = devin_cmd(devin_argv, step, prompt_path, export_path)
    LOG.info("step %s: %s (cwd=%s)", step.name, " ".join(cmd), work_dir)
    t0 = time.time()
    timed_out = False
    rc: int | None = None
    with open(stdout_log, "wb") as out:
        try:
            proc = subprocess.Popen(cmd, cwd=str(work_dir), stdout=out,
                                    stderr=subprocess.STDOUT,
                                    stdin=subprocess.DEVNULL,
                                    env=clean_devin_env())
            # Record pid in memory only: writing state.json into run_dir at
            # this point lands inside the worker's watched workspace (for
            # self-hosted runs) and the CLI's user-edit awareness ends the
            # fresh session mid-init. The final save_state below persists it.
            with STATE_LOCK:
                rec["pid"] = proc.pid
            try:
                rc = proc.wait(timeout=step.timeout_min * 60)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
                timed_out = True
        except OSError as exc:
            LOG.error("step %s spawn failed: %s", step.name, exc)
            log_event(run_dir, event="spawn_error", step=step.name, error=str(exc))
    elapsed = round(time.time() - t0, 1)

    new_sessions = (session_ids(devin_argv, work_dir) or set()) - before
    status_line = tail_status(stdout_log)
    verify_ok = None
    if step.verify and not timed_out:
        v = subprocess.run(step.verify, cwd=str(work_dir), shell=True,
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=600)
        verify_ok = v.returncode == 0
        (run_dir / f"{step.name}.verify.log").write_text(
            (v.stdout or "") + (v.stderr or ""), encoding="utf-8",
            errors="replace")

    if cfg.checkpoint and is_git_repo(work_dir) and git_dirty(work_dir):
        git_commit(work_dir, f"conductor: work from {step.name}")

    if timed_out:
        status = "timed_out"
    elif rc is None or rc != 0:
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
        rec["exit_code"] = rc
        rec["elapsed_s"] = elapsed
        if status_line is None and rc == 0:
            rec["note"] = ("worker exited 0 without a STATUS line "
                           "(no-op run - nothing was done)")
        rec.update(status=status, ended_at=now_iso(), status_line=status_line,
                   verify_ok=verify_ok, next=tail_next(stdout_log))
        save_state(run_dir, state)
    log_event(run_dir, event="step_end", step=step.name, status=status,
              exit_code=rc, elapsed_s=elapsed, session=rec.get("session_id"))
    return status


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
                          encoding="utf-8", errors="replace")


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
        state = {"run": cfg.name, "manifest": str(manifest_path),
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
                # Branch carries the RUN DIR name, not the manifest name:
                # re-running the same manifest can never collide with a
                # stale branch still checked out in an old run's worktree.
                branch = sanitize_branch(
                    f"conductor/{run_dir.name}/{step.name}")
                wt = DATA_DIR / "worktrees" / run_dir.name / step.name
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
            + (f" [wave {s.wave}]" if s.wave is not None else "")
        print(f"  {s.name:<20} {slug:<22} model={s.model or '-':<10} "
              f"mode={s.mode:<8} timeout={s.timeout_min:g}m{tags}")
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
        return subprocess.run(devin_argv + ["rm", sid, "--force"],
                              cwd=str(cwd), capture_output=True, text=True,
                              encoding="utf-8", errors="replace",
                              timeout=60, env=clean_devin_env()).returncode
    except Exception:
        return 1


def collect_sessions(runs_dir: Path, devin_argv: list[str]) -> list[dict]:
    """Conductor-spawned devin sessions still registered under their work_dir.

    Whitelist on ids recorded in run state.json - interactive sessions are
    never in that registry, so human sessions can never appear here. Each
    row carries age_s for --keep-hours decisions.
    """
    spawned: dict[str, dict] = {}
    for sj in sorted(runs_dir.glob("*/state.json")):
        try:
            st = json.loads(sj.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for name, rec in (st.get("steps") or {}).items():
            sid = rec.get("session_id")
            if not sid or "/" in str(sid):
                continue            # cloud URL refs aren't local sessions
            spawned[str(sid)] = {
                "run": st.get("run"), "step": name,
                "status": rec.get("status"),
                "ended": rec.get("ended_at") or st.get("ended_at"),
                "run_ended": bool(st.get("ended_at")),
                "work_dir": rec.get("work_dir") or rec.get("project")}
    by_dir: dict[str, list[str]] = {}
    for sid, info in spawned.items():
        if info["work_dir"] and Path(info["work_dir"]).is_dir():
            by_dir.setdefault(info["work_dir"], []).append(sid)
    out: list[dict] = []
    for wd, sids in by_dir.items():
        live_ids = session_ids(devin_argv, Path(wd)) or set()
        for sid in sids:
            if sid in live_ids:
                info = dict(spawned[sid], sid=sid)
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


# ---------------------------------------------------------------- dashboard

SERVE_PORT = 8773    # registered in C:\Projects\AGENTS.md

_STATUS_COLOR = {"done": "#3fb950", "partial": "#d29922", "blocked": "#d29922",
                 "failed": "#f85149", "timed_out": "#f0883e",
                 "running": "#58a6ff", "queued": "#8b949e",
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
                             env=clean_devin_env())
        txt = (out.stdout + out.stderr).lower()
        label = ("NOT LOGGED IN" if out.returncode != 0
                 or "not logged in" in txt else "ok")
    except (OSError, subprocess.SubprocessError):
        label = "devin missing"
    _AUTH_CACHE[0], _AUTH_CACHE[1] = time.time(), label
    return label


def live_sessions_by_dir(runs_dir: Path,
                         devin_argv: list[str]) -> dict[str, set | None]:
    """work_dir -> session ids currently in `devin list` there, cached 2min.
    A dir maps to None when listing failed (devin down/missing) - never
    interpret None as 'all sessions gone'."""
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
            if wd and rec.get("session_id") and Path(wd).is_dir():
                dirs.add(wd)
    out = {d: session_ids(devin_argv, Path(d)) for d in dirs}
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


def collect_dashboard(runs_dir: Path,
                      devin_argv: list[str] | None = None) -> dict:
    """Flatten every run's state.json into the shape the UI renders: one
    agent row per step record, one run row per state file, newest first.
    state.json is the single source of truth - this adds no second registry."""
    sess_by_dir = live_sessions_by_dir(runs_dir, devin_argv or ["devin"])
    runs: list[dict] = []
    agents: list[dict] = []
    for sj in sorted(runs_dir.glob("*/state.json"), reverse=True):
        try:
            st = json.loads(sj.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        counts: dict[str, int] = {}
        eff = effective_statuses(st)
        for name, rec in st.get("steps", {}).items():
            status = eff.get(name, rec.get("status", "?"))
            counts[status] = counts.get(status, 0) + 1
            sid = rec.get("session_id")
            wd = rec.get("work_dir") or rec.get("project")
            live = sess_by_dir.get(wd)
            agents.append({
                "run": st.get("run"), "run_dir": st.get("run_dir"),
                "step": rec.get("name"), "slug": rec.get("slug"),
                "work_dir": rec.get("work_dir") or rec.get("project"),
                "status": status, "started_at": rec.get("started_at"),
                "ended_at": rec.get("ended_at"),
                "elapsed_s": rec.get("elapsed_s"),
                "alive": step_live(rec),
                "pid": rec.get("pid"), "session_id": rec.get("session_id"),
                "model": rec.get("model"), "mode": rec.get("mode"),
                "effort": rec.get("effort"), "task": task_label(rec),
                "cloud": rec.get("cloud"), "verify_ok": rec.get("verify_ok"),
                "note": rec.get("note"), "next": rec.get("next"),
                "session_gone": bool(sid and live is not None
                                     and str(sid) not in live),
                "exit_code": rec.get("exit_code"),
                "branch": rec.get("branch"), "merged": rec.get("merged"),
                "stdout_log": rec.get("stdout_log"),
                "tail": stdout_tail(rec.get("stdout_log")),
                "export": rec.get("export"), "prompt": rec.get("prompt")})
    # session_id is discovered post-spawn (diff of `devin list`); while a
    # step runs the cell is empty. Attribute it live instead: a running
    # step's work_dir should list exactly one session id not yet claimed
    # by any record.
    claimed = {a["session_id"] for a in agents if a.get("session_id")}
    for a in agents:
        if a["status"] == "running" and not a.get("session_id"):
            live = sess_by_dir.get(a.get("work_dir")) or set()
            cand = [s for s in live if str(s) not in claimed]
            if len(cand) == 1:
                a["session_id"], a["session_live"] = cand[0], True
        a["tail_last"] = next(
            (ln for ln in reversed((a.get("tail") or "").splitlines())
             if ln.strip()), None)
        runs.append({"run": st.get("run"), "dir": str(sj.parent),
                     "started_at": st.get("started_at"),
                     "ended_at": st.get("ended_at"), "counts": counts,
                     "max_parallel": st.get("max_parallel"),
                     "projects": sorted({r.get("slug") for r in
                                         st.get("steps", {}).values()
                                         if r.get("slug")})})
    agents.sort(key=lambda a: a.get("started_at") or "", reverse=True)
    return {"generated_at": now_iso(), "cpu_count": os.cpu_count(),
            "free_ram_gb": free_ram_gb(), "runs": runs, "agents": agents,
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
tr.agen { cursor:pointer; }
.dots span { animation:blink 1.2s infinite; color:#58a6ff; }
.dots span:nth-child(2) { animation-delay:.4s; }
.dots span:nth-child(3) { animation-delay:.8s; }
@keyframes blink { 0%,100%{opacity:.15} 50%{opacity:1} }
tr.agen td { transition:background .12s; }
tr.agen:hover td { background:#26405a; }
tr.hl td { background:#1c2833; }
tr.agen.hl:hover td { background:#2a4a68; }
.gone { opacity:.45; text-decoration:line-through; }
.chip { display:inline-block; border:1px solid #30363d; border-radius:10px; padding:1px 9px; margin:0 5px 5px 0; font-size:12px; cursor:pointer; user-select:none; }
.chip.off { opacity:.35; }
.dot { display:inline-block; width:9px; height:9px; border-radius:50%; margin-right:6px; }
.dot.run { animation:pulse 1.2s ease-in-out infinite; }
@keyframes pulse { 50% { opacity:.35; } }
.wrapw { overflow-x:auto; }
.counts span { background:#161b22; border-radius:8px; padding:1px 8px; margin-right:5px; font-size:12px; }
a { color:#58a6ff; text-decoration:none; }
#backdrop { position:fixed; inset:0; background:rgba(0,0,0,.55); z-index:8; }
#detail { position:fixed; left:50%; top:50%; transform:translate(-50%,-50%); width:min(660px,92vw); max-height:72vh; background:#161b22; border:1px solid #30363d; border-radius:10px; padding:18px 22px; overflow:auto; z-index:9; font-size:13px; }
#detail h3 { margin:0 0 10px; font-size:14px; }
#detail .kv { display:grid; grid-template-columns:96px 1fr; gap:4px 12px; }
#detail .k { color:#8b949e; }
#detail pre { white-space:pre-wrap; color:#8b949e; background:#0d1117; border-radius:6px; padding:8px 10px; font-size:12px; max-height:180px; overflow:auto; }
#detail .x { position:absolute; top:10px; right:16px; cursor:pointer; color:#8b949e; }
@media (max-width:640px) { .hide-s { display:none; } #detail { width:92vw; } }
</style></head><body>
<h1>agent-conductor</h1>
<div class="sub" id="meta">loading&hellip;</div>
<div class="sub" id="filters"></div>
<h2>runs</h2><div class="wrapw"><table id="runs"></table></div>
<h2>agents by project</h2><div id="agents"></div>
<div id="backdrop" hidden><div id="detail"><span class="x" id="dx">[close]</span><div id="dbody"></div></div></div>
<script>
const COLORS = {done:'#3fb950',partial:'#d29922',blocked:'#d29922',
failed:'#f85149',timed_out:'#f0883e',running:'#58a6ff',queued:'#8b949e',
lost:'#6e7681'};
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
function elapsed(a, now){
  if (a.elapsed_s != null) return fmtS(a.elapsed_s);
  if (a.started_at && (a.status==='running'||a.status==='queued'))
    return fmtS((now - new Date(a.started_at))/1000);
  return '-'; }
const expanded = {};      // slug -> true: show all rows, not just latest 10
const hidden = new Set(); // statuses filtered out via chips
const SHOW_N = 10;
const ORDER = ['running','queued','done','partial','blocked','timed_out',
               'failed','lost'];
let lastData = null;
let AGENTS = [];          // last payload, for the detail modal

// column layout shared by every per-project table (fixed layout -> the
// same widths line up across tables)
const AGENT_COLS = '<colgroup>' +
  '<col style="width:8%"><col style="width:8%"><col style="width:22%">' +
  '<col style="width:7%"><col class="hide-s" style="width:7%">' +
  '<col class="hide-s" style="width:5%"><col class="hide-s" style="width:6%">' +
  '<col style="width:6%"><col style="width:5%"><col style="width:6%">' +
  '<col class="hide-s" style="width:6%"><col style="width:14%">' +
  '</colgroup>';

let detailIdx = null;
function showDetail(i){
  const a = AGENTS[i]; if(!a) return;
  detailIdx = i;
  const kv = [['step',a.step],['run',a.run],['project',a.slug],
    ['status',a.status],['task',a.task],['result',a.next||a.note],
    ['model',(a.model||'-')+(a.effort?' (effort '+a.effort+')':'')],
    ['mode',(a.mode||'-')+(a.cloud?' cloud':'')],
    ['started',a.started_at],['ended',a.ended_at],
    ['elapsed',a.elapsed_s!=null?fmtS(a.elapsed_s):'-'],
    ['session',(a.session_id||'-')+(a.session_gone?' (deleted from sidebar)':'')],
    ['verify',a.verify_ok==null?'-':(a.verify_ok?'pass':'FAIL')],
    ['exit',a.exit_code],['work dir',a.work_dir],['branch',a.branch],
    ['prompt',a.prompt],['stdout',a.stdout_log],['export',a.export]];
  document.getElementById('dbody').innerHTML =
    `<h3>${esc(a.slug)} / ${esc(a.step)}</h3><div class="kv">` +
    kv.map(([k,v])=>`<div class="k">${k}</div><div class="wrap">${esc(v??'-')}</div>`).join('') +
    `</div>` +
    (a.tail ? `<h3 style="margin-top:14px">stdout tail</h3><pre id="dtail">${esc(a.tail)}</pre>` : '');
  document.getElementById('backdrop').hidden = false;
}

let lastSig = null;
function tickElapsed(now){
  for (const a of AGENTS){
    const td = document.querySelector(`td[data-el="${a._i}"]`);
    if (td) td.textContent = elapsed(a, now);
  }
}
function render(d){
  lastData = d; AGENTS = d.agents;
  AGENTS.forEach((a,i)=>a._i=i);   // stable cell ids before the sig check
  const now = Date.now();
  const live = d.agents.filter(a=>a.status==='running');
  document.getElementById('meta').innerHTML =
    `${d.agents.length} agents | ${live.length} running | ` +
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
  const sig = JSON.stringify([d.runs, d.agents.map(({tail,...r})=>r)]);
  const tailEl = document.getElementById('dtail');
  if (sig === lastSig){
    tickElapsed(now);
    if (tailEl && detailIdx!=null && AGENTS[detailIdx]
        && tailEl.textContent !== AGENTS[detailIdx].tail)
      tailEl.textContent = AGENTS[detailIdx].tail;   // live stdout stream
    return;
  }
  lastSig = sig;

  // status filter chips (click toggles)
  const byStatus = {};
  for (const a of d.agents) byStatus[a.status]=(byStatus[a.status]||0)+1;
  document.getElementById('filters').innerHTML = 'filter: ' +
    ORDER.filter(s=>byStatus[s]).map(s=>
      `<span class="chip${hidden.has(s)?' off':''}" data-st="${s}" ` +
      `style="color:${COLORS[s]}">${byStatus[s]} ${s}</span>`).join('') +
    (hidden.size?` <a href="#" data-st="__all">reset</a>`:'');

  // runs table first - a run can span projects (chips show which)
  let h = '<colgroup><col style="width:14%"><col style="width:16%">' +
    '<col style="width:9%"><col style="width:10%"><col style="width:20%">' +
    '<col style="width:6%"><col class="hide-s" style="width:25%"></colgroup>' +
    '<tr><th>run</th><th>projects</th><th>started</th><th>ended</th>' +
    '<th>steps</th><th>max//</th><th class="hide-s">dir</th></tr>';
  for (const r of d.runs){
    const counts = Object.entries(r.counts||{})
      .map(([k,v])=>`<span style="color:${COLORS[k]||'#8b949e'}">${v} ${k}</span>`)
      .join('');
    h += `<tr data-run="${esc(r.run)}"><td>${esc(r.run)}</td>` +
      `<td>${(r.projects||[]).map(p=>`<span class="chip">${esc(p)}</span>`).join('')}</td>` +
      `<td>${loc(r.started_at)}</td>` +
      `<td>${r.ended_at?loc(r.ended_at):'<b>running</b>'}</td>` +
      `<td class="counts">${counts}</td><td>${r.max_parallel||'-'}</td>` +
      `<td class="hide-s wrap">${esc(r.dir)}</td></tr>`;
  }
  document.getElementById('runs').innerHTML = h;

  // one fixed-width table per project slug, ordered by newest activity
  const groups = {};
  d.agents.forEach(a=>{ (groups[a.slug||'?'] ??= []).push(a); });
  const slugs = Object.keys(groups).sort((x,y)=>
    (groups[y][0].started_at||'') < (groups[x][0].started_at||'') ? -1 : 1);
  h = '';
  for (const slug of slugs){
    const rows = groups[slug].filter(a=>!hidden.has(a.status));
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
      '<th>ended</th><th class="hide-s">session</th><th>result</th></tr>';
    for (const a of show){
      const c = COLORS[a.status]||'#8b949e';
      const pulse = a.status==='running' ? ' run' : '';
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
      h += `<tr class="agen" data-i="${a._i}" data-run="${esc(a.run)}">` +
        `<td><span class="dot${pulse}" style="background:${c}" ` +
        `title="${a.status}${a.alive?' (pid '+a.pid+')':''}"></span>` +
        `${a.status}</td>` +
        `<td>${esc(a.step)}</td><td class="wrap">${esc(a.task||'')}</td>` +
        `<td>${esc(a.run)}</td>` +
        `<td class="hide-s">${esc(a.model||'-')}</td>` +
        `<td class="hide-s">${esc(a.effort||'-')}</td>` +
        `<td class="hide-s">${esc(a.mode||'-')}${a.cloud?' cloud':''}</td>` +
        `<td>${loc(a.started_at)}</td>` +
        `<td data-el="${a._i}">${elapsed(a,now)}</td>` +
        `<td>${loc(a.ended_at)}</td>` + sessTd +
        (a.status==='running'
          ? `<td title="${esc(a.tail_last||'')}">${esc(a.tail_last||'')}` +
            `<span class="dots"><span>.</span><span>.</span><span>.</span>` +
            `</span></td>`
          : `<td class="wrap">${esc(a.next||a.note||'')}</td>`) + `</tr>`;
    }
    if (rows.length > SHOW_N)
      h += `<tr><td colspan="12"><a href="#" data-slug="${esc(slug)}">` +
        (expanded[slug] ? `show latest ${SHOW_N} only` :
          `show ${rows.length - SHOW_N} older agent(s)`)+`</a></td></tr>`;
    h += '</table></div>';
  }
  document.getElementById('agents').innerHTML =
    h || '<span class="sub">no agents yet</span>';
  applyHl();
  if (detailIdx != null && !document.getElementById('backdrop').hidden)
    showDetail(detailIdx);        // refresh open modal on real changes
}
document.addEventListener('click', async e=>{
  const t = e.target;
  if (t.id==='dx' || t.id==='backdrop'){
    detailIdx = null;
    document.getElementById('backdrop').hidden = true; return; }
  if (t.dataset && t.dataset.st){            // status filter chip
    e.preventDefault();
    if (t.dataset.st==='__all') hidden.clear();
    else hidden.has(t.dataset.st) ? hidden.delete(t.dataset.st)
                                  : hidden.add(t.dataset.st);
    if (lastData) render(lastData); return;
  }
  if (t.dataset && t.dataset.slug){          // expand/collapse a table
    e.preventDefault();
    expanded[t.dataset.slug] = !expanded[t.dataset.slug];
    if (lastData) render(lastData); return;
  }
  if (t.dataset && t.dataset.auth){
    e.preventDefault();
    if (t.dataset.auth==='login'){
      await fetch('/api/auth/login', {method:'POST'});
      t.outerHTML = ' <span style="color:#8b949e">a terminal opened - ' +
        'sign in at the link, paste the code back; this clears itself</span>';
    } else {
      await fetch('/api/auth/recheck', {method:'POST'});
    }
    return;
  }
  const row = t.closest && t.closest('tr.agen');
  if (row) showDetail(+row.dataset.i);
});
document.addEventListener('keydown', e=>{
  if (e.key==='Escape'){
    detailIdx = null;
    document.getElementById('backdrop').hidden = true;
  }
});
// hovering a run row highlights its agent rows across all project tables.
// hoverRun persists across the 2s re-render so the highlight doesn't flicker.
let hoverRun = null;
function applyHl(){
  document.querySelectorAll('tr.hl').forEach(x=>x.classList.remove('hl'));
  if (hoverRun)
    document.querySelectorAll(
      `tr.agen[data-run="${CSS.escape(hoverRun)}"]`)
      .forEach(x=>x.classList.add('hl'));
}
document.addEventListener('mouseover', e=>{
  const tr = e.target.closest && e.target.closest('tr[data-run]');
  hoverRun = tr && tr.dataset.run || null;
  applyHl();
});
async function tick(){
  try { render(await (await fetch('/api/state')).json()); }
  catch(e){} finally { setTimeout(tick, 2000); } }
tick();
</script></body></html>"""


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

    class Handler(http.server.BaseHTTPRequestHandler):
        def _send(self, body: bytes, ctype: str) -> None:
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:                    # noqa: N802 - stdlib name
            if self.path == "/api/state":
                self._send(json.dumps(
                    collect_dashboard(runs_dir, devin_argv)).encode(),
                    "application/json")
            elif self.path in ("/", "/index.html"):
                self._send(_DASH_HTML.encode(), "text/html; charset=utf-8")
            else:
                self.send_error(404)

        def do_POST(self) -> None:                   # noqa: N802 - stdlib name
            if self.path == "/api/auth/login":
                # Login is an interactive console flow (trap 3) - the most a
                # dashboard button can do is pop a REAL terminal for it.
                # logout first: a stale credentials.toml makes plain `login`
                # short-circuit with "already logged in" (trap 4).
                subprocess.Popen(
                    'start "" cmd /k "devin auth logout & '
                    'devin auth login --force-manual-token-flow"',
                    shell=True, env=clean_devin_env())
                _AUTH_CACHE[0] = 0.0      # next tick re-probes
                self._send(b"{}", "application/json")
            elif self.path == "/api/auth/recheck":
                _AUTH_CACHE[0] = 0.0
                self._send(json.dumps(
                    {"auth": auth_status(devin_argv)}).encode(),
                    "application/json")
            else:
                self.send_error(404)

        def log_message(self, fmt: str, *a) -> None:
            LOG.debug("http %s", fmt % a)

    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
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
        subprocess.run(["git", "init"], cwd=str(proj), capture_output=True)
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
            stream.reconfigure(errors="backslashreplace")
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
