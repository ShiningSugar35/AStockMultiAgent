"""Frozen-evidence common investment research kernel."""

from astock.research.config import (
    load_research_core_config,
    load_research_diagnostic_config,
    load_research_skill_registry,
)
from astock.research.diagnostics import (
    DiagnosticExecution,
    ResearchDiagnosticsService,
    ResearchMemoExecution,
)
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
    "DiagnosticExecution",
    "ResearchCoreService",
    "ResearchDiagnosticsService",
    "ResearchMemoExecution",
    "ResearchRepository",
    "ResearchSkillService",
    "SkillRegistryExecution",
    "SpecialistDeltaExecution",
    "SpecialistRouteExecution",
    "load_research_core_config",
    "load_research_diagnostic_config",
    "load_research_skill_registry",
]
