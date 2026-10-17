"""Reusable dashboard assistant core - transport-agnostic conversation
engine + durable sqlite storage.

Module map (DESIGN.md section 6.1; later manifest steps fill the marked
parts):

    store.py        sqlite schema + migrations + queries      [this step]
    core.py         AssistantCore application interface       [this step]
    context.py      per-turn view-context envelopes           [this step]
    actions.py      action audit + ActionAdapter ABC          [contract]
    retrieval.py    RetrievalAdapter ABC + budgets            [contract]
    providers/      ProviderAdapter ABC + usage deltas        [contract]
    http_api.py     route table -> core calls                 [this step]
    static/         chat.js/css/md.js      [persistent-chat-interface]
    providers/devin_print.py               [assistant-runtime]

The package never imports conductor; host specifics arrive as injected
callables (see assistant_host/conductor_adapters.py for the wiring).
"""
from .core import AssistantCore, AssistantError, price_usage  # noqa: F401
from .store import Store, SCHEMA_VERSION  # noqa: F401
