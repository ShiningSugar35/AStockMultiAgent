"""Portfolio transition, holding-state intake, and hedge-governance service."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from math import floor
from pathlib import Path
from typing import Any, cast
from zoneinfo import ZoneInfo

import numpy as np
import yaml

from astock.core.errors import PolicyError
from astock.core.hashing import canonical_json_bytes, content_hash, sha256_bytes
from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.external_accounts import ExternalAccountRepository
from astock.local_portfolio import LocalPortfolioService
from astock.portfolio.analytics import portfolio_risk_statistics
from astock.portfolio.service import PortfolioService, _History
from astock.schemas.external_accounts import (
    ExternalAccountEventDraft,
    ExternalAccountEventType,
)
from astock.schemas.knowledge import PositionAction
from astock.schemas.market import InstrumentType, Market, SourceClass
from astock.schemas.portfolio import (
    PortfolioAnalysisReport,
    PortfolioAnalysisRequest,
    PortfolioAnalysisStatus,
    PortfolioConstructionReport,
)
from astock.schemas.portfolio_decision import (
    DeclaredTradeValidationStatus,
    ETFProductProfile,
    ETFResearchMetrics,
    ETFResearchMetricsRequest,
    ExternalTradeImportReceipt,
    HedgeClassification,
    HedgeEffectivenessReport,
    HedgeEffectivenessRequest,
    HedgeInstrumentCandidate,
    InstrumentTradingUnitRule,
    PortfolioComplementCandidate,
    PortfolioComplementScreenReport,
    PortfolioComplementScreenRequest,
    PortfolioImplementationCostInput,
    PortfolioIntentProfile,
    PortfolioRiskGap,
    PortfolioRiskObjective,
    PortfolioTransitionReport,
    PortfolioTransitionRequest,
    PortfolioVariantMetrics,
    PositionTargetBand,
    RebalanceBandPolicy,
    SettlementCycle,
    UserDeclaredTradeCapture,
    UserPortfolioPosition,
    UserPortfolioSnapshot,
    ValidatedExternalTradeImport,
)
from astock.schemas.research_seeds import ResearchSeedReport, ResearchSeedStatus
from astock.schemas.source_access import OfficialWebDocumentCapture


class PortfolioDecisionService:
    """Build read-only portfolio transition decisions on top of canonical services."""

    def __init__(
        self,
        state: StateStore,
        objects: ObjectStore,
        local_portfolio: LocalPortfolioService,
        portfolio: PortfolioService,
        project_root: Path,
    ) -> None:
        self.state = state
        self.objects = objects
        self.local_portfolio = local_portfolio
        self.external_accounts = ExternalAccountRepository(state, objects)
        self.portfolio = portfolio
        self.project_root = project_root.resolve()
        self._config = self._load_config(self.project_root / "configs" / "portfolio_decision.yaml")
        self.rebalance_policy = RebalanceBandPolicy.model_validate(
            self._config["rebalance_band_policy"]
        )

    def import_declared_trade(
        self,
        capture: UserDeclaredTradeCapture,
    ) -> ExternalTradeImportReceipt:
        capture_id, capture_hash = self._freeze(
            "UserDeclaredTradeCapture",
            capture.schema_version,
            capture.model_dump(mode="json"),
        )
        missing = capture.missing_fields()
        if missing:
            receipt = ExternalTradeImportReceipt(
                receipt_id="external-trade-receipt:"
                + content_hash({"capture": capture_hash, "missing": missing}),
                status=DeclaredTradeValidationStatus.NEEDS_INFO,
                capture_artifact_id=capture_id,
                reason_codes=sorted(f"MISSING_{item.upper()}" for item in missing),
                created_at=capture.declared_at,
            )
            return self._persist_receipt(receipt, [capture_hash])

        assert capture.market is not None
        assert capture.symbol is not None
        assert capture.side is not None
        assert capture.quantity is not None
        assert capture.price_cny is not None
        assert capture.occurred_at is not None
        try:
            instrument, release_id = self.portfolio.verifier.resolve_instrument(
                capture.symbol,
                visible_at=capture.declared_at,
            )
        except (PolicyError, ValueError):
            receipt = ExternalTradeImportReceipt(
                receipt_id="external-trade-receipt:"
                + content_hash({"capture": capture_hash, "identity": "unproven"}),
                status=DeclaredTradeValidationStatus.NEEDS_INFO,
                capture_artifact_id=capture_id,
                reason_codes=["INSTRUMENT_IDENTITY_UNPROVEN"],
                created_at=capture.declared_at,
            )
            return self._persist_receipt(receipt, [capture_hash])
        if instrument.market is not capture.market or instrument.symbol != capture.symbol:
            receipt = ExternalTradeImportReceipt(
                receipt_id="external-trade-receipt:"
                + content_hash({"capture": capture_hash, "identity": instrument.instrument_id}),
                status=DeclaredTradeValidationStatus.CONFLICT,
                capture_artifact_id=capture_id,
                reason_codes=["DECLARED_MARKET_IDENTITY_CONFLICT"],
                created_at=capture.declared_at,
            )
            return self._persist_receipt(receipt, [capture_hash])
        trade_date = capture.occurred_at.astimezone(ZoneInfo("Asia/Shanghai")).date()
        outside_listed_period = (
            instrument.listing_date is not None and trade_date < instrument.listing_date
        ) or (instrument.delisting_date is not None and trade_date > instrument.delisting_date)
        if outside_listed_period:
            receipt = ExternalTradeImportReceipt(
                receipt_id="external-trade-receipt:"
                + content_hash({"capture": capture_hash, "trade_date": trade_date.isoformat()}),
                status=DeclaredTradeValidationStatus.CONFLICT,
                capture_artifact_id=capture_id,
                reason_codes=["TRADE_OCCURRED_OUTSIDE_LISTED_PERIOD"],
                created_at=capture.declared_at,
            )
            return self._persist_receipt(receipt, [capture_hash])

        validation = ValidatedExternalTradeImport(
            capture_artifact_id=capture_id,
            instrument_id=instrument.instrument_id,
            market=capture.market,
            symbol=capture.symbol,
            side=cast(Any, capture.side),
            quantity=capture.quantity,
            price_cny=capture.price_cny,
            occurred_at=capture.occurred_at,
            raw_statement=capture.raw_statement,
            created_at=capture.declared_at,
        )
        validation_id, validation_hash = self._freeze(
            "ValidatedExternalTradeImport",
            validation.schema_version,
            validation.model_dump(mode="json"),
            input_hashes=[capture_hash],
        )
        instrument_release = self.state.artifact_record(f"market-reference:{release_id}")
        input_hashes = [capture_hash, validation_hash]
        if instrument_release is not None:
            instrument_hash = str(instrument_release["object_hash"])
            if self.objects.verify(instrument_hash):
                input_hashes.append(instrument_hash)
        try:
            if self.external_accounts.get_account("default") is None:
                self.external_accounts.create_account(
                    account_id="default",
                    display_name="默认外部账户",
                    created_at=capture.declared_at,
                )
            inserted_event_ids, duplicate_event_ids = self.external_accounts.append_drafts(
                [
                    ExternalAccountEventDraft(
                        account_id="default",
                        event_type=ExternalAccountEventType.TRADE,
                        occurred_at=capture.occurred_at,
                        available_to_system_at=capture.declared_at,
                        market=capture.market,
                        symbol=capture.symbol,
                        side=cast(Any, capture.side),
                        quantity=capture.quantity,
                        price_cny=capture.price_cny,
                        source_artifact_hash=validation_hash,
                        idempotency_key=f"validated-external:{validation_hash}",
                        note=f"user-declared:{capture_id}",
                        created_at=capture.declared_at,
                    )
                ]
            )
        except ValueError as exc:
            receipt = ExternalTradeImportReceipt(
                receipt_id="external-trade-receipt:"
                + content_hash({"validation": validation_hash, "conflict": str(exc)}),
                status=DeclaredTradeValidationStatus.CONFLICT,
                capture_artifact_id=capture_id,
                validation_artifact_id=validation_id,
                reason_codes=[self._reason_code(exc)],
                created_at=capture.declared_at,
            )
            return self._persist_receipt(receipt, input_hashes)

        event_id = (inserted_event_ids or duplicate_event_ids)[0]
        duplicate = not inserted_event_ids
        reason_codes: list[str] = []
        position_projection: dict[str, object] | None = None
        try:
            compatibility = self.local_portfolio.record_validated_external_trade(validation)
            positions = cast(list[dict[str, object]], compatibility["positions"])
            position_projection = next(
                (
                    item
                    for item in positions
                    if str(item["market"]) == capture.market.value
                    and str(item["symbol"]) == capture.symbol
                ),
                None,
            )
        except (OSError, ValueError):
            reason_codes.append("LEGACY_LOCAL_PROJECTION_REFRESH_FAILED")
        try:
            self.external_accounts.write_markdown_projection(
                self.project_root,
                as_of=capture.declared_at,
            )
        except (OSError, ValueError):
            reason_codes.append("EXTERNAL_ACCOUNT_MARKDOWN_PROJECTION_REFRESH_FAILED")
        if position_projection is None:
            projected = self.external_accounts.projection("default", as_of=capture.declared_at)
            canonical_position = next(
                (
                    item
                    for item in projected.positions
                    if item.market is capture.market and item.symbol == capture.symbol
                ),
                None,
            )
            if canonical_position is not None:
                position_projection = {
                    "market": canonical_position.market.value,
                    "symbol": canonical_position.symbol,
                    "quantity": canonical_position.quantity,
                    "average_cost_cny": str(canonical_position.average_cost_cny),
                    "opened_at": canonical_position.opened_at.isoformat(),
                    "last_trade_at": canonical_position.last_event_at.isoformat(),
                }
        reason_codes = sorted(set(reason_codes))
        trade_id = f"external-account-event:{event_id}"
        receipt = ExternalTradeImportReceipt(
            receipt_id="external-trade-receipt:"
            + content_hash(
                {
                    "validation": validation_hash,
                    "trade_id": trade_id,
                    "duplicate": duplicate,
                    "reason_codes": reason_codes,
                }
            ),
            status=(
                DeclaredTradeValidationStatus.DUPLICATE
                if duplicate
                else DeclaredTradeValidationStatus.READY
            ),
            capture_artifact_id=capture_id,
            validation_artifact_id=validation_id,
            trade_id=trade_id,
            reason_codes=reason_codes,
            deduplicated=duplicate,
            position_projection=position_projection,
            created_at=capture.declared_at,
        )
        return self._persist_receipt(receipt, input_hashes)

    def snapshot_local_portfolio(self, *, as_of: datetime) -> UserPortfolioSnapshot:
        """Freeze the default external-account authority plus local review/order projections."""

        status = self.local_portfolio.status()
        legacy_positions = cast(list[dict[str, object]], status["positions"])
        review_by_key = {
            (str(item["market"]), str(item["symbol"])): item for item in legacy_positions
        }
        open_orders = [
            {str(key): value for key, value in item.items()}
            for item in cast(list[dict[str, object]], status["open_orders"])
        ]
        external_account = self.external_accounts.get_account("default")
        if external_account is not None:
            external_projection = self.external_accounts.projection("default", as_of=as_of)
            external_events = self.external_accounts.list_events("default", as_of=as_of)
            if (
                external_projection.cash_known
                and external_projection.cash_cny is not None
                and external_projection.cash_cny < 0
            ):
                raise ValueError("external account cash cannot be negative for portfolio snapshot")
            positions = []
            for item in external_projection.positions:
                review = review_by_key.get((item.market.value, item.symbol), {})
                positions.append(
                    UserPortfolioPosition(
                        market=item.market,
                        symbol=item.symbol,
                        quantity=item.quantity,
                        average_cost_cny=item.average_cost_cny,
                        opened_at=item.opened_at,
                        last_trade_at=item.last_event_at,
                        last_review_at=(
                            datetime.fromisoformat(str(review["last_review_at"]))
                            if review.get("last_review_at")
                            else None
                        ),
                        last_action=str(review.get("last_action", "HOLD")),
                        thesis_status=str(review.get("thesis_status", "UNREVIEWED")),
                        review_note=str(review.get("review_note", "")),
                        created_at=as_of,
                    )
                )
            trade_count = sum(
                1
                for item in external_events
                if item.event_type is ExternalAccountEventType.TRADE
            )
            cash_cny = external_projection.cash_cny
            cash_known = external_projection.cash_known
            snapshot_source = "EXTERNAL_ACCOUNT_DEFAULT"
            boundary_payload: dict[str, object] = {
                "as_of": as_of.isoformat(),
                "authority": snapshot_source,
                "external_projection": external_projection.model_dump(mode="json"),
                "open_orders": open_orders,
                "legacy_review_projection": legacy_positions,
            }
        else:
            positions = [
                UserPortfolioPosition(
                    market=Market(str(item["market"])),
                    symbol=str(item["symbol"]),
                    quantity=int(str(item["quantity"])),
                    average_cost_cny=Decimal(str(item["average_cost_cny"])),
                    opened_at=datetime.fromisoformat(str(item["opened_at"])),
                    last_trade_at=datetime.fromisoformat(str(item["last_trade_at"])),
                    last_review_at=(
                        datetime.fromisoformat(str(item["last_review_at"]))
                        if item.get("last_review_at")
                        else None
                    ),
                    last_action=str(item.get("last_action", "HOLD")),
                    thesis_status=str(item.get("thesis_status", "UNREVIEWED")),
                    review_note=str(item.get("review_note", "")),
                    created_at=as_of,
                )
                for item in legacy_positions
            ]
            trade_count = int(str(status["trade_count"]))
            cash_cny = None
            cash_known = False
            snapshot_source = "LOCAL_USER_STATE"
            boundary_payload = {
                "as_of": as_of.isoformat(),
                "authority": snapshot_source,
                "status": status,
            }
        seed = {
            "as_of": as_of.isoformat(),
            "source": snapshot_source,
            "positions": [item.model_dump(mode="json") for item in positions],
            "open_orders": open_orders,
            "trade_count": trade_count,
            "cash_cny": str(cash_cny) if cash_cny is not None else None,
            "cash_known": cash_known,
        }
        _, boundary_hash = self._freeze(
            "UserPortfolioStateBoundary",
            "user-portfolio-state-v2",
            boundary_payload,
        )
        snapshot = UserPortfolioSnapshot(
            snapshot_id="user-portfolio:" + content_hash(seed),
            as_of=as_of,
            positions=positions,
            open_orders=open_orders,
            trade_count=trade_count,
            cash_cny=cash_cny,
            cash_known=cash_known,
            source=cast(Any, snapshot_source),
            created_at=as_of,
        )
        self._freeze_exact(
            f"UserPortfolioSnapshot:{snapshot.snapshot_id}",
            "UserPortfolioSnapshot",
            snapshot.schema_version,
            snapshot.model_dump(mode="json"),
            input_hashes=[boundary_hash],
        )
        return snapshot

    def register_etf_profile(self, profile: ETFProductProfile) -> ETFProductProfile:
        if profile.available_to_system_at > datetime.now(UTC):
            raise ValueError("ETF product profile cannot be future-visible")
        self._verify_provenance_pairs(
            profile.official_source_artifact_ids,
            profile.official_source_object_hashes,
            label="ETF official product",
            visible_at=profile.available_to_system_at,
            require_official_web=True,
        )
        inputs = list(profile.official_source_object_hashes)
        if profile.paper_replay_supported:
            raise ValueError(
                "ETF paper replay remains disabled until instrument-specific execution is admitted"
            )
        artifact_id = f"ETFProductProfile:{profile.profile_id}"
        self._freeze_exact(
            artifact_id,
            "ETFProductProfile",
            profile.schema_version,
            profile.model_dump(mode="json"),
            input_hashes=inputs,
        )
        return profile

    def evaluate_etf_metrics(self, request: ETFResearchMetricsRequest) -> ETFResearchMetrics:
        """Freeze PIT ETF liquidity, volatility, fee and tracking diagnostics."""

        request_id, request_hash = self._freeze(
            "ETFResearchMetricsRequest",
            request.schema_version,
            request.model_dump(mode="json"),
        )
        profile, profile_hash = self._load_model(
            request.profile_artifact_id,
            "ETFProductProfile",
            ETFProductProfile,
        )
        if profile.available_to_system_at > request.as_of:
            raise ValueError("ETF product profile was not visible at metrics as_of")
        policy_id, policy_hash = self._policy_artifact()
        source_ids = {request_id, request.profile_artifact_id, policy_id}
        source_hashes = {request_hash, profile_hash, policy_hash}
        history = self.portfolio._history(
            profile.symbol,
            profile.market,
            as_of=request.as_of,
            lookback_sessions=request.lookback_sessions,
            minimum_sessions=request.minimum_sessions,
            live=False,
            allow_live_capture=False,
        )
        self._add_history_lineage(history, source_ids, source_hashes)
        research_closes = history.research_closes_by_date or history.closes_by_date
        ordered = np.asarray([research_closes[key] for key in sorted(research_closes)], dtype=float)
        if len(ordered) < request.minimum_sessions + 1:
            raise ValueError("ETF research metrics have too few PIT observations")
        returns = ordered[1:] / ordered[:-1] - 1.0
        annualized_volatility = float(np.std(returns, ddof=1) * np.sqrt(252.0))
        tracking_error: float | None = None
        tracking_benchmark_id: str | None = None
        warnings: set[str] = set(history.research_series_warning_codes)
        observation_count = len(returns)
        if (
            profile.tracking_benchmark_market is not None
            and profile.tracking_benchmark_symbol is not None
        ):
            benchmark = self.portfolio._history(
                profile.tracking_benchmark_symbol,
                profile.tracking_benchmark_market,
                as_of=request.as_of,
                lookback_sessions=request.lookback_sessions,
                minimum_sessions=request.minimum_sessions,
                live=False,
                allow_live_capture=False,
            )
            self._add_history_lineage(benchmark, source_ids, source_hashes)
            aligned = self.portfolio._align(
                [history],
                benchmark,
                lookback_sessions=request.lookback_sessions,
                minimum_sessions=request.minimum_sessions,
            )
            tracking_error = float(
                np.std(
                    aligned.asset_returns[:, 0] - aligned.benchmark_returns,
                    ddof=1,
                )
                * np.sqrt(252.0)
            )
            annualized_volatility = float(
                np.std(aligned.asset_returns[:, 0], ddof=1) * np.sqrt(252.0)
            )
            observation_count = aligned.common_session_count
            tracking_benchmark_id = (
                f"{profile.tracking_benchmark_market.value}:{profile.tracking_benchmark_symbol}"
            )
        else:
            warnings.add("ETF_TRACKING_BENCHMARK_NOT_BOUND")
        if history.average_daily_amount_cny is None:
            warnings.add("ETF_LIQUIDITY_AMOUNT_UNAVAILABLE")
        if profile.total_expense_ratio_bps is None:
            warnings.add("ETF_TOTAL_EXPENSE_RATIO_UNAVAILABLE")
        if profile.management_fee_bps is None or profile.custody_fee_bps is None:
            warnings.add("ETF_COMPONENT_FEES_INCOMPLETE")
        warnings.add("ETF_PREMIUM_DISCOUNT_UNAVAILABLE_NO_NAV_SERIES")
        seed = {
            "request_hash": request_hash,
            "profile_hash": profile_hash,
            "source_hashes": sorted(source_hashes),
            "tracking_error": tracking_error,
            "average_amount": history.average_daily_amount_cny,
        }
        metrics = ETFResearchMetrics(
            metrics_id="etf-research-metrics:" + content_hash(seed),
            profile_artifact_id=request.profile_artifact_id,
            instrument_id=profile.instrument_id,
            market=profile.market,
            symbol=profile.symbol,
            as_of=request.as_of,
            observation_count=observation_count,
            average_daily_amount_cny=history.average_daily_amount_cny,
            annualized_volatility=annualized_volatility,
            tracking_benchmark_instrument_id=tracking_benchmark_id,
            tracking_error_annualized=tracking_error,
            premium_discount_rate=None,
            management_fee_bps=profile.management_fee_bps,
            custody_fee_bps=profile.custody_fee_bps,
            total_expense_ratio_bps=profile.total_expense_ratio_bps,
            warning_codes=sorted(warnings),
            source_artifact_ids=sorted(source_ids),
            source_object_hashes=sorted(source_hashes),
            created_at=request.as_of,
        )
        self._freeze_exact(
            f"ETFResearchMetrics:{metrics.metrics_id}",
            "ETFResearchMetrics",
            metrics.schema_version,
            metrics.model_dump(mode="json"),
            input_hashes=metrics.source_object_hashes,
        )
        return metrics

    def screen_complements(
        self, request: PortfolioComplementScreenRequest
    ) -> PortfolioComplementScreenReport:
        """Rank bounded ResearchSeed candidates only by portfolio-risk complementarity."""

        request_id, request_hash = self._freeze(
            "PortfolioComplementScreenRequest",
            request.schema_version,
            request.model_dump(mode="json"),
        )
        current, current_hash = self._load_model(
            request.current_analysis_artifact_id,
            "PortfolioAnalysisReport",
            PortfolioAnalysisReport,
        )
        seeds, seed_hash = self._load_model(
            request.research_seed_report_artifact_id,
            "ResearchSeedReport",
            ResearchSeedReport,
        )
        if current.status is not PortfolioAnalysisStatus.READY or current.metrics is None:
            raise ValueError("portfolio complement screening requires a READY analysis")
        if seeds.status is not ResearchSeedStatus.READY:
            raise ValueError("portfolio complement screening requires a READY seed report")
        if current.as_of != seeds.as_of:
            raise ValueError("portfolio complement screening requires one exact as_of")
        if request.objective in {
            PortfolioRiskObjective.REDUCE_INDUSTRY_EXPOSURE,
            PortfolioRiskObjective.PROTECT_SCENARIO,
        }:
            raise ValueError(
                "portfolio complement screening needs typed industry/scenario exposures "
                "for this objective"
            )
        analysis_request = self._source_request(current, PortfolioAnalysisRequest)
        policy_id, policy_hash = self._policy_artifact()
        source_ids = {
            request_id,
            request.current_analysis_artifact_id,
            request.research_seed_report_artifact_id,
            policy_id,
        }
        source_hashes = {request_hash, current_hash, seed_hash, policy_hash}
        current_ids = {item.company_id for item in current.assets}
        pre_screen_limit = min(12, max(6, request.max_candidates))
        ranked_seed_inputs = sorted(
            (item for item in seeds.seeds if item.company_id not in current_ids),
            key=lambda item: (-item.research_priority_score, item.company_id),
        )[:pre_screen_limit]
        current_histories: list[_History] = []
        current_weights: dict[str, float] = {}
        for asset in current.assets:
            history = self.portfolio._history(
                asset.company_id,
                asset.market,
                as_of=current.as_of,
                lookback_sessions=analysis_request.lookback_sessions,
                minimum_sessions=analysis_request.minimum_common_sessions,
                live=False,
                allow_live_capture=False,
            )
            current_histories.append(history)
            current_weights[asset.company_id] = asset.weight
            self._add_history_lineage(history, source_ids, source_hashes)
        benchmark = self.portfolio._history(
            analysis_request.benchmark_symbol,
            analysis_request.benchmark_market,
            as_of=current.as_of,
            lookback_sessions=analysis_request.lookback_sessions,
            minimum_sessions=analysis_request.minimum_common_sessions,
            live=False,
            allow_live_capture=False,
        )
        self._add_history_lineage(benchmark, source_ids, source_hashes)
        candidates: list[PortfolioComplementCandidate] = []
        warnings: set[str] = set()
        for seed in ranked_seed_inputs:
            try:
                history = self.portfolio._history(
                    seed.company_id,
                    seed.market,
                    as_of=current.as_of,
                    lookback_sessions=analysis_request.lookback_sessions,
                    minimum_sessions=analysis_request.minimum_common_sessions,
                    live=False,
                    allow_live_capture=False,
                )
                self._add_history_lineage(history, source_ids, source_hashes)
                aligned = self.portfolio._align(
                    [*current_histories, history],
                    benchmark,
                    lookback_sessions=analysis_request.lookback_sessions,
                    minimum_sessions=analysis_request.minimum_common_sessions,
                )
                baseline_weights = np.asarray(
                    [current_weights.get(item.company_id, 0.0) for item in current_histories],
                    dtype=float,
                )
                portfolio_returns = (
                    aligned.asset_returns[:, : len(current_histories)] @ baseline_weights
                )
                candidate_returns = aligned.asset_returns[:, -1]
                corr, corr_valid = self._correlation_with_status(
                    portfolio_returns, candidate_returns
                )
                benchmark_var = float(np.var(aligned.benchmark_returns, ddof=1))
                beta = (
                    float(np.cov(candidate_returns, aligned.benchmark_returns, ddof=1)[0, 1])
                    / benchmark_var
                    if benchmark_var > 1e-15
                    else 0.0
                )
                annualized_vol = float(np.std(candidate_returns, ddof=1) * np.sqrt(252.0))
                if request.objective is PortfolioRiskObjective.REDUCE_MARKET_BETA:
                    benefit = 1.0 / (1.0 + abs(beta))
                else:
                    benefit = max(0.0, min(1.0, (1.0 - corr) / 2.0)) if corr_valid else 0.0
                liquidity = float(seed.market_liquidity_score or 0.0)
                score = min(
                    1.0,
                    max(
                        0.0,
                        0.60 * benefit + 0.20 * liquidity + 0.20 * seed.research_priority_score,
                    ),
                )
                candidates.append(
                    PortfolioComplementCandidate(
                        company_id=seed.company_id,
                        market=seed.market,
                        name=seed.name,
                        prefilter_score=score,
                        portfolio_correlation=corr,
                        beta_to_benchmark=beta,
                        annualized_volatility=annualized_vol,
                        market_liquidity_score=seed.market_liquidity_score,
                        research_priority_score=seed.research_priority_score,
                        created_at=current.as_of,
                    )
                )
            except (PolicyError, ValueError, np.linalg.LinAlgError):
                warnings.add(f"COMPLEMENT_HISTORY_UNAVAILABLE:{seed.company_id}")
        candidates.sort(key=lambda item: (-item.prefilter_score, item.company_id))
        candidates = candidates[: request.max_candidates]
        if not seeds.formal_full_market_coverage_allowed:
            warnings.add("RESEARCH_SEED_UNIVERSE_COVERAGE_NOT_FORMAL_FULL_MARKET")
        if not candidates:
            warnings.add("NO_COMPLEMENT_CANDIDATES_AFTER_BOUNDED_SCREEN")
        report_seed = {
            "request_hash": request_hash,
            "current_hash": current_hash,
            "seed_hash": seed_hash,
            "candidate_ids": [item.company_id for item in candidates],
            "source_hashes": sorted(source_hashes),
        }
        report = PortfolioComplementScreenReport(
            report_id="portfolio-complement-screen:" + content_hash(report_seed),
            as_of=current.as_of,
            objective=request.objective,
            candidates=candidates,
            universe_coverage_complete=seeds.formal_full_market_coverage_allowed,
            warning_codes=sorted(warnings),
            source_artifact_ids=sorted(source_ids),
            source_object_hashes=sorted(source_hashes),
            created_at=current.as_of,
        )
        self._freeze_exact(
            f"PortfolioComplementScreenReport:{report.report_id}",
            "PortfolioComplementScreenReport",
            report.schema_version,
            report.model_dump(mode="json"),
            input_hashes=report.source_object_hashes,
        )
        return report

    def evaluate_hedge(
        self,
        request: HedgeEffectivenessRequest,
    ) -> HedgeEffectivenessReport:
        """Recalculate one hedge/diversifier candidate on PIT-aligned local returns."""

        request_id, request_hash = self._freeze(
            "HedgeEffectivenessRequest",
            request.schema_version,
            request.model_dump(mode="json"),
        )
        current, current_hash = self._load_model(
            request.current_analysis_artifact_id,
            "PortfolioAnalysisReport",
            PortfolioAnalysisReport,
        )
        if current.status is not PortfolioAnalysisStatus.READY or current.metrics is None:
            raise ValueError("hedge effectiveness requires a READY current analysis")
        if request.symbol in {item.company_id for item in current.assets}:
            raise ValueError("hedge effectiveness candidate must be outside the current holdings")
        analysis_request = self._source_request(current, PortfolioAnalysisRequest)
        if analysis_request.as_of != current.as_of:
            raise ValueError("portfolio analysis request/report as_of drift")
        if (
            current.metrics.invested_weight + request.hedge_weight
            > min(
                1.0,
                float(self.portfolio.committee_rules.max_total_exposure),
            )
            + 1e-9
        ):
            raise ValueError("hedge overlay exceeds the portfolio total exposure limit")

        policy_id, policy_hash = self._policy_artifact()
        source_ids = {request_id, request.current_analysis_artifact_id, policy_id}
        source_hashes = {request_hash, current_hash, policy_hash}
        if request.instrument_type is InstrumentType.ETF:
            assert request.etf_profile_artifact_id is not None
            profile, profile_hash = self._load_model(
                request.etf_profile_artifact_id,
                "ETFProductProfile",
                ETFProductProfile,
            )
            if (
                profile.instrument_id != request.instrument_id
                or profile.market is not request.market
                or profile.symbol != request.symbol
            ):
                raise ValueError("hedge ETF request does not match the registered product profile")
            if profile.available_to_system_at > current.as_of:
                raise ValueError("hedge ETF product profile was not visible at the portfolio as_of")
            source_ids.add(request.etf_profile_artifact_id)
            source_hashes.add(profile_hash)
            assert request.etf_metrics_artifact_id is not None
            etf_metrics, etf_metrics_hash = self._load_model(
                request.etf_metrics_artifact_id,
                "ETFResearchMetrics",
                ETFResearchMetrics,
            )
            if (
                etf_metrics.profile_artifact_id != request.etf_profile_artifact_id
                or etf_metrics.instrument_id != request.instrument_id
                or etf_metrics.as_of != current.as_of
            ):
                raise ValueError("ETF hedge metrics do not match profile/instrument/as_of")
            source_ids.add(request.etf_metrics_artifact_id)
            source_hashes.add(etf_metrics_hash)
        else:
            etf_metrics = None
        mechanism_verified = self._verify_provenance_pairs(
            request.mechanism_source_artifact_ids,
            request.mechanism_source_object_hashes,
            label="hedge mechanism",
            visible_at=current.as_of,
            require_official_web=True,
        )
        source_ids.update(request.mechanism_source_artifact_ids)
        source_hashes.update(request.mechanism_source_object_hashes)
        cost = request.implementation_cost
        if cost is not None:
            self._verify_cost(cost, visible_at=current.as_of)
            source_ids.update(cost.source_artifact_ids)
            source_hashes.update(cost.source_object_hashes)

        histories: list[_History] = []
        current_weights: dict[str, float] = {}
        for asset in current.assets:
            history = self.portfolio._history(
                asset.company_id,
                asset.market,
                as_of=current.as_of,
                lookback_sessions=analysis_request.lookback_sessions,
                minimum_sessions=analysis_request.minimum_common_sessions,
                live=False,
                allow_live_capture=False,
            )
            histories.append(history)
            current_weights[asset.company_id] = asset.weight
            self._add_history_lineage(history, source_ids, source_hashes)
        hedge_history = self.portfolio._history(
            request.symbol,
            request.market,
            as_of=current.as_of,
            lookback_sessions=analysis_request.lookback_sessions,
            minimum_sessions=analysis_request.minimum_common_sessions,
            live=False,
            allow_live_capture=False,
        )
        histories.append(hedge_history)
        self._add_history_lineage(hedge_history, source_ids, source_hashes)
        benchmark = self.portfolio._history(
            analysis_request.benchmark_symbol,
            analysis_request.benchmark_market,
            as_of=current.as_of,
            lookback_sessions=analysis_request.lookback_sessions,
            minimum_sessions=analysis_request.minimum_common_sessions,
            live=False,
            allow_live_capture=False,
        )
        self._add_history_lineage(benchmark, source_ids, source_hashes)
        aligned = self.portfolio._align(
            histories,
            benchmark,
            lookback_sessions=analysis_request.lookback_sessions,
            minimum_sessions=analysis_request.minimum_common_sessions,
        )
        minimum_sessions = int(self._config["hedge_effectiveness"]["minimum_common_sessions"])
        if aligned.common_session_count < minimum_sessions:
            raise ValueError("hedge effectiveness has too few common PIT sessions")
        symbols = [item.company_id for item in histories]
        baseline_weights = np.asarray(
            [current_weights.get(symbol, 0.0) for symbol in symbols],
            dtype=float,
        )
        hedged_weights = baseline_weights.copy()
        hedge_index = symbols.index(request.symbol)
        hedged_weights[hedge_index] = request.hedge_weight
        baseline_stats = portfolio_risk_statistics(aligned, baseline_weights)
        hedged_stats = portfolio_risk_statistics(aligned, hedged_weights)
        baseline_returns = aligned.asset_returns @ baseline_weights
        hedge_returns = aligned.asset_returns[:, hedge_index]
        normal_corr, normal_corr_valid = self._correlation_with_status(
            baseline_returns, hedge_returns
        )
        tail_fraction = float(self._config["hedge_effectiveness"]["stress_tail_fraction"])
        tail_count = max(5, int(np.ceil(len(baseline_returns) * tail_fraction)))
        tail_indices = np.argsort(baseline_returns)[:tail_count]
        stress_corr, stress_corr_valid = self._correlation_with_status(
            baseline_returns[tail_indices],
            hedge_returns[tail_indices],
        )
        baseline_risk, hedged_risk, supported = self._targeted_risk_values(
            request.targeted_risk,
            baseline_stats,
            hedged_stats,
        )
        gross_reduction = (
            (baseline_risk - hedged_risk) / baseline_risk
            if supported and baseline_risk > 1e-12
            else 0.0
        )
        minimum_reduction = float(
            self._config["hedge_effectiveness"]["minimum_gross_risk_reduction_fraction"]
        )
        maximum_cost_bps = float(
            self._config["hedge_effectiveness"]["maximum_round_trip_cost_bps_for_formal_hedge"]
        )
        verified_cost_bps = (
            float(cost.estimated_round_trip_cost_bps)
            if cost is not None and cost.verified
            else None
        )
        cost_acceptable = verified_cost_bps is not None and verified_cost_bps <= maximum_cost_bps
        maximum_stress_corr = float(
            self._config["hedge_effectiveness"]["maximum_stress_correlation"]
        )
        basis_codes: set[str] = {
            "HEDGE_EFFECTIVENESS_IS_HISTORICAL_MODEL_ESTIMATE",
            "HEDGE_TEST_IS_GROSS_OVERLAY_NOT_SELF_FINANCING",
        }
        if cost is None or not cost.verified:
            basis_codes.add("IMPLEMENTATION_COST_UNVERIFIED")
        elif not cost_acceptable:
            basis_codes.add("IMPLEMENTATION_COST_ABOVE_FORMAL_HEDGE_LIMIT")
        else:
            basis_codes.add("IMPLEMENTATION_COST_WITHIN_FORMAL_HEDGE_LIMIT")
        if not mechanism_verified:
            basis_codes.add("ECONOMIC_HEDGE_MECHANISM_UNVERIFIED")
        if not normal_corr_valid:
            basis_codes.add("NORMAL_CORRELATION_UNDEFINED")
        if not stress_corr_valid:
            basis_codes.add("STRESS_CORRELATION_UNDEFINED")
        elif stress_corr > maximum_stress_corr:
            basis_codes.add("STRESS_CORRELATION_TOO_HIGH")
        if not supported:
            basis_codes.add("TARGETED_RISK_NOT_DETERMINISTICALLY_SUPPORTED")
        etf_metrics_adequate = True
        if request.instrument_type is InstrumentType.ETF:
            assert etf_metrics is not None
            etf_metrics_adequate = (
                etf_metrics.average_daily_amount_cny is not None
                and etf_metrics.total_expense_ratio_bps is not None
                and (
                    etf_metrics.tracking_benchmark_instrument_id is None
                    or etf_metrics.tracking_error_annualized is not None
                )
            )
            if not etf_metrics_adequate:
                basis_codes.add("ETF_RESEARCH_METRICS_INCOMPLETE_FOR_FORMAL_HEDGE")
            if etf_metrics.premium_discount_rate is None:
                basis_codes.add("ETF_PREMIUM_DISCOUNT_UNAVAILABLE")

        classification = HedgeClassification.UNPROVEN
        if supported and gross_reduction > 0:
            classification = HedgeClassification.DIVERSIFICATION
        if (
            supported
            and mechanism_verified
            and etf_metrics_adequate
            and cost is not None
            and cost.verified
            and cost_acceptable
            and stress_corr_valid
            and gross_reduction >= minimum_reduction
            and stress_corr <= maximum_stress_corr
        ):
            classification = HedgeClassification.NATURAL_HEDGE
        # The current long-only stock/ETF system has no inverse, short, derivative,
        # margin, or daily-settlement contract, so it never emits EXPLICIT_HEDGE.
        if request.instrument_type is InstrumentType.ETF:
            basis_codes.add("EXPLICIT_HEDGE_NOT_AVAILABLE_WITH_CURRENT_LONG_ONLY_TOOLKIT")

        report_seed = {
            "request_hash": request_hash,
            "current_hash": current_hash,
            "source_hashes": sorted(source_hashes),
            "baseline_risk": baseline_risk,
            "hedged_risk": hedged_risk,
            "normal_corr": normal_corr,
            "stress_corr": stress_corr,
        }
        report = HedgeEffectivenessReport(
            report_id="hedge-effectiveness:" + content_hash(report_seed),
            current_analysis_artifact_id=request.current_analysis_artifact_id,
            instrument_id=request.instrument_id,
            targeted_risk=request.targeted_risk,
            hedge_weight=request.hedge_weight,
            classification=classification,
            baseline_risk_value=max(0.0, baseline_risk),
            hedged_risk_value=max(0.0, hedged_risk),
            gross_risk_reduction_fraction=gross_reduction,
            estimated_round_trip_cost_bps=verified_cost_bps,
            cost_verified=verified_cost_bps is not None,
            cost_acceptable=cost_acceptable,
            normal_correlation=normal_corr,
            stress_correlation=stress_corr,
            common_session_count=aligned.common_session_count,
            basis_risk_codes=sorted(basis_codes),
            source_artifact_ids=sorted(source_ids),
            source_object_hashes=sorted(source_hashes),
            created_at=current.as_of,
        )
        self._freeze_exact(
            f"HedgeEffectivenessReport:{report.report_id}",
            "HedgeEffectivenessReport",
            report.schema_version,
            report.model_dump(mode="json"),
            input_hashes=report.source_object_hashes,
        )
        return report

    def transition(self, request: PortfolioTransitionRequest) -> PortfolioTransitionReport:
        request_id, request_hash = self._freeze(
            "PortfolioTransitionRequest",
            request.schema_version,
            request.model_dump(mode="json"),
        )
        current, current_hash = self._load_model(
            request.current_analysis_artifact_id,
            "PortfolioAnalysisReport",
            PortfolioAnalysisReport,
        )
        construction, construction_hash = self._load_model(
            request.target_construction_artifact_id,
            "PortfolioConstructionReport",
            PortfolioConstructionReport,
        )
        if current.status is not PortfolioAnalysisStatus.READY or current.metrics is None:
            raise ValueError("portfolio transition requires a READY current analysis")
        if construction.status is not PortfolioAnalysisStatus.READY:
            raise ValueError("portfolio transition requires a READY target construction")
        if current.portfolio_id != request.intent.portfolio_id:
            raise ValueError("portfolio transition intent does not match current portfolio")
        if current.as_of != construction.as_of or current.as_of != request.intent.as_of:
            raise ValueError("portfolio transition requires one exact as_of across all inputs")
        proposal = next(
            (item for item in construction.proposals if item.method is request.selected_method),
            None,
        )
        if proposal is None:
            raise ValueError("selected portfolio allocation method is unavailable")

        analysis_request = self._source_request(current, PortfolioAnalysisRequest)
        current_weights = {item.company_id: item.weight for item in current.assets}
        target_weights = dict(proposal.weights)
        warnings: set[str] = set()
        policy_id, policy_hash = self._policy_artifact()
        source_ids = {
            request_id,
            request.current_analysis_artifact_id,
            request.target_construction_artifact_id,
            policy_id,
        }
        source_hashes = {request_hash, current_hash, construction_hash, policy_hash}
        if not request.intent.constraints_complete:
            warnings.add("USER_CONSTRAINTS_INCOMPLETE_DEFAULT_POLICY_APPLIED")
        if (
            construction.admitted_company_ids
            and InstrumentType.STOCK not in request.intent.allowed_instrument_types
        ):
            raise ValueError("target stocks are outside the investor allowed instrument set")
        if request.intent.max_industry_exposure is not None:
            raise ValueError(
                "explicit industry exposure limit requires a verified industry taxonomy "
                "that is not available in the current portfolio transition contract"
            )

        etf_profiles: dict[str, ETFProductProfile] = {}
        hedge_by_instrument = {item.instrument_id: item for item in request.hedge_candidates}
        for overlay in request.supplemental_assets:
            if InstrumentType.ETF not in request.intent.allowed_instrument_types:
                raise ValueError("ETF overlay is outside the investor allowed instrument set")
            profile, profile_hash = self._load_model(
                str(overlay.etf_profile_artifact_id),
                "ETFProductProfile",
                ETFProductProfile,
            )
            if (
                profile.instrument_id != overlay.instrument_id
                or profile.market is not overlay.market
                or profile.symbol != overlay.symbol
            ):
                raise ValueError("ETF overlay does not match the registered product profile")
            if profile.available_to_system_at > request.intent.as_of:
                raise ValueError(
                    "ETF overlay product profile was not visible at the transition as_of"
                )
            assert overlay.etf_metrics_artifact_id is not None
            metrics, metrics_hash = self._load_model(
                overlay.etf_metrics_artifact_id,
                "ETFResearchMetrics",
                ETFResearchMetrics,
            )
            if (
                metrics.profile_artifact_id != overlay.etf_profile_artifact_id
                or metrics.instrument_id != overlay.instrument_id
                or metrics.as_of != request.intent.as_of
            ):
                raise ValueError("ETF overlay metrics do not match profile/instrument/as_of")
            source_ids.add(overlay.etf_metrics_artifact_id)
            source_hashes.add(metrics_hash)
            warnings.update(metrics.warning_codes)
            hedge = hedge_by_instrument.get(overlay.instrument_id)
            if hedge is None:
                raise ValueError(
                    "ETF overlay requires an explicit diversification/hedge assessment"
                )
            self._verify_hedge_candidate(hedge, visible_at=request.intent.as_of)
            etf_profiles[overlay.instrument_id] = profile
            target_weights[overlay.symbol] = (
                target_weights.get(overlay.symbol, 0.0) + overlay.target_weight
            )
            source_ids.add(str(overlay.etf_profile_artifact_id))
            source_hashes.add(profile_hash)

        max_total = min(
            float(self.portfolio.committee_rules.max_total_exposure),
            float(
                request.intent.max_total_exposure
                or self.portfolio.committee_rules.max_total_exposure
            ),
            1.0 - request.intent.minimum_cash_weight,
        )
        max_single = min(
            float(self.portfolio.committee_rules.max_single_position),
            float(
                request.intent.max_single_position
                or self.portfolio.committee_rules.max_single_position
            ),
        )
        if sum(target_weights.values()) > max_total + 1e-9:
            raise ValueError("target portfolio breaches total exposure/cash constraints")
        if max(target_weights.values(), default=0.0) > max_single + 1e-9:
            raise ValueError("target portfolio breaches the maximum single-position constraint")
        for locked in request.intent.user_locked_company_ids:
            if (
                locked in current_weights
                and target_weights.get(locked, 0.0) + 1e-12 < current_weights[locked]
            ):
                raise ValueError("target portfolio trims a user-locked holding")

        instrument_meta: dict[str, tuple[Market, InstrumentType]] = {
            item.company_id: (item.market, InstrumentType.STOCK) for item in current.assets
        }
        for company_id in construction.admitted_company_ids:
            instrument, release_id = self.portfolio.verifier.resolve_instrument(
                company_id,
                visible_at=request.intent.as_of,
            )
            if instrument.instrument_type is not InstrumentType.STOCK:
                raise ValueError("committee-approved portfolio candidates must remain stocks")
            instrument_meta[company_id] = (instrument.market, InstrumentType.STOCK)
            release = self.state.artifact_record(f"market-reference:{release_id}")
            if release is not None and self.objects.verify(str(release["object_hash"])):
                source_ids.add(f"market-reference:{release_id}")
                source_hashes.add(str(release["object_hash"]))
        for profile in etf_profiles.values():
            instrument_meta[profile.symbol] = (profile.market, InstrumentType.ETF)

        union_symbols = sorted(set(current_weights) | set(target_weights))
        histories: list[_History] = []
        for symbol in union_symbols:
            meta = instrument_meta.get(symbol)
            if meta is None:
                instrument, _ = self.portfolio.verifier.resolve_instrument(
                    symbol,
                    visible_at=request.intent.as_of,
                )
                meta = (instrument.market, instrument.instrument_type)
                instrument_meta[symbol] = meta
            history = self.portfolio._history(
                symbol,
                meta[0],
                as_of=request.intent.as_of,
                lookback_sessions=analysis_request.lookback_sessions,
                minimum_sessions=analysis_request.minimum_common_sessions,
                live=False,
                allow_live_capture=False,
            )
            histories.append(history)
            source_ids.add(f"market-reference:{history.release_id}")
            source_hashes.add(history.release_object_hash)
            if history.corporate_action_release_id and history.corporate_action_release_object_hash:
                source_ids.add(f"market-reference:{history.corporate_action_release_id}")
                source_hashes.add(history.corporate_action_release_object_hash)
            warnings.update(history.research_series_warning_codes)
        benchmark = self.portfolio._history(
            analysis_request.benchmark_symbol,
            analysis_request.benchmark_market,
            as_of=request.intent.as_of,
            lookback_sessions=analysis_request.lookback_sessions,
            minimum_sessions=analysis_request.minimum_common_sessions,
            live=False,
            allow_live_capture=False,
        )
        source_ids.add(f"market-reference:{benchmark.release_id}")
        source_hashes.add(benchmark.release_object_hash)
        aligned = self.portfolio._align(
            histories,
            benchmark,
            lookback_sessions=analysis_request.lookback_sessions,
            minimum_sessions=analysis_request.minimum_common_sessions,
        )
        history_by_symbol = {item.company_id: item for item in histories}

        current_variant = self._variant(
            "CURRENT",
            union_symbols,
            current_weights,
            aligned,
            created_at=request.intent.as_of,
        )
        target_variant = self._variant(
            "TARGET",
            union_symbols,
            target_weights,
            aligned,
            created_at=request.intent.as_of,
        )
        self._enforce_target_risk_constraints(target_variant, request.intent)
        anchor_variant = self._anchor_variant(
            request.intent,
            union_symbols,
            current_weights,
            target_weights,
            aligned,
        )
        risk_gaps = self._risk_gaps(current, request.intent)

        cost_by_instrument = {item.instrument_id: item for item in request.implementation_costs}
        for item in request.implementation_costs:
            self._verify_cost(item, visible_at=request.intent.as_of)
            source_ids.update(item.source_artifact_ids)
            source_hashes.update(item.source_object_hashes)
        explicit_rules = {item.instrument_id: item for item in request.trading_rules}
        target_bands: list[PositionTargetBand] = []
        for index, symbol in enumerate(union_symbols):
            market, instrument_type = instrument_meta[symbol]
            instrument_id = f"{market.value}:{symbol}"
            current_weight = current_weights.get(symbol, 0.0)
            target_weight = target_weights.get(symbol, 0.0)
            cost = cost_by_instrument.get(instrument_id)
            volatility = float(np.std(aligned.asset_returns[:, index], ddof=1) * np.sqrt(252.0))
            half_band = self._half_band(volatility, cost)
            if cost is None or not cost.verified:
                warnings.add(f"IMPLEMENTATION_COST_UNVERIFIED:{instrument_id}")
            lower = max(0.0, target_weight - half_band)
            upper = min(max_single, max_total, target_weight + half_band)
            action = self._band_action(current_weight, target_weight, lower, upper)
            rule = explicit_rules.get(instrument_id)
            if rule is None and instrument_type is InstrumentType.STOCK:
                rule = self._stock_rule(instrument_id)
            if instrument_type is InstrumentType.ETF:
                profile = etf_profiles.get(instrument_id)
                if profile is None:
                    raise ValueError("ETF target lacks a registered product profile")
                rule = profile.trading_rule
            quantity_values = self._quantity_band(
                action=action,
                current_quantity=request.current_quantities.get(symbol),
                nav_fen=request.portfolio_nav_fen,
                raw_price_fen=history_by_symbol[symbol].latest_close_fen,
                lower=lower,
                upper=upper,
                rule=rule,
            )
            if quantity_values is None and request.portfolio_nav_fen is not None:
                warnings.add(f"TRADING_UNIT_EXACT_TRANSITION_UNAVAILABLE:{instrument_id}")
            target_bands.append(
                PositionTargetBand(
                    instrument_id=instrument_id,
                    current_weight=current_weight,
                    target_weight_lower=lower,
                    target_weight_mid=target_weight,
                    target_weight_upper=upper,
                    action=action,
                    current_quantity=request.current_quantities.get(symbol),
                    target_quantity_min=quantity_values[0] if quantity_values else None,
                    target_quantity_max=quantity_values[1] if quantity_values else None,
                    estimated_trade_quantity_min=quantity_values[2] if quantity_values else None,
                    estimated_trade_quantity_max=quantity_values[3] if quantity_values else None,
                    created_at=request.intent.as_of,
                )
            )

        all_hedges: list[HedgeInstrumentCandidate] = []
        for candidate in request.hedge_candidates:
            self._verify_hedge_candidate(candidate)
            source_ids.update(candidate.source_artifact_ids)
            source_hashes.update(candidate.source_object_hashes)
            all_hedges.append(candidate)
        turnover = 0.5 * sum(
            abs(target_weights.get(symbol, 0.0) - current_weights.get(symbol, 0.0))
            for symbol in union_symbols
        )
        if request.intent.maximum_turnover_weight is not None and turnover > (
            request.intent.maximum_turnover_weight + 1e-9
        ):
            raise ValueError("target portfolio breaches the investor turnover budget")
        implementation_cost_fen = self._implementation_cost(
            nav_fen=request.portfolio_nav_fen,
            current_weights=current_weights,
            target_weights=target_weights,
            instrument_meta=instrument_meta,
            costs=cost_by_instrument,
        )
        if implementation_cost_fen is None and turnover > 0:
            warnings.add("TOTAL_IMPLEMENTATION_COST_UNAVAILABLE")

        report_seed = {
            "request": request.model_dump(mode="json", exclude={"created_at"}),
            "current_hash": current_hash,
            "construction_hash": construction_hash,
            "target": target_variant.model_dump(mode="json", exclude={"created_at"}),
            "sources": sorted(source_hashes),
        }
        report = PortfolioTransitionReport(
            report_id="portfolio-transition:" + content_hash(report_seed),
            portfolio_id=current.portfolio_id,
            as_of=request.intent.as_of,
            current=current_variant,
            anchor_only=anchor_variant,
            target=target_variant,
            risk_gaps=risk_gaps,
            target_bands=target_bands,
            hedge_candidates=all_hedges,
            estimated_turnover_weight=turnover,
            estimated_implementation_cost_fen=implementation_cost_fen,
            warning_codes=sorted(warnings),
            source_artifact_ids=sorted(source_ids),
            source_object_hashes=sorted(source_hashes),
            created_at=request.intent.as_of,
        )
        ref = self.objects.put_json(report.model_dump(mode="json"))
        artifact_id = f"PortfolioTransitionReport:{report.report_id}"
        self.state.register_artifact(
            artifact_id=artifact_id,
            artifact_type="PortfolioTransitionReport",
            schema_version=report.schema_version,
            object_hash=ref.sha256,
            input_hashes=report.source_object_hashes,
        )
        self.state.set_checkpoint(
            scope_type="portfolio-transition",
            scope_key=report.portfolio_id,
            cursor={"artifact_id": artifact_id, "as_of": report.as_of.isoformat()},
            status="SUCCEEDED",
            object_hash=ref.sha256,
        )
        return report

    def audit(self, artifact_id: str) -> dict[str, object]:
        record = self.state.artifact_record(artifact_id)
        findings: set[str] = set()
        if record is None or str(record["type"]) not in {
            "PortfolioTransitionReport",
            "PortfolioComplementScreenReport",
            "ETFResearchMetrics",
            "HedgeEffectivenessReport",
            "UserPortfolioSnapshot",
            "ExternalTradeImportReceipt",
            "ETFProductProfile",
        }:
            return {
                "status": "FAIL",
                "artifact_id": artifact_id,
                "finding_codes": ["UNKNOWN_PORTFOLIO_DECISION_ARTIFACT"],
                "paper_ledger_write_allowed": False,
                "broker_execution_allowed": False,
            }
        if not self.objects.verify(str(record["object_hash"])):
            findings.add("ARTIFACT_OBJECT_UNAVAILABLE")
        for input_hash in record["input_hashes"]:
            if len(str(input_hash)) == 64 and not self.objects.verify(str(input_hash)):
                findings.add("INPUT_OBJECT_UNAVAILABLE")
        return {
            "status": "PASS" if not findings else "FAIL",
            "artifact_id": artifact_id,
            "finding_codes": sorted(findings),
            "allocation_override_allowed": False,
            "paper_ledger_write_allowed": False,
            "broker_execution_allowed": False,
        }

    def status(self, portfolio_id: str) -> dict[str, object]:
        checkpoint = self.state.get_checkpoint("portfolio-transition", portfolio_id)
        if checkpoint is None:
            return {"status": "NOT_RUN", "portfolio_id": portfolio_id}
        return {
            "status": checkpoint["status"],
            "portfolio_id": portfolio_id,
            "artifact_id": checkpoint["cursor"].get("artifact_id"),
            "object_hash": checkpoint.get("object_hash"),
            "broker_execution_allowed": False,
        }

    def _variant(
        self,
        variant: str,
        union_symbols: list[str],
        weights: dict[str, float],
        aligned: Any,
        *,
        created_at: datetime,
    ) -> PortfolioVariantMetrics:
        vector = np.asarray([weights.get(item, 0.0) for item in union_symbols], dtype=float)
        if float(vector.sum()) <= 0:
            raise ValueError("portfolio variant must retain positive invested exposure")
        stats = portfolio_risk_statistics(aligned, vector)
        return PortfolioVariantMetrics(
            variant=cast(Any, variant),
            weights={key: value for key, value in sorted(weights.items()) if value > 0},
            cash_weight=max(0.0, 1.0 - float(vector.sum())),
            annualized_volatility=stats.annualized_volatility,
            beta_to_benchmark=stats.beta_to_benchmark,
            max_drawdown=stats.max_drawdown,
            historical_cvar_95=stats.historical_cvar_95,
            concentration_hhi=stats.concentration_hhi,
            max_abs_pair_correlation=stats.max_abs_pair_correlation,
            created_at=created_at,
        )

    def _anchor_variant(
        self,
        intent: PortfolioIntentProfile,
        union_symbols: list[str],
        current_weights: dict[str, float],
        target_weights: dict[str, float],
        aligned: Any,
    ) -> PortfolioVariantMetrics | None:
        if not intent.anchor_company_id:
            return None
        anchor = intent.anchor_company_id
        desired = target_weights.get(anchor, 0.0)
        current = current_weights.get(anchor, 0.0)
        if desired <= current + 1e-12:
            return self._variant(
                "ANCHOR_ONLY",
                union_symbols,
                current_weights,
                aligned,
                created_at=intent.as_of,
            )
        cash = max(0.0, 1.0 - sum(current_weights.values()))
        add = min(desired - current, cash)
        weights = dict(current_weights)
        weights[anchor] = current + add
        return self._variant(
            "ANCHOR_ONLY",
            union_symbols,
            weights,
            aligned,
            created_at=intent.as_of,
        )

    def _enforce_target_risk_constraints(
        self,
        target: PortfolioVariantMetrics,
        intent: PortfolioIntentProfile,
    ) -> None:
        max_corr = min(
            float(self.portfolio.committee_rules.max_abs_correlation),
            float(intent.max_abs_correlation or self.portfolio.committee_rules.max_abs_correlation),
        )
        max_drawdown = min(
            float(self.portfolio.committee_rules.max_portfolio_drawdown),
            float(intent.max_drawdown or self.portfolio.committee_rules.max_portfolio_drawdown),
        )
        if target.max_abs_pair_correlation > max_corr + 1e-9:
            raise ValueError("target portfolio breaches the maximum correlation constraint")
        if target.max_drawdown > max_drawdown + 1e-9:
            raise ValueError("target portfolio breaches the maximum drawdown constraint")
        if (
            intent.max_market_beta is not None
            and abs(target.beta_to_benchmark) > intent.max_market_beta + 1e-9
        ):
            raise ValueError("target portfolio breaches the investor market-beta constraint")
        if (
            intent.target_annualized_volatility is not None
            and target.annualized_volatility > intent.target_annualized_volatility + 1e-9
        ):
            raise ValueError("target portfolio breaches the investor volatility constraint")

    def _risk_gaps(
        self,
        current: PortfolioAnalysisReport,
        intent: PortfolioIntentProfile,
    ) -> list[PortfolioRiskGap]:
        assert current.metrics is not None
        metrics = current.metrics
        values: list[PortfolioRiskGap] = []
        limits = {
            "TOTAL_EXPOSURE": (
                metrics.invested_weight,
                float(
                    intent.max_total_exposure or self.portfolio.committee_rules.max_total_exposure
                ),
            ),
            "SINGLE_POSITION": (
                max((item.weight for item in current.assets), default=0.0),
                float(
                    intent.max_single_position or self.portfolio.committee_rules.max_single_position
                ),
            ),
            "ABS_CORRELATION": (
                metrics.max_abs_pair_correlation,
                float(
                    intent.max_abs_correlation or self.portfolio.committee_rules.max_abs_correlation
                ),
            ),
            "MAX_DRAWDOWN": (
                metrics.max_drawdown,
                float(intent.max_drawdown or self.portfolio.committee_rules.max_portfolio_drawdown),
            ),
        }
        if intent.max_market_beta is not None:
            values[0:0] = [
                PortfolioRiskGap(
                    gap_code="MARKET_BETA",
                    current_value=abs(metrics.beta_to_benchmark),
                    target_or_limit=intent.max_market_beta,
                    severity=(
                        "HARD"
                        if abs(metrics.beta_to_benchmark) > intent.max_market_beta
                        else "INFO"
                    ),
                    source_artifact_ids=[],
                    source_object_hashes=[],
                    created_at=intent.as_of,
                )
            ]
        if intent.target_annualized_volatility is not None:
            values.append(
                PortfolioRiskGap(
                    gap_code="ANNUALIZED_VOLATILITY",
                    current_value=metrics.annualized_volatility,
                    target_or_limit=intent.target_annualized_volatility,
                    severity=(
                        "MATERIAL"
                        if metrics.annualized_volatility > intent.target_annualized_volatility
                        else "INFO"
                    ),
                    created_at=intent.as_of,
                )
            )
        for code, (observed, limit) in limits.items():
            values.append(
                PortfolioRiskGap(
                    gap_code=code,
                    current_value=observed,
                    target_or_limit=limit,
                    severity="HARD" if observed > limit + 1e-9 else "INFO",
                    created_at=intent.as_of,
                )
            )
        return sorted(values, key=lambda item: item.gap_code)

    def _half_band(
        self,
        volatility: float,
        cost: PortfolioImplementationCostInput | None,
    ) -> float:
        policy = self.rebalance_policy
        if cost is None or not cost.verified:
            base = policy.unverified_cost_weight_band
        else:
            base = max(
                policy.minimum_weight_band,
                cost.estimated_round_trip_cost_bps / 10_000 * policy.cost_to_band_multiplier,
            )
        return min(
            policy.maximum_weight_band,
            max(
                policy.minimum_weight_band,
                base + volatility * policy.volatility_to_band_multiplier,
            ),
        )

    @staticmethod
    def _band_action(
        current_weight: float,
        target_weight: float,
        lower: float,
        upper: float,
    ) -> PositionAction:
        if current_weight < lower - 1e-12:
            return PositionAction.ADD
        if current_weight > upper + 1e-12:
            return PositionAction.EXIT if target_weight <= 1e-12 else PositionAction.TRIM
        return PositionAction.HOLD

    def _quantity_band(
        self,
        *,
        action: PositionAction,
        current_quantity: int | None,
        nav_fen: int | None,
        raw_price_fen: int,
        lower: float,
        upper: float,
        rule: InstrumentTradingUnitRule | None,
    ) -> tuple[int, int, int, int] | None:
        if nav_fen is None or current_quantity is None or rule is None:
            return None
        if raw_price_fen <= 0:
            return None
        min_qty_raw = int(np.ceil(nav_fen * lower / raw_price_fen - 1e-12))
        max_qty_raw = floor(nav_fen * upper / raw_price_fen + 1e-12)
        if max_qty_raw < min_qty_raw:
            return None
        if action is PositionAction.HOLD:
            if not (min_qty_raw <= current_quantity <= max_qty_raw):
                return None
            return min_qty_raw, max_qty_raw, 0, 0
        if action is PositionAction.ADD:
            add_min_raw = max(0, min_qty_raw - current_quantity)
            add_max_raw = max(0, max_qty_raw - current_quantity)
            add_min = self._round_up(add_min_raw, rule.buy_lot_size) if add_min_raw else 0
            add_max = self._round_target_quantity(add_max_raw, rule.buy_lot_size)
            if add_max < add_min:
                return None
            return (
                current_quantity + add_min,
                current_quantity + add_max,
                add_min,
                add_max,
            )
        if action is PositionAction.EXIT:
            if not rule.allow_odd_lot_full_exit and current_quantity % rule.sell_lot_size:
                return None
            return 0, 0, -current_quantity, -current_quantity
        trim_min_raw = max(0, current_quantity - max_qty_raw)
        trim_max_raw = max(0, current_quantity - min_qty_raw)
        trim_min = self._round_up(trim_min_raw, rule.sell_lot_size) if trim_min_raw else 0
        trim_max = self._round_target_quantity(trim_max_raw, rule.sell_lot_size)
        if trim_max < trim_min:
            return None
        target_min = current_quantity - trim_max
        target_max = current_quantity - trim_min
        if target_min < min_qty_raw or target_max > max_qty_raw:
            return None
        return target_min, target_max, -trim_max, -trim_min

    @staticmethod
    def _round_target_quantity(quantity: int, lot: int) -> int:
        return max(0, quantity // lot * lot)

    @staticmethod
    def _round_up(quantity: int, lot: int) -> int:
        return ((quantity + lot - 1) // lot) * lot

    def _stock_rule(self, instrument_id: str) -> InstrumentTradingUnitRule:
        raw = self._config["stock_trading_rule"]
        return InstrumentTradingUnitRule(
            instrument_id=instrument_id,
            instrument_type=InstrumentType.STOCK,
            buy_lot_size=int(raw["buy_lot_size"]),
            sell_lot_size=int(raw["sell_lot_size"]),
            allow_odd_lot_full_exit=bool(raw["allow_odd_lot_full_exit"]),
            tick_size_cny=Decimal(str(raw["tick_size_cny"])),
            settlement_cycle=SettlementCycle(str(raw["settlement_cycle"])),
            effective_from=datetime.fromisoformat(str(raw["effective_from"])).date(),
            source_urls=sorted(str(item) for item in raw["source_urls"]),
            created_at=datetime.now(UTC),
        )

    @staticmethod
    def _correlation_with_status(left: np.ndarray, right: np.ndarray) -> tuple[float, bool]:
        if len(left) < 2 or len(right) < 2:
            return 0.0, False
        left_std = float(np.std(left, ddof=1))
        right_std = float(np.std(right, ddof=1))
        if left_std <= 1e-15 or right_std <= 1e-15:
            return 0.0, False
        value = float(np.corrcoef(left, right)[0, 1])
        if not np.isfinite(value):
            return 0.0, False
        return float(np.clip(value, -1.0, 1.0)), True

    @staticmethod
    def _targeted_risk_values(
        objective: PortfolioRiskObjective,
        baseline: Any,
        hedged: Any,
    ) -> tuple[float, float, bool]:
        if objective is PortfolioRiskObjective.REDUCE_MARKET_BETA:
            return abs(baseline.beta_to_benchmark), abs(hedged.beta_to_benchmark), True
        if objective in {
            PortfolioRiskObjective.REDUCE_VOLATILITY,
            PortfolioRiskObjective.DIVERSIFY,
        }:
            return baseline.annualized_volatility, hedged.annualized_volatility, True
        if objective is PortfolioRiskObjective.REDUCE_CONCENTRATION:
            return baseline.concentration_hhi, hedged.concentration_hhi, True
        return 0.0, 0.0, False

    @staticmethod
    def _add_history_lineage(
        history: _History,
        source_ids: set[str],
        source_hashes: set[str],
    ) -> None:
        source_ids.add(f"market-reference:{history.release_id}")
        source_hashes.add(history.release_object_hash)
        if history.corporate_action_release_id and history.corporate_action_release_object_hash:
            source_ids.add(f"market-reference:{history.corporate_action_release_id}")
            source_hashes.add(history.corporate_action_release_object_hash)

    def _verify_provenance_pairs(
        self,
        artifact_ids: list[str],
        object_hashes: list[str],
        *,
        label: str,
        visible_at: datetime | None = None,
        require_official_web: bool = False,
    ) -> bool:
        if not artifact_ids and not object_hashes:
            return False
        if len(artifact_ids) != len(object_hashes):
            raise ValueError(f"{label} artifact/hash counts must match")
        for artifact_id, expected_hash in zip(artifact_ids, object_hashes, strict=True):
            record = self.state.artifact_record(artifact_id)
            if record is None or str(record["object_hash"]) != expected_hash:
                raise ValueError(f"{label} provenance does not match registry")
            if not self.objects.verify(expected_hash):
                raise ValueError(f"{label} source object is unavailable")
            artifact_type = str(record["type"])
            if require_official_web and artifact_type != "OfficialWebDocumentCapture":
                raise ValueError(f"{label} requires official Web capture provenance")
            if artifact_type == "OfficialWebDocumentCapture":
                capture = OfficialWebDocumentCapture.model_validate_json(
                    self.objects.get_bytes(expected_hash)
                )
                if (
                    require_official_web
                    and capture.source_class is not SourceClass.PRIMARY_OFFICIAL_WEB
                ):
                    raise ValueError(f"{label} requires PRIMARY_OFFICIAL_WEB provenance")
                if visible_at is not None and capture.observed_at > visible_at:
                    raise ValueError(f"{label} source was not visible at the requested as_of")
            elif artifact_type == "HedgeEffectivenessReport" and visible_at is not None:
                report = HedgeEffectivenessReport.model_validate_json(
                    self.objects.get_bytes(expected_hash)
                )
                if report.created_at > visible_at:
                    raise ValueError(f"{label} report was not visible at the requested as_of")
        return True

    def _verify_hedge_candidate(
        self,
        candidate: HedgeInstrumentCandidate,
        *,
        visible_at: datetime | None = None,
    ) -> None:
        self._verify_provenance_pairs(
            candidate.source_artifact_ids,
            candidate.source_object_hashes,
            label="hedge candidate",
            visible_at=visible_at,
        )
        if candidate.classification in {
            HedgeClassification.NATURAL_HEDGE,
            HedgeClassification.EXPLICIT_HEDGE,
        }:
            effectiveness: HedgeEffectivenessReport | None = None
            for artifact_id, expected_hash in zip(
                candidate.source_artifact_ids,
                candidate.source_object_hashes,
                strict=True,
            ):
                record = self.state.artifact_record(artifact_id)
                if record is None or str(record["type"]) != "HedgeEffectivenessReport":
                    continue
                report = HedgeEffectivenessReport.model_validate_json(
                    self.objects.get_bytes(expected_hash)
                )
                if report.instrument_id == candidate.instrument_id:
                    effectiveness = report
                    break
            if effectiveness is None:
                raise ValueError("formal hedge classification requires HedgeEffectivenessReport")
            if effectiveness.classification is not candidate.classification:
                raise ValueError(
                    "hedge candidate classification disagrees with effectiveness report"
                )
            if effectiveness.gross_risk_reduction_fraction <= 0:
                raise ValueError("formal hedge candidate lacks positive risk reduction")
            if not effectiveness.cost_verified or not effectiveness.cost_acceptable:
                raise ValueError(
                    "formal hedge candidate lacks acceptable verified implementation cost"
                )
            if visible_at is not None and effectiveness.created_at > visible_at:
                raise ValueError(
                    "hedge effectiveness report was not visible at the requested as_of"
                )
        if candidate.classification is HedgeClassification.EXPLICIT_HEDGE:
            if candidate.instrument_type is not InstrumentType.ETF:
                raise ValueError(
                    "current explicit hedge classification is limited to verified ETF tools"
                )
            raise ValueError("explicit hedge is not admitted by the current long-only toolkit")

    def _verify_cost(
        self,
        cost: PortfolioImplementationCostInput,
        *,
        visible_at: datetime | None = None,
    ) -> None:
        if not cost.verified:
            return
        self._verify_provenance_pairs(
            cost.source_artifact_ids,
            cost.source_object_hashes,
            label="implementation cost",
            visible_at=visible_at,
        )

    @staticmethod
    def _implementation_cost(
        *,
        nav_fen: int | None,
        current_weights: dict[str, float],
        target_weights: dict[str, float],
        instrument_meta: dict[str, tuple[Market, InstrumentType]],
        costs: dict[str, PortfolioImplementationCostInput],
    ) -> int | None:
        if nav_fen is None:
            return None
        total = Decimal("0")
        for symbol in set(current_weights) | set(target_weights):
            market = instrument_meta[symbol][0]
            instrument_id = f"{market.value}:{symbol}"
            cost = costs.get(instrument_id)
            change = abs(target_weights.get(symbol, 0.0) - current_weights.get(symbol, 0.0))
            if change <= 1e-12:
                continue
            if cost is None or not cost.verified:
                return None
            total += (
                Decimal(nav_fen)
                * Decimal(str(change))
                * Decimal(str(cost.estimated_round_trip_cost_bps))
                / Decimal("10000")
                / Decimal("2")
            )
        return int(total.quantize(Decimal("1")))

    def _source_request(
        self,
        report: PortfolioAnalysisReport,
        model: type[PortfolioAnalysisRequest],
    ) -> PortfolioAnalysisRequest:
        artifact_id = next(
            (
                item
                for item in report.source_artifact_ids
                if item.startswith("PortfolioAnalysisRequest:")
            ),
            None,
        )
        if artifact_id is None:
            raise ValueError("portfolio analysis lacks its frozen request lineage")
        payload, _ = self._load_model(artifact_id, "PortfolioAnalysisRequest", model)
        return payload

    def _persist_receipt(
        self,
        receipt: ExternalTradeImportReceipt,
        input_hashes: list[str],
    ) -> ExternalTradeImportReceipt:
        artifact_id = f"ExternalTradeImportReceipt:{receipt.receipt_id}"
        self._freeze_exact(
            artifact_id,
            "ExternalTradeImportReceipt",
            receipt.schema_version,
            receipt.model_dump(mode="json"),
            input_hashes=input_hashes,
        )
        return receipt

    def _freeze(
        self,
        artifact_type: str,
        schema_version: str,
        payload: dict[str, object],
        *,
        input_hashes: list[str] | None = None,
    ) -> tuple[str, str]:
        identity = sha256_bytes(canonical_json_bytes(payload))
        artifact_id = f"{artifact_type}:{identity}"
        return self._freeze_exact(
            artifact_id,
            artifact_type,
            schema_version,
            payload,
            input_hashes=input_hashes or [],
        )

    def _freeze_exact(
        self,
        artifact_id: str,
        artifact_type: str,
        schema_version: str,
        payload: dict[str, object],
        *,
        input_hashes: list[str],
    ) -> tuple[str, str]:
        ref = self.objects.put_json(payload)
        existing = self.state.artifact_record(artifact_id)
        if existing is not None:
            if (
                str(existing["type"]) != artifact_type
                or str(existing["object_hash"]) != ref.sha256
                or sorted(str(item) for item in existing["input_hashes"]) != sorted(input_hashes)
            ):
                raise ValueError("portfolio-decision artifact identity collision")
            return artifact_id, ref.sha256
        self.state.register_artifact(
            artifact_id=artifact_id,
            artifact_type=artifact_type,
            schema_version=schema_version,
            object_hash=ref.sha256,
            input_hashes=sorted(set(input_hashes)),
        )
        return artifact_id, ref.sha256

    def _load_model(
        self, artifact_id: str, artifact_type: str, model: type[Any]
    ) -> tuple[Any, str]:
        record = self.state.artifact_record(artifact_id)
        if record is None or str(record["type"]) != artifact_type:
            raise ValueError(f"required {artifact_type} artifact is unavailable")
        object_hash = str(record["object_hash"])
        if not self.objects.verify(object_hash):
            raise ValueError(f"required {artifact_type} object is unavailable")
        return model.model_validate_json(self.objects.get_bytes(object_hash)), object_hash

    def _policy_artifact(self) -> tuple[str, str]:
        return self._freeze(
            "PortfolioDecisionPolicy",
            str(self._config["schema_version"]),
            {str(key): value for key, value in self._config.items()},
        )

    @staticmethod
    def _load_config(path: Path) -> dict[str, Any]:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or raw.get("schema_version") != "portfolio-decision-v1":
            raise ValueError("unsupported portfolio decision config")
        return {str(key): value for key, value in raw.items()}

    @staticmethod
    def _reason_code(exc: Exception) -> str:
        text = str(exc).upper()
        return "EXTERNAL_TRADE_CONFLICT:" + "_".join(text.split())[:160]


__all__ = ["PortfolioDecisionService"]
