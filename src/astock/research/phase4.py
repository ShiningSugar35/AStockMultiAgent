"""Read-only status and audit aggregation for the complete Phase 4 chain."""

from __future__ import annotations

from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.research.diagnostics import ResearchDiagnosticsService
from astock.research.lifecycle import PositionLifecycleService
from astock.research.repository import ResearchRepository
from astock.research.service import ResearchCoreService
from astock.research.skills import ResearchSkillService
from astock.schemas import (
    PositionLifecycleConfig,
    ResearchCoreConfig,
    ResearchDiagnosticConfig,
    ResearchSkillRegistry,
)


class Phase4ChainService:
    def __init__(
        self,
        state: StateStore,
        object_store: ObjectStore,
        core_config: ResearchCoreConfig,
        skill_registry: ResearchSkillRegistry,
        diagnostic_config: ResearchDiagnosticConfig,
        lifecycle_config: PositionLifecycleConfig,
    ) -> None:
        self.repository = ResearchRepository(state, object_store)
        self.core = ResearchCoreService(state, object_store, core_config)
        self.skills = ResearchSkillService(state, object_store, skill_registry)
        self.diagnostics = ResearchDiagnosticsService(
            state,
            object_store,
            skill_registry,
            diagnostic_config,
        )
        self.lifecycle = PositionLifecycleService(
            state,
            object_store,
            lifecycle_config,
        )

    def status(
        self,
        company_id: str,
        *,
        position_id: str | None = None,
    ) -> dict[str, object]:
        base = self.repository.latest_base_case_summary(company_id)
        if base is None:
            return {
                "status": "NOT_RUN",
                "company_id": company_id,
                "position_id": position_id,
            }
        base_case_id = str(base["base_case_id"])
        specialist = self.skills.status(base_case_id)
        diagnostics = self.diagnostics.status(base_case_id)
        lifecycle = self.lifecycle.status(position_id) if position_id else None
        missing = [
            name
            for name, stage in (
                ("SPECIALIST", specialist),
                ("DIAGNOSTIC", diagnostics),
                ("LIFECYCLE", lifecycle),
            )
            if stage is not None and stage.get("status") == "NOT_RUN"
        ]
        if lifecycle is not None and lifecycle.get("status") != "NOT_RUN":
            lifecycle_plan = lifecycle.get("plan")
            if (
                not isinstance(lifecycle_plan, dict)
                or lifecycle_plan.get("company_id") != company_id
            ):
                missing.append("POSITION_COMPANY_MISMATCH")
            if lifecycle.get("latest_review") is None:
                missing.append("HOLDING_REVIEW")
        return {
            "status": "AVAILABLE" if not missing else "PARTIAL",
            "company_id": company_id,
            "position_id": position_id,
            "base_case": base,
            "specialist": specialist,
            "diagnostics": diagnostics,
            "lifecycle": lifecycle,
            "missing_stage_codes": [
                item if item.endswith("MISMATCH") else f"{item}_NOT_RUN"
                for item in missing
            ],
        }

    def audit(
        self,
        company_id: str,
        *,
        position_id: str | None = None,
    ) -> dict[str, object]:
        base = self.repository.latest_base_case_summary(company_id)
        core_audit = self.core.audit(company_id)
        if base is None:
            return {
                "status": "NOT_RUN",
                "company_id": company_id,
                "position_id": position_id,
                "stage_statuses": {"core": core_audit["status"]},
                "finding_codes": ["CORE_NOT_RUN"],
            }
        base_case_id = str(base["base_case_id"])
        specialist_audit = self.skills.audit(base_case_id)
        diagnostic_status = self.diagnostics.status(base_case_id)
        diagnostic_audit = self.diagnostics.audit(base_case_id)
        lifecycle_audit = self.lifecycle.audit(position_id) if position_id else None
        lifecycle_status = self.lifecycle.status(position_id) if position_id else None

        finding_codes: set[str] = set()
        for stage, report in (
            ("CORE", core_audit),
            ("SPECIALIST", specialist_audit),
            ("DIAGNOSTIC", diagnostic_audit),
            ("LIFECYCLE", lifecycle_audit),
        ):
            if report is None:
                continue
            status = str(report.get("status"))
            if status == "NOT_RUN":
                finding_codes.add(f"{stage}_NOT_RUN")
            elif status != "PASS":
                codes = report.get("finding_codes")
                if isinstance(codes, list) and codes:
                    finding_codes.update(f"{stage}:{code}" for code in codes)
                else:
                    finding_codes.add(f"{stage}_{status}")
        if diagnostic_status.get("status") == "NOT_RUN":
            finding_codes.add("DIAGNOSTIC_NOT_RUN")
        elif diagnostic_status.get("memo") is None:
            finding_codes.add("RESEARCH_MEMO_NOT_RUN")
        if lifecycle_status is not None and lifecycle_status.get("status") != "NOT_RUN":
            lifecycle_plan = lifecycle_status.get("plan")
            if (
                not isinstance(lifecycle_plan, dict)
                or lifecycle_plan.get("company_id") != company_id
            ):
                finding_codes.add("POSITION_COMPANY_MISMATCH")
            if lifecycle_status.get("latest_review") is None:
                finding_codes.add("HOLDING_REVIEW_NOT_RUN")

        stage_statuses = {
            "core": core_audit["status"],
            "specialist": specialist_audit["status"],
            "diagnostic": diagnostic_audit["status"],
            "lifecycle": lifecycle_audit["status"] if lifecycle_audit else None,
        }
        return {
            "status": "PASS" if not finding_codes else "PARTIAL",
            "company_id": company_id,
            "position_id": position_id,
            "base_case_id": base_case_id,
            "stage_statuses": stage_statuses,
            "finding_codes": sorted(finding_codes),
            "core": core_audit,
            "specialist": specialist_audit,
            "diagnostic": diagnostic_audit,
            "lifecycle": lifecycle_audit,
        }


__all__ = ["Phase4ChainService"]
