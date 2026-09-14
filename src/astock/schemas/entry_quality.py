"""Deterministic price-location and entry-quality research contracts."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Literal

from pydantic import AwareDatetime, Field, field_validator, model_validator

from astock.schemas.base import AStockModel

_SHA256 = r"^[0-9a-f]{64}$"


class EntryQualityState(StrEnum):
    ATTRACTIVE_DISLOCATION = "ATTRACTIVE_DISLOCATION"
    BASE_BUILDING = "BASE_BUILDING"
    TREND_CONFIRMED = "TREND_CONFIRMED"
    FALLING_KNIFE_RISK = "FALLING_KNIFE_RISK"
    EXTENDED = "EXTENDED"
    NEUTRAL = "NEUTRAL"
    INSUFFICIENT_HISTORY = "INSUFFICIENT_HISTORY"


class EntryQualityWindow(AStockModel):
    window_days: int = Field(ge=2, le=500)
    observations: int = Field(ge=2, le=500)
    range_position: float = Field(ge=0, le=1, allow_inf_nan=False)
    return_ratio: Decimal
    drawdown_from_high: Decimal = Field(le=0)
    realized_volatility_annualized: float = Field(ge=0, allow_inf_nan=False)


class EntryQualitySnapshot(AStockModel):
    schema_version: str = "entry-quality-snapshot-v1"
    entry_quality_id: str = Field(min_length=1)
    instrument_id: str = Field(min_length=1)
    as_of: AwareDatetime
    latest_session_date: date
    current_price: Decimal = Field(gt=0)
    state: EntryQualityState
    score: float = Field(ge=0, le=1, allow_inf_nan=False)
    windows: list[EntryQualityWindow] = Field(min_length=1)
    moving_average_distance: dict[str, Decimal]
    trend_alignment_score: float = Field(ge=0, le=1, allow_inf_nan=False)
    volume_ratio_5d_to_20d: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    amount_ratio_5d_to_20d: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    relative_strength_60d: Decimal | None = None
    benchmark_source_artifact_id: str | None = None
    benchmark_source_object_hash: str | None = Field(default=None, pattern=_SHA256)
    falling_knife_risk: bool
    extended_risk: bool
    reason_codes: list[str]
    source_artifact_id: str = Field(min_length=1)
    source_object_hash: str = Field(pattern=_SHA256)
    price_source_artifact_id: str | None = Field(default=None, min_length=1)
    price_source_object_hash: str | None = Field(default=None, pattern=_SHA256)
    source_snapshot_ids: list[str]
    recommendation_allowed: Literal[False] = False
    portfolio_weight_authority_allowed: Literal[False] = False

    @field_validator("reason_codes", "source_snapshot_ids")
    @classmethod
    def sorted_unique(cls, value: list[str]) -> list[str]:
        if value != sorted(set(value)):
            raise ValueError("entry-quality list fields must be sorted and unique")
        return value

    @model_validator(mode="after")
    def validate_snapshot(self) -> EntryQualitySnapshot:
        if [item.window_days for item in self.windows] != sorted(
            {item.window_days for item in self.windows}
        ):
            raise ValueError("entry-quality windows must be sorted and unique")
        if self.latest_session_date > self.as_of.date():
            raise ValueError("entry-quality session cannot postdate as_of")
        if not self.source_snapshot_ids:
            raise ValueError("entry-quality requires source snapshot lineage")
        price_fields = (
            self.price_source_artifact_id,
            self.price_source_object_hash,
        )
        if any(value is not None for value in price_fields) and not all(
            value is not None for value in price_fields
        ):
            raise ValueError("entry-quality price lineage must be complete or absent")
        benchmark_fields = (
            self.benchmark_source_artifact_id,
            self.benchmark_source_object_hash,
        )
        if any(value is not None for value in benchmark_fields) and not all(
            value is not None for value in benchmark_fields
        ):
            raise ValueError("entry-quality benchmark lineage must be complete or absent")
        if self.relative_strength_60d is not None and not all(benchmark_fields):
            raise ValueError("entry-quality relative strength requires benchmark lineage")
        return self


__all__ = ["EntryQualitySnapshot", "EntryQualityState", "EntryQualityWindow"]
