"""Conductor-side wiring for the assistant package (DESIGN.md 6.1:
"THE host wiring"). Everything conductor-specific lives here so
`assistant/` stays portable - the core takes these as injected callables
and never imports conductor.

Layout under the project's gitignored data/:

    data/assistant/assistant.db     chat history (sqlite; Store)
    data/assistant/workspace/       cwd for provider sessions - keeps
                                    `devin list` there assistant-only
    data/assistant/spool/           prompt/export files per dispatch
    data/attachments/               image bytes (existing endpoint)

The db is a SIBLING of workspace/, outside the provider session's watched
dir (trap 5), and every mutating core call runs the same
_assert_unwatched guard request writes use - a live agent-conductor
worker watching this checkout makes chat writes refuse with 409.

Context + retrieval live in sibling modules so this file stays wiring:

- ``assistant_host.context.ConductorContext`` - per-view server-side
  enrichment of each message's frozen context envelope (shared-modal
  views plus a ``register_provider`` hook for custom surfaces).
- ``assistant_host.retrieval.ConductorRetrieval`` - bounded search/fetch
  over runs + requests producing navigable citation sources.
- ``assistant_host.modelpick`` - catalog-aware model/effort/fast/vision
  resolution; the settings validator + runtime resolver both run it.
- ``static_preamble()`` - the host's standing knowledge text
  (``host_conductor_static.md`` next to this file).

open_runtime() adds the conversational surface on top: the
DevinPrintProvider (authenticated ``devin -p [-r]`` spawns in
data/assistant/workspace - the verified transport), the AssistantRuntime
dispatcher, catalog-backed settings validation, and the seeded product
default (gpt-6-luna, low thinking, fast).
"""
import functools
import os
import shlex
from pathlib import Path

import conductor
from assistant.core import AssistantCore
from assistant.providers.devin_print import DevinPrintProvider
from assistant.runtime import AssistantRuntime
from assistant.store import Store
from assistant_host import modelpick
from assistant_host.context import ConductorContext
from assistant_host.retrieval import ConductorRetrieval

ASSISTANT_DIR = conductor.DATA_DIR / "assistant"
WORKSPACE_DIR = ASSISTANT_DIR / "workspace"
SPOOL_DIR = ASSISTANT_DIR / "spool"

STATIC_PREAMBLE_PATH = Path(__file__).parent / "host_conductor_static.md"


def static_preamble(path=None) -> str:
    """The assistant's standing host knowledge - page design, dashboard
    vocabulary, where evidence lives. A missing file degrades to an
    empty string, never a crash."""
    try:
        return Path(path or STATIC_PREAMBLE_PATH).read_text(
            encoding="utf-8")
    except OSError:
        return ""


def default_devin_argv() -> list[str]:
    """Same derivation serve uses for its workers."""
    return shlex.split(os.environ.get("CONDUCTOR_DEVIN") or "devin")


def open_core(*, data_dir: Path = ASSISTANT_DIR,
              runs_dir: Path = conductor.RUNS_DIR,
              requests_dir: Path = conductor.REQUESTS_DIR,
              devin_argv=None, dispatcher=None, actions=None,
              retrieval=None, enricher=None, catalog=None,
              settings_validator=None) -> AssistantCore:
    """Build AssistantCore on this project's data dir with the conductor
    seams attached. `catalog` defaults to the pricing-merged model list
    the settings modal shows; `retrieval`/`enricher` default to the
    conductor adapters (stubs injectable for tests)."""
    data_dir = Path(data_dir)
    (data_dir / "workspace").mkdir(parents=True, exist_ok=True)
    (data_dir / "spool").mkdir(parents=True, exist_ok=True)
    store = Store(data_dir / "assistant.db", now=conductor._now)
    if enricher is None:
        enricher = ConductorContext(
            runs_dir=runs_dir, requests_dir=requests_dir).enrich
    if retrieval is None:
        retrieval = ConductorRetrieval(
            runs_dir=runs_dir, requests_dir=requests_dir)
    return AssistantCore(
        store,
        guard=functools.partial(conductor._assert_unwatched, runs_dir),
        attachment_saver=conductor.save_attachment,
        attachments_dir=conductor.ATTACHMENTS_DIR,
        dispatcher=dispatcher, actions=actions,
        retrieval=retrieval, enricher=enricher,
        catalog=catalog or functools.partial(
            _default_catalog, devin_argv or ["devin"]),
        settings_validator=settings_validator,
        now=conductor._now)


def _price_snapshot(uid: str, label: str | None) -> dict | None:
    """Rates + provenance for one exchange's price snapshot: the
    self-serve table entry by uid, else by the label the session
    ACTUALLY reported - never an assumed match."""
    try:
        table = conductor.price_table()
    except Exception:                       # noqa: BLE001
        return None
    ent, used = table.get(uid), uid
    if ent is None and label:
        for cand, e in table.items():
            if (e.get("label") or "").lower() == str(label).lower():
                ent, used = e, cand
                break
    if not ent:
        return None
    return {"in": ent.get("in"), "cached": ent.get("cr"),
            "out": ent.get("out"),
            "fetched_at": conductor._PRICE_CACHE.get("fetched"),
            "source": conductor._PRICE_CACHE.get("source") or "docs"}


def _pricing_meta() -> dict:
    """Provenance of the rate table itself, for the settings modal's
    'where do these prices come from' line - the same snapshot rows
    exchanges store per-record."""
    try:
        cache = conductor._PRICE_CACHE or {}
    except Exception:                       # noqa: BLE001
        cache = {}
    return {"currency": "USD", "unit": "per 1M tokens",
            "tier": "TEAMS_TIER_PRO",
            "fetched_at": cache.get("fetched"),
            "source": cache.get("source")
            or "docs.devin.ai modelCostData (TEAMS_TIER_PRO)"}


def _seed_defaults(core: AssistantCore) -> None:
    """The product's initial preference (verified gpt-6-luna, low
    thinking, fast). A PREFERENCE, not an availability claim - dispatch
    re-validates against the live catalog and fails honestly when the
    family is unavailable. Written only on first open; existing stored
    values are never overwritten."""
    if core.store.get_setting("default.model"):
        return
    for k, v in modelpick.DEFAULT_SETTINGS.items():
        core.store.set_setting(f"default.{k}", v)


def open_runtime(*, data_dir: Path = ASSISTANT_DIR,
                 runs_dir: Path = conductor.RUNS_DIR,
                 requests_dir: Path = conductor.REQUESTS_DIR,
                 manifests_dir: Path = conductor.MANIFESTS_DIR,
                 trash_dir: Path | None = None, projects=None,
                 devin_argv=None, actions=None, power=None,
                 provider=None, reconcile=True) -> tuple:
    """The full host stack: core + provider + dispatcher + actions,
    wired together. Returns (core, runtime). `reconcile` re-owns inflight
    exchanges after a serve restart; tests inject a fake provider and
    reconcile=False. `power` is the optional serve_power seam (serve
    passes the bound server; tests/standalone leave it unwired)."""
    argv = list(devin_argv or default_devin_argv())
    data_dir = Path(data_dir)
    workspace = data_dir / "workspace"
    spool = data_dir / "spool"
    if actions is None:
        from assistant_host.actions import ConductorActions
        actions = ConductorActions(
            runs_dir=runs_dir, requests_dir=requests_dir,
            manifests_dir=manifests_dir, trash_dir=trash_dir,
            devin_argv=argv, power=power)
    provider = provider or DevinPrintProvider(
        argv, env=conductor.clean_devin_env,
        spawn_kwargs=conductor.hidden_subprocess_kwargs(),
        kill=conductor.kill_tree, alive=conductor.pid_alive)

    def options():
        return conductor.model_options(argv)

    def resolve(snap, attachments_present=False):
        return modelpick.resolve_settings(
            snap, options() or {},
            attachments_present=attachments_present)

    runtime = AssistantRuntime(
        None, provider, workspace=workspace, spool_dir=spool,
        resolve=resolve, price_for=_price_snapshot,
        preamble=static_preamble(), now=conductor._now)
    core = open_core(
        data_dir=data_dir, runs_dir=runs_dir, requests_dir=requests_dir,
        devin_argv=argv, dispatcher=runtime, actions=actions,
        settings_validator=modelpick.make_validator(options),
        catalog=lambda: {
            **modelpick.annotate_catalog(
                _default_catalog(argv), options() or {}, core.defaults()),
            "pricing": _pricing_meta()})
    runtime.bind(core)
    runtime._retrieval = runtime._retrieval or core.retrieval
    _seed_defaults(core)
    if reconcile:
        runtime.reconcile()
    return core, runtime


def _default_catalog(devin_argv) -> list[dict]:
    """model_options() families + price_table() rates merged - the data
    the settings modal shows before a model is picked. Failures degrade
    to an empty list, never a 500."""
    try:
        table = conductor.price_table()
    except Exception:                      # noqa: BLE001 - pricing optional
        table = {}
    try:
        options = conductor.model_options(devin_argv)
    except Exception:                      # noqa: BLE001 - catalog optional
        options = {}
    out = []
    for fam, suffixes in (options or {}).items():
        for suffix in suffixes or [""]:
            uid = f"{fam}-{suffix}" if suffix else fam
            ent = table.get(uid) or table.get(fam) or {}
            out.append({"uid": uid, "family": fam,
                        "label": ent.get("label") or uid,
                        "in": ent.get("in"), "cached": ent.get("cr"),
                        "out": ent.get("out")})
    return out
