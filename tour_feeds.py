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
"""
import datetime as dt
import json
import os
import re
import subprocess
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


def _collect_window(rng: dict, *, repo_dir: Path, runs_dir: Path,
                    requests_dir: Path, manifests_dir: Path,
                    warnings: list[str], polish=None) -> list[dict]:
    """Raw feed entries for one {base,tip} window - no state annotation.
    Reused by build_feed (live window) and history (stored windows).

    ``polish`` is an optional callable ``(rng) -> validated card list``
    (tour_polish.Polisher.cards_for): when it returns cards for this
    range, its commit groupings replace the deterministic area grouping
    for the commits they claim; everything else - run/request/follow-up/
    curated cards, leftover commits - stays deterministic."""
    base, tip = rng.get("base") or {}, rng.get("tip") or {}
    commits, warn = collect_commits(repo_dir, base.get("commit"),
                                    tip.get("commit"))
    if warn:
        warnings.append(warn)

    runs_all = _iter_runs(runs_dir)
    reqs_all = _iter_requests(requests_dir)
    # association maps over ALL records, not just the window's
    key_by_dir = {d: _run_key(d, st, p) for d, p, st in runs_all}
    run_key_by_req = {st.get("request_id"): _run_key(d, st, p)
                      for d, p, st in runs_all if st.get("request_id")}

    def run_key_of(ref: str) -> str:
        # a curated 'runs' entry may name a dir, a request:<id> key, or
        # a bare request id - normalize to the dashboard key form
        if ref in key_by_dir:
            return key_by_dir[ref]
        rid = ref.split(":", 1)[-1]
        return run_key_by_req.get(rid, ref)

    win_runs = {}   # run key -> (dir_name, st)
    for d, p, st in runs_all:
        if _in_window(st.get("ended_at"), base.get("at"), tip.get("at")):
            win_runs[key_by_dir[d]] = (d, st)
    win_reqs = {}   # request id -> rec
    for rec in reqs_all:
        if _in_window(rec.get("created_at"), base.get("at"), tip.get("at")):
            win_reqs[rec["id"]] = rec

    # ---- curated records (claims resolve before cards form so a change
    # never appears both curated and raw)
    curated, warns = load_curated(manifests_dir)
    warnings.extend(warns)
    cards, claimed, augments, aug_shas = _resolve_curated(
        curated, commits, win_reqs, win_runs, run_key_of, repo_dir,
        warnings)
    fold_req = {}                       # request id -> rec
    fold_run = {}                       # run key -> rec
    for rec in augments.get("records", []):
        for rid in rec["requests"]:
            fold_req.setdefault(rid, rec)
        for rk in rec["runs"]:
            fold_run.setdefault(rk, rec)
    suppress_req = {rid for card in cards for rid in card["requests"]}
    suppress_run = {rk for card in cards for rk in card["runs"]}

    # ---- run cards (finished runs) + conductor-merge attribution
    run_by_dir = {d: k for k, (d, _st) in win_runs.items()}
    claim_to_run: dict[str, str] = {}     # commit sha -> run key
    for c in commits:
        if c["merge"] and c["merge"][0] in run_by_dir \
                and run_by_dir[c["merge"][0]] not in suppress_run:
            rk = run_by_dir[c["merge"][0]]
            claim_to_run[c["sha"]] = rk
            for chain_sha in _merged_chain(repo_dir, c["sha"]):
                claim_to_run.setdefault(chain_sha, rk)

    entries: list[dict] = []
    for key, (dname, st) in win_runs.items():
        if key in suppress_run:
            continue
        own = [c for c in commits if claim_to_run.get(c["sha"]) == key]
        counts = {}
        for rec in (st.get("steps") or {}).values():
            s = rec.get("status", "?")
            counts[s] = counts.get(s, 0) + 1
        bits = ", ".join(f"{n} {s}" for s, n in sorted(counts.items()))
        name = st.get("run") or dname
        title = f"Run finished: {name}"
        aug = fold_run.get(key)
        entries.append({
            "id": f"run:{dname}:{st.get('ended_at')}",
            "kind": "run", "stop": "runs",
            "title": (aug["title"] or title) if aug else title,
            "blurb": aug["blurb"] if aug else
                     _short((st.get("description") or "") +
                            (f"  Steps: {bits}." if bits else ""),
                            _BLURB_MAX) or title,
            "tag": _card_tag(own, aug["tag"] if aug else None)
                   if own else (aug["tag"] if aug and aug["tag"] else
                                _tag_for_subject(
                                    f"{name} {st.get('description') or ''}")),
            "commits": [{"sha": c["sha"], "subject": c["subject"]}
                        for c in own],
            "runs": [key],
            "requests": ([st["request_id"]] if st.get("request_id")
                         else []),
            "at": st.get("ended_at"), "range": rng})

    # ---- request + follow-up cards
    for rid, rec in win_reqs.items():
        if rid in suppress_req:
            continue
        launched = run_key_by_req.get(rid)
        label = rec.get("label") or rec.get("raw") or rid
        aug = fold_req.get(rid)
        if _is_followup_request(rec):
            src_key = (rec.get("source_run_key")
                       or (run_key_of(rec["source_run"])
                           if rec.get("source_run") else None))
            title = f"Follow-up proposal: {_short(label, 80)}"
            entries.append({
                "id": f"followup:{rid}", "kind": "followup",
                "stop": "requests",
                "title": (aug["title"] or title) if aug else title,
                "blurb": aug["blurb"] if aug else
                         _short(rec.get("description")
                                or f"Registered from {rec.get('source_run')}",
                                _BLURB_MAX),
                "tag": (aug["tag"] or "new") if aug else "new",
                "commits": [], "requests": [rid],
                "runs": ([src_key] if src_key else [])
                        + ([launched] if launched else []),
                "at": rec.get("created_at"), "range": rng})
        else:
            title = f"New proposal: {_short(label, 80)}"
            entries.append({
                "id": f"request:{rid}", "kind": "request",
                "stop": "requests",
                "title": (aug["title"] or title) if aug else title,
                "blurb": aug["blurb"] if aug else
                         (_short(rec.get("description") or rec.get("raw")
                                 or "", _BLURB_MAX)
                          or "A new request record was filed."),
                "tag": (aug["tag"] or "new") if aug else "new",
                "commits": [], "requests": [rid],
                "runs": [launched] if launched else [],
                "at": rec.get("created_at"), "range": rng})

    # ---- commit cards: validated LLM groupings first (they claim the
    # commits they name), then deterministic area grouping for the rest.
    # A polished card keeps the deterministic id scheme
    # (area:<stop>:<newest-sha>) so persisted viewed/snoozed descriptors
    # re-resolve identically; curated fold-ins still win over generated
    # text, and record links are re-filtered against this window's real
    # records.
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
            entries.append({
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
        entries.append({
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
        entries.append({
            "id": card["id"], "kind": "curated", "stop": card["stop"],
            "title": title, "blurb": rec["blurb"],
            "tag": _card_tag(cs, rec["tag"]),
            "commits": [{"sha": c["sha"], "subject": c["subject"]}
                        for c in cs],
            "runs": card["runs"], "requests": card["requests"],
            "at": (cs[0]["at"] if cs else rec["at"] or tip.get("at")),
            "range": rng})
    return entries


def _rematerialize(desc: dict, kwargs: dict) -> dict:
    """Rebuild the card a recorded descriptor pointed at, over the
    descriptor's own stored range - the only way a viewed entry stays
    retrievable once the live watermark advanced past its window."""
    rng = desc.get("range") or {}
    warnings: list[str] = []
    try:
        for e in _collect_window(rng, warnings=warnings, **kwargs):
            if e["id"] == desc["id"]:
                return e
    except Exception:
        pass
    return {"id": desc["id"], "kind": "unknown", "stop": None,
            "title": "Change (history)",
            "blurb": "This card's recorded window can no longer be "
                     "rebuilt (history rewritten or records gone).",
            "tag": "new", "commits": [], "runs": [], "requests": [],
            "at": (rng.get("tip") or {}).get("at"), "range": rng,
            "unavailable": True}


def build_feed(state: dict | None = None, boundary: dict | None = None,
               *, repo_dir: Path = DEFAULT_REPO,
               runs_dir: Path = DEFAULT_RUNS_DIR,
               requests_dir: Path = DEFAULT_REQUESTS_DIR,
               manifests_dir: Path = DEFAULT_MANIFESTS_DIR,
               now: float | None = None, polish=None) -> dict:
    """The full tour feed for the current watermark + boundary.

    ``entries`` = window cards plus re-injected snoozed/unviewed
    descriptors (each annotated with its persisted state); ``history`` =
    re-materialized viewed cards; ``unseen_count`` = entries still
    'new'. Pure reads only - nothing here mutates persisted state.

    ``polish`` is an optional ``(rng) -> cards|None`` hook (the tour
    enrichment cache, tour_polish.Polisher.cards_for): it is consulted
    per window - including the stored ranges history re-materializes -
    so polished cards reproduce the same ids a descriptor recorded."""
    state = state or {"watermark": None, "viewed": [], "snoozed": []}
    boundary = boundary or tour_state.capture_boundary(repo_dir, now=now)
    rng = tour_state.feed_range(state, boundary)
    warnings: list[str] = []
    kw = dict(repo_dir=repo_dir, runs_dir=runs_dir,
              requests_dir=requests_dir, manifests_dir=manifests_dir,
              polish=polish)

    wm_status = tour_state.watermark_status(state, repo_dir)
    base_commit = rng["base"].get("commit")
    if base_commit is not None and not wm_status.get("available", True):
        warnings.append(
            f"watermark commit {base_commit[:12]} no longer resolves - "
            "feed falls back to the unbounded range")
        rng = {"base": {"commit": None, "at": rng["base"].get("at")},
               "tip": rng["tip"]}

    entries = _collect_window(rng, warnings=warnings, **kw)
    seen_ids = {e["id"] for e in entries}
    history: list[dict] = []
    for desc in tour_state.entries(state):
        if desc["state"] == "viewed":
            h = _rematerialize(desc, kw)
            h["state"] = "viewed"
            for k in ("viewed_at", "snoozed_at", "unviewed_at"):
                if desc.get(k):
                    h[k] = desc[k]
            history.append(h)
        elif desc["id"] not in seen_ids:
            # snoozed deferral or an explicitly unviewed card whose
            # producing window is already behind the watermark - it
            # still belongs to the tour.
            e = _rematerialize(desc, kw)
            entries.append(e)
            seen_ids.add(desc["id"])

    for e in entries + history:
        if "state" not in e:
            e["state"] = tour_state.entry_state(state, e["id"])
        e["anchor"] = _anchor(e.get("stop"))
        e["stop_title"] = _stop_title(e.get("stop"))
    # the deck is what you haven't seen: a card marked viewed leaves the
    # walkthrough immediately (it already re-materialized into history),
    # even while newer unmarked cards keep the watermark from advancing.
    entries = [e for e in entries if e["state"] != "viewed"]
    entries.sort(key=lambda e: (_iso_ts(e.get("at")) or 0, e["id"]))
    history.sort(key=lambda e: (_iso_ts(e.get("viewed_at")) or 0,
                                e["id"]), reverse=True)
    return {"boundary": boundary, "range": rng, "entries": entries,
            "history": history,
            "unseen_count": sum(1 for e in entries
                                if e["state"] == "new"),
            "warnings": warnings}
