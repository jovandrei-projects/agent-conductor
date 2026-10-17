"""tour_state.py - persistent state + watermark for the dashboard
'What's new' tour.

State file: ``data/tour-state.json`` (gitignored with the rest of data/).
Top-level shape, exactly:

    {
      "watermark": {"commit": <sha|null>, "at": <iso|null>,
                    "origin": "init"|"advance"},
      "viewed":  [<entry descriptor>, ...],
      "snoozed": [<entry descriptor>, ...]
    }

Watermark semantics
-------------------
The watermark is the last ACKNOWLEDGED boundary, a pair:

  * ``commit`` - a resolved git revision (full sha) or None. Commit feeds
    enumerate ``git log <commit>..<boundary.commit>``; None means
    unbounded - every commit is unacknowledged.
  * ``at`` - an ISO instant bounding the record feeds (requests'
    ``created_at``, runs' ``ended_at``, follow-up registrations): records
    stamped in ``(wm.at, boundary.at]`` are unacknowledged. None means
    unbounded.

It is only ever a concrete revision + instant captured at a real moment -
never the symbolic name ``HEAD`` (which floats) and never the serve boot
id (which changes every restart). Nothing touches it at server start: a
restart neither acknowledges nor erases unviewed changes.

First initialization writes ``{commit: null, at: null, origin: "init"}``:
nothing has ever been acknowledged, so everything reads as new. That is
deliberate - pinning HEAD at first sight would silently acknowledge all
existing history, and re-creating a deleted file would hide every change
that arrived in between. A lost/corrupt file therefore fails LOUD (all
changes re-present) rather than quietly empty.

A recorded ``watermark.commit`` that no longer resolves (history rewritten,
gc'd, foreign checkout) is kept verbatim and reported ``available: False``
by ``watermark_status``; advancement is refused while it stays
unresolvable. It is never silently reset to HEAD.

Entry descriptors
-----------------
Every entry the user has acted on is recorded once - in exactly ONE list -
as::

    {"id": <stable entry id>,
     "range": {"base": {"commit":.., "at":..},
               "tip":  {"commit":.., "at":..}},
     "viewed_at"|"snoozed_at": <iso>,
     "unviewed_at": <iso>            # only after an explicit unview
    }

``range`` is the feed window that produced the entry: ``base`` is the
watermark in effect when its feed was built, ``tip`` the boundary that
feed was captured against. That pair is everything the feed needs to
re-materialize the entry after the live watermark advances past it -
which is how viewed history survives advancement and how an explicitly
unviewed entry behind the watermark can resurface as new. Membership is
mutually exclusive: marking viewed evicts the snoozed record, snoozing
evicts the viewed record, and ``unviewed_at`` marks a descriptor as
returned to 'new' without dropping its range.

Transitions
-----------
  * ``mark_viewed`` - acknowledge an entry. The ONLY transition that may
    advance the watermark, and only when the caller reports no entries
    still 'new' in the acknowledged window (``remaining_new`` empty).
    Advancement lands at the feed's captured ``boundary`` - never at a
    fresh HEAD - so changes arriving after capture stay unacknowledged.
    A snoozed entry does not count as 'new': deferral must not pin the
    watermark forever.
  * ``snooze`` - defer: moves the descriptor to ``snoozed``. Never
    acknowledges, never advances.
  * ``unview`` - restores 'new' by flagging ``unviewed_at``; the range is
    kept so an entry behind the watermark can be re-injected by the feed.

Serialization: a module-level RLock serializes read-modify-write across
threads; every write is tmp-file + os.replace with the same Windows
PermissionError retry save_state uses, so a crashed write never leaves a
torn file and concurrent actions cannot lose updates. The dashboard is
the sole writer - that matches every other data/*.json here.
"""
import datetime as dt
import json
import os
import re
import subprocess
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DEFAULT_STATE_PATH = ROOT / "data" / "tour-state.json"
DEFAULT_REPO = ROOT

_LOCK = threading.RLock()

# A watermark commit is a resolved revision or null - never a symbolic
# name like HEAD and never a timestamp-shaped string like a serve_boot id.
_SHA_RE = re.compile(r"[0-9a-fA-F]{4,64}")
_ENTRY_ID_MAX = 512

STATES = ("new", "viewed", "snoozed")


def _now() -> float:
    """Clock seam, same role as conductor._now: tests inject `now=`."""
    return time.time()


def _iso(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts).isoformat(timespec="seconds")


def _iso_ts(s) -> float | None:
    """Parse a stored ISO stamp back to epoch; None when unusable."""
    if not isinstance(s, str):
        return None
    try:
        return dt.datetime.fromisoformat(s).timestamp()
    except ValueError:
        return None


def _git(repo_dir: Path, *args: str) -> subprocess.CompletedProcess:
    kw = {"creationflags": subprocess.CREATE_NO_WINDOW} \
        if os.name == "nt" else {}
    return subprocess.run(["git", "-C", str(repo_dir), *args],
                          capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=60,
                          **kw)


def _resolve(repo_dir: Path, rev: str) -> str | None:
    """rev -> full commit sha, or None when it does not name a commit."""
    if not isinstance(rev, str) or not _SHA_RE.fullmatch(rev):
        return None
    r = _git(repo_dir, "rev-parse", "--verify", "--quiet",
             f"{rev}^{{commit}}")
    if r.returncode != 0:
        return None
    return r.stdout.strip() or None


def _head(repo_dir: Path) -> str | None:
    """The checkout's current commit sha, or None (no repo / unborn)."""
    r = _git(repo_dir, "rev-parse", "--verify", "--quiet", "HEAD")
    if r.returncode != 0:
        return None
    return r.stdout.strip() or None


# ------------------------------------------------------------------ shape

def _norm_point(p) -> dict:
    """A {commit, at} boundary/watermark point, normalized."""
    p = p if isinstance(p, dict) else {}
    commit = p.get("commit")
    at = p.get("at")
    return {"commit": commit if isinstance(commit, str) else None,
            "at": at if isinstance(at, str) else None}


def _norm_range(rng) -> dict:
    """An entry's producing window {base, tip} of normalized points."""
    rng = rng if isinstance(rng, dict) else {}
    return {"base": _norm_point(rng.get("base")),
            "tip": _norm_point(rng.get("tip"))}


def _norm_descriptor(d) -> dict | None:
    """Normalize one viewed/snoozed record; None = drop it (no usable id)."""
    if not isinstance(d, dict):
        return None
    eid = d.get("id")
    if not isinstance(eid, str) or not eid or len(eid) > _ENTRY_ID_MAX:
        return None
    out = {"id": eid, "range": _norm_range(d.get("range"))}
    for k in ("viewed_at", "snoozed_at", "unviewed_at"):
        if isinstance(d.get(k), str):
            out[k] = d[k]
    return out


def _norm_state(raw) -> dict | None:
    """Tolerate malformed content: keep what is usable, normalize the rest.
    Returns None only when the document is not a JSON object at all."""
    if not isinstance(raw, dict):
        return None
    wm = raw.get("watermark")
    if isinstance(wm, dict):
        watermark = {"commit": wm.get("commit")
                     if isinstance(wm.get("commit"), str) else None,
                     "at": wm.get("at")
                     if isinstance(wm.get("at"), str) else None,
                     "origin": wm.get("origin")
                     if isinstance(wm.get("origin"), str) else "unknown"}
    else:
        watermark = None
    viewed, snoozed = [], []
    for d in raw.get("viewed") or []:
        nd = _norm_descriptor(d)
        if nd is not None:
            viewed.append(nd)
    for d in raw.get("snoozed") or []:
        nd = _norm_descriptor(d)
        if nd is not None:
            snoozed.append(nd)
    # Mutual exclusivity is an invariant; a corrupt file that lists an id
    # in both resolves toward the deferred (unacknowledged) side.
    snz_ids = {d["id"] for d in snoozed}
    viewed = [d for d in viewed if d["id"] not in snz_ids]
    return {"watermark": watermark, "viewed": viewed, "snoozed": snoozed}


def _default_state() -> dict:
    """No file yet: nothing acknowledged, nothing recorded."""
    return {"watermark": None, "viewed": [], "snoozed": []}


def _init_state() -> dict:
    """First-write watermark: unbounded. Pinning HEAD here would silently
    acknowledge every existing change - the failure mode this module
    exists to avoid."""
    return {"watermark": {"commit": None, "at": None, "origin": "init"},
            "viewed": [], "snoozed": []}


# ------------------------------------------------------------------- io

def _quarantine(path: Path) -> None:
    """Move an unreadable state file aside - bytes preserved for
    forensics - so reads fall back to the unacknowledged default instead
    of trusting half a JSON document."""
    for n in range(20):
        try:
            path.replace(path.with_name(
                f"{path.name}.corrupt-{_now():.0f}"
                + ("" if n == 0 else f"-{n}")))
            return
        except OSError:
            if n == 19:
                return


def _read(path: Path) -> dict:
    """Load + normalize. A corrupt file is quarantined and reads as a
    fresh default - losing the marks is bad, but silently rewriting what
    the file 'meant' is worse."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return _default_state()
    except (OSError, ValueError):
        _quarantine(path)
        return _default_state()
    st = _norm_state(raw)
    if st is None:
        _quarantine(path)
        return _default_state()
    return st


def _write(path: Path, st: dict) -> None:
    """tmp + os.replace: an interrupted write never leaves half a file.
    The PermissionError retry mirrors save_state - Windows readers/AV can
    hold either path open for a few ms."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(st, indent=2, ensure_ascii=False),
                   encoding="utf-8")
    for attempt in range(20):
        try:
            tmp.replace(path)
            return
        except PermissionError:
            if attempt == 19:
                raise
            time.sleep(0.1)


def load(path: Path = DEFAULT_STATE_PATH) -> dict:
    """Read-only view of persisted state (normalized). Never writes: a
    GET-shaped caller cannot move the watermark just by looking."""
    with _LOCK:
        return _read(path)


def update(fn, *, path: Path = DEFAULT_STATE_PATH) -> object:
    """Serialized read-modify-write. ``fn(state) -> (result, changed)``;
    the file is written only when fn reports a change, and absent files
    are seeded with the init watermark first. Use this for compound ops
    (e.g. recompute the feed inside the lock before deciding a mark)."""
    with _LOCK:
        st = _read(path)
        if not path.exists():
            st = _init_state()
        result, changed = fn(st)
        if changed:
            _write(path, st)
        return result


def ensure_initialized(path: Path = DEFAULT_STATE_PATH) -> dict:
    """Create the state file once, with the unbounded init watermark.
    Existing files are never rewritten - 'initialized' is recorded, not
    re-derived on every boot."""
    def _init(st):
        return st, not path.exists()
    return update(_init, path=path)


# ------------------------------------------------------------- watermark

def capture_boundary(repo_dir: Path = DEFAULT_REPO,
                     *, now: float | None = None) -> dict:
    """Snapshot {commit, at} as of right now - the tip a feed is built
    against. The commit is a RESOLVED sha (never 'HEAD'), so a boundary
    keeps meaning the same history after newer commits land."""
    return {"commit": _head(repo_dir),
            "at": _iso(_now() if now is None else now)}


def feed_range(state: dict, boundary: dict) -> dict:
    """The {base, tip} window a feed built at ``boundary`` covers, for
    storing on entry descriptors."""
    wm = state.get("watermark") or {}
    return {"base": _norm_point(wm), "tip": _norm_point(boundary)}


def watermark_status(state: dict,
                     repo_dir: Path = DEFAULT_REPO) -> dict:
    """Report, never repair: whether the recorded commit still resolves.
    ``valid`` = the stored value is sha-shaped (or null); ``available`` =
    null (unbounded) or resolvable in repo_dir. A lost revision stays
    recorded and unavailable - it is never silently reset to HEAD."""
    wm = state.get("watermark") or {}
    commit = wm.get("commit")
    out = {"commit": commit, "at": wm.get("at"),
           "origin": wm.get("origin"), "valid": True, "available": True,
           "unbounded": commit is None}
    if commit is None:
        return out
    if not _SHA_RE.fullmatch(commit):
        out["valid"] = False
        out["available"] = False
        return out
    out["available"] = _resolve(repo_dir, commit) is not None
    return out


def _advance(st: dict, boundary: dict, repo_dir: Path) -> tuple[bool, str]:
    """Move the watermark to ``boundary`` - the tip the caller just fully
    acknowledged. Refuses (returns (False, why)) rather than acknowledge
    anything unverifiable; commits arriving after the boundary are never
    covered because the boundary - not HEAD - is the target."""
    wm = st.get("watermark") or {"commit": None, "at": None}
    b = _norm_point(boundary)
    bcommit = b["commit"]
    if bcommit is None:
        return False, "boundary-missing-commit"
    if not _SHA_RE.fullmatch(bcommit):
        return False, "boundary-commit-invalid"
    if _resolve(repo_dir, bcommit) is None:
        return False, "boundary-commit-unavailable"
    wcommit = wm.get("commit")
    if wcommit is not None:
        if not isinstance(wcommit, str) or not _SHA_RE.fullmatch(wcommit):
            return False, "watermark-commit-invalid"
        if _resolve(repo_dir, wcommit) is None:
            return False, "watermark-commit-unavailable"
        if _git(repo_dir, "merge-base", "--is-ancestor",
                wcommit, bcommit).returncode != 0:
            return False, "boundary-not-descendant"
    # `at` is monotone too: never let a stale boundary drag the record
    # horizon backwards and re-expose acknowledged records.
    wat, bat = _iso_ts(wm.get("at")), _iso_ts(b["at"])
    new_at = b["at"] if wat is None else (
        wm["at"] if bat is None or bat < wat else b["at"])
    st["watermark"] = {"commit": bcommit, "at": new_at,
                       "origin": "advance"}
    return True, ""


# ------------------------------------------------------------- transitions

def _find(st: dict, entry_id: str) -> tuple[list | None, dict | None]:
    for lst_name in ("viewed", "snoozed"):
        for d in st[lst_name]:
            if d["id"] == entry_id:
                return st[lst_name], d
    return None, None


def entry_state(state: dict, entry_id: str) -> str:
    """'new' | 'viewed' | 'snoozed'. An unviewed descriptor reads 'new' -
    it keeps its range so the feed can re-materialize it, but it is
    unread again for every counting purpose."""
    _lst, d = _find(state, entry_id)
    if d is None:
        return "new"
    if d.get("unviewed_at"):
        return "new"
    return "viewed" if _lst is state["viewed"] else "snoozed"


def descriptor(state: dict, entry_id: str) -> dict | None:
    """The recorded descriptor (with range + stamps) for an id."""
    _lst, d = _find(state, entry_id)
    return d


def entries(state: dict) -> list[dict]:
    """Every recorded descriptor annotated with its current state - what
    the feed needs to re-materialize viewed history, snoozed deferrals,
    and reopened ('unviewed') entries behind the watermark."""
    out = []
    for lst_name, s in (("viewed", "viewed"), ("snoozed", "snoozed")):
        for d in state[lst_name]:
            out.append({**d, "state": ("new" if d.get("unviewed_at")
                                       else s)})
    return out


def _check_id(entry_id) -> str:
    if not isinstance(entry_id, str) or not entry_id.strip():
        raise ValueError("entry id must be a non-empty string")
    entry_id = entry_id.strip()
    if len(entry_id) > _ENTRY_ID_MAX:
        raise ValueError("entry id too long")
    return entry_id


def mark_viewed(entry_id: str, rng: dict | None = None,
                boundary: dict | None = None,
                remaining_new=None,
                *, path: Path = DEFAULT_STATE_PATH,
                repo_dir: Path = DEFAULT_REPO,
                now: float | None = None) -> dict:
    """Acknowledge ``entry_id``: it leaves snoozed if parked there and
    lands in ``viewed`` with its producing ``range`` (default: the window
    from the current watermark to ``boundary``).

    ``remaining_new`` is the caller's list of entry ids still 'new' in
    the acknowledged window AFTER this mark - compute it from the feed
    built at ``boundary``. Only when it is empty does the watermark
    advance, and only as far as ``boundary``: changes that arrived after
    capture stay unacknowledged. ``remaining_new=None`` (or a stale
    boundary) never advances. The mark itself always lands."""
    entry_id = _check_id(entry_id)
    ts = _iso(_now() if now is None else now)

    def _mut(st):
        lst, old = _find(st, entry_id)
        if lst is not None:
            lst.remove(old)
        if rng is not None:
            rng_n = _norm_range(rng)
        elif old is not None:
            rng_n = old["range"]
        else:
            rng_n = {"base": _norm_point(st.get("watermark") or {}),
                     "tip": _norm_point(boundary or {})}
        st["viewed"].append({"id": entry_id, "range": rng_n,
                             "viewed_at": ts})
        advanced, why = False, "unread-remain"
        if remaining_new is not None:
            still = [i for i in remaining_new
                     if entry_state(st, i) == "new"]
            if not still:
                advanced, why = _advance(st, boundary or {}, repo_dir)
        return {"id": entry_id, "state": "viewed", "changed": True,
                "advanced": advanced, "advance_blocked": (None if advanced
                                                          else why),
                "watermark": st.get("watermark")}, True
    return update(_mut, path=path)


def snooze(entry_id: str, rng: dict | None = None,
           *, path: Path = DEFAULT_STATE_PATH,
           now: float | None = None) -> dict:
    """Defer ``entry_id``: it leaves ``viewed`` if it was there and lands
    in ``snoozed`` - deferred, NOT acknowledged; it never advances the
    watermark, and it does not count as 'new' (a snooze that pinned the
    watermark forever would wedge the tour)."""
    entry_id = _check_id(entry_id)
    ts = _iso(_now() if now is None else now)

    def _mut(st):
        lst, old = _find(st, entry_id)
        if lst is not None:
            lst.remove(old)
        if rng is not None:
            rng_n = _norm_range(rng)
        elif old is not None:
            rng_n = old["range"]
        else:
            rng_n = {"base": _norm_point(st.get("watermark") or {}),
                     "tip": {"commit": None, "at": None}}
        st["snoozed"].append({"id": entry_id, "range": rng_n,
                              "snoozed_at": ts})
        return {"id": entry_id, "state": "snoozed", "changed": True}, True
    return update(_mut, path=path)


def unview(entry_id: str, *, path: Path = DEFAULT_STATE_PATH,
           now: float | None = None) -> dict:
    """Restore ``entry_id`` to 'new'. The descriptor stays - flagged
    ``unviewed_at`` - so its range survives for the feed to re-materialize
    it even after the watermark advanced past the window that produced
    it. Unviewing an id with no record is a no-op (it already is new)."""
    entry_id = _check_id(entry_id)
    ts = _iso(_now() if now is None else now)

    def _mut(st):
        _lst, d = _find(st, entry_id)
        if d is None:
            return {"id": entry_id, "state": "new", "changed": False}, \
                False
        d["unviewed_at"] = ts
        return {"id": entry_id, "state": "new", "changed": True}, True
    return update(_mut, path=path)
