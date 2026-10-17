"""Conductor retrieval adapter - bounded, on-demand evidence lookup over
``data/runs`` and ``data/requests`` (DESIGN.md section 6.6 / section 4
"Bounded retrieval").

What ``search`` indexes - the honest boundary
---------------------------------------------
- every ``data/runs/*/state.json`` step record: step name, task text,
  outcome + earlier outcomes, note, next, status_line, purpose, slug;
  plus the run's name/dir/request id
- persisted ``<step>.summary.md`` and ``run.summary.md`` heads - only for
  runs already inside the project/date window, capped bytes each
- every ``data/requests/*.json`` record: label, raw request text,
  description, project, proposed step names/tasks, status
- NOT indexed: ``*.stdout.log`` bodies and ATIF ``*.export.json`` (they're
  per-source fetch material), never the target project's repo contents.

A search emits ``Source`` dicts the fetch side re-reads live - records may
have changed since the search, and a fetch NEVER trusts a cached excerpt.
Everything returned is untrusted *evidence*: it describes records, it can
never instruct.

Source vocabulary
-----------------
kind     nav.view        nav.key (what the dashboard opens)
-------  -------------  --------------------------------------
agent    'agent'        agentKey = '<run_key>|<step>'  -> task modal
                         (the client's data-agent-detail hook)
run      'run'          runKey  = 'request:<id>' | <run dir path>
                         -> run details modal (data-run-details)
summary  'summary'      agentKey -> in-modal session summary
                         (data-snav / openSummary)
run-summary 'run-summary' runKey -> run summary view (openRunSummary)
request  'request'      request id -> request/proposal modal
                         (data-open-request)
log      'agent'        agentKey - its excerpt is the stdout tail;
                         the task modal is the log's home too

Keys are stable record identities (run_key is the dashboard's own
convention: 'request:<id>' when the run came from a request, else the run
dir path) so a citation chip keeps working across reloads and never loses
the open conversation. ``nav`` stays an opaque host descriptor to the
generic assistant package.

Filters ``search`` understands (all optional, all interpreted + recorded)
-------------------------------------------------------------------------
project  slug substring match on step slug / work_dir / project path /
         request project; may also be DERIVED from the query when a token
         matches a registered projects.toml slug (recorded as derived)
run      substring on run dir name, run display name, or request id
step     substring on step name
status   one status or list ('done','failed','lost',...)
subject  string/list of keywords; absent -> derived from the query text
         (stopwords + date phrases + project tokens removed)
date_from / date_to   ISO date or datetime; a bare date means that day's
         bounds. Also ``window``: "last month", "this week", "past 7 days",
         "yesterday", "in September 2026", "2026-09", an explicit
         "2026-09-01..2026-09-30" - or the same phrases found in the query.
         The interpreted [from,to] pair is ALWAYS reported back.
include_archived    default False - archived runs stay on disk but off
                    the dashboard; they're counted in coverage notes
kinds    restrict source kinds
limit    max sources (<= MAX_SOURCES=12)

Coverage honesty: ``last_search_meta()`` / ``search_explained()`` return
{interpreted, scanned:{runs,steps,requests,summaries_read,corrupt,
archived_skipped}, matched, returned, truncated, notes[]} - the answer can
say "0 matches inside 2026-09-01..2026-09-30 (6 October runs scanned; 4
archived excluded)" instead of implying the search saw nothing.
"""
import datetime as dt
import json
import re
import time
from pathlib import Path

import conductor
from assistant.retrieval import (RetrievalAdapter, MAX_SOURCES,
                                 MAX_TOTAL_BYTES, MAX_EXCERPT_BYTES)

TASK_CHARS = 1536            # _summary_prompt's task budget, shared shape
STEP_KV_OUTCOME = 320
SEARCH_HEAD_BYTES = 24576    # summary-head bytes read per candidate run
LOG_TAIL_LINES = 15          # stdout_tail's modal convention
LOG_TAIL_BYTES = 4096

_KIND_NAV = {"agent": "agent", "run": "run", "summary": "summary",
             "run-summary": "run-summary", "request": "request",
             "log": "agent"}
_VALID_KINDS = tuple(_KIND_NAV)

_MONTHS = {m: i for i, m in enumerate(
    ("january february march april may june july august september october "
     "november december").split(), 1)}

_STOPWORDS = set("""
a an the and or of for to in on at by with about into from as is are was
were be been being do does did have has had i me my we our you your it its
this that these those there here what which who whom whose when where why
how all any both each few more most other some such no nor not only own
same so than too very can will just should now show list tell give find
get got did tasks task runs run steps step work job jobs changes
modification modifications modified change changed fix fixed make made
relate related relating involving involved exist exists did do done
""".split())

_DATE_RANGE_RE = re.compile(
    r"(\d{4}-\d{2}-\d{2})\s*(?:\.\.|--|to|until|through)\s*"
    r"(\d{4}-\d{2}-\d{2})")
_MONTH_NAME_RE = re.compile(
    r"(?:\bin\s+)?(january|february|march|april|may|june|july|august|"
    r"september|october|november|december)(?:\s+(\d{4}))?", re.I)
_MONTH_NUM_RE = re.compile(r"\b(\d{4})-(0[1-9]|1[0-2])\b")
_REL_RE = re.compile(
    r"\b(?:last|past|previous)\s+(\d+)\s+(hour|day|week|month)s?\b", re.I)
_NAMED_RE = re.compile(
    r"\b(last|previous|this)\s+(month|week)\b|\b(today|yesterday|"
    r"last\s*night)\b", re.I)
# A full ISO date (optionally after 'on') means that single day; it must
# be tried BEFORE the year-month pattern or '2026-09-15' truncates to a
# whole month.
_ON_DATE_RE = re.compile(r"(?:\bon\s+)?(\d{4}-\d{2}-\d{2})(?![-\d])")


def _iso(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts).isoformat(timespec="seconds")


def _parse_dt(s: str | None) -> float | None:
    """ISO string -> epoch; a bare 'YYYY-MM-DD' is that day's START.
    Records keep naive local stamps, so naive parsing is correct here."""
    if not s:
        return None
    try:
        return dt.datetime.fromisoformat(str(s)[:26]).timestamp()
    except (ValueError, TypeError):
        return None


def _end_of_day(ts: float) -> float:
    d = dt.datetime.fromtimestamp(ts)
    return d.replace(hour=23, minute=59, second=59).timestamp()


def _tokens(text: str) -> list[str]:
    return [t for t in re.split(r"[^\w.-]+", (text or "").lower())
            if len(t) >= 3 and t not in _STOPWORDS]


def interpret_window(text: str | None, now: float) -> tuple | None:
    """Free-text date phrase -> (from_ts, to_ts, normalized_label) or None.

    Only the FIRST phrase wins (the rest of the text stays subject terms);
    anything unrecognized returns None - never a guessed window."""
    if not text:
        return None
    t = str(text).strip().lower()
    m = _DATE_RANGE_RE.search(t)
    if m:
        a, b = _parse_dt(m.group(1)), _parse_dt(m.group(2))
        if a is not None and b is not None:
            if b < a:
                a, b = b, a
            return a, _end_of_day(b), f"{m.group(1)}..{m.group(2)}"
    m = _REL_RE.search(t)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        span = {"hour": 3600, "day": 86400,
                "week": 7 * 86400}[unit] if unit != "month" else None
        if unit == "month":
            base = dt.datetime.fromtimestamp(now)
            base = base.replace(day=1)
            for _ in range(n):
                base = (base - dt.timedelta(days=1)).replace(day=1)
            return base.timestamp(), now, f"past {n} month(s)"
        return now - n * span, now, f"past {n} {unit}{'s' if n != 1 else ''}"
    m = _NAMED_RE.search(t)
    if m:
        which = (m.group(1) or m.group(3) or "").replace(" ", "")
        unit = m.group(2)
        d = dt.datetime.fromtimestamp(now)
        if which == "today":
            a = d.replace(hour=0, minute=0, second=0, microsecond=0)
            return a.timestamp(), now, "today"
        if which == "yesterday":
            a = (d - dt.timedelta(days=1)).replace(
                hour=0, minute=0, second=0, microsecond=0)
            return a.timestamp(), _end_of_day(a.timestamp()), "yesterday"
        if which == "lastnight":
            a = (d - dt.timedelta(days=1)).replace(hour=18, minute=0,
                                                   second=0)
            return a.timestamp(), a.replace(hour=23,
                                            minute=59).timestamp(), \
                "last night"
        if unit == "month":
            first = d.replace(day=1, hour=0, minute=0, second=0,
                              microsecond=0)
            if which in ("last", "previous"):
                prev_first = (first - dt.timedelta(days=1)).replace(day=1)
                return prev_first.timestamp(), first.timestamp() - 1, \
                    "last calendar month"
            return first.timestamp(), now, "this calendar month"
        if unit == "week":
            monday = (d - dt.timedelta(days=d.weekday())).replace(
                hour=0, minute=0, second=0, microsecond=0)
            if which in ("last", "previous"):
                prev_mon = monday - dt.timedelta(days=7)
                return prev_mon.timestamp(), monday.timestamp() - 1, \
                    "last calendar week (Mon-Sun)"
            return monday.timestamp(), now, "this calendar week (Mon..)"
    m = _ON_DATE_RE.search(t)
    if m:
        a = _parse_dt(m.group(1))
        if a is not None:
            return a, _end_of_day(a), m.group(1)
    m = _MONTH_NAME_RE.search(t)
    if m:
        mon = _MONTHS[m.group(1).lower()]
        year = int(m.group(2)) if m.group(2) else None
        d = dt.datetime.fromtimestamp(now)
        if year is None:
            year = d.year if mon <= d.month else d.year - 1
        a = dt.datetime(year, mon, 1)
        nxt = dt.datetime(year + (mon == 12), mon % 12 + 1, 1)
        return a.timestamp(), nxt.timestamp() - 1, \
            f"{m.group(1).lower()} {year}"
    m = _MONTH_NUM_RE.search(t)
    if m:
        year, mon = int(m.group(1)), int(m.group(2))
        a = dt.datetime(year, mon, 1)
        nxt = dt.datetime(year + (mon == 12), mon % 12 + 1, 1)
        return a.timestamp(), nxt.timestamp() - 1, f"{year}-{mon:02d}"
    return None


_CRED_RE = re.compile(
    r"(?i)(api[_-]?key|access[_-]?token|auth[_-]?token|secret|password|"
    r"passwd|bearer|private[_-]?key|client[_-]?secret)"
    r"([\"'\s:=]+)([^\s\"',}]{4,})")


def _scrub(text: str) -> str:
    """Best-effort credential masking for free-text excerpts (logs/report
    bodies). Structured fields are whitelisted at the record level - this
    is the second line, for prose that could smuggle a key=value past the
    field filter."""
    return _CRED_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}***", text)


def _clip(text: str | None, cap: int) -> str:
    if not text:
        return ""
    text = str(text)
    return text if len(text) <= cap else \
        text[:cap] + f"\n[...{len(text) - cap} chars truncated...]"


_HEADING_RE = re.compile(r"^#{1,6}\s|^\*\*[^\n*]{2,60}\*\*:?\s*$", re.M)


def _section_window(text: str, keywords: list[str],
                    budget: int) -> tuple[str, bool]:
    """Slice `text` around the first keyword hit, preferring whole
    sections: the enclosing '# '/bold-lead block containing the match,
    plus the heading above it for context, extended forward while it fits
    budget. Returns (excerpt, truncated).

    A 'verification section' in a report is often a `**Verification:**`
    lead paragraph rather than a heading - both are section starts here.
    """
    if not text:
        return "", False
    pos = -1
    for kw in keywords:
        pos = text.lower().find(kw.lower())
        if pos >= 0:
            break
    if pos < 0:
        return _clip(text, budget), len(text) > budget
    heads = sorted({m.start() for m in _HEADING_RE.finditer(text)}
                   | {len(text)})
    start = max((h for h in heads if h <= pos), default=0)
    nxt = [h for h in heads if h > pos]
    end = nxt[0] if nxt else len(text)
    prevs = [h for h in heads if h < start]
    if prevs and end - prevs[-1] <= budget:
        start = prevs[-1]
    lo, hi = start, end
    for h in heads:                     # extend through following sections
        if h > hi and h - lo <= budget:
            hi = h
    if hi < len(text) and len(text) - lo <= budget:
        hi = len(text)
    if hi - lo > budget:
        # the matched section alone overflows - window centered on it
        lo = max(start, pos - budget // 2)
        hi = min(len(text), lo + budget)
    return ("...\n" if lo > 0 else "") + text[lo:hi].strip("\n") + \
        ("\n..." if hi < len(text) else ""), (hi < len(text) or lo > 0)


class ConductorRetrieval(RetrievalAdapter):
    """RetrievalAdapter over a runs dir + a requests dir.

    ``projects`` is the registry map (or a callable returning it) used for
    slug->path matching; ``now`` the clock seam tests inject.
    """

    def __init__(self, *, runs_dir, requests_dir=None, projects=None,
                 now=None, summary_head_bytes=SEARCH_HEAD_BYTES):
        self._runs = Path(runs_dir)
        self._requests = (Path(requests_dir) if requests_dir
                          else self._runs.parent / "requests")
        self._projects = projects
        self._now = now or time.time
        self._summary_head = summary_head_bytes
        self._last = {"interpreted": {}, "scanned": {}, "matched": 0,
                      "returned": 0, "truncated": False, "notes": []}

    # --------------------------------------------------------- contract
    def search(self, query, filters=None) -> list:
        """Structured filter first, keyword score second, excerpts never -
        returns Source stubs (no excerpt until fetch())."""
        query = str(query or "")
        filters = dict(filters or {})
        meta = self._interpret(query, filters)
        out = self._scan(meta)
        self._last["matched"] = len(out)
        try:
            limit = min(int(filters.get("limit") or MAX_SOURCES),
                        MAX_SOURCES)
        except (TypeError, ValueError):
            limit = MAX_SOURCES
        limit = max(1, limit)
        out.sort(key=lambda s: s.get("at") or "", reverse=True)
        out.sort(key=lambda s: -s["_score"])   # stable: recency ties break
        out = out[:limit]
        self._last["returned"] = len(out)
        self._last["truncated"] = self._last["matched"] > len(out)
        for s in out:
            s.pop("_score", None)
        return out

    def fetch(self, source, budget: int = MAX_EXCERPT_BYTES) -> dict:
        """Attach a bounded `excerpt` re-read from the live record."""
        budget = max(256, min(int(budget or MAX_EXCERPT_BYTES),
                              MAX_EXCERPT_BYTES))
        src = dict(source)
        try:
            text = self._excerpt_for(src, budget)
        except Exception as exc:            # noqa: BLE001 - honest empty
            src.update({"excerpt": "",
                        "error": f"{type(exc).__name__}: {exc}",
                        "bytes": 0, "truncated": False})
            return src
        body = text.encode("utf-8")[:budget]
        src["excerpt"] = body.decode("utf-8", "ignore")
        src["bytes"] = len(body)
        src["truncated"] = len(text.encode("utf-8")) > len(body)
        return src

    def fetch_many(self, sources, total: int = MAX_TOTAL_BYTES) -> list:
        """fetch() the whole set inside the shared budget, in rank order.
        Sources that don't fit are kept (chips still render) with
        ``omitted: 'budget'`` instead of a partial excerpt."""
        remaining = max(0, int(total or MAX_TOTAL_BYTES))
        out = []
        for src in sources or []:
            cap = min(remaining, MAX_EXCERPT_BYTES)
            if cap < 256:
                out.append({**src, "excerpt": "", "bytes": 0,
                            "omitted": "budget"})
                continue
            got = self.fetch(src, budget=cap)
            remaining -= got.get("bytes") or 0
            out.append(got)
        return out

    def last_search_meta(self) -> dict:
        return dict(self._last)

    def search_explained(self, query, filters=None) -> dict:
        """search() + its interpretation/coverage in one return value -
        the shape the runtime hands the answer composer."""
        sources = self.search(query, filters)
        return {"sources": sources, **self.last_search_meta()}

    # ------------------------------------------------------ interpreter
    def _interpret(self, query: str, filters: dict) -> dict:
        now = self._now()
        interp = {"query": query}
        notes = []
        # ---- date window: explicit filters beat a phrase in the query
        d_from = _parse_dt(filters.get("date_from"))
        d_to = _parse_dt(filters.get("date_to"))
        w_src = None
        if d_to is not None and \
                len(str(filters["date_to"]).strip()) == 10:
            d_to = _end_of_day(d_to)          # bare date = whole day
        if d_from is None and d_to is None:
            win = interpret_window(filters.get("window"), now) or \
                interpret_window(query, now)
            if win:
                d_from, d_to, w_src = win
        interp["date_from"] = _iso(d_from) if d_from is not None else None
        interp["date_to"] = _iso(d_to) if d_to is not None else None
        if w_src:
            interp["window_source"] = w_src
        # ---- project: explicit, else derived from a registry slug in text
        proj = filters.get("project")
        projs = self._projects_map()
        derived = False
        if not proj:
            q_toks = set(_tokens(query))
            for slug in projs:
                slug_l = str(slug).lower()
                if slug_l in query.lower() or slug_l in q_toks or \
                        all(t in q_toks for t in slug_l.split("-")):
                    proj, derived = slug_l, True
                    break
        interp["project"] = proj
        if derived:
            notes.append(f"project '{proj}' derived from query text")
        interp["run"] = filters.get("run")
        interp["step"] = filters.get("step")
        st = filters.get("status")
        interp["status"] = ([st] if isinstance(st, str)
                            else list(st) if st else None)
        interp["include_archived"] = bool(filters.get("include_archived"))
        kinds = filters.get("kinds")
        interp["kinds"] = ([k for k in kinds if k in _VALID_KINDS]
                           if kinds else None)
        # ---- subject keywords: explicit subject beats query tokens
        subj = filters.get("subject") or filters.get("keywords")
        if isinstance(subj, str):
            subj = [w for w in _tokens(subj)]
        elif subj:
            subj = [str(w).lower() for w in subj]
        else:
            drop = set()
            if interp["project"]:
                drop.update(str(interp["project"]).lower().split("-"))
            m = _DATE_RANGE_RE.search(query.lower()) or \
                _REL_RE.search(query.lower()) or \
                _NAMED_RE.search(query.lower()) or \
                _MONTH_NAME_RE.search(query.lower()) or \
                _MONTH_NUM_RE.search(query.lower())
            if m:
                drop.update(_tokens(m.group(0)))
            subj = [t for t in _tokens(query) if t not in drop]
        interp["subject"] = subj
        if not subj and not any(interp[k] for k in
                                ("project", "run", "step", "date_from",
                                 "status")):
            notes.append("no subject or filters - returning the most "
                         "recent records only")
        self._last = {"interpreted": interp,
                      "scanned": {"runs": 0, "steps": 0, "requests": 0,
                                  "summaries_read": 0, "corrupt": 0,
                                  "archived_skipped": 0},
                      "matched": 0, "returned": 0, "truncated": False,
                      "notes": notes}
        return interp

    def _projects_map(self) -> dict:
        p = self._projects() if callable(self._projects) else self._projects
        if p is None:
            try:
                p = conductor.load_projects()
            except Exception:              # noqa: BLE001
                p = {}
        return p or {}

    # ------------------------------------------------------------ scan
    def _scan(self, interp: dict) -> list[dict]:
        out: list[dict] = []
        kinds = interp["kinds"] or _VALID_KINDS
        scanned = self._last["scanned"]
        proj_path = None
        if interp["project"]:
            ent = self._projects_map().get(interp["project"]) or {}
            proj_path = str(ent.get("path") or "").lower() or None
        for sj in sorted(self._runs.glob("*/state.json"), reverse=True):
            try:
                st = json.loads(sj.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                scanned["corrupt"] += 1
                continue
            scanned["runs"] += 1
            if st.get("archived") and not interp["include_archived"]:
                scanned["archived_skipped"] += 1
                continue
            d = sj.parent
            run_key = (f"request:{st['request_id']}"
                       if st.get("request_id") else str(d))
            run_name = st.get("run") or d.name
            if interp["run"] and interp["run"].lower() not in (
                    f"{d.name} {run_name} {st.get('request_id') or ''}"
                    .lower()):
                continue
            run_ok = self._in_window(
                interp, [st.get("started_at"), st.get("ended_at")])
            steps = st.get("steps") or {}
            run_hits = self._count_hits(interp, " ".join(
                str(st.get(k) or "") for k in
                ("run", "description", "request_id")))
            step_out = []
            for name, rec in steps.items():
                scanned["steps"] += 1
                if interp["step"] and interp["step"].lower() \
                        not in name.lower():
                    continue
                if interp["project"] and not self._match_project(
                        rec, interp["project"], proj_path):
                    continue
                if not self._in_window(
                        interp, [rec.get("started_at"),
                                 rec.get("first_started_at"),
                                 rec.get("ended_at")]):
                    continue
                if interp["status"] and \
                        rec.get("status") not in interp["status"]:
                    continue
                hay = self._step_haystack(name, rec)
                hits = self._count_hits(interp, hay)
                matched = []
                if hits:
                    matched.append(f"{hits}x subject in record fields")
                summary_hit = 0
                if interp["subject"] and "summary" in kinds:
                    summary_hit, was_read = self._summary_hits(
                        d / f"{name}.summary.md", interp["subject"])
                    if was_read:
                        scanned["summaries_read"] += 1
                    if summary_hit:
                        matched.append(f"{summary_hit}x subject in "
                                       "session summary")
                # a subject-bearing query requires a hit somewhere; a
                # structured-only query lists the whole filtered set
                if not interp["subject"] or matched:
                    akey = f"{run_key}|{name}"
                    step_out.append(self._mk(
                        "agent", akey, f"{run_name} / {name}", {
                            "view": "agent", "key": akey},
                        run=run_name, run_dir=d.name, step=name,
                        at=rec.get("first_started_at")
                        or rec.get("started_at"),
                        matched=matched or ["structured filters only"],
                        score=2 * hits + (1 if interp["step"] else 0)))
                    if summary_hit:
                        step_out.append(self._mk(
                            "summary", akey + "#summary",
                            f"{run_name} / {name} - session summary",
                            {"view": "summary", "key": akey},
                            run=run_name, run_dir=d.name, step=name,
                            at=rec.get("ended_at") or
                            rec.get("started_at"),
                            matched=[f"{summary_hit}x subject in "
                                     "session summary"],
                            score=2 * summary_hit + 1,
                            focus=interp["subject"]))
            out.extend(step_out)
            if not steps:
                continue
            if "run" in kinds and run_ok and (
                    run_hits or step_out
                    or (interp["run"] and not interp["subject"])):
                out.append(self._mk(
                    "run", run_key, f"run {run_name}",
                    {"view": "run", "key": run_key},
                    run=run_name, run_dir=d.name,
                    at=st.get("started_at"),
                    matched=([f"{run_hits}x subject in run fields"]
                             if run_hits else ["structured filters only"]),
                    score=run_hits + (1 if interp["run"] else 0)))
            if run_ok and interp["subject"] and "run-summary" in kinds:
                rsum, was_read = self._summary_hits(d / "run.summary.md",
                                                    interp["subject"])
                if was_read:
                    scanned["summaries_read"] += 1
                if rsum:
                    out.append(self._mk(
                        "run-summary", run_key + "#run-summary",
                        f"run {run_name} - run summary",
                        {"view": "run-summary", "key": run_key},
                        run=run_name, run_dir=d.name,
                        at=st.get("ended_at") or st.get("started_at"),
                        matched=[f"{rsum}x subject in run summary"],
                        score=2 * rsum, focus=interp["subject"]))
        if "request" in kinds:
            out.extend(self._scan_requests(interp))
        if scanned["archived_skipped"]:
            self._last["notes"].append(
                f"{scanned['archived_skipped']} archived run(s) excluded "
                "(include_archived=false)")
        return out

    def _scan_requests(self, interp) -> list[dict]:
        out = []
        scanned = self._last["scanned"]
        if not self._requests.is_dir():
            return out
        for rf in sorted(self._requests.glob("*.json")):
            if not re.fullmatch(r"\d{8}-\d{6}-[0-9a-f]{8}", rf.stem):
                continue
            try:
                rec = json.loads(rf.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                scanned["corrupt"] += 1
                continue
            scanned["requests"] += 1
            rid = str(rec.get("id") or rf.stem)
            if interp["project"] and interp["project"].lower() not in (
                    str(rec.get("project") or "").lower()):
                continue
            if interp["run"] and interp["run"].lower() not in (
                    f"{rid} {rec.get('label') or ''} "
                    f"{rec.get('run') or ''}".lower()):
                continue
            if not self._in_window(
                    interp, [rec.get("created_at"), rec.get("updated_at")]):
                continue
            hay = " ".join(str(rec.get(k) or "") for k in
                           ("label", "raw", "description", "project",
                            "status")) + " " + " ".join(
                f"{s.get('step', '')} {s.get('task', '')}"
                for s in (rec.get("proposed_steps") or [])
                if isinstance(s, dict))
            hits = self._count_hits(interp, hay)
            if interp["subject"] and not hits:
                continue
            if not interp["subject"] and not any(
                    interp[k] for k in ("project", "date_from", "run")):
                continue
            label = rec.get("label") or (rec.get("raw") or "")[:60]
            out.append(self._mk(
                "request", rid,
                f"request {rid}: {label}",
                {"view": "request", "key": rid},
                request_id=rid, at=rec.get("updated_at")
                or rec.get("created_at"),
                matched=([f"{hits}x subject in request fields"]
                         if hits else ["structured filters only"]),
                score=hits))
        return out

    # --------------------------------------------------------- matching
    def _match_project(self, rec: dict, proj: str, proj_path) -> bool:
        p = proj.lower()
        if p in str(rec.get("slug") or "").lower():
            return True
        if proj_path:
            for k in ("work_dir", "project"):
                v = str(rec.get(k) or "").lower().replace("/", "\\")
                if proj_path.replace("/", "\\") in v or \
                        v and v in proj_path.replace("/", "\\"):
                    return True
        return False

    def _in_window(self, interp, stamps) -> bool:
        d_from = _parse_dt(interp.get("date_from"))
        d_to = _parse_dt(interp.get("date_to"))
        if d_from is None and d_to is None:
            return True
        ts = [t for t in (_parse_dt(s) for s in stamps) if t is not None]
        if not ts:
            return False
        lo = min(ts)
        hi = max(ts)
        if d_from is not None and hi < d_from:
            return False
        if d_to is not None and lo > d_to:
            return False
        return True

    @staticmethod
    def _step_haystack(name: str, rec: dict) -> str:
        parts = [name,
                 rec.get("task"), rec.get("outcome"), rec.get("note"),
                 rec.get("next"), rec.get("status_line"),
                 rec.get("purpose"), rec.get("slug"), rec.get("status"),
                 rec.get("branch")]
        for e in rec.get("outcomes") or []:
            if isinstance(e, dict):
                parts.append(e.get("outcome"))
        return " ".join(str(p or "") for p in parts)

    @staticmethod
    def _count_hits(interp: dict, haystack: str) -> int:
        subj = interp["subject"]
        if not subj:
            return 0
        low = haystack.lower()
        return sum(1 for kw in subj if kw.lower() in low)

    def _summary_hits(self, path: Path, keywords) -> tuple:
        """(subject hits, file_was_read) - a bounded head-read for subject
        matching; absent files just don't count, content is never loaded
        beyond SEARCH_HEAD_BYTES."""
        try:
            if not path.is_file():
                return 0, False
            data = path.read_bytes()[: self._summary_head]
        except OSError:
            return 0, False
        low = data.decode("utf-8", "replace").lower()
        return sum(1 for kw in keywords if kw.lower() in low), True

    @staticmethod
    def _mk(kind, key, label, nav, *, run=None, run_dir=None, step=None,
            request_id=None, at=None, matched=None, score=0,
            focus=None) -> dict:
        src = {"kind": kind, "key": key, "label": label, "nav": nav,
               "at": at, "matched": matched or []}
        if run:
            src["run"] = run
        if run_dir:
            src["run_dir"] = run_dir
        if step:
            src["step"] = step
        if request_id:
            src["request_id"] = request_id
        if focus:
            src["focus"] = focus
        src["_score"] = score
        return src

    # ----------------------------------------------------------- fetch
    def _load_run(self, run_dir_name: str):
        name = Path(str(run_dir_name or "")).name
        if not name or not conductor.RUN_DIR_RE.fullmatch(name):
            return None
        d = self._runs / name
        try:
            return d, json.loads(
                (d / "state.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def _excerpt_for(self, src: dict, budget: int) -> str:
        kind = src.get("kind")
        if kind in ("agent", "log", "summary"):
            found = self._load_run(src.get("run_dir") or "")
            if found is None:
                return ("(record unavailable: run dir "
                        f"{src.get('run_dir') or '?'} not found)")
            d, st = found
            rec = (st.get("steps") or {}).get(src.get("step") or "")
            if rec is None:
                return (f"(record unavailable: step "
                        f"{src.get('step') or '?'} not in "
                        f"{d.name})")
            if kind == "summary":
                return self._summary_excerpt(d, st, src, rec, budget)
            if kind == "log":
                tail = conductor.stdout_tail(rec.get("stdout_log"),
                                             LOG_TAIL_LINES)
                return _scrub(tail or "(no stdout log retained)")
            return _scrub(self._agent_excerpt(st, d.name, src, rec, tail_budget=budget))
        if kind == "run":
            found = self._load_run(src.get("run_dir") or "")
            if found is None:
                return "(record unavailable: run dir not found)"
            d, st = found
            return _scrub(self._run_excerpt(st, d.name, budget))
        if kind == "run-summary":
            found = self._load_run(src.get("run_dir") or "")
            if found is None:
                return "(record unavailable: run dir not found)"
            d, st = found
            p = d / "run.summary.md"
            try:
                if p.is_file():
                    text = p.read_text(encoding="utf-8", errors="replace")
                else:
                    text = conductor.run_report_md(st)
            except OSError:
                text = conductor.run_report_md(st)
            ex, _ = _section_window(text, src.get("focus") or [], budget)
            return _scrub(ex)
        if kind == "request":
            try:
                rec = conductor._read_request(str(src.get("request_id")
                                                  or ""),
                                            self._requests)
            except Exception:              # noqa: BLE001
                rec = None
            if not isinstance(rec, dict):
                return "(record unavailable: request not found)"
            return _scrub(self._request_excerpt(rec, budget))
        return f"(unsupported source kind {kind!r})"

    def _agent_excerpt(self, st, run_dir_name, src, rec, tail_budget):
        name = src.get("step") or rec.get("name") or "?"
        eff = conductor.effective_statuses(st)
        lines = [
            f"### {st.get('run') or run_dir_name} / {name} (task record)",
            "",
            f"- status: {eff.get(name, rec.get('status'))} "
            f"(recorded: {rec.get('status')}) · "
            f"verify_ok: {rec.get('verify_ok')} · "
            f"exit: {rec.get('exit_code', '-')}",
            f"- started: {rec.get('first_started_at') or rec.get('started_at') or '-'} · "
            f"ended: {rec.get('ended_at') or '-'}",
            f"- model: {rec.get('model_actual') or rec.get('model') or '-'} "
            f"effort={rec.get('effort') or '-'} mode={rec.get('mode') or '-'} "
            f"attempts={rec.get('attempts', 1)}/{rec.get('max_attempts', 1)}",
            f"- session: {rec.get('session_id') or '-'} · "
            f"slug: {rec.get('slug') or '-'} · "
            f"merged: {bool(rec.get('merged'))}",
        ]
        t = rec.get("tokens") or {}
        if t:
            lines.append(
                f"- tokens: in={t.get('prompt', 0)} out={t.get('completion', 0)} "
                f"cached={t.get('cached', 0)} turns={t.get('llm_steps', 0)}")
        task = conductor.task_display(
            conductor.task_label(rec, full=True))
        if task:
            lines += ["", "task:", _clip(task, TASK_CHARS)]
        for k in ("outcome", "note", "next"):
            if rec.get(k):
                lines += ["", f"{k}: {_clip(str(rec[k]), STEP_KV_OUTCOME)}"]
        tail_room = max(0, tail_budget - sum(len(l) + 1 for l in lines) - 40)
        if tail_room > 400:
            tail = conductor.stdout_tail(rec.get("stdout_log"),
                                         LOG_TAIL_LINES)
            if tail:
                lines += ["", "log tail:", _clip(tail, min(tail_room,
                                                         LOG_TAIL_BYTES))]
        return "\n".join(lines)

    def _run_excerpt(self, st, run_dir_name, budget):
        eff = conductor.effective_statuses(st)
        steps = st.get("steps") or {}
        counts = {s: sum(1 for v in eff.values() if v == s)
                  for s in set(eff.values())}
        lines = [
            f"### run {st.get('run') or run_dir_name}",
            "",
            f"- steps: {', '.join(f'{n} {k}' for k, n in sorted(counts.items()))}",
            f"- started: {st.get('started_at') or '-'} · "
            f"ended: {st.get('ended_at') or '-'} · "
            f"elapsed_s: {conductor.run_elapsed_s(st)}",
            f"- archived: {bool(st.get('archived'))} · "
            f"request_id: {st.get('request_id') or '-'}",
        ]
        if st.get("description"):
            lines.append(f"- description: {_clip(st['description'], 240)}")
        lines.append("")
        room = budget - sum(len(l) + 1 for l in lines)
        for n, rec in steps.items():
            row = (f"- {n}: {eff.get(n, rec.get('status'))} "
                   f"{rec.get('slug') or ''} "
                   f"{_clip(rec.get('outcome') or '', 120)}").rstrip()
            if room - len(row) < 0:
                lines.append(f"- ...{len(steps) - len(lines)} more steps")
                break
            lines.append(row)
            room -= len(row) + 1
        return "\n".join(lines)

    def _request_excerpt(self, rec, budget):
        steps = rec.get("proposed_steps") or []
        lines = [
            f"### request {rec.get('id')}",
            "",
            f"- label: {rec.get('label') or '-'} · "
            f"status: {rec.get('status') or '-'} · "
            f"project: {rec.get('project') or '-'}",
            f"- model: {rec.get('model') or '-'} effort="
            f"{rec.get('effort') or '-'} · "
            f"created: {rec.get('created_at') or '-'} · "
            f"updated: {rec.get('updated_at') or '-'}",
        ]
        if rec.get("description"):
            lines.append(f"- description: {_clip(rec['description'], 240)}")
        if rec.get("raw"):
            lines += ["", "request text:", _clip(rec["raw"], TASK_CHARS)]
        if steps:
            lines += ["", "proposed steps:"]
            for s in steps[:24]:
                if isinstance(s, dict):
                    lines.append(
                        f"- {s.get('step') or '?'} "
                        f"({s.get('slug') or '-'}): "
                        f"{_clip(s.get('task') or '', 120)}")
        return "\n".join(lines)

    def _summary_excerpt(self, d, st, src, rec, budget):
        step = src.get("step") or ""
        p = d / f"{step}.summary.md"
        text = ""
        try:
            if p.is_file():
                text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        if not text:
            try:
                text = conductor.step_summary_md(
                    {**rec, "name": rec.get("name") or step}, st,
                    st.get("run") or d.name)
            except Exception:              # noqa: BLE001
                text = ""
        if not text:
            return "(no session summary retained for this step)"
        ex, _ = _section_window(text, src.get("focus") or [], budget)
        return _scrub(ex)
