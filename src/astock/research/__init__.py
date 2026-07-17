"""Frozen-evidence common investment research kernel."""

from astock.research.config import load_research_core_config, load_research_skill_registry
from astock.research.repository import ResearchRepository
from astock.research.service import (
    BaseCaseExecution,
    EvidenceFreezeExecution,
    ResearchCoreService,
)
from astock.research.skills import (
    ResearchSkillService,
    SkillRegistryExecution,
    SpecialistDeltaExecution,
    SpecialistRouteExecution,
)

__all__ = [
    "BaseCaseExecution",
    "EvidenceFreezeExecution",
    "ResearchCoreService",
    "ResearchRepository",
    "ResearchSkillService",
    "SkillRegistryExecution",
    "SpecialistDeltaExecution",
    "SpecialistRouteExecution",
    "load_research_core_config",
    "load_research_skill_registry",
]
