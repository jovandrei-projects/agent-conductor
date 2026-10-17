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
    python conductor.py merge  <run_dir>      land a run's pending branches
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
import socket
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
OUTCOME_RE = re.compile(r"^OUTCOME:\s*(.+)", re.M)
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
# After the verify gate's cap kills its process tree, the pipes should
# EOF at once; a stray grandchild holding a handle gets this grace
# before the gate abandons the capture and records the timeout anyway.
VERIFY_KILL_GRACE_S = 10.0
# A recorded session_id can briefly vanish from `devin list` right after
# spawn (registration lags seconds-to-a-minute, and the dashboard's own
# listing cache is 120s stale) - don't read that as 'deleted' for the
# first few minutes of a step.
SESSION_GONE_GRACE_S = 300.0

# Android device ownership (see the 'devices' section): a `device`-declaring
# step leases ONE adb serial machine-wide for its whole life - attempts,
# interrupt respawns and the verify gate all see the same device, and a
# busy device queues the step instead of letting two workers race it.
DEVICE_POLL_S = 10.0        # cadence of the device-wait loop
DEVICE_WAIT_SAVE_S = 30.0   # state.json heartbeat cadence while queued
DEVICE_BOOT_S = 180.0       # a booted emulator gets this long to finish
DEVICE_EMU_PORTS = range(5554, 5586, 2)   # adb emulator console ports (even)
DEVICE_SERIAL_RE = re.compile(r"[A-Za-z0-9_.:-]+")
DEVICE_WAIT_S = 600.0       # default give-up for a device that never appears

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

# Deleting a run record or discarding a proposal is RECOVERABLE: the bundle
# moves to data/trash/<entry>/ (meta.json + payload/) instead of rmtree and
# is hidden from normal listings for TRASH_RETAIN_S - the dashboard's Undo
# action or Trash view restores it. A bounded sweep purges only expired
# entries; 'Empty trash' is the one irreversible op and is confirmed twice.
TRASH_DIR = DATA_DIR / "trash"
try:
    TRASH_RETAIN_S = float(
        os.environ.get("CONDUCTOR_TRASH_HOURS") or 48) * 3600
except ValueError:
    TRASH_RETAIN_S = 48 * 3600
TRASH_SWEEP_MAX = 64        # entries purged per sweep pass - bounded work
TRASH_ID_RE = re.compile(r"^\d{8}-\d{6}-[0-9a-f]{6}$")

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
{service_note}
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
- End your final message with exactly these three lines, each at the
  start of its own line, as the LAST thing you print. They are read by a
  machine parsing your stdout - writing STATUS inside STATE.md, a commit
  message, or a Markdown bullet does NOT count and marks the step failed:

STATUS: done | partial | blocked
NEXT: <one line - what the next session should do first>
OUTCOME: <one or two sentences for the human reviewing this run - what
THIS step actually accomplished and its material limits (partial work, a
red verify gate, tests still pending). Not next-step instructions, not
sibling-task progress, and never a success you did not verify.>
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

# Prepended to the task body when the dashboard's 'Interrupt + instruct'
# respawns a step: the operator asked for a stop, the request folded the
# new instructions into the notes channel below, and this fresh session
# continues the same step under both the original task and the add-on.
INTERRUPT_NOTE = """\
NOTE FROM THE CONDUCTOR: the operator interrupted this step and resumed \
it with additional instructions - they are appended under 'NOTE FROM \
THE OPERATOR' below. The previous session's work is already committed \
in this repo: inspect the recent git log and .agent_tasks state, then \
CONTINUE from where it stopped instead of redoing finished work.
"""

# Prepended to the task body when the step owns an Android device lease -
# the worker is told which serial it has and the lifecycle rules (the
# conductor, not the worker, owns boot/kill; other attached devices are
# someone else's).
DEVICE_NOTE = """\
NOTE FROM THE CONDUCTOR: this step owns Android device `{serial}` for its \
whole run - a machine-wide lease no other agent can take. `ANDROID_SERIAL` \
(and `CONDUCTOR_DEVICE_SERIAL`) is already set in your environment, so plain \
`adb` and tools that honor it target this device automatically; prefer \
explicit `adb -s {serial}` anyway for clarity. Other attached devices or \
emulators belong to other workers or to the user - do NOT send them \
commands, and never reboot, `emu kill`, taskkill or otherwise stop ANY \
emulator or device, not even this one: the conductor owns the lifecycle \
and tears down what it booted. If the device becomes unusable mid-step, \
finish with STATUS: blocked and say so - do not requisition another.

If your work intentionally restarts or reloads a WebView app, its \
unsaved-changes guard may surface a NATIVE 'Confirm Navigation' dialog \
(Leave this Page / Stay on this Page) that CDP/JS cannot answer - the \
navigation never completes and every automation call behind it waits \
forever (this has already burned a real step). Do not leave it hanging \
and never accept it blindly: use the conductor helper `python {helper}` \
instead - its `relaunch <pkg>` does a process restart the \
dialog cannot block (it settles first so completed saves flush; in-memory \
unsaved state is lost, so only when that state is disposable), and \
`confirm-navigation --pkg <pkg> --leave` answers the known \
dialog after an in-page reload - `--leave` asserts the dirty state is \
your own disposable test data or already saved. With no decision flag it \
refuses rather than guess, and unexpected dialogs fail with a captured \
uiautomator/screenshot diagnosis - nothing is ever auto-accepted. Every \
call is scoped to `{serial}` and bounded; a bare `location.reload()` on \
a possibly-dirty page without this helper is the trap.
"""

# Rendered into the prompt's service-note slot when this step runs
# alongside other agents - sibling pool members of a parallel run or wave,
# a merge resolver working next to live steps, or just other runs holding
# machine slots. The rules are the same either way: own your checkout,
# own your leases, never fight a peer over a shared resource.
PARALLEL_NOTE = """\
NOTE FROM THE CONDUCTOR: other agent sessions are running in parallel on \
this machine right now - sibling steps of this run in their own \
worktrees, merge resolvers, or entirely separate runs. Share the box: \
work only inside your own checkout, bind listeners only to your leased \
port, and use only your leased Android device. For any other shared \
resource - fixed ports (adb forward, CDP), temp files, container or \
service names, external accounts - pick unique names or ephemeral free \
ports instead of assuming ownership, and never kill, stop or reconfigure \
a process, server, emulator or session you did not spawn: it belongs to \
a peer or to the user. If the work genuinely cannot proceed without an \
exclusive resource that is held, end with STATUS: blocked saying which \
one - a later resume retries when the pool frees up.
"""

# Closing review step appended when a request's follow-up checkbox is set -
# the planner is told to write it, and _publish_proposal appends this block
# itself when the planner forgot, so the checkbox is a guarantee.
FOLLOWUP_STEP = '''

[[step]]
name = "follow-up-plan"
project = "{slug}"
timeout_min = 30
prompt = 'This run just ended. Review what it accomplished against the request it ran from: per-step logs, STATUS/NEXT/OUTCOME lines and notes are under {runs} (the newest dir ending in -{run}), and progress files live under this project .agent_tasks/. Then write a NEW conductor TOML manifest proposing ONLY the still-unfinished or newly discovered work, saved as {manifests}/{run}-followup.toml - if nothing remains, write the file anyway but with no [[step]] entries (a run table + comments saying so), which registers as "no work remains" rather than a runnable job. It must pass proposal validation: [run].name a short safe slug, [run].description one sentence of AT MOST 300 characters (overlong descriptions fail validation and the proposal lands broken), checkpoint = true, every [[step]].project a registered projects.toml slug. The file is imported into the proposals list automatically - write it exactly once, do not implement or launch it, and do not schedule anything: only a human can start it.'
'''

RESOLVE_BODY = """\
<!-- conductor-purpose: resolve-merge -->
A git merge is in progress in this repo and needs a human-style
resolution. Conductor tried to merge branch `{branch}` - a parallel
agent's work, still checked out at {worktree} - back into {where}, and
it conflicts.

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

# Task bodies conductor writes itself (currently only RESOLVE_BODY) open
# with a machine-readable HTML comment declaring the step's internal
# role: `<!-- conductor-purpose: resolve-merge -->`. The marker ships IN
# the dispatched prompt - prompt-file consumers (mock_devin's
# resolve-merge trigger among them) rely on it - but it is metadata, not
# task prose. Human-facing surfaces (the stored 160-char `task`, the
# modal's task prompt, the /summary report) therefore translate it into
# a label via the helpers below instead of rendering a raw comment.
# The marker is honored only at the HEAD of a body: the same text
# appearing mid-task is a mention - a task can legitimately discuss the
# marker or carry HTML comments inside code examples - never metadata
# to strip.
PURPOSE_RE = re.compile(
    r"^\s*<!--\s*conductor-purpose\s*:\s*([\w.-]+)\s*-->\s*")

# Human phrasing for known purpose tokens; an unknown token displays
# as itself.
PURPOSE_LABELS = {"resolve-merge": "merge conflict resolution"}


def split_purpose(text: str | None) -> tuple[str | None, str]:
    """Split leading `<!-- conductor-purpose: X -->` markers off a task
    body -> (first purpose token or None, remaining text)."""
    rest = text or ""
    purpose = None
    while True:
        m = PURPOSE_RE.match(rest)
        if not m:
            return purpose, rest
        purpose = purpose or m.group(1)
        rest = rest[m.end():]


def purpose_label(purpose: str | None) -> str:
    """Human-facing label for a conductor-purpose token."""
    return PURPOSE_LABELS.get(purpose, purpose or "")


def task_display(text: str | None) -> str:
    """Human-facing rendering of a task body/summary: a leading
    conductor-purpose marker is metadata, so the remaining body shows
    instead - or, when the body is marker-only, a label naming the
    internal task. Non-marker comments and mid-text mentions pass
    through untouched."""
    purpose, body = split_purpose(text)
    body = body.strip()
    if body:
        return body
    return f"conductor task: {purpose_label(purpose)}" if purpose else ""


def task_summary(body: str | None, limit: int = 160) -> str:
    """The `task` string stored on a record and shown in lists:
    whitespace squashed + truncated, with a leading conductor-purpose
    marker translated - so a resolver row reads as its real
    instructions, not `<!-- ...`."""
    purpose, disp = split_purpose(body)
    one = " ".join(disp.split())[:limit]
    if one or not purpose:
        return one
    return f"conductor task: {purpose_label(purpose)}"


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
    verify_timeout_min: float = 10     # the verify gate's wall-clock cap -
                                       # TimeoutExpired counts as a failed
                                       # verify like any other red run
    cloud: bool = False
    wave: int | None = None            # adjacent steps sharing a wave run in parallel
    worktree: bool = False             # run in a temp git worktree + branch, merged after
    merge: str = "auto"                # auto = merge after step (legacy);
                                       # deferred = retain branch+worktree as a
                                       # pending merge until merge_run is asked
                                       # (implies worktree)
    effort: str | None = None          # thinking-effort variant suffix fused onto
                                       # the model id ("min" -> "model-min")
    merge_resolver: bool = False       # on merge conflict, spawn a resolver
                                       # session in the main checkout to
                                       # finish the merge instead of only
                                       # parking the worktree for a human
    merge_target: str | None = None    # local branch the worktree branch is
                                       # created from and merged back into;
                                       # None = the checkout's current branch
    max_attempts: int = 1              # a step hitting timeout_min is committed
                                       # and retried in a fresh session until
                                       # this many total attempts ran
    stall_min: float = 20              # past timeout_min a still-talking log
                                       # auto-extends; this much stdout silence
                                       # (or the 4x ceiling) ends it. 0 = hard
                                       # deadline, no extension
    device: str | None = None          # adb serial this step must own for its
                                       # whole run: "any" | "emulator" |
                                       # "physical" | an exact serial.
                                       # None = no device coordination.
    device_wait_s: float = DEVICE_WAIT_S  # give up -> 'blocked' when NO
                                       # matching device appears within this
                                       # long; a merely-busy device queues
                                       # uncapped. 0 = wait forever too.
    device_boot: bool | None = None    # None: boot an AVD only for device=
                                       # "emulator"; True: "any" may boot too;
                                       # False: never boot, attached only.
    depends_on: tuple[str, ...] = ()   # step names that must reach a terminal
                                       # state first - 'failed'/'skipped' count
                                       # (merge glitches never strand later
                                       # work); running/queued/waiting hold it


@dataclasses.dataclass
class Manifest:
    path: Path
    name: str
    on_failure: str                    # stop | continue
    checkpoint: bool
    preamble: str | None
    max_hours: float | None            # wall-clock cap for the whole run
    max_parallel: int | None           # cap on concurrent agents; None = auto
    parallel: bool                     # every step is pool-scheduled at once
                                       # (one shared wave); a trailing
                                       # follow-up-plan step still runs last
    merge_resolver: bool               # run-wide default for step.merge_resolver
    merge_policy: str                  # run-wide default for step.merge
    merge_target: str | None           # run-wide default for step.merge_target
    steps: list[Step]
    description: str | None = None
    summarize: str | None = None       # end-of-run LLM summary session:
                                       # None = off, "" = on with the CLI
                                       # default model, "<model>" pins one


class ManifestError(ValueError):
    pass


def _norm_device(val, what: str) -> str | None:
    """Normalize a `device` manifest/project value: 'any'|'emulator'|
    'physical'|an exact adb serial, or None when unset/off. Serials are
    case-sensitive (handsets are e.g. RFGL11NN0QV), only the class
    keywords are case-insensitive."""
    if val is None or val is False:
        return None
    if val is True:
        return "any"
    s = str(val).strip()
    if not s or s.lower() == "none":
        return None
    if s.lower() in ("any", "emulator", "physical"):
        return s.lower()
    if DEVICE_SERIAL_RE.fullmatch(s):
        return s
    raise ManifestError(
        f"{what}: bad device {val!r} - expected 'any' | 'emulator' | "
        f"'physical' | an adb serial like emulator-5554")


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
    run_merge = str(run.get("merge", "auto"))
    if run_merge not in ("auto", "deferred"):
        raise ManifestError(
            f"run.merge must be auto|deferred, got {run_merge!r}")
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
        merge_pol = str(merged.get("merge") or run.get("merge", "auto"))
        if merge_pol not in ("auto", "deferred"):
            raise ManifestError(
                f"step {merged['name']!r}: merge must be auto|deferred, got "
                f"{merge_pol!r}")
        if merge_pol == "deferred":
            if merged.get("worktree") is False:
                raise ManifestError(
                    f"step {merged['name']!r}: merge=deferred needs worktree "
                    f"isolation (it implies worktree=true - drop the "
                    f"explicit worktree=false)")
            if merged.get("cloud"):
                raise ManifestError(
                    f"step {merged['name']!r}: merge=deferred retains a local "
                    f"branch - it makes no sense with cloud=true")
            merged["worktree"] = True
        # merge_target: an existing local branch the step's worktree is
        # created from and merged back into. A step value (or an inherited
        # [defaults] one) wins; "" unsets. On a step without worktree that
        # explicit choice is a manifest error - a merge target on a step
        # that never merges must not be silently ignored. [run].merge_target
        # is different: it is only a DEFAULT for worktree steps, so a
        # run-wide choice (the request form's field) never breaks a plan
        # that keeps some steps on the main checkout.
        mt = merged.get("merge_target")
        explicit_mt = mt is not None
        if mt is None:
            mt = run.get("merge_target")
        if mt is not None:
            if not isinstance(mt, str):
                raise ManifestError(
                    f"step {merged['name']!r}: merge_target must be a "
                    f"branch name string")
            mt = mt.strip() or None
        if mt and not merged.get("worktree"):
            if explicit_mt:
                raise ManifestError(
                    f"step {merged['name']!r}: merge_target requires "
                    f"worktree = true (non-worktree steps never merge)")
            mt = None
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
        try:
            vt = float(merged.get("verify_timeout_min", 10))
        except (TypeError, ValueError):
            vt = 0
        if vt <= 0:
            raise ManifestError(
                f"step {merged['name']!r}: verify_timeout_min must be > 0")
        deps = merged.get("depends_on", ())
        if isinstance(deps, str):
            deps = (deps,)
        elif isinstance(deps, list):
            deps = tuple(deps)
        if not all(isinstance(d, str) and d for d in deps):
            raise ManifestError(
                f"step {merged['name']!r}: depends_on must name steps "
                "(a string or list of strings)")
        # Device ownership: step > [defaults] > the project's registry entry
        # (a mobile slug may declare `device = "emulator"` once, like
        # `verify`). A cloud step can't see local adb devices - inheriting
        # is skipped for it, but an EXPLICIT device + cloud is a manifest
        # bug and fails loudly.
        device = _norm_device(merged.get("device"),
                              f"step {merged['name']!r}")
        pent: dict = {}
        if (device is None or "device_wait_s" not in merged
                or "device_boot" not in merged):
            if registry is None:
                try:
                    registry = load_projects()
                except Exception:
                    registry = {}
            pent = registry.get(merged["project"]) or {}
            if device is None and not merged.get("cloud"):
                device = _norm_device(
                    pent.get("device"), f"project {merged['project']!r}")
        if device and merged.get("cloud"):
            raise ManifestError(
                f"step {merged['name']!r}: device= targets a local adb "
                f"serial - it makes no sense with cloud=true (a cloud "
                f"worker cannot see this machine's devices)")
        try:
            raw_dws = (merged.get("device_wait_s")
                       if "device_wait_s" in merged
                       else pent.get("device_wait_s"))
            device_wait_s = DEVICE_WAIT_S if raw_dws is None \
                else float(raw_dws)
        except (TypeError, ValueError):
            raise ManifestError(
                f"step {merged['name']!r}: bad device_wait_s")
        if device_wait_s < 0:
            raise ManifestError(
                f"step {merged['name']!r}: device_wait_s must be >= 0")
        dboot = merged.get("device_boot", pent.get("device_boot"))
        device_boot = None if dboot is None else bool(dboot)
        steps.append(Step(
            name=merged["name"], project=merged["project"],
            prompt=merged.get("prompt"),
            prompt_file=merged.get("prompt_file"),
            model=merged.get("model"), mode=mode,
            timeout_min=float(merged.get("timeout_min", 60)),
            verify=verify,
            verify_must_pass=bool(merged.get("verify_must_pass", True)),
            verify_timeout_min=vt,
            cloud=bool(merged.get("cloud", False)),
            wave=merged.get("wave"),
            worktree=bool(merged.get("worktree", False)),
            merge=merge_pol,
            effort=merged.get("effort") or None,
            merge_resolver=bool(mr), merge_target=mt, max_attempts=ma,
            stall_min=float(merged.get("stall_min", 20)),
            device=device, device_wait_s=device_wait_s,
            device_boot=device_boot, depends_on=deps))
    if not steps:
        raise ManifestError("manifest has no [[step]] entries")
    max_parallel = run.get("max_parallel")
    if max_parallel is not None:
        max_parallel = int(max_parallel)
        if max_parallel < 1:
            raise ManifestError("run.max_parallel must be >= 1")
    parallel = bool(run.get("parallel", False))
    if parallel and any(s.wave is not None for s in steps):
        raise ManifestError(
            "run.parallel already makes every step concurrent - the "
            "wave= grouping keys are redundant (drop them or drop "
            "parallel)")
    sz = run.get("summarize")
    if sz is True:
        summarize = ""                  # on, CLI default model
    elif sz in (None, False):
        summarize = None
    elif isinstance(sz, str) and sz.strip():
        summarize = sz.strip()
    else:
        raise ManifestError(
            'run.summarize must be true, false or a "<model>" string')
    run_mt = run.get("merge_target")
    if run_mt is not None:
        if not isinstance(run_mt, str):
            raise ManifestError("run.merge_target must be a branch name string")
        run_mt = run_mt.strip() or None
    if "depends_on" in defaults:
        raise ManifestError(
            "depends_on belongs on [[step]] - a [defaults] value would "
            "give every step the same dependency")
    # Dependency graph: names must resolve, no self-deps, no cycles, and
    # every dep must be dispatchable no later than its dependent - an
    # earlier-listed step, a same-wave sibling, or anything under
    # parallel = true (the shared pool is eligibility-scheduled). A dep
    # on a later group could never satisfy: groups dispatch in order.
    byname = {s.name: s for s in steps}
    order = {s.name: i for i, s in enumerate(steps)}
    for s in steps:
        for dep in s.depends_on:
            if dep == s.name:
                raise ManifestError(
                    f"step {s.name!r}: cannot depend on itself")
            d = byname.get(dep)
            if d is None:
                raise ManifestError(
                    f"step {s.name!r}: unknown depends_on {dep!r}")
            if order[dep] > order[s.name] and not (
                    parallel or (s.wave is not None and d.wave == s.wave)):
                raise ManifestError(
                    f"step {s.name!r}: depends_on {dep!r} dispatches later "
                    "- reorder the manifest or give both steps the same wave")
    color: dict[str, int] = {s.name: 0 for s in steps}   # 0/1/2 dfs marks
    def _dep_dfs(n: str) -> None:
        color[n] = 1
        for dep in byname[n].depends_on:
            if color[dep] == 1:
                raise ManifestError(f"depends_on cycle through {dep!r}")
            if color[dep] == 0:
                _dep_dfs(dep)
        color[n] = 2
    for s in steps:
        if color[s.name] == 0:
            _dep_dfs(s.name)
    return Manifest(path=path, name=name, on_failure=on_failure,
                    checkpoint=bool(run.get("checkpoint", True)),
                    preamble=preamble, max_hours=run.get("max_hours"),
                    max_parallel=max_parallel, parallel=parallel,
                    merge_resolver=bool(run.get("merge_resolver", False)),
                    merge_policy=run_merge, merge_target=run_mt,
                    steps=steps, description=description,
                    summarize=summarize)


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
                           "verify": ent.get("verify"),
                           "device": ent.get("device"),
                           "device_wait_s": ent.get("device_wait_s"),
                           "device_boot": ent.get("device_boot")})
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


def plan_groups(resolved: list[tuple[Path, str, Step]],
                cfg: "Manifest") -> list[list]:
    """Execution groups, in order. Under `[run] parallel = true` every
    step joins one shared pool scheduled as slots free (explicit wave=
    keys are rejected at manifest load - they would be redundant), except
    a 'follow-up-plan' step: it reviews what the run produced, so it
    always waits in its own last group for the pool to drain. Otherwise
    adjacent same-wave steps group and everything else is sequential."""
    if not cfg.parallel:
        return group_waves(resolved)
    tail = [it for it in resolved if it[2].name == "follow-up-plan"]
    pool = [it for it in resolved if it[2].name != "follow-up-plan"]
    return [g for g in (pool, tail) if g]


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


DEP_WAIT_STATUSES = frozenset(
    {"running", "queued", "waiting", "planned", "pending"})


def deps_unmet(step: "Step", state: dict) -> list[str]:
    """depends_on names still non-terminal: dispatched/queued/waiting or
    never attempted (no record). A dep satisfies on ANY terminal record -
    'failed', 'stopped' and 'skipped' count: merge-park glitches and red
    outcomes must not strand later work - the dependent agent can recover
    or fetch the parked branch's files itself."""
    recs = state.get("steps") or {}
    return [dep for dep in step.depends_on
            if (recs.get(dep) or {}).get("status") in
            (None, *DEP_WAIT_STATUSES)]


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
                  manifest_dir: Path, preamble: str | None,
                  service_note: str = "") -> str:
    if step.prompt_file:
        body = (manifest_dir / step.prompt_file).read_text(encoding="utf-8")
    else:
        body = step.prompt
    return (preamble or PREAMBLE).format(body=body, project=slug,
                                       project_dir=project_dir,
                                       service_note=service_note)


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
    # A resolve-<step> record exists only to finish its parent's merge -
    # once that branch has landed (merged=True) a failed/lost resolver is
    # bookkeeping, not work still owed: it displays 'superseded' instead
    # of failing forever (and no longer keeps the Resume button alive).
    for n in out:
        if (out[n] not in ("done", "superseded") and
                n.startswith("resolve-") and
                (steps.get(n[len("resolve-"):]) or {}).get("merged")
                is True):
            out[n] = "superseded"
    return out


def run_executing(st: dict) -> str | None:
    """Why this run still counts as executing, else None.

    The driver pid is authoritative for runs this code drives: a live
    conductor means the run is mid-flight even at instants when no step
    record reads 'running' - between waves, queued-but-unspawned under
    the cap, the post-exit verify/merge window, or a stop/pause shutdown
    still in progress (a raised halt sentinel is a REQUEST; the run has
    not stopped until the driver is gone). Run records that predate
    driver_pid fall back to live step rows - the pre-2026-10-15 shape.
    """
    if driver_alive(st):
        return "its conductor process is still running"
    live = sorted(n for n, r in (st.get("steps") or {}).items()
                  if step_live(r))
    if live:
        return f"agent(s) still live: {', '.join(live[:3])}"
    return None


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


def current_branch(repo_dir: Path) -> str | None:
    """The branch the checkout has checked out, or None on a detached HEAD."""
    r = _git(repo_dir, "symbolic-ref", "--short", "-q", "HEAD")
    return r.stdout.strip() or None if r.returncode == 0 else None


def branch_name_ok(name: str) -> bool:
    """True when `name` is a syntactically valid local branch name, checked
    through git itself (check-ref-format) rather than a hand-rolled regex.
    A leading dash is refused anyway - it would parse as an option at every
    downstream call site."""
    if not name or len(name) > 200 or name.startswith("-") \
            or name != name.strip():
        return False
    r = subprocess.run(["git", "check-ref-format", "--allow-onelevel", name],
                       capture_output=True, text=True, timeout=15,
                       **hidden_subprocess_kwargs())
    return r.returncode == 0


def local_branch_exists(repo_dir: Path, name: str) -> bool:
    """refs/heads/<name> exists - a LOCAL branch only. Tags, remote refs and
    abbreviations can never satisfy a merge target."""
    return _git(repo_dir, "rev-parse", "-q", "--verify",
                f"refs/heads/{name}").returncode == 0


def list_local_branches(repo_dir: Path) -> list[str]:
    """Local branch names for the dashboard selector. conductor/* run
    branches are per-run work product, never a sane merge target."""
    r = _git(repo_dir, "for-each-ref", "--format=%(refname:short)",
             "refs/heads")
    if r.returncode != 0:
        return []
    return [b for b in r.stdout.split() if not b.startswith("conductor/")]


def worktree_list(repo_dir: Path) -> list[tuple[Path, str | None]]:
    """(worktree dir, checked-out branch|None) for every registered worktree
    of the repo, main checkout first (`git worktree list --porcelain`)."""
    r = _git(repo_dir, "worktree", "list", "--porcelain")
    if r.returncode != 0:
        return [(repo_dir, current_branch(repo_dir))]
    out: list[tuple[Path, str | None]] = []
    wt: Path | None = None
    branch: str | None = None
    for ln in r.stdout.splitlines() + [""]:
        if ln.startswith("worktree "):
            if wt is not None:
                out.append((wt, branch))
            wt, branch = Path(ln[9:]), None
        elif ln.startswith("branch "):
            ref = ln[7:]
            branch = (ref[len("refs/heads/"):]
                      if ref.startswith("refs/heads/") else ref)
    if wt is not None:                        # covers a detached tail entry
        out.append((wt, branch))
    return out


def merge_target_error(label: str, project_dir: Path,
                       target: str) -> str | None:
    """The actionable rejection for a bad merge_target, or None when the
    target is usable: name must be a valid branch name AND already exist as
    a local branch in the project repo. `label` is the offending entity in
    the message (a step or project name). Never silently substitutes
    another branch and never creates one."""
    if not branch_name_ok(target):
        return (f"{label}: merge_target {target!r} is not a "
                f"valid branch name")
    if not is_git_repo(project_dir):
        return (f"{label}: merge_target {target!r} cannot be "
                f"checked - {project_dir} is not a git repository root")
    if not local_branch_exists(project_dir, target):
        return (f"{label}: merge_target {target!r} does not "
                f"exist in {project_dir} - create the branch there "
                f"(git -C <repo> branch {target}) or choose another "
                f"target; conductor will not fall back to the default "
                f"branch or create it")
    return None


def validate_merge_targets(resolved: list[tuple[Path, str, Step]]) -> None:
    """Pre-flight check for cmd_plan/cmd_run: every explicit merge_target
    must name an existing local branch in its own project's repo. A missing
    branch fails the whole manifest before any state exists - per-project
    targets are checked independently, so a multi-project run reports every
    bad target at once."""
    errs = [merge_target_error(f"step {s.name!r}", pdir, s.merge_target)
            for pdir, _slug, s in resolved if s.merge_target]
    errs = [e for e in errs if e]
    if errs:
        raise ManifestError("\n".join(errs))


def merge_wt_for(wt_path: Path) -> Path:
    """The conductor-owned 'merge worktree' for a step - a second linked
    worktree used to merge into a target the main checkout does not have
    checked out. Lives next to the step's worktree under data/worktrees."""
    return wt_path.with_name(wt_path.name + ".merge")


def _merge_loc(repo_dir: Path, target: str | None,
               merge_wt: Path) -> tuple[Path | None, str | None]:
    """Where a merge into `target` runs. (loc, None) on success: the main
    checkout when target is unset or IS its current branch, else merge_wt -
    created on demand on the target branch. (None, actionable error) when
    the target vanished or sits in a foreign checkout."""
    if not target or target == current_branch(repo_dir):
        return repo_dir, None
    own = (repo_dir.resolve(), merge_wt.resolve())
    for wdir, wbranch in worktree_list(repo_dir):
        if wbranch == target and wdir.resolve() not in own:
            return None, (f"merge target {target!r} is checked out in "
                          f"{wdir} - detach that worktree or merge there "
                          f"by hand; conductor never merges into a "
                          f"checkout it does not own")
    entry = next(((wdir, b) for wdir, b in worktree_list(repo_dir)
                  if wdir.resolve() == merge_wt.resolve()), None)
    if entry is not None and entry[1] != target:
        # A previous attempt's merge worktree sits on another branch (or
        # detached) - ours to recycle, but it is not the target's checkout.
        _git(repo_dir, "worktree", "remove", "--force", str(merge_wt))
        entry = None
    if entry is None:
        if merge_wt.is_dir() and any(merge_wt.iterdir()):
            return None, (f"stale merge worktree {merge_wt} is not a "
                          f"registered git worktree - remove it by hand "
                          f"so the merge can proceed")
        if not local_branch_exists(repo_dir, target):
            return None, (f"merge target {target!r} no longer exists in "
                          f"{repo_dir} - restore the branch or pick "
                          f"another target")
        # Plain branch name - NOT refs/heads/<target>: a full ref is a
        # commit-ish to `worktree add` and checks out a DETACHED head, so
        # the merge would land on a throwaway head and leave the branch
        # unmoved. Verified below because a name matching a repo path
        # could still detach silently.
        r = _git(repo_dir, "worktree", "add", str(merge_wt), target)
        if r.returncode != 0:
            return None, (f"cannot create merge worktree on {target!r}: "
                          f"{r.stderr.strip()[:200]}")
        if current_branch(merge_wt) != target:
            _git(repo_dir, "worktree", "remove", "--force",
                 str(merge_wt))
            return None, (f"merge worktree {merge_wt} did not check out "
                          f"{target!r} - refusing to merge onto a "
                          f"detached head")
    return merge_wt, None


def merge_step_branch(repo_dir: Path, wt_path: Path, branch: str,
                      target: str | None, merge_wt: Path
                      ) -> tuple[bool, Path | None, str | None]:
    """Merge the step's branch into `target` (None/empty = the main
    checkout's current branch - the legacy behavior).

    Returns (merged, loc, err):
    - merged=True: landed. A conductor-created merge worktree is removed;
      the step's worktree+branch too.
    - merged=False, loc set: the merge was attempted in `loc` and
      conflicted (or a foreign merge blocked it); it was aborted, and a
      merge worktree is kept clean on the target at `loc` when `loc` is
      not the main checkout - a resolver session re-enters the merge
      there. The step's worktree+branch stay either way.
    - merged=False, loc None, err set: no merge could be attempted -
      the target vanished or sits in a foreign checkout. `err` is the
      actionable note; a resolver session cannot fix this."""
    loc, err = _merge_loc(repo_dir, target, merge_wt)
    if loc is None:
        LOG.warning("merge of %s skipped: %s", branch, err)
        return False, None, err
    if merge_in_progress(loc) and not abort_own_merge(loc):
        return False, loc, "a merge is already in progress there"
    r = _git(loc, "merge", "--no-edit", branch)
    if r.returncode != 0:
        _git(loc, "merge", "--abort")
        return False, loc, None
    if _git(loc, "merge-base", "--is-ancestor", branch,
            "HEAD").returncode != 0:
        # rc=0 yet the branch never landed - treat like a conflict:
        # park the work so nothing is silently dropped.
        _git(loc, "merge", "--abort")
        return False, loc, ("merge reported success but the branch did "
                            "not land - worktree+branch kept")
    worktree_remove(repo_dir, wt_path, branch)
    if loc != repo_dir:
        # Our throwaway checkout of the target - the branch must stay.
        _git(repo_dir, "worktree", "remove", "--force", str(loc))
    return True, loc, None


def worktree_add(repo_dir: Path, wt_path: Path, branch: str,
                 start: str | None = None) -> bool:
    if wt_path.is_dir():
        return True   # resume: reuse the worktree left by an earlier attempt
    cmd = ["worktree", "add", str(wt_path), "-b", branch]
    if start:
        cmd.append(start)   # baseline the branch on the merge target
    r = _git(repo_dir, *cmd)
    if r.returncode != 0 and "already exists" in r.stderr:
        r = _git(repo_dir, "worktree", "add", str(wt_path), branch)
    return r.returncode == 0


def worktree_remove(repo_dir: Path, wt_path: Path, branch: str) -> None:
    _git(repo_dir, "worktree", "remove", "--force", str(wt_path))
    _git(repo_dir, "branch", "-D", branch)


def worktree_debris(repo_dir: Path, wt_path: Path, branch: str,
                    target: str | None = None) -> bool:
    """A branch is debris when it carries no commits beyond the merge
    target (or the repo's HEAD when none was chosen) - the step died before
    committing anything. Safe to delete."""
    r = _git(repo_dir, "rev-list", "--count", f"{target or 'HEAD'}..{branch}")
    return r.returncode == 0 and r.stdout.strip() == "0"


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


def _repo_merge_busy(repo_dir: Path) -> bool:
    """A live process holds this repo's merge lockfile - another conductor's
    merge (or staged deferred-merge op) is in flight; don't touch its
    in-progress merge state."""
    gd = _git_dir(repo_dir)
    if gd is None:
        return False
    lock = gd / "conductor-merge.lock"
    try:
        info = json.loads(lock.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        return False
    pid = info.get("pid")
    if pid == os.getpid():
        return False                # our own lock, taken upstream
    return pid_alive(pid)


def abort_own_merge(repo_dir: Path) -> bool:
    """Abort an in-progress merge ONLY if it is conductor's own: MERGE_HEAD
    must sit on a conductor/* branch. A merge a human started (or one on an
    unrelated branch) is left alone. Covers 'conductor died mid-resolution':
    resume finds the stale merge and backs it out before re-running.
    A live repo merge lock means another conductor is mid-merge right now -
    aborting 'our' conductor/* merge would be killing THEIR live op."""
    mh = _git(repo_dir, "rev-parse", "-q", "--verify", "MERGE_HEAD")
    if mh.returncode != 0:
        return False
    if _repo_merge_busy(repo_dir):
        return False
    sha = mh.stdout.strip()
    brs = _git(repo_dir, "branch", "--contains", sha,
               "--list", "conductor/*")
    if "conductor/" not in brs.stdout:
        return False
    return _git(repo_dir, "merge", "--abort").returncode == 0


# ---------------------------------------------------- deferred merge engine
#
# merge="deferred" separates worktree isolation from the merge-back: a step's
# branch, worktree, commits and verify result are RETAINED as a pending merge
# (state["pending_merges"], one group per project repo) until a human asks
# merge_run to land them. Sequential deferred steps on the same project must
# still see their verified predecessors' work, so each run keeps an isolated
# per-project integration baseline branch (conductor/<run>/baseline-<slug>)
# that fast-forwards over each verified sequential step's tip and absorbs
# target-branch movement via plumbing merges. Parallel same-project wave
# members are alternatives: they share the baseline tip as their merge base
# but never join it - each stays an individually identifiable pending entry.

def _baseline_name(run_name: str, slug: str) -> str:
    return sanitize_branch(f"conductor/{run_name}/baseline-{slug}")


def _repo_key(repo_dir: Path) -> str:
    return os.path.normcase(str(repo_dir.resolve()))


def _pending_groups(state: dict) -> dict:
    return state.setdefault("pending_merges", {})


def _baseline_tip(repo_dir: Path, ref: str) -> str | None:
    r = _git(repo_dir, "rev-parse", "--verify", ref)
    return r.stdout.strip() if r.returncode == 0 else None


def _merge_tree(repo_dir: Path, ours: str, theirs: str) -> str | None:
    """Plumbing 3-way merge (git>=2.38): returns the merged tree's commit oid
    (parents ours,theirs) or None on conflict/unsupported. Never touches any
    checkout - the baseline absorbs target movement without a worktree."""
    r = _git(repo_dir, "merge-tree", "--write-tree", ours, theirs)
    if r.returncode != 0:
        return None
    tree = r.stdout.splitlines()[0].strip() if r.stdout.strip() else ""
    if not tree:
        return None
    c = subprocess.run(["git", "-C", str(repo_dir), "commit-tree", tree,
                        "-p", ours, "-p", theirs,
                        "-m", "conductor: absorb target movement into "
                              "baseline"],
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=120,
                       **hidden_subprocess_kwargs())
    return c.stdout.strip() if c.returncode == 0 and c.stdout.strip() else None


def _baseline_rebuild(repo_dir: Path, grp: dict, steps: dict,
                      run_dir: Path) -> bool:
    """The baseline ref is gone but pending entries remain (a deleted
    branch, or state/ref mismatch after manual cleanup): rebuild it by
    folding each verified sequential entry's branch tip over the recorded
    target sha, in entry order. A conflict mid-fold keeps what folded -
    the rest still reconciles at explicit-merge time."""
    cur = grp.get("target_sha") or ""
    if not cur or _baseline_tip(repo_dir, cur) is None:
        return False
    for n in grp.get("entries") or []:
        rec = steps.get(n) or {}
        if rec.get("merge_kind") != "sequential" \
                or not rec.get("deferred_verified"):
            continue
        tip = _baseline_tip(repo_dir, rec.get("branch") or "?")
        if tip is None or _git(repo_dir, "merge-base", "--is-ancestor",
                               tip, cur).returncode == 0:
            continue
        nxt = _merge_tree(repo_dir, cur, tip)
        if nxt is None:
            break
        cur = nxt
    ok = _git(repo_dir, "update-ref", f"refs/heads/{grp['baseline']}",
              cur).returncode == 0
    if ok:
        log_event(run_dir, event="baseline_rebuild",
                  project=str(repo_dir), baseline=grp["baseline"], tip=cur)
    return ok


def _baseline_ensure(repo_dir: Path, slug: str, run_name: str,
                     state: dict, run_dir: Path,
                     target: str | None = None) -> dict | None:
    """Get-or-create the run's integration baseline group for this repo.

    The recorded target is the step's merge_target when one was chosen,
    else the branch checked out when the FIRST deferred step on this repo
    was prepared (+ its tip sha); later deferred merges aim at that
    recorded ref, not wherever HEAD happens to point by then. The
    baseline branch itself is created at that same recorded tip - or,
    when the group exists but the ref was lost, rebuilt from the verified
    sequential entries it used to carry."""
    groups = _pending_groups(state)
    key = _repo_key(repo_dir)
    grp = groups.get(key)
    if grp and _baseline_tip(repo_dir, grp["baseline"]):
        return grp
    if target is None:
        target = _git(repo_dir, "rev-parse", "--abbrev-ref",
                      "HEAD").stdout.strip()
        sha = _git(repo_dir, "rev-parse", "HEAD").stdout.strip()
        if target == "HEAD" or not sha:
            # detached HEAD or unborn branch: no sane recorded target
            return None
    else:
        sha = _baseline_tip(repo_dir, target)
        if not sha:
            return None
    baseline = (grp or {}).get("baseline") or _baseline_name(run_name, slug)
    # Two projects can slug-collide (literal paths share p.name); keep the
    # ref unique per repo within the run.
    used = {g["baseline"] for k, g in groups.items() if k != key}
    if baseline in used:
        baseline = sanitize_branch(
            f"{baseline}-{_repo_key(repo_dir)[-6:]}")
    grp = dict(grp or {})
    grp.update(repo_dir=str(repo_dir), slug=slug,
               target_branch=(grp or {}).get("target_branch") or target,
               target_sha=(grp or {}).get("target_sha") or sha,
               baseline=baseline,
               baseline_stuck=bool(grp.get("baseline_stuck")),
               entries=list((grp or {}).get("entries") or []))
    if _baseline_tip(repo_dir, baseline) is None:
        if grp["entries"] and _baseline_rebuild(
                repo_dir, grp, state.get("steps") or {}, run_dir):
            pass
        elif _git(repo_dir, "update-ref", f"refs/heads/{baseline}",
                  sha).returncode != 0:
            return None
        else:
            log_event(run_dir, event="baseline_create",
                      project=str(repo_dir), baseline=baseline,
                      target=grp["target_branch"], sha=sha)
    else:
        log_event(run_dir, event="baseline_reattach", project=str(repo_dir),
                  baseline=baseline)
    groups[key] = grp
    return grp


def _baseline_sync(repo_dir: Path, grp: dict, run_dir: Path) -> str | None:
    """Absorb recorded-target movement (human commits, auto-mode merges) into
    the baseline so later deferred steps see it. Returns the baseline tip to
    branch from. A conflicting absorb keeps the old tip and marks the group
    baseline_stuck - the staged merge reconciles main-vs-run at land time."""
    ref = grp["baseline"]
    tip = _baseline_tip(repo_dir, ref)
    target_tip = _baseline_tip(repo_dir, grp["target_branch"])
    if tip is None or target_tip is None:
        return tip
    if _git(repo_dir, "merge-base", "--is-ancestor",
            target_tip, tip).returncode == 0:
        return tip                      # baseline already covers the target
    merged = _merge_tree(repo_dir, tip, target_tip)
    if merged is None:
        grp["baseline_stuck"] = True
        log_event(run_dir, event="baseline_stuck", project=str(repo_dir),
                  baseline=ref, target_tip=target_tip)
        return tip
    if _git(repo_dir, "update-ref", f"refs/heads/{ref}", merged,
            tip).returncode == 0:
        grp["baseline_stuck"] = False
        grp["target_sha"] = target_tip
        log_event(run_dir, event="baseline_sync", project=str(repo_dir),
                  baseline=ref, tip=merged)
        return merged
    return tip


def _baseline_advance(repo_dir: Path, grp: dict, step_branch: str,
                      step_name: str, run_dir: Path) -> bool:
    """Fast-forward the baseline over a verified sequential step's tip. The
    step branched off the baseline tip, so its tip already contains it - the
    update-ref expected-value makes the advance atomic."""
    tip = _baseline_tip(repo_dir, grp["baseline"])
    step_tip = _baseline_tip(repo_dir, step_branch)
    if tip is None or step_tip is None:
        return False
    if _git(repo_dir, "merge-base", "--is-ancestor",
            step_tip, tip).returncode == 0:
        return True                     # nothing new (e.g. zero-commit step)
    ok = _git(repo_dir, "update-ref", f"refs/heads/{grp['baseline']}",
              step_tip, tip).returncode == 0
    if ok:
        log_event(run_dir, event="baseline_advance", project=str(repo_dir),
                  baseline=grp["baseline"], step=step_name, tip=step_tip)
    else:
        log_event(run_dir, event="baseline_advance_failed",
                  project=str(repo_dir), baseline=grp["baseline"],
                  step=step_name)
    return ok


def _deferred_pending_note(rec: dict, grp: dict, wt: Path) -> None:
    _append_note(
        rec, f"deferred merge: retained at {wt} as pending work on "
             f"{grp['baseline']} (target {grp['target_branch']}); "
             "lands only via an explicit merge")


def _deferred_record(rec: dict, grp: dict, kind: str, step_name: str) -> None:
    """Attach the step to its repo's pending-merge group and stamp the
    deferred metadata the merge op + dashboard read. Verified here means:
    the step reported done and no verify gate went red."""
    rec.update(deferred=True, merge_kind=kind, baseline=grp["baseline"],
               target_branch=grp["target_branch"],
               deferred_verified=(rec.get("status") == "done"
                                  and rec.get("verify_ok") is not False))
    if step_name not in grp["entries"]:
        grp["entries"].append(step_name)


# ---------------------------------------------------------------- run state

def _now() -> float:
    """The one clock seam: every timestamp the conductor RECORDS
    (now_iso stamps, step/run elapsed math) routes through here so tests
    can drive a deterministic clock. Liveness probes (step_live,
    pid_alive, wait_worker deadlines) stay on real time - they gate on
    real processes, not recorded history."""
    return time.time()


def now_iso() -> str:
    return dt.datetime.fromtimestamp(_now()).isoformat(
        timespec="seconds")


def _iso_from(ts: float | None) -> str | None:
    """Epoch -> the same naive local ISO shape now_iso() produces."""
    return (dt.datetime.fromtimestamp(ts).isoformat(timespec="seconds")
            if ts is not None else None)


def _step_seed_intervals(rec: dict) -> None:
    """Fold a pre-exec_intervals record's stored timing into one honest
    first interval. A started/ended pair is real data; a lone elapsed_s
    survives as {'dur'} - its endpoints are gone, and inventing them
    would lie about when the work ran."""
    ivs: list[dict] = []
    t0s, t1s = rec.get("started_at"), rec.get("ended_at")
    span = rec.get("elapsed_s")
    if t0s and t1s:
        ivs.append({"start": t0s, "end": t1s})
    elif t0s and isinstance(span, (int, float)):
        t0 = _iso_ts(t0s)
        ivs.append({"start": t0s,
                    "end": _iso_from(t0 + span) if t0 is not None
                    else None})
    elif t1s and isinstance(span, (int, float)):
        t1 = _iso_ts(t1s)
        ivs.append({"start": _iso_from(t1 - span) if t1 is not None
                    else None, "end": t1s})
    elif isinstance(span, (int, float)):
        ivs.append({"dur": float(span)})
    rec["exec_intervals"] = ivs


def _step_last_write(rec: dict, run_dir: Path | None) -> float | None:
    """Newest mtime over the step's own log files - the last sign of
    life a dispatch that died mid-flight left behind."""
    if run_dir is None or not rec.get("name"):
        return None
    newest = None
    try:
        for f in run_dir.glob(f"{rec['name']}*.stdout.log"):
            try:
                m = f.stat().st_mtime
            except OSError:
                continue
            newest = m if newest is None else max(newest, m)
    except OSError:
        pass
    return newest


def _iv_close(rec: dict, end_iso: str, *, current: dict | None = None,
              run_dir: Path | None = None) -> None:
    """Close open exec_intervals and refresh the stored cumulative
    elapsed_s. `current` - this dispatch's interval - ends at end_iso
    verbatim (timeout extensions legitimately exceed the liveness
    horizon). A stale survivor (driver died mid-dispatch) ends at the
    last recorded sign of life - its stdout log's mtime - or the
    liveness horizon when no evidence exists; never at end_iso, which
    would fold the dead gap into elapsed."""
    ivs = rec.get("exec_intervals") or []
    stale = [iv for iv in ivs
             if iv.get("end") is None and "dur" not in iv
             and iv is not current]
    wit = _step_last_write(rec, run_dir) if stale else None
    horizon_s = float(rec.get("timeout_min") or 60) * 60 + 900
    end_t = _iso_ts(end_iso)
    for iv in ivs:
        if iv.get("end") is not None or "dur" in iv:
            continue
        t0 = _iso_ts(iv.get("start"))
        if iv is current or t0 is None or end_t is None:
            iv["end"] = end_iso
            continue
        cand = wit if (wit is not None and wit > t0) \
            else t0 + horizon_s
        close_t = min(end_t, cand)
        iv["end"] = _iso_from(max(t0, close_t))
    els = rec_elapsed_s(rec)
    if els is not None:
        rec["elapsed_s"] = els


def _iv_open(rec: dict, start_iso: str,
             *, run_dir: Path | None = None) -> dict:
    """Append a fresh execution interval for this dispatch. First touch
    under the schema seeds the record's legacy timing; any still-open
    interval is a crashed dispatch's orphan and closes at evidence."""
    if "exec_intervals" not in rec:
        _step_seed_intervals(rec)
    _iv_close(rec, start_iso, run_dir=run_dir)
    iv = {"start": start_iso, "end": None}
    rec["exec_intervals"].append(iv)
    return iv


def rec_elapsed_s(rec: dict, now: float | None = None,
                  live: bool | None = None) -> float | None:
    """The one authoritative step elapsed: every recorded closed
    interval, plus the open interval ticking to `now` while the step is
    actually in flight (`live` lets callers that already computed
    liveness - dashboard, status - say so; unset falls back to the
    record's own status/ended_at). A stale open interval contributes
    nothing until _iv_close stamps its evidence-bounded end. Records
    predating exec_intervals fall back to their stored span or
    start/end pair - recorded data, never an estimate. None when no
    timing exists at all (the '-' the views render)."""
    if live is None:
        live = rec.get("status") == "running" \
            and rec.get("ended_at") is None
    ivs = rec.get("exec_intervals")
    if not ivs:
        t0 = _iso_ts(rec.get("started_at"))
        t1 = _iso_ts(rec.get("ended_at"))
        if t0 is not None and t1 is not None:
            return round(max(0.0, t1 - t0), 1)
        span = rec.get("elapsed_s")
        if isinstance(span, (int, float)):
            return round(float(span), 1)
        if t0 is not None and live:
            n = _now() if now is None else now
            return round(max(0.0, n - t0), 1)
        return None
    total = 0.0
    for iv in ivs:
        if isinstance(iv.get("dur"), (int, float)):
            total += float(iv["dur"])
            continue
        t0 = _iso_ts(iv.get("start"))
        if t0 is None:
            continue
        t1 = _iso_ts(iv.get("end"))
        if t1 is None:
            if not live:
                continue
            t1 = _now() if now is None else now
        total += max(0.0, t1 - t0)
    return round(total, 1)


def _run_witness_ts(state: dict, run_dir: Path | None = None) -> float | None:
    """The last recorded sign of a driver session's life: the newest
    stamp in state.json plus the tail of events.jsonl. Used to end a
    crashed driver's open interval at evidence, not discovery time."""
    stamps = [_iso_ts(state.get(k))
              for k in ("started_at", "ended_at", "driver_started_at",
                        "archived")]
    for rec in (state.get("steps") or {}).values():
        if isinstance(rec, dict):
            stamps += [_iso_ts(rec.get(k))
                       for k in ("started_at", "ended_at", "merged_at")]
    if run_dir is not None:
        try:
            ev = run_dir / "events.jsonl"
            data = ev.read_bytes()[-8192:].decode("utf-8", "replace")
            for line in reversed(data.splitlines()):
                try:
                    stamps.append(_iso_ts(
                        json.loads(line).get("ts")))
                    if stamps[-1] is not None:
                        break
                except ValueError:
                    continue
        except OSError:
            pass
    return max((t for t in stamps if t is not None), default=None)


def _run_iv_close(state: dict, end_iso: str, *, run_dir: Path | None = None,
                  witness_cap: bool = False) -> None:
    """Close the open driver interval. A stamp written by the live driver
    itself is verbatim (witness_cap=False - its bookkeeping after the
    last step is real driver work); a discovery-time close (sweep
    finding a dead driver, a resume that found no ended_at) caps the
    interval at the last recorded sign of life so the dead gap stays
    out of the total."""
    ivs = state.get("exec_intervals") or []
    end_t = _iso_ts(end_iso)
    wit = None
    for iv in ivs:
        if iv.get("end") is not None:
            continue
        t0 = _iso_ts(iv.get("start"))
        if end_t is None or t0 is None or not witness_cap:
            iv["end"] = end_iso
            continue
        if wit is None:
            wit = _run_witness_ts(state, run_dir)
        iv["end"] = _iso_from(wit) \
            if wit is not None and t0 <= wit < end_t else end_iso


def _run_event_windows(run_dir: Path | None
                       ) -> tuple[list[tuple[float, float]], float | None]:
    """(closed windows, trailing open start) rebuilt from the run's own
    events.jsonl: every dispatch logs `run_start`, and run_end /
    run_abort / run_stop / run_deadline closes it. Legacy state rows (no
    exec_intervals) lost every dispatch but the last to stamp bookkeeping
    - the event log still has all of them."""
    closed: list[tuple[float, float]] = []
    open0: float | None = None
    if run_dir is None:
        return closed, open0
    try:
        lines = (run_dir / "events.jsonl").read_text(
            encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return closed, open0
    for ln in lines:
        try:
            ev = json.loads(ln)
        except ValueError:
            continue
        ts = _iso_ts(ev.get("ts"))
        if ts is None:
            continue
        kind = ev.get("event")
        if kind == "run_start":
            open0 = ts
        elif kind in ("run_end", "run_abort", "run_stop",
                      "run_deadline") and open0 is not None:
            if ts > open0:
                closed.append((open0, ts))
            open0 = None
    return closed, open0


def _run_elapsed_split(state: dict,
                       run_dir: Path | None = None
                       ) -> tuple[float | None, str | None]:
    """(closed_seconds, live_start_iso) for a run's elapsed. The closed
    part sums every finished driver interval plus a crashed interval
    capped at its last sign of life - stable across refreshes. The live
    interval never enters it: its start ships separately so the client
    ticks base + now-start. Legacy rows (no exec_intervals) rebuild
    driver windows from the event log's run_start/terminal pairs when a
    run_dir is in hand - a resumed legacy run keeps EVERY recorded
    dispatch instead of only the last driver's provable seconds; without
    events they fall back to the recorded span (last driver_started_at->
    ended_at window for a resumed run, the full pair for a single
    execution, last witness for a dead unended one); nothing recorded
    -> (None, None) so the column shows '-' not a guess."""
    ivs = state.get("exec_intervals")
    live = not state.get("ended_at") and driver_alive(state)
    if ivs:
        total = 0.0
        exec_start = None
        wit = None
        for iv in ivs:
            t0 = _iso_ts(iv.get("start"))
            if t0 is None:
                continue
            t1 = _iso_ts(iv.get("end"))
            if t1 is None:
                t1 = _iso_ts(state.get("ended_at"))
            if t1 is None:
                if live:
                    exec_start = iv["start"]
                    continue
                if wit is None:
                    wit = _run_witness_ts(state, run_dir)
                t1 = wit if wit is not None and wit > t0 else t0
            total += max(0.0, t1 - t0)
        return round(total, 1), exec_start
    t0 = _iso_ts(state.get("started_at"))
    t1 = _iso_ts(state.get("ended_at"))
    d0 = _iso_ts(state.get("driver_started_at"))
    wins, open0 = _run_event_windows(run_dir)
    if wins or open0 is not None:
        # Event-derived windows beat the single-stamp heuristics: the
        # resume that stamped driver_started_at also logged run_start, so
        # resumed legacy runs report all dispatches, not a 4-second tail.
        total = sum(max(0.0, b - a) for a, b in wins)
        if live:
            start = open0 if open0 is not None else (d0 or t0)
            return (round(total, 1),
                    _iso_from(start) if start is not None else None)
        if open0 is not None:           # died mid-dispatch - end at evidence
            wit = _run_witness_ts(state, run_dir)
            if wit is not None and wit > open0:
                total += wit - open0
        return round(total, 1), None
    if live:
        base = d0 if d0 is not None else t0
        return (0.0, _iso_from(base)) if base is not None else (None, None)
    if t1 is not None and (t0 is not None or d0 is not None):
        # resumed before the schema existed: the earlier executions'
        # spans are unrecoverable - report the provable last one
        base = d0 if (d0 is not None and t0 is not None
                      and d0 > t0 + 1) else (t0 if t0 is not None else d0)
        return round(max(0.0, t1 - base), 1), None
    wit = _run_witness_ts(state, run_dir)
    base = d0 if d0 is not None else t0
    if wit is not None and base is not None:
        return round(max(0.0, wit - base), 1), None
    return None, None


def run_elapsed_s(state: dict, now: float | None = None,
                  *, run_dir: Path | None = None) -> float | None:
    """The one authoritative run elapsed: the driver's own execution
    intervals (idle gaps between resumes are outside all of them), plus
    the live interval to `now` while a driver is in flight. Never a sum
    of step spans - wave members overlap in wall time."""
    els, start = _run_elapsed_split(state, run_dir)
    if els is None and start is None:
        return None
    total = els or 0.0
    if start is not None:
        t0 = _iso_ts(start)
        if t0 is not None:
            n = _now() if now is None else now
            total += max(0.0, n - t0)
    return round(total, 1)


def save_state(run_dir: Path, state: dict) -> None:
    _fold_review_flags(run_dir, state)
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


def _run_hist_file(run_dir: Path) -> Path:
    """Per-run USER-action history - kept in the flag dir, not run_dir,
    so dashboard writes never trip a live worker's user-edit-awareness
    (trap 5). events.jsonl itself stays driver-owned; the modals merge
    both feeds."""
    return _flag_dir() / f"hist-{_flag_key(run_dir)}.jsonl"


def log_user_event(run_dir: Path, **kv) -> None:
    """Best-effort audit line for dashboard-side actions (prompt edits,
    notes, review marks, timeout changes, control clicks). A failed
    history write must never fail the action it annotates."""
    try:
        _flag_dir().mkdir(exist_ok=True)
        with open(_run_hist_file(run_dir), "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": now_iso(), "by": "dashboard",
                                **kv}, ensure_ascii=False) + "\n")
    except OSError:
        pass


def run_events(run_dir: Path, limit: int = 2000) -> list[dict]:
    """Merged run history for the modals: the driver's events.jsonl plus
    the dashboard's flag-dir action log, sorted oldest-first and capped
    at the newest `limit` entries."""
    out: list[dict] = []
    for src, tag in ((run_dir / "events.jsonl", "run"),
                     (_run_hist_file(run_dir), "ui")):
        try:
            lines = src.read_text(encoding="utf-8",
                                  errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if isinstance(ev, dict):
                ev["src"] = tag
                out.append(ev)
    out.sort(key=lambda e: str(e.get("ts") or ""))
    return out[-limit:]


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


def tail_outcome(stdout_log: Path) -> str | None:
    """The worker's OUTCOME: line - the human-facing one-to-two-sentence
    summary of what the step ACTUALLY accomplished (concrete results plus
    material limits like partial work or a red verify), written for the
    person reviewing the run rather than for the next session. Whole-file
    like tail_status: the closing report can push it past any fixed tail
    window. None when the step predates the contract or ended before its
    report - callers must show that honestly, never substitute the NEXT:
    handoff or a log tail."""
    try:
        data = stdout_log.read_bytes().decode("utf-8", "replace")
    except OSError:
        return None
    found = OUTCOME_RE.findall(data)
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


def tail_session_id(export_path: Path) -> str | None:
    """The session id recorded in the ATIF export - top-level `session_id`
    (the mock writes `session` instead). None when the session never wrote
    an export (killed early) or the file can't be parsed."""
    try:
        d = json.loads(export_path.read_text(encoding="utf-8",
                                             errors="replace"))
    except (OSError, ValueError):
        return None
    sid = d.get("session_id") or d.get("session")
    return sid if isinstance(sid, str) and sid else None


def final_report(export_path) -> str | None:
    """The worker's own closing report - the trailing run of agent
    messages with no tool calls in the ATIF export, minus the STATUS:/
    NEXT: contract lines. That prose is the same 'what I did / what I
    found / what's left' wrap-up a chat session ends with; conductor
    summaries surface it instead of re-describing the session
    mechanically. None when the export is missing/unreadable or the turn
    ended without a report (crash, no-op spawn, truncated mid-turn)."""
    try:
        d = json.loads(Path(export_path).read_text(encoding="utf-8",
                                                   errors="replace"))
    except (OSError, ValueError, TypeError):
        return None
    texts: list[str] = []
    for s in reversed(d.get("steps") or []):
        if not isinstance(s, dict) or s.get("source") != "agent" \
                or s.get("tool_calls"):
            break
        msg = (s.get("message") or "").strip()
        if msg:
            texts.append(msg)
    if not texts:
        return None
    lines = "\n\n".join(reversed(texts)).splitlines()
    while lines and lines[-1].startswith(("STATUS:", "NEXT:")):
        lines.pop()
    body = "\n".join(lines).rstrip()
    return body or None


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
    try:
        em = os.path.getmtime(export_path)
    except OSError:
        em = None
    # em: export mtime - the dashboard keys its full-text detail cache on
    # path+mtime, so a resumed step's rewritten export refetches cleanly.
    # groups_total reports the pre-cap count: past 40 groups the UI shows
    # an honest remainder note instead of dropping them silently (their
    # detail is still served - the /trajectory endpoint knows every sid).
    return {"steps": len(steps), "groups": out[:40], "em": em,
            "groups_total": len(out)}


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


# Full trajectory text: the arc rows carry only a ~110-char `head`, so
# expanding an entry re-reads the SAME export file for the fields the
# CLI retained verbatim - message, reasoning_content, tool-call
# arguments, and the tool results under `observation`. Served on demand
# (never inlined into the polled /api/state payload).
ARC_DETAIL_CAP = 200_000   # chars served per text field


def _arc_cut(text, trunc: dict, field: str) -> str | None:
    """Cap one served field - a multi-MB blob would stall the modal, so
    an overlong field ships its head with the ORIGINAL length recorded
    in `trunc` (labeled honestly in the UI, never silently dropped)."""
    if not isinstance(text, str) or not text:
        return None
    if len(text) <= ARC_DETAIL_CAP:
        return text
    trunc[field] = len(text)
    return text[:ARC_DETAIL_CAP]


def _arc_args_str(arguments) -> str | None:
    """Tool-call arguments for display: dicts become 'key: value' lines -
    a multiline string value (an edit payload, a command) stays literal
    instead of JSON-escaped, so the text reads as it was written."""
    if arguments is None:
        return None
    if isinstance(arguments, str):
        return arguments
    if not isinstance(arguments, dict):
        try:
            return json.dumps(arguments, ensure_ascii=False)
        except (TypeError, ValueError):
            return str(arguments)
    lines = []
    for k, v in arguments.items():
        if isinstance(v, str):
            lines.append(f"{k}:\n{v}" if "\n" in v else f"{k}: {v}")
        else:
            try:
                lines.append(f"{k}: {json.dumps(v, ensure_ascii=False)}")
            except (TypeError, ValueError):
                lines.append(f"{k}: {v}")
    return "\n".join(lines)


def trajectory_detail(export_path) -> dict | None:
    """Full per-entry text behind the arc rows. Returns
    {"entries": {<sid>: {src, model, msg, think, calls:[{name,args,
    result}], extra_obs, trunc}}}; None when the export is missing,
    corrupt, or holds no steps (mock exports - nothing was recorded)."""
    try:
        d = json.loads(Path(export_path).read_text(
            encoding="utf-8", errors="replace"))
    except (OSError, ValueError, TypeError):
        return None
    steps = d.get("steps")
    if not isinstance(steps, list) or not steps:
        return None
    entries: dict[str, dict] = {}
    for s in steps:
        if not isinstance(s, dict):
            continue
        sid = s.get("step_id")
        if sid is None:
            continue
        trunc: dict[str, int] = {}
        obs = s.get("observation")
        results = [r for r in ((obs or {}).get("results") or [])
                   if isinstance(r, dict)] if isinstance(obs, dict) else []
        first_for: dict = {}     # source_call_id -> results[] index
        for i, r in enumerate(results):
            cid = r.get("source_call_id")
            if cid is not None and cid not in first_for:
                first_for[cid] = i
        used: set[int] = set()
        calls = []
        for c in (s.get("tool_calls") or []):
            if not isinstance(c, dict):
                continue
            args = _arc_args_str(c.get("arguments"))
            ri = first_for.get(c.get("tool_call_id"))
            res = results[ri].get("content") if ri is not None else None
            if ri is not None:
                used.add(ri)
            calls.append({"name": c.get("function_name") or "?",
                          "args": _arc_cut(args, trunc, "args"),
                          "result": _arc_cut(res, trunc, "result")})
        entries[str(sid)] = {
            "sid": sid,
            "src": s.get("source"),
            "model": s.get("model_name"),
            "msg": _arc_cut(s.get("message"), trunc, "message"),
            "think": _arc_cut(s.get("reasoning_content"),
                              trunc, "reasoning"),
            "calls": calls,
            # tool results that matched no call still surface -
            # dropping them would hide retained text
            "extra_obs": [_arc_cut(r.get("content"), trunc, "observation")
                          for i, r in enumerate(results)
                          if i not in used],
            "trunc": trunc or None}
    return {"entries": entries} if entries else None


_ARC_DETAIL_CACHE: dict[str, tuple[float, dict | None]] = {}


def cached_arc_detail(path) -> dict | None:
    """mtime-keyed memo over trajectory_detail - same rationale as
    cached_arc (the export lands once), plus a bound: detail payloads
    are ~the export's own size, so only the last few stay resident."""
    if not path:
        return None
    try:
        m = os.path.getmtime(path)
    except OSError:
        return None
    key = str(path)
    hit = _ARC_DETAIL_CACHE.get(key)
    if hit and hit[0] == m:
        return hit[1]
    det = trajectory_detail(path)
    if len(_ARC_DETAIL_CACHE) >= 16:
        del _ARC_DETAIL_CACHE[next(iter(_ARC_DETAIL_CACHE))]
    _ARC_DETAIL_CACHE[key] = (m, det)
    return det


def _fmtK(n: int) -> str:
    """Python twin of the dashboard's fmtK: 10754521 -> 10.8M."""
    if n >= 1_000_000:
        s, u = f"{n / 1_000_000:.1f}", "M"
    elif n >= 1000:
        s, u = f"{n / 1000:.1f}", "k"
    else:
        return str(n)
    return s.rstrip("0").rstrip(".") + u


def _branch_commits(repo: str | None, branch: str | None,
                    target: str | None = None) -> list[str]:
    """`git log --oneline <base>..<branch>` for a step's branch - the base
    is the recorded merge target when one exists, else the usual default-
    branch guesses. Best effort: gone branch or deleted worktree just
    yields no section."""
    if not repo or not branch:
        return []
    for base in ([target] if target else []) + ["master", "main"]:
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
    span = rec_elapsed_s(rec)
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
        tgt = rec.get("merge_target")
        branch_s = f"`{branch}` - " + (
            f"merged{f' into `{tgt}`' if tgt else ''}" if merged
            else "unmerged, kept at "
            f"`{rec.get('work_dir') or rec.get('project')}`"
            + (f" (target `{tgt}`)" if tgt else ""))
    # `purpose` is recorded on dispatch; old records only have the marker
    # embedded in `task`, so parse it back out - either way the raw
    # comment must not render as the step's description.
    purpose = rec.get("purpose") or split_purpose(rec.get("task"))[0]
    # 'started' is the run's overall first dispatch; a later-resumed
    # step's latest dispatch start stays visible as a parenthetical.
    first_start = rec.get("first_started_at") or rec.get("started_at")
    started_s = first_start or "-"
    if rec.get("started_at") and rec["started_at"] != first_start:
        started_s += f" (latest dispatch {rec['started_at']})"
    L = [f"# {run_name} / {name}", ""]
    L += [
        f"- **status:** {status} · **verify:** {verify_s} · "
        f"**exit:** {rec.get('exit_code', '-')}",
        f"- **started:** {started_s} · "
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
    if purpose:
        L.append(f"- **purpose:** `{purpose}` ({purpose_label(purpose)})")
    if branch_s:
        L.append(f"- **branch:** {branch_s}")
    if rec.get("outcome"):
        L.append(f"- **outcome:** {rec['outcome']}")
        for e in [x for x in rec.get("outcomes") or []
                  if x.get("outcome") and x.get("outcome") != rec["outcome"]]:
            L.append(f"- **earlier outcome** (attempt "
                     f"{e.get('attempt') or '?'}, "
                     f"{e.get('status') or '?'}): {e.get('outcome')}")
    if rec.get("note"):
        L.append(f"- **note:** {rec['note']}")
    if rec.get("next"):
        L.append(f"- **worker says next:** {rec['next']}")
    L.append("")
    # The FULL task body (task_label reads it back out of the prompt
    # file), fenced so _summary_body draws the same fixed-size box the
    # task modal's 'task prompt' row uses - a one-line 'follow-up-plan'
    # body shouldn't collapse the layout. task_display is the marker
    # safety net for the no-prompt-file path, where task_label hands
    # back the stored summary with its purpose comment still embedded.
    task_full = task_display(task_label(rec, full=True)
                             or rec.get("task"))
    if task_full:
        L += ["## Task", "", "```", task_full, "```", ""]
    report = final_report(rec.get("export"))
    if report:
        # the worker's own closing message - the same end-of-session
        # wrap-up Devin prints in a chat, lifted verbatim from the export
        L += ["## Report", "", report, ""]
    commits = _branch_commits(rec.get("project") or rec.get("work_dir"),
                              branch, rec.get("merge_target"))
    if commits:
        L += [f"## Commits ({len(commits)})", ""]
        L += [f"- {c}" for c in commits]
        L.append("")
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


def step_trajectory(run_name: str, step_name: str,
                    runs_dir: Path = RUNS_DIR) -> dict:
    """GET /api/runs/<run>/steps/<step>/trajectory - the full retained
    text behind a step's arc entries, read from the same ATIF export the
    arc was condensed from. Serves only what the export actually holds:
    no export record, a deleted file, or a step-less (mock) export is a
    404 with the reason, never a padded answer."""
    if not RUN_DIR_RE.fullmatch(step_name):
        raise RequestError("Invalid step name", 404)
    d, st = _run_dir_checked(run_name, runs_dir)
    rec = (st.get("steps") or {}).get(step_name)
    if rec is None:
        raise RequestError("Step not in this run", 404)
    exp = rec.get("export")
    if not exp:
        raise RequestError("No trajectory export was recorded for this "
                           "step - there is no retained full text", 404)
    p = Path(exp).resolve()
    if p.parent != d.resolve():
        raise RequestError("Export path is outside the run dir", 404)
    if not p.is_file():
        raise RequestError("The export file is gone - this step's full "
                           "text was not retained", 404)
    det = cached_arc_detail(p)
    if det is None:
        raise RequestError("The export holds no readable trajectory "
                           "(mock or corrupt export)", 404)
    return det


def request_trajectory(request_id: str,
                       requests_dir: Path = REQUESTS_DIR) -> dict:
    """GET /api/requests/<id>/trajectory - same full text for the
    retained plan-proposal session. Mirrors plan_attempt_agents: only
    the newest attempt's <id>.gen.export.json exists on disk."""
    _request_path(request_id, requests_dir)   # REQUEST_ID_RE gate
    p = (requests_dir / f"{request_id}.gen.export.json").resolve()
    if p.parent != requests_dir.resolve() or not p.is_file():
        raise RequestError("No planner export was retained for this "
                           "request", 404)
    det = cached_arc_detail(p)
    if det is None:
        raise RequestError("The export holds no readable trajectory "
                           "(mock or corrupt export)", 404)
    return det


SUMMARIZE_TIMEOUT_S = 900      # one cheap prose pass, not a work session


def _elapsed_md(rec: dict) -> str:
    span = rec_elapsed_s(rec)
    return (f"{int(span // 60)}m{int(span % 60):02d}s"
            if isinstance(span, (int, float)) else "-")


def _run_elapsed_md(st: dict) -> str:
    span = run_elapsed_s(st)
    return (f"{int(span // 60)}m{int(span % 60):02d}s"
            if isinstance(span, (int, float)) else "-")


def _summary_marker(run_dir: Path) -> Path:
    return run_dir / ".summarizing"          # holds the summarizer pid


def _summarizing(run_dir: Path) -> bool:
    """Is a run-summary session currently live for this run dir? The
    marker holds the subprocess pid; a stale marker whose pid is gone
    (crash between launch and write, or an orphaned file) doesn't
    count."""
    try:
        pid = int(_summary_marker(run_dir).read_text().strip())
    except (OSError, ValueError):
        return False
    return pid_alive(pid)


def _run_step_stats(st: dict) -> dict:
    """Aggregate run-level stats from step records - status counts,
    token sums, merge tallies - for both the deterministic roll-up and
    the summarizer prompt."""
    steps = st.get("steps") or {}
    counts: dict[str, int] = {}
    prompt = completion = cached = merged = parked = 0
    for rec in steps.values():
        status = rec.get("status") or "?"
        counts[status] = counts.get(status, 0) + 1
        tk = rec.get("tokens") or {}
        prompt += tk.get("prompt") or 0
        completion += tk.get("completion") or 0
        cached += tk.get("cached") or 0
        if rec.get("merged"):
            merged += 1
        elif rec.get("branch"):
            parked += 1
    return {"counts": counts, "prompt": prompt, "completion": completion,
            "cached": cached, "merged": merged, "parked": parked}


def run_report_md(st: dict) -> str:
    """Deterministic run roll-up - run stats plus each step's own closing
    report in record order. Served by the run summary endpoint whenever
    no LLM-written run.summary.md exists, and embedded verbatim in the
    summarizer prompt as the source material."""
    agg = _run_step_stats(st)
    L = [f"# {st.get('run') or '?'} - run summary", "",
         "- **steps:** " + (", ".join(
             f"{n} {k}" for k, n in sorted(agg["counts"].items()))
             or "none"),
         f"- **started:** {st.get('started_at') or '-'} · "
         f"**ended:** {st.get('ended_at') or '-'} · "
         f"**elapsed:** {_run_elapsed_md(st)}"]
    if agg["prompt"] or agg["completion"]:
        L.append(f"- **tokens:** {_fmtK(agg['prompt'])} prompt + "
                 f"{_fmtK(agg['completion'])} completion"
                 + (f" ({_fmtK(agg['cached'])} cached)"
                    if agg["cached"] else ""))
    L.append(f"- **merged:** {agg['merged']} · "
             f"**parked branch:** {agg['parked']}")
    for name, rec in (st.get("steps") or {}).items():
        meta = [_elapsed_md(rec),
                rec.get("model_actual") or rec.get("model") or "?"]
        if (rec.get("attempts") or 0) > 1:
            meta.append(f"{rec['attempts']} attempts")
        if rec.get("note"):
            meta.append(f"note: {rec['note']}")
        if rec.get("merged"):
            meta.append("merged" + (f" @{rec['merged_at']}"
                                    if rec.get("merged_at") else ""))
        L += ["", f"## {name} - {rec.get('status') or '?'}", "",
              "- " + " · ".join(meta), ""]
        L.append(final_report(rec.get("export"))
                 or "_the worker's turn ended without a closing report - "
                    "see its session summary or stdout log for what ran_")
    return "\n".join(L) + "\n"


def _summary_prompt(st: dict) -> str:
    """Prompt for the end-of-run summarizer session: the run stats plus
    each step's task text and own closing report, so the model weighs
    intent against outcome. `conductor-run-summary` marks the document -
    the mock devin keys on it."""
    agg = _run_step_stats(st)
    counts = ", ".join(f"{n} {k}" for k, n in sorted(
        agg["counts"].items())) or "none"
    parts = [
        "# conductor-run-summary",
        "",
        "You are writing the end-of-run report for an automated pipeline",
        "of fresh Devin CLI sessions (agent-conductor). The run below",
        "just finished. Write ONE executive summary for the operator",
        "reading the dashboard in the morning:",
        "",
        "- what the run set out to do and whether it got there",
        "- what actually landed (merged vs parked vs failed); group by",
        "  theme when that reads better than step order",
        "- failures, surprises, unfinished work, anything worth a",
        "  human look in the morning",
        "- do NOT invent work or verification - only report what the",
        "  step statuses and their closing reports below actually say;",
        "  a missing closing report means the session ended mid-turn",
        "",
        "Plain markdown, ~300-600 words unless the run earned more.",
        "Your final message IS the document - output only the summary,",
        "no preamble, no sign-off.",
        "",
        "## Run",
        "",
        f"- name: {st.get('run') or '?'}",
        f"- description: {st.get('description') or '(none)'}",
        f"- steps: {counts}",
        f"- started: {st.get('started_at') or '-'}",
        f"- ended: {st.get('ended_at') or '-'}",
        f"- tokens: {agg['prompt']} prompt + "
        f"{agg['completion']} completion",
        f"- merged: {agg['merged']} · parked branches: {agg['parked']}",
        "",
        "## Steps",
    ]
    for name, rec in (st.get("steps") or {}).items():
        task = (rec.get("task") or "?").strip()
        if len(task) > 1500:
            task = task[:1500] + " [...task truncated...]"
        report = final_report(rec.get("export"))
        if report and len(report) > 6000:
            report = report[:6000] + "\n\n[...report truncated...]"
        parts += [
            "",
            f"### {name} - {rec.get('status') or '?'}",
            "",
            f"task: {task}",
            "",
            f"elapsed: {_elapsed_md(rec)} · "
            f"exit: {rec.get('exit_code')} · "
            f"verify: {rec.get('verify_ok')} · "
            f"merged: {'yes' if rec.get('merged') else 'no'}",
            f"note: {rec.get('note') or '-'}",
            "",
            "closing report:",
            "",
            report or "(none - the session ended without one)",
        ]
    return "\n".join(parts) + "\n"


def _summary_result(st: dict, run_dir: Path, status: str, t0: float,
                    export_path: Path, model: str, rc: int,
                    note: str | None = None,
                    session: str | None = None) -> None:
    """Terminal bookkeeping for a summarizer session - the same metric
    shape a step record carries (status, interval, ATIF token sums,
    requested vs resolved model, session id) so the dashboard can show
    it as a first-class bookkeeping row instead of a log path."""
    st["run_summary"] = {
        "status": status,
        "started_at": (st.get("run_summary") or {}).get("started_at"),
        "ended_at": now_iso(),
        "elapsed_s": round(time.time() - t0, 1),
        "model": model or None,
        "model_actual": tail_agent_model(export_path),
        "session": session or tail_session_id(export_path),
        "tokens": tail_metrics(export_path),
        "exit_code": rc,
        "generated_at": now_iso() if status == "done" else None,
        "note": note}
    save_state(run_dir, st)


def summarize_run(run_dir: Path, devin_argv: list[str] | None = None,
                  model: str = "") -> bool:
    """One fresh `devin -p` session whose only job is prose: it reads the
    run record plus every step's own closing report from a tempdir and
    answers with an executive run summary, lifted out of the export and
    written as run.summary.md. Spawned detached by the driver at run end
    when the manifest opts in ([run] summarize), or on demand from the
    dashboard. Never alters step or run status - a failure just leaves
    the deterministic roll-up serving at the endpoint."""
    devin_argv = devin_argv or ["devin"]
    try:
        st = json.loads((run_dir / "state.json").read_text(
            encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"summarize: cannot read run state: {exc}")
        return False
    steps = st.get("steps") or {}
    if not steps:
        print("summarize: run has no steps - nothing to summarize")
        return False
    if any(step_live(r, driver_alive(st)) for r in steps.values()):
        print("summarize: a step is still live - refusing to write into "
              "an active run dir")
        return False
    marker = _summary_marker(run_dir)
    try:
        marker.write_text(str(os.getpid()), encoding="utf-8")
    except OSError:
        pass
    # The dashboard reads state.run_summary for its bookkeeping row, so
    # stamp it BEFORE the session spawns - 'running' until the terminal
    # write below replaces it. save_state while the run has ended is
    # safe: the live-step guard above already refused an active run.
    st["run_summary"] = {"status": "running", "started_at": now_iso(),
                         "model": model or None}
    save_state(run_dir, st)
    t0 = time.time()
    try:
        log = run_dir / "summarize.stdout.log"
        export_path = run_dir / "summarize.export.json"
        with tempfile.TemporaryDirectory(
                prefix="conductor-summary-") as wd:
            pf = Path(wd) / "summarize.md"
            pf.write_text(_summary_prompt(st), encoding="utf-8")
            cmd = devin_argv + [
                "--print", "--prompt-file", str(pf),
                "--permission-mode", "bypass",
                "--respect-workspace-trust", "false",
                "--export", str(export_path)]
            if model:
                cmd += ["--model", model]
            print(f"[summary] spawning: {shlex.join(cmd)}")
            with open(log, "wb") as out:
                proc = subprocess.Popen(
                    cmd, cwd=wd, stdout=out, stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL, env=clean_devin_env(),
                    **_detached_kwargs())
                rc, timed_out, _hung, _stopped, _ext = wait_worker(
                    proc, log, SUMMARIZE_TIMEOUT_S)
            if timed_out or rc != 0:
                print(f"[summary] session exited rc={rc}"
                      + (" (timed out)" if timed_out else ""))
                _summary_result(st, run_dir, "failed", t0,
                                export_path, model, rc,
                                "timed out" if timed_out else None)
                log_event(run_dir, event="run_summary_failed",
                          exit_code=rc, timed_out=timed_out)
                return False
        report = final_report(export_path)
        if not report:
            print("[summary] session produced no closing report")
            _summary_result(st, run_dir, "failed", t0,
                            export_path, model, rc,
                            "no closing report in export")
            log_event(run_dir, event="run_summary_failed",
                      reason="no closing report in export")
            return False
        sid = tail_session_id(export_path)
        header = (f"# {st.get('run') or run_dir.name} - run summary\n\n"
                  f"_written {now_iso()}"
                  + (f" · session `{sid}`" if sid else "") + "_\n\n")
        (run_dir / "run.summary.md").write_text(
            header + report.rstrip() + "\n", encoding="utf-8")
        _summary_result(st, run_dir, "done", t0, export_path, model, rc,
                        session=sid)
        log_event(run_dir, event="run_summarized",
                  model=model or None, session=sid)
        print(f"[summary] wrote {run_dir / 'run.summary.md'}")
        return True
    finally:
        marker.unlink(missing_ok=True)


def run_summary_doc(name: str, runs_dir: Path = RUNS_DIR) -> str:
    """GET /api/runs/<run>/summary - the LLM-written run.summary.md when
    a summarizer session produced one, otherwise the deterministic
    roll-up built live from the step records."""
    d, st = _run_dir_checked(name, runs_dir)
    f = d / "run.summary.md"
    if f.is_file():
        return f.read_text(encoding="utf-8", errors="replace")
    return run_report_md(st)


def summarize_run_start(name: str, runs_dir: Path = RUNS_DIR,
                        devin_argv: list[str] | None = None,
                        model: str | None = None) -> dict:
    """POST /api/runs/<run>/summarize - spawn the LLM run-summary session
    out of process (same detach discipline as resume/merge launches).
    The subprocess owns the run dir writes; this process only plants the
    marker so the dashboard can show 'Generating...' immediately."""
    d, st = _run_dir_checked(name, runs_dir)
    steps = st.get("steps") or {}
    if not steps:
        raise RequestError("No steps ran yet - nothing to summarize", 409)
    if any(step_live(r, driver_alive(st)) for r in steps.values()):
        raise RequestError("A step is still live - wait for the run to "
                           "finish before summarizing", 409)
    pid = _SUM_PIDS.get(name)
    if (pid and pid_alive(pid)) or _summarizing(d):
        raise RequestError("A summary session is already running for "
                           "this run", 409)
    if model is None:
        model = st.get("summarize_model") or ""
    # The run's own worker binary wins over the server's env default -
    # same rule as resume.
    argv = (st.get("devin_argv")
            or (shlex.split(st["devin"]) if st.get("devin") else None)
            or devin_argv or ["devin"])
    cmd = [sys.executable, str(ROOT / "conductor.py"), "summarize",
           str(d), "--runs-dir", str(runs_dir)]
    if argv != ["devin"]:
        cmd += ["--devin", shlex.join(argv)]
    if model:
        cmd += ["--model", model]
    try:
        pid = _launch_process(cmd, d / "summarize.launch.log")
    except OSError as exc:
        raise RequestError("Could not start the summarizer process",
                           500) from exc
    _SUM_PIDS[name] = pid
    try:
        _summary_marker(d).write_text(str(pid), encoding="utf-8")
    except OSError:
        pass
    log_user_event(d, event="run_summary_requested", pid=pid,
                   model=model or None)
    return {"summarizing": name, "pid": pid}


_ST_CLASS = {
    "done": "st-ok",
    "failed": "st-bad", "timed_out": "st-bad", "lost": "st-bad",
    "blocked": "st-bad", "error": "st-bad",
    "stopped": "st-warn", "partial": "st-warn", "queued": "st-warn",
    "needs_merge": "st-warn", "needs_work": "st-warn",
    "running": "st-run", "skipped": "st-other", "idle": "st-other",
    "superseded": "st-other",
}


def _summary_body(md: str) -> str:
    """Inner markup of a generated summary report - the in-app summary
    modal embeds it via ``?frag=1`` while the standalone page wraps it in
    a document shell.

    We only emit a narrow markdown subset (#-####, - and 1. bullets, ```
    fences, **bold**, *italics*, `code`, ---), and every line is
    HTML-escaped BEFORE inline markup is applied, so agent-authored
    content (task text, stdout, trajectory heads, the LLM run summary)
    can never inject markup."""
    def inline(s: str) -> str:
        s = html.escape(s)
        s = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s)
        s = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"<em>\1</em>", s)
        return re.sub(r"`(.+?)`", r"<code>\1</code>", s)

    body, in_ul, in_ol, in_pre = [], False, False, False
    # '## Task' fences render as the same fixed-size prompt box the task
    # modal shows - tracked via the last heading seen.
    cur_h = ""

    def close_ul() -> None:
        nonlocal in_ul
        if in_ul:
            body.append("</ul>")
            in_ul = False

    def close_ol() -> None:
        nonlocal in_ol
        if in_ol:
            body.append("</ol>")
            in_ol = False

    def close_lists() -> None:
        close_ul()
        close_ol()

    for ln in md.splitlines():
        if in_pre:
            if ln.strip() == "```":
                body.append("</pre>")
                in_pre = False
            else:
                body.append(html.escape(ln))
            continue
        if ln.strip() == "```":
            close_lists()
            body.append('<pre class="taskpre">' if cur_h == "Task"
                        else "<pre>")
            in_pre = True
            continue
        m = re.match(r"^(#{1,4}) ", ln)
        if m:
            close_lists()
            lvl = len(m.group(1))
            cur_h = ln[lvl + 1:].strip()
            body.append(f"<h{lvl}>{inline(ln[lvl + 1:])}</h{lvl}>")
        elif re.fullmatch(r"-{3,}", ln.strip()):
            close_lists()
            body.append("<hr>")
        elif ln.startswith("- "):
            close_ol()
            if not in_ul:
                body.append("<ul>")
                in_ul = True
            item = inline(ln[2:])
            if ln.startswith("- **status:**"):
                # the status token takes the same .stc color the task
                # modal's status cell uses (only our fact-sheet bullets
                # start this way - agent prose is unaffected)
                item = re.sub(
                    r"^<strong>status:</strong>\s*([a-z_]+)",
                    lambda m: (f"<strong>status:</strong> "
                               f'<span class="stc {_ST_CLASS.get(m.group(1), "st-other")}">'
                               f"{m.group(1)}</span>"),
                    item, count=1)
            body.append(f"<li>{item}</li>")
        elif (m := re.match(r"^\d+\. ", ln)):
            close_ul()
            if not in_ol:
                body.append("<ol>")
                in_ol = True
            body.append(f"<li>{inline(ln[m.end():])}</li>")
        elif not ln.strip():
            close_lists()
        else:
            close_lists()
            body.append(f"<p>{inline(ln)}</p>")
    close_lists()
    if in_pre:
        body.append("</pre>")
    return "".join(body)


def summary_html(md: str, title: str) -> str:
    """Render our own generated summary markdown as a styled page - the
    standalone document reachable at the /summary URL directly."""
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
            f"<body>{_summary_body(md)}</body></html>")


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


def intrq_flag(run_dir: Path, step: str) -> Path:
    """Dashboard 'Interrupt + instruct' request: a durable JSON sentinel
    holding the operator's supplemental instructions + attachment refs.
    wait_worker polls it like a stop flag - the worker must die before
    the resume, so submission and the kill land under one name; run_step
    then folds the request into the rnote channel and respawns the SAME
    step. Kept by clear_run_flags like rnote, so a pending interrupt
    survives a driver restart and lands on the next dispatch."""
    return _flag_dir() / f"intrq-{_flag_key(run_dir)}-{_safe_name(step)}"


def gen_stop_flag(request_id: str) -> Path:
    """Dashboard 'Stop proposal' sentinel: polled by the planner's
    wait_worker while a request generates. Request ids are already
    filename-safe (YYYYMMDD-HHMMSS-<8hex>, REQUEST_ID_RE), and unlike run
    sentinels they cannot collide across checkouts - each id exists in
    exactly one requests dir."""
    return _flag_dir() / f"stopgen-{request_id}"


def merge_claim_flag(run_dir: Path) -> Path:
    """The run's merge-ownership claim: while this sentinel exists, a
    manual merge of the run's pending branches (or a resume's startup
    stamp) owns the run's merge surface and the other side must stay
    out. Same flag-dir home as the halt sentinels - machine-global,
    path-keyed to the run's resolved dir, and never inside a watched
    tree (trap 5). clear_run_flags must leave it alone: resume clearing
    its own halt sentinels must not silently drop a merge's live claim.
    The file's JSON payload says who holds it ('by': merge|resume)."""
    return _flag_dir() / f"merge-{_flag_key(run_dir)}"


def _flag_text(flag: Path) -> str | None:
    try:
        return flag.read_text(encoding="utf-8") if flag.is_file() else None
    except OSError:
        return None


def review_flag(run_dir: Path, step: str | None = None) -> Path:
    """Dashboard review marks ('Mark as reviewed' / 'Mark as needing
    review'): one JSON sentinel per run and per step -
    {"reviewed": bool, "seq": int|null, "at": iso}. `seq` records which
    completion_seq the mark covers, so an ack for an older completion
    can never hide a newer one. Durable like skip-: clear_run_flags
    leaves it, save_state folds it into state.json, and the dashboard
    overlays it live the way prompt-/rnote-/skip- flags are read."""
    n = "review-" + _flag_key(run_dir)
    return _flag_dir() / (n + f"-{_safe_name(step)}" if step else n)


def _review_flag_body(run_dir: Path, step: str | None = None) -> dict | None:
    """The review sentinel's parsed body, or None when absent/malformed."""
    try:
        body = json.loads(review_flag(run_dir, step).read_text(
            encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return body if isinstance(body, dict) and "reviewed" in body else None


def _review_mark(run_dir: Path, stored,
                 step: str | None = None) -> dict | None:
    """The newest review mark for a run or step: the live sentinel body
    when present, else the mark already folded into state.json. The two
    compare on `at` (ISO text sorts chronologically) so a stale sentinel
    can't resurrect an older mark over a newer folded one."""
    flag = _review_flag_body(run_dir, step)
    if flag is None:
        return stored if isinstance(stored, dict) else None
    s_at = str((stored or {}).get("at") or "") \
        if isinstance(stored, dict) else ""
    return flag if str(flag.get("at") or "") >= s_at else stored


def _needs_review(seq, mark: dict | None) -> bool:
    """Does this completion still need a human look?

    True on an explicit 'needs review' tombstone (mark.reviewed is
    False), or when a completion_seq exists that the newest mark does
    not cover - an ack binds the seq it saw, so a re-completion after
    resume/retry flags again while repeated saves of the same outcome
    keep the ack. A seq-less legacy record is never flagged on its own.
    """
    if isinstance(mark, dict) and mark.get("reviewed") is False:
        return True
    return seq is not None and (not isinstance(mark, dict)
                                or mark.get("seq") != seq)


# Step outcomes that left work worth a human look - a review tombstone or
# a cascade mark only ever lands on these (or a seq-bearing record, which
# already implies one). queued/skipped/waiting steps never ran: marking
# them 'needs review' would invent work that never happened.
_REVIEWABLE_ST = {"done", "failed", "timed_out", "stopped", "lost",
                  "blocked", "partial"}


def seed_review_flags(runs_dir: Path) -> list[str]:
    """One-time 'nothing was ever reviewed' seeding: completions that
    predate completion_seq can't flag themselves (a seq-less record only
    shows on an explicit tombstone), so runs/steps that ended with no
    mark at all get one here and read 'unreviewed' like everything
    newer. Records that carry ANY mark - ack or tombstone - are left
    alone, which makes the pass idempotent and safe at every serve
    start; it can never undo a human's ack."""
    lines = []
    try:
        _flag_dir().mkdir(exist_ok=True)
    except OSError:
        return lines
    for sj in sorted(runs_dir.glob("*/state.json")):
        try:
            st = json.loads(sj.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        d = sj.parent
        wrote = False
        # The run flags itself on a seq mismatch once ended; only a
        # seq-less ending needs the explicit tombstone.
        if (st.get("ended_at") and st.get("completion_seq") is None
                and _review_mark(d, st.get("review")) is None):
            try:
                review_flag(d).write_text(json.dumps(
                    {"reviewed": False, "seq": None,
                     "at": now_iso()}), encoding="utf-8")
                wrote = True
            except OSError:
                pass
        for sname, srec in (st.get("steps") or {}).items():
            if (srec.get("completion_seq") is not None
                    or srec.get("status") not in _REVIEWABLE_ST
                    or _review_mark(d, srec.get("review"), sname)
                    is not None):
                continue
            try:
                review_flag(d, sname).write_text(json.dumps(
                    {"reviewed": False, "seq": None,
                     "at": now_iso()}), encoding="utf-8")
                wrote = True
            except OSError:
                pass
        if not wrote:
            continue
        lines.append(f"{d.name}: seeded needs-review marks")
        # Fold at once when nothing in the run is live - the marks then
        # survive a tempdir cleanup; a live/watched run keeps them
        # flag-only until a later save lands them (same rule as the
        # endpoint's own writes).
        if not driver_alive(st) and not any(
                step_live(r, False)
                for r in (st.get("steps") or {}).values()):
            try:
                _assert_unwatched(runs_dir, d)
            except RequestError:
                pass
            else:
                save_state(d, st)
    return lines


def _fold_review_flags(run_dir: Path, state: dict) -> None:
    """Fold this run's review-* sentinels into the state dict - called
    from save_state so every driver save carries the newest human marks
    into the durable record (a sentinel wins on `at` ties since the flag
    channel is where endpoints always write first). A mark for a step
    with no record yet stays flag-only until the rec exists."""
    prefix = f"review-{_flag_key(run_dir)}"
    try:
        flags = list(_flag_dir().glob(prefix + "*"))
    except OSError:
        return
    steps = state.get("steps") or {}
    for f in flags:
        try:
            body = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(body, dict) or "reviewed" not in body:
            continue
        suffix = f.name[len(prefix):]
        if not suffix:
            target = state                    # run-level mark
        else:
            step_safe = suffix[1:]            # '-<safe step name>'
            name = next((n for n in steps
                         if _safe_name(n) == step_safe), None)
            if name is None:
                continue                      # pending/renamed step: no rec
            target = steps[name]
        old = target.get("review")
        old_at = str(old.get("at") or "") \
            if isinstance(old, dict) else ""
        if str(body.get("at") or "") >= old_at:
            target["review"] = {"reviewed": body.get("reviewed"),
                                "seq": body.get("seq"),
                                "at": body.get("at")}


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


# Bound on the per-step outcome history kept in state.json - enough to
# keep earlier attempts' accomplishments across retries/resumes without
# letting a repeatedly resumed step grow the record forever.
OUTCOME_HISTORY_MAX = 8


def _record_outcome(rec: dict, outcome: str | None, *, status: str,
                    attempt: int | None = None) -> None:
    """Persist a worker-reported OUTCOME line on the step record.

    rec['outcome'] always points at the NEWEST report - the current
    result - while rec['outcomes'] keeps each distinct earlier report
    with the attempt and time that produced it, so a resume's fresher
    outcome never erases what a previous attempt finished. Re-reporting
    the last recorded text just re-stamps that entry (a re-parse of the
    same dispatch, or a legitimately identical report); a call with no
    outcome leaves the prior one in place - a stale-but-real summary
    beats none.
    """
    if not outcome:
        return
    hist = rec.setdefault("outcomes", [])
    ent = {"outcome": outcome, "ended_at": now_iso(), "status": status}
    if attempt is not None:
        ent["attempt"] = attempt
    if hist and hist[-1].get("outcome") == outcome:
        hist[-1].update(ent)
    else:
        hist.append(ent)
        del hist[:-OUTCOME_HISTORY_MAX]
    rec["outcome"] = outcome


def _bump_completion(rec: dict) -> None:
    """Advance completion_seq on a step rec or run state - the review
    workflow's 'a new outcome exists to look at' counter. Bumped once at
    each terminal-outcome write (step end, merge flip, sweep's
    lost-mark, run ended_at); re-saves of the same outcome leave it
    alone so a recorded ack still covers them."""
    rec["completion_seq"] = int(rec.get("completion_seq") or 0) + 1


def _flag_raise(name: str) -> None:
    """Write a stop/drain sentinel. Best-effort: a mkdir/write failure only
    delays the stop by one poll, so callers don't crash on it."""
    try:
        _flag_dir().mkdir(exist_ok=True)
        (_flag_dir() / name).touch()
    except OSError:
        pass


def clear_run_flags(run_dir: Path) -> None:
    """Remove all HALT sentinels for a run - called on resume so a
    previously stopped run can actually start work again. Clears both
    path-keyed names and legacy bare-name ones: a run paused under old
    code must be resumable by new code. Skip sentinels deliberately
    survive: a user's skip stays in force until they Unskip, or a resume
    would silently resurrect a step they removed from the queue. The
    prompt-edit, resume-notes, interrupt-request and merge-claim
    sentinels survive too - the resume is exactly when run_step reads
    them, and a merge in flight keeps its claim."""
    name = _safe_name(run_dir.name)
    pats = [f"*{_flag_key(run_dir)}*",
            f"stop-{name}*", f"drain-{name}", f"pause-{name}",
            f"timeout-{name}-*"]
    try:
        for pat in pats:
            for f in _flag_dir().glob(pat):
                # skip- stays in force until Unskip; prompt-/rnote- are
                # the pending step's edited prompt and resume notes, and
                # intrq- is an unsubmitted-in-flight 'Interrupt + instruct'
                # request - all are meant to survive the resume that is
                # about to read them, so none are halt sentinels.
                # review- marks are durable too: a resume re-completion
                # bumps completion_seq so an old ack simply stops
                # matching - wiping the mark would fabricate 'never
                # reviewed' instead. merge- is a merge-vs-resume claim,
                # not a halt signal - clearing it mid-merge would let a
                # second driver overlap the in-flight operation.
                if f.name.startswith(
                        ("skip-", "prompt-", "rnote-", "intrq-",
                         "review-", "merge-")):
                    continue
                try:
                    f.unlink()
                except OSError:
                    pass
    except OSError:
        pass


TIMEOUT_MAX_EXT = 3        # a live log auto-extends past timeout_min up to
                           # this many extra floor units: ceiling = 4x


def _body_with_notes(base: str | None, notes: str | None) -> str:
    """The dispatch body: the step's own text, then the operator-notes
    block appended under it. Kept tiny because run_step recomputes it
    mid-loop when an interrupt folds fresh text into the notes sentinel."""
    b = base or ""
    if notes and notes.strip():
        b = b.rstrip() + "\n\n" + RESUME_NOTES.format(notes=notes.strip())
    return b


def _intrq_to_notes(run_dir: Path, step: str) -> str | None:
    """Fold a pending 'Interrupt + instruct' request into the step's
    resume-notes sentinel - the single durable channel every dispatch
    already reads, so instructions can be applied exactly once and can
    never double-land on a retry.

    Returns the rendered notes block, or None when no request is pending
    (or the notes write failed). The intrq flag is deleted only AFTER the
    notes write lands: the request's text lives in intrq xor rnote, so a
    restart can neither lose it nor apply it twice."""
    flag = intrq_flag(run_dir, step)
    src = _flag_text(flag)
    if src is None:
        return None
    try:
        req = json.loads(src)
        instructions = str(req.get("instructions") or "").strip()
        attachments = req.get("attachments") or []
        stamp = str(req.get("submitted_at") or "")
    except (ValueError, TypeError):
        # a hand-written or interrupted write left plain text - still
        # deliver it as instructions rather than dropping the request.
        req, instructions, attachments, stamp = {}, src.strip(), [], ""
    lines = [f"INTERRUPT {stamp or now_iso()}: the operator stopped this "
             "step and added the instructions below - they apply IN "
             "ADDITION to the original task.",
             instructions or "(no text - see attachments)"]
    imgs = [a for a in attachments if isinstance(a, dict)]
    if imgs:
        lines.append("IMAGE ATTACHMENTS (absolute paths - Read each one "
                     "to see the screenshot):")
        lines += [f"- {a.get('name') or 'image'}: {a.get('path') or ''}"
                  for a in imgs]
    block = "\n".join(lines)
    notes = rnote_flag(run_dir, step)
    try:
        existing = notes.read_text(encoding="utf-8") \
            if notes.is_file() else ""
        merged = (existing.rstrip() + "\n\n" + block) \
            if existing.strip() else block
        _flag_dir().mkdir(exist_ok=True)
        notes.write_text(merged, encoding="utf-8")
    except OSError:
        return None          # keep intrq - the next dispatch retries the fold
    flag.unlink(missing_ok=True)
    return block


# ---------------------------------------------------------------- devices
# Android device ownership for `device`-declaring steps. The problem this
# closes: adb devices are a SHARED machine resource - before this existed,
# two concurrent mobile steps (two wave members, or two separate runs)
# would each run `adb devices`, pick the same serial, and race (measured in
# run artifacts: workers hardcode `adb -s emulator-5554` and one step's
# cleanup `emu kill` would have killed a sibling's session mid-test).
#
# A step that declares `device` leases exactly one serial for its whole
# life - every attempt, interrupt respawn and the verify gate see the same
# device. The lease is a machine-global O_EXCL file in %TEMP% (same shape
# as .git/conductor-merge.lock): it records owner+worker pids and is
# broken only once, when BOTH are dead - a dead driver's orphaned worker
# may still be on the device, so it keeps the lease until it also exits.

def _device_dir() -> Path:
    """Machine-global lease registry - next to the run sentinels so it
    coordinates across runs AND checkouts (a worktree conductor and the
    main one share one adb server). %TEMP% is also trap-5 safe."""
    return Path(os.environ.get("CONDUCTOR_DEVICE_DIR")
                or Path(tempfile.gettempdir()) / "agent-conductor-devices")


def _adb_argv() -> list[str]:
    """adb command prefix: CONDUCTOR_ADB wins, then PATH, then the standard
    SDK locations - adb is NOT on PATH on this machine (the SDK lives under
    %LOCALAPPDATA%\\Android\\Sdk, same place project tooling looks)."""
    raw = os.environ.get("CONDUCTOR_ADB")
    if raw:
        return shlex.split(raw)
    found = shutil.which("adb")
    if found:
        return [found]
    local = os.environ.get("LOCALAPPDATA")
    for base in (os.environ.get("ANDROID_HOME"),
                 os.environ.get("ANDROID_SDK_ROOT"),
                 str(Path(local) / "Android" / "Sdk") if local else None):
        if not base:
            continue
        cand = Path(base) / "platform-tools" / (
            "adb.exe" if os.name == "nt" else "adb")
        if cand.is_file():
            return [str(cand)]
    return ["adb"]                    # last resort - _adb_run reports None


def _emulator_argv() -> list[str]:
    """emulator command prefix, same discovery order as _adb_argv."""
    raw = os.environ.get("CONDUCTOR_EMULATOR")
    if raw:
        return shlex.split(raw)
    found = shutil.which("emulator")
    if found:
        return [found]
    local = os.environ.get("LOCALAPPDATA")
    for base in (os.environ.get("ANDROID_HOME"),
                 os.environ.get("ANDROID_SDK_ROOT"),
                 str(Path(local) / "Android" / "Sdk") if local else None):
        if not base:
            continue
        cand = Path(base) / "emulator" / (
            "emulator.exe" if os.name == "nt" else "emulator")
        if cand.is_file():
            return [str(cand)]
    return ["emulator"]


def _adb_run(*args: str,
             timeout: float = 15.0) -> subprocess.CompletedProcess | None:
    """One bounded adb call; None when adb can't even be started."""
    try:
        return subprocess.run(
            _adb_argv() + list(args), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout,
            **hidden_subprocess_kwargs())
    except (OSError, subprocess.TimeoutExpired):
        return None


def adb_devices() -> list[tuple[str, str]] | None:
    """(serial, adb-state) pairs from `adb devices`, or None when adb can't
    run/list - callers must treat None as 'unknown', not 'none attached'.
    Only state 'device' is usable ('offline'/'unauthorized' are not)."""
    out = _adb_run("devices")
    if out is None or out.returncode != 0:
        return None
    devs = []
    for line in (out.stdout or "").splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 2:
            devs.append((parts[0], parts[1]))
    return devs


def emulator_avds() -> list[str] | None:
    """`emulator -list-avds` names, or None when no emulator binary exists
    (which also means 'boot is unsupported on this machine')."""
    try:
        out = subprocess.run(
            _emulator_argv() + ["-list-avds"], capture_output=True,
            text=True, encoding="utf-8", errors="replace", timeout=15,
            **hidden_subprocess_kwargs())
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out.returncode != 0:
        return None
    return [ln.strip() for ln in (out.stdout or "").splitlines()
            if ln.strip() and not ln.startswith(("INFO", "WARNING"))]


def _device_class_match(spec: str, serial: str) -> bool:
    if spec == "any":
        return True
    if spec == "emulator":
        return serial.startswith("emulator-")
    if spec == "physical":
        return not serial.startswith("emulator-")
    return serial == spec


def _device_lease_path(serial: str, devdir: Path | None = None) -> Path:
    return (devdir or _device_dir()) / (_safe_name(serial) + ".json")


def _device_lease_info(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8",
                                         errors="replace"))
    except (OSError, ValueError):
        return {}


def _device_lease_live(info: dict) -> bool:
    """A lease is held while its driver OR its recorded worker is alive.
    Both-pids-dead is the stale test - same trust model as the merge lock,
    with the worker pid recorded so a driver-killed mid-step doesn't free
    a device its orphaned worker is still using."""
    if pid_alive(info.get("owner_pid"), info.get("owner_started_at")):
        return True
    return pid_alive(info.get("worker_pid"), info.get("worker_started_at"))


def _proc_image_name(pid: int) -> str | None:
    """Best-effort image name of a pid ('emulator.exe', 'qemu-system-...') -
    the guard that keeps a recycled pid from being killed as 'our'
    emulator. None when undeterminable."""
    try:
        if os.name == "nt":
            out = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                capture_output=True, text=True, timeout=15,
                **hidden_subprocess_kwargs())
            first = (out.stdout or "").strip().splitlines()
            return first[0].split('","')[0].strip('"') or None \
                if first and out.returncode == 0 else None
        out = subprocess.run(["ps", "-p", str(pid), "-o", "comm="],
                             capture_output=True, text=True, timeout=15)
        return (out.stdout or "").strip() or None
    except (OSError, subprocess.TimeoutExpired):
        return None


_EMU_IMAGE_RE = re.compile(r"emulator|qemu", re.I)


def _device_kill_emu(serial: str, pid: int | None, run_dir: Path) -> None:
    """Stop exactly one conductor-booted emulator: `emu kill` on its own
    serial (the adb server drops it cleanly), then a pid-verified taskkill
    fallback. ONLY ever called for instances a lease proves we booted -
    a user's emulator or device is never stopped."""
    _adb_run("-s", serial, "emu", "kill", timeout=20)
    if not pid:
        return
    deadline = time.time() + 20
    while pid_alive(pid) and time.time() < deadline:
        time.sleep(1)
    if pid_alive(pid):
        name = _proc_image_name(pid) or ""
        if _EMU_IMAGE_RE.search(name):
            try:
                if os.name == "nt":
                    subprocess.run(
                        ["taskkill", "/PID", str(pid), "/T", "/F"],
                        capture_output=True, timeout=20,
                        **hidden_subprocess_kwargs())
                else:
                    os.kill(pid, 15)
            except OSError:
                pass


def _device_reap_stale(info: dict, serial: str, run_dir: Path) -> None:
    """About to break a stale lease: if it records a conductor-booted
    emulator whose pid is still up, that orphan is OURS - kill exactly
    that instance so booted emulators don't accumulate behind a dead
    driver. Anything not flagged `booted` is left running."""
    booted = info.get("booted") or {}
    pid = booted.get("emu_pid")
    if pid and pid_alive(pid):
        name = _proc_image_name(pid) or ""
        if _EMU_IMAGE_RE.search(name):
            _device_kill_emu(serial, pid, run_dir)
            log_event(run_dir, event="device_reaped", serial=serial, pid=pid)


class _DeviceLease:
    """One adb serial owned machine-wide for a step's lifetime."""

    def __init__(self, path: Path, serial: str, token: str, step: str,
                 booted: dict | None = None):
        self.path = path
        self.serial = serial
        self.token = token
        self.step = step
        self.booted = booted            # {avd, port, emu_pid} when we booted
        self.avd_slot: _DeviceLease | None = None


def _self_started_iso() -> str | None:
    """This process's own creation time as an ISO stamp - written as a
    lease's owner_started_at so the pid_alive creation-window check sees
    'the process born at that stamp', not 'the process that reached
    cmd_run >5s after python.exe spawned' (a slow interpreter start would
    otherwise mark a LIVE owner dead and let a second conductor steal the
    serial). None = undeterminable -> pure liveness, merge-lock behavior."""
    if os.name != "nt":
        return None
    try:
        import ctypes
        k32 = ctypes.windll.kernel32
        h = k32.OpenProcess(0x1000, False, os.getpid())
        if not h:
            return None
        try:
            epoch = _proc_started_epoch(h)
        finally:
            k32.CloseHandle(h)
        return dt.datetime.fromtimestamp(epoch).isoformat() \
            if epoch else None
    except (OSError, AttributeError):
        return None


def _device_try_lease(serial: str, run_dir: Path, state: dict, step: Step,
                      booted: dict | None = None) -> _DeviceLease | None:
    """Take the machine-wide lease on `serial`; None when a live owner
    holds it. A stale lease (owner+worker pids both dead) is reaped then
    broken once."""
    path = _device_lease_path(serial)
    token = secrets.token_hex(8)
    for _attempt in (0, 1):
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            info = _device_lease_info(path)
            if not info.get("token"):
                # A creator is mid-write between O_EXCL and its payload, or
                # died there: fresh empty files count as HELD, only a file
                # that stayed broken past a grace counts as abandoned.
                try:
                    if time.time() - path.stat().st_mtime < 10:
                        return None
                except OSError:
                    return None
            elif _device_lease_live(info):
                return None
            _device_reap_stale(info, serial, run_dir)
            try:
                path.unlink(missing_ok=True)
            except OSError:
                return None            # someone else broke it first
            continue
        except OSError:
            return None
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps({
                "serial": serial, "token": token,
                "spec": step.device, "run_dir": str(run_dir),
                "run_key": _flag_key(run_dir), "step": step.name,
                "owner_pid": os.getpid(),
                "owner_started_at": _self_started_iso()
                                    or state.get("driver_started_at")
                                    or state.get("started_at"),
                "worker_pid": None, "worker_started_at": None,
                "leased_at": now_iso(), "booted": booted}))
        return _DeviceLease(path, serial, token, step.name, booted=booted)
    return None


def _device_lease_set_worker(lease: _DeviceLease, pid: int) -> None:
    """Record the spawned worker pid so a dead driver doesn't free the
    device out from under its orphaned worker."""
    try:
        info = _device_lease_info(lease.path)
        if info.get("token") != lease.token:
            return                    # broken + re-leased by someone else
        info["worker_pid"] = pid
        info["worker_started_at"] = now_iso()
        lease.path.write_text(json.dumps(info), encoding="utf-8")
    except OSError:
        pass


def _device_release(lease: _DeviceLease | None, run_dir: Path) -> None:
    """Free a step's device: a conductor-booted emulator dies FIRST (the
    serial never re-leases mid-kill), then the leases unlink - token-checked
    so a broken-then-reattaken file is never unlinked from under its new
    owner."""
    if lease is None:
        return
    if lease.booted:
        _device_kill_emu(lease.serial, lease.booted.get("emu_pid"), run_dir)
    # Logged BEFORE the unlinks so the event stream's acq>=rel ordering is
    # the real serialization boundary - a waiter can only acquire after
    # the serial file is gone.
    log_event(run_dir, event="device_released", step=lease.step,
              serial=lease.serial, booted=bool(lease.booted))
    slot = lease.avd_slot
    for path, token in ((lease.path, lease.token),
                        (slot.path, slot.token) if slot else (None, None)):
        if not path:
            continue
        try:
            cur = _device_lease_info(path)
        except OSError:
            cur = {}
        if cur.get("token") == token:
            path.unlink(missing_ok=True)


def _device_holder_desc(path: Path) -> str:
    info = _device_lease_info(path)
    owner = Path(info.get("run_dir") or "?").name
    return f"{owner}/{info.get('step', '?')}"


def _running_avd_names(attached: list[str]) -> set[str]:
    """AVD names already running as emulator-* serials - prevents booting a
    second instance of an AVD a user (or another conductor) already runs."""
    names: set[str] = set()
    for s in attached:
        if not s.startswith("emulator-"):
            continue
        out = _adb_run("-s", s, "emu", "avd", "name", timeout=10)
        if out is not None and out.returncode == 0:
            lines = (out.stdout or "").splitlines()
            if lines and lines[0].strip() and lines[0].strip() != "OK":
                names.add(lines[0].strip())
    return names


def _free_emu_port(attached: list[str],
                   devdir: Path | None = None) -> int | None:
    """Lowest free even console port: unused by attached emulators AND by
    lease files (a leased serial may be mid-boot, invisible to adb yet)."""
    used = {int(m.group(1)) for s in attached
            if (m := re.fullmatch(r"emulator-(\d+)", s))}
    try:
        for f in (devdir or _device_dir()).glob("emulator-*.json"):
            m = re.fullmatch(r"emulator-(\d+)\.json", f.name)
            if m:
                used.add(int(m.group(1)))
    except OSError:
        pass
    for p in DEVICE_EMU_PORTS:
        if p not in used:
            return p
    return None


def _spawn_emulator(avd: str, port: int, log_path: Path):
    """Popen the emulator detached-ish: it must outlive this function but
    is reaped via the lease's booted record if the driver dies."""
    log = open(log_path, "ab")
    return subprocess.Popen(
        _emulator_argv() + ["-avd", avd, "-port", str(port), "-no-boot-anim"],
        stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
        env=clean_devin_env(), **hidden_subprocess_kwargs())


def _wait_emu_boot(serial: str, proc,
                   timeout: float = DEVICE_BOOT_S) -> bool:
    """True once `serial` is attached AND sys.boot_completed=1."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            return False
        st = dict(adb_devices() or {}).get(serial)
        if st == "device":
            out = _adb_run("-s", serial, "shell", "getprop",
                           "sys.boot_completed", timeout=10)
            if out is not None and out.returncode == 0 \
                    and (out.stdout or "").strip() == "1":
                return True
        time.sleep(3)
    return False


def _device_try_boot(run_dir: Path, state: dict, step: Step,
                     tried: set[str], attached: list[str],
                     ) -> _DeviceLease | None:
    """Boot a free AVD into its own emulator-<port> serial and lease it -
    the 'separate local emulator instances' path, used only where tooling
    supports it (emulator binary + defined AVDs). An `avd-<name>` slot
    lease keeps two conductors from booting the same image at once."""
    avds = emulator_avds()
    if not avds:
        return None
    running = _running_avd_names(attached)
    for avd in avds:
        if avd in tried or avd in running:
            continue
        slot = _device_try_lease(f"avd-{avd}", run_dir, state, step)
        if slot is None:
            continue                    # another conductor is booting it
        port = _free_emu_port(attached)
        if port is None:
            _device_release(slot, run_dir)
            break
        tried.add(avd)
        log_path = run_dir / f"{step.name}.emulator.log"
        proc = None
        try:
            proc = _spawn_emulator(avd, port, log_path)
        except OSError as exc:
            LOG.warning("emulator boot failed for %s: %s", avd, exc)
        serial = f"emulator-{port}"
        if proc is not None and _wait_emu_boot(serial, proc):
            lease = _device_try_lease(
                serial, run_dir, state, step,
                booted={"avd": avd, "port": port, "emu_pid": proc.pid})
            if lease is not None:
                lease.avd_slot = slot
                log_event(run_dir, event="device_booted", step=step.name,
                          avd=avd, serial=serial, port=port)
                return lease
            _device_kill_emu(serial, proc.pid, run_dir)   # raced away
        elif proc is not None:
            _device_kill_emu(serial, proc.pid, run_dir)
        _device_release(slot, run_dir)
    return None


def _device_wait_acquire(step: Step, run_dir: Path, state: dict,
                         slug: str) -> _DeviceLease | str:
    """Block until this step owns a device lease; return it, or a terminal
    status string ('skipped'/'blocked') already recorded in state.

    Queue semantics: a busy-but-present device is waited on uncapped (it
    frees when its step ends or its owner dies) - 'queuing when only a
    shared device is available'. A device that isn't attached at all gets
    device_wait_s to appear (a sibling's boot, a phone getting plugged in)
    before the step records 'blocked'. Halt sentinels work while queued.
    """
    spec = step.device or "any"
    t_wait = time.time()
    absent_since = t_wait
    tried_avds: set[str] = set()
    last_note = None
    last_save = 0.0
    wait_logged = False
    while True:
        # Halt controls apply while queued too - a step that never spawned
        # records 'skipped' exactly like the pre-spawn flag check.
        halt = _flag_set(stop_flag(run_dir), stop_flag(run_dir, step.name),
                         pause_flag(run_dir), drain_flag(run_dir))
        skipped = halt is None and skip_flag(run_dir, step.name).is_file()
        if halt is not None or skipped:
            why = ("skipped by user (Skip step)" if skipped else
                   "run paused" if pause_flag(run_dir).is_file() else
                   "run drained" if drain_flag(run_dir).is_file()
                   else "stopped by user")
            with STATE_LOCK:
                state["steps"].setdefault(step.name, {}).update(
                    name=step.name, slug=slug, status="skipped",
                    ended_at=now_iso(),
                    note=f"{why} while waiting for a device")
                save_state(run_dir, state)
            log_event(run_dir, event="step_stopped", step=step.name,
                      phase="device_wait")
            return "skipped"

        devs = adb_devices()
        attached = sorted(s for s, st in (devs or []) if st == "device")
        if spec in ("any", "emulator", "physical"):
            cand = [s for s in attached if _device_class_match(spec, s)]
        else:
            cand = [spec] if spec in attached else []

        lease = None
        for serial in cand:
            lease = _device_try_lease(serial, run_dir, state, step)
            if lease is not None:
                log_event(run_dir, event="device_acquired",
                          step=step.name, serial=serial, spec=spec,
                          waited_s=round(time.time() - t_wait, 1))
                return lease

        # Separate emulator instances only where tooling supports them:
        # boot a free AVD when no attached candidate was leasable. Never
        # while adb itself is down - a fresh emulator can't even register.
        boot_wanted = devs is not None and (
            (spec == "emulator" and step.device_boot is not False)
            or (spec == "any" and step.device_boot))
        if boot_wanted:
            lease = _device_try_boot(run_dir, state, step, tried_avds,
                                     attached)
            if lease is not None:
                log_event(run_dir, event="device_acquired",
                          step=step.name, serial=lease.serial, spec=spec,
                          waited_s=round(time.time() - t_wait, 1),
                          booted=True)
                return lease

        if cand:
            if not wait_logged:
                wait_logged = True
                log_event(run_dir, event="device_wait", step=step.name,
                          spec=spec, reason="leased", candidates=cand)
            holders = ", ".join(
                _device_holder_desc(_device_lease_path(s)) for s in cand)
            note = (f"waiting for Android device ({spec}): "
                    f"{', '.join(cand)} held by {holders} - queued")
            absent_since = time.time()      # exists, just busy: no deadline
        else:
            if not wait_logged:
                wait_logged = True
                log_event(run_dir, event="device_wait", step=step.name,
                          spec=spec, reason="absent")
            note = ("waiting for Android device "
                    f"({spec}): adb unavailable"
                    if devs is None else
                    f"waiting for Android device ({spec}): "
                    "no matching device attached")
            if step.device_wait_s and \
                    time.time() - absent_since >= step.device_wait_s:
                msg = (f"no Android device matching {spec!r} appeared "
                       f"within {step.device_wait_s:g}s")
                with STATE_LOCK:
                    state["steps"].setdefault(step.name, {}).update(
                        name=step.name, slug=slug, status="blocked",
                        ended_at=now_iso(), device_wait=spec, note=msg)
                    save_state(run_dir, state)
                log_event(run_dir, event="device_blocked",
                          step=step.name, spec=spec,
                          waited_s=round(time.time() - t_wait, 1))
                return "blocked"

        with STATE_LOCK:
            rec = state["steps"].setdefault(step.name,
                                            {"name": step.name})
            rec.update(slug=slug, status="queued", device_wait=spec,
                       device_waited_s=round(time.time() - t_wait, 1),
                       note=note)
            if note != last_note \
                    or time.time() - last_save >= DEVICE_WAIT_SAVE_S:
                save_state(run_dir, state)
                last_save = time.time()
                last_note = note
        time.sleep(DEVICE_POLL_S)


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


# ------------------------------------------------------- worker service ports
# A worker that needs a dev server (vite, `npm run dev`, `conductor.py
# serve` on this repo) must never collide with the main workspace's
# listeners or a sibling run's - an isolated checkout still shares the
# port space. Each dispatched LOCAL step leases one loopback port for its
# whole lifetime; the worker learns it via prompt text + CONDUCTOR_* env
# vars. Leases are files under %TEMP%/agent-conductor-ports (same pattern
# as the flag sentinels - trap 5: never inside the run dir): the O_EXCL
# create is the atomic claim between concurrent conductors, the socket
# bind probe is the truth for processes that never took a lease, and the
# recorded owner pid (with its creation stamp) makes a dead conductor's
# leftover leases reclaimable instead of leaking ports forever.

SERVICE_PORT_MIN = 8806     # just past the C:\Projects registry's last
SERVICE_PORT_MAX = 8859     # assigned port (8805) - never hand a worker a
                            # registered project port, even a free one
# A lease file younger than this that is not yet parseable belongs to an
# allocator mid-claim (O_EXCL create -> write is a two-step window), not a
# corrupt leftover - leave it alone.
LEASE_CLAIM_GRACE_S = 5.0


def _port_lease_dir() -> Path:
    return Path(tempfile.gettempdir()) / "agent-conductor-ports"


def _port_free(port: int) -> bool:
    """OS truth: can anything bind 127.0.0.1:port right now?"""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False


def _own_pid_stamp() -> str | None:
    """ISO stamp of THIS process's creation - stored in the lease as
    `pid_created` so pid_alive()'s reuse window rejects a recycled pid
    instead of trusting the number. Windows-only probe; posix callers get
    None and rely on plain liveness."""
    if os.name != "nt":
        return None
    try:
        import ctypes
        k32 = ctypes.windll.kernel32
        h = k32.OpenProcess(0x1000, False, os.getpid())
        if not h:
            return None
        try:
            ep = _proc_started_epoch(h)
        finally:
            k32.CloseHandle(h)
        return (dt.datetime.fromtimestamp(ep).isoformat(timespec="seconds")
                if ep else None)
    except Exception:
        return None


def _lease_owner_alive(lease: Path) -> bool:
    """Is the conductor that claimed this lease still running? A lease is
    live while its owner pid is alive (creation-stamp guarded); unreadable
    or dead-owner leases are stale. A fresh unreadable file is a claim in
    flight - treated as live for LEASE_CLAIM_GRACE_S."""
    try:
        if time.time() - lease.stat().st_mtime < LEASE_CLAIM_GRACE_S:
            return True
        info = json.loads(lease.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    pid = info.get("pid")
    return bool(isinstance(pid, int) and pid > 0
                and pid_alive(pid, info.get("pid_created")))


def alloc_service_port(owner: dict | None = None,
                       lease_dir: Path | None = None) -> int | None:
    """Lease a distinct loopback port for one step's worker. Returns the
    port, or None when the range is exhausted (the step proceeds without
    one - a missing port never fails a run).

    Order: claim the lease file atomically FIRST (O_EXCL arbitrates between
    concurrent conductors), THEN verify the port is bindable (a foreign
    listener - the main dashboard, another project's server - is never
    ours to take, so the claim is dropped and the scan moves on).
    `owner` is stored in the lease for review; pid/pid_created are filled
    in here so staleness survives a crashed conductor.
    """
    ld = lease_dir or _port_lease_dir()
    rec = {"pid": os.getpid(), "pid_created": _own_pid_stamp(),
           "created_at": now_iso(), **(owner or {})}
    try:
        ld.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass                       # bind probe still works without leases
    for port in range(SERVICE_PORT_MIN, SERVICE_PORT_MAX + 1):
        lease = ld / f"svc-{port}.json"
        try:
            if lease.is_file():
                if _lease_owner_alive(lease):
                    continue
                lease.unlink(missing_ok=True)   # stale - reclaim the port
        except OSError:
            continue
        try:
            fd = os.open(str(lease),
                         os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except OSError:
            continue             # live lease, or a racing conductor won it
        if not _port_free(port):
            # A foreign listener holds the port - never ours to take:
            # close the claim fd first, then drop the lease file (Windows
            # refuses to unlink an open file).
            try:
                os.close(fd)
            except OSError:
                pass
            try:
                lease.unlink(missing_ok=True)
            except OSError:
                pass
            continue
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(json.dumps(rec))
        except OSError:
            try:
                os.close(fd)
            except OSError:
                pass
            try:
                lease.unlink(missing_ok=True)
            except OSError:
                pass
            continue
        return port
    return None


def release_service_port(port: int | None,
                         lease_dir: Path | None = None) -> bool | None:
    """Drop the step's lease at step end. Returns None when there was no
    port, True when the port is bindable afterward, False when something
    still holds it - almost always a dev server the worker left running
    (kill_tree already took its process tree; a survivor is a detached
    child, which no parent-scoped kill can reach). We report rather than
    hunt the listener: binding a port proves nothing about WHO owns the
    replacement, so a port-scoped kill could hit a foreign service."""
    if port is None:
        return None
    try:
        (lease_dir or _port_lease_dir()
         ).joinpath(f"svc-{port}.json").unlink(missing_ok=True)
    except OSError:
        pass
    return _port_free(port)


def sweep_service_ports(lease_dir: Path | None = None) -> list[str]:
    """Reclaim lease files whose owner conductor is dead (crashed/killed
    mid-step). A live owner's lease stays; a still-bound port stays
    untouched - sweeping drops bookkeeping, never listeners."""
    ld = lease_dir or _port_lease_dir()
    out: list[str] = []
    try:
        leases = sorted(ld.glob("svc-*.json"))
    except OSError:
        return out
    for lease in leases:
        if _lease_owner_alive(lease):
            continue
        try:
            lease.unlink()
            out.append(f"service port lease {lease.name} reclaimed "
                       "(dead owner)")
        except OSError:
            pass
    return out


def env_service_port() -> int | None:
    """The port leased to the worker this process runs under, if any.
    `conductor.py serve` honors it so a dashboard launched from a worktree
    worker binds the step's own port instead of the main workspace's."""
    try:
        p = int(os.environ.get("CONDUCTOR_SERVICE_PORT") or 0)
        return p if 0 < p < 65536 else None
    except ValueError:
        return None


def _service_note(work_dir: Path, repo_dir: Path,
                  port: int | None) -> str:
    """Worker-context block rendered into the prompt's {service_note}
    slot: which checkout is this step's (a worktree step must not touch
    the shared checkout it will merge into) and which loopback port is
    leased to it. Empty when there is nothing extra to declare."""
    parts: list[str] = []
    if os.path.normcase(str(work_dir.resolve())) != \
            os.path.normcase(str(repo_dir.resolve())):
        parts.append(
            f"Work ONLY inside {work_dir} - it is this step's isolated "
            f"git worktree. The shared checkout at {repo_dir} is the "
            f"merge target other agents may be using: never write to it, "
            f"launch servers from it, or kill its processes.")
    if port is not None:
        parts.append(
            f"A loopback port is leased to this step alone: {port} "
            f"(exported as $CONDUCTOR_SERVICE_PORT; base URL "
            f"http://127.0.0.1:{port} in $CONDUCTOR_SERVICE_URL). If the "
            f"task needs a local dev server or dashboard, bind it there - "
            f"not to a project's registered/default port - and never take "
            f"over or kill a listener you did not spawn: it belongs to "
            f"the main workspace or another run.")
    return ("\n" + "\n\n".join(parts) + "\n") if parts else ""


# --------------------------------------------------------- agent slot pool
# run.max_parallel bounds ONE manifest; the slot pool bounds the MACHINE -
# the same O_EXCL lease pattern as devices/ports, machine-global so N
# concurrent runs (dashboard launches, CLI runs, worktree drivers) share
# one ceiling instead of multiplying load. Every dispatched step holds one
# slot for its whole run_step() life - attempts and interrupt respawns
# included - and a merge-resolver session (spawned via run_step) counts
# too. A step that finds no free slot waits as 'queued' holding NOTHING -
# queued workers never block a running one, so the pool always drains.

DEFAULT_AGENT_CAP = 8     # this box already runs 4 sessions comfortably;
                          # 8 is the accepted default ceiling
SLOT_POLL_S = 5.0         # cadence of the slot-wait loop
SLOT_WAIT_SAVE_S = 30.0   # state.json heartbeat cadence while queued


def _slot_dir() -> Path:
    """Machine-global agent-slot registry - %TEMP% like the other leases
    (trap-5 safe, shared across checkouts and runs)."""
    return Path(os.environ.get("CONDUCTOR_SLOT_DIR")
                or Path(tempfile.gettempdir()) / "agent-conductor-slots")


MAX_AGENT_CAP = 32        # sanity ceiling on the live cap control -
                          # the box physically cannot use more


def _slot_cap_file() -> Path:
    return _slot_dir() / "cap.json"


def agent_slot_cap() -> int:
    """Machine-wide concurrent-agent ceiling. CONDUCTOR_MAX_AGENTS wins;
    then the dashboard's live cap file (POST /api/pool writes it - every
    driver re-reads it each slot poll, so a change lands mid-wait);
    otherwise 4..8 scaled to the box (never below 4 - four parallel
    sessions are proven fine on this machine)."""
    raw = os.environ.get("CONDUCTOR_MAX_AGENTS")
    if raw:
        try:
            n = int(raw)
            if n >= 1:
                return n
        except ValueError:
            pass
    try:
        n = int(json.loads(
            _slot_cap_file().read_text(encoding="utf-8"))["cap"])
        if n >= 1:
            return min(n, MAX_AGENT_CAP)
    except (OSError, ValueError, TypeError, KeyError):
        pass
    return max(4, min(DEFAULT_AGENT_CAP, os.cpu_count() or 8))


def set_agent_cap(n: int) -> dict:
    """POST /api/pool: persist the machine-wide cap; drivers pick it up
    within SLOT_POLL_S. Lowering never kills running agents - it only
    stops queued steps from leasing."""
    if not isinstance(n, int) or isinstance(n, bool)             or not 1 <= n <= MAX_AGENT_CAP:
        raise RequestError(f"cap must be an integer 1..{MAX_AGENT_CAP}")
    f = _slot_cap_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    tmp = f.with_suffix(".tmp")
    tmp.write_text(json.dumps({"cap": n, "set_at": now_iso()}),
                   encoding="utf-8")
    os.replace(tmp, f)
    return _slots_view()


def _slot_info(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8",
                                         errors="replace"))
    except (OSError, ValueError):
        return {}


def _slot_live(info: dict) -> bool:
    """Same trust model as device leases: a slot is held while its driver
    OR its recorded worker is alive - a dead driver's orphaned worker is
    still a running agent and keeps the slot until it exits."""
    if not info.get("token"):
        return False
    if pid_alive(info.get("owner_pid"), info.get("owner_started_at")):
        return True
    return pid_alive(info.get("worker_pid"), info.get("worker_started_at"))


def _slot_holders(slot_dir: Path | None = None) -> list[dict]:
    """Live lease payloads (a lease still being written counts as held)."""
    sd = slot_dir or _slot_dir()
    out: list[dict] = []
    try:
        files = sorted(sd.glob("slot-*.json"))
    except OSError:
        return out
    for f in files:
        info = _slot_info(f)
        if not info.get("token"):
            try:
                if time.time() - f.stat().st_mtime < LEASE_CLAIM_GRACE_S:
                    out.append({"holder": "claiming"})
            except OSError:
                pass
            continue
        if _slot_live(info):
            out.append(info)
    return out


def _slot_holder_desc(info: dict) -> str:
    owner = Path(info.get("run_dir") or "?").name
    return f"{owner}/{info.get('step', '?')}"


@dataclasses.dataclass
class _AgentSlot:
    path: Path
    token: str
    step: str
    index: int


def _slot_try_lease(i: int, run_dir: Path, state: dict, step: Step,
                    slot_dir: Path | None = None) -> _AgentSlot | None:
    """Take agent slot i; None when a live owner holds it. A stale lease
    (owner+worker pids both dead) is broken once and retaken."""
    sd = slot_dir or _slot_dir()
    path = sd / f"slot-{i}.json"
    token = secrets.token_hex(8)
    for _attempt in (0, 1):
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            info = _slot_info(path)
            if not info.get("token"):
                # mid-claim window (O_EXCL create before the payload
                # write) counts as held for a grace period, same as the
                # device leases
                try:
                    if time.time() - path.stat().st_mtime < \
                            LEASE_CLAIM_GRACE_S:
                        return None
                except OSError:
                    return None
            elif _slot_live(info):
                return None
            try:
                path.unlink(missing_ok=True)
            except OSError:
                return None            # someone else broke it first
            continue
        except OSError:
            return None
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps({
                "slot": i, "token": token,
                "run_dir": str(run_dir), "run_key": _flag_key(run_dir),
                "run": state.get("run"), "step": step.name,
                "owner_pid": os.getpid(),
                "owner_started_at": _self_started_iso()
                                    or state.get("driver_started_at")
                                    or state.get("started_at"),
                "worker_pid": None, "worker_started_at": None,
                "leased_at": now_iso()}))
        return _AgentSlot(path, token, step.name, i)
    return None


def _slot_set_worker(slot: _AgentSlot, pid: int) -> None:
    """Record the spawned worker pid so a dead driver doesn't free the
    slot out from under its orphaned session."""
    try:
        info = _slot_info(slot.path)
        if info.get("token") != slot.token:
            return                    # broken + re-leased by someone else
        info["worker_pid"] = pid
        info["worker_started_at"] = now_iso()
        slot.path.write_text(json.dumps(info), encoding="utf-8")
    except OSError:
        pass


def _slot_release(slot: _AgentSlot | None, run_dir: Path) -> None:
    if slot is None:
        return
    try:
        log_event(run_dir, event="slot_released", step=slot.step,
                  slot=slot.index)
    except OSError:
        pass                 # run dir gone mid-teardown: the lease
    try:                     # unlink below is the part that matters
        if _slot_info(slot.path).get("token") == slot.token:
            slot.path.unlink(missing_ok=True)
    except OSError:
        pass


def sweep_agent_slots(slot_dir: Path | None = None) -> list[str]:
    """Reclaim slot files whose owner AND worker are both dead (a crashed
    conductor would otherwise cap the pool forever)."""
    sd = slot_dir or _slot_dir()
    out: list[str] = []
    try:
        files = sorted(sd.glob("slot-*.json"))
    except OSError:
        return out
    for f in files:
        info = _slot_info(f)
        if not info.get("token"):
            try:
                if time.time() - f.stat().st_mtime < LEASE_CLAIM_GRACE_S:
                    continue
            except OSError:
                continue
        elif _slot_live(info):
            continue
        try:
            f.unlink()
            out.append(f"agent slot {f.name} reclaimed (dead owner)")
        except OSError:
            pass
    return out


def _slots_view(slot_dir: Path | None = None) -> dict:
    """The /api/state pool summary: how many machine-wide agent slots are
    held right now and by whom (run:step names only - never task text)."""
    holders = _slot_holders(slot_dir)
    return {"cap": agent_slot_cap(), "in_use": len(holders),
            "holders": [{"run": h.get("run"),
                         "run_dir": Path(h["run_dir"]).name
                         if h.get("run_dir") else None,
                         "step": h.get("step")}
                        for h in holders]}


def _agent_slot_acquire(step: Step, run_dir: Path, state: dict,
                        slug: str,
                        slot_dir: Path | None = None) -> _AgentSlot | str:
    """Block until this step owns a machine-wide agent slot; return the
    lease, or a terminal 'skipped' already recorded in state.

    Queue semantics: the wait is uncapped because it can never deadlock -
    a waiting step holds no slot, and every holder is a worker bounded by
    its own timeout. Halt/skip sentinels work while queued. The step
    record carries slot_wait=True + a note naming the holders so the
    dashboard queue shows who's ahead."""
    t_wait = time.time()
    last_save = 0.0
    last_note = None
    wait_logged = False
    while True:
        # re-read every poll: POST /api/pool changes the cap mid-wait -
        # a raise frees this step on the next tick, a drop just keeps it
        # queued (running agents are never preempted)
        cap = agent_slot_cap()
        halt = _flag_set(stop_flag(run_dir), stop_flag(run_dir, step.name),
                         pause_flag(run_dir), drain_flag(run_dir))
        skipped = halt is None and skip_flag(run_dir, step.name).is_file()
        if halt is not None or skipped:
            why = ("skipped by user (Skip step)" if skipped else
                   "run paused" if pause_flag(run_dir).is_file() else
                   "run drained" if drain_flag(run_dir).is_file()
                   else "stopped by user")
            with STATE_LOCK:
                state["steps"].setdefault(step.name, {}).update(
                    name=step.name, slug=slug, status="skipped",
                    ended_at=now_iso(), slot_wait=None,
                    note=f"{why} while queued for an agent slot")
                save_state(run_dir, state)
            log_event(run_dir, event="step_stopped", step=step.name,
                      phase="slot_wait")
            return "skipped"

        for i in range(1, cap + 1):
            slot = _slot_try_lease(i, run_dir, state, step, slot_dir)
            if slot is not None:
                log_event(run_dir, event="slot_acquired", step=step.name,
                          slot=i,
                          waited_s=round(time.time() - t_wait, 1))
                return slot

        holders = _slot_holders(slot_dir)
        who = ", ".join(_slot_holder_desc(h) for h in holders[:4])
        if len(holders) > 4:
            who += f", +{len(holders) - 4} more"
        note = (f"queued for an agent slot: {len(holders)}/{cap} in use"
                + (f" ({who})" if who else ""))
        if not wait_logged:
            wait_logged = True
            log_event(run_dir, event="slot_wait", step=step.name,
                      in_use=len(holders), cap=cap)
        with STATE_LOCK:
            rec = state["steps"].setdefault(step.name,
                                            {"name": step.name})
            rec.update(slug=slug, status="queued", slot_wait=True,
                       slot_waited_s=round(time.time() - t_wait, 1),
                       note=note)
            if note != last_note \
                    or time.time() - last_save >= SLOT_WAIT_SAVE_S:
                save_state(run_dir, state)
                last_save = time.time()
                last_note = note
        time.sleep(SLOT_POLL_S)


def run_step(step: Step, slug: str, work_dir: Path, cfg: Manifest,
             run_dir: Path, devin_argv: list[str], state: dict,
             do_checkpoint: bool = True, branch: str | None = None,
             post_commit: bool = True,
             resume_note: str | None = None,
             repo_dir: Path | None = None) -> str:
    """Run one step; return final status: done|partial|blocked|failed|timed_out
    |stopped|skipped.

    work_dir is where the session runs and where checkpoints/verify happen -
    the project dir, or a git worktree for parallel steps. repo_dir is the
    project's own checkout (defaults to work_dir); for worktree steps it is
    the shared checkout the branch merges into - the worker is told it is
    off-limits. When a worktree is
    used the caller owns the pre-step checkpoint (on the main checkout), so
    do_checkpoint=False. post_commit=False skips the post-step `git add -A &&
    commit` sweep - required for the merge resolver, whose work_dir sits in
    an in-progress merge where a blind commit could adopt conflict markers.

    Every local (non-cloud) step leases a loopback service port for its
    whole duration: the prompt + CONDUCTOR_* env vars tell the worker to
    bind any dev server there rather than a default/registered port, and
    the lease is released when the step ends. The port lands in the step
    record so the dashboard can show which service address the worker used.

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

    An 'Interrupt + instruct' sentinel (intrq-*, a JSON request) polls in
    the same slot as a stop flag but is NOT a stop: firing alone it kills
    the worker, folds the operator's instructions into the rnote-*
    channel, and respawns the SAME step (no attempt spent) under
    INTERRUPT_NOTE - the killed worker's product is committed first, the
    worktree/work_dir and accumulated changes are kept. A request that
    lands after the turn already ended (STATUS printed, verify running,
    or a real stop flag also firing) never respawns - it banks into the
    notes channel for the next dispatch instead. The sentinel is only
    deleted once its content is banked, so a driver restart mid-
    transition can neither lose the request nor apply it twice.
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
            rec = state["steps"].setdefault(step.name, {})
            # a crashed prior dispatch may have left its interval open -
            # close it at evidence before stamping this skip
            _iv_close(rec, now_iso(), run_dir=run_dir)
            rec.update(
                name=step.name, slug=slug, status="skipped",
                wt=step.worktree, cloud=step.cloud, ended_at=now_iso(),
                note=note + " before the session spawned")
            save_state(run_dir, state)
        log_event(run_dir, event="step_stopped", step=step.name,
                  phase="before_spawn", user_skip=user_skip)
        return "skipped"

    body_base = step.prompt
    if body_base is None and step.prompt_file:
        try:
            body_base = (cfg.path.parent / step.prompt_file).read_text(
                encoding="utf-8")
        except OSError:
            body_base = step.prompt_file

    # Dashboard prompt edits / resume notes / interrupt requests are
    # tempdir sentinels (trap-5 safe): an override replaces the task body
    # for every dispatch while it exists; notes append operator context
    # under it. All are durable (clear_run_flags leaves them) so they
    # also apply on resume. A banked 'Interrupt + instruct' request -
    # driver died mid-transition, or a hard stop landed first - folds
    # into the notes channel BEFORE they are read, so the instructions
    # reach this dispatch exactly once.
    prompt_edited = False
    try:
        over = prompt_flag(run_dir, step.name)
        if over.is_file():
            body_base = over.read_text(encoding="utf-8")
            prompt_edited = True
    except OSError:
        pass
    intr_folded = _intrq_to_notes(run_dir, step.name) is not None
    rnotes = _flag_text(rnote_flag(run_dir, step.name))
    body_src = _body_with_notes(body_base, rnotes)

    # Android device lease: a `device`-declaring step owns exactly one adb
    # serial for its whole life - every attempt, interrupt respawn and the
    # verify gate see the same device, and a busy device queues the step
    # (status 'queued') instead of racing a sibling for it. A device that
    # never appears ends the step 'blocked' before any session spawns.
    device_lease: _DeviceLease | None = None
    if step.device:
        got = _device_wait_acquire(step, run_dir, state, slug)
        if not isinstance(got, _DeviceLease):
            return got                # 'skipped'/'blocked' already recorded
        device_lease = got
    # The device note hands the worker the relaunch/dialog helper that
    # ships next to this script - a worktree checkout points at its own
    # copy, and the path is absolute so it works from the project cwd.
    helper_path = Path(__file__).resolve().parent / "mobile_device.py"
    device_note = (DEVICE_NOTE.format(serial=device_lease.serial,
                                      helper=str(helper_path))
                   if device_lease else "")
    if device_lease is not None:
        # The dashboard exposes queued steps as editable - a prompt edit,
        # resume note or interrupt that landed DURING the device wait must
        # still reach this dispatch exactly once, so re-fold all three.
        intr_folded = _intrq_to_notes(run_dir, step.name) is not None \
            or intr_folded
        try:
            over = prompt_flag(run_dir, step.name)
            if over.is_file():
                body_base = over.read_text(encoding="utf-8")
                prompt_edited = True
        except OSError:
            pass
        rnotes = _flag_text(rnote_flag(run_dir, step.name))
        body_src = _body_with_notes(body_base, rnotes)

    # Machine-wide agent slot: the pool bounds concurrent sessions across
    # ALL runs, not just this manifest's max_parallel. Acquired after the
    # device lease - a device-waiter hasn't spawned a session, so it must
    # not count against the agent pool (the slot is the compute claim);
    # a slot-waiter keeps its device, it's committed to run next. A 'skip'
    # landing mid-queue releases the device before returning.
    agent_slot = _agent_slot_acquire(step, run_dir, state, slug)
    if not isinstance(agent_slot, _AgentSlot):
        _device_release(device_lease, run_dir)
        return agent_slot          # 'skipped' recorded while queued
    # The slot wait is over - re-fold prompt overrides/notes/interrupts
    # that landed while queued, exactly like the device-wait refold above.
    intr_folded = _intrq_to_notes(run_dir, step.name) is not None \
        or intr_folded
    try:
        over = prompt_flag(run_dir, step.name)
        if over.is_file():
            body_base = over.read_text(encoding="utf-8")
            prompt_edited = True
    except OSError:
        pass
    rnotes = _flag_text(rnote_flag(run_dir, step.name))
    body_src = _body_with_notes(body_base, rnotes)
    # Other live slot holders mean sibling sessions ARE running right now
    # (this run's pool members, other runs, resolvers) - the worker gets
    # the shared-machine rules then.
    parallel_note = PARALLEL_NOTE if (
        cfg.parallel or step.wave is not None or len(_slot_holders()) > 1
        or step.name.startswith("resolve-")) else ""

    repo_dir = repo_dir or work_dir
    # Lease this step's worker a distinct loopback port BEFORE the prompt
    # is rendered (the lease note rides inside it). Worktree or not, a
    # local worker that boots a dev server gets its own address instead of
    # a project default port some other workspace may already hold. Cloud
    # workers bind nothing local - skip them.
    service_port = None
    if not step.cloud:
        service_port = alloc_service_port(
            {"run": run_dir.name, "step": step.name,
             "run_dir": str(run_dir), "work_dir": str(work_dir)})
        if service_port is None:
            LOG.warning("no free service port for %s (range %d-%d "
                        "exhausted)", step.name, SERVICE_PORT_MIN,
                        SERVICE_PORT_MAX)
    service_note = _service_note(work_dir, repo_dir, service_port) \
        + parallel_note

    prompt_path = run_dir / "prompts" / f"{step.name}.md"
    prompt_path.parent.mkdir(exist_ok=True)
    # Always render from body_src: it is the resolved body - manifest
    # prompt or prompt_file contents - with any dashboard prompt override
    # and resume notes already folded in.
    render_step = dataclasses.replace(
        step, prompt=device_note + (resume_note or "") + (body_src or ""),
        prompt_file=None)
    prompt_path.write_text(
        render_prompt(render_step, slug, work_dir, cfg.path.parent,
                      cfg.preamble, service_note=service_note),
        encoding="utf-8")
    stdout_log = run_dir / f"{step.name}.stdout.log"
    export_path = run_dir / f"{step.name}.export.json"

    purpose, _disp_body = split_purpose(body_src)
    with STATE_LOCK:
        rec = state["steps"].setdefault(step.name, {})
        # Every dispatch is its own execution interval - prior dispatch
        # spans stay banked in exec_intervals, so a resume accumulates
        # instead of overwriting. started_at remains the CURRENT
        # dispatch's start (pid/liveness windows need it); the original
        # start lives on first_started_at.
        start_iso = now_iso()
        cur_iv = _iv_open(rec, start_iso, run_dir=run_dir)
        rec.update(name=step.name, slug=slug, project=str(work_dir),
                   work_dir=str(work_dir), branch=branch,
                   wt=step.worktree,
                   status="running", started_at=start_iso, ended_at=None,
                   first_started_at=(rec.get("first_started_at")
                                     or rec.get("started_at")
                                     or start_iso),
                   model=step.model, mode=step.mode, cloud=step.cloud,
                   effort=step.effort, timeout_min=step.timeout_min,
                   max_attempts=step.max_attempts,
                   device=(device_lease.serial if device_lease else None),
                   device_spec=step.device,
                   device_booted=(device_lease.booted.get("avd")
                                  if device_lease and device_lease.booted
                                  else None),
                   task=task_summary(body_src),
                   prompt=str(prompt_path), stdout_log=str(stdout_log),
                   export=str(export_path),
                   service_port=service_port,
                   service_url=(f"http://127.0.0.1:{service_port}"
                                if service_port is not None else None))
        # The purpose marker keeps its own machine-readable field now
        # that `task` holds display text; a re-dispatch under a prompt
        # override may legitimately drop it, so clear a stale value.
        if purpose:
            rec["purpose"] = purpose
        else:
            rec.pop("purpose", None)
        save_state(run_dir, state)
    log_event(run_dir, event="step_start", step=step.name, project=str(work_dir))

    if cfg.checkpoint and do_checkpoint:
        if not is_git_repo(work_dir):
            _device_release(device_lease, run_dir)
            _slot_release(agent_slot, run_dir)
            with STATE_LOCK:
                _iv_close(rec, now_iso(), current=cur_iv,
                          run_dir=run_dir)
                rec.update(status="failed",
                           ended_at=now_iso(),
                           note="not a git repo root; checkpoint required")
                _bump_completion(rec)
                save_state(run_dir, state)
            release_service_port(service_port)
            return "failed"
        git_commit(work_dir, f"conductor: checkpoint before {step.name}",
                   allow_empty=True)

    # The leased serial reaches the worker through adb's own ANDROID_SERIAL
    # plus an explicit CONDUCTOR_DEVICE_SERIAL - every automation command
    # the session runs (and the verify gate below) inherits it.
    step_env = clean_devin_env()
    if device_lease is not None:
        step_env["ANDROID_SERIAL"] = device_lease.serial
        step_env["CONDUCTOR_DEVICE_SERIAL"] = device_lease.serial
    # The worker's environment carries its context explicitly: the actual
    # checkout it owns (a worktree for isolated steps), the shared repo
    # root it must not serve over, the run/step it belongs to, and the
    # leased service port. Tooling spawned by the session inherits these.
    # Drop any inherited context values first (a conductor run from inside
    # a worker would otherwise leak the PARENT step's identity/port into
    # this worker).
    for k in ("CONDUCTOR_RUN", "CONDUCTOR_STEP", "CONDUCTOR_RUN_DIR",
              "CONDUCTOR_WORK_DIR", "CONDUCTOR_REPO_DIR", "CONDUCTOR_BRANCH",
              "CONDUCTOR_SERVICE_PORT", "CONDUCTOR_SERVICE_URL"):
        step_env.pop(k, None)
    step_env.update(CONDUCTOR_RUN=run_dir.name, CONDUCTOR_STEP=step.name,
                    CONDUCTOR_RUN_DIR=str(run_dir),
                    CONDUCTOR_WORK_DIR=str(work_dir),
                    CONDUCTOR_REPO_DIR=str(repo_dir))
    if branch:
        step_env["CONDUCTOR_BRANCH"] = branch
    if service_port is not None:
        step_env["CONDUCTOR_SERVICE_PORT"] = str(service_port)
        step_env["CONDUCTOR_SERVICE_URL"] = \
            f"http://127.0.0.1:{service_port}"
    # The machine-wide agent-slot registry gets a per-step sandbox: a
    # worker on the conductor repo itself (or its verify gate running the
    # suite) would otherwise lease REAL slots, and a test shard parked on
    # a full pool waits uncapped while holding the pipes that keep its
    # gate alive - the run deadlocks against its own leases (hit
    # 2026-10-17: two verify-stuck steps pinned slots 4/5, three test
    # shards held 1/6/8, and every queued acquire waited forever).
    step_env["CONDUCTOR_SLOT_DIR"] = str(
        Path(tempfile.gettempdir()) / "agent-conductor-slots-"
        f"{_safe_name(run_dir.name)}-{_safe_name(step.name)}")

    t0 = _now()
    timed_out = False
    hung = False
    stopped = False
    ext_s = 0.0
    rc: int | None = None
    status_line = None
    new_sessions: set[str] = set()
    all_sessions: set[str] = set()
    tok_acc: dict[str, int] = {}
    attempt_outcomes: list[tuple[int, str]] = []
    attempt = 0
    interrupts = 0
    intr_respawn = False
    intr_late = False
    intr_banked = False
    watched = True            # spawn block recomputes; True = no mid-step save
    while True:
        if intr_respawn:
            # 'Interrupt + instruct': the worker was killed by our own
            # sentinel and its product committed at the bottom of this
            # loop - this respawn is the SAME step (not a retry attempt;
            # it costs no max_attempts) resumed under INTERRUPT_NOTE with
            # the operator's text folded into the notes channel.
            intr_respawn = False
            interrupts += 1
            prompt_path = run_dir / "prompts" / \
                f"{step.name}.interrupt{interrupts}.md"
            stdout_log = run_dir / \
                f"{step.name}.interrupt{interrupts}.stdout.log"
            export_path = run_dir / \
                f"{step.name}.interrupt{interrupts}.export.json"
            intr_step = dataclasses.replace(
                step, prompt=INTERRUPT_NOTE + device_note + (body_src or ""),
                prompt_file=None)
            prompt_path.write_text(
                render_prompt(intr_step, slug, work_dir, cfg.path.parent,
                              cfg.preamble, service_note=service_note),
                encoding="utf-8")
            log_event(run_dir, event="step_interrupt_respawn",
                      step=step.name, interrupt=interrupts)
        else:
            attempt += 1
            if attempt > 1:
                # The timed-out attempt's work was committed at the bottom
                # of this loop; the retry is a fresh session pointed at
                # that state. Per-attempt files keep every transcript.
                prompt_path = run_dir / "prompts" / \
                    f"{step.name}.attempt{attempt}.md"
                stdout_log = \
                    run_dir / f"{step.name}.attempt{attempt}.stdout.log"
                export_path = \
                    run_dir / f"{step.name}.attempt{attempt}.export.json"
                retry_step = dataclasses.replace(
                    step, prompt=RETRY_NOTE.format(
                        timeout=step.timeout_min, prev=attempt - 1,
                        total=step.max_attempts) + "\n" + device_note
                        + (body_src or ""),
                    prompt_file=None)
                prompt_path.write_text(
                    render_prompt(retry_step, slug, work_dir,
                                  cfg.path.parent, cfg.preamble,
                                  service_note=service_note),
                    encoding="utf-8")
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
                                        env=step_env,
                                        **hidden_subprocess_kwargs())
                if device_lease is not None:
                    _device_lease_set_worker(device_lease, proc.pid)
                _slot_set_worker(agent_slot, proc.pid)
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
                    # a fresh session hasn't reported anything yet -
                    # drop the previous dispatch's stamp
                    rec["reported"] = None
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
                                    stop_flag(run_dir, step.name),
                                    intrq_flag(run_dir, step.name)),
                        timeout_flag=timeout_flag(run_dir, step.name),
                        stall_s=(step.stall_min * 60
                                 if step.stall_min > 0 else None))
                except subprocess.TimeoutExpired:      # unreachable; safety
                    kill_tree(proc)
                    proc.wait()
                    timed_out, ext_s = True, 0.0
                if stopped:
                    # The per-step sentinel is consumed either way; a
                    # run-level stop flag stays so later waves see it too.
                    # Then split what did the killing: a real stop flag
                    # (per-step or run-level) ends the step, while an
                    # 'Interrupt + instruct' request that fired ALONE is
                    # not a stop - the same step respawns below once the
                    # request folds into the notes channel.
                    had_step_stop = \
                        stop_flag(run_dir, step.name).is_file()
                    stop_flag(run_dir, step.name).unlink(missing_ok=True)
                    if intrq_flag(run_dir, step.name).is_file() \
                            and not had_step_stop \
                            and not stop_flag(run_dir).is_file():
                        if tail_status(stdout_log) is None:
                            # our sentinel did the killing mid-turn: fold
                            # the request, then respawn. A notes-write
                            # failure keeps the flag and reports stopped -
                            # the pending request still applies on resume.
                            if _intrq_to_notes(run_dir, step.name) \
                                    is not None:
                                body_src = _body_with_notes(
                                    body_base,
                                    _flag_text(rnote_flag(run_dir,
                                                          step.name)))
                                intr_respawn, stopped = True, False
                                with STATE_LOCK:
                                    rec["interrupts"] = \
                                        (rec.get("interrupts") or 0) + 1
                        else:
                            # the worker had already ended its turn when
                            # the request landed - natural completion
                            # wins, so this is NOT a user stop: the step
                            # records its real status. Bank the text for
                            # the next dispatch instead of respawning a
                            # finished turn; a fold failure keeps the
                            # flag pending.
                            intr_late = _intrq_to_notes(
                                run_dir, step.name) is not None
                            stopped = False
                            log_event(run_dir,
                                      event="step_interrupt_late",
                                      step=step.name)
                    elif intrq_flag(run_dir, step.name).is_file():
                        # a real stop flag AND an interrupt landed
                        # together: the stop wins the terminal status,
                        # but the instructions still bank into notes so
                        # a later resume carries them.
                        intr_banked = _intrq_to_notes(
                            run_dir, step.name) is not None
                    # Only a REAL stop breaks early. A respawn and a late
                    # arrival both reset `stopped`, so they fall through
                    # to the post block - which commits the killed (or
                    # finished) worker's product and reads its STATUS.
                    if stopped:
                        break
            except OSError as exc:
                LOG.error("step %s spawn failed: %s", step.name, exc)
                log_event(run_dir, event="spawn_error", step=step.name,
                          error=str(exc))
                spawn_failed = True

        new_sessions = (session_ids(devin_argv, work_dir) or set()) - before
        all_sessions |= new_sessions
        status_line = tail_status(stdout_log)
        # The worker's own verdict, readable while teardown still runs:
        # post-commit, the verify gate and the pool merge can stretch for
        # minutes (or wedge), and bare 'running' can't tell a mid-turn
        # step from one whose session already ended 'done'.
        if status_line is not None and rec.get("reported") != status_line:
            with STATE_LOCK:
                rec["reported"] = status_line
                if not watched:
                    save_state(run_dir, state)
        attempt_outcome = tail_outcome(stdout_log)
        if attempt_outcome:
            attempt_outcomes.append((attempt, attempt_outcome))
        _merge_tokens(tok_acc, tail_metrics(export_path))
        # Commit each attempt's product BEFORE deciding to retry - the next
        # session resumes from committed state, not the working tree. The
        # same commit covers a killed worker's partial work before an
        # interrupt respawn continues it.
        if post_commit and cfg.checkpoint and is_git_repo(work_dir) \
                and git_dirty(work_dir):
            git_commit(work_dir, f"conductor: work from {step.name}")
        if intr_respawn:
            continue
        if spawn_failed or not timed_out or attempt >= step.max_attempts:
            break
    elapsed = round(_now() - t0, 1)

    verify_ok = None
    if step.verify and not timed_out and not stopped:
        # Popen + communicate, not subprocess.run: a shell=True gate that
        # hits its cap must die by TREE. subprocess.run's timeout kills
        # only the cmd shell, and children it spawned keep the captured
        # pipes open - the post-kill communicate() retry then blocks on
        # an EOF that never comes and the step records 'running' forever
        # (hit 2026-10-17: a slot-starved verify suite outlived its dead
        # shell; two 'STATUS: done' steps froze mid-teardown and their
        # agent slots never released). kill_tree gets the whole tree so
        # the pipes EOF and the second communicate returns.
        vproc = subprocess.Popen(step.verify, cwd=str(work_dir),
                                 shell=True, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE,
                                 stdin=subprocess.DEVNULL, text=True,
                                 encoding="utf-8", errors="replace",
                                 env=step_env,
                                 **hidden_subprocess_kwargs())
        vout = verr = ""
        verify_timed_out = False
        try:
            vout, verr = vproc.communicate(
                timeout=step.verify_timeout_min * 60)
            verify_ok = vproc.returncode == 0
        except subprocess.TimeoutExpired as te:
            kill_tree(vproc)
            verify_timed_out = True
            # keep whatever the gate already printed - it is the only
            # trace of a hung suite
            vout = te.output or ""
            verr = te.stderr or ""
            try:
                vout, verr = vproc.communicate(
                    timeout=VERIFY_KILL_GRACE_S)
            except subprocess.TimeoutExpired as te2:
                # Orphaned grandchildren still pin a pipe - abandon the
                # capture rather than deadlock the step record. The
                # reader threads die with the driver.
                vout = te2.output or vout
                verr = te2.stderr or verr
                for stream in (vproc.stdout, vproc.stderr):
                    try:
                        stream.close()
                    except OSError:
                        pass
            verify_ok = False
        if isinstance(vout, bytes):
            vout = vout.decode("utf-8", "replace")
        if isinstance(verr, bytes):
            verr = verr.decode("utf-8", "replace")
        (run_dir / f"{step.name}.verify.log").write_text(
            (vout or "") + (verr or "")
            + (f"\nverify timed out after "
               f"{int(step.verify_timeout_min * 60)}s - gate tree "
               f"killed\n" if verify_timed_out else ""),
            encoding="utf-8", errors="replace")

    # An interrupt request that landed during verify/teardown can never be
    # consumed for THIS dispatch - bank it into the notes channel like a
    # late arrival rather than leaving a kill sentinel to poison the next
    # dispatch's first flag poll. A fold failure keeps the flag so the
    # request still lands on the next dispatch.
    if intrq_flag(run_dir, step.name).is_file():
        intr_late = _intrq_to_notes(run_dir, step.name) is not None \
            or intr_late
        log_event(run_dir, event="step_interrupt_late", step=step.name)

    # The serial frees only after worker + verify both let go of it. An
    # exception escaping this function crashes the driver - the lease then
    # goes stale via dead pids and is broken by the next waiter, so the
    # explicit release path covers every status, not just success.
    _device_release(device_lease, run_dir)

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

    # The worker is dead and the step is decided - drop the port lease
    # and the machine-wide agent slot.
    # A False probe means a listener outlived the worker's process tree:
    # record it for review but never kill by port - the bound port does
    # not prove which process owns it.
    port_freed = release_service_port(service_port)
    _slot_release(agent_slot, run_dir)

    with STATE_LOCK:
        rec["session_id"] = (sorted(new_sessions)[-1] if new_sessions
                             else tail_session_ref(stdout_log))
        if all_sessions:
            rec["session_ids"] = sorted(all_sessions)
        rec["exit_code"] = rc
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
        if interrupts:
            notes.append(f"operator interrupted and resumed the step with "
                         f"added instructions ({interrupts}x) - the same "
                         "step continued, not a retry attempt")
        if intr_late:
            notes.append("an operator interrupt request arrived after the "
                         "worker's turn ended - banked into resume notes "
                         "for the next dispatch")
        if intr_banked:
            notes.append("a stop landed before a pending interrupt - its "
                         "instructions were banked into resume notes")
        if intr_folded:
            notes.append("a banked interrupt request was folded into "
                         "this dispatch's prompt")
        if prompt_edited:
            notes.append("prompt body was overridden via the dashboard "
                         "edit control before dispatch")
        if rnotes and rnotes.strip():
            notes.append("dashboard resume notes were appended to the "
                         "prompt")
        if port_freed is False:
            notes.append(f"leased port {service_port} was still bound "
                         "after the worker exited - a dev server it "
                         "spawned is likely still running there")
        if notes:
            rec["note"] = "; ".join(notes)
        # The newest OUTCOME report wins rec['outcome']; every earlier one
        # (a timed-out attempt that still reported, or a previous
        # dispatch's) stays in rec['outcomes'] - a resume's fresh result
        # never erases what earlier attempts finished. Only attempts that
        # end by timeout can precede the last, hence their status label.
        for i, (att, oc) in enumerate(attempt_outcomes):
            _record_outcome(
                rec, oc,
                status=status if i == len(attempt_outcomes) - 1
                else "timed_out",
                attempt=att)
        # Close this dispatch's interval at the real end; elapsed_s is
        # the cumulative sum over every interval, not just this one.
        end_iso = now_iso()
        _iv_close(rec, end_iso, current=cur_iv, run_dir=run_dir)
        rec.update(status=status, ended_at=end_iso, status_line=status_line,
                   verify_ok=verify_ok, next=tail_next(stdout_log),
                   timeout_extended_s=round(ext_s) if ext_s > 1 else None,
                   tokens=tok_acc or None,
                   model_actual=tail_agent_model(export_path),
                   prompt_edited=prompt_edited or None,
                   prompt=str(prompt_path), stdout_log=str(stdout_log),
                   export=str(export_path))
        _bump_completion(rec)
        save_state(run_dir, state)
    log_event(run_dir, event="step_end", step=step.name, status=status,
              exit_code=rc, elapsed_s=elapsed, session=rec.get("session_id"),
              attempts=attempt)
    return status


def resolve_merge_conflict(step: Step, slug: str, repo_dir: Path,
                           work_dir: Path, branch: str, cfg: Manifest,
                           run_dir: Path, devin_argv: list[str],
                           state: dict, merge_dir: Path | None = None,
                           already_started: bool = False,
                           remove_on_success: bool = True,
                           dirty_guard: bool = True,
                           target: str | None = None) -> bool:
    """A worktree branch conflicted at merge-back: re-enter the merge in
    merge_dir (the main checkout, the merge worktree when the recorded
    target lives on another branch, or the deferred-merge staging
    worktree) and hand the resolution to a fresh session (manifest
    `merge_resolver`). Returns True when the merge was completed.

    already_started=True means the caller already ran `git merge` there
    and it conflicted, so only the resolution remains.

    Safety shape: the resolver may stage a fix without committing - the
    conductor finishes `git commit --no-edit` itself once the index is
    conflict-free, marker-free, and the branch tip is an ancestor of HEAD
    (a resolver that quietly aborted the merge can never pass). Anything
    else -> merge --abort + the worktree/branch stay for a human.

    Refuses entirely on a dirty merge checkout: another agent's uncommitted
    work sitting in the tree would get swept into the merge commit
    (trap 10 shape) - that is human territory. dirty_guard=False is for the
    disposable staging worktree, where mid-merge dirt is ours alone.
    """
    merge_dir = merge_dir or repo_dir
    where = ("the main checkout" if merge_dir == repo_dir else
             f"the merge worktree {merge_dir} (target {target or '-'})")
    if dirty_guard and git_dirty_tracked(merge_dir):
        LOG.warning("%s: merge checkout has modified tracked files; not "
                    "spawning a merge resolver", step.name)
        rec = state["steps"].get(step.name, {})
        _append_note(rec, f"merge resolver refused: modified tracked files "
                          f"in {where} (trap 10/11 - commit or stash "
                          "them, then resume)")
        save_state(run_dir, state)
        return False
    if not already_started:
        r = _git(merge_dir, "merge", "--no-edit", branch)
        if r.returncode == 0:                # raced clean after all
            if remove_on_success:
                worktree_remove(repo_dir, work_dir, branch)
                if merge_dir != repo_dir:
                    _git(repo_dir, "worktree", "remove", "--force",
                         str(merge_dir))
            return True
    conflicted = unmerged_paths(merge_dir)
    body = RESOLVE_BODY.format(
        branch=branch, worktree=work_dir, where=where,
        files="\n".join(f"- {f}" for f in conflicted) or "- (see git status)")
    rstep = Step(name=f"resolve-{step.name}", project=slug, prompt=body,
                 model=step.model, mode=step.mode,
                 timeout_min=step.timeout_min)
    # Keep the step's recorded status (done/failed): what is pending is the
    # MERGE, tracked via merged=False - the resume path keys off exactly
    # that, so a conductor dying mid-resolution resumes through late-merge.
    rec = state["steps"].get(step.name, {})
    rec.update(note=f"merge conflict on {branch}; resolver session "
                    f"{rstep.name} spawned in {where}",
               merged=False)   # orange 'unmerged' chip while resolving
    save_state(run_dir, state)
    log_event(run_dir, event="resolve_start", step=step.name, branch=branch,
              files=conflicted)
    status = run_step(rstep, slug, merge_dir, cfg, run_dir, devin_argv,
                      state, do_checkpoint=False, post_commit=False)
    log_event(run_dir, event="resolve_end", step=step.name, status=status)

    # Adopt any resolved-but-unstaged file that carries no markers, then
    # decide. A file still holding markers is never staged.
    for f in conflicted:
        if not has_conflict_markers(merge_dir, f):
            _git(merge_dir, "add", f)
    if status == "done" and not unmerged_paths(merge_dir) \
            and not any(has_conflict_markers(merge_dir, f)
                        for f in conflicted) \
            and merge_in_progress(merge_dir):
        # Index clean, merge still open -> conductor commits the resolution.
        _git(merge_dir, "commit", "--no-edit")
    tip = _git(repo_dir, "rev-parse", branch).stdout.strip()
    landed = bool(tip) and _git(merge_dir, "merge-base", "--is-ancestor",
                                tip, "HEAD").returncode == 0
    if status == "done" and landed and not unmerged_paths(merge_dir):
        if remove_on_success:
            worktree_remove(repo_dir, work_dir, branch)
            if merge_dir != repo_dir:
                _git(repo_dir, "worktree", "remove", "--force",
                     str(merge_dir))
        return True
    if merge_in_progress(merge_dir):
        _git(merge_dir, "merge", "--abort")
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
    el = run_elapsed_s(state)
    body = ("run dir: " + state.get("run_dir", "?") + "\n"
            + "ended: " + (state.get("ended_at") or "still running") + "\n"
            + (f"elapsed: {el:.0f}s\n" if el is not None else "")
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
    # Validate before any run state exists: a bad wave or merge target must
    # not leave a state.json the dashboard will show as "running" forever.
    groups = plan_groups(resolved, cfg)
    validate_groups(groups)
    validate_merge_targets(resolved)
    cap = effective_max_parallel(cfg, getattr(args, "max_parallel", None))
    agent_cap = agent_slot_cap()

    if getattr(args, "keep_awake", True):
        keep_awake()

    if args.resume is not None:
        run_dir = find_resume_dir(cfg, runs_dir, args.resume or "latest")
        # Serialize the startup stamp against a manual pending-branch
        # merge: merge_run holds the run's merge claim for its whole
        # operation and re-reads state under it, so stamping driver_pid
        # while WE hold the same claim is what makes a just-started resume
        # visible to a merge that already passed its first check - the
        # two can never overlap.
        try:
            resume_claim = _claim_run_merge(run_dir, "resume")
        except RequestError as exc:
            print(f"--resume: {exc} - retry once it finishes")
            return 2
        try:
            state = json.loads(
                (run_dir / "state.json").read_text(encoding="utf-8"))
            if driver_alive(state):
                print(f"--resume: {run_dir.name} still has a live "
                      "conductor - let it end first")
                return 2
            # The driver is now THIS code version: it polls the path-keyed
            # sentinels only, so the dashboard can stop writing legacy
            # names.
            state["flags"] = "v2"
            # ...and it polls the intrq- sentinel, so the dashboard's
            # 'Interrupt + instruct' action can accept requests again.
            state["intrq"] = True
            # A resumed run is in flight again: the stale
            # 'aborted'/'ended_at' from the pause/crash would keep showing
            # the old end-state notice (e.g. 'Paused by user') until the
            # loop re-stamps them.
            state.pop("aborted", None)
            prev_end = state.pop("ended_at", None)
            # ...and the previous driver session's execution interval
            # closes before the new one opens - at its recorded end when
            # the run terminated cleanly, at the last sign of life after a
            # crash.
            _run_iv_close(state, prev_end or now_iso(), run_dir=run_dir,
                          witness_cap=prev_end is None)
            # A re-entered run is in flight again - drop the auto-archive
            # stamp so it resurfaces on the default dashboard view.
            state.pop("archived", None)
            # The driver is THIS process now - restamp so driver_alive()
            # tracks the resumed conductor, not the dead original.
            state["driver_pid"] = os.getpid()
            state["driver_started_at"] = now_iso()
            ivs = state.setdefault("exec_intervals", [])
            if not ivs:
                # A legacy run (no exec_intervals yet) seeds its first
                # interval from recorded stamps - start->ended_at after a
                # clean end, start->last-witness after a crash - so even
                # pre-schema work banks instead of vanishing.
                t0s = state.get("started_at")
                t1s = prev_end or _iso_from(
                    _run_witness_ts(state, run_dir))
                if t0s and t1s and t1s > t0s:
                    ivs.append({"start": t0s, "end": t1s})
            ivs.append({"start": state["driver_started_at"], "end": None})
            # The current manifest's policy applies to steps prepared from
            # now on; already-recorded recs keep their own `deferred`
            # flags.
            state["merge_policy"] = cfg.merge_policy
            state["summarize_model"] = cfg.summarize
            save_state(run_dir, state)
            print(f"[resume] {run_dir.name}")
            # Resume must be able to start work again: drop any stop/drain
            # sentinels left by a dashboard Stop click.
            clear_run_flags(run_dir)
        finally:
            resume_claim.unlink(missing_ok=True)
    else:
        run_dir = new_run_dir(cfg, runs_dir)
        run_start = now_iso()
        state = {"run": cfg.name, "description": cfg.description,
                 "request_id": getattr(args, "request_id", None),
                 "manifest": str(manifest_path),
                 "merge_policy": cfg.merge_policy,
                 "started_at": run_start, "devin": " ".join(devin_argv),
                 "devin_argv": devin_argv,
                 "run_dir": str(run_dir), "max_parallel": cap,
                 "parallel": cfg.parallel, "agent_cap": agent_cap,
                 "flags": "v2", "intrq": True,
                 "summarize_model": cfg.summarize,
                 "driver_pid": os.getpid(),
                 "driver_started_at": run_start,
                 # the driver's own execution interval - the unit run
                 # elapsed sums over (never a sum of overlapping steps)
                 "exec_intervals": [{"start": run_start, "end": None}],
                 "free_ram_gb": free_ram_gb(), "steps": {}}
        save_state(run_dir, state)
    hub = getattr(args, "hub", True)
    tid = hub_start(cfg, run_dir) if hub else None
    log_event(run_dir, event="run_start", manifest=str(manifest_path),
              hub_task=tid)

    if "devin" in os.path.basename(devin_argv[-1]).lower():
        if not check_auth(devin_argv):
            print("devin is not logged in. Run `devin auth login` first.")
            end_iso = now_iso()
            _run_iv_close(state, end_iso)
            state["ended_at"] = end_iso
            state["aborted"] = "auth"
            _bump_completion(state)
            save_state(run_dir, state)
            log_event(run_dir, event="run_abort", reason="auth")
            hub_update(tid, state, final=True, final_status="dropped")
            return 2

    run_t0 = _now()
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
                        wt=s_.worktree, cloud=s_.cloud,
                        note=why + " before this step ran")
            state["aborted"] = ("paused" if paused else
                                "stopped" if halted else "drained")
            end_iso = now_iso()
            _run_iv_close(state, end_iso)
            state["ended_at"] = end_iso
            _bump_completion(state)
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
        if cfg.max_hours and _now() - run_t0 > cfg.max_hours * 3600:
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
                                wt=s_.worktree, cloud=s_.cloud,
                                note="auth lost mid-run; --resume after "
                                     "devin auth login")
                state["aborted"] = "auth_midrun"
                end_iso = now_iso()
                _run_iv_close(state, end_iso)
                state["ended_at"] = end_iso
                _bump_completion(state)
                save_state(run_dir, state)
            hub_update(tid, state, final=True, final_status="dropped")
            return 2
        # Materialize worktrees for members that want isolation.
        prepared: list[tuple[Step, str, Path, Path, str | None]] = []
        targets: dict[str, str | None] = {}   # step -> recorded merge target
        wave_ckpt_done = False
        # Deferred steps sharing a repo inside this group are alternatives:
        # they all branch off the same baseline tip and none of them joins
        # it (a lone deferred member is 'sequential' and advances it).
        deferred_counts: dict[str, int] = {}
        for _pd, _sl, _st in members:
            if _st.merge == "deferred":
                k = _repo_key(_pd)
                deferred_counts[k] = deferred_counts.get(k, 0) + 1
        for project_dir, slug, step in members:
            prior = state["steps"].get(step.name, {})
            unmerged = bool(prior.get("branch")) \
                and prior.get("merged") is not True \
                and not prior.get("deferred")
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
                # The RECORDED target wins over the manifest key so a retry
                # can never drift to a different baseline than the work was
                # created from; pre-feature runs fall back to the manifest,
                # then to the checkout's current branch (legacy behavior).
                tgt = prior.get("merge_target", step.merge_target)
                mwt = Path(prior.get("merge_dir") or merge_wt_for(wt))
                if _git(project_dir, "rev-parse", "-q", "--verify",
                        prior["branch"]).returncode == 0:
                    via_resolver = False
                    merr = None
                    try:
                        with _RepoMergeLock(
                                project_dir,
                                wait=(state, run_dir, prior)):
                            abort_own_merge(project_dir)
                            ok, loc, merr = merge_step_branch(
                                project_dir, wt, prior["branch"], tgt, mwt)
                            if loc is not None and loc != project_dir:
                                prior["merge_dir"] = str(loc)
                            # merr=None marks a real conflict a resolver
                            # can fix; an error string means no merge got
                            # far enough to have conflicts (bad loc,
                            # vanished target, foreign merge) - spawning a
                            # session would just fail again.
                            if not ok and step.merge_resolver \
                                    and loc is not None and merr is None:
                                ok = resolve_merge_conflict(
                                    step, slug, project_dir, wt,
                                    prior["branch"], cfg, run_dir,
                                    devin_argv, state,
                                    merge_dir=loc, target=tgt)
                                via_resolver = ok
                    except RequestError:
                        ok = False
                    was = prior.get('status')
                    if ok:
                        prior.update(status="done", merged=True,
                                     merged_at=now_iso())
                        _append_note(prior, f"merged on resume (was {was})")
                        _bump_completion(prior)
                        save_state(run_dir, state)
                        log_event(run_dir, event="step_merged",
                                  step=step.name, how="resume", was=was,
                                  branch=prior["branch"],
                                  resolver=via_resolver)
                        print(f"[merged] {step.name} (merge on resume)")
                    elif was == "done":
                        prior.update(status="failed", merged=False)
                        _append_note(prior, (merr or "merge conflict")
                                            + f" on resume; worktree "
                                              f"kept at {wt}")
                        _bump_completion(prior)
                        save_state(run_dir, state)
                        log_event(run_dir, event="step_merge_conflict",
                                  step=step.name, how="resume", was=was,
                                  branch=prior["branch"])
                        print(f"[ failed] {step.name} (merge conflict "
                              "on resume)")
                        if cfg.on_failure == "stop":
                            stop = True
                            break
                    else:
                        prior["merged"] = False
                        _append_note(prior, merr or
                                     "merge still conflicts; resolve the "
                                     "kept worktree by hand or enable "
                                     "merge_resolver - re-running the same "
                                     "branch cannot fix a conflict")
                        _bump_completion(prior)
                        save_state(run_dir, state)
                        log_event(run_dir, event="step_merge_conflict",
                                  step=step.name, how="resume", was=was,
                                  branch=prior["branch"])
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
                dgrp = None
                start = None
                kind = None
                # The merge target is decided HERE, once: a previously
                # recorded target (the reused worktree's baseline is already
                # baked in), else the manifest's merge_target, else the
                # checkout's current branch. The branch could vanish after
                # plan-time validation - that is a step failure with the
                # same actionable note, never a silent fallback to whatever
                # is checked out.
                tgt = (prior.get("merge_target") or step.merge_target
                       or current_branch(project_dir))
                mterr = (merge_target_error(f"step {step.name!r}",
                                            project_dir, step.merge_target)
                         if step.merge_target else None)
                if mterr is None and step.merge == "deferred":
                    # Deferred work branches off the run's per-project
                    # integration baseline (created at the recorded target
                    # tip, then synced over target movement) - never off
                    # bare HEAD, or each step would restart from unchanged
                    # main and lose its verified predecessors.
                    dgrp = _baseline_ensure(project_dir, slug, run_dir.name,
                                            state, run_dir,
                                            target=step.merge_target)
                    start = (dgrp and
                             _baseline_sync(project_dir, dgrp, run_dir))
                    kind = ("alternative"
                            if deferred_counts.get(
                                _repo_key(project_dir), 0) > 1
                            else "sequential")
                    if start is None:
                        with STATE_LOCK:
                            state["steps"].setdefault(step.name, {}).update(
                                name=step.name, slug=slug, status="failed",
                                wt=step.worktree, cloud=step.cloud,
                                note="deferred merge needs a branch "
                                     "checked out in the project "
                                     "(detached/unborn HEAD)")
                            save_state(run_dir, state)
                        print(f"[  failed] {step.name} (no merge target)")
                        if cfg.on_failure == "stop":
                            stop = True
                            break
                        continue
                elif mterr is None:
                    # Auto-merge steps branch off their recorded target.
                    start = f"refs/heads/{tgt}" if tgt else None
                if mterr is None and worktree_add(
                        project_dir, wt, branch, start):
                    targets[step.name] = tgt
                    if dgrp is not None:
                        # Stamp deferred + pending-entry NOW: a driver dying
                        # between prep and the post-wave pass would
                        # otherwise leave a branch that resume reads as a
                        # legacy unmerged auto-merge (and finalize it
                        # into main).
                        with STATE_LOCK:
                            _deferred_record(
                                state["steps"].setdefault(
                                    step.name, {"name": step.name}),
                                dgrp, kind, step.name)
                            save_state(run_dir, state)
                    prepared.append((step, slug, wt, project_dir, branch,
                                     rnote, dgrp))
                    continue
                with STATE_LOCK:
                    state["steps"].setdefault(step.name, {}).update(
                        name=step.name, slug=slug, status="failed",
                        wt=step.worktree, cloud=step.cloud,
                        merge_target=tgt,
                        note=mterr or "worktree add failed")
                    save_state(run_dir, state)
                print(f"[  failed] {step.name} "
                      f"({mterr or 'worktree add'})")
                if cfg.on_failure == "stop":
                    stop = True
                    break
                continue
            else:
                prepared.append((step, slug, project_dir, project_dir,
                                 None, rnote, None))
        if stop:
            break
        if not prepared:
            continue   # every member of this group was already done (resume)

        # Mark members queued up front: the pool may hold them while a
        # max_parallel-sized set runs, and the dashboard shows the queue.
        # merge_target is persisted per step NOW - the recorded value (the
        # manifest's choice or the checkout's branch at materialization) is
        # what merge-back, retries, resume and the manual Merge button all
        # target, so none of them can silently fall back to main.
        with STATE_LOCK:
            for step, _s, _w, _r, _b, _n, _g in prepared:
                rec = state["steps"].setdefault(step.name, {})
                rec.update(status="queued", name=step.name, wt=step.worktree,
                           cloud=step.cloud,
                           merge_target=targets.get(step.name),
                           deps=list(step.depends_on) or None)
                unmet = deps_unmet(step, state)
                if unmet:
                    rec.update(dep_wait=unmet,
                               note="waiting on step(s): "
                                    + ", ".join(unmet))
            save_state(run_dir, state)

        has_deps = any(s.depends_on for s, *_ in prepared)
        if len(prepared) == 1 and not has_deps:
            step, slug, work_dir, repo_dir, branch, rnote, _dg = prepared[0]
            statuses = {step.name: run_step(step, slug, work_dir, cfg,
                                            run_dir, devin_argv, state,
                                            do_checkpoint=(branch is None),
                                            branch=branch,
                                            resume_note=rnote,
                                            repo_dir=repo_dir)}
            hub_update(tid, state)
        else:
            statuses = {}
            pending = list(prepared)
            futs: dict = {}
            with concurrent.futures.ThreadPoolExecutor(
                    max_workers=min(len(prepared), cap)) as ex:
                while pending or futs:
                    for pm in list(pending):
                        unmet = deps_unmet(pm[0], state)
                        rec = state["steps"].get(pm[0].name)
                        if rec is not None and                                 unmet != (rec.get("dep_wait") or []):
                            with STATE_LOCK:
                                rec["dep_wait"] = unmet or None
                                if unmet:
                                    rec["note"] = ("waiting on step(s): "
                                                   + ", ".join(unmet))
                                elif (rec.get("note") or "").startswith(
                                        "waiting on step"):
                                    rec["note"] = None
                                save_state(run_dir, state)
                        if unmet:
                            continue
                        pending.remove(pm)
                        step, slug, work_dir, repo_dir, branch, rnote, _dg = pm
                        futs[ex.submit(
                            run_step, step, slug, work_dir, cfg,
                            run_dir, devin_argv, state,
                            do_checkpoint=(branch is None),
                            branch=branch,
                            resume_note=rnote,
                            repo_dir=repo_dir)] = step.name
                    if not futs:
                        # No live dispatcher can resolve the rest (a dep
                        # was filtered out or never got a record) - skip
                        # them rather than hang the wave forever.
                        for pm in pending:
                            unmet = deps_unmet(pm[0], state)
                            with STATE_LOCK:
                                state["steps"].setdefault(
                                    pm[0].name, {}).update(
                                    status="skipped", dep_wait=None,
                                    ended_at=now_iso(),
                                    note="dependency never resolved: "
                                         + ", ".join(unmet))
                                save_state(run_dir, state)
                            log_event(run_dir, event="step_dep_blocked",
                                      step=pm[0].name, deps=unmet)
                            statuses[pm[0].name] = "skipped"
                        break
                    done, _ = concurrent.futures.wait(
                        futs,
                        return_when=concurrent.futures.FIRST_COMPLETED)
                    for f in done:
                        statuses[futs.pop(f)] = f.result()
                        hub_update(tid, state)

        # Merge worktree branches back into their recorded targets,
        # in manifest order. The merge runs in the main checkout when the
        # target is its current branch, else in a conductor-owned merge
        # worktree on the target (<step>.merge) - the checkout's HEAD
        # never moves for a non-default target. Deferred steps skip this
        # pass entirely - they land via the pending-merge pipeline.
        for step, slug, work_dir, repo_dir, branch, _n, dgrp in prepared:
            status = statuses.get(step.name)
            print(f"[{status or 'skipped':>8}] {step.name}")
            if branch and dgrp is not None:
                # Deferred merge: the branch, worktree, commits and verify
                # result are retained as a pending merge for an explicit
                # human-requested land - never merged here. A verified
                # SEQUENTIAL step also fast-forwards the run baseline so
                # later steps on this project see it; wave siblings are
                # alternatives and never join the baseline. Retained
                # worktrees are never debris-cleaned - their recorded
                # branch is the recoverable state.
                rec = state["steps"].setdefault(
                    step.name, {"name": step.name})
                kind = ("alternative"
                        if deferred_counts.get(_repo_key(repo_dir), 0) > 1
                        else "sequential")
                _deferred_record(rec, dgrp, kind, step.name)
                if kind == "sequential" and rec.get("deferred_verified"):
                    _baseline_advance(repo_dir, dgrp, branch, step.name,
                                      run_dir)
                _deferred_pending_note(rec, dgrp, work_dir)
                save_state(run_dir, state)
                continue
            if branch:
                rec = state["steps"].get(step.name, {})
                tgt = rec.get("merge_target", step.merge_target)
                if status in ("done", "partial", "blocked"):
                    via_resolver = False
                    merr = None
                    try:
                        with _RepoMergeLock(
                                repo_dir, wait=(state, run_dir, rec)):
                            ok, loc, merr = merge_step_branch(
                                repo_dir, work_dir, branch, tgt,
                                merge_wt_for(work_dir))
                            if loc is not None and loc != repo_dir:
                                rec["merge_dir"] = str(loc)
                            # merr=None = a real conflict a resolver can
                            # fix; an error string means the merge never
                            # got that far (bad/vanished target, foreign
                            # checkout) - a session would just fail
                            # there too.
                            if not ok and step.merge_resolver \
                                    and loc is not None and merr is None:
                                ok = resolve_merge_conflict(
                                    step, slug, repo_dir, work_dir,
                                    branch, cfg, run_dir, devin_argv,
                                    state, merge_dir=loc, target=tgt)
                                if ok:
                                    via_resolver = True
                                    _append_note(
                                        rec, "merge conflict resolved by "
                                             f"resolve-{step.name}")
                    except RequestError as exc:
                        ok = False
                        _append_note(rec, "another merge holds this repo "
                                          f"({exc}); worktree kept at "
                                          f"{work_dir}")
                    if ok:
                        rec["merged"] = True
                        rec["merged_at"] = now_iso()
                        log_event(run_dir, event="step_merged",
                                  step=step.name, how="wave", was=status,
                                  branch=branch, resolver=via_resolver)
                    else:
                        rec.update(status="failed", merged=False)
                        _append_note(rec, merr or
                                     "merge conflict; worktree kept "
                                     f"at {work_dir}")
                        log_event(run_dir, event="step_merge_conflict",
                                  step=step.name, how="wave", was=status,
                                  branch=branch)
                        status = "failed"
                        statuses[step.name] = "failed"
                    _bump_completion(rec)
                    save_state(run_dir, state)
                elif worktree_debris(repo_dir, work_dir, branch, tgt):
                    # step died before doing work and the branch carries
                    # nothing - remove the worktree+branch instead of
                    # leaving orphan debris in the target repo.
                    worktree_remove(repo_dir, work_dir, branch)
                    rec = state["steps"].get(step.name, {})
                    _append_note(rec, "worktree removed: step failed "
                                      "with no work product")
                    save_state(run_dir, state)
                    log_event(run_dir, event="worktree_removed",
                              step=step.name, branch=branch)
                else:
                    rec = state["steps"].get(step.name, {})
                    _append_note(rec, f"worktree kept at {work_dir} - "
                                      "branch has unmerged work")
                    save_state(run_dir, state)
                    log_event(run_dir, event="step_merge_kept",
                              step=step.name, was=status, branch=branch)
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

    end_iso = now_iso()
    _run_iv_close(state, end_iso)
    state["ended_at"] = end_iso
    _bump_completion(state)
    save_state(run_dir, state)
    log_event(run_dir, event="run_end")
    hub_update(tid, state, final=True)
    counts: dict[str, int] = {}
    for rec in state["steps"].values():
        counts[rec["status"]] = counts.get(rec["status"], 0) + 1
    print(f"run {cfg.name}: {dict(counts)}  ({run_dir})")
    # A closing follow-up-plan step's output is an intake artifact of THIS
    # run: register <run>-followup.toml as a proposal request so it shows
    # as 'Ready to run' - never launched. Best-effort only: a checkout a
    # live agent still watches defers to the dashboard's rescan rather
    # than failing the run.
    try:
        for line in discover_followups(runs_dir, manifests_dir=MANIFESTS_DIR,
                                       run_dir=run_dir, scan_orphans=False):
            print(f"[followup] {line}")
    except Exception as exc:
        LOG.exception("follow-up discovery failed")
        print(f"[followup] discovery failed: {exc}")
    # [run] summarize: a detached `conductor.py summarize` session reads
    # the step exports and writes run.summary.md - out of process so the
    # run's exit code is already fixed and a hung session can't hold the
    # driver. _launch_process is the same spawn path the dashboard uses.
    if cfg.summarize is not None and any(
            r.get("export") for r in state["steps"].values()):
        cmd = [sys.executable, str(ROOT / "conductor.py"), "summarize",
               str(run_dir), "--runs-dir", str(runs_dir)]
        if devin_argv != ["devin"]:
            cmd += ["--devin", shlex.join(devin_argv)]
        if cfg.summarize:
            cmd += ["--model", cfg.summarize]
        try:
            pid = _launch_process(cmd, run_dir / "summarize.launch.log")
            try:
                _summary_marker(run_dir).write_text(
                    str(pid), encoding="utf-8")
            except OSError:
                pass
            print("[summary] LLM run-summary session launched "
                  "(run.summary.md lands when it finishes)")
        except OSError as exc:
            print(f"[summary] summarizer launch failed: {exc}")
    return 0 if set(counts) <= {"done", "partial"} else 1


# ---------------------------------------------------------------- plan/status

def cmd_plan(args: argparse.Namespace) -> int:
    manifest_path = Path(args.manifest).resolve()
    cfg = load_manifest(manifest_path)
    projects = load_projects()
    resolved = [(*resolve_project(s, projects, manifest_path.parent), s)
                for s in cfg.steps]
    validate_groups(plan_groups(resolved, cfg))
    validate_merge_targets(resolved)
    print(f"run {cfg.name!r}: {len(cfg.steps)} steps, on_failure={cfg.on_failure}, "
          f"checkpoint={cfg.checkpoint}, parallel={cfg.parallel}, "
          f"max_parallel={effective_max_parallel(cfg)}"
          f" (machine pool {agent_slot_cap()})")
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
            + (" [resolver]" if s.merge_resolver else "") \
            + (f" [device={s.device}]" if s.device else "") \
            + (" [deferred]" if s.merge == "deferred" else "") \
            + (f" [merge -> {s.merge_target}]" if s.merge_target else "")
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
    run_el = run_elapsed_s(state, run_dir=run_dir)
    run_el_s = f"{run_el:.0f}s" if run_el is not None else "-"
    print(f"run {state.get('run', '?')}  started "
          f"{state.get('started_at') or '?'}  ended {ended}  "
          f"elapsed {run_el_s}  ({run_dir})")
    for name, rec in (state.get("steps") or {}).items():
        sess = rec.get("session_id") or "-"
        span = rec_elapsed_s(rec, live=eff.get(name) == "running")
        span_s = f"{span:>7.0f}s" if span is not None else "      -"
        print(f"  {eff.get(name, '?'):>9}  {name:<20} {span_s}  "
              f"ended={rec.get('ended_at') or '-'}  "
              f"session={sess}  verify={rec.get('verify_ok')}")
        if rec.get("outcome"):
            print(f"             outcome: {rec['outcome']}")
        if rec.get("note"):
            print(f"             note: {rec['note']}")
        if sess not in (None, "-") and not str(sess).startswith("mock"):
            how = "devin --cloud -r" if "/" in str(sess) else "devin -r"
            print(f"             resume: cd {rec.get('project')} && {how} {sess}")
    return 0


# ---------------------------------------------------------------- hygiene

def _runs_watched(tree: Path, states: list[tuple[Path, dict]]) -> list[str]:
    """Trap-5 write guard: identities of the live steps whose work_dir
    contains `tree` - a self-hosted run in flight means ANY write under it
    lands in the worker's watched tree (an external edit ends its
    session). Empty = unwatched."""
    return _watchers_of(tree, (tree,), states=states)


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
        if driver_alive(st) or _merge_claim_held(sj.parent):
            # The run's conductor is alive (possibly mid-verify/merge with
            # a dead worker pid), or a manual merge is mid-flight on it -
            # either way the run's records are being actively written.
            # Orphan-marking is for abandoned runs.
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
                    # its dispatch interval died with the driver - close
                    # at the last sign of life (log mtime), not at sweep
                    # time, so the dead gap never inflates elapsed
                    _iv_close(rec, now_iso(), run_dir=sj.parent)
                    if not rec.get("ended_at"):
                        rec["ended_at"] = now_iso()
                    rec.setdefault(
                        "note", "marked lost by sweep: nothing alive behind "
                                "the record (conductor or worker died?)")
                    _bump_completion(rec)
                    dirty = True
        if steps and not st.get("ended_at") and not any(
                r.get("status") in ("running", "queued")
                for r in steps.values()):
            lines.append(f"{sj.parent.name}: run closed (no live steps)")
            if write:
                end_iso = now_iso()
                _run_iv_close(st, end_iso, run_dir=sj.parent,
                              witness_cap=True)
                st["ended_at"] = end_iso
                _bump_completion(st)
                dirty = True
        if dirty:
            if watched:
                lines.insert(0, f"DRY-RUN: live agent(s) watch {runs_dir} "
                                f"({_watcher_summary(watched)}) - not "
                                "writing (trap 5)")
                break
            save_state(sj.parent, st)
    lines += archive_runs(runs_dir, write=write)
    lines += sweep_trash(_trash_root(runs_dir), runs_dir.parent / "requests",
                         runs_dir, write=write)
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
        if ended is None or now - ended < ARCHIVE_AFTER_S \
                or _merge_claim_held(sj.parent):
            continue
        lines.append(f"{sj.parent.name}: archived (ended {st['ended_at']}, "
                     f"over {ARCHIVE_AFTER_S / 3600:g}h old)")
        if write and not watched:
            st["archived"] = now_iso()
            save_state(sj.parent, st)
    if lines and write and watched:
        lines.insert(0, f"DRY-RUN: live agent(s) watch {runs_dir} "
                        f"({_watcher_summary(watched)}) - not writing "
                        "(trap 5)")
    return lines


_ARCHIVE_TRIED = 0.0
_ARCHIVE_POLL_S = 1800.0    # /api/state is polled ~2s; scan twice an hour


def _auto_archive(runs_dir: Path) -> None:
    """serve's startup sweep archives once at boot; a dashboard left up for
    days would never hide runs that finished after boot without this
    throttled pass (failures just keep the current view). The same pass
    purges expired trash - bounded, expired-only, and self-dry-running
    while a live worker watches the data tree."""
    global _ARCHIVE_TRIED
    if time.time() - _ARCHIVE_TRIED < _ARCHIVE_POLL_S:
        return
    _ARCHIVE_TRIED = time.time()
    try:
        archive_runs(runs_dir)
        sweep_trash(_trash_root(runs_dir), runs_dir.parent / "requests",
                    runs_dir)
    except Exception:
        LOG.exception("auto-archive sweep failed")


def cmd_sweep(args: argparse.Namespace) -> int:
    runs_dir = Path(args.runs_dir).resolve() if args.runs_dir else RUNS_DIR
    lines = sweep_runs(runs_dir)
    devin_argv = shlex.split(getattr(args, "devin", None)
                             or os.environ.get("CONDUCTOR_DEVIN") or "devin")
    lines += sweep_sessions(runs_dir, devin_argv,
                            getattr(args, "keep_hours", 48.0))
    lines += [f"followup: {ln}" for ln in discover_followups(runs_dir)]
    lines += sweep_service_ports()
    lines += sweep_agent_slots()
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
# In-flight run summarizer subprocesses by run-dir name - guards the
# spawn gap between the endpoint's launch and the subprocess's own
# .summarizing marker write (same shape as _RESUME_PIDS).
_SUM_PIDS: dict[str, int] = {}
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
PLANNER_SPOOL = Path(os.environ.get("CONDUCTOR_PLANNER_SPOOL")
                     or (Path(tempfile.gettempdir())
                         / "agent-conductor-planner"))
WATCH_POLL_S = 5.0            # _wait_writable poll interval
PLANNER_ORPHAN_GRACE_S = 60   # gen.log this fresh may still be written by
                              # an orphaned planner - drain waits it out
_DRAIN_POLL_S = 10.0          # collect_dashboard drain throttle
_DRAIN_TRIED = 0.0
# rec['plan_sessions'] is bounded preparation history - one record per
# planner attempt (done/stopped/failed/questions-parked), enough that a
# repeatedly regenerated request can't grow the JSON without limit.
PLAN_HISTORY_MAX = 12


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
    # swe-2 promo runs until 2026-11-01; gpt-6-luna takes over as the
    # default the day it ends.
    return (("swe-2", "max") if (today or dt.date.today())
            < dt.date(2026, 11, 1) else ("gpt-6-luna", "max"))


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
    path = _spool_dir(request_id) / "request.json"
    rec = None
    for attempt in range(4):
        try:
            rec = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None                    # genuinely absent
        except (ValueError, RequestError):
            return None                    # corrupt or bad id shape
        except OSError:
            # Transient only: a share-lock (AV/indexer) or a mid-replace
            # read surfaces as a brief PermissionError on Windows, and
            # reading "no overlay" here has real consequences - a stop
            # skips the overlay drop it came to do, a save_draft thinks
            # it lost ownership, and the surviving 'generating' overlay
            # then wedges the request for the whole orphan grace.
            if attempt == 3:
                return None
            time.sleep(0.05)
            continue
        break
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
    return {"name": name, "path": str(p.resolve()), "ctype": sig[1]}


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
        ent = {"name": name, "path": path}
        # save_attachment's POST row carries the magic-sniffed type -
        # keep it when the client echoes the row back; the record then
        # round-trips byte-for-byte and the assistant's stored
        # attachment refs agree (it never becomes a served header -
        # GET /api/attachments re-sniffs)
        ctype = str(a.get("ctype") or "")
        if ctype.startswith("image/") and len(ctype) <= 64:
            ent["ctype"] = ctype
        out.append(ent)
    return out


def _request_fields(payload: dict, rec: dict | None = None) -> dict:
    if not isinstance(payload, dict) or set(payload) - {
            "raw", "label", "project", "model", "effort", "manifest",
            "followup", "on_failure", "max_attempts",
            "plan_model", "plan_effort", "merge_target",
            "worktrees", "merge_resolver", "merge", "summarize", "ask",
            "parallel", "attachments"}:
        raise RequestError("Invalid request fields")
    model, effort = default_model_effort()
    fields = {k: (rec or {}).get(k, v) for k, v in (
        ("raw", ""), ("label", ""), ("project", ""), ("on_failure", "continue"),
        ("merge", "auto"),
        ("model", model), ("effort", effort), ("merge_target", ""),
        ("plan_model", model), ("plan_effort", effort))}
    for key in fields:
        if key in payload:
            if not isinstance(payload[key], str):
                raise RequestError(f"{key} must be text")
            fields[key] = payload[key].strip()
    for bkey in ("followup", "worktrees", "merge_resolver", "summarize",
                 "ask", "parallel"):
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
    if fields["merge"] not in ("auto", "deferred"):
        raise RequestError("merge must be auto or deferred")
    if not fields["raw"] or len(fields["raw"]) > 100_000:
        raise RequestError("Request text must be 1–100,000 characters")
    if len(fields["label"]) > 80:
        raise RequestError("Label must be at most 80 characters")
    if fields["project"] and fields["project"] not in load_projects():
        raise RequestError("Choose a registered project from projects.toml")
    # merge_target is validated through git itself: name syntax via
    # check-ref-format, and - once the project is known - existence via
    # refs/heads/<name> in that repo. An inferred project defers the
    # existence check to proposal validation (each step's resolved repo).
    if fields["merge_target"]:
        if not branch_name_ok(fields["merge_target"]):
            raise RequestError(
                f"merge_target {fields['merge_target']!r} is not a valid "
                f"branch name")
        if fields["project"]:
            pdir = load_projects()[fields["project"]]["path"]
            err = merge_target_error(f"project {fields['project']!r}",
                                     pdir, fields["merge_target"])
            if err:
                raise RequestError(err)
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


def _watcher_identity(run_dir_name: str, step_name: str,
                      rec: dict) -> str:
    """One-line owner of a live-watched workspace, for guard errors:
    '<run-dir>:<step>' plus the project slug and a short session id when
    the record carries them. state.json is the authoritative registry of
    who runs where; every field degrades when missing (older records may
    lack slug/session_id). Only identifiers are surfaced - the task body
    never enters an error string."""
    label = f"{run_dir_name}:{step_name or rec.get('name') or '?'}"
    extras = [str(rec["slug"])] if rec.get("slug") else []
    sid = str(rec.get("session_id") or "").strip()
    if sid:
        extras.append(f"session {sid[:8]}")
    return label + (f" ({', '.join(extras)})" if extras else "")


def _watchers_of(runs_dir: Path, destinations: tuple[Path, ...],
                 states: list[tuple[Path, dict]] | None = None
                 ) -> list[str]:
    """Identities of every live step whose watched work_dir contains one
    of `destinations` - the runs' own state.json records are the
    authoritative ownership source. All matches are collected (not just
    the first) so the refusal can name them. `states` lets a caller that
    already loaded the run states (the sweep/archive passes) skip the
    re-read."""
    if states is None:
        states = []
        for sj in runs_dir.glob("*/state.json"):
            try:
                states.append((sj, json.loads(
                    sj.read_text(encoding="utf-8"))))
            except (OSError, ValueError):
                continue
    watchers: list[str] = []
    for sj, st in states:
        drv = driver_alive(st)
        for name, step in (st.get("steps") or {}).items():
            wd = step.get("work_dir") or step.get("project")
            if not wd or not step_live(step, drv):
                continue
            try:
                wd = Path(wd).resolve()
            except OSError:
                continue
            if any(p.resolve().is_relative_to(wd) for p in destinations):
                watchers.append(
                    _watcher_identity(sj.parent.name, name, step))
    # Sorted for a stable message: the run-dir prefix is timestamped, so
    # the order is also chronological and identical across callers/tests.
    watchers.sort()
    return watchers


def _watcher_summary(watchers: list[str]) -> str:
    """The parenthesized owner list in a watcher refusal - at most three
    identities spelled out, the rest counted."""
    shown = "; ".join(watchers[:3])
    if len(watchers) > 3:
        shown += f"; +{len(watchers) - 3} more"
    return shown


def _assert_unwatched(runs_dir: Path, *destinations: Path) -> None:
    watchers = _watchers_of(runs_dir, destinations)
    if watchers:
        raise RequestError(
            "A live agent is watching this workspace "
            f"({_watcher_summary(watchers)}); wait until it finishes "
            "before changing requests or manifests", 409)
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
            if step.merge_target:
                terr = merge_target_error(f"step {step.name!r}",
                                          project_dir, step.merge_target)
                if terr:
                    raise RequestError(terr)
        validate_groups(plan_groups([
            (*resolve_project(s, projects, manifests_dir), s)
            for s in cfg.steps], cfg))
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
    for members in plan_groups(resolved, cfg):
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
    Only keys present in `fields` are touched: {on_failure, merge_resolver,
    merge, summarize} write [run]; {model, effort, max_attempts} write
    [defaults] and are stripped from [[step]] sections so the form value
    wins; {worktrees} sets/clears per-step worktree (kept on same-project
    wave members and never added to cloud steps - both would fail
    validation); {merge} is stripped from [[step]]s so [run] is the sole
    policy - 'deferred' then implies worktree on every non-cloud step and
    writes a per-step merge = "auto" on cloud steps (deferred+cloud is a
    manifest error); {merge_target} writes [run] as the default or
    per-step keys on the selected project's worktree steps; {followup}
    adds/removes the closing follow-up-plan step. Request text and project
    are NOT applyable - those need regeneration."""
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
    if "parallel" in fields:
        _toml_set(lines, *ensure("run"), "parallel",
                  "true" if fields["parallel"] else "false")
        if fields["parallel"]:
            # parallel replaces wave grouping entirely - wave= keys on
            # steps would fail manifest validation, so they are stripped
            # (the pool supersedes them), and non-cloud steps take
            # worktree isolation the same way merge=deferred implies it.
            drop_wave = sorted((i for k, s, e in _toml_tables(lines)
                               if k == "step"
                               for i, key in _toml_keys(lines, s, e)
                               if key == "wave"), reverse=True)
            for i in drop_wave:
                del lines[i]
            for s in cfg.steps:
                if s.cloud:
                    continue
                ss, se = step_sect(s.name)
                if ss is not None:
                    _toml_set(lines, ss, se, "worktree", "true")
    if fields.get("merge") in ("auto", "deferred"):
        _toml_set(lines, *ensure("run"), "merge",
                  json.dumps(fields["merge"]))
    if "summarize" in fields:
        _toml_set(lines, *ensure("run"), "summarize",
                  "true" if fields["summarize"] else "false")
    if "merge_target" in fields:
        # The field binds to the project selector: with a project chosen
        # it writes/clears per-step keys on THAT project's worktree steps
        # (a run-level key would leak to every project, so it goes too;
        # a key on a non-worktree step would fail validation outright -
        # 'effective' below is the post-apply state, since the same Apply
        # may be adding worktree flags right now). With 'Infer from
        # request' it is the run-wide default target - [run].merge_target
        # applies to worktree steps only - and per-step keys stay as
        # overrides.
        mt_val = (fields.get("merge_target") or "").strip()
        mt_proj = (fields.get("project") or "").strip()
        wt_effective = {
            s.name for s in cfg.steps
            if s.worktree
            or (fields.get("worktrees") and not s.cloud)
            or (fields.get("parallel") and not s.cloud)
            or s.name in _wave_worktree_required(cfg, manifests_dir)}
        rs, re_ = sect("run")
        if mt_proj:
            if rs is not None:
                i = _toml_key_at(lines, rs, re_, "merge_target")
                if i is not None:
                    del lines[i]
            for s in cfg.steps:
                if s.project != mt_proj:
                    continue
                ss, se = step_sect(s.name)
                if ss is None:
                    continue
                if mt_val and s.name in wt_effective:
                    _toml_set(lines, ss, se, "merge_target",
                              json.dumps(mt_val))
                elif not mt_val:
                    i = _toml_key_at(lines, ss, se, "merge_target")
                    if i is not None:
                        del lines[i]
        elif mt_val:
            _toml_set(lines, *ensure("run"), "merge_target",
                      json.dumps(mt_val))
        elif rs is not None:
            i = _toml_key_at(lines, rs, re_, "merge_target")
            if i is not None:
                del lines[i]
    # Per-step overrides of fields the form owns would silently fight the
    # applied value - strip them so defaults/run keys are the sole source.
    drop_keys = ({"merge_resolver"} if "merge_resolver" in fields else
                 set()) | (
        {"merge"} if fields.get("merge") in ("auto", "deferred")
        else set()) | (
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
        if fields.get("parallel"):
            # parallel was applied above: every non-cloud step just took
            # worktree = true and same-project siblings REQUIRE it - an
            # unchecked worktrees box must not strip what parallel needs
            required |= {s.name for s in cfg.steps if not s.cloud}
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
    if fields.get("merge") == "deferred":
        # merge=deferred implies worktree on every step where isolation is
        # possible; on cloud steps a local retained branch is meaningless,
        # so they carry an explicit per-step merge = "auto" out of the
        # run-wide deferred policy (deferred+cloud fails validation).
        for s in cfg.steps:
            ss, se = step_sect(s.name)
            if ss is None:
                continue
            if s.cloud:
                _toml_set(lines, ss, se, "merge", '"auto"')
            else:
                _toml_set(lines, ss, se, "worktree", "true")
    if "followup" in fields:
        ss, se = step_sect("follow-up-plan")
        if fields["followup"] and ss is None and cfg.steps:
            slug = fields.get("project") or cfg.steps[-1].project
            lines.extend(FOLLOWUP_STEP.format(
                slug=slug, run=cfg.name, runs=RUNS_DIR.as_posix(),
                manifests=MANIFESTS_DIR.as_posix()).splitlines())
            # the step list was parsed before this append, so the worktree
            # pass above never saw the new section - a checked box must
            # reach it or the result is a mixed state the form can't say
            if fields.get("worktrees") or fields.get("parallel"):
                _toml_set(lines, *step_sect("follow-up-plan"),
                          "worktree", "true")
        elif not fields["followup"] and ss is not None:
            del lines[ss:se]
            if ss and ss - 1 < len(lines) and not lines[ss - 1].strip():
                del lines[ss - 1]
    return "\n".join(lines) + ("\n" if text.endswith("\n") else "")


def _guarantee_isolation(text: str, rec: dict,
                         manifests_dir: Path = MANIFESTS_DIR) -> str:
    """The worktrees/merge_resolver/merge-timing options and the
    merge-target/summarize fields are guarantees, not hints: add them
    when the planner omitted them. One-directional - an unchecked box
    never strips a deliberate flag, 'auto' merge timing never strips a
    hand-set merge key, and an empty merge target never removes a
    deliberate merge_target."""
    fields = {}
    if rec.get("worktrees"):
        fields["worktrees"] = True
    if rec.get("merge_resolver"):
        fields["merge_resolver"] = True
    if rec.get("merge") == "deferred":
        fields["merge"] = "deferred"
    if rec.get("summarize"):
        fields["summarize"] = True
    if rec.get("parallel"):
        fields["parallel"] = True
    if rec.get("merge_target"):
        fields["merge_target"] = rec["merge_target"]
        fields["project"] = rec.get("project") or ""
    if not fields:
        return text
    try:
        return _apply_fields_to_toml(text, fields, manifests_dir)
    except RequestError:
        return text   # unparseable planner output fails validation anyway


# ---------------------------------------------------------------- staleness
# A proposal is stale only while a form field semantically diverges from
# it - computed, never a sticky flag. The fields split by what reconciles
# them: GEN_INPUT_FIELDS feed the planner prompt (request text, project,
# image attachments - only Regenerate rebuilds the plan around them, so
# their baseline is a snapshot of what the planner saw); OPTION_FIELDS map
# to known TOML keys ('Apply options to TOML' patches them in place, and a
# regeneration writes them too - their baseline is the TOML itself, so a
# hand edit of the manifest moves the baseline on its own). Everything
# else - label, plan_model/plan_effort, ask - is cosmetic or feeds only
# the next planning pass and can never stale the current proposal.
GEN_INPUT_FIELDS = ("raw", "project", "attachments")
OPTION_FIELDS = ("model", "effort", "on_failure", "max_attempts",
                 "worktrees", "merge_resolver", "merge", "followup",
                 "merge_target", "parallel")


def _att_key(lst) -> list:
    return [(a.get("name") or "", a.get("path") or "")
            if isinstance(a, dict) else (str(a), "")
            for a in lst or []]


def _gen_inputs_of(rec: dict) -> dict:
    """The task-generation inputs as the planner saw them - stamped on the
    record when a proposal publishes, diffed against the live fields for
    the 'gen' pending set. Attachments compare by (name, path) pairs."""
    return {"raw": rec.get("raw") or "", "project": rec.get("project")
            or "", "attachments": [dict(a) for a in rec.get("attachments")
                                   or []]}


def _manifest_options(text: str, manifests_dir: Path) -> dict:
    """The option fields as the manifest TOML itself encodes them - the
    Apply baseline. Read at the level the form owns: [defaults] model /
    effort / max_attempts and [run] on_failure / merge_resolver (per-step
    overrides are beyond the form's vocabulary and not drift). worktrees
    reads True only when every non-cloud step is isolated and False when
    none beyond same-project-wave-required steps are; a mixed state is
    None - the checkbox cannot express it, so it drifts either way until
    reconciled. followup is the presence of a 'follow-up-plan' step."""
    doc = tomllib.loads(text)
    run, defaults = doc.get("run", {}), doc.get("defaults", {})
    cfg = load_manifest(manifests_dir / "pending.toml", text)
    cloud = {s.name for s in cfg.steps if s.cloud}
    required = _wave_worktree_required(cfg, manifests_dir)
    if all(s.worktree or s.name in cloud for s in cfg.steps):
        wt = True
    elif not any(s.worktree for s in cfg.steps if s.name not in required):
        # only wave-required steps are isolated (or none) - cloud steps
        # can never carry worktree (load_manifest rejects the pairing)
        wt = False
    else:
        wt = None          # mixed - Apply or a hand edit must reconcile it
    try:
        attempts = int(defaults.get("max_attempts"))
    except (TypeError, ValueError):
        attempts = 1       # load_manifest's own fallback for a missing key
    # The merge-target field reads run-level first, then a unanimous
    # per-step choice; differing per-step targets are a mixed state the
    # single field cannot express - None, drifting until reconciled.
    mt = run.get("merge_target")
    if mt is None:
        step_mts = {s.merge_target for s in cfg.steps if s.merge_target}
        mt = step_mts.pop() if len(step_mts) == 1 else (
            None if step_mts else "")
    elif not isinstance(mt, str):
        mt = None
    # Merge timing reads run-level first (what Apply writes), then a
    # unanimous effective policy across non-cloud steps (a cloud step's
    # forced merge="auto" under a deferred run is plumbing, not policy);
    # a mixed set is None - the single select cannot express it.
    mg = run.get("merge")
    if mg is None:
        eff = {s.merge for s in cfg.steps if not s.cloud}
        mg = eff.pop() if len(eff) == 1 else (None if eff else "auto")
    return {"model": defaults.get("model"),
            "effort": defaults.get("effort") or "",
            "on_failure": run.get("on_failure", "stop"),
            "max_attempts": attempts, "worktrees": wt,
            "merge_resolver": bool(run.get("merge_resolver", False)),
            "merge": mg,
            "parallel": bool(run.get("parallel", False)),
            "followup": any(s.name == "follow-up-plan" for s in cfg.steps),
            "merge_target": mt}


def _pending_actions(rec: dict) -> dict:
    """Semantic staleness for a manifested request, in two buckets:
    'gen' - task-generation inputs that diverged from what the published
            proposal was built around; only Regenerate resolves them.
    'opts' - option fields whose form value differs from what the TOML
            encodes; 'Apply options to TOML' (or a Regenerate) resolves.
    Both empty means the proposal matches the form. A pre-baseline record
    (no gen_inputs) keeps the old sticky flag as an opaque 'fields' gen
    marker until a publish re-baselines it; a manifest that does not read
    or parse reports the pseudo-key 'manifest' (Apply can't patch it)."""
    out = {"gen": [], "opts": [], "mixed": []}
    if not rec.get("manifest"):
        return out
    base = rec.get("gen_inputs")
    if base is None:
        if rec.get("stale"):
            out["gen"].append("fields")
    else:
        if (rec.get("raw") or "") != (base.get("raw") or ""):
            out["gen"].append("raw")
        if (rec.get("project") or "") != (base.get("project") or ""):
            out["gen"].append("project")
        if _att_key(rec.get("attachments")) != _att_key(
                base.get("attachments")):
            out["gen"].append("attachments")
    try:
        manifest = Path(rec["manifest"])
        opts = _manifest_options(
            manifest.read_text(encoding="utf-8"), manifest.parent)
    except (OSError, ManifestError, tomllib.TOMLDecodeError, ValueError,
            KeyError):
        opts = None
    if opts is None:
        out["opts"].append("manifest")
    else:
        form = {"model": rec.get("model") or "",
                "effort": rec.get("effort") or "",
                "on_failure": rec.get("on_failure") or "continue",
                "max_attempts": int(rec.get("max_attempts") or 3),
                "worktrees": bool(rec.get("worktrees")),
                "merge_resolver": bool(rec.get("merge_resolver")),
                "merge": rec.get("merge") or "auto",
                "parallel": bool(rec.get("parallel")),
                "followup": bool(rec.get("followup")),
                "merge_target": (rec.get("merge_target") or "").strip()}
        for key, value in form.items():
            bv = opts.get(key)
            if key == "model" and bv is None:
                bv = ""        # absent [defaults].model pins nothing
            # a None baseline on a mixed-capable key means the TOML
            # carries per-step values the single field cannot express -
            # unrepresentable, not drift: it must not flag 'stale' or
            # block launch (Apply can still normalize it on purpose)
            if key in _MIXED_OPTS and bv is None:
                out["mixed"].append(key)
            elif bv is None or value != bv:
                out["opts"].append(key)
    return out


# option keys where a None manifest baseline means "the TOML mixes
# per-step values" rather than "absent" - worktrees (some steps
# isolated, some not), merge (differing per-step timing), merge_target
# (differing per-step targets). The single form control cannot express
# those, so pendingNow flags them 'mixed' instead of 'changed'.
_MIXED_OPTS = {"worktrees", "merge", "merge_target"}


def _request_baseline(rec: dict) -> dict:
    """Per-field baseline values for the client's live diff - the gen
    inputs the proposal was built from and the option values the TOML
    encodes. A key is None when unknowable (pre-baseline record whose
    divergence was never attributed) or unexpressible (mixed worktrees);
    the client then keeps the server-computed pending verdict for it."""
    out = {k: None for k in GEN_INPUT_FIELDS + OPTION_FIELDS}
    if not rec.get("manifest"):
        return out
    base = rec.get("gen_inputs")
    if base is None and not rec.get("stale"):
        # A never-stale pre-baseline record's stored fields ARE the
        # publish-time fields - synthesize so the client diff works.
        base = _gen_inputs_of(rec)
    if base is not None:
        out.update({"raw": base.get("raw") or "",
                    "project": base.get("project") or "",
                    "attachments": [dict(a) for a in
                                    base.get("attachments") or []]})
    try:
        manifest = Path(rec["manifest"])
        out.update(_manifest_options(
            manifest.read_text(encoding="utf-8"), manifest.parent))
    except (OSError, ManifestError, tomllib.TOMLDecodeError, ValueError,
            KeyError):
        pass               # Nones left - the 'manifest' pending key says why
    return out


def _clamp_description(text: str) -> str:
    """Trim an overlong [run].description to the 300-char cap before it
    can dead-end a publish. A planner/worker that overshoots by one
    sentence otherwise wedges the record 'error' forever - every save,
    apply and regenerate re-fails on the same line. The cap itself is
    still enforced by validate_proposal (empty still fails); this only
    rescues the length, preserving the sentence's leading content.
    No-op on unparseable text - the validator reports that instead."""
    try:
        doc = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        return text
    desc = doc.get("run", {}).get("description")
    if not isinstance(desc, str) or len(desc) <= 300:
        return text
    lines = text.splitlines()
    run = next((t for t in _toml_tables(lines) if t[0] == "run"), None)
    if not run:
        return text
    i = _toml_key_at(lines, run[1], run[2], "description")
    if i is None:
        return text
    # the value may span several lines (a ''' or triple-quoted literal) -
    # replace the whole span, not just the key's first line
    m = re.match(r"^\s*description\s*=\s*(.*)$", lines[i])
    val = m.group(1) if m else ""
    end = i + 1
    for q in ("'''", '"""'):
        if val.startswith(q) and val.count(q) == 1:
            j = i + 1
            while j < run[2] and q not in lines[j]:
                j += 1
            end = min(j + 1, run[2])
            break
    lines[i:end] = ["description = " +
                    json.dumps(desc[:297].rstrip() + "...")]
    out = "\n".join(lines)
    return out + ("\n" if text.endswith("\n") else "")


def _publish_proposal(rec: dict, text: str, requests_dir: Path,
                      manifests_dir: Path, runs_dir: Path) -> dict:
    text = _clamp_description(text)
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
    if rec.get("worktrees") or rec.get("merge_resolver") \
            or rec.get("merge") == "deferred" or rec.get("merge_target"):
        # The checkboxes/fields are guarantees: add what the planner omitted.
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
               gen_inputs=_gen_inputs_of(rec),
               pending_questions=[],
               proposed_steps=[{
                   "step": s.name, "slug": s.project, "model": s.model,
                   "effort": s.effort, "mode": s.mode,
                   "task": task_summary(s.prompt), "wt": s.worktree,
                   "mt": s.merge_target, "merge": s.merge,
                   "cloud": s.cloud,
                   "deps": list(s.depends_on) or None} for s in cfg.steps])
    # The gen baseline moved to what this publish was built from, but the
    # option fields can still diverge from a hand-edited TOML - compute
    # rather than assume clean.
    pend = _pending_actions(rec)
    rec["stale"] = bool(pend["gen"] or pend["opts"])
    _save_request(rec, requests_dir)
    rec["pending"] = pend      # derived, not persisted - callers diff live
    return dict(rec)


def save_request(request_id: str, payload: dict,
                 requests_dir: Path = REQUESTS_DIR,
                 manifests_dir: Path = MANIFESTS_DIR,
                 runs_dir: Path = RUNS_DIR) -> dict:
    with REQUEST_LOCK:
        _assert_unwatched(runs_dir, requests_dir, manifests_dir)
        rec = _read_request(request_id, requests_dir)
        stage = _request_status(rec, runs_dir)
        if stage not in _REQUEST_EDITABLE_STAGES:
            raise RequestError("This request is being processed or already run", 409)
        rec["status"] = stage
        fields = _request_fields(payload, rec)
        if rec.get("manifest") and "gen_inputs" not in rec \
                and not rec.get("stale"):
            # Pre-baseline record: never-stale means the stored fields
            # still describe the published proposal - pin them as the
            # diff baseline BEFORE this save can move them. A record
            # already marked stale can't be attributed this way; it
            # keeps the opaque 'fields' marker until a publish lands.
            rec["gen_inputs"] = _gen_inputs_of(rec)
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
        # Staleness is a live diff, not a latched flag: _pending_actions
        # diffs generation inputs against the publish-time gen_inputs and
        # option fields against the TOML itself, so reverting a field to
        # its baseline clears the mark without any publish.
        pend = _pending_actions(rec)
        rec["stale"] = bool(pend["gen"] or pend["opts"])
        _save_request(rec, requests_dir)
        rec["pending"] = pend
        return dict(rec)


def apply_request(request_id: str, payload: dict,
                  requests_dir: Path = REQUESTS_DIR,
                  manifests_dir: Path = MANIFESTS_DIR,
                  runs_dir: Path = RUNS_DIR) -> dict:
    """Save the form fields AND rewrite the proposal TOML to match - the
    deterministic alternative to regeneration. Refused when a generation
    input (request text, target project, attachments) diverges from what
    the proposal was built around: those shape the step prompts, which a
    deterministic patch cannot rewrite. The diff runs against the
    publish-time gen_inputs baseline, so a divergence that was saved in
    an earlier edit (or reverted since) is judged correctly - not against
    the record's last save."""
    with REQUEST_LOCK:
        _assert_unwatched(runs_dir, requests_dir, manifests_dir)
        rec = _read_request(request_id, requests_dir)
        stage = _request_status(rec, runs_dir)
        if stage not in _REQUEST_EDITABLE_STAGES:
            raise RequestError("This request is being processed or already run",
                               409)
        if not rec.get("manifest"):
            raise RequestError("No proposal yet - generate one first", 409)
        rec["status"] = stage
        fields = _request_fields(payload, rec)
        base = rec.get("gen_inputs")
        if base is None:
            # Pre-baseline record: fall back to comparing against the
            # stored fields (the only reference the old flag left).
            moved = (["request text"] if fields["raw"] != rec.get("raw")
                     else []) + (["project"] if fields["project"]
                                 != rec.get("project") else [])
        else:
            moved = [label for key, label in
                     (("raw", "request text"), ("project", "project"),
                      ("attachments", "attachments"))
                     if (_att_key(fields[key]) if key == "attachments"
                         else fields[key] or "")
                     != (_att_key(base.get(key)) if key == "attachments"
                         else base.get(key) or "")]
        if moved:
            raise RequestError(
                "The " + " and ".join(moved) + " changed since this "
                "proposal was generated - step prompts depend on them; "
                "regenerate the proposal instead", 409)
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


# ------------------------------------------------------------- follow-up intake
# A run's closing follow-up-plan step is asked to write
# manifests/<run>-followup.toml (a worktree producer's own checkout also
# works - the prompt path is absolute, but workers have committed the file
# on their branch before). Discovery turns that file into an ordinary
# proposal request - status 'ready' - so it lands in the Runs section for a
# human to review, edit, Start or Discard. Registration NEVER launches:
# the row is a proposal like any other, and only the separate Start run
# click runs it. Outputs that cannot be a launchable proposal still land
# as records so nothing is silently lost:
#   ready   - the file parses and passes the whole proposal gate
#   empty   - the file parses but declares no [[step]] (nothing remains),
#             or the step ended reporting nothing left to write
#   error   - the file exists but is malformed or fails validation
#   missing - the follow-up-plan step claims completion but wrote no file
# state["followup"] links the run row forward to the request; the request's
# source_run/source_manifest fields link it back and keep the original file
# reachable (the request owns a COPY - the source is only ever read).
FOLLOWUP_INDEX = "followups.index"   # data/requests/ ledger - JSON inside,
                                     # named .index so *.json globs in the
                                     # request paths never misread it as a
                                     # request record
_FOLLOWUP_POLL_S = 60.0              # collect_dashboard rescan throttle
_FOLLOWUP_TRIED = 0.0
# Owned proposal copies are <request-id>-<run>.toml - the id prefix is the
# orphan scan's exclusion so a copy can never re-import as a new proposal.
_FOLLOWUP_OWNED_RE = re.compile(r"^\d{8}-\d{6}-[0-9a-f]{8}-.+\.toml$")
# The step's own report saying there was nothing left to write - consulted
# only when it left no file at all (a zero-step TOML is the stronger,
# file-backed 'empty' signal).
_FOLLOWUP_EMPTY_RE = re.compile(
    r"\b(?:no (?:remaining|further|unfinished|follow[- ]?up|work)|"
    r"nothing (?:left|remains|to do|to propose|else)|"
    r"no work (?:left|remains)|nothing more to do|"
    r"all (?:the )?work (?:is )?(?:done|complete))\b", re.I)
# Stages a follow-up record can be in that still allow form edits/discard -
# the classic set plus the two terminal intake statuses.
_REQUEST_EDITABLE_STAGES = ("draft", "ready", "error", "launch_failed",
                            "questions", "empty", "missing")


def _followup_index_path(requests_dir: Path) -> Path:
    return requests_dir / FOLLOWUP_INDEX


def _followup_index(requests_dir: Path) -> dict:
    """The follow-up registration ledger: source key -> {request, sha,
    discarded, ...}. It is what makes registration idempotent across
    retries, rescans and restarts - and makes Discard a durable veto (a
    discarded source never silently re-registers; the explicit import path
    can always override)."""
    try:
        doc = json.loads(
            _followup_index_path(requests_dir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"entries": {}}
    if not isinstance(doc, dict) or not isinstance(doc.get("entries"), dict):
        return {"entries": {}}
    return doc


def _save_followup_index(requests_dir: Path, index: dict) -> None:
    requests_dir.mkdir(parents=True, exist_ok=True)
    p = _followup_index_path(requests_dir)
    tmp = p.with_suffix(".index.tmp")
    tmp.write_text(json.dumps(index, indent=2), encoding="utf-8")
    tmp.replace(p)


def _followup_mark(requests_dir: Path, request_id: str,
                   discarded: bool) -> None:
    """Flip the veto mark on every ledger entry that produced this request:
    Discard sets it (the source stays parked), Restore clears it."""
    index = _followup_index(requests_dir)
    changed = False
    for ent in index["entries"].values():
        if ent.get("request") == request_id and \
                bool(ent.get("discarded")) != discarded:
            ent["discarded"] = discarded
            ent["updated_at"] = now_iso()
            changed = True
    if changed:
        _save_followup_index(requests_dir, index)


def _followup_sources(st: dict, owner_md: Path) -> list[Path]:
    """Existing files a run's follow-up output could be, canonical first:
    the checkout manifests/<run>-followup.toml the prompt asks for (its
    {manifests} interpolation is the DRIVER's own checkout manifests
    dir), the dir the run's manifest lived in (a driver in another
    checkout wrote there), and the same relative path inside the step's
    worktree (a worktree producer can commit it on its branch)."""
    name = st.get("run") or ""
    if not name:
        return []
    rec = (st.get("steps") or {}).get("follow-up-plan") or {}
    cands = [owner_md / f"{name}-followup.toml"]
    if st.get("manifest"):
        p = Path(st["manifest"]).resolve().parent / f"{name}-followup.toml"
        if p not in cands:
            cands.append(p)
    wd = rec.get("work_dir")
    if wd:
        p = Path(wd) / "manifests" / f"{name}-followup.toml"
        if p not in cands:
            cands.append(p)
    return [p for p in cands if p.is_file()]


def _classify_followup(text: str, manifests_dir: Path,
                       doc: dict | None = None
                       ) -> tuple[str, "Manifest | None", str | None]:
    """(status, cfg, error) for a written follow-up file. 'ready' means it
    passed the whole proposal gate - parse, run section, registered slugs,
    git-repo targets, wave shape, execution options. 'empty' is a parseable
    TOML declaring no [[step]] - the worker's file-backed way to say
    nothing remains; it must never read as a runnable job. Anything else
    is 'error', kept for review rather than dropped."""
    if doc is None:
        try:
            doc = tomllib.loads(text)
        except tomllib.TOMLDecodeError as exc:
            return "error", None, f"not valid TOML: {exc}"
    if not isinstance(doc, dict):
        return "error", None, "not a TOML document"
    steps = doc.get("step")
    if steps is None or steps == []:
        return "empty", None, None
    if not isinstance(steps, list):
        return "error", None, "a [step] table is not a [[step]] list"
    try:
        cfg = validate_proposal(text, manifests_dir)
    except RequestError as exc:
        return "error", None, str(exc)
    return "ready", cfg, None


def _followup_rec_base(source: Path | None, run_ctx: dict | None) -> dict:
    """The record skeleton every follow-up intake shares. Option fields
    start neutral; a 'ready' classification then syncs them to what the
    TOML itself encodes (the same baseline _manifest_options gives the
    pending diff, so a fresh import is not born 'stale')."""
    run_ctx = run_ctx or {}
    run_name = run_ctx.get("run") or ""
    return {"status": "draft", "created_at": now_iso(),
            "manifest": None, "description": "",
            "run": f"{run_name}-followup" if run_name else "",
            "proposed_steps": [], "stale": False, "error": None,
            "label": f"follow-up: {run_name}" if run_name else "",
            "raw": (f"Follow-up drafted by run {run_ctx.get('dir')}'s "
                    "follow-up-plan step."
                    if run_ctx.get("dir") else "Imported proposal manifest."),
            "project": "", "attachments": [], "model": "", "effort": "",
            "on_failure": "continue", "max_attempts": 1,
            "followup": False, "worktrees": False, "merge_resolver": False,
            "summarize": False, "ask": False, "parallel": False,
            "imported": "followup",
            "source_run": run_ctx.get("dir"),
            "source_run_key": (f"request:{run_ctx.get('request')}"
                               if run_ctx.get("request")
                               else run_ctx.get("path")),
            "source_request": run_ctx.get("request"),
            "source_step": run_ctx.get("step"),
            "source_manifest": str(source) if source else None,
            "pending_questions": []}


def _followup_apply(rec: dict, text: str, manifests_dir: Path,
                    requests_dir: Path, runs_dir: Path) -> str:
    """Copy the source text into the request's owned manifest and set the
    status fields - 'ready'/'empty'/'error'. The owned copy keeps the
    request inside the normal proposal machinery (_manifest_path, Apply,
    Discard, launch); the source file is only ever read."""
    _assert_unwatched(runs_dir, requests_dir, manifests_dir)
    # an overlong [run].description is a dead record, not a defect worth
    # keeping: the owned copy gets the clamped text, the worker's source
    # file is untouched
    text = _clamp_description(text)
    try:
        doc = tomllib.loads(text)
    except tomllib.TOMLDecodeError:
        doc = None
    status, cfg, err = _classify_followup(text, manifests_dir, doc)
    run_doc = doc.get("run") if isinstance(doc, dict) else None
    run_doc = run_doc if isinstance(run_doc, dict) else {}
    name = cfg.name if cfg else str(run_doc.get("name") or "")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,59}", name):
        name = _safe_name(Path(
            rec.get("source_manifest") or "followup").stem)[:60]
    if status == "empty":
        # Nothing to launch or review in TOML terms: keep the source
        # pointer, no owned copy, no steps - the row reads 'No work
        # remains' and can never drift stale or be started.
        rec.update(manifest=None, proposed_steps=[],
                   run=name or rec.get("run"),
                   description=str(run_doc.get("description") or "")[:300],
                   error=None, stale=False)
        rec["status"] = "empty"
        return "empty"
    p = manifests_dir / f"{rec['id']}-{name}.toml"
    tmp = p.with_suffix(".toml.tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(p)
    # A re-import under a new [run].name leaves the previous owned copy
    # behind - drop it so manifests/ keeps one file per registered
    # proposal (only ever <id>-prefixed, never the source).
    old = Path(rec["manifest"]) if rec.get("manifest") else None
    if old is not None and old != p \
            and old.name.startswith(f"{rec['id']}-") \
            and old.parent.resolve() == manifests_dir.resolve():
        old.unlink(missing_ok=True)
    rec["manifest"] = str(p)
    if status == "ready":
        opts = _manifest_options(text, manifests_dir)
        rec.update(
            status="ready", run=cfg.name,
            description=cfg.description.strip(), error=None,
            gen_inputs=_gen_inputs_of(rec), pending_questions=[],
            model=opts.get("model") or "", effort=opts.get("effort") or "",
            on_failure=opts.get("on_failure") or "continue",
            max_attempts=opts.get("max_attempts") or 1,
            worktrees=bool(opts.get("worktrees")),
            merge_resolver=bool(opts.get("merge_resolver")),
            parallel=bool(opts.get("parallel")),
            followup=bool(opts.get("followup")),
            summarize=cfg.summarize is not None,
            proposed_steps=[{
                "step": s.name, "slug": s.project, "model": s.model,
                "effort": s.effort, "mode": s.mode,
                "task": task_summary(s.prompt), "wt": s.worktree,
                "cloud": s.cloud,
                "deps": list(s.depends_on) or None} for s in cfg.steps])
        pend = _pending_actions(rec)
        rec["stale"] = bool(pend["gen"] or pend["opts"])
    else:
        # Kept for review: the broken text stays inspectable in the modal
        # through the owned copy; 'manifest' in pending names the culprit.
        rec.update(status="error", run=name or rec.get("run"),
                   proposed_steps=[],
                   error=(f"Follow-up manifest failed validation: {err}"
                          if err else
                          "Follow-up manifest failed validation")[:300],
                   stale=False)
    return status


def _followup_entry(source: Path | None, rec: dict, sha: str | None,
                    run_ctx: dict | None) -> dict:
    ent = {"request": rec["id"], "sha": sha, "discarded": False,
           "status": rec.get("status"), "updated_at": now_iso()}
    if source is not None:
        ent["source"] = str(source.resolve())
    if run_ctx:
        ent.update(run=run_ctx.get("dir"), step=run_ctx.get("step"))
    return ent


def _register_followup_source(source: Path, run_ctx: dict | None,
                              index: dict, requests_dir: Path,
                              manifests_dir: Path, runs_dir: Path,
                              force: bool = False
                              ) -> tuple[str, dict | None]:
    """Register a written follow-up file as a proposal request - the whole
    idempotency story in one place. Keyed on the resolved path: a rescanned
    unchanged file is a no-op, a rewritten file re-publishes into the same
    record (retries, merges landing the worktree copy), a discarded source
    stays vetoed unless force, and a consumed record (launched, or owned by
    a planner pass) is left alone. Raises RequestError on the trap-5
    watched-dir guard - the caller defers or reports."""
    key = "file:" + str(source.resolve())
    ent = index["entries"].get(key)
    if ent and ent.get("discarded") and not force:
        return "discarded", None
    try:
        text = source.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        # a binary/garbled drop still lands as a reviewable 'error'
        text = source.read_text(encoding="utf-8", errors="replace")
    sha = hashlib.sha1(text.encode("utf-8")).hexdigest()
    rec = None
    if ent:
        try:
            rec = _read_request_file(ent["request"], requests_dir)
        except RequestError:
            rec = None
    alt_key = None
    if rec is None and run_ctx:
        # the file resolves a 'missing'/'empty' outcome record this same
        # (run, step) already landed - fold it in place so the one logical
        # row flips to its real status instead of shadowing a stale twin
        alt_key = f"run:{run_ctx['dir']}:{run_ctx['step']}"
        aent = index["entries"].get(alt_key)
        if aent and not aent.get("discarded"):
            try:
                rec = _read_request_file(aent["request"], requests_dir)
            except RequestError:
                rec = None
            if rec is not None and \
                    rec.get("status") not in ("missing", "empty"):
                rec = None          # not ours to fold - register normally
    if rec is not None:
        stage = _request_status(rec, runs_dir)
        if stage in ("launched", "launching", "generating", "questions"):
            return f"kept:{stage}", rec
        if ent and ent.get("sha") == sha and \
                rec.get("status") == ent.get("status"):
            # the source is unchanged: leave the record (and any human
            # edits to the owned copy) alone - only a MISSING owned copy
            # is quietly re-copied
            owned = rec.get("manifest")
            if not owned:
                return "unchanged", rec
            p = Path(owned)
            if p.is_file():
                return "unchanged", rec
            _assert_unwatched(runs_dir, requests_dir, manifests_dir)
            tmp = p.with_suffix(".toml.tmp")
            tmp.write_text(text, encoding="utf-8")
            tmp.replace(p)
            return "repaired", rec
        if ent and ent.get("sha") != sha and rec.get("manifest"):
            # the source moved - but a human edit to the owned copy wins:
            # the file on disk no longer matches what we last imported,
            # so re-copying would clobber their work. The request's own
            # pending state already surfaces that divergence.
            p = Path(rec["manifest"])
            if p.is_file():
                try:
                    cur = hashlib.sha1(p.read_text(
                        encoding="utf-8").encode("utf-8")).hexdigest()
                except (OSError, UnicodeDecodeError):
                    cur = None
                if cur and cur != ent.get("sha") and cur != sha:
                    return "edited", rec
        status = _followup_apply(rec, text, manifests_dir, requests_dir,
                                 runs_dir)
        if run_ctx:
            rec.update(source_run=run_ctx.get("dir")
                       or rec.get("source_run"),
                       source_run_key=(f"request:{run_ctx['request']}"
                                       if run_ctx.get("request")
                                       else run_ctx.get("path"))
                       or rec.get("source_run_key"),
                       source_request=run_ctx.get("request")
                       or rec.get("source_request"),
                       source_step=run_ctx.get("step")
                       or rec.get("source_step"))
        rec["source_manifest"] = str(source)
        _save_request(rec, requests_dir)
        entry = _followup_entry(source, rec, sha, run_ctx)
        index["entries"][key] = entry
        if alt_key is not None:
            index["entries"][alt_key] = dict(entry)
        _save_followup_index(requests_dir, index)
        return "updated", rec
    rec = _followup_rec_base(source, run_ctx)
    rec["id"] = (f"{dt.datetime.now():%Y%m%d-%H%M%S}-"
                 f"{secrets.token_hex(4)}")
    status = _followup_apply(rec, text, manifests_dir, requests_dir,
                             runs_dir)
    _save_request(rec, requests_dir)
    index["entries"][key] = _followup_entry(source, rec, sha, run_ctx)
    _save_followup_index(requests_dir, index)
    return "registered", rec


def _followup_empty_signal(step_rec: dict) -> bool:
    """The step left no file - did it say there was nothing to write?
    Reads its own report lines (recorded fields plus the log's OUTCOME/
    NEXT), never guesses."""
    parts = [str(step_rec.get(k) or "") for k in
             ("outcome", "next", "status_line", "note")]
    log = step_rec.get("stdout_log")
    if log:
        parts += [tail_outcome(Path(log)) or "",
                  tail_next(Path(log)) or ""]
    return bool(_FOLLOWUP_EMPTY_RE.search(" ".join(parts)))


def _register_followup_outcome(run_ctx: dict, step_rec: dict,
                               expected: Path, index: dict,
                               requests_dir: Path, manifests_dir: Path,
                               runs_dir: Path,
                               force: bool = False
                               ) -> tuple[str, dict | None]:
    """No file was produced: land the outcome as its own dismissible record
    so a finished follow-up-plan step can never silently lose its result.
    'empty' when the step's report says nothing remained; 'missing' when it
    claimed completion without the artifact."""
    key = f"run:{run_ctx['dir']}:{run_ctx['step']}"
    s = step_rec.get("status")
    if s not in ("done", "partial"):
        # a step that failed/stopped/timed out never claimed an artifact -
        # the run row already carries that failure; the stamp only
        return "absent", None
    ent = index["entries"].get(key)
    if ent and ent.get("discarded") and not force:
        return "discarded", None
    status = "empty" if _followup_empty_signal(step_rec) else "missing"
    error = (
        None if status == "empty" else
        (f"The follow-up-plan step finished "
         f"({step_rec.get('status')}) but wrote no manifest - expected "
         f"{expected}. Import it with `python conductor.py import <file>` "
         "once it exists, or Discard this record."))
    rec = None
    if ent:
        try:
            rec = _read_request_file(ent["request"], requests_dir)
        except RequestError:
            rec = None
    if rec is not None:
        stage = _request_status(rec, runs_dir)
        if stage in ("launched", "launching", "generating", "questions"):
            return f"kept:{stage}", rec
        if rec.get("status") == status and rec.get("error") == error \
                and not rec.get("manifest"):
            return "unchanged", rec
        _assert_unwatched(runs_dir, requests_dir)
        rec.update(status=status, error=error, manifest=None,
                   proposed_steps=[], stale=False)
        _save_request(rec, requests_dir)
        index["entries"][key] = _followup_entry(None, rec, None, run_ctx)
        _save_followup_index(requests_dir, index)
        return "updated", rec
    _assert_unwatched(runs_dir, requests_dir)
    rec = _followup_rec_base(None, run_ctx)
    rec["id"] = (f"{dt.datetime.now():%Y%m%d-%H%M%S}-"
                 f"{secrets.token_hex(4)}")
    rec.update(status=status, error=error,
               description=("follow-up-plan reported no remaining work"
                            if status == "empty" else
                            "follow-up-plan wrote no manifest"))
    _save_request(rec, requests_dir)
    index["entries"][key] = _followup_entry(None, rec, None, run_ctx)
    _save_followup_index(requests_dir, index)
    return "registered", rec


def _followup_stamp(st: dict, outcome: str, rec: dict | None,
                    source: Path | None) -> tuple[dict, bool]:
    """The run-side half of the link: what this run's follow-up-plan step
    left behind, rendered as a small chip on the run row. Returns (stamp,
    unchanged) - 'at' refreshes only when the substance moves, so a quiet
    rescan doesn't rewrite state.json forever."""
    status = (rec.get("status") if rec else
              {"missing": "missing", "deferred": "deferred",
               "discarded": "discarded", "absent": "absent"}.get(outcome))
    if status == "ready":
        status = "registered"
    fu = {"status": status or outcome, "at": now_iso()}
    if rec is not None:
        fu["request"] = rec["id"]
    if source is not None:
        fu["source"] = str(source)
    old = st.get("followup") or {}
    st["followup"] = fu
    same = {k: v for k, v in old.items() if k != "at"} == \
        {k: v for k, v in fu.items() if k != "at"}
    return fu, same


def _followup_source_run(src: Path, runs_dir: Path) -> Path | None:
    """Best-effort originating run dir for an imported file: the newest
    <ts>-<name> dir for a <name>-followup.toml stem."""
    m = re.fullmatch(r"(.+)-followup", src.stem)
    if not m:
        return None
    cands = [d for d in runs_dir.glob(f"*-{m.group(1)}")
             if d.is_dir() and (d / "state.json").is_file()]
    return max(cands, key=lambda d: d.name) if cands else None


def import_followup_file(path, requests_dir: Path = REQUESTS_DIR,
                         manifests_dir: Path = MANIFESTS_DIR,
                         runs_dir: Path = RUNS_DIR,
                         *, run_dir: str | None = None,
                         force: bool = True) -> dict:
    """The explicit import/recovery path: register an already-written
    follow-up (or any proposal) TOML as a request record. Idempotent on the
    resolved source path; the default force honours a deliberate import
    over a past Discard veto. Links the record to its originating run when
    one can be named or derived. Never launches anything."""
    if path is None or not str(path).strip():
        raise RequestError("Missing manifest path")
    src = Path(path)
    if not src.is_file():
        raise RequestError(f"Follow-up file not found: {src}", 404)
    with REQUEST_LOCK:
        index = _followup_index(requests_dir)
        run_ctx = None
        rd = (runs_dir / run_dir) if run_dir else _followup_source_run(
            src, runs_dir)
        if rd is not None and (rd / "state.json").is_file():
            try:
                st = json.loads(
                    (rd / "state.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                st = {}
            run_ctx = {"run": st.get("run"), "dir": rd.name,
                       "request": st.get("request_id"),
                       "step": "follow-up-plan", "path": str(rd)}
        outcome, rec = _register_followup_source(
            src, run_ctx, index, requests_dir, manifests_dir, runs_dir,
            force=force)
        if rec is None:
            raise RequestError(
                f"The follow-up could not be registered ({outcome})", 409)
        return dict(rec)


def _followup_owner_dir(runs_dir: Path) -> Path:
    """The manifests dir a data tree owns: the checkout's own manifests/
    when runs_dir is this checkout's data tree (ROOT/data/runs);
    <checkout>/manifests for another checkout's data/runs; the flat
    sibling 'manifests' dir for a test sandbox - a dashboard must never
    reach into a tree it doesn't serve."""
    try:
        if runs_dir.resolve().parent == RUNS_DIR.resolve().parent:
            return MANIFESTS_DIR
    except OSError:
        pass
    for cand in (runs_dir.parent.parent / "manifests",
                     runs_dir.parent / "manifests"):
        if cand.is_dir():
            return cand.resolve()
    return runs_dir.parent / "manifests"


def discover_followups(runs_dir: Path = RUNS_DIR,
                       requests_dir: Path | None = None,
                       manifests_dir: Path | None = None,
                       *, run_dir: Path | None = None,
                       scan_orphans: bool = True,
                       manifests_dirs: list[Path] | None = None
                       ) -> list[str]:
    """Register produced follow-up manifests as proposal requests -
    idempotent across run_end, serve startup, sweep and the throttled
    dashboard rescan. With run_dir set, only that run's step is examined
    (the driver's end-of-run path); a full scan also sweeps
    manifests_dirs (default: the owner dir) for files no recorded run
    produced - other checkouts, pre-discovery runs. Each run's SOURCE is
    found beside its own recorded manifest (a worktree-driven run resolves
    to that tree), while copies + validation land in the owner dir.
    Registration only - nothing here can launch work."""
    requests_dir = requests_dir or runs_dir.parent / "requests"
    owner_md = manifests_dir or _followup_owner_dir(runs_dir)
    manifests_dirs = ([Path(d) for d in manifests_dirs]
                      if manifests_dirs is not None else [owner_md])
    lines: list[str] = []
    with REQUEST_LOCK:
        index = _followup_index(requests_dir)
        sjs = ([run_dir / "state.json"] if run_dir else
               sorted(runs_dir.glob("*/state.json"), reverse=True))
        for sj in sjs:
            try:
                st = json.loads(sj.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            rd = sj.parent
            steprec = (st.get("steps") or {}).get("follow-up-plan")
            if steprec is None:
                continue
            live = step_live(steprec, driver_alive(st))
            # Produced is produced: an existing file registers even while
            # the step record is mid-flight; only its absence waits on a
            # terminal step state.
            sources = _followup_sources(st, owner_md)
            source = (max(sources, key=lambda p: p.stat().st_mtime)
                      if sources else None)
            run_ctx = {"run": st.get("run"), "dir": rd.name,
                       "request": st.get("request_id"),
                       "step": "follow-up-plan", "path": str(rd)}
            try:
                if source is not None:
                    outcome, rec = _register_followup_source(
                        source, run_ctx, index, requests_dir,
                        owner_md, runs_dir)
                elif not live and (steprec.get("ended_at")
                                   or st.get("ended_at")):
                    outcome, rec = _register_followup_outcome(
                        run_ctx, steprec,
                        owner_md / f"{st.get('run')}-followup.toml",
                        index, requests_dir, owner_md, runs_dir)
                else:
                    continue
            except (RequestError, OSError) as exc:
                # a busy checkout (live watcher, open merge) defers - the
                # next pass retries instead of losing the output
                outcome, rec = "deferred", None
                st["followup"] = {"status": "deferred", "at": now_iso(),
                                  "note": str(exc)[:160]}
                save_state(rd, st)
                lines.append(f"{rd.name}: follow-up registration deferred "
                             f"({exc})")
                continue
            fu, same = _followup_stamp(st, outcome, rec, source)
            if not same:
                save_state(rd, st)
            # log once when the substance moves: a fresh registration, a
            # re-import, or a stamp transition (incl. discarded/vetoed);
            # steady-state passes stay quiet
            if not same or outcome in ("registered", "updated", "repaired",
                                       "absent"):
                log_event(rd, event=f"followup_{outcome}",
                          status=fu["status"],
                          request=(rec or {}).get("id"),
                          source=str(source) if source else None)
                lines.append(f"{rd.name}: follow-up {fu['status']}"
                             + (f" -> request {rec['id']}" if rec else ""))
        if scan_orphans:
            owned = set()
            for rf in requests_dir.glob("*.json"):
                try:
                    r = json.loads(rf.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                if r.get("manifest"):
                    owned.add(str(Path(r["manifest"]).resolve()))
            for md in manifests_dirs:
                for p in sorted(md.glob("*-followup.toml")):
                    if _FOLLOWUP_OWNED_RE.match(p.name) or \
                            str(p.resolve()) in owned:
                        continue   # a request's own copy - never re-imports
                    key = "file:" + str(p.resolve())
                    if key in index["entries"]:
                        continue  # already registered (its run or before)
                    rd = _followup_source_run(p, runs_dir)
                    run_ctx = None
                    if rd is not None:
                        try:
                            ost = json.loads(
                                (rd / "state.json").read_text(
                                    encoding="utf-8"))
                            run_ctx = {"run": ost.get("run"),
                                       "dir": rd.name,
                                       "request": ost.get("request_id"),
                                       "step": "follow-up-plan",
                                       "path": str(rd)}
                        except (OSError, ValueError):
                            pass
                    try:
                        outcome, rec = _register_followup_source(
                            p, run_ctx, index, requests_dir, owner_md,
                            runs_dir)
                    except (RequestError, OSError) as exc:
                        lines.append(f"{p.name}: follow-up import deferred "
                                     f"({exc})")
                        continue
                    if outcome in ("registered", "updated"):
                        lines.append(
                            f"{p.name}: follow-up {rec['status']} -> "
                            f"request {rec['id']}")
    return lines


def _auto_followups(runs_dir: Path, requests_dir: Path) -> None:
    """Throttled rescan for a long-lived serve: a follow-up file landing
    after the driver already left (a late write, a merge landing the
    worktree copy, a run this process never drove) still registers within
    a minute."""
    global _FOLLOWUP_TRIED
    if time.time() - _FOLLOWUP_TRIED < _FOLLOWUP_POLL_S:
        return
    _FOLLOWUP_TRIED = time.time()
    try:
        discover_followups(runs_dir, requests_dir)
    except Exception:
        LOG.exception("follow-up discovery failed")


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
        pend = _pending_actions(rec)
        rec["pending"] = pend
        rec["stale"] = bool(pend["gen"] or pend["opts"])
        rec["baseline"] = _request_baseline(rec)
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
    if rec.get("merge") == "deferred":
        isolate += ("The human asked for held merges: set merge = "
                    "\"deferred\" in [run]. Steps still run in worktrees "
                    "(deferred implies worktree = true) but NOTHING merges "
                    "back automatically - verified branches stay pending "
                    "for a human-requested merge at the end, and "
                    "sequential steps on one project still see their "
                    "verified predecessors through a per-run integration "
                    "baseline. On any cloud = true step add a per-step "
                    "merge = \"auto\" (deferred keeps a local branch, "
                    "which makes no sense for cloud work).\n\n")
    if rec.get("merge_resolver"):
        isolate += ("The human asked for automatic conflict resolution: "
                    "set merge_resolver = true in [run], so a conflicted "
                    "worktree merge is finished by a resolve-<step> "
                    "session instead of only being parked.\n\n")
    if rec.get("parallel"):
        isolate += ("The human asked for fully parallel execution: set "
                    "parallel = true under [run] - every [[step]] then "
                    "schedules concurrently, bounded by the machine-wide "
                    "agent slot pool, and merges still serialize per repo "
                    "after the pool drains. With parallel set do NOT use "
                    "wave = keys (they are redundant and fail validation), "
                    "give every non-cloud [[step]] worktree = true, and "
                    "write each prompt so steps own distinct files or "
                    "areas - parallel worktrees that edit the same lines "
                    "conflict at merge time. A closing 'follow-up-plan' "
                    "step still runs last, after the pool drains.\n\n")
    if (rec.get("merge_target") or "").strip():
        mt = rec["merge_target"].strip()
        scope = (f"the [[step]]s on project {rec['project']!r}"
                 if rec.get("project")
                 else "every worktree [[step]] (set it under [run] as the "
                      "default; a step may override it for a different "
                      "project's target branch)")
        isolate += (f"The human pinned the merge target: worktree "
                    f"branches must be created from and merged back into "
                    f"the existing local branch {json.dumps(mt)} - set "
                    f"merge_target = {json.dumps(mt)} on {scope} "
                    f"(requires worktree = true; the branch must already "
                    f"exist in that project's repo - never substitute "
                    f"another name).\n\n")
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
    branch_bits = []
    for s, p in sorted(projects.items()):
        bs = list_local_branches(p["path"])[:20]
        if bs:
            branch_bits.append(f"{s}: {', '.join(bs)}")
    branches_hint = ("Existing local branches per project: "
                     + "; ".join(branch_bits) + ". " if branch_bits else "")
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
[run] needs name (short safe slug), description (one useful sentence,
300 characters MAX - longer fails validation), on_failure =
{json.dumps(rec.get('on_failure') or 'continue')}, and checkpoint = true.
[defaults] needs model = {json.dumps(rec['model'])}, effort =
{json.dumps(rec['effort'])}, mode = "bypass", and timeout_min.
Set max_attempts = {int(rec.get('max_attempts') or 3)} in [defaults]: a step
that underestimates its timeout_min is then continued by a fresh session
instead of ending early (1 = no retries).
Each [[step]] needs a unique name, a registered project slug, and a concrete
prompt describing only that step. Prefer sequential steps. If using waves on
one project, every member needs worktree = true. Cloud steps require explicit
user intent and a project permitted for cloud. No prompt_file or preamble.
merge_target = "<branch>" (optional, [run] or per [[step]]) names an existing
local branch the step's worktree branch is created from and merged back into
instead of the checkout's current branch; it requires worktree = true.
{branches_hint}Omit merge_target unless the human named one - a guessed
branch name fails validation.
depends_on = ["step-name", ...] (optional, per [[step]], never [defaults])
holds a step until every named step reaches a terminal state - 'failed'
counts, so a merge glitch never strands later work; a dep must name an
earlier step or a same-wave sibling (under [run] parallel = true anything
goes). Use it when later work needs earlier output (filenames, APIs,
research notes the predecessor should leave behind for it) - a trailing
'follow-up-plan' style step should depend on the rest. When the request
asks for parallelism, make a best effort: pack independent steps into
shared waves (or run.parallel) and keep only real prerequisites in
depends_on.
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
        # The planner's session id is worth banking even when the pass
        # dies: the dashboard keeps it as preparation history ('step 0').
        # `devin list` is cwd-scoped, so diff while the tempdir still
        # exists; the export/log refs cover a session that never
        # registered there (killed before list propagation).
        before = session_ids(devin_argv, Path(wd)) or set()
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
            # The detached planner may outlive its owner - job.json gets
            # its pid so a drain under a dead owner still waits it out
            # (a --print session writes the log only at turn end, so an
            # empty log is NOT proof of a dead planner).
            try:
                jf = log.parent / "job.json"
                if jf.is_file() and getattr(proc, "pid", None):
                    job = json.loads(jf.read_text(encoding="utf-8"))
                    job["planner_pid"] = proc.pid
                    jf.write_text(json.dumps(job), encoding="utf-8")
            except (OSError, ValueError):
                pass
            try:
                rc, timed_out, hung, stopped, _ext = wait_worker(
                    proc, log, 600,
                    stop_flags=(sflag,) if sflag is not None else ())
            finally:
                if rid:
                    with REQUEST_LOCK:
                        if _GEN_PROCS.get(rid) is proc:
                            _GEN_PROCS.pop(rid, None)
        sids = sorted(
            (session_ids(devin_argv, Path(wd)) or set()) - before)
        ref = tail_session_id(export_path) or tail_session_ref(log)
        if ref and ref not in sids:
            sids.append(ref)
        if sids:
            rec["_gen_session_ids"] = sids
        if stopped or (sflag is not None and sflag.is_file()):
            raise GenerationStopped(f"request {rid} proposal stopped")
        text = log.read_text(encoding="utf-8", errors="replace")
        # A STATUS: line ends the planner's turn - its reply is complete,
        # so a nonzero rc afterwards (CLI exit-path failure, or our own
        # kill at STATUS grace) must not discard a finished proposal.
        if timed_out or (rc != 0 and not hung
                         and not STATUS_RE.search(text)):
            raise RequestError("Planner did not finish; check CLI auth and "
                               "retry generation", 502)
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
    generation was parked, and the parked state must not overwrite it.

    updated_at has only SECOND granularity, so a same-second write - a
    stop_generation restore racing the publish check - used to pass this
    gate and let a killed attempt's state land over the restored record.
    The file's mtime (ns resolution) and its generation_stopped_at stamp
    are compared too: a stop always rewrites both."""
    if job.get("base_updated_at") is None:
        return False
    try:
        disk = _read_request_file(request_id, requests_dir)
        if disk.get("updated_at") != job["base_updated_at"]:
            return False
        if disk.get("generation_stopped_at") != job.get("base_stopped_at"):
            return False                     # a stop landed mid-generation
        base_mtime = job.get("base_mtime_ns")
        return base_mtime is None or _request_path(
            request_id, requests_dir).stat().st_mtime_ns == base_mtime
    except (RequestError, OSError):
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
                        gen_id: str | None = None,
                        gen_sids: list[str] | None = None) -> None:
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
                    # plan_gen_id marks the attempt that produced THIS
                    # manifest - the row labelled 'plan-proposal' at rest.
                    # An earlier stopped/superseded session is never
                    # attributed to a later attempt's plan. A drain-driven
                    # publish has no captured ids - the spooled export is
                    # the fallback source.
                    sids = list(gen_sids or [])
                    if not sids:
                        sid = tail_session_id(spool / "gen.export.json")
                        if sid:
                            sids = [sid]
                    overlay["plan_gen_id"] = gen_id
                    _record_plan_attempt(overlay, _plan_attempt_entry(
                        overlay, gen_id, sids, "done", tok_plan))
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


def _plan_attempt_entry(rec: dict, gen_id: str, sids: list[str],
                        outcome: str, tok: dict | None = None,
                        note: str | None = None) -> dict:
    """One planner attempt's terminal record for rec['plan_sessions'] -
    the persistent 'step 0' association between a create-proposal
    session and the plan it produced (or tried to). `outcome` is the
    session's real terminal status: done (published, or parked its
    questions), stopped, or failed."""
    entry = {"gen_id": gen_id, "status": outcome,
             "session_id": sids[-1] if sids else None,
             "started_at": rec.get("generation_started_at"),
             "ended_at": now_iso(),
             "model": rec.get("plan_model") or rec.get("model"),
             "effort": (rec.get("plan_effort")
                        if rec.get("plan_effort") is not None
                        else rec.get("effort"))}
    if len(sids) > 1:
        entry["session_ids"] = list(sids)
    if tok:
        entry["tokens"] = dict(tok)
    if note:
        entry["note"] = note
    t0, t1 = _iso_ts(entry["started_at"]), _iso_ts(entry["ended_at"])
    if t0 and t1:
        entry["elapsed_s"] = round(t1 - t0, 1)
    return entry


def _record_plan_attempt(rec: dict, entry: dict) -> None:
    """Append a finished planner attempt to rec['plan_sessions'] - the
    preparation history the dashboard renders as step-0 rows. Deduped by
    gen_id (each attempt records exactly one terminal outcome) and bounded
    by PLAN_HISTORY_MAX."""
    sess = [a for a in (rec.get("plan_sessions") or [])
            if a.get("gen_id") != entry.get("gen_id")]
    sess.append(entry)
    rec["plan_sessions"] = sess[-PLAN_HISTORY_MAX:]


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
    # Session ids of the devin sessions this attempt spawned - captured by
    # _planner_response (a malformed-TOML retry spawns a second one). They
    # land in the attempt's plan_sessions record, the persistent link
    # between the proposal and the session that drafted it.
    gen_sids: list[str] = []

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
        try:
            base_mtime = _request_path(
                request_id, requests_dir).stat().st_mtime_ns
        except OSError:
            base_mtime = None
        (spool / "job.json").write_text(json.dumps({
            "request_id": request_id, "requests_dir": str(requests_dir),
            "manifests_dir": str(manifests_dir), "runs_dir": str(runs_dir),
            "gen_id": gen_id,
            # Ownership for cross-process drain liveness: _GENERATING only
            # covers THIS process, so a foreign drain (a second serve, a
            # status call - anything running collect_dashboard) must check
            # pids. SERVE_BOOT is module-load time = this process's start.
            "owner_pid": os.getpid(),
            "owner_boot": SERVE_BOOT,
            "base_updated_at": base.get("updated_at"),
            "base_status": base.get("status"),
            "base_stopped_at": base.get("generation_stopped_at"),
            "base_mtime_ns": base_mtime,
            "created_at": now_iso()}), encoding="utf-8")
        log_path = spool / "gen.log"
        _rotate_log(log_path)
        export_path = log_path.with_suffix(".export.json")
        projects = load_projects()
        qa = rec.get("qa") or None    # set -> this pass refines on answers
        try:
            text = _planner_response(rec, projects, devin_argv, log_path,
                                     qa=qa)
            gen_sids += rec.pop("_gen_session_ids", [])
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
                gen_sids += rec.pop("_gen_session_ids", [])
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
            gen_sids += rec.pop("_gen_session_ids", [])
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
                _record_plan_attempt(rec, _plan_attempt_entry(
                    rec, gen_id, gen_sids, "done", tok_plan,
                    "asked clarifying questions"))
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
                            gen_id=gen_id, gen_sids=gen_sids)
    except GenerationStopped:
        # stop_generation already restored the record (draft, or ready with
        # the previous proposal). Bank the killed attempt's token usage and
        # record its session as a stopped preparation attempt - the entry
        # is keyed by this attempt's own gen_id, so it can never be read
        # as the producer of a plan some other attempt published.
        gen_sids += rec.pop("_gen_session_ids", [])
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
                _record_plan_attempt(rec, _plan_attempt_entry(
                    rec, gen_id, gen_sids, "stopped", tok_plan,
                    "stopped before producing a plan"))
                _save_request(rec, requests_dir)
        except RequestError:
            pass
    except Exception as exc:
        gen_sids += rec.pop("_gen_session_ids", [])
        _wait_writable(runs_dir, requests_dir, LOGS_DIR)
        LOG.warning("request %s generation failed: %s", request_id, exc)
        msg = (f"Planner returned malformed TOML ({exc})"
               if isinstance(exc, tomllib.TOMLDecodeError) else str(exc))
        with REQUEST_LOCK:
            overlay = _spool_rec(request_id)
            if overlay is None and _disk_untouched(
                    request_id, requests_dir, _job_meta(spool)):
                # The planner died before a draft ever spooled, so no
                # overlay exists yet - the untouched file is still the
                # record this attempt owns, and parking it as 'error'
                # keeps the failure (and the session record) visible
                # instead of silently reverting to the pre-run stage.
                try:
                    overlay = _read_request_file(request_id, requests_dir)
                except RequestError:
                    overlay = None
            if overlay is not None and still_mine(overlay):
                plan_tok = _merge_tokens(
                    dict(overlay.get("plan_tokens") or {}), tok_plan)
                if plan_tok:
                    overlay["plan_tokens"] = plan_tok
                _record_plan_attempt(overlay, _plan_attempt_entry(
                    overlay, gen_id, gen_sids, "failed", tok_plan,
                    msg[:160]))
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
            # Every exit path above either publishes/flushes the overlay
            # or leaves it to a newer attempt's gen_id. An overlay still
            # marked 'generating' with OUR gen after we exit is always a
            # lie (a transient spool read could once have skipped the
            # handoff) - drop it so _request_status exposes the request
            # file instead of wedging on the 660s orphan grace.
            cur = _spool_rec(request_id)
            if (cur or {}).get("gen_id") == gen_id \
                    and cur.get("status") == "generating":
                try:
                    (_spool_dir(request_id) / "request.json").unlink(
                        missing_ok=True)
                except OSError:
                    pass


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
        job = _job_meta(d)
        live = {job.get("gen_id"),
                (_spool_rec(d.name) or {}).get("gen_id")}
        if _GENERATING.intersection(live - {None}) \
                or d.name in _DRAINING:
            continue
        # _GENERATING only covers THIS process: a live foreign owner (a
        # second serve, a parallel generation) is not an orphan - a drain
        # here would mark its still-running generation 'interrupted'.
        if pid_alive(job.get("owner_pid"), job.get("owner_boot")):
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
    job = _job_meta(spool)
    # A dead owner's detached planner may still be writing (a --print
    # session writes the log only at turn end, so an empty/stale log is
    # NOT proof of death) - leave the spool for the next pass.
    if pid_alive(job.get("planner_pid"),
                 overlay.get("generation_started_at")):
        return
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
                sid = tail_session_id(spool / "gen.export.json")
                _record_plan_attempt(cur, _plan_attempt_entry(
                    cur, gid, [sid] if sid else [], "failed", None,
                    f"Recovered draft failed to publish: {exc}"[:160]))
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
        sid = tail_session_id(spool / "gen.export.json")
        _record_plan_attempt(overlay, _plan_attempt_entry(
            overlay, gid, [sid] if sid else [], "failed", None,
            "interrupted by a server restart"))
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
        if _request_status(rec, runs_dir) not in _REQUEST_EDITABLE_STAGES:
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
        # Drop the parked spool overlay BEFORE restoring the file: it
        # still says 'generating' and outranks the file for every
        # _read_request caller, so the restored stage would stay
        # invisible (and the stopped attempt's draft publishable by a
        # drain) until the spool was cleaned. Delete first so a crash
        # mid-stop can't leave the stale overlay masking the file. Only
        # an overlay owned by THIS attempt is touched - a newer
        # generation's overlay is that attempt's problem.
        spool_overlay = _spool_rec(request_id)
        if (spool_overlay or {}).get("gen_id") == rec.get("gen_id"):
            try:
                (_spool_dir(request_id) / "request.json").unlink(
                    missing_ok=True)
            except OSError:
                pass
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


def _request_status(rec: dict, runs_dir: Path,
                    launched: set[str] | None = None) -> str:
    # `launched` - a request_id set the caller already derived while
    # scanning run dirs (collect_dashboard builds one anyway) - skips an
    # O(every state.json) glob per request, the dominant payload cost
    # once runs accumulate.
    if (rec["id"] in launched) if launched is not None \
            else _request_run_dir(rec["id"], runs_dir):
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


# Detached dashboard spawns (run/resume/merge/summarize) deliberately
# outlive this process - but a dropped Popen object GCs to a
# "subprocess is still running" ResourceWarning while its child is
# mid-flight. Keep each handle here; poll() of finished ones sets the
# returncode so they prune (and GC) silently.
_DETACHED_PROCS: list = []


def _launch_process(cmd: list[str], log_path: Path) -> int:
    """Spawn a conductor `run`/`resume`/`merge`/`summarize` subprocess
    from the dashboard."""
    with open(log_path, "wb") as out:
        proc = subprocess.Popen(cmd, cwd=str(ROOT), stdout=out,
                                stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                env=clean_devin_env(), **_detached_kwargs())
    # Prune finished handles; a test-mocked Popen without poll() is kept
    # rather than crash every later launch.
    alive = []
    for p in _DETACHED_PROCS:
        try:
            if p.poll() is None:
                alive.append(p)
        except Exception:
            alive.append(p)
    _DETACHED_PROCS[:] = alive
    _DETACHED_PROCS.append(proc)
    return proc.pid


def launch_request(request_id: str,
                   requests_dir: Path = REQUESTS_DIR,
                   manifests_dir: Path = MANIFESTS_DIR,
                   runs_dir: Path = RUNS_DIR,
                   devin_argv: list[str] | None = None) -> dict:
    with REQUEST_LOCK:
        _assert_unwatched(runs_dir, requests_dir)
        rec = _read_request(request_id, requests_dir)
        pend = _pending_actions(rec)
        if _request_status(rec, runs_dir) not in ("ready", "launch_failed") \
                or pend["gen"] or pend["opts"]:
            which = []
            if pend["gen"]:
                which.append("request inputs changed ("
                             + ", ".join(pend["gen"]) + ") - Regenerate "
                             "tasks")
            if pend["opts"]:
                which.append("options differ from the proposal TOML ("
                             + ", ".join(pend["opts"]) + ") - Apply "
                             "options to TOML or regenerate")
            raise RequestError(
                ("The proposal does not match the request form: "
                 + "; ".join(which)
                 + ". Review and reconcile before launching; already "
                   "launched requests cannot be started again")
                if which else
                "Review and save a current proposal before launching; "
                "already launched requests cannot be started again", 409)
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
            drv = driver_alive(st)
            for sname, srec in (st.get("steps") or {}).items():
                # Worktree steps write under their own dir - only a
                # non-worktree (branch-less) record can own a project dir.
                if srec.get("branch") or not step_live(srec, drv):
                    continue
                try:
                    wp = Path(srec.get("work_dir") or
                              srec.get("project") or "").resolve()
                except OSError:
                    continue
                if wp in targets:
                    raise RequestError(
                        "A live agent already works in this project (" +
                        _watcher_identity(sj.parent.name, sname, srec) +
                        "); wait or isolate work in a worktree", 409)
        if ROOT.resolve() in targets and any(
                _request_status(r, runs_dir) == "generating" for f in
                requests_dir.glob("*.json")
                # *.gen.export.json transcripts share the dir and match the
                # glob - only a request record carries the 'id' field
                # _request_status reads
                if REQUEST_ID_RE.fullmatch(f.stem)
                for r in [json.loads(f.read_text(encoding="utf-8"))]):
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
    if driver_alive(st):
        raise RequestError("This run's conductor is still running - "
                           "resume once it has stopped", 409)
    if any(step_live(r, driver_alive(st)) for r in (st.get("steps") or {}).values()):
        raise RequestError("This run still has a live agent - wait for it "
                           "or let it die first", 409)
    pid = _RESUME_PIDS.get(name)
    if pid and pid_alive(pid):
        raise RequestError("A resume is already starting for this run", 409)
    # A manual merge holds the run's merge claim for its whole operation;
    # resume must not launch a driver over it (the spawned cmd_run would
    # refuse on the claim anyway - checking here keeps the answer clean).
    holder = _merge_claim_held(d)
    if holder:
        raise RequestError(
            f"A {holder.get('by') or 'merge'} is in flight for this run "
            "- resume once it finishes", 409)
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
    log_user_event(d, event="run_resume_requested",
                   pid=_RESUME_PIDS[name])
    return {"resumed": name, "pid": _RESUME_PIDS[name]}


def _git_dir(repo_dir: Path) -> Path | None:
    r = _git(repo_dir, "rev-parse", "--git-dir")
    if r.returncode != 0:
        return None
    p = Path(r.stdout.strip())
    return p if p.is_absolute() else (repo_dir / p).resolve()


def _merge_claim_held(run_dir: Path) -> dict | None:
    """The live claim on this run's merge surface, else None. The payload's
    'by' names the owner ('merge' = a merge_run in flight, 'resume' = a
    driver mid-stamp) so refusals can say who is operating. A claim whose
    owner pid is dead is stale and broken by the next acquirer; a fresh
    but unreadable one counts as held for a short grace - an O_EXCL
    winner is mid-write between create and payload, and breaking the file
    under it would orphan a real claim (same race the device lease
    covers)."""
    p = merge_claim_flag(run_dir)
    try:
        info = json.loads(p.read_text(encoding="utf-8", errors="replace"))
        if not isinstance(info, dict):
            info = None
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        info = None
    if info and pid_alive(info.get("pid")):
        return info
    if info is None:
        try:
            if time.time() - p.stat().st_mtime < 10:
                return {"by": "operation", "pid": None}
        except OSError:
            pass
    return None


def _claim_run_merge(run_dir: Path, by: str) -> Path:
    """Take the run's merge claim or raise 409 naming the live owner.

    Same O_EXCL + pid shape as _RepoMergeLock, but per RUN and machine-
    global (the flag dir), because the parties live in different
    processes: a dashboard merge_run, a CLI merge, and a `run --resume`
    driver stamping itself into state.json. Whoever holds the claim owns
    the run's merge surface; merge_run holds it for the whole operation
    and re-reads state under it, so a resume that already passed its own
    checks is caught by the recheck instead of overlapping the merge.
    Returns the claim path - the caller unlinks it on release."""
    p = merge_claim_flag(run_dir)
    try:
        p.parent.mkdir(exist_ok=True)
    except OSError:
        pass
    for _attempt in (0, 1):
        try:
            fd = os.open(p, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            holder = _merge_claim_held(run_dir)
            if holder:
                who = holder.get("by") or "merge"
                pidbit = (f" (pid {holder['pid']})"
                          if holder.get("pid") else "")
                raise RequestError(
                    f"a {who} is already operating on this run{pidbit}",
                    409)
            try:
                p.unlink(missing_ok=True)  # dead owner: break once
            except OSError:
                # lost the break race or the file is still being written -
                # treat as held rather than force it
                raise RequestError(
                    "another operation is already claiming this run", 409)
            continue
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps(
                {"pid": os.getpid(), "at": now_iso(), "by": by}))
        return p
    raise RequestError("the run's merge claim is held but its owner "
                       "cannot be read", 409)


MERGE_LOCK_POLL_S = 2.0      # cadence of the queued-for-merge wait


class _RepoMergeLock:
    """Serialize merge operations on one repo across conductor PROCESSES.

    Two drivers (or a dashboard merge plus a CLI one) must never run `git
    merge` against the same checkout/stage at once - merges are not
    composable. The lock is an O_EXCL file under the repo's git dir holding
    the holder's pid + timestamp; a lock left by a dead process is broken
    once and retaken. `.git` is never a watched work tree, so this cannot
    trip a live worker's user-edit awareness (trap 5).

    wait=(state, run_dir, rec) turns the fail-fast into a merge QUEUE:
    the caller polls until the holder's pid dies or the lock frees - a
    resolver session can hold it for a whole step timeout, so there is no
    cap, but halt sentinels (stop/drain/pause) bail out with RequestError
    so a queued merge never ignores a Stop click. While queued the step
    record carries merge_queue=<holder pid> and a note saying who is
    ahead; both clear when the lock lands or the wait ends.
    """

    def __init__(self, repo_dir: Path,
                 wait: tuple[dict, Path, dict] | None = None):
        self.repo_dir = repo_dir
        self.wait = wait
        self.path: Path | None = None

    def _note_queue(self, who: str | None, waited_s: float = 0.0) -> None:
        state, run_dir, rec = self.wait or (None, None, None)
        if rec is None:
            return
        with STATE_LOCK:
            if who is None:
                rec.pop("merge_queue", None)
            else:
                rec["merge_queue"] = who
                rec["merge_waited_s"] = round(waited_s, 1)
                _append_note(rec, f"queued for the repo merge lock "
                                  f"(holder {who})")
            if run_dir is not None and state is not None:
                save_state(run_dir, state)

    def __enter__(self):
        gd = _git_dir(self.repo_dir)
        if gd is None:
            raise RequestError(f"not a git repo: {self.repo_dir}", 500)
        self.path = gd / "conductor-merge.lock"
        t_wait = time.time()
        noted = False
        while True:
            try:
                fd = os.open(self.path,
                             os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                try:
                    info = json.loads(self.path.read_text(
                        encoding="utf-8", errors="replace"))
                except (OSError, ValueError):
                    info = {}
                # plain liveness, no since-window: a long-running serve
                # process legitimately takes locks far after its start
                if pid_alive(info.get("pid")):
                    if self.wait is None:
                        raise RequestError(
                            f"another merge is already operating on "
                            f"{self.repo_dir} (pid {info.get('pid')})", 409)
                    _state, run_dir, _rec = self.wait
                    if not noted:
                        noted = True
                        self._note_queue(
                            f"pid {info.get('pid')}",
                            time.time() - t_wait)
                        if run_dir is not None:
                            log_event(run_dir, event="merge_wait",
                                      holder=info.get("pid"))
                    if run_dir is not None and _flag_set(
                            stop_flag(run_dir), pause_flag(run_dir),
                            drain_flag(run_dir)) is not None:
                        self._note_queue(None)
                        raise RequestError(
                            "run halted while queued for the repo merge "
                            "lock", 409)
                    time.sleep(MERGE_LOCK_POLL_S)
                    continue
                self.path.unlink(missing_ok=True)  # stale lock: break once
                continue
            with os.fdopen(fd, "w") as f:
                f.write(json.dumps({"pid": os.getpid(), "at": now_iso()}))
            if noted:
                self._note_queue(None)
            return self

    def __exit__(self, *exc) -> None:
        if self.path is not None:
            try:
                self.path.unlink(missing_ok=True)
            except OSError:
                pass


def _branch_checked_out_at(repo_dir: Path, branch: str) -> Path | None:
    """The worktree path where `branch` is checked out, if any."""
    r = _git(repo_dir, "worktree", "list", "--porcelain")
    if r.returncode != 0:
        return None
    want = f"refs/heads/{branch}"
    wt: Path | None = None
    for ln in r.stdout.splitlines():
        if ln.startswith("worktree "):
            wt = Path(ln[9:])
        elif ln == f"branch {want}" and wt is not None:
            return wt
        elif ln.strip() == "" or ln.startswith("worktree "):
            continue
    return None


def _any_live_step_in(repo_dir: Path, runs_dir: Path,
                      exclude_run: str) -> str | None:
    """Name of a live step (any run) whose worker watches `repo_dir` as its
    work_dir - landing a merge into a checked-out target rewrites files
    under it, which a live session reads as external edits (trap 5)."""
    want = os.path.normcase(str(repo_dir.resolve()))
    for sj in runs_dir.glob("*/state.json"):
        if sj.parent.name == exclude_run:
            continue
        try:
            st = json.loads(sj.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        drv = driver_alive(st)
        for n, rec in (st.get("steps") or {}).items():
            if not step_live(rec, drv):
                continue
            wd = rec.get("work_dir") or rec.get("project")
            if not wd:
                continue
            try:
                wdp = os.path.normcase(str(Path(wd).resolve()))
            except OSError:
                continue
            if wdp == want or want.startswith(wdp + os.sep):
                return _watcher_identity(sj.parent.name, n, rec)
    return None


def _land_staged(repo_dir: Path, target: str, staged_tip: str,
                 staged_base: str, runs_dir: Path,
                 exclude_run: str) -> tuple[bool, str]:
    """Move the recorded target ref to the verified staged tip, atomically.

    Target checked out in the main checkout -> `merge --ff-only`, which
    refuses when the target moved past the staging base or local changes
    overlap; a live agent watching the checkout blocks it first (trap 5).
    Target not checked out anywhere -> `update-ref` with the expected old
    value: the recheck of target movement is the atomicity mechanism.
    """
    co = _branch_checked_out_at(repo_dir, target)
    if co is not None and os.path.normcase(str(co.resolve())) == \
            os.path.normcase(str(repo_dir.resolve())):
        watcher = _any_live_step_in(repo_dir, runs_dir, exclude_run)
        if watcher:
            return False, f"a live agent watches the checkout ({watcher})"
        if merge_in_progress(repo_dir):
            return False, "a merge is already in progress in the checkout"
        r = _git(repo_dir, "merge", "--ff-only", staged_tip)
        if r.returncode != 0:
            return False, ("target moved or checkout not clean: "
                           + (r.stderr or r.stdout).strip()[:200])
        return True, ""
    if co is not None:
        return False, f"target branch is checked out at {co}"
    r = _git(repo_dir, "update-ref", f"refs/heads/{target}",
             staged_tip, staged_base)
    if r.returncode != 0:
        return False, ("target moved since staging (expected "
                       f"{staged_base[:12]}): {(r.stderr or '').strip()[:160]}")
    return True, ""


def _merge_deferred_group(d: Path, st: dict, grp: dict, names: list[str],
                          cfg: Manifest, devin_argv: list[str],
                          runs_dir: Path, results: dict) -> None:
    """Land one repo's pending VERIFIED deferred branches onto its recorded
    target. The combination is prepared and verified in an isolated staging
    worktree first; the target ref moves only on a fully merged + verified
    stage, and only if it hasn't moved since staging began.

    Unverified entries (failed/partial/blocked/verify-red) are never
    included - they stay pending and surface in results['unverified'].
    A conflict without a working resolver or a red combined verify keeps
    the stage dir as recoverable state and leaves every unlanded entry
    pending. Nothing here deletes retained work.
    """
    steps = st.get("steps") or {}
    repo_dir = Path(grp["repo_dir"])
    slug = grp.get("slug") or repo_dir.name
    target = grp.get("target_branch")
    order = {s.name: i for i, s in enumerate(cfg.steps)}
    by_name = {s.name: s for s in cfg.steps}
    verified: list[tuple[str, dict]] = []
    for n in sorted(names, key=lambda x: order.get(x, 1 << 30)):
        rec = steps.get(n)
        if rec is None or not rec.get("branch") or rec.get("merged") is True:
            continue
        if rec.get("deferred_verified"):
            verified.append((n, rec))
        else:
            results["unverified"].append(n)
            _append_note(rec, "not merged: step is not verified "
                              "(needs status=done and no red verify)")
    if not verified:
        return
    if not target:
        for n, _r in verified:
            results["skipped"].append(n)
        log_event(d, event="merge_skipped", reason="no recorded target",
                  project=str(repo_dir))
        return
    thash = hashlib.sha1(
        f"{_repo_key(repo_dir)}|{target}".encode()).hexdigest()[:10]
    stage_wt = runs_dir.parent / "worktrees" / d.name / f"stage-{thash}"
    stage_ref = sanitize_branch(f"conductor/{d.name}/stage-{thash}")
    target_tip = _baseline_tip(repo_dir, target)
    if target_tip is None:
        for n, _r in verified:
            results["skipped"].append(n)
            _append_note(steps[n], f"not merged: target branch {target!r} "
                                   "is gone")
        return
    with _RepoMergeLock(repo_dir):
        if stage_wt.is_dir():
            if merge_in_progress(stage_wt) or git_dirty_tracked(stage_wt):
                # A prior attempt's stage is mid-merge or holds edited
                # tracked files - recoverable state a human may be
                # resolving in. Never clobber it. (A CLEAN kept stage is
                # reproducible scratch: the pending branches are the real
                # state, so a retry just rebuilds it.)
                for n, _r in verified:
                    results["kept"].append(n)
                log_event(d, event="merge_kept", stage=str(stage_wt),
                          project=str(repo_dir))
                return
            worktree_remove(repo_dir, stage_wt, stage_ref)
            if stage_wt.is_dir():
                # an unregistered leftover dir (crashed mid-remove)
                shutil.rmtree(stage_wt, ignore_errors=True)
        else:
            _git(repo_dir, "branch", "-D", stage_ref)   # orphan ref cleanup
        if not worktree_add(repo_dir, stage_wt, stage_ref, target_tip):
            for n, _r in verified:
                results["skipped"].append(n)
            log_event(d, event="merge_stage_failed", project=str(repo_dir))
            return
        staged_base = target_tip
        staged: list[tuple[str, dict]] = []
        dv = (st.get("devin_argv")
              or (shlex.split(st["devin"]) if st.get("devin") else None)
              or devin_argv or ["devin"])
        for n, rec in verified:
            branch = rec["branch"]
            tip = _baseline_tip(repo_dir, branch)
            head = _git(stage_wt, "rev-parse", "HEAD").stdout.strip()
            if tip is None:
                results["skipped"].append(n)
                _append_note(rec, "not merged: branch is gone")
                continue
            if head and _git(repo_dir, "merge-base", "--is-ancestor",
                             tip, head).returncode == 0:
                # tip already inside the stage (sequential chain covered
                # by a later branch, or a zero-commit branch)
                staged.append((n, rec))
                continue
            r = _git(stage_wt, "merge", "--no-edit", branch)
            ok = r.returncode == 0
            if not ok:
                step_obj = by_name.get(n) or Step(
                    name=n, project=slug, prompt="",
                    merge_resolver=cfg.merge_resolver)
                if (by_name.get(n) or step_obj).merge_resolver:
                    ok = resolve_merge_conflict(
                        step_obj, slug, repo_dir,
                        Path(rec.get("work_dir") or stage_wt), branch, cfg,
                        d, dv, st, merge_dir=stage_wt, already_started=True,
                        remove_on_success=False, dirty_guard=False)
                if not ok:
                    if merge_in_progress(stage_wt):
                        _git(stage_wt, "merge", "--abort")
                    results["conflicted"].append(n)
                    _append_note(rec, f"merge conflicts staging into "
                                      f"{target}; kept pending, stage at "
                                      f"{stage_wt}")
                    continue
            staged.append((n, rec))
        if not staged:
            worktree_remove(repo_dir, stage_wt, stage_ref)
            return
        # The combined result must pass each merged step's verify gate
        # again - what lands on the target is the union, not each branch
        # in isolation.
        verify_failed: list[str] = []
        seen_v: set[str] = set()
        for n, _r in staged:
            sobj = by_name.get(n)
            v = sobj.verify if sobj and sobj.verify_must_pass else None
            if not v or v in seen_v:
                continue
            seen_v.add(v)
            vt_s = (sobj.verify_timeout_min if sobj else 10) * 60
            try:
                vp = subprocess.run(v, cwd=str(stage_wt), shell=True,
                                    capture_output=True, text=True,
                                    encoding="utf-8", errors="replace",
                                    timeout=vt_s,
                                    **hidden_subprocess_kwargs())
                ok_v = vp.returncode == 0
                out = (vp.stdout or "") + (vp.stderr or "")
            except subprocess.TimeoutExpired:
                ok_v = False
                out = f"staged verify timed out after {int(vt_s)}s\n"
            (d / f"{n}.mergeverify.log").write_text(
                out, encoding="utf-8", errors="replace")
            if not ok_v:
                verify_failed.append(n)
        if verify_failed:
            results["verify_failed"].extend(verify_failed)
            grp["stage_kept"] = str(stage_wt)
            for n, _r in staged:
                _append_note(steps[n], "combined result failed verify in "
                                       f"the staging worktree; kept at "
                                       f"{stage_wt} - nothing landed")
            log_event(d, event="merge_verify_failed", project=str(repo_dir),
                      steps=verify_failed)
            return
        staged_tip = _git(stage_wt, "rev-parse", "HEAD").stdout.strip()
        landed, why = _land_staged(repo_dir, target, staged_tip,
                                   staged_base, runs_dir, d.name)
        if not landed:
            # The target moved under us (or is dirty/watched) - the staged
            # work is reproducible from the pending branches, so drop the
            # stale stage and keep every entry pending for the next try.
            results["moved"].append(slug)
            grp["moved_reason"] = why
            for n, _r in staged:
                _append_note(steps[n], f"merge not landed: {why}")
            worktree_remove(repo_dir, stage_wt, stage_ref)
            log_event(d, event="merge_moved", project=str(repo_dir),
                      reason=why)
            return
        for n, rec in staged:
            rec["merged"] = True
            rec["merged_at"] = now_iso()
            _append_note(rec, f"deferred merge landed on {target} via "
                              "staged+verified merge")
            _bump_completion(rec)
            wt = Path(rec["work_dir"]) if rec.get("work_dir") else None
            if wt and wt != stage_wt:
                worktree_remove(repo_dir, wt, rec["branch"])
            if n in grp["entries"]:
                grp["entries"].remove(n)
            results["merged"].append(n)
        worktree_remove(repo_dir, stage_wt, stage_ref)
        grp.pop("stage_kept", None)
        log_event(d, event="merge_landed", project=str(repo_dir),
                  target=target, steps=[n for n, _r in staged],
                  tip=staged_tip)


_MERGE_BUSY: set[str] = set()
_MERGE_LOCK = threading.Lock()
# Detached `conductor.py merge` children spawned via merge_run_start -
# consulted like _RESUME_PIDS so a dashboard merge stays refused while
# its child still runs (and the child's run claim covers every other
# process, including a restarted serve that lost this table).
_MERGE_PIDS: dict[str, int] = {}


def merge_run(name: str, runs_dir: Path = RUNS_DIR,
              devin_argv: list[str] | None = None,
              only: list[str] | None = None) -> dict:
    """Merge a finished run's pending worktree branches - the dashboard's
    manual counterpart to the post-wave merge, and the explicit human
    request that lands deferred work.

    Legacy parked branches (auto-policy conflicts) keep the direct
    merge-into-checkout path. Deferred entries go through the staged
    pipeline: prepared + verified in an isolated worktree, target movement
    rechecked, landed atomically, serialized per repo, resolver-honored;
    unverified work is never included and retained work is never deleted.

    `only` restricts the operation to the named steps (for landing one
    alternative instead of combining them all). Refused while the run is
    still executing (live driver - including the verify/merge and
    stop-shutdown windows - or a live step), while a resume is in flight
    or starting, or while another merge already holds the run's claim."""
    with _MERGE_LOCK:
        if name in _MERGE_BUSY:
            raise RequestError("A merge is already running for this run",
                               409)
        _MERGE_BUSY.add(name)
    try:
        return _merge_run(name, runs_dir, devin_argv, only)
    finally:
        with _MERGE_LOCK:
            _MERGE_BUSY.discard(name)


def merge_run_start(name: str, runs_dir: Path = RUNS_DIR,
                    devin_argv: list[str] | None = None,
                    only: list[str] | None = None) -> dict:
    """POST /api/runs/<run>/merge - spawn the merge out of process (the
    same detach discipline as resume/summarize launches). A staged merge
    can run resolver sessions for tens of minutes; run in-request it dies
    with this process - a service stop tree-kills the serve AND its
    mid-merge resolvers, orphaning a real git merge in a watched
    checkout. The child re-validates everything under its own run claim;
    the checks here only refuse requests that cannot possibly succeed."""
    if not RUN_DIR_RE.fullmatch(name):
        raise RequestError("Invalid run dir", 404)
    d = (runs_dir / name).resolve()
    if d.parent != runs_dir.resolve() or not (d / "state.json").is_file():
        raise RequestError("Run not found", 404)
    try:
        st = json.loads((d / "state.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RequestError("Cannot read run state", 500) from exc
    _merge_refuse_executing(run_executing(st))
    pid = _RESUME_PIDS.get(name)
    if pid and pid_alive(pid):
        raise RequestError("A resume is already running for this run", 409)
    pid = _MERGE_PIDS.get(name)
    if pid and pid_alive(pid):
        raise RequestError("A merge is already running for this run", 409)
    holder = _merge_claim_held(d)
    if holder:
        raise RequestError(
            f"A {holder.get('by') or 'merge'} is in flight for this run "
            "- merge once it finishes", 409)
    steps = st.get("steps") or {}
    onlyset = set(only) if only else None
    pending = {n for n, r in steps.items()
               if r.get("branch") and r.get("merged") is not True
               and not r.get("deferred")}
    if onlyset is not None:
        pending &= onlyset
    if not pending and not (st.get("pending_merges") or {}):
        return {"dir": name, "merged": [], "conflicted": [], "skipped": [],
                "unverified": [], "verify_failed": [], "moved": [],
                "busy": [], "kept": []}
    manifest = st.get("manifest")
    if not manifest or not Path(manifest).is_file():
        raise RequestError("Run's manifest is gone; merge the branches "
                           "by hand from each kept worktree", 409)
    cmd = [sys.executable, str(ROOT / "conductor.py"), "merge", name,
           "--runs-dir", str(runs_dir)]
    # The run's own worker binary wins over the server's env default -
    # same rule as resume: a mock-devin run resolves merges with it too.
    dv = (st.get("devin_argv")
          or (shlex.split(st["devin"]) if st.get("devin") else None)
          or devin_argv)
    if dv and dv != ["devin"]:
        cmd += ["--devin", shlex.join(dv)]
    if only:
        cmd += ["--steps", *only]
    try:
        _MERGE_PIDS[name] = _launch_process(cmd, d / "merge.launch.log")
    except OSError as exc:
        raise RequestError("Could not start the merge process", 500) from exc
    log_user_event(d, event="run_merge_requested",
                   pid=_MERGE_PIDS[name],
                   steps=only if only else None)
    return {"merging": name, "pid": _MERGE_PIDS[name]}


def _merge_refuse_executing(why: str | None) -> None:
    """A manual merge runs only once the run has ACTUALLY stopped or
    finished: a live driver means still executing even when no step
    record reads 'running' at this instant - between waves, queued under
    the cap, the verify/merge window after a worker exits, or a stop/
    pause shutdown that has not finished (a raised sentinel is a request,
    not a completed stop)."""
    if why:
        raise RequestError(f"Merge pending branches is unavailable while "
                           f"the run is still executing ({why}) - it "
                           "unlocks once the run has stopped or "
                           "finished", 409)


def _merge_run(name: str, runs_dir: Path,
               devin_argv: list[str] | None,
               only: list[str] | None = None) -> dict:
    if not RUN_DIR_RE.fullmatch(name):
        raise RequestError("Invalid run dir", 404)
    d = (runs_dir / name).resolve()
    if d.parent != runs_dir.resolve() or not (d / "state.json").is_file():
        raise RequestError("Run not found", 404)
    try:
        st = json.loads((d / "state.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RequestError("Cannot read run state", 500) from exc
    _merge_refuse_executing(run_executing(st))
    pid = _RESUME_PIDS.get(name)
    if pid and pid_alive(pid):
        raise RequestError("A resume is already running for this run", 409)
    # Claim the run's merge surface for the whole operation, then recheck
    # under it: a resume holds the same claim while it stamps driver_pid,
    # so a driver that was mid-startup when we first read state is caught
    # by the second check - the two operations can never overlap. The
    # claim is also what serializes a dashboard merge against a CLI
    # `conductor.py merge` (the in-process _MERGE_BUSY set cannot).
    claim = _claim_run_merge(d, "merge")
    try:
        try:
            st = json.loads(
                (d / "state.json").read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise RequestError("Cannot read run state", 500) from exc
        _merge_refuse_executing(run_executing(st))
        pid = _RESUME_PIDS.get(name)
        if pid and pid_alive(pid):
            raise RequestError("A resume is already running for this run",
                               409)
        return _merge_run_body(d, name, st, runs_dir, devin_argv, only)
    finally:
        claim.unlink(missing_ok=True)


def _merge_run_body(d: Path, name: str, st: dict, runs_dir: Path,
                    devin_argv: list[str] | None,
                    only: list[str] | None = None) -> dict:
    steps = st.get("steps") or {}
    onlyset = set(only) if only else None
    pending = {n for n, r in steps.items()
               if r.get("branch") and r.get("merged") is not True
               and not r.get("deferred")}
    if onlyset is not None:
        pending &= onlyset
    if not pending and not (st.get("pending_merges") or {}):
        return {"dir": name, "merged": [], "conflicted": [], "skipped": [],
                "unverified": [], "verify_failed": [], "moved": [],
                "busy": [], "kept": []}
    manifest = st.get("manifest")
    if not manifest or not Path(manifest).is_file():
        raise RequestError("Run's manifest is gone; merge the branches "
                           "by hand from each kept worktree", 409)
    cfg = load_manifest(Path(manifest))
    projects = load_projects()
    results = {"dir": name, "merged": [], "conflicted": [], "skipped": [],
               "unverified": [], "verify_failed": [], "moved": [],
               "busy": [], "kept": []}
    for step in cfg.steps:                    # manifest order, like cmd_run
        rec = steps.get(step.name) or {}
        branch = rec.get("branch")
        if not branch or rec.get("merged") is True \
                or rec.get("deferred") \
                or (onlyset is not None and step.name not in onlyset):
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
        # The recorded target wins: a run that chose (or resolved) a
        # non-default merge target merges there, never back to whatever
        # the checkout happens to be on now.
        tgt = rec.get("merge_target", step.merge_target)
        mwt = Path(rec.get("merge_dir") or merge_wt_for(wt))
        if _git(repo_dir, "rev-parse", "-q", "--verify",
                branch).returncode != 0:
            rec["note"] = "merge skipped: branch is gone"
            results["skipped"].append(step.name)
            continue
        # A stale conductor merge inside the merge location is cleared by
        # merge_step_branch itself; a human's merge makes it refuse. The
        # repo merge lock serializes us against other conductors'
        # in-flight merges on the same repo.
        via_resolver = False
        merr = None
        try:
            with _RepoMergeLock(repo_dir):
                ok, loc, merr = merge_step_branch(
                    repo_dir, wt, branch, tgt, mwt)
                if loc is not None and loc != repo_dir:
                    rec["merge_dir"] = str(loc)
                if not ok and step.merge_resolver and loc is not None \
                        and merr is None:
                    dv = (st.get("devin_argv")
                          or (shlex.split(st["devin"])
                              if st.get("devin") else None)
                          or devin_argv or ["devin"])
                    ok = resolve_merge_conflict(step, slug, repo_dir, wt,
                                                branch, cfg, d, dv, st,
                                                merge_dir=loc, target=tgt)
                    via_resolver = ok
        except RequestError as exc:
            rec["note"] = f"merge deferred: {exc}"
            results["busy"].append(step.name)
            continue
        was = rec.get("status")
        if ok:
            rec.update(status="done" if was == "failed" else was,
                       merged=True, merged_at=now_iso(),
                       note=f"merged via dashboard (was {was})")
            _bump_completion(rec)
            results["merged"].append(step.name)
            log_event(d, event="step_merged", step=step.name,
                      how="manual", was=was, branch=branch,
                      resolver=via_resolver)
        else:
            rec.update(merged=False,
                       note=(merr + "; " if merr else "") +
                            "merge still conflicts; the worktree is kept "
                            f"at {wt} for manual resolution")
            _bump_completion(rec)
            results["conflicted"].append(step.name)
            log_event(d, event="step_merge_conflict", step=step.name,
                      how="manual", was=was, branch=branch)
    pending -= set(results["merged"])
    for n in pending - set(results["conflicted"]) - set(results["skipped"]):
        # recorded branch belongs to a step the manifest no longer names
        steps[n]["note"] = "merge skipped: step not in the manifest anymore"
        results["skipped"].append(n)
    # Deferred pending merges: per-repo groups land through the staged
    # pipeline, entries in manifest order.
    for key, grp in (st.get("pending_merges") or {}).items():
        names = [n for n in grp.get("entries") or []
                 if onlyset is None or n in onlyset]
        if not names:
            continue
        try:
            _merge_deferred_group(d, st, grp, names, cfg,
                                  devin_argv, runs_dir, results)
        except RequestError as exc:
            results["busy"].append(grp.get("slug") or key)
            log_event(d, event="merge_busy", project=grp.get("repo_dir"),
                      reason=str(exc))
    save_state(d, st)
    log_event(d, event="merge_manual", merged=results["merged"],
              conflicted=results["conflicted"], skipped=results["skipped"],
              unverified=results["unverified"],
              verify_failed=results["verify_failed"],
              moved=results["moved"], busy=results["busy"],
              kept=results["kept"])
    try:
        (runs_dir.parent / "worktrees" / d.name).rmdir()
    except OSError:
        pass
    return results


def cmd_merge(args: argparse.Namespace) -> int:
    """CLI handle for the explicit merge: land a run's pending branches -
    the same op the dashboard's 'Merge selected pending work' runs.
    For deferred runs this is THE merge: staged, verified, serialized."""
    try:
        out = merge_run(args.run_dir,
                        Path(args.runs_dir).resolve()
                        if args.runs_dir else RUNS_DIR,
                        shlex.split(args.devin) if args.devin else None,
                        only=args.steps or None)
    except RequestError as exc:
        print(f"merge: {exc}")
        return 2
    for key in ("merged", "conflicted", "skipped", "unverified",
                "verify_failed", "moved", "busy", "kept"):
        if out.get(key):
            print(f"  {key}: {', '.join(out[key])}")
    if not any(out.get(k) for k in
               ("merged", "conflicted", "skipped", "unverified",
                "verify_failed", "moved", "busy", "kept")):
        print("nothing pending to merge")
    return 1 if (out["conflicted"] or out["verify_failed"] or out["moved"]
                 or out["busy"] or out["kept"]) else 0


def cmd_summarize(args: argparse.Namespace) -> int:
    """CLI handle for the LLM run summary - the same op the dashboard's
    'Generate LLM summary' button and a `[run] summarize = true` run's
    end-of-run spawn run. run_dir is the run name or its path."""
    d = Path(args.run_dir)
    if not d.is_dir():
        alt = (Path(args.runs_dir).resolve() if args.runs_dir
               else RUNS_DIR) / args.run_dir
        if alt.is_dir():
            d = alt
    if not d.is_dir():
        print(f"summarize: run dir not found: {args.run_dir}")
        return 2
    ok = summarize_run(d, shlex.split(args.devin) if args.devin else None,
                       args.model or "")
    return 0 if ok else 1


def cmd_import(args: argparse.Namespace) -> int:
    """`conductor.py import <file.toml>` - the explicit intake path for an
    already-written manifest (a follow-up discovery never saw, or any
    proposal TOML): validate + register it as a 'Ready to run' request row.
    Registration only - nothing is scheduled or launched."""
    runs_dir = Path(args.runs_dir).resolve() if args.runs_dir else RUNS_DIR
    try:
        rec = import_followup_file(
            args.manifest, runs_dir.parent / "requests",
            _followup_owner_dir(runs_dir), runs_dir,
            run_dir=getattr(args, "run", None))
    except RequestError as exc:
        print(f"import: {exc}")
        return 1
    print(f"request {rec['id']}: {rec['status']}"
          + (f" - {rec['error']}" if rec.get("error") else ""))
    if rec.get("manifest"):
        print(f"  proposal: {rec['manifest']}")
    if rec.get("source_manifest"):
        print(f"  source:   {rec['source_manifest']}")
    if rec.get("source_run"):
        print(f"  linked run: {rec['source_run']}")
    return 0 if rec.get("status") in ("ready", "empty") else 1


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
    # step_stopped/run_stop itself; the flag-dir history file is outside
    # every watched cwd and safe to append.
    log_user_event(d, event="step_stop_requested", step=step_name)
    return {"stopping": step_name}


def interrupt_step(run_name: str, step_name: str, body: dict,
                   runs_dir: Path = RUNS_DIR) -> dict:
    """Dashboard 'Interrupt + instruct': persist the operator's
    supplemental instructions (+ image attachment refs) in a durable
    intrq-* sentinel the running driver polls in the same slot as a stop
    flag - on sight the worker's process tree dies, the request folds
    into the step's resume notes, and the SAME step respawns with them.

    Only the sentinel is written here: no state.json touch, no respawn
    from this handler (the driver owns the transition, so a dead driver
    never leaves an orphaned replacement - the pending flag simply waits
    for the next dispatch). A second request while one is pending is
    refused: instructions must apply exactly once, so there is no
    'latest wins' queue. A step that already ended is refused too - the
    natural-completion race resolves on the driver side, and the UI
    offers resume notes for follow-ups to a finished step."""
    if not RUN_DIR_RE.fullmatch(step_name):
        raise RequestError("Invalid step name", 404)
    d, st = _run_dir_checked(run_name, runs_dir)
    rec = (st.get("steps") or {}).get(step_name)
    if rec is None:
        raise RequestError("Step not in this run", 404)
    if rec.get("status") != "running":
        raise RequestError(
            f"Step is {rec.get('status')}, not running - its turn is "
            "over, so an interrupt has nothing to stop. Leave resume "
            "notes instead; they reach the next dispatch.", 409)
    if not step_live(rec, driver_alive(st)):
        raise RequestError("This step is already dead (its record is "
                           "stale - sweep will mark it lost)", 409)
    if not st.get("intrq"):
        raise RequestError(
            "This run's conductor does not poll interrupt requests (it "
            "predates the feature) - use Stop this step, then resume "
            "notes + Resume instead", 409)
    instructions = body.get("instructions")
    if not isinstance(instructions, str) or not instructions.strip():
        raise RequestError("instructions must be non-empty text")
    instructions = instructions.strip()
    if len(instructions) > 20_000:
        raise RequestError("instructions must be at most 20,000 "
                           "characters", 422)
    attachments = _attachment_fields(body.get("attachments"))
    if intrq_flag(d, step_name).is_file():
        raise RequestError(
            "An interrupt request is already pending for this step - "
            "wait for the current one to land", 409)
    payload = {"instructions": instructions, "attachments": attachments,
               "submitted_at": now_iso()}
    # atomic land: a crash mid-write must never leave half a JSON body
    # for the driver to find unparseable (it would stay 'pending'
    # forever); tmp-then-replace makes the request all-or-nothing.
    flag = intrq_flag(d, step_name)
    tmp = flag.with_name(flag.name + ".tmp")
    try:
        _flag_dir().mkdir(exist_ok=True)
        tmp.write_text(json.dumps(payload, ensure_ascii=False),
                       encoding="utf-8")
        tmp.replace(flag)
    except OSError as exc:
        tmp.unlink(missing_ok=True)
        raise RequestError(
            f"Could not write the interrupt request: {exc}", 500) from exc
    # Same courtesy as stop_step: when the pid is visible kill it now
    # rather than waiting out the flag poll - the driver sees the
    # sentinel either way and only the pid's own tree dies (never the
    # run, sibling steps, or the dashboard's process).
    if rec.get("pid"):
        _kill_pid(rec["pid"], rec.get("started_at"))
    # Same trap-5 rule as stop_step - the flag-dir history is safe.
    log_user_event(d, event="step_interrupt_requested", step=step_name,
                   chars=len(instructions), preview=instructions[:300],
                   attachments=len(attachments))
    return {"interrupting": step_name}


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
        log_user_event(d, event="step_skip_requested", step=step_name)
        return {"skipped": step_name}
    skip_flag(d, step_name).unlink(missing_ok=True)
    log_user_event(d, event="step_unskip_requested", step=step_name)
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
        log_user_event(d, event="run_pause_requested", live=len(live))
        return {"pausing": live}
    if body.get("drain"):
        for f in drain_flags_for(d, st):
            _flag_raise(f.name)
        log_user_event(d, event="run_drain_requested")
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
    log_user_event(d, event="run_stop_requested", live=len(live))
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
    log_user_event(d, event="run_archive_mark", archived=archived)
    return {"run": run_name, "archived": archived}


# ------------------------------------------------------------------- trash
# 'Delete run' / 'Discard' move their bundle to data/trash/<entry>/
# (meta.json + payload/) instead of destroying it. The record drops out of
# every normal listing the moment it moves, stays recoverable for
# TRASH_RETAIN_S (default 48h), and restores via the dashboard Undo action
# or the Trash view. Only two paths destroy for real: the bounded sweep of
# EXPIRED entries, and 'Empty trash' behind an explicit confirm.
#
# What moves: for a run, the whole data/runs/<ts>-<name>/ dir; for a
# request, the <id>.json record, its owned <id>-*.toml proposal, every
# <id>.* side artifact (gen.log/export/draft/run.log), and the planner
# spool dir (it must move - a parked overlay would otherwise flush and
# resurrect the record). What never moves: spawned devin sessions (restore
# re-whitelists them via the returned state.json), parked worktree
# branches/checkouts, request image attachments (absolute-path references,
# possibly shared), and anything outside the managed roots - payload names
# are minted here and every dest name is re-validated as a direct child of
# its root on restore, so a hand-edited meta cannot aim the engine at
# unrelated data.


def _trash_root(runs_dir: Path) -> Path:
    return runs_dir.parent / "trash"


def _trash_log(trash_dir: Path, **kv) -> None:
    """Append-only audit line into the trash's own events.jsonl - a trashed
    run dir moves wholesale, so its own events.jsonl can't carry the
    deletion record."""
    try:
        trash_dir.mkdir(parents=True, exist_ok=True)
        kv["ts"] = now_iso()
        with STATE_LOCK:
            with open(trash_dir / "events.jsonl", "a",
                      encoding="utf-8") as f:
                f.write(json.dumps(kv, ensure_ascii=False) + "\n")
    except OSError:
        pass


def _trash_new(trash_dir: Path, kind: str, name: str, ref: str,
               note: str = "") -> tuple[Path, dict]:
    """Mint an entry dir + meta skeleton. meta.json is written LAST, after
    every payload move - it is the entry's commit point, so a crash mid-
    move leaves a dir no listing or sweep recognizes as trash."""
    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    for _ in range(20):
        d = trash_dir / f"{stamp}-{secrets.token_hex(3)}"
        try:
            (d / "payload").mkdir(parents=True)
            break
        except FileExistsError:
            continue
    else:
        raise RequestError("Could not create a trash entry", 500)
    deleted = now_iso()
    exp = dt.datetime.fromtimestamp(
        time.time() + TRASH_RETAIN_S).isoformat(timespec="seconds")
    meta = {"id": d.name, "kind": kind, "name": name, "ref": ref,
            "deleted_at": deleted, "expires_at": exp, "note": note,
            "items": []}
    return d, meta


def _trash_take(entry_dir: Path, meta: dict, src: Path, slot: str,
                dest_kind: str, dest_name: str, is_dir: bool) -> None:
    """Move `src` into the entry payload and record how it goes back:
    `dest_kind` names the managed root (runs/requests/manifests/spool) and
    `dest_name` the direct-child basename it restores under."""
    payload = entry_dir / "payload" / f"{len(meta['items']):02d}-{slot}"
    shutil.move(str(src), str(payload))
    meta["items"].append({"slot": slot, "payload": payload.name,
                          "dest": dest_kind, "name": dest_name,
                          "dir": bool(is_dir)})


def _trash_store_record(entry_dir: Path, meta: dict, rec: dict,
                        dest_name: str) -> None:
    """Persist a request record that exists only as a spool overlay (the
    <id>.json file never landed) as the entry's record payload."""
    p = entry_dir / "payload" / f"{len(meta['items']):02d}-record"
    p.write_text(json.dumps(rec, indent=2, ensure_ascii=False),
                 encoding="utf-8")
    meta["items"].append({"slot": "record", "payload": p.name,
                          "dest": "requests", "name": dest_name,
                          "dir": False})


def _trash_write_meta(entry_dir: Path, meta: dict) -> None:
    tmp = entry_dir / "meta.json.tmp"
    tmp.write_text(json.dumps(meta, indent=2, ensure_ascii=False),
                   encoding="utf-8")
    tmp.replace(entry_dir / "meta.json")


def _trash_entry(trash_dir: Path, entry_id: str) -> tuple[Path, dict]:
    """Resolve + read a real trash entry (id == dir name == meta id); the
    regex gate and resolved-parent check keep callers inside trash_dir."""
    if not TRASH_ID_RE.fullmatch(entry_id or ""):
        raise RequestError("Invalid trash entry", 404)
    d = (trash_dir / entry_id).resolve()
    if d.parent != trash_dir.resolve() or not d.is_dir():
        raise RequestError("Trash entry not found", 404)
    try:
        meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RequestError("Trash entry is not readable", 500) from exc
    if not isinstance(meta, dict) or meta.get("id") != entry_id:
        raise RequestError("Trash entry metadata mismatch", 500)
    return d, meta


def _legacy_discards(requests_dir: Path, now: float) -> list[dict]:
    """Pre-trash 'Discard' tombstones: status=='discarded' records still
    sitting in requests_dir (their manifest file was already unlinked).
    Listed as `legacy-<id>` trash entries so they can still be restored -
    they come back as drafts - or purged; the deletion clock is the
    record's own updated_at."""
    out = []
    try:
        files = sorted(requests_dir.glob("*.json"))
    except OSError:
        return out
    for rf in files:
        try:
            rec = json.loads(rf.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(rec, dict) or rec.get("status") != "discarded" \
                or not REQUEST_ID_RE.fullmatch(str(rec.get("id") or "")):
            continue
        deleted = rec.get("updated_at") or rec.get("created_at")
        exp = (_iso_ts(deleted) or now) + TRASH_RETAIN_S
        out.append({"id": f"legacy-{rec['id']}", "kind": "request",
                    "name": rec.get("label") or rec["id"], "ref": rec["id"],
                    "deleted_at": deleted,
                    "expires_at": dt.datetime.fromtimestamp(exp).isoformat(
                        timespec="seconds"),
                    "expires_in_s": max(0.0, exp - now),
                    "note": "discarded before trash existed - restores as "
                            "a draft; its proposal file was not kept",
                    "items": 0, "legacy": True})
    return out


def list_trash(trash_dir: Path, requests_dir: Path,
               now: float | None = None) -> list[dict]:
    """Every recoverable entry, newest first: real data/trash/*/ entries
    plus legacy 'discarded' request tombstones."""
    now = time.time() if now is None else now
    out = []
    try:
        dirs = sorted(trash_dir.iterdir())
    except OSError:
        dirs = []
    for d in dirs:
        if not d.is_dir() or not TRASH_ID_RE.fullmatch(d.name):
            continue
        try:
            meta = json.loads((d / "meta.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue   # a mid-move entry is not trash yet - leave it alone
        if not isinstance(meta, dict) or meta.get("id") != d.name:
            continue
        out.append({"id": d.name, "kind": meta.get("kind"),
                    "name": meta.get("name"), "ref": meta.get("ref"),
                    "deleted_at": meta.get("deleted_at"),
                    "expires_at": meta.get("expires_at"),
                    "expires_in_s": max(
                        0.0, (_iso_ts(meta.get("expires_at")) or now) - now),
                    "note": meta.get("note"),
                    "items": len(meta.get("items") or []), "legacy": False})
    out += _legacy_discards(requests_dir, now)
    out.sort(key=lambda e: e.get("deleted_at") or "", reverse=True)
    return out


def _trash_dest(item: dict, dirs: dict[str, Path]) -> Path:
    """The restore destination of one item - always a direct child of its
    managed root, or the move refuses to follow it."""
    name = item.get("name")
    root = dirs.get(item.get("dest"))
    if not name or Path(str(name)).name != name or root is None:
        raise RequestError("Corrupt trash entry item", 500)
    dest = (root / name).resolve()
    if dest.parent != root.resolve():
        raise RequestError("Trash item escapes its managed root", 500)
    return dest


def _rewrite_run_paths(run_dir: Path, old_abs: str) -> None:
    """A run restored under a different dir name gets its state.json's
    self-references (run_dir + each step's prompt/stdout_log/export, all
    recorded as absolute paths) retargeted to the new location."""
    sj = run_dir / "state.json"
    try:
        st = json.loads(sj.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    old = os.path.normcase(str(old_abs)).rstrip("\\/")
    new = str(run_dir)

    def rw(v):
        if not isinstance(v, str):
            return v
        nv = os.path.normcase(v)
        if nv == old:
            return new
        return new + v[len(old):] if nv.startswith(
            old + os.sep) or nv.startswith(old + "/") else v

    st["run_dir"] = rw(st.get("run_dir"))
    for r in (st.get("steps") or {}).values():
        for k in ("prompt", "stdout_log", "export"):
            r[k] = rw(r.get(k))
    try:
        sj.write_text(json.dumps(st, indent=2), encoding="utf-8")
    except OSError:
        pass


def delete_run(run_name: str, body: dict, runs_dir: Path = RUNS_DIR,
               trash_dir: Path | None = None,
               requests_dir: Path = REQUESTS_DIR) -> dict:
    """Dashboard 'Delete run': move the run's whole on-disk record - state,
    events, prompts, stdout logs, ATIF exports - to recoverable trash
    (data/trash/<entry>/), NOT rmtree. The row leaves every normal list at
    once and the entry restores via the in-app Undo action or the Trash
    view for TRASH_RETAIN_S; after that the bounded sweep purges it, and
    'Empty trash' is the only immediate permanent delete.

    Same gates resume_run/merge_run use: {"confirm": true}, refused while
    any step is live, a resume/merge is in flight, or a summarizer session
    is writing into the dir. The devin sessions it spawned are NOT deleted
    (they leave the `sessions` whitelist until a restore brings state.json
    back, same as trap-13 orphans) and parked worktree branches outlive
    the record - the entry meta names them; trash never touches git."""
    if body.get("confirm") is not True:
        raise RequestError("Confirm delete explicitly")
    trash_dir = trash_dir or _trash_root(runs_dir)
    d, st = _run_dir_checked(run_name, runs_dir)
    if driver_alive(st):
        raise RequestError("This run's conductor is still running - "
                           "delete it once the run has stopped", 409)
    if any(step_live(r, driver_alive(st)) for r in (st.get("steps") or {}).values()):
        raise RequestError("This run still has a live agent - wait for it "
                           "or let it die first", 409)
    pid = _RESUME_PIDS.get(run_name)
    if pid and pid_alive(pid):
        raise RequestError("A resume is already running for this run", 409)
    with _MERGE_LOCK:
        if run_name in _MERGE_BUSY or pid_alive(_MERGE_PIDS.get(run_name)):
            raise RequestError("A merge is already running for this run",
                               409)
    holder = _merge_claim_held(d)
    if holder:
        raise RequestError(
            f"A {holder.get('by') or 'merge'} is in flight for this run "
            "- delete once it finishes", 409)
    if _summarizing(d) or pid_alive(_SUM_PIDS.get(run_name)):
        raise RequestError("A summary session is still writing into this "
                           "run dir - wait for it or let it die first", 409)
    _assert_unwatched(runs_dir, d, trash_dir)
    unmerged = [n for n, r in (st.get("steps") or {}).items()
                if r.get("branch") and r.get("merged") is not True]
    entry_dir, meta = _trash_new(
        trash_dir, "run", st.get("run") or run_name, run_name,
        note=("spawned devin sessions are not deleted - restoring "
              "re-whitelists them" +
              (f"; unmerged branches stay parked: {', '.join(unmerged)}"
               if unmerged else "")))
    meta["origin"] = str(d)
    try:
        _trash_take(entry_dir, meta, d, "run", "runs", d.name, is_dir=True)
    except OSError as exc:
        shutil.rmtree(entry_dir, ignore_errors=True)
        raise RequestError(f"Could not move the run to trash: {exc}",
                           500) from exc
    _trash_write_meta(entry_dir, meta)
    _trash_log(trash_dir, event="trash_add", kind="run", ref=run_name,
               entry=meta["id"], name=meta["name"])
    # Sentinels in %TEMP% key on the run's resolved path - sweep the
    # path-keyed set (stop/skip/prompt/rnote/timeout) plus the bare
    # run-level legacy names. A restored dir re-keys identically when it
    # lands under its old name, but deliberately starts flag-clean: an
    # interrupted skip/stop decision is not something a restore replays.
    for f in _flag_dir().glob(f"*{_flag_key(d)}*"):
        if f.name.startswith("hist-"):
            continue          # audit, not a control sentinel
        try:
            f.unlink()
        except OSError:
            pass
    log_user_event(d, event="run_trashed", trash_id=meta["id"])
    for prefix in ("stop", "drain", "pause"):
        try:
            (_flag_dir() / f"{prefix}-{_safe_name(d.name)}").unlink(
                missing_ok=True)
        except OSError:
            pass
    # The request that spawned this run only reads 'launched' while a run
    # dir carrying its request_id exists; with the dir trashed its stored
    # 'launching' would reconcile to 'launch_failed' - a lie (the launch
    # succeeded; the run was deleted later). Hand it back to 'ready' so
    # the row offers Start run (or Discard); restoring the run re-anchors
    # it to 'launched' via the request_id scan either way.
    rid = st.get("request_id")
    if rid and REQUEST_ID_RE.fullmatch(str(rid)):
        try:
            rrec = _read_request(str(rid), requests_dir)
            if rrec.get("status") == "launching":
                rrec.update(status="ready", error=None,
                            updated_at=now_iso())
                _save_request(rrec, requests_dir)
        except (OSError, ValueError):
            pass
    return {"deleted": run_name, "trashed": True,
            "trash_id": meta["id"], "expires_at": meta["expires_at"]}


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
    log_user_event(d, event="step_timeout_changed", step=step_name,
                   minutes=minutes)
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
            log_user_event(d, event="step_prompt_reverted",
                           step=step_name)
            return {"prompt_override": None}
        if not isinstance(body["prompt"], str):
            raise RequestError("prompt must be text")
        if not 1 <= len(body["prompt"]) <= 100_000:
            raise RequestError("prompt must be 1-100,000 characters", 422)
        flag.write_text(body["prompt"], encoding="utf-8")
    except OSError as exc:
        raise RequestError(f"Could not write the prompt flag: {exc}",
                           500) from exc
    log_user_event(d, event="step_prompt_edited", step=step_name,
                   chars=len(body["prompt"]))
    return {"prompt_override": body["prompt"]}


def set_step_notes(run_name: str, step_name: str, body: dict,
                   runs_dir: Path = RUNS_DIR) -> dict:
    """Dashboard notes/instructions: operator text in a tempdir sentinel
    that run_step appends to the step's next dispatched prompt - the
    original prompt itself is never edited. For steps that ran and will
    re-run (stopped/failed/timed_out/lost/blocked/partial) it is 'resume
    notes'; for steps still waiting their first dispatch (queued, skipped,
    or a manifest step with no record yet) it is 'additional instructions'
    - same channel, so they land in the first prompt too. {"notes": ""}
    clears the field."""
    if not RUN_DIR_RE.fullmatch(step_name):
        raise RequestError("Invalid step name", 404)
    d, st = _run_dir_checked(run_name, runs_dir)
    rec = (st.get("steps") or {}).get(step_name)
    if rec is None:
        if step_name not in _manifest_step_names(st):
            raise RequestError("Step not in this run", 404)
    else:
        eff = effective_statuses(st).get(step_name, rec.get("status"))
        if eff not in ("blocked", "partial", "failed", "timed_out",
                       "stopped", "lost", "queued", "skipped"):
            raise RequestError(
                f"Step is {eff} - notes apply to steps still waiting or "
                "re-running; a running step takes Interrupt + instruct", 409)
    flag = rnote_flag(d, step_name)
    try:
        _flag_dir().mkdir(exist_ok=True)
        if body.get("notes") in (None, ""):
            flag.unlink(missing_ok=True)
            log_user_event(d, event="step_notes_cleared", step=step_name)
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
    log_user_event(d, event="step_notes_updated", step=step_name,
                   chars=len(body["notes"]))
    return {"resume_notes": body["notes"]}


def set_run_review(run_name: str, body: dict,
                   runs_dir: Path = RUNS_DIR) -> dict:
    """Dashboard 'Mark as reviewed'/'Mark as needing review' for the RUN
    as a whole - the run's own mark, never folded into its tasks."""
    return _set_review(run_name, None, body, runs_dir)


def set_step_review(run_name: str, step_name: str, body: dict,
                    runs_dir: Path = RUNS_DIR) -> dict:
    """The same marks for one TASK - never touches the run's own mark."""
    if not RUN_DIR_RE.fullmatch(step_name):
        raise RequestError("Invalid step name", 404)
    return _set_review(run_name, step_name, body, runs_dir)


def _set_review(run_name: str, step_name: str | None, body: dict,
                runs_dir: Path) -> dict:
    """A review mark is written to a review-* sentinel first - the only
    write that is always safe under trap 5 - then folded into state.json
    by the driver's next save_state, or immediately when nothing in the
    run is live. An ack binds the completion_seq it covered: a resume/
    retry re-completion bumps seq so the stale ack stops matching, while
    repeated saves of the same outcome never resurrect the indicator.
    An explicit {'seq': n} body guards the ack against a race - when a
    newer completion already landed than the view the click was made
    from, the POST is refused and the human sees the new state first."""
    reviewed = body.get("reviewed")
    if not isinstance(reviewed, bool):
        raise RequestError("'reviewed' must be true or false", 422)
    d, st = _run_dir_checked(run_name, runs_dir)
    if step_name is None:
        target, seq = st, st.get("completion_seq")
    else:
        target = (st.get("steps") or {}).get(step_name)
        if target is None:
            if step_name not in _manifest_step_names(st):
                raise RequestError("Step not in this run", 404)
            target, seq = {}, None
        else:
            seq = target.get("completion_seq")
    seen = body.get("seq", "__unset__")
    if reviewed and seen != "__unset__" and seen != seq:
        raise RequestError("A newer completion landed since this view "
                           "loaded - reload and review it", 409)
    mark = _review_mark(d, target.get("review"), step_name)
    if reviewed and seq is None and not (
            isinstance(mark, dict) and mark.get("reviewed") is False):
        raise RequestError(
            "Nothing to review yet - this "
            + ("run" if step_name is None else "step")
            + " has no recorded completion", 409)
    out = {"reviewed": reviewed, "seq": seq, "at": now_iso()}
    try:
        _flag_dir().mkdir(exist_ok=True)
        review_flag(d, step_name).write_text(
            json.dumps(out), encoding="utf-8")
        # Marks cascade parent <-> children so the two levels never read
        # inconsistently: a run-level mark covers every recorded task
        # ('needs review' flags them all, an ack clears each completion
        # it saw), and a task mark re-derives the run's - reviewed only
        # once EVERY recorded step is covered, unreviewed as soon as one
        # needs a look. Steps that never completed anything (no seq, no
        # terminal outcome) are skipped: flagging them would invent work
        # to review that never happened.
        steps = st.get("steps") or {}
        if step_name is None:
            for sname, srec in steps.items():
                if not reviewed and srec.get("completion_seq") is None \
                        and srec.get("status") not in _REVIEWABLE_ST:
                    continue
                review_flag(d, sname).write_text(json.dumps(
                    {"reviewed": reviewed,
                     "seq": srec.get("completion_seq"),
                     "at": out["at"]}), encoding="utf-8")
        elif steps:
            covered = all(not _needs_review(
                srec.get("completion_seq"),
                _review_mark(d, srec.get("review"), sname))
                for sname, srec in steps.items())
            review_flag(d).write_text(json.dumps(
                {"reviewed": covered, "seq": st.get("completion_seq"),
                 "at": out["at"]}), encoding="utf-8")
    except OSError as exc:
        raise RequestError(f"Could not write the review flag: {exc}",
                           500) from exc
    # Nothing of this run's live -> fold the marks into state.json now
    # (same shape the driver's own save performs next). Two cases keep
    # the mark flag-only until a later save lands it: a driver still up
    # would clobber the fold with its in-memory state anyway (and its
    # next save re-folds the flag), and a watched runs_dir makes ANY
    # state write a trap-5 violation.
    if not driver_alive(st) and not any(
            step_live(r, False) for r in (st.get("steps") or {}).values()):
        try:
            _assert_unwatched(runs_dir, d)
        except RequestError:
            pass
        else:
            save_state(d, st)
    log_user_event(d, event="review_marked",
                   step=step_name, reviewed=reviewed)
    return {"run": run_name, "step": step_name, "review": out,
            "needs_review": _needs_review(seq, out)}


def discard_request(request_id: str,
                    requests_dir: Path = REQUESTS_DIR,
                    manifests_dir: Path = MANIFESTS_DIR,
                    runs_dir: Path = RUNS_DIR,
                    trash_dir: Path | None = None) -> dict:
    """Dashboard 'Discard': move the request's proposal bundle to
    recoverable trash instead of marking it destroyed - the <id>.json
    record, the owned <id>-*.toml proposal, every <id>.* artifact
    (gen.log/export/draft/run.log) and the planner spool all keep for
    TRASH_RETAIN_S. The row leaves every normal list at once; Undo/Restore
    returns it at the same workflow stage with attachments and the
    follow-up flag intact. Running/launched proposals still refuse, as do
    watched destinations (trap 5)."""
    trash_dir = trash_dir or _trash_root(runs_dir)
    with REQUEST_LOCK:
        rec = _read_request(request_id, requests_dir)   # overlay wins
        if _request_status(rec, runs_dir) not in _REQUEST_EDITABLE_STAGES:
            raise RequestError("A running or launched proposal cannot be discarded",
                               409)
        _assert_unwatched(runs_dir, requests_dir, manifests_dir, trash_dir)
        entry_dir, meta = _trash_new(
            trash_dir, "request", rec.get("label") or request_id,
            request_id,
            note=("image attachments stay in data/attachments (referenced "
                  "by path, possibly shared); the planner spool moved with "
                  "the record so a pending flush cannot resurrect it"))
        try:
            # the parked overlay decides which copy of the record is
            # authoritative - decide BEFORE the spool dir moves away
            overlay = _spool_rec(request_id) is not None
            spool = _spool_dir(request_id)
            if spool.is_dir():
                _trash_take(entry_dir, meta, spool, "spool", "spool",
                            spool.name, is_dir=True)
            if rec.get("manifest"):
                try:
                    mp = _manifest_path(rec, manifests_dir)
                except RequestError:
                    mp = None          # not owned / bad shape - leave it
                if mp is not None and mp.is_file():
                    _trash_take(entry_dir, meta, mp, "manifest",
                                "manifests", mp.name, is_dir=False)
            recp = _request_path(request_id, requests_dir)
            for art in sorted(requests_dir.glob(f"{request_id}.*")):
                if art.is_file() and art.name != recp.name:
                    _trash_take(entry_dir, meta, art, "artifact",
                                "requests", art.name, is_dir=False)
            if recp.is_file() and not overlay:
                _trash_take(entry_dir, meta, recp, "record", "requests",
                            recp.name, is_dir=False)
            else:
                # A parked spool overlay is what readers see - the on-disk
                # copy is its shadow. Persist the overlay AS the record;
                # the shadow is superseded, not payload.
                recp.unlink(missing_ok=True)
                _trash_store_record(entry_dir, meta, rec, recp.name)
        except OSError as exc:
            shutil.rmtree(entry_dir, ignore_errors=True)
            raise RequestError(
                f"Could not move the request to trash: {exc}", 500) from exc
        _trash_write_meta(entry_dir, meta)
        _trash_log(trash_dir, event="trash_add", kind="request",
                   ref=request_id, entry=meta["id"], name=meta["name"])
        # An imported follow-up's source file stays on disk after Discard -
        # the veto mark is what keeps discovery from resurrecting the row
        _followup_mark(requests_dir, request_id, True)
        out = dict(rec)
        out.update(trashed=True, trash_id=meta["id"],
                   expires_at=meta["expires_at"])
        return out


def _restore_request_entry(entry_dir: Path, meta: dict,
                           requests_dir: Path, manifests_dir: Path,
                           runs_dir: Path) -> dict:
    """Put a request bundle back: artifacts -> requests/, the proposal
    TOML -> manifests/ (a `-restored` rename when a newer file holds the
    old name), the parked spool -> PLANNER_SPOOL, and the <id>.json record
    LAST - an existing record means a same-id request was recreated, which
    is newer work and is never overwritten (the whole restore refuses).
    The record comes back byte-identical, so the effective workflow stage
    is whatever _request_status recomputes for it."""
    payload = (entry_dir / "payload").resolve()
    dirs = {"runs": runs_dir, "requests": requests_dir,
            "manifests": manifests_dir, "spool": Path(PLANNER_SPOOL)}
    plan: list[tuple[dict, Path, Path | None]] = []
    rec_item = None
    for item in meta.get("items") or []:
        src = (payload / str(item.get("payload") or "")).resolve()
        if src.parent != payload or not src.exists():
            raise RequestError(
                f"Trash payload {item.get('payload')} is missing", 500)
        dest = _trash_dest(item, dirs)
        if item.get("slot") == "record":
            rec_item = (item, src, dest)
            continue
        if item.get("slot") == "manifest" and dest.exists():
            # newer work owns the name - land beside it under a -restored
            # variant (still <id>-prefixed, so _manifest_path accepts it)
            rid = str(meta.get("ref") or "")
            base_name = dest.name[:-5] if dest.name.endswith(".toml") \
                else dest.name
            for n in range(1, 20):
                cand = dest.with_name(
                    f"{base_name}-restored{'' if n == 1 else '-' + str(n)}"
                    f".toml")
                if not cand.exists():
                    dest = cand
                    break
            else:
                raise RequestError(
                    "No free manifest name to restore under", 500)
            if not cand.name.startswith(f"{rid}-"):
                raise RequestError(
                    "Restored manifest name would not belong to the "
                    "request", 500)
        plan.append((item, src, dest))
    if rec_item is not None and rec_item[2].exists():
        raise RequestError(
            "A request with this id already exists - not overwriting "
            "newer work", 409)
    rid = str(meta.get("ref") or "")
    if rec_item is None:
        raise RequestError("Trash entry has no request record", 500)
    manifest_dest = next((d for i, s, d in plan
                          if i.get("slot") == "manifest"), None)
    moved: list[tuple[dict, Path]] = []
    failed: list[str] = []
    for item, src, dest in plan:
        if dest.exists():
            # an occupied side-file/spool dest means newer work owns it -
            # keep it and drop our copy with the entry
            moved.append((item, dest))
            continue
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dest))
            moved.append((item, dest))
        except OSError:
            failed.append(item.get("payload") or "?")
    if failed:
        meta["items"] = [rec_item[0]] + [
            i for i, _s, _d in plan
            if (i.get("payload") or "?") in failed]
        _trash_write_meta(entry_dir, meta)
        raise RequestError(
            "Restore was partial - failed item(s) stay in trash: "
            + ", ".join(failed), 500)
    rec = json.loads(rec_item[1].read_text(encoding="utf-8"))
    if manifest_dest is not None:
        rec["manifest"] = str(manifest_dest)   # repoint (rename or moved
                                               # manifests root)
    recp = _request_path(rid, requests_dir)
    tmp = requests_dir / f"{rid}.tmp"
    tmp.write_text(json.dumps(rec, indent=2, ensure_ascii=False),
                   encoding="utf-8")
    tmp.replace(recp)                          # atomic, like _save_request
    return {"restored": rid, "status": rec.get("status") or "draft"}


def _restore_run_entry(entry_dir: Path, meta: dict,
                       runs_dir: Path) -> dict:
    """Put a run dir back under data/runs. A same-named dir is newer work
    and is never overwritten - the payload lands as `<name>-restored[-N]`
    and its internal absolute self-references are rewritten."""
    payload = (entry_dir / "payload").resolve()
    item = next((i for i in (meta.get("items") or [])
                 if i.get("slot") == "run"), None)
    if item is None:
        raise RequestError("Trash entry has no run payload", 500)
    src = (payload / str(item.get("payload") or "")).resolve()
    if src.parent != payload or not src.is_dir():
        raise RequestError("Run payload is missing from trash", 500)
    dest = _trash_dest(item, {"runs": runs_dir})
    actual = dest
    if dest.exists():
        for n in range(1, 20):
            cand = runs_dir / (f"{item['name']}-restored"
                               + ("" if n == 1 else f"-{n}"))
            if not cand.exists():
                actual = cand
                break
        else:
            raise RequestError(
                "No free run dir name to restore under", 500)
    _assert_unwatched(runs_dir, actual)
    try:
        shutil.move(str(src), str(actual))
    except OSError as exc:
        raise RequestError(f"Could not restore the run dir: {exc}",
                           500) from exc
    renamed = actual != dest
    if renamed:
        _rewrite_run_paths(actual, str(meta.get("origin") or dest))
    log_event(actual, event="run_restored", entry=meta.get("id"),
              restored_as=actual.name)
    return {"restored": item["name"], "restored_as": actual.name,
            "renamed": renamed}


def _restore_legacy_discard(request_id: str, requests_dir: Path) -> dict:
    """A pre-trash tombstone never left requests_dir - restoring clears its
    'discarded' mark back to 'draft' (its proposal file was already gone
    when the old code discarded it)."""
    with REQUEST_LOCK:
        rec = _read_request_file(request_id, requests_dir)
        if rec.get("status") != "discarded":
            raise RequestError("Request is not in trash", 409)
        rec["status"] = "draft"
        rec["error"] = None
        _save_request(rec, requests_dir)
        return {"restored": request_id, "status": "draft",
                "legacy": True}


def restore_trash(entry_id: str, trash_dir: Path = TRASH_DIR,
                  runs_dir: Path = RUNS_DIR,
                  requests_dir: Path = REQUESTS_DIR,
                  manifests_dir: Path = MANIFESTS_DIR) -> dict:
    """Trash view 'Restore' (and the post-delete Undo): put the entry's
    payloads back at their recorded destinations, at the workflow state
    they carried. Never overwrites an occupied destination - run dirs
    rename to `<name>-restored[-N]` with internal paths rewritten, a
    proposal manifest lands beside a same-named newer file under a
    `-restored` variant (rec['manifest'] repointed), side artifacts keep
    the existing file, and an occupied request record refuses the whole
    restore. Refused while a destination sits in a live agent's watched
    workspace (trap 5)."""
    m = re.fullmatch(r"legacy-(\d{8}-\d{6}-[0-9a-f]{8})", entry_id or "")
    if m:
        return _restore_legacy_discard(m.group(1), requests_dir)
    entry_dir, meta = _trash_entry(trash_dir, entry_id)
    _assert_unwatched(runs_dir, requests_dir, manifests_dir, trash_dir)
    if meta.get("kind") == "run":
        out = _restore_run_entry(entry_dir, meta, runs_dir)
    elif meta.get("kind") == "request":
        out = _restore_request_entry(entry_dir, meta, requests_dir,
                                     manifests_dir, runs_dir)
        # a restored import resumes its registration - discovery updates it
        # again instead of treating the source as vetoed
        _followup_mark(requests_dir, out.get("restored") or "", False)
    else:
        raise RequestError("Unknown trash entry kind", 500)
    shutil.rmtree(entry_dir, ignore_errors=True)
    _trash_log(trash_dir, event="trash_restore", kind=meta.get("kind"),
               ref=meta.get("ref"), entry=entry_id)
    return out


def empty_trash(body: dict, trash_dir: Path = TRASH_DIR,
                requests_dir: Path = REQUESTS_DIR,
                runs_dir: Path = RUNS_DIR) -> dict:
    """Trash view 'Empty trash': the ONLY immediate permanent delete -
    every entry dir plus legacy discard tombstones. The handler requires
    {"confirm": true} and the UI words the confirm as irreversible; a live
    agent watching the trash/requests tree refuses it (trap 5)."""
    if body.get("confirm") is not True:
        raise RequestError("Confirm emptying trash explicitly")
    _assert_unwatched(runs_dir, trash_dir, requests_dir)
    entries = list_trash(trash_dir, requests_dir)
    gone = 0
    for e in entries:
        if e.get("legacy"):
            try:
                _request_path(e["ref"], requests_dir).unlink(
                    missing_ok=True)
                gone += 1
            except (OSError, RequestError):
                pass
            continue
        d = (trash_dir / e["id"]).resolve()
        try:
            if d.parent == trash_dir.resolve() and d.is_dir():
                shutil.rmtree(d)
                gone += 1
        except OSError:
            pass
    _trash_log(trash_dir, event="trash_empty", count=gone)
    return {"emptied": gone}


def sweep_trash(trash_dir: Path, requests_dir: Path,
                runs_dir: Path | None = None, now: float | None = None,
                write: bool = True,
                limit: int = TRASH_SWEEP_MAX) -> list[str]:
    """Purge ONLY expired trash entries - oldest expiry first, capped at
    `limit` per pass so the dashboard poll stays bounded. Entries with no
    readable meta.json are in-flight moves, not trash; they are never
    touched. Same trap-5 rule as sweep_runs: while a live worker's
    work_dir contains the trash or requests tree this degrades to a
    dry-run report."""
    now = time.time() if now is None else now
    watched: list[str] = []
    if runs_dir is not None:
        states = _load_run_states(runs_dir)
        watched = sorted({w for d in (trash_dir, requests_dir)
                          if d is not None
                          for w in _runs_watched(d, states)})
    lines: list[str] = []
    doomed = [e for e in list_trash(trash_dir, requests_dir, now)
              if e["expires_in_s"] <= 0]
    doomed.sort(key=lambda e: e.get("expires_at") or "")
    for e in doomed[:max(0, limit)]:
        lines.append(f"{e['id']}: purged ({e['kind']} "
                     f"'{e.get('name') or e.get('ref')}', expired "
                     f"{e.get('expires_at')})")
        if not write or watched:
            continue
        try:
            if e.get("legacy"):
                _request_path(e["ref"], requests_dir).unlink(
                    missing_ok=True)
            else:
                d = (trash_dir / e["id"]).resolve()
                if d.parent == trash_dir.resolve() and d.is_dir():
                    shutil.rmtree(d)
        except (OSError, RequestError):
            continue
        _trash_log(trash_dir, event="trash_purge", kind=e.get("kind"),
                   ref=e.get("ref"), entry=e["id"])
    if lines and write and watched:
        lines.insert(0, "DRY-RUN: live agent(s) watch the trash tree "
                        f"({_watcher_summary(watched)}) - not purging "
                        "(trap 5)")
    return lines


# ---------------------------------------------------------------- dashboard

SERVE_PORT = 8773    # registered in C:\Projects\AGENTS.md

_STATUS_COLOR = {"done": "#3fb950", "partial": "#d29922", "blocked": "#d29922",
                 "failed": "#f85149", "timed_out": "#f0883e",
                 "running": "#58a6ff", "queued": "#8b949e",
                 "waiting": "#8b949e", "skipped": "#6e7681",
                 "planned": "#bc8cff", "ready": "#bc8cff",
                 "generating": "#bc8cff", "draft": "#d29922",
                 "launching": "#58a6ff", "launch_failed": "#f85149",
                 "lost": "#6e7681", "superseded": "#8b949e"}

_AUTH_CACHE = [0.0, "unknown"]        # [checked_at, label]
_SESS_CACHE = [0.0, {}]               # [checked_at, {work_dir: set|None}]
_SESS_BUSY = False                    # a background re-list is in flight
_SESS_LOCK = threading.Lock()         # guards _SESS_BUSY check-and-set
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


def _collect_session_dirs(runs_dir: Path) -> set[str]:
    """work_dirs worth a `devin list`: records with a session_id (gone
    checks) and steps still plausibly alive (live attribution of the
    not-yet-recorded session id)."""
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
    return dirs


def live_sessions_by_dir(runs_dir: Path,
                         devin_argv: list[str]) -> dict[str, dict | None]:
    """work_dir -> {session id: last_activity epoch} from `devin list`
    there, cached 2min. A dir maps to None when listing failed (devin
    down/missing) - never interpret None as 'all sessions gone'.

    Each listing is a `devin list` SUBPROCESS per dir - tens of them
    serialize into multi-second stalls, so an expired cache re-lists on
    a background thread and the caller gets the last map meanwhile
    (stale answers beat a frozen poll; a dir missing from the map reads
    'unknown', which the gone-checks already treat as can't-tell)."""
    if _SESS_CACHE[1] and time.time() - _SESS_CACHE[0] < 120:
        return _SESS_CACHE[1]
    global _SESS_BUSY
    with _SESS_LOCK:
        if _SESS_BUSY:
            return _SESS_CACHE[1]
        _SESS_BUSY = True

    def work() -> None:
        global _SESS_BUSY
        try:
            dirs = _collect_session_dirs(runs_dir)
            out = {d: session_activity(devin_argv, Path(d))
                   for d in dirs}
            _SESS_CACHE[0], _SESS_CACHE[1] = time.time(), out
        finally:
            _SESS_BUSY = False

    try:
        threading.Thread(target=work, daemon=True).start()
    except Exception:
        _SESS_BUSY = False
    return _SESS_CACHE[1]


def task_label(rec: dict, full: bool = False) -> str | None:
    """What the agent was asked to do. New records store `task` directly
    (160-char summary); `full` goes back to the prompt file for the
    untruncated body the detail modal wants - falling back to the stored
    summary when the file is gone. Whatever the source, a leading
    conductor-purpose marker is metadata - task_display swaps it for the
    real body or a purpose label."""
    if rec.get("task") and not full:
        return task_display(rec["task"]) or rec["task"]
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
        return task_display(body) or rec.get("task")
    one = " ".join(task_display(body).split())
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


def live_step_log(rec: dict) -> Path | None:
    """The log the CURRENT dispatch is writing. rec['stdout_log'] only
    repoints at step end, so a retry's attemptN or an interrupt respawn's
    interruptN log would otherwise be invisible until then - and a stale
    attempt's 'STATUS: done' would masquerade as the live session's."""
    base = rec.get("stdout_log")
    if not base:
        return None
    p = Path(base)
    stem = rec.get("name") or p.name.split(".")[0]
    if rec.get("interrupts"):
        cand = p.with_name(
            f"{stem}.interrupt{rec['interrupts']}.stdout.log")
    elif (rec.get("attempts") or 1) > 1:
        cand = p.with_name(
            f"{stem}.attempt{rec['attempts']}.stdout.log")
    else:
        cand = p
    return cand if cand.is_file() else p


def file_mtime_iso(path: str | Path | None) -> str | None:
    """A file's last-write stamp - the modal's 'last update' row for
    stdout logs (a live step's log keeps ticking between polls)."""
    if not path:
        return None
    try:
        return dt.datetime.fromtimestamp(
            Path(path).stat().st_mtime).isoformat(timespec="seconds")
    except OSError:
        return None


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
            "task": step.get("task"), "task_full": None, "purpose": None,
            "depends_on": step.get("deps"), "dep_wait": None,
            "model_actual": None, "wt": step.get("wt"),
            "merge_target": step.get("mt"),
            "timeout_min": step.get("timeout_min"),
            "attempts": None, "max_attempts": None,
            "merged": None, "cloud": step.get("cloud"), "tokens": None,
            "deferred": step.get("merge") == "deferred",
            "merge_kind": None, "target_branch": None,
            "deferred_verified": None,
            "cost": None,
            "alive": False, "outcome": None, "outcomes": None,
            "note": "Run ended before this step started" if status == "skipped"
            else None}


def plan_attempt_agents(rec: dict, key: str, *, generating: bool,
                        log_path: Path, prices: dict) -> list[dict]:
    """Agent rows for a request's recorded planner sessions - the 'step 0'
    preparation history of its plan.

    Each completed attempt lands one row carrying its real terminal
    status and the session id captured while it ran. Labels keep
    agentKey (run_key|step) unique AND keep attribution honest: at rest,
    the attempt that produced the current manifest (rec['plan_gen_id'])
    is 'plan-proposal'; every other recorded attempt is 'plan-proposal-N'
    by attempt order. While a newer pass is generating, the live row
    holds the bare name and all completed attempts are numbered - an
    earlier session is never shown as a later attempt's producer. Only
    the newest completed attempt gets log/export/tail links: all attempts
    write the same <id>.gen.* files, so only its content is on disk.

    These rows are history, not work: no run_dir, no counts, never
    runnable (retry/resume operate on state.json + the manifest)."""
    entries = rec.get("plan_sessions") or []
    if not entries:
        return []
    producer = rec.get("plan_gen_id")
    export_path = log_path.with_suffix(".export.json")
    task = "Propose a TOML runbook for: " + " ".join(
        (rec.get("raw") or "").split())[:120]
    # the modal's prompt box wants the whole ask - the row label is the
    # clipped version, task_full carries the raw request verbatim
    raw_full = (rec.get("raw") or "").strip()
    task_full = "Propose a TOML runbook for: " + raw_full if raw_full else None
    out = []
    for i, e in enumerate(entries):
        produced = bool(e.get("gen_id")) and e.get("gen_id") == producer
        files = i == len(entries) - 1 and not generating
        log = str(log_path) if files and log_path.is_file() else None
        exp = str(export_path) if files and export_path.is_file() else None
        status = e.get("status") or "done"
        if produced:
            note = ("prepared this plan - preparation history, not a "
                    "run step")
        elif status == "stopped":
            note = e.get("note") or "stopped before producing a plan"
        elif e.get("note"):
            note = e["note"]
        elif status == "failed":
            note = "preparation attempt failed"
        else:
            note = "superseded by a later preparation"
        out.append({
            "run": rec.get("run") or rec.get("label"),
            "run_dir": None, "run_key": key, "request_id": rec["id"],
            "step": ("plan-proposal" if produced and not generating
                     else f"plan-proposal-{i + 1}"),
            "slug": rec.get("project") or "agent-conductor",
            "work_dir": "planner tempdir (auto-cleaned)",
            "status": status,
            "created_at": e.get("started_at") or rec.get("updated_at"),
            "started_at": e.get("started_at"),
            "ended_at": e.get("ended_at"),
            "elapsed_s": e.get("elapsed_s"),
            "alive": False, "pid": None,
            "session_id": e.get("session_id"), "session_gone": False,
            "model": e.get("model"),
            "model_actual": e.get("model_actual"),
            "effort": e.get("effort"),
            "mode": "planner", "cloud": False, "verify_ok": None,
            "task": task, "task_full": task_full, "purpose": None,
            "note": note, "next": None, "exit_code": None,
            "outcome": None, "outcomes": None,
            "branch": None, "merged": None,
            "deferred": False, "merge_kind": None, "target_branch": None,
            "deferred_verified": None,
            "tokens": e.get("tokens"), "wt": False,
            "cost": estimate_cost(e.get("tokens"), e.get("model"),
                                  e.get("effort"), prices,
                                  e.get("model_actual")),
            "stdout_log": log,
            "tail": stdout_tail(log) if log else None,
            "export": exp,
            "arc": cached_arc(exp) if exp else None,
            "prompt": None, "prompt_editable": False,
            "notes_enabled": False, "skipped_by_user": False,
            "seq": -1 if produced else None})
    return out


def summary_agent(st: dict, key: str, run_dir: Path,
                  prices: dict) -> list[dict]:
    """The bookkeeping row for a run's detached summarizer session -
    `aux` so the dashboard can render it muted and out of the pipeline
    nav. Emitted whenever a summarize session ran or is running (the
    .summarizing marker covers the launch window before the record's
    'running' write lands). Like the planner rows: not a pipeline step,
    never in counts, never runnable - but its tokens/session/elapsed
    deserve the same visibility the workers get."""
    rs = st.get("run_summary") or {}
    live = _summarizing(run_dir)
    if not rs and not live:
        return []
    status = ("running" if live else
              (rs.get("status") or ("done" if rs.get("generated_at")
                                    else "failed")))
    model = rs.get("model")
    tokens = rs.get("tokens")
    log = run_dir / "summarize.stdout.log"
    exp = run_dir / "summarize.export.json"
    return [{
        "run": st.get("run"), "run_dir": str(run_dir), "run_key": key,
        "request_id": st.get("request_id"),
        "step": "run-summary", "slug": "conductor",
        "aux": True, "seq": None,
        "work_dir": "tempdir (auto-cleaned)",
        "status": status, "created_at": rs.get("started_at"),
        "started_at": rs.get("started_at"),
        "ended_at": rs.get("ended_at"),
        "elapsed_s": rs.get("elapsed_s"),
        "alive": live, "pid": None,
        "session_id": rs.get("session"), "session_gone": False,
        "model": model, "model_actual": rs.get("model_actual"),
        "effort": None, "mode": "summarizer", "cloud": False,
        "verify_ok": None,
        "task": "Write this run's executive summary (run.summary.md)",
        "task_full": None, "purpose": None,
        "note": rs.get("note"), "next": None,
        "exit_code": rs.get("exit_code"),
        "outcome": ("wrote run.summary.md"
                    if rs.get("generated_at") else None),
        "outcomes": None,
        "branch": None, "merged": None,
        "deferred": False, "merge_kind": None, "target_branch": None,
        "deferred_verified": None,
        "tokens": tokens, "wt": False,
        "cost": estimate_cost(tokens, model, None, prices,
                              rs.get("model_actual")),
        "stdout_log": str(log) if log.is_file() else None,
        "tail": stdout_tail(str(log)) if log.is_file() else None,
        "log_mtime": file_mtime_iso(log) if log.is_file() else None,
        "export": str(exp) if exp.is_file() else None,
        "arc": cached_arc(str(exp)) if exp.is_file() else None,
        "prompt": None, "prompt_editable": False,
        "notes_enabled": False, "skipped_by_user": False}]


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
        task = task_summary(step.prompt)
        if not task and step.prompt_file:
            task = step.prompt_file
        out.append({"name": step.name, "slug": slug, "work_dir": wd,
                    "task": task, "model": step.model,
                    "effort": step.effort, "mode": step.mode,
                    "cloud": step.cloud, "wt": step.worktree,
                    "mt": step.merge_target, "merge": step.merge,
                    "timeout_min": step.timeout_min})
    return out


def _run_isolation(cfg: "Manifest | None", st: dict,
                   request: dict | None) -> dict | None:
    """The run's effective worktree isolation as configured, before any
    step has run: {'wt','local','cloud'} counts over its steps, or None
    when no local step schedules a worktree (or no configuration can be
    recovered - the dashboard then shows no label at all).

    The parsed manifest is authoritative - load_manifest already folds
    merge='deferred' into worktree=true and rejects worktree+cloud, so a
    step's worktree flag is what it WILL run under, not what it happened
    to spawn. Fallbacks for a gone/unparsable manifest: the request's
    proposed_steps snapshot (written from the same parse at publish/apply)
    then per-step 'wt' recorded at queue/dispatch. Branch names are never
    consulted - they only exist after dispatch and prove nothing about
    the rest of the pipeline."""
    items: list[tuple[bool, bool]] | None = None
    if cfg is not None:
        items = [(s.worktree, s.cloud) for s in cfg.steps]
    elif request and request.get("proposed_steps"):
        items = [(bool(s.get("wt")), bool(s.get("cloud")))
                 for s in request["proposed_steps"]]
    else:
        recs = list((st.get("steps") or {}).values())
        if recs and all("wt" in r for r in recs):
            items = [(bool(r["wt"]), bool(r.get("cloud")))
                     for r in recs]
    if not items:
        return None
    cloud = sum(1 for _w, c in items if c)
    wt = sum(1 for w, c in items if w and not c)
    if not wt:
        return None
    return {"wt": wt, "local": len(items) - cloud, "cloud": cloud}


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
            "task": sd.get("task"), "task_full": None, "purpose": None,
            "model_actual": None, "wt": sd.get("wt"),
            "merge_target": sd.get("mt"),
            "timeout_min": sd.get("timeout_min"),
            "attempts": None, "max_attempts": None,
            "merged": None, "cloud": sd.get("cloud"), "tokens": None,
            "deferred": sd.get("merge") == "deferred",
            "merge_kind": None, "target_branch": None,
            "deferred_verified": None,
            "cost": None, "alive": False, "outcome": None,
            "outcomes": None, "note": None}


def _pending_merge_view(st: dict, eff: dict[str, str]) -> tuple[
        list[dict], list[dict]]:
    """(deferred groups, legacy parked branches) for the run modal.

    Deferred entries come from state["pending_merges"] - one group per
    project repo, entries = step names still unlanded. Each entry carries
    what the selection UI needs: status, verify, kind (sequential |
    alternative - alternatives are same-wave variants that must never be
    silently combined), live-ness and the retained branch/worktree. The
    group surfaces the recorded target branch, baseline state and any
    kept-stage/moved-target residue from a previous merge attempt.

    Legacy pending = auto-policy worktree branches that never landed
    (conflict parked them, or the conductor died before its post-wave
    merge). They merge through the same merge_run(only=) op.
    """
    steps = st.get("steps") or {}
    drv = driver_alive(st)
    groups: list[dict] = []
    for _key, grp in (st.get("pending_merges") or {}).items():
        entries = []
        for n in grp.get("entries") or []:
            rec = steps.get(n) or {}
            if not rec.get("branch") or rec.get("merged") is True:
                continue
            entries.append({
                "step": n, "status": eff.get(n, rec.get("status", "?")),
                "branch": rec.get("branch"),
                "kind": rec.get("merge_kind") or "sequential",
                "verified": bool(rec.get("deferred_verified")),
                "verify_ok": rec.get("verify_ok"),
                "live": step_live(rec, drv),
                "note": rec.get("note"),
                "work_dir": rec.get("work_dir")})
        if not entries:
            continue
        repo = grp.get("repo_dir") or ""
        groups.append({
            "slug": grp.get("slug") or (Path(repo).name if repo else "?"),
            "repo": repo,
            "target": grp.get("target_branch"),
            "target_sha": grp.get("target_sha"),
            # a resolve-<step> session alive right now means the staged
            # merge is in conflict resolution - surface it on the group
            "resolving": [f"resolve-{e['step']}" for e in entries
                          if step_live(steps.get(f"resolve-{e['step']}")
                                       or {}, drv)],
            "baseline": grp.get("baseline"),
            "baseline_stuck": bool(grp.get("baseline_stuck")),
            "stage_kept": grp.get("stage_kept"),
            "moved_reason": grp.get("moved_reason"),
            # A live merge lock held by ANOTHER pid = a foreign merge is in
            # flight on this repo (our own merges surface via merge_busy).
            "busy": bool(repo) and _repo_merge_busy(Path(repo)),
            "entries": entries})
    legacy = [{
        "step": n, "status": eff.get(n, rec.get("status", "?")),
        "slug": rec.get("slug"),
        "branch": rec.get("branch"),
        "verify_ok": rec.get("verify_ok"),
        "live": step_live(rec, drv),
        "note": rec.get("note"),
        "work_dir": rec.get("work_dir")}
        for n, rec in steps.items()
        if rec.get("branch") and rec.get("merged") is not True
        and not rec.get("deferred") and not step_live(rec, drv)]
    return groups, legacy


def serve_safety(runs_dir: Path, requests_dir: Path | None = None) -> dict:
    """What the dashboard's Stop/Restart dialog reports, in two restart-
    risk tiers. HIGH - work coordinated by THIS serve process: a proposal
    generation's planner child is detached but the drain thread that
    publishes the proposal lives here, so a restart orphans it and the
    spool sweep stamps the request 'interrupted'; a request launcher's
    child also survives, but _LAUNCH_PIDS dies with this process, so a
    'launching' record past its grace window can mislabel 'launch_failed'
    until the run's state.json links it. LOW - detached work that
    outlives the dashboard: step workers and their drivers, in-flight
    merges (repo merge locks, MERGE_HEADs, merge-* claims are held by
    detached children that finish on their own and clear them), and
    serve-spawned resume/merge/summarize children tracked by pid. A
    restart loses only the live progress view - the new serve re-adopts
    state.json records and on-disk claims, so LOW items inform but never
    arm the confirm gate."""
    tasks, merges = [], []
    for sj in sorted(runs_dir.glob("*/state.json")):
        try:
            st = json.loads(sj.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        drv = driver_alive(st)
        for name, rec in (st.get("steps") or {}).items():
            if step_live(rec, drv):
                tasks.append({"run": st.get("run") or sj.parent.name,
                              "dir": sj.parent.name, "step": name,
                              "status": rec.get("status")})
    try:
        projects = load_projects()
    except Exception:
        projects = {}
    for slug, ent in projects.items():
        repo = Path(ent["path"])
        gd = _git_dir(repo)
        if gd is None:
            continue
        why = []
        try:
            info = json.loads((gd / "conductor-merge.lock")
                              .read_text(encoding="utf-8"))
        except (OSError, ValueError):
            info = {}
        pid = info.get("pid")
        if pid and pid != os.getpid() and pid_alive(pid):
            why.append(f"merge lock held by pid {pid}")
        if (gd / "MERGE_HEAD").is_file():
            why.append("merge in progress (MERGE_HEAD)")
        if why:
            merges.append({"where": slug, "dir": str(repo),
                           "why": " · ".join(why)})
    try:
        claims = sorted(_flag_dir().glob("merge-*"))
    except OSError:
        claims = []
    for f in claims:
        try:
            info = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        pid = info.get("pid")
        if pid and pid != os.getpid() and pid_alive(pid):
            merges.append({"where": "run merge", "dir": "",
                           "why": f"claim {f.name} held by pid {pid}"})
    # In-process request work dies with this process: the planner child
    # and its drain thread (a restart orphans the request mid-generation)
    # and the `run --request-id` launcher (the run may start, but the
    # record's 'launching' bookkeeping is lost).
    generating, launching = [], []
    def _req_rec(rid: str) -> dict:
        if requests_dir is None:
            return {}
        try:
            return _read_request(rid, requests_dir)
        except Exception:
            return {}
    seen_gen: set[str] = set()
    for rid, proc in list(_GEN_PROCS.items()):
        if proc is not None and proc.poll() is None:
            rec = _req_rec(rid)
            seen_gen.add(rid)
            generating.append({
                "id": rid,
                "label": str(rec.get("label") or rec.get("run") or rid)})
    # a drain in progress has no live planner proc but is exactly as
    # restart-fragile - the spool it is publishing from dies unread
    for rid in list(_DRAINING):
        if rid not in seen_gen:
            rec = _req_rec(rid)
            generating.append({
                "id": rid,
                "label": str(rec.get("label") or rec.get("run") or rid)})
    for rid, pid in list(_LAUNCH_PIDS.items()):
        rec = _req_rec(rid)
        # `since` guards Windows pid reuse, same as the launching-stage
        # check on the record itself
        if pid and pid_alive(pid, rec.get("launched_at")):
            launching.append({
                "id": rid,
                "label": str(rec.get("label") or rec.get("run") or rid)})
    # Detached serve-spawned children (resume/merge/summarize) - every
    # one survives a restart and cleans its own on-disk bookkeeping, so
    # they are reported but stay low-risk.
    jobs: list[dict] = []
    for kind, table in (("resume", _RESUME_PIDS), ("merge", _MERGE_PIDS),
                        ("summarize", _SUM_PIDS)):
        for name, pid in list(table.items()):
            if pid and pid_alive(pid):
                jobs.append({"kind": kind, "target": name, "pid": pid})
    return {"tasks": tasks, "merges": merges,
            "generating": generating, "launching": launching,
            "jobs": jobs}


def _respawn_serve(args: argparse.Namespace) -> None:
    """Spawn the detached replacement dashboard for Stop/Restart ->
    restart: same port (the child retries the bind until this process
    releases it), same runs dir, inherited env (CONDUCTOR_DEVIN etc.)."""
    argv = [sys.executable, str(Path(__file__).resolve()), "serve",
            "--port", str(args.port), "--wait-port"]
    if getattr(args, "runs_dir", None):
        argv += ["--runs-dir", str(args.runs_dir)]
    kw: dict = {"stdin": subprocess.DEVNULL,
                "stdout": subprocess.DEVNULL,
                "stderr": subprocess.DEVNULL,
                "cwd": str(Path(__file__).resolve().parent)}
    if os.name == "nt":
        kw["creationflags"] = (subprocess.CREATE_NO_WINDOW
                               | subprocess.DETACHED_PROCESS
                               | subprocess.CREATE_NEW_PROCESS_GROUP)
    else:
        kw["start_new_session"] = True
    subprocess.Popen(argv, **kw)


# ------------------------------------------------------------ tour feed api
#
# The 'What's new' endpoints (GET /api/tour + the viewed/snooze/unview
# transitions) are a thin HTTP layer over tour_state (the persisted
# watermark + entry descriptors) and tour_feeds (the deterministic card
# list). All caches live in a per-server `tour_ctx` so a second
# dashboard (tests, a worktree serve) never shares another's feed.
TOUR_FEED_TTL_S = 60.0      # ~60s feed cache: the badge polls /api/state
                            # far more often than repo changes arrive


def tour_ctx(runs_dir: Path, requests_dir: Path | None = None,
             manifests_dir: Path = MANIFESTS_DIR,
             repo_dir: Path | None = None,
             devin_argv: list[str] | None = None) -> dict:
    """The dirs the tour reads plus its caches. ``repo_dir`` is the repo
    the change feed runs `git log` on - the conductor checkout itself in
    production (the tour covers THIS tool's changes), a fixture repo in
    tests. The state file sits next to the served runs/ dir, matching
    tour_state's data/tour-state.json; the OPTIONAL LLM enrichment cache
    is the sibling tour-cache.json (tour_polish, off unless
    CONDUCTOR_TOUR_POLISH is set)."""
    return {"repo_dir": Path(repo_dir) if repo_dir is not None else ROOT,
            "runs_dir": runs_dir,
            "requests_dir": requests_dir or runs_dir.parent / "requests",
            "manifests_dir": manifests_dir,
            "state_path": runs_dir.parent / "tour-state.json",
            "devin_argv": list(devin_argv or ["devin"]),
            "feed": None,     # {"at": epoch, "payload": GET /api/tour body}
            "count": None,    # {"at": epoch, "value": int}
            "busy": False,    # a background build owns the refresh
            "lock": threading.Lock()}


def _tour_mods():
    """tour_state/tour_feeds, imported lazily: the dashboard must still
    boot when a checkout predates them - the badge then reads 'unknown'
    instead of taking /api/state down."""
    import tour_feeds                                   # noqa: E402
    import tour_state                                   # noqa: E402
    return tour_state, tour_feeds


def _tour_polisher(ctx: dict):
    """The ctx's tour_polish.Polisher, built once so its in-flight set
    coalesces this serve's attempts. None when the module is absent (an
    old checkout) or init somehow failed - the tour then simply serves
    deterministic cards forever."""
    if "polisher" not in ctx:
        try:
            import tour_polish                          # noqa: E402
            ctx["polisher"] = tour_polish.Polisher(
                cache_path=ctx["state_path"].with_name("tour-cache.json"),
                repo_dir=ctx["repo_dir"],
                devin_argv=ctx.get("devin_argv"))
        except Exception:                                # noqa: BLE001
            ctx["polisher"] = None
    return ctx["polisher"]


def _tour_build(ctx: dict, *, now: float | None = None) -> dict:
    """One feed build, cached as both the GET payload and the unseen
    count. Pure reads (tour_state.load never writes): nothing on the
    GET path can mark an entry or move the watermark. Optional LLM
    enrichment applies ONLY from the persisted tour-cache.json (never
    generated inline); an uncached range kicks one coalesced background
    attempt that invalidates the cache when it lands."""
    tour_state, tour_feeds = _tour_mods()
    ts = time.time() if now is None else now
    polisher = _tour_polisher(ctx)
    polish_on = polisher is not None and polisher.enabled()
    state = tour_state.load(ctx["state_path"])
    boundary = tour_state.capture_boundary(ctx["repo_dir"], now=now)
    feed = tour_feeds.build_feed(
        state, boundary, repo_dir=ctx["repo_dir"],
        runs_dir=ctx["runs_dir"], requests_dir=ctx["requests_dir"],
        manifests_dir=ctx["manifests_dir"],
        polish=polisher.cards_for if polish_on else None)
    payload = {"generated_at": (_iso_from(now) if now is not None
                                else now_iso()),
               "boundary": feed["boundary"], "range": feed["range"],
               "entries": feed["entries"], "history": feed["history"],
               "unseen_count": feed["unseen_count"],
               "warnings": feed["warnings"],
               "enrichment": {"enabled": polish_on}}
    if polish_on:
        rec = polisher.record(feed["range"])
        payload["enrichment"]["status"] = (
            "generating" if polisher.generating(feed["range"])
            else (rec or {}).get("status") or "pending")
        if rec is None:
            polisher.kick(feed["range"],
                          on_done=lambda _r: _tour_invalidate(ctx))
    with ctx["lock"]:
        ctx["feed"] = {"at": ts, "payload": payload}
        ctx["count"] = {"at": ts, "value": feed["unseen_count"]}
    return payload


def _tour_invalidate(ctx: dict) -> None:
    """A background polish attempt finished (ok or fallback): drop the
    cached feed so the next poll rebuilds with the outcome instead of
    waiting out the TTL."""
    with ctx["lock"]:
        ctx["feed"] = None


def tour_feed(ctx: dict, *, now: float | None = None,
              fresh: bool = False) -> dict:
    """GET /api/tour's payload: the ~60s cache while warm, one rebuild
    on expiry (or when `fresh` forces it). Read-only."""
    ts = time.time() if now is None else now
    with ctx["lock"]:
        cached = ctx["feed"]
    if not fresh and cached is not None \
            and ts - cached["at"] < TOUR_FEED_TTL_S:
        return cached["payload"]
    return _tour_build(ctx, now=now)


def tour_unseen(ctx: dict, *, now: float | None = None) -> int | None:
    """The unseen-count field /api/state carries: the LAST computed
    value (None = never computed - an honest unknown, not a fake 0).
    Never builds inline: a missing/stale feed re-arms ONE daemon rebuild
    (the _SESS_BUSY pattern) so the dashboard paint and the first state
    response never wait on git log or record scans; the next poll picks
    the count up."""
    ts = time.time() if now is None else now
    spawn = False
    with ctx["lock"]:
        stale = ctx["feed"] is None \
            or ts - ctx["feed"]["at"] >= TOUR_FEED_TTL_S
        if stale and not ctx["busy"]:
            ctx["busy"] = spawn = True
        count = ctx["count"]
    if spawn:
        def work() -> None:
            try:
                _tour_build(ctx)
            except Exception:                            # noqa: BLE001
                LOG.exception("tour feed refresh failed")
            finally:
                with ctx["lock"]:
                    ctx["busy"] = False
        try:
            threading.Thread(target=work, daemon=True).start()
        except Exception:                                # noqa: BLE001
            with ctx["lock"]:
                ctx["busy"] = False
    return count["value"] if count is not None else None


def _tour_find(payload: dict, entry_id: str) -> dict | None:
    """Entry lookup across live entries AND the re-materialized viewed
    history - every recorded descriptor lands in exactly one of them,
    so this covers every id the persisted transitions accept."""
    for e in payload["entries"] + payload["history"]:
        if e.get("id") == entry_id:
            return e
    return None


def tour_transition(ctx: dict, entry_id: str, action: str,
                    *, now: float | None = None) -> dict:
    """POST /api/tour/<id>/<action>: validate the entry against the feed
    (a cached miss retries once on a fresh build - a change that landed
    inside the TTL is still acknowledgeable), run the persisted
    transition, then rebuild so neither the feed cache nor the unseen
    count can serve pre-mutation state."""
    if action not in ("viewed", "snooze", "unview"):
        raise RequestError(f"unknown tour action: {action}", 404)
    entry_id = str(entry_id or "").strip()
    if not entry_id or len(entry_id) > 512:
        raise RequestError("invalid tour entry id")
    tour_state, _tour_feeds = _tour_mods()
    payload = tour_feed(ctx, now=now)
    entry = _tour_find(payload, entry_id)
    if entry is None:
        payload = tour_feed(ctx, now=now, fresh=True)
        entry = _tour_find(payload, entry_id)
    if entry is None:
        raise RequestError(f"unknown tour entry: {entry_id}", 404)
    if action == "viewed":
        # ids still 'new' after this mark - only an empty list lets the
        # watermark advance, and only to THIS feed's captured boundary
        still_new = [e["id"] for e in payload["entries"]
                     if e.get("state") == "new" and e["id"] != entry_id]
        result = tour_state.mark_viewed(
            entry_id, entry.get("range"), payload["boundary"],
            remaining_new=still_new, path=ctx["state_path"],
            repo_dir=ctx["repo_dir"], now=now)
    elif action == "snooze":
        result = tour_state.snooze(entry_id, entry.get("range"),
                                   path=ctx["state_path"], now=now)
    else:
        result = tour_state.unview(entry_id, path=ctx["state_path"],
                                   now=now)
    with ctx["lock"]:
        ctx["feed"] = ctx["count"] = None    # drop pre-mutation copies
    refreshed = _tour_build(ctx, now=now)
    result["unseen_count"] = refreshed["unseen_count"]
    return result


def collect_dashboard(runs_dir: Path,
                      devin_argv: list[str] | None = None,
                      requests_dir: Path | None = None,
                      include_archived: bool = False,
                      include_trash: bool = False) -> dict:
    """Show executed run states alongside saved, not-yet-executed requests.
    A launched request uses state.json for its rows, not its proposal
    preview. Runs stamped state['archived'] (auto-archive, or the modal's
    Archive button) are omitted unless include_archived - they stay on
    disk either way, and the 'archived_runs' count always reflects them.
    Trashed runs/requests never appear here (their files moved to
    data/trash); trash_count/trash_ids feed the view chip and the Undo
    bar's liveness check, and include_trash adds the full entry list."""
    requests_dir = requests_dir or runs_dir.parent / "requests"
    _auto_archive(runs_dir)
    _auto_drain()
    _auto_followups(runs_dir, requests_dir)
    sess_by_dir = live_sessions_by_dir(runs_dir, devin_argv or ["devin"])
    projects = load_projects()
    refresh_prices(runs_dir)
    prices = price_table(runs_dir)
    requests: dict[str, dict] = {}
    trash_ids: list[str] = []
    for rf in requests_dir.glob("*.json"):
        try:
            rec = json.loads(rf.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(rec, dict) or not REQUEST_ID_RE.fullmatch(
                str(rec.get("id") or "")):
            continue
        if rec.get("status") == "discarded":
            trash_ids.append(f"legacy-{rec['id']}")   # pre-trash tombstone
            continue
        # a parked planner overlay is more current than the file
        requests[rec["id"]] = _spool_rec(rec["id"]) or rec
    trash_dir = _trash_root(runs_dir)
    try:
        for td in trash_dir.iterdir():
            if td.is_dir() and TRASH_ID_RE.fullmatch(td.name) \
                    and (td / "meta.json").is_file():
                trash_ids.append(td.name)
    except OSError:
        pass
    launched: set[str] = set()
    emitted_prep: set[str] = set()   # plan_sessions rows emitted once
                                     # per request (dup run dirs share
                                     # the same request:<id> agentKey)
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
        # 'executing' is the honest "the run is mid-flight" signal the
        # merge guard applies: a live driver (covers the between-wave /
        # verify / shutdown windows where no step reads 'running') or a
        # step record still backed by something alive, plus the brief
        # in-process resume-launch window.
        drv = driver_alive(st)
        executing = drv or pid_alive(_RESUME_PIDS.get(sj.parent.name)) \
            or any(step_live(r, drv)
                   for r in (st.get("steps") or {}).values())
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
            # task_display keeps a leading purpose marker out of the
            # human-facing text in both cases.
            tfull = task_label(rec, full=True)
            if tfull == rec.get("task"):
                tfull = task_display(mbodies.get(name)) or tfull
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
            # Review mark: live sentinel overlay when present (newer `at`
            # wins), else the mark folded into state.json. A LIVE step
            # suppresses the flag rather than dropping the seq: a
            # re-dispatch supersedes the old completion until the next
            # terminal write stamps a new seq, and popping would let an
            # old ack collide with the recycled value.
            rmark = _review_mark(sj.parent, rec.get("review"),
                                 rec.get("name") or name)
            alive = step_live(rec, drv)
            agents.append({
                "run": st.get("run"), "run_dir": st.get("run_dir") or str(sj.parent),
                "run_key": key, "request_id": st.get("request_id"),
                "step": rec.get("name") or name, "slug": slug,
                "seq": seq_of.get(rec.get("name") or name,
                                  prop_seq.get(rec.get("name") or name)),
                "work_dir": wd,
                "status": status, "started_at": rec.get("started_at"),
                "first_started_at": rec.get("first_started_at"),
                "ended_at": rec.get("ended_at"),
                # closed-intervals cumulative; the client adds the live
                # interval (now - started_at) while status is 'running'
                "elapsed_s": rec.get("elapsed_s"),
                "alive": alive,
                "pid": rec.get("pid"), "session_id": rec.get("session_id"),
                "model": model,
                "model_actual": rec.get("model_actual"),
                "mode": rec.get("mode") or plan.get("mode"),
                "effort": effort,
                "task": (task_display(task_label(rec) or plan.get("task"))
                         or plan.get("task")),
                "task_full": tfull or task_display(plan.get("task")),
                # Machine-readable resolver metadata stays machine-
                # readable even though `task`/`task_full` now carry
                # display text; old records still hold the marker in
                # `task`, so it is parsed back out here.
                "purpose": rec.get("purpose") or
                           split_purpose(rec.get("task"))[0],
                # The edit control exists only while the step never
                # dispatched (queued/skipped records are pre-spawn);
                # anything that ran takes resume notes instead - the
                # original prompt is left untouched on purpose.
                "depends_on": rec.get("deps"),
                "dep_wait": rec.get("dep_wait"),
                "prompt_editable": rec.get("status") in
                ("queued", "skipped"),
                # re-runnable outcomes take 'resume notes'; still-waiting
                # steps (queued/skipped pre-spawn) take the same channel
                # as 'additional instructions' for their first dispatch
                "notes_enabled": status in
                ("blocked", "partial", "failed", "timed_out", "stopped",
                 "lost", "queued", "skipped"),
                "prompt_override": _flag_text(
                    prompt_flag(sj.parent, rec.get("name") or name)),
                "resume_notes": _flag_text(
                    rnote_flag(sj.parent, rec.get("name") or name)),
                "prompt_edited": bool(rec.get("prompt_edited")),
                # 'Interrupt + instruct': a pending request sentinel on a
                # live step (the driver may be mid-kill), plus how many
                # respawns this step has already done.
                "intr_pending": intrq_flag(
                    sj.parent, rec.get("name") or name).is_file(),
                "interrupts": rec.get("interrupts"),
                "cloud": rec.get("cloud", plan.get("cloud")),
                "device": rec.get("device"),
                "device_wait": rec.get("device_wait"),
                "slot_wait": rec.get("slot_wait"),
                "slot_waited_s": rec.get("slot_waited_s"),
                "merge_queue": rec.get("merge_queue"),
                "verify_ok": rec.get("verify_ok"),
                "skipped_by_user": _safe_name(
                    rec.get("name") or name) in flagged,
                "note": rec.get("note"), "next": rec.get("next"),
                # rec['outcome'] is the step's own newest report;
                # rec['outcomes'] keeps the earlier attempts it replaced
                "outcome": rec.get("outcome"),
                "outcomes": rec.get("outcomes"),
                "completion_seq": rec.get("completion_seq"),
                "review": rmark,
                "needs_review": (not alive) and _needs_review(
                    rec.get("completion_seq"), rmark),
                "session_gone": session_gone,
                "exit_code": rec.get("exit_code"),
                "status_line": rec.get("status_line"),
                # The worker's STATUS line can be in the log while the
                # record still reads 'running' (post-commit, the verify
                # gate, a pool-merge wait). rec['reported'] is the
                # driver's stamp; the log read is the same truth for
                # drivers that predate the field and for orphaned runs.
                "reported": rec.get("reported") or (
                    tail_status(live_step_log(rec))
                    if status == "running" else None),
                "service_port": rec.get("service_port"),
                "service_url": rec.get("service_url"),
                "branch": rec.get("branch"), "merged": rec.get("merged"),
                "merged_at": rec.get("merged_at"),
                "merge_target": rec.get("merge_target"),
                "deferred": bool(rec.get("deferred")),
                "merge_kind": rec.get("merge_kind"),
                "target_branch": rec.get("target_branch"),
                "deferred_verified": rec.get("deferred_verified"),
                "timeout_min": timeout_min,
                "timeout_ext_s": rec.get("timeout_extended_s"),
                "attempts": rec.get("attempts"),
                "max_attempts": rec.get("max_attempts"),
                "tokens": rec.get("tokens"), "cost": cost,
                # wt = configured isolation, not just a dispatched branch:
                # rec['wt'] is stamped at queue/dispatch time, so a
                # worktree step shows 'wt' before its session spawns.
                "wt": bool(rec.get("wt") or rec.get("branch")
                           or plan.get("wt")),
                "stdout_log": rec.get("stdout_log"),
                "tail": stdout_tail(rec.get("stdout_log")),
                "log_mtime": file_mtime_iso(rec.get("stdout_log")),
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
            # full text and the form's prefill. Kept literal on purpose:
            # it is what will dispatch, and an embedded purpose marker
            # must survive a save round-trip.
            row["task_full"] = mbodies.get(row["step"]) or row.get("task")
            row["prompt_editable"] = True
            # never-dispatched: 'additional instructions' appended to the
            # first prompt via the same rnote channel resume notes use
            row["notes_enabled"] = True
            row["prompt_override"] = _flag_text(
                prompt_flag(sj.parent, row["step"]))
            row["resume_notes"] = _flag_text(
                rnote_flag(sj.parent, row["step"]))
            row["prompt_edited"] = row["prompt_override"] is not None
            row["seq"] = seq_of.get(row["step"], prop_seq.get(row["step"]))
            counts[row["status"]] = counts.get(row["status"], 0) + 1
            agents.append(row)
        # Preparation history rides the launched run's task list under
        # the same request:<id> run_key - the session that wrote this
        # plan, kept as step 0. Deliberately NOT in counts: it is not a
        # run step, and a 'stopped' planner row must never read as
        # resumable work.
        if request and request["id"] not in emitted_prep:
            emitted_prep.add(request["id"])
            agents.extend(plan_attempt_agents(
                request, key, generating=False,
                log_path=requests_dir / f"{request['id']}.gen.log",
                prices=prices))
        # The detached summarizer session rides the same task list as an
        # aux row - bookkeeping, not a pipeline step (seq=None keeps it
        # out of prev/next; the client hides it until the run is
        # filtered into).
        agents.extend(summary_agent(st, key, sj.parent, prices))
        # Run-level review mark, same sentinel/folded layering as the
        # steps'. A tombstone shows at once; a seq mismatch only reads as
        # 'new completion to review' once the run actually ended (a
        # resumed run keeps its old seq but pops ended_at - flagging it
        # mid-run would resurrect an ack the in-flight work superseded).
        run_mark = _review_mark(sj.parent, st.get("review"))
        run_needs = bool(
            (isinstance(run_mark, dict)
             and run_mark.get("reviewed") is False)
            or (st.get("ended_at")
                and _needs_review(st.get("completion_seq"), run_mark)))
        # closed-intervals driver cumulative; exec_start carries the
        # live interval's start so the runs table ticks base+now-start
        # without a payload refresh
        run_els, run_exec_start = _run_elapsed_split(st, sj.parent)
        pm_groups, pm_legacy = _pending_merge_view(st, eff)
        runs.append({"run": st.get("run"), "dir": str(sj.parent),
                     "key": key, "request_id": st.get("request_id"),
                     "archived": st.get("archived"),
                     "followup": st.get("followup"),
                     # A run launched FROM a follow-up proposal carries
                     # the same back-link the proposal row does, so the
                     # run modal and its tasks can jump to the source run
                     "followup_src": (
                         {"run": request.get("source_run"),
                          "key": request.get("source_run_key") or
                                 request.get("source_run"),
                          "manifest": request.get("source_manifest")}
                         if request and (request.get("source_run")
                                         or request.get("source_manifest"))
                         else None),
                     "executing": executing,
                     "iso": _run_isolation(cfg, st, request),
                     # Retained-but-unlanded worktree work for the run
                     # modal's selective merge: deferred groups carry
                     # recorded targets + alternatives; legacy rows are
                     # auto-policy branches that never landed.
                     "pending_merges": pm_groups,
                     "pending_branches": pm_legacy,
                     "merge_busy": sj.parent.name in _MERGE_BUSY
                     or pid_alive(_MERGE_PIDS.get(sj.parent.name))
                     or _merge_claim_held(sj.parent) is not None,
                     "description": st.get("description") or
                     (request or {}).get("description"),
                     "started_at": st.get("started_at"),
                     "ended_at": st.get("ended_at"), "counts": counts,
                     "aborted": st.get("aborted"),
                     "elapsed_s": run_els, "exec_start": run_exec_start,
                     "resume_log": (str(sj.parent / "resume.launch.log")
                                    if (sj.parent /
                                        "resume.launch.log").is_file()
                                    else None),
                     "has_summary": (sj.parent / "run.summary.md")
                                    .is_file(),
                     "summarizing": _summarizing(sj.parent),
                     "completion_seq": st.get("completion_seq"),
                     "review": run_mark, "needs_review": run_needs,
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
        stage = _request_status(rec, runs_dir, launched)
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
                     # The owned TOML (when published) is the config of
                     # record - it reflects hand edits; proposed_steps is
                     # its publish-time snapshot fallback.
                     "iso": _run_isolation(
                         _manifest_cached(rec.get("manifest") or ""),
                         {}, rec),
                     "description": rec.get("description"),
                     "followup_src": ({"run": rec.get("source_run"),
                                      "key": rec.get("source_run_key") or
                                      (f"request:{rec['source_request']}"
                                       if rec.get("source_request")
                                       else rec.get("source_run")),
                                      "step": rec.get("source_step"),
                                      "manifest": rec.get("source_manifest")}
                                     if rec.get("source_run")
                                     or rec.get("source_manifest")
                                     else None),
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
                     "stale": bool((pend := _pending_actions(rec))["gen"]
                                   or pend["opts"]),
                     "pending": pend, "gen_last": gen_last,
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
                "task_full": ("Propose a TOML runbook for: "
                              + (rec.get("raw") or "").strip()
                              if (rec.get("raw") or "").strip() else None),
                "purpose": None,
                "note": None, "next": None, "exit_code": None,
                "outcome": None, "outcomes": None,
                "branch": None, "merged": None, "tokens": None, "wt": False,
                "deferred": False, "merge_kind": None,
                "target_branch": None, "deferred_verified": None,
                "cost": None,
                "stdout_log": str(log_p) if log_p.is_file() else None,
                "tail": gen_tail, "export": None, "prompt": None})
        pbodies = _manifest_bodies(rec)
        for i, s in enumerate(steps):
            a = proposal_agent(s, rec, projects, key, "planned")
            a["task_full"] = (task_display(pbodies.get(s["step"]))
                              or a.get("task"))
            a["prompt_editable"] = False      # edited via the TOML review
            a["notes_enabled"] = False
            a["seq"] = i               # proposal order is its pipeline
            agents.append(a)
        # Step 0: completed planner sessions stay on the plan's task list
        # after preparation ends (and after reload - they are rebuilt from
        # the request record each poll). While a new pass generates, the
        # live 'plan-proposal' row above holds the bare name and every
        # completed attempt is numbered instead.
        agents.extend(plan_attempt_agents(
            rec, key, generating=stage == "generating",
            log_path=log_p, prices=prices))
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
            "slot_pool": _slots_view(),
            "archived_runs": archived_runs,
            "trash_count": len(trash_ids), "trash_ids": trash_ids,
            "trash": (list_trash(trash_dir, requests_dir)
                      if include_trash else None),
            "project_options": [{"slug": slug, "cloud_ok": p["cloud_ok"]}
                                for slug, p in sorted(projects.items())],
            "pricing": {"tier": "self-serve", "models": len(prices),
                        "fetched": (dt.datetime.fromtimestamp(
                            _PRICE_CACHE["fetched"]).isoformat(
                                timespec="seconds")
                            if _PRICE_CACHE["fetched"] else None),
                        "source": _PRICE_CACHE["source"]},
            "defaults": {**dict(zip(("model", "effort"),
                                    default_model_effort())),
                         # isolation + the resolver are the sane default:
                         # steps merge back cleanly and conflicts resolve
                         # themselves; unchecking is the deliberate choice
                         "worktrees": True, "merge_resolver": True,
                         "parallel": False},
            "model_options": model_options(devin_argv or ["devin"]),
            "auth": auth_status(devin_argv or ["devin"])}


_DASH_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>agent-conductor</title>
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'%3E%3Crect width='64' height='64' rx='14' fill='%2313161f'/%3E%3Cline x1='13' y1='19' x2='49' y2='7' stroke='%23e6edf3' stroke-width='2.6' stroke-linecap='round'/%3E%3Ccircle cx='13' cy='19' r='3' fill='%23e6edf3'/%3E%3Ccircle cx='12' cy='37' r='3' fill='%2358a6ff'/%3E%3Crect x='8.5' y='41' width='7' height='11' rx='2' fill='%2358a6ff'/%3E%3Ccircle cx='23.5' cy='31' r='3' fill='%23bc8cff'/%3E%3Crect x='20' y='35' width='7' height='17' rx='2' fill='%23bc8cff'/%3E%3Ccircle cx='35' cy='26' r='3' fill='%23d29922'/%3E%3Crect x='31.5' y='30' width='7' height='22' rx='2' fill='%23d29922'/%3E%3Ccircle cx='46.5' cy='34' r='3' fill='%233fb950'/%3E%3Crect x='43' y='38' width='7' height='14' rx='2' fill='%233fb950'/%3E%3C/svg%3E">
<link rel="stylesheet" href="/api/assistant/static/chat.css">
<script src="/api/assistant/static/md.js"></script>
<script src="/api/assistant/static/chat.js" defer></script>
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
/* live machine-pool control: -/+ the concurrent-agent cap, value shows
   leased slots / cap and goes amber while the pool is full */
.poolbox { display:inline-flex; align-items:center; border:1px solid #30363d;
  border-radius:7px; overflow:hidden; font-size:12px; }
.poolbox button { background:transparent; border:0; color:#8b949e;
  font:inherit; padding:5px 8px; cursor:pointer; }
.poolbox button:hover:not(:disabled) { color:#fff; background:#1f6feb; }
.poolbox button:disabled { opacity:.35; cursor:default; }
.poolbox #pool-val { padding:5px 9px; color:#e6edf3; min-width:64px;
  text-align:center; border-left:1px solid #30363d;
  border-right:1px solid #30363d; }
.poolbox.full #pool-val { color:#d29922; }
/* task/run history feed (driver events.jsonl + dashboard action log) */
.evlog { margin:6px 0 10px; max-height:220px; overflow:auto;
  border:1px solid #21262d; border-radius:7px; }
.evrow { display:grid; grid-template-columns:70px 30px 1fr; gap:8px;
  padding:4px 10px; border-bottom:1px solid #161b22; font-size:12px;
  align-items:baseline; }
.evrow:last-child { border-bottom:0; }
.evtime { color:#8b949e; white-space:nowrap; font-size:11px; }
.evtag { font-size:9px; padding:1px 5px; border-radius:8px;
  background:#21262d; color:#8b949e; text-transform:uppercase;
  text-align:center; }
.evtag.ui { background:#1c3041; color:#79b8ff; }
.evtxt { color:#c9d1d9; overflow:hidden; text-overflow:ellipsis; }
.evtxt .evd { color:#8b949e; }
.evempty { padding:12px; color:#6e7681; font-size:12px;
  text-align:center; }
table { border-collapse:collapse; width:100%; table-layout:fixed; }
th { position:sticky; top:var(--topbar-h,110px); z-index:7;
  background:#0d1117; text-align:left; font-weight:600; color:#8b949e;
  font-size:11px; text-transform:uppercase; padding:4px 10px 4px 0;
  border-bottom:1px solid #21262d; }
td { padding:6px 10px 6px 0; border-bottom:1px solid #161b22; vertical-align:top; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
/* a left gutter inside every first cell: the left-edge state markers
   (unreviewed cyan, selected/dsel blue) paint an inset stripe there, so
   the stripe sits in the gutter instead of overlapping the cell text */
.wrapw td:first-child, .wrapw th:first-child { padding-left:8px; }
/* the agents table's run name: extra right padding keeps a long,
   ellipsized name from visually colliding with the model column */
td.rcell { padding-right:18px; }
/* td's text-overflow only ellipsizes the cell's own text - a clipped
   link button showed a hard mid-word cut right against the next
   column. Letting the button ellipsize itself gives a real '...' plus
   the rcell gutter, so a long run name degrades gracefully. */
.run-link { display:inline-block; max-width:100%; overflow:hidden;
  text-overflow:ellipsis; white-space:nowrap; vertical-align:bottom; }
td.wrap { white-space:normal; color:#8b949e; font-size:12px; }
td.step { white-space:normal; }  /* wt/merged chips wrap under a long name */
/* tr.agen is not clickable as a row anymore - its named controls (step
   name -> details, run name -> filter) carry pointer cursors themselves,
   so the row does not advertise a click that does nothing. */
tr.runrow { cursor:pointer; }
tr.runrow:hover td { background:#26405a; }
tr.runrow.selected td { background:#1c3041; }
tr.runrow.selected td:first-child { box-shadow:inset 3px 0 #58a6ff; }
.dots span { animation:blink 1.2s infinite; color:#58a6ff; }
.dots span:nth-child(2) { animation-delay:.4s; }
.dots span:nth-child(3) { animation-delay:.8s; }
@keyframes blink { 0%,100%{opacity:.15} 50%{opacity:1} }
tr.agen td { transition:background .12s; }
tr.agen:hover td { background:#26405a; }
/* .hl is the hover's run group: the hovered row's siblings plus a linked
   follow-up pair. It stays a notch under the directly hovered row -
   hover beats .hl, and a row that is BOTH gets the strongest shade. */
tr.hl td { background:#1c2833; }
tr.hl:hover td { background:#2a4a68; }
/* .dsel marks the row whose details are on screen - the open task modal's
   agent row, an open run/request/questionnaire/summary modal's run row. It
   is independent of the pointer (hover no longer erases it) and distinct
   from .selected (the run filter) and .hl (run-group hover): a filtered
   run row keeps .selected while a task modal marks a task row .dsel. */
tr.dsel td { background:#1c3041; }
tr.dsel td:first-child { box-shadow:inset 3px 0 #58a6ff; }
tr.agen.dsel:hover td, tr.runrow.dsel:hover td { background:#2a4a68; }
.gone { opacity:.45; text-decoration:line-through; }
td.res { color:#8b949e; font-size:12px; }
td.res.fresh { animation:fresh 1.2s ease-out; }
@keyframes fresh { 0% { background:rgba(88,166,255,.25); } 100% { background:transparent; } }
.chip { display:inline-block; border:1px solid #30363d; border-radius:10px; padding:1px 9px; margin:0 5px 5px 0; font-size:12px; cursor:pointer; user-select:none; }
.chip.off { opacity:.35; }
.chip.active { background:#1c3041; border-color:#58a6ff; }
button.chip { background:transparent; font:inherit; color:inherit; }
button.chip.active { background:#1c3041; }
button.chip.busy { position:relative; padding-right:22px; }
button.chip.busy::after { content:""; position:absolute; right:6px;
  top:50%; width:8px; height:8px; margin-top:-5px; border:2px solid #58a6ff;
  border-top-color:transparent; border-radius:50%;
  animation:chipspin .7s linear infinite; }
@keyframes chipspin { to { transform:rotate(360deg) } }
body.viewload #runs, body.viewload #agents, body.viewload #trashwrap
  { opacity:.5; transition:opacity .15s; }
button.chip:focus-visible, button:focus-visible, tr.runrow:focus-visible, tr.agen:focus-visible { outline:2px solid #58a6ff; outline-offset:2px; }
.filter-group { display:inline-block; margin:0 14px 5px 0; }
.filter-label { color:#8b949e; margin-right:6px; }
.dot { display:inline-block; width:9px; height:9px; border-radius:50%; margin-right:6px; }
.dot.run { animation:pulse 1.2s ease-in-out infinite; }
@keyframes pulse { 50% { opacity:.35; } }
.wrapw { }  /* overflow here would create a scroll container and break
   the sticky th - tables are width:100% fixed, nothing overflows */
/* compact density: wrap cells ellipsize to fewer lines, sub-lines
   (dir path, planner output, other second-line detail) hide */
body.compact td { padding-top:2px; padding-bottom:2px; }
body.compact td.wrap, body.compact td.res {
  white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
body.compact td .sub { display:none; }
body.compact td br { display:none; }
/* .toksub carries the labeled planner:/steps:/est token lines in a
   run's steps cell - compact density hides them entirely (the numbers
   stay reachable at full density and in the run modal). .rlabels is a
   plain block box keeping the run row's chips on their own line under
   the name in both densities (a <br> would disappear in compact and
   let them clip inline after the name); its one rule is below. */
.toksub { color:#8b949e; font-size:12px; }
body.compact td .toksub { display:none; }
/* .toks is .toksub's look without the compact hide - the agents table's
   tokens column keeps its labeled numbers at both densities */
.toks { color:#8b949e; font-size:12px; }
/* Chips on the .rlabels line must never clip invisibly at the td edge:
   compact squeezes td.wrap to nowrap, which pins every chip to one line -
   a pill starting mid-line can shrink to max-width:100% yet still
   overflow past the cell. Normal whitespace lets a following chip wrap
   to its own line inside the label block instead (harmless at full
   density, where td.wrap already wraps). */
.rlabels { white-space:normal; }
/* .genline: a proposal row's compact live-output line - display:none at
   full density (the .sub line under the stage chip shows the same
   gen_last there). Under compact it is one nowrap ellipsized line: as an
   inline-block with max-width it keeps its own line when the .rlabels
   wrap puts it after a chip, and ellipsizes at the cell edge rather
   than clipping. .fresh flashes it on new output - the same animation
   the agent result cells use, on the .sub line too. */
.genline { display:none; }
body.compact td .genline { display:inline-block; max-width:100%;
  overflow:hidden; text-overflow:ellipsis; white-space:nowrap;
  vertical-align:bottom; color:#8b949e; font-size:12px; margin-left:6px; }
.genline.fresh, td .sub.fresh { animation:fresh 1.2s ease-out; }
.counts span, .counts .rchip { background:#161b22; border-radius:8px; padding:1px 8px; margin-right:5px; font-size:12px; }
.counts .rchip { border:0; font-family:inherit; cursor:pointer; }
.counts .rchip:hover { background:#1c3041; }
a { color:#58a6ff; text-decoration:none; }
[hidden] { display:none !important; }
#backdrop { position:fixed; inset:0; background:rgba(0,0,0,.55); z-index:50; }
/* themed confirm/alert + power dialogs - native confirm()/alert()
   chrome replaced so every prompt matches the dashboard palette */
#askwrap, #powerwrap { position:fixed; inset:0; background:rgba(0,0,0,.6); z-index:70; display:flex; align-items:center; justify-content:center; }
#askbox, #powerbox { background:#161b22; border:1px solid #30363d; border-radius:10px; padding:16px 20px; max-width:460px; width:min(460px,90vw); font-size:13px; box-shadow:0 12px 36px rgba(0,0,0,.5); }
#asktitle { font-weight:600; margin-bottom:8px; }
#askmsg { color:#c9d1d9; margin-bottom:14px; white-space:pre-wrap; }
#askbtns { display:flex; gap:8px; justify-content:flex-end; }
#powerbox h3 { margin:0 0 10px; font-size:14px; }
#powerbox ul { margin:6px 0; padding-left:18px; }
/* footer bar: the separator lifts the controls off the report text,
   dismissive Cancel sits alone at the left, the disruptive pair
   (Stop before Restart, primary rightmost) groups at the right */
#powerbox .actions { display:flex; gap:10px; align-items:center; margin-top:16px; padding-top:14px; border-top:1px solid #21262d; }
#powerbox .actions .btn { min-width:88px; padding:7px 14px; }
#powerbox .actions #pw-cancel { margin-right:auto; }
#powerbox .actions .btn.danger:hover:not(:disabled) { border-color:#f85149; }
#pwstat { margin-top:10px; min-height:15px; }
/* 'What's new' tour - a Driver.js-style spotlight with no library.
   #tourring's oversized box-shadow IS the page dim: its transparent
   center lets the anchor show through lit while everything else darkens
   (a ring stacked under a plain dim layer could never cut the hole).
   A card with no usable anchor gets the wrap's own dim + centering.
   z 60 sits above the details modal (50/51) but below the themed
   confirm/power dialogs (70); while a modal is open .suspended drops
   the whole layer so the dialog owns the screen, and closing it resumes
   the same card. */
#tourwrap { position:fixed; inset:0; z-index:60; }
#tourwrap.dim { background:rgba(0,0,0,.55); }
#tourwrap.suspended { display:none; }
#tourring { position:fixed; border:2px solid #58a6ff; border-radius:10px;
  box-shadow:0 0 0 9999px rgba(0,0,0,.55); pointer-events:none;
  box-sizing:border-box; }
#tourring::after { content:''; position:absolute; inset:-4px;
  border:2px solid #58a6ff; border-radius:12px;
  animation:tourpulse 1.6s ease-out infinite; }
@keyframes tourpulse { 0% { transform:scale(1); opacity:.8; }
  100% { transform:scale(1.22); opacity:0; } }
@media (prefers-reduced-motion: reduce) {
  #tourring::after { animation:none; } }
#tourcard { position:fixed; left:50%; top:50%; transform:translate(-50%,-50%);
  width:min(440px,92vw); max-height:82vh; overflow:auto;
  background:#161b22; border:1px solid #30363d; border-radius:10px;
  padding:16px 18px; font-size:13px; box-sizing:border-box;
  box-shadow:0 12px 36px rgba(0,0,0,.5); }
#tourcard.anchored { transform:none; }
#tourcard .tpos { color:#8b949e; font-size:11px; text-transform:uppercase;
  letter-spacing:.06em; }
#tourcard h3 { margin:4px 40px 8px 0; font-size:14px; }
#tourcard .ttag { display:inline-block; border:1px solid; border-radius:9px;
  padding:0 8px; font-size:11px; }
#tourcard .tblurb { color:#c9d1d9; margin:9px 0; white-space:pre-wrap;
  overflow-wrap:anywhere; }
#tourcard .tsec { color:#8b949e; font-size:11px; font-weight:600;
  text-transform:uppercase; letter-spacing:.05em; margin:10px 0 3px; }
#tourcard .tlist { margin:2px 0 0; padding-left:18px; color:#8b949e;
  font-size:12px; max-height:120px; overflow:auto; }
#tourcard .tlist code { background:#0d1117; border-radius:4px;
  padding:0 4px; margin-right:2px; }
#tourcard .trel .chip { margin:0 6px 4px 0; }
#tourcard .trel .chip:hover { border-color:#58a6ff; }
#tourcard .tactions { display:flex; gap:8px; align-items:center;
  margin-top:14px; padding-top:12px; border-top:1px solid #21262d; }
#tourcard .tactions .tgap { flex:1; }
#tourcard .tourhist { margin-top:12px; }
#tourcard .tourhist > summary { color:#8b949e; font-size:12px;
  cursor:pointer; }
#tourcard .thist { display:block; width:100%; text-align:left;
  background:none; border:0; border-top:1px solid #161b22; color:#e6edf3;
  font:inherit; font-size:12px; padding:5px 2px; cursor:pointer; }
#tourcard .thist:hover { color:#58a6ff; }
#tourcard .thist .sub { float:right; font-size:11px; }
#tour-btn .tbadge { display:inline-block; margin-left:7px; padding:0 6px;
  border-radius:9px; background:#1f6feb; color:#fff; font-size:11px;
  line-height:15px; vertical-align:1px; }
#tour-btn .tbadge::before { content:''; display:inline-block; width:6px;
  height:6px; border-radius:50%; background:#7ee787; margin-right:4px;
  vertical-align:1px; }
#detail { position:fixed; left:50%; top:50%; transform:translate(-50%,-50%); width:min(780px,92vw); max-height:82vh; background:#161b22; border:1px solid #30363d; border-radius:10px; padding:18px 22px; overflow:auto; z-index:51; font-size:13px; box-sizing:border-box; }
#detail h3 { margin:0 170px 10px 0; font-size:14px; }
#detail .kv { display:grid; grid-template-columns:96px 1fr; gap:4px 12px; }
#detail .k { color:#8b949e; }
#detail pre { white-space:pre-wrap; color:#8b949e; background:#0d1117; border-radius:6px; padding:8px 10px; font-size:12px; max-height:180px; overflow:auto; }
/* fixed, not absolute: #detail's translate() transform makes it the
   containing block, so the Close button stays pinned to the
   modal's top-right instead of scrolling away with #dbody */
#detail .x { position:fixed; top:10px; right:16px; z-index:8;
  cursor:pointer; color:#8b949e; background:none; border:0;
  font:inherit; }
/* prev/next task bar - sticky like .topbar: top:0 plus margins that undo
   #detail's padding so it pins flush over the scroll gap. The right pad
   leaves room for the Close X which overlays the bar. */
/* margin-bottom buys real clearance: a sticky first-child with the
   -18px top pull leaves the next line tucked ~6px under the bar's
   bottom edge in Chrome, so the nominal gap must exceed that drift */
#detail .dnav { position:sticky; top:0; z-index:6; display:flex; align-items:center; gap:10px; margin:-18px -22px 26px; padding:7px 170px 7px 22px; background:#161b22; border-bottom:1px solid #30363d; border-radius:9px 9px 0 0; }
#detail .dnav-btn { display:inline-flex; align-items:center; justify-content:center; gap:6px; min-width:38px; max-width:40%; background:#21262d; border:1px solid #30363d; color:#e6edf3; border-radius:6px; padding:4px 10px; font:inherit; font-size:12px; cursor:pointer; }
#detail .dnav-btn.nav { min-width:130px; }
#detail .dnav-btn span { overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
#detail .dnav-btn:hover:not(:disabled) { border-color:#58a6ff; }
#detail .dnav-btn:disabled { opacity:.4; cursor:default; }
/* three zones - Back pins left, the pipeline arrows pin right, and the
   counter centers on the BAR, not the leftover space, so it never
   shifts when sibling names differ in length */
#detail .dnav-l { display:flex; align-items:center; gap:10px; flex:1; min-width:0; }
#detail .dnav-r { display:flex; align-items:center; gap:6px; flex:1; justify-content:flex-end; min-width:0; }
#detail .dnav-pos { position:absolute; left:50%; transform:translateX(-50%); color:#8b949e; font-size:12px; white-space:nowrap; }
/* scroll-anchored repaints and focus jumps must land content below the
   sticky bar, never tucked under it */
#detail .dnav ~ * { scroll-margin-top:48px; }
/* status color coding shared by the task modal's kv row and the
   summary docs' **status:** bullets (_summary_body injects .stc too) */
.stc.st-ok { color:#3fb950; font-weight:600; }
.stc.st-bad { color:#f85149; font-weight:600; }
.stc.st-warn { color:#d29922; font-weight:600; }
.stc.st-run { color:#58a6ff; font-weight:600; }
.stc.st-other { color:#8b949e; }
/* the Back button sits in the same sticky .dnav bar as prev/next (or
   alone in a bar of its own on modals without pipeline nav) */
#detail .dnav-back { flex:none; }
/* the summary modal embeds _summary_body's fragment (?frag=1) - the same
   escaped markup the standalone page wraps, themed to the dialog */
#detail .sumdoc { color:#c9d1d9; }
#detail .sumdoc h1 { font-size:15px; font-weight:600; color:#e6edf3; border-bottom:1px solid #21262d; padding-bottom:5px; margin:2px 170px 10px 0; }
#detail .sumdoc h2 { font-size:13px; font-weight:600; color:#79b8ff; margin:14px 0 5px; text-transform:none; }
#detail .sumdoc h3, #detail .sumdoc h4 { font-size:12px; font-weight:600; color:#8b949e; margin:12px 0 4px; }
/* an LLM-written run.summary.md gets accent headings so it reads as a
   generated report, not as more conductor chrome */
#detail .sumdoc .llmsum h1, #detail .sumdoc .llmsum h2,
#detail .sumdoc .llmsum h3, #detail .sumdoc .llmsum h4 { color:#ffa657; }
/* a run summary's '<step> - <status>' headings are live: the step name
   opens the task modal, the status word takes the .stc color, a 'mark
   as reviewed' link toggles the flag, and a reviewed step's heading
   strikes through */
#detail .sumdoc .sumh-rev { font-size:11px; font-weight:400; }
#detail .sumdoc .sumh-reviewed .sumh-text { text-decoration:line-through; opacity:.65; }
#detail .sumdoc ul { padding-left:20px; margin:6px 0; }
#detail .sumdoc li { margin:3px 0; }
#detail .sumdoc p { margin:8px 0; }
#detail .sumdoc code { background:#0d1117; border-radius:4px; padding:1px 5px; font-size:12px; }
#detail .sumdoc strong { color:#e6edf3; }
/* the summary modal's own title - quiet, like the doc header under it */
#detail h3.sumtitle { font-size:13px; font-weight:500; color:#8b949e; margin:0 170px 4px 0; }
/* the task prompt box: a fixed visual slot in modal and summary alike,
   so a one-line task doesn't collapse the layout */
#detail pre.taskpre, #detail .sumdoc pre.taskpre { min-height:110px; }
/* bookkeeping rows (the detached run summarizer) sit under their run's
   filter, muted and italic so they never read as pipeline work */
tr.agen.aux { opacity:.6; }
tr.agen.aux td.step > span { font-style:italic; }
/* the Actions zone: every control that mutates task/run state sits
   below one rule under one header - IntelliJ-style, view-only
   controls (trajectory expand/collapse) stay with their content */
#detail .actzone { border-top:1px solid #30363d; margin-top:16px; }
#detail .acth { margin:12px 0 4px; font-size:11px; font-weight:600; text-transform:uppercase; letter-spacing:.05em; color:#8b949e; }
#detail .actzone .actions { margin:10px 0; }
#detail .actzone .actions .sub { font-size:12px; }
.arc { font-size:12px; border:1px solid #21262d; border-radius:6px; padding:6px 8px; max-height:280px; overflow-y:auto; background:#0d1117; }
.arcspan { color:#8b949e; min-width:44px; text-align:right; flex:none; }
.arcphase { flex:none; width:78px; }
.archead { color:#8b949e; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
details.arcent > summary:hover .archead { color:#e6edf3; }
details.arcgrp > summary { display:flex; gap:8px; padding:1px 0; cursor:pointer; list-style:none; }
details.arcgrp > summary::-webkit-details-marker { display:none; }
details.arcgrp > summary::before { content:'▸'; color:#6e7681; flex:none; }
details.arcgrp[open] > summary::before { content:'▾'; }
.arcdetail { margin:2px 0 6px 18px; border-left:1px solid #21262d; padding-left:8px; }
.arctime { color:#6e7681; min-width:60px; flex:none; }
.arcdur { color:#6e7681; min-width:44px; text-align:right; flex:none; }
.arctok { color:#6e7681; min-width:96px; text-align:right; flex:none; }
.arc-setup, .arc-work { color:#8b949e; }
.arc-audit { color:#79c0ff; }
.arc-implement { color:#d2a8ff; }
.arc-test { color:#ffa657; }
.arc-docs { color:#7ee787; }
.arc-commit { color:#3fb950; }
/* per-entry expansion: each arc row is a <details> whose body carries
   the step's full retained text, fetched from the export on demand */
details.arcent > summary { display:flex; gap:8px; padding:1px 0; cursor:pointer; list-style:none; font-size:11px; }
details.arcent > summary::-webkit-details-marker { display:none; }
details.arcent > summary::before { content:'▸'; color:#6e7681; flex:none; }
details.arcent[open] > summary::before { content:'▾'; }
details.arcent[open] > summary .archead { color:#e6edf3; }
.arcfull { margin:2px 0 8px 14px; border-left:1px solid #21262d; padding-left:8px;
  /* skip layout/paint of offscreen bodies - expand-all on a big export
     must not freeze the modal */
  content-visibility:auto; contain-intrinsic-size:auto 120px; }
.arclbl { color:#6e7681; font-size:11px; margin:7px 0 1px; }
.arcmeta { color:#8b949e; font-size:11px; margin:2px 0 4px; }
.arcnote { color:#6e7681; font-style:italic; padding:2px 0 4px; }
#detail .arcpre { white-space:pre-wrap; overflow-wrap:anywhere; word-break:break-word;
  user-select:text; max-height:320px; overflow:auto; background:#0d1117;
  border:1px solid #21262d; border-radius:4px; padding:6px 8px;
  margin:3px 0; font-size:11px; color:#c9d1d9; }
.arcbar { margin:5px 0 2px; }
.arcbar .btn { padding:2px 8px; font-size:11px; }
/* expanded entries get room to read: the arc list releases its compact
   height cap and the modal scrolls instead */
.arc:has(details.arcent[open]) { max-height:70vh; }

.stage { display:inline-block; border-radius:9px; padding:1px 7px; font-size:11px; border:1px solid #30363d; white-space:nowrap; }
/* a pill wider than its column clipped invisibly at the td edge
   (narrow viewports) - ellipsize inside the pill instead, and keep its
   baseline even with overflow:hidden so siblings still line up */
td .stage { max-width:100%; overflow:hidden; text-overflow:ellipsis;
  vertical-align:top; }
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
.stage.empty { color:#8b949e; border-color:#30363d; }
.stage.missing { color:#f0883e; border-color:#7a3d12; background:#2d1f14; }
.stage.followup { color:#8b949e; }
.stage.wt { color:#58a6ff; }
.stage.halted { color:#8b949e; }
.stage.archived { color:#8b949e; border-color:#21262d; }
/* 'unreviewed' = a completion nobody has marked reviewed yet. Cyan is a
   free hue in this palette (green reads as success onto failures,
   orange is timed_out/questions) - and the pill's own TEXT is the
   carrier per WCAG 1.4.1, with the row's left inset + bolded name as
   the non-color marks, so the flag never relies on hue alone. */
.stage.unreviewed { color:#39c5cf; border-color:#1e6e77; background:#122e33; }
/* the left-edge inset mirrors .selected's filter mark - unreviewed wins
   the slot when both classes land on one row (this rule is later) */
tr.unreviewed td:first-child { box-shadow:inset 3px 0 #39c5cf; }
tr.agen.unreviewed td.step > span { font-weight:700; }
.linkbtn { background:none; border:0; color:#58a6ff; cursor:pointer; font:inherit; padding:0; text-decoration:underline; }
.run-name { color:#58a6ff; text-decoration:underline; text-align:left; }
.run-name:hover { color:#79c0ff; }
/* Follow-up links are metadata chips - the same pill chrome as .stage
   so the row reads as labels, not actions: neutral grey, the underline
   only appears on hover. A bad outcome (error/invalid/missing/...)
   colors just the status word (.fu-bad), the prefix stays grey. */
.fulink { display:inline-block; border:1px solid #30363d; border-radius:9px;
  padding:1px 7px; font-size:11px; line-height:1.6; background:none;
  color:#8b949e; cursor:pointer; font:inherit; text-decoration:none;
  white-space:nowrap; vertical-align:middle; }
.fulink:hover { color:#c9d1d9; border-color:#8b949e;
  text-decoration:underline; }
span.fulink { cursor:default; }
span.fulink:hover { color:#8b949e; border-color:#30363d;
  text-decoration:none; }
.fu-bad { color:#f85149; }
/* the other half of a follow-up pair while a modal shows one side -
   lighter than .dsel (the open view's own row) */
tr.dlinked td { background:#141e29; }
tr.dlinked td:first-child { box-shadow:inset 3px 0 #3d444d; }
/* a request field whose value differs from the saved proposal/baseline
   gets a subtle amber frame so the pend-note's list maps onto the form */
input.pendchg, select.pendchg, textarea.pendchg, div.pendchg {
  border-color:#d29922 !important;
  box-shadow:0 0 0 2px rgba(210,153,34,.4); }
input[type="checkbox"].pendchg { border-color:#d29922 !important;
  outline:2px solid rgba(210,153,34,.85); outline-offset:2px;
  box-shadow:none; }
label.check.pendchg-lab { box-shadow:0 0 0 2px rgba(210,153,34,.35);
  border-radius:4px; }
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
/* the paperclip control opens the OS image picker into the same upload
   path - styled as an icon-sized .btn.secondary, fed by a hidden
   input[type=file] */
.attach-row { display:flex; align-items:flex-start; gap:8px; }
.attach-btn { flex:none; display:inline-flex; align-items:center; justify-content:center; width:26px; height:26px; margin-top:1px; padding:0; border:1px solid #30363d; border-radius:6px; background:#21262d; color:#8b949e; cursor:pointer; }
.attach-btn:hover { color:#e6edf3; border-color:#8b949e; }
#detail textarea.drop { border-color:#58a6ff; }
#detail label.check { display:flex; align-items:flex-start; gap:8px; margin:12px 0 4px; font-size:13px; cursor:pointer; }
#detail label.check input[type=checkbox] { width:auto; margin-top:2px; }
#detail textarea { min-height:145px; resize:vertical; }
#detail textarea.toml { min-height:270px; font:12px/1.5 ui-monospace,Consolas,monospace; }
#detail input:disabled, #detail textarea:disabled, #detail select:disabled { opacity:.65; }
#detail .field-row { display:grid; grid-template-columns:2fr 1fr 1fr; gap:10px; }
/* model comboboxes: the input stays free text; its popup pairs a typed-
   filter Recommendations group with a separated, scrollable All models
   group (.mall). Anchored under the field, flips .up near the modal's
   bottom edge so it never stretches the dialog's scroll area */
#detail .mcombo { position:relative; }
#detail .mpop { position:absolute; top:100%; left:0; right:0; z-index:8;
  display:flex; flex-direction:column; margin-top:4px; padding:4px;
  background:#1c2128; border:1px solid #30363d; border-radius:8px;
  box-shadow:0 8px 24px rgba(0,0,0,.55); max-height:300px; }
#detail .mpop.up { top:auto; bottom:100%; margin:0 0 4px; }
/* the recs group is capped, never squeezed: flex-shrink would collapse
   the group under max-height while its rows keep their size, smearing
   them over the all-models list below (the 'dotted rows' glitch). The
   inner .mall scrolls, same as the all-models group. */
#detail .mpop .mgrp { display:flex; flex-direction:column; min-height:0;
  flex:0 0 auto; max-height:160px; overflow:hidden; }
#detail .mpop .mgrp-all { flex:1 1 auto; max-height:none; }
#detail .mpop .mgrp + .mgrp { border-top:1px solid #30363d; margin-top:2px; }
#detail .mpop .msec { color:#6e7681; font-size:10px; letter-spacing:.04em;
  text-transform:uppercase; padding:5px 8px 3px; flex:none; }
#detail .mpop .mall { overflow-y:auto; min-height:0; }
#detail .mpop .mopt { padding:5px 10px; border-radius:5px; font-size:12px;
  color:#e6edf3; cursor:pointer; white-space:nowrap; overflow:hidden;
  text-overflow:ellipsis; }
#detail .mpop .mopt.on { background:#1f6feb; color:#fff; }
#detail .mpop .mopt[aria-selected="true"] { color:#79c0ff; }
#detail .mpop .mopt.on[aria-selected="true"] { color:#fff; }
#detail .mpop .mnone { color:#6e7681; font-size:11px; font-style:italic;
  padding:4px 10px; }
#detail .actions { display:flex; gap:8px; flex-wrap:wrap; align-items:center; margin:16px 0 0; }
#detail .notice, #powerbox .notice, #tourcard .notice { color:#8b949e; background:#0d1117; border-left:3px solid #30363d; padding:8px 10px; margin:10px 0; }
#detail .notice.warn, #powerbox .notice.warn, #tourcard .notice.warn { border-color:#d29922; color:#e6edf3; }
#detail .notice.error, #powerbox .notice.error, #tourcard .notice.error { border-color:#f85149; color:#ffb2aa; }
#powerbox .notice.ok, #tourcard .notice.ok { border-color:#3fb950; color:#c9d1d9; }
#powerbox .notice.info { border-color:#58a6ff; color:#c9d1d9; }
#detail details.proposal { margin-top:14px; }
#detail details.proposal summary { cursor:pointer; color:#58a6ff; }
#detail .proposal-list { margin-top:12px; padding-left:20px; }
#detail .proposal-list li { margin:8px 0; }
#detail .proposal-list small { color:#8b949e; }
/* pending-merge panel in run details: one boxed group per project repo,
   each pending step a tickable row (unverified rows stay untickable) */
#detail .pmgrp { border:1px solid #21262d; border-radius:6px; padding:8px 10px; margin:8px 0; background:#0d1117; }
#detail .pmgrp .sub { margin:2px 0 6px; }
#detail label.pmentry { display:flex; align-items:flex-start; gap:8px; margin:4px 0; font-size:13px; color:#e6edf3; cursor:pointer; }
#detail label.pmentry input[type=checkbox] { width:auto; margin-top:3px; }
#detail label.pmentry input[type=checkbox]:disabled { cursor:not-allowed; }
/* run modal: each action cluster (meta / pending merges / run control /
   admin) is its own panel so related copy+button stay together instead of
   one wrapped button row */
#detail .runpanel { border:1px solid #30363d; border-radius:8px; padding:10px 12px; margin:12px 0; }
#detail .runpanel h3 { margin-top:0; }
#detail .runpanel .actions { margin:8px 0 0; }
/* merge button with nothing ticked: outline-only so it reads as idle but
   stays clickable (a click explains what to tick via pm-msg) */
.btn.idle { background:transparent; color:#8b949e; border-color:#30363d; }
.btn.idle:hover:not(:disabled) { border-color:#58a6ff; color:#e6edf3; }
/* sessions kv row: collapsed to a summary line, full list scrolls */
#detail details.sessx summary { cursor:pointer; }
#detail details.sessx .sessfull { margin-top:6px; max-height:140px; overflow:auto; }
/* questionnaire modal (planner 'ask me questions' flow) */
.qopts { display:flex; flex-direction:column; gap:6px; margin:10px 0 4px; }
.qopt { text-align:left; border:1px solid #30363d; border-radius:6px;
  background:#0d1117; color:#e6edf3; padding:8px 10px; cursor:pointer;
  font:inherit; }
.qopt:hover { border-color:#58a6ff; }
.qopt.on { border-color:#58a6ff; background:#1c3041; }
.qopt .sub { display:block; margin-top:2px; }
/* row context menus: a fixed-position action list on right-click (or
   Shift+F10 / the Menu key on a focused row). Items are plain buttons
   carrying the same data-* hooks the modal action buttons use - one
   control path, the server still validates state. */
#ctxmenu { position:fixed; z-index:60; min-width:220px; max-width:340px;
  background:#1c2128; border:1px solid #30363d; border-radius:8px;
  padding:4px; box-shadow:0 8px 24px rgba(0,0,0,.55); user-select:none; }
#ctxmenu .ctxi { display:block; width:100%; text-align:left; background:none;
  border:0; color:#e6edf3; font:inherit; font-size:12px; padding:6px 10px;
  border-radius:5px; cursor:pointer; box-sizing:border-box; }
#ctxmenu .ctxi:hover:not(:disabled), #ctxmenu .ctxi:focus-visible {
  background:#1f6feb; color:#fff; outline:none; }
#ctxmenu .ctxi.danger { color:#ffb2aa; }
#ctxmenu .ctxi.danger:hover:not(:disabled),
#ctxmenu .ctxi.danger:focus-visible { background:#9f3535; color:#fff; }
#ctxmenu .ctxi:disabled { opacity:.45; cursor:default; }
#ctxmenu .ctxsep { height:1px; background:#30363d; margin:4px 2px; }
#ctxmenu .ctxhint { color:#6e7681; font-size:10px; padding:5px 8px 3px;
  border-top:1px solid #21262d; margin-top:4px; }
/* the row a menu is open on keeps a marker while the hover highlight is
   gone (mouseover on the menu clears hoverRun) */
tr.ctxanchor td { background:#1c3041; }
@media (max-width:640px) { .hide-s { display:none; } #detail { width:92vw; } #detail .field-row { grid-template-columns:1fr; } }
</style></head><body>
<div class="topbar">
  <div class="tb-row">
    <h1>agent-conductor</h1>
    <span class="sub" id="meta">loading&hellip;</span>
    <span class="tb-gap"></span>
    <button type="button" class="btn" id="new-request">New request</button>
    <span class="poolbox" id="poolbox" role="group" aria-label="Parallel agents"
      title="machine-wide parallel-agent cap - live slots / cap. Every driver re-checks the cap within seconds; raising it frees queued steps, lowering it never kills a running agent">
      <button type="button" id="pool-dec" aria-label="one fewer parallel agent">&minus;</button><span id="pool-val">agents &hellip;</span><button type="button" id="pool-inc" aria-label="one more parallel agent">+</button>
    </span>
    <button type="button" class="btn secondary" id="tour-btn"
      title="what's new - a guided tour of changes, finished runs and proposals since your last look">What's new<span id="tour-badge" class="tbadge" hidden></span></button>
    <button type="button" class="btn secondary" id="power-btn"
      title="stop or restart this dashboard - warns about merges and live tasks first">&#x23FB;</button>
    <div class="seg" id="density" role="group" aria-label="Row density">
      <button type="button" data-density="compact">Compact</button>
      <button type="button" data-density="full" class="on">Full</button>
    </div>
  </div>
  <div class="sub tb-flow">Save draft &rarr; generate proposal &rarr; review &rarr; start when ready. Generating never runs tasks. Right-click a run or task row for its actions (Shift+F10 from the keyboard; Shift+right-click keeps the browser menu).</div>
  <div class="sub" id="filters"></div>
  <div class="sub" id="undobar" hidden></div>
</div>
<h2>runs</h2><div class="wrapw"><table id="runs"></table></div>
<div id="trashwrap" hidden></div>
<h2>agents by project</h2><div id="agents"></div>
<div id="ctxmenu" role="menu" aria-label="row actions" hidden></div>
<div id="backdrop" hidden><div id="detail" role="dialog" aria-modal="true" aria-labelledby="modal-title"><button type="button" class="x" id="dx">Close ×</button><button type="button" id="dask" data-ask-ai hidden aria-label="Ask AI about this view" title="Ask AI about this view"><span class="ai-spark" aria-hidden="true">&#x2726;</span>Ask AI</button><div id="dbody"></div></div></div>
<div id="askwrap" hidden><div id="askbox" role="alertdialog" aria-modal="true"><div id="asktitle"></div><div id="askmsg"></div><div id="askbtns" class="actions"></div></div></div>
<div id="powerwrap" hidden><div id="powerbox" role="dialog" aria-modal="true"><h3>Dashboard power</h3><div id="pwbody"></div><div class="actions"><button type="button" class="btn secondary" id="pw-cancel">Cancel</button><button type="button" class="btn danger" id="pw-stop">Stop</button><button type="button" class="btn" id="pw-restart">Restart</button></div><div class="sub" id="pwstat"></div></div></div>
<div id="tourwrap" hidden><div id="tourring" hidden></div><div id="tourcard" role="dialog" aria-modal="true" aria-labelledby="tourcard-title" tabindex="-1"></div></div>
<script>
const COLORS = {done:'#3fb950',partial:'#d29922',blocked:'#d29922',
stopped:'#d29922',
failed:'#f85149',timed_out:'#f0883e',running:'#58a6ff',queued:'#8b949e',
waiting:'#8b949e',skipped:'#6e7681',planned:'#bc8cff',ready:'#bc8cff',
generating:'#bc8cff',draft:'#d29922',questions:'#ffa657',
launching:'#58a6ff',launch_failed:'#f85149',lost:'#6e7681',
superseded:'#8b949e',
empty:'#8b949e',missing:'#f0883e'};
const STAGES = {draft:'Draft · unsent',generating:'Preparing proposal',
  questions:'Needs your answers',
  ready:'Ready to run',launching:'Starting run',launch_failed:'Start failed',
  error:'Planning failed',empty:'No work remains',
  missing:'Follow-up missing'};
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
// token readout: billed-in/out/cached + LLM turns, order fixed and
// spelled out in TOK_HELP + the cell title - the one labeled short
// form both tables use at every density
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
// Review workflow: 'unreviewed' = a completion a human hasn't marked
// reviewed. role=status so a live refresh announces it; the pill text is
// the carrier - color is never the only signal. Independent of the
// outcome's own status chips/dots, so a failed result stays red AND
// carries this flag until someone looks.
const unreviewedPill = what =>
  `<span class="stage unreviewed" role="status" ` +
  `title="${esc(what)} finished and has not been marked reviewed - ` +
  `open details for the review controls">unreviewed</span>`;
// kv-row text for the review field: an ack names the mark's stamp, a
// seq-less legacy record says so outright instead of implying work.
const reviewLine = e => e.needs_review ? 'needs review'
  : (e.review && e.review.reviewed === true
    ? `reviewed ${e.review.at || ''}`
    : (e.completion_seq != null ? 'no review recorded'
                               : 'no completion recorded'));
// row density + sticky-topbar height (test harness stubs `document`
// only - guard every browser global)
const store = (typeof localStorage!=='undefined') ? localStorage :
  {getItem:()=>null, setItem:()=>{}};
let density = store.getItem('ac-density') || 'full';
// 'archived' view chip: archived runs stay on disk but are hidden from the
// default listing - the toggle refetches /api/state?archived=1 to list them.
let showArchived = store.getItem('ac-arch') === '1';
// 'trash' view chip: recoverable deletions (run records, proposal drafts)
// list with Restore + a confirmed Empty trash until the 48h sweep purges
// them; the toggle refetches /api/state?trash=1 for the entry list.
let showTrash = store.getItem('ac-trash') === '1';
let lastTrash = null;   // {id,label} of the just-trashed entry - the Undo
                        // bar rides it until used, dismissed, or the entry
                        // disappears (restored/emptied/purged elsewhere)
const stateUrl = () => '/api/state' +
  ((showArchived||showTrash)
    ? '?' + [showArchived?'archived=1':'', showTrash?'trash=1':'']
        .filter(Boolean).join('&') : '');
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
const tokenHelp = '';  // the '?' explainer widget was removed; TOK_HELP
// survives as the hover tooltip on tokens cells
// Cumulative contract: elapsed_s is the sum of CLOSED execution
// intervals (each dispatch banks its own); while a row is running the
// live interval adds now - started_at on top. A queued resumed row
// shows its banked total - never idle-ticks off the stale started_at.
function elapsed(a, now){
  const base = a.elapsed_s||0;
  if (a.status==='running' && a.started_at)
    return fmtS(base + Math.max(0,(now - new Date(a.started_at))/1000));
  if (a.elapsed_s != null) return fmtS(a.elapsed_s);
  return '-'; }
// Runs: same contract - elapsed_s = closed driver intervals, exec_start
// = the live one's start while a driver is in flight.
function runElapsed(r, now){
  const base = r.elapsed_s||0;
  if (r.exec_start)
    return fmtS(base + Math.max(0,(now - new Date(r.exec_start))/1000));
  if (r.elapsed_s != null) return fmtS(r.elapsed_s);
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
const projs = new Set();  // project chips/group names: empty = all slugs
let selectedRun = null;
const prevTail = {};      // run|step -> last tail_last shown; a change
                          // flashes the result cell (.fresh) as logs stream.
                          // 'genline|<runkey>' keys the same diff for a
                          // proposal row's compact live-output line
const SHOW_N = 10;
let runsShown = SHOW_N;   // visible run-table rows; active runs never hide
const ORDER = ['running','queued','waiting','planned','done','partial',
               'blocked','stopped','timed_out','failed','skipped','lost',
               'superseded'];
const FACETS = ['unreviewed','wt','merged','unmerged','deferred'];
const FACET_TINT = {unreviewed:'#39c5cf'};
const matchesFacet = (a,f) => f==='wt' ? a.wt :
  f==='merged' ? a.merged===true :
  f==='unmerged' ? a.merged===false :
  f==='deferred' ? a.deferred===true : !!a.needs_review;
const runKey = r => r.key||r.dir;
let lastData = null;
let AGENTS = [];          // last payload, for the detail modal

// column layout shared by every per-project table (fixed layout -> the
// same widths line up across tables)
const AGENT_COLS = '<colgroup>' +
  '<col style="width:8%"><col style="width:19%">' +
  '<col style="width:10%"><col class="hide-s" style="width:6%">' +
  '<col class="hide-s" style="width:5%"><col class="hide-s" style="width:5%">' +
  '<col style="width:6%"><col style="width:6%">' +
  '<col class="hide-s" style="width:10%"><col class="hide-s" style="width:5%">' +
  '<col style="width:6%"><col class="hide-s" style="width:5%">' +
  '<col style="width:9%">' +
  '</colgroup>';

let detailIdx = null;
let detailKey = null;       // run_key|step of the open agent modal - the
                            // payload re-sorts each tick, so the index is
                            // re-resolved by key before every re-render
let modalRunKey = null;     // run_key of the open run-details modal - the
                            // pending-merge panel re-renders on data
                            // change, so a mid-merge refresh stays live
const mergeSel = {};        // runKey -> Set of pending steps ticked for merge
const mergeResult = {};     // runKey -> last merge attempt's outcome text
const sessOpen = {};        // runKey -> sessions list expanded in run modal
let modalRequest = null;
let modalQA = null;         // {id, rec, sel:Set, free:''} while the planner
                            // questionnaire modal is open
let runModalKey = null;     // runKey while the run-details modal shows
let summaryView = null;     // {key: agentKey} while the summary modal shows
let modalScroll = null;     // scrollTop a Back navigation restores on render
const modalHist = [];       // Back stack: {kind, key|id, scroll} entries of
                            // the view each modal-to-modal jump left behind
let ctxState = null;        // {items, idx, anchor, returnEl} while the row
                            // context menu is open; null when closed
let rqAttachments = [];     // [{name,path}] on the open request form
const qDismissed = new Set();   // request|qids sigs the human closed - the
                              // auto-popup never re-nags for the same set
const promptDrafts = {};    // key -> unsaved 'Edit prompt' text
const notesDrafts = {};     // key -> unsaved 'resume notes' text
// 'Interrupt + instruct' composer, all keyed by agentKey like the draft
// maps so the 2s rebuild never eats an in-flight draft.
const intrOpen = {};        // key -> composer section is open
const intrDrafts = {};      // key -> unsent instructions text
const intrAttach = {};      // key -> [{name,path}] attachments to send
const intrSent = {};        // key -> {at: interrupts count when sent}
// Trajectory expansion: which arc groups/entries are open per agent, and
// the fetched full text per export file. Both survive the 2s modal
// rebuild - the rebuild re-renders open entries from cache instead of
// refetching or collapsing them.
const trajOpen = {};        // agentKey -> {g:Set<groupIdx>, e:Set<sid>}
const trajDetail = {};      // export@mtime -> 'loading'|{entries}|{error}
// run_key namespaces per run/request - run_dir alone collides for
// unlaunched proposals (their dir is null), which would let the detail
// modal (and a nav click) land on another request's same-named step.
const agentKey = a => (a.run_key||a.run_dir||'') + '|' + a.step;
const trajOpenFor = key => trajOpen[key] ||
  (trajOpen[key] = {g:new Set(), e:new Set()});
// the fetch body is keyed on path+mtime: a resumed step's rewritten
// export is a different cache entry, never a stale reuse
const trajKeyOf = a => a.export
  ? a.export+'@'+((a.arc&&a.arc.em)||'') : null;
const trajURL = a => a.run_dir
  ? '/api/runs/'+encodeURIComponent(base(a.run_dir))+
    '/steps/'+encodeURIComponent(a.step)+'/trajectory'
  : (a.request_id
    ? '/api/requests/'+encodeURIComponent(a.request_id)+'/trajectory'
    : null);
function trajEnsure(a){
  const key=trajKeyOf(a), url=trajURL(a);
  if (!key || !url || trajDetail[key]) return;
  trajDetail[key] = 'loading';
  fetch(url).then(async r=>{
    const j = await r.json().catch(()=>({}));
    trajDetail[key] = r.ok ? j
      : {error:(j && j.error) || ('HTTP '+r.status)};
    fillOpenArc(key);
  }).catch(e=>{
    trajDetail[key] = {error:'fetch failed: '+(e && e.message || e)};
    fillOpenArc(key);
  });
}
// patch the open entries' bodies the moment the fetch lands - no wait
// for the next 2s rebuild, and no innerHTML reset while reading
function fillOpenArc(key){
  try{
    const a = AGENTS.find(x=>agentKey(x)===detailKey);
    if (!a || trajKeyOf(a)!==key) return;
    document.querySelectorAll('#detail details.arcent[open]')
      .forEach(d=>{
        const body = d.querySelector('.arcfull');
        if (body) body.innerHTML = arcFullHTML(a, d.dataset.asid);
      });
  }catch(e){}
}
const arcNote = s => '<div class="arcnote">'+esc(s)+'</div>';
const arcBlock = (label, text) =>
  '<div class="arclbl">'+esc(label)+'</div>'+
  '<pre class="arcpre">'+esc(text)+'</pre>';
function arcFullHTML(a, sid){
  const key = trajKeyOf(a);
  if (!key)
    return arcNote('no export is linked to this step - its full text '+
      'was not retained');
  const det = trajDetail[key];
  if (!det || det==='loading')
    return arcNote('loading full text…');
  if (det.error) return arcNote('full text unavailable: '+det.error);
  const ent = det.entries && det.entries[String(sid)];
  if (!ent)
    return arcNote('no detailed text was recorded for this step - the '+
      'export only carries its summary line');
  let body = '';
  if (ent.think) body += arcBlock('reasoning', ent.think);
  if (ent.msg)   body += arcBlock('message', ent.msg);
  (ent.calls||[]).forEach((c,i)=>{
    body += arcBlock('tool call '+(i+1)+' · '+(c.name||'?'),
                     c.args||'(no arguments)');
    if (c.result!=null) body += arcBlock('result', c.result);
  });
  (ent.extra_obs||[]).forEach(o=>{ body += arcBlock('observation', o); });
  if (!body)
    return arcNote('no detailed text was recorded for this step - the '+
      'export only carries its summary line');
  let h = '';
  if (ent.src || ent.model)
    h += '<div class="arcmeta">source '+esc(ent.src||'?')+
      (ent.model?' · model '+esc(ent.model):'')+'</div>';
  h += body;
  const tn = Object.keys(ent.trunc||{});
  if (tn.length)
    h += arcNote('truncated for display: '+
      tn.map(f=>f+' ('+kfmt(ent.trunc[f])+' chars total)').join(', ')+
      ' - the export file holds the rest');
  h += '<div class="arcbar"><button type="button" '+
    'class="btn secondary" data-arc-copy="'+esc(sid)+'" '+
    'title="copy the full text of this step to the clipboard">'+
    'Copy step text</button></div>';
  return h;
}
// the verbatim text for the clipboard - complete, section-labeled, and
// never through HTML (escaping is a display concern only)
function arcPlainText(ent){
  const NL = String.fromCharCode(10);
  const parts = [];
  if (ent.think) parts.push('--- reasoning ---'+NL+ent.think);
  if (ent.msg)   parts.push('--- message ---'+NL+ent.msg);
  (ent.calls||[]).forEach((c,i)=>{
    let s = '--- tool call '+(i+1)+' '+(c.name||'?')+' ---'+NL+
      (c.args||'');
    if (c.result!=null) s += NL+'--- result ---'+NL+c.result;
    parts.push(s);
  });
  (ent.extra_obs||[]).forEach(o=>parts.push('--- observation ---'+NL+o));
  const tn = Object.keys(ent.trunc||{});
  if (tn.length)
    parts.push('(display-truncated fields: '+
      tn.map(f=>f+'='+ent.trunc[f]+' chars').join(', ')+')');
  return parts.join(NL+NL);
}
function copyText(s){
  if (typeof navigator!=='undefined' && navigator.clipboard &&
      navigator.clipboard.writeText)
    return navigator.clipboard.writeText(s).then(()=>true,()=>false);
  try{
    const ta = document.createElement('textarea');
    ta.value = s;
    ta.style.position='fixed'; ta.style.opacity='0';
    document.body.appendChild(ta); ta.focus(); ta.select();
    const ok = document.execCommand && document.execCommand('copy');
    ta.remove();
    return Promise.resolve(!!ok);
  }catch(e){ return Promise.resolve(false); }
}
// a text selection inside the open modal is mid-read (or mid-copy) - the
// 2s rebuild would drop it, so it pauses the modal body rebuild only
function modalSelActive(){
  try{
    const s = typeof window!=='undefined' && window.getSelection &&
      window.getSelection();
    const d = document.getElementById('detail');
    return !!(s && !s.isCollapsed && d && d.contains &&
              d.contains(s.anchorNode));
  }catch(e){ return false; }
}
// --- scroll-stable repaint of the open task modal ----------------------
// The refresh tick rebuilds #dbody wholesale, which leaves the browser
// nothing to scroll-anchor: numeric scrollTop survives the swap but the
// CONTENT moves - a shorter repaint clamps #detail permanently (the
// offset never comes back when it regrows), and growth above the
// viewport pushes the text the user was reading off-screen. Before the
// swap we remember which landmark sits at the viewport top and its
// pixel offset; after it we put the SAME landmark back at that offset.
// Everything is keyed by the open task: only a repaint of the view
// already on screen restores anything, so a position can never bleed
// into another task's modal (deliberate sibling nav still lands at the
// top; a Back jump restores its own modalScroll snapshot instead).
const MODAL_LANDMARKS = 'h3,.k,.wrap,.actions,.notice,.arcnote,#dtail,' +
  '.arcgrp,.arcent,textarea,input,.sumdoc';
// A landmark's identity across the rebuild: real ids first, then the
// stable data hooks the markup already carries, then content-derived
// keys. Occurrence index (n) disambiguates repeated sigs ('earlier'
// outcome rows, .actions/.notice blocks).
function landmarkSig(el){
  if (el.id) return '#'+el.id;
  const d = el.dataset || {};
  if (d.asid!==undefined) return 'sid:'+d.asid;
  if (d.ag!==undefined) return 'ag:'+d.ag;
  const cl = el.classList;
  if (cl && cl.contains && cl.contains('k'))
    return 'k:'+(el.textContent||'');
  if ((el.tagName||'').toUpperCase()==='H3')
    return 'h3:'+(el.textContent||'').slice(0,48);
  return (el.tagName||'?').toLowerCase()+'.'+
    (typeof el.className==='string' ? el.className.split(' ')[0] : '');
}
function modalLandmarks(){
  const b = document.getElementById('dbody');
  return (b && b.querySelectorAll)
    ? b.querySelectorAll(MODAL_LANDMARKS) : [];
}
function captureModalScroll(){
  const sc = document.getElementById('detail');
  const out = {top:(sc && sc.scrollTop)||0, sig:null, n:0, dy:0,
               dtail:null};
  const dt = document.getElementById('dtail');
  if (dt) out.dtail = dt.scrollTop;      // the tail <pre> scrolls itself
  if (!sc || !sc.getBoundingClientRect) return out;
  const sTop = sc.getBoundingClientRect().top;
  const seen = {};
  for (const el of modalLandmarks()){
    const sig = landmarkSig(el), idx = seen[sig]||0;
    seen[sig] = idx + 1;
    if (out.sig!=null || !el.getBoundingClientRect) continue;
    const r = el.getBoundingClientRect();
    if (r.bottom > sTop + 4){            // first still-visible landmark
      out.sig = sig; out.n = idx; out.dy = r.top - sTop;
    }
  }
  return out;
}
function restoreModalScroll(s){
  const sc = document.getElementById('detail');
  if (!sc || !s) return;
  if (s.dtail!=null){
    const dt = document.getElementById('dtail');
    if (dt) dt.scrollTop = s.dtail;
  }
  if (s.sig && sc.getBoundingClientRect){
    const sTop = sc.getBoundingClientRect().top, seen = {};
    for (const el of modalLandmarks()){
      const sig = landmarkSig(el), idx = seen[sig]||0;
      seen[sig] = idx + 1;
      if (sig===s.sig && idx===s.n && el.getBoundingClientRect){
        // same landmark at the same viewport offset - growth or
        // shrinkage above it is absorbed here, not as a viewport jump
        sc.scrollTop += (el.getBoundingClientRect().top - sTop) - s.dy;
        return;
      }
    }
  }
  sc.scrollTop = s.top;                  // landmark gone - numeric hold
}
// The same idea for keyboard focus: the rebuild drops whatever was
// focused, so a stable signature finds the equivalent new element. The
// draft maps already carry textarea CONTENT; this restores where the
// caret/focus ring was - with preventScroll so it can't fight the
// anchor restore above.
// status word -> .stc color class (task modal + summary docs share it)
const STCLASS = {done:'ok',failed:'bad',timed_out:'bad',lost:'bad',
  blocked:'bad',error:'bad',stopped:'warn',partial:'warn',
  queued:'warn',needs_merge:'warn',needs_work:'warn',
  running:'run',skipped:'other',idle:'other',superseded:'other'};
const FOCUS_ATTRS = ['modalBack','nav','snav','summary','runSummary',
  'agentDetail','editPrompt','arcAll','arcNone','arcCopy','stopAgent',
  'intrOpen','intrSend','intrCancel','skipStep','unskipStep',
  'setTimeout','savePrompt','revertPrompt','saveNotes','openRequest',
  'stopGen','reviewStep','reviewRun','filterRun','runDetails','mergeRun',
  'resumeRun','pauseRun','stopRun','drainRun','archRun','deleteRun',
  'summarizeRun','action','qopt','qsend','qskip','qskipall'];
const MODAL_FOCUSABLE = '#detail button,#detail a,#detail summary,' +
  '#detail input,#detail textarea,#detail select,#detail [tabindex]';
function focusSig(el){
  if (!el) return null;
  const d = el.dataset || {};
  for (const k of FOCUS_ATTRS)
    if (d[k]!==undefined) return 'd:'+k+'='+d[k];
  const ks = Object.keys(d);
  if (ks.length){ const k = ks.sort()[0]; return 'd:'+k+'='+d[k]; }
  if (el.id) return '#'+el.id;
  if ((el.tagName||'')==='A' && el.getAttribute)
    return 'a:'+(el.getAttribute('href')||'');
  return null;
}
function captureModalFocus(){
  const f0 = document.activeElement;
  const d = document.getElementById('detail');
  if (!f0 || (d && d.contains && !d.contains(f0))) return null;
  const fid = f0.id;
  // textareas: the draft maps carry the text; carry the caret position.
  // #d-timeout has no draft map - carry its unsaved value too, but let
  // a changed server value win (compare against the rendered default).
  if (fid==='d-prompt'||fid==='d-rnotes'||fid==='d-instr')
    return {id:fid, sel:[f0.selectionStart, f0.selectionEnd]};
  if (fid==='d-timeout')
    return {id:fid, val:f0.value,
            rendered:(f0.getAttribute ? f0.getAttribute('value')
                                      : null)};
  const sig = focusSig(f0);
  if (!sig) return null;
  let n = 0;
  for (const x of document.querySelectorAll(MODAL_FOCUSABLE)){
    if (x===f0) break;
    if (focusSig(x)===sig) n++;
  }
  return {sig, n};
}
function restoreModalFocus(f){
  if (!f) return;
  let el = null;
  if (f.id) el = document.getElementById(f.id);
  else if (f.sig!=null){
    let n = 0;
    for (const x of document.querySelectorAll(MODAL_FOCUSABLE)){
      if (focusSig(x)===f.sig){ if (n===f.n){ el=x; break; } n++; }
    }
  }
  if (!el || !el.focus) return;
  try { el.focus({preventScroll:true}); }
  catch(e){ try { el.focus(); } catch(e2){} }
  if (f.sel && el.setSelectionRange){
    try { el.setSelectionRange(f.sel[0], f.sel[1]); } catch(e){}
  }
  if (f.id==='d-timeout' && f.val!==undefined &&
      el.value===f.rendered) el.value = f.val;
}
const qSig = (id, pend) => id + '|' + (pend||[]).map(q=>q.qid).join(',');
let detailHTML = '';    // markup last painted into #dbody for the OPEN
                        // task view - a byte-identical repaint skips the
                        // DOM write entirely, so scroll, selection,
                        // focus and hover all survive untouched
// --- themed ask/alert ---------------------------------------------------
// Native confirm()/alert() draw the browser's chrome - every prompt on
// this page routes through #askbox instead so dialogs keep the dashboard
// palette. _ask resolves with the clicked button's index (-1 on a
// backdrop/Escape dismiss); askConfirm maps that to a bool, showAlert
// just waits for dismissal.
let _askRes = null;
function _ask(msgHtml, opts = {}){
  return new Promise(res => {
    _askRes = res;
    document.getElementById('asktitle').textContent = opts.title || '';
    document.getElementById('askmsg').innerHTML = msgHtml;
    document.getElementById('askbtns').innerHTML = (opts.btns ||
      [{label:'OK'}]).map((b, i) =>
      `<button type="button" class="btn${b.cls ? ' ' + b.cls : ''}" ` +
      `data-ask="${i}">${esc(b.label)}</button>`).join('');
    document.getElementById('askwrap').hidden = false;
    const last = document.querySelector('#askbtns .btn:last-child');
    if (last) last.focus();
  });
}
function _askDone(i){
  document.getElementById('askwrap').hidden = true;
  const r = _askRes; _askRes = null;
  if (r) r(i);
}
const askConfirm = (msg, opts = {}) =>
  _ask(`<div>${esc(msg)}</div>`, {title: opts.title || 'Confirm',
    btns: [{label: opts.cancel || 'Cancel', cls: 'secondary'},
           {label: opts.ok || 'OK', cls: opts.danger ? 'danger' : ''}]})
    .then(i => i === 1);
const showAlert = (msg, title) =>
  _ask(`<div>${esc(msg)}</div>`, {title: title || ''}).then(() => {});
// --- dashboard power ----------------------------------------------------
// The topbar power button opens #powerwrap with the server's own safety
// report (/api/serve/power): live merges flagged red (a restart orphans
// them mid-flight), in-flight proposal generations and run launches
// flagged red too (the planner/launcher IS this process - a restart
// kills it outright), live steps flagged amber (they survive but
// nothing new launches and a merge firing mid-restart can miss). The
// SAME report is re-fetched at button-press time - the dialog can sit
// open while work starts behind it, so the open-time answer never
// authorizes the click. An unsafe press re-renders the fresh report and
// arms once: a second press on the same button is the informed
// override. Restart respawns a detached `conductor.py serve --wait-port`
// child; the boot-id check in refresh() reloads open tabs once it's
// back - the poll below just makes it immediate.
let pwArmed = null;   // action the operator saw the fresh unsafe report
                      // for and still clicked again
function powerBodyHtml(s){
  const m = s.merges || [], t = s.tasks || [],
        g = s.generating || [], l = s.launching || [],
        j = s.jobs || [];
  const high = g.length + l.length;
  const low = m.length + t.length + j.length;
  return (high
      ? `<div class="notice error"><b>interrupted or wedged by a ` +
        `restart</b>` +
        (g.length ? `<ul><li><b>proposal generation</b><ul>` +
          `${g.map(x => `<li>${esc(x.label)}</li>`).join('')}</ul></li>` +
          `</ul>the planner's drain lives in this dashboard process - ` +
          `restarting orphans the generation and the request lands in ` +
          `error ('Planning was interrupted'); the previous proposal is ` +
          `kept, but the planning session's work is lost` : '') +
        (l.length ? `<ul><li><b>run launch</b><ul>` +
          `${l.map(x => `<li>${esc(x.label)}</li>`).join('')}</ul></li>` +
          `</ul>the launcher itself is detached and keeps going, but ` +
          `the dashboard's tracking of it dies - the request can ` +
          `wedge in 'Starting run' (or briefly mislabel 'Start ` +
          `failed') until the run's state links it` : '') +
        `</div>` : '') +
    (t.length
      ? `<div class="notice info"><b>keeps running through a ` +
        `restart: ${t.length} task(s) live</b><ul>` +
        `${t.map(x => `<li>${esc(x.run)} / ${esc(x.step)} ` +
          `(${esc(x.status)})</li>`).join('')}</ul>` +
        `workers and their drivers are detached processes - the ` +
        `restart does not touch them and the new dashboard re-adopts ` +
        `them from state.json</div>` : '') +
    (m.length || j.length
      ? `<div class="notice info"><b>detached jobs that finish on ` +
        `their own</b><ul>` +
        `${m.map(x => `<li>${esc(x.where)} - ${esc(x.why)}</li>`)
          .join('')}` +
        `${j.map(x => `<li>${esc(x.kind)} ${esc(x.target)} ` +
          `(pid ${x.pid})</li>`).join('')}</ul>` +
        `merges, resumes and summaries run in detached processes - a ` +
        `restart only hides their live progress; they still complete ` +
        `and clean up their own claims</div>` : '') +
    (!high && !low
      ? `<div class="notice ok">nothing merging, planning or running ` +
        `- safe to restart</div>` : '');
}
async function openPower(){
  pwArmed = null;
  const w = document.getElementById('powerwrap');
  document.getElementById('pwstat').textContent = '';
  document.getElementById('pwbody').innerHTML =
    '<div class="notice">checking live state&hellip;</div>';
  w.hidden = false;
  let s;
  try { s = await api('GET', '/api/serve/power'); }
  catch (e) {
    document.getElementById('pwbody').innerHTML =
      `<div class="notice error">${esc(e.message || e)}</div>`;
    return;
  }
  document.getElementById('pwbody').innerHTML = powerBodyHtml(s);
}
async function powerAct(action){
  const stat = document.getElementById('pwstat');
  const btns = document.querySelectorAll('#powerbox .btn');
  btns.forEach(b => b.disabled = true);
  // the open-time report is stale the moment it renders - re-check at
  // press time so a merge that started since can't be orphaned silently
  stat.textContent = 'rechecking live state…';
  let s;
  try { s = await api('GET', '/api/serve/power'); }
  catch (e) {
    stat.textContent = 'Safety recheck failed: ' + (e.message || e);
    btns.forEach(b => b.disabled = false);
    return;
  }
  document.getElementById('pwbody').innerHTML = powerBodyHtml(s);
  // only serve-coordinated work (generation drain, launch bookkeeping)
  // is genuinely broken by a restart - detached tasks/merges/jobs get a
  // report, not a confirm gate
  if (((s.generating||[]).length || (s.launching||[]).length) &&
      pwArmed!==action){
    pwArmed = action;   // the warning is now on screen - a repeat click
                        // is an informed choice, not a stale-read accident
    stat.textContent = 'unsafe right now - press ' +
      (action==='restart' ? 'Restart' : 'Stop') + ' again to do it anyway';
    btns.forEach(b => b.disabled = false);
    return;
  }
  stat.textContent = action === 'restart'
    ? 'restarting - this page reloads when the dashboard is back\u2026'
    : 'stopping\u2026';
  try { await api('POST', '/api/serve/power', {action}); }
  catch (e) {
    stat.textContent = 'Request failed: ' + (e.message || e);
    btns.forEach(b => b.disabled = false);
    return;
  }
  if (action === 'stop'){
    stat.textContent =
      'Dashboard stopped - start it again with `python conductor.py serve`.';
    return;
  }
  for (;;){
    await new Promise(r => setTimeout(r, 1000));
    try {
      const d = await api('GET', '/api/state');
      if (d && d.serve_boot !== BOOT){ location.reload(); return; }
    } catch (e) { /* down between processes - keep polling */ }
  }
}
function _closeDetailNow(keepStack){
  // closing the questionnaire mid-way dismisses the auto-popup for THIS
  // question set - the request's Answer button still re-opens it, and a
  // fresh question round pops again (its qids differ)
  if (modalQA && modalQA.rec)
    qDismissed.add(qSig(modalQA.id, modalQA.rec.pending_questions));
  detailIdx = null; detailKey = null; modalRequest = null; modalQA = null;
  runModalKey = null; summaryView = null; detailHTML = '';
  modalRunKey = null;
  if (!keepStack){ modalHist.length = 0; modalScroll = null; }
  document.getElementById('backdrop').hidden = true;
  applySel();        // every row mark clears with the dialog it belongs to
  return true;
}
function closeDetail(force=false, keepStack=false){
  // synchronous unless a dirty request form needs the themed prompt -
  // then it returns a Promise<bool> instead of a bool. Async callers
  // `await` either shape; sync callers route through closeThen, which
  // re-runs the would-be opener after a confirm (the retry's own
  // closeDetail sees modalRequest already cleared and takes the
  // synchronous path)
  if (!force && modalRequest?.dirty)
    return askConfirm('Discard unsaved edits?').then(ok =>
      ok ? _closeDetailNow(keepStack) : false);
  return _closeDetailNow(keepStack);
}
function closeThen(c, retry){
  if (c === true) return true;
  if (c && typeof c.then === 'function')
    c.then(ok => { if (ok) retry(); });
  return false;
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
      showDetail(j, true);          // sibling nav keeps the Back stack
      const d = document.getElementById('detail');
      if (d) d.scrollTop = 0;      // land on the task top, not mid-scroll
    }
  }
}
// The run modal's prev/next walks exactly the visible RUNS table: same
// run/status/facet filters, same active-then-finished ordering, same
// runsShown cap - arrowing can never land on a row the table isn't
// showing.
const runSibs = () => {
  const d = lastData;
  if (!d) return [];
  const matchAgent = a => (!only.size||only.has(a.status)) &&
    (!projs.size||projs.has(a.slug||'?')) &&
    [...facets].every(f=>matchesFacet(a,f));
  const allRuns = d.runs
    .filter(r=>!selectedRun||runKey(r)===selectedRun)
    .filter(r=>{
      if (!only.size && !facets.size && !projs.size) return true;
      const kids = d.agents.filter(
        a=>(a.run_key||a.run_dir)===runKey(r));
      return kids.length ? kids.some(matchAgent) : false;
    });
  const act = allRuns.filter(r=>!r.ended_at);
  return act.concat(allRuns.filter(r=>r.ended_at)
    .slice(0, Math.max(runsShown - act.length, 0)));
};
function navRun(dir){
  const sibs = runSibs();
  const pos = sibs.findIndex(r=>runKey(r)===runModalKey);
  const b = pos>=0 && sibs[pos+dir];
  if (b){
    showRunDetails(runKey(b), true);   // sibling nav keeps the Back stack
    const d = document.getElementById('detail');
    if (d) d.scrollTop = 0;            // land on the run top, not mid-scroll
  }
}
// --- modal history (Back) ----------------------------------------------
// Modal-to-modal jumps (task -> its request/summary, request -> its run)
// snapshot the view they leave so a Back button can restore that exact
// view - same task key, same scroll offset, filters untouched (they live
// outside the modal). Sibling walks and plain closes do NOT stack, and
// Escape / Close still tear the whole dialog down.
function snapshotModal(){
  const d=document.getElementById('detail');
  const scroll=(d&&d.scrollTop)||0;
  if (summaryView) return {kind:'summary',key:summaryView.key,scroll};
  if (modalQA) return null;          // the questionnaire has no back target
  if (modalRequest && modalRequest.id)
    return {kind:'request',id:modalRequest.id,scroll};
  if (detailKey!=null) return {kind:'agent',key:detailKey,scroll};
  if (runModalKey) return {kind:'run',key:runModalKey,scroll};
  return null;
}
function pushHist(snap){
  const last=modalHist[modalHist.length-1];
  if (last && last.kind===snap.kind &&
      (snap.key||snap.id)===(last.key||last.id)){
    last.scroll=snap.scroll; return;   // re-entering the same view refreshes
  }
  modalHist.push(snap);
  if (modalHist.length>20) modalHist.shift();
}
function histLabel(snap){
  if (snap.kind==='summary' && String(snap.key).startsWith('run|')){
    const r=(lastData?.runs||[]).find(x=>runKey(x)===snap.key.slice(4));
    return 'summary: '+(r?r.run:'run');
  }
  if (snap.kind==='agent'||snap.kind==='summary'){
    const a=AGENTS.find(x=>agentKey(x)===snap.key);
    return (snap.kind==='summary'?'summary: ':'')+(a?a.step:'task');
  }
  if (snap.kind==='run'){
    const r=(lastData?.runs||[]).find(x=>runKey(x)===snap.key);
    return 'run '+(r?r.run:snap.key);
  }
  return 'request';
}
function backBtnHTML(){
  const top=modalHist[modalHist.length-1];
  if (!top) return '';
  const lbl=histLabel(top);
  // 'Back' only - the destination stays on the tooltip so the bar's
  // free space belongs to the position counter + nav arrows
  return `<button type="button" class="dnav-btn dnav-back" ` +
    `data-modal-back title="return to ${esc(lbl)}">&larr; Back</button>`;
}
function applyModalScroll(){
  if (modalScroll==null) return;
  const d=document.getElementById('detail');
  if (d) d.scrollTop=modalScroll;
  modalScroll=null;
}
function modalBack(){
  if (modalRequest?.dirty){
    askConfirm('Discard unsaved edits?').then(ok => {
      if (ok){ modalRequest=null; modalBack(); }
    });
    return;
  }
  while (modalHist.length){
    const top=modalHist.pop();
    // the open*() calls below run their own closeDetail(keepStack) -
    // clear this view's markers first so its dirty flag can't re-prompt
    detailIdx=null; detailKey=null; modalRequest=null; modalQA=null;
    runModalKey=null; summaryView=null;
    modalScroll=top.scroll;
    if (top.kind==='agent'){
      const j=AGENTS.findIndex(x=>agentKey(x)===top.key);
      if (j<0) continue;               // row vanished - try the older entry
      showDetail(j,true); return;
    }
    if (top.kind==='run'){
      if (!(lastData?.runs||[]).some(x=>runKey(x)===top.key)) continue;
      showRunDetails(top.key,true); return;
    }
    if (top.kind==='request'){ openRequest(top.id,true); return; }
    if (top.kind==='summary'){ openSummary(top.key,false); return; }
  }
  modalScroll=null;
  closeDetail(true);                   // history exhausted - nothing to show
}
// --- in-app session summary ---------------------------------------------
// The task modal's 'Session summary' opens the report in THIS modal now,
// not a separate browser window: the server hands back just the body
// markup (?frag=1 - same escaping as the standalone page), and prev/next
// walk the same pipeSibs chain the task nav uses. The link keeps its href
// + target=_blank so the /summary URL stays usable (copy link, middle or
// ctrl-click opens the standalone page).
// finished-step summaries are immutable - cache the fetched fragment so
// prev/next walks and revisits render in one paint instead of the
// loading-frame/fetch/reflash cycle that made the view flicker. Errors
// are never cached (a retry is free), live steps always re-fetch, and
// neighbors are prefetched after a successful open.
const sumCache = {};
const sumKeyOf = a => 's|'+base(a.run_dir||'')+'|'+a.step;
async function openSummary(key, push=true){
  if (String(key).startsWith('run|'))
    return openRunSummary(key.slice(4), push);   // run-level summary view
  const a=AGENTS.find(x=>agentKey(x)===key);
  if (!a) return;
  // the bookkeeping row's "session summary" IS the run summary - the
  // summarizer has no per-step report of its own
  if (a.aux) return openRunSummary(a.run_key||a.run_dir, push);
  const snap = push ? snapshotModal() : null;
  // already inside the summary view? sibling nav swaps the content in
  // place - no close/open, so the backdrop never blinks
  if (!summaryView && !(await closeDetail(false, true))) return;
  if (snap && !(snap.kind==='summary'&&snap.key===key)) pushHist(snap);
  summaryView={key};
  if (modalScroll==null) modalScroll=0;    // fresh open lands at the top;
                                         // a Back restore carries its own
  const hit = a.run_dir && !a.alive && sumCache[sumKeyOf(a)];
  if (hit){
    renderSummaryFrame(a, hit.frag, hit.err);
    document.getElementById('backdrop').hidden=false;
    applySel();
    return;
  }
  renderSummaryFrame(a, null, null);       // loading frame, nav works already
  document.getElementById('backdrop').hidden=false;
  applySel();          // the summary view keeps the task's row marked
  let frag=null, err=null;
  if (!a.run_dir){
    err='No session summary for this step yet - it has not run.';
  } else {
    try{
      const res=await fetch('/api/runs/'+encodeURIComponent(base(a.run_dir))+
        '/steps/'+encodeURIComponent(a.step)+'/summary?frag=1');
      if (res.ok) frag=await res.text();
      else { const o=await res.json().catch(()=>({}));
             err='Summary unavailable: '+(o.error||('HTTP '+res.status)); }
    }catch(e){ err='Summary unavailable: '+(e.message||e); }
  }
  if (!summaryView || summaryView.key!==key) return;  // moved on meanwhile
  if (frag!=null && !a.alive) sumCache[sumKeyOf(a)]={frag};
  renderSummaryFrame(a, frag, err);
  prefetchSibs(a);
}
// warm the cache one step to each side so the next arrow press is a
// single paint even on a first visit
function prefetchSibs(a){
  const sibs=pipeSibs(a);
  const pos=sibs.findIndex(x=>agentKey(x)===summaryView.key);
  for (const b of [sibs[pos-1], sibs[pos+1]]){
    if (!b || b.aux || !b.run_dir || b.alive) continue;
    const ck=sumKeyOf(b);
    if (sumCache[ck]) continue;
    fetch('/api/runs/'+encodeURIComponent(base(b.run_dir))+
      '/steps/'+encodeURIComponent(b.step)+'/summary?frag=1')
      .then(r=>r.ok?r.text():null)
      .then(t=>{ if (t!=null) sumCache[ck]={frag:t}; })
      .catch(()=>{});
  }
}
function renderSummaryFrame(a, frag, err){
  // same chrome as the task modal's .dnav, but the summary view's
  // buttons carry arrows only (names live on the tooltips) and the
  // counter centers on the bar
  const sibs=pipeSibs(a);
  const pos=summaryView ? sibs.findIndex(x=>agentKey(x)===summaryView.key) : -1;
  const sbtn=(b,dir)=> b
    ? `<button type="button" class="dnav-btn" ` +
      `data-snav="${esc(agentKey(b))}" title="summary: ${esc(b.step)}">` +
      (dir<0?'&larr;':'&rarr;') + `</button>`
    : `<button type="button" class="dnav-btn" disabled>` +
      (dir<0?'&larr;':'&rarr;') + `</button>`;
  const bar = (modalHist.length || (pos>=0 && sibs.length>1))
    ? `<div class="dnav"><span class="dnav-l">${backBtnHTML()}</span>` +
      (pos>=0 && sibs.length>1
        ? `<span class="dnav-pos">task ${pos+1} of ${sibs.length}` +
          `</span><span class="dnav-r">${sbtn(sibs[pos-1],-1)}` +
          `${sbtn(sibs[pos+1],1)}</span>` : '') +
      `</div>` : '';
  const runKey_ = a.run_key||a.run_dir;
  // the same per-task review mark the task modal's Actions zone owns -
  // surfaced here too since reading the summary IS the review
  const revBtn = (!a.aux && a.run_dir)
    ? `<div class="actions" style="margin-bottom:8px">` +
      `<button type="button" ` +
      `class="btn${a.needs_review?'':' secondary'} sum-review" ` +
      `data-review-step="${esc(base(a.run_dir))}" ` +
      `data-step="${esc(a.step)}" ` +
      `data-review-set="${a.needs_review?1:0}" ` +
      `data-review-seq="${a.completion_seq??''}" ` +
      `title="a private 'you looked at this' flag - it changes nothing ` +
      `about the outcome and never marks the run itself">` +
      `${a.needs_review?'Mark as reviewed':'Mark as needing review'}` +
      `</button><span class="sub">a personal review mark for this ` +
      `task - the run keeps its own.</span></div>` : '';
  document.getElementById('dbody').innerHTML =
    bar +
    `<h3 id="modal-title" class="sumtitle">Session summary - ` +
    `${esc(a.slug)} / ${esc(a.step)}</h3>` +
    (runKey_ ? `<div class="sub" style="margin-bottom:8px">run ` +
      `<button type="button" class="linkbtn" ` +
      `data-run-details="${esc(runKey_)}">${esc(a.run||runKey_)}` +
      `</button></div>` : '') +
    (err!=null ? `<div class="notice error">${esc(err)}</div>`
      : frag==null ? `<div class="notice">Loading summary&hellip;</div>`
      : `<div class="sumdoc">${frag}</div>`) +
    // the mark sits under the document: read first, then acknowledge
    revBtn;
  // the loading frame consumed modalScroll already, so a Back restore
  // keeps its offset through the async fetch below
  applyModalScroll();
}
// The run modal's 'Run summary' opens the whole-run report in the same
// modal: the LLM-written run.summary.md when a summarizer session has
// produced one, otherwise the deterministic roll-up of each step's own
// closing report. summaryView keys for runs are 'run|'+runKey so the
// Back stack can tell them apart from per-step views.
async function openRunSummary(key, push=true){
  const r=(lastData?.runs||[]).find(x=>runKey(x)===key);
  if (!r || r.stage) return;
  const snap = push ? snapshotModal() : null;
  if (!(await closeDetail(false, true))) return;
  if (snap && !(snap.kind==='summary'&&snap.key==='run|'+key))
    pushHist(snap);
  summaryView={key:'run|'+key};
  if (modalScroll==null) modalScroll=0;
  const bar = modalHist.length
    ? `<div class="dnav"><span class="dnav-l">${backBtnHTML()}</span>` +
      `</div>` : '';
  const head=`<h3 id="modal-title" class="sumtitle">Run summary - ` +
    `${esc(r.run||key)}</h3>`;
  document.getElementById('dbody').innerHTML =
    bar + head + `<div class="notice">Loading summary&hellip;</div>`;
  document.getElementById('backdrop').hidden=false;
  applySel();          // the run summary keeps the run's row marked
  let frag=null, err=null;
  try{
    const res=await fetch('/api/runs/'+encodeURIComponent(base(r.dir))+
      '/summary?frag=1');
    if (res.ok) frag=await res.text();
    else { const o=await res.json().catch(()=>({}));
           err='Summary unavailable: '+(o.error||('HTTP '+res.status)); }
  }catch(e){ err='Summary unavailable: '+(e.message||e); }
  if (!summaryView || summaryView.key!=='run|'+key) return;
  document.getElementById('dbody').innerHTML =
    bar + head +
    (r.summarizing?`<div class="notice">A summary session is running - ` +
      `reopen this view when it finishes; the deterministic roll-up ` +
      `below is the interim answer.</div>`:'') +
    (err!=null ? `<div class="notice error">${esc(err)}</div>`
      : `<div class="sumdoc">${frag}</div>`);
  decorateRunSum(document.querySelector('#detail .sumdoc'), key);
  applyModalScroll();
}
// A run summary's '<step> - <status>' headings are live controls, not
// prose: the step name (rendered in its canonical lowercase, even when
// an LLM summary shouted it) opens the task modal via the existing
// data-agent-detail hook - Back returns here - the status word takes
// the .stc color, and a review link toggles the same flag the task
// modal owns; a reviewed step's heading strikes through. The decorated
// step is remembered on the heading's dataset so the refresh pass can
// rebuild it from live state without re-parsing mutated text.
function decorateRunSum(root, rkey){
  if (!root || !root.querySelectorAll) return;
  const byName = {};
  for (const a of AGENTS)
    if (!a.aux && (a.run_key||a.run_dir||'')===rkey)
      byName[String(a.step).toLowerCase()] = a;
  root.querySelectorAll('h2,h3,h4').forEach(h=>{
    if (!h.dataset) return;
    if (!h.dataset.sumstep){
      const m = (h.textContent||'').match(
        /^\s*([A-Za-z0-9_.-]+)\s*[-\u2013\u2014]\s*([A-Za-z_?]+)\s*$/);
      if (!m) return;
      if (!byName[m[1].toLowerCase()]) return;
      if (m[2]!=='?' && !(m[2].toLowerCase() in STCLASS)) return;
      h.dataset.sumstep = m[1].toLowerCase();
      h.dataset.sumstat = m[2].toLowerCase();
    }
    const a = byName[h.dataset.sumstep];
    if (!a) return;
    const st = h.dataset.sumstat;
    const rev = !!(a.review && a.review.reviewed===true &&
                   !a.needs_review);
    if (h.classList) h.classList.toggle('sumh-reviewed', rev);
    const rlab = a.needs_review ? 'mark as reviewed'
                                : 'mark as needing review';
    h.innerHTML =
      `<span class="sumh-text"><button type="button" ` +
      `class="linkbtn sumh-name" ` +
      `data-agent-detail="${esc(agentKey(a))}" ` +
      `title="open this task's details">${esc(a.step)}</button> - ` +
      `<span class="stc st-${STCLASS[st]||'other'}">${esc(st)}</span>` +
      `</span>` +
      (a.run_dir
        ? ` <button type="button" class="linkbtn sumh-rev" ` +
          `data-review-step="${esc(base(a.run_dir))}" ` +
          `data-step="${esc(a.step)}" ` +
          `data-review-set="${a.needs_review?1:0}" ` +
          `data-review-seq="${a.completion_seq??''}" ` +
          `title="a private 'you looked at this' flag for this task">` +
          `${rlab}</button>` : '');
  });
}
// Summary views are painted once and don't join the refresh repaint -
// but the review state they surface is live, so each payload change
// re-decorates in place: step headings in a run summary, the review
// button in a session summary.
function refreshSummaryMarks(){
  if (!summaryView || document.getElementById('backdrop').hidden) return;
  const k = String(summaryView.key);
  if (k.startsWith('run|')){
    decorateRunSum(document.querySelector('#detail .sumdoc'),
                   k.slice(4));
    return;
  }
  const a = AGENTS.find(x=>agentKey(x)===k);
  const rb = a && document.querySelector('#detail .sum-review');
  if (a && rb && rb.dataset){
    rb.disabled = false;
    rb.dataset.reviewSet = a.needs_review?'1':'0';
    rb.dataset.reviewSeq = a.completion_seq??'';
    rb.textContent = a.needs_review
      ? 'Mark as reviewed' : 'Mark as needing review';
    rb.className = 'btn'+(a.needs_review?'':' secondary')+' sum-review';
  }
}
function summaryNav(d){
  const a=AGENTS.find(x=>agentKey(x)===(summaryView&&summaryView.key));
  if (!a) return;
  const sibs=pipeSibs(a);
  const pos=sibs.findIndex(x=>agentKey(x)===summaryView.key);
  const b=pos>=0 && sibs[pos+d];
  if (b) openSummary(agentKey(b), false);   // sibling nav keeps the Back stack
}
// 'Interrupt + instruct' composer inside the task modal. Opening it only
// reveals a section - nothing reaches the server until the explicit
// 'Interrupt + resume' confirm, and a step that ends while it sits open
// degrades to a tombstone (the draft stays visible so nothing is lost).
// After sending, the section follows the transition's phases off the
// agent row's intr_pending flag and interrupts counter.
function intrComposerHTML(a){
  const key = agentKey(a);
  if (!intrOpen[key]) return '';
  const sent = intrSent[key];
  const draft = intrDrafts[key];
  const list = intrAttach[key]||[];
  const thumbs = editable =>
    `<div id="d-instr-attach" class="attach-strip" aria-live="polite" ` +
    `aria-label="Attached images"${list.length?'':' hidden'}>` +
    list.map(x=>
      `<span class="attach-item"><a href="/api/attachments/` +
      `${encodeURIComponent(x.name)}" target="_blank" ` +
      `title="${esc(x.path)}"><img src="/api/attachments/` +
      `${encodeURIComponent(x.name)}" alt="${esc(x.name)}"></a>` +
      (editable?`<button type="button" class="attach-x" ` +
        `data-attach-rm="${esc(x.name)}" ` +
        `title="Remove this attachment">×</button>`:'') +
      `</span>`).join('') + `</div>`;
  if (sent){
    // post-submit progress: the request sentinel + the record's respawn
    // counter name the phase the driver is in
    let msg;
    if (a.intr_pending)
      msg='Interrupt requested - the driver kills the worker on its ' +
        'next flag poll (seconds), commits its work, then resumes this ' +
        'same step with your instructions.';
    else if ((a.interrupts||0) > (sent.at||0))
      msg='Done - the worker was stopped and this step resumed with ' +
        'your instructions (interrupt '+a.interrupts+').';
    else if (a.status!=='running')
      msg='The step ended ('+a.status+') - check its note for whether ' +
        'the interrupt applied or was banked for the next dispatch.';
    else
      msg='Worker stopped - resuming this step with your ' +
        'instructions…';
    return `<h3 style="margin-top:14px">Interrupt + instruct</h3>` +
      `<div class="notice${a.status==='running' ? '' : ' warn'}">` +
      `${esc(msg)}</div>` + thumbs(false) +
      `<div class="actions"><button type="button" ` +
      `class="btn secondary" data-intr-cancel>Close</button></div>`;
  }
  if (a.status!=='running'){
    return `<h3 style="margin-top:14px">Interrupt + instruct</h3>` +
      `<div class="notice warn">This step finished (${esc(a.status)}) ` +
      `while the composer was open - nothing was sent and the worker ` +
      `was never touched.</div>` +
      (draft!==undefined
        ? `<textarea disabled style="min-height:70px">${esc(draft)}` +
          `</textarea>` : '') +
      `<div class="actions"><button type="button" ` +
      `class="btn secondary" data-intr-cancel>Close</button></div>`;
  }
  return `<h3 style="margin-top:14px">Interrupt + instruct</h3>` +
    `<div class="notice">Stops this step's worker, then immediately ` +
    `resumes the SAME step in a fresh session - committed work and the ` +
    `worktree stay, the run keeps going, and your text lands under ` +
    `'NOTE FROM THE OPERATOR' on the new prompt. Use Stop this step ` +
    `instead if the step should just end.</div>` +
    `<textarea id="d-instr" style="min-height:90px" spellcheck="false" ` +
    `placeholder="additional instructions for the resumed session…">` +
    `${esc(draft||'')}</textarea>` + thumbs(true) +
    `<div class="sub attach-row"><button type="button" ` +
    `class="attach-btn" data-attach-pick="d-instr-file" ` +
    `aria-label="Choose image files to attach" ` +
    `title="Attach screenshots - they reach the resumed session as ` +
    `image paths">` +
    `<svg viewBox="0 0 24 24" width="14" height="14" fill="none" ` +
    `stroke="currentColor" stroke-width="2" stroke-linecap="round" ` +
    `stroke-linejoin="round" aria-hidden="true" focusable="false">` +
    `<path d="m21.44 11.05-9.19 9.19a6 6 0 0 1-8.49-8.49l8.57-8.57` +
    `A4 4 0 1 1 18 8.84l-8.59 8.57a2 2 0 0 1-2.83-2.83l8.49-8.48"/>` +
    `</svg></button>` +
    `<span>Optional screenshots - pick files, drop, or paste. They ` +
    `upload to data/attachments and reach the resumed session as ` +
    `paths.</span></div>` +
    `<input type="file" id="d-instr-file" accept="image/*" multiple ` +
    `hidden>` +
    `<div id="d-instr-msg" class="notice" hidden></div>` +
    `<div class="actions">` +
    `<button type="button" class="btn" ` +
    `data-intr-send="${esc(base(a.run_dir))}" ` +
    `data-step="${esc(a.step)}"${a.intr_pending ? ' disabled' : ''}>` +
    `${a.intr_pending ? 'Interrupt pending…' : 'Interrupt + resume'}` +
    `</button>` +
    `<button type="button" class="btn secondary" data-intr-cancel>` +
    `Cancel</button>` +
    `<span class="sub">the click confirms the request - the worker is ` +
    `killed on the driver's next flag poll and the same step resumes ` +
    `with these instructions.</span></div>`;
}
// --- task/run history feed ----------------------------------------------
// GET /api/runs/<dir>/events merges the driver's events.jsonl with the
// dashboard's own action log; fetched lazily per open modal and re-read
// at most every ~4s while the modal stays open.
const histCache = {};   // run-dir name -> {at:ms, events:[..]|null}
async function loadHist(dir){
  try {
    const d = await api('GET',
      '/api/runs/'+encodeURIComponent(dir)+'/events');
    histCache[dir] = {at:Date.now(), events:d.events||[]};
  } catch (e) {
    histCache[dir] = {at:Date.now(), events:[]};
  }
  // repaint whichever modal is open so fresh rows land without a
  // re-open (loadHist is also fired by the 2s refresh via histFor)
  if (detailIdx!=null && AGENTS[detailIdx] &&
      base(AGENTS[detailIdx].run_dir||'')===dir)
    showDetail(detailIdx, true);
  else if (modalRunKey!=null){
    const r = lastData && lastData.runs.find(x=>runKey(x)===modalRunKey);
    if (r && base(r.dir)===dir) showRunDetails(modalRunKey);
  }
}
function histFor(dir){
  const c = histCache[dir];
  if (!c){ histCache[dir] = {at:0, events:null}; loadHist(dir); return null; }
  if (c.events && Date.now()-c.at > 4000) loadHist(dir);
  return c.events;
}
const EV_HIDE = new Set(['ts','event','src','by','step','run']);
function evDetail(e){
  const bits = [];
  for (const k of ['status','minutes','chars','preview','reviewed',
      'attachments','live','pid','how','why','reason','model','branch',
      'session','exit_code','elapsed_s','slot','waited_s','drain'])
    if (e[k]!=null && e[k]!=='' && e[k]!==false)
      bits.push(k+' '+(k==='elapsed_s'||k==='waited_s'?fmtS(e[k]):e[k]));
  if (e.steps) bits.push('steps '+e.steps.join(', '));
  if (e.attempts) bits.push(e.attempts+' attempt(s)');
  return bits.join(' · ');
}
function evRow(e){
  return `<div class="evrow"><span class="evtime">` +
    `${e.ts?loc(e.ts):''}</span>` +
    `<span class="evtag${e.src==='ui'?' ui':''}">` +
    `${e.src==='ui'?'you':'run'}</span>` +
    `<span class="evtxt"><b>${esc(String(e.event||'event')
      .replace(/_/g,' '))}</b>` +
    (e.step?` <span class="sub">${esc(e.step)}</span>`:'') +
    (evDetail(e)?` <span class="evd">${esc(evDetail(e))}</span>`:'') +
    `</span></div>`;
}
function histBlock(dir, step){
  const evs = dir ? histFor(dir) : [];
  const head = `<h3 style="margin-top:14px">history</h3>`;
  if (evs===null)
    return head + `<div class="evlog"><div class="evempty">` +
      `loading history&hellip;</div></div>`;
  const rows = step ? evs.filter(e=>e.step===step || !e.step) : evs;
  return head +
    (rows.length
      ? `<div class="evlog">` +
        rows.slice().reverse().map(evRow).join('') + `</div>`
      : `<div class="evlog"><div class="evempty">` +
        `no events recorded</div></div>`);
}
function showDetail(i, keepStack){
  const a = AGENTS[i]; if(!a) return;
  const key = agentKey(a);
  // Repaint in place while THIS task's view is already on screen - the
  // refresh tick re-enters here constantly during live work, and
  // routing it through closeDetail would hide+reshow the dialog and
  // churn every piece of view state for nothing.
  const sameView = detailKey===key && !modalRequest && !modalQA &&
    !runModalKey && !summaryView &&
    !document.getElementById('backdrop').hidden;
  if (!sameView && !closeThen(closeDetail(false, !!keepStack),
      ()=>showDetail(i, keepStack))) return;
  detailIdx = i; detailKey = key;
  const pk = detailKey;
  const pd = promptDrafts[pk];   // unsaved edit (undefined = none open)
  const nd = notesDrafts[pk];
  // status word colored like the table's chips; any suffix (e.g.
  // ' - ended without the STATUS: line') stays default
  const statusHTML = v => {
    const s = String(v ?? '-'), m = s.match(/^[a-z_]+/);
    if (!m) return esc(s);
    const cls = STCLASS[m[0]] || 'other';
    return `<span class="stc st-${cls}">${esc(m[0])}</span>` +
           esc(s.slice(m[0].length));
  };
  const runKey_ = a.run_key||a.run_dir;
  // Follow-up links both ways: a task inside a run that DRAFTED one
  // links forward to the proposal; a task inside the follow-up run
  // links back to the run that drafted it
  const ownRun = (lastData?.runs||[]).find(x=>runKey(x)===runKey_) || {};
  const ownFu = ownRun.followup, ownSrc = ownRun.followup_src;
  const kv = [['step',a.step],['run',a.run],
    ...(ownSrc ? [['follow-up of',
      `<button type="button" class="linkbtn" ` +
      `data-run-details="${esc(ownSrc.key || ownSrc.run)}">` +
      `${esc(ownSrc.run || '?')}</button>`]] : []),
    ...(ownFu ? [['follow-up',
      (ownFu.request
        ? `<button type="button" class="linkbtn" ` +
          `data-open-request="${esc(ownFu.request)}">` +
          `open the follow-up proposal</button>`
        : `<span class="sub">${esc(ownFu.status || 'recorded')}` +
          ` - no proposal linked</span>`)]] : []),
    ['project',a.slug],
    ['status',a.status+(a.status==='failed'&&a.exit_code===0&&
      a.status_line==null?' - ended without the STATUS: line':'')+
      (a.status==='running'&&a.reported
        ?' - worker reported '+a.reported:'')],
    ...(a.depends_on && a.depends_on.length
      ? [['depends on', a.depends_on.map(dn =>
          a.dep_wait && a.dep_wait.includes(dn) ? dn+' (waiting)' : dn
        ).join(', ')]]
      : []),
    ['review',reviewLine(a)],
    ['task prompt',a.task_full||a.task],
    // The worker-reported OUTCOME: line - the human-facing "what it did".
    // Honest empty state for records that predate the contract or ended
    // before the report; the NEXT: line is a handoff, never a stand-in.
    ['outcome',a.outcome],
    ...((a.outcomes||[]).filter(e=>e&&e.outcome&&e.outcome!==a.outcome)
      .map(e=>['earlier',`attempt ${e.attempt||'?'} (${e.status||'?'}` +
        `${e.ended_at?', '+String(e.ended_at).slice(0,16):''}): ` +
        `${e.outcome}`])),
    ['next',a.next],['note',a.note],['log tail',a.tail_last],
    ['model',(a.model||'-')+(a.effort?' (effort '+a.effort+')':'')+
      (a.model_actual?' → '+a.model_actual:'')],
    ['mode',(a.mode||'-')+(a.cloud?' cloud':'')],
    // cumulative over every dispatch's execution interval - the live
    // one included; 'started' keeps the ORIGINAL dispatch, with the
    // latest one parenthesized when a resume restamped it
    ['started',(a.first_started_at && a.started_at &&
      a.first_started_at!==a.started_at)
      ? a.first_started_at+' (latest dispatch '+a.started_at+')'
      : a.started_at],
    ['ended',a.ended_at],
    ['elapsed',elapsed(a, Date.now())],
    ['timeout',a.timeout_min!=null?a.timeout_min+' min'+
      (a.timeout_ext_s?` (+${fmtS(a.timeout_ext_s)} active past it)`:
      ''):'-'],
    ['attempt',a.attempts!=null?`${a.attempts} of ${a.max_attempts||'?'}`:'-'],
    ['interrupts',a.interrupts
      ? a.interrupts+'x (the operator stopped + resumed it with added ' +
        'instructions)' : '-'],
    ['tokens',tokTitle(a.tokens)||'-'],
    ['est cost',(fmtC(a.cost)!=='-'?fmtC(a.cost)+' - ':'')+costTitle(a.cost)],
    ['session',(a.session_id||'-')+(a.session_gone?' (deleted from sidebar)':'')],
    ['verify',a.verify_ok==null?'-':(a.verify_ok?'pass':'FAIL')],
    ['exit',a.exit_code],['work dir',a.work_dir],
    ['service',a.service_url?a.service_url+(a.alive?' (leased to this step)':' (released)'):'-'],
    ['branch',a.branch],['device',a.device||'-'],
    ['merged',a.merged===true?'yes'+(a.merged_at?' @ '+a.merged_at:'')
      :a.merged],
    ['merge target',a.merge_target],
    ['merge',a.deferred
      ? `deferred${a.merge_kind?` (${a.merge_kind})`:''} - held pending;` +
        ` target ${a.target_branch||'?'}; verified: ${
        a.deferred_verified==null?'-':(a.deferred_verified?'yes':'no')}`
      : 'auto - merges after its step/wave'],
    ['prompt file',a.prompt],['stdout',a.stdout_log],
    ['last update',a.log_mtime],
    ['export',a.export],['state file',a.state_md]];
  // A conductor-internal step (resolve-*) names its machine purpose - the
  // marker itself no longer renders inside the task text.
  if(a.purpose) kv.splice(4,0,['purpose',a.purpose]);
  // prev/next walks this step's own pipeline - sibling buttons name their
  // target, the counter gives position. Hidden for a lone/seq-less row.
  const sibs = pipeSibs(a);
  const pos = sibs.findIndex(x=>agentKey(x)===detailKey);
  const navBtn = (b,dir)=> b
    ? `<button type="button" class="dnav-btn nav" ` +
      `data-nav="${esc(agentKey(b))}" title="${esc(b.step)}">` +
      (dir<0 ? `&larr; <span>${esc(b.step)}</span>`
             : `<span>${esc(b.step)}</span> &rarr;`) + `</button>`
    : `<button type="button" class="dnav-btn nav" disabled>` +
      (dir<0 ? '&larr;' : '&rarr;') + `</button>`;
  const navBar = (pos>=0 && sibs.length>1)
    ? `<div class="dnav"><span class="dnav-l">${backBtnHTML()}</span>` +
      `<span class="dnav-pos">task ${pos+1} of ${sibs.length}</span>` +
      `<span class="dnav-r">${navBtn(sibs[pos-1],-1)}` +
      `${navBtn(sibs[pos+1],1)}</span></div>`
    : (modalHist.length
       ? `<div class="dnav"><span class="dnav-l">${backBtnHTML()}` +
         `</span></div>` : '');
  // the step name carries the summary link - the aux bookkeeping row's
  // report IS the run summary
  const sumKey = a.aux ? 'run|'+(a.run_key||base(a.run_dir))
                       : agentKey(a);
  // Every control that mutates the task or its run lives in ONE zone at
  // the end of the modal - review, lifecycle, prompt/notes edits, the
  // timeout editor - under one rule + header, IntelliJ-style. View-only
  // controls (trajectory expand/collapse) stay with their content.
  const actBits = (a.run_dir && !a.aux)
    ? (`<div class="actions"><button type="button" ` +
      `class="btn${a.needs_review?'':' secondary'}" ` +
      `data-review-step="${esc(base(a.run_dir))}" ` +
      `data-step="${esc(a.step)}" ` +
      `data-review-set="${a.needs_review?1:0}" ` +
      `data-review-seq="${a.completion_seq??''}" ` +
      `title="a private 'you looked at this' flag - it changes nothing ` +
      `about the outcome and never marks the run itself">` +
      `${a.needs_review?'Mark as reviewed':'Mark as needing review'}` +
      `</button><span class="sub">` +
      `a personal review mark for this task only - the run keeps its ` +
      `own mark, and a re-run completion flags it again.</span></div>` +
      (a.status==='running'
        ? `<div class="actions"><button type="button" class="btn danger" ` +
          `data-stop-agent="${esc(base(a.run_dir))}" ` +
          `data-step="${esc(a.step)}" ` +
          `title="kill this agent's process tree now - the run ` +
          `proceeds to the next step">Stop this step</button>` +
          `<span class="sub">tree-kills the ` +
          `devin process; committed work stays. The run continues; ` +
          `Resume re-runs the step.</span></div>` +
          `<div class="actions"><button type="button" ` +
          `class="btn secondary" ` +
          `data-intr-open="${esc(agentKey(a))}" ` +
          `title="stop the worker, then immediately resume this same ` +
          `step with added instructions/screenshots - committed work ` +
          `and the worktree stay; the run keeps going">` +
          `Interrupt + instruct</button>` +
          `<span class="sub">opens a composer ` +
          `first - the same step resumes with your text; nothing is ` +
          `skipped and the run does not pause.</span></div>` : '') +
      intrComposerHTML(a) +
      (((a.status==='queued'||a.status==='waiting') && !a.skipped_by_user)
        ? `<div class="actions"><button type="button" ` +
          `class="btn secondary" ` +
          `data-skip-step="${esc(base(a.run_dir))}" ` +
          `data-step="${esc(a.step)}" ` +
          `title="never launch this step while marked - it keeps its ` +
          `queue position and stays skipped (even across resume) ` +
          `until you Unskip">Skip step</button>` +
          `<span class="sub">the step records ` +
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
          `value="${a.timeout_min??''}" style="width:90px" ` +
          `data-timeout-orig="${a.timeout_min??''}">` +
          `<button type="button" class="btn secondary" ` +
          `id="d-timeout-save" disabled ` +
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
        // never dispatched -> additive pre-start instructions ('just
        // like interrupt+instruct, minus the interrupt'); re-running
        // -> the same channel labelled for the resume it feeds
        ? `<h3 style="margin-top:14px">` +
          (a.prompt_editable ? 'Additional instructions'
                             : 'Resume notes') + `</h3>` +
          `<textarea id="d-rnotes" style="min-height:70px" ` +
          `spellcheck="false" placeholder="` +
          (a.prompt_editable
            ? `extra context for the agent before this step starts`
            : `context for the resume, e.g. VPN is restored`) +
          `">${esc(nd!==undefined?nd:
            (a.resume_notes||''))}</textarea><div class="actions">` +
          `<button type="button" class="btn secondary" ` +
          `data-save-notes="${esc(base(a.run_dir))}" ` +
          `data-step="${esc(a.step)}">Save notes</button>` +
          `<span class="sub">appended to this step's ` +
          (a.prompt_editable ? 'first dispatched' : 'next dispatched') +
          ` prompt - the original prompt is never edited. Empty + ` +
          `Save clears stored notes.</span></div>` : '')) : '';
  const genBit = (a.request_id && a.step==='plan-proposal' &&
    a.status==='running'
    ? `<div class="actions"><button type="button" class="btn danger" ` +
      `data-stop-gen="${esc(a.request_id)}" ` +
      `title="cancel this planning pass - the request stays editable ` +
      `and any previous proposal is kept">Stop proposal</button></div>`
    : '');
  const actHtml = (actBits || genBit)
    ? `<div class="actzone"><div class="acth">Actions</div>` +
      actBits + genBit + `</div>` : '';
  const html =
    navBar +
    `<h3 id="modal-title">${esc(a.slug)} / ${esc(a.step)}` +
    (a.run_dir ? ` &middot; <button type="button" class="linkbtn" ` +
      `data-summary="${esc(sumKey)}" title="human-readable ` +
      `end-of-step report from the ATIF export - opens in this dialog ` +
      `with prev/next navigation">Summary</button>` : '') +
    `</h3><div class="kv">` +
    kv.map(([k,v])=>`<div class="k">${k}</div><div class="wrap">` +
      (k==='status' ? statusHTML(v)
        : k==='task prompt' ? `<pre class="taskpre">${esc(v??'-')}</pre>`
        : k.startsWith('follow-up')
        ? v    // the link/label above is already escaped markup
        : k==='run' && runKey_
        ? `<button type="button" class="linkbtn" ` +
          `data-run-details="${esc(runKey_)}">${esc(v??'-')}</button>`
        : k==='outcome' && !v
        ? `<span class="sub">not reported - the step predates the ` +
          `OUTCOME: line or ended before printing it</span>`
        : esc(v??'-')) + '</div>').join('') +
    `</div>` +
    (a.run_dir ? histBlock(base(a.run_dir), a.step) : '') +
    (a.tail ? `<h3 style="margin-top:14px">stdout tail</h3><pre id="dtail">${esc(a.tail)}</pre>` : '') +
    (a.arc && a.arc.groups && a.arc.groups.length
      ? `<h3 style="margin-top:14px">trajectory` +
        ` <span class="sub" style="margin:0">${a.arc.steps} LLM steps, ` +
        `grouped by phase - expand a row for per-step times & real ` +
        `per-turn tokens</span></h3>` +
        `<div class="actions" style="margin:2px 0 8px">` +
        `<button type="button" class="btn secondary" data-arc-all ` +
        `title="open every group and every step's full recorded text ` +
        `(fetched once from the ATIF export)">Expand all</button>` +
        `<button type="button" class="btn secondary" data-arc-none ` +
        `title="back to the compact grouped view">Collapse all</button>` +
        `<span class="sub">expanding a step reads its full text from ` +
        `the .export.json on demand - nothing is re-generated or ` +
        `estimated</span></div>` +
        `<div class="arc" data-akey="${esc(detailKey)}">` +
        a.arc.groups.map((g, gi) => {
          const gOpen = trajOpenFor(detailKey).g.has(gi);
          return `<details class="arcgrp" data-ag="${gi}"` +
          (gOpen ? ' open' : '') +
          `><summary><span class="arcspan">` +
          `${esc(g.span)}</span><span class="arcphase arc-` +
          `${esc(g.phase)}">${esc(g.phase)}</span>` +
          `<span class="arctime">${loc(g.t0)}</span>` +
          `<span class="arctok" title="billed in / out / cached ` +
          `(per-turn ATIF metrics, not estimates)">${g.tin!=null
            ? kfmt(Math.max(g.tin-(g.tcached||0),0))+'/'+
              kfmt(g.tout||0)+'/'+kfmt(g.tcached||0)
            : ''}</span>` +
          `<span class="archead">${esc(g.head)}</span></summary>` +
          `<div class="arcdetail">` + (g.entries||[]).map(e => {
            const eOpen = trajOpenFor(detailKey).e.has(String(e.sid));
            return `<details class="arcent" data-asid="${esc(e.sid)}"` +
            (eOpen ? ' open' : '') +
            `><summary><span class="arcspan">${esc(e.sid)}` +
            `</span><span class="arctime">${loc(e.ts)}</span>` +
            `<span class="arcdur">${e.dur_s!=null?fmtS(e.dur_s):''}` +
            `</span><span class="arctok" title="billed in / out / ` +
            `cached (full prompt = in+cached: ${e.in||'-'})">` +
            `${e.in!=null
              ? kfmt(Math.max(e.in-(e.cached||0),0))+'/'+
                kfmt(e.out||0)+'/'+kfmt(e.cached||0)
              : ''}</span>` +
            `<span class="archead" title="${esc(e.head)}">` +
            `${esc(e.head)}</span></summary>` +
            `<div class="arcfull">` +
            (eOpen ? arcFullHTML(a, e.sid) : '') +
            `</div></details>`;
          }).join('') + `</div></details>`;
        }).join('') +
        (a.arc.groups_total > a.arc.groups.length
          ? `<div class="arcnote">… ${a.arc.groups_total -
              a.arc.groups.length} later phase group(s) not shown ` +
            `(the list caps at ${a.arc.groups.length} - their full text ` +
            `is still in the export)</div>`
          : '') +
        `</div>` : '') +
    actHtml;
  if (sameView && html===detailHTML){
    // identical repaint (most refresh ticks while nothing visible
    // changed) - leave the live DOM alone: scroll offset, selection,
    // focus and hover all survive untouched
  } else {
    // real change: carry focus/caret and the scroll anchor across the
    // swap so the update doesn't eat in-progress input or jump the
    // viewport (see captureModalScroll for the anchor contract)
    const fcap = captureModalFocus();
    const scap = sameView ? captureModalScroll() : null;
    document.getElementById('dbody').innerHTML = html;
    detailHTML = html;
    restoreModalFocus(fcap);
    if (scap) restoreModalScroll(scap);
  }
  // Deliberate task switch policy: a fresh open lands at the top. The
  // hidden round-trip does NOT reset scrollTop in modern browsers, so
  // without this a different task would inherit the previous one's
  // position. A Back jump skips this - applyModalScroll restores its
  // own snapshot right below.
  if (!sameView && modalScroll==null){
    const dd = document.getElementById('detail');
    if (dd) dd.scrollTop = 0;
  }
  applyModalScroll();
  document.getElementById('backdrop').hidden = false;
  applySel();        // mark this task's row while its modal is open
}

// One source for "which lifecycle controls apply to this run right now" -
// the run-details modal and the row context menu both read it, so neither
// can drift into its own idea of what is currently allowed.
function runCtl(r){
  const key = runKey(r);
  const liveSteps = AGENTS.filter(a=>(a.run_key||a.run_dir)===key &&
    ['running','queued'].includes(a.status));
  const cks = Object.keys(r.counts||{});
  // 'executing' comes from the server (live driver pid or a live step
  // record) and stays true through the windows where no step currently
  // reads 'running' - between waves, queued under the cap, the
  // verify/merge window, and a still-finishing stop/pause. It gates
  // actions that are unsafe mid-run (manual merge); the POST re-checks
  // server-side so a stale page cannot slip through.
  const executing = !!r.executing || !!liveSteps.length;
  // 'superseded' counts as settled like 'done': it only ever labels an
  // aux resolve-* record whose parent's merge already landed, so it must
  // never keep Resume/interrupted offers alive on its own
  const unfinished = cks.some(s=>s!=='done' && s!=='superseded');
  return {key, liveSteps, executing,
    canResume: !!(cks.length &&
      !cks.some(s=>['running','queued'].includes(s)) &&
      unfinished),
    // worktree branches that never landed (conflict parked the worktree, or
    // the conductor died between the wave and the merge) - mergeable once
    // the run has actually stopped or finished
    pendingMerge: AGENTS.some(a=>a.branch && a.merged!==true &&
      (a.run_key||a.run_dir)===key &&
      !['running','queued'].includes(a.status)),
    // 'paused' means someone clicked Pause run (a graceful hold); a run
    // with no ended_at and nothing live means the conductor itself died.
    interrupted: !r.ended_at && !r.aborted && !liveSteps.length &&
      unfinished};
}
function showRunDetails(key, keepStack){
  const r = lastData?.runs.find(x=>runKey(x)===key);
  if (!r) return;
  // a non-null snapshot means a modal view is on screen - this is a
  // modal->modal jump, so the Back stack survives the closeDetail below
  const snap = keepStack ? null : snapshotModal();
  if (!closeThen(closeDetail(false, !!(keepStack || snap)),
      ()=>showRunDetails(key, keepStack))) return;
  if (snap && !(snap.kind==='run' && snap.key===key)) pushHist(snap);
  else if (snap && modalScroll==null)
    modalScroll = snap.scroll;  // same-view re-render keeps its offset
  runModalKey = key;
  const total = Object.values(r.counts||{}).reduce((s,n)=>s+n,0);
  const runUsd = (r.cost&&r.cost.usd!=null)||(r.plan_cost&&r.plan_cost.usd!=null)
    ? (r.cost&&r.cost.usd||0)+(r.plan_cost&&r.plan_cost.usd||0) : null;
  const kv = [['description',r.description||'-'],['projects',(r.projects||[]).join(', ')],
    ['started',r.started_at],['ended',r.ended_at],
    ['elapsed',runElapsed(r, Date.now())],
    // review marks live on run records only - a proposal/planner row is
    // preparation history, never a completion to ack
    ...(r.stage?[]:[['review',reviewLine(r)]]),
    // a run launched from a follow-up proposal links back to the run
    // that drafted it
    ...(r.followup_src ? [['follow-up of',
      `<button type="button" class="linkbtn" ` +
      `data-run-details="${esc(r.followup_src.key ||
        r.followup_src.run)}">${esc(r.followup_src.run || '?')}` +
      `</button>`]] : []),
    // the follow-up this run drafted, when the closing step left one -
    // the link opens the proposal (Back returns here; the proposal's own
    // row marks .dlinked while this modal shows)
    ...(r.followup ? [['follow-up',
      (r.followup.request
        ? `<button type="button" class="linkbtn" ` +
          `data-open-request="${esc(r.followup.request)}">` +
          `open the follow-up proposal</button>`
        : `<span class="sub">${esc(r.followup.status || 'recorded')}` +
          ` - no proposal linked</span>`)]] : []),
    ['steps',(total?`${total} total · `:'')+
      Object.entries(r.counts||{}).map(([s,n])=>`${n} ${s}`).join(', ')],
    ['tokens',tokTitle(r.tokens)||'-'],
    ['est cost',runUsd!=null?fmtC({usd:runUsd})+
      (r.cost&&r.cost.unpriced?` (+${r.cost.unpriced} step(s) unpriced)`:'')+
      ` (self-serve rates, plan + steps)`:'-'],
    ['manifest / dir',r.dir],
    ['resume log',r.resume_log],
    // sessions collapse to a one-line <details> - the full step:session
    // list is long and rarely needed (sessOpen[key] persists expansion
    // across refresh re-renders)
    ['sessions', (()=>{ const ss = AGENTS.filter(
        a=>(a.run_key||a.run_dir)===key && a.session_id);
      return ss.length
        ? `<details class="sessx"${sessOpen[key]?' open':''}>` +
          `<summary>${ss.length} session(s)</summary>` +
          `<div class="sessfull">` +
          ss.map(a=>`${esc(a.step)}: ${esc(a.session_id)}`).join(' \u00b7 ') +
          `</div></details>`
        : '-'; })()]];
  const ctl = runCtl(r);
  const canResume = ctl.canResume;
  const liveSteps = ctl.liveSteps;
  const interrupted = ctl.interrupted;
  const executing = ctl.executing;
  // the run's task list as links into each task's modal - pipeline seq
  // order, aux bookkeeping rows last and marked
  const tasks = AGENTS.filter(a=>(a.run_key||a.run_dir)===key)
    .slice().sort((x,y)=>((x.seq??1e9)-(y.seq??1e9)) ||
      String(x.started_at||'').localeCompare(String(y.started_at||'')));
  const taskTbl = tasks.length
    ? `<h3 style="margin-top:14px">tasks</h3>` +
      `<div class="wrapw"><table>` +
      `<tr><th>step</th><th>status</th><th>review</th><th>elapsed</th>` +
      `</tr>` +
      tasks.map(t=>`<tr>` +
        `<td><button type="button" class="linkbtn" ` +
        `data-agent-detail="${esc(agentKey(t))}">${esc(t.step)}` +
        `</button>${t.aux?' <span class="sub">aux</span>':''}</td>` +
        `<td><span style="color:${COLORS[t.status]||'#8b949e'}">` +
        `${esc(t.status)}</span></td>` +
        `<td>${t.needs_review ? unreviewedPill('this task')
          : (t.review && t.review.reviewed===true
            ? '<span class="sub">reviewed</span>'
            : '<span class="sub">-</span>')}</td>` +
        `<td>${elapsed(t, Date.now())}</td></tr>`).join('') +
      `</table></div>` : '';
  // Retained worktree work awaiting an explicit merge, straight from
  // state.json: deferred groups (one per project repo - recorded target,
  // baseline state, alternatives, per-entry verify) plus legacy
  // auto-policy branches that never landed (conflict parked them, or the
  // conductor died between the wave and the merge). Ticked entries
  // survive the re-render via mergeSel[key]; the POST sends just them.
  const sel = mergeSel[key] || (mergeSel[key] = new Set());
  const vchip = e =>
    ` <span class="chip" style="color:${
      e.verify_ok==null?'#8b949e':(e.verify_ok?'#3fb950':'#f85149')}" ` +
    `title="${e.verify_ok==null?'no verify gate was set':
      (e.verify_ok?'verify gate passed':'verify gate FAILED')}">` +
    `${e.verify_ok==null?'no verify':
      (e.verify_ok?'verified':'verify FAILED')}</span>`;
  const pmbox = (e, dis, altGrp, why) =>
    `<label class="pmentry"><input type="checkbox" data-pm="` +
    `${esc(e.step)}"` +
    (altGrp!=null?` data-pm-alt="${altGrp}"`:'') +
    `${sel.has(e.step)?' checked':''}${dis?' disabled':''}>` +
    `<span><b>${esc(e.step)}</b>` +
    ` <span class="chip" style="color:${COLORS[e.status]||'#8b949e'}">` +
    `${esc(e.status)}</span>` +
    // same chip the task rows carry for merged===false - every entry in
    // this panel is unmerged by definition, so say it the same way
    ` <span class="chip" style="color:#f0883e" ` +
    `title="this branch was never merged back - ticking it merges it ` +
    `now">unmerged</span>${vchip(e)}` +
    (e.kind==='alternative'
      ? ` <span class="chip" style="color:#d2a8ff" title="an ` +
        `alternative implementation from a same-project wave - merge ` +
        `alternatives one at a time (ticking one unticks the others); ` +
        `they are never combined silently">alternative</span>` : '') +
    `<br><span class="sub">${esc(e.branch||'')}` +
    `${e.note?' · '+esc(e.note):''}${why?` · ${esc(why)}`:''}` +
    `</span></span></label>`;
  const pmGroups = (r.pending_merges||[]).map((g,gi)=>
    `<div class="pmgrp"><div><b>${esc(g.slug)}</b> ` +
    `<span class="sub">merges into</span> ` +
    `<code>${esc(g.target||'?')}</code>` +
    (g.busy ? ` <span class="stage live" title="another process holds ` +
      `this repo's merge lock">repo busy - a merge is in flight</span>`
      : '') +
    ((g.resolving||[]).length ? ` <span class="stage live" ` +
      `title="a resolve-* session is finishing the staged merge">` +
      `resolving: ${esc(g.resolving.join(', '))}</span>` : '') + `</div>` +
    `<div class="sub">integration branch ` +
    `<code>${esc(g.baseline||'-')}</code>` +
    (g.baseline_stuck
      ? ` · <span style="color:#d29922">baseline diverged from the ` +
        `target - the staged merge reconciles it at land time</span>`
      : '') +
    (g.stage_kept ? ` · a previous attempt's staging worktree is kept ` +
      `at ${esc(g.stage_kept)} for inspection` : '') +
    (g.moved_reason ? ` · last land attempt refused: ` +
      `${esc(g.moved_reason)}` : '') + `</div>` +
    g.entries.map(e => pmbox(e, !e.verified || !!e.live,
      e.kind==='alternative' ? gi : null,
      e.live ? 'step still live - unticks are harmless, the merge '
        + 'is refused anyway' :
        (!e.verified ? 'not verified - the merge never lands it '
          + '(merge by hand if it is actually wanted)' : ''))).join('') +
    `</div>`).join('');
  const pmLegacy = (r.pending_branches||[]).length ? (() => {
    const bySlug = {};
    (r.pending_branches||[]).forEach(e =>
      (bySlug[e.slug||'?'] ??= []).push(e));
    return Object.entries(bySlug).map(([slug, es]) =>
      `<div class="pmgrp"><div><b>${esc(slug)}</b> ` +
      `<span class="sub">worktree branches parked by a merge conflict ` +
      `or an interrupted run - they merge into the project's checkout ` +
      `(no recorded target)</span></div>` +
      es.map(e => pmbox(e, !!e.live, null,
        e.live ? 'step still live' : '')).join('') + `</div>`).join('');
  })() : '';
  const pmCount = (r.pending_merges||[]).reduce(
    (s,g)=>s+g.entries.length,0) + (r.pending_branches||[]).length;
  // Pending merges is its own panel - finished work parked on worktree
  // branches, awaiting an explicit merge. The button belongs to the
  // panel, not the general run actions; it renders outline-only ('idle')
  // until something is ticked.
  const pmHtml = pmCount ? `<div class="runpanel">` +
    `<h3>Pending merges (${pmCount})</h3>` +
    `<div class="sub" style="margin-bottom:8px">finished work still ` +
    `sitting on worktree branches - tick entries to merge them into ` +
    `the target branch now, or ` +
    `<a href="#" data-pm-all="1" ` +
    `title="tick every mergeable entry - disabled (unverified/live) ` +
    `entries stay unticked and each alternative group keeps one ` +
    `pick">select all</a> · ` +
    `<a href="#" data-pm-clear="1" title="untick every entry">clear</a>. ` +
    `Unticked entries stay parked untouched; ` +
    `unverified entries are never merged.</div>` +
    (r.merge_busy ? `<div class="notice warn">A merge is in progress ` +
      `for this run - new clicks are ignored.</div>`:'') +
    pmGroups + pmLegacy +
    `<div class="actions"><button type="button" id="pm-go" ` +
    `class="btn${sel.size?'':' idle'}" ` +
    `data-merge-run="${esc(base(r.dir))}" ` +
    `data-rk="${esc(key)}"${(executing || r.merge_busy)
      ? ' disabled' : ''} ` +
    `title="${r.merge_busy
      ? 'a merge is already running for this run'
      : (executing
        ? 'unavailable while the run is still executing - it unlocks ' +
          'once the run has actually stopped or finished (a stop ' +
          'request needs a moment to land)'
        : 'merge the ticked pending branches into their target ' +
          'branches - verified entries are staged, re-verified and ' +
          'merged atomically; unticked entries stay parked')}">` +
    `Merge selected pending work</button>` +
    `<span class="sub" id="pm-msg" style="margin-left:8px"></span>` +
    `</div></div>` : '';
  // Run control is its own panel too: resume re-runs every non-done step
  // in a fresh session (parked branches get another merge attempt first)
  // - the heavier option compared to a merge-only pass.
  const ctlHtml = (liveSteps.length || canResume)
    ? `<div class="runpanel"><h3>Run control</h3>` +
      `<div class="sub" style="margin-bottom:8px">` +
      (liveSteps.length
        ? `the run is executing - pause lets in-flight steps finish, ` +
          `stop kills them now; committed work stays either way`
        : `resume re-runs every step that isn't done in a fresh ` +
          `session - parked branches get another merge attempt first, ` +
          `and a per-step note set from the task's own view reaches the ` +
          `new session (e.g. "VPN is back on")`) + `</div>` +
      `<div class="actions">` +
      (liveSteps.length ? `<button type="button" class="btn" ` +
        `data-pause-run="${esc(base(r.dir))}" ` +
        `title="let in-flight steps finish, then hold the run - nothing ` +
        `new launches until Resume">Pause run</button>` +
        `<button type="button" class="btn danger" ` +
        `data-stop-run="${esc(base(r.dir))}" ` +
        `title="kill every live step's process tree now - committed ` +
        `work stays; nothing new launches; Resume restarts unfinished ` +
        `steps">Stop run now</button>` : '') +
      (canResume ? `<button type="button" class="btn" ` +
        `data-resume-run="${esc(base(r.dir))}" ` +
        `title="re-run every non-done step in a fresh session - covers ` +
        `paused, stopped and interrupted runs">` +
        `Resume unfinished steps</button>` : '') +
      `</div></div>` : '';
  const notice = (r.aborted==='paused' && canResume)
    ? `<div class="notice warn">Paused - in-flight steps finished and ` +
      `nothing new launched. Resume continues where it left off ` +
      `(safe across a restart).</div>`
    : interrupted
    ? `<div class="notice warn">Interrupted - the conductor process is ` +
      `gone (crash or machine restart). Resume picks up unfinished ` +
      `steps from committed state.</div>` : '';
  // prev/next walks the visible RUNS table (runSibs), sibling-style:
  // buttons name their target run, the counter gives position, and the
  // walk never stacks history entries (Back still works once)
  const rsibs = runSibs();
  const rpos = rsibs.findIndex(x=>runKey(x)===key);
  const rnavBtn = (b,dir)=> b
    ? `<button type="button" class="dnav-btn nav" ` +
      `data-nav-run="${esc(runKey(b))}" title="${esc(b.run)}">` +
      (dir<0 ? `&larr; <span>${esc(b.run)}</span>`
             : `<span>${esc(b.run)}</span> &rarr;`) + `</button>`
    : `<button type="button" class="dnav-btn nav" disabled>` +
      (dir<0 ? '&larr;' : '&rarr;') + `</button>`;
  const navBar = (rpos>=0 && rsibs.length>1)
    ? `<div class="dnav"><span class="dnav-l">${backBtnHTML()}</span>` +
      `<span class="dnav-pos">run ${rpos+1} of ${rsibs.length}</span>` +
      `<span class="dnav-r">${rnavBtn(rsibs[rpos-1],-1)}` +
      `${rnavBtn(rsibs[rpos+1],1)}</span></div>`
    : (modalHist.length
       ? `<div class="dnav"><span class="dnav-l">${backBtnHTML()}` +
         `</span></div>` : '');
  document.getElementById('dbody').innerHTML =
    navBar +
    `<h3 id="modal-title">${esc(r.run)}</h3>` +
    (r.stage ? `<span class="stage ${esc(r.stage)}">${esc(STAGES[r.stage]||r.stage)}</span>` : '') +
    notice +
    `<div class="kv" style="margin-top:12px">` +
    kv.map(([k,v])=>`<div class="k">${k}</div><div class="wrap">` +
      (k==='tokens'
        ? `<span title="${esc(TOK_HELP)}">${esc(v??'-')}</span>`
        : k.startsWith('follow-up')
          ? v    // the link/label above is already escaped markup
        : k==='sessions' && v!=='-'
          ? v    // already-escaped <details> markup, not plain text
          : esc(v??'-')) + `</div>`).join('') + '</div>' +
    (mergeResult[key]
      ? `<div class="notice">${esc(mergeResult[key])}</div>` : '') +
    // meta/view actions get their own compact row right under the kv -
    // the execution controls below each live inside their own panel
    `<div class="actions" style="margin-top:10px">` +
    `<button type="button" class="btn secondary" ` +
    `data-filter-run="${esc(key)}">Show only this run</button>` +
    (r.stage ? '' : `<button type="button" class="btn secondary" ` +
      `data-run-summary="${esc(key)}" ` +
      `title="read this run's summary - the LLM-written executive ` +
      `report when one was generated, otherwise each step's own ` +
      `closing report">See run summary</button>` +
      `<button type="button" class="btn secondary" ` +
      `data-summarize-run="${esc(base(r.dir))}" ` +
      `data-rk="${esc(key)}"` +
      `${(r.summarizing||liveSteps.length)?' disabled':''} ` +
      `title="${liveSteps.length
        ? 'disabled while a step is still live'
        : r.summarizing
          ? 'a summary session is running - run.summary.md lands ' +
            'when it finishes'
          : 'spawn a fresh Devin session to write an executive ' +
            'summary of this run (run.summary.md)' +
            (r.has_summary ? ' - replaces the existing file' : '')}">` +
      `${r.summarizing?'Generating\u2026':'Generate LLM summary'}</button>`) +
    (r.request_id ? `<button type="button" class="btn secondary" ` +
      `data-open-request="${esc(r.request_id)}">View original request / TOML</button>` : '') +
    (r.stage ? '' : `<button type="button" ` +
      `class="btn${r.needs_review?'':' secondary'}" ` +
      `data-review-run="${esc(base(r.dir))}" ` +
      `data-review-set="${r.needs_review?1:0}" ` +
      `data-review-seq="${r.completion_seq??''}" ` +
      `title="a private 'you looked at this' flag for the run as a whole ` +
      `- its tasks keep their own marks">` +
      `${r.needs_review?'Mark as reviewed':'Mark as needing review'}` +
      `</button>`) +
    (r.stage==='generating' && r.request_id
      ? `<button type="button" class="btn danger" ` +
        `data-stop-gen="${esc(r.request_id)}" ` +
        `title="cancel this planning pass - the request stays editable ` +
        `and any previous proposal is kept">Stop proposal</button>` : '') +
    '</div>' +
    pmHtml +
    ctlHtml +
    taskTbl +
    (r.stage ? '' : histBlock(base(r.dir), null)) +
    // admin/destructive actions sit at the very bottom, after the task
    // list - away from the controls a user reaches for first
    (r.stage ? '' : `<div class="actions">` +
      `<button type="button" class="btn secondary" ` +
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
        : 'move this run record - state, logs, prompts, exports - to ' +
          'Trash; recoverable for 48h, Empty trash deletes it for good'}">` +
      `Delete run</button></div>`);
  modalRunKey = key;
  applyModalScroll();
  document.getElementById('backdrop').hidden = false;
  applySel();        // mark this run's row while its modal is open
}
// --- row context menus -------------------------------------------------
// Right-click (or Shift+F10 / Menu key on a focused row) opens a small
// action menu on run and task rows. Menu items are <button>s carrying the
// SAME data-* hooks the modal action buttons use, so activation runs
// through the one delegated click handler (confirms, sentinel endpoints
// and their server-side state validation included) - there is no second
// execution-control path here, just a new way to reach the existing one.
// The native menu is deliberately preserved where it matters: on an
// editable field, on a live text selection (copy stays native), and
// everywhere via Shift+right-click as the explicit escape.
const ctxAttrs = o => Object.entries(o)
  .map(([k,v])=>` data-${k}="${esc(v)}"`).join('');
function hideCtxMenu(){
  const m = document.getElementById('ctxmenu');
  if (m) m.hidden = true;
  if (ctxState && ctxState.anchor && ctxState.anchor.classList)
    ctxState.anchor.classList.remove('ctxanchor');
  ctxState = null;
}
// Availability mirrors the modal's predicates exactly (runCtl for runs);
// items the state doesn't allow are simply absent - same as the modal -
// except Delete run, which the modal also renders but disables while live.
function ctxRunItems(r){
  const c = runCtl(r), key = c.key, it = [];
  if (r.stage){
    // proposal/request row: the name button opens the request modal, so
    // 'Open details' does too
    it.push({label:'Open details', attrs:ctxAttrs({'open-request':r.request_id}),
      title:'open the request / proposal review'});
    if (r.stage==='questions' && (r.questions||[]).length)
      it.push({label:'Answer questions',
        attrs:ctxAttrs({'open-questions':r.request_id})});
    if (r.stage==='generating')
      it.push({label:'Stop proposal', danger:true,
        attrs:ctxAttrs({'stop-gen':r.request_id}),
        title:'cancel this planning pass - the request stays editable'});
  } else {
    it.push({label:'Open details', attrs:ctxAttrs({'run-details':key}),
      title:'run details - same as clicking the run name'});
    if (r.request_id)
      it.push({label:'View original request / TOML',
        attrs:ctxAttrs({'open-request':r.request_id})});
  }
  if (selectedRun===key)
    it.push({label:'Clear run filter', attrs:ctxAttrs({'clear-run':''})});
  else
    it.push({label:'Filter by this item', attrs:ctxAttrs({'filter-run':key}),
      title:'show only this run and its tasks'});
  if (!r.stage){
    const act = [];
    if (c.liveSteps.length){
      act.push({label:'Pause run', attrs:ctxAttrs({'pause-run':base(r.dir)}),
        title:'let in-flight steps finish, then hold the run - nothing ' +
          'new launches until Resume'});
      act.push({label:'Stop run now', danger:true,
        attrs:ctxAttrs({'stop-run':base(r.dir)}),
        title:'kill every live step now - committed work stays; Resume ' +
          'restarts unfinished steps'});
    }
    if (c.canResume)
      act.push({label:'Resume unfinished steps',
        attrs:ctxAttrs({'resume-run':base(r.dir)}),
        title:'re-run every non-done step in a fresh session'});
    if (c.pendingMerge)
      act.push({label:'Merge pending branches\u2026',
        attrs:ctxAttrs({'run-details':key}),
        title: c.executing
          ? 'open the run details - the pending merges panel shows ' +
            'progress while the run executes; merging unlocks once ' +
            'it has actually stopped or finished'
          : 'open the run details to tick and merge this run\u2019s ' +
            'parked or deferred worktree branches'});
    if (act.length) it.push({sep:1}, ...act);
    it.push({sep:1});
    it.push({label:r.needs_review?'Mark as reviewed':'Mark as needing review',
      attrs:ctxAttrs({'review-run':base(r.dir),'review-set':r.needs_review?1:0,
                      'review-seq':r.completion_seq??''}),
      title:'personal review mark for the RUN as a whole - its tasks ' +
            'keep their own'});
    it.push({label:r.archived?'Unarchive run':'Archive run',
      attrs:ctxAttrs({'arch-run':base(r.dir),'arch-set':r.archived?0:1})});
    it.push({label:'Delete run', danger:true, dis:!!c.liveSteps.length,
      attrs:ctxAttrs({'delete-run':base(r.dir)}),
      title:c.liveSteps.length
        ? 'disabled while a step is still live - stop the run first'
        : 'move this run record to Trash - recoverable for 48h, Empty ' +
          'trash deletes it for good'});
  }
  return it;
}
function ctxAgentItems(a){
  const key = agentKey(a), it = [];
  it.push({label:'Open details', attrs:ctxAttrs({'agent-detail':key}),
    title:'task details - same as the step name'});
  if (a.run_dir)
    it.push({label:a.aux?'See run summary':'Session summary',
      attrs:ctxAttrs({'summary':key}),
      title:a.aux
        ? "this run's executive summary, opened in this dialog"
        : 'the end-of-session report for this step, opened in this ' +
          'dialog'});
  it.push({label:'Filter by this item',
    attrs:ctxAttrs({'filter-run':a.run_key||a.run_dir||''}),
    title:'show only the run this task belongs to'});
  if (a.slug)
    it.push({label:'Filter to this project',
      attrs:ctxAttrs({'proj':a.slug}),
      title:'toggle the project filter - same as the group name or the ' +
        'project chips up top'});
  if (a.request_id)
    it.push({label:'View request / runbook',
      attrs:ctxAttrs({'open-request':a.request_id})});
  const act = [];
  // aux bookkeeping rows (the run summarizer) aren't pipeline steps -
  // no stop/skip/review/prompt controls exist for them
  if (!a.aux){
  if (a.status==='running' && a.run_dir){
    act.push({label:'Interrupt + instruct',
      attrs:ctxAttrs({'intr-open':key}),
      title:'stop the worker and resume this same step with added ' +
        'instructions/screenshots - committed work stays; the run ' +
        'keeps going'});
    act.push({label:'Stop this step', danger:true,
      attrs:ctxAttrs({'stop-agent':base(a.run_dir),'step':a.step}),
      title:'tree-kill this agent now - the run proceeds to the next step'});
  }
  if ((a.status==='queued'||a.status==='waiting') && !a.skipped_by_user &&
      a.run_dir)
    act.push({label:'Skip step',
      attrs:ctxAttrs({'skip-step':base(a.run_dir),'step':a.step}),
      title:'never launch this step while marked - stays in force until ' +
        'Unskip'});
  if (a.skipped_by_user && a.run_dir)
    act.push({label:'Unskip step',
      attrs:ctxAttrs({'unskip-step':base(a.run_dir),'step':a.step})});
  if (a.step==='plan-proposal' && a.status==='running' && a.request_id)
    act.push({label:'Stop proposal', danger:true,
      attrs:ctxAttrs({'stop-gen':a.request_id})});
  if (a.run_dir)
    act.push({label:a.needs_review?'Mark as reviewed':'Mark as needing review',
      attrs:ctxAttrs({'review-step':base(a.run_dir),'step':a.step,
                      'review-set':a.needs_review?1:0,
                      'review-seq':a.completion_seq??''}),
      title:'personal review mark for THIS task - the run keeps its own'});
  if (a.prompt_editable && a.run_dir)
    act.push({label:'Edit prompt', attrs:ctxAttrs({'edit-prompt':key}),
      title:'open the task and jump to the prompt editor'});
  }
  if (act.length) it.push({sep:1}, ...act);
  return it;
}
function openCtxMenu(row, items, x, y){
  const m = document.getElementById('ctxmenu');
  if (!m || !items.length) return;
  hideCtxMenu();
  ctxState = {items, idx:0, anchor:row, returnEl:row};
  m.innerHTML = items.map((it,i)=> it.sep
    ? '<div class="ctxsep"></div>'
    : `<button type="button" class="ctxi${it.danger?' danger':''}" ` +
      `role="menuitem" data-ctxi="${i}"` +
      (it.dis?' disabled':'') +
      (it.title?` title="${esc(it.title)}"`:'') + it.attrs +
      `>${esc(it.label)}</button>`).join('') +
    '<div class="ctxhint">Shift+F10 reopens this menu from the keyboard · ' +
    'Shift+right-click keeps the browser menu</div>';
  // clamp inside the viewport - hidden elements have no layout, so unhide
  // first (the test harness has no layout - fall back to fixed estimates)
  m.hidden = false;
  const st = m.style || (m.style = {});
  const W = (typeof window!=='undefined' && window.innerWidth) || 1e9;
  const H = (typeof window!=='undefined' && window.innerHeight) || 1e9;
  const mw = m.offsetWidth || 240, mh = m.offsetHeight || items.length*26+30;
  st.left = Math.max(4, Math.min(x, W-mw-4)) + 'px';
  st.top  = Math.max(4, Math.min(y, H-mh-4)) + 'px';
  if (row.classList) row.classList.add('ctxanchor');
  // native menus grab focus - keyboard users need the items reachable, and
  // Esc returning focus to the row keeps the flow
  const first = m.querySelector &&
    m.querySelector('.ctxi:not([disabled])');
  if (first && first.focus) first.focus();
}
function openRowMenu(row, x, y){
  if (!row || !row.dataset) return;
  let items = null;
  if (row.classList && row.classList.contains('runrow')){
    const r = (lastData?.runs||[]).find(v=>runKey(v)===row.dataset.runKey);
    if (r) items = ctxRunItems(r);
  } else if (row.classList && row.classList.contains('agen')){
    const a = AGENTS[+row.dataset.i];
    if (a) items = ctxAgentItems(a);
  }
  if (items && items.length) openCtxMenu(row, items, x, y);
}
function ctxMoveFocus(key){
  const m = document.getElementById('ctxmenu');
  if (!m || !m.querySelectorAll) return;
  const bs = [...m.querySelectorAll('.ctxi:not([disabled])')];
  if (!bs.length) return;
  let i = ctxState ? ctxState.idx : -1;
  if (key==='Home') i = 0;
  else if (key==='End') i = bs.length-1;
  else i = (i + (key==='ArrowDown'?1:-1) + bs.length) % bs.length;
  ctxState.idx = i;
  if (bs[i].focus) bs[i].focus();
}
async function api(method,path,body,timeoutMs){
  const opt = method==='GET' ? {} : {method,
    headers:{'Content-Type':'application/json'}, body:JSON.stringify(body||{})};
  // a never-settling fetch would park callers forever - chipBusy never
  // clears, its view chip spins and stays inert until a page reload
  if (timeoutMs && typeof AbortSignal!=='undefined' && AbortSignal.timeout)
    opt.signal = AbortSignal.timeout(timeoutMs);
  const res=await fetch(path,opt);
  const out=await res.json();
  if (!res.ok) throw new Error(out.error||`HTTP ${res.status}`);
  return out;
}
// /api/state requests can overlap - the 2s tick plus every post-action
// refresh() land whenever they land. Sequence them: only the
// latest-ISSUED response may paint, so an older body can't arrive late
// and resurrect a stale view (or repaint the open modal off it) for the
// two seconds until the next fetch.
let stateReqSeq = 0;
async function refresh(){
  const my = ++stateReqSeq;
  // 30s: far above the worst observed payload (~6s cold session cache,
  // ~1s typical) - a fetch past this is wedged (dropped/half-open
  // connection, stalled handler) and must settle so busy chips release
  const d = await api('GET', stateUrl(), null, 30000);
  if (my===stateReqSeq) render(d);
}
// A user-clicked refetch (view chips, trash ops) can take seconds - the
// state payload re-reads every run dir, and a stale session cache lists
// devin sessions in the background. Show it processing: spinner on the
// clicked chip + a dim over the content being recomputed, so a slow
// answer never reads as a dropped click. Busy state is keyed in
// chipBusy, not on the element - render() rebuilds the chips every poll,
// so a slow fetch outlives the node it started on (the re-rendered chip
// re-stamps 'busy' from the set and its click guard keeps holding - a
// repeat click mid-flight must never re-toggle the view flag back off).
const chipBusy = new Set();
let viewloadN = 0;
async function busyRefresh(el, key){
  if (key){ if (chipBusy.has(key)) return; chipBusy.add(key); }
  if (el){
    el._busy = true;
    if (el.classList) el.classList.add('busy');
    if (el.setAttribute) el.setAttribute('aria-busy','true');
  }
  viewloadN++;
  if (document.body && document.body.classList)
    document.body.classList.add('viewload');
  let ferr = null;
  try { await refresh(); }
  catch(err){ ferr = err; }
  if (key) chipBusy.delete(key);
  if (el){
    el._busy = false;
    if (el.classList) el.classList.remove('busy');
    if (el.removeAttribute) el.removeAttribute('aria-busy');
  }
  if (--viewloadN<=0){
    viewloadN = 0;
    if (document.body && document.body.classList)
      document.body.classList.remove('viewload');
  }
  if (ferr){   // the toggle already persisted - the next tick retries
    if (typeof showAlert==='function')
      await showAlert('Update failed: '+(ferr.message || ferr));
  }
}
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
// --- model comboboxes ---------------------------------------------------
// The Planner/Step model fields keep free text (a value need not be in
// the catalog - an older model id stays valid) while the popup offers a
// filtered Recommendations group on top of a separated, scrollable All
// models group. Both read modelCatalog() - the same `devin models list`
// parse that fed the old datalist, so provider families stay distinct.
const MODEL_FIELDS=['rq-plan_model','rq-model'];
const MCOMBO={};   // input id -> {items:[{id,value}], active} - the flat
                   // option order across BOTH groups, so an item present
                   // in recs AND the full list is two reachable entries
function modelCatalog(){
  const mo=(lastData&&lastData.model_options)||{};
  const list=Object.keys(mo).length?Object.keys(mo):['swe-2','gpt-6-luna'];
  if(!list.includes('adaptive'))list.push('adaptive');
  return list;
}
function mcombo(inp){return MCOMBO[inp.id]||(MCOMBO[inp.id]={items:[],active:-1});}
function mpopRender(inp){
  const pop=document.getElementById(inp.id+'-pop');
  if(!pop)return;
  const st=mcombo(inp), all=modelCatalog();
  const cur=(inp.value||'').trim(), q=cur.toLowerCase();
  // recommendations: substring hits, exact-prefix first - an empty query
  // skips the group entirely (all list alone carries the popup then)
  const recs=q?all.filter(m=>m.toLowerCase().includes(q))
    .sort((a,b)=>(b.toLowerCase().startsWith(q)?1:0)-
                (a.toLowerCase().startsWith(q)?1:0)):[];
  st.items=[];
  const opt=(m,sec)=>{
    const i=st.items.length, it={id:`${inp.id}-opt-${sec}${i}`,value:m};
    st.items.push(it);
    return `<div class="mopt" role="option" id="${it.id}" ` +
      `data-mfor="${inp.id}" data-mi="${i}" ` +
      `aria-selected="${m===cur?'true':'false'}">${esc(m)}</div>`;
  };
  pop.innerHTML=
    (q?`<div class="mgrp" role="group" aria-label="Recommendations">` +
       `<div class="msec">Recommendations</div>` +
       `<div class="mall">` +
       (recs.length?recs.map(m=>opt(m,'r')).join('')
        :`<div class="mnone">no matches - the full list is below</div>`) +
       `</div></div>`:'') +
    `<div class="mgrp mgrp-all" role="group" aria-label="All models">` +
    `<div class="msec">${q?'All models':'Supported models'}</div>` +
    `<div class="mall">${all.map(m=>opt(m,'a')).join('')}</div></div>`;
  // the current value's row is the landing spot - its recommendations
  // occurrence wins (top of the list); a free-text value has none
  st.active=st.items.findIndex(i=>i.value===cur);
}
function mpopOpen(inp){
  const pop=document.getElementById(inp.id+'-pop');
  if(!pop||inp.disabled)return;
  mpopRender(inp);
  pop.hidden=false;
  inp.setAttribute('aria-expanded','true');
  // near the modal's bottom edge the popup grows upward instead of
  // stretching the dialog's scroll area out of view
  const d=document.getElementById('detail'),
        dr=d&&d.getBoundingClientRect&&d.getBoundingClientRect(),
        ir=inp.getBoundingClientRect&&inp.getBoundingClientRect();
  if(pop.classList)pop.classList.toggle('up',
    !!(dr&&ir&&ir.bottom+280>dr.bottom&&ir.top-dr.top>280));
  const st=mcombo(inp);
  if(st.active>=0)mpopActivate(inp,st.active);
}
function mpopClose(inp){
  const pop=document.getElementById(inp.id+'-pop');
  if(!pop||pop.hidden)return;
  pop.hidden=true;
  inp.setAttribute('aria-expanded','false');
  inp.removeAttribute('aria-activedescendant');
  mcombo(inp).active=-1;
}
function mpopActivate(inp,idx,scroll){
  const st=mcombo(inp);
  if(idx<0||!st.items.length){
    st.active=-1;
    inp.removeAttribute('aria-activedescendant');return;
  }
  st.active=(idx+st.items.length)%st.items.length;
  const it=st.items[st.active];
  inp.setAttribute('aria-activedescendant',it.id);
  const pop=document.getElementById(inp.id+'-pop'),
        prev=pop&&pop.querySelector?pop.querySelector('.mopt.on'):null;
  if(prev&&prev.classList)prev.classList.remove('on');
  const el=document.getElementById(it.id);
  if(el){
    if(el.classList)el.classList.add('on');
    if(scroll!==false&&el.scrollIntoView)
      el.scrollIntoView({block:'nearest'});
  }
}
function mpopPick(inp,idx){
  const st=mcombo(inp), it=st&&st.items[idx];
  if(!it)return;
  inp.value=it.value;
  // a synthetic input event routes the pick through the SAME delegated
  // handler typing uses - dirty flag, effort sync, pending re-diff -
  // then the popup closes (the handler re-renders it open first)
  if(inp.dispatchEvent)
    inp.dispatchEvent(new Event('input',{bubbles:true}));
  mpopClose(inp);
}
// Held merges imply worktree isolation - deferred work has to live on a
// retained branch. Picking 'hold for review' checks + greys the
// worktrees box; switching back restores whatever the box was before the
// coupling forced it (a deliberate checked state is never undone).
function syncMergeTiming(){
  const m=document.getElementById('rq-merge'),
        wt=document.getElementById('rq-worktrees'),
        mr=document.getElementById('rq-merge_resolver'),
        pa=document.getElementById('rq-parallel');
  if(!m||!wt) return;
  // held merges AND parallel scheduling both imply worktree isolation -
  // while either is on the box stays checked and locked, and the FORCED
  // marker undoes the check only if we put it there
  const forced = m.value==='deferred' || !!(pa&&pa.checked);
  if(forced){
    if(!wt.disabled && !wt.checked && wt.dataset) wt.dataset.forced='1';
    wt.checked=true; wt.disabled=true;
  } else {
    wt.disabled=!!m.disabled;
    if(wt.dataset && wt.dataset.forced){
      wt.checked=false; delete wt.dataset.forced; }
  }
  // merge_resolver only resolves worktree merges: with isolation off it
  // can never fire, so it locks off - remembering its tick (dataset.was)
  // so re-checking worktrees restores it rather than forcing a choice
  if(mr){
    if(!wt.checked){
      if(mr.checked && mr.dataset) mr.dataset.was='1';
      mr.checked=false; mr.disabled=true;
    } else {
      mr.disabled=!!m.disabled;
      if(mr.dataset && mr.dataset.was){ mr.checked=true;
        delete mr.dataset.was; }
    }
  }
}
function openNew(){
  if (!closeThen(closeDetail(), openNew)) return;
  const defaults=lastData?.defaults||{model:'swe-2',effort:'max',
    worktrees:true,merge_resolver:true};
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
  // model fields are free-text comboboxes: typing filters a
  // recommendations group that sits on top of a separated, scrollable
  // 'All models' group - both fed by the live modelCatalog()
  const modelField=(id,label,value)=>
    `<div><label for="${id}">${label}</label>` +
    `<div class="mcombo"><input id="${id}" class="mcombo-in" ` +
    `role="combobox" aria-expanded="false" aria-autocomplete="list" ` +
    `aria-controls="${id}-pop" autocomplete="off" spellcheck="false" ` +
    `value="${esc(value||'')}"${disabled}>` +
    `<div class="mpop" id="${id}-pop" role="listbox" ` +
    `aria-label="${esc(label)} - model choices" hidden></div></div></div>`;
  const steps=rec.proposed_steps||[];
  const canEdit=editable && stage!=='new';
  document.getElementById('dbody').innerHTML =
    (modalHist.length ? `<div class="dnav"><span class="dnav-l">${backBtnHTML()}</span></div>` : '') +
    // the proposal's own name sits in the title bar - the stage word
    // alone ('Request details') doesn't say WHICH proposal is open
    `<h3 id="modal-title">${stage==='new'?'New request':
      stage==='ready'?'Review proposed run':
      stage==='launched'?'Original request': 'Request details'}` +
      `${(rec.run||rec.label) ? ' - '+esc(rec.run||rec.label) : ''}</h3>` +
    (stage==='new'?'':`<span class="stage ${esc(stage)}">` +
      `${esc(STAGES[stage]||stage)}</span>`) +
    // a follow-up proposal names the run that drafted it - the link opens
    // that run's details (Back returns here) and its row marks .dlinked
    (rec.source_run
      ? `<div class="notice">Drafted as the follow-up of run ` +
        `<button type="button" class="linkbtn" ` +
        `data-run-details="${esc(rec.source_run_key ||
          rec.source_run)}">${esc(rec.source_run)}</button>.</div>` : '') +
    `<div class="notice">Save keeps your request as a draft. Generate asks Devin ` +
      `to propose a TOML runbook only; no implementation starts. ` +
      `Starting the run requires a separate, explicit click. Generated TOML ` +
      `is a tracked file: review it before sharing.</div>` +
    (rec.error ? `<div class="notice error">${esc(rec.error)}</div>`:'') +
    // pending guidance is rebuilt live by refreshPending from a real
    // field-vs-baseline diff - reverting a field to its baseline clears
    // its mark without a save, and nothing is flagged while planning is
    // in flight (the change is being regenerated/answered already)
    `<div id="pend-note" class="notice warn" hidden></div>` +
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
    modelField('rq-plan_model','Planner model',
               rec.plan_model||rec.model) +
    `<div><label for="rq-plan_effort">Planner effort</label>` +
    `<input id="rq-plan_effort" list="rq-plan_efforts" ` +
    `value="${esc(rec.plan_effort||rec.effort||'')}"${disabled}>` +
    `<datalist id="rq-plan_efforts">` +
    `${effortOptionsFor(rec.plan_model||rec.model)}</datalist>` +
    `</div></div>` +
    `<div class="field-row">` +
    modelField('rq-model','Step model',rec.model) +
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
      `value="${esc(rec.max_attempts??3)}"${disabled}></div>` +
    `<div><label for="rq-merge_target">Merge target</label>` +
    `<input id="rq-merge_target" list="rq-branches" ` +
      `value="${esc(rec.merge_target||'')}"${disabled} ` +
      `placeholder="checkout branch">` +
    `<datalist id="rq-branches"></datalist></div></div>` +
    `<div class="field-row"><div><label for="rq-merge">Merge timing</label>` +
    `<select id="rq-merge"${disabled}>` +
      `<option value="auto"${rec.merge==='deferred'?'':' selected'}>` +
      `merge after each step - verified branches land back as the run ` +
      `goes (default)</option>` +
      `<option value="deferred"${rec.merge==='deferred'?' selected':''}>` +
      `hold for review - keep work on pending branches; merge what you ` +
      `pick from the run details at the end</option></select></div></div>` +
    `<div class="sub" id="rq-mt-hint">An existing local branch the ` +
      `worktree branches are created from and merged back into - empty ` +
      `means the project checkout's current branch. With a project ` +
      `chosen it is checked against that repo on save; an inferred ` +
      `project is checked when the proposal resolves it. Needs ` +
      `worktree isolation.</div>` +
    `<label for="rq-raw">Your request</label><textarea id="rq-raw"${disabled} ` +
    `placeholder="Describe the work in your own words…">${esc(rec.raw||'')}</textarea>` +
    `<div id="rq-attach" class="attach-strip" aria-live="polite" ` +
      `aria-label="Attached images" hidden></div>` +
    (editable?`<div class="sub attach-row"><button type="button" ` +
      `class="attach-btn" data-attach-pick ` +
      `aria-label="Choose image files to attach" ` +
      `title="Attach images - pick files from this device">` +
      `<svg viewBox="0 0 24 24" width="14" height="14" fill="none" ` +
      `stroke="currentColor" stroke-width="2" stroke-linecap="round" ` +
      `stroke-linejoin="round" aria-hidden="true" focusable="false">` +
      `<path d="m21.44 11.05-9.19 9.19a6 6 0 0 1-8.49-8.49l8.57-8.57` +
      `A4 4 0 1 1 18 8.84l-8.59 8.57a2 2 0 0 1-2.83-2.83l8.49-8.48"/>` +
      `</svg></button>` +
      `<span>Attach images - click the paperclip to pick files from ` +
      `this device, drop image files anywhere on this dialog, or paste ` +
      `a screenshot. Each is saved under data/attachments and its path ` +
      `is inserted into the request; remove one with its &times; any ` +
      `time before generating or starting.</span></div>` +
      `<input type="file" id="rq-file" accept="image/*" multiple ` +
      `hidden>`:'') +
    `<label class="check"><input type="checkbox" id="rq-followup"` +
      `${rec.followup?' checked':''}${disabled}><span>End the run with a ` +
      `planning step that drafts a follow-up manifest into manifests/ ` +
      `for anything unfinished</span></label>` +
    `<label class="check"><input type="checkbox" id="rq-worktrees"` +
      `${rec.worktrees?' checked':''}${disabled}><span>Run every step in ` +
      `its own git worktree (<code>worktree = true</code>): changes land ` +
      `on a conductor/&lt;run&gt;/&lt;step&gt; branch - <i>Merge timing</i> ` +
      `decides whether they merge back after each step or stay pending ` +
      `for a manual merge. Required anyway for same-project parallel ` +
      `steps, and implied by held merges</span></label>` +
    `<label class="check"><input type="checkbox" id="rq-merge_resolver"` +
      `${rec.merge_resolver?' checked':''}${disabled}><span>On a ` +
      `worktree merge conflict, spawn a resolve-* session to finish the ` +
      `merge (<code>merge_resolver = true</code>) instead of only ` +
      `parking the worktree for a human</span></label>` +
    `<label class="check"><input type="checkbox" id="rq-parallel"` +
      `${rec.parallel?' checked':''}${disabled}><span>Run the steps in ` +
      `parallel (<code>parallel = true</code>): every step schedules at ` +
      `once, bounded by the machine-wide agent slot pool it shares with ` +
      `other runs - queued steps wait for a free slot, and merges still ` +
      `serialize per repo after the pool drains. Workers are told they ` +
      `share the machine and must not fight over shared resources. Needs ` +
      `worktree isolation (forced on while ticked); a closing ` +
      `follow-up-plan step still runs last</span></label>` +
    `<label class="check"><input type="checkbox" id="rq-summarize"` +
      `${rec.summarize?' checked':''}${disabled}><span>When the run ends, ` +
      `spawn one small session that writes an executive summary ` +
      `(<code>run.summarize = true</code>) - the run modal's ` +
      `"See run summary" reads it; without it the per-step closing ` +
      `reports still roll up</span></label>` +
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
        `${s.wt?' · worktree':''}` +
        `${s.mt?' · merge&rarr;'+esc(s.mt):''}` +
        `${s.merge==='deferred'?' · merge held':''}` +
        `</small><br>${esc(s.task||'')}</li>`).join('')}</ol>`:'') +
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
      // Regenerate is a neutral secondary - the primary slot on a
      // reviewed proposal belongs to Start run, and an amber 'attn' flag
      // would be a colour-only staleness signal (the note above names
      // the pending fields instead)
      `<button type="button" id="rq-generate" ` +
      `class="btn${rec.manifest?' secondary':''}" data-action="generate" ` +
      `title="plan a fresh TOML runbook from the request fields - ` +
      `replaces the current proposal only when generation succeeds">` +
      `${rec.manifest?'Regenerate tasks':'Generate proposal'}</button>` +
      (rec.manifest?`<button type="button" id="rq-apply" ` +
        `class="btn secondary" data-action="apply">` +
        `Apply options to TOML</button>`:''):'') +
    ((stage==='error'||stage==='questions')&&rec.manifest_text
      ? `<button type="button" class="btn secondary" data-action="restore"` +
        ` title="republish the previous proposal and drop the pending ` +
        `questions">Keep previous plan</button>`:'') +
    (stage==='ready'||stage==='launch_failed' ? `<button type="button" ` +
      `id="rq-run" class="btn" data-action="run"${rec.stale?' disabled':''}>` +
      `${stage==='launch_failed'?'Retry start':'Start run'}</button>`:'') +
    (canEdit?`<button type="button" class="btn danger" data-action="discard">` +
      `Discard</button>`:'') +
    (stage==='launched'?`<button type="button" class="btn secondary" ` +
      `data-run-details="request:${esc(rec.id)}">View run details</button>`:'') +
    `</div>`;
  applyModalScroll();
  document.getElementById('backdrop').hidden = false;
  renderAttach(editable);
  syncMergeTiming();
  syncEffort(document.getElementById('rq-model'),'rq-effort','rq-efforts');
  syncEffort(document.getElementById('rq-plan_model'),
             'rq-plan_effort','rq-plan_efforts');
  refreshPending();
  const projSel=document.getElementById('rq-project');
  if (projSel && projSel.addEventListener)
    projSel.addEventListener('change',()=>loadBranches(projSel.value));
  // A stored choice is the user's own; an untouched field gets the
  // implied default of whatever project is selected (or 'main' while the
  // project is still inferred).
  mtTouched=!!rec.merge_target;
  mtImplied='';
  loadBranches(mtSlug());
  // the record only arrived now - mark the source run's row .dlinked
  // without waiting out the next refresh tick
  applySel();
}

// --- proposal pending state ---------------------------------------------
// 'Stale' is a computed field-vs-baseline diff, not a latched flag. Two
// baselines: rec.baseline carries the generation inputs the proposal was
// built from (raw/project/attachments - only Regenerate reconciles those)
// and the option values the TOML itself encodes (Apply patches those
// deterministically). pendingNow() re-diffs the live form so reverting a
// field to its baseline clears the mark without a save round-trip; a null
// baseline key keeps the server's pending verdict (unknowable for
// pre-baseline records, unexpressible for a mixed worktree state).
const GEN_KEYS=['raw','project','attachments'];
const OPT_KEYS=['model','effort','on_failure','max_attempts','worktrees',
  'merge_resolver','merge','followup','merge_target','parallel'];
// mirrors conductor._MIXED_OPTS - keys whose manifest baseline may
// legitimately be null because the TOML carries a per-step mix
const MIXED_KEYS=new Set(['worktrees','merge','merge_target']);
const PEND_LABEL={raw:'request text',project:'target project',
  attachments:'image attachments',fields:'request fields',
  model:'step model',effort:'step effort',on_failure:'failure policy',
  max_attempts:'attempts/step',worktrees:'worktrees',
  merge_resolver:'merge resolver',merge:'merge timing',
  parallel:'parallel scheduling',
  followup:'follow-up step',
  merge_target:'merge target',manifest:'the proposal TOML'};
const pendName=k=>PEND_LABEL[k]||k;
function sameBase(k,v,b){
  if (k==='attachments')
    return (v||[]).map(a=>a&&a.name).join('|')===
           (b||[]).map(a=>a&&a.name).join('|');
  if (k==='max_attempts') return parseInt(v||'0',10)===b;
  if (typeof b==='boolean') return !!v===b;
  return String(v==null?'':v)===String(b==null?'':b);
}
function pendingNow(){
  const rec=(modalRequest&&modalRequest.record)||{};
  const srv=rec.pending||{};
  const pend={gen:new Set(srv.gen||[]),opts:new Set(srv.opts||[])};
  // mixed-baseline keys live apart from 'opts' (changed): nothing the
  // field can be set to matches a per-step mix, and a valid mixed TOML
  // must not block Start - only real drift does.
  const mixed=new Set(srv.mixed||[]);
  // a pre-pending record carries only the lumped flag - an unattributed
  // 'fields' marker, same fallback _pending_actions makes server-side
  if (rec.stale && rec.pending===undefined) pend.gen.add('fields');
  const base=rec.baseline;
  // no proposal yet, no baselines shipped, or no editable form on screen:
  // the server's saved-state verdict stands alone
  if (!rec.manifest || !base || !document.getElementById('rq-raw'))
    return {gen:[...pend.gen],opts:[...pend.opts],mixed:[]};
  const f=requestFields();
  for (const k of GEN_KEYS){
    if (base[k]==null) continue;      // unknowable - keep server verdict
    pend.gen.delete(k);
    if (!sameBase(k,f[k],base[k])) pend.gen.add(k);
  }
  for (const k of OPT_KEYS){
    if (!(k in base)) continue;        // not shipped - keep server verdict
    pend.opts.delete(k); mixed.delete(k);
    // A null baseline splits three ways server-side (_manifest_options):
    // 'model' absent means nothing pinned - '' is the match; worktrees,
    // merge and merge_target null means the TOML MIXES per-step values
    // the single control cannot express - unrepresentable, not drift;
    // a null on any other key is unknowable - real drift.
    const bv = k==='model' && base[k]==null ? '' : base[k];
    if (bv==null){ (MIXED_KEYS.has(k)?mixed:pend.opts).add(k); }
    else if (!sameBase(k,f[k],bv)) pend.opts.add(k);
  }
  return {gen:[...pend.gen],opts:[...pend.opts],mixed:[...mixed]};
}
function pendNote(p){
  if (p.opts.includes('manifest'))
    return 'The saved proposal TOML does not parse - fix it in the ' +
      'editor below, or <b>Regenerate tasks</b> to rebuild it from the ' +
      'request.';
  if (p.gen.includes('fields'))
    return 'Request fields changed since this proposal was generated ' +
      '(which ones is not recorded for this older draft) - ' +
      '<b>Regenerate tasks</b> is the safe refresh; it also writes the ' +
      'current options into the new TOML.';
  const hasGen=p.gen.length>0, hasOpts=p.opts.length>0 ||
    (p.mixed||[]).length>0;
  if (!hasGen && !hasOpts) return '';
  const genTxt=p.gen.map(pendName).join(', ');
  // options whose baseline the form cannot express (mixed per-step
  // values in the TOML) are pending forever - saying they 'changed' is
  // a lie, and Apply FLATTENS the mix rather than reconciling it.
  const mix=(p.mixed||[]);
  const chg=p.opts.filter(k=>!mix.includes(k));
  const optTxt=chg.map(pendName).join(', ');
  const mixTxt=mix.map(pendName).join(', ');
  const mixCap=mixTxt ? mixTxt[0].toUpperCase()+mixTxt.slice(1) : '';
  const mixPart=mixTxt
    ? `${mixCap} ${mix.length>1?'are':'is'} mixed across the ` +
      `proposal's steps - a single option cannot represent that, so ` +
      `the frame stays no matter what it is set to. <b>Apply options ` +
      `to TOML</b> would normalize every step to the form's value ` +
      `(check the TOML first - a deliberate mix would be flattened).` : '';
  if (hasGen && hasOpts)
    return `Two kinds of changes are pending: ${genTxt} - only ` +
      `<b>Regenerate tasks</b> rebuilds the plan around those - and ` +
      `options (${[optTxt,mixTxt].filter(Boolean).join(', ')}) that ` +
      `Apply options to TOML could patch. ` +
      `Regenerate first: the new plan is built from the current request ` +
      `and writes the current options too, so a single Regenerate ` +
      `clears both. ${mixPart}`;
  if (hasGen)
    return `${genTxt} changed since this proposal was generated - the ` +
      `step prompts were built from the old input. <b>Regenerate ` +
      `tasks</b> rebuilds the plan around the current fields; Apply ` +
      `options to TOML only patches option keys. ${mixPart}`;
  return (optTxt
      ? `Options changed (${optTxt}) since this TOML was written - ` +
        `<b>Apply options to TOML</b> patches them in place, no planner ` +
        `call needed. Regenerate tasks also works but re-plans the ` +
        `whole proposal. ${mixPart}` : mixPart);
}
// pending key -> the form field it lives in, so the pend-note's list
// maps onto the actual controls (a subtle amber frame, not just words)
const PEND_EL={raw:'rq-raw',project:'rq-project',attachments:'rq-attach',
  model:'rq-model',effort:'rq-effort',on_failure:'rq-on_failure',
  max_attempts:'rq-attempts',worktrees:'rq-worktrees',
  merge_resolver:'rq-merge_resolver',merge:'rq-merge',
  parallel:'rq-parallel',
  followup:'rq-followup',merge_target:'rq-merge_target',
  manifest:'rq-manifest'};
function refreshPending(){
  const stage=(modalRequest&&modalRequest.status)||'';
  const p=pendingNow();
  // the changed field/checkbox is what 'Needs review' refers to - frame
  // it so the note's key list stops being guesswork. Suppressed mid-
  // planning like the note itself (the change is being regenerated).
  const quiet = stage==='generating'||stage==='questions';
  const hot = quiet ? new Set()
    : new Set([...p.gen, ...p.opts, ...(p.mixed||[])]
        .map(k=>PEND_EL[k]).filter(Boolean));
  const mix = quiet ? new Set()
    : new Set((p.mixed||[]).map(k=>PEND_EL[k]).filter(Boolean));
  for (const id of new Set(Object.values(PEND_EL))){
    const el=document.getElementById(id);
    if (!el || !el.classList) continue;
    const on = hot.has(id);
    el.classList.toggle('pendchg', on);
    // a mixed-baseline frame cannot be toggled off - say so on the
    // element itself, and only clear a title WE set (dataset marker)
    if (mix.has(id)){
      el.title='the proposal TOML mixes per-step values this single ' +
        'control cannot represent - Apply options to TOML normalizes ' +
        'every step to this value';
      if (el.dataset) el.dataset.pmix='1';
    } else if (el.dataset && el.dataset.pmix){
      el.title=''; delete el.dataset.pmix;
    }
    const lab = el.closest && el.closest('label.check');
    if (lab && lab.classList) lab.classList.toggle('pendchg-lab', on);
  }
  const note=document.getElementById('pend-note');
  if (note){
    // mid-planning the regeneration/answers already own the change
    const html=(stage==='generating'||stage==='questions')?'':pendNote(p);
    note.innerHTML=html;
    note.hidden=!html;
  }
  const ap=document.getElementById('rq-apply');
  if (ap){
    // Apply patches CHANGED options and NORMALIZES mixed ones alike -
    // both are patchable, so either enables it
    const usable=!p.gen.length && (p.opts.length>0 || mix.size>0) &&
                 !p.opts.includes('manifest');
    ap.disabled=!usable;
    ap.title= p.gen.length
      ? 'disabled while generation inputs differ - Regenerate tasks ' +
        'first (it writes the pending options into the new TOML too)'
      : p.opts.includes('manifest')
      ? 'disabled - the proposal TOML does not parse; fix it in the ' +
        'editor or regenerate'
      : usable
      ? 'patch ' + [...p.opts, ...(p.mixed||[])]
          .map(pendName).join(', ') +
        ' into the proposal TOML without a planner call'
      : 'options already match the proposal TOML - nothing to apply';
  }
  const run=document.getElementById('rq-run');
  if (run){
    // a mixed option stays pending forever by nature - it is NOT drift,
    // so it cannot block launch; only real diffs (gen inputs, changed
    // options) do
    run.disabled=!!(p.gen.length||p.opts.length);
    run.title= run.disabled
      ? 'disabled while fields differ from the proposal - Regenerate ' +
        'tasks or Apply options to TOML first' : '';
  }
}
// The merge-target field's datalist comes from the SELECTED project's
// real repo. The checkout's current branch is the editable default while
// the field stays untouched; 'main' is the editable default when the
// project is still inferred (validated per resolved project at
// generation). mtImplied tracks which default was offered so a project
// switch can replace only an untouched default, never a typed value.
// (Module state, not DOM dataset - the node:vm test stubs are plain
// objects.) The server still does the authoritative check on
// save/generate.
let mtBranches={slug:null,list:[]}, mtTouched=false, mtImplied='';
// GET /api/projects/<slug>/branches spawns a git listing server-side -
// memoize per slug for a minute so reopening a modal or switching
// projects doesn't re-run it every time.
const BRANCH_CACHE={}, BRANCH_TTL_MS=60000;
// The merge-target field's project context: an explicit selection wins;
// an inferred one falls back to the proposal TOML's step projects when
// they all share one slug. Multi-project manifests resolve to '' - no
// single branch list applies.
function mtSlug(){
  const sel=document.getElementById('rq-project');
  if (sel && sel.value) return sel.value;
  const rec=(modalRequest&&modalRequest.record)||{};
  if (rec.project) return rec.project;
  const slugs=new Set();
  for (const m of String(rec.manifest_text||'')
      .matchAll(/^[ \t]*project\s*=\s*"([^"]+)"/gm))
    slugs.add(m[1]);
  return slugs.size===1 ? [...slugs][0] : '';
}
async function loadBranches(slug){
  const inp=document.getElementById('rq-merge_target');
  const dl=document.getElementById('rq-branches');
  if(!inp) return;
  // A manifested record's field mirrors the proposal TOML - never
  // overwrite it with a guessed default: a 'main' fill on a repo whose
  // default is 'master' flags the field pending for no reason and
  // Apply would write the phantom into [run].merge_target. Guessed
  // defaults are a new-draft convenience only.
  const manifested=!!(modalRequest&&modalRequest.record&&
                      modalRequest.record.manifest);
  if(!slug){
    mtBranches={slug:'',list:[]};
    if(dl) dl.innerHTML='';
    mtImplied=manifested?'':'main';
    if(!mtTouched && !manifested && !inp.value) inp.value='main';
    inp.placeholder=manifested?"each step project's checked-out branch"
                              :'main';
    updateMtHint();
    return;
  }
  try {
    const hit=BRANCH_CACHE[slug];
    const b=(hit && Date.now()-hit.at<BRANCH_TTL_MS) ? hit.data
      : await api('GET',
        `/api/projects/${encodeURIComponent(slug)}/branches`);
    BRANCH_CACHE[slug]={at:Date.now(),data:b};
    if(mtSlug()!==slug) return;   // a newer modal/project won the race
    mtBranches={slug,list:b.branches||[]};
    if(dl) dl.innerHTML=mtBranches.list
      .map(x=>`<option value="${esc(x)}">`).join('');
    const cur=b.current||'';
    mtImplied=cur;
    if(!mtTouched && !manifested) inp.value=cur;
    inp.placeholder=cur||'checkout branch';
  } catch(err){
    mtBranches={slug,list:[]};
    if(dl) dl.innerHTML='';
  }
  updateMtHint();
}
function updateMtHint(){
  const inp=document.getElementById('rq-merge_target');
  const hint=document.getElementById('rq-mt-hint');
  const slug=mtSlug();
  if(!inp||!hint) return;
  const manifested=!!(modalRequest&&modalRequest.record&&
                      modalRequest.record.manifest);
  const base=`An existing local branch the worktree branches are created ` +
    `from and merged back into - empty means the project checkout's ` +
    `current branch. Needs worktree isolation.`;
  const v=String(inp.value||'').trim();
  if(!slug){
    hint.innerHTML=manifested
      ? esc(base)+" The proposal's steps span multiple or no resolved "+
        'projects - a target named here must exist in every repo a ' +
        'worktree step uses.'
      : esc(base)+' With an inferred project the editable ' +
        `<b>main</b> default is validated per resolved project at ` +
        `generation.`;
  } else if(v && mtBranches.slug===slug &&
            !mtBranches.list.includes(v)){
    hint.innerHTML=`<b>${esc(v)}</b> is not a local branch in ` +
      `<b>${esc(slug)}</b> - pick one from the list or create it in ` +
      `the repo first; the run will not substitute another branch ` +
      `or create it.`;
  } else if(v){
    hint.innerHTML=`Worktree steps on <b>${esc(slug)}</b> merge back ` +
      `into <b>${esc(v)}</b>.`;
  } else {
    hint.innerHTML=esc(base);
  }
}
document.addEventListener('input',ev=>{
  if(ev.target && ev.target.id==='rq-merge_target'){
    mtTouched=true;
    updateMtHint();
  }
});
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
// The editable request textarea OR the interrupt composer's textarea
// gates every drop/paste listener - other modals (agent detail,
// questionnaire) reuse the same backdrop, so a bare document-level bind
// must bail when neither is present or both are disabled.
function attachTarget(){
  if (modalQA) return null;
  const rq=document.getElementById('rq-raw');
  if (rq && !rq.disabled) return rq;
  const di=document.getElementById('d-instr');
  return (di && !di.disabled) ? di : null;
}
function renderIntrAttach(){
  const strip=document.getElementById('d-instr-attach');
  if(!strip) return;
  const list=intrAttach[detailKey]||[];
  strip.innerHTML=list.map(a=>
    `<span class="attach-item"><a href="/api/attachments/` +
    `${encodeURIComponent(a.name)}" target="_blank" ` +
    `title="${esc(a.path)}"><img src="/api/attachments/` +
    `${encodeURIComponent(a.name)}" alt="${esc(a.name)}"></a>` +
    `<button type="button" class="attach-x" ` +
    `data-attach-rm="${esc(a.name)}" title="Remove this attachment">` +
    `×</button>` +
    `</span>`).join('');
  strip.hidden=!list.length;
}
function attachErr(msg, intr){
  // upload errors land in the composer's own notice when the interrupted
  // step's textarea was the target, else in the request form's
  const e=intr && document.getElementById('d-instr-msg');
  if (e){ e.hidden=false; e.className='notice error'; e.textContent=msg; }
  else formError(msg);
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
  const intr = ta.id==='d-instr';
  const imgs=[...files].filter(f=>(f.type||'').startsWith('image/'));
  if(!imgs.length){
    attachErr('Only image files can be attached.', intr); return; }
  for (const f of imgs){
    try {
      const res=await fetch('/api/attachments',{method:'POST',
        headers:{'Content-Type':f.type||'application/octet-stream'},
        body:f});
      const out=await res.json().catch(()=>({}));
      if(!res.ok) throw new Error(out.error||('HTTP '+res.status));
      if (intr){
        // the composer sends attachments as references in the POST body -
        // the instructions text stays prose, no caret insertion
        const b=(intrAttach[detailKey]=intrAttach[detailKey]||[]);
        if(!b.some(x=>x.name===out.name)) b.push(out);
        renderIntrAttach();
      } else {
        if(!rqAttachments.some(x=>x.name===out.name)) rqAttachments.push(out);
        renderAttach(true);
        insertAtCaret(ta,out.path);
        if(modalRequest) modalRequest.dirty=true;
        refreshPending();
      }
      try{ta.dispatchEvent(new Event('input',{bubbles:true}));}catch(e){}
    } catch(err){ attachErr('Attach failed: '+err.message, intr); }
  }
}
async function openRequest(id, keepStack){
  // a non-null snapshot means a modal view is on screen - this is a
  // modal->modal jump, so the Back stack survives the closeDetail below
  const snap = keepStack ? null : snapshotModal();
  if (!(await closeDetail(false, !!(keepStack || snap)))) return;
  // re-opening the SAME request (post-save reload, stage follow) is not a
  // new history entry - the earlier back chain stays untouched
  if (snap && !(snap.kind==='request' && snap.id===id)) pushHist(snap);
  modalRequest={id,status:'loading',dirty:false,originalManifest:''};
  document.getElementById('dbody').innerHTML =
    (modalHist.length ? `<div class="dnav"><span class="dnav-l">${backBtnHTML()}</span></div>` : '') +
    '<h3 id="modal-title">Loading request…</h3>';
  document.getElementById('backdrop').hidden = false;
  applySel();          // the request's run row stays marked while open
  try {
    const rec=await api('GET',`/api/requests/${encodeURIComponent(id)}`);
    if (!modalRequest || modalRequest.id!==id || modalRequest.dirty) return;
    modalRequest={id,status:rec.status,dirty:false,
      originalManifest:rec.manifest_text||'',record:rec};
    showRequestForm(rec);
  } catch(err){
    document.getElementById('dbody').innerHTML =
      (modalHist.length ? `<div class="dnav"><span class="dnav-l">${backBtnHTML()}</span></div>` : '') +
      `<h3 id="modal-title">Request unavailable</h3><div class="notice error">` +
      `${esc(err.message)}</div>`;
    applyModalScroll();
  }
}
function requestFields(){
  const f=Object.fromEntries(
    ['raw','label','project','model','effort','on_failure',
     'plan_model','plan_effort','merge','merge_target'].map(k=>
    [k,document.getElementById(`rq-${k}`).value]));
  if (!f.merge) f.merge='auto';   // an unread select reads as the default
  for (const k of ['followup','worktrees','merge_resolver','summarize',
                   'ask','parallel']){
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
  if (action==='discard' && !await askConfirm('Discard this request, its proposal ' +
      'TOML, and all planned steps? They move to Trash, recoverable for ' +
      '48h (Undo right after, or the Trash view).')) return;
  // Regenerate replaces the generated tasks when it succeeds, so it
  // always confirms - and names any unsaved TOML edits it would drop
  if (action==='generate' && rec.manifest && !await askConfirm(
      'Regenerate the proposal?' +
      (editedManifest ? ' Your unsaved TOML edits are dropped and ' : ' ') +
      'the current generated TOML is replaced only if generation ' +
      'succeeds.')) return;
  if (action==='stop' && !await askConfirm(
      'Stop preparing this proposal? The planner session is cancelled - ' +
      'your request and any previous proposal are kept.')) return;
  if (action==='run'){
    if (modalRequest.dirty){ formError('Save and review edits before starting.'); return; }
    if (!await askConfirm(`Start ${rec.run} now? This will execute ${rec.proposed_steps?.length||0} ` +
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
      const out=await api('POST',`/api/requests/${rid}/discard`,{confirm:true});
      if (out && out.trash_id) lastTrash={id:out.trash_id,
        label:`request ${rec.label||rid}`};
      selectedRun=null;
      closeDetail(true);
      await refresh();
      return;
    }
    modalRequest.dirty=false;
    await refresh();
    await openRequest(rid, true);
  } catch(err){ formError(err.message); }
  // a failed action keeps the modal - the blanket re-enable must not
  // resurrect a pending-disabled Apply, so the diff re-arms it
  finally { buttons.forEach(b=>b.disabled=false); refreshPending(); }
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
  if (!(await closeDetail())){ modalQA=null; return; }
  modalQA={id,rec,sel:new Set(),free:''};
  showQuestion();
  document.getElementById('backdrop').hidden=false;
  applySel();          // the questionnaire's request row stays marked
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
function syncPoolBox(){
  const sp = lastData && lastData.slot_pool,
        pv = document.getElementById('pool-val');
  if (!pv || !sp) return;
  pv.textContent = `agents ${sp.in_use}/${sp.cap}`;
  pv.parentElement.classList.toggle('full', sp.in_use>=sp.cap);
  const dec=document.getElementById('pool-dec'),
        inc=document.getElementById('pool-inc');
  if (dec) dec.disabled = sp.cap<=1;
  if (inc) inc.disabled = sp.cap>=32;
}
async function poolBump(dlt, btn){
  const sp = lastData && lastData.slot_pool;
  if (!sp || !btn || btn.disabled) return;
  const box = document.getElementById('poolbox');
  box.querySelectorAll('button').forEach(b=>b.disabled=true);
  try {
    const sp2 = await api('POST','/api/pool',{cap:sp.cap+dlt});
    if (lastData) lastData.slot_pool = sp2;
  } catch (e) { await showAlert('Pool cap: '+(e.message||e)); }
  syncPoolBox();
}
function tickElapsed(now){
  for (const a of AGENTS){
    const td = document.querySelector(`td[data-el="${a._i}"]`);
    if (td) td.textContent = elapsed(a, now);
  }
  // live run rows tick the same way - keyed by runKey, found via the
  // attribute (querySelector can't safely quote an arbitrary key)
  for (const td of document.querySelectorAll('td[data-rel]')){
    const r = (lastData?.runs||[]).find(x=>runKey(x)===td.dataset.rel);
    if (r) td.textContent = runElapsed(r, now);
  }
}
// The Trash view (view: 'N trash' chip). Every row is a recoverable
// deletion with its purge clock; Restore lands it back where it was
// (collisions rename, never overwrite) and Empty trash is the one
// irreversible op, so it confirms plainly and the server confirms again.
function trashHtml(d){
  const items = d.trash||[];
  let h = `<h2>trash</h2><div class="sub">deleted run records and ` +
    `proposal drafts rest here for 48h, then a sweep purges them - ` +
    `Restore puts each back exactly where it was. Trashing never removed ` +
    `spawned sessions, parked branches, or worktrees - and nothing here ` +
    `runs.</div>`;
  if (items.length)
    h += `<div style="margin:0 0 8px"><button type="button" ` +
      `class="btn danger" data-empty-trash ` +
      `title="permanently delete every trash entry now - there is no ` +
      `restore after this">Empty trash</button></div>`;
  h += `<div class="wrapw"><table>` +
    `<colgroup><col style="width:23%"><col style="width:10%">` +
    `<col style="width:12%"><col style="width:14%"><col style="width:30%">` +
    `<col style="width:11%"></colgroup>` +
    `<tr><th>name</th><th>kind</th><th>deleted</th><th>purges in</th>` +
    `<th>notes</th><th></th></tr>`;
  for (const e of items){
    const left = e.expires_in_s<=0 ? 'expired - purge pending'
      : fmtS(e.expires_in_s);
    h += `<tr><td class="wrap">${esc(e.name||e.ref||'?')}` +
      `<div class="sub">${esc(e.ref!==e.name?(e.ref||''):'')}` +
      `${esc(e.id)}</div></td>` +
      `<td>${e.kind==='run'?'run':'proposal'}` +
      `${e.legacy?' <span class="stage skipped" title="discarded before ' +
      `the Trash view existed">legacy</span>`:''}</td>` +
      `<td>${loc(e.deleted_at)}</td>` +
      `<td title="purge-eligible at ${esc(e.expires_at||'?')}">${left}</td>` +
      `<td class="wrap sub">${esc(e.note||((e.items||0)+' item(s)'))}</td>` +
      `<td><button type="button" class="btn secondary" ` +
      `data-restore-trash="${esc(e.id)}" ` +
      `title="put it back where it was - nothing is overwritten; a busy ` +
      `name lands under a -restored suffix">Restore</button></td></tr>`;
  }
  h += `</table></div>`;
  if (!items.length) h += `<div class="sub">trash is empty.</div>`;
  return h;
}

function render(d){
  if (BOOT===null) BOOT = d.serve_boot;
  else if (d.serve_boot !== BOOT){ location.reload(); return; }
  lastData = d; AGENTS = d.agents;
  AGENTS.forEach((a,i)=>a._i=i);   // stable cell ids before the sig check
  // the What's-new badge rides the state poll - it updates even on a
  // sig-identical repaint (the count isn't in the sig), and null keeps
  // the pill hidden until the server's async build lands
  tourBadge(d.tour_unseen);
  if (selectedRun && !d.runs.some(r=>runKey(r)===selectedRun)) selectedRun=null;
  const now = Date.now();
  const live = d.agents.filter(a=>a.status==='running');
  const planned = d.agents.filter(a=>a.status==='planned').length;
  document.getElementById('meta').innerHTML =
    `${d.agents.length-planned} agents | ${planned} planned | ${live.length} running | ` +
    (d.slot_pool ? `<b title="machine-wide agent slots (CONDUCTOR_MAX_AGENTS` +
      `, default ${d.slot_pool.cap}); queued = steps holding no slot yet"` +
      `>slots ${d.slot_pool.in_use}/${d.slot_pool.cap}</b>` +
      (d.slot_pool.in_use>=d.slot_pool.cap
        ? ` <span style="color:#d29922">queued: ` +
          `${d.agents.filter(a=>a.slot_wait).length}</span>` : '') +
      ` | ` : '') +
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
  // density is in the sig: fmtT/fmtTc token text differs per density, so
  // a Compact|Full flip must re-render even when the payload is identical.
  const sig = JSON.stringify([d.runs, d.agents.map(({tail,...r})=>r),
    selectedRun, [...only].sort(), [...facets].sort(), [...projs].sort(),
    expanded,
    runsShown===Infinity?'all':runsShown, density,
    d.trash, d.trash_ids, showTrash, lastTrash&&lastTrash.id]);
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
  // aux bookkeeping rows (the run summarizer) are hidden in the default
  // view - they only appear as children of their run once you filter
  // into it, so they never read as pipeline work
  // rscope is the run-filtered pool; the project chips list every slug in
  // it so toggling one project never hides the others' chips. `scope`
  // adds the project selection on top - status/tag counts, the project
  // tables and the 'N filtered' notes all reflect it.
  const rscope = d.agents.filter(a=>a.aux
    ? selectedRun===(a.run_key||a.run_dir)
    : (!selectedRun || (a.run_key||a.run_dir)===selectedRun));
  const scope = rscope.filter(a=>!projs.size || projs.has(a.slug||'?'));
  const byStatus = {};
  for (const a of scope) if (!a.aux)
    byStatus[a.status]=(byStatus[a.status]||0)+1;
  const projCounts = {};
  for (const a of rscope) if (!a.aux)
    projCounts[a.slug||'?']=(projCounts[a.slug||'?']||0)+1;
  // a selected slug stays listed even with nothing in scope right now,
  // same 'sticky selection' the tag chips get
  const projList = [...new Set([...Object.keys(projCounts), ...projs])]
    .sort();
  const selected = d.runs.find(r=>runKey(r)===selectedRun);
  const facetCounts = Object.fromEntries(FACETS.map(f=>
    [f,scope.filter(a=>matchesFacet(a,f)).length]));
  document.getElementById('filters').innerHTML =
    (selected?`<span class="filter-group"><span class="filter-label">run:</span>` +
      `<button type="button" class="chip active" data-clear-run ` +
      `aria-label="Clear run filter">${esc(selected.run)} ×</button></span>`:'') +
    (projList.length
      ? `<span class="filter-group"><span class="filter-label">` +
        `project:</span>` +
        projList.map(s=>
          `<button type="button" class="chip${projs.has(s)?' active':''}` +
          `${projs.size&&!projs.has(s)?' off':''}" data-proj="${esc(s)}" ` +
          `aria-pressed="${projs.has(s)}" title="${projs.has(s)
            ?'clear the project filter':'show only project '+esc(s)}">` +
          `${projCounts[s]||0} ${esc(s)}${projs.has(s)?' ×':''}</button>`
        ).join('') + '</span>'
      : '') +
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
      `data-facet="${f}" aria-pressed="${facets.has(f)}" ` +
      `${FACET_TINT[f]?`style="color:${FACET_TINT[f]}" `:''}>` +
      `${facetCounts[f]} ${f}${facets.has(f)?' ×':''}</button>`).join('') +
    '</span>' +
    `<span class="filter-group"><span class="filter-label">view:</span>` +
    `<button type="button" class="chip${showArchived?' active':''}` +
    `${chipBusy.has('arch')?' busy':''}" data-arch ` +
    `aria-pressed="${showArchived}"` +
    `${chipBusy.has('arch')?' aria-busy="true"':''} ` +
    `title="list archived runs - finished runs are auto-archived after ` +
    `48h (CONDUCTOR_ARCHIVE_HOURS); files stay on disk, Unarchive in the ` +
    `run modal brings one back">${d.archived_runs||0} archived` +
    `${showArchived?' ×':''}</button>` +
    `<button type="button" class="chip${showTrash?' active':''}` +
    `${chipBusy.has('trash')?' busy':''}" data-trash ` +
    `aria-pressed="${showTrash}"` +
    `${chipBusy.has('trash')?' aria-busy="true"':''} ` +
    `title="recoverable trash - deleted runs and proposals rest here for ` +
    `48h before a sweep purges them; Restore puts one back, Empty trash ` +
    `removes all of them for good">${d.trash_count||0} trash` +
    `${showTrash?' ×':''}</button></span>` +
    (only.size||facets.size||projs.size?` <button type="button" ` +
      `class="chip" data-reset>clear filters</button>`:'');

  // Undo bar: the entry a delete/discard just trashed stays one click
  // away until dismissed, restored, or purged (trash_ids liveness).
  const ub = document.getElementById('undobar');
  if (ub){
    if (lastTrash && !(d.trash_ids||[]).includes(lastTrash.id))
      lastTrash = null;
    ub.hidden = !lastTrash;
    ub.innerHTML = lastTrash
      ? `<span class="filter-label">trashed:</span> ` +
        `${esc(lastTrash.label)} ` +
        `<button type="button" class="chip" data-undo-trash=` +
        `"${esc(lastTrash.id)}" title="restore it right now">` +
        `Undo</button> ` +
        `<button type="button" class="chip" data-trash-open ` +
        `title="open the Trash view">open trash</button> ` +
        `<button type="button" class="chip" data-undo-dismiss ` +
        `title="leave it in trash - it auto-purges after 48h">` +
        `dismiss</button>` : '';
  }
  const tw = document.getElementById('trashwrap');
  if (tw){
    tw.hidden = !showTrash;
    if (showTrash) tw.innerHTML = trashHtml(d);
  }

  // runs table first - a run can span projects (chips show which)
  let h = '<colgroup><col style="width:13%"><col style="width:18%">' +
    '<col style="width:13%"><col style="width:8%"><col style="width:8%">' +
    '<col style="width:7%"><col style="width:15%"><col style="width:5%">' +
    '<col class="hide-s" style="width:13%"></colgroup>' +
    '<tr><th>run</th><th>description</th><th>projects</th><th>started</th>' +
    '<th>ended</th>' +
    '<th title="active execution time - driver intervals summed, ' +
    'excluding idle gaps between resumes (never a sum of concurrent ' +
    'task spans)">elapsed</th>' +
    '<th title="per-status step counts, then token sums ' +
    'for the planner session and the worker steps, then estimated ' +
    'cost">steps</th>' +
    '<th title="parallel-agent cap for this run (run.max_parallel)">cap</th>' +
    '<th class="hide-s">dir</th></tr>';
  // rows are newest-first; active runs and unlaunched requests always show,
  // finished history is capped at SHOW_N until "show more"/"show all".
  // The status/tag chips also scope the runs table: a run stays visible
  // while at least one of its agents matches the active filters.
  const matchAgent = a => (!only.size||only.has(a.status)) &&
    (!projs.size||projs.has(a.slug||'?')) &&
    [...facets].every(f=>matchesFacet(a,f));
  const allRuns = d.runs.filter(r=>!selectedRun||runKey(r)===selectedRun)
    .filter(r=>{
      if (!only.size && !facets.size && !projs.size) return true;
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
    const left = Object.keys(c).some(s=>s!=='done' && s!=='superseded');
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
  // 'wt' chip: the run's CONFIGURED worktree isolation, not runtime
  // artifacts - the server derives r.iso from the manifest/proposal
  // (parse already folds merge=deferred into worktree and never counts
  // cloud), so a still-queued worktree step counts and a stray branch
  // name never invents one. iso={wt,local,cloud}; partial coverage is
  // spelled out on the chip itself ('wt 2/3') - a tooltip alone is not
  // an accessible distinction.
  const isoChip = r => {
    const i = r.iso;
    if (!i || !i.wt) return '';
    const full = i.wt >= i.local;
    const cl = i.cloud ? `; ${i.cloud} step(s) run in the cloud` : '';
    const tip = full
      ? `worktree isolation: all ${i.local} local step(s) run in git ` +
        `worktrees${cl}`
      : `worktree isolation: ${i.wt} of ${i.local} local steps run in ` +
        `git worktrees - the rest write in the project checkout${cl}`;
    return ` <span class="stage wt" title="${esc(tip)}">` +
      `wt${full ? '' : ` ${i.wt}/${i.local}`}</span>`;
  };
  // What a run's closing follow-up-plan step left behind: a link into the
  // imported proposal row when one exists (it IS a request record -
  // opening it is the same modal), a muted word when nothing produced.
  const fuChip = fu => {
    const L = {registered:'ready', empty:'no work', missing:'missing',
      invalid:'invalid', deferred:'pending', discarded:'discarded',
      absent:'none'};
    const T = {registered:'follow-up manifest registered as a proposal',
      empty:'follow-up reported nothing remains',
      missing:'follow-up step wrote no manifest',
      invalid:'follow-up manifest failed validation',
      deferred:'follow-up registration is waiting on the checkout',
      discarded:'follow-up proposal was discarded',
      absent:'follow-up step did not finish cleanly - no file produced'};
    // a broken outcome colors the status word red; the 'follow-up ->'
    // prefix stays neutral either way
    const BAD = {invalid:1, missing:1, absent:1, error:1};
    const label = L[fu.status] || fu.status || '?';
    const lab = BAD[fu.status]
      ? `<b class="fu-bad">${esc(label)}</b>` : esc(label);
    const tip = (T[fu.status] || 'follow-up-plan outcome') +
      (fu.source ? ` - source ${fu.source}` : '');
    return fu.request
      ? ` <button type="button" class="fulink" ` +
        `data-open-request="${esc(fu.request)}" ` +
        `title="${esc(tip)} - open the proposal">` +
        `follow-up &rarr; ${lab}</button>`
      : ` <span class="fulink" ` +
        `title="${esc(tip)}">follow-up &rarr; ${lab}</span>`;
  };
  for (const r of shownRuns){
    const counts = Object.entries(r.counts||{})
      .map(([k,v])=>`<span style="color:${COLORS[k]||'#8b949e'}">${v} ${k}</span>`)
      .join('');
    // finished-but-unacked tasks in this run - a cyan count pill in the
    // counts cell; clicking it applies the 'unreviewed' tag filter
    const unrev = AGENTS.filter(a=>!a.aux && a.needs_review &&
      (a.run_key||a.run_dir)===runKey(r)).length;
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
    // staleness is a computed diff (gen inputs vs publish baseline,
    // option fields vs the TOML itself); mid-planning stages would
    // double-label the row ('Preparing proposal' + 'Needs review'), so
    // the chip only means something once planning has stopped. The
    // title carries WHICH side is pending - the colour alone says less.
    const staleTip = r.pending
      ? [
          (r.pending.gen||[]).length
            ? 'request inputs changed (' +
              (r.pending.gen).join(', ') + ') - Regenerate tasks' : '',
          (r.pending.opts||[]).length
            ? 'options differ from the TOML (' +
              (r.pending.opts).join(', ') + ') - Apply options or ' +
              'regenerate' : '',
        ].filter(Boolean).join('; ')
      : 'fields changed since the proposal was generated';
    const staleChip = r.stale &&
      r.stage!=='generating' && r.stage!=='questions'
        ? ` <span class="stage error" title="${esc(staleTip)}">` +
          `Needs review</span>`:'';
    // 'unreviewed' marks a finished completion nobody acked - distinct
    // from the stale-proposal 'Needs review' chip above (that one means
    // the manifest drifted from its request, not a review ack).
    const revChip = r.needs_review ? ` ${unreviewedPill('this run')}` : '';
    h += `<tr class="runrow${runKey(r)===selectedRun?' selected':''}` +
      `${r.needs_review?' unreviewed':''}" ` +
      `data-run-key="${esc(runKey(r))}" tabindex="0" ` +
      `aria-haspopup="menu" ` +
      `aria-label="Filter to run ${esc(r.run)}` +
      `${r.needs_review?' · unreviewed':''}"><td class="wrap">` +
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
          `<b>${esc(r.run)}</b></button>`) +
      // Compact hides td <br> and squeezes td.wrap to one ellipsized
      // line, so these labels used to collapse INLINE after the run
      // name and clip away depending on name length / column width -
      // the "labels under the run table sometimes not visible" report.
      // .rlabels is a plain block box: the chips + genline keep their
      // own line under the name in BOTH densities (it replaces the <br>
      // that broke after the name at full density).
      `<div class="rlabels">` +
      (r.stage ? `<span class="stage ${esc(r.stage)}" ` +
        `title="${esc(STAGES[r.stage]||r.stage)}">` +
        `${esc(STAGES[r.stage]||r.stage)}` +
        (r.stage==='generating'
          ? `<span class="dots"><span>.</span><span>.</span><span>.</span></span>`
          : '') + `</span>` + staleChip + isoChip(r) +
        // the 'this IS a follow-up' chip: compact and neutral - the
        // source run's name moved to the tooltip, and a click opens the
        // source run's details (dlinked row pair while it shows)
        (r.followup_src
          ? (r.followup_src.run
            ? ` <button type="button" class="fulink" ` +
              `data-run-details="${esc(r.followup_src.key ||
                r.followup_src.run)}" ` +
              `title="follow-up drafted by run ` +
              `${esc(r.followup_src.run)}` +
              `${r.followup_src.manifest
                ? ` - source ${esc(r.followup_src.manifest)}` : ''}` +
              ` - open that run">follow-up</button>`
            : ` <span class="stage followup" ` +
              `title="imported follow-up proposal manifest">` +
              `follow-up</span>`)
          : '') +
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
        runStateChip(r) + staleChip + isoChip(r) + revChip +
        (r.followup ? fuChip(r.followup) : '') + `<br>` +
        `<span class="sub">${esc(base(r.dir))}</span>`) +
      `</div></td>` +
      `<td class="wrap">${r.description?esc(r.description):
        r.stage?'<span class="sub">Awaiting proposal</span>':'-'}</td>` +
      `<td>${(r.projects||[]).map(p=>
        `<button type="button" class="chip${projs.has(p)?' active':''}" ` +
        `data-proj="${esc(p)}" ` +
        `title="${projs.has(p)?'clear the project filter'
          :'filter to project '+esc(p)}">${esc(p)}</button>`).join('')}` +
      `</td>` +
      `<td>${loc(r.started_at)}</td>` +
      `<td>${r.ended_at?loc(r.ended_at):(r.started_at?'<b>running</b>':'-')}</td>` +
      `<td data-rel="${esc(runKey(r))}" ` +
      `title="${r.exec_start?'live - cumulative driver execution time':
        'cumulative driver execution time'}">${runElapsed(r,now)}</td>` +
      `<td class="counts">` +
      `${counts||'<span class="toksub">not planned</span>'}` +
      (unrev ? `<button type="button" class="rchip" ` +
        `data-facet="unreviewed" style="color:#39c5cf" ` +
        `title="${unrev} task(s) finished but not marked reviewed` +
        ` - click to filter">${unrev} unreviewed</button>` : '') +
      // the run table shows token lines at full density only (compact
      // hides .toksub); they always render in fmtTc short form - the
      // same labeled numbers the agents table's tokens column shows
      (r.plan_tokens ? `<br><span class="toksub" ` +
        `title="${esc(tokTitle(r.plan_tokens))}">planner: ` +
        `${fmtTc(r.plan_tokens)}` +
        (r.plan_cost?` · ${fmtC(r.plan_cost)}`:'') + `</span>` : '') +
      (r.tokens ? `<br><span class="toksub" ` +
        `title="${esc(tokTitle(r.tokens))}">steps: ` +
        `${fmtTc(r.tokens)}` +
        (r.cost?` · ${fmtC(r.cost)}`:'') + `</span>` : '') +
      (runUsd!=null ? `<br><span class="toksub" ` +
        `title="estimated self-serve ` +
        `cost, plan + steps${r.cost&&r.cost.unpriced
          ?`; ${r.cost.unpriced} step(s) have no rate`:''}">` +
        `est ${fmtC({usd:runUsd})}</span>` : '') +
      `</td><td>${r.max_parallel||'-'}</td>` +
      `<td class="hide-s wrap">${esc(r.dir)}</td></tr>`;
  }
  if (hiddenRuns > 0)
    h += `<tr><td colspan="9"><a href="#" data-rmore>show more ` +
      `(${hiddenRuns} finished run${hiddenRuns===1?'':'s'})</a> · ` +
      `<a href="#" data-rall>show all ${allRuns.length}</a></td></tr>`;
  else if (runsShown > SHOW_N)
    h += `<tr><td colspan="9"><a href="#" data-rless>` +
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
    // the group name IS the project-filter toggle (same gesture as the
    // chips up top - a filtered-in project's name clears it again)
    h += `<h3 class="proj"><button type="button" class="linkbtn" ` +
      `data-proj="${esc(slug)}" title="${projs.has(slug)
        ?'clear the project filter':'filter to project '+esc(slug)}">` +
      `${esc(slug)}</button> <span class="sub">` +
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
      '<th>ended</th><th class="hide-s">session</th>' +
      '<th title="the worker-reported OUTCOME: summary once the step ' +
      'ends; the live log tail while it runs; the conductor note when ' +
      'no outcome was reported">result</th></tr>';
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
      const aRk = a.run_key||a.run_dir||'';
      h += `<tr class="agen${a.needs_review?' unreviewed':''}` +
        `${a.aux?' aux':''}" ` +
        `data-i="${a._i}" ` +
        `data-run-key="${esc(aRk)}" data-akey="${esc(agentKey(a))}" ` +
        `tabindex="0" ` +
        `aria-haspopup="menu" aria-label="task ${esc(a.step)}` +
        `${a.needs_review?' · unreviewed':''}">` +
        `<td><span class="dot${pulse}" style="background:${c}" ` +
        `title="${a.status}${a.alive?' (pid '+a.pid+')':''}"></span>` +
        `${a.status}` +
        (a.status==='failed'&&a.exit_code===0&&a.status_line==null
          ? `<span class="sub" title="${a.tokens&&a.tokens.llm_steps
            ?'exited cleanly but never printed the STATUS: report line - '+
             'the turn ended mid-report; tokens show real work ran, so '+
             'committed work may sit on the branch (Resume finalizes the '+
             'merge without re-running)'
            :'exited cleanly before the turn produced anything and '+
             'recorded no tokens - a no-op run'}"> ·no STATUS</span>`:'') +
        (a.attempts>1 ? `<span class="sub" title="ran ` +
          `${a.attempts} session attempts (timeout retries)"> ` +
          `×${a.attempts}</span>` : '') +
        (a.timeout_ext_s ? `<span class="sub" title="stayed active ` +
          `${fmtS(a.timeout_ext_s)} past the timeout floor"> +ext</span>`
          : '') +
        (a.intr_pending && a.status==='running'
          ? `<span class="sub" title="an 'Interrupt + instruct' request ` +
            `is pending - the driver stops the worker on its next flag ` +
            `poll and resumes the same step"> ·interrupting</span>` : '') +
        (a.interrupts ? `<span class="sub" title="the operator stopped ` +
          `and resumed this step with added instructions ` +
          `${a.interrupts}x - same step, not a retry"> ` +
          `·intr×${a.interrupts}</span>` : '') +
        (a.status==='running' && a.reported
          ? `<span class="sub" title="the session already printed ` +
            `STATUS: ${esc(a.reported)} - the driver is finishing up ` +
            `(post-commit, verify gate, or waiting for the pool before ` +
            `merge); the recorded status catches up when teardown ` +
            `completes"> ·reported ${esc(a.reported)}</span>` : '') +
        (a.needs_review ? ` ${unreviewedPill('this task')}` : '') + `</td>` +
        // the step name is the task-modal affordance - a real button, so
        // keyboard activation is free; a bare row click no longer opens it.
        // data-agent-detail keys by agentKey, not row index, so a re-sorted
        // table can't open the wrong task.
        `<td class="step"><button type="button" class="linkbtn step-name" ` +
        `data-agent-detail="${esc(agentKey(a))}" ` +
        `title="open task details">${esc(a.step)}</button>${a.wt
          ? ` <button type="button" class="chip" data-facet="wt" ` +
            `style="color:#58a6ff" title="${esc(a.branch||'worktree')}">wt</button>` +
            (a.merged===true
             ? ` <button type="button" class="chip" data-facet="merged" ` +
               `style="color:#3fb950" title="${a.merged_at
                 ?esc('landed '+a.merged_at)
                 :'merged into the main checkout'}">merged</button>`
             : a.merged===false
             ? ` <button type="button" class="chip" data-facet="unmerged" ` +
               `style="color:#f0883e">unmerged</button>`
             : '') +
            (a.deferred
             ? ` <button type="button" class="chip" data-facet="deferred" ` +
               `style="color:#d2a8ff" title="merge held - this branch ` +
               `stays pending until an explicit merge from the run ` +
               `details${a.merge_kind==='alternative'
                 ? ' · alternative variant' : ''}">deferred</button>`
             : '')
          : ''}</td>` +
        // the run name applies the existing run filter (same path as the
        // run modal's 'Show only this run' and the row menu's 'Filter by
        // this item') instead of opening the modal. run_key is the stable
        // identity - a request-backed run's key survives its dir changing;
        // a task with no run association keeps plain text: there is no
        // filter target to apply.
        `<td class="rcell">${aRk && a.run!=null && a.run!==''
          ? `<button type="button" class="linkbtn run-link" ` +
            `data-filter-run="${esc(aRk)}" ` +
            `title="show only run ${esc(a.run)} and its tasks">` +
            `${esc(a.run)}</button>`
          : esc(a.run||'-')}</td>` +
        `<td class="hide-s" title="${a.model==='adaptive'&&a.model_actual
          ?esc(`adaptive resolved to ${a.model_actual}`):''}">` +
        `${esc(a.model||'-')}${a.model==='adaptive'&&a.model_actual
          ?` → ${esc(a.model_actual)}`:''}</td>` +
        `<td class="hide-s">${esc(a.effort||'-')}</td>` +
        `<td class="hide-s">${esc(a.mode||'-')}${a.cloud?' cloud':''}</td>` +
        `<td>${loc(a.started_at)}</td>` +
        `<td data-el="${a._i}">${elapsed(a,now)}</td>` +
        // same labeled fmtTc form as the run table's steps:/planner:
        // lines, at BOTH densities - 'tokens:' is the label here since
        // a task row has no planner/steps split
        `<td class="hide-s" title="${esc(tokTitle(a.tokens))}">` +
        `${a.tokens&&a.tokens.prompt!=null
          ? `<span class="toks">tokens: ${fmtTc(a.tokens)}</span>`
          : '-'}</td>` +
        `<td class="hide-s" title="${esc(costTitle(a.cost))}">` +
        `${fmtC(a.cost)}</td>` +
        `<td>${loc(a.ended_at)}</td>` + sessTd +
        (a.status==='running'
          ? `<td class="res${fresh?' fresh':''}" ` +
            `title="${esc(a.tail_last||'')}">${esc(a.tail_last||'')}` +
            `<span class="dots"><span>.</span><span>.</span><span>.</span>` +
            `</span></td>`
          : `<td class="wrap">` +
            // finished step: the worker-reported OUTCOME leads (what it
            // actually did); the conductor's diagnostic note drops to a
            // second .sub line - steps that never reported keep the note
            // as the cell text, as before
            (a.outcome ? esc(a.outcome) : esc(a.note||'-')) +
            (a.outcome && a.note
              ? `<br><span class="sub">${esc(a.note)}</span>` : '') +
            `</td>`) + `</tr>`;
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
  applySel();          // re-mark the open modal's row on the fresh DOM
  streamGenTail();
  if (detailIdx != null && !document.getElementById('backdrop').hidden){
    // agent rows re-sort as steps start/finish - keep the modal (and
    // its prompt form) on the SAME agent, not whatever row now sits at
    // the old index
    const j = detailKey==null ? detailIdx
      : AGENTS.findIndex(x=>agentKey(x)===detailKey);
    if (j >= 0 && AGENTS[j]){
      // a live selection inside the modal is mid-read/copy - rebuilding
      // would destroy it, so the refresh defers until it's released
      if (!modalSelActive()) showDetail(j, true);
    } else closeDetail(true);
  }
  // The open run-details modal re-renders on every payload change: the
  // pending-merge panel must reflect a merge's progress (busy flag,
  // landed entries dropping off, a kept stage appearing) without the
  // user reopening it. Ticked boxes ride mergeSel across the rebuild.
  if (modalRunKey!=null && !document.getElementById('backdrop').hidden){
    if (d.runs.some(x=>runKey(x)===modalRunKey))
      showRunDetails(modalRunKey);
    else closeDetail(true);
  }
  // the open summary view isn't repainted on refresh - its document is
  // static - but the review marks it surfaces are live; re-decorate in
  // place so a click flips pill/link/strikethrough without a reopen
  refreshSummaryMarks();
  const modalRun = modalRequest?.id && d.runs.find(r=>r.request_id===modalRequest.id);
  const modalStage = modalRun && (modalRun.stage||'launched');
  // an open request modal follows its stage - 'questions' swaps it for
  // the questionnaire (the form stays reachable via the run-name click);
  // keepStack: the re-opened same request is not a new Back entry
  if (modalStage && modalRequest.status!=='loading' && !modalRequest.dirty &&
      modalRequest.status!==modalStage)
    (modalStage==='questions' ? openQuestions : openRequest)(modalRequest.id, true);
  // A planner questionnaire is blocked on a human - pop it once per
  // question set when nothing else is modal (closing it marks the set
  // dismissed; the request's Answer button re-opens it).
  const qr = !modalQA && document.getElementById('backdrop').hidden &&
    d.runs.find(r=>r.stage==='questions' && (r.questions||[]).length);
  if (qr && !qDismissed.has(
      qr.request_id+'|'+qr.questions.map(q=>q.qid).join(','))){
    hideCtxMenu();
    openQuestions(qr.request_id); }
  syncTopbar();      // filter rows can wrap and change the bar height
  syncPoolBox();
}
// right-click on a run/task row opens the action menu INSTEAD of the
// browser menu - but the native one stays where it matters: editable
// fields, a live text selection (copy keeps working), right-click
// anywhere that isn't a row, and Shift+right-click as the explicit escape.
document.addEventListener('contextmenu', e=>{
  const t = e.target;
  if (t && t.closest && t.closest('#ctxmenu')){ e.preventDefault(); return; }
  hideCtxMenu();
  if (!t || !t.closest) return;
  if (e.shiftKey) return;
  if (t.isContentEditable ||
      t.closest('input,textarea,select,[contenteditable]:not([contenteditable="false"])'))
    return;
  const sel = typeof window!=='undefined' && window.getSelection &&
              window.getSelection();
  if (sel && !sel.isCollapsed && sel.toString && sel.toString().length)
    return;
  const row = t.closest('tr.runrow') || t.closest('tr.agen');
  if (!row) return;
  e.preventDefault();
  // a keyboard-triggered contextmenu event (a platform the keydown didn't
  // catch) arrives at 0,0 - anchor to the row's rect instead of a corner
  let x = e.clientX||0, y = e.clientY||0;
  if (!x && !y){
    const rc = row.getBoundingClientRect ? row.getBoundingClientRect()
      : {left:24, bottom:40};
    x = rc.left+8; y = rc.bottom;
  }
  openRowMenu(row, x, y);
});
// the menu dies with the view it belongs to (scrolled off / resized away)
document.addEventListener('scroll', ()=>{ if (ctxState) hideCtxMenu(); },
  true);
document.addEventListener('click', async e=>{
  const t = e.target;
  // context menu: an item click closes it and falls through to the normal
  // data-* dispatch below; ANY click outside it is a pure dismissal and
  // must never reach the row under it (no accidental filter/detail jump)
  const inCtx = t.closest && t.closest('#ctxmenu');
  if (ctxState){
    hideCtxMenu();
    if (!inCtx) return;
  }
  // a model-combobox option picks by flat index - data-mi disambiguates
  // a model that shows in both Recommendations and the full list
  const mopt = t.closest && t.closest('.mopt');
  if (mopt && mopt.dataset && mopt.dataset.mi!==undefined){
    e.preventDefault();
    const inp=document.getElementById(mopt.dataset.mfor);
    if (inp) mpopPick(inp, +mopt.dataset.mi);
    return;
  }
  // any other click outside an open model popup dismisses it - and like
  // a native datalist, clicking the input itself re-opens it (a focused
  // field gets no second focusin, so this is the only reopen gesture)
  for (const id of MODEL_FIELDS){
    const pop=document.getElementById(id+'-pop');
    if (t.id===id){ if (t.classList && t.classList.contains('mcombo-in'))
      mpopOpen(t); continue; }
    if (pop && !pop.hidden &&
        !(t.closest && t.closest('#'+id+'-pop'))){
      const inp=document.getElementById(id);
      if (inp) mpopClose(inp);
    }
  }
  if (t.id==='dx' || t.id==='backdrop'){
    closeDetail(); return; }
  // themed ask/alert buttons resolve the pending _ask promise; a click
  // on the dimmed frame itself dismisses like Cancel
  const askBtn = t.closest && t.closest('[data-ask]');
  if (askBtn){ _askDone(+askBtn.dataset.ask); return; }
  if (t.id==='askwrap'){ _askDone(-1); return; }
  // What's new tour: the button opens it, a click landing on the dim
  // (#tourwrap itself - the ring is pointer-events:none and the card is
  // a child) is a backdrop dismissal = Skip, and card buttons route
  // through their data-tour action
  if (t.id==='tour-btn'){ openTour(); return; }
  if (t.id==='tourwrap'){ tourClose(); return; }
  const tourActEl = t.closest && t.closest('[data-tour]');
  if (tourActEl){ tourAct(tourActEl.dataset.tour, tourActEl); return; }
  // dashboard power dialog
  if (t.id==='power-btn'){ openPower(); return; }
  if (t.id==='pw-cancel' || t.id==='powerwrap'){
    document.getElementById('powerwrap').hidden=true; return; }
  if (t.id==='pw-restart'){ powerAct('restart'); return; }
  if (t.id==='pw-stop'){ powerAct('stop'); return; }
  if (t.id==='pool-dec'){ poolBump(-1, t); return; }
  if (t.id==='pool-inc'){ poolBump(1, t); return; }
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
    const n = rmAtt.dataset.attachRm;
    if ((intrAttach[detailKey]||[]).some(a=>a.name===n)){
      intrAttach[detailKey]=intrAttach[detailKey].filter(
        a=>a.name!==n);
      renderIntrAttach();
    } else {
      rqAttachments=rqAttachments.filter(a=>a.name!==n);
      renderAttach(true);
      if (modalRequest) modalRequest.dirty=true;
      refreshPending();
    }
    return; }
  const pickAtt = t.closest && t.closest('[data-attach-pick]');
  if (pickAtt){
    e.preventDefault();
    const fi=document.getElementById(
      pickAtt.dataset.attachPick||'rq-file');
    if (fi && attachTarget()) fi.click();
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
  // 'Session summary' is a real link: plain click opens the in-app summary
  // view; ctrl/middle-click still reaches the standalone /summary page.
  const sumEl = t.closest && t.closest('[data-summary]');
  if (sumEl){
    if (!e.ctrlKey && !e.metaKey && !e.shiftKey && !e.altKey){
      e.preventDefault();
      openSummary(sumEl.dataset.summary);
    }
    return; }
  const snavEl = t.closest && t.closest('[data-snav]');
  if (snavEl){ openSummary(snavEl.dataset.snav, false); return; }
  const rsumEl = t.closest && t.closest('[data-run-summary]');
  if (rsumEl){ openRunSummary(rsumEl.dataset.runSummary); return; }
  const backEl = t.closest && t.closest('[data-modal-back]');
  if (backEl){ modalBack(); return; }
  const navEl = t.closest && t.closest('[data-nav]');
  if (navEl){
    const j = AGENTS.findIndex(x=>agentKey(x)===navEl.dataset.nav);
    if (j>=0){
      showDetail(j, true);
      const d = document.getElementById('detail');
      if (d) d.scrollTop = 0;
    }
    return; }
  // run modal's own prev/next - sibling semantics: the Back stack is
  // kept, not grown
  const nrunEl = t.closest && t.closest('[data-nav-run]');
  if (nrunEl){
    showRunDetails(nrunEl.dataset.navRun, true);
    const d = document.getElementById('detail');
    if (d) d.scrollTop = 0;
    return; }
  // pending-merge 'select all' / 'clear': ticks every mergeable box -
  // disabled (unverified/live) entries stay unticked, and each
  // alternative group keeps exactly one pick (a ticked member wins the
  // slot, else the first enabled variant)
  const pmSelEl = t.closest && t.closest('[data-pm-all],[data-pm-clear]');
  if (pmSelEl){
    e.preventDefault();
    if (modalRunKey==null) return;
    const sel = mergeSel[modalRunKey] || (mergeSel[modalRunKey] = new Set());
    const boxes=[...document.querySelectorAll('#detail input[data-pm]')];
    if (pmSelEl.dataset.pmClear!==undefined){
      boxes.forEach(b=>{ b.checked=false; sel.delete(b.dataset.pm); });
    } else {
      const groups={};
      boxes.forEach(b=>{ const alt=b.dataset.pmAlt;
        if (alt!==undefined && alt!=='') (groups[alt]??=[]).push(b); });
      const keep=new Set(Object.values(groups).map(g=>
        g.find(b=>b.checked && !b.disabled) ||
        g.find(b=>!b.disabled)).filter(Boolean));
      boxes.forEach(b=>{
        if (b.disabled) return;
        const alt=b.dataset.pmAlt;
        const ok = alt===undefined || alt==='' || keep.has(b);
        b.checked=ok;
        if (ok) sel.add(b.dataset.pm); else sel.delete(b.dataset.pm);
      });
    }
    const pb=document.getElementById('pm-go');
    if (pb) pb.classList.toggle('idle', !sel.size);
    const pmMsg=document.getElementById('pm-msg');
    if (pmMsg && !sel.size) pmMsg.textContent='';
    return; }
  // context-menu task entries - keyed by agentKey so a re-sorted table
  // can't open the wrong row (same keying the modal refresh uses)
  const adEl = t.closest && t.closest('[data-agent-detail]');
  if (adEl){
    const j = AGENTS.findIndex(x=>agentKey(x)===adEl.dataset.agentDetail);
    if (j>=0){
      // a modal->modal jump (e.g. the run modal's task table) stacks the
      // view it leaves so Back restores it; from the bare table snap is
      // null and nothing changes
      const snap = snapshotModal();
      if (snap) pushHist(snap);
      showDetail(j, true);
    }
    return; }
  const epEl = t.closest && t.closest('[data-edit-prompt]');
  if (epEl){
    const j = AGENTS.findIndex(x=>agentKey(x)===epEl.dataset.editPrompt);
    if (j>=0){
      showDetail(j);
      const p = document.getElementById('d-prompt');
      if (p && p.focus) p.focus();
    }
    return; }
  // trajectory expansion controls - state lives in trajOpen (keyed per
  // agent) so the 2s modal rebuild keeps what's open, and the full text
  // is fetched once per export then served from trajDetail
  const arcAll = t.closest && t.closest('[data-arc-all]');
  if (arcAll){
    const a = AGENTS.find(x=>agentKey(x)===detailKey);
    if (!a || !a.arc) return;
    const st = trajOpenFor(detailKey);
    a.arc.groups.forEach((g,gi)=>{
      st.g.add(gi);
      (g.entries||[]).forEach(e=>st.e.add(String(e.sid))); });
    trajEnsure(a);
    const j = AGENTS.findIndex(x=>agentKey(x)===detailKey);
    if (j>=0) showDetail(j, true);
    return; }
  const arcNone = t.closest && t.closest('[data-arc-none]');
  if (arcNone){
    const st = trajOpenFor(detailKey);
    st.g.clear(); st.e.clear();
    const j = AGENTS.findIndex(x=>agentKey(x)===detailKey);
    if (j>=0) showDetail(j, true);
    return; }
  const arcCopy = t.closest && t.closest('[data-arc-copy]');
  if (arcCopy){
    const a = AGENTS.find(x=>agentKey(x)===detailKey);
    const key = a && trajKeyOf(a);
    const det = key && trajDetail[key];
    const ent = det && det.entries &&
      det.entries[arcCopy.dataset.arcCopy];
    if (ent)
      copyText(arcPlainText(ent)).then(ok=>{
        arcCopy.textContent = ok ? 'Copied' : 'Copy failed';
      });
    return; }
  if (t.dataset && t.dataset.resumeRun){
    if(!await askConfirm('Resume this run? Every non-done step re-runs in a fresh ' +
                'session with a fresh timeout - covers paused, stopped ' +
                'and interrupted (crashed) runs alike.')) return;
    t.disabled=true; t.textContent='Resuming…';
    try {
      await api('POST',`/api/runs/${encodeURIComponent(t.dataset.resumeRun)}/resume`,{});
      await refresh();
    } catch(err){ await showAlert('Resume failed: '+err.message); t.disabled=false;
      t.textContent='Resume unfinished steps'; }
    return; }
  if (t.dataset && t.dataset.mergeRun){
    const rk = t.dataset.rk;
    const steps=[...(document.querySelectorAll(
      '#detail [data-pm]:checked')||[])].map(b=>b.dataset.pm);
    const msg=document.getElementById('pm-msg');
    if(!steps.length){
      if (msg) msg.textContent='Tick at least one pending change above ' +
        '(unverified entries are never merged).';
      return; }
    if(!await askConfirm(`Merge the ${steps.length} ticked pending change(s) ` +
                `(${steps.join(', ')}) into their target branches? ` +
                `Verified deferred work is staged, re-verified and ` +
                `merged atomically; conflicts stay parked for manual ` +
                `resolution (or a resolve-* session when merge_resolver ` +
                `is set). Everything unticked stays parked - no steps ` +
                `are re-run.`)) return;
    t.disabled=true; t.textContent='Merging…';
    if (msg) msg.textContent='staged merge running - a resolver session ' +
      'can make this take minutes; the page keeps refreshing';
    try {
      const out=await api('POST',
        `/api/runs/${encodeURIComponent(t.dataset.mergeRun)}/merge`,
        {steps});
      if (out.merging){
        // The merge now runs as a detached conductor process (a resolver
        // session can take tens of minutes and must outlive this server):
        // outcomes land in the pending list + event log as they happen.
        const note=`merge running in background (pid ${out.pid}) - ` +
          `each pending change updates live; merge.launch.log in the ` +
          `run dir is the process log`;
        if (rk){ mergeResult[rk]=note; await refresh(); showRunDetails(rk); }
        else await showAlert(note);
        return; }
      const bits=[];
      if (out.merged?.length) bits.push(`merged: ${out.merged.join(', ')}`);
      if (out.conflicted?.length)
        bits.push(`still conflicted (worktree kept): ${out.conflicted.join(', ')}`);
      if (out.skipped?.length) bits.push(`skipped: ${out.skipped.join(', ')}`);
      if (out.unverified?.length)
        bits.push(`not verified, kept pending: ${out.unverified.join(', ')}`);
      if (out.verify_failed?.length)
        bits.push(`combined verify failed (stage kept): ${out.verify_failed.join(', ')}`);
      if (out.moved?.length)
        bits.push(`target moved mid-merge, nothing landed: ${out.moved.join(', ')}`);
      if (out.busy?.length)
        bits.push(`repo busy with another merge: ${out.busy.join(', ')}`);
      if (out.kept?.length)
        bits.push(`prior merge attempt kept for inspection: ${out.kept.join(', ')}`);
      if (rk){ mergeResult[rk]=bits.join(' | ')||'Nothing pending to merge.';
        steps.forEach(s=>(mergeSel[rk]||new Set()).delete(s)); }
      await refresh();
      if (rk) showRunDetails(rk); else
        await showAlert(bits.length ? bits.join(' | ') :
                        'Nothing pending to merge.');
    } catch(err){
      if (rk){ mergeResult[rk]='Merge failed: '+err.message;
        await refresh(); showRunDetails(rk); }
      else await showAlert('Merge failed: '+err.message);
      t.disabled=false; t.textContent='Merge selected pending work'; }
    return; }
  if (t.dataset && (t.dataset.skipStep || t.dataset.unskipStep)){
    const un = !!t.dataset.unskipStep;
    const dir = t.dataset.skipStep || t.dataset.unskipStep;
    if(!await askConfirm(un
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
        await showAlert(`Unskipped - '${t.dataset.step}' already passed its turn ` +
              `this run, so it re-runs on the next resume.`);
    } catch(err){ await showAlert((un?'Unskip':'Skip')+' failed: '+err.message);
      t.disabled=false; }
    return; }
  if (t.dataset && t.dataset.stopAgent){
    if(!await askConfirm(`Stop step '${t.dataset.step}'? Its devin process tree ` +
                `is killed (committed work stays) and the step records ` +
                `'stopped'. The run proceeds to the next step; resume ` +
                `re-runs it.`)) return;
    t.disabled=true;
    try {
      await api('POST',
        `/api/runs/${encodeURIComponent(t.dataset.stopAgent)}/steps/` +
        `${encodeURIComponent(t.dataset.step)}/stop`,{});
      closeDetail(true); await refresh();
    } catch(err){ await showAlert('Stop failed: '+err.message); t.disabled=false; }
    return; }
  const intrOpenEl = t.closest && t.closest('[data-intr-open]');
  if (intrOpenEl){
    // opens the composer section on that task's modal - composing is
    // free; nothing reaches the server until the explicit send
    const j = AGENTS.findIndex(
      x=>agentKey(x)===intrOpenEl.dataset.intrOpen);
    if (j>=0){
      intrOpen[agentKey(AGENTS[j])]=true;
      showDetail(j, true);
      const p = document.getElementById('d-instr');
      if (p && p.focus) p.focus();
    }
    return; }
  const intrCancel = t.closest && t.closest('[data-intr-cancel]');
  if (intrCancel){
    // cancel is a pure view change - the worker is never touched
    intrOpen[detailKey]=false;
    delete intrSent[detailKey];
    const j = AGENTS.findIndex(x=>agentKey(x)===detailKey);
    if (j>=0) showDetail(j, true);
    return; }
  const intrSend = t.closest && t.closest('[data-intr-send]');
  if (intrSend){
    const dir = intrSend.dataset.intrSend, step = intrSend.dataset.step;
    const key = detailKey;
    const ta = document.getElementById('d-instr');
    const instructions = (ta ? ta.value : '').trim();
    const msg = m => { const el=document.getElementById('d-instr-msg');
      if (el){ el.hidden=false; el.className='notice error';
               el.textContent=m; } };
    if (!instructions){
      msg('Write the instructions first - they are what the resumed ' +
          'session continues with.'); return; }
    if(!await askConfirm(`Interrupt '${step}'? Its worker is killed now and the ` +
                `same step resumes in a fresh session carrying your ` +
                `instructions (plus ${(intrAttach[key]||[]).length} ` +
                `attached image(s)). Committed work and the worktree ` +
                `stay; the run keeps going.`)) return;
    intrSend.disabled=true;
    try {
      await api('POST',
        `/api/runs/${encodeURIComponent(dir)}/steps/` +
        `${encodeURIComponent(step)}/interrupt`,
        {instructions,
         attachments:(intrAttach[key]||[]).map(
           a=>({name:a.name,path:a.path}))});
      const a = AGENTS.find(x=>agentKey(x)===key);
      intrSent[key] = {at:(a && a.interrupts)||0};
      delete intrDrafts[key]; intrAttach[key]=[];
      const j = AGENTS.findIndex(x=>agentKey(x)===key);
      if (j>=0) showDetail(j, true);
      await refresh();
    } catch(err){
      msg('Interrupt failed: '+err.message);
      intrSend.disabled=false; }
    return; }
  if (t.dataset && t.dataset.stopGen){
    if(!await askConfirm('Stop preparing this proposal? The planner session is ' +
                'cancelled - the request stays editable and any previous ' +
                'proposal is kept.')) return;
    t.disabled=true;
    try {
      await api('POST',
        `/api/requests/${encodeURIComponent(t.dataset.stopGen)}/stop`,{});
      closeDetail(true); await refresh();
    } catch(err){ await showAlert('Stop failed: '+err.message); t.disabled=false; }
    return; }
  if (t.dataset && t.dataset.setTimeout){
    const inp=document.getElementById('d-timeout');
    const mins=parseFloat(inp?.value);
    if(!(mins>=1)){ await showAlert('Enter a timeout in minutes (1-1440).'); return; }
    t.disabled=true;
    try {
      await api('POST',
        `/api/runs/${encodeURIComponent(t.dataset.setTimeout)}/steps/` +
        `${encodeURIComponent(t.dataset.step)}/timeout`,{minutes:mins});
      await refresh();
    } catch(err){ await showAlert('Timeout update failed: '+err.message);
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
    } catch(err){ await showAlert((revert?'Revert':'Prompt save')+' failed: ' +
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
    } catch(err){ await showAlert('Notes save failed: '+err.message);
      t.disabled=false; }
    return; }
  if (t.dataset && t.dataset.pauseRun){
    if(!await askConfirm('Pause this run? In-flight steps run to completion, ' +
                'then the run holds - NO further step launches ' +
                'regardless of on_failure. It stays resumable across a ' +
                'dashboard or machine restart. To kill live work ' +
                'immediately, use Stop run now instead.')) return;
    t.disabled=true;
    try {
      const out=await api('POST',
        `/api/runs/${encodeURIComponent(t.dataset.pauseRun)}/pause`,{});
      closeDetail(true); await refresh();
      await showAlert(out.pausing?.length
        ? `Pause set: ${out.pausing.join(', ')} finishes first, ` +
          `then the run holds.` :
          'Pause set - nothing was in flight; the run is held.');
    } catch(err){ await showAlert('Pause failed: '+err.message); t.disabled=false; }
    return; }
  if (t.dataset && (t.dataset.stopRun || t.dataset.drainRun)){
    const drain=!!t.dataset.drainRun, dir=t.dataset.stopRun||t.dataset.drainRun;
    if(!await askConfirm(drain
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
      if (!drain) await showAlert(out.stopping?.length
        ? `Stopping: ${out.stopping.join(', ')}` :
          'Stop requested - nothing was live.');
    } catch(err){ await showAlert('Stop failed: '+err.message); t.disabled=false; }
    return; }
  if (t.dataset && t.dataset.archRun){
    const on=t.dataset.archSet==='1';
    t.disabled=true;
    try {
      await api('POST',
        `/api/runs/${encodeURIComponent(t.dataset.archRun)}/archive`,
        {archived:on});
      closeDetail(true); await refresh();
    } catch(err){ await showAlert((on?'Archive':'Unarchive')+' failed: ' +
      err.message); t.disabled=false; }
    return; }
  // Review marks: run-level (data-review-run) and task-level
  // (data-review-step + data-step). No confirm - the mark is a private,
  // reversible 'you looked at this' flag, and the modal stays open so
  // the pill + kv line flip in place on the refresh. data-review-seq
  // rides the POST so an ack on a view older than the newest completion
  // is refused instead of silently covering it.
  if (t.dataset && (t.dataset.reviewRun || t.dataset.reviewStep)){
    const dir=t.dataset.reviewRun||t.dataset.reviewStep;
    const reviewed=t.dataset.reviewSet==='1';
    const rawSeq=t.dataset.reviewSeq;
    t.disabled=true;
    try {
      await api('POST',
        `/api/runs/${encodeURIComponent(dir)}` +
        (t.dataset.reviewStep
          ? `/steps/${encodeURIComponent(t.dataset.step)}` : '') +
        `/review`,
        {reviewed, seq: (rawSeq===''||rawSeq==null) ? null : +rawSeq});
      await refresh();
    } catch(err){ await showAlert('Review update failed: '+err.message);
      t.disabled=false; }
    return; }
  if (t.dataset && t.dataset.deleteRun){
    const dir=t.dataset.deleteRun;
    const r=(lastData?.runs||[]).find(x=>base(x.dir)===dir);
    const unmerged=AGENTS.filter(a=>a.branch && a.merged!==true &&
      (a.run_key||a.run_dir)===(r?runKey(r):'')).length;
    if(!await askConfirm(`Delete run '${r?r.run:dir}'? Its whole record - state, ` +
      `logs, prompts, session exports - moves to Trash, recoverable for ` +
      `48h (Undo right after, or the Trash view). Empty trash is what ` +
      `deletes it for good.` +
      (unmerged?` ${unmerged} worktree branch(es) are still unmerged - ` +
        `merge them first or they stay parked.`:''))) return;
    t.disabled=true;
    try {
      const out=await api('POST',`/api/runs/${encodeURIComponent(dir)}/delete`,
                {confirm:true});
      if (out && out.trash_id) lastTrash={id:out.trash_id,
        label:`run ${r?r.run:dir}`};
      closeDetail(true); await refresh();
    } catch(err){ await showAlert('Delete failed: '+err.message); t.disabled=false; }
    return; }
  if (t.dataset && t.dataset.summarizeRun){
    const rk = t.dataset.rk;
    if(!await askConfirm('Spawn a Devin session to write an executive summary ' +
                'for this run? It reads the steps\u2019 own closing ' +
                'reports and writes run.summary.md (replacing any ' +
                'existing one). The run itself is untouched.')) return;
    t.disabled=true; t.textContent='Generating\u2026';
    try {
      await api('POST',
        `/api/runs/${encodeURIComponent(t.dataset.summarizeRun)}/summarize`,
        {});
      await refresh();
      if (rk) showRunDetails(rk, true);
    } catch(err){ await showAlert('Summarize failed: '+err.message);
      t.disabled=false; t.textContent='Generate LLM summary'; }
    return; }
  // closest(), not t.dataset - like the other attr dispatches: the task
  // table's run-name button wraps the label text, and a click landing on
  // inner markup must still find the attr rather than bubble to the row
  const filtEl = t.closest && t.closest('[data-filter-run]');
  if (filtEl){
    selectedRun=filtEl.dataset.filterRun;
    closeDetail(true); if (lastData) render(lastData); return; }
  if (t.dataset && t.dataset.clearRun!==undefined){
    selectedRun=null; if (lastData) render(lastData); return; }
  if (t.dataset && t.dataset.reset!==undefined){
    only.clear(); facets.clear(); projs.clear();
    if (lastData) render(lastData); return; }
  if (t.dataset && t.dataset.arch!==undefined){   // 'N archived' view chip
    if (chipBusy.has('arch')) return;
    showArchived=!showArchived;
    store.setItem('ac-arch', showArchived?'1':'0');
    await busyRefresh(t,'arch'); return;
  }
  if (t.dataset && t.dataset.trash!==undefined){   // 'N trash' view chip
    if (chipBusy.has('trash')) return;
    showTrash=!showTrash;
    store.setItem('ac-trash', showTrash?'1':'0');
    await busyRefresh(t,'trash'); return;
  }
  if (t.dataset && t.dataset.trashOpen!==undefined){   // Undo bar link
    showTrash=true; store.setItem('ac-trash','1');
    await busyRefresh(t,'trash');
    const tw=document.getElementById('trashwrap');
    if (tw && tw.scrollIntoView) tw.scrollIntoView();
    return;
  }
  if (t.dataset && t.dataset.undoTrash){   // Undo a delete/discard
    const id=t.dataset.undoTrash;
    t.disabled=true;
    try {
      const out=await api('POST',
        `/api/trash/${encodeURIComponent(id)}/restore`,{});
      lastTrash=null; await busyRefresh(t);
      await showAlert(`Restored ${out.restored_as||out.restored||'it'}.`);
    } catch(err){ await showAlert('Restore failed: '+err.message); t.disabled=false; }
    return; }
  if (t.dataset && t.dataset.undoDismiss!==undefined){
    lastTrash=null; if (lastData) render(lastData); return; }
  if (t.dataset && t.dataset.restoreTrash){   // Trash view row
    const id=t.dataset.restoreTrash;
    t.disabled=true;
    try {
      const out=await api('POST',
        `/api/trash/${encodeURIComponent(id)}/restore`,{});
      if (lastTrash && lastTrash.id===id) lastTrash=null;
      await busyRefresh(t);
      await showAlert(`Restored ${out.restored_as||out.restored||'it'}.`);
    } catch(err){ await showAlert('Restore failed: '+err.message); t.disabled=false; }
    return; }
  if (t.dataset && t.dataset.emptyTrash!==undefined){
    const n=(lastData?.trash||[]).length;
    if(!await askConfirm(`Empty trash? ${n} item(s) are PERMANENTLY deleted now ` +
      `- run records, logs, prompts and proposal drafts cannot be ` +
      `recovered after this.`)) return;
    t.disabled=true;
    try {
      await api('POST','/api/trash/empty',{confirm:true});
      lastTrash=null; await busyRefresh(t);
    } catch(err){ await showAlert('Empty trash failed: '+err.message);
      t.disabled=false; }
    return; }
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
  if (t.dataset && t.dataset.proj){          // project filter toggle:
    const p=t.dataset.proj;                  // sticky-bar chips, project
    projs.has(p)?projs.delete(p):projs.add(p);  // cells, group names
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
  // no tr.agen catch-all on purpose: a bare click on a task row does
  // nothing now - the named controls (step name -> task modal via
  // data-agent-detail, run name -> run filter via data-filter-run, the
  // facet chips) are the row's only actions, so text selection and
  // misclicks can't pop a modal.
});
// <details> toggling: 'toggle' does not bubble, so this runs in the
// CAPTURE phase to catch arc group/entry flips. Persisted in trajOpen
// so the 2s modal rebuild doesn't collapse them; opening an entry also
// kicks the one-shot detail fetch.
document.addEventListener('toggle', e=>{
  const d = e.target;
  if (!d || !d.classList || !d.closest) return;
  // the run modal's sessions list is a plain <details> too - persist its
  // expansion separately (tests register ONE handler per event type, so
  // this stays folded in here, never a second 'toggle' listener)
  if (d.classList.contains('sessx')){
    if (runModalKey) sessOpen[runModalKey]=d.open;
    return;
  }
  const box = d.closest('.arc[data-akey]');
  if (!box) return;
  const st = trajOpenFor(box.dataset.akey);
  if (d.classList.contains('arcgrp')){
    const gi = +d.dataset.ag;
    if (d.open) st.g.add(gi); else st.g.delete(gi);
    return;
  }
  if (!d.classList.contains('arcent')) return;
  const sid = String(d.dataset.asid);
  if (!d.open){ st.e.delete(sid); return; }
  st.e.add(sid);
  const a = AGENTS.find(x=>agentKey(x)===box.dataset.akey);
  if (!a) return;
  trajEnsure(a);
  const body = d.querySelector('.arcfull');
  if (body) body.innerHTML = arcFullHTML(a, sid);   // cache hit or a
}, true);                                           // 'loading…' note
document.addEventListener('input', e=>{
  // typed questionnaire text survives an option-toggle re-render
  if (modalQA && e.target.id==='q-free') modalQA.free=e.target.value;
  // unsaved prompt/notes/instructions text survives the 2s modal
  // rebuild - keyed by run_key|step so a re-sorted row never inherits
  // another step's draft
  if ((e.target.id==='d-prompt'||e.target.id==='d-rnotes'||
       e.target.id==='d-instr')
      && detailKey!=null)
    (e.target.id==='d-prompt'?promptDrafts:
      e.target.id==='d-rnotes'?notesDrafts:intrDrafts)[detailKey]=
      e.target.value;
  // timeout Save stays disabled until the field actually differs from
  // the applied value - a no-op click would just rewrite the flag
  if (e.target.id==='d-timeout'){
    const sv=document.getElementById('d-timeout-save');
    if (sv) sv.disabled =
      e.target.value===(e.target.dataset.timeoutOrig||'');
  }
  if (modalRequest && e.target.id?.startsWith('rq-')){
    modalRequest.dirty=true;
    const msg=document.getElementById('form-msg');
    if (msg) msg.hidden=true;
    // syncEffort may rewrite the effort input when its model changed -
    // run it first so the pending diff sees the settled value
    if (e.target.id==='rq-merge' || e.target.id==='rq-worktrees' ||
        e.target.id==='rq-parallel') syncMergeTiming();
    if (e.target.id==='rq-model' || e.target.id==='rq-plan_model'){
      syncEffort(e.target,
                 e.target.id==='rq-model'?'rq-effort':'rq-plan_effort',
                 e.target.id==='rq-model'?'rq-efforts':'rq-plan_efforts');
      // every keystroke re-filters Recommendations; the All models group
      // below them always lists the full catalog regardless of matches
      mpopOpen(e.target);
    }
    // stale state is a live diff against the publish baselines - edit
    // and revert both re-evaluate, so returning a field to its baseline
    // clears its mark without a save
    refreshPending();
  }
});
// model combobox focus = the native-datalist gesture: the popup opens on
// focus and closes when the field loses it (a pick keeps focus on the
// input, so the popup closing on its own click never looks like a blur)
document.addEventListener('focusin', e=>{
  if (e.target && e.target.classList &&
      e.target.classList.contains('mcombo-in')) mpopOpen(e.target);
});
document.addEventListener('focusout', e=>{
  if (e.target && e.target.classList &&
      e.target.classList.contains('mcombo-in')) mpopClose(e.target);
});
// a mousedown inside an open model popup (option or scrollbar) must not
// blur the input first - the option click itself does the picking
document.addEventListener('mousedown', e=>{
  if (e.target.closest && e.target.closest('.mpop')) e.preventDefault();
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
  const ta=attachTarget();
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
// the paperclip's hidden file input shares the drop/paste upload path;
// pending-merge checkboxes ride the same listener (one 'change'
// registration keeps every delegate reachable - including under the
// node test stubs, which keep only the last-registered handler).
// A canceled dialog delivers no files and changes nothing; clearing the
// value lets picking the same file twice still fire 'change'.
document.addEventListener('change', e=>{
  const fi=e.target;
  if(fi && (fi===document.getElementById('rq-file') ||
            fi===document.getElementById('d-instr-file'))){
    const files=[...(fi.files||[])];
    try{fi.value='';}catch(err){}
    if(files.length) attachFiles(files);
  }
  // pending-merge ticks live in mergeSel (they survive the modal's
  // per-refresh rebuild); alternatives are same-wave variants of the
  // same task, so ticking one unticks its group's siblings - the merge
  // must never silently combine them.
  const t=e.target;
  if (!(t.dataset && t.dataset.pm!==undefined) || modalRunKey==null) return;
  const sel = mergeSel[modalRunKey] || (mergeSel[modalRunKey] = new Set());
  if (t.checked){
    sel.add(t.dataset.pm);
    if (t.dataset.pmAlt!==undefined && t.dataset.pmAlt!=='')
      document.querySelectorAll(
        `#detail [data-pm-alt="${t.dataset.pmAlt}"]`).forEach(b=>{
        if (b!==t){ b.checked=false; sel.delete(b.dataset.pm); }});
    const msg=document.getElementById('pm-msg');
    if (msg && t.dataset.pmAlt) msg.textContent='alternative picked - ' +
      'its sibling variants stay parked; merge them one at a time.';
  } else sel.delete(t.dataset.pm);
  // the merge button reads as outline-only while nothing is ticked and
  // fills once there is work selected - no re-render needed
  const pb=document.getElementById('pm-go');
  if (pb) pb.classList.toggle('idle', !sel.size);
  const pmMsg=document.getElementById('pm-msg');
  if (pmMsg && !sel.size) pmMsg.textContent='';
});
document.addEventListener('keydown', e=>{
  // the open row menu owns the keyboard: Esc dismisses (focus returns to
  // the row it came from) and arrows/Home/End move between items; a
  // focused item's Enter/Space is the native button activation
  if (ctxState){
    if (e.key==='Escape'){
      e.preventDefault();
      const r = ctxState.returnEl;
      hideCtxMenu();
      if (r && r.isConnected !== false && r.focus) r.focus();
      return;
    }
    if (e.key==='ArrowDown'||e.key==='ArrowUp'||
        e.key==='Home'||e.key==='End'){
      e.preventDefault(); ctxMoveFocus(e.key); return; }
    // Tab stays trapped inside the menu like a native one (Esc leaves)
    if (e.key==='Tab'){
      e.preventDefault(); ctxMoveFocus(e.shiftKey?'ArrowUp':'ArrowDown');
      return; }
    // Enter/Space landing outside the menu while it's open is treated like
    // a dismissal - never a stray activation of the row behind the click
    if ((e.key==='Enter'||e.key===' ') &&
        e.target.closest && !e.target.closest('#ctxmenu')){
      e.preventDefault(); hideCtxMenu(); return; }
  }
  // model combobox: while its popup is open the field owns arrows/Home/
  // End/Enter/Esc - Esc closes only the popup (the modal stays up), and
  // the flat item order spans Recommendations + All models so a model
  // listed in both is two distinct, separately reachable options
  if (e.target && e.target.classList &&
      e.target.classList.contains('mcombo-in')){
    const inp=e.target,
          pop=document.getElementById(inp.id+'-pop'),
          open=!!(pop&&!pop.hidden);
    if (e.key==='ArrowDown'||e.key==='ArrowUp'){
      e.preventDefault();
      if(!open)mpopOpen(inp);      // closed: the arrow opens AND moves
      const st=mcombo(inp); if(!st.items.length)return;
      const i=st.active<0 ? (e.key==='ArrowDown'?0:st.items.length-1)
        : (st.active+(e.key==='ArrowDown'?1:-1)+st.items.length)
          %st.items.length;
      mpopActivate(inp,i);return;
    }
    if (open && (e.key==='Home'||e.key==='End')){
      e.preventDefault();
      const st=mcombo(inp); if(!st.items.length)return;
      mpopActivate(inp,e.key==='Home'?0:st.items.length-1);return;
    }
    if (e.key==='Enter'&&open){
      const st=mcombo(inp);
      if(st.active>=0){e.preventDefault();mpopPick(inp,st.active);}
      else mpopClose(inp);
      return;
    }
    if (e.key==='Escape'&&open){
      e.preventDefault();mpopClose(inp);return;
    }
  }
  // keyboard equivalent of right-click: the Menu key or Shift+F10 on a
  // focused run/task row opens the same menu anchored to the row (not while
  // a modal is up - the rows it acts on are covered anyway)
  if ((e.key==='ContextMenu' || (e.key==='F10' && e.shiftKey)) &&
      !e.ctrlKey && !e.altKey &&
      document.getElementById('backdrop').hidden){
    const el = (e.target && e.target.closest) ? e.target
      : (document.activeElement || {});
    const row = el.closest && el.closest('tr.runrow,tr.agen');
    if (row){
      e.preventDefault();
      const rc = row.getBoundingClientRect ? row.getBoundingClientRect()
        : {left:24, top:24, bottom:40};
      openRowMenu(row, rc.left+8, rc.bottom);
      return;
    }
  }
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
  // task rows are focusable too (Shift+F10 works on them) - Enter/Space on
  // one is the left-click equivalent: open its details
  if ((e.key==='Enter'||e.key===' ') &&
      e.target.matches && e.target.matches('tr.agen')){
    e.preventDefault();
    showDetail(+e.target.dataset.i);
  }
  // Left/Right walks the open task's (or open summary's, or open run's)
  // chain - same links as the .dnav arrows - but never while typing in
  // a form field.
  if ((e.key==='ArrowLeft'||e.key==='ArrowRight') &&
      (detailKey!=null || summaryView || runModalKey!=null) &&
      !document.getElementById('backdrop').hidden){
    const ae = document.activeElement;
    if (!(ae && /^(INPUT|TEXTAREA|SELECT)$/.test(ae.tagName||''))){
      e.preventDefault();
      const dir = e.key==='ArrowLeft'?-1:1;
      if (summaryView) summaryNav(dir);
      else if (detailKey!=null) navStep(dir);
      else navRun(dir);
      return;
    }
  }
  if (e.key==='Escape'){
    if (!document.getElementById('askwrap').hidden){ _askDone(-1); return; }
    if (!document.getElementById('powerwrap').hidden){
      document.getElementById('powerwrap').hidden=true; return; }
    if (!document.getElementById('backdrop').hidden){ closeDetail(); return; }
    // an open tour dismisses like Skip - never a viewed/snooze POST; a
    // suspended one stays put (the modal above it owns Escape)
    if (tourSt) tourClose();
  }
});
// hovering a run row highlights its agent rows across all project tables,
// plus the OTHER half of a follow-up pair: a run that produced a follow-up
// proposal highlights that request row (or the run it launched), and a
// follow-up request/run row highlights the run that drafted it.
// hoverRun persists across the 2s re-render so the highlight doesn't flicker.
let hoverRun = null;
function hoverLinked(){
  const s = new Set();
  if (!hoverRun) return s;
  const rows = (lastData && lastData.runs) || [];
  const row = rows.find(x=>runKey(x)===hoverRun);
  const fs = row && row.followup_src;
  if (fs && (fs.key || fs.run)) s.add(fs.key || fs.run);
  const fq = row && row.followup;
  if (fq && fq.request) s.add('request:'+fq.request);
  for (const r2 of rows){
    const f2 = r2.followup_src;
    if (f2 && (f2.key || f2.run) === hoverRun) s.add(runKey(r2));
    const q2 = r2.followup;
    if (q2 && q2.request && ('request:'+q2.request) === hoverRun)
      s.add(runKey(r2));
  }
  s.delete(hoverRun);
  return s;
}
function applyHl(){
  const link = hoverLinked();
  document.querySelectorAll('tr[data-run-key]').forEach(x=>
    x.classList.toggle('hl', x.dataset.runKey===selectedRun ||
      !!hoverRun && (x.dataset.runKey===hoverRun ||
                     link.has(x.dataset.runKey))));
}
// .dsel marks the row whose details are on screen: the open task modal's
// agent row, an open run-details/request/questionnaire modal's run row, a
// summary view's subject row. Rows are keyed by agentKey ('a:') or runKey
// ('r:') - never a row index - so a re-sort, a filter or the 2s refresh
// can't paint the wrong row; a sibling nav moves the mark because
// detailKey itself moved, and closing the dialog clears it via closeDetail.
function selKeys(){
  const s = new Set();
  if (detailKey!=null) s.add('a:'+detailKey);
  if (summaryView){
    const k = String(summaryView.key);
    s.add(k.startsWith('run|') ? 'r:'+k.slice(4) : 'a:'+k);
  }
  if (runModalKey) s.add('r:'+runModalKey);
  if (modalRequest && modalRequest.id)
    s.add('r:request:'+modalRequest.id);
  if (modalQA && modalQA.id) s.add('r:request:'+modalQA.id);
  return s;
}
// Run rows RELATED to the open view but not the view itself - the other
// half of a follow-up pair (a source run <-> the proposal it drafted)
// and the run a task/summary modal's step belongs to. They render
// .dlinked, a notch under .dsel which marks the open view's own row.
function linkKeys(){
  const s = new Set();
  const runs = (lastData && lastData.runs) || [];
  const rowOf = rk => runs.find(x=>runKey(x)===rk) || {};
  const fuReq = rk => {
    const f = rowOf(rk).followup;
    return f && f.request; };
  const srcOf = rk => {
    const f = rowOf(rk).followup_src;
    return f && (f.key || f.run); };
  const ownRunOf = akey => {
    const a = AGENTS.find(x=>agentKey(x)===akey);
    return a && (a.run_key||a.run_dir); };
  const linkRun = rk => {
    const rid = fuReq(rk);
    if (rid) s.add('r:request:'+rid);
    const sk = srcOf(rk);
    if (sk) s.add('r:'+sk); };
  if (runModalKey) linkRun(runModalKey);
  const mrec = (modalRequest && modalRequest.record) || {};
  if (mrec.source_run_key || mrec.source_run)
    s.add('r:' + (mrec.source_run_key || mrec.source_run));
  if (detailKey!=null){
    const rk = ownRunOf(detailKey);
    if (rk){ s.add('r:'+rk); linkRun(rk); }
  }
  if (summaryView){
    const k = String(summaryView.key);
    const rk = k.startsWith('run|') ? k.slice(4) : ownRunOf(k);
    if (rk){ s.add('r:'+rk); linkRun(rk); }
  }
  // the open view's own row keeps the stronger .dsel mark
  const strong = selKeys();
  for (const k of strong) s.delete(k);
  return s;
}
function applySel(){
  const want = selKeys(), link = linkKeys();
  document.querySelectorAll('tr.runrow[data-run-key],tr.agen[data-akey]')
    .forEach(x=>{
      const agen = x.classList && x.classList.contains('agen');
      const k = agen?x.dataset.akey:x.dataset.runKey;
      x.classList.toggle('dsel',
        want.has((agen?'a:':'r:') + k));
      x.classList.toggle('dlinked', !agen && link.has('r:'+k));
    });
  // a What's-new tour suspends beneath an open details modal - the
  // dialog is the stronger layer; closing it resumes the same card,
  // re-placed against whatever the page did behind it
  const tw = document.getElementById('tourwrap');
  if (tourSt && tw && tw.classList){
    const susp = !document.getElementById('backdrop').hidden;
    const was = tw.classList.contains('suspended');
    tw.classList.toggle('suspended', susp);
    if (!susp && was) tourPlace(true);   // resume, not every poll
  }
  // the assistant's "next question" view chip follows modal navigation;
  // applySel fires on every open/close/re-render so the chip never polls
  if (globalThis.aiSync){ try{ globalThis.aiSync(); }catch(e){} }
}
// --- What's new tour ---------------------------------------------------
// A Driver.js-style spotlight walkthrough with no library: #tourring's
// oversized box-shadow IS the page dim and its transparent center pops
// the anchor through; an entry with no usable anchor falls back to a
// dimmed centered card. The deck (feed.entries, oldest -> newest) is the
// walkthrough; feed.history is the viewed section that reopens cards the
// watermark already passed. All state is `tourSt` - null means closed.
let tourSt = null;
const TOUR_TAG_STYLE = {new:'#bc8cff',improved:'#58a6ff',fixed:'#3fb950'};
// The badge is a pure function of /api/state's tour_unseen field: null
// means the server hasn't computed a count yet - an honest unknown, not
// a fake 0 - so the pill stays hidden rather than lying. Tour transition
// POSTs carry the fresh count too, so marking a card repaints it
// without waiting for the next poll.
function tourBadge(n){
  const b = document.getElementById('tour-badge');
  if (!b) return;
  const v = (typeof n==='number' && n>0) ? n : 0;
  b.hidden = !v;
  b.textContent = v>99 ? '99+' : String(v);
  const btn = document.getElementById('tour-btn');
  if (btn && btn.classList) btn.classList.toggle('unread', !!v);
}
const tourReduced = () => typeof matchMedia==='function' &&
  matchMedia('(prefers-reduced-motion: reduce)').matches;
const tourDeck = () => (tourSt && tourSt.feed && tourSt.feed.entries) || [];
const tourHist = () => (tourSt && tourSt.feed && tourSt.feed.history) || [];
const tourCur = () => tourSt && (tourSt.mode==='hist'
  ? tourHist()[tourSt.hi] : tourDeck()[tourSt.idx]);
// every repository/record string below flows through esc() - curated
// blurbs, commit subjects and request labels are never trusted markup
function tourRelatedHtml(e){
  const runs = (e.runs||[]).map(k=>{
    const r = ((lastData&&lastData.runs)||[]).find(x=>runKey(x)===k);
    return `<button type="button" class="chip" ` +
      `data-run-details="${esc(k)}" title="open this run's details">` +
      `${esc(r && (r.run||k) || base(k))}</button>`;
  }).join('');
  const reqs = (e.requests||[]).map(rid=>{
    const r = ((lastData&&lastData.runs)||[]).find(x=>x.request_id===rid);
    return `<button type="button" class="chip" ` +
      `data-open-request="${esc(rid)}" title="open this request">` +
      `${esc(r && (r.run||rid) || rid)}</button>`;
  }).join('');
  return (runs || reqs)
    ? `<div class="tsec">related</div><div class="trel">${runs}${reqs}</div>`
    : '';
}
function tourCardHtml(e, posText){
  const tag = TOUR_TAG_STYLE[e.tag] ? e.tag : 'new';
  const commits = (e.commits||[]).map(c=>
    `<li><code>${esc(String(c.sha||'').slice(0,10))}</code> ` +
    `${esc(c.subject)}</li>`).join('');
  return `<div class="tpos">${esc(posText)}` +
    (e.stop_title ? ` · ${esc(e.stop_title)}` : '') + `</div>` +
    `<h3 id="tourcard-title">${esc(e.title||'(untitled)')}</h3>` +
    `<span class="ttag" style="color:${TOUR_TAG_STYLE[tag]};` +
      `border-color:${TOUR_TAG_STYLE[tag]}">${tag}</span>` +
    (e.state==='snoozed'
      ? ` <span class="sub">deferred earlier</span>` : '') +
    (e.state==='viewed'
      ? ` <span class="sub">viewed</span>` : '') +
    (e.unavailable
      ? ` <span class="sub">source window lost</span>` : '') +
    `<div class="tblurb">${esc(e.blurb||'')}</div>` +
    (commits
      ? `<div class="tsec">commits</div><ul class="tlist">${commits}</ul>`
      : '') +
    tourRelatedHtml(e);
}
function tourActionsHtml(){
  if (tourSt.mode==='hist')
    return `<button type="button" class="btn secondary" ` +
      `data-tour="back">&larr; Back</button>` +
      `<button type="button" class="btn secondary" data-tour="unview"` +
      `${tourSt.pending?' disabled':''}>mark unread</button>` +
      `<span class="tgap"></span>` +
      `<button type="button" class="btn secondary" data-tour="skip">` +
      `Close</button>`;
  const e = tourCur(), deck = tourDeck();
  return `<button type="button" class="btn secondary" data-tour="prev"` +
    `${tourSt.idx<=0?' disabled':''}>&larr;</button>` +
    `<button type="button" class="btn secondary" data-tour="next"` +
    `${tourSt.idx>=deck.length-1?' disabled':''}>&rarr;</button>` +
    `<span class="tgap"></span>` +
    `<button type="button" class="btn secondary" data-tour="skip">` +
    `Skip</button>` +
    (e && e.state==='viewed'
      ? `<span class="sub">marked viewed</span>`
      : `<button type="button" class="btn" data-tour="mark"` +
        `${tourSt.pending?' disabled':''}>` +
        `${tourSt.pending?'marking&hellip;':'Mark viewed'}</button>`);
}
function tourHistHtml(){
  const hs = tourHist();
  if (!hs.length) return '';
  return `<details class="tourhist"><summary>viewed earlier ` +
    `(${hs.length})</summary>` + hs.map((h,i)=>
      `<button type="button" class="thist" data-tour="hist" ` +
      `data-hi="${i}" title="reopen this card">` +
      `${esc(h.title||'(untitled)')}` +
      `<span class="sub">${esc(h.viewed_at ? loc(h.viewed_at)
        : (h.state||'viewed'))}</span></button>`).join('') +
    `</details>`;
}
function tourShow(){
  const wrap = document.getElementById('tourwrap');
  const card = document.getElementById('tourcard');
  if (!wrap || !card) return;
  if (!tourSt){ wrap.hidden = true; return; }
  wrap.hidden = false;
  // sync the suspend marker too - applySel re-derives it on every modal
  // open/close, but a tour opened while a dialog is already up must
  // start covered (the z-60 layer sits over the modal otherwise)
  if (wrap.classList)
    wrap.classList.toggle('suspended',
      !document.getElementById('backdrop').hidden);
  let inner;
  if (tourSt.err){
    inner = `<h3 id="tourcard-title">What's new</h3>` +
      `<div class="notice error">${esc(tourSt.err)}</div>` +
      `<div class="tactions"><span class="tgap"></span>` +
      `<button type="button" class="btn secondary" data-tour="skip">` +
      `Close</button>` +
      `<button type="button" class="btn" data-tour="retry">Retry</button>` +
      `</div>`;
  } else if (!tourSt.feed){
    inner = `<h3 id="tourcard-title">What's new</h3>` +
      `<div class="notice">loading the change feed&hellip;</div>`;
  } else if (tourSt.mode==='hist'){
    const h = tourHist()[tourSt.hi];
    inner = h
      ? tourCardHtml(h, `viewed ${tourSt.hi+1} of ${tourHist().length}`) +
        `<div class="tactions">${tourActionsHtml()}</div>`
      : `<h3 id="tourcard-title">What's new</h3>` +
        `<div class="notice">That card is gone.</div>` +
        `<div class="tactions">${tourActionsHtml()}</div>`;
  } else {
    const deck = tourDeck(), e = deck[tourSt.idx];
    inner = e
      ? tourCardHtml(e, `Update ${tourSt.idx+1} of ${deck.length}`) +
        (tourSt.notice
          ? `<div class="notice error">${esc(tourSt.notice)}</div>` : '') +
        `<div class="tactions">${tourActionsHtml()}</div>` +
        tourHistHtml()
      : `<h3 id="tourcard-title">All caught up</h3>` +
        `<div class="notice ok">nothing new since the last ` +
        `walkthrough</div>` +
        `<div class="tactions"><span class="tgap"></span>` +
        `<button type="button" class="btn secondary" data-tour="skip">` +
        `Close</button></div>` + tourHistHtml();
  }
  card.innerHTML = inner;
  card.classList.toggle('busy', !!tourSt.pending);
  tourPlace(true);
}
function tourAnchorEl(e){
  const sel = e && e.anchor;
  if (!sel || !document.querySelector) return null;
  let el = null;
  try { el = document.querySelector(sel); } catch(_){ el = null; }
  if (!el || !el.getBoundingClientRect) return null;
  const r = el.getBoundingClientRect();
  // a usable anchor has real layout - a hidden/zero-sized element gets
  // the centered fallback like a missing selector
  if (!r || r.width<=0 || r.height<=0) return null;
  return {el, rect:r};
}
function tourPlace(reveal){
  const wrap = document.getElementById('tourwrap');
  const ring = document.getElementById('tourring');
  const card = document.getElementById('tourcard');
  if (!tourSt || !wrap || !ring || !card || wrap.hidden) return;
  // while suspended under a modal the layer is display:none - don't
  // re-place (and especially don't scrollIntoView) behind the dialog
  if (wrap.classList && wrap.classList.contains('suspended')) return;
  const a = tourAnchorEl(tourCur());
  const vw = (typeof window!=='undefined' && window.innerWidth) || 1024;
  const vh = (typeof window!=='undefined' && window.innerHeight) || 768;
  if (!a){
    wrap.classList.add('dim'); ring.hidden = true;
    card.classList.remove('anchored');
    card.style.top = card.style.left = '';
    return;
  }
  let r = a.rect;
  // reveal - only when a card is (re)shown, never on a scroll/resize
  // re-place: scrollIntoView there would fight the user's own scrolling
  const off = r.top<0 || (r.bottom!=null?r.bottom:r.top+r.height)>vh ||
    r.left<0 || (r.right!=null?r.right:r.left+r.width)>vw;
  if (reveal && off && a.el.scrollIntoView){
    a.el.scrollIntoView({block:'nearest',
      behavior:tourReduced()?'auto':'smooth'});
    r = a.el.getBoundingClientRect();
  }
  wrap.classList.remove('dim'); ring.hidden = false;
  const pad = 8, gap = 12, margin = 12;
  const rb = r.bottom!=null ? r.bottom : r.top+r.height;
  ring.style.left = (r.left-pad)+'px'; ring.style.top = (r.top-pad)+'px';
  ring.style.width = (r.width+pad*2)+'px';
  ring.style.height = (r.height+pad*2)+'px';
  const cw = Math.min(440, vw-margin*2);
  card.style.width = cw+'px';
  const ch = card.offsetHeight || 300;
  // prefer below the ring, flip above when it wouldn't fit, then clamp -
  // the card must stay reachable whatever the anchor's position
  let top = rb + pad + gap;
  if (top + ch > vh - margin) top = r.top - pad - gap - ch;
  top = Math.max(margin, Math.min(top, Math.max(margin, vh-ch-margin)));
  card.style.top = top+'px';
  card.style.left = Math.max(margin,
    Math.min(r.left, vw-cw-margin))+'px';
  card.classList.add('anchored');
}
// scroll/resize listeners live only while the tour is open - closing
// removes them so nothing keeps firing on a dead overlay
function tourBind(on){
  if (on){
    const sc = () => { if (tourSt) tourPlace(false); };
    tourSt._sc = sc;
    document.addEventListener('scroll', sc, true);
    if (typeof window!=='undefined' && window.addEventListener)
      window.addEventListener('resize', sc);
  } else if (tourSt && tourSt._sc){
    document.removeEventListener('scroll', tourSt._sc, true);
    if (typeof window!=='undefined' && window.removeEventListener)
      window.removeEventListener('resize', tourSt._sc);
    tourSt._sc = null;
  }
}
async function tourLoad(){
  if (!tourSt) return;
  tourSt.feed = null; tourSt.err = null; tourShow();
  try { const f = await api('GET', '/api/tour');
    if (tourSt) tourSt.feed = f;
  } catch(e){ if (tourSt) tourSt.err = e.message || String(e); }
  if (tourSt) tourShow();
}
function openTour(){
  if (tourSt) return;
  tourSt = {feed:null, err:null, notice:null, idx:0, hi:0, mode:'deck',
    pending:false, returnEl:document.activeElement||null, _sc:null};
  tourBind(true);
  tourShow();
  const card = document.getElementById('tourcard');
  if (card && card.focus) card.focus();
  tourLoad();
}
function tourClose(){
  // the ONLY close path: Skip, Escape, the dim and Close all land here -
  // none of them POSTs viewed or snooze, so dismissing never
  // acknowledges or implicitly defers an entry
  if (!tourSt) return;
  tourBind(false);
  const back = tourSt.returnEl;
  tourSt = null;
  tourShow();
  const el = (back && back.focus && back.isConnected!==false)
    ? back : document.getElementById('tour-btn');
  if (el && el.focus) el.focus();
}
async function tourMark(){
  const e = tourCur();
  if (!tourSt || !e || tourSt.pending || e.state==='viewed') return;
  tourSt.pending = true; tourSt.notice = null; tourShow();
  let r;
  try {
    r = await api('POST',
      '/api/tour/'+encodeURIComponent(e.id)+'/viewed', {});
  } catch(err){
    // a failed mark must NOT advance: the card stays put with the error
    // so the click is retryable instead of silently skipping the entry
    if (!tourSt) return;
    tourSt.pending = false;
    tourSt.notice = 'Mark viewed failed: ' + (err.message || err);
    tourShow(); return;
  }
  if (!tourSt) return;                          // closed mid-flight
  tourSt.pending = false;
  e.state = 'viewed'; e.viewed_at = new Date().toISOString();
  const hs = tourSt.feed.history || (tourSt.feed.history = []);
  if (!hs.some(h=>h.id===e.id)) hs.unshift(e);
  tourBadge(r.unseen_count);                    // fresh count, no poll wait
  const deck = tourDeck();
  let ni = deck.findIndex((x,j)=>j>tourSt.idx && x.state!=='viewed');
  if (ni<0) ni = deck.findIndex(x=>x.state!=='viewed');
  tourSt.idx = ni<0 ? deck.length : ni;         // past end -> caught up
  tourSt.mode = 'deck';
  tourShow();
}
async function tourUnview(){
  const h = tourSt && tourSt.mode==='hist' && tourHist()[tourSt.hi];
  if (!h || tourSt.pending) return;
  tourSt.pending = true; tourShow();
  try {
    const r = await api('POST',
      '/api/tour/'+encodeURIComponent(h.id)+'/unview', {});
    if (!tourSt) return;
    tourSt.pending = false;
    tourBadge(r.unseen_count);
    tourSt.mode = 'deck';
    // the card returns to the live deck - the server already rebuilt
    // the feed, so re-pull it instead of guessing positions locally
    await tourLoad();
    if (!tourSt) return;
    const j = tourDeck().findIndex(x=>x.id===h.id);
    if (j>=0) tourSt.idx = j;
    tourShow();
  } catch(err){
    if (!tourSt) return;
    tourSt.pending = false;
    tourSt.err = 'Unview failed: ' + (err.message || err);
    tourShow();
  }
}
function tourAct(act, el){
  if (!tourSt) return;
  if (act==='skip' || act==='close'){ tourClose(); return; }
  if (act==='retry'){ tourLoad(); return; }
  // a persist call is in flight - close stays live (it abandons the
  // reply) but deck movement and other transitions wait for it
  if (tourSt.pending) return;
  if (act==='mark'){ tourMark(); return; }
  if (act==='unview'){ tourUnview(); return; }
  if (act==='prev' && tourSt.idx>0){ tourSt.idx--; tourShow(); return; }
  if (act==='next' && tourSt.idx<tourDeck().length-1){
    tourSt.idx++; tourShow(); return; }
  if (act==='hist'){
    const i = +(el && el.dataset && el.dataset.hi || 0);
    if (tourHist()[i]){ tourSt.mode='hist'; tourSt.hi=i; tourShow(); }
    return; }
  if (act==='back'){ tourSt.mode='deck'; tourShow(); return; }
}
// --- assistant surface bridge ----------------------------------------
// The chat client (assistant/static/chat.js) reads which view is on
// screen through aiView() - the let-globals above are not reachable from
// outside this script, so this is the one sanctioned channel (globalThis
// instead of window keeps the vm test contexts working too). A citation
// click calls back through aiNav, which reuses the same openers the
// dashboard's own buttons use; the chat itself lives outside #detail.
globalThis.aiView = () => ({
  detailKey,
  summaryView: summaryView && {key: summaryView.key},
  runModalKey,
  modalRequest: modalRequest && {id: modalRequest.id,
    status: modalRequest.status},
  modalQA: modalQA && {id: modalQA.id},
  power: !document.getElementById('powerwrap').hidden,
  only: [...only], facets: [...facets], projs: [...projs],
  selectedRun,
  agents: AGENTS
});
globalThis.aiNav = {
  agent(k){ const j = AGENTS.findIndex(x=>agentKey(x)===k);
    if (j>=0){ showDetail(j); return true; } return false; },
  summary(k){ openSummary(k); return true; },
  runSummary(k){ openRunSummary(k); return true; },
  run(k){ showRunDetails(k); return true; },
  request(id){ openRequest(id); return true; }
};
document.addEventListener('mouseover', e=>{
  // hovering a model option tracks it as the active descendant - the
  // mouse and Arrow keys then agree on what Enter would pick
  const mo = e.target.closest && e.target.closest('.mopt');
  if (mo && mo.dataset && mo.dataset.mi!==undefined){
    const inp=document.getElementById(mo.dataset.mfor);
    if (inp) mpopActivate(inp,+mo.dataset.mi,false);
  }
  const tr = e.target.closest && e.target.closest('tr[data-run-key]');
  hoverRun = tr && tr.dataset.runKey || null;
  applyHl();
});
async function tick(){
  try { await refresh(); }
  catch(e){} finally { setTimeout(tick, 2000); } }
setDensity(density);
if (typeof window!=='undefined' && window.addEventListener)
  window.addEventListener('resize', ()=>{ syncTopbar();
    if (ctxState) hideCtxMenu(); });
tick();
</script></body></html>"""


def _serve_power(server, act) -> dict:
    """Stop/Restart the dashboard itself - shared by the /api/serve/power
    endpoint and the assistant's serve_power action. The response is
    flushed before the shutdown timer fires; restart spawns the detached
    replacement first so the gap is just the bind handoff (--wait-port)."""
    act = str(act or "")
    if act not in ("stop", "restart"):
        raise RequestError("action must be 'stop' or 'restart'")
    if act == "restart":
        sargs = getattr(server, "serve_args", None)
        if sargs is None:
            raise RequestError("this server was not started by "
                               "conductor.py serve - cannot respawn")
        _respawn_serve(sargs)
    threading.Timer(0.4, server.shutdown).start()
    return {"ok": True, "action": act}


def make_dashboard_handler(runs_dir: Path, devin_argv: list[str],
                           requests_dir: Path | None = None,
                           manifests_dir: Path = MANIFESTS_DIR,
                           attachments_dir: Path | None = None,
                           trash_dir: Path | None = None,
                           tour_repo: Path | None = None):
    requests_dir = requests_dir or runs_dir.parent / "requests"
    attachments_dir = attachments_dir or runs_dir.parent / "attachments"
    trash_dir = trash_dir or runs_dir.parent / "trash"
    # The 'What's new' tour endpoints share one ctx: its feed/count caches
    # are per-serve so a fixture or second dashboard never sees another's.
    tour = tour_ctx(runs_dir, requests_dir, manifests_dir, tour_repo,
                    devin_argv=devin_argv)

    # The assistant surface: one lazily-opened host runtime per serve
    # process. Errors are cached - a broken provider init degrades to a
    # 503 payload on /api/assistant/*, never a dead dashboard.
    _assist = {"core": None, "runtime": None, "err": None, "err_at": 0.0}
    _ASSIST_RETRY_AFTER = 60.0    # a transient init failure (sqlite hiccup,
                                # brief disk issue) retires after a minute -
                                # never a permanent 503 until restart

    def assistant_open(server=None):
        with STATE_LOCK:
            if _assist["err"] is not None and \
                    _now() - _assist["err_at"] >= _ASSIST_RETRY_AFTER:
                _assist["err"] = None          # stale failure - retry
            if _assist["core"] is None and _assist["err"] is None:
                try:
                    from assistant_host.conductor_adapters import (
                        open_runtime)
                    core, rt = open_runtime(
                        data_dir=runs_dir.parent / "assistant",
                        runs_dir=runs_dir, requests_dir=requests_dir,
                        manifests_dir=manifests_dir, trash_dir=trash_dir,
                        devin_argv=devin_argv,
                        power=((lambda act, s=server: _serve_power(s, act))
                               if server is not None else
                               None))   # no server -> kind unlisted, not
                                        # a silent no-op lambda
                    _assist.update(core=core, runtime=rt)
                except Exception as exc:                # noqa: BLE001
                    LOG.exception("assistant runtime init failed")
                    _assist["err"] = f"{type(exc).__name__}: {exc}"
                    _assist["err_at"] = _now()
        return _assist["core"], _assist["err"]

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

        # The chat client's own assets ship with the assistant package
        # and are served verbatim - whitelisted names only, and NO core
        # dependency: a broken provider init must not take the UI shell
        # down with it (the panel then renders 'assistant unavailable').
        _AI_STATIC = {"chat.js": "text/javascript; charset=utf-8",
                      "md.js": "text/javascript; charset=utf-8",
                      "chat.css": "text/css; charset=utf-8"}
        _AI_STATIC_DIR = Path(__file__).resolve().parent / \
            "assistant" / "static"

        def _assistant_static(self, path: str) -> None:
            name = path.rsplit("/", 1)[-1]
            ctype = self._AI_STATIC.get(name)
            if ctype is None:
                raise RequestError("Not found", 404)
            try:
                data = (self._AI_STATIC_DIR / name).read_bytes()
            except OSError:
                raise RequestError("Not found", 404)
            self._send(data, ctype)

        def _assistant(self, method: str, path: str) -> None:
            """Route /api/assistant/* into the chat core's route table.
            POSTs pass the RAW body through - the attachments route takes
            an image body, everything else is JSON parsed by dispatch.
            GET attachments/<id> is the ONE binary route: it mirrors
            /api/attachments/<name> (bytes + sniffed ctype) but the
            authorization boundary is the assistant's attachment row -
            core.read_upload, never a client-supplied path."""
            from assistant import http_api
            from assistant.core import AssistantError
            core, err = assistant_open(server=self.server)
            if core is None:
                self._json({"error": f"assistant unavailable: {err}"},
                           503)
                return
            if method == "GET":
                m = re.fullmatch(
                    r"/api/assistant/attachments/([^/?]+)", path)
                if m:
                    try:
                        data, ctype = core.read_upload(
                            urllib.parse.unquote(m[1]))
                    except AssistantError as exc:
                        self._json({"error": str(exc)}, exc.status)
                        return
                    self._send(data, ctype)
                    return
            raw = b""
            if method != "GET":
                try:
                    size = int(self.headers.get("Content-Length") or 0)
                except ValueError:
                    raise RequestError("Missing request body")
                # the upload route alone gets the image-size allowance -
                # JSON POSTs stay at the dashboard's usual cap
                cap = (ATTACH_MAX_BYTES if path.endswith("/attachments")
                       else 524288)
                if not 0 <= size <= cap:
                    raise RequestError("Request body is too large", 413)
                raw = self.rfile.read(size)
            status, body = http_api.dispatch(
                core, method, path, body=raw,
                query=urllib.parse.parse_qs(
                    urllib.parse.urlsplit(self.path).query))
            self._json(body, status)

        def do_PUT(self) -> None:                    # noqa: N802 - stdlib name
            path = urllib.parse.urlsplit(self.path).path
            try:
                self._check_origin()
                if path.startswith("/api/assistant/"):
                    self._assistant("PUT", path)
                    return
                self.send_error(404)
            except RequestError as exc:
                self._json({"error": str(exc)}, exc.status)
            except Exception:
                LOG.exception("dashboard PUT failed")
                self._json({"error": "Dashboard request failed"}, 500)

        def do_GET(self) -> None:                    # noqa: N802 - stdlib name
            path = urllib.parse.urlsplit(self.path).path
            try:
                self._check_origin()
                if path == "/api/state":
                    q = urllib.parse.parse_qs(
                        urllib.parse.urlsplit(self.path).query)
                    payload = collect_dashboard(
                        runs_dir, devin_argv, requests_dir,
                        include_archived=q.get("archived", [""])[0]
                        in ("1", "true", "yes"),
                        include_trash=q.get("trash", [""])[0]
                        in ("1", "true", "yes"))
                    # the tour badge rides the state poll as one tiny
                    # field, filled by a background rebuild - never an
                    # inline feed collection on the paint path
                    payload["tour_unseen"] = tour_unseen(tour)
                    self._json(payload)
                elif path == "/api/tour":
                    self._json(tour_feed(tour))
                elif (m := re.fullmatch(
                        r"/api/projects/([^/]+)/branches", path)):
                    # Merge-target selector data: eligible local branches of
                    # the SELECTED project's real repo plus the branch its
                    # checkout is on now (the effective default target).
                    slug = urllib.parse.unquote(m[1])
                    entry = load_projects().get(slug)
                    if entry is None:
                        raise RequestError(f"Unknown project: {slug}", 404)
                    pdir = entry["path"]
                    if not is_git_repo(pdir):
                        raise RequestError(
                            f"{slug}: not a git repository root")
                    self._json({"slug": slug, "current": current_branch(pdir),
                                "branches": list_local_branches(pdir)})
                elif (m := re.fullmatch(
                        r"/api/runs/([^/]+)/events", path)):
                    # the modal history feed: driver events.jsonl + the
                    # dashboard's own action log (flag dir) merged
                    name = m.group(1)
                    if not RUN_DIR_RE.fullmatch(name):
                        raise RequestError("Invalid run dir", 404)
                    d = (runs_dir / name).resolve()
                    if d.parent != runs_dir.resolve()                             or not (d / "state.json").is_file():
                        raise RequestError("Run not found", 404)
                    self._json({"run": name, "events": run_events(d)})
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
                    elif q.get("frag", [""])[0] in ("1", "true"):
                        # the in-app summary modal embeds the report body
                        # markup - same escaping as the standalone page
                        self._send(_summary_body(md).encode("utf-8"),
                                   "text/html; charset=utf-8")
                    else:
                        self._send(summary_html(
                            md, f"{m.group(1)} / {m.group(2)}")
                            .encode("utf-8"),
                            "text/html; charset=utf-8")
                elif (m := re.fullmatch(r"/api/runs/([^/]+)/summary",
                                        path)):
                    # run-level report: the LLM-written run.summary.md
                    # when a summarizer session produced one, else the
                    # deterministic roll-up of the steps' own reports
                    md = run_summary_doc(m.group(1), runs_dir)
                    q = urllib.parse.parse_qs(
                        urllib.parse.urlsplit(self.path).query)
                    if q.get("raw", [""])[0] in ("1", "true"):
                        self._send(md.encode("utf-8"),
                                   "text/markdown; charset=utf-8")
                    elif q.get("frag", [""])[0] in ("1", "true"):
                        # an LLM-written run.summary.md gets the accent
                        # headings - the deterministic roll-up keeps the
                        # default chrome so the difference is visible
                        _d, _st = _run_dir_checked(m.group(1), runs_dir)
                        frag = _summary_body(md)
                        if (_d / "run.summary.md").is_file():
                            frag = '<div class="llmsum">' + frag + '</div>'
                        self._send(frag.encode("utf-8"),
                                   "text/html; charset=utf-8")
                elif (m := re.fullmatch(
                        r"/api/runs/([^/]+)/steps/([^/]+)/trajectory",
                        path)):
                    # full per-entry text behind the modal's arc rows -
                    # fetched on expansion, never part of /api/state
                    self._json(step_trajectory(m.group(1), m.group(2),
                                               runs_dir))
                elif (m := re.fullmatch(
                        r"/api/requests/([^/]+)/trajectory", path)):
                    self._json(request_trajectory(m.group(1),
                                                  requests_dir))
                elif (m := re.fullmatch(r"/api/attachments/([^/]+)", path)):
                    data, ctype = read_attachment(
                        urllib.parse.unquote(m[1]), attachments_dir)
                    self._send(data, ctype)
                elif path == "/api/serve/power":
                    # the Stop/Restart dialog's safety report: live
                    # merges (restart orphans them), still-live steps,
                    # and this process's in-flight generations/launches
                    # (a restart kills those outright)
                    self._json(serve_safety(runs_dir, requests_dir))
                elif path in ("/", "/index.html"):
                    self._send(_DASH_HTML.encode("utf-8"),
                               "text/html; charset=utf-8")
                elif path.startswith("/api/assistant/static/"):
                    self._assistant_static(path)
                elif path.startswith("/api/assistant/"):
                    self._assistant("GET", path)
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
                if path.startswith("/api/assistant/"):
                    self._assistant("POST", path)
                    return
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
                if path == "/api/serve/power":
                    self._json(_serve_power(self.server,
                                            body.get("action")))
                    return
                if path == "/api/pool":
                    self._json(set_agent_cap(body.get("cap")))
                    return
                if path == "/api/requests":
                    self._json(create_request(body, requests_dir, runs_dir), 201)
                elif path == "/api/requests/import":
                    # the explicit recovery path: register an
                    # already-written follow-up/proposal file as a request
                    # row - never launches it
                    self._json(import_followup_file(
                        body.get("path"), requests_dir, manifests_dir,
                        runs_dir, run_dir=body.get("run")), 201)
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
                        self._json(discard_request(
                            request_id, requests_dir, manifests_dir,
                            runs_dir, trash_dir))
                elif (m := re.fullmatch(
                        r"/api/tour/([^/]+)/(viewed|snooze|unview)", path)):
                    self._json(tour_transition(
                        tour, urllib.parse.unquote(m.group(1)),
                        m.group(2)))
                elif (m := re.fullmatch(
                        r"/api/trash/([^/]+)/restore", path)):
                    self._json(restore_trash(m.group(1), trash_dir,
                                             runs_dir, requests_dir,
                                             manifests_dir))
                elif path == "/api/trash/empty":
                    self._json(empty_trash(body, trash_dir, requests_dir,
                                           runs_dir))
                elif (m := re.fullmatch(r"/api/runs/([^/]+)/resume", path)):
                    self._json(resume_run(m.group(1), runs_dir,
                                          devin_argv), 202)
                elif (m := re.fullmatch(r"/api/runs/([^/]+)/summarize",
                                        path)):
                    self._json(summarize_run_start(
                        m.group(1), runs_dir, devin_argv,
                        model=body.get("model")), 202)
                elif (m := re.fullmatch(r"/api/runs/([^/]+)/merge", path)):
                    only = body.get("steps") if isinstance(
                        body.get("steps"), list) else None
                    self._json(merge_run_start(
                        m.group(1), runs_dir, devin_argv, only=only), 202)
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
                    self._json(delete_run(m.group(1), body, runs_dir,
                                          trash_dir, requests_dir))
                elif (m := re.fullmatch(r"/api/runs/([^/]+)/review", path)):
                    self._json(set_run_review(m.group(1), body, runs_dir))
                elif (m := re.fullmatch(
                        r"/api/runs/([^/]+)/steps/([^/]+)/review", path)):
                    self._json(set_step_review(m.group(1), m.group(2), body,
                                               runs_dir))
                elif (m := re.fullmatch(
                        r"/api/runs/([^/]+)/steps/([^/]+)/interrupt", path)):
                    self._json(interrupt_step(
                        m.group(1), m.group(2), body, runs_dir), 202)
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
                    _DETACHED_PROCS.append(subprocess.Popen(
                        'start "" cmd /k "devin auth logout & '
                        'devin auth login --force-manual-token-flow"',
                        shell=True, env=clean_devin_env()))
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

    def assistant_close() -> None:
        """Release the lazily-opened assistant runtime (sqlite handles
        matter on Windows; also the seam serve shutdown/tests use)."""
        core = _assist.get("core")
        if core is not None:
            try:
                core.store.close()
            except Exception:                  # noqa: BLE001
                pass
            _assist["core"] = _assist["runtime"] = None

    Handler.assistant_close = staticmethod(assistant_close)
    Handler.tour_ctx = tour          # tests drive the cache via this
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
    for line in sweep_service_ports():
        print(f"sweep: {line}")
    for line in sweep_agent_slots():
        print(f"sweep: {line}")
    # A restarted serve adopts generations whose owner died: the spool
    # replays deferred publishes and lands parked questions/errors.
    for rid in drain_planner_spool():
        print(f"sweep: adopted planner spool for request {rid}")
    # ...and registers follow-up manifests produced while it was down -
    # the same pass then reruns throttled inside the dashboard loop.
    for line in discover_followups(runs_dir):
        print(f"sweep: {line}")
    # completions that predate completion_seq get explicit needs-review
    # tombstones so every finished task reads 'unreviewed' until a human
    # looks - idempotent, never overwrites an existing mark
    for line in seed_review_flags(runs_dir):
        print(f"sweep: {line}")
    handler = make_dashboard_handler(runs_dir, devin_argv)
    # --port wins; a worker-spawned serve falls back to the port its step
    # leased (CONDUCTOR_SERVICE_PORT), never the main workspace's 8773.
    port = args.port or env_service_port() or SERVE_PORT
    # --wait-port: a Stop/Restart respawn retries the bind while the
    # outgoing process finishes shutting down and releases the socket
    deadline = time.time() + (15 if getattr(args, "wait_port", False)
                              else 0)
    while True:
        try:
            httpd = http.server.ThreadingHTTPServer(("127.0.0.1", port),
                                                  handler)
            break
        except OSError:
            if time.time() >= deadline:
                raise
            time.sleep(0.5)
    args.port = port                 # the respawn inherits the effective port
    httpd.serve_args = args          # the power endpoint respawns these
    print(f"conductor dashboard: http://127.0.0.1:{port}  "
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
            ("merge", cmd_merge,
             "land a run's pending (deferred/parked) branches"),
            ("summarize", cmd_summarize,
             "write a run's LLM executive summary (run.summary.md)"),
            ("import", cmd_import,
             "register an already-written manifest as a proposal "
             "(follow-up recovery - never launches)"),
            ("sessions", cmd_sessions,
             "list / clean up conductor-spawned devin sessions"),
            ("demo", cmd_demo, "self-test against a mock devin")):
        p = sub.add_parser(name, help=helptext)
        if name in ("plan", "run", "import"):
            p.add_argument("manifest")
        if name == "import":
            p.add_argument("--run", default=None,
                           help="originating run dir name to link the "
                                "imported proposal to (derived from the "
                                "<run>-followup.toml name when omitted)")
        if name == "run":
            p.add_argument("--request-id", default=None, help=argparse.SUPPRESS)
            p.add_argument("--resume", nargs="?", const="latest", default=None,
                           help="resume a run dir (default: latest for this manifest)")
            p.add_argument("--max-parallel", type=int, default=None,
                           help="cap concurrent agents (overrides the "
                                "manifest's run.max_parallel)")
        if name in ("plan", "run", "demo", "sessions", "sweep", "merge",
                    "summarize"):
            p.add_argument("--devin", default=None,
                           help="devin command override (e.g. mock shim). "
                                "Split with shlex - quote or use forward "
                                "slashes in Windows paths")
        if name in ("run", "demo", "serve", "sweep", "sessions", "merge",
                    "summarize", "import"):
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
            p.add_argument("--port", type=int, default=None,
                           help="dashboard port (default: the step-leased "
                                "CONDUCTOR_SERVICE_PORT when set, else "
                                f"{SERVE_PORT})")
            p.add_argument("--wait-port", action="store_true",
                           help="retry the bind up to 15s - used by the "
                                "dashboard's restart respawn while the "
                                "outgoing process releases the socket")
        if name == "status":
            p.add_argument("run_dir", nargs="?", default=None)
        if name == "merge":
            p.add_argument("run_dir")
            p.add_argument("--steps", nargs="+", default=None,
                           help="merge only these steps (pick one of "
                                "several parked alternatives)")
        if name == "summarize":
            p.add_argument("run_dir")
            p.add_argument("--model", default=None,
                           help="model for the summary session "
                                "(default: the run's recorded "
                                "summarize_model, else the CLI default)")
    args = ap.parse_args(argv)
    return {"plan": cmd_plan, "run": cmd_run, "status": cmd_status,
            "serve": cmd_serve, "sweep": cmd_sweep, "merge": cmd_merge,
            "summarize": cmd_summarize, "import": cmd_import,
            "sessions": cmd_sessions, "demo": cmd_demo}[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
