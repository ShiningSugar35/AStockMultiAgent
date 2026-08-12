"""Phase 10 portfolio risk explanation, implementation stress, and ex-post attribution."""

from __future__ import annotations

from math import sqrt
from pathlib import Path
from statistics import median
from typing import Any

import numpy as np

from astock.core.hashing import content_hash
from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.paper_trading.replay import load_fee_schedule
from astock.portfolio.service import PortfolioService
from astock.schemas.portfolio import PortfolioAnalysisReport, PortfolioAnalysisStatus
from astock.schemas.portfolio_vnext import (
    AssetAttributionResult,
    AttributionComponent,
    CompactFactorExposure,
    CompactRiskFactor,
    LiquidityImplementationEstimate,
    PortfolioAssetRiskExplanation,
    PortfolioAttributionReport,
    PortfolioAttributionRequest,
    PortfolioFundamentalFactorInput,
    PortfolioRiskExplanationReport,
    PortfolioRiskExplanationRequest,
    PortfolioStressReport,
    PortfolioStressRequest,
    PortfolioStressResult,
    ResearchAttributionSummary,
    RiskExposureProvenance,
)

_ANNUALIZATION = 252.0


class PortfolioVNextService:
    """Add explanatory risk tooling without changing the production allocation default."""

    def __init__(
        self,
        state: StateStore,
        objects: ObjectStore,
        portfolio: PortfolioService,
        project_root: Path,
    ) -> None:
        self.state = state
        self.objects = objects
        self.portfolio = portfolio
        self.fee_schedule = load_fee_schedule(project_root / "configs" / "fee_rules.yaml")

    def explain_risk(
        self, request: PortfolioRiskExplanationRequest
    ) -> PortfolioRiskExplanationReport:
        analysis, analysis_hash = self._analysis_report(request.portfolio_analysis_artifact_id)
        if analysis.status is not PortfolioAnalysisStatus.READY or analysis.metrics is None:
            raise ValueError("portfolio risk explanation requires a READY PortfolioAnalysisReport")
        if analysis.as_of != request.as_of:
            raise ValueError("portfolio risk explanation as_of must match the analysis report")
        request_id, request_hash = self._freeze_request(
            "PortfolioRiskExplanationRequest", request.schema_version, request
        )
        source_ids = {request.portfolio_analysis_artifact_id, request_id}
        source_hashes = {analysis_hash, request_hash}
        factor_by_company = {item.company_id: item for item in request.fundamental_factors}
        assets: list[PortfolioAssetRiskExplanation] = []
        warnings: set[str] = set()
        total_cost = 0
        all_costs_available = True

        for asset in analysis.assets:
            bars, _ = self.portfolio.verifier.visible_daily_history(
                asset.market,
                asset.company_id,
                visible_at=request.as_of,
            )
            bars = [item for item in bars if item.available_to_system_at <= request.as_of]
            bars = sorted(bars, key=lambda item: item.session_date)[
                -request.liquidity_lookback_sessions :
            ]
            if len(bars) < 20:
                raise ValueError(
                    f"portfolio liquidity explanation requires 20 sessions for {asset.company_id}"
                )
            closes = np.asarray([float(item.close) for item in bars], dtype=float)
            volumes = np.asarray([float(item.volume) for item in bars], dtype=float)
            amounts = [float(item.amount) for item in bars if item.amount is not None]
            ranges = np.asarray(
                [float((item.high - item.low) / item.close) for item in bars], dtype=float
            )
            returns = closes[1:] / closes[:-1] - 1.0
            momentum = float(closes[-1] / closes[0] - 1.0)
            volatility = float(np.std(returns, ddof=1) * sqrt(_ANNUALIZATION))
            avg_volume = float(volumes.mean()) if len(volumes) else 0.0
            avg_amount_fen = int(round(sum(amounts) / len(amounts) * 100)) if amounts else None
            range_fraction = float(median(float(value) for value in ranges))
            liquidity_exposure = (
                range_fraction / max((avg_amount_fen or 0) / 100_000_000, 1e-9)
                if avg_amount_fen is not None
                else None
            )
            notional = request.position_notional_fen.get(asset.company_id)
            liquidity = self._liquidity_estimate(
                company_id=asset.company_id,
                avg_volume=avg_volume,
                avg_amount_fen=avg_amount_fen,
                range_fraction=range_fraction,
                notional_fen=notional,
                participation_cap=request.participation_cap,
                round_trip=request.round_trip,
            )
            warnings.update(liquidity.warning_codes)
            if liquidity.estimated_round_trip_cost_fen is None:
                all_costs_available = False
            else:
                total_cost += liquidity.estimated_round_trip_cost_fen

            fundamental = factor_by_company.get(asset.company_id)
            factors = self._asset_factors(
                asset=asset,
                momentum=momentum,
                volatility=volatility,
                liquidity_exposure=liquidity_exposure,
                fundamental=fundamental,
            )
            if fundamental is None or any(
                item.provenance is RiskExposureProvenance.UNAVAILABLE
                for item in factors
                if item.factor
                in {
                    CompactRiskFactor.SIZE,
                    CompactRiskFactor.VALUE,
                    CompactRiskFactor.QUALITY_PROFITABILITY,
                }
            ):
                warnings.add("FUNDAMENTAL_FACTOR_EXPOSURE_INCOMPLETE")
            if asset.industry_tag is None:
                warnings.add("INDUSTRY_FACTOR_UNVERIFIED")
            if fundamental is not None and fundamental.source_artifact_id:
                self._verify_factor_provenance(fundamental)
                source_ids.add(fundamental.source_artifact_id)
                assert fundamental.source_object_hash is not None
                source_hashes.add(fundamental.source_object_hash)
            assets.append(
                PortfolioAssetRiskExplanation(
                    company_id=asset.company_id,
                    weight=asset.weight,
                    industry_tag=asset.industry_tag,
                    factors=factors,
                    liquidity=liquidity,
                    created_at=request.as_of,
                )
            )

        portfolio_factors: dict[CompactRiskFactor, float | None] = {}
        for factor in CompactRiskFactor:
            if factor is CompactRiskFactor.INDUSTRY:
                exposures = analysis.metrics.industry_exposures
                portfolio_factors[factor] = max(exposures.values()) if exposures else None
                continue
            values: list[float] = []
            complete = True
            for asset in assets:
                exposure = next(item for item in asset.factors if item.factor is factor).exposure
                if exposure is None:
                    complete = False
                    break
                values.append(asset.weight * exposure)
            portfolio_factors[factor] = sum(values) if complete else None

        seed = {
            "request_hash": request_hash,
            "analysis_hash": analysis_hash,
            "source_hashes": sorted(source_hashes),
            "assets": [item.model_dump(mode="json") for item in assets],
        }
        report = PortfolioRiskExplanationReport(
            report_id=f"portfolio-risk-explanation:{content_hash(seed)}",
            portfolio_id=analysis.portfolio_id,
            as_of=request.as_of,
            assets=sorted(assets, key=lambda item: item.company_id),
            portfolio_factor_exposures=portfolio_factors,
            estimated_one_way_turnover_weight=analysis.metrics.invested_weight,
            estimated_round_trip_implementation_cost_fen=(
                total_cost if all_costs_available else None
            ),
            warning_codes=sorted(warnings),
            source_artifact_ids=sorted(source_ids),
            source_object_hashes=sorted(source_hashes),
            created_at=request.as_of,
        )
        self._persist_report(report, "PortfolioRiskExplanationReport", sorted(source_hashes))
        return report

    def stress(self, request: PortfolioStressRequest) -> PortfolioStressReport:
        analysis, analysis_hash = self._analysis_report(request.portfolio_analysis_artifact_id)
        if analysis.status is not PortfolioAnalysisStatus.READY:
            raise ValueError("portfolio stress requires a READY PortfolioAnalysisReport")
        if analysis.as_of != request.as_of:
            raise ValueError("portfolio stress as_of must match the analysis report")
        request_id, request_hash = self._freeze_request(
            "PortfolioStressRequest", request.schema_version, request
        )
        weights = {item.company_id: item.weight for item in analysis.assets}
        source_ids = {request.portfolio_analysis_artifact_id, request_id}
        source_hashes = {analysis_hash, request_hash}
        warnings: set[str] = set()
        results: list[PortfolioStressResult] = []
        for scenario in request.scenarios:
            shocks = {item.company_id: item for item in scenario.asset_shocks}
            uncovered = sorted(set(weights) - set(shocks))
            if uncovered:
                warnings.add("STRESS_SCENARIO_PARTIAL_COVERAGE")
            contributions = {
                company_id: weights[company_id] * shock.shock_return
                for company_id, shock in shocks.items()
                if company_id in weights
            }
            weighted_shock = sum(contributions.values())
            for shock in scenario.asset_shocks:
                source_ids.update(shock.source_artifact_ids)
                source_hashes.update(shock.source_object_hashes)
            largest = (
                max(contributions.values(), key=lambda value: abs(value)) if contributions else 0.0
            )
            results.append(
                PortfolioStressResult(
                    scenario=scenario.scenario,
                    weighted_return_shock=weighted_shock,
                    stressed_nav_fraction=max(0.0, min(2.0, 1.0 + weighted_shock)),
                    largest_asset_contribution=largest,
                    uncovered_company_ids=uncovered,
                    rationale_codes=sorted({item.rationale_code for item in scenario.asset_shocks}),
                    created_at=request.as_of,
                )
            )
        report = PortfolioStressReport(
            report_id="portfolio-stress:"
            + content_hash(
                {
                    "request_hash": request_hash,
                    "analysis_hash": analysis_hash,
                    "results": [item.model_dump(mode="json") for item in results],
                }
            ),
            portfolio_id=analysis.portfolio_id,
            as_of=request.as_of,
            results=results,
            warning_codes=sorted(warnings),
            source_artifact_ids=sorted(source_ids),
            source_object_hashes=sorted(source_hashes),
            created_at=request.as_of,
        )
        self._persist_report(report, "PortfolioStressReport", sorted(source_hashes))
        return report

    def attribute(self, request: PortfolioAttributionRequest) -> PortfolioAttributionReport:
        request_id, request_hash = self._freeze_request(
            "PortfolioAttributionRequest", request.schema_version, request
        )
        assets: list[AssetAttributionResult] = []
        totals = {component: 0.0 for component in AttributionComponent}
        memo: dict[str, float] = {}
        skills: dict[str, float] = {}
        deltas: dict[str, float] = {}
        realized_total = 0.0
        total_residual = 0.0
        for item in request.assets:
            excess = item.beginning_weight * (item.realized_return - item.benchmark_return)
            sector = item.beginning_weight * item.sector_contribution
            compact_factor = item.beginning_weight * item.compact_factor_contribution
            timing = item.beginning_weight * item.timing_contribution
            implementation = -item.beginning_weight * item.implementation_cost_return
            stock_selection = excess - sector - compact_factor - timing - implementation
            components = {
                AttributionComponent.STOCK_SELECTION: stock_selection,
                AttributionComponent.SECTOR: sector,
                AttributionComponent.COMPACT_FACTOR: compact_factor,
                AttributionComponent.TIMING: timing,
                AttributionComponent.IMPLEMENTATION_COST: implementation,
            }
            reconciled = sum(components.values())
            residual = excess - reconciled
            realized_total += excess
            total_residual += residual
            for key, value in components.items():
                totals[key] += value
            research_credit = stock_selection + compact_factor
            self._spread_credit(memo, item.research_links.research_memo_id, research_credit)
            self._spread_credits(skills, item.research_links.skill_ids, research_credit)
            self._spread_credits(deltas, item.research_links.specialist_delta_ids, research_credit)
            assets.append(
                AssetAttributionResult(
                    company_id=item.company_id,
                    realized_excess_contribution=excess,
                    components=components,
                    residual=residual,
                    research_links=item.research_links,
                    created_at=request.period_end,
                )
            )
        source_ids = sorted(set([request_id, *request.source_artifact_ids]))
        source_hashes = sorted(set([request_hash, *request.source_object_hashes]))
        report = PortfolioAttributionReport(
            report_id="portfolio-attribution:"
            + content_hash(
                {
                    "request_hash": request_hash,
                    "assets": [item.model_dump(mode="json") for item in assets],
                }
            ),
            portfolio_id=request.portfolio_id,
            period_start=request.period_start,
            period_end=request.period_end,
            assets=assets,
            component_totals=totals,
            realized_excess_return=realized_total,
            total_residual=total_residual,
            research_attribution=ResearchAttributionSummary(
                research_memo_contributions=dict(sorted(memo.items())),
                skill_contributions=dict(sorted(skills.items())),
                specialist_delta_contributions=dict(sorted(deltas.items())),
                created_at=request.period_end,
            ),
            warning_codes=["RESEARCH_ATTRIBUTION_IS_ASSOCIATIVE_NOT_CAUSAL"],
            source_artifact_ids=source_ids,
            source_object_hashes=source_hashes,
            created_at=request.period_end,
        )
        self._persist_report(report, "PortfolioAttributionReport", source_hashes)
        return report

    def audit(self, artifact_id: str) -> dict[str, Any]:
        record = self.state.artifact_record(artifact_id)
        allowed = {
            "PortfolioRiskExplanationReport",
            "PortfolioStressReport",
            "PortfolioAttributionReport",
        }
        if record is None or str(record["type"]) not in allowed:
            return {
                "status": "FAIL",
                "artifact_id": artifact_id,
                "finding_codes": ["UNKNOWN_REPORT"],
            }
        findings: set[str] = set()
        if not self.objects.verify(str(record["object_hash"])):
            findings.add("REPORT_OBJECT_UNAVAILABLE")
        for input_hash in record["input_hashes"]:
            if len(str(input_hash)) == 64 and not self.objects.verify(str(input_hash)):
                findings.add("REPORT_INPUT_OBJECT_UNAVAILABLE")
        return {
            "status": "PASS" if not findings else "FAIL",
            "artifact_id": artifact_id,
            "object_hash": str(record["object_hash"]),
            "finding_codes": sorted(findings),
            "allocation_override_allowed": False,
            "paper_ledger_write_allowed": False,
            "broker_execution_allowed": False,
        }

    def _analysis_report(self, artifact_id: str) -> tuple[PortfolioAnalysisReport, str]:
        record = self.state.artifact_record(artifact_id)
        if record is None or str(record["type"]) != "PortfolioAnalysisReport":
            raise ValueError("Phase 10 portfolio tooling requires PortfolioAnalysisReport")
        object_hash = str(record["object_hash"])
        if not self.objects.verify(object_hash):
            raise ValueError("portfolio analysis report object is unavailable")
        return PortfolioAnalysisReport.model_validate_json(
            self.objects.get_bytes(object_hash)
        ), object_hash

    def _verify_factor_provenance(self, factor: PortfolioFundamentalFactorInput) -> None:
        assert factor.source_artifact_id is not None
        assert factor.source_object_hash is not None
        record = self.state.artifact_record(factor.source_artifact_id)
        if record is None or str(record["object_hash"]) != factor.source_object_hash:
            raise ValueError("fundamental factor provenance does not match the artifact registry")
        if not self.objects.verify(factor.source_object_hash):
            raise ValueError("fundamental factor source object is unavailable")

    def _asset_factors(
        self,
        *,
        asset: Any,
        momentum: float,
        volatility: float,
        liquidity_exposure: float | None,
        fundamental: PortfolioFundamentalFactorInput | None,
    ) -> list[CompactFactorExposure]:
        items = [
            CompactFactorExposure(
                factor=CompactRiskFactor.MARKET,
                exposure=asset.beta_to_benchmark,
                provenance=RiskExposureProvenance.MARKET_DERIVED,
                methodology="rolling-beta-to-request-benchmark-v1",
            ),
            self._fundamental_exposure(CompactRiskFactor.SIZE, fundamental, "size_exposure"),
            self._fundamental_exposure(CompactRiskFactor.VALUE, fundamental, "value_exposure"),
            CompactFactorExposure(
                factor=CompactRiskFactor.MOMENTUM,
                exposure=max(-20.0, min(20.0, momentum)),
                provenance=RiskExposureProvenance.MARKET_DERIVED,
                methodology="trailing-total-return-v1",
            ),
            self._fundamental_exposure(
                CompactRiskFactor.QUALITY_PROFITABILITY,
                fundamental,
                "quality_profitability_exposure",
            ),
            CompactFactorExposure(
                factor=CompactRiskFactor.VOLATILITY,
                exposure=min(20.0, volatility),
                provenance=RiskExposureProvenance.MARKET_DERIVED,
                methodology="annualized-realized-volatility-v1",
            ),
            CompactFactorExposure(
                factor=CompactRiskFactor.LIQUIDITY,
                exposure=(
                    min(20.0, liquidity_exposure) if liquidity_exposure is not None else None
                ),
                provenance=(
                    RiskExposureProvenance.MARKET_DERIVED
                    if liquidity_exposure is not None
                    else RiskExposureProvenance.UNAVAILABLE
                ),
                methodology="daily-range-per-cny-million-turnover-v1",
            ),
            CompactFactorExposure(
                factor=CompactRiskFactor.INDUSTRY,
                exposure=1.0 if asset.industry_tag else None,
                provenance=(
                    RiskExposureProvenance.CALLER_SUPPLIED_UNVERIFIED
                    if asset.industry_tag
                    else RiskExposureProvenance.UNAVAILABLE
                ),
                methodology="industry-one-hot-exposure-v1",
            ),
        ]
        for item in items:
            item.created_at = asset.created_at
        return items

    @staticmethod
    def _fundamental_exposure(
        factor: CompactRiskFactor,
        snapshot: PortfolioFundamentalFactorInput | None,
        field_name: str,
    ) -> CompactFactorExposure:
        value = getattr(snapshot, field_name) if snapshot is not None else None
        if value is None:
            return CompactFactorExposure(
                factor=factor,
                exposure=None,
                provenance=RiskExposureProvenance.UNAVAILABLE,
                methodology="fundamental-factor-input-v1",
            )
        assert snapshot is not None
        frozen = bool(snapshot.source_artifact_id and snapshot.source_object_hash)
        return CompactFactorExposure(
            factor=factor,
            exposure=float(value),
            provenance=(
                RiskExposureProvenance.FROZEN_INPUT
                if frozen
                else RiskExposureProvenance.CALLER_SUPPLIED_UNVERIFIED
            ),
            methodology=snapshot.methodology_version or "caller-supplied-factor-v1",
            source_artifact_id=snapshot.source_artifact_id,
            source_object_hash=snapshot.source_object_hash,
        )

    def _liquidity_estimate(
        self,
        *,
        company_id: str,
        avg_volume: float,
        avg_amount_fen: int | None,
        range_fraction: float,
        notional_fen: int | None,
        participation_cap: float,
        round_trip: bool,
    ) -> LiquidityImplementationEstimate:
        warnings: set[str] = set()
        if avg_amount_fen is None or avg_amount_fen <= 0:
            warnings.add("DAILY_AMOUNT_UNAVAILABLE")
        days: float | None = None
        slippage_bps: float | None = None
        cost: int | None = None
        if notional_fen is None:
            warnings.add("POSITION_NOTIONAL_UNAVAILABLE")
        elif avg_amount_fen is not None and avg_amount_fen > 0:
            daily_capacity = avg_amount_fen * participation_cap
            days = notional_fen / daily_capacity
            required_participation = min(1.0, notional_fen / avg_amount_fen)
            slippage_bps = range_fraction * 10_000 * sqrt(required_participation) * 0.5
            schedule = self.fee_schedule
            commission = max(
                schedule.minimum_commission_fen,
                int(round(notional_fen * float(schedule.commission_rate))),
            )
            transfer = int(round(notional_fen * float(schedule.transfer_fee_rate)))
            sell_tax = int(round(notional_fen * float(schedule.stamp_tax_sell_rate)))
            slippage = int(round(notional_fen * slippage_bps / 10_000))
            one_way_buy = commission + transfer + slippage
            one_way_sell = commission + transfer + sell_tax + slippage
            cost = one_way_buy + one_way_sell if round_trip else one_way_sell
            if required_participation > participation_cap:
                warnings.add("POSITION_EXCEEDS_ONE_DAY_PARTICIPATION_CAP")
        return LiquidityImplementationEstimate(
            company_id=company_id,
            average_daily_volume_shares=avg_volume,
            average_daily_amount_fen=avg_amount_fen,
            median_daily_range_fraction=range_fraction,
            position_notional_fen=notional_fen,
            participation_cap=participation_cap,
            days_to_liquidate=days,
            estimated_slippage_bps=slippage_bps,
            estimated_round_trip_cost_fen=cost,
            warning_codes=sorted(warnings),
        )

    @staticmethod
    def _spread_credit(target: dict[str, float], key: str | None, value: float) -> None:
        if key:
            target[key] = target.get(key, 0.0) + value

    @staticmethod
    def _spread_credits(target: dict[str, float], keys: list[str], value: float) -> None:
        if not keys:
            return
        share = value / len(keys)
        for key in keys:
            target[key] = target.get(key, 0.0) + share

    def _freeze_request(
        self, artifact_type: str, schema_version: str, request: Any
    ) -> tuple[str, str]:
        object_ref = self.objects.put_json(request.model_dump(mode="json"))
        artifact_id = f"{artifact_type}:{content_hash({'object_hash': object_ref.sha256})}"
        self.state.register_artifact(
            artifact_id=artifact_id,
            artifact_type=artifact_type,
            schema_version=schema_version,
            object_hash=object_ref.sha256,
            input_hashes=[],
        )
        return artifact_id, object_ref.sha256

    def _persist_report(self, report: Any, artifact_type: str, input_hashes: list[str]) -> None:
        object_ref = self.objects.put_json(report.model_dump(mode="json"))
        self.state.register_artifact(
            artifact_id=report.report_id,
            artifact_type=artifact_type,
            schema_version=report.schema_version,
            object_hash=object_ref.sha256,
            input_hashes=input_hashes,
        )
        self.state.set_checkpoint(
            scope_type="portfolio-vnext",
            scope_key=report.report_id,
            cursor={"artifact_id": report.report_id, "artifact_type": artifact_type},
            status="SUCCEEDED",
            object_hash=object_ref.sha256,
        )


__all__ = ["PortfolioVNextService"]
