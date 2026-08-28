"""Point-in-time availability and source-revision contracts."""

from __future__ import annotations

from datetime import date
from enum import StrEnum

from pydantic import AwareDatetime, model_validator

from astock.schemas.base import AStockModel


class PointInTimeStatus(StrEnum):
    CERTIFIED = "CERTIFIED"
    DOCUMENT_RECONSTRUCTED = "DOCUMENT_RECONSTRUCTED"
    APPROXIMATED = "APPROXIMATED"
    NOT_PIT_SAFE = "NOT_PIT_SAFE"


class AvailabilityBasis(StrEnum):
    OFFICIAL_PUBLICATION_TIMESTAMP = "OFFICIAL_PUBLICATION_TIMESTAMP"
    FETCH_OBSERVED = "FETCH_OBSERVED"
    USER_DECLARED = "USER_DECLARED"
    PROVIDER_CURRENT_VALUE = "PROVIDER_CURRENT_VALUE"


class PointInTimeMetadata(AStockModel):
    pit_id: str
    source_id: str
    source_document_id: str | None = None
    source_snapshot_id: str | None = None
    period_end: date | None = None
    published_at: AwareDatetime | None = None
    effective_at: AwareDatetime | None = None
    ingested_at: AwareDatetime
    available_to_system_at: AwareDatetime
    revised_at: AwareDatetime | None = None
    supersedes_source_id: str | None = None
    point_in_time_status: PointInTimeStatus
    availability_basis: AvailabilityBasis

    @model_validator(mode="after")
    def validate_timeline_and_lineage(self) -> PointInTimeMetadata:
        if self.ingested_at < self.available_to_system_at:
            raise ValueError("ingested_at must not precede available_to_system_at")
        if self.published_at is not None and self.available_to_system_at < self.published_at:
            raise ValueError("available_to_system_at must not precede published_at")
        if (
            self.revised_at is not None
            and self.published_at is not None
            and self.revised_at < self.published_at
        ):
            raise ValueError("revised_at must not precede published_at")
        if self.supersedes_source_id == self.source_id:
            raise ValueError("a PIT source cannot supersede itself")
        if self.point_in_time_status is PointInTimeStatus.DOCUMENT_RECONSTRUCTED:
            if self.source_document_id is None or self.source_snapshot_id is None:
                raise ValueError("DOCUMENT_RECONSTRUCTED requires document and snapshot lineage")
            if self.published_at is None:
                raise ValueError("DOCUMENT_RECONSTRUCTED requires published_at")
        if self.point_in_time_status is PointInTimeStatus.CERTIFIED:
            if (
                self.availability_basis is AvailabilityBasis.OFFICIAL_PUBLICATION_TIMESTAMP
                and self.published_at is None
            ):
                raise ValueError("publication-certified PIT requires published_at")
            if (
                self.availability_basis is AvailabilityBasis.FETCH_OBSERVED
                and self.source_snapshot_id is None
            ):
                raise ValueError("fetch-certified PIT requires source_snapshot_id")
            if self.availability_basis not in {
                AvailabilityBasis.OFFICIAL_PUBLICATION_TIMESTAMP,
                AvailabilityBasis.FETCH_OBSERVED,
            }:
                raise ValueError("CERTIFIED PIT requires an auditable availability basis")
        return self
