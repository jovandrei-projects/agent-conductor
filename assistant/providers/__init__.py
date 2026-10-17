"""Provider transport contract (DESIGN.md sections 6.7/3.x).

A ProviderAdapter owns one way of getting an assistant turn answered:
spawning/resuming a provider session, polling it for progressive text,
cancelling it, and reporting usage. The `devin -p [-r]` print transport
(verified baseline, research probes) and the future ACP transport both
implement this ABC; `core` only ever sees the contract.

Usage is reported as a *delta* per exchange: the provider's ATIF export
carries cumulative session counters, so the runtime subtracts the
session's previous totals - `usage_delta` is that pure function.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class ProviderHandle:
    """One in-flight provider dispatch."""
    pid: int | None = None
    session_id: str | None = None       # provider session (resumable)
    request_id: str | None = None       # provider request, when reported
    prompt_path: str | None = None
    export_path: str | None = None


@dataclass
class PollResult:
    """Progressive state of a dispatched exchange."""
    done: bool = False
    text: str = ""                      # newest agent text so far
    usage: "UsageDelta | None" = None
    error: str | None = None
    # Raw provider-side detail (devin_print fills these; the runtime turns
    # raw_metrics' CUMULATIVE counters into per-exchange deltas):
    raw_metrics: dict | None = None     # provider's cumulative counters
    model_label: str | None = None      # model the session actually ran
    session_id: str | None = None       # provider session (discovered live)
    request_id: str | None = None       # provider request id, when reported
    exit_code: int | None = None


@dataclass
class UsageDelta:
    prompt_t: int = 0
    completion_t: int = 0
    cached_t: int = 0                   # cached tokens are a SHARE of input
    llm_steps: int = 0
    model_uid: str = ""
    model_label: str = ""
    extra: dict = field(default_factory=dict)


def usage_delta(prev: dict | None, cur: dict | None) -> UsageDelta:
    """Cumulative export counters -> this exchange's share. `prev` is the
    session's metrics snapshot taken before this dispatch (None = fresh
    session), `cur` the metrics at turn end. Counter keys follow the ATIF
    final_metrics naming."""
    cur = cur or {}
    prev = prev or {}

    def d(key):
        return max(0, int(cur.get(key) or 0) - int(prev.get(key) or 0))

    return UsageDelta(
        prompt_t=d("total_prompt_tokens"),
        completion_t=d("total_completion_tokens"),
        cached_t=d("total_cached_tokens"),
        llm_steps=d("total_steps"))


# Transport registry for additional provider adapters (the "fast"
# transport and any future ones). An adapter module registers itself with
# register_transport() at import time; the host wiring also auto-discovers
# concrete ProviderAdapter subclasses in this package when nothing was
# registered - the registry keeps that discovery name-free.
_REGISTERED_TRANSPORTS: dict = {}


def register_transport(name: str, factory) -> None:
    """Register `factory` (a class or zero-seam-matched callable) under a
    transport id such as "fast". Called by adapter modules at import; the
    host instantiates through `registered_transports()`."""
    _REGISTERED_TRANSPORTS[str(name)] = factory


def registered_transports() -> dict:
    """transport id -> adapter class/factory, copy."""
    return dict(_REGISTERED_TRANSPORTS)


class ProviderAdapter(ABC):
    """The `devin_print` adapter (assistant-runtime step) and the future
    `devin_acp` adapter both implement this."""

    @abstractmethod
    def send(self, prompt_path: str, *, session_id: str | None,
             model_uid: str, export_path: str,
             workspace) -> ProviderHandle:
        """Dispatch one turn. `session_id` resumes a session (settings are
        then the session's own - a model change must spawn fresh)."""

    @abstractmethod
    def poll(self, handle: ProviderHandle) -> PollResult:
        """Non-blocking state read: partial text, done flag, usage when
        finished, error when failed/cancelled."""

    @abstractmethod
    def cancel(self, handle: ProviderHandle) -> None:
        """Best-effort kill of the in-flight dispatch."""

    @abstractmethod
    def sessions(self, workspace) -> set[str] | None:
        """Live provider session ids for the assistant workspace, or None
        when the listing failed (callers must not read that as 'gone')."""

    def alive_pid(self, pid) -> bool:
        """Is this provider pid still running? reconcile() uses it to
        adopt in-flight dispatches orphaned by a serve restart."""
        return False

    def attach(self, exchange: dict) -> ProviderHandle:
        """Rebuild a handle for an in-flight dispatch from its persisted
        row (pid/session_id/prompt_path/export_path) - no live Popen.
        Poll then works off pid liveness + the export file."""
        return ProviderHandle(
            pid=exchange.get("pid"),
            session_id=exchange.get("provider_session_id"),
            prompt_path=exchange.get("prompt_path"),
            export_path=exchange.get("export_path"))
