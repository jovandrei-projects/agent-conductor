"""tour_polish.py - OPTIONAL, disabled-by-default LLM polish for the
dashboard 'What's new' tour.

The deterministic tour (tour_state + tour_feeds) never depends on this
module: it produces commit-subject cards on its own and keeps working
when no provider exists. When the operator opts in
(``CONDUCTOR_TOUR_POLISH=1``), one BOUNDED `devin -p` call per uncached
watermark range rewrites the commit-grouping into nicer cards - tighter
titles/blurbs, saner stop assignment - while every record card
(request/run/follow-up/curated) and the unread state stay deterministic.

Provider
--------
The repo's provider integration is the `devin` CLI itself: spawned like
the run summarizer (``devin --print --prompt-file F --export F``), with
credentials living in the CLI's own credentials.toml - nothing secret is
copied into tracked files, and ``clean_devin_env``-style env scrubbing
(ACP_/WINDSURF_/VSCODE_) is replicated here so this module never imports
conductor. ``provider=`` is a test seam: any ``(prompt: str) -> str``
callable.

Bounds
------
Input: ``git log --oneline --stat`` capped at ~MAX_COMMITS commits and
PROMPT_MAX_CHARS. Output: REPLY_MAX_CHARS read cap, MAX_CARDS cards,
per-field length caps. Duration: TIMEOUT_S on the child process; a
timeout kills it.

Validation
----------
Every returned card is checked against the input it was shown: stops
must be known (TOUR_STOPS or the unanchored area names), commit refs
must prefix-match exactly one commit in the window (invented shas are
dropped), titles/blurbs carrying URLs are rejected (no invented links),
and run/request links are filtered against the window's real records at
apply time. Cards are persisted with FULL shas so later application is
unambiguous. Curated blurbs are never touched - tour_feeds applies
curated fold-ins on top of whatever text the cards carry. Entry
identities stay deterministic: a polished card's id is still
``area:<stop>:<newest-sha12>`` derived from its content, so a persisted
range always re-renders the same ids and viewed/unviewed descriptors
stay resolvable.

Cache
-----
``data/tour-cache.json``, atomic tmp+os.replace writes like tour_state.
Keyed by the commit pair ``"<base>..<tip>"`` (the record-window `at`
bounds are deliberately outside the key: model input is commits only, so
identical commit ranges share output). A FAILED attempt persists
``{"status": "fallback", "error": ...}`` - ordinary page loads then
never retry the range. Concurrent attempts for one range coalesce on a
per-Polisher in-flight set: one provider call, the rest observe.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import os
import re
import subprocess
import tempfile
import threading
import time
from pathlib import Path

LOG = logging.getLogger("conductor.tour_polish")

ROOT = Path(__file__).resolve().parent
DEFAULT_CACHE = ROOT / "data" / "tour-cache.json"
CACHE_VERSION = 1
_CACHE_KEEP = 40            # persisted ranges retained (oldest pruned)


def _truthy(v) -> bool:
    return str(v or "").strip().lower() in ("1", "true", "yes", "on")


# Configuration constants - the project's convention is env-overridable
# module constants (CONDUCTOR_DEVIN, CONDUCTOR_MAX_AGENTS, ...).
ENABLED = _truthy(os.environ.get("CONDUCTOR_TOUR_POLISH"))
# Cheap + fast uid; 'gpt-6-luna-low-priority' is the verified low/fast
# lane documented in assistant_host/modelpick.py. Never a paid default.
MODEL = os.environ.get("CONDUCTOR_TOUR_POLISH_MODEL") \
    or "gpt-6-luna-low-priority"
TIMEOUT_S = float(os.environ.get("CONDUCTOR_TOUR_POLISH_TIMEOUT_S")
                  or 120)
MAX_COMMITS = 30            # ~one page of git log per attempt
MAX_CARDS = 24              # a window never needs more distinct cards
PROMPT_MAX_CHARS = 32_000
REPLY_MAX_CHARS = 16_000

_SHA_PREFIX_RE = re.compile(r"^[0-9a-fA-F]{4,64}$")
_URL_RE = re.compile(r"https?://", re.I)
_LOCK = threading.RLock()


class ProviderError(RuntimeError):
    """The provider transport failed - spawn error, timeout, nonzero
    exit with no reply. The caller turns this into a fallback record."""


# ------------------------------------------------------------------- env

def _clean_env() -> dict:
    """Mirrors conductor.clean_devin_env: strip the host-injected
    ACP_/WINDSURF_/VSCODE_/TERM_PROGRAM vars that make the devin CLI miss
    its credentials.toml. Duplicated so tour_polish stays
    conductor-free."""
    env = dict(os.environ)
    for k in list(env):
        if k.startswith(("ACP_", "WINDSURF_", "VSCODE_", "TERM_PROGRAM")):
            del env[k]
    return env


def _export_answer(doc: dict | None) -> str:
    """Trailing run of tool-less agent messages from an ATIF export -
    the same extraction devin_print.export_answer uses."""
    texts: list[str] = []
    for s in reversed((doc or {}).get("steps") or []):
        if not isinstance(s, dict) or s.get("source") != "agent" \
                or s.get("tool_calls"):
            break
        msg = (s.get("message") or "").strip()
        if msg:
            texts.append(msg)
    return "\n\n".join(reversed(texts)).strip()


def _git(repo_dir: Path, *args: str) -> subprocess.CompletedProcess:
    kw = {"creationflags": subprocess.CREATE_NO_WINDOW} \
        if os.name == "nt" else {}
    return subprocess.run(["git", "-C", str(repo_dir), *args],
                          capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=60,
                          **kw)


# ------------------------------------------------------------- provider

def devin_provider(prompt: str, *, devin_argv=("devin",),
                   model: str = MODEL,
                   timeout_s: float = TIMEOUT_S) -> str:
    """One bounded `devin -p` print session: prompt file in, reply text
    out of the ATIF export. Runs in a temp dir so its cwd-scoped session
    registry never pollutes a real workspace; `--permission-mode auto`
    refuses mutation/exec tools - this call only needs prose."""
    with tempfile.TemporaryDirectory(prefix="tour-polish-") as wd:
        pf = Path(wd) / "prompt.md"
        pf.write_text(prompt, encoding="utf-8")
        export = Path(wd) / "export.json"
        cmd = list(devin_argv) + [
            "--print", "--prompt-file", str(pf),
            "--permission-mode", "auto",
            "--export", str(export),
            "--respect-workspace-trust", "false"]
        if model:
            cmd += ["--model", model]
        kw = {"creationflags": subprocess.CREATE_NO_WINDOW} \
            if os.name == "nt" else {}
        try:
            proc = subprocess.Popen(
                cmd, cwd=wd, stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace",
                env=_clean_env(), **kw)
        except OSError as exc:
            raise ProviderError(f"provider spawn failed: {exc}")
        try:
            out, _ = proc.communicate(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
                proc.communicate(timeout=10)
            except Exception:                            # noqa: BLE001
                pass
            raise ProviderError(
                f"provider timed out after {timeout_s:.0f}s")
        doc = None
        try:
            doc = json.loads(export.read_text(encoding="utf-8",
                                              errors="replace"))
        except (OSError, ValueError):
            pass
        text = _export_answer(doc)
        if not text and out:                             # stdout tail
            text = "\n".join(
                ln.rsplit("\r", 1)[-1] for ln in out.split("\n")).strip()
        if proc.returncode != 0 and not text:
            raise ProviderError(f"provider exited {proc.returncode}")
        if not text:
            raise ProviderError("provider finished without a reply")
        return text[:REPLY_MAX_CHARS]


# ---------------------------------------------------------------- prompt

def _stops_doc() -> dict:
    """{id: title} the model may name - TOUR_STOPS plus the unanchored
    area titles. Imported lazily so this module also imports cleanly
    when tour_feeds is absent (then only generic stops are offered)."""
    try:
        import tour_feeds
        out = {k: v["title"] for k, v in tour_feeds.TOUR_STOPS.items()}
        out.update(tour_feeds._AREA_TITLES)
        return out
    except Exception:                                    # noqa: BLE001
        return {}


def git_stat(repo_dir: Path, rng: dict,
             max_commits: int = MAX_COMMITS,
             max_chars: int = PROMPT_MAX_CHARS) -> tuple[str, str | None]:
    """Bounded ``git log --oneline --stat`` for the window's commit
    range. Returns (text, warning); empty text + warning on failure."""
    base = ((rng or {}).get("base") or {}).get("commit")
    tip = ((rng or {}).get("tip") or {}).get("commit")
    if not tip:
        return "", "range has no tip commit"
    spec = f"{base}..{tip}" if base else tip
    r = _git(repo_dir, "log", spec, "--oneline", "--stat",
             f"-n{max_commits + 1}")
    if r.returncode != 0:
        return "", (f"git log {spec} failed: "
                    f"{(r.stderr or '').strip()[:160]}")
    text = r.stdout or ""
    clipped = len(text) > max_chars
    if clipped:
        text = text[:max_chars].rsplit("\n", 1)[0] + "\n[truncated]\n"
    return text, None


_PROMPT_HEAD = """\
You are grouping git commits into 'What's new' tour cards for a
dashboard. Below is `git log --oneline --stat` for ONE change window,
then the registry of UI stops a card may anchor to.

STOPS (id -> title):
{stops}

RULES - violating any of them discards the card:
- "stop" must be one of the stop ids above. Pick the closest fit; use a
  generic area name only when no stop fits.
- "commits" lists sha prefixes taken VERBATIM from the git log below -
  never invent a sha, and a commit may appear in at most one card.
- "tag" is one of "new", "improved", "fixed".
- "title" <= 160 chars, "blurb" <= 600 chars of plain text - no URLs,
  no request ids, no run keys, no markdown links.
- Group related commits into ONE card; skip pure churn (merge checkoints,
  typos) only when a card would add no user-facing information.

Reply with ONLY a JSON object:
{{"cards": [{{"stop": "...", "title": "...", "blurb": "...",
"tag": "new|improved|fixed", "commits": ["<sha>", ...]}}]}}

GIT LOG ({spec}, capped at {max_commits} commits):
"""


def build_prompt(stat_text: str, rng: dict,
                 max_commits: int = MAX_COMMITS) -> str:
    base = ((rng or {}).get("base") or {}).get("commit") or "(unbounded)"
    tip = ((rng or {}).get("tip") or {}).get("commit") or "?"
    spec = f"{base[:12]}..{tip[:12]}"
    return _PROMPT_HEAD.format(
        stops=json.dumps(_stops_doc(), indent=1, ensure_ascii=False),
        spec=spec, max_commits=max_commits) + stat_text


# ------------------------------------------------------------ validation

def parse_reply(text: str) -> dict | None:
    """Extract the JSON object from a reply. Accepts a bare document or
    one fenced/prose-wrapped; anything else is a parse failure."""
    t = (text or "").strip()[:REPLY_MAX_CHARS]
    if not t:
        return None
    for fence in ("```json", "```JSON", "```"):
        if t.startswith(fence):
            t = t[len(fence):].strip()
            if t.endswith("```"):
                t = t[:-3].strip()
            break
    try:
        return json.loads(t)
    except ValueError:
        pass
    m = re.search(r"\{.*\}", t, re.S)
    if m:
        try:
            return json.loads(m.group(0))
        except ValueError:
            return None
    return None


def _clean_text(v, limit: int) -> str | None:
    if not isinstance(v, str) or not v.strip():
        return None
    return " ".join(v.split())[:limit]


def sanitize_cards(raw_cards, *, window_shas: list[str],
                   known_stops: set[str]) -> tuple[list[dict], list[str]]:
    """Validate model-produced cards against the input they were shown.

    Each kept card: {stop, title, blurb, tag, commits[full sha],
    requests[], runs[]}. A commit ref that doesn't prefix-match exactly
    one window commit is dropped (invented shas never land); a card left
    with no real commits is dropped. URLs in title/blurb drop the card -
    generated text must not carry invented links. Record links are kept
    as raw strings and re-filtered against the window's real records at
    apply time (this module never scans record dirs)."""
    cards: list[dict] = []
    drops: list[str] = []
    used: set[str] = set()
    for i, c in enumerate(raw_cards or []):
        if len(cards) >= MAX_CARDS:
            drops.append(f"card {i}: over MAX_CARDS - dropped")
            break
        if not isinstance(c, dict):
            drops.append(f"card {i}: not an object - dropped")
            continue
        stop = c.get("stop")
        if not isinstance(stop, str) or stop.strip() not in known_stops:
            drops.append(f"card {i}: unknown stop {stop!r} - dropped")
            continue
        title = _clean_text(c.get("title"), 160)
        blurb = _clean_text(c.get("blurb"), 600)
        if not title or not blurb:
            drops.append(f"card {i}: missing title/blurb - dropped")
            continue
        if _URL_RE.search(title) or _URL_RE.search(blurb):
            drops.append(f"card {i}: URL in text - dropped")
            continue
        shas: list[str] = []
        for ref in c.get("commits") or []:
            if not isinstance(ref, str) or not _SHA_PREFIX_RE.fullmatch(
                    ref.strip()):
                continue
            ref = ref.strip().lower()
            hits = [s for s in window_shas if s.startswith(ref)]
            if len(hits) != 1 or hits[0] in used:
                continue                # invented/ambiguous/claimed
            shas.append(hits[0])
            used.add(hits[0])
        if not shas:
            drops.append(f"card {i}: no real commits - dropped")
            continue
        tag = c.get("tag")
        cards.append({
            "stop": stop.strip(), "title": title, "blurb": blurb,
            "tag": tag if tag in ("new", "improved", "fixed") else None,
            "commits": shas,
            "requests": [r for r in (c.get("requests") or [])
                         if isinstance(r, str) and r.strip()],
            "runs": [r for r in (c.get("runs") or [])
                     if isinstance(r, str) and r.strip()],
        })
    return cards, drops


# ------------------------------------------------------------- polisher

class Polisher:
    """One per dashboard serve: owns the range-keyed cache file and the
    in-flight set that coalesces concurrent attempts for the same range.

    Seams for tests: ``provider`` (any (prompt)->str callable; default
    spawns `devin -p`), ``enabled`` (default: module ENABLED env flag),
    ``max_commits``/``timeout_s``/``model`` overrides.
    """

    def __init__(self, cache_path: Path = DEFAULT_CACHE,
                 repo_dir: Path = ROOT, *,
                 devin_argv=None, provider=None, model: str = "",
                 timeout_s: float | None = None, enabled=None,
                 max_commits: int | None = None):
        self.cache_path = Path(cache_path)
        self.repo_dir = Path(repo_dir)
        self.devin_argv = list(devin_argv or ["devin"])
        self.provider = provider
        self.model = model or MODEL
        self.timeout_s = TIMEOUT_S if timeout_s is None else timeout_s
        self._enabled = ENABLED if enabled is None else bool(enabled)
        self.max_commits = max_commits or MAX_COMMITS
        self._inflight: set[str] = set()
        self._lock = threading.Lock()

    # -------------------------------------------------------- config

    def enabled(self) -> bool:
        return self._enabled

    @staticmethod
    def range_key(rng: dict) -> str:
        """The cache key for a window: its commit pair. The `at` bounds
        stay out - model input is commits only, so equal commit ranges
        share output and a moved record window can't force a re-call."""
        b = (((rng or {}).get("base") or {}).get("commit")) or "none"
        t = (((rng or {}).get("tip") or {}).get("commit")) or "none"
        return f"{b}..{t}"

    def generating(self, rng: dict) -> bool:
        with self._lock:
            return self.range_key(rng) in self._inflight

    # ---------------------------------------------------------- cache

    def _load(self) -> dict:
        try:
            doc = json.loads(self.cache_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"version": CACHE_VERSION, "ranges": {}}
        if not isinstance(doc, dict) \
                or not isinstance(doc.get("ranges"), dict):
            return {"version": CACHE_VERSION, "ranges": {}}
        return doc

    def _store(self, rng: dict, record: dict) -> None:
        """Serialized read-modify-write + atomic replace (tour_state's
        tmp + PermissionError-retry pattern). A failed attempt's
        'fallback' record is persisted too - page loads must not
        re-arm a doomed call every TTL."""
        key = self.range_key(rng)
        with _LOCK:
            doc = self._load()
            ranges = doc["ranges"]
            ranges[key] = {**record, "key": key}
            while len(ranges) > _CACHE_KEEP:      # oldest 'at' first
                oldest = min(ranges, key=lambda k: str(
                    (ranges[k] or {}).get("at") or ""))
                if oldest == key:
                    break                          # never drop the new one
                del ranges[oldest]
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.cache_path.with_name(self.cache_path.name + ".tmp")
            tmp.write_text(json.dumps(doc, indent=1, ensure_ascii=False),
                           encoding="utf-8")
            for attempt in range(20):
                try:
                    tmp.replace(self.cache_path)
                    return
                except PermissionError:
                    if attempt == 19:
                        raise
                    time.sleep(0.1)

    def record(self, rng: dict) -> dict | None:
        """The persisted record for this range (ok or fallback), or None
        when uncached. Disabled polishers read as uncached."""
        if not self._enabled:
            return None
        return self._load()["ranges"].get(self.range_key(rng))

    def cards_for(self, rng: dict):
        """Validated card list for tour_feeds' polish hook, or None:
        only a persisted 'ok' record serves - fallbacks and misses both
        mean 'deterministic cards for this range'."""
        rec = self.record(rng)
        if rec and rec.get("status") == "ok" \
                and isinstance(rec.get("cards"), list):
            return rec["cards"]
        return None

    # ------------------------------------------------------- generation

    def _rec(self, status: str, *, cards=None, error=None,
             model=None) -> dict:
        return {"status": status, "cards": cards or [],
                "error": error, "model": model,
                "at": dt.datetime.now().isoformat(timespec="seconds")}

    def _generate(self, rng: dict) -> dict:
        """One provider attempt for the range - never raises; any
        failure shape (git error, provider error, malformed reply, no
        usable cards) returns a 'fallback' record instead."""
        stat, warn = git_stat(self.repo_dir, rng, self.max_commits,
                              PROMPT_MAX_CHARS)
        if not stat.strip():
            return self._rec("fallback", error=warn or "empty git log")
        prompt = build_prompt(stat, rng, self.max_commits)
        fn = self.provider or (
            lambda p: devin_provider(p, devin_argv=self.devin_argv,
                                     model=self.model,
                                     timeout_s=self.timeout_s))
        try:
            reply = fn(prompt)
        except Exception as exc:                         # noqa: BLE001
            return self._rec(
                "fallback", model=self.model,
                error=f"{type(exc).__name__}: {exc}"[:300])
        doc = parse_reply(str(reply or ""))
        if not isinstance(doc, dict) \
                or not isinstance(doc.get("cards"), list):
            return self._rec("fallback", model=self.model,
                             error="malformed reply: no cards object")
        window_shas = self._window_shas(rng)
        cards, drops = sanitize_cards(doc["cards"],
                                      window_shas=window_shas,
                                      known_stops=set(_stops_doc()))
        if not cards:
            return self._rec("fallback", model=self.model,
                             error="no usable cards: "
                                   + "; ".join(drops[:4])[:240])
        rec = self._rec("ok", cards=cards, model=self.model)
        if drops:
            rec["dropped"] = drops[:8]
        return rec

    def _window_shas(self, rng: dict) -> list[str]:
        """Full shas shown to the model - the same bounded window the
        prompt used (prefix resolution needs the authoritative list)."""
        base = ((rng or {}).get("base") or {}).get("commit")
        tip = ((rng or {}).get("tip") or {}).get("commit")
        if not tip:
            return []
        spec = f"{base}..{tip}" if base else tip
        r = _git(self.repo_dir, "rev-list", spec,
                 f"-n{self.max_commits}")
        if r.returncode != 0:
            return []
        return [ln.strip() for ln in r.stdout.splitlines() if ln.strip()]

    def attempt(self, rng: dict) -> dict:
        """Synchronously generate+persist for ``rng`` when uncached.
        Coalesced: a concurrent attempt for the same key observes
        {'status': 'generating'} instead of doubling the provider call.
        Never raises - failures persist as fallback records."""
        if not self._enabled:
            return {"status": "disabled"}
        key = self.range_key(rng)
        existing = self._load()["ranges"].get(key)
        if existing is not None:
            return existing
        with self._lock:
            if key in self._inflight:
                return {"status": "generating", "key": key}
            self._inflight.add(key)
        try:
            rec = self._generate(rng)
            try:
                self._store(rng, rec)
            except OSError as exc:
                return self._rec("fallback",
                                 error=f"cache write failed: {exc}")
            return rec
        finally:
            with self._lock:
                self._inflight.discard(key)

    def kick(self, rng: dict, on_done=None) -> bool:
        """Spawn a background attempt when the range is uncached and
        idle. Returns True when a generation was started; False for
        disabled / no tip commit / cached / already in-flight."""
        if not self._enabled \
                or not (((rng or {}).get("tip") or {}).get("commit")):
            return False
        key = self.range_key(rng)
        if self._load()["ranges"].get(key) is not None:
            return False
        with self._lock:
            if key in self._inflight:
                return False
            self._inflight.add(key)

        def work() -> None:
            try:
                rec = self._generate(rng)
                try:
                    self._store(rng, rec)
                except OSError:
                    LOG.exception("tour polish cache write failed")
                if on_done is not None:
                    on_done(rec)
            except Exception:                            # noqa: BLE001
                LOG.exception("tour polish generation failed")
            finally:
                with self._lock:
                    self._inflight.discard(key)

        try:
            threading.Thread(target=work, daemon=True,
                             name="tour-polish").start()
        except Exception:                                # noqa: BLE001
            with self._lock:
                self._inflight.discard(key)
            return False
        return True
