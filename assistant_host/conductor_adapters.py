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
"""
import functools
from pathlib import Path

import conductor
from assistant.core import AssistantCore
from assistant.store import Store

ASSISTANT_DIR = conductor.DATA_DIR / "assistant"
WORKSPACE_DIR = ASSISTANT_DIR / "workspace"
SPOOL_DIR = ASSISTANT_DIR / "spool"


def open_core(*, data_dir: Path = ASSISTANT_DIR,
              runs_dir: Path = conductor.RUNS_DIR,
              devin_argv=None, dispatcher=None, actions=None,
              catalog=None) -> AssistantCore:
    """Build AssistantCore on this project's data dir with the conductor
    seams attached. `catalog` defaults to the pricing-merged model list
    the settings modal shows."""
    data_dir = Path(data_dir)
    (data_dir / "workspace").mkdir(parents=True, exist_ok=True)
    (data_dir / "spool").mkdir(parents=True, exist_ok=True)
    store = Store(data_dir / "assistant.db", now=conductor._now)
    return AssistantCore(
        store,
        guard=functools.partial(conductor._assert_unwatched, runs_dir),
        attachment_saver=conductor.save_attachment,
        attachments_dir=conductor.ATTACHMENTS_DIR,
        dispatcher=dispatcher, actions=actions,
        catalog=catalog or functools.partial(
            _default_catalog, devin_argv or ["devin"]),
        now=conductor._now)


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
