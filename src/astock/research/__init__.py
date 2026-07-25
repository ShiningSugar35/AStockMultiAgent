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
from astock.research.evidence_task import (
    EvidenceCollectionTaskExecution,
    EvidenceCollectionTaskService,
)
from astock.research.evidence_run import (
    EvidenceCollectionRunExecution,
    EvidenceCollectionRunService,
)
from astock.research.evidence_pack import (
    EvidencePackExecution,
    EvidencePackService,
)
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
    "ResearchCoreService",
    "ResearchDiagnosticsService",
    "ResearchMemoExecution",
    "ResearchRequestExecution",
    "ResearchRequestService",
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
