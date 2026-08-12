"""Frozen-evidence common investment research kernel."""

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
    "load_open_source_audit",
    "load_research_core_config",
    "load_research_diagnostic_config",
    "load_research_skill_registry",
    "load_position_lifecycle_config",
    "validate_registry_open_source_audits",
    "verify_open_source_tree",
]
