"""Source snapshot contracts shared by market and knowledge ingestion."""

from __future__ import annotations

from enum import StrEnum

from pydantic import AwareDatetime, Field

from astock.schemas.base import AStockModel


class FetchStatus(StrEnum):
    SUCCEEDED = "SUCCEEDED"
    FETCH_FAILED = "FETCH_FAILED"
    ACCESS_RESTRICTED = "ACCESS_RESTRICTED"
    PARTIAL = "PARTIAL"


class SourceSnapshot(AStockModel):
    snapshot_id: str
    source_id: str
    object_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    fetched_at: AwareDatetime
    available_to_system_at: AwareDatetime
    source_url: str | None = None
    mime: str
    byte_size: int = Field(ge=0)
    headers_hash: str | None = None
    fetch_status: FetchStatus = FetchStatus.SUCCEEDED
    rights_status: str = "LOCAL_RESEARCH"
