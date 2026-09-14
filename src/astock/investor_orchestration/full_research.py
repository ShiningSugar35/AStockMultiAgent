"""Fail-closed recommendation-level research, portfolio, publication and replay gates."""

from __future__ import annotations

import re
import uuid
from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import datetime
from decimal import ROUND_DOWN, Decimal
from itertools import islice
from pathlib import Path
from typing import Any, Literal, cast

import yaml

from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.investor_orchestration.models import InvestorRequestEnvelope
from astock.investor_orchestration.store import InvestorOrchestrationStore
from astock.investor_orchestration.utils import content_hash, utc_now
from astock.schemas.entry_quality import EntryQualityState
from astock.schemas.full_research import (
    CandidateRankingEntry,
    ChallengerAssessment,
    CompanyFundamentalSnapshot,
    EvidenceConflict,
    ExecutionInstruction,
    FinancialQualityAssessment,
    FullResearchComponentType,
    FullResearchDAGReceipt,
    FullResearchNode,
    FullResearchNodeExecution,
    FullResearchNodeStatus,
    FullResearchRequestContract,
    GovernanceAssessment,
    IndustryResearchOutcome,
    MacroResearchOutcome,
    ModelPortfolioAssumptions,
    NewsEventResearchPack,
    PointInTimeSnapshot,
    PortfolioAssumptionSource,
    PortfolioConstructionSnapshot,
    PortfolioPositionPlan,
    PortfolioRiskAudit,
    PublicationDecision,
    QuantFactorResearchPack,
    RecommendationReevaluationRecord,
    RecommendationResearchReceipt,
    RecommendationValuationSnapshot,
    SourceFamily,
    SourceLineageEntry,
)

_POLICY_PATH = Path("configs/full_research_recommendation.yaml")

_COMPONENT_MODELS: dict[FullResearchComponentType, type[Any]] = {
    FullResearchComponentType.MACRO: MacroResearchOutcome,
    FullResearchComponentType.INDUSTRY: IndustryResearchOutcome,
    FullResearchComponentType.FUNDAMENTAL: CompanyFundamentalSnapshot,
    FullResearchComponentType.FINANCIAL_QUALITY: FinancialQualityAssessment,
    FullResearchComponentType.GOVERNANCE: GovernanceAssessment,
    FullResearchComponentType.EVENT_RESEARCH: NewsEventResearchPack,
    FullResearchComponentType.QUANT_FACTOR: QuantFactorResearchPack,
    FullResearchComponentType.CHALLENGER: ChallengerAssessment,
}


class FullResearchPolicy:
    """Validated access to the checked-in recommendation policy."""

    def __init__(self, raw: Mapping[str, Any]) -> None:
        self.raw = dict(raw)
        if self.raw.get("schema_version") != "full-research-recommendation-policy-v1":
            raise ValueError("unsupported full research policy schema")
        if self.raw.get("policy_id") != "full-research-recommendation-v1":
            raise ValueError("unexpected full research policy identity")
        if self.raw.get("status") != "ENABLED":
            raise ValueError("full research recommendation policy must be ENABLED")
        configured_nodes = tuple(
            FullResearchNode(value) for value in self.raw["mandatory_dag"]["nodes"]
        )
        if configured_nodes != tuple(FullResearchNode):
            raise ValueError(
                "configured Mandatory Research DAG differs from the canonical node order"
            )
        allowed = set(self.raw["mandatory_dag"]["allowed_statuses"])
        if allowed != {item.value for item in FullResearchNodeStatus}:
            raise ValueError("configured research node statuses differ from the canonical contract")
        multipliers = self.raw.get("portfolio", {}).get("entry_quality_weight_multipliers", {})
        if not isinstance(multipliers, dict):
            raise ValueError("entry-quality portfolio multipliers must be a mapping")
        if multipliers and set(multipliers) != {state.value for state in EntryQualityState}:
            raise ValueError("entry-quality portfolio multipliers must cover every canonical state")
        for state, value in multipliers.items():
            multiplier = Decimal(str(value))
            if not str(state) or not Decimal("0") <= multiplier <= Decimal("1"):
                raise ValueError("entry-quality portfolio multiplier is outside 0..1")

    @classmethod
    def load(cls, project_root: Path | None = None) -> FullResearchPolicy:
        root = project_root or Path(__file__).resolve().parents[3]
        payload = yaml.safe_load((root / _POLICY_PATH).read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("full research policy must be a mapping")
        return cls(payload)

    @property
    def policy_id(self) -> str:
        return str(self.raw["policy_id"])

    @property
    def mandatory_nodes(self) -> tuple[FullResearchNode, ...]:
        return tuple(FullResearchNode(value) for value in self.raw["mandatory_dag"]["nodes"])

    @property
    def degraded_allowed(self) -> tuple[FullResearchNode, ...]:
        return tuple(
            FullResearchNode(value) for value in self.raw["mandatory_dag"]["degraded_allowed"]
        )

    @property
    def critical_source_families(self) -> tuple[SourceFamily, ...]:
        return tuple(SourceFamily(value) for value in self.raw["critical_source_families"])

    @property
    def quote_freshness_sla_seconds(self) -> int:
        return int(self.raw["execution"]["current_quote_freshness_sla_seconds"])

    @property
    def portfolio(self) -> Mapping[str, Any]:
        return self.raw["portfolio"]


class FullResearchRecommendationService:
    """Build and certify recommendation-level projections over canonical artifacts."""

    def __init__(
        self,
        store: InvestorOrchestrationStore | None = None,
        *,
        project_root: Path | None = None,
        policy: FullResearchPolicy | None = None,
        objects: ObjectStore | None = None,
    ) -> None:
        self.project_root = project_root or Path(__file__).resolve().parents[3]
        self.policy = policy or FullResearchPolicy.load(self.project_root)
        self.store = store
        self.state = StateStore(store.path) if store is not None else None
        self.objects = (
            objects
            if objects is not None
            else ObjectStore(store.path.parent / "objects" / "sha256")
            if store is not None
            else None
        )

    def register_component(
        self,
        component_type: FullResearchComponentType,
        payload: Mapping[str, Any],
    ) -> dict[str, str]:
        """Register one typed Full Research component in the canonical stores."""

        if self.state is None or self.objects is None:
            raise ValueError("component registration requires canonical local stores")
        model_type = _COMPONENT_MODELS[component_type]
        component = model_type.model_validate(payload)
        input_hashes: set[str] = set()

        source_ids = tuple(getattr(component, "source_artifact_ids", ()))
        source_hashes = tuple(getattr(component, "source_object_hashes", ()))
        if len(source_ids) != len(source_hashes):
            raise ValueError("component source artifact/hash lineage must be one-to-one")
        for artifact_id, expected_hash in zip(source_ids, source_hashes, strict=True):
            record = self.state.artifact_record(artifact_id)
            if record is None or str(record["object_hash"]) != expected_hash:
                raise ValueError("component source artifact/hash binding is not canonical")
            self.objects.get_bytes(expected_hash)
            input_hashes.add(expected_hash)

        raw_fact_ids = tuple(getattr(component, "raw_fact_artifact_ids", ()))
        for artifact_id in raw_fact_ids:
            record = self.state.artifact_record(artifact_id)
            if record is None:
                raise ValueError("component raw-fact artifact is unavailable")
            digest = str(record["object_hash"])
            self.objects.get_bytes(digest)
            input_hashes.add(digest)

        from astock.evidence.repository import EvidenceRepository

        evidence_repository = EvidenceRepository(self.state)
        for evidence_id in tuple(getattr(component, "evidence_ids", ())):
            evidence = evidence_repository.get_evidence(evidence_id)
            if evidence is None:
                raise ValueError("component references unknown Evidence")
            self.objects.get_bytes(evidence.excerpt_object_sha256)
            input_hashes.add(evidence.excerpt_object_sha256)

        reference = self.objects.put_json(component.model_dump(mode="json"))
        artifact_id = f"{model_type.__name__}:{content_hash(component.model_dump(mode='json'))}"
        self.state.register_artifact(
            artifact_id=artifact_id,
            artifact_type=model_type.__name__,
            schema_version=str(component.schema_version),
            object_hash=reference.sha256,
            input_hashes=sorted(input_hashes),
        )
        return {
            "artifact_id": artifact_id,
            "artifact_type": model_type.__name__,
            "object_hash": reference.sha256,
        }

    def request_contract(self, request: InvestorRequestEnvelope) -> FullResearchRequestContract:
        request = InvestorRequestEnvelope.model_validate(request.model_dump())
        if request.normalized_intent.value != "FULL_RESEARCH_RECOMMENDATION":
            raise ValueError("recommendation request did not enter FULL_RESEARCH_RECOMMENDATION")
        defaults = self.policy.raw["model_portfolio_defaults"]
        supplied = request.metadata.get("portfolio_assumptions")
        if isinstance(supplied, Mapping):
            payload = dict(supplied)
            source = PortfolioAssumptionSource.USER
            user_constraints = {
                str(key): value
                for key, value in payload.items()
                if isinstance(value, (str, int, float, bool))
            }
        else:
            payload = dict(defaults)
            source = PortfolioAssumptionSource.MODEL_PORTFOLIO
            user_constraints = {}
        risk_profile_raw = str(payload.get("risk_profile", defaults["risk_profile"])).upper()
        if risk_profile_raw not in {"LOW", "MEDIUM", "HIGH"}:
            raise ValueError("portfolio risk profile must be LOW, MEDIUM or HIGH")
        risk_profile = cast(Literal["LOW", "MEDIUM", "HIGH"], risk_profile_raw)
        assumptions = ModelPortfolioAssumptions(
            source=source,
            capital_rmb=Decimal(str(payload.get("capital_rmb", defaults["capital_rmb"]))),
            risk_profile=risk_profile,
            horizon_min_months=int(
                payload.get("horizon_min_months", defaults["horizon_min_months"])
            ),
            horizon_max_months=int(
                payload.get("horizon_max_months", defaults["horizon_max_months"])
            ),
            long_only=bool(payload.get("long_only", defaults["long_only"])),
            margin_allowed=bool(payload.get("margin_allowed", defaults["margin_allowed"])),
            short_allowed=bool(payload.get("short_allowed", defaults["short_allowed"])),
            cash_allowed=bool(payload.get("cash_allowed", defaults["cash_allowed"])),
            target_position_min=int(
                payload.get("target_position_min", defaults["target_position_min"])
            ),
            target_position_max=int(
                payload.get("target_position_max", defaults["target_position_max"])
            ),
            user_constraints=user_constraints,
        )
        requested_count = self._requested_count(request.raw_text)
        return FullResearchRequestContract(
            request_id=request.request_id,
            as_of_timestamp=request.evidence_cutoff,
            raw_text_hash=content_hash(request.raw_text),
            portfolio_assumptions=assumptions,
            requested_instruments=tuple(request.entity_ids),
            requested_count=requested_count,
            current_recommendation=request.research_mode == "CURRENT",
            decision_context=(
                "EXISTING_HOLDING"
                if request.metadata.get("full_research_holding_context")
                else "NEW_ALLOCATION"
            ),
        )

    @staticmethod
    def _requested_count(raw_text: str) -> int | None:
        match = re.search(r"(?:选|挑|推荐)\s*(\d+)\s*(?:只|支|个)?(?:股|股票)", raw_text)
        if match is None:
            return None
        value = int(match.group(1))
        return value if value > 0 else None

    def point_in_time_snapshot(
        self,
        request: InvestorRequestEnvelope,
        sources: Sequence[SourceLineageEntry],
        *,
        conflicts: Sequence[EvidenceConflict] = (),
    ) -> PointInTimeSnapshot:
        request = InvestorRequestEnvelope.model_validate(request.model_dump())
        visible: list[SourceLineageEntry] = []
        leakage = 0
        for source in sources:
            normalized = SourceLineageEntry.model_validate(source.model_dump())
            if normalized.available_to_system_at > request.evidence_cutoff:
                leakage += 1
                continue
            visible.append(normalized)
        families = {item.family for item in visible}
        required = set(self.policy.critical_source_families)
        coverage = (
            Decimal(len(required & families)) / Decimal(len(required)) if required else Decimal("1")
        )
        open_conflicts = [item for item in conflicts if item.status == "OPEN"]
        status = (
            FullResearchNodeStatus.PASS
            if not leakage and not open_conflicts and coverage == Decimal("1")
            else FullResearchNodeStatus.FAIL
            if leakage
            else FullResearchNodeStatus.BLOCKED
        )
        body = {
            "request_id": request.request_id,
            "as_of": request.evidence_cutoff,
            "source_ids": sorted(item.source_id for item in visible),
            "conflicts": sorted(item.conflict_id for item in conflicts),
        }
        return PointInTimeSnapshot(
            snapshot_id=f"pit-{uuid.uuid5(uuid.NAMESPACE_URL, content_hash(body))}",
            request_id=request.request_id,
            as_of_timestamp=request.evidence_cutoff,
            sources=tuple(sorted(visible, key=lambda item: item.source_id)),
            conflicts=tuple(sorted(conflicts, key=lambda item: item.conflict_id)),
            critical_source_families=self.policy.critical_source_families,
            lineage_coverage=coverage,
            point_in_time_leakage_count=leakage,
            status=status,
        )

    @staticmethod
    def apply_candidate_vetoes(
        candidates: Sequence[CandidateRankingEntry],
        financial_quality: Sequence[FinancialQualityAssessment],
        governance: Sequence[GovernanceAssessment],
    ) -> tuple[CandidateRankingEntry, ...]:
        financial = {item.instrument_id: item for item in financial_quality}
        governance_by_id = {item.instrument_id: item for item in governance}
        result: list[CandidateRankingEntry] = []
        for candidate in candidates:
            reasons = list(candidate.rejection_reasons)
            financial_veto = bool(
                candidate.accounting_critical_veto
                or (
                    financial.get(candidate.instrument_id)
                    and financial[candidate.instrument_id].critical_veto
                )
            )
            governance_veto = bool(
                candidate.governance_critical_veto
                or (
                    governance_by_id.get(candidate.instrument_id)
                    and governance_by_id[candidate.instrument_id].critical_veto
                )
            )
            if financial_veto:
                reasons.append("FINANCIAL_CRITICAL_VETO")
            if governance_veto:
                reasons.append("GOVERNANCE_CRITICAL_VETO")
            eligible = candidate.eligible and not financial_veto and not governance_veto
            if not eligible and not reasons:
                reasons.append("CANDIDATE_NOT_ADMITTED")
            result.append(
                candidate.model_copy(
                    update={
                        "eligible": eligible,
                        "rejection_reasons": tuple(dict.fromkeys(reasons)),
                        "accounting_critical_veto": financial_veto,
                        "governance_critical_veto": governance_veto,
                    }
                )
            )
        return tuple(
            sorted(
                result,
                key=lambda item: (
                    not item.eligible,
                    -item.expected_return,
                    -item.quality,
                    -item.valuation,
                    item.entry_timing_risk,
                    -(item.entry_quality_score or Decimal("0")),
                    -item.evidence_confidence,
                    item.instrument_id,
                ),
            )
        )

    def build_portfolio(
        self,
        contract: FullResearchRequestContract,
        candidates: Sequence[CandidateRankingEntry],
        valuations: Sequence[RecommendationValuationSnapshot],
        *,
        pairwise_correlations: Mapping[str, Decimal] | None = None,
    ) -> PortfolioConstructionSnapshot:
        config = self.policy.portfolio
        capital = contract.portfolio_assumptions.capital_rmb
        max_single = Decimal(str(config["max_single_weight"]))
        max_industry = Decimal(str(config["max_industry_weight"]))
        max_factor = Decimal(str(config["max_factor_abs_exposure"]))
        max_corr = Decimal(str(config["max_pairwise_correlation"]))
        lot = int(config["lot_size_shares"])
        transaction_cost_rate = Decimal(str(config["transaction_cost_bps"])) / Decimal("10000")
        slippage_rate = Decimal(str(config["default_slippage_bps"])) / Decimal("10000")
        entry_quality_multipliers = {
            str(state): Decimal(str(value))
            for state, value in config.get("entry_quality_weight_multipliers", {}).items()
        }
        target_max = min(
            contract.portfolio_assumptions.target_position_max,
            int(config["target_position_max"]),
        )
        eligible = tuple(islice((item for item in candidates if item.eligible), target_max))
        positions, industry_weights, remaining_capital = self._build_positions(
            capital=capital,
            eligible=eligible,
            valuations={item.instrument_id: item for item in valuations},
            max_single=max_single,
            max_industry=max_industry,
            lot=lot,
            transaction_cost_rate=transaction_cost_rate,
            slippage_rate=slippage_rate,
            entry_quality_multipliers=entry_quality_multipliers,
        )
        factor_exposures = self._portfolio_factor_exposures(positions, eligible)
        violations = self._portfolio_constraint_violations(
            positions=positions,
            eligible=eligible,
            industry_weights=industry_weights,
            factor_exposures=factor_exposures,
            pairwise_correlations=pairwise_correlations or {},
            max_single=max_single,
            max_industry=max_industry,
            max_factor=max_factor,
            max_corr=max_corr,
        )
        cash = max(Decimal("0"), remaining_capital)
        return PortfolioConstructionSnapshot(
            capital=capital,
            positions=positions,
            cash=cash,
            cash_weight=cash / capital,
            industry_weights=dict(sorted(industry_weights.items())),
            factor_exposures=dict(sorted(factor_exposures.items())),
            estimated_transaction_cost=sum(
                (item.estimated_cost for item in positions), Decimal("0")
            ),
            estimated_slippage=sum((item.estimated_slippage for item in positions), Decimal("0")),
            constraint_violations=tuple(sorted(violations)),
        )

    @staticmethod
    def _build_positions(
        *,
        capital: Decimal,
        eligible: tuple[CandidateRankingEntry, ...],
        valuations: Mapping[str, RecommendationValuationSnapshot],
        max_single: Decimal,
        max_industry: Decimal,
        lot: int,
        transaction_cost_rate: Decimal,
        slippage_rate: Decimal,
        entry_quality_multipliers: Mapping[str, Decimal],
    ) -> tuple[tuple[PortfolioPositionPlan, ...], dict[str, Decimal], Decimal]:
        positions: list[PortfolioPositionPlan] = []
        industry_weights: defaultdict[str, Decimal] = defaultdict(lambda: Decimal("0"))
        remaining_capital = capital
        raw_equal = Decimal("0") if not eligible else Decimal("1") / Decimal(len(eligible))
        for candidate in eligible:
            valuation = valuations.get(candidate.instrument_id)
            if valuation is None:
                continue
            industry_room = max(
                Decimal("0"), max_industry - industry_weights[candidate.industry_id]
            )
            timing_multiplier = entry_quality_multipliers.get(
                candidate.entry_quality_state.value
                if candidate.entry_quality_state is not None
                else "",
                Decimal("1"),
            )
            planned_weight = min(raw_equal, max_single, industry_room) * timing_multiplier
            if planned_weight <= 0:
                continue
            planned_amount = min(capital * planned_weight, remaining_capital)
            all_in_unit = (
                valuation.current_price
                * Decimal(lot)
                * (Decimal("1") + transaction_cost_rate + slippage_rate)
            )
            shares = (
                int((planned_amount / all_in_unit).to_integral_value(rounding=ROUND_DOWN)) * lot
            )
            if shares <= 0:
                continue
            notional = valuation.current_price * Decimal(shares)
            transaction_cost = notional * transaction_cost_rate
            slippage = notional * slippage_rate
            total_amount = notional + transaction_cost + slippage
            actual_weight = total_amount / capital
            positions.append(
                PortfolioPositionPlan(
                    instrument_id=candidate.instrument_id,
                    industry_id=candidate.industry_id,
                    target_weight=actual_weight,
                    target_amount=total_amount,
                    reference_price=valuation.current_price,
                    target_shares=shares,
                    lot_size=lot,
                    estimated_cost=transaction_cost,
                    estimated_slippage=slippage,
                )
            )
            remaining_capital -= total_amount
            industry_weights[candidate.industry_id] += actual_weight
        return tuple(positions), dict(industry_weights), remaining_capital

    @staticmethod
    def _portfolio_factor_exposures(
        positions: tuple[PortfolioPositionPlan, ...],
        eligible: tuple[CandidateRankingEntry, ...],
    ) -> dict[str, Decimal]:
        by_candidate = {item.instrument_id: item for item in eligible}
        factor_names = (
            "value",
            "quality",
            "growth",
            "momentum",
            "low_volatility",
            "liquidity",
            "size",
            "earnings_revision",
            "profitability",
            "crowding",
        )
        exposures: dict[str, Decimal] = {}
        for name in factor_names:
            values: list[tuple[Decimal, Decimal]] = []
            for position in positions:
                factor_value = getattr(
                    by_candidate[position.instrument_id].factor_snapshot,
                    name,
                )
                if factor_value is None:
                    raise ValueError(
                        f"selected portfolio position lacks required factor exposure: {name}"
                    )
                values.append((position.target_weight, Decimal(str(factor_value))))
            exposures[name.upper()] = sum(
                (weight * value for weight, value in values),
                Decimal("0"),
            )
        return exposures

    @staticmethod
    def _portfolio_constraint_violations(
        *,
        positions: tuple[PortfolioPositionPlan, ...],
        eligible: tuple[CandidateRankingEntry, ...],
        industry_weights: Mapping[str, Decimal],
        factor_exposures: Mapping[str, Decimal],
        pairwise_correlations: Mapping[str, Decimal],
        max_single: Decimal,
        max_industry: Decimal,
        max_factor: Decimal,
        max_corr: Decimal,
    ) -> set[str]:
        violations: set[str] = set()
        if any(position.target_weight > max_single for position in positions):
            violations.add("MAX_SINGLE_WEIGHT")
        if any(value > max_industry for value in industry_weights.values()):
            violations.add("MAX_INDUSTRY_WEIGHT")
        violations.update(
            f"FACTOR_CONCENTRATION:{factor}"
            for factor, value in factor_exposures.items()
            if abs(value) > max_factor
        )
        by_candidate = {item.instrument_id: item for item in eligible}
        dominant: defaultdict[str, Decimal] = defaultdict(lambda: Decimal("0"))
        for position in positions:
            for exposure in by_candidate[position.instrument_id].factor_snapshot.dominant_exposures:
                dominant[exposure] += position.target_weight
        violations.update(
            f"DOMINANT_FACTOR_CONCENTRATION:{exposure}"
            for exposure, weight in dominant.items()
            if weight > max_factor
        )
        violations.update(
            f"PAIRWISE_CORRELATION:{pair}"
            for pair, value in pairwise_correlations.items()
            if Decimal(str(value)) > max_corr
        )
        return violations

    def audit_portfolio_risk(
        self,
        portfolio: PortfolioConstructionSnapshot,
        *,
        portfolio_beta: Decimal,
        expected_volatility: Decimal,
        expected_shortfall: Decimal,
        max_drawdown_proxy: Decimal,
        max_order_adv_fraction: Decimal,
        turnover: Decimal,
        source_artifact_ids: Sequence[str],
        source_object_hashes: Sequence[str],
    ) -> PortfolioRiskAudit:
        """Audit canonical portfolio metrics without recomputing a parallel risk model."""

        config = self.policy.portfolio
        violations = list(portfolio.constraint_violations)
        adv_limit = Decimal(str(config["max_order_adv_fraction"]))
        liquidity_pass = max_order_adv_fraction <= adv_limit
        if not liquidity_pass:
            violations.append("LIQUIDITY_CAPACITY")
        correlation_pass = not any(
            value.startswith("PAIRWISE_CORRELATION:") for value in violations
        )
        industry_pass = "MAX_INDUSTRY_WEIGHT" not in violations
        single_name_pass = "MAX_SINGLE_WEIGHT" not in violations
        factor_pass = not any(
            value.startswith(("FACTOR_CONCENTRATION:", "DOMINANT_FACTOR_CONCENTRATION:"))
            for value in violations
        )
        status = FullResearchNodeStatus.PASS if not violations else FullResearchNodeStatus.FAIL
        return PortfolioRiskAudit(
            status=status,
            portfolio_beta=portfolio_beta,
            expected_volatility=expected_volatility,
            expected_shortfall=expected_shortfall,
            max_drawdown_proxy=max_drawdown_proxy,
            liquidity_capacity_pass=liquidity_pass,
            max_order_adv_fraction=max_order_adv_fraction,
            turnover=turnover,
            transaction_cost=portfolio.estimated_transaction_cost,
            slippage=portfolio.estimated_slippage,
            correlation_constraint_pass=correlation_pass,
            industry_constraint_pass=industry_pass,
            factor_constraint_pass=factor_pass,
            single_name_constraint_pass=single_name_pass,
            violations=tuple(sorted(set(violations))),
            source_artifact_ids=tuple(source_artifact_ids),
            source_object_hashes=tuple(source_object_hashes),
        )

    def execution_plans(
        self,
        as_of: datetime,
        portfolio: PortfolioConstructionSnapshot,
        valuations: Sequence[RecommendationValuationSnapshot],
        quote_sources: Mapping[str, SourceLineageEntry],
        *,
        technical_support: Mapping[str, Decimal] | None = None,
        atr: Mapping[str, Decimal] | None = None,
        thesis_invalidations: Mapping[str, Sequence[str]] | None = None,
        add_conditions: Mapping[str, Sequence[str]] | None = None,
        exit_conditions: Mapping[str, Sequence[str]] | None = None,
        event_exit_conditions: Mapping[str, Sequence[str]] | None = None,
    ) -> tuple[ExecutionInstruction, ...]:
        valuation_by_id = {item.instrument_id: item for item in valuations}
        support = technical_support or {}
        atr_values = atr or {}
        invalidations = thesis_invalidations or {}
        add_by_id = add_conditions or {}
        exit_by_id = exit_conditions or {}
        event_by_id = event_exit_conditions or {}
        sla = self.policy.quote_freshness_sla_seconds
        minimum_mos = Decimal(
            str(self.policy.raw["execution"].get("minimum_base_case_margin_of_safety", 0.15))
        )
        result: list[ExecutionInstruction] = []
        for position in portfolio.positions:
            valuation = valuation_by_id[position.instrument_id]
            quote = quote_sources[position.instrument_id]
            age = (
                max(0, int((as_of - quote.observed_at).total_seconds()))
                if quote.observed_at
                else sla + 1
            )
            fresh = bool(
                quote.observed_at is not None
                and quote.available_to_system_at <= as_of
                and age <= sla
            )
            scenarios = {item.scenario: item for item in valuation.scenarios}
            bear = scenarios["BEAR"].per_share_value
            base = scenarios["BASE"].per_share_value
            maximum_price = base * (Decimal("1") - minimum_mos)
            center = support.get(
                position.instrument_id, min(valuation.current_price, maximum_price)
            )
            volatility = atr_values.get(
                position.instrument_id, max(Decimal("0.01"), center * Decimal("0.03"))
            )
            buy_low = max(Decimal("0.01"), max(bear, center - volatility))
            buy_high = min(maximum_price, center + volatility)
            if buy_high < buy_low:
                buy_low = buy_high = max(Decimal("0.01"), min(maximum_price, center))
            thesis = tuple(invalidations.get(position.instrument_id, ()))
            if not thesis:
                thesis = ("核心盈利、资产质量或治理假设出现可验证的实质恶化",)
            instant = fresh
            result.append(
                ExecutionInstruction(
                    instrument_id=position.instrument_id,
                    quote_observed_at=quote.observed_at or quote.available_to_system_at,
                    quote_available_to_system_at=quote.available_to_system_at,
                    reference_price=valuation.current_price,
                    quote_age_seconds=age,
                    quote_freshness_sla_seconds=sla,
                    quote_fresh=fresh,
                    initial_shares=max(
                        position.lot_size,
                        position.target_shares // 2 // position.lot_size * position.lot_size,
                    )
                    if instant and position.target_shares
                    else None,
                    target_shares=position.target_shares if instant else None,
                    buy_range_low=buy_low,
                    buy_range_high=buy_high,
                    maximum_acceptable_price=maximum_price,
                    add_conditions=tuple(
                        add_by_id.get(
                            position.instrument_id, ("估值安全边际扩大且核心投资逻辑继续成立",)
                        )
                    ),
                    reduce_exit_conditions=tuple(
                        exit_by_id.get(
                            position.instrument_id, ("风险预算被突破或相对赔率显著恶化",)
                        )
                    ),
                    thesis_invalidation_conditions=thesis,
                    time_stop_condition="在计划投资期限内催化剂未兑现且预期收益不再覆盖风险预算时复核退出",
                    valuation_exit_condition="价格达到或超过基础/乐观情景合理价值且预期收益不足时减仓或退出",
                    event_exit_conditions=tuple(
                        event_by_id.get(
                            position.instrument_id,
                            ("重大负面公告、监管或治理事件使原投资逻辑失效",),
                        )
                    ),
                    instant_quantity_allowed=instant,
                )
            )
        return tuple(result)

    def dag_receipt(
        self,
        request: InvestorRequestEnvelope,
        statuses: Mapping[FullResearchNode | str, FullResearchNodeStatus | str],
        *,
        artifact_ids: Mapping[FullResearchNode | str, Sequence[str]] | None = None,
        evidence_ids: Mapping[FullResearchNode | str, Sequence[str]] | None = None,
        reasons: Mapping[FullResearchNode | str, str] | None = None,
        at: datetime | None = None,
    ) -> FullResearchDAGReceipt:
        request = InvestorRequestEnvelope.model_validate(request.model_dump())
        normalized = {
            FullResearchNode(key): FullResearchNodeStatus(value) for key, value in statuses.items()
        }
        missing = set(self.policy.mandatory_nodes) - set(normalized)
        extra = set(normalized) - set(self.policy.mandatory_nodes)
        if missing or extra:
            missing_names = sorted(item.value for item in missing)
            extra_names = sorted(item.value for item in extra)
            message = (
                f"Mandatory Research DAG must be explicit; missing={missing_names}, "
                f"extra={extra_names}"
            )
            raise ValueError(message)
        artifacts = {
            FullResearchNode(key): tuple(value) for key, value in (artifact_ids or {}).items()
        }
        evidence = {
            FullResearchNode(key): tuple(value) for key, value in (evidence_ids or {}).items()
        }
        reason_map = {FullResearchNode(key): value for key, value in (reasons or {}).items()}
        timestamp = at or utc_now()
        executions: list[FullResearchNodeExecution] = []
        accepted = 0
        for node in self.policy.mandatory_nodes:
            status = normalized[node]
            if status == FullResearchNodeStatus.PASS or (
                status == FullResearchNodeStatus.DEGRADED and node in self.policy.degraded_allowed
            ):
                accepted += 1
            executions.append(
                FullResearchNodeExecution(
                    node=node,
                    status=status,
                    artifact_ids=artifacts.get(node, ()),
                    evidence_ids=evidence.get(node, ()),
                    reason=reason_map.get(node) if status != FullResearchNodeStatus.PASS else None,
                    started_at=timestamp,
                    completed_at=timestamp,
                )
            )
        coverage = Decimal(accepted) / Decimal(len(self.policy.mandatory_nodes))
        body = {
            "request_id": request.request_id,
            "as_of": request.evidence_cutoff,
            "policy": self.policy.policy_id,
            "executions": [item.model_dump(mode="json") for item in executions],
        }
        return FullResearchDAGReceipt(
            dag_id=f"full-research-dag-{uuid.uuid5(uuid.NAMESPACE_URL, content_hash(body))}",
            policy_id=self.policy.policy_id,
            request_id=request.request_id,
            as_of_timestamp=request.evidence_cutoff,
            executions=tuple(executions),
            degraded_allowed=self.policy.degraded_allowed,
            mandatory_coverage=coverage,
            publication_ready=accepted == len(self.policy.mandatory_nodes),
        )

    def publication_decision(
        self,
        dag: FullResearchDAGReceipt,
        pit: PointInTimeSnapshot,
        portfolio: PortfolioConstructionSnapshot,
        risk_audit: PortfolioRiskAudit,
        execution_plans: Sequence[ExecutionInstruction],
        candidates: Sequence[CandidateRankingEntry],
    ) -> PublicationDecision:
        reasons: list[str] = []
        formal = (
            dag.publication_ready
            and pit.status == FullResearchNodeStatus.PASS
            and risk_audit.status == FullResearchNodeStatus.PASS
        )
        if portfolio.constraint_violations:
            formal = False
            reasons.extend(portfolio.constraint_violations)
        if any(
            item.eligible and (item.accounting_critical_veto or item.governance_critical_veto)
            for item in candidates
        ):
            formal = False
            reasons.append("CRITICAL_VETO_LEAK")
        has_positions = bool(portfolio.positions)
        instant = (
            formal
            and has_positions
            and len(execution_plans) == len(portfolio.positions)
            and all(item.quote_fresh and item.instant_quantity_allowed for item in execution_plans)
        )
        if formal and has_positions and not instant:
            reasons.append("CURRENT_QUOTE_FRESHNESS_SLA_NOT_MET")
            status = "CONDITIONAL_ONLY"
        elif formal:
            status = "PUBLISH"
        else:
            status = "BLOCKED"
            if not dag.publication_ready:
                reasons.append("MANDATORY_RESEARCH_DAG_NOT_READY")
            if pit.status != FullResearchNodeStatus.PASS:
                reasons.append("POINT_IN_TIME_EVIDENCE_NOT_READY")
            if risk_audit.status != FullResearchNodeStatus.PASS:
                reasons.append("RISK_AUDIT_NOT_READY")
        return PublicationDecision(
            status=status,
            formal_recommendation_allowed=formal,
            instant_trade_parameters_allowed=instant,
            reasons=tuple(dict.fromkeys(reasons)),
        )

    def seal_receipt(self, payload: Mapping[str, Any]) -> RecommendationResearchReceipt:
        """Seal and optionally persist an immutable, self-contained research receipt."""

        body = dict(payload)
        body.pop("receipt_id", None)
        body.pop("receipt_hash", None)
        body.setdefault("schema_version", "recommendation-research-receipt-v1")
        body.setdefault("created_at", body.get("as_of", utc_now()))
        provisional = RecommendationResearchReceipt(
            **body,
            receipt_id="RecommendationResearchReceipt:PENDING",
            receipt_hash="0" * 64,
        )
        canonical_body = provisional.model_dump(exclude={"receipt_id", "receipt_hash"})
        digest = content_hash(canonical_body)
        receipt = RecommendationResearchReceipt.model_validate(
            {
                **provisional.model_dump(),
                "receipt_id": f"RecommendationResearchReceipt:{digest}",
                "receipt_hash": digest,
            }
        )
        self.verify_receipt(receipt)
        if self.state is not None and self.objects is not None:
            input_hashes = sorted(set(receipt.input_artifact_hashes.values()))
            for artifact_id, expected_hash in receipt.input_artifact_hashes.items():
                record = self.state.artifact_record(artifact_id)
                if record is None or record["object_hash"] != expected_hash:
                    raise ValueError("recommendation input artifact/hash binding is not canonical")
                self.objects.get_bytes(expected_hash)
            for source in receipt.source_manifest:
                if source.artifact_id is not None:
                    record = self.state.artifact_record(source.artifact_id)
                    if record is None or record["object_hash"] != source.object_hash:
                        raise ValueError("source lineage artifact/hash binding is not canonical")
                    self.objects.get_bytes(source.object_hash)
            reference = self.objects.put_json(receipt.model_dump(mode="json"))
            self.state.register_artifact(
                artifact_id=receipt.receipt_id,
                artifact_type="RecommendationResearchReceipt",
                schema_version=receipt.schema_version,
                object_hash=reference.sha256,
                input_hashes=input_hashes,
            )
            if self.store is not None:
                import logging
                import sqlite3

                from astock.investor_orchestration.entry_watch import enroll_waiting_entries
                from astock.investor_orchestration.subjects import ResearchSubjectRegistryService

                try:
                    enroll_waiting_entries(
                        receipt,
                        ResearchSubjectRegistryService(self.store),
                        self.state,
                        self.objects,
                    )
                except (ValueError, OSError, sqlite3.Error) as exc:
                    # Optional monitoring cannot turn a valid sealed analysis into NEEDS_INFO.
                    logging.getLogger(__name__).warning(
                        "Optional entry-watch enrollment deferred: %s", type(exc).__name__
                    )
            if receipt.publication.formal_recommendation_allowed and receipt.portfolio.positions:
                from astock.investor_orchestration.models import PortfolioLane, SubjectEventKind
                from astock.investor_orchestration.subjects import ResearchSubjectRegistryService

                assert self.store is not None
                registry = ResearchSubjectRegistryService(self.store)
                for position in receipt.portfolio.positions:
                    registry.append(
                        instrument_id=position.instrument_id,
                        event_type=SubjectEventKind.RECOMMENDED,
                        lane=PortfolioLane.RESEARCH,
                        request_id=receipt.request_id,
                        artifact_id=receipt.receipt_id,
                        reason="sealed full-research recommendation",
                        available_at=receipt.as_of,
                        idempotency_key=content_hash(
                            {
                                "receipt_id": receipt.receipt_id,
                                "instrument_id": position.instrument_id,
                                "event": SubjectEventKind.RECOMMENDED.value,
                            }
                        ),
                    )
                    registry.append(
                        instrument_id=position.instrument_id,
                        event_type=SubjectEventKind.MONITOR_ENROLLED,
                        lane=PortfolioLane.WATCHLIST,
                        request_id=receipt.request_id,
                        artifact_id=receipt.receipt_id,
                        reason="full-research recommendation tracking",
                        available_at=receipt.as_of,
                        idempotency_key=content_hash(
                            {
                                "receipt_id": receipt.receipt_id,
                                "instrument_id": position.instrument_id,
                                "event": SubjectEventKind.MONITOR_ENROLLED.value,
                            }
                        ),
                    )
        return receipt

    def verify_receipt(self, receipt: RecommendationResearchReceipt) -> dict[str, Any]:
        receipt = RecommendationResearchReceipt.model_validate(receipt.model_dump())
        self._verify_receipt_identity(receipt)
        self._verify_receipt_dag_lineage(receipt)
        self._verify_receipt_valuations(receipt)
        self._verify_receipt_portfolio(receipt)
        self._verify_receipt_holding_lineage(receipt)
        self._verify_receipt_publication(receipt)
        return {
            "status": "PASS",
            "request_id": receipt.request_id,
            "receipt_id": receipt.receipt_id,
            "receipt_hash": receipt.receipt_hash,
            "candidate_count": len(receipt.candidate_rankings),
            "position_count": len(receipt.portfolio.positions),
            "publication_status": receipt.publication.status,
        }

    def _verify_receipt_identity(self, receipt: RecommendationResearchReceipt) -> None:
        semantic_hash = content_hash(receipt.model_dump(exclude={"receipt_id", "receipt_hash"}))
        if semantic_hash != receipt.receipt_hash:
            raise ValueError("recommendation receipt semantic hash mismatch")
        if receipt.receipt_id != f"RecommendationResearchReceipt:{receipt.receipt_hash}":
            raise ValueError("recommendation receipt identity/hash mismatch")
        if tuple(item.node for item in receipt.dag.executions) != self.policy.mandatory_nodes:
            raise ValueError("recommendation receipt DAG order differs from policy")

    @staticmethod
    def _verify_receipt_dag_lineage(receipt: RecommendationResearchReceipt) -> None:
        declared_artifacts = set(receipt.input_artifact_hashes)
        declared_artifacts.update(
            source.artifact_id
            for source in receipt.source_manifest
            if source.artifact_id is not None
        )
        declared_evidence = {
            source.evidence_id
            for source in receipt.source_manifest
            if source.evidence_id is not None
        }
        for execution in receipt.dag.executions:
            if not set(execution.artifact_ids) <= declared_artifacts:
                raise ValueError("Mandatory Research DAG references an undeclared artifact")
            if not set(execution.evidence_ids) <= declared_evidence:
                raise ValueError("Mandatory Research DAG references undeclared Evidence")

    @staticmethod
    def _verify_receipt_valuations(receipt: RecommendationResearchReceipt) -> None:
        for valuation in receipt.valuations:
            weighted = sum(
                (item.probability * item.expected_return for item in valuation.scenarios),
                Decimal("0"),
            )
            if abs(weighted - valuation.expected_return_mean) > Decimal("0.000001"):
                raise ValueError("valuation expected-return distribution cannot be replayed")
            base = next(item for item in valuation.scenarios if item.scenario == "BASE")
            expected_mos = (base.per_share_value - valuation.current_price) / base.per_share_value
            if abs(expected_mos - valuation.margin_of_safety) > Decimal("0.000001"):
                raise ValueError("valuation margin of safety cannot be replayed")

    @staticmethod
    def _verify_receipt_portfolio(receipt: RecommendationResearchReceipt) -> None:
        rankings = {item.instrument_id: item for item in receipt.candidate_rankings}
        valuations = {item.instrument_id: item for item in receipt.valuations}
        for position in receipt.portfolio.positions:
            ranking = rankings.get(position.instrument_id)
            if ranking is None or not ranking.eligible:
                raise ValueError("portfolio replay found an ineligible position")
            valuation = valuations.get(position.instrument_id)
            if valuation is None or valuation.current_price != position.reference_price:
                raise ValueError("portfolio price differs from frozen valuation price")
            if position.target_shares % position.lot_size:
                raise ValueError("portfolio replay violates lot rounding")

    @staticmethod
    def _verify_receipt_holding_lineage(receipt: RecommendationResearchReceipt) -> None:
        for review in receipt.holding_reviews:
            if (
                receipt.input_artifact_hashes.get(review.source_artifact_id)
                != review.source_object_hash
            ):
                raise ValueError("holding review artifact/hash binding is not canonical")

    @staticmethod
    def _verify_receipt_publication(receipt: RecommendationResearchReceipt) -> None:
        if receipt.publication.formal_recommendation_allowed:
            if receipt.dag.mandatory_coverage != Decimal("1") or not receipt.dag.publication_ready:
                raise ValueError("publication gate bypassed incomplete Mandatory Research")
            if receipt.pit_snapshot.point_in_time_leakage_count:
                raise ValueError("publication gate bypassed point-in-time leakage")
            if receipt.pit_snapshot.lineage_coverage != Decimal("1"):
                raise ValueError("publication gate bypassed incomplete source lineage")
        if receipt.publication.instant_trade_parameters_allowed and any(
            not item.quote_fresh for item in receipt.execution_plans
        ):
            raise ValueError("instant trade parameters bypassed quote freshness SLA")

    def load_and_replay(
        self, receipt_id: str
    ) -> tuple[RecommendationResearchReceipt, dict[str, Any]]:
        if self.state is None or self.objects is None:
            raise ValueError("offline receipt replay requires canonical local stores")
        record = self.state.artifact_record(receipt_id)
        if record is None or record["type"] != "RecommendationResearchReceipt":
            raise ValueError("recommendation receipt is unavailable")
        receipt = RecommendationResearchReceipt.model_validate_json(
            self.objects.get_bytes(str(record["object_hash"]))
        )
        return receipt, self.verify_receipt(receipt)

    def freeze_reevaluation(
        self,
        *,
        original: RecommendationResearchReceipt,
        instrument_id: str,
        trigger: str,
        previous_view: str,
        new_view: str,
        change_reason: str,
        recommendation_change: str,
        observed_at: datetime,
        source_artifact_ids: Sequence[str] = (),
        realized_return: Decimal | None = None,
        maximum_favorable_excursion: Decimal | None = None,
        maximum_adverse_excursion: Decimal | None = None,
        attribution: Sequence[str] = (),
    ) -> RecommendationReevaluationRecord:
        self.verify_receipt(original)
        body = {
            "original_receipt_id": original.receipt_id,
            "original_receipt_hash": original.receipt_hash,
            "instrument_id": instrument_id,
            "trigger": trigger,
            "observed_at": observed_at,
            "previous_view": previous_view,
            "new_view": new_view,
            "change_reason": change_reason,
            "recommendation_change": recommendation_change,
            "realized_return": realized_return,
            "maximum_favorable_excursion": maximum_favorable_excursion,
            "maximum_adverse_excursion": maximum_adverse_excursion,
            "attribution": tuple(attribution),
            "source_artifact_ids": tuple(source_artifact_ids),
        }
        digest = content_hash(body)
        revision = RecommendationReevaluationRecord(
            **body,
            revision_id=f"RecommendationReevaluationRecord:{digest}",
            revision_hash=digest,
        )
        if self.state is not None and self.objects is not None:
            original_record = self.state.artifact_record(original.receipt_id)
            if (
                original_record is None
                or original_record["type"] != "RecommendationResearchReceipt"
            ):
                raise ValueError("historical recommendation receipt is not canonically registered")
            input_hashes = [str(original_record["object_hash"])]
            for artifact_id in source_artifact_ids:
                record = self.state.artifact_record(artifact_id)
                if record is None:
                    raise ValueError("reevaluation source artifact is unavailable")
                input_hashes.append(str(record["object_hash"]))
            reference = self.objects.put_json(revision.model_dump(mode="json"))
            self.state.register_artifact(
                artifact_id=revision.revision_id,
                artifact_type="RecommendationReevaluationRecord",
                schema_version=revision.schema_version,
                object_hash=reference.sha256,
                input_hashes=sorted(set(input_hashes)),
            )
            assert self.store is not None
            from astock.investor_orchestration.models import PortfolioLane, SubjectEventKind
            from astock.investor_orchestration.subjects import ResearchSubjectRegistryService

            ResearchSubjectRegistryService(self.store).append(
                instrument_id=instrument_id,
                event_type=SubjectEventKind.WATCHLIST_UPDATED,
                lane=PortfolioLane.WATCHLIST,
                artifact_id=revision.revision_id,
                reason=f"recommendation reevaluation: {trigger}",
                available_at=observed_at,
                metadata={
                    "original_receipt_id": original.receipt_id,
                    "recommendation_change": recommendation_change,
                },
                idempotency_key=content_hash(
                    {
                        "revision_id": revision.revision_id,
                        "instrument_id": instrument_id,
                        "trigger": trigger,
                    }
                ),
            )
        if original.receipt_hash != self.verify_receipt(original)["receipt_hash"]:
            raise ValueError("historical recommendation receipt changed during reevaluation")
        return revision
