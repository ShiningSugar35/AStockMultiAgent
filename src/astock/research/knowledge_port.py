"""Runtime-facing port for admitted knowledge Skills.

The research runtime depends only on this Protocol and versioned schemas.  It must
never import knowledge repositories or query knowledge tables directly.
"""

from __future__ import annotations

from typing import Protocol

from astock.schemas.knowledge_completion import (
    KnowledgeProviderStatus,
    KnowledgeSkillQuery,
    KnowledgeSkillSelection,
)


class KnowledgeSkillProvider(Protocol):
    """Narrow, versioned boundary between research orchestration and knowledge storage."""

    def status(self, run_id: str) -> KnowledgeProviderStatus:
        """Return whether an immutable admitted registry is usable."""
        ...

    def select(self, run_id: str, query: KnowledgeSkillQuery) -> KnowledgeSkillSelection:
        """Return a bounded top-k summary without exposing repository internals."""
        ...


__all__ = ["KnowledgeSkillProvider"]
