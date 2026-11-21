"""Research-only technical indicator snapshot; no trade or recommendation authority."""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import AwareDatetime, Field, model_validator

from astock.core.hashing import content_hash
from astock.schemas.base import AStockModel
from astock.schemas.market import AdjustmentMode, Frequency


class TechnicalIndicatorSnapshot(AStockModel):
    schema_version: str = "technical-indicator-snapshot-v1"
    snapshot_id: str
    instrument_id: str = Field(min_length=1)
    as_of: AwareDatetime
    latest_session_date: date
    frequency: Literal[Frequency.D1] = Frequency.D1
    adjustment_mode: Literal[AdjustmentMode.NONE] = AdjustmentMode.NONE
    policy_version: str = Field(min_length=1)
    policy_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    context_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_artifact_id: str = Field(min_length=1)
    source_object_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_snapshot_ids: tuple[str, ...] = Field(min_length=1)
    input_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    included_bar_count: int = Field(ge=1, le=2048)

    ema_fast: float | None = Field(default=None, allow_inf_nan=False)
    ema_slow: float | None = Field(default=None, allow_inf_nan=False)
    macd_dif: float | None = Field(default=None, allow_inf_nan=False)
    macd_signal: float | None = Field(default=None, allow_inf_nan=False)
    macd_histogram: float | None = Field(default=None, allow_inf_nan=False)
    rsi: float | None = Field(default=None, ge=0, le=100, allow_inf_nan=False)
    atr: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    bollinger_mid: float | None = Field(default=None, allow_inf_nan=False)
    bollinger_upper: float | None = Field(default=None, allow_inf_nan=False)
    bollinger_lower: float | None = Field(default=None, allow_inf_nan=False)
    volume_ratio: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    breakout_reference_high: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    support_reference_low: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    breakout_state: Literal[
        "INSUFFICIENT_HISTORY",
        "NO_TRADE",
        "NO_BREAKOUT",
        "PRICE_ONLY",
        "VOLUME_CONFIRMED",
        "CORPORATE_ACTION_REVIEW",
    ]
    caution_codes: tuple[str, ...] = ()
    recommendation_allowed: Literal[False] = False
    paper_order_authority_allowed: Literal[False] = False

    @model_validator(mode="after")
    def _cross_field_invariants(self) -> TechnicalIndicatorSnapshot:
        if self.source_snapshot_ids != tuple(sorted(set(self.source_snapshot_ids))):
            raise ValueError("source snapshot IDs must be sorted and unique")
        if self.bollinger_upper is not None and self.bollinger_lower is not None:
            if self.bollinger_upper < self.bollinger_lower:
                raise ValueError("invalid Bollinger bounds")
        if (self.macd_dif is None) != (self.ema_slow is None):
            raise ValueError("MACD DIF must require its slow EMA")
        if self.macd_signal is not None and self.macd_dif is None:
            raise ValueError("MACD signal requires a valid DIF")
        if self.macd_histogram is not None and self.macd_signal is None:
            raise ValueError("MACD histogram requires its signal")
        if self.snapshot_id != "technical:" + content_hash(
            self.model_dump(mode="python", exclude={"snapshot_id", "created_at"})
        ):
            raise ValueError("technical snapshot content hash mismatch")
        return self


__all__ = ["TechnicalIndicatorSnapshot"]
