"""tour_feeds.py - deterministic, no-LLM change feeds for the dashboard
'What's new' tour.

Everything here is free: git log, record scans, one curated JSONL file.
No LLM call, no HTTP, no UI - the tour-api step wraps this module.

Feed window
-----------
A feed is built against a ``boundary`` ({commit, at}) captured with
``tour_state.capture_boundary`` and covers the range
``{base: watermark, tip: boundary}`` from ``tour_state.feed_range``:

  * commits   - ``git log <base.commit>..<tip.commit>``. A null
    ``base.commit`` is unbounded (first-init semantics: nothing has been
    acknowledged). A recorded base that no longer resolves is treated
    as unbounded (loud failure - a lost revision must never hide
    changes) and surfaces a warning; it is never "repaired" to HEAD.
  * records   - requests, runs and follow-up registrations bounded by
    THEIR OWN event stamps inside ``(base.at, tip.at]``:
    requests/follow-ups on the request's ``created_at`` (registration is
    the event - a follow-up proposal is new when REGISTERED, not when
    its source run was created), runs on ``ended_at`` (the run_end
    boundary - never ``started_at``/run creation).
  * curated   - worker-appended lines in ``manifests/whatsnew.jsonl``.

Cards
-----
Commits group into one card per feature area (TOUR_STOPS id or an
unanchored pseudo-area) per window. A card's id embeds its newest
commit (``area:<stop>:<tip-sha>``), so re-feeds over the same window
reproduce the same id (viewed state binds) while a new commit in an
already-viewed area yields a NEW id (it can never silently inherit
'viewed'). Record cards key on the record's own identity
(``request:<rid>``, ``run:<dir>:<ended>``, ``followup:<rid>``) - a
re-finished run is a new event, not the old card acknowledged again.

Sections + record events
------------------------
Every card carries ``section`` ('tool' | 'work') + ``section_title``:
change/curated cards are "Tool updates" (spotlight anchors preserved);
run/request/follow-up/pipeline cards are the "Work digest" (anchored on
the runs/requests surfaces). Record cards' ``tag`` is EVENT-derived -
'ready' for proposals (plain or follow-up), the run's terminal
``outcome`` for run/pipeline cards - never the change-style
new/improved/fixed labels, which stay reserved for tool updates.

Work-card folding
-----------------
A request and the run it launched fold into ONE pipeline card ONLY when
both events sit inside the same window (request ``created_at`` and run
``ended_at`` in ``(base.at, tip.at]``): the run card absorbs the
proposal - id stays ``run:<dir>:<ended>`` (the completion is the newest
event), ``kind`` becomes ``pipeline``, and ``covers`` lists the
absorbed standalone ids so a viewed ``request:<rid>`` descriptor still
re-materializes to it. The card carries ``proposed_at`` +
``completed_at``, the actual terminal ``outcome`` (a recorded
``aborted`` reason wins, else the step verdicts), ``steps_done`` /
``steps_total`` and the run's ``unreviewed`` task count. A follow-up
proposal folds into its ASSOCIATED request's work card (``followups``
list + ``covers``) when that request is in the window - association is
the recorded ``source_request`` / ``source_run`` -> ``request_id``
link, never a display-name match. Requests whose run finished in a
different window, runs whose request was acknowledged earlier, and
follow-ups whose associated request is outside the window all keep
their standalone cards.

Curated records ({stop, blurb, title?, tag?, commits?/requests?/runs?})
associate to the window through the identities they name; records whose
identities resolve to nothing in the window belong to a different range
and are skipped, as are malformed lines - the feed never breaks on
them. A record whose claimed commits all sit inside the area the record
names FOLDS into that area's card (its stable id is preserved - the
human blurb upgrades the card in place); otherwise claimed commits are
extracted into a dedicated card so the same change never shows twice.

Merge commits naming ``conductor/<run-dir>/<step>`` attribute themselves
and their merged chain to that run's card when the run finished inside
the window - the run's delivery is one card, not N ghost commit cards.
Unattributed merges group normally.

``history()`` re-materializes recorded (viewed/snoozed/unviewed) entry
descriptors over their own stored ``{base,tip}`` ranges, which is how a
viewed card stays retrievable after the live watermark advances past it.
Within one ``build_feed`` every distinct ``{base,tip}`` range collects
ONCE - the live window and descriptor windows share a per-build memo -
and cards that are already 'viewed' never sit in the live ``entries``
deck (they remain reachable through ``history``).
"""
import datetime as dt
import hashlib
import json
import os
import re
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import tour_state

ROOT = Path(__file__).resolve().parent
DEFAULT_REPO = ROOT
DEFAULT_RUNS_DIR = ROOT / "data" / "runs"
DEFAULT_REQUESTS_DIR = ROOT / "data" / "requests"
DEFAULT_MANIFESTS_DIR = ROOT / "manifests"
CURATED_NAME = "whatsnew.jsonl"

TAGS = ("new", "improved", "fixed")
_CARD_KM = 12          # sha prefix length used inside entry ids
_BLURB_MAX = 600
_TITLE_MAX = 160

# The feed's two sections: change/curated cards are tool updates (the
# tool changed); record cards are the work digest (things the conductor
# DID). The renderer splits the walkthrough on this - change kind
# 'pipeline' is a request+run folded work card.
SECTIONS = {"tool": "Tool updates", "work": "Work digest"}
_TOOL_KINDS = {"change", "curated"}
_WORK_KINDS = {"run", "request", "followup", "pipeline"}

# Record-event tags - deliberately distinct from the change-style TAGS
# so a proposal's 'ready' or a run's 'done'/'failed' never reads as a
# feature-change label.
TAG_REQUEST = "ready"

DEFAULT_FLAGS_DIR = Path(tempfile.gettempdir()) / "agent-conductor-flags"

# Bounded collector concurrency: the four independent window scans (commit
# log, run dirs, request records, curated jsonl) plus merge-chain lookups
# share one pool per build - never more than this many git/fs calls at once.
_COLLECT_WORKERS = 4
# The window scans, in the order callers/tests expect their stages and
# warnings to appear.
_SOURCE_ORDER = ("commits", "runs", "requests", "curated")
_SOURCE_STAGE = {
    "commits": "reading commit log",
    "runs": "loading run records",
    "requests": "loading requests",
}
_SOURCE_DONE = {
    "commits": "commit log collected",
    "runs": "run records collected",
    "requests": "requests collected",
    "curated": "curated records collected",
}
_SOURCE_LABEL = {
    "commits": "commits", "runs": "runs",
    "requests": "requests", "curated": "curated",
}

# ------------------------------------------------------------------- stops
# The tour's feature-area -> DOM anchor registry. Every selector names an
# element that exists in conductor.py's _DASH_HTML today, except the
# 'tour' stop's first alternative - the What's-new button the
# tour-dashboard step adds ("beside the power button"); the CSS group
# selector degrades to #power-btn until #tour-btn exists.
TOUR_STOPS = {
    "tour":        {"selector": "#tour-btn, #power-btn",
                    "title": "What's new tour"},
    "runs":        {"selector": "#runs", "title": "Runs"},
    "requests":    {"selector": "#runs", "title": "Proposals & requests"},
    "new-request": {"selector": "#new-request", "title": "New request"},
    "agents":      {"selector": "#agents", "title": "Agents by project"},
    "filters":     {"selector": "#filters", "title": "Filters"},
    "density":     {"selector": "#density", "title": "Row density"},
    "power":       {"selector": "#power-btn", "title": "Dashboard power"},
    "assistant":   {"selector": "#dask", "title": "Ask AI assistant"},
    "modal":       {"selector": "#backdrop", "title": "Details modal"},
    "dialogs":     {"selector": "#askwrap", "title": "Themed dialogs"},
    "trash":       {"selector": "#trashwrap", "title": "Trash & recovery"},
    "ctxmenu":     {"selector": "#ctxmenu", "title": "Row actions"},
    "meta":        {"selector": "#meta", "title": "Status line"},
    "undobar":     {"selector": "#undobar", "title": "Undo bar"},
}

# Feature areas with no dashboard anchor: cards still render, unanchored.
# These names deliberately avoid colliding with TOUR_STOPS ids.
_AREA_TITLES = {
    "conductor": "Conductor core",
    "mobile": "Mobile device helper",
    "manifests": "Manifests",
    "tracking": "Docs & task tracking",
    "mixed": "Cross-cutting changes",
    "tests": "Tests",
}

# Path rules map repo-relative paths to feature areas. Ordered: the
# first rule hit wins. ``None`` means "the dashboard monolith" - such
# files defer the area decision to commit-subject keywords below.
_PATH_RULES = [
    (re.compile(r"^assistant(?:_host)?/"), "assistant"),
    (re.compile(r"^tour_(?:state|feeds)\.py$|^tests/test_tour"), "tour"),
    (re.compile(r"^mobile_device\.py$|^tests/test_mobile"), "mobile"),
    (re.compile(r"^conductor\.py$"), None),
    (re.compile(r"^tests/"), "tests"),
    (re.compile(r"^manifests/"), "manifests"),
    (re.compile(r"^\.agent_tasks/|\.md$|^examples/"), "tracking"),
]

# Subject keywords pick a stop for conductor.py-touching commits (whose
# path alone only says "the monolith changed"). Ordered, first hit wins.
_SUBJECT_RULES = [
    (re.compile(r"\b(?:power|restart|shutdown|keep.?awake)\b", re.I), "power"),
    (re.compile(r"\b(?:ask.?ai|assistant|chat|dictation|transcri\w*)\b", re.I),
     "assistant"),
    (re.compile(r"\b(?:what'?s.?new|tour|walkthrough)\b", re.I), "tour"),
    (re.compile(r"\b(?:trash|discard|restore|recover|delet\w+)\b", re.I),
     "trash"),
    (re.compile(r"\b(?:dialog|confirm|alert|themed|backdrop)\b", re.I),
     "dialogs"),
    (re.compile(r"\b(?:filter|facet|search)\b", re.I), "filters"),
    (re.compile(r"\b(?:density|compact)\b", re.I), "density"),
    (re.compile(r"\b(?:undo|undobar)\b", re.I), "undobar"),
    (re.compile(r"\b(?:modal|detail|trajectory|summary|summar\w+)\b", re.I),
     "modal"),
    (re.compile(r"\b(?:context menu|ctxmenu|right.?click)\b", re.I), "ctxmenu"),
    (re.compile(r"\b(?:merge|worktree|deferred|conflict|branch)\b", re.I),
     "runs"),
    (re.compile(r"\b(?:new request|request form|draft|manifest editor|"
                r"rq-|form)\b", re.I), "new-request"),
    (re.compile(r"\b(?:request|proposal|planner|manifest|question\w*|"
                r"follow.?up|import)\b", re.I), "requests"),
    (re.compile(r"\b(?:run|step|task|agent|worker|session)\b", re.I),
     "agents"),
    (re.compile(r"\b(?:meta|status line|slots?)\b", re.I), "meta"),
]

_FIX_RE = re.compile(
    r"\b(?:fix(?:e[ds])?|bug|regression|hotfix|patch|repair\w*|correct\w*|"
    r"crash|broken|defect|revert\w*)\b", re.I)
_IMPROVE_RE = re.compile(
    r"\b(?:improv\w*|refactor\w*|polish\w*|cleanup|clean up|optimi\w*|"
    r"rework\w*|extend\w*|enhance\w*|tweak\w*|renam\w*|simplif\w*|"
    r"updat\w*|upgrad\w*|faster|perf|harden\w*|clarif\w*|"
    r"streamline\w*|redesign\w*)\b", re.I)

# Merge commits authored by the conductor: 'Merge branch
# 'conductor/<run-dir-name>/<step>' (also covers resolve-* sessions).
_MERGE_RE = re.compile(
    r"^Merge branch ['\"]?conductor/([^/'\"\s]+)/([^/'\"\s]+)['\"]?")

_REQUEST_ID_RE = re.compile(r"^\d{8}-\d{6}-[0-9a-f]{8}$")
_SHA_PREFIX_RE = re.compile(r"^[0-9a-fA-F]{4,64}$")


# ------------------------------------------------------------------ helpers

def _git(repo_dir: Path, *args: str) -> subprocess.CompletedProcess:
    kw = {"creationflags": subprocess.CREATE_NO_WINDOW} \
        if os.name == "nt" else {}
    return subprocess.run(["git", "-C", str(repo_dir), *args],
                          capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=60,
                          **kw)


def _iso_ts(s) -> float | None:
    """ISO stamp -> epoch; tolerant of naive (local) and aware stamps."""
    if not isinstance(s, str):
        return None
    try:
        return dt.datetime.fromisoformat(s).timestamp()
    except ValueError:
        return None


def _in_window(stamp, base_at, tip_at) -> bool:
    """True when ``stamp`` (iso str) lands in (base_at, tip_at]. Either
    bound may be None (unbounded)."""
    ts = _iso_ts(stamp)
    if ts is None:
        return False
    lo, hi = _iso_ts(base_at), _iso_ts(tip_at)
    if lo is not None and ts <= lo:
        return False
    if hi is not None and ts > hi:
        return False
    return True


def _tag_for_subject(subject: str) -> str:
    if _FIX_RE.search(subject or ""):
        return "fixed"
    if _IMPROVE_RE.search(subject or ""):
        return "improved"
    return "new"


def _card_tag(commits: list[dict], explicit: str | None = None) -> str:
    """Deterministic card tag: an explicit (curated) tag wins; otherwise
    the strongest signal among commits, fixed > improved > new."""
    if explicit in TAGS:
        return explicit
    tags = {_tag_for_subject(c["subject"]) for c in commits}
    for t in ("fixed", "improved", "new"):
        if t in tags:
            return t
    return "new"


def _short(text: str, n: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= n else text[:n - 1].rstrip() + "…"


def _anchor(stop: str | None) -> str | None:
    st = TOUR_STOPS.get(stop or "")
    return st["selector"] if st else None


def _stop_title(stop: str | None) -> str:
    if stop and stop in TOUR_STOPS:
        return TOUR_STOPS[stop]["title"]
    return _AREA_TITLES.get(stop or "", "Other changes")


def _section(e: dict) -> str:
    """The feed section a card belongs to. 'unknown' history stubs keep
    whatever their id prefix says (record ids are work, the rest tool)."""
    k = e.get("kind")
    if k in _TOOL_KINDS:
        return "tool"
    if k in _WORK_KINDS:
        return "work"
    i = e.get("id") or ""
    return ("work" if i.startswith(("request:", "run:", "followup:"))
            else "tool")


def _run_outcome(st: dict) -> str:
    """The run's actual terminal outcome: a recorded halt reason
    ('paused', 'stopped', 'drained', ...) wins; otherwise the step
    verdicts - 'done' only when every recorded step closed clean."""
    ab = st.get("aborted")
    if isinstance(ab, str) and ab:
        return ab
    sts = [r.get("status") for r in (st.get("steps") or {}).values()
           if isinstance(r, dict)]
    if not sts:
        return "ended"
    if any(s in ("failed", "timed_out", "lost") for s in sts):
        return "failed"
    if all(s in ("done", "superseded", "skipped") for s in sts):
        return "done"
    return "partial"


# ------------------------------------------------------------- review data
# The unreviewed-task count a work card carries. Mirrors conductor.py's
# sentinel/folded review-mark layering (review-* flags in the shared
# flags dir win on `at` ties over the mark folded into state.json) -
# duplicated here, stdlib-only, so this module never imports the
# monolith. Read-only: counting never writes a mark.

def _flag_key(run_dir: Path) -> str:
    """Same name conductor._flag_key derives: the sentinel pins the run's
    resolved path so equal dir names in two checkouts don't collide."""
    h = hashlib.sha256(os.path.normcase(
        str(run_dir.resolve())).encode()).hexdigest()[:8]
    safe = re.sub(r"[^\w.-]", "_", run_dir.name)
    return f"{safe}-{h}"


def _review_mark(run_dir: Path, stored, step: str | None,
                 flags_dir: Path) -> dict | None:
    """The newest task review mark: the live sentinel body when present,
    else the mark already folded into state.json - whichever is newer
    by `at`."""
    n = "review-" + _flag_key(run_dir)
    safe = re.sub(r"[^\w.-]", "_", step) if step else ""
    p = flags_dir / (f"{n}-{safe}" if safe else n)
    flag = None
    try:
        body = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(body, dict) and "reviewed" in body:
            flag = body
    except (OSError, ValueError):
        pass
    if flag is None:
        return stored if isinstance(stored, dict) else None
    s_at = str(stored.get("at") or "") if isinstance(stored, dict) else ""
    return flag if str(flag.get("at") or "") >= s_at else stored


def _needs_review(seq, mark: dict | None) -> bool:
    """Same rule as conductor._needs_review: an explicit tombstone, or a
    recorded completion_seq the newest mark does not cover."""
    if isinstance(mark, dict) and mark.get("reviewed") is False:
        return True
    return seq is not None and (not isinstance(mark, dict)
                                or mark.get("seq") != seq)


def _unreviewed_tasks(run_dir: Path, st: dict, flags_dir: Path) -> int:
    """How many of a finished run's task records still need a human
    look - the count the dashboard derives from the same marks."""
    n = 0
    for sname, srec in (st.get("steps") or {}).items():
        if isinstance(srec, dict) and _needs_review(
                srec.get("completion_seq"),
                _review_mark(run_dir, srec.get("review"), sname,
                             flags_dir)):
            n += 1
    return n


# ------------------------------------------------------------------ git

def collect_commits(repo_dir: Path, base_commit: str | None,
                    tip_commit: str | None) -> tuple[list[dict], str | None]:
    """Commits in ``base..tip`` (or everything reachable from tip when
    base is None), newest first.

    Returns (commits, warning). Each commit: {sha, subject, at, files,
    merge} where merge is (run_dir_name, step) when the subject is a
    conductor worktree merge."""
    if not tip_commit or base_commit == tip_commit:
        return [], None
    spec = f"{base_commit}..{tip_commit}" if base_commit else tip_commit
    r = _git(repo_dir, "log", spec,
             "--format=%x1e%H%x00%P%x00%s%x00%cI%x00", "--name-only", "-z")
    if r.returncode != 0:
        return [], (f"git log {spec} failed: "
                    f"{(r.stderr or '').strip()[:160]}")
    out: list[dict] = []
    for rec in r.stdout.split("\x1e"):
        if not rec.strip():
            continue
        parts = rec.split("\x00")
        if len(parts) < 4:
            continue
        sha, parents, subject, at = (parts[0].strip(), parts[1].strip(),
                                     parts[2].strip(), parts[3].strip())
        if not sha:
            continue
        files = [p.strip() for p in parts[4:] if p.strip()]
        m = _MERGE_RE.match(subject) if len(parents.split()) > 1 else None
        out.append({"sha": sha, "subject": subject, "at": at,
                    "files": files,
                    "merge": (m.group(1), m.group(2)) if m else None})
    return out, None


def _merged_chain(repo_dir: Path, sha: str) -> set[str]:
    """Commits a merge brought in (``merge^1..merge^2``) - the worktree
    branch's own commits, so they can attribute to the run card instead
    of re-grouping by file path."""
    r = _git(repo_dir, "rev-list", f"{sha}^1..{sha}^2")
    if r.returncode != 0:
        return set()
    return {ln.strip() for ln in r.stdout.splitlines() if ln.strip()}


# Path-rule areas that carry a change on their own vs support dirs that
# only describe one when nothing else was touched.
_PRIMARY_AREAS = {"assistant", "tour", "mobile"}
_SUPPORT_AREAS = {"tests", "manifests", "tracking"}


def _commit_area(subject: str, files: list[str]) -> str:
    """The feature area a commit belongs to.

    Distinct REAL areas win (one = it, several = 'mixed'); a change that
    only touched conductor.py (the monolith) falls back to subject
    keywords; a tests/docs/manifests-only change keeps its support area;
    unmatched/other root files land in 'conductor'."""
    primary, support, monolith = set(), set(), False
    for f in files:
        for rx, area in _PATH_RULES:
            if rx.search(f):
                if area is None:
                    monolith = True
                elif area in _PRIMARY_AREAS:
                    primary.add(area)
                else:
                    support.add(area)
                break
        else:
            primary.add("conductor")
    if len(primary) == 1:
        return next(iter(primary))
    if len(primary) > 1:
        return "mixed"
    if monolith or not support:
        for rx, stop in _SUBJECT_RULES:
            if rx.search(subject or ""):
                return stop
        return "conductor"
    return sorted(support)[0]


# ------------------------------------------------------------------ records

def _iter_requests(requests_dir: Path) -> list[dict]:
    """Every well-formed request record on disk (any age - callers need
    them for association as well as for the window feed)."""
    out = []
    try:
        files = sorted(requests_dir.glob("*.json"))
    except OSError:
        return out
    for p in files:
        if not _REQUEST_ID_RE.fullmatch(p.stem):
            continue
        try:
            rec = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(rec, dict) and isinstance(rec.get("id"), str):
            out.append(rec)
    return out


def _iter_runs(runs_dir: Path) -> list[tuple[str, Path, dict]]:
    """(dir name, dir path, state.json dict) for every recorded run."""
    out = []
    try:
        files = sorted(runs_dir.glob("*/state.json"))
    except OSError:
        return out
    for p in files:
        try:
            st = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(st, dict):
            out.append((p.parent.name, p.parent, st))
    return out


def _run_key(dir_name: str, st: dict | None,
             run_dir_abs: Path | None = None) -> str:
    """The dashboard's run key: request:<id> when the run carries a
    request, else the run dir path (same rule collect_dashboard uses)."""
    rid = (st or {}).get("request_id")
    if rid:
        return f"request:{rid}"
    if run_dir_abs is not None:
        return str(run_dir_abs)
    return dir_name


def _is_followup_request(rec: dict) -> bool:
    return bool(rec.get("source_run") or rec.get("source_manifest")
                or rec.get("imported") == "followup")


# ------------------------------------------------------------------ curated

def load_curated(manifests_dir: Path) -> tuple[list[dict], list[str]]:
    """Parse manifests/whatsnew.jsonl. Tolerant: malformed JSON, non-
    dicts, missing/empty blurb or stop, unusable tag, and records with
    no resolvable change identity are skipped with a warning - never
    fatal. Valid record: {stop, blurb, title?, tag?, commits?|requests?|
    runs?|since?+until?}."""
    path = manifests_dir / CURATED_NAME
    records, warnings = [], []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return records, warnings          # absent file is not an error
    for i, line in enumerate(lines, 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        where = f"{CURATED_NAME}:{i}"
        try:
            rec = json.loads(line)
        except ValueError:
            warnings.append(f"{where}: not JSON - skipped")
            continue
        if not isinstance(rec, dict):
            warnings.append(f"{where}: not an object - skipped")
            continue
        blurb, stop = rec.get("blurb"), rec.get("stop")
        if not isinstance(blurb, str) or not blurb.strip():
            warnings.append(f"{where}: missing blurb - skipped")
            continue
        if not isinstance(stop, str) or not stop.strip():
            warnings.append(f"{where}: missing stop - skipped")
            continue
        tag = rec.get("tag")
        commits = [c for c in (rec.get("commits") or [])
                   if isinstance(c, str) and _SHA_PREFIX_RE.fullmatch(c)]
        reqs = [r for r in (rec.get("requests") or [])
                if isinstance(r, str) and r.strip()]
        runs = [r for r in (rec.get("runs") or [])
                if isinstance(r, str) and r.strip()]
        if not commits and not reqs and not runs \
                and not (rec.get("since") and rec.get("until")):
            warnings.append(
                f"{where}: no change identity (commits/requests/runs/"
                "since+until) - skipped")
            continue
        records.append({
            "line": i, "stop": stop.strip(), "blurb": blurb.strip(),
            "title": (rec["title"].strip()
                      if isinstance(rec.get("title"), str)
                      and rec["title"].strip() else None),
            "tag": tag if tag in TAGS else None,
            "commits": commits, "requests": reqs, "runs": runs,
            "since": (rec["since"] if isinstance(rec.get("since"), str)
                      else None),
            "until": (rec["until"] if isinstance(rec.get("until"), str)
                      else None),
            "at": (rec["at"] if isinstance(rec.get("at"), str) else None)})
    return records, warnings


# ------------------------------------------------------------------ feed

def _resolve_curated(records: list[dict], commits: list[dict],
                     win_reqs: dict[str, dict],
                     win_runs: dict[str, tuple[str, dict]],
                     run_key_of, repo_dir: Path,
                     warnings) -> tuple[list[dict], set[str],
                                        dict[str, dict],
                                        dict[str, set[str]]]:
    """Bind curated records to the window.

    Returns (claim_cards, claimed_shas, augments, aug_shas): claim_cards
    are dedicated curated cards; claimed_shas are commits removed from
    area grouping; augments maps area -> record (fold-in upgrades) and
    aug_shas maps that area -> the record's matched commit shas, so a
    polished card that claims them under a different stop still folds the
    human text in (generated text never overrides curated)."""
    cards, augments = [], {}
    aug_shas: dict[str, set[str]] = {}
    claimed: set[str] = set()
    for rec in records:
        where = f"{CURATED_NAME}:{rec['line']}"
        # resolve commit identity: explicit prefixes and/or since..until
        want = set(rec["commits"])
        if rec["since"] and rec["until"]:
            r = _git(repo_dir, "rev-list",
                     f"{rec['since']}..{rec['until']}")
            if r.returncode == 0:
                want.update(ln.strip()
                            for ln in r.stdout.splitlines() if ln.strip())
            else:
                warnings.append(f"{where}: since..until does not "
                                "resolve - skipped")
                continue
        matched = [c for c in commits
                   if any(c["sha"].startswith(p) for p in want)]
        # resolve record identity
        hit_reqs = [rid for rid in rec["requests"] if rid in win_reqs]
        hit_runs = [k for r in rec["runs"] for k in [run_key_of(r)]
                    if k in win_runs]
        matched = [c for c in matched if c["sha"] not in claimed]
        if not matched and not hit_reqs and not hit_runs:
            warnings.append(f"{where}: nothing in this window matched "
                            "its change identity - skipped")
            continue
        # Pure record-claims fold their human text INTO the named cards
        # (the record card keeps its stable id); commit claims either
        # fold into their area card or extract into a dedicated card.
        if not matched:
            augments.setdefault("records", []).append(
                {**rec, "requests": hit_reqs, "runs": hit_runs})
            continue
        claimed.update(c["sha"] for c in matched)
        areas = {_commit_area(c["subject"], c["files"]) for c in matched}
        if not hit_reqs and not hit_runs and len(areas) == 1 \
                and next(iter(areas)) == rec["stop"]:
            if rec["stop"] not in augments:     # first fold wins
                augments[rec["stop"]] = rec
                aug_shas[rec["stop"]] = {c["sha"] for c in matched}
            claimed.difference_update(c["sha"] for c in matched)
            continue
        tip = matched[0]["sha"]
        cards.append({"id": f"curated:{rec['stop']}:{tip[:_CARD_KM]}",
                      "stop": rec["stop"], "rec": rec, "commits": matched,
                      "requests": hit_reqs, "runs": hit_runs})
    return cards, claimed, augments, aug_shas


def _source_tasks(rng: dict, *, repo_dir: Path, runs_dir: Path,
                  requests_dir: Path, manifests_dir: Path) -> dict:
    """The four independent collectors for one window as {name: fn}.
    Each returns the partial sources dict it fills and has no shared
    mutable state, so they can run on a shared pool."""
    base, tip = rng.get("base") or {}, rng.get("tip") or {}

    def _commits():
        commits, warn = collect_commits(repo_dir, base.get("commit"),
                                        tip.get("commit"))
        return {"commits": commits, "commit_warn": warn}

    def _runs():
        return {"runs_all": _iter_runs(runs_dir)}

    def _requests():
        return {"reqs_all": _iter_requests(requests_dir)}

    def _curated():
        records, warns = load_curated(manifests_dir)
        return {"curated": records, "curated_warns": warns}

    return {"commits": _commits, "runs": _runs,
            "requests": _requests, "curated": _curated}


def _source_run(name: str, fn) -> tuple[str, dict, str | None]:
    """Run one collector into a (name, partial-sources, error) triple -
    a source that raises degrades to an empty partial result so the
    window keeps every other collection."""
    try:
        return name, fn(), None
    except Exception as exc:                            # noqa: BLE001
        return name, {}, f"{_SOURCE_LABEL[name]} scan failed: {exc}"


def _collect_sources(tasks: dict, *, pool=None, warnings=None,
                     progress=None, on_source=None) -> dict:
    """Reduce one window's collectors into the sources dict.

    Serial without a pool; with one, the independent scans overlap
    (bounded by the pool's size) and gather on the caller's thread, so
    no result dict is ever shared across workers. Per-source warnings
    land in _SOURCE_ORDER regardless of completion order, and
    ``on_source(name, error)`` reports each completion for staged
    progress."""
    def _stage(s: str | None) -> None:
        if s and progress:
            progress(s)

    out = {"commits": [], "commit_warn": None, "runs_all": [],
           "reqs_all": [], "curated": [], "curated_warns": []}
    warns: dict[str, str] = {}
    if pool is None:
        for name in _SOURCE_ORDER:
            _stage(_SOURCE_STAGE.get(name))
            _, res, err = _source_run(name, tasks[name])
            out.update(res)
            if err:
                warns[name] = err
            else:
                _stage(_SOURCE_DONE[name])
            if on_source is not None:
                on_source(name, err)
    else:
        for name in _SOURCE_ORDER:
            _stage(_SOURCE_STAGE.get(name))
        futs = {name: pool.submit(_source_run, name, tasks[name])
                for name in _SOURCE_ORDER}
        for fut in as_completed(futs.values()):
            name, res, err = fut.result()      # _source_run never raises
            out.update(res)
            if err:
                warns[name] = err
            else:
                _stage(_SOURCE_DONE[name])
            if on_source is not None:
                on_source(name, err)
    if warnings is not None:
        warnings.extend(warns[n] for n in _SOURCE_ORDER if n in warns)
    return out


def _collect_window(rng: dict, *, repo_dir: Path, runs_dir: Path,
                    requests_dir: Path, manifests_dir: Path,
                    warnings: list[str], polish=None,
                    flags_dir: Path = DEFAULT_FLAGS_DIR,
                    progress=None, pool=None, emit=None) -> list[dict]:
    """Raw feed entries for one {base,tip} window - no state annotation.
    Reused by build_feed (live window) and history (stored windows).

    ``polish`` is an optional callable ``(rng) -> validated card list``
    (tour_polish.Polisher.cards_for): when it returns cards for this
    range, its commit groupings replace the deterministic area grouping
    for the commits they claim; everything else - run/request/follow-up/
    curated cards, leftover commits - stays deterministic.

    ``progress`` is an optional ``(stage: str) -> None`` hook called at
    phase boundaries - the dashboard's loading screen narrates it.

    ``pool`` is an optional shared ThreadPoolExecutor used for the four
    independent window scans and for merge-chain lookups (submissions
    only ever come from this thread - pool workers never submit, so the
    bounded pool can't deadlock).

    ``emit`` is the staged-publish hook ``emit(group, parts)``: interim
    'collect' parts carry ``{"sources": {name: 'ok'|'error'}}`` (with
    ``"complete": True`` once every scan resolved); 'tool' and 'work'
    parts carry ``{"entries": [raw cards]}`` - raw, unannotated cards,
    emitted as soon as each group is assembled so a caller can publish
    tool updates while the work digest is still being built. Raw card
    dicts stay owned by this window's result - sinks must copy, never
    mutate."""
    def _stage(s: str) -> None:
        if progress:
            progress(s)

    base, tip = rng.get("base") or {}, rng.get("tip") or {}
    seen_src: dict[str, str] = {}

    def _src_done(name: str, err: str | None) -> None:
        seen_src[name] = "error" if err else "ok"
        if emit is not None:
            emit("collect", {"sources": dict(seen_src),
                             "complete": False})

    sources = _collect_sources(
        _source_tasks(rng, repo_dir=repo_dir, runs_dir=runs_dir,
                      requests_dir=requests_dir,
                      manifests_dir=manifests_dir),
        pool=pool, warnings=warnings, progress=progress,
        on_source=_src_done)
    if emit is not None:
        emit("collect", {"sources": dict(seen_src), "complete": True})
    commits, warn = sources["commits"], sources["commit_warn"]
    if warn:
        warnings.append(warn)
    runs_all = sources["runs_all"]
    reqs_all = sources["reqs_all"]
    # association maps over ALL records, not just the window's
    key_by_dir = {d: _run_key(d, st, p) for d, p, st in runs_all}
    run_key_by_req = {st.get("request_id"): _run_key(d, st, p)
                      for d, p, st in runs_all if st.get("request_id")}
    req_by_dir = {d: st.get("request_id")
                  for d, p, st in runs_all if st.get("request_id")}
    req_by_id = {r["id"]: r for r in reqs_all}

    def run_key_of(ref: str) -> str:
        # a curated 'runs' entry may name a dir, a request:<id> key, or
        # a bare request id - normalize to the dashboard key form
        if ref in key_by_dir:
            return key_by_dir[ref]
        rid = ref.split(":", 1)[-1]
        return run_key_by_req.get(rid, ref)

    win_runs = {}   # run key -> (dir_name, dir_path, st)
    for d, p, st in runs_all:
        if _in_window(st.get("ended_at"), base.get("at"), tip.get("at")):
            win_runs[key_by_dir[d]] = (d, p, st)
    win_reqs = {}   # request id -> rec
    for rec in reqs_all:
        if _in_window(rec.get("created_at"), base.get("at"), tip.get("at")):
            win_reqs[rec["id"]] = rec

    # ---- curated records (claims resolve before cards form so a change
    # never appears both curated and raw)
    _stage("resolving curated updates")
    warnings.extend(sources["curated_warns"])
    cards, claimed, augments, aug_shas = _resolve_curated(
        sources["curated"], commits, win_reqs, win_runs, run_key_of,
        repo_dir, warnings)
    fold_req = {}                       # request id -> rec
    fold_run = {}                       # run key -> rec
    for rec in augments.get("records", []):
        for rid in rec["requests"]:
            fold_req.setdefault(rid, rec)
        for rk in rec["runs"]:
            fold_run.setdefault(rk, rec)
    suppress_req = {rid for card in cards for rid in card["requests"]}
    suppress_run = {rk for card in cards for rk in card["runs"]}

    # ---- work-card folding: a request + the run it launched share one
    # card when both events sit inside this window; a follow-up proposal
    # folds into its associated request's card when that request is also
    # in the window. Links are the RECORDED relationships (request_id /
    # source_request / source_run), never display-name matches.
    def assoc_req(rec: dict) -> str | None:
        """The request a follow-up proposal belongs to: its recorded
        source_request, else the source run's own request link."""
        if isinstance(rec.get("source_request"), str) \
                and rec["source_request"]:
            return rec["source_request"]
        if isinstance(rec.get("source_run"), str):
            rid = req_by_dir.get(rec["source_run"])
            if rid:
                return rid
        k = rec.get("source_run_key") or ""
        return k.split(":", 1)[1] if k.startswith("request:") else None

    fold_into: dict[str, str] = {}   # follow-up rid -> associated rid
    pipe: dict[str, str] = {}        # request rid -> in-window run key
    for rid, rec in win_reqs.items():
        if rid in suppress_req:
            continue
        launched = run_key_by_req.get(rid)
        has_run = launched in win_runs and launched not in suppress_run
        if _is_followup_request(rec):
            src = assoc_req(rec)
            if src and src != rid and src in win_reqs \
                    and src not in suppress_req:
                fold_into[rid] = src
                continue
        if has_run:
            pipe[rid] = launched

    # ---- conductor-merge attribution: a merge naming an in-window run
    # claims its whole chain for that run's card. Chain lookups are
    # independent git calls - the shared pool overlaps them.
    run_by_dir = {d: k for k, (d, _p, _st) in win_runs.items()}
    need_chains = {c["sha"]: run_by_dir[c["merge"][0]] for c in commits
                   if c["merge"] and c["merge"][0] in run_by_dir
                   and run_by_dir[c["merge"][0]] not in suppress_run}
    if pool is not None and len(need_chains) > 1:
        chain_futs = {s: pool.submit(_merged_chain, repo_dir, s)
                      for s in need_chains}
        chains = {s: f.result() for s, f in chain_futs.items()}
    else:
        chains = {s: _merged_chain(repo_dir, s) for s in need_chains}
    claim_to_run: dict[str, str] = {}     # commit sha -> run key
    for sha, rk in need_chains.items():
        claim_to_run[sha] = rk
        for chain_sha in chains.get(sha, ()):
            claim_to_run.setdefault(chain_sha, rk)
    # ---- Tool updates first: commit/curated cards assemble from the
    # shared maps and publish before the work digest so the first usable
    # card is never behind run-directory detail scans.
    # Validated LLM groupings first (they claim the commits they name),
    # then deterministic area grouping for the rest. A polished card
    # keeps the deterministic id scheme (area:<stop>:<newest-sha>) so
    # persisted viewed/snoozed descriptors re-resolve identically;
    # curated fold-ins still win over generated text, and record links
    # are re-filtered against this window's real records.
    _stage("assembling cards")
    tool: list[dict] = []
    grouped: set[str] = set()
    aug_used: set[str] = set()
    try:
        polished = polish(rng) if callable(polish) else None
    except Exception:                                    # noqa: BLE001
        polished = None         # a broken enrichment hook never breaks
        #                       # the deterministic feed
    if polished:
        free = {c["sha"]: c for c in commits
                if c["sha"] not in claimed and c["sha"] not in claim_to_run}
        for card in polished:
            if not isinstance(card, dict):
                continue
            stop, title, blurb = (card.get("stop"), card.get("title"),
                                  card.get("blurb"))
            if not isinstance(stop, str) or not stop \
                    or not isinstance(title, str) \
                    or not isinstance(blurb, str):
                continue
            want = {s for s in (card.get("commits") or [])
                    if isinstance(s, str)}
            cs = [c for c in commits
                  if c["sha"] in want and c["sha"] in free
                  and c["sha"] not in grouped]
            if not cs:
                continue
            grouped.update(c["sha"] for c in cs)
            # a curated fold-in still wins over generated text: first the
            # augment naming this stop, else an augment whose claimed
            # commits this card grouped under a different stop
            aug_key = (stop if stop in augments and stop != "records"
                       and stop not in aug_used else None)
            if aug_key is None:
                cshas = {c["sha"] for c in cs}
                aug_key = next((k for k, shas in aug_shas.items()
                                if k not in aug_used and shas & cshas),
                               None)
            aug = augments.get(aug_key) if aug_key is not None else None
            if aug is not None:
                aug_used.add(aug_key)
            tag = card.get("tag")
            tool.append({
                "id": f"area:{stop}:{cs[0]['sha'][:_CARD_KM]}",
                "kind": "change", "stop": stop,
                "title": (aug["title"] if aug and aug["title"]
                          else _short(title, _TITLE_MAX)),
                "blurb": aug["blurb"] if aug
                         else _short(blurb, _BLURB_MAX),
                "tag": _card_tag(cs, (aug["tag"] if aug else
                                      (tag if tag in TAGS else None))),
                "commits": [{"sha": c["sha"], "subject": c["subject"]}
                            for c in cs],
                "runs": [r for r in (card.get("runs") or [])
                         if r in win_runs],
                "requests": [r for r in (card.get("requests") or [])
                             if r in win_reqs],
                "at": cs[0]["at"], "range": rng, "polished": True})
    groups: dict[str, list[dict]] = {}
    for c in commits:
        if c["sha"] in claimed or c["sha"] in claim_to_run \
                or c["sha"] in grouped:
            continue
        groups.setdefault(_commit_area(c["subject"], c["files"]),
                          []).append(c)
    for area, cs in groups.items():
        aug = augments.get(area) if area != "records" \
            and area not in aug_used else None
        if aug is not None:
            aug_used.add(area)
        subjects = [c["subject"] for c in cs]
        title = (aug["title"] if aug and aug["title"]
                 else (_short(subjects[0], _TITLE_MAX) if len(cs) == 1
                       else f"{_stop_title(area)} - {len(cs)} changes"))
        blurb = (aug["blurb"] if aug else
                 _short(" · ".join(subjects[:3])
                        + (f" (+{len(cs) - 3} more)" if len(cs) > 3
                           else ""), _BLURB_MAX))
        tool.append({
            "id": f"area:{area}:{cs[0]['sha'][:_CARD_KM]}",
            "kind": "change", "stop": area, "title": title,
            "blurb": blurb, "tag": _card_tag(cs, aug["tag"] if aug else None),
            "commits": [{"sha": c["sha"], "subject": c["subject"]}
                        for c in cs],
            "runs": [], "requests": [],
            "at": cs[0]["at"], "range": rng})

    # ---- dedicated curated cards (claims spanning areas, or naming a
    # stop different from the commits' own grouping)
    for card in cards:
        rec, cs = card["rec"], card["commits"]
        title = rec["title"] or (_short(cs[0]["subject"], _TITLE_MAX)
                                 if len(cs) == 1 else
                                 _stop_title(card["stop"]))
        tool.append({
            "id": card["id"], "kind": "curated", "stop": card["stop"],
            "title": title, "blurb": rec["blurb"],
            "tag": _card_tag(cs, rec["tag"]),
            "commits": [{"sha": c["sha"], "subject": c["subject"]}
                        for c in cs],
            "runs": card["runs"], "requests": card["requests"],
            "at": (cs[0]["at"] if cs else rec["at"] or tip.get("at")),
            "range": rng})
    if emit is not None:
        emit("tool", {"entries": tool})

    # ---- work digest: finished runs' cards + conductor-merge
    # attribution, then standalone request/follow-up cards
    work: list[dict] = []
    cards_for_rid: dict[str, dict] = {}   # rid -> its emitted card
    for key, (dname, dirp, st) in win_runs.items():
        if key in suppress_run:
            continue
        own = [c for c in commits if claim_to_run.get(c["sha"]) == key]
        counts = {}
        for srec in (st.get("steps") or {}).values():
            s = (srec or {}).get("status", "?")
            counts[s] = counts.get(s, 0) + 1
        bits = ", ".join(f"{n} {s}" for s, n in sorted(counts.items()))
        outcome = _run_outcome(st)
        rid = st.get("request_id")
        prec = win_reqs.get(rid) if rid else None
        folded = prec is not None and pipe.get(rid) == key
        name = st.get("run") or dname
        title = f"Run finished: {name}"
        aug = fold_run.get(key) or (fold_req.get(rid) if folded else None)
        label = ((prec or {}).get("label") or (prec or {}).get("raw")
                 or rid) if prec else None
        e = {
            "id": f"run:{dname}:{st.get('ended_at')}",
            "kind": "pipeline" if folded else "run", "stop": "runs",
            "title": (aug["title"] or title) if aug else title,
            "blurb": aug["blurb"] if aug else
                     _short((f"Proposal '{label}' - " if folded else "")
                            + (st.get("description") or "")
                            + (f"  Steps: {bits}." if bits else ""),
                            _BLURB_MAX) or title,
            "tag": outcome,
            "commits": [{"sha": c["sha"], "subject": c["subject"]}
                        for c in own],
            "runs": [key],
            "requests": ([rid] if rid else []),
            "at": st.get("ended_at"), "range": rng,
            # the run's terminal verdict + delivery detail - the 'done/
            # failed' event words the work digest renders on the card
            "outcome": outcome,
            "completed_at": st.get("ended_at"),
            "proposed_at": ((prec or req_by_id.get(rid) or {})
                            .get("created_at") if rid else None),
            "steps_done": counts.get("done", 0),
            "steps_total": len(st.get("steps") or {}),
            "unreviewed": _unreviewed_tasks(dirp, st, flags_dir)}
        if folded:
            e["covers"] = [f"followup:{rid}" if _is_followup_request(prec)
                           else f"request:{rid}"]
            cards_for_rid[rid] = e
        work.append(e)

    # ---- request + follow-up cards (events whose counterparts fell in
    # a different window stay standalone)
    for rid, rec in win_reqs.items():
        if rid in suppress_req or rid in pipe or rid in fold_into:
            continue
        launched = run_key_by_req.get(rid)
        label = rec.get("label") or rec.get("raw") or rid
        aug = fold_req.get(rid)
        if _is_followup_request(rec):
            src_key = (rec.get("source_run_key")
                       or (run_key_of(rec["source_run"])
                           if rec.get("source_run") else None))
            title = f"Follow-up proposal: {_short(label, 80)}"
            e = {"id": f"followup:{rid}", "kind": "followup",
                 "stop": "requests",
                 "title": (aug["title"] or title) if aug else title,
                 "blurb": aug["blurb"] if aug else
                          _short(rec.get("description")
                                 or f"Registered from {rec.get('source_run')}",
                                 _BLURB_MAX),
                 "tag": TAG_REQUEST,
                 "commits": [], "requests": [rid],
                 "runs": ([src_key] if src_key else [])
                         + ([launched] if launched else []),
                 "at": rec.get("created_at"), "range": rng,
                 "proposed_at": rec.get("created_at")}
        else:
            title = f"New proposal: {_short(label, 80)}"
            e = {"id": f"request:{rid}", "kind": "request",
                 "stop": "requests",
                 "title": (aug["title"] or title) if aug else title,
                 "blurb": aug["blurb"] if aug else
                          (_short(rec.get("description") or rec.get("raw")
                                  or "", _BLURB_MAX)
                           or "A new request record was filed."),
                 "tag": TAG_REQUEST,
                 "commits": [], "requests": [rid],
                 "runs": [launched] if launched else [],
                 "at": rec.get("created_at"), "range": rng,
                 "proposed_at": rec.get("created_at")}
        work.append(e)
        cards_for_rid[rid] = e

    # ---- fold follow-up proposals into their associated request's
    # card - the root card after chasing fold chains (a follow-up's
    # associated request may itself be a folded follow-up)
    for frid, src in fold_into.items():
        seen, root = {frid}, src
        while root in fold_into and root not in seen:
            seen.add(root)
            root = fold_into[root]
        card = cards_for_rid.get(root)
        rec = win_reqs[frid]
        if card is None:
            # no reachable card (theoretical fold cycle): keep the
            # standalone follow-up card rather than dropping the event
            launched = run_key_by_req.get(frid)
            src_key = (rec.get("source_run_key")
                       or (run_key_of(rec["source_run"])
                           if rec.get("source_run") else None))
            label = rec.get("label") or rec.get("raw") or frid
            work.append({
                "id": f"followup:{frid}", "kind": "followup",
                "stop": "requests",
                "title": f"Follow-up proposal: {_short(label, 80)}",
                "blurb": _short(rec.get("description")
                                or f"Registered from "
                                   f"{rec.get('source_run')}",
                                _BLURB_MAX),
                "tag": TAG_REQUEST,
                "commits": [], "requests": [frid],
                "runs": ([src_key] if src_key else [])
                        + ([launched] if launched else []),
                "at": rec.get("created_at"), "range": rng,
                "proposed_at": rec.get("created_at")})
            continue
        launched = run_key_by_req.get(frid)
        src_key = (rec.get("source_run_key")
                   or (run_key_of(rec["source_run"])
                       if rec.get("source_run") else None))
        card.setdefault("followups", []).append({
            "id": f"followup:{frid}",
            "label": rec.get("label") or rec.get("raw") or frid,
            "at": rec.get("created_at"),
            "requests": [frid],
            "runs": ([src_key] if src_key else [])
                    + ([launched] if launched else [])})
        card.setdefault("covers", []).append(f"followup:{frid}")
    if emit is not None:
        emit("work", {"entries": work})
    return work + tool


def _rematerialize(desc: dict, collect) -> dict:
    """Rebuild the card a recorded descriptor pointed at, over the
    descriptor's own stored range - the only way a viewed entry stays
    retrievable once the live watermark advanced past its window. The
    descriptor's standalone id also matches a card that lists it in
    ``covers`` (a request card folded into a pipeline still resolves).
    The returned dict is a copy: the shared per-build collection must
    never be mutated by card assembly."""
    rng = desc.get("range") or {}
    try:
        for e in collect(rng):
            if e["id"] == desc["id"] \
                    or desc["id"] in (e.get("covers") or ()):
                return dict(e)
    except Exception:
        pass
    return {"id": desc["id"], "kind": "unknown", "stop": None,
            "title": "Change (history)",
            "blurb": "This card's recorded window can no longer be "
                     "rebuilt (history rewritten or records gone).",
            "tag": "new", "commits": [], "runs": [], "requests": [],
            "at": (rng.get("tip") or {}).get("at"), "range": rng,
            "unavailable": True}


def _range_key(rng: dict) -> tuple:
    """The exact (base, tip) window a collection covers - the per-build
    memo key."""
    b, t = (rng or {}).get("base") or {}, (rng or {}).get("tip") or {}
    return (b.get("commit"), b.get("at"), t.get("commit"), t.get("at"))


def build_feed(state: dict | None = None, boundary: dict | None = None,
               *, repo_dir: Path = DEFAULT_REPO,
               runs_dir: Path = DEFAULT_RUNS_DIR,
               requests_dir: Path = DEFAULT_REQUESTS_DIR,
               manifests_dir: Path = DEFAULT_MANIFESTS_DIR,
               flags_dir: Path | None = None,
               now: float | None = None, polish=None,
               progress=None, emit=None, workers: int | None = None,
               _pool=None) -> dict:
    """The full tour feed for the current watermark + boundary.

    ``entries`` = window cards still needing eyes (state 'new' or
    'snoozed' - 'viewed' cards are excluded from the live deck even
    before the watermark advances) plus re-injected unviewed
    descriptors; ``history`` = re-materialized viewed cards for explicit
    reopening; ``unseen_count`` = entries still 'new'. Pure reads only -
    nothing here mutates persisted state.

    ``polish`` is an optional ``(rng) -> cards|None`` hook (the tour
    enrichment cache, tour_polish.Polisher.cards_for): it is consulted
    per window - including the stored ranges history re-materializes -
    so polished cards reproduce the same ids a descriptor recorded.

    ``progress`` is an optional ``(stage: str) -> None`` hook the
    dashboard's loading screen narrates while the build runs.

    ``emit`` is the optional staged-delivery hook ``emit(group, parts)``
    the server uses to publish progressively: each group's parts arrive
    already state-annotated and viewed-filtered, with ``warnings``
    attached - 'collect' (``sources`` status + the live
    ``boundary``/``range``), 'tool' and 'work' (``entries``), and
    'history' (``entries`` reinjections + ``history`` cards). A
    ``workers`` count bounds the shared collection pool (default
    _COLLECT_WORKERS; 1 keeps every scan serial); ``_pool`` lets a
    caller inject its own bounded executor instead. The emitted groups
    are exactly the same cards the returned payload carries - nothing
    extra and nothing missing, so serial, parallel and staged builds
    are identical."""
    def _stage(s: str) -> None:
        if progress:
            progress(s)

    _stage("capturing change boundary")
    state = state or {"watermark": None, "viewed": [], "snoozed": []}
    boundary = boundary or tour_state.capture_boundary(repo_dir, now=now)
    rng = tour_state.feed_range(state, boundary)
    warnings: list[str] = []

    def annotate(e: dict) -> dict:
        e = dict(e)
        if "state" not in e:
            e["state"] = tour_state.entry_state(state, e["id"])
        e["anchor"] = _anchor(e.get("stop"))
        e["stop_title"] = _stop_title(e.get("stop"))
        e["section"] = _section(e)
        e["section_title"] = SECTIONS[e["section"]]
        return e

    def _emit(group: str, parts: dict) -> None:
        """Annotate + viewed-filter a raw group, then forward it to the
        caller's sink. Emitted entries already look like the final
        payload's, so a published snapshot never deals a viewed card.
        A broken sink never breaks the feed."""
        if emit is None:
            return
        out = dict(parts)
        for k in ("entries", "history"):
            if out.get(k) is not None:
                out[k] = [annotate(e) for e in out[k]]
        if out.get("entries") is not None:
            out["entries"] = [e for e in out["entries"]
                              if e["state"] != "viewed"]
        out["warnings"] = list(warnings)
        try:
            emit(group, out)
        except Exception:                                # noqa: BLE001
            pass

    kw = dict(repo_dir=repo_dir, runs_dir=runs_dir,
              requests_dir=requests_dir, manifests_dir=manifests_dir,
              polish=polish, progress=progress,
              flags_dir=Path(flags_dir) if flags_dir is not None
              else DEFAULT_FLAGS_DIR)

    wm_status = tour_state.watermark_status(state, repo_dir)
    base_commit = rng["base"].get("commit")
    if base_commit is not None and not wm_status.get("available", True):
        warnings.append(
            f"watermark commit {base_commit[:12]} no longer resolves - "
            "feed falls back to the unbounded range")
        rng = {"base": {"commit": None, "at": rng["base"].get("at")},
               "tip": rng["tip"]}

    n_workers = _COLLECT_WORKERS if workers is None else workers
    pool = _pool if _pool is not None else (
        ThreadPoolExecutor(max_workers=n_workers)
        if n_workers and n_workers > 1 else None)
    try:
        # One collection per distinct range per build: the live window
        # and every descriptor's stored window share this memo
        # (descriptors of one acknowledgment round usually record the
        # SAME range). Entries are copied before card assembly annotates
        # them - the shared results themselves are never mutated.
        collected: dict[tuple, list[dict]] = {}

        def collect(r: dict) -> list[dict]:
            key = _range_key(r)
            if key not in collected:
                collected[key] = _collect_window(
                    r, warnings=warnings, pool=pool, **kw)
            return collected[key]

        _emit("collect", {"boundary": boundary, "range": rng,
                          "sources": {}, "complete": False})
        live = _collect_window(rng, warnings=warnings, pool=pool,
                               emit=_emit, **kw)
        collected[_range_key(rng)] = live
        entries = [dict(e) for e in live]
        seen_ids = {e["id"] for e in entries}
        _stage("rebuilding viewed history")
        hist: dict[str, dict] = {}
        reinjects: list[dict] = []
        for desc in tour_state.entries(state):
            if desc["state"] == "viewed":
                h = _rematerialize(desc, collect)
                h["state"] = "viewed"
                for k in ("viewed_at", "snoozed_at", "unviewed_at"):
                    if desc.get(k):
                        h[k] = desc[k]
                # two descriptors can resolve to the same card (a folded
                # pipeline covers the standalone request id): keep one -
                # prefer a real card over a lost-window stub, and an
                # exact-id descriptor over a covered one
                if h["id"] not in hist or not h.get("unavailable") and (
                        hist[h["id"]].get("unavailable")
                        or desc["id"] == h["id"]):
                    hist[h["id"]] = h
            elif desc["id"] in seen_ids:
                continue                        # already live in the deck
            else:
                # snoozed deferral or an explicitly unviewed card whose
                # producing window is already behind the watermark - it
                # still belongs to the tour. Skip when a live folded card
                # already covers its id (no duplicate of the same event).
                e = _rematerialize(desc, collect)
                if e["id"] in seen_ids:
                    continue
                entries.append(e)
                reinjects.append(e)
                seen_ids.add(e["id"])
        history = list(hist.values())
        _emit("history", {"entries": reinjects, "history": history})
    finally:
        if pool is not None and _pool is None:
            pool.shutdown(wait=True)

    for e in entries + history:
        if "state" not in e:
            e["state"] = tour_state.entry_state(state, e["id"])
        e["anchor"] = _anchor(e.get("stop"))
        e["stop_title"] = _stop_title(e.get("stop"))
        e["section"] = _section(e)
        e["section_title"] = SECTIONS[e["section"]]
    # the deck is what you haven't seen: a card marked viewed leaves the
    # walkthrough immediately (it already re-materialized into history),
    # even while newer unmarked cards keep the watermark from advancing.
    # Viewed history stays reachable via `history` for explicit
    # reopening.
    entries = [e for e in entries if e["state"] != "viewed"]
    _stage("sorting the deck")
    entries.sort(key=lambda e: (_iso_ts(e.get("at")) or 0, e["id"]))
    history.sort(key=lambda e: (_iso_ts(e.get("viewed_at")) or 0,
                                e["id"]), reverse=True)
    return {"boundary": boundary, "range": rng, "entries": entries,
            "history": history,
            "unseen_count": sum(1 for e in entries
                                if e["state"] == "new"),
            "warnings": warnings}
