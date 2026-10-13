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
    python conductor.py demo                  end-to-end against a mock devin

Manifest: see examples/night.example.toml. Project registry: projects.toml.
"""
from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
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
import concurrent.futures
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
VALID_MODES = {"normal", "accept-edits", "smart", "bypass", "dangerous", "yolo",
               "auto"}

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


@dataclasses.dataclass
class Manifest:
    path: Path
    name: str
    on_failure: str                    # stop | continue
    checkpoint: bool
    preamble: str | None
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
            worktree=bool(merged.get("worktree", False))))
    if not steps:
        raise ManifestError("manifest has no [[step]] entries")
    return Manifest(path=path, name=name, on_failure=on_failure,
                    checkpoint=bool(run.get("checkpoint", True)),
                    preamble=preamble, steps=steps)


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
        cmd += ["--model", step.model]
    cmd += ["--permission-mode", step.mode,
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


def session_ids(devin_argv: list[str], cwd: Path) -> set[str]:
    try:
        out = subprocess.run(devin_argv + ["list", "--format", "json"],
                             cwd=str(cwd), capture_output=True, text=True,
                             encoding="utf-8", errors="replace", timeout=60)
        return {s["id"] for s in json.loads(out.stdout or "[]")}
    except Exception:
        return set()


def check_auth(devin_argv: list[str]) -> bool:
    try:
        out = subprocess.run(devin_argv + ["auth", "status"],
                             capture_output=True, text=True, timeout=60,
                             encoding="utf-8", errors="replace")
        return "not logged in" not in (out.stdout + out.stderr).lower()
    except Exception:
        return False


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


def worktree_merge(repo_dir: Path, wt_path: Path, branch: str) -> bool:
    """Merge the step's branch into the main checkout. On conflict, abort the
    merge and LEAVE the worktree+branch for a human - the work is not lost."""
    r = _git(repo_dir, "merge", "--no-edit", branch)
    if r.returncode != 0:
        _git(repo_dir, "merge", "--abort")
        return False
    _git(repo_dir, "worktree", "remove", "--force", str(wt_path))
    _git(repo_dir, "branch", "-D", branch)
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


def run_step(step: Step, slug: str, work_dir: Path, cfg: Manifest,
             run_dir: Path, devin_argv: list[str], state: dict,
             do_checkpoint: bool = True) -> str:
    """Run one step; return final status: done|partial|blocked|failed|timed_out.

    work_dir is where the session runs and where checkpoints/verify happen -
    the project dir, or a git worktree for parallel steps. When a worktree is
    used the caller owns the pre-step checkpoint (on the main checkout), so
    do_checkpoint=False.
    """
    with STATE_LOCK:
        rec = state["steps"].setdefault(step.name, {})
        rec.update(name=step.name, slug=slug, project=str(work_dir),
                   status="running", started_at=now_iso(),
                   model=step.model, mode=step.mode, cloud=step.cloud)
        save_state(run_dir, state)
    log_event(run_dir, event="step_start", step=step.name, project=str(work_dir))

    prompt_path = run_dir / "prompts" / f"{step.name}.md"
    prompt_path.parent.mkdir(exist_ok=True)
    prompt_path.write_text(
        render_prompt(step, slug, work_dir, cfg.path.parent, cfg.preamble),
        encoding="utf-8")
    stdout_log = run_dir / f"{step.name}.stdout.log"
    export_path = run_dir / f"{step.name}.export.json"

    if cfg.checkpoint and do_checkpoint:
        if not is_git_repo(work_dir):
            with STATE_LOCK:
                rec.update(status="failed",
                           note="not a git repo root; checkpoint required")
                save_state(run_dir, state)
            return "failed"
        git_commit(work_dir, f"conductor: checkpoint before {step.name}",
                   allow_empty=True)

    before = session_ids(devin_argv, work_dir)
    cmd = devin_cmd(devin_argv, step, prompt_path, export_path)
    LOG.info("step %s: %s (cwd=%s)", step.name, " ".join(cmd), work_dir)
    t0 = time.time()
    timed_out = False
    with open(stdout_log, "wb") as out:
        try:
            proc = subprocess.run(cmd, cwd=str(work_dir), stdout=out,
                                  stderr=subprocess.STDOUT,
                                  timeout=step.timeout_min * 60)
            rc = proc.returncode
        except subprocess.TimeoutExpired:
            rc, timed_out = None, True
        except OSError as exc:
            rc, timed_out = None, False
            LOG.error("step %s spawn failed: %s", step.name, exc)
            log_event(run_dir, event="spawn_error", step=step.name, error=str(exc))
    elapsed = round(time.time() - t0, 1)

    new_sessions = session_ids(devin_argv, work_dir) - before
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
    elif verify_ok is False and step.verify_must_pass:
        status = "failed"
    else:
        status = "done"

    with STATE_LOCK:
        rec["session_id"] = sorted(new_sessions)[-1] if new_sessions else None
        rec["exit_code"] = rc
        rec["elapsed_s"] = elapsed
        if status == "done" and status_line is None:
            rec["note"] = "worker did not emit a STATUS line; assuming done"
        rec.update(status=status, ended_at=now_iso(), status_line=status_line,
                   verify_ok=verify_ok)
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


def hub_report(cfg: Manifest, run_dir: Path, state: dict) -> None:
    """One row in hub.db per finished run, for morning review on the phone."""
    if not TASK_PY.is_file():
        return
    counts: dict[str, int] = {}
    for rec in state["steps"].values():
        counts[rec["status"]] = counts.get(rec["status"], 0) + 1
    body = (f"run dir: {run_dir}\n"
            + "\n".join(f"- {r['name']}: {r['status']}"
                        for r in state["steps"].values()))
    try:
        subprocess.run(
            [sys.executable, str(TASK_PY), "add", "--project", "agent-conductor",
             "--kind", "proposal",
             "--title", f"conductor run {cfg.name}: {dict(counts)}",
             "--body", body, "--by", "conductor"],
            capture_output=True, timeout=60)
    except Exception as exc:
        LOG.warning("hub.db report failed: %s", exc)


def cmd_run(args: argparse.Namespace) -> int:
    manifest_path = Path(args.manifest).resolve()
    cfg = load_manifest(manifest_path)
    projects = load_projects()
    devin_argv = shlex.split(args.devin or os.environ.get("CONDUCTOR_DEVIN")
                             or "devin")
    runs_dir = Path(args.runs_dir).resolve() if args.runs_dir else RUNS_DIR

    resolved = [(*resolve_project(s, projects, manifest_path.parent), s)
                for s in cfg.steps]

    if args.resume is not None:
        run_dir = find_resume_dir(cfg, runs_dir, args.resume or "latest")
        state = json.loads((run_dir / "state.json").read_text(encoding="utf-8"))
        print(f"[resume] {run_dir.name}")
    else:
        run_dir = new_run_dir(cfg, runs_dir)
        state = {"run": cfg.name, "manifest": str(manifest_path),
                 "started_at": now_iso(), "devin": " ".join(devin_argv),
                 "steps": {}}
        save_state(run_dir, state)
    log_event(run_dir, event="run_start", manifest=str(manifest_path))

    if "devin" in os.path.basename(devin_argv[0]).lower():
        if not check_auth(devin_argv):
            print("devin is not logged in. Run `devin auth login` first.")
            log_event(run_dir, event="run_abort", reason="auth")
            return 2

    groups = group_waves(resolved)
    validate_groups(groups)
    stop = False
    for members in groups:
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
                    git_commit(project_dir,
                               f"conductor: checkpoint before wave "
                               f"{step.wave}", allow_empty=True)
                    wave_ckpt_done = True
                branch = sanitize_branch(f"conductor/{cfg.name}/{step.name}")
                wt = DATA_DIR / "worktrees" / run_dir.name / step.name
                if not worktree_add(project_dir, wt, branch):
                    with STATE_LOCK:
                        state["steps"].setdefault(step.name, {}).update(
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

        if len(prepared) == 1:
            step, slug, work_dir, _repo, branch = prepared[0]
            statuses = {step.name: run_step(step, slug, work_dir, cfg,
                                            run_dir, devin_argv, state,
                                            do_checkpoint=(branch is None))}
        else:
            statuses = {}
            with concurrent.futures.ThreadPoolExecutor(
                    max_workers=len(prepared)) as ex:
                futs = {ex.submit(run_step, step, slug, work_dir, cfg,
                                  run_dir, devin_argv, state,
                                  do_checkpoint=(branch is None)): step.name
                        for step, slug, work_dir, _r, branch in prepared}
                for f in concurrent.futures.as_completed(futs):
                    statuses[futs[f]] = f.result()

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
        if cfg.on_failure == "stop" and any(
                s in ("failed", "timed_out", "blocked")
                for s in statuses.values()):
            print("stopping run after wave")
            break

    state["ended_at"] = now_iso()
    save_state(run_dir, state)
    log_event(run_dir, event="run_end")
    hub_report(cfg, run_dir, state)
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
          f"checkpoint={cfg.checkpoint}")
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
    print(f"run {state['run']}  started {state['started_at']}  ({run_dir})")
    for name, rec in state["steps"].items():
        sess = rec.get("session_id") or "-"
        print(f"  {rec['status']:>9}  {name:<20} {rec.get('elapsed_s', 0):>7.0f}s  "
              f"session={sess}  verify={rec.get('verify_ok')}")
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
                            devin=f"{py} {mock}", runs_dir=args.runs_dir)
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
            ("demo", cmd_demo, "self-test against a mock devin")):
        p = sub.add_parser(name, help=helptext)
        if name in ("plan", "run"):
            p.add_argument("manifest")
        if name == "run":
            p.add_argument("--resume", nargs="?", const="latest", default=None,
                           help="resume a run dir (default: latest for this manifest)")
        if name in ("plan", "run", "demo"):
            p.add_argument("--devin", default=None,
                           help="devin command override (e.g. mock shim). "
                                "Split with shlex - quote or use forward "
                                "slashes in Windows paths")
        if name in ("run", "demo"):
            p.add_argument("--runs-dir", default=None, help=argparse.SUPPRESS)
        if name == "status":
            p.add_argument("run_dir", nargs="?", default=None)
    args = ap.parse_args(argv)
    return {"plan": cmd_plan, "run": cmd_run, "status": cmd_status,
            "demo": cmd_demo}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
