"""Readable trade-plan projection from frozen committee and classification artifacts."""

from __future__ import annotations

from datetime import datetime

from astock.core.hashing import content_hash
from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.market_data.reference import MarketReferenceService
from astock.paper_trading.operation import MarketReferencePaperVerifier
from astock.schemas.committee import DecisionPack, TradeProtocol
from astock.schemas.research_runtime import ClassifiedTradeProtocol, TradingClassificationRelease
from astock.schemas.trade_view import PriceRangeFen, TradePlanView


class TradePlanViewService:
    """Project final research artifacts into a concise, non-authorizing user view."""

    def __init__(
        self,
        state: StateStore,
        objects: ObjectStore,
        reference: MarketReferenceService,
    ) -> None:
        self.state = state
        self.objects = objects
        self.reference = reference
        self.verifier = MarketReferencePaperVerifier(reference)

    def build(
        self,
        classified_protocol_artifact_id: str,
        *,
        reference_price_fen: int | None = None,
        reference_price_source: str | None = None,
    ) -> TradePlanView:
        protocol, protocol_hash = self._load(
            classified_protocol_artifact_id,
            "ClassifiedTradeProtocol",
            ClassifiedTradeProtocol,
        )
        decision, decision_hash = self._load(
            protocol.decision_pack_artifact_id,
            "DecisionPack",
            DecisionPack,
        )
        committee, committee_hash = self._load(
            protocol.committee_protocol_artifact_id,
            "TradeProtocol",
            TradeProtocol,
        )
        classification, classification_hash = self._load(
            protocol.trading_classification_artifact_id,
            "TradingClassificationRelease",
            TradingClassificationRelease,
        )
        if (
            decision.company_id != protocol.company_id
            or committee.company_id != protocol.company_id
            or classification.company_id != protocol.company_id
            or decision.decision_id != committee.decision_id
            or decision.decision_sha256 != committee.decision_sha256
        ):
            raise ValueError("trade-plan frozen lineage identity mismatch")
        if (
            protocol.decision_pack_object_hash != decision_hash
            or protocol.committee_protocol_object_hash != committee_hash
            or protocol.trading_classification_object_hash != classification_hash
        ):
            raise ValueError("trade-plan frozen lineage object hash mismatch")

        warnings: set[str] = {
            "COMMITTEE_PRICE_RANGES_ARE_SCENARIOS_NOT_TARGETS",
            "EXACT_ENTRY_AND_EXIT_PRICES_REQUIRE_STRUCTURED_PRICE_EVIDENCE",
        }
        price_artifact: str | None = None
        price_hash: str | None = None
        price_source = reference_price_source
        if reference_price_fen is not None:
            if reference_price_fen <= 0:
                raise ValueError("reference price must be positive")
            price_source = price_source or "USER_SUPPLIED_REFERENCE_PRICE"
            warnings.add("USER_SUPPLIED_REFERENCE_PRICE_NOT_MARKET_VERIFIED")
        else:
            try:
                bars, release_id = self.verifier.visible_daily_history(
                    classification.market,
                    classification.symbol,
                    visible_at=protocol.as_of,
                )
                visible = [
                    item
                    for item in bars
                    if item.available_to_system_at <= protocol.as_of
                    and item.session_close_at <= protocol.as_of
                ]
                if visible:
                    latest = max(visible, key=lambda item: item.session_date)
                    reference_price_fen = int(round(float(latest.close) * 100))
                    price_source = "LATEST_PIT_DAILY_CLOSE"
                    price_artifact = f"market-reference:{release_id}"
                    record = self.state.artifact_record(price_artifact)
                    if record is not None:
                        price_hash = str(record["object_hash"])
                    warnings.add("REFERENCE_PRICE_IS_LATEST_PIT_DAILY_CLOSE")
            except (OSError, ValueError):
                warnings.add("REFERENCE_PRICE_UNAVAILABLE")

        expected = (
            float(decision.expected_return_range.lower),
            float(decision.expected_return_range.upper),
        )
        downside = (
            float(decision.downside_range.lower),
            float(decision.downside_range.upper),
        )
        expected_prices = self._scenario_range(
            reference_price_fen,
            expected,
            created_at=protocol.as_of,
        )
        downside_prices = self._scenario_range(
            reference_price_fen,
            downside,
            created_at=protocol.as_of,
        )
        source_ids: list[str] = [
            classified_protocol_artifact_id,
            protocol.decision_pack_artifact_id,
            protocol.committee_protocol_artifact_id,
            protocol.trading_classification_artifact_id,
        ]
        if price_artifact is not None:
            source_ids.append(price_artifact)
        source_ids = sorted(set(source_ids))
        source_hashes: list[str] = [
            protocol_hash,
            decision_hash,
            committee_hash,
            classification_hash,
        ]
        if price_hash is not None:
            source_hashes.append(price_hash)
        source_hashes = sorted(set(source_hashes))
        seed = {
            "protocol_hash": protocol_hash,
            "decision_hash": decision_hash,
            "committee_hash": committee_hash,
            "classification_hash": classification_hash,
            "reference_price_fen": reference_price_fen,
            "reference_price_source": price_source,
        }
        view = TradePlanView(
            view_id="trade-plan-view:" + content_hash(seed),
            company_id=protocol.company_id,
            as_of=protocol.as_of,
            final_outcome=protocol.final_outcome,
            reference_price_fen=reference_price_fen,
            reference_price_source=price_source,
            reference_price_artifact_id=price_artifact,
            expected_return_range=expected,
            downside_return_range=downside,
            committee_expected_scenario_price_range_fen=expected_prices,
            committee_downside_scenario_price_range_fen=downside_prices,
            confidence=float(decision.confidence),
            max_position_fraction=float(decision.max_position),
            entry_rule=committee.entry_rule,
            position_size_rule=committee.position_size_rule,
            price_stop_rule=committee.price_stop_rule,
            volatility_stop_rule=committee.volatility_stop_rule,
            trailing_stop_rule=committee.trailing_stop_rule,
            time_stop_rule=committee.time_stop_rule,
            take_profit_rule=committee.take_profit_rule,
            thesis_invalidation_rule=committee.thesis_invalidation_rule,
            review_events=committee.review_events,
            review_at=decision.review_at,
            max_holding_period_days=committee.max_holding_period_days,
            special_regime=classification.special_regime,
            price_limit_regime=classification.price_limit_regime,
            price_limit_rate_bps=classification.price_limit_rate_bps,
            warning_codes=sorted(warnings),
            source_artifact_ids=source_ids,
            source_object_hashes=source_hashes,
            created_at=protocol.as_of,
        )
        object_ref = self.objects.put_json(view.model_dump(mode="json"))
        artifact_id = f"TradePlanView:{view.view_id}"
        existing = self.state.artifact_record(artifact_id)
        if existing is not None:
            if (
                str(existing["type"]) != "TradePlanView"
                or str(existing["object_hash"]) != object_ref.sha256
                or sorted(existing["input_hashes"]) != source_hashes
            ):
                raise ValueError("trade-plan view identity collision")
        else:
            self.state.register_artifact(
                artifact_id=artifact_id,
                artifact_type="TradePlanView",
                schema_version=view.schema_version,
                object_hash=object_ref.sha256,
                input_hashes=source_hashes,
            )
        self.state.set_checkpoint(
            scope_type="trade-plan-view",
            scope_key=protocol.company_id,
            cursor={"artifact_id": artifact_id, "view_id": view.view_id},
            status="READY",
            object_hash=object_ref.sha256,
        )
        return view

    @staticmethod
    def _scenario_range(
        reference_price_fen: int | None,
        returns: tuple[float, float],
        *,
        created_at: datetime,
    ) -> PriceRangeFen | None:
        if reference_price_fen is None:
            return None
        values = [max(0, int(round(reference_price_fen * (1.0 + value)))) for value in returns]
        return PriceRangeFen(
            lower_fen=min(values),
            upper_fen=max(values),
            created_at=created_at,
        )

    def _load(self, artifact_id: str, expected_type: str, model_type):
        record = self.state.artifact_record(artifact_id)
        if record is None or str(record["type"]) != expected_type:
            raise ValueError(f"unknown {expected_type} artifact")
        object_hash = str(record["object_hash"])
        if not self.objects.verify(object_hash):
            raise ValueError(f"{expected_type} object is unavailable")
        return model_type.model_validate_json(self.objects.get_bytes(object_hash)), object_hash


__all__ = ["TradePlanViewService"]
