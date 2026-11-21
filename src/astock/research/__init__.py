"""Frozen-evidence research exports, imported only when requested.

Lightweight process workers must not initialize the research CLI and numerical
stack merely because Python imports their parent package.
"""

from __future__ import annotations

from importlib import import_module
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from astock.research.config import (
        load_position_lifecycle_config,
        load_research_core_config,
        load_research_diagnostic_config,
        load_research_skill_registry,
    )
    from astock.research.diagnostics import (
        DiagnosticExecution,
        ResearchDiagnosticsService,
        ResearchMemoExecution,
    )
    from astock.research.evidence_pack import (
        EvidencePackExecution,
        EvidencePackService,
    )
    from astock.research.evidence_run import (
        EvidenceCollectionRunExecution,
        EvidenceCollectionRunService,
    )
    from astock.research.evidence_task import (
        EvidenceCollectionTaskExecution,
        EvidenceCollectionTaskService,
    )
    from astock.research.formal_preparation import (
        FormalResearchPreparationService,
        ResearchPreparationExecution,
        ResearchPreparationRejectedError,
    )
    from astock.research.institutional import InstitutionalResearchService
    from astock.research.knowledge_port import KnowledgeSkillProvider
    from astock.research.lifecycle import (
        HoldingReviewExecution,
        PositionLifecycleService,
        PositionPlanExecution,
    )
    from astock.research.lifecycle_repository import LifecycleRepository
    from astock.research.open_source_audit import (
        load_local_adaptation_release,
        load_open_source_audit,
        validate_registry_open_source_audits,
        verify_open_source_tree,
    )
    from astock.research.phase4 import Phase4ChainService
    from astock.research.repository import ResearchRepository
    from astock.research.request import ResearchRequestExecution, ResearchRequestService
    from astock.research.runtime import ResearchRunService
    from astock.research.runtime_cli import register_research_runtime_commands
    from astock.research.runtime_readiness import ResearchRuntimeReadinessService
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
    "EvidenceCollectionTaskExecution",
    "EvidenceCollectionTaskService",
    "EvidenceCollectionRunExecution",
    "EvidenceCollectionRunService",
    "EvidencePackExecution",
    "EvidencePackService",
    "FormalResearchPreparationService",
    "ResearchPreparationExecution",
    "ResearchPreparationRejectedError",
    "ResearchCoreService",
    "ResearchDiagnosticsService",
    "InstitutionalResearchService",
    "ResearchMemoExecution",
    "ResearchRequestExecution",
    "ResearchRequestService",
    "KnowledgeSkillProvider",
    "ResearchRunService",
    "ResearchRuntimeReadinessService",
    "register_research_runtime_commands",
    "HoldingReviewExecution",
    "LifecycleRepository",
    "PositionLifecycleService",
    "PositionPlanExecution",
    "Phase4ChainService",
    "ResearchRepository",
    "ResearchSkillService",
    "SkillRegistryExecution",
    "SpecialistDeltaExecution",
    "SpecialistRouteExecution",
    "load_local_adaptation_release",
    "load_open_source_audit",
    "load_research_core_config",
    "load_research_diagnostic_config",
    "load_research_skill_registry",
    "load_position_lifecycle_config",
    "validate_registry_open_source_audits",
    "verify_open_source_tree",
]

_EXPORT_MODULES = {
    "load_position_lifecycle_config": "config",
    "load_research_core_config": "config",
    "load_research_diagnostic_config": "config",
    "load_research_skill_registry": "config",
    "DiagnosticExecution": "diagnostics",
    "ResearchDiagnosticsService": "diagnostics",
    "ResearchMemoExecution": "diagnostics",
    "EvidencePackExecution": "evidence_pack",
    "EvidencePackService": "evidence_pack",
    "EvidenceCollectionRunExecution": "evidence_run",
    "EvidenceCollectionRunService": "evidence_run",
    "EvidenceCollectionTaskExecution": "evidence_task",
    "EvidenceCollectionTaskService": "evidence_task",
    "FormalResearchPreparationService": "formal_preparation",
    "ResearchPreparationExecution": "formal_preparation",
    "ResearchPreparationRejectedError": "formal_preparation",
    "InstitutionalResearchService": "institutional",
    "KnowledgeSkillProvider": "knowledge_port",
    "HoldingReviewExecution": "lifecycle",
    "PositionLifecycleService": "lifecycle",
    "PositionPlanExecution": "lifecycle",
    "LifecycleRepository": "lifecycle_repository",
    "load_local_adaptation_release": "open_source_audit",
    "load_open_source_audit": "open_source_audit",
    "validate_registry_open_source_audits": "open_source_audit",
    "verify_open_source_tree": "open_source_audit",
    "Phase4ChainService": "phase4",
    "ResearchRepository": "repository",
    "ResearchRequestExecution": "request",
    "ResearchRequestService": "request",
    "ResearchRunService": "runtime",
    "register_research_runtime_commands": "runtime_cli",
    "ResearchRuntimeReadinessService": "runtime_readiness",
    "BaseCaseExecution": "service",
    "EvidenceFreezeExecution": "service",
    "ResearchCoreService": "service",
    "ResearchSkillService": "skills",
    "SkillRegistryExecution": "skills",
    "SpecialistDeltaExecution": "skills",
    "SpecialistRouteExecution": "skills",
}


def __getattr__(name: str) -> Any:
    module = _EXPORT_MODULES.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(f".{module}", __name__), name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
