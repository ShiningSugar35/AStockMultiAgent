"""Deterministic portfolio diagnostics and constrained allocation proposals."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from math import sqrt
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np

from astock.core.errors import PolicyError
from astock.core.hashing import canonical_json_bytes, content_hash, sha256_bytes
from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.market_data.reference import MarketReferenceService
from astock.paper_trading.ledger import LedgerService
from astock.paper_trading.operation import MarketReferencePaperVerifier
from astock.portfolio.allocators import (
    PortfolioAllocatorRegistry,
    default_portfolio_allocator_registry,
    load_portfolio_allocator_policy,
)
from astock.portfolio.analytics import (
    AlignedPortfolioData,
    align_return_histories,
    constrain_scores,
    hierarchical_risk_weights,
    minimum_variance_weights,
    portfolio_risk_statistics,
)
from astock.schemas.committee import CommitteeRuleConfig, TradeProtocolOutcome
from astock.schemas.market import Market
from astock.schemas.portfolio import (
    PortfolioAllocationProposal,
    PortfolioAnalysisReport,
    PortfolioAnalysisRequest,
    PortfolioAnalysisStatus,
    PortfolioAssetRisk,
    PortfolioConstructionReport,
    PortfolioConstructionRequest,
    PortfolioHoldingInput,
    PortfolioRiskMetrics,
)
from astock.schemas.reference_data import ReferenceCoverageStatus
from astock.schemas.research_runtime import ClassifiedTradeProtocol, TradingClassificationRelease

_SHANGHAI = ZoneInfo("Asia/Shanghai")
_ANNUALIZATION = 252.0
_CURRENT_LIVE_TOLERANCE = timedelta(minutes=10)


@dataclass(frozen=True, slots=True)
class _History:
    company_id: str
    market: Market
    closes_by_date: dict[str, float]
    latest_close_fen: int
    release_id: str
    release_object_hash: str
    cutoff_at: datetime


_Aligned = AlignedPortfolioData


class PortfolioService:
    """Evaluate portfolios and compare robust long-only construction proposals."""

    def __init__(
        self,
        state: StateStore,
        objects: ObjectStore,
        reference: MarketReferenceService,
        committee_rules: CommitteeRuleConfig,
        *,
        allocator_registry: PortfolioAllocatorRegistry | None = None,
        allocator_policy_path: Path | None = None,
    ) -> None:
        self.state = state
        self.objects = objects
        self.reference = reference
        self.committee_rules = committee_rules
        self.verifier = MarketReferencePaperVerifier(reference)
        self.ledger = LedgerService(state, objects)
        self.allocator_registry = allocator_registry or default_portfolio_allocator_registry()
        policy_path = allocator_policy_path or (
            Path(__file__).resolve().parents[3] / "configs" / "portfolio_allocators.yaml"
        )
        self.allocator_policy = load_portfolio_allocator_policy(policy_path)
        missing_methods = set(self.allocator_policy.enabled_methods) - set(
            self.allocator_registry.methods()
        )
        if missing_methods:
            raise ValueError(f"portfolio allocator plugins are missing: {sorted(missing_methods)}")

    def analyze(self, request: PortfolioAnalysisRequest) -> PortfolioAnalysisReport:
        request_artifact, request_hash = self._freeze_input(
            "PortfolioAnalysisRequest",
            request.schema_version,
            request.model_dump(mode="json"),
        )
        try:
            holdings, account_snapshot_id, account_snapshot_hash = self._resolve_holdings(request)
        except (PolicyError, RuntimeError, ValueError) as exc:
            return self._analysis_needs_info(
                request,
                request_artifact,
                request_hash,
                [self._reason_code(exc)],
            )
        source_ids = [request_artifact]
        source_hashes = [request_hash]
        if account_snapshot_id and account_snapshot_hash:
            source_ids.append(account_snapshot_id)
            source_hashes.append(account_snapshot_hash)
        if not holdings:
            report = PortfolioAnalysisReport(
                report_id="portfolio-analysis:"
                + content_hash({"request_hash": request_hash, "status": "EMPTY"}),
                portfolio_id=request.portfolio_id,
                as_of=request.as_of,
                data_cutoff_at=request.as_of,
                status=PortfolioAnalysisStatus.EMPTY,
                common_session_count=0,
                assets=[],
                warning_codes=[],
                hard_breach_codes=[],
                source_artifact_ids=sorted(source_ids),
                source_object_hashes=sorted(source_hashes),
                created_at=request.as_of,
            )
            self._persist_report(report, source_hashes, "portfolio-analysis")
            return report

        try:
            histories: list[_History] = []
            cutoff = request.as_of
            for holding in holdings:
                history = self._history(
                    holding.company_id,
                    holding.market,
                    as_of=cutoff,
                    lookback_sessions=request.lookback_sessions,
                    minimum_sessions=request.minimum_common_sessions,
                    live=request.live,
                    allow_live_capture=True,
                )
                cutoff = max(cutoff, history.cutoff_at)
                histories.append(history)
                source_ids.append(f"market-reference:{history.release_id}")
                source_hashes.append(history.release_object_hash)
            benchmark = self._history(
                request.benchmark_symbol,
                request.benchmark_market,
                as_of=cutoff,
                lookback_sessions=request.lookback_sessions,
                minimum_sessions=request.minimum_common_sessions,
                live=request.live,
                allow_live_capture=True,
            )
            cutoff = max(cutoff, benchmark.cutoff_at)
            source_ids.append(f"market-reference:{benchmark.release_id}")
            source_hashes.append(benchmark.release_object_hash)
            holdings = self._materialize_account_weights(
                request,
                holdings,
                histories,
                cutoff=cutoff,
            )
            aligned = self._align(
                histories,
                benchmark,
                lookback_sessions=request.lookback_sessions,
                minimum_sessions=request.minimum_common_sessions,
            )
            report = self._analysis_report(
                request=request,
                holdings=holdings,
                histories=histories,
                benchmark=benchmark,
                aligned=aligned,
                data_cutoff_at=cutoff,
                source_ids=source_ids,
                source_hashes=source_hashes,
            )
        except (PolicyError, ValueError, np.linalg.LinAlgError) as exc:
            return self._analysis_needs_info(
                request,
                request_artifact,
                request_hash,
                [self._reason_code(exc)],
                source_ids=source_ids,
                source_hashes=source_hashes,
            )
        self._persist_report(report, report.source_object_hashes, "portfolio-analysis")
        return report

    def construct(self, request: PortfolioConstructionRequest) -> PortfolioConstructionReport:
        request_artifact, request_hash = self._freeze_input(
            "PortfolioConstructionRequest",
            request.schema_version,
            request.model_dump(mode="json"),
        )
        source_ids = [request_artifact]
        source_hashes = [request_hash]
        admitted: list[tuple[str, Market, str]] = []
        rejected: list[str] = []
        warnings: set[str] = {"RISK_GROUP_IS_CALLER_SUPPLIED"}
        for candidate in request.candidates:
            try:
                protocol, classification, protocol_hash, classification_hash = (
                    self._portfolio_candidate(
                        candidate.classified_protocol_artifact_id, request.as_of
                    )
                )
                if protocol.company_id != candidate.company_id:
                    raise ValueError("classified protocol company mismatch")
                admitted.append((candidate.company_id, classification.market, candidate.risk_group))
                source_ids.extend(
                    [
                        candidate.classified_protocol_artifact_id,
                        protocol.trading_classification_artifact_id,
                    ]
                )
                source_hashes.extend([protocol_hash, classification_hash])
            except (OSError, ValueError):
                rejected.append(candidate.company_id)
        admitted.sort(key=lambda item: item[0])
        if len(admitted) < 2:
            report = PortfolioConstructionReport(
                report_id="portfolio-construction:"
                + content_hash(
                    {
                        "request_hash": request_hash,
                        "admitted": [item[0] for item in admitted],
                        "rejected": sorted(rejected),
                    }
                ),
                portfolio_id=request.portfolio_id,
                as_of=request.as_of,
                data_cutoff_at=request.as_of,
                status=PortfolioAnalysisStatus.NEEDS_INFO,
                proposals=[],
                admitted_company_ids=sorted(item[0] for item in admitted),
                rejected_company_ids=sorted(rejected),
                common_session_count=0,
                warning_codes=sorted({*warnings, "AT_LEAST_TWO_APPROVED_CANDIDATES_REQUIRED"}),
                source_artifact_ids=sorted(set(source_ids)),
                source_object_hashes=sorted(set(source_hashes)),
                created_at=request.as_of,
            )
            self._persist_report(report, report.source_object_hashes, "portfolio-construction")
            return report

        histories: list[_History] = []
        try:
            for company_id, market, _ in admitted:
                history = self._history(
                    company_id,
                    market,
                    as_of=request.as_of,
                    lookback_sessions=request.lookback_sessions,
                    minimum_sessions=request.minimum_common_sessions,
                    live=False,
                    allow_live_capture=False,
                )
                histories.append(history)
                source_ids.append(f"market-reference:{history.release_id}")
                source_hashes.append(history.release_object_hash)
            benchmark = self._history(
                request.benchmark_symbol,
                request.benchmark_market,
                as_of=request.as_of,
                lookback_sessions=request.lookback_sessions,
                minimum_sessions=request.minimum_common_sessions,
                live=False,
                allow_live_capture=False,
            )
            source_ids.append(f"market-reference:{benchmark.release_id}")
            source_hashes.append(benchmark.release_object_hash)
            aligned = self._align(
                histories,
                benchmark,
                lookback_sessions=request.lookback_sessions,
                minimum_sessions=request.minimum_common_sessions,
            )
            if np.max(np.abs(aligned.asset_returns)) > 0.35:
                raise ValueError(
                    "unadjusted history contains a material corporate-action-like jump"
                )
            groups = {company_id: group for company_id, _, group in admitted}
            target = min(
                float(request.target_total_exposure or self.committee_rules.max_total_exposure),
                float(self.committee_rules.max_total_exposure),
                len(admitted) * float(self.committee_rules.max_single_position),
            )
            proposals = self._proposals(aligned, groups, target)
            report = PortfolioConstructionReport(
                report_id="portfolio-construction:"
                + content_hash(
                    {
                        "request_hash": request_hash,
                        "source_hashes": sorted(set(source_hashes)),
                        "proposals": [item.model_dump(mode="json") for item in proposals],
                    }
                ),
                portfolio_id=request.portfolio_id,
                as_of=request.as_of,
                data_cutoff_at=request.as_of,
                status=PortfolioAnalysisStatus.READY,
                proposals=proposals,
                admitted_company_ids=sorted(item[0] for item in admitted),
                rejected_company_ids=sorted(rejected),
                common_session_count=aligned.common_session_count,
                warning_codes=sorted(warnings),
                source_artifact_ids=sorted(set(source_ids)),
                source_object_hashes=sorted(set(source_hashes)),
                created_at=request.as_of,
            )
        except (PolicyError, ValueError, np.linalg.LinAlgError) as exc:
            warnings.add(self._reason_code(exc))
            report = PortfolioConstructionReport(
                report_id="portfolio-construction:"
                + content_hash(
                    {
                        "request_hash": request_hash,
                        "warnings": sorted(warnings),
                        "sources": sorted(set(source_hashes)),
                    }
                ),
                portfolio_id=request.portfolio_id,
                as_of=request.as_of,
                data_cutoff_at=request.as_of,
                status=PortfolioAnalysisStatus.NEEDS_INFO,
                proposals=[],
                admitted_company_ids=sorted(item[0] for item in admitted),
                rejected_company_ids=sorted(rejected),
                common_session_count=0,
                warning_codes=sorted(warnings),
                source_artifact_ids=sorted(set(source_ids)),
                source_object_hashes=sorted(set(source_hashes)),
                created_at=request.as_of,
            )
        self._persist_report(report, report.source_object_hashes, "portfolio-construction")
        return report

    def status(self, portfolio_id: str, *, construction: bool = False) -> dict[str, Any]:
        scope = "portfolio-construction" if construction else "portfolio-analysis"
        checkpoint = self.state.get_checkpoint(scope, portfolio_id)
        if checkpoint is None:
            return {"status": "NOT_RUN", "portfolio_id": portfolio_id, "scope": scope}
        return {
            "status": checkpoint["status"],
            "portfolio_id": portfolio_id,
            "scope": scope,
            "artifact_id": checkpoint["cursor"].get("artifact_id"),
            "object_hash": checkpoint.get("object_hash"),
        }

    def audit(self, artifact_id: str) -> dict[str, Any]:
        record = self.state.artifact_record(artifact_id)
        findings: set[str] = set()
        if record is None or str(record["type"]) not in {
            "PortfolioAnalysisReport",
            "PortfolioConstructionReport",
        }:
            return {
                "status": "FAIL",
                "artifact_id": artifact_id,
                "finding_codes": ["UNKNOWN_REPORT"],
            }
        object_hash = str(record["object_hash"])
        if not self.objects.verify(object_hash):
            findings.add("REPORT_OBJECT_UNAVAILABLE")
        for input_hash in record["input_hashes"]:
            if not self.objects.verify(str(input_hash)):
                findings.add("REPORT_INPUT_OBJECT_UNAVAILABLE")
        return {
            "status": "PASS" if not findings else "FAIL",
            "artifact_id": artifact_id,
            "object_hash": object_hash,
            "finding_codes": sorted(findings),
            "paper_ledger_write_allowed": False,
            "broker_execution_allowed": False,
        }

    def _resolve_holdings(
        self,
        request: PortfolioAnalysisRequest,
    ) -> tuple[list[PortfolioHoldingInput], str | None, str | None]:
        if request.holdings:
            return request.holdings, None, None
        assert request.account_id is not None
        status = self.ledger.status(request.account_id)
        rows = [row for row in status["positions"] if int(row["qty_total"]) > 0]
        if not rows:
            return [], None, None
        cutoff = request.as_of
        holdings: list[PortfolioHoldingInput] = []
        try:
            resolved = [
                self.verifier.resolve_instrument(str(row["symbol"]), visible_at=cutoff)[0]
                for row in rows
            ]
        except PolicyError:
            if not self._live_current_allowed(request.live, request.as_of):
                raise
            self.reference.sync_instruments(live=True)
            cutoff = datetime.now(UTC)
            resolved = [
                self.verifier.resolve_instrument(str(row["symbol"]), visible_at=cutoff)[0]
                for row in rows
            ]
        market_by_symbol = {item.symbol: item.market for item in resolved}
        for row in rows:
            symbol = str(row["symbol"])
            market = market_by_symbol.get(symbol)
            if market is None:
                raise ValueError("paper position has no visible instrument identity")
            holdings.append(
                PortfolioHoldingInput(
                    company_id=symbol,
                    market=market,
                    weight=None,
                    created_at=request.as_of,
                )
            )
        snapshot_payload = {
            "schema_version": "portfolio-account-snapshot-v1",
            "account_id": request.account_id,
            "as_of": request.as_of.isoformat(),
            "balances_fen": status["balances_fen"],
            "positions": rows,
            "last_event_seq": status["last_event_seq"],
        }
        snapshot_id, snapshot_hash = self._freeze_input(
            "PortfolioAccountSnapshot",
            "portfolio-account-snapshot-v1",
            snapshot_payload,
        )
        return holdings, snapshot_id, snapshot_hash

    def _materialize_account_weights(
        self,
        request: PortfolioAnalysisRequest,
        holdings: list[PortfolioHoldingInput],
        histories: list[_History],
        *,
        cutoff: datetime,
    ) -> list[PortfolioHoldingInput]:
        if request.account_id is None:
            return holdings
        status = self.ledger.status(request.account_id)
        qty_by_symbol = {
            str(row["symbol"]): int(row["qty_total"])
            for row in status["positions"]
            if int(row["qty_total"]) > 0
        }
        price_by_symbol = {item.company_id: item.latest_close_fen for item in histories}
        values = {
            symbol: qty * price_by_symbol[symbol]
            for symbol, qty in qty_by_symbol.items()
            if symbol in price_by_symbol
        }
        cash = int(status["balances_fen"]["CASH"]) + int(status["balances_fen"]["FROZEN_CASH"])
        nav = cash + sum(values.values())
        if nav <= 0:
            raise ValueError("paper portfolio NAV is not positive")
        return [
            holding.model_copy(
                update={
                    "weight": values.get(holding.company_id, 0) / nav,
                    "created_at": cutoff,
                }
            )
            for holding in holdings
        ]

    def _history(
        self,
        company_id: str,
        market: Market,
        *,
        as_of: datetime,
        lookback_sessions: int,
        minimum_sessions: int,
        live: bool,
        allow_live_capture: bool,
    ) -> _History:
        start = as_of.astimezone(_SHANGHAI).date() - timedelta(
            days=max(120, int(lookback_sessions * 2.2))
        )
        end = as_of.astimezone(_SHANGHAI).date()
        cutoff = as_of
        try:
            bars, release_id = self.verifier.visible_daily_history(
                market,
                company_id,
                visible_at=cutoff,
            )
        except PolicyError:
            if not allow_live_capture or not self._live_current_allowed(live, as_of):
                raise
            report = self.reference.sync_daily(company_id, market, start, end, live=True)
            if report.status is ReferenceCoverageStatus.FAILED or report.release_id is None:
                raise ValueError(
                    "live daily synchronization did not publish a release"
                ) from None
            cutoff = datetime.now(UTC)
            bars, release_id = self.verifier.visible_daily_history(
                market,
                company_id,
                visible_at=cutoff,
            )
        bars = [
            item
            for item in bars
            if start <= item.session_date <= end and item.available_to_system_at <= cutoff
        ]
        if (
            len(bars) < minimum_sessions + 1
            and allow_live_capture
            and self._live_current_allowed(live, as_of)
        ):
            report = self.reference.sync_daily(company_id, market, start, end, live=True)
            if report.release_id is not None:
                cutoff = datetime.now(UTC)
                bars, release_id = self.verifier.visible_daily_history(
                    market,
                    company_id,
                    visible_at=cutoff,
                )
                bars = [
                    item
                    for item in bars
                    if start <= item.session_date <= end and item.available_to_system_at <= cutoff
                ]
        if len(bars) < minimum_sessions + 1:
            raise ValueError("daily history has too few point-in-time observations")
        bars = sorted(bars, key=lambda item: item.session_date)[-(lookback_sessions + 1) :]
        if any(float(item.close) <= 0 for item in bars):
            raise ValueError("daily history contains a non-positive close")
        artifact_id = f"market-reference:{release_id}"
        artifact = self.state.artifact_record(artifact_id)
        if artifact is None:
            raise ValueError("daily reference release artifact is missing")
        object_hash = str(artifact["object_hash"])
        if not self.objects.verify(object_hash):
            raise ValueError("daily reference release object is unavailable")
        closes = {item.session_date.isoformat(): float(item.close) for item in bars}
        return _History(
            company_id=company_id,
            market=market,
            closes_by_date=closes,
            latest_close_fen=int(round(float(bars[-1].close) * 100)),
            release_id=release_id,
            release_object_hash=object_hash,
            cutoff_at=cutoff,
        )

    @staticmethod
    def _live_current_allowed(live: bool, as_of: datetime) -> bool:
        if not live:
            return False
        now = datetime.now(UTC)
        return abs(now - as_of.astimezone(UTC)) <= _CURRENT_LIVE_TOLERANCE

    @staticmethod
    def _align(
        histories: list[_History],
        benchmark: _History,
        *,
        lookback_sessions: int,
        minimum_sessions: int,
    ) -> _Aligned:
        return align_return_histories(
            [item.company_id for item in histories],
            [item.closes_by_date for item in histories],
            benchmark.closes_by_date,
            lookback_sessions=lookback_sessions,
            minimum_sessions=minimum_sessions,
        )

    def _analysis_report(
        self,
        *,
        request: PortfolioAnalysisRequest,
        holdings: list[PortfolioHoldingInput],
        histories: list[_History],
        benchmark: _History,
        aligned: _Aligned,
        data_cutoff_at: datetime,
        source_ids: list[str],
        source_hashes: list[str],
    ) -> PortfolioAnalysisReport:
        weight_by_company = {item.company_id: float(item.weight or 0) for item in holdings}
        weights = np.array([weight_by_company[item.company_id] for item in histories], dtype=float)
        invested = float(weights.sum())
        if invested <= 0 or invested > 1.0000001:
            raise ValueError("portfolio invested weight must be within 0..1")
        stats = portfolio_risk_statistics(aligned, weights)
        benchmark_returns = aligned.benchmark_returns
        contribution_fractions = stats.risk_contribution_fractions
        top_risk = (
            float(np.max(np.abs(contribution_fractions)))
            if len(contribution_fractions)
            else 0.0
        )
        industries: dict[str, float] = {}
        warnings: set[str] = {"UNADJUSTED_DAILY_RETURN_SERIES"}
        for holding in holdings:
            tag = holding.industry_tag or "UNVERIFIED"
            industries[tag] = industries.get(tag, 0.0) + float(holding.weight or 0)
            if holding.industry_tag is None:
                warnings.add("INDUSTRY_EXPOSURE_NOT_FULLY_VERIFIED")
        if np.max(np.abs(aligned.asset_returns)) > 0.35:
            warnings.add("MATERIAL_PRICE_JUMP_REVIEW_CORPORATE_ACTION")
        hard: set[str] = set()
        if invested > float(self.committee_rules.max_total_exposure) + 1e-9:
            hard.add("MAX_TOTAL_EXPOSURE_BREACH")
        if max(weights) > float(self.committee_rules.max_single_position) + 1e-9:
            hard.add("MAX_SINGLE_POSITION_BREACH")
        verified_industries = {
            key: value for key, value in industries.items() if key != "UNVERIFIED"
        }
        if (
            verified_industries
            and max(verified_industries.values())
            > float(self.committee_rules.max_industry_exposure) + 1e-9
        ):
            hard.add("MAX_INDUSTRY_EXPOSURE_BREACH")
        if stats.max_abs_pair_correlation > float(
            self.committee_rules.max_abs_correlation
        ) + 1e-9:
            hard.add("MAX_CORRELATION_BREACH")
        if stats.max_drawdown > float(self.committee_rules.max_portfolio_drawdown) + 1e-9:
            hard.add("MAX_DRAWDOWN_BREACH")
        metrics = PortfolioRiskMetrics(
            invested_weight=invested,
            cash_weight=max(0.0, 1.0 - invested),
            constant_weight_historical_annualized_return=stats.annualized_return,
            annualized_volatility=stats.annualized_volatility,
            annualized_downside_deviation=stats.annualized_downside_deviation,
            beta_to_benchmark=stats.beta_to_benchmark,
            annualized_tracking_error=stats.annualized_tracking_error,
            max_drawdown=stats.max_drawdown,
            historical_var_95=stats.historical_var_95,
            historical_cvar_95=stats.historical_cvar_95,
            historical_cdar_95=stats.historical_cdar_95,
            concentration_hhi=stats.concentration_hhi,
            effective_number_of_positions=stats.effective_number_of_positions,
            max_abs_pair_correlation=stats.max_abs_pair_correlation,
            top_risk_contribution_fraction=top_risk,
            industry_exposures=dict(sorted(industries.items())),
            created_at=request.as_of,
        )
        asset_metrics: list[PortfolioAssetRisk] = []
        benchmark_var = float(np.var(benchmark_returns, ddof=1))
        for index, history in enumerate(histories):
            series = aligned.asset_returns[:, index]
            asset_beta = (
                float(np.cov(series, benchmark_returns, ddof=1)[0, 1]) / benchmark_var
                if benchmark_var > 1e-15
                else 0.0
            )
            pair_corr = (
                float(np.max(np.abs(np.delete(aligned.correlation[index], index))))
                if len(histories) > 1
                else 0.0
            )
            holding = next(item for item in holdings if item.company_id == history.company_id)
            asset_metrics.append(
                PortfolioAssetRisk(
                    company_id=history.company_id,
                    market=history.market,
                    weight=float(weights[index]),
                    latest_close_fen=history.latest_close_fen,
                    observation_count=aligned.common_session_count,
                    annualized_volatility=float(np.std(series, ddof=1) * sqrt(_ANNUALIZATION)),
                    beta_to_benchmark=asset_beta,
                    risk_contribution_fraction=float(contribution_fractions[index]),
                    max_abs_pair_correlation=pair_corr,
                    industry_tag=holding.industry_tag,
                    daily_release_id=history.release_id,
                    daily_release_object_hash=history.release_object_hash,
                    created_at=request.as_of,
                )
            )
        report_seed = {
            "request": request.model_dump(mode="json", exclude={"created_at"}),
            "cutoff": data_cutoff_at.isoformat(),
            "source_hashes": sorted(set(source_hashes)),
            "metrics": metrics.model_dump(mode="json", exclude={"created_at"}),
        }
        return PortfolioAnalysisReport(
            report_id="portfolio-analysis:" + content_hash(report_seed),
            portfolio_id=request.portfolio_id,
            as_of=request.as_of,
            data_cutoff_at=data_cutoff_at,
            status=PortfolioAnalysisStatus.READY,
            common_session_count=aligned.common_session_count,
            assets=sorted(asset_metrics, key=lambda item: item.company_id),
            metrics=metrics,
            benchmark_release_id=benchmark.release_id,
            benchmark_release_object_hash=benchmark.release_object_hash,
            warning_codes=sorted(warnings),
            hard_breach_codes=sorted(hard),
            source_artifact_ids=sorted(set(source_ids)),
            source_object_hashes=sorted(set(source_hashes)),
            created_at=request.as_of,
        )

    def _analysis_needs_info(
        self,
        request: PortfolioAnalysisRequest,
        request_artifact: str,
        request_hash: str,
        reasons: list[str],
        *,
        source_ids: list[str] | None = None,
        source_hashes: list[str] | None = None,
    ) -> PortfolioAnalysisReport:
        ids = sorted(set([request_artifact] + list(source_ids or [])))
        hashes = sorted(set([request_hash] + list(source_hashes or [])))
        report = PortfolioAnalysisReport(
            report_id="portfolio-analysis:"
            + content_hash(
                {"request_hash": request_hash, "reasons": sorted(set(reasons)), "sources": hashes}
            ),
            portfolio_id=request.portfolio_id,
            as_of=request.as_of,
            data_cutoff_at=request.as_of,
            status=PortfolioAnalysisStatus.NEEDS_INFO,
            common_session_count=0,
            assets=[],
            warning_codes=sorted(set(reasons)),
            hard_breach_codes=[],
            source_artifact_ids=ids,
            source_object_hashes=hashes,
            created_at=request.as_of,
        )
        self._persist_report(report, hashes, "portfolio-analysis")
        return report

    def _portfolio_candidate(
        self,
        artifact_id: str,
        as_of: datetime,
    ) -> tuple[ClassifiedTradeProtocol, TradingClassificationRelease, str, str]:
        record = self.state.artifact_record(artifact_id)
        if record is None or str(record["type"]) != "ClassifiedTradeProtocol":
            raise ValueError("portfolio candidate requires ClassifiedTradeProtocol")
        protocol_hash = str(record["object_hash"])
        if not self.objects.verify(protocol_hash):
            raise ValueError("classified protocol object is unavailable")
        protocol = ClassifiedTradeProtocol.model_validate_json(
            self.objects.get_bytes(protocol_hash)
        )
        if (
            protocol.final_outcome is not TradeProtocolOutcome.APPROVE_SIMULATION
            or not protocol.paper_simulation_allowed
            or protocol.blocking_codes
        ):
            raise ValueError("portfolio candidate is not simulation-approved")
        if protocol.as_of != as_of:
            raise ValueError("portfolio candidates must share the exact construction as_of")
        for parent_id, parent_type, expected_hash in (
            (
                protocol.decision_pack_artifact_id,
                "DecisionPack",
                protocol.decision_pack_object_hash,
            ),
            (
                protocol.committee_protocol_artifact_id,
                "TradeProtocol",
                protocol.committee_protocol_object_hash,
            ),
        ):
            parent = self.state.artifact_record(parent_id)
            if parent is None or str(parent["type"]) != parent_type:
                raise ValueError("portfolio candidate parent lineage is unavailable")
            parent_hash = str(parent["object_hash"])
            if parent_hash != expected_hash or not self.objects.verify(parent_hash):
                raise ValueError("portfolio candidate parent lineage drift")
        classification_record = self.state.artifact_record(
            protocol.trading_classification_artifact_id
        )
        if (
            classification_record is None
            or str(classification_record["type"]) != "TradingClassificationRelease"
        ):
            raise ValueError("portfolio candidate classification is unavailable")
        classification_hash = str(classification_record["object_hash"])
        if (
            classification_hash != protocol.trading_classification_object_hash
            or not self.objects.verify(classification_hash)
        ):
            raise ValueError("portfolio candidate classification lineage drift")
        classification = TradingClassificationRelease.model_validate_json(
            self.objects.get_bytes(classification_hash)
        )
        if (
            classification.company_id != protocol.company_id
            or classification.as_of != protocol.as_of
            or classification.classification.board != protocol.board
            or classification.classification.risk_status != protocol.risk_status
            or classification.special_regime is not protocol.special_regime
            or classification.price_limit_regime is not protocol.price_limit_regime
            or classification.price_limit_rate_bps != protocol.price_limit_rate_bps
        ):
            raise ValueError("portfolio candidate classification projection drift")
        if not (classification.effective_from <= as_of <= classification.valid_until):
            raise ValueError("portfolio candidate classification is outside its validity")
        return protocol, classification, protocol_hash, classification_hash

    def _proposals(
        self,
        aligned: _Aligned,
        groups: dict[str, str],
        target: float,
    ) -> list[PortfolioAllocationProposal]:
        proposals: list[PortfolioAllocationProposal] = []
        for method in self.allocator_policy.enabled_methods:
            plugin = self.allocator_registry.get(method)
            raw_scores = plugin.build_scores(aligned)
            if raw_scores.shape != (len(aligned.company_ids),):
                raise ValueError(f"portfolio allocator returned an invalid score vector: {method}")
            weights, binding = self._constrain_scores(
                aligned.company_ids,
                raw_scores,
                groups,
                target=target,
            )
            vector = np.array([weights[item] for item in aligned.company_ids], dtype=float)
            invested = float(vector.sum())
            normalized = vector / invested if invested > 0 else vector
            hhi = float(np.sum(normalized**2)) if invested > 0 else 0.0
            group_weights: dict[str, float] = {}
            for company_id, weight in weights.items():
                group = groups[company_id]
                group_weights[group] = group_weights.get(group, 0.0) + weight
            proposals.append(
                PortfolioAllocationProposal(
                    method=method,
                    weights=dict(sorted((key, float(value)) for key, value in weights.items())),
                    cash_weight=max(0.0, 1.0 - invested),
                    ex_ante_annualized_volatility=sqrt(
                        max(float(vector @ aligned.covariance @ vector), 0.0) * _ANNUALIZATION
                    ),
                    concentration_hhi=hhi,
                    max_single_weight=max(weights.values(), default=0.0),
                    max_group_weight=max(group_weights.values(), default=0.0),
                    binding_constraint_codes=sorted(binding),
                    model_risk_codes=sorted(
                        {*plugin.model_risk_codes, "RISK_GROUP_IS_CALLER_SUPPLIED"}
                    ),
                )
            )
        return proposals

    def _constrain_scores(
        self,
        company_ids: list[str],
        raw_scores: np.ndarray,
        groups: dict[str, str],
        *,
        target: float,
    ) -> tuple[dict[str, float], set[str]]:
        return constrain_scores(
            company_ids,
            raw_scores,
            groups,
            target=target,
            max_single=float(self.committee_rules.max_single_position),
            max_group=float(self.committee_rules.max_industry_exposure),
        )

    @staticmethod
    def _minimum_variance_weights(covariance: np.ndarray) -> np.ndarray:
        return minimum_variance_weights(covariance)

    @staticmethod
    def _hierarchical_weights(aligned: _Aligned) -> np.ndarray:
        return hierarchical_risk_weights(aligned)

    def _freeze_input(
        self,
        artifact_type: str,
        schema_version: str,
        payload: dict[str, Any],
    ) -> tuple[str, str]:
        object_ref = self.objects.put_json(payload)
        identity = sha256_bytes(canonical_json_bytes(payload))
        artifact_id = f"{artifact_type}:{identity}"
        existing = self.state.artifact_record(artifact_id)
        if existing is not None:
            if (
                str(existing["type"]) != artifact_type
                or str(existing["object_hash"]) != object_ref.sha256
            ):
                raise ValueError(f"{artifact_type} identity collision")
            return artifact_id, object_ref.sha256
        self.state.register_artifact(
            artifact_id=artifact_id,
            artifact_type=artifact_type,
            schema_version=schema_version,
            object_hash=object_ref.sha256,
            input_hashes=[],
        )
        return artifact_id, object_ref.sha256

    def _persist_report(
        self,
        report: PortfolioAnalysisReport | PortfolioConstructionReport,
        input_hashes: list[str],
        checkpoint_scope: str,
    ) -> str:
        payload = report.model_dump(mode="json")
        object_ref = self.objects.put_json(payload)
        artifact_type = type(report).__name__
        artifact_id = f"{artifact_type}:{report.report_id}"
        existing = self.state.artifact_record(artifact_id)
        inputs = sorted(set(input_hashes))
        if existing is not None:
            if (
                str(existing["type"]) != artifact_type
                or str(existing["object_hash"]) != object_ref.sha256
                or sorted(existing["input_hashes"]) != inputs
            ):
                raise ValueError("portfolio report identity collision")
        else:
            self.state.register_artifact(
                artifact_id=artifact_id,
                artifact_type=artifact_type,
                schema_version=report.schema_version,
                object_hash=object_ref.sha256,
                input_hashes=inputs,
            )
        self.state.set_checkpoint(
            scope_type=checkpoint_scope,
            scope_key=report.portfolio_id,
            cursor={"artifact_id": artifact_id, "report_id": report.report_id},
            status=report.status.value,
            object_hash=object_ref.sha256,
        )
        return artifact_id

    @staticmethod
    def _reason_code(exc: Exception) -> str:
        message = str(exc).casefold()
        if "unknown paper account" in message:
            return "PORTFOLIO_ACCOUNT_REQUIRED"
        if "daily" in message or "session" in message or "reference" in message:
            return "PORTFOLIO_MARKET_HISTORY_REQUIRED"
        if "corporate-action" in message or "price jump" in message:
            return "PORTFOLIO_CORPORATE_ACTION_REVIEW_REQUIRED"
        if "classified" in message or "classification" in message:
            return "PORTFOLIO_CANDIDATE_RESEARCH_NOT_CURRENT"
        if "instrument" in message or "market" in message:
            return "PORTFOLIO_INSTRUMENT_IDENTITY_REQUIRED"
        if "nav" in message or "weight" in message:
            return "PORTFOLIO_WEIGHT_STATE_INVALID"
        return "PORTFOLIO_ANALYSIS_NEEDS_INFO"


__all__ = ["PortfolioService"]
