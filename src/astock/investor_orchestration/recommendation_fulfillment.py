"""Machine-verifiable breadth fulfillment for broad current recommendation requests."""

from __future__ import annotations

from enum import StrEnum

from astock.core.artifact_reading import ArtifactReadError, decode_registered_artifact
from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.investor_orchestration.models import (
    CapabilityCoverageReceipt,
    CapabilityRunStatus,
    InvestorRequestEnvelope,
    StrictModel,
    recommendation_quantity_targets,
)
from astock.schemas.full_research import RecommendationResearchReceipt
from astock.schemas.research_seeds import ResearchSeedReport


class RecommendationFulfillmentState(StrEnum):
    SATISFIED = "SATISFIED"
    WAITING_RESEARCH = "WAITING_RESEARCH"
    REFRESH_CURRENT_RESEARCH = "REFRESH_CURRENT_RESEARCH"
    RECOVER_FORMAL_UNIVERSE = "RECOVER_FORMAL_UNIVERSE"
    EXPAND_EXISTING_SEEDS = "EXPAND_EXISTING_SEEDS"
    GENERATE_NEXT_SEED_BATCH = "GENERATE_NEXT_SEED_BATCH"


class RecommendationFulfillmentAssessment(StrictModel):
    request_id: str
    minimum_actionable_count: int
    target_actionable_count: int
    actionable_instruments: tuple[str, ...] = ()
    immediate_actionable_instruments: tuple[str, ...] = ()
    near_term_conditional_instruments: tuple[str, ...] = ()
    researched_instruments: tuple[str, ...] = ()
    watch_instruments: tuple[str, ...] = ()
    direct_seed_report_ids: tuple[str, ...] = ()
    formal_full_market_coverage: bool | None = None
    next_candidate_ids: tuple[str, ...] = ()
    next_seed_exclusions: tuple[str, ...] = ()
    state: RecommendationFulfillmentState
    satisfied: bool
    next_action: str


_SUCCESS = {CapabilityRunStatus.COMPLETED, CapabilityRunStatus.REUSED}


def _company_id(value: str) -> str:
    """Return a six-digit company id from an explicit or bare equity identity."""
    if ":" in value:
        market, symbol = value.split(":", 1)
    elif "." in value:
        symbol, market = value.rsplit(".", 1)
    else:
        market, symbol = None, value
    if market is not None and market not in {"XSHG", "XSHE", "BJSE"}:
        raise ValueError("recommendation identity is not an A-share equity")
    if len(symbol) != 6 or not symbol.isdigit():
        raise ValueError("recommendation company id must be six digits")
    return symbol


class RecommendationFulfillmentService:
    """Inspect exact same-request sealed outputs; never create recommendation authority."""

    def __init__(self, state: StateStore, objects: ObjectStore) -> None:
        self.state = state
        self.objects = objects

    def assess(
        self,
        request: InvestorRequestEnvelope,
        coverage: CapabilityCoverageReceipt,
        *,
        next_batch_limit: int = 12,
    ) -> RecommendationFulfillmentAssessment | None:
        request = InvestorRequestEnvelope.model_validate(request.model_dump())
        coverage = CapabilityCoverageReceipt.model_validate(coverage.model_dump())
        targets = self._targets(request)
        if targets is None:
            return None
        if not 1 <= next_batch_limit <= 32:
            raise ValueError("next recommendation batch limit must be between 1 and 32")
        minimum, target = targets
        base = {
            "request_id": request.request_id,
            "minimum_actionable_count": minimum,
            "target_actionable_count": target,
        }
        gate = next(
            (
                record
                for record in coverage.records
                if record.capability_id == "FULL_RESEARCH_GATE" and record.status in _SUCCESS
            ),
            None,
        )
        if gate is None or len(gate.artifact_ids) != 1:
            return RecommendationFulfillmentAssessment(
                **base,
                state=RecommendationFulfillmentState.WAITING_RESEARCH,
                satisfied=False,
                next_action="COMPLETE_FULL_RESEARCH_GATE",
            )
        try:
            receipt = self._receipt(gate.artifact_ids[0], request.request_id)
        except ArtifactReadError:
            return RecommendationFulfillmentAssessment(
                **base,
                state=RecommendationFulfillmentState.REFRESH_CURRENT_RESEARCH,
                satisfied=False,
                next_action="REBUILD_CURRENT_RECOMMENDATION_RECEIPT",
            )
        immediate, conditional = self._actionability(receipt)
        # The breadth quota is a current-buildability contract. A stale quote or
        # an untriggered conditional entry remains useful to the watch section, but
        # must not make a 3–5 name request look satisfied.
        actionable = immediate
        researched = tuple(sorted(receipt.candidate_universe))
        researched_company_ids = {_company_id(value) for value in researched}
        watch = tuple(
            sorted(
                item.instrument_id
                for item in receipt.candidate_rankings
                if not item.eligible and "COMMITTEE_WATCH" in item.rejection_reasons
            )
        )
        seed_reports = self._seed_reports(receipt)
        report_ids = tuple(report.report_id for report in seed_reports)
        universe_formal = (
            all(report.formal_full_market_coverage_allowed for report in seed_reports)
            if seed_reports
            else None
        )
        ranked_seeds = sorted(
            (
                seed
                for report in seed_reports
                for seed in report.seeds
                if seed.company_id not in researched_company_ids
            ),
            key=lambda item: (-item.research_priority_score, item.company_id),
        )
        next_candidates = tuple(
            dict.fromkeys(seed.company_id for seed in ranked_seeds)
        )[:next_batch_limit]
        exclusions = tuple(
            sorted(
                {
                    *researched_company_ids,
                    *(
                        seed.company_id
                        for report in seed_reports
                        for seed in report.seeds
                    ),
                }
            )
        )
        if len(actionable) >= minimum and universe_formal is True:
            return RecommendationFulfillmentAssessment(
                **base,
                actionable_instruments=actionable,
                immediate_actionable_instruments=immediate,
                near_term_conditional_instruments=conditional,
                researched_instruments=researched,
                watch_instruments=watch,
                direct_seed_report_ids=report_ids,
                formal_full_market_coverage=universe_formal,
                next_candidate_ids=next_candidates,
                next_seed_exclusions=exclusions,
                state=RecommendationFulfillmentState.SATISFIED,
                satisfied=True,
                next_action=(
                    "OPTIONALLY_EXPAND_TO_TARGET"
                    if len(actionable) < target
                    else "PUBLISH_VERIFIED_RECOMMENDATION"
                ),
            )
        if universe_formal is not True:
            state = RecommendationFulfillmentState.RECOVER_FORMAL_UNIVERSE
            action = "RECOVER_OFFICIAL_UNIVERSE_AND_REGENERATE_SEEDS"
        elif next_candidates:
            state = RecommendationFulfillmentState.EXPAND_EXISTING_SEEDS
            action = "DEEP_RESEARCH_NEXT_CANDIDATE_BATCH"
        else:
            state = RecommendationFulfillmentState.GENERATE_NEXT_SEED_BATCH
            action = "GENERATE_NEXT_SEED_BATCH_WITH_EXCLUSIONS"
        return RecommendationFulfillmentAssessment(
            **base,
            actionable_instruments=actionable,
            immediate_actionable_instruments=immediate,
            near_term_conditional_instruments=conditional,
            researched_instruments=researched,
            watch_instruments=watch,
            direct_seed_report_ids=report_ids,
            formal_full_market_coverage=universe_formal,
            next_candidate_ids=next_candidates,
            next_seed_exclusions=exclusions,
            state=state,
            satisfied=False,
            next_action=action,
        )

    @staticmethod
    def _targets(request: InvestorRequestEnvelope) -> tuple[int, int] | None:
        minimum = request.metadata.get("recommendation_minimum_actionable")
        target = request.metadata.get("recommendation_target_actionable")
        if isinstance(minimum, int) and isinstance(target, int):
            if minimum <= 0 or target < minimum:
                raise ValueError("recommendation quantity targets are invalid")
            return minimum, target
        return recommendation_quantity_targets(request.raw_text)

    def _receipt(self, artifact_id: str, request_id: str) -> RecommendationResearchReceipt:
        record = self.state.artifact_record(artifact_id)
        if record is None or record["type"] != "RecommendationResearchReceipt":
            raise ArtifactReadError(
                "RECOMMENDATION_RECEIPT_UNAVAILABLE",
                artifact_type="RecommendationResearchReceipt",
            )
        receipt = decode_registered_artifact(
            self.objects.get_bytes(str(record["object_hash"])),
            RecommendationResearchReceipt,
            registry_schema_version=str(record["schema_version"]),
        )
        if receipt.request_id != request_id:
            raise ValueError("recommendation fulfillment cannot reuse another request")
        return receipt

    @staticmethod
    def _actionability(
        receipt: RecommendationResearchReceipt,
    ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        if not receipt.publication.formal_recommendation_allowed:
            return (), ()
        execution = {item.instrument_id: item for item in receipt.execution_plans}
        immediate: list[str] = []
        conditional: list[str] = []
        for position in receipt.portfolio.positions:
            plan = execution.get(position.instrument_id)
            if (
                position.target_weight <= 0
                or position.target_shares <= 0
                or plan is None
                or plan.buy_range_low is None
                or plan.buy_range_high is None
                or plan.maximum_acceptable_price is None
            ):
                continue
            buy_low = plan.buy_range_low
            buy_high = plan.buy_range_high
            maximum_price = plan.maximum_acceptable_price
            current_entry = (
                plan.quote_fresh
                and buy_low <= plan.reference_price <= buy_high
                and plan.reference_price <= maximum_price
            )
            if current_entry:
                immediate.append(position.instrument_id)
            else:
                conditional.append(position.instrument_id)
        return tuple(sorted(immediate)), tuple(sorted(conditional))

    @classmethod
    def _actionable(cls, receipt: RecommendationResearchReceipt) -> tuple[str, ...]:
        immediate, _conditional = cls._actionability(receipt)
        return immediate

    def _seed_reports(
        self, receipt: RecommendationResearchReceipt
    ) -> tuple[ResearchSeedReport, ...]:
        reports: list[ResearchSeedReport] = []
        for artifact_id, expected_hash in sorted(receipt.input_artifact_hashes.items()):
            record = self.state.artifact_record(artifact_id)
            if record is None or record["type"] != "ResearchSeedReport":
                continue
            if str(record["object_hash"]) != expected_hash:
                raise ValueError("recommendation seed lineage hash differs from sealed receipt")
            report = decode_registered_artifact(
                self.objects.get_bytes(expected_hash),
                ResearchSeedReport,
                registry_schema_version=str(record["schema_version"]),
            )
            reports.append(report)
        return tuple(reports)


__all__ = [
    "RecommendationFulfillmentAssessment",
    "RecommendationFulfillmentService",
    "RecommendationFulfillmentState",
]
