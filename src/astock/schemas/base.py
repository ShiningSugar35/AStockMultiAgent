"""Shared strict Pydantic model configuration."""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field


class AStockModel(BaseModel):
    """Base for durable public schemas."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, validate_assignment=True)

    schema_version: str = Field(default="1.0", min_length=1)
    created_at: AwareDatetime = Field(default_factory=lambda: datetime.now(UTC))
