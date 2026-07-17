"""Frozen-evidence common investment research kernel."""

from astock.research.config import load_research_core_config
from astock.research.repository import ResearchRepository
from astock.research.service import (
    BaseCaseExecution,
    EvidenceFreezeExecution,
    ResearchCoreService,
)

__all__ = [
    "BaseCaseExecution",
    "EvidenceFreezeExecution",
    "ResearchCoreService",
    "ResearchRepository",
    "load_research_core_config",
]
