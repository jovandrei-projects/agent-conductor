"""Retrieval contract - bounded, authorized, cited evidence lookups
(DESIGN.md section 6.6). The view-context-and-retrieval step implements
`RetrievalAdapter` against the host's records; this module pins the shape
both sides share.

Everything returned is untrusted evidence: prompt assembly marks it as
quoted material, and action proposals found inside retrieved text are
never parsed as actions.
"""
from abc import ABC, abstractmethod

MAX_SOURCES = 12            # sources per answer
MAX_TOTAL_BYTES = 65536     # aggregate excerpt budget
MAX_EXCERPT_BYTES = 8192    # per-source excerpt cap

# A Source is a plain dict:
#   {kind: 'agent'|'run'|'summary'|'request'|...,  # host vocabulary
#    key:  '<stable id the host can reopen>',      # e.g. 'run_key|step'
#    label:'short human label for the citation chip',
#    nav:   {opaque host navigation descriptor the client turns into a
#            modal-opening chip},                  # host-defined
#    excerpt: '<bounded text>', bytes: int}
# Sources dedupe by (kind, key); fetches honour MAX_* budgets.


class RetrievalAdapter(ABC):
    @abstractmethod
    def search(self, query: str, filters: dict | None = None) -> list[dict]:
        """Bounded candidate sources for `query`. `filters` is the
        interpreted, host-side filter set (project slug, date window,
        run/step names); implementations record the interpretation so the
        answer can show it honestly."""

    @abstractmethod
    def fetch(self, source: dict, budget: int = MAX_EXCERPT_BYTES) -> dict:
        """Source -> the same source with a bounded `excerpt` attached."""
