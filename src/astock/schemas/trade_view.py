"""User-facing trade-plan view derived only from frozen committee artifacts."""

from __future__ import annotations

from typing import Literal

from pydantic import AwareDatetime, Field, model_validator

from astock.schemas.base import AStockModel
from astock.schemas.committee import TradeProtocolOutcome
from astock.schemas.research_runtime import TradingPriceLimitRegime, TradingSpecialRegime


class PriceRangeFen(AStockModel):
    lower_fen: int = Field(ge=0)
    upper_fen: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_range(self) -> PriceRangeFen:
        if self.lower_fen > self.upper_fen:
            raise ValueError("price range lower bound cannot exceed upper bound")
        return self


class TradePlanView(AStockModel):
    schema_version: str = "trade-plan-view-v1"
    view_id: str = Field(min_length=1)
    company_id: str = Field(pattern=r"^\d{6}$")
    as_of: AwareDatetime
    final_outcome: TradeProtocolOutcome
    reference_price_fen: int | None = Field(default=None, gt=0)
    reference_price_source: str | None = None
    reference_price_artifact_id: str | None = None
    expected_return_range: tuple[float, float]
    downside_return_range: tuple[float, float]
    committee_expected_scenario_price_range_fen: PriceRangeFen | None = None
    committee_downside_scenario_price_range_fen: PriceRangeFen | None = None
    confidence: float = Field(ge=0, le=1, allow_inf_nan=False)
    max_position_fraction: float = Field(ge=0, le=1, allow_inf_nan=False)
    entry_rule: str = Field(min_length=1)
    position_size_rule: str = Field(min_length=1)
    price_stop_rule: str = Field(min_length=1)
    volatility_stop_rule: str = Field(min_length=1)
    trailing_stop_rule: str = Field(min_length=1)
    time_stop_rule: str = Field(min_length=1)
    take_profit_rule: str = Field(min_length=1)
    thesis_invalidation_rule: str = Field(min_length=1)
    review_events: list[str] = Field(min_length=1)
    review_at: AwareDatetime
    max_holding_period_days: int = Field(ge=1)
    special_regime: TradingSpecialRegime
    price_limit_regime: TradingPriceLimitRegime
    price_limit_rate_bps: int | None = Field(default=None, ge=1)
    exact_entry_zone_available: Literal[False] = False
    exact_exit_target_available: Literal[False] = False
    scenario_prices_are_targets: Literal[False] = False
    requires_user_confirmation: Literal[True] = True
    paper_ledger_write_allowed: Literal[False] = False
    broker_execution_allowed: Literal[False] = False
    warning_codes: list[str]
    source_artifact_ids: list[str]
    source_object_hashes: list[str]

    @model_validator(mode="after")
    def validate_view(self) -> TradePlanView:
        if self.expected_return_range[0] > self.expected_return_range[1]:
            raise ValueError("expected return range is reversed")
        if self.downside_return_range[0] > self.downside_return_range[1]:
            raise ValueError("downside return range is reversed")
        if (self.reference_price_fen is None) != (self.reference_price_source is None):
            raise ValueError("reference price and source must be paired")
        if self.reference_price_fen is None and (
            self.committee_expected_scenario_price_range_fen is not None
            or self.committee_downside_scenario_price_range_fen is not None
        ):
            raise ValueError("scenario price ranges require a reference price")
        for values in (
            self.review_events,
            self.warning_codes,
            self.source_artifact_ids,
            self.source_object_hashes,
        ):
            if values != sorted(set(values)):
                raise ValueError("trade-plan view list values must be sorted and unique")
        return self


__all__ = ["PriceRangeFen", "TradePlanView"]
