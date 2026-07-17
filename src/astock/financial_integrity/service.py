"""Deterministic, evidence-bound financial-integrity audit service."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from astock.core.hashing import canonical_json_bytes, sha256_bytes
from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.evidence import EvidenceRepository
from astock.financial_integrity.advanced_calculations import (
    altman_z_score,
    beneish_m_score,
    dupont_decomposition,
    midrank_percentile,
    percentage_change,
    piotroski_f_score,
    sloan_accrual_ratio,
)
from astock.financial_integrity.calculations import (
    balance_identity_difference,
    cash_identity_difference,
    decimal_ratio,
    reporting_rounding_tolerance,
)
from astock.financial_integrity.config import (
    load_financial_industry_profiles,
    load_financial_rule_registry,
    validate_financial_config,
)
from astock.financial_integrity.repository import FinancialIntegrityRepository
from astock.pit import PointInTimeRepository, PointInTimeService
from astock.schemas import (
    EvidenceGrade,
    FactStatus,
    FetchStatus,
    FinancialAuditRequest,
    FinancialCoverageStatus,
    FinancialDerivationType,
    FinancialDerivedMetric,
    FinancialDocumentConflict,
    FinancialDurationSemantics,
    FinancialEvidenceGap,
    FinancialFact,
    FinancialFieldCode,
    FinancialFindingStatus,
    FinancialGapType,
    FinancialImplementationStatus,
    FinancialIndustryProfileDefinition,
    FinancialIntegrityEvidencePack,
    FinancialManualTask,
    FinancialPeerPercentile,
    FinancialPeriodType,
    FinancialRiskLevel,
    FinancialRuleDefinition,
    FinancialRuleFinding,
    FinancialSeriesRequest,
    FinancialSeverity,
    FinancialStatementType,
    FinancialUnit,
    RecalculatedFinancialMetric,
    RunStatus,
    VerifiedFinancialNumber,
)

_UNIT_MULTIPLIERS: dict[FinancialUnit, Decimal] = {
    FinancialUnit.CNY: Decimal("1"),
    FinancialUnit.THOUSAND_CNY: Decimal("1000"),
    FinancialUnit.TEN_THOUSAND_CNY: Decimal("10000"),
    FinancialUnit.MILLION_CNY: Decimal("1000000"),
    FinancialUnit.HUNDRED_MILLION_CNY: Decimal("100000000"),
    FinancialUnit.SHARES: Decimal("1"),
}

_EXPECTED_STATEMENTS: dict[FinancialFieldCode, FinancialStatementType] = {
    FinancialFieldCode.TOTAL_ASSETS: FinancialStatementType.BALANCE_SHEET,
    FinancialFieldCode.TOTAL_LIABILITIES: FinancialStatementType.BALANCE_SHEET,
    FinancialFieldCode.TOTAL_EQUITY: FinancialStatementType.BALANCE_SHEET,
    FinancialFieldCode.ACCOUNTS_RECEIVABLE: FinancialStatementType.BALANCE_SHEET,
    FinancialFieldCode.INVENTORY: FinancialStatementType.BALANCE_SHEET,
    FinancialFieldCode.PREPAYMENTS: FinancialStatementType.BALANCE_SHEET,
    FinancialFieldCode.OTHER_RECEIVABLES: FinancialStatementType.BALANCE_SHEET,
    FinancialFieldCode.CURRENT_ASSETS: FinancialStatementType.BALANCE_SHEET,
    FinancialFieldCode.CURRENT_LIABILITIES: FinancialStatementType.BALANCE_SHEET,
    FinancialFieldCode.TOTAL_DEBT: FinancialStatementType.BALANCE_SHEET,
    FinancialFieldCode.RETAINED_EARNINGS: FinancialStatementType.BALANCE_SHEET,
    FinancialFieldCode.NET_PROFIT_INCOME: FinancialStatementType.INCOME_STATEMENT,
    FinancialFieldCode.REVENUE: FinancialStatementType.INCOME_STATEMENT,
    FinancialFieldCode.OPERATING_COST: FinancialStatementType.INCOME_STATEMENT,
    FinancialFieldCode.EBIT: FinancialStatementType.INCOME_STATEMENT,
    FinancialFieldCode.DEPRECIATION_AMORTIZATION: FinancialStatementType.INCOME_STATEMENT,
    FinancialFieldCode.SELLING_GENERAL_ADMIN_EXPENSE: FinancialStatementType.INCOME_STATEMENT,
    FinancialFieldCode.PROPERTY_PLANT_EQUIPMENT: FinancialStatementType.BALANCE_SHEET,
    FinancialFieldCode.LONG_TERM_DEBT: FinancialStatementType.BALANCE_SHEET,
    FinancialFieldCode.MARKET_CAP: FinancialStatementType.BALANCE_SHEET,
    FinancialFieldCode.SHARES_OUTSTANDING: FinancialStatementType.BALANCE_SHEET,
    FinancialFieldCode.CASH_BEGINNING: FinancialStatementType.CASH_FLOW_STATEMENT,
    FinancialFieldCode.CASH_ENDING: FinancialStatementType.CASH_FLOW_STATEMENT,
    FinancialFieldCode.NET_CASH_OPERATING: FinancialStatementType.CASH_FLOW_STATEMENT,
    FinancialFieldCode.NET_CASH_INVESTING: FinancialStatementType.CASH_FLOW_STATEMENT,
    FinancialFieldCode.NET_CASH_FINANCING: FinancialStatementType.CASH_FLOW_STATEMENT,
    FinancialFieldCode.EXCHANGE_EFFECT: FinancialStatementType.CASH_FLOW_STATEMENT,
    FinancialFieldCode.NET_PROFIT_CASH_FLOW: FinancialStatementType.CASH_FLOW_STATEMENT,
}


@dataclass(frozen=True, slots=True)
class FinancialAuditExecution:
    pack: FinancialIntegrityEvidencePack
    artifact_hash: str
    reused_existing: bool


@dataclass(frozen=True, slots=True)
class _ValidatedFact:
    fact: FinancialFact
    value_cny: Decimal
    reporting_quantum_cny: Decimal


@dataclass(frozen=True, slots=True)
class _ValidationResult:
    verified_numbers: list[VerifiedFinancialNumber]
    conflicts: list[FinancialDocumentConflict]
    safe_snapshot_ids: list[str]
    safe_pit_ids: list[str]


@dataclass(frozen=True, slots=True)
class _SeriesPoint:
    period_end: date
    value: Decimal
    fact_ids: list[str]
    evidence_ids: list[str]


class _GapCollector:
    def __init__(self, company_id: str, created_at: datetime) -> None:
        self.company_id = company_id
        self.created_at = created_at
        self._gaps: dict[str, FinancialEvidenceGap] = {}

    def add(
        self,
        gap_type: FinancialGapType,
        detail_code: str,
        *,
        period_end: date | None = None,
        field_codes: list[FinancialFieldCode] | None = None,
        fact_ids: list[str] | None = None,
        rule_ids: list[str] | None = None,
        safe_evidence_ids: list[str] | None = None,
    ) -> FinancialEvidenceGap:
        identity = {
            "company_id": self.company_id,
            "gap_type": gap_type.value,
            "detail_code": detail_code,
            "period_end": period_end,
            "field_codes": sorted(code.value for code in (field_codes or [])),
            "fact_ids": sorted(set(fact_ids or [])),
            "rule_ids": sorted(set(rule_ids or [])),
            "safe_evidence_ids": sorted(set(safe_evidence_ids or [])),
        }
        gap_id = f"financial-gap:{sha256_bytes(canonical_json_bytes(identity))}"
        gap = FinancialEvidenceGap(
            gap_id=gap_id,
            gap_type=gap_type,
            detail_code=detail_code,
            period_end=period_end,
            field_codes=sorted(set(field_codes or []), key=lambda code: code.value),
            related_fact_ids=sorted(set(fact_ids or [])),
            related_rule_ids=sorted(set(rule_ids or [])),
            safe_evidence_ids=sorted(set(safe_evidence_ids or [])),
            created_at=self.created_at,
        )
        self._gaps[gap_id] = gap
        return gap

    def values(self) -> list[FinancialEvidenceGap]:
        return sorted(self._gaps.values(), key=lambda gap: gap.gap_id)


class FinancialIntegrityService:
    ENGINE_VERSION = "financial-deterministic-m3.2.0"

    def __init__(
        self,
        state: StateStore,
        object_store: ObjectStore,
        *,
        rule_config_path: Path,
        industry_profile_path: Path,
    ) -> None:
        self.state = state
        self.object_store = object_store
        self.repository = FinancialIntegrityRepository(state, object_store)
        self.evidence_repository = EvidenceRepository(state)
        self.pit_repository = PointInTimeRepository(state)
        self.rule_registry = load_financial_rule_registry(rule_config_path)
        self.profile_registry = load_financial_industry_profiles(industry_profile_path)
        validate_financial_config(self.rule_registry, self.profile_registry)
        if self.rule_registry.compatible_engine_version != self.ENGINE_VERSION:
            raise ValueError(
                "financial rule registry is incompatible with the deterministic engine: "
                f"{self.rule_registry.compatible_engine_version} != {self.ENGINE_VERSION}"
            )
        self._rules = {rule.rule_id: rule for rule in self.rule_registry.rules}
        self._profiles = {
            profile.profile_id: profile for profile in self.profile_registry.profiles
        }

    def run(self, request: FinancialAuditRequest) -> FinancialAuditExecution:
        request = FinancialAuditRequest.model_validate(request.model_dump(mode="python"))
        profile = self._profiles.get(request.industry_profile)
        if profile is None:
            raise ValueError(f"unknown financial industry profile: {request.industry_profile}")
        selected_rules = self._select_rules(request, profile)
        semantic_request = self._semantic_request(request)
        request_hash = sha256_bytes(canonical_json_bytes(semantic_request))
        request_object = self.object_store.put_json(semantic_request)
        audit_run_id = self._audit_run_id(request_hash)
        record = self.repository.ensure_run(
            audit_run_id=audit_run_id,
            request_hash=request_hash,
            company_id=request.company_id,
            as_of=request.as_of.isoformat(),
            industry_profile=request.industry_profile.value,
            rule_registry_version=self.rule_registry.registry_version,
            industry_profile_version=self.profile_registry.registry_version,
            request_object_hash=request_object.sha256,
        )
        if record.status in {RunStatus.SUCCEEDED, RunStatus.NEEDS_INFO}:
            existing = self.repository.get_pack(audit_run_id)
            if existing is not None and record.report_object_hash is not None:
                return FinancialAuditExecution(existing, record.report_object_hash, True)

        attempt_id = self.repository.start_attempt(audit_run_id)
        gaps = _GapCollector(request.company_id, record.created_at)
        try:
            validation = self._validate_and_reconcile_facts(request, gaps, record.created_at)
            self.repository.checkpoint(audit_run_id, "LINEAGE_VALIDATED")
            metrics, findings = self._evaluate_rules(
                request,
                selected_rules,
                validation.verified_numbers,
                gaps,
                record.created_at,
            )
            self.repository.checkpoint(audit_run_id, "RULES_EVALUATED")
            derived_metrics = self._derive_series_metrics(
                request.series_requests,
                validation.verified_numbers,
                gaps,
                record.created_at,
            )
            self.repository.checkpoint(audit_run_id, "SERIES_DERIVED")
            peer_percentiles = self._evaluate_peer_cohorts(
                audit_run_id,
                request,
                metrics,
                derived_metrics,
                gaps,
                record.created_at,
            )
            self.repository.checkpoint(audit_run_id, "PEERS_EVALUATED")
            evidence_gaps = gaps.values()
            status = RunStatus.NEEDS_INFO if evidence_gaps else RunStatus.SUCCEEDED
            pack = FinancialIntegrityEvidencePack(
                audit_run_id=audit_run_id,
                request_hash=request_hash,
                status=status,
                coverage_status=self._coverage_status(
                    validation.verified_numbers, evidence_gaps
                ),
                company_id=request.company_id,
                as_of=request.as_of,
                industry_profile=request.industry_profile,
                periods=sorted({fact.period_end for fact in request.facts}),
                input_fact_ids=sorted(fact.fact_id for fact in request.facts),
                source_snapshot_ids=validation.safe_snapshot_ids,
                pit_ids=validation.safe_pit_ids,
                verified_numbers=validation.verified_numbers,
                recalculated_metrics=metrics,
                derived_metrics=derived_metrics,
                peer_percentiles=peer_percentiles,
                rule_findings=findings,
                time_series_anomalies=[],
                peer_anomalies=[],
                document_conflicts=validation.conflicts,
                governance_findings=[],
                benign_explanations=[],
                evidence_gaps=evidence_gaps,
                manual_tasks=self._manual_tasks(audit_run_id, evidence_gaps, record.created_at),
                risk_level=self._risk_level(findings),
                hard_blocks=[],
                advisory_only=True,
                rule_versions={rule.rule_id: rule.formula_version for rule in selected_rules},
                model_versions={
                    "deterministic_engine": self.ENGINE_VERSION,
                    "time_series_models": "M3_2_DECIMAL_SERIES_V1",
                    "peer_models": "M3_2_MIDRANK_PERCENTILE_V1",
                    "pyod": "DISABLED_UNTIL_M3_3",
                },
                capability_status={
                    "deterministic_reconciliation": "AVAILABLE_M3_1",
                    "descriptive_ratios": "AVAILABLE_M3_1_NO_FLAG_THRESHOLDS",
                    "cross_period_metrics": "AVAILABLE_M3_2_EXPLICIT_REQUEST",
                    "peer_percentiles": "AVAILABLE_M3_2_VALIDATED_COHORTS",
                    "financial_scores": "AVAILABLE_M3_2_EXPLICIT_REQUEST",
                    "time_series_anomalies": "DISABLED_UNTIL_M3_3",
                    "peer_anomalies": "DISABLED_UNTIL_M3_3",
                    "pyod": "DISABLED_UNTIL_M3_3",
                },
                created_at=record.created_at,
            )
            report_object = self.object_store.put_json(pack.model_dump(mode="json"))
            evidence_ids = sorted(
                {
                    evidence_id
                    for number in validation.verified_numbers
                    for evidence_id in number.evidence_ids
                }
                | {
                    evidence_id
                    for conflict in validation.conflicts
                    for evidence_id in conflict.evidence_ids
                }
                | {
                    evidence_id
                    for metric in derived_metrics
                    for evidence_id in metric.evidence_ids
                }
                | {
                    evidence_id
                    for percentile in peer_percentiles
                    for evidence_id in percentile.evidence_ids
                }
            )
            self.state.register_artifact(
                artifact_id=f"FinancialIntegrityEvidencePack:{audit_run_id}",
                artifact_type="FinancialIntegrityEvidencePack",
                schema_version=pack.schema_version,
                object_hash=report_object.sha256,
                input_hashes=[
                    request_object.sha256,
                    *validation.safe_snapshot_ids,
                    *validation.safe_pit_ids,
                    *evidence_ids,
                    *(
                        snapshot_id
                        for percentile in peer_percentiles
                        for snapshot_id in percentile.source_snapshot_ids
                    ),
                    *(
                        pit_id
                        for percentile in peer_percentiles
                        for pit_id in percentile.pit_ids
                    ),
                ],
            )
            self.repository.checkpoint(audit_run_id, "ARTIFACT_REGISTERED")
            self.repository.complete(
                audit_run_id=audit_run_id,
                attempt_id=attempt_id,
                status=status,
                report_object_hash=report_object.sha256,
                pack=pack,
            )
            return FinancialAuditExecution(pack, report_object.sha256, False)
        except Exception as exc:
            self.repository.fail(audit_run_id, attempt_id, type(exc).__name__)
            raise

    def _select_rules(
        self,
        request: FinancialAuditRequest,
        profile: FinancialIndustryProfileDefinition,
    ) -> list[FinancialRuleDefinition]:
        unknown = sorted(set(request.requested_rule_ids) - set(self._rules))
        if unknown:
            raise ValueError(f"unknown financial rules: {', '.join(unknown)}")
        selected_ids = {
            rule.rule_id for rule in self.rule_registry.rules if rule.default_enabled
        }
        selected_ids.update(request.requested_rule_ids)
        selected_ids.update(profile.excluded_rule_ids)
        return [rule for rule in self.rule_registry.rules if rule.rule_id in selected_ids]

    def _semantic_request(self, request: FinancialAuditRequest) -> dict[str, Any]:
        facts: list[dict[str, Any]] = []
        for fact in sorted(request.facts, key=lambda item: item.fact_id):
            payload = fact.model_dump(mode="json", exclude={"created_at"})
            payload["evidence_ids"] = sorted(fact.evidence_ids)
            facts.append(payload)
        return {
            "schema_version": request.schema_version,
            "company_id": request.company_id,
            "as_of": request.as_of.isoformat(),
            "industry_profile": request.industry_profile.value,
            "facts": facts,
            "requested_rule_ids": sorted(request.requested_rule_ids),
            "series_requests": [
                item.model_dump(mode="json", exclude={"created_at"})
                for item in sorted(request.series_requests, key=lambda value: value.request_id)
            ],
            "peer_cohorts": [
                cohort.model_dump(
                    mode="json",
                    exclude={
                        "created_at": True,
                        "observations": {"__all__": {"created_at"}},
                    },
                )
                for cohort in sorted(request.peer_cohorts, key=lambda value: value.cohort_id)
            ],
            "formal_historical": request.formal_historical,
            "allow_approximated_pit": request.allow_approximated_pit,
        }

    def _audit_run_id(self, request_hash: str) -> str:
        identity = {
            "request_hash": request_hash,
            "rule_registry_version": self.rule_registry.registry_version,
            "industry_profile_version": self.profile_registry.registry_version,
        }
        return f"financial-audit:{sha256_bytes(canonical_json_bytes(identity))}"

    def _validate_and_reconcile_facts(
        self,
        request: FinancialAuditRequest,
        gaps: _GapCollector,
        created_at: datetime,
    ) -> _ValidationResult:
        usable: list[_ValidatedFact] = []
        for fact in sorted(request.facts, key=lambda item: item.fact_id):
            issue_count = len(gaps.values())
            multiplier = _UNIT_MULTIPLIERS.get(fact.unit)
            if multiplier is None:
                gaps.add(
                    FinancialGapType.UNIT_MISMATCH,
                    "MONETARY_FIELD_REQUIRES_CNY_UNIT",
                    period_end=fact.period_end,
                    field_codes=[fact.field_code],
                    fact_ids=[fact.fact_id],
                )
            if (
                fact.field_code is FinancialFieldCode.SHARES_OUTSTANDING
                and fact.unit is not FinancialUnit.SHARES
            ) or (
                fact.field_code is not FinancialFieldCode.SHARES_OUTSTANDING
                and fact.unit is FinancialUnit.SHARES
            ):
                gaps.add(
                    FinancialGapType.UNIT_MISMATCH,
                    "SHARE_FIELD_REQUIRES_SHARES_AND_MONETARY_FIELDS_REQUIRE_CURRENCY",
                    period_end=fact.period_end,
                    field_codes=[fact.field_code],
                    fact_ids=[fact.fact_id],
                )
            expected_statement = _EXPECTED_STATEMENTS[fact.field_code]
            if fact.statement_type is not expected_statement:
                gaps.add(
                    FinancialGapType.STATEMENT_TYPE_MISMATCH,
                    "FIELD_CODE_DOES_NOT_MATCH_STATEMENT_TYPE",
                    period_end=fact.period_end,
                    field_codes=[fact.field_code],
                    fact_ids=[fact.fact_id],
                )
            snapshot = self._snapshot_row(fact.source_snapshot_id)
            if fact.source_snapshot_id is None:
                gaps.add(
                    FinancialGapType.MISSING_SNAPSHOT_REFERENCE,
                    "FACT_HAS_NO_SOURCE_SNAPSHOT_ID",
                    period_end=fact.period_end,
                    field_codes=[fact.field_code],
                    fact_ids=[fact.fact_id],
                )
            elif snapshot is None:
                gaps.add(
                    FinancialGapType.UNKNOWN_SNAPSHOT,
                    "SOURCE_SNAPSHOT_ID_NOT_REGISTERED",
                    period_end=fact.period_end,
                    field_codes=[fact.field_code],
                    fact_ids=[fact.fact_id],
                )
            else:
                availability = datetime.fromisoformat(str(snapshot["availability_at"]))
                if availability > request.as_of:
                    gaps.add(
                        FinancialGapType.SNAPSHOT_NOT_AVAILABLE,
                        "SOURCE_SNAPSHOT_IS_FUTURE_AT_AS_OF",
                        period_end=fact.period_end,
                        field_codes=[fact.field_code],
                        fact_ids=[fact.fact_id],
                    )
                if str(snapshot["fetch_status"]) != FetchStatus.SUCCEEDED.value:
                    gaps.add(
                        FinancialGapType.SNAPSHOT_FETCH_INCOMPLETE,
                        "SOURCE_SNAPSHOT_FETCH_NOT_SUCCEEDED",
                        period_end=fact.period_end,
                        field_codes=[fact.field_code],
                        fact_ids=[fact.fact_id],
                    )
                if not self.object_store.verify(str(snapshot["object_hash"])):
                    gaps.add(
                        FinancialGapType.SNAPSHOT_OBJECT_MISSING,
                        "IMMUTABLE_SOURCE_OBJECT_NOT_VERIFIABLE",
                        period_end=fact.period_end,
                        field_codes=[fact.field_code],
                        fact_ids=[fact.fact_id],
                    )

            pit = self.pit_repository.get(fact.pit_id) if fact.pit_id else None
            if fact.pit_id is None:
                gaps.add(
                    FinancialGapType.MISSING_PIT_REFERENCE,
                    "FACT_HAS_NO_PIT_ID",
                    period_end=fact.period_end,
                    field_codes=[fact.field_code],
                    fact_ids=[fact.fact_id],
                )
            elif pit is None:
                gaps.add(
                    FinancialGapType.UNKNOWN_PIT,
                    "PIT_ID_NOT_REGISTERED",
                    period_end=fact.period_end,
                    field_codes=[fact.field_code],
                    fact_ids=[fact.fact_id],
                )
            else:
                try:
                    PointInTimeService.assert_usable(
                        pit,
                        request.as_of,
                        formal_historical=request.formal_historical,
                        allow_approximated=request.allow_approximated_pit,
                    )
                except ValueError:
                    gaps.add(
                        FinancialGapType.PIT_NOT_USABLE,
                        "PIT_SOURCE_NOT_USABLE_AT_AS_OF",
                        period_end=fact.period_end,
                        field_codes=[fact.field_code],
                        fact_ids=[fact.fact_id],
                    )
                if pit.source_snapshot_id != fact.source_snapshot_id:
                    gaps.add(
                        FinancialGapType.LINEAGE_MISMATCH,
                        "PIT_SNAPSHOT_DOES_NOT_MATCH_FACT_SNAPSHOT",
                        period_end=fact.period_end,
                        field_codes=[fact.field_code],
                        fact_ids=[fact.fact_id],
                    )
                if pit.period_end is not None and pit.period_end != fact.period_end:
                    gaps.add(
                        FinancialGapType.LINEAGE_MISMATCH,
                        "PIT_PERIOD_DOES_NOT_MATCH_FACT_PERIOD",
                        period_end=fact.period_end,
                        field_codes=[fact.field_code],
                        fact_ids=[fact.fact_id],
                    )

            if not fact.evidence_ids:
                gaps.add(
                    FinancialGapType.MISSING_EVIDENCE,
                    "FACT_HAS_NO_EVIDENCE_ID",
                    period_end=fact.period_end,
                    field_codes=[fact.field_code],
                    fact_ids=[fact.fact_id],
                )
            for evidence_id in fact.evidence_ids:
                evidence = self.evidence_repository.get_evidence(evidence_id)
                if evidence is None:
                    gaps.add(
                        FinancialGapType.UNKNOWN_EVIDENCE,
                        "EVIDENCE_ID_NOT_REGISTERED",
                        period_end=fact.period_end,
                        field_codes=[fact.field_code],
                        fact_ids=[fact.fact_id],
                    )
                    continue
                if (
                    evidence.available_to_system_at > request.as_of
                    or (evidence.valid_from is not None and evidence.valid_from > request.as_of)
                    or (evidence.valid_to is not None and evidence.valid_to < request.as_of)
                ):
                    gaps.add(
                        FinancialGapType.EVIDENCE_NOT_USABLE,
                        "EVIDENCE_IS_NOT_VALID_AT_AS_OF",
                        period_end=fact.period_end,
                        field_codes=[fact.field_code],
                        fact_ids=[fact.fact_id],
                    )
                if evidence.evidence_grade is not EvidenceGrade.PRIMARY_OFFICIAL:
                    gaps.add(
                        FinancialGapType.UNSUITABLE_EVIDENCE_GRADE,
                        "FINANCIAL_FACT_REQUIRES_PRIMARY_OFFICIAL_EVIDENCE",
                        period_end=fact.period_end,
                        field_codes=[fact.field_code],
                        fact_ids=[fact.fact_id],
                    )
                if evidence.fact_status is not FactStatus.DIRECT:
                    gaps.add(
                        FinancialGapType.EVIDENCE_NOT_USABLE,
                        "FINANCIAL_FACT_REQUIRES_DIRECT_EVIDENCE",
                        period_end=fact.period_end,
                        field_codes=[fact.field_code],
                        fact_ids=[fact.fact_id],
                    )
                accepted_entities = {request.company_id, f"company:{request.company_id}"}
                if not accepted_entities.intersection(evidence.entity_ids):
                    gaps.add(
                        FinancialGapType.LINEAGE_MISMATCH,
                        "EVIDENCE_ENTITY_DOES_NOT_MATCH_COMPANY",
                        period_end=fact.period_end,
                        field_codes=[fact.field_code],
                        fact_ids=[fact.fact_id],
                    )
                if evidence.snapshot_id != fact.source_snapshot_id:
                    gaps.add(
                        FinancialGapType.LINEAGE_MISMATCH,
                        "EVIDENCE_SNAPSHOT_DOES_NOT_MATCH_FACT_SNAPSHOT",
                        period_end=fact.period_end,
                        field_codes=[fact.field_code],
                        fact_ids=[fact.fact_id],
                    )
                if pit is not None and (
                    pit.source_document_id is not None
                    and evidence.document_id != pit.source_document_id
                ):
                    gaps.add(
                        FinancialGapType.LINEAGE_MISMATCH,
                        "EVIDENCE_DOCUMENT_DOES_NOT_MATCH_PIT_DOCUMENT",
                        period_end=fact.period_end,
                        field_codes=[fact.field_code],
                        fact_ids=[fact.fact_id],
                    )
            if len(gaps.values()) == issue_count and multiplier is not None:
                exponent = fact.reported_value.as_tuple().exponent
                if not isinstance(exponent, int):  # guarded by the finite Decimal schema
                    raise ValueError("financial reported values must be finite")
                usable.append(
                    _ValidatedFact(
                        fact=fact,
                        value_cny=fact.reported_value * multiplier,
                        reporting_quantum_cny=(
                            multiplier
                            * Decimal(1).scaleb(exponent)
                        ),
                    )
                )

        grouped: dict[
            tuple[date, FinancialPeriodType, FinancialFieldCode], list[_ValidatedFact]
        ] = defaultdict(list)
        for item in usable:
            grouped[(item.fact.period_end, item.fact.period_type, item.fact.field_code)].append(
                item
            )
        verified: list[VerifiedFinancialNumber] = []
        conflicts: list[FinancialDocumentConflict] = []
        for key in sorted(grouped, key=lambda item: (item[0], item[1].value, item[2].value)):
            items = grouped[key]
            signatures = {
                (item.value_cny, item.fact.period_start, item.fact.duration_semantics)
                for item in items
            }
            if len(signatures) > 1:
                evidence_ids = sorted(
                    {evidence_id for item in items for evidence_id in item.fact.evidence_ids}
                )
                identity = {
                    "company_id": request.company_id,
                    "period_end": key[0],
                    "period_type": key[1].value,
                    "field_code": key[2].value,
                    "fact_ids": sorted(item.fact.fact_id for item in items),
                    "values": sorted(str(item.value_cny) for item in items),
                }
                conflict = FinancialDocumentConflict(
                    conflict_id=f"financial-conflict:{sha256_bytes(canonical_json_bytes(identity))}",
                    period_end=key[0],
                    period_type=key[1],
                    field_code=key[2],
                    fact_ids=sorted(item.fact.fact_id for item in items),
                    normalized_values_cny=sorted(item.value_cny for item in items),
                    evidence_ids=evidence_ids,
                    created_at=created_at,
                )
                conflicts.append(conflict)
                gaps.add(
                    FinancialGapType.CONFLICTING_VALUES,
                    "OFFICIAL_VALUES_CONFLICT_FOR_SAME_PERIOD_AND_FIELD",
                    period_end=key[0],
                    field_codes=[key[2]],
                    fact_ids=conflict.fact_ids,
                    safe_evidence_ids=evidence_ids,
                )
                continue
            first = items[0]
            verified.append(
                VerifiedFinancialNumber(
                    field_code=first.fact.field_code,
                    statement_type=first.fact.statement_type,
                    period_start=first.fact.period_start,
                    period_end=first.fact.period_end,
                    period_type=first.fact.period_type,
                    duration_semantics=first.fact.duration_semantics,
                    value_cny=first.value_cny,
                    reporting_quantum_cny=max(
                        item.reporting_quantum_cny for item in items
                    ),
                    fact_ids=sorted(item.fact.fact_id for item in items),
                    source_snapshot_ids=sorted(
                        {
                            item.fact.source_snapshot_id
                            for item in items
                            if item.fact.source_snapshot_id is not None
                        }
                    ),
                    pit_ids=sorted(
                        {item.fact.pit_id for item in items if item.fact.pit_id is not None}
                    ),
                    evidence_ids=sorted(
                        {evidence_id for item in items for evidence_id in item.fact.evidence_ids}
                    ),
                    created_at=created_at,
                )
            )
        return _ValidationResult(
            verified_numbers=verified,
            conflicts=sorted(conflicts, key=lambda conflict: conflict.conflict_id),
            safe_snapshot_ids=sorted(
                {
                    item.fact.source_snapshot_id
                    for item in usable
                    if item.fact.source_snapshot_id is not None
                }
            ),
            safe_pit_ids=sorted(
                {item.fact.pit_id for item in usable if item.fact.pit_id is not None}
            ),
        )

    def _snapshot_row(self, snapshot_id: str | None) -> Any | None:
        if snapshot_id is None:
            return None
        with self.state.connect() as connection:
            return connection.execute(
                "SELECT object_hash,availability_at,fetch_status "
                "FROM source_snapshot_index WHERE snapshot_id=?",
                (snapshot_id,),
            ).fetchone()

    def _evaluate_rules(
        self,
        request: FinancialAuditRequest,
        rules: list[FinancialRuleDefinition],
        verified: list[VerifiedFinancialNumber],
        gaps: _GapCollector,
        created_at: datetime,
    ) -> tuple[list[RecalculatedFinancialMetric], list[FinancialRuleFinding]]:
        index = {
            (number.period_end, number.period_type, number.field_code): number
            for number in verified
        }
        periods = sorted(
            {(fact.period_end, fact.period_type) for fact in request.facts},
            key=lambda item: (item[0], item[1].value),
        )
        metrics: list[RecalculatedFinancialMetric] = []
        findings: list[FinancialRuleFinding] = []
        profile = self._profiles[request.industry_profile]
        for rule in rules:
            not_applicable = (
                request.industry_profile in rule.excluded_industries
                or request.industry_profile not in rule.applicable_industries
                or rule.rule_id in profile.excluded_rule_ids
            )
            if not_applicable:
                findings.append(
                    self._finding(
                        rule,
                        None,
                        FinancialFindingStatus.NOT_APPLICABLE,
                        FinancialSeverity.INFO,
                        "RULE_EXCLUDED_BY_INDUSTRY_PROFILE",
                        created_at=created_at,
                        applicability_reason_code=(
                            f"{request.industry_profile.value}_PROFILE_EXCLUSION"
                        ),
                    )
                )
                continue
            if rule.implementation_status not in {
                FinancialImplementationStatus.IMPLEMENTED_M3_1,
                FinancialImplementationStatus.IMPLEMENTED_M3_2,
            }:
                gap = gaps.add(
                    FinancialGapType.CAPABILITY_DISABLED,
                    f"{rule.implementation_status.value}_NOT_IMPLEMENTED",
                    field_codes=rule.required_fields,
                    rule_ids=[rule.rule_id],
                )
                findings.append(
                    self._finding(
                        rule,
                        None,
                        FinancialFindingStatus.INSUFFICIENT_DATA,
                        FinancialSeverity.INFO,
                        "RULE_CAPABILITY_DEFERRED",
                        evidence_gap_ids=[gap.gap_id],
                        created_at=created_at,
                    )
                )
                continue
            if rule.implementation_status is FinancialImplementationStatus.IMPLEMENTED_M3_2:
                metric, finding = self._evaluate_m3_2_rule(
                    request,
                    rule,
                    index,
                    periods,
                    gaps,
                    created_at,
                )
                if metric is not None:
                    metrics.append(metric)
                findings.append(finding)
                continue
            evaluation_periods = periods or [(None, None)]
            for period_end, period_type in evaluation_periods:
                values: dict[FinancialFieldCode, VerifiedFinancialNumber] = {}
                if period_end is not None and period_type is not None:
                    values = {
                        field: index[(period_end, period_type, field)]
                        for field in rule.required_fields
                        if (period_end, period_type, field) in index
                    }
                missing = [field for field in rule.required_fields if field not in values]
                if missing:
                    gap = gaps.add(
                        FinancialGapType.MISSING_FACT,
                        "RULE_REQUIRED_FIELDS_UNAVAILABLE",
                        period_end=period_end,
                        field_codes=missing,
                        rule_ids=[rule.rule_id],
                    )
                    findings.append(
                        self._finding(
                            rule,
                            period_end,
                            FinancialFindingStatus.INSUFFICIENT_DATA,
                            FinancialSeverity.INFO,
                            "REQUIRED_FINANCIAL_FIELDS_MISSING",
                            evidence_gap_ids=[gap.gap_id],
                            created_at=created_at,
                        )
                    )
                    continue
                if period_end is None:  # pragma: no cover - nonempty required_fields imply missing
                    raise RuntimeError("financial rule period unexpectedly missing")
                metric, finding = self._calculate_rule(
                    rule, period_end, values, gaps, created_at
                )
                if metric is not None:
                    metrics.append(metric)
                findings.append(finding)
        return (
            sorted(metrics, key=lambda metric: metric.metric_id),
            sorted(findings, key=lambda finding: finding.finding_id),
        )

    def _evaluate_m3_2_rule(
        self,
        request: FinancialAuditRequest,
        rule: FinancialRuleDefinition,
        index: dict[
            tuple[date, FinancialPeriodType, FinancialFieldCode], VerifiedFinancialNumber
        ],
        periods: list[tuple[date, FinancialPeriodType]],
        gaps: _GapCollector,
        created_at: datetime,
    ) -> tuple[RecalculatedFinancialMetric | None, FinancialRuleFinding]:
        period_type = FinancialPeriodType(str(rule.parameters.get("period_type", "ANNUAL")))
        eligible = sorted({period_end for period_end, kind in periods if kind is period_type})
        if len(eligible) < rule.minimum_periods:
            gap = gaps.add(
                FinancialGapType.INSUFFICIENT_PERIODS,
                "ADVANCED_RULE_REQUIRES_MORE_PERIODS",
                field_codes=rule.required_fields,
                rule_ids=[rule.rule_id],
            )
            return None, self._finding(
                rule,
                eligible[-1] if eligible else None,
                FinancialFindingStatus.INSUFFICIENT_DATA,
                FinancialSeverity.INFO,
                "ADVANCED_RULE_PERIOD_HISTORY_INSUFFICIENT",
                evidence_gap_ids=[gap.gap_id],
                created_at=created_at,
            )
        selected_periods = eligible[-rule.minimum_periods :]
        if len(selected_periods) > 1 and not self._periods_are_contiguous(
            selected_periods, period_type
        ):
            gap = gaps.add(
                FinancialGapType.NON_CONTIGUOUS_PERIODS,
                "ADVANCED_RULE_PERIODS_ARE_NOT_CONTIGUOUS",
                period_end=selected_periods[-1],
                field_codes=rule.required_fields,
                rule_ids=[rule.rule_id],
            )
            return None, self._finding(
                rule,
                selected_periods[-1],
                FinancialFindingStatus.INSUFFICIENT_DATA,
                FinancialSeverity.INFO,
                "ADVANCED_RULE_PERIOD_HISTORY_NON_CONTIGUOUS",
                evidence_gap_ids=[gap.gap_id],
                created_at=created_at,
            )

        by_period: dict[date, dict[FinancialFieldCode, VerifiedFinancialNumber]] = {}
        missing: list[FinancialFieldCode] = []
        for period_end in selected_periods:
            values = {
                field: index[(period_end, period_type, field)]
                for field in rule.required_fields
                if (period_end, period_type, field) in index
            }
            by_period[period_end] = values
            missing.extend(field for field in rule.required_fields if field not in values)
        if missing:
            gap = gaps.add(
                FinancialGapType.MISSING_FACT,
                "ADVANCED_RULE_REQUIRED_FIELDS_UNAVAILABLE",
                period_end=selected_periods[-1],
                field_codes=sorted(set(missing), key=lambda item: item.value),
                rule_ids=[rule.rule_id],
            )
            return None, self._finding(
                rule,
                selected_periods[-1],
                FinancialFindingStatus.INSUFFICIENT_DATA,
                FinancialSeverity.INFO,
                "ADVANCED_RULE_REQUIRED_FIELDS_MISSING",
                evidence_gap_ids=[gap.gap_id],
                created_at=created_at,
            )

        current_numbers = by_period[selected_periods[-1]]
        prior_numbers = (
            by_period[selected_periods[-2]] if len(selected_periods) > 1 else current_numbers
        )
        current = self._advanced_value_map(current_numbers)
        prior = self._advanced_value_map(prior_numbers)
        evidence_ids = sorted(
            {
                evidence_id
                for values in by_period.values()
                for number in values.values()
                for evidence_id in number.evidence_ids
            }
        )
        fact_ids = sorted(
            {
                fact_id
                for values in by_period.values()
                for number in values.values()
                for fact_id in number.fact_ids
            }
        )
        try:
            if rule.calculator_id == "beneish_m_score":
                actual, components = beneish_m_score(current, prior)
                formula = "BENEISH_M_SCORE_8_VARIABLE"
                unit = FinancialUnit.SCORE
            elif rule.calculator_id == "altman_z_score":
                actual, components = altman_z_score(current)
                formula = "ALTMAN_Z_SCORE_PUBLIC_MANUFACTURING_5_FACTOR"
                unit = FinancialUnit.SCORE
            elif rule.calculator_id == "piotroski_f_score":
                actual, components = piotroski_f_score(current, prior)
                formula = "PIOTROSKI_F_SCORE_9_SIGNAL"
                unit = FinancialUnit.SCORE
            elif rule.calculator_id == "sloan_accrual_ratio":
                actual, components = sloan_accrual_ratio(current, prior)
                formula = "(NET_PROFIT-CFO)/AVERAGE_TOTAL_ASSETS"
                unit = FinancialUnit.RATIO
            elif rule.calculator_id == "dupont_decomposition":
                actual, components = dupont_decomposition(current, prior)
                formula = "NET_MARGIN*ASSET_TURNOVER*EQUITY_MULTIPLIER"
                unit = FinancialUnit.RATIO
            else:
                raise ValueError(f"unsupported M3.2 calculator: {rule.calculator_id}")
        except ZeroDivisionError:
            return None, self._finding(
                rule,
                selected_periods[-1],
                FinancialFindingStatus.INSUFFICIENT_DATA,
                FinancialSeverity.INFO,
                "ADVANCED_RULE_NOT_CALCULATED_ZERO_DENOMINATOR",
                evidence_ids=evidence_ids,
                created_at=created_at,
            )

        metric = self._metric(
            rule,
            selected_periods[-1],
            actual,
            unit,
            formula,
            fact_ids,
            evidence_ids,
            created_at,
            input_period_ends=selected_periods,
            component_values=components,
        )
        calculation_only = {
            str(value) for value in rule.parameters.get("calculation_only_industries", [])
        }
        if (
            rule.output_type.value == "METRIC_ONLY"
            or request.industry_profile.value in calculation_only
        ):
            return metric, self._finding(
                rule,
                selected_periods[-1],
                FinancialFindingStatus.CALCULATED,
                FinancialSeverity.INFO,
                "ADVANCED_SCORE_CALCULATED_WITHOUT_INDUSTRY_FLAG_THRESHOLD",
                actual_value=actual,
                unit=unit,
                evidence_ids=evidence_ids,
                created_at=created_at,
            )

        thresholds = rule.parameters.get("thresholds", {})
        if not isinstance(thresholds, dict) or request.industry_profile.value not in thresholds:
            return metric, self._finding(
                rule,
                selected_periods[-1],
                FinancialFindingStatus.CALCULATED,
                FinancialSeverity.INFO,
                "ADVANCED_SCORE_CALCULATED_WITHOUT_INDUSTRY_FLAG_THRESHOLD",
                actual_value=actual,
                unit=unit,
                evidence_ids=evidence_ids,
                created_at=created_at,
            )
        threshold = Decimal(str(thresholds[request.industry_profile.value]))
        direction = str(rule.parameters.get("flag_direction", "HIGH"))
        flagged = (
            actual > threshold
            if direction == "HIGH"
            else actual < threshold
            if direction == "LOW"
            else abs(actual) > threshold
            if direction == "ABS_HIGH"
            else False
        )
        if direction not in {"HIGH", "LOW", "ABS_HIGH"}:
            raise ValueError(f"unknown M3.2 threshold direction: {direction}")
        return metric, self._finding(
            rule,
            selected_periods[-1],
            FinancialFindingStatus.FLAG if flagged else FinancialFindingStatus.PASS,
            rule.severity if flagged else FinancialSeverity.INFO,
            "ADVANCED_SCORE_THRESHOLD_FLAG" if flagged else "ADVANCED_SCORE_THRESHOLD_PASS",
            actual_value=actual,
            threshold_value=threshold,
            unit=unit,
            evidence_ids=evidence_ids,
            created_at=created_at,
        )

    @staticmethod
    def _advanced_value_map(
        values: dict[FinancialFieldCode, VerifiedFinancialNumber],
    ) -> dict[str, Decimal]:
        aliases = {
            "total_assets": FinancialFieldCode.TOTAL_ASSETS,
            "total_liabilities": FinancialFieldCode.TOTAL_LIABILITIES,
            "total_equity": FinancialFieldCode.TOTAL_EQUITY,
            "current_assets": FinancialFieldCode.CURRENT_ASSETS,
            "current_liabilities": FinancialFieldCode.CURRENT_LIABILITIES,
            "retained_earnings": FinancialFieldCode.RETAINED_EARNINGS,
            "ebit": FinancialFieldCode.EBIT,
            "revenue": FinancialFieldCode.REVENUE,
            "operating_cost": FinancialFieldCode.OPERATING_COST,
            "accounts_receivable": FinancialFieldCode.ACCOUNTS_RECEIVABLE,
            "ppe": FinancialFieldCode.PROPERTY_PLANT_EQUIPMENT,
            "depreciation": FinancialFieldCode.DEPRECIATION_AMORTIZATION,
            "sga": FinancialFieldCode.SELLING_GENERAL_ADMIN_EXPENSE,
            "net_profit": FinancialFieldCode.NET_PROFIT_INCOME,
            "cfo": FinancialFieldCode.NET_CASH_OPERATING,
            "long_term_debt": FinancialFieldCode.LONG_TERM_DEBT,
            "market_cap": FinancialFieldCode.MARKET_CAP,
            "shares_outstanding": FinancialFieldCode.SHARES_OUTSTANDING,
        }
        return {
            alias: values[field].value_cny for alias, field in aliases.items() if field in values
        }

    def _derive_series_metrics(
        self,
        requests: list[FinancialSeriesRequest],
        verified: list[VerifiedFinancialNumber],
        gaps: _GapCollector,
        created_at: datetime,
    ) -> list[FinancialDerivedMetric]:
        output: list[FinancialDerivedMetric] = []
        for request in sorted(requests, key=lambda item: item.request_id):
            metric = self._derive_one_series_metric(request, verified, gaps, created_at)
            if metric is not None:
                output.append(metric)
        return sorted(output, key=lambda item: item.derived_metric_id)

    def _derive_one_series_metric(
        self,
        request: FinancialSeriesRequest,
        verified: list[VerifiedFinancialNumber],
        gaps: _GapCollector,
        created_at: datetime,
    ) -> FinancialDerivedMetric | None:
        numbers = sorted(
            [number for number in verified if number.field_code is request.field_code],
            key=lambda item: (item.period_end, item.period_type.value),
        )
        if not numbers:
            gaps.add(
                FinancialGapType.MISSING_FACT,
                "SERIES_FIELD_UNAVAILABLE",
                period_end=request.period_end,
                field_codes=[request.field_code],
            )
            return None
        if request.derivation_type is FinancialDerivationType.PER_SHARE:
            target = self._select_number(numbers, request.period_end)
            if target is None:
                gaps.add(
                    FinancialGapType.MISSING_FACT,
                    "SERIES_TARGET_PERIOD_UNAVAILABLE",
                    period_end=request.period_end,
                    field_codes=[request.field_code],
                )
                return None
            shares = next(
                (
                    number
                    for number in verified
                    if number.field_code is request.shares_field_code
                    and number.period_end == target.period_end
                    and number.period_type is target.period_type
                ),
                None,
            )
            if shares is None:
                gaps.add(
                    FinancialGapType.MISSING_FACT,
                    "PER_SHARE_DENOMINATOR_UNAVAILABLE",
                    period_end=target.period_end,
                    field_codes=[request.shares_field_code],
                )
                return None
            if shares.value_cny == 0:
                gaps.add(
                    FinancialGapType.MODEL_INPUT_INVALID,
                    "PER_SHARE_DENOMINATOR_IS_ZERO",
                    period_end=target.period_end,
                    field_codes=[request.shares_field_code],
                    fact_ids=shares.fact_ids,
                    safe_evidence_ids=shares.evidence_ids,
                )
                return None
            value = decimal_ratio(target.value_cny, shares.value_cny)
            points = [
                _SeriesPoint(
                    target.period_end,
                    target.value_cny,
                    target.fact_ids,
                    target.evidence_ids,
                ),
                _SeriesPoint(
                    shares.period_end,
                    shares.value_cny,
                    shares.fact_ids,
                    shares.evidence_ids,
                ),
            ]
            formula = f"{request.field_code.value}/{request.shares_field_code.value}"
            unit = FinancialUnit.CNY_PER_SHARE
            period_end = target.period_end
        elif request.derivation_type is FinancialDerivationType.YEAR_OVER_YEAR:
            target_number = self._select_number(numbers, request.period_end)
            if target_number is None:
                gaps.add(
                    FinancialGapType.MISSING_FACT,
                    "SERIES_TARGET_PERIOD_UNAVAILABLE",
                    period_end=request.period_end,
                    field_codes=[request.field_code],
                )
                return None
            if target_number.period_type is FinancialPeriodType.QUARTERLY:
                quarter_points = self._quarter_series_points(numbers, request, gaps)
                point_by_date = {point.period_end: point for point in quarter_points}
                current = point_by_date.get(target_number.period_end)
                comparison = point_by_date.get(
                    target_number.period_end.replace(year=target_number.period_end.year - 1)
                )
            else:
                current = self._point_from_number(target_number)
                comparison_number = next(
                    (
                        number
                        for number in numbers
                        if number.period_type is target_number.period_type
                        and number.period_end
                        == target_number.period_end.replace(
                            year=target_number.period_end.year - 1
                        )
                    ),
                    None,
                )
                comparison = (
                    self._point_from_number(comparison_number)
                    if comparison_number is not None
                    else None
                )
            if current is None or comparison is None:
                gaps.add(
                    FinancialGapType.INSUFFICIENT_PERIODS,
                    "YEAR_OVER_YEAR_COMPARISON_UNAVAILABLE",
                    period_end=target_number.period_end,
                    field_codes=[request.field_code],
                )
                return None
            try:
                value = percentage_change(current.value, comparison.value)
            except ZeroDivisionError:
                return None
            points = [current, comparison]
            formula = "(CURRENT-PRIOR_YEAR_COMPARABLE)/ABS(PRIOR_YEAR_COMPARABLE)"
            unit = FinancialUnit.RATIO
            period_end = current.period_end
        else:
            quarter_points = self._quarter_series_points(numbers, request, gaps)
            if not quarter_points:
                return None
            by_date = {point.period_end: point for point in quarter_points}
            target_date = request.period_end or quarter_points[-1].period_end
            current = by_date.get(target_date)
            if current is None:
                gaps.add(
                    FinancialGapType.MISSING_FACT,
                    "SERIES_TARGET_QUARTER_UNAVAILABLE",
                    period_end=target_date,
                    field_codes=[request.field_code],
                )
                return None
            if request.derivation_type is FinancialDerivationType.QUARTER_OVER_QUARTER:
                previous_date = self._previous_quarter_end(target_date)
                comparison = by_date.get(previous_date) if previous_date is not None else None
                if comparison is None:
                    gaps.add(
                        FinancialGapType.INSUFFICIENT_PERIODS,
                        "QUARTER_OVER_QUARTER_COMPARISON_UNAVAILABLE",
                        period_end=target_date,
                        field_codes=[request.field_code],
                    )
                    return None
                try:
                    value = percentage_change(current.value, comparison.value)
                except ZeroDivisionError:
                    return None
                points = [current, comparison]
                formula = "(CURRENT_QUARTER-PRIOR_QUARTER)/ABS(PRIOR_QUARTER)"
                unit = FinancialUnit.RATIO
                period_end = target_date
            elif request.derivation_type is FinancialDerivationType.TTM:
                points = [current]
                cursor = target_date
                for _ in range(3):
                    previous_date = self._previous_quarter_end(cursor)
                    if previous_date is None or previous_date not in by_date:
                        gaps.add(
                            FinancialGapType.INSUFFICIENT_PERIODS,
                            "TTM_REQUIRES_FOUR_CONTIGUOUS_QUARTERS",
                            period_end=target_date,
                            field_codes=[request.field_code],
                        )
                        return None
                    points.append(by_date[previous_date])
                    cursor = previous_date
                value = sum((point.value for point in points), Decimal(0))
                formula = "SUM(LATEST_FOUR_STANDALONE_QUARTERS)"
                unit = FinancialUnit.CNY
                period_end = target_date
            else:  # pragma: no cover - exhaustive enum handling
                raise ValueError(f"unsupported series derivation: {request.derivation_type}")

        fact_ids = sorted({fact_id for point in points for fact_id in point.fact_ids})
        evidence_ids = sorted(
            {evidence_id for point in points for evidence_id in point.evidence_ids}
        )
        identity = {
            "request_id": request.request_id,
            "derivation_type": request.derivation_type.value,
            "field_code": request.field_code.value,
            "period_end": period_end,
            "value": value,
            "fact_ids": fact_ids,
        }
        return FinancialDerivedMetric(
            derived_metric_id=(
                f"financial-derived:{sha256_bytes(canonical_json_bytes(identity))}"
            ),
            request_id=request.request_id,
            metric_key=f"{request.derivation_type.value}:{request.field_code.value}",
            derivation_type=request.derivation_type,
            field_code=request.field_code,
            period_end=period_end,
            comparison_period_ends=sorted({point.period_end for point in points}),
            value=value,
            unit=unit,
            formula=formula,
            input_fact_ids=fact_ids,
            evidence_ids=evidence_ids,
            created_at=created_at,
        )

    def _quarter_series_points(
        self,
        numbers: list[VerifiedFinancialNumber],
        request: FinancialSeriesRequest,
        gaps: _GapCollector,
    ) -> list[_SeriesPoint]:
        quarterly = {
            number.period_end: number
            for number in numbers
            if number.period_type is FinancialPeriodType.QUARTERLY
        }
        output: list[_SeriesPoint] = []
        for period_end, number in sorted(quarterly.items()):
            if number.statement_type is FinancialStatementType.BALANCE_SHEET:
                output.append(self._point_from_number(number))
                continue
            if number.duration_semantics is FinancialDurationSemantics.STANDALONE_PERIOD:
                output.append(self._point_from_number(number))
                continue
            if number.duration_semantics is FinancialDurationSemantics.YEAR_TO_DATE:
                if period_end.month == 3:
                    output.append(self._point_from_number(number))
                    continue
                previous_date = self._previous_quarter_end(period_end)
                previous = quarterly.get(previous_date) if previous_date is not None else None
                if (
                    previous is None
                    or previous.period_end.year != period_end.year
                    or previous.duration_semantics is not FinancialDurationSemantics.YEAR_TO_DATE
                ):
                    gaps.add(
                        FinancialGapType.INSUFFICIENT_PERIODS,
                        "YEAR_TO_DATE_QUARTER_REQUIRES_PRIOR_CUMULATIVE_PERIOD",
                        period_end=period_end,
                        field_codes=[request.field_code],
                    )
                    continue
                output.append(
                    _SeriesPoint(
                        period_end=period_end,
                        value=number.value_cny - previous.value_cny,
                        fact_ids=sorted(set(number.fact_ids + previous.fact_ids)),
                        evidence_ids=sorted(
                            set(number.evidence_ids + previous.evidence_ids)
                        ),
                    )
                )
                continue
            gaps.add(
                FinancialGapType.AMBIGUOUS_PERIOD_SEMANTICS,
                "QUARTERLY_DURATION_VALUE_REQUIRES_EXPLICIT_SEMANTICS",
                period_end=period_end,
                field_codes=[request.field_code],
                fact_ids=number.fact_ids,
                safe_evidence_ids=number.evidence_ids,
            )
        return sorted(output, key=lambda point: point.period_end)

    @staticmethod
    def _point_from_number(number: VerifiedFinancialNumber) -> _SeriesPoint:
        return _SeriesPoint(
            period_end=number.period_end,
            value=number.value_cny,
            fact_ids=number.fact_ids,
            evidence_ids=number.evidence_ids,
        )

    @staticmethod
    def _select_number(
        numbers: list[VerifiedFinancialNumber], target: date | None
    ) -> VerifiedFinancialNumber | None:
        if target is not None:
            candidates = [number for number in numbers if number.period_end == target]
        else:
            candidates = numbers
        if not candidates:
            return None
        preference = {
            FinancialPeriodType.ANNUAL: 2,
            FinancialPeriodType.SEMIANNUAL: 1,
            FinancialPeriodType.QUARTERLY: 0,
        }
        return max(candidates, key=lambda item: (item.period_end, preference[item.period_type]))

    @staticmethod
    def _previous_quarter_end(value: date) -> date | None:
        mapping = {
            (3, 31): date(value.year - 1, 12, 31),
            (6, 30): date(value.year, 3, 31),
            (9, 30): date(value.year, 6, 30),
            (12, 31): date(value.year, 9, 30),
        }
        return mapping.get((value.month, value.day))

    @classmethod
    def _periods_are_contiguous(
        cls, periods: list[date], period_type: FinancialPeriodType
    ) -> bool:
        if len(periods) < 2:
            return True
        if period_type is FinancialPeriodType.ANNUAL:
            return all(
                current.year == previous.year + 1
                for previous, current in zip(periods[:-1], periods[1:], strict=True)
            )
        if period_type is FinancialPeriodType.QUARTERLY:
            return all(
                cls._previous_quarter_end(current) == previous
                for previous, current in zip(periods[:-1], periods[1:], strict=True)
            )
        return all(
            current.year == previous.year + 1
            for previous, current in zip(periods[:-1], periods[1:], strict=True)
        )

    def _evaluate_peer_cohorts(
        self,
        audit_run_id: str,
        request: FinancialAuditRequest,
        metrics: list[RecalculatedFinancialMetric],
        derived: list[FinancialDerivedMetric],
        gaps: _GapCollector,
        created_at: datetime,
    ) -> list[FinancialPeerPercentile]:
        metric_index: dict[
            str, tuple[date, Decimal, FinancialUnit, str, list[str]]
        ] = {}
        for metric in metrics:
            candidate = (
                metric.period_end,
                metric.value,
                metric.unit,
                metric.formula_version,
                metric.evidence_ids,
            )
            if metric.rule_id not in metric_index or candidate[0] > metric_index[metric.rule_id][0]:
                metric_index[metric.rule_id] = candidate
        for metric in derived:
            candidate = (
                metric.period_end,
                metric.value,
                metric.unit,
                metric.formula_version,
                metric.evidence_ids,
            )
            if (
                metric.metric_key not in metric_index
                or candidate[0] > metric_index[metric.metric_key][0]
            ):
                metric_index[metric.metric_key] = candidate

        output: list[FinancialPeerPercentile] = []
        for cohort in sorted(request.peer_cohorts, key=lambda item: item.cohort_id):
            subject = metric_index.get(cohort.metric_id)
            if subject is None:
                gaps.add(
                    FinancialGapType.MISSING_FACT,
                    "PEER_COHORT_SUBJECT_METRIC_UNAVAILABLE",
                    rule_ids=[cohort.metric_id],
                )
                continue
            period_end, company_value, unit, formula_version, subject_evidence = subject
            if formula_version != cohort.formula_version:
                gaps.add(
                    FinancialGapType.PEER_COHORT_MISMATCH,
                    "PEER_COHORT_FORMULA_VERSION_MISMATCH",
                    period_end=period_end,
                    rule_ids=[cohort.metric_id],
                    safe_evidence_ids=subject_evidence,
                )
                continue
            usable = []
            lineage_failed = False
            for observation in cohort.observations:
                if observation.unit is not unit or observation.period_end != period_end:
                    lineage_failed = True
                    continue
                if not self._peer_observation_is_usable(request, observation):
                    lineage_failed = True
                    continue
                usable.append(observation)
            if lineage_failed:
                gaps.add(
                    FinancialGapType.PEER_LINEAGE_INVALID,
                    "PEER_COHORT_CONTAINS_UNUSABLE_OBSERVATIONS",
                    period_end=period_end,
                    rule_ids=[cohort.metric_id],
                    safe_evidence_ids=subject_evidence,
                )
                continue
            if len(usable) < cohort.minimum_sample_size:
                gaps.add(
                    FinancialGapType.INSUFFICIENT_PEER_SAMPLE,
                    "PEER_COHORT_BELOW_MINIMUM_SAMPLE_SIZE",
                    period_end=period_end,
                    rule_ids=[cohort.metric_id],
                    safe_evidence_ids=subject_evidence,
                )
                continue
            percentile = midrank_percentile(
                company_value, [observation.value for observation in usable]
            )
            source_snapshot_ids = sorted(
                {
                    snapshot_id
                    for observation in usable
                    for snapshot_id in observation.source_snapshot_ids
                }
            )
            pit_ids = sorted(
                {pit_id for observation in usable for pit_id in observation.pit_ids}
            )
            evidence_ids = sorted(
                set(subject_evidence)
                | {
                    evidence_id
                    for observation in usable
                    for evidence_id in observation.evidence_ids
                }
            )
            identity = {
                "cohort_id": cohort.cohort_id,
                "company_id": request.company_id,
                "metric_id": cohort.metric_id,
                "period_end": period_end,
                "value": company_value,
                "peers": sorted(observation.observation_id for observation in usable),
            }
            output.append(
                FinancialPeerPercentile(
                    percentile_id=(
                        f"financial-peer-percentile:"
                        f"{sha256_bytes(canonical_json_bytes(identity))}"
                    ),
                    cohort_id=cohort.cohort_id,
                    metric_id=cohort.metric_id,
                    formula_version=cohort.formula_version,
                    period_end=period_end,
                    company_value=company_value,
                    unit=unit,
                    percentile=percentile,
                    sample_size=len(usable),
                    peer_company_ids=sorted(observation.company_id for observation in usable),
                    source_snapshot_ids=source_snapshot_ids,
                    pit_ids=pit_ids,
                    evidence_ids=evidence_ids,
                    created_at=created_at,
                )
            )
            cohort_object = self.object_store.put_json(
                cohort.model_dump(
                    mode="json",
                    exclude={
                        "created_at": True,
                        "observations": {"__all__": {"created_at"}},
                    },
                )
            )
            self.repository.register_peer_cohort(
                audit_run_id=audit_run_id,
                cohort=cohort,
                object_hash=cohort_object.sha256,
            )
        return sorted(output, key=lambda item: item.percentile_id)

    def _peer_observation_is_usable(self, request: FinancialAuditRequest, observation: Any) -> bool:
        if observation.available_at > request.as_of:
            return False
        for snapshot_id in observation.source_snapshot_ids:
            row = self._snapshot_row(snapshot_id)
            if row is None:
                return False
            if datetime.fromisoformat(str(row["availability_at"])) > request.as_of:
                return False
            if str(row["fetch_status"]) != FetchStatus.SUCCEEDED.value:
                return False
            if not self.object_store.verify(str(row["object_hash"])):
                return False
        for pit_id in observation.pit_ids:
            pit = self.pit_repository.get(pit_id)
            if pit is None:
                return False
            try:
                PointInTimeService.assert_usable(
                    pit,
                    request.as_of,
                    formal_historical=request.formal_historical,
                    allow_approximated=request.allow_approximated_pit,
                )
            except ValueError:
                return False
        accepted_entities = {observation.company_id, f"company:{observation.company_id}"}
        for evidence_id in observation.evidence_ids:
            evidence = self.evidence_repository.get_evidence(evidence_id)
            if evidence is None:
                return False
            if evidence.evidence_grade is not EvidenceGrade.PRIMARY_OFFICIAL:
                return False
            if evidence.fact_status is not FactStatus.DIRECT:
                return False
            if not accepted_entities.intersection(evidence.entity_ids):
                return False
            if evidence.snapshot_id not in observation.source_snapshot_ids:
                return False
        return True

    def _calculate_rule(
        self,
        rule: FinancialRuleDefinition,
        period_end: date,
        values: dict[FinancialFieldCode, VerifiedFinancialNumber],
        gaps: _GapCollector,
        created_at: datetime,
    ) -> tuple[RecalculatedFinancialMetric | None, FinancialRuleFinding]:
        evidence_ids = sorted(
            {evidence_id for value in values.values() for evidence_id in value.evidence_ids}
        )
        fact_ids = sorted({fact_id for value in values.values() for fact_id in value.fact_ids})
        if rule.calculator_id == "balance_identity":
            actual = balance_identity_difference(
                values[FinancialFieldCode.TOTAL_ASSETS].value_cny,
                values[FinancialFieldCode.TOTAL_LIABILITIES].value_cny,
                values[FinancialFieldCode.TOTAL_EQUITY].value_cny,
            )
            formula = "TOTAL_ASSETS-TOTAL_LIABILITIES-TOTAL_EQUITY"
        elif rule.calculator_id == "cash_identity":
            actual = cash_identity_difference(
                values[FinancialFieldCode.CASH_ENDING].value_cny,
                values[FinancialFieldCode.CASH_BEGINNING].value_cny,
                values[FinancialFieldCode.NET_CASH_OPERATING].value_cny,
                values[FinancialFieldCode.NET_CASH_INVESTING].value_cny,
                values[FinancialFieldCode.NET_CASH_FINANCING].value_cny,
                values[FinancialFieldCode.EXCHANGE_EFFECT].value_cny,
            )
            formula = "CASH_ENDING-(CASH_BEGINNING+CFO+CFI+CFF+FX)"
        elif rule.calculator_id == "net_profit_cross_statement":
            actual = (
                values[FinancialFieldCode.NET_PROFIT_INCOME].value_cny
                - values[FinancialFieldCode.NET_PROFIT_CASH_FLOW].value_cny
            )
            formula = "NET_PROFIT_INCOME-NET_PROFIT_CASH_FLOW"
        elif rule.calculator_id == "ratio":
            numerator_code = FinancialFieldCode(str(rule.parameters["numerator_field"]))
            denominator_code = FinancialFieldCode(str(rule.parameters["denominator_field"]))
            denominator = values[denominator_code].value_cny
            if denominator == 0:
                return None, self._finding(
                    rule,
                    period_end,
                    FinancialFindingStatus.INSUFFICIENT_DATA,
                    FinancialSeverity.INFO,
                    "RATIO_NOT_CALCULATED_ZERO_DENOMINATOR",
                    evidence_ids=evidence_ids,
                    created_at=created_at,
                )
            actual = decimal_ratio(values[numerator_code].value_cny, denominator)
            formula = f"{numerator_code.value}/{denominator_code.value}"
            metric = self._metric(
                rule,
                period_end,
                actual,
                FinancialUnit.RATIO,
                formula,
                fact_ids,
                evidence_ids,
                created_at,
            )
            finding = self._finding(
                rule,
                period_end,
                FinancialFindingStatus.CALCULATED,
                FinancialSeverity.INFO,
                "DESCRIPTIVE_METRIC_CALCULATED_NO_FLAG_THRESHOLD",
                actual_value=actual,
                unit=FinancialUnit.RATIO,
                evidence_ids=evidence_ids,
                created_at=created_at,
            )
            return metric, finding
        else:  # pragma: no cover - config validation tests guard known M3.1 calculators
            raise ValueError(f"unsupported M3.1 calculator: {rule.calculator_id}")

        tolerance_units = Decimal(str(rule.parameters.get("tolerance_reporting_units", "0")))
        tolerance = reporting_rounding_tolerance(
            [value.reporting_quantum_cny for value in values.values()], tolerance_units
        )
        flagged = abs(actual) > tolerance
        metric = self._metric(
            rule,
            period_end,
            actual,
            FinancialUnit.CNY,
            formula,
            fact_ids,
            evidence_ids,
            created_at,
        )
        finding = self._finding(
            rule,
            period_end,
            FinancialFindingStatus.FLAG if flagged else FinancialFindingStatus.PASS,
            rule.severity if flagged else FinancialSeverity.INFO,
            "IDENTITY_MISMATCH_EXCEEDS_ROUNDING_TOLERANCE"
            if flagged
            else "IDENTITY_RECONCILED_WITHIN_ROUNDING_TOLERANCE",
            actual_value=actual,
            threshold_value=tolerance,
            unit=FinancialUnit.CNY,
            evidence_ids=evidence_ids,
            created_at=created_at,
        )
        return metric, finding

    def _metric(
        self,
        rule: FinancialRuleDefinition,
        period_end: date,
        value: Decimal,
        unit: FinancialUnit,
        formula: str,
        fact_ids: list[str],
        evidence_ids: list[str],
        created_at: datetime,
        *,
        input_period_ends: list[date] | None = None,
        component_values: dict[str, Decimal] | None = None,
    ) -> RecalculatedFinancialMetric:
        identity = {
            "rule_id": rule.rule_id,
            "formula_version": rule.formula_version,
            "period_end": period_end,
            "value": value,
            "fact_ids": fact_ids,
        }
        return RecalculatedFinancialMetric(
            metric_id=f"financial-metric:{sha256_bytes(canonical_json_bytes(identity))}",
            rule_id=rule.rule_id,
            period_end=period_end,
            value=value,
            unit=unit,
            formula=formula,
            formula_version=rule.formula_version,
            input_field_codes=rule.required_fields,
            input_fact_ids=fact_ids,
            evidence_ids=evidence_ids,
            input_period_ends=sorted(set(input_period_ends or [period_end])),
            component_values=component_values or {},
            created_at=created_at,
        )

    def _finding(
        self,
        rule: FinancialRuleDefinition,
        period_end: date | None,
        status: FinancialFindingStatus,
        severity: FinancialSeverity,
        message_code: str,
        *,
        actual_value: Decimal | None = None,
        threshold_value: Decimal | None = None,
        unit: FinancialUnit | None = None,
        evidence_ids: list[str] | None = None,
        evidence_gap_ids: list[str] | None = None,
        applicability_reason_code: str | None = None,
        created_at: datetime,
    ) -> FinancialRuleFinding:
        identity = {
            "rule_id": rule.rule_id,
            "formula_version": rule.formula_version,
            "period_end": period_end,
            "status": status.value,
            "message_code": message_code,
            "actual_value": actual_value,
            "threshold_value": threshold_value,
            "evidence_ids": sorted(evidence_ids or []),
            "evidence_gap_ids": sorted(evidence_gap_ids or []),
            "applicability_reason_code": applicability_reason_code,
        }
        return FinancialRuleFinding(
            finding_id=f"financial-finding:{sha256_bytes(canonical_json_bytes(identity))}",
            rule_id=rule.rule_id,
            formula_version=rule.formula_version,
            period_end=period_end,
            status=status,
            severity=severity,
            message_code=message_code,
            actual_value=actual_value,
            threshold_value=threshold_value,
            unit=unit,
            evidence_ids=sorted(evidence_ids or []),
            evidence_gap_ids=sorted(evidence_gap_ids or []),
            applicability_reason_code=applicability_reason_code,
            created_at=created_at,
        )

    def _manual_tasks(
        self,
        audit_run_id: str,
        gaps: list[FinancialEvidenceGap],
        created_at: datetime,
    ) -> list[FinancialManualTask]:
        tasks: list[FinancialManualTask] = []
        for gap in gaps:
            if gap.gap_type is FinancialGapType.CAPABILITY_DISABLED:
                continue
            if gap.gap_type is FinancialGapType.CONFLICTING_VALUES:
                action = "RESOLVE_OFFICIAL_DOCUMENT_CONFLICT"
            elif gap.gap_type in {
                FinancialGapType.SNAPSHOT_NOT_AVAILABLE,
                FinancialGapType.PIT_NOT_USABLE,
                FinancialGapType.EVIDENCE_NOT_USABLE,
            }:
                action = "USE_AS_OF_COMPATIBLE_OFFICIAL_SOURCE"
            elif gap.gap_type is FinancialGapType.SNAPSHOT_OBJECT_MISSING:
                action = "RESTORE_IMMUTABLE_SOURCE_OBJECT"
            elif gap.gap_type in {
                FinancialGapType.UNIT_MISMATCH,
                FinancialGapType.STATEMENT_TYPE_MISMATCH,
                FinancialGapType.LINEAGE_MISMATCH,
            }:
                action = "CORRECT_FINANCIAL_FACT_METADATA"
            else:
                action = "PROVIDE_PRIMARY_OFFICIAL_STATEMENT_EVIDENCE"
            identity = {"audit_run_id": audit_run_id, "gap_id": gap.gap_id, "action": action}
            tasks.append(
                FinancialManualTask(
                    task_id=f"financial-task:{sha256_bytes(canonical_json_bytes(identity))}",
                    audit_run_id=audit_run_id,
                    reason_code=gap.gap_type.value,
                    required_action_code=action,
                    related_gap_ids=[gap.gap_id],
                    created_at=created_at,
                )
            )
        return sorted(tasks, key=lambda task: task.task_id)

    @staticmethod
    def _coverage_status(
        verified: list[VerifiedFinancialNumber], gaps: list[FinancialEvidenceGap]
    ) -> FinancialCoverageStatus:
        if gaps and not verified:
            return FinancialCoverageStatus.BLOCKED
        if gaps:
            return FinancialCoverageStatus.PARTIAL
        return FinancialCoverageStatus.COMPLETE

    @staticmethod
    def _risk_level(findings: list[FinancialRuleFinding]) -> FinancialRiskLevel:
        flagged = [
            finding for finding in findings if finding.status is FinancialFindingStatus.FLAG
        ]
        if any(finding.severity is FinancialSeverity.HIGH for finding in flagged):
            return FinancialRiskLevel.HIGH
        if any(
            finding.severity in {FinancialSeverity.MEDIUM, FinancialSeverity.LOW}
            for finding in flagged
        ):
            return FinancialRiskLevel.MEDIUM
        return FinancialRiskLevel.LOW
