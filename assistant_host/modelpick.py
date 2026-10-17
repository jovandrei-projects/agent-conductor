"""Model/effort/fast/vision resolution against the LIVE devin model
catalog (DESIGN 3.x/6.8; manifest: validate server-side, never silently
substitute).

The devin catalog is `{family_slug: [variant suffixes]}` from
`devin models list` (conductor.model_options). A variant uid is
`<family>` or `<family>-<suffix>`; effort suffixes are the first dash
segment (`low`, `max`, ...), and a FAST variant adds a family-specific
marker after the effort (`-priority` for gpt-6/5.6, `-fast` for Claude,
`-lightning` for SWE - verified 2026-10-17; NEVER a hardcoded universal
suffix). A family can also be fast-BRANDED itself (`swe-1.6-fast` is its
own family - no extra suffix).

resolve() either returns a fully-spec'd uid or raises AssistantError with
a concrete alternative list - callers surface the message verbatim.
"""
import re

from assistant.core import AssistantError

# The verified product default: gpt-6 Luna, low thinking, fast mode
# (uid `gpt-6-luna-low-priority` on the current catalog).
DEFAULT_MODEL = "gpt-6-luna"
DEFAULT_EFFORT = "low"
DEFAULT_FAST = True
DEFAULT_SETTINGS = {"model": DEFAULT_MODEL, "effort": DEFAULT_EFFORT,
                    "fast": DEFAULT_FAST, "readonly": True,
                    "transport": "fast"}

# Effort ladder seen in the catalog (used only to RECOGNIZE an effort
# segment in a suffix, never to restrict - unknown ones pass through).
_EFFORT_HEADS = {"none", "low", "medium", "high", "xhigh", "max"}

# Fast-variant markers verified in the catalog. Detection is
# per-family (a marker is whatever follows the effort segment).
_FAST_BRANDS = ("fast", "lightning", "priority", "turbo")

# Vision capability VERIFIED live (probe attached the example PNG through
# the provider read-tool on 2026-10-17). Absent family = unknown -> the
# runtime refuses attachments with a clear explanation rather than
# pretending.
VISION = {"gpt-6-luna": True}

_SLUG = re.compile(r"^[a-z0-9][a-z0-9.-]*$")


def _fast_markers(suffixes: list[str]) -> list[str]:
    """The family's fast markers: the segment AFTER an effort head in a
    two-part suffix ('low-priority' -> 'priority')."""
    out = []
    for s in suffixes or []:
        parts = str(s).split("-", 1)
        if len(parts) == 2 and parts[0] in _EFFORT_HEADS \
                and parts[1] not in out:
            out.append(parts[1])
    return out


def family_is_fast(family: str) -> bool:
    """The family name itself carries a fast brand (swe-1.6-fast)."""
    f = (family or "").lower().replace("_", "-")
    return any(f.endswith("-" + b) or f.endswith("." + b)
               for b in _FAST_BRANDS)


def _find_family(model: str, options: dict):
    """model slug -> (family, suffix or None when bare family). A full
    uid parses greedily on the longest matching family name."""
    for fam in sorted(options, key=len, reverse=True):
        if model == fam:
            return fam, ""
        if model.startswith(fam + "-"):
            tail = model[len(fam) + 1:]
            if tail in options[fam]:
                return fam, tail
    return None, None


def _alternatives(options: dict, effort: str, fast: bool) -> list[str]:
    """Verified uids that DO satisfy the request - the honest answer to
    'what CAN I pick instead'. _alt=True suppresses nested alternative
    expansion (no recursion)."""
    out = []
    for fam in sorted(options):
        try:
            out.append(resolve(fam, effort, fast, options,
                               _alt=True)["uid"])
        except AssistantError:
            continue
    return out[:8]


def vision_alternatives(options: dict, effort="", fast=False) -> list[str]:
    """uids whose family has verified image support - shown when a
    request attaches an image to a model with unverified vision."""
    out = []
    for fam in sorted(VISION):
        if fam in options:
            try:
                out.append(resolve(fam, effort, fast, options)["uid"])
            except AssistantError:
                continue
    return out


def resolve(model: str, effort: str, fast, options: dict,
            _alt: bool = False) -> dict:
    """(model, effort, fast) + the live catalog -> resolved spec.

    Returns {uid, family, effort, fast, suffix, vision}. Raises
    AssistantError (400) with concrete verified alternatives when the
    combination cannot be produced - silent fallback is never an option.
    """
    options = options or {}
    model = str(model or "").strip().lower()
    effort = str(effort or "").strip().lower()
    fast = bool(fast)
    if not model:
        model = DEFAULT_MODEL
    if not _SLUG.fullmatch(model):
        raise AssistantError(f"invalid model name: {model!r}")
    if not options:
        raise AssistantError(
            "model catalog unavailable - the devin CLI cannot be "
            "reached or is not authenticated; no model can be verified "
            "right now")
    fam, tail = _find_family(model, options)
    if fam is None:
        raise AssistantError(
            f"unknown model {model!r} - not in the provider catalog. "
            f"Available families: {', '.join(sorted(options)[:10])}")
    suffixes = options[fam]
    if tail is not None and tail != "":
        # a full uid was given: derive effort/fast FROM the suffix so
        # an explicit contradiction is caught, never silently honored
        derived_eff, derived_fast = tail, False
        parts = tail.rsplit("-", 1)
        if len(parts) == 2 and parts[1] in _fast_markers(suffixes):
            derived_eff, derived_fast = parts[0], True
        if effort and effort != derived_eff:
            raise AssistantError(
                f"{model} already encodes effort '{derived_eff}' - "
                f"contradicts effort='{effort}'")
        if fast and not derived_fast and not family_is_fast(fam):
            raise AssistantError(
                f"{model} is not the fast variant; pick a *-"
                f"{'/'.join(_fast_markers(suffixes)) or '?'} uid or "
                "set effort+fast separately")
        return {"uid": f"{fam}-{tail}", "family": fam,
                "effort": derived_eff, "fast": derived_fast,
                "suffix": tail, "vision": VISION.get(fam)}

    if effort and effort not in suffixes and \
            not any(s.startswith(effort + "-") for s in suffixes):
        raise AssistantError(
            f"effort {effort!r} not offered for {fam}; available: "
            f"{', '.join(repr(s) for s in suffixes) or '(bare uid only)'}")
    if fast and not family_is_fast(fam):
        markers = _fast_markers(suffixes)
        if effort:
            want = [f"{effort}-{m}" for m in markers]
        else:
            # no effort pinned: fast variants only exist as
            # '<effort>-<marker>' pairs, so any of them satisfies 'fast'
            # - catalog order picks the cheapest lane offered
            want = [s for s in suffixes
                    if s.rsplit("-", 1)[-1] in markers]
        suffix = next((s for s in want if s in suffixes), None)
        if suffix is None:
            alts = ([] if _alt else _alternatives(options, effort, True))
            raise AssistantError(
                f"{fam} has no fast variant"
                + (f" for effort '{effort}'" if effort else "")
                + " - verified alternatives: "
                + (", ".join(alts) if alts else "none on this catalog")
                + (f"; or drop 'fast' and use `{fam}-{effort or 'default'}`"))
        return {"uid": f"{fam}-{suffix}", "family": fam,
                "effort": effort or suffix.rsplit("-", 1)[0],
                "fast": True, "suffix": suffix, "vision": VISION.get(fam)}
    uid = f"{fam}-{effort}" if effort else fam
    if effort and uid not in {fam, *[f"{fam}-{s}" for s in suffixes]}:
        raise AssistantError(f"no variant {uid!r} in the catalog")
    return {"uid": uid, "family": fam,
            "effort": effort, "fast": fast and family_is_fast(fam),
            "suffix": effort, "vision": VISION.get(fam)}


def resolve_settings(snapshot: dict, options: dict,
                     attachments_present: bool = False) -> dict:
    """A conversation/exchange settings snapshot -> the resolved spec the
    runtime dispatches on. Adds `vision` (True/None) and, when attachments
    are present, `vision_alternatives` for the refusal message."""
    resolved = resolve(snapshot.get("model"), snapshot.get("effort"),
                     snapshot.get("fast"), options)
    if attachments_present:
        resolved["vision_alternatives"] = vision_alternatives(
            options, resolved["effort"], resolved["fast"])
    return resolved


def make_validator(options_fn):
    """settings_validator seam for AssistantCore: validates EFFECTIVE
    {model,effort,fast} against the live catalog. An EMPTY catalog
    (CLI down/unauthed) is not a reason to block saving a preference -
    dispatch fails honestly instead."""
    def validate(effective: dict) -> None:
        opts = options_fn() or {}
        if not opts:
            return                       # unverifiable -> defer to dispatch
        resolve(effective.get("model"), effective.get("effort"),
                effective.get("fast"), opts)
    return validate


def annotate_catalog(models: list[dict], options: dict,
                     defaults: dict) -> dict:
    """The /models payload: catalog entries annotated with capabilities,
    plus `default_resolution` = whether the configured default actually
    resolves today (the UI's honest-availability story)."""
    out = []
    for m in models or []:
        uid = m.get("uid") or ""
        fam, tail = _find_family(uid, options)
        fast_flag = False
        if tail:
            parts = tail.rsplit("-", 1)
            fast_flag = (len(parts) == 2
                         and parts[1] in _fast_markers(options[fam]))
        out.append({**m,
                    "vision": VISION.get(fam) if fam else None,
                    "fast_variant": fast_flag or family_is_fast(fam)})
    try:
        res = resolve_settings(defaults, options)
        dres = {"ok": True, **res}
    except AssistantError as exc:
        dres = {"ok": False, "error": str(exc)}
    return {"models": out, "default_resolution": dres}
