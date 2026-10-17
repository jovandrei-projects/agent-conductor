"""Guarded actions - the assistant's ONLY write path (DESIGN.md 6.8).

Read-only is the default. When a conversation turns actions on, the model
may still only *propose* one of the whitelisted kinds below - the proposal
is validated server-side, stored, shown as a confirmation card, and never
runs until the human confirms. Two tiers:

- ``proposed`` -> human clicks Run -> executed (most kinds)
- launch-class kinds (spawning agents/sessions, process kills, merges,
  deletes, power) need a *separate* explicit confirmation that names the
  exact target and effect - the execution call must echo the adapter's
  ``expected_ack`` string or the server refuses.

The model's tool surface stays read-only: search/fetch/calc are the only
executable calls. Anything that looks like a write inside model output or
retrieved evidence is plain text - it cannot create an action row, flip a
conversation out of read-only, or touch the dashboard.
"""
import re

KIND_RE = re.compile(r"[a-z][a-z0-9_]{1,40}\Z")

# Kinds whose execution launches work, kills processes, merges/deletes
# data or bounces the server. They carry requires_confirm AND demand the
# echoed expected_ack at execute time - the client must have shown the
# exact target + effect in a second, separate confirmation step.
LAUNCH_TIER_KINDS = frozenset({
    "launch_request", "generate_request", "answer_request",
    "discard_request", "stop_generation",
    "resume_run", "merge_run", "summarize_run", "stop_run",
    "stop_step", "batch_step_stop", "interrupt_step",
    "delete_run", "empty_trash", "serve_power",
})


class ActionSpec:
    """One whitelisted action kind's public shape.

    - ``requires_confirm``: False is for read-only-safe calls ONLY (none
      ship in v1); every mutating kind leaves it True.
    - ``confirm_text``: the effect sentence the card shows before the
      human approves.
    """

    def __init__(self, kind: str, *, requires_confirm=True,
                 confirm_text=""):
        self.kind = kind
        self.requires_confirm = bool(requires_confirm)
        self.confirm_text = confirm_text


class ActionAdapter:
    """Host-supplied bridge between stored action rows and real host ops.

    ``spec_for(kind)``      -> ActionSpec|None  (None = rejected)
    ``validate(kind, payload)`` -> sanitized payload or raise ValueError
    ``execute(action_row)`` -> result dict (persisted verbatim)
    ``expected_ack(action)``-> exact-target string launch-tier kinds must
                               see echoed at execute time; None = none
    ``describe()``          -> prompt-facing catalog text for the model
    """

    def spec_for(self, kind: str):                      # pragma: no cover
        return None

    def validate(self, kind: str, payload: dict) -> dict:  # pragma: no cover
        return dict(payload)

    def execute(self, action: dict) -> dict:            # pragma: no cover
        raise NotImplementedError

    def expected_ack(self, action: dict) -> str | None:
        return None

    def describe(self) -> str:
        return ""
