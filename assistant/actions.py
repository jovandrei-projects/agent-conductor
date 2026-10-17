"""Controlled-action contracts - the audit-trail half of the read-only
gate (DESIGN.md sections 6.2/6.8).

The model never executes anything: it may emit a *proposal* as structured
JSON in its reply; the host's ActionAdapter whitelists kinds, validates
params against the real functions' schemas, and runs them through the
host's existing endpoints under the same origin/workspace guards a human's
click gets. This module owns the record shape and the proposed ->
confirmed/denied -> executed/failed state machine. `AssistantCore`
drives the transitions; the adapter (supplied by the guarded-actions step)
owns validation + execution.
"""
from abc import ABC, abstractmethod

ACTION_STATES = ("proposed", "confirmed", "denied", "executed", "failed")
KIND_RE = r"[a-z][a-z0-9_]{0,39}"   # e.g. set_step_review, create_request

# Whitelist tiers (DESIGN 6.8): mutating actions always need the confirm
# card; launch/destructive kinds additionally belong to LAUNCH_TIER so the
# UI can demand an explicit second confirmation.
LAUNCH_TIER_KINDS = frozenset({
    "launch_request", "resume_run", "merge_run", "delete_run",
    "empty_trash", "summarize_run", "discard_request", "serve_power"})


class ActionSpec:
    """The adapter's description of one whitelisted action kind."""

    def __init__(self, kind: str, *, requires_confirm: bool = True,
                 confirm_text: str = "", description: str = ""):
        self.kind = kind
        self.requires_confirm = requires_confirm
        self.confirm_text = confirm_text
        self.description = description


class ActionAdapter(ABC):
    """Host whitelist. The guarded-dashboard-actions step implements this
    against conductor's existing functions (create_request, save_request,
    set_*_review, ...). No adapter configured = no actions at all."""

    @abstractmethod
    def spec_for(self, kind: str) -> ActionSpec | None:
        """ActionSpec for a whitelisted kind, or None to refuse it."""

    @abstractmethod
    def validate(self, kind: str, payload: dict) -> dict:
        """Clean + check the proposal's params against the real function's
        schema; returns the sanitized payload or raises."""

    @abstractmethod
    def execute(self, action: dict) -> dict:
        """Run the confirmed action through the host's existing mechanism.
        `action` is the audit row (kind/payload/conversation ids). Returns
        a JSON-able result dict; raise on failure -> state 'failed'."""
