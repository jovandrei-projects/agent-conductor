"""Per-turn view-context envelopes - the generic half of "the assistant
knows what page/modal is open" (DESIGN.md section 6.5).

The client reports a lightweight `client_ctx`; the host adapter enriches it
server-side with bounded record data (never trusting client text as
authority). The frozen JSON lands on the user message's `ctx_ref` column so
every later turn still shows the view its question was asked from, while
new questions pick up the now-current view.

This module is host-agnostic: it knows the envelope *shape* and the
sanitization rules, not conductor's view vocabulary - `ViewSpec` names are
slugs the host defines (dashboard, agent, run, summary, ...).
"""
import copy
import json
import re

VIEW_RE = re.compile(r"[a-z][a-z0-9_-]{0,39}\Z")
REF_KEY_RE = re.compile(r"[a-z][a-z0-9_.-]{0,63}\Z")

# Field-name denylist applied recursively while freezing: an envelope must
# never carry credential material into persisted chat metadata. Matches
# whole key names and common suffixes - deliberately NOT the substring
# 'token' alone, since 'tokens'/'prompt_t' usage fields are legitimate.
SENSITIVE_KEY_RE = re.compile(
    r"(?i)(password|passwd|secret|credential|api[_-]?key|access[_-]?key|"
    r"api[_-]?token|auth[_-]?token|access[_-]?token|refresh[_-]?token|"
    r"session[_-]?token|bearer|cookie|private[_-]?key|client[_-]?secret)")

SELECTION_MAX = 2048          # user text-selection excerpt cap
ENVELOPE_MAX_BYTES = 16384    # whole frozen envelope cap
REF_VALUE_MAX = 512           # any single ref/state leaf value
MAX_REFS = 32                 # distinct id keys per envelope
MAX_DEPTH = 6


class ContextError(ValueError):
    """Raised for malformed envelopes (bad view slug, oversize fields)."""


def _sanitize(value, depth=0):
    """Deep-copy `value` with sensitive keys dropped and leaf sizes capped.
    Untrusted client/host structures both pass through here before anything
    is persisted."""
    if depth > MAX_DEPTH:
        return None
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            key = str(k)
            if SENSITIVE_KEY_RE.search(key):
                continue                       # credentials stay out
            if not REF_KEY_RE.fullmatch(key):
                continue                       # weird keys stay out too
            sv = _sanitize(v, depth + 1)
            if sv is not None:
                out[key] = sv
            if len(out) >= MAX_REFS:
                break
        return out
    if isinstance(value, (list, tuple)):
        out = [_sanitize(v, depth + 1) for v in value[:64]]
        return [v for v in out if v is not None]
    if isinstance(value, str):
        return value[:REF_VALUE_MAX]
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, (int, float)):
        return value
    return str(value)[:REF_VALUE_MAX]


def envelope(view: str, *, refs=None, filters=None, selection="",
             window="", enriched=None, at=None) -> dict:
    """Build a validated, sanitized envelope. `view` is the host's surface
    slug; `refs` carries the stable identifiers ({run_key, step,
    request_id, ...} in conductor's adapter); `window` is the originating
    browser-window identity the client supplies; `enriched` is bounded
    server-side context the host attaches."""
    view = str(view or "dashboard").strip().lower()
    if not VIEW_RE.fullmatch(view):
        raise ContextError(f"invalid view {view!r}")
    if selection and not isinstance(selection, str):
        raise ContextError("selection must be text")
    env = {
        "view": view,
        "window": str(window or "")[:128],
        "refs": _sanitize(refs or {}),
        "filters": _sanitize(filters or {}),
        "selection": (selection or "")[:SELECTION_MAX],
        "enriched": _sanitize(enriched or {}),
        "at": str(at or "")[:40],
    }
    return env


def freeze(env: dict) -> str:
    """Envelope -> the canonical JSON stored in messages.ctx_ref. Size is
    enforced here so nothing oversized reaches the row."""
    data = json.dumps(env, ensure_ascii=False, sort_keys=True)
    if len(data.encode("utf-8")) > ENVELOPE_MAX_BYTES:
        # Shed bulk deterministically: selection first, then enriched.
        env = dict(env, selection=(env.get("selection") or "")[:512],
                   enriched={})
        data = json.dumps(env, ensure_ascii=False, sort_keys=True)
        if len(data.encode("utf-8")) > ENVELOPE_MAX_BYTES:
            raise ContextError("context envelope exceeds size limit")
    return data


def thaw(frozen: str | None) -> dict:
    """ctx_ref JSON -> envelope dict; None/empty/corrupt -> minimal dict.
    A thawed envelope is still untrusted *content* - it records where a
    question was asked, not instructions."""
    if not frozen:
        return {}
    try:
        env = json.loads(frozen)
    except ValueError:
        return {}
    return env if isinstance(env, dict) else {}


def ctx_label(env: dict) -> str:
    """One-line 'about: <surface>' description for the UI chip."""
    view = env.get("view") or "dashboard"
    refs = env.get("refs") or {}
    for key in ("step", "run_key", "request_id", "summary_key", "question"):
        if refs.get(key):
            return f"{view}:{refs[key]}"
    return view


def new_envelope_from_client(client_ctx: dict, *, at=None) -> dict:
    """Validate the raw client_ctx dict a browser posts."""
    if not isinstance(client_ctx, dict):
        raise ContextError("client_ctx must be an object")
    return envelope(
        client_ctx.get("view"),
        refs=client_ctx.get("refs"),
        filters=client_ctx.get("filters"),
        selection=client_ctx.get("selection"),
        window=client_ctx.get("window"),
        enriched=client_ctx.get("enriched"),
        at=client_ctx.get("at") or at)
