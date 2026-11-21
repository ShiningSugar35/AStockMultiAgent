"""Deterministic orchestration for full-market, team-style investment research."""

from __future__ import annotations

import ctypes
import os
import platform
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import yaml

from astock.core.artifact_reading import decode_registered_artifact
from astock.core.errors import StorageError
from astock.core.hashing import content_hash
from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.evidence.repository import EvidenceRepository
from astock.research.policy import load_default_current_research_policy
from astock.schemas.financial import FinancialCoverageStatus, FinancialIntegrityEvidencePack
from astock.schemas.full_research import (
    ChallengerAssessment,
    CompanyFundamentalSnapshot,
    FinancialQualityAssessment,
    GovernanceAssessment,
    IndustryResearchOutcome,
    MacroResearchOutcome,
    NewsEventResearchPack,
    QuantFactorResearchPack,
)
from astock.schemas.research_acquisition import (
    CurrentResearchAcquisitionReport,
    CurrentResearchAcquisitionStatus,
)
from astock.schemas.research_seeds import ResearchSeedReport
from astock.schemas.research_team import (
    FullResearchInputReadinessReport,
    FullResearchInputReadinessRequest,
    FullResearchInputReadinessStatus,
    HardwareBudget,
    MarketContextPack,
    ResearchCoverageReport,
    ResearchCoverageRequest,
    ResearchCoverageScore,
    ResearchExecutionBackend,
    ResearchResourceClass,
    ResearchRoleOutput,
    ResearchRoleResult,
    ResearchTaskRole,
    ResearchTeamDepth,
    ResearchTeamPlan,
    ResearchTeamScope,
    ResearchTeamTask,
    ResearchTeamTaskState,
)
from astock.schemas.runs import RunStatus
from astock.schemas.technical import TechnicalIndicatorSnapshot


@dataclass(frozen=True, slots=True)
class ResearchTeamPolicy:
    policy_version: str
    default_backend: ResearchExecutionBackend
    automatic_resolution_budget_seconds: int
    background_service_required: bool
    on_demand_only: bool
    required_checks: tuple[str, ...]
    company_required_checks: tuple[str, ...]
    hardware: dict[ResearchResourceClass, dict[str, int]]
    manual_candidate_fallback_allowed: bool
    skill_share_gate_enabled: bool
    reserve_blind_market_tranche: bool
    max_breadth_challenger_seeds: int
    max_long_horizon_value_seeds: int
    breadth_min_market_score_ratio: float
    breadth_max_boards_per_domain: int
    expert_overlay_max_priority_bonus: float
    universal_coverage_minimum: float
    industry_coverage_minimum: float
    evidence_coverage_minimum: float


def load_research_team_policy(path: Path) -> ResearchTeamPolicy:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or raw.get("schema_version") != "research-team-policy-v1":
        raise ValueError("Unsupported research-team policy")
    execution = raw.get("execution")
    acquisition = raw.get("acquisition")
    discovery = raw.get("discovery")
    coverage = raw.get("coverage")
    gate = raw.get("recommendation_gate")
    safety = raw.get("safety")
    if not all(
        isinstance(item, dict)
        for item in (execution, acquisition, discovery, coverage, gate, safety)
    ):
        raise ValueError("research-team policy sections are invalid")
    assert isinstance(execution, dict)
    assert isinstance(acquisition, dict)
    assert isinstance(discovery, dict)
    assert isinstance(coverage, dict)
    assert isinstance(gate, dict)
    assert isinstance(safety, dict)

    def validated_checks(key: str) -> tuple[str, ...]:
        checks = tuple(str(item) for item in gate.get(key, []))
        if not checks or len(checks) != len(set(checks)):
            raise ValueError(f"recommendation gate {key} must be non-empty and unique")
        if "TEAM_DAG_COMPLETE" not in checks:
            checks = (*checks, "TEAM_DAG_COMPLETE")
        return checks

    required_checks = validated_checks("required_checks")
    company_required_checks = validated_checks("company_required_checks")

    resource_fields = {
        "provider_workers": "market_fetch_workers_by_resource",
        "agent_workers": "agent_workers_by_resource",
        "duckdb_threads": "duckdb_threads_by_resource",
        "max_parallel_companies": "max_parallel_companies_by_resource",
        "max_deep_candidates": "max_deep_candidates_by_resource",
    }
    hardware: dict[ResearchResourceClass, dict[str, int]] = {
        item: {} for item in ResearchResourceClass
    }
    for output_name, source_name in resource_fields.items():
        mapping = acquisition.get(source_name)
        if not isinstance(mapping, dict):
            raise ValueError(f"missing acquisition resource mapping: {source_name}")
        for resource_class in ResearchResourceClass:
            value = int(mapping.get(resource_class.value, 0))
            if value <= 0:
                raise ValueError(f"invalid {source_name} for {resource_class.value}")
            hardware[resource_class][output_name] = value

    if "automatic_resolution_budget_seconds" in execution:
        raise ValueError(
            "research-team policy must not duplicate "
            "the current-research automatic resolution budget"
        )
    current_research_policy = load_default_current_research_policy(path.parent.parent)
    canonical_budget = current_research_policy.automatic_resolution_budget_seconds
    policy = ResearchTeamPolicy(
        policy_version=str(raw.get("policy_version") or ""),
        default_backend=ResearchExecutionBackend(str(execution.get("default_backend"))),
        automatic_resolution_budget_seconds=canonical_budget,
        background_service_required=bool(execution.get("background_service_required")),
        on_demand_only=bool(execution.get("on_demand_only")),
        required_checks=required_checks,
        company_required_checks=company_required_checks,
        hardware=hardware,
        manual_candidate_fallback_allowed=bool(safety.get("manual_candidate_fallback_allowed")),
        skill_share_gate_enabled=bool(discovery.get("skill_share_gate_enabled")),
        reserve_blind_market_tranche=bool(discovery.get("reserve_blind_market_tranche")),
        max_breadth_challenger_seeds=int(discovery.get("max_breadth_challenger_seeds", 0)),
        max_long_horizon_value_seeds=int(discovery.get("max_long_horizon_value_seeds", 0)),
        breadth_min_market_score_ratio=float(discovery.get("breadth_min_market_score_ratio", 0.0)),
        breadth_max_boards_per_domain=int(discovery.get("breadth_max_boards_per_domain", 1)),
        expert_overlay_max_priority_bonus=float(
            discovery.get("expert_overlay_max_priority_bonus", 0.0)
        ),
        universal_coverage_minimum=float(coverage.get("universal_minimum", 0.0)),
        industry_coverage_minimum=float(coverage.get("industry_minimum", 0.0)),
        evidence_coverage_minimum=float(coverage.get("evidence_minimum", 0.0)),
    )
    if not policy.policy_version:
        raise ValueError("research-team policy_version is required")
    if policy.automatic_resolution_budget_seconds <= 0:
        raise ValueError("automatic resolution budget must be positive")
    if policy.background_service_required:
        raise ValueError("current research-team policy must not require a background service")
    if not policy.on_demand_only:
        raise ValueError("current research-team policy must be on-demand")
    if policy.manual_candidate_fallback_allowed:
        raise ValueError("manual candidate fallback must be disabled")
    if policy.skill_share_gate_enabled:
        raise ValueError("author-relative Skill share gate has been retired")
    if not policy.reserve_blind_market_tranche:
        raise ValueError("blind market tranche reservation must remain enabled")
    if not 0 <= policy.max_breadth_challenger_seeds <= 20:
        raise ValueError("breadth challenger seed budget is outside governance bounds")
    if not 0 <= policy.max_long_horizon_value_seeds <= 30:
        raise ValueError("long-horizon value seed budget is outside governance bounds")
    if not 0 <= policy.breadth_min_market_score_ratio <= 1:
        raise ValueError("breadth challenger market-score ratio is outside 0..1")
    if not 1 <= policy.breadth_max_boards_per_domain <= 8:
        raise ValueError("breadth challenger board bound is outside governance bounds")
    if not 0 <= policy.expert_overlay_max_priority_bonus <= 0.25:
        raise ValueError("expert overlay bonus is outside the governance bound")
    if coverage.get("private_skill_gates_recommendation") is not False:
        raise ValueError("private Skill coverage must remain edge-only")
    for value in (
        policy.universal_coverage_minimum,
        policy.industry_coverage_minimum,
        policy.evidence_coverage_minimum,
    ):
        if not 0 <= value <= 100:
            raise ValueError("research coverage minimum is outside 0..100")
    return policy


def _physical_memory_gib() -> float | None:
    if platform.system() == "Windows":

        class MemoryStatusEx(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        status = MemoryStatusEx()
        status.dwLength = ctypes.sizeof(MemoryStatusEx)
        try:
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            if not kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                return None
            return status.ullTotalPhys / (1024**3)
        except (AttributeError, OSError):
            return None
    try:
        sysconf_object = getattr(os, "sysconf", None)
        if not callable(sysconf_object):
            return None
        sysconf = cast(Any, sysconf_object)
        pages = int(sysconf("SC_PHYS_PAGES"))
        page_size = int(sysconf("SC_PAGE_SIZE"))
        if pages <= 0 or page_size <= 0:
            return None
        return pages * page_size / (1024**3)
    except (AttributeError, OSError, ValueError):
        return None


def detect_hardware_budget(
    policy: ResearchTeamPolicy,
    *,
    cpu_count: int | None = None,
    memory_gib: float | None = None,
    created_at: datetime | None = None,
) -> HardwareBudget:
    cpu = max(1, int(cpu_count or os.cpu_count() or 1))
    memory = memory_gib if memory_gib is not None else _physical_memory_gib()
    if cpu <= 4 or (memory is not None and memory < 16):
        resource_class = ResearchResourceClass.LOW_RESOURCE
    elif cpu >= 16 and memory is not None and memory >= 32:
        resource_class = ResearchResourceClass.HIGH_RESOURCE
    else:
        resource_class = ResearchResourceClass.STANDARD
    selected = policy.hardware[resource_class]
    return HardwareBudget(
        resource_class=resource_class,
        created_at=created_at or datetime.now(UTC),
        cpu_count=cpu,
        memory_gib=round(memory, 2) if memory is not None else None,
        provider_workers=selected["provider_workers"],
        agent_workers=selected["agent_workers"],
        duckdb_threads=selected["duckdb_threads"],
        max_parallel_companies=selected["max_parallel_companies"],
        max_deep_candidates=selected["max_deep_candidates"],
    )


class ResearchTeamService:
    """Create durable research DAGs and enforce fail-closed recommendation authority."""

    def __init__(
        self,
        *,
        project_root: Path,
        state: StateStore,
        objects: ObjectStore,
        policy: ResearchTeamPolicy | None = None,
    ) -> None:
        self.project_root = project_root
        self.state = state
        self.objects = objects
        self.policy = policy or load_research_team_policy(
            project_root / "configs" / "research_team.yaml"
        )
        full_research_policy = yaml.safe_load(
            (project_root / "configs" / "full_research_recommendation.yaml").read_text(
                encoding="utf-8"
            )
        )
        if not isinstance(full_research_policy, dict):
            raise ValueError("full research recommendation policy must be a mapping")
        self.full_research_news_windows = tuple(
            int(value) for value in full_research_policy["news_windows_days"]
        )
        self.full_research_news_categories = tuple(
            str(value) for value in full_research_policy["news_categories"]
        )

    def runtime_profile(self) -> dict[str, object]:
        budget = detect_hardware_budget(self.policy)
        return {
            "schema_version": "research-runtime-profile-v1",
            "policy_version": self.policy.policy_version,
            "default_backend": self.policy.default_backend.value,
            "on_demand_only": self.policy.on_demand_only,
            "background_service_required": self.policy.background_service_required,
            "automatic_resolution_budget_seconds": (
                self.policy.automatic_resolution_budget_seconds
            ),
            "hardware_budget": budget.model_dump(mode="json"),
            "manual_candidate_fallback_allowed": False,
            "skill_share_gate_enabled": False,
        }

    def create_full_market_plan(
        self,
        *,
        as_of: datetime | None = None,
        backend: ResearchExecutionBackend | None = None,
        depth: ResearchTeamDepth = ResearchTeamDepth.INSTITUTIONAL,
        cpu_count: int | None = None,
        memory_gib: float | None = None,
    ) -> ResearchTeamPlan:
        timestamp = as_of or datetime.now(UTC)
        selected_backend = backend or self.policy.default_backend
        budget = detect_hardware_budget(
            self.policy,
            cpu_count=cpu_count,
            memory_gib=memory_gib,
            created_at=timestamp,
        )
        tasks = self._full_market_tasks(timestamp)
        self._validate_readiness_ownership(tasks, self.policy.required_checks)
        identity = {
            "scope": ResearchTeamScope.FULL_MARKET.value,
            "depth": depth.value,
            "backend": selected_backend.value,
            "as_of": timestamp.isoformat(),
            "policy_version": self.policy.policy_version,
            "hardware_budget": budget.model_dump(mode="json"),
            "tasks": [item.model_dump(mode="json") for item in tasks],
        }
        plan = ResearchTeamPlan(
            plan_id="research-team:" + content_hash(identity),
            scope=ResearchTeamScope.FULL_MARKET,
            depth=depth,
            backend=selected_backend,
            as_of=timestamp,
            policy_version=self.policy.policy_version,
            hardware_budget=budget,
            tasks=tasks,
            automatic_resolution_budget_seconds=self.policy.automatic_resolution_budget_seconds,
            created_at=timestamp,
        )
        self._persist_plan(plan)
        return plan

    def create_company_plan(
        self,
        *,
        company_id: str,
        acquisition_report_artifact_id: str,
        as_of: datetime | None = None,
        backend: ResearchExecutionBackend | None = None,
        depth: ResearchTeamDepth = ResearchTeamDepth.INSTITUTIONAL,
        cpu_count: int | None = None,
        memory_gib: float | None = None,
    ) -> ResearchTeamPlan:
        report_record = self.state.artifact_record(acquisition_report_artifact_id)
        if (
            report_record is None
            or str(report_record["type"]) != "CurrentResearchAcquisitionReport"
            or not self.objects.verify(str(report_record["object_hash"]))
        ):
            raise ValueError("company research requires an available acquisition report artifact")
        report = CurrentResearchAcquisitionReport.model_validate_json(
            self.objects.get_bytes(str(report_record["object_hash"]))
        )
        if report.company_id != company_id:
            raise ValueError("company research acquisition report belongs to a different company")
        if report.external_research_needs or report.manual_actions:
            raise ValueError("company research cannot start before acquisition gaps are resolved")
        if report.status not in {
            CurrentResearchAcquisitionStatus.READY,
            CurrentResearchAcquisitionStatus.DEGRADED,
        }:
            raise ValueError("company research acquisition report is not ready for team research")

        timestamp = as_of or datetime.now(UTC)
        selected_backend = backend or self.policy.default_backend
        budget = detect_hardware_budget(
            self.policy,
            cpu_count=cpu_count,
            memory_gib=memory_gib,
            created_at=timestamp,
        )
        tasks = self._company_tasks(timestamp)
        self._validate_readiness_ownership(tasks, self.policy.company_required_checks)
        identity = {
            "scope": ResearchTeamScope.COMPANY.value,
            "company_id": company_id,
            "acquisition_report_artifact_id": acquisition_report_artifact_id,
            "acquisition_report_object_hash": str(report_record["object_hash"]),
            "depth": depth.value,
            "backend": selected_backend.value,
            "as_of": timestamp.isoformat(),
            "policy_version": self.policy.policy_version,
            "hardware_budget": budget.model_dump(mode="json"),
            "tasks": [item.model_dump(mode="json") for item in tasks],
        }
        plan = ResearchTeamPlan(
            plan_id="research-team:" + content_hash(identity),
            scope=ResearchTeamScope.COMPANY,
            company_id=company_id,
            acquisition_report_artifact_id=acquisition_report_artifact_id,
            depth=depth,
            backend=selected_backend,
            as_of=timestamp,
            policy_version=self.policy.policy_version,
            hardware_budget=budget,
            tasks=tasks,
            automatic_resolution_budget_seconds=self.policy.automatic_resolution_budget_seconds,
            created_at=timestamp,
        )
        self._persist_plan(plan)
        return plan

    def get_plan(self, plan_id: str) -> ResearchTeamPlan | None:
        artifact_id = f"ResearchTeamPlan:{plan_id}"
        record = self.state.artifact_record(artifact_id)
        if record is None or str(record["type"]) != "ResearchTeamPlan":
            return None
        object_hash = str(record["object_hash"])
        if not self.objects.verify(object_hash):
            return None
        return ResearchTeamPlan.model_validate_json(self.objects.get_bytes(object_hash))

    def register_role_output(self, output: ResearchRoleOutput) -> dict[str, object]:
        plan = self.get_plan(output.plan_id)
        if plan is None:
            raise ValueError("unknown research-team plan")
        task = next((item for item in plan.tasks if item.task_id == output.task_id), None)
        if task is None:
            raise ValueError("role output references unknown task")
        if task.role is ResearchTaskRole.RECOMMENDATION_GATE:
            raise ValueError("deterministic recommendation gate owns its output")
        if output.output_contract != task.output_contract:
            raise ValueError("role output contract does not match the planned task")
        if set(output.readiness_check_results) != set(task.readiness_checks):
            raise ValueError("role output readiness checks do not match the planned task")
        if task.required_for_recommendation and not output.member_artifact_ids:
            raise ValueError(
                "recommendation-required role output requires at least one "
                "registered member artifact"
            )

        output, member_hashes, technical_observations = self._resolve_role_output_members(
            output, task, plan,
        )
        if task.role is ResearchTaskRole.UNIVERSE:
            universe_is_full = self._universe_output_has_full_coverage(output)
            if output.readiness_check_results.get("UNIVERSE_COVERAGE") is not universe_is_full:
                raise ValueError(
                    "UNIVERSE_COVERAGE must be derived from one frozen ResearchSeedReport"
                )
        if task.role is ResearchTaskRole.FINANCIAL_INTEGRITY:
            financial_is_formal = self._financial_output_has_formal_coverage(output)
            if output.readiness_check_results.get("FINANCIAL_INTEGRITY") is not financial_is_formal:
                raise ValueError(
                    "FINANCIAL_INTEGRITY must be derived from COMPLETE SUCCEEDED financial packs "
                    "plus typed accounting-quality evidence"
                )
        typed_checks = self._typed_full_research_role_checks(task, output)
        if typed_checks is not None:
            for check, expected in typed_checks.items():
                if output.readiness_check_results.get(check) is not expected:
                    raise ValueError(
                        f"{check} must be derived from typed Full Research member artifacts"
                    )
        if (
            task.role is ResearchTaskRole.VALUATION
            and output.readiness_check_results.get("VALUATION") is True
            and self._derived_readiness_checks(plan).get("FINANCIAL_INTEGRITY") is not True
        ):
            raise ValueError("precise VALUATION requires COMPLETE SUCCEEDED financial packs")

        evidence_hashes: list[str] = []
        evidence_repository = EvidenceRepository(self.state)
        for evidence_id in output.evidence_ids:
            evidence = evidence_repository.get_evidence(evidence_id)
            if evidence is None:
                raise ValueError("role output references unknown Evidence")
            if not self.objects.verify(evidence.excerpt_object_sha256):
                raise ValueError("role output Evidence object is unavailable")
            evidence_hashes.append(evidence.excerpt_object_sha256)

        ref = self.objects.put_json(output.model_dump(mode="json"))
        artifact_id = "ResearchRoleOutput:" + content_hash(
            {
                "plan_id": output.plan_id,
                "task_id": output.task_id,
                "output_contract": output.output_contract,
                "object_hash": ref.sha256,
            }
        )
        plan_record = self.state.artifact_record(f"ResearchTeamPlan:{plan.plan_id}")
        input_hashes = sorted(
            {
                *member_hashes,
                *evidence_hashes,
                str(plan_record["object_hash"]) if plan_record is not None else "",
            }
            - {""}
        )
        self.state.register_artifact(
            artifact_id=artifact_id,
            artifact_type="ResearchRoleOutput",
            schema_version=output.schema_version,
            object_hash=ref.sha256,
            input_hashes=input_hashes,
        )
        return {
            "status": "REGISTERED",
            "plan_id": plan.plan_id,
            "task_id": task.task_id,
            "role": task.role.value,
            "output_contract": task.output_contract,
            "technical_observations": technical_observations,
            "artifact_id": artifact_id,
            "object_hash": ref.sha256,
        }

    def _resolve_role_output_members(
        self, output: ResearchRoleOutput, task: ResearchTeamTask, plan: ResearchTeamPlan,
    ) -> tuple[ResearchRoleOutput, list[str], list[dict[str, str]]]:
        """Resolve immutable member lineage and optional canonical technical enrichment."""
        member_hashes: list[str] = []
        member_ids: list[str] = []
        technical_observations: list[dict[str, str]] = []
        for member_artifact_id in output.member_artifact_ids:
            record = self.state.artifact_record(member_artifact_id)
            if record is None or not self.objects.verify(str(record["object_hash"])):
                raise ValueError("role output references an unavailable member artifact")
            member_hash = str(record["object_hash"])
            if (
                task.role is ResearchTaskRole.MARKET_CONTEXT
                and record["type"] == "MarketContextPack"
            ):
                context = MarketContextPack.model_validate_json(
                    self.objects.get_bytes(member_hash)
                )
                if plan.company_id is not None and context.company_id != plan.company_id:
                    raise ValueError("market-context company mismatch")
                if context.technical_snapshot_artifact_id is not None:
                    if not self._market_context_technical_valid(context):
                        raise ValueError("technical market-context provenance mismatch")
                else:
                    from astock.technical.policy import load_technical_analysis_policy
                    from astock.technical.registered import attach_market_context

                    try:
                        enriched, _ = attach_market_context(
                            self.state, self.objects, context,
                            policy=load_technical_analysis_policy(
                                self.project_root / "configs" / "technical_analysis.yaml"
                            ),
                            calendar_path=(
                                self.project_root / "configs" / "official_trading_calendar.yaml"
                            ),
                        )
                    except (ValueError, OSError, StorageError) as exc:
                        # Optional research enrichment never changes price authority
                        # or blocks an otherwise complete canonical market context.
                        technical_observations.append({
                            "artifact_id": member_artifact_id,
                            "status": "TECHNICAL_INPUT_UNAVAILABLE",
                            "reason_type": type(exc).__name__,
                        })
                    else:
                        ref = self.objects.put_json(enriched.model_dump(mode="json"))
                        derived_id = "MarketContextPack:" + ref.sha256
                        self.state.register_artifact(
                            artifact_id=derived_id, artifact_type="MarketContextPack",
                            schema_version=enriched.schema_version, object_hash=ref.sha256,
                            input_hashes=[
                                member_hash, str(enriched.technical_snapshot_object_hash),
                                enriched.daily_release_object_hash,
                            ],
                        )
                        member_artifact_id, member_hash = derived_id, ref.sha256
            member_ids.append(member_artifact_id)
            member_hashes.append(member_hash)
        if member_ids != output.member_artifact_ids:
            output = output.model_copy(update={"member_artifact_ids": member_ids})
        return output, member_hashes, technical_observations

    def evaluate_coverage(self, request: ResearchCoverageRequest) -> ResearchCoverageReport:
        def score(completed: list[str], required: list[str], *, empty: float = 0.0) -> float:
            if not required:
                return empty
            return round(100.0 * len(completed) / len(required), 2)

        universal = score(request.universal_completed_ids, request.universal_required_ids)
        industry = score(request.industry_completed_ids, request.industry_required_ids)
        private_skill = score(
            request.private_skill_matched_ids,
            request.private_skill_available_ids,
        )
        evidence = score(request.evidence_satisfied_ids, request.evidence_required_ids)
        coverage_score = ResearchCoverageScore(
            universal_research_coverage=universal,
            industry_specialist_coverage=industry,
            private_skill_coverage=private_skill,
            evidence_coverage=evidence,
            created_at=request.created_at,
        )
        core_pass = (
            universal >= self.policy.universal_coverage_minimum
            and industry >= self.policy.industry_coverage_minimum
            and evidence >= self.policy.evidence_coverage_minimum
        )
        identity = {
            "request": request.model_dump(mode="json", exclude={"created_at"}),
            "score": coverage_score.model_dump(mode="json", exclude={"created_at"}),
            "policy_version": self.policy.policy_version,
        }
        report = ResearchCoverageReport(
            report_id="research-coverage:" + content_hash(identity),
            company_id=request.company_id,
            score=coverage_score,
            universal_minimum=self.policy.universal_coverage_minimum,
            industry_minimum=self.policy.industry_coverage_minimum,
            evidence_minimum=self.policy.evidence_coverage_minimum,
            core_coverage_pass=core_pass,
            missing_universal_ids=sorted(
                set(request.universal_required_ids) - set(request.universal_completed_ids)
            ),
            missing_industry_ids=sorted(
                set(request.industry_required_ids) - set(request.industry_completed_ids)
            ),
            missing_evidence_ids=sorted(
                set(request.evidence_required_ids) - set(request.evidence_satisfied_ids)
            ),
            created_at=request.created_at,
        )
        ref = self.objects.put_json(report.model_dump(mode="json"))
        self.state.register_artifact(
            artifact_id=f"ResearchCoverageReport:{report.report_id}",
            artifact_type="ResearchCoverageReport",
            schema_version=report.schema_version,
            object_hash=ref.sha256,
            input_hashes=[],
        )
        return report

    def register_role_result(self, result: ResearchRoleResult) -> dict[str, object]:
        plan = self.get_plan(result.plan_id)
        if plan is None:
            raise ValueError("unknown research-team plan")
        task = next((item for item in plan.tasks if item.task_id == result.task_id), None)
        if task is None:
            raise ValueError("role result references unknown task")
        if task.role is ResearchTaskRole.RECOMMENDATION_GATE:
            raise ValueError("deterministic recommendation gate cannot be manually completed")
        dependency_hashes: list[str] = []
        output_hashes: list[str] = []
        output_evidence_ids: set[str] = set()
        if result.state is ResearchTeamTaskState.COMPLETE:
            for output_artifact_id in result.output_artifact_ids:
                record = self.state.artifact_record(output_artifact_id)
                if (
                    record is None
                    or str(record["type"]) != "ResearchRoleOutput"
                    or not self.objects.verify(str(record["object_hash"]))
                ):
                    raise ValueError(
                        "COMPLETE role result requires registered ResearchRoleOutput artifacts"
                    )
                output = ResearchRoleOutput.model_validate_json(
                    self.objects.get_bytes(str(record["object_hash"]))
                )
                if (
                    output.plan_id != plan.plan_id
                    or output.task_id != task.task_id
                    or output.output_contract != task.output_contract
                ):
                    raise ValueError("ResearchRoleOutput lineage does not match the planned task")
                output_hashes.append(str(record["object_hash"]))
                output_evidence_ids.update(output.evidence_ids)
            if set(result.evidence_ids) != output_evidence_ids:
                raise ValueError(
                    "role result Evidence ids must equal the union of its role outputs"
                )
            for dependency in task.dependencies:
                checkpoint = self._task_checkpoint(plan.plan_id, dependency)
                if (
                    checkpoint is None
                    or checkpoint["status"] != ResearchTeamTaskState.COMPLETE.value
                ):
                    raise ValueError("cannot complete task before all dependencies are complete")
                object_hash = checkpoint.get("object_hash")
                if object_hash:
                    dependency_hashes.append(str(object_hash))
            self._validate_independence(plan, task, result)

        ref = self.objects.put_json(result.model_dump(mode="json"))
        artifact_id = "ResearchRoleResult:" + content_hash(
            {
                "plan_id": result.plan_id,
                "task_id": result.task_id,
                "state": result.state.value,
                "independent_context_id": result.independent_context_id,
                "object_hash": ref.sha256,
            }
        )
        plan_record = self.state.artifact_record(f"ResearchTeamPlan:{plan.plan_id}")
        input_hashes = sorted(
            {
                *dependency_hashes,
                *output_hashes,
                str(plan_record["object_hash"]) if plan_record is not None else "",
            }
            - {""}
        )
        self.state.register_artifact(
            artifact_id=artifact_id,
            artifact_type="ResearchRoleResult",
            schema_version=result.schema_version,
            object_hash=ref.sha256,
            input_hashes=input_hashes,
        )
        self.state.set_checkpoint(
            scope_type="research-team-task",
            scope_key=f"{plan.plan_id}:{task.task_id}",
            cursor={
                "artifact_id": artifact_id,
                "role": task.role.value,
                "independent_context_id": result.independent_context_id,
            },
            status=result.state.value,
            object_hash=ref.sha256,
        )
        return {
            "status": result.state.value,
            "plan_id": plan.plan_id,
            "task_id": task.task_id,
            "role": task.role.value,
            "artifact_id": artifact_id,
            "next_ready_tasks": self.status(plan.plan_id)["ready_tasks"],
        }

    def status(self, plan_id: str) -> dict[str, object]:
        plan = self.get_plan(plan_id)
        if plan is None:
            return {"status": "NOT_FOUND", "plan_id": plan_id}
        completed: list[str] = []
        blocked: list[str] = []
        pending: list[str] = []
        ready: list[str] = []
        for task in plan.tasks:
            checkpoint = self._task_checkpoint(plan_id, task.task_id)
            state = checkpoint["status"] if checkpoint else ResearchTeamTaskState.PENDING.value
            if state == ResearchTeamTaskState.COMPLETE.value:
                completed.append(task.task_id)
            elif state == ResearchTeamTaskState.BLOCKED.value:
                blocked.append(task.task_id)
            else:
                pending.append(task.task_id)
        completed_set = set(completed)
        for task in plan.tasks:
            if task.task_id in pending and set(task.dependencies).issubset(completed_set):
                ready.append(task.task_id)
        return {
            "status": "COMPLETE" if len(completed) == len(plan.tasks) else "IN_PROGRESS",
            "plan_id": plan_id,
            "completed_tasks": sorted(completed),
            "blocked_tasks": sorted(blocked),
            "pending_tasks": sorted(pending),
            "ready_tasks": sorted(ready),
            "reviewer_input": self.reviewer_input(plan_id)
            if "investment-red-team" in ready
            else None,
            "full_research_input_ready": False,
        }

    def reviewer_input(self, plan_id: str) -> dict[str, Any]:
        """Reuse the existing Reviewer task; freeze one bounded dispute input.

        No model is called to discover disputes. Different registered valuations
        are an explicit disagreement; prose differences alone prove nothing.
        The normal Reviewer and publication requirements remain mandatory.
        """
        from astock.research.debate_evaluation import digest, load_quality_policy, sealed
        from astock.schemas.research_team import IndependentBearCase, IndependentBullCase

        plan = self.get_plan(plan_id)
        if plan is None:
            raise ValueError("unknown research-team plan")
        cases: dict[str, Any] = {}
        case_source_hashes: dict[str, set[str]] = {}
        hashes = []
        for task_id, model in (
            ("bull-case", IndependentBullCase),
            ("bear-case", IndependentBearCase),
        ):
            checkpoint = self._task_checkpoint(plan_id, task_id)
            if checkpoint is None or checkpoint["status"] != "COMPLETE":
                return {"status": "INDEPENDENT_CASES_PENDING", "publication_authority": False}
            if not self.objects.verify(str(checkpoint["object_hash"])):
                raise ValueError("REVIEWER_INPUT_UNAVAILABLE")
            result = ResearchRoleResult.model_validate_json(
                self.objects.get_bytes(str(checkpoint["object_hash"]))
            )
            if result.plan_id != plan_id or result.task_id != task_id:
                raise ValueError("REVIEWER_INPUT_LINEAGE_MISMATCH")
            for output_id in result.output_artifact_ids:
                record = self.state.artifact_record(output_id)
                if record is None or not self.objects.verify(str(record["object_hash"])):
                    raise ValueError("REVIEWER_INPUT_UNAVAILABLE")
                output = ResearchRoleOutput.model_validate_json(
                    self.objects.get_bytes(str(record["object_hash"]))
                )
                if output.plan_id != plan_id or output.task_id != task_id:
                    raise ValueError("REVIEWER_INPUT_LINEAGE_MISMATCH")
                for member_id in output.member_artifact_ids:
                    member = self.state.artifact_record(member_id)
                    if member is None or member["type"] != model.__name__:
                        continue
                    if not self.objects.verify(str(member["object_hash"])):
                        raise ValueError("REVIEWER_INPUT_UNAVAILABLE")
                    case = model.model_validate_json(
                        self.objects.get_bytes(str(member["object_hash"]))
                    )
                    if plan.company_id is not None and case.company_id != plan.company_id:
                        raise ValueError("SECURITY_MISMATCH")
                    if task_id in cases:
                        raise ValueError("AMBIGUOUS_REVIEWER_CASE")
                    cases[task_id] = case
                    case_source_hashes[task_id] = set(member.get("input_hashes") or ())
                    hashes.append(str(member["object_hash"]))
        if len(cases) != 2:
            return {"status": "TYPED_CASES_UNAVAILABLE", "publication_authority": False}
        bull, bear = cases["bull-case"], cases["bear-case"]
        if bull.company_id != bear.company_id or bull.as_of != bear.as_of:
            raise ValueError("REVIEWER_INPUT_LINEAGE_MISMATCH")
        if bull.independent_context_id == bear.independent_context_id:
            raise ValueError("INDEPENDENCE_MISMATCH")
        quality = load_quality_policy(self.project_root / "configs/research_quality.yaml")
        if any(len(case.case) > quality.max_text_chars for case in (bull, bear)):
            raise ValueError("INPUT_LIMIT")
        questions = []
        if bull.valuation_per_share != bear.valuation_per_share:
            questions.append({
                "topic_id": "valuation",
                "kind": "ASSUMPTION",
                "bull_quote": bull.case,
                "bear_quote": bear.case,
                "question": "核对冻结证据与估值假设；未核实的分歧继续保留。",
            })
        position_questions, source_hashes = self._reviewer_position_questions(
            bull, bear, case_source_hashes,
        )
        questions.extend(position_questions)
        hashes.extend(source_hashes)
        deferred = max(0, len(questions) - quality.max_questions)
        questions = questions[:quality.max_questions]
        disputed = bool(questions)
        body = sealed(
            "reviewer-dispute-input",
            {
                "plan_id": plan_id,
                "company_id": bull.company_id,
                "input_hashes": sorted(set(hashes)),
                "status": "DISPUTED" if disputed else "NO_DISAGREEMENT",
                "reviewer_questions": questions,
                "deferred_question_count": deferred,
                "reviewer_task_id": "investment-red-team",
                "maximum_rounds": quality.cross_response_rounds if disputed else 0,
                "generation_limit": quality.cross_response_limit if disputed else 0,
                "additional_llm_calls": 0,
                "publication_authority": False,
            },
        )
        identity = "ReviewerDisputeInput:" + digest({"plan": plan_id, "frozen_input": body})
        existing = self.state.artifact_record(identity)
        if existing is None:
            ref = self.objects.put_json(body)
            self.state.register_artifact(
                artifact_id=identity,
                artifact_type="ReviewerDisputeInput",
                schema_version=body["schema_version"],
                object_hash=ref.sha256,
                input_hashes=sorted(set(hashes)),
            )
        review = self.state.get_checkpoint("dispute-reviewer", identity)
        return {
            **body,
            "artifact_id": identity,
            "cross_review": dict(review["cursor"]) if review else None,
        }

    def _reviewer_position_questions(
        self, bull: Any, bear: Any, case_source_hashes: dict[str, set[str]],
    ) -> tuple[list[dict[str, Any]], list[str]]:
        """Bind typed material disagreements to the two cases' own frozen sources."""
        hashes: list[str] = []
        questions: list[dict[str, Any]] = []
        from astock.schemas.institutional_research import DriverTree, ForecastPack, ValuationPack
        from astock.schemas.research import FrozenEvidencePack

        source_models = {
            "FrozenEvidencePack": FrozenEvidencePack,
            "CompanyFundamentalSnapshot": CompanyFundamentalSnapshot,
            "DriverTree": DriverTree, "ForecastPack": ForecastPack, "ValuationPack": ValuationPack,
        }
        positions = {}
        for side, case in (("BULL", bull), ("BEAR", bear)):
            indexed = {}
            for position in case.positions:
                if position.topic_id in indexed or position.statement not in case.case:
                    raise ValueError("REVIEWER_POSITION_NOT_BOUND")
                bindings = []
                for artifact_id in position.evidence_artifact_ids:
                    record = self.state.artifact_record(artifact_id)
                    if record is None or not self.objects.verify(str(record["object_hash"])):
                        raise ValueError("REVIEWER_POSITION_SOURCE_UNAVAILABLE")
                    source_hash = str(record["object_hash"])
                    if source_hash not in case_source_hashes[side.lower() + "-case"]:
                        raise ValueError("REVIEWER_POSITION_SOURCE_NOT_IN_CASE")
                    model = source_models.get(str(record["type"]))
                    if model is None:
                        raise ValueError("REVIEWER_POSITION_SOURCE_SCOPE_MISMATCH")
                    source = decode_registered_artifact(
                        self.objects.get_bytes(source_hash), model,
                        registry_schema_version=record["schema_version"],
                    )
                    source_as_of = getattr(source, "as_of", None)
                    if (
                        getattr(source, "company_id", None) != case.company_id
                        or source_as_of is None or source_as_of > case.as_of
                    ):
                        raise ValueError("REVIEWER_POSITION_SOURCE_SCOPE_MISMATCH")
                    bindings.append(source_hash)
                    hashes.append(source_hash)
                indexed[position.topic_id] = (position, sorted(set(bindings)))
            positions[side] = indexed
        for topic in sorted(positions["BULL"].keys() & positions["BEAR"].keys()):
            left, left_sources = positions["BULL"][topic]
            right, right_sources = positions["BEAR"][topic]
            if (
                not (left.material or right.material)
                or "STYLE" in {left.kind, right.kind}
                or (left.position == right.position and left.kind == right.kind)
            ):
                continue
            questions.append({
                "topic_id": topic,
                "kind": left.kind if left.kind == right.kind else "EPISTEMIC_CLASSIFICATION",
                "bull_quote": left.statement,
                "bear_quote": right.statement,
                "source_hashes": sorted(set(left_sources) | set(right_sources)),
                "question": "区分事实、假设和计算分歧，说明对结论的影响，不为达成共识改写证据。",
            })
        return questions, hashes

    def run_dispute_reviewer(self, plan_id: str, reviewer=None) -> dict[str, Any]:
        """One opt-in host call on a disputed frozen input, never a second vote.

        An interrupted call stays reserved: automatic retries could duplicate a
        paid generation. Its result has no publication or readiness authority.
        """
        payload = self.reviewer_input(plan_id)
        if payload["status"] != "DISPUTED" or not payload.get("maximum_rounds"):
            return {"status": "SKIPPED", "additional_llm_calls": 0}
        key = str(payload["artifact_id"])
        from astock.core.hashing import canonical_json_bytes, sha256_bytes
        from astock.core.state import utc_now_text
        from astock.research.debate_evaluation import digest

        input_hash = digest({k: v for k, v in payload.items() if k != "cross_review"})
        operation_id = "dispute-review:" + digest({"input": key, "hash": input_hash})
        reserved = {
            "status": "RESERVED", "reason": "REVIEWER_CALL_ALREADY_RESERVED",
            "operation_id": operation_id, "input_hash": input_hash,
            "generation_limit": payload["generation_limit"],
            "publication_authority": False,
        }
        # Claim in the existing checkpoint lane atomically; set_checkpoint is an
        # upsert and cannot express insert-only ownership across worker sessions.
        with self.state.transaction() as connection:
            inserted = connection.execute(
                "INSERT INTO checkpoint(checkpoint_id,scope_type,scope_key,cursor_json,"
                "status,committed_at) VALUES(?,?,?,?,?,?) "
                "ON CONFLICT(scope_type,scope_key) DO NOTHING",
                (
                    sha256_bytes(f"dispute-reviewer:{key}".encode()),
                    "dispute-reviewer",
                    key,
                    canonical_json_bytes(reserved).decode(),
                    "RESERVED",
                    utc_now_text(),
                ),
            ).rowcount
        if not inserted:
            checkpoint = self.state.get_checkpoint("dispute-reviewer", key)
            return dict(checkpoint["cursor"]) if checkpoint else reserved
        if reviewer is None:
            return {**reserved, "dispatch_granted": True, "input": payload}
        response = reviewer(payload)
        return self.complete_dispute_reviewer(
            key, operation_id=operation_id, input_hash=input_hash, response=response
        )

    def complete_dispute_reviewer(
        self, input_artifact_id: str, *, operation_id: str, input_hash: str, response: str
    ) -> dict[str, Any]:
        """Reconcile an already reserved host result; never dispatch another generation."""
        from astock.core.hashing import canonical_json_bytes
        from astock.core.state import utc_now_text
        from astock.research.debate_evaluation import digest

        checkpoint = self.state.get_checkpoint("dispute-reviewer", input_artifact_id)
        if checkpoint is None:
            raise ValueError("REVIEWER_OPERATION_NOT_RESERVED")
        reservation = dict(checkpoint["cursor"])
        if (reservation.get("operation_id"), reservation.get("input_hash")) != (
            operation_id, input_hash
        ):
            raise ValueError("REVIEWER_CALLBACK_IDENTITY_MISMATCH")
        if (
            not isinstance(response, str)
            or not response
            or len(response) > reservation["generation_limit"]
        ):
            raise ValueError("REVIEWER_RESPONSE_OUT_OF_BOUNDS")
        response_hash = digest({"response": response})
        if reservation.get("status") == "COMPLETE":
            if reservation.get("response_hash") != response_hash:
                raise ValueError("REVIEWER_CALLBACK_CONFLICT")
            return reservation
        if reservation.get("status") != "RESERVED":
            raise ValueError("REVIEWER_OPERATION_NOT_RESERVED")
        ref = self.objects.put_json({
            "input_artifact_id": input_artifact_id, "input_hash": input_hash,
            "operation_id": operation_id, "response": response, "publication_authority": False,
        })
        result = {
            **reservation, "status": "COMPLETE", "object_hash": ref.sha256,
            "response_hash": response_hash, "publication_authority": False,
        }
        with self.state.transaction() as connection:
            row = connection.execute(
                "SELECT cursor_json,status FROM checkpoint WHERE scope_type=? AND scope_key=?",
                ("dispute-reviewer", input_artifact_id),
            ).fetchone()
            if row is None:
                raise ValueError("REVIEWER_OPERATION_NOT_RESERVED")
            import json

            current = json.loads(row["cursor_json"])
            if current.get("status") == "COMPLETE":
                if current.get("response_hash") != response_hash:
                    raise ValueError("REVIEWER_CALLBACK_CONFLICT")
                return current
            if current != reservation:
                raise ValueError("REVIEWER_RESERVATION_CHANGED")
            connection.execute(
                "UPDATE checkpoint SET cursor_json=?,status=?,object_hash=?,committed_at=? "
                "WHERE scope_type=? AND scope_key=? AND status='RESERVED'",
                (
                    canonical_json_bytes(result).decode(), "COMPLETE", ref.sha256,
                    utc_now_text(), "dispute-reviewer", input_artifact_id,
                ),
            )
        return result

    def evaluate_readiness(
        self, request: FullResearchInputReadinessRequest
    ) -> FullResearchInputReadinessReport:
        plan = self.get_plan(request.plan_id)
        required = sorted(self._required_checks(plan))
        unknown_checks = set(request.checks) - set(required)
        if unknown_checks:
            raise ValueError("readiness request contains unknown checks")
        derived = self._derived_readiness_checks(plan)
        passed = {
            check
            for check in required
            if check != "TEAM_DAG_COMPLETE"
            and derived.get(check) is True
            and request.checks.get(check, True) is True
        }
        if plan is not None and self._team_dag_complete(plan):
            passed.add("TEAM_DAG_COMPLETE")
        missing = sorted(set(required) - passed)
        ready = not missing
        timestamp = request.created_at
        identity = {
            "plan_id": request.plan_id,
            "required": required,
            "passed": sorted(passed),
            "missing": missing,
            "created_at": timestamp.isoformat(),
        }
        report = FullResearchInputReadinessReport(
            report_id="full-research-input-readiness:" + content_hash(identity),
            plan_id=request.plan_id,
            status=(
                FullResearchInputReadinessStatus.READY
                if ready
                else FullResearchInputReadinessStatus.OBSERVATION_ONLY
            ),
            required_checks=required,
            passed_checks=sorted(passed),
            missing_or_failed_checks=missing,
            full_research_input_ready=ready,
            created_at=timestamp,
        )
        ref = self.objects.put_json(report.model_dump(mode="json"))
        self.state.register_artifact(
            artifact_id=f"FullResearchInputReadinessReport:{report.report_id}",
            artifact_type="FullResearchInputReadinessReport",
            schema_version=report.schema_version,
            object_hash=ref.sha256,
            input_hashes=self._readiness_input_hashes(plan),
        )
        self.state.set_checkpoint(
            scope_type="full-research-input-readiness",
            scope_key=request.plan_id,
            cursor={
                "report_id": report.report_id,
                "full_research_input_ready": ready,
            },
            status=report.status.value,
            object_hash=ref.sha256,
        )
        if plan is not None:
            self.state.set_checkpoint(
                scope_type="research-team-task",
                scope_key=f"{plan.plan_id}:recommendation-gate",
                cursor={
                    "artifact_id": f"FullResearchInputReadinessReport:{report.report_id}",
                    "role": ResearchTaskRole.RECOMMENDATION_GATE.value,
                    "independent_context_id": "deterministic-recommendation-gate",
                },
                status=(
                    ResearchTeamTaskState.COMPLETE.value
                    if ready
                    else ResearchTeamTaskState.BLOCKED.value
                ),
                object_hash=ref.sha256,
            )
        return report

    def _required_checks(self, plan: ResearchTeamPlan | None) -> tuple[str, ...]:
        if plan is not None and plan.scope is ResearchTeamScope.COMPANY:
            return self.policy.company_required_checks
        return self.policy.required_checks

    @staticmethod
    def _validate_readiness_ownership(
        tasks: list[ResearchTeamTask],
        required_checks: tuple[str, ...],
    ) -> None:
        mapped_checks = {check for task in tasks for check in task.readiness_checks}
        expected_checks = set(required_checks) - {"TEAM_DAG_COMPLETE"}
        if mapped_checks != expected_checks:
            raise ValueError("research-team readiness check ownership does not match policy")

    def _persist_plan(self, plan: ResearchTeamPlan) -> None:
        ref = self.objects.put_json(plan.model_dump(mode="json"))
        artifact_id = f"ResearchTeamPlan:{plan.plan_id}"
        input_hashes: list[str] = []
        if plan.acquisition_report_artifact_id is not None:
            report_record = self.state.artifact_record(plan.acquisition_report_artifact_id)
            if report_record is None or not self.objects.verify(str(report_record["object_hash"])):
                raise ValueError("research-team plan acquisition lineage is unavailable")
            input_hashes.append(str(report_record["object_hash"]))
        self.state.register_artifact(
            artifact_id=artifact_id,
            artifact_type="ResearchTeamPlan",
            schema_version=plan.schema_version,
            object_hash=ref.sha256,
            input_hashes=sorted(input_hashes),
        )
        self.state.set_checkpoint(
            scope_type="research-team-plan",
            scope_key=plan.plan_id,
            cursor={"artifact_id": artifact_id, "backend": plan.backend.value},
            status="PLANNED",
            object_hash=ref.sha256,
        )

    def _task_checkpoint(self, plan_id: str, task_id: str) -> dict[str, Any] | None:
        return self.state.get_checkpoint("research-team-task", f"{plan_id}:{task_id}")

    def _team_dag_complete(self, plan: ResearchTeamPlan) -> bool:
        for item in plan.tasks:
            if not item.required_for_recommendation:
                continue
            checkpoint = self._task_checkpoint(plan.plan_id, item.task_id)
            if checkpoint is None or checkpoint["status"] != ResearchTeamTaskState.COMPLETE.value:
                return False
        bull = self._task_checkpoint(plan.plan_id, "bull-case")
        bear = self._task_checkpoint(plan.plan_id, "bear-case")
        if bull is None or bear is None:
            return False
        return bull["cursor"].get("independent_context_id") != bear["cursor"].get(
            "independent_context_id"
        )

    def _derived_readiness_checks(self, plan: ResearchTeamPlan | None) -> dict[str, bool]:
        derived = {
            check: False for check in self._required_checks(plan) if check != "TEAM_DAG_COMPLETE"
        }
        if plan is None:
            return derived
        for task in plan.tasks:
            if not task.readiness_checks:
                continue
            checkpoint = self._task_checkpoint(plan.plan_id, task.task_id)
            if (
                checkpoint is None
                or checkpoint["status"] != ResearchTeamTaskState.COMPLETE.value
                or not checkpoint.get("object_hash")
                or not self.objects.verify(str(checkpoint["object_hash"]))
            ):
                continue
            result = ResearchRoleResult.model_validate_json(
                self.objects.get_bytes(str(checkpoint["object_hash"]))
            )
            outputs: list[ResearchRoleOutput] = []
            for artifact_id in result.output_artifact_ids:
                record = self.state.artifact_record(artifact_id)
                if (
                    record is None
                    or str(record["type"]) != "ResearchRoleOutput"
                    or not self.objects.verify(str(record["object_hash"]))
                ):
                    outputs = []
                    break
                output = ResearchRoleOutput.model_validate_json(
                    self.objects.get_bytes(str(record["object_hash"]))
                )
                if output.plan_id != plan.plan_id or output.task_id != task.task_id:
                    outputs = []
                    break
                outputs.append(output)
            if not outputs:
                continue
            if task.role is ResearchTaskRole.UNIVERSE:
                derived["UNIVERSE_COVERAGE"] = all(
                    self._universe_output_has_full_coverage(output) for output in outputs
                )
                continue
            if task.role is ResearchTaskRole.FINANCIAL_INTEGRITY:
                derived["FINANCIAL_INTEGRITY"] = all(
                    self._financial_output_has_formal_coverage(output) for output in outputs
                )
                continue
            typed = [self._typed_full_research_role_checks(task, output) for output in outputs]
            if typed and all(item is not None for item in typed):
                for check in task.readiness_checks:
                    derived[check] = all(bool(item and item.get(check)) for item in typed)
                continue
            for check in task.readiness_checks:
                derived[check] = all(
                    output.readiness_check_results.get(check) is True for output in outputs
                )
        if derived.get("FINANCIAL_INTEGRITY") is not True:
            derived["VALUATION"] = False
        return derived

    def _universe_output_has_full_coverage(self, output: ResearchRoleOutput) -> bool:
        reports: list[ResearchSeedReport] = []
        for artifact_id in output.member_artifact_ids:
            record = self.state.artifact_record(artifact_id)
            if record is None or str(record["type"]) != "ResearchSeedReport":
                continue
            object_hash = str(record["object_hash"])
            if not self.objects.verify(object_hash):
                return False
            try:
                reports.append(
                    ResearchSeedReport.model_validate_json(self.objects.get_bytes(object_hash))
                )
            except ValueError:
                return False
        return len(reports) == 1 and self._universe_report_has_verified_formal_coverage(reports[0])

    def _universe_report_has_verified_formal_coverage(
        self,
        report: ResearchSeedReport,
    ) -> bool:
        proof = report.universe_coverage_proof
        if (
            proof is None
            or not proof.formal_full_market_coverage_allowed
            or not report.formal_full_market_coverage_allowed
        ):
            return False
        report_snapshot_ids = set(report.source_snapshot_ids)
        report_object_hashes = set(report.source_object_hashes)
        for reconciliation in proof.market_reconciliations:
            reconciliation_snapshot_ids = set(reconciliation.source_snapshot_ids)
            if (
                not reconciliation_snapshot_ids
                or not reconciliation_snapshot_ids.issubset(report_snapshot_ids)
                or reconciliation.denominator_object_hash is None
                or reconciliation.numerator_object_hash is None
                or reconciliation.source_version not in reconciliation_snapshot_ids
            ):
                return False
            snapshots = {}
            for snapshot_id in reconciliation_snapshot_ids:
                snapshot = self.state.get_snapshot(snapshot_id)
                if (
                    snapshot is None
                    or snapshot.available_to_system_at > proof.as_of
                    or snapshot.object_sha256 not in report_object_hashes
                    or not self.objects.verify(snapshot.object_sha256)
                ):
                    return False
                snapshots[snapshot_id] = snapshot
            snapshot_hashes = {item.object_sha256 for item in snapshots.values()}
            if not {
                reconciliation.denominator_object_hash,
                reconciliation.numerator_object_hash,
            }.issubset(snapshot_hashes):
                return False
            denominator_snapshot = snapshots.get(reconciliation.source_version)
            if (
                denominator_snapshot is None
                or denominator_snapshot.source_id != reconciliation.denominator_source_id
                or denominator_snapshot.object_sha256 != reconciliation.denominator_object_hash
            ):
                return False
        return True

    def _financial_output_has_formal_coverage(self, output: ResearchRoleOutput) -> bool:
        packs: list[FinancialIntegrityEvidencePack] = []
        quality: list[FinancialQualityAssessment] = []
        for artifact_id in output.member_artifact_ids:
            record = self.state.artifact_record(artifact_id)
            if record is None:
                return False
            object_hash = str(record["object_hash"])
            if not self.objects.verify(object_hash):
                return False
            try:
                if str(record["type"]) == "FinancialIntegrityEvidencePack":
                    packs.append(
                        decode_registered_artifact(
                            self.objects.get_bytes(object_hash),
                            FinancialIntegrityEvidencePack,
                            registry_schema_version=str(record["schema_version"]),
                        )
                    )
                elif str(record["type"]) == "FinancialQualityAssessment":
                    quality.append(
                        FinancialQualityAssessment.model_validate_json(
                            self.objects.get_bytes(object_hash)
                        )
                    )
                else:
                    return False
            except ValueError:
                return False
        formal_packs = bool(packs) and all(
            pack.status is RunStatus.SUCCEEDED
            and pack.coverage_status is FinancialCoverageStatus.COMPLETE
            for pack in packs
        )
        formal_quality = bool(quality) and all(
            self._financial_quality_is_formal(item) for item in quality
        )
        return formal_packs and formal_quality

    def _component_lineage_is_registered(self, item: object) -> bool:
        evidence_repository = EvidenceRepository(self.state)
        for evidence_id in tuple(getattr(item, "evidence_ids", ())):
            evidence = evidence_repository.get_evidence(evidence_id)
            if evidence is None or not self.objects.verify(evidence.excerpt_object_sha256):
                return False
        source_ids = tuple(getattr(item, "source_artifact_ids", ()))
        source_hashes = tuple(getattr(item, "source_object_hashes", ()))
        if len(source_ids) != len(source_hashes):
            return False
        for artifact_id, expected_hash in zip(source_ids, source_hashes, strict=True):
            record = self.state.artifact_record(artifact_id)
            if (
                record is None
                or str(record["object_hash"]) != expected_hash
                or not self.objects.verify(expected_hash)
            ):
                return False
        for artifact_id in tuple(getattr(item, "raw_fact_artifact_ids", ())):
            record = self.state.artifact_record(artifact_id)
            if record is None or not self.objects.verify(str(record["object_hash"])):
                return False
        return True

    def _financial_quality_is_formal(self, item: FinancialQualityAssessment) -> bool:
        placeholders = {"NOT_SEPARATELY_EXPOSED", "COVERED_BY_CANONICAL_FINANCIAL_INTEGRITY"}
        return (
            self._component_lineage_is_registered(item)
            and bool(item.evidence_ids)
            and item.audit_opinion not in placeholders
            and all(value not in placeholders for value in item.checks.values())
        )

    def _governance_is_formal(self, item: GovernanceAssessment) -> bool:
        placeholders = {"NOT_SEPARATELY_EXPOSED", "COVERED_BY_CANONICAL_GOVERNANCE_RESEARCH"}
        return (
            self._component_lineage_is_registered(item)
            and bool(item.evidence_ids)
            and item.controller is not None
            and all(value not in placeholders for value in item.checks.values())
        )

    def _macro_is_formal(self, item: MacroResearchOutcome) -> bool:
        return (
            self._component_lineage_is_registered(item)
            and bool(item.evidence_ids)
            and all(
                not (isinstance(value, str) and value.startswith("NOT_SEPARATELY_EXPOSED"))
                for value in item.dimensions.values()
            )
        )

    def _industry_is_formal(self, item: IndustryResearchOutcome) -> bool:
        return (
            self._component_lineage_is_registered(item)
            and bool(item.evidence_ids)
            and item.recommendation_peer_coverage_complete()
            and all(
                not (isinstance(value, str) and value.startswith("NOT_SEPARATELY_EXPOSED"))
                for value in item.dimensions.values()
            )
        )

    def _load_typed_member_artifacts(
        self,
        output: ResearchRoleOutput,
        model: type[object],
    ) -> list[object] | None:
        values: list[object] = []
        for artifact_id in output.member_artifact_ids:
            record = self.state.artifact_record(artifact_id)
            if record is None or str(record["type"]) != model.__name__:
                return None
            object_hash = str(record["object_hash"])
            if not self.objects.verify(object_hash):
                return None
            try:
                value = model.model_validate_json(self.objects.get_bytes(object_hash))  # type: ignore[attr-defined]
            except ValueError:
                return None
            if not self._component_lineage_is_registered(value):
                return None
            values.append(value)
        return values or None

    def _technical_snapshot(
        self, artifact_id: str, expected_hash: str
    ) -> TechnicalIndicatorSnapshot | None:
        """Inspect only registered technical provenance, without replaying bars."""
        record = self.state.artifact_record(artifact_id)
        if (
            record is None
            or str(record["type"]) != "TechnicalIndicatorSnapshot"
            or str(record["object_hash"]) != expected_hash
            or not self.objects.verify(expected_hash)
        ):
            return None
        try:
            snapshot = decode_registered_artifact(
                self.objects.get_bytes(expected_hash),
                TechnicalIndicatorSnapshot,
                registry_schema_version=str(record["schema_version"]),
            )
        except ValueError:
            return None
        parent = self.state.artifact_record(snapshot.source_artifact_id)
        if (
            parent is None
            or str(parent["type"]) != "DatasetReleaseManifest"
            or str(parent["object_hash"]) != snapshot.source_object_hash
        ):
            return None
        return snapshot

    def _market_context_technical_valid(self, context: MarketContextPack) -> bool:
        if context.technical_snapshot_artifact_id is None:
            return True
        if context.technical_snapshot_object_hash is None:
            return False
        snapshot = self._technical_snapshot(
            context.technical_snapshot_artifact_id, context.technical_snapshot_object_hash
        )
        return bool(
            snapshot
            and snapshot.instrument_id.split(":")[-1] == context.company_id
            and snapshot.source_artifact_id == context.daily_release_artifact_id
            and snapshot.source_object_hash == context.daily_release_object_hash
            and snapshot.as_of <= context.as_of
        )

    def _quant_technical_sources_valid(self, pack: QuantFactorResearchPack) -> bool:
        """Optional indicator cannot refer to another ticker or a later research instant."""
        identities = {factor.instrument_id for factor in pack.factors}
        for artifact_id, object_hash in zip(
            pack.source_artifact_ids, pack.source_object_hashes, strict=True
        ):
            record = self.state.artifact_record(artifact_id)
            if record is None or str(record["type"]) != "TechnicalIndicatorSnapshot":
                continue
            snapshot = self._technical_snapshot(artifact_id, object_hash)
            if (
                snapshot is None
                or snapshot.instrument_id not in identities
                or snapshot.as_of > pack.as_of
            ):
                return False
        return True

    def _typed_full_research_role_checks(
        self,
        task: ResearchTeamTask,
        output: ResearchRoleOutput,
    ) -> dict[str, bool] | None:
        if task.role is ResearchTaskRole.MACRO:
            values = self._load_typed_member_artifacts(output, MacroResearchOutcome)
            formal = bool(values) and all(
                self._macro_is_formal(item)
                for item in values
                if isinstance(item, MacroResearchOutcome)
            )
            return {check: formal for check in task.readiness_checks}
        if task.role is ResearchTaskRole.INDUSTRY:
            values = self._load_typed_member_artifacts(output, IndustryResearchOutcome)
            formal = bool(values) and all(
                self._industry_is_formal(item)
                for item in values
                if isinstance(item, IndustryResearchOutcome)
            )
            return {check: formal for check in task.readiness_checks}
        if task.role is ResearchTaskRole.FUNDAMENTAL:
            values = self._load_typed_member_artifacts(output, CompanyFundamentalSnapshot)
            formal = bool(values) and all(
                item.recommendation_ready()
                for item in values
                if isinstance(item, CompanyFundamentalSnapshot)
            )
            return {check: formal for check in task.readiness_checks}
        if task.role is ResearchTaskRole.GOVERNANCE:
            values = self._load_typed_member_artifacts(output, GovernanceAssessment)
            formal = bool(values) and all(
                self._governance_is_formal(item)
                for item in values
                if isinstance(item, GovernanceAssessment)
            )
            return {check: formal for check in task.readiness_checks}
        if task.role is ResearchTaskRole.CATALYST:
            values = self._load_typed_member_artifacts(output, NewsEventResearchPack)
            formal = bool(values) and all(
                item.recommendation_ready(
                    required_windows=self.full_research_news_windows,
                    required_categories=self.full_research_news_categories,
                )
                for item in values
                if isinstance(item, NewsEventResearchPack)
            )
            return {check: formal for check in task.readiness_checks}
        if task.role is ResearchTaskRole.QUANT_FACTOR:
            values = self._load_typed_member_artifacts(output, QuantFactorResearchPack)
            formal = bool(values) and all(
                item.recommendation_ready() and self._quant_technical_sources_valid(item)
                for item in values
                if isinstance(item, QuantFactorResearchPack)
            )
            return {check: formal for check in task.readiness_checks}
        if task.role is ResearchTaskRole.REVIEWER:
            values = self._load_typed_member_artifacts(output, ChallengerAssessment)
            formal = bool(values) and all(
                not item.primary_final_label_visible and not item.material_conflict_with_primary
                for item in values
                if isinstance(item, ChallengerAssessment)
            )
            return {check: formal for check in task.readiness_checks}
        return None

    def _validate_independence(
        self,
        plan: ResearchTeamPlan,
        task: ResearchTeamTask,
        result: ResearchRoleResult,
    ) -> None:
        counterpart = {
            ResearchTaskRole.BULL: "bear-case",
            ResearchTaskRole.BEAR: "bull-case",
        }.get(task.role)
        if counterpart is None:
            return
        checkpoint = self._task_checkpoint(plan.plan_id, counterpart)
        if checkpoint is None:
            return
        other = checkpoint["cursor"].get("independent_context_id")
        if other == result.independent_context_id:
            raise ValueError("Bull and Bear must use different independent_context_id values")

    def _readiness_input_hashes(self, plan: ResearchTeamPlan | None) -> list[str]:
        if plan is None:
            return []
        hashes: set[str] = set()
        plan_record = self.state.artifact_record(f"ResearchTeamPlan:{plan.plan_id}")
        if plan_record is not None:
            hashes.add(str(plan_record["object_hash"]))
        for task in plan.tasks:
            checkpoint = self._task_checkpoint(plan.plan_id, task.task_id)
            if checkpoint is not None and checkpoint.get("object_hash"):
                hashes.add(str(checkpoint["object_hash"]))
        return sorted(hashes)

    @staticmethod
    def _company_tasks(created_at: datetime) -> list[ResearchTeamTask]:
        raw = [
            ("company-intent", ResearchTaskRole.CIO, 0, [], None, "CompanyResearchIntent"),
            (
                "macro-regime",
                ResearchTaskRole.MACRO,
                1,
                ["company-intent"],
                None,
                "MacroResearchOutcome",
            ),
            (
                "policy-regime",
                ResearchTaskRole.POLICY,
                1,
                ["company-intent"],
                None,
                "PolicyRegimeProfile",
            ),
            (
                "industry-value-chain",
                ResearchTaskRole.INDUSTRY,
                1,
                ["company-intent"],
                None,
                "IndustryResearchOutcome",
            ),
            (
                "governance-management-quality",
                ResearchTaskRole.GOVERNANCE,
                1,
                ["company-intent"],
                None,
                "GovernanceAssessment",
            ),
            (
                "company-financial-integrity",
                ResearchTaskRole.FINANCIAL_INTEGRITY,
                2,
                ["company-intent"],
                None,
                "FinancialIntegrityEvidencePack",
            ),
            (
                "company-fundamental",
                ResearchTaskRole.FUNDAMENTAL,
                3,
                ["company-financial-integrity", "industry-value-chain"],
                None,
                "CompanyFundamentalSnapshot",
            ),
            (
                "company-catalyst",
                ResearchTaskRole.CATALYST,
                3,
                [
                    "governance-management-quality",
                    "industry-value-chain",
                    "macro-regime",
                    "policy-regime",
                ],
                None,
                "NewsEventResearchPack",
            ),
            (
                "company-market-context",
                ResearchTaskRole.MARKET_CONTEXT,
                2,
                ["company-intent"],
                None,
                "MarketContextPack",
            ),
            (
                "valuation",
                ResearchTaskRole.VALUATION,
                4,
                ["company-financial-integrity", "company-fundamental", "company-market-context"],
                None,
                "ValuationPack",
            ),
            (
                "quant-factor",
                ResearchTaskRole.QUANT_FACTOR,
                5,
                ["company-fundamental", "company-market-context", "valuation"],
                None,
                "QuantFactorResearchPack",
            ),
            (
                "bull-case",
                ResearchTaskRole.BULL,
                5,
                ["company-catalyst", "governance-management-quality", "valuation"],
                None,
                "IndependentBullCase",
            ),
            (
                "bear-case",
                ResearchTaskRole.BEAR,
                5,
                ["company-catalyst", "governance-management-quality", "valuation"],
                None,
                "IndependentBearCase",
            ),
            (
                "investment-red-team",
                ResearchTaskRole.REVIEWER,
                6,
                ["bear-case", "bull-case"],
                None,
                "ChallengerAssessment",
            ),
            (
                "model-risk-validation",
                ResearchTaskRole.MODEL_RISK,
                6,
                ["bear-case", "bull-case", "company-financial-integrity", "valuation"],
                None,
                "ModelRiskValidationReport",
            ),
            (
                "committee",
                ResearchTaskRole.COMMITTEE,
                7,
                ["investment-red-team", "model-risk-validation", "quant-factor"],
                None,
                "DecisionPack",
            ),
            (
                "recommendation-gate",
                ResearchTaskRole.RECOMMENDATION_GATE,
                8,
                ["committee"],
                None,
                "FullResearchInputReadinessReport",
            ),
        ]
        readiness_by_task: dict[str, list[str]] = {
            "macro-regime": ["MACRO_REGIME"],
            "policy-regime": ["POLICY_REGIME"],
            "industry-value-chain": ["INDUSTRY_PROFILE"],
            "governance-management-quality": ["GOVERNANCE_QUALITY"],
            "company-financial-integrity": ["FINANCIAL_INTEGRITY"],
            "company-fundamental": [
                "COMPANY_ECONOMICS",
                "DRIVER_TREE",
                "FORECAST_BULL_BASE_BEAR",
            ],
            "company-catalyst": ["CATALYST_RISK"],
            "company-market-context": ["MARKET_PRICE_ANCHOR"],
            "valuation": ["VALUATION"],
            "quant-factor": ["QUANT_FACTOR"],
            "bull-case": ["BULL_CASE"],
            "bear-case": ["BEAR_CASE"],
            "investment-red-team": ["INDEPENDENT_REVIEW"],
            "model-risk-validation": ["MODEL_RISK_VALIDATION"],
            "committee": ["COMMITTEE"],
        }
        return [
            ResearchTeamTask(
                task_id=task_id,
                role=role,
                stage=stage,
                dependencies=sorted(dependencies),
                fanout_key=fanout_key,
                required_for_recommendation=(role is not ResearchTaskRole.RECOMMENDATION_GATE),
                output_contract=output_contract,
                readiness_checks=sorted(readiness_by_task.get(task_id, [])),
                created_at=created_at,
            )
            for task_id, role, stage, dependencies, fanout_key, output_contract in raw
        ]

    @staticmethod
    def _full_market_tasks(created_at: datetime) -> list[ResearchTeamTask]:
        raw = [
            ("cio-intent", ResearchTaskRole.CIO, 0, [], None, "ResearchIntent"),
            (
                "macro-regime",
                ResearchTaskRole.MACRO,
                1,
                ["cio-intent"],
                None,
                "MacroResearchOutcome",
            ),
            (
                "policy-regime",
                ResearchTaskRole.POLICY,
                1,
                ["cio-intent"],
                None,
                "PolicyRegimeProfile",
            ),
            (
                "liquidity-risk",
                ResearchTaskRole.LIQUIDITY_RISK,
                1,
                ["cio-intent"],
                None,
                "MarketRiskProfile",
            ),
            (
                "universe-acquisition",
                ResearchTaskRole.UNIVERSE,
                1,
                ["cio-intent"],
                None,
                "CandidateUniverseProof",
            ),
            (
                "blind-candidate-scan",
                ResearchTaskRole.BLIND_CANDIDATE,
                2,
                ["universe-acquisition"],
                None,
                "BlindCandidateShortlist",
            ),
            (
                "sector-comparison",
                ResearchTaskRole.INDUSTRY,
                3,
                ["blind-candidate-scan", "liquidity-risk", "macro-regime", "policy-regime"],
                None,
                "IndustryResearchOutcomeSet",
            ),
            (
                "company-fundamental",
                ResearchTaskRole.FUNDAMENTAL,
                4,
                ["sector-comparison"],
                "candidate_shortlist",
                "CompanyFundamentalSnapshotSet",
            ),
            (
                "company-financial-integrity",
                ResearchTaskRole.FINANCIAL_INTEGRITY,
                4,
                ["sector-comparison"],
                "candidate_shortlist",
                "FinancialIntegrityEvidencePackSet",
            ),
            (
                "company-catalyst",
                ResearchTaskRole.CATALYST,
                4,
                ["sector-comparison"],
                "candidate_shortlist",
                "NewsEventResearchPackSet",
            ),
            (
                "company-market-context",
                ResearchTaskRole.MARKET_CONTEXT,
                4,
                ["sector-comparison"],
                "candidate_shortlist",
                "MarketContextPackSet",
            ),
            (
                "valuation",
                ResearchTaskRole.VALUATION,
                5,
                ["company-financial-integrity", "company-fundamental", "company-market-context"],
                "candidate_shortlist",
                "ValuationPackSet",
            ),
            (
                "quant-factor",
                ResearchTaskRole.QUANT_FACTOR,
                6,
                ["company-fundamental", "company-market-context", "valuation"],
                "candidate_shortlist",
                "QuantFactorResearchPack",
            ),
            (
                "bull-case",
                ResearchTaskRole.BULL,
                6,
                ["company-catalyst", "valuation"],
                "candidate_shortlist",
                "IndependentBullCaseSet",
            ),
            (
                "bear-case",
                ResearchTaskRole.BEAR,
                6,
                ["company-catalyst", "valuation"],
                "candidate_shortlist",
                "IndependentBearCaseSet",
            ),
            (
                "independent-review",
                ResearchTaskRole.REVIEWER,
                7,
                ["bear-case", "bull-case"],
                "candidate_shortlist",
                "ChallengerAssessmentSet",
            ),
            (
                "committee",
                ResearchTaskRole.COMMITTEE,
                8,
                ["independent-review", "quant-factor"],
                "candidate_shortlist",
                "DecisionPackSet",
            ),
            (
                "portfolio-construction",
                ResearchTaskRole.PORTFOLIO,
                9,
                ["committee"],
                None,
                "ApprovedPortfolioProposal",
            ),
            (
                "recommendation-gate",
                ResearchTaskRole.RECOMMENDATION_GATE,
                10,
                ["portfolio-construction"],
                None,
                "FullResearchInputReadinessReport",
            ),
        ]
        readiness_by_task: dict[str, list[str]] = {
            "macro-regime": ["MACRO_REGIME"],
            "universe-acquisition": ["UNIVERSE_COVERAGE"],
            "blind-candidate-scan": ["BLIND_CANDIDATE_SCAN"],
            "sector-comparison": ["SECTOR_COMPARISON"],
            "company-fundamental": [
                "COMPANY_ECONOMICS",
                "DRIVER_TREE",
                "FORECAST_BULL_BASE_BEAR",
                "INDUSTRY_PROFILE",
            ],
            "company-financial-integrity": ["FINANCIAL_INTEGRITY"],
            "company-catalyst": ["CATALYST_RISK"],
            "company-market-context": ["MARKET_PRICE_ANCHOR"],
            "valuation": ["VALUATION"],
            "quant-factor": ["QUANT_FACTOR"],
            "bull-case": ["BULL_CASE"],
            "bear-case": ["BEAR_CASE"],
            "independent-review": ["INDEPENDENT_REVIEW"],
            "committee": ["COMMITTEE"],
            "portfolio-construction": ["PORTFOLIO_CONSTRUCTION"],
        }
        return [
            ResearchTeamTask(
                task_id=task_id,
                role=role,
                stage=stage,
                dependencies=sorted(dependencies),
                fanout_key=fanout_key,
                required_for_recommendation=(role is not ResearchTaskRole.RECOMMENDATION_GATE),
                output_contract=output_contract,
                readiness_checks=sorted(readiness_by_task.get(task_id, [])),
                created_at=created_at,
            )
            for task_id, role, stage, dependencies, fanout_key, output_contract in raw
        ]


__all__ = [
    "ResearchTeamPolicy",
    "ResearchTeamService",
    "detect_hardware_budget",
    "load_research_team_policy",
]
