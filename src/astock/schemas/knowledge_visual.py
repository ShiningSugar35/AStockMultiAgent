"""Contracts for real Zhihu visual completion over frozen semantic runs."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import Field, model_validator

from astock.schemas.base import AStockModel
from astock.schemas.knowledge_completion import ZhihuVisualPacketStatus, ZhihuVisualType

_SHA256_PATTERN = r"^[0-9a-f]{64}$"


class ZhihuVisualInventoryStatus(StrEnum):
    READY_FOR_CAPTURE = "READY_FOR_CAPTURE"
    BLOCKED_SOURCE = "BLOCKED_SOURCE"
    BLOCKED_CONTEXT = "BLOCKED_CONTEXT"
    BLOCKED_ARGUMENT = "BLOCKED_ARGUMENT"


class ZhihuVisualPackStatus(StrEnum):
    READY = "READY"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    NEEDS_INFO = "NEEDS_INFO"


class ZhihuVisualInventoryEntry(AStockModel):
    schema_version: str = "zhihu-visual-inventory-entry-v1"
    placement_id: str = Field(min_length=1)
    author_source_id: str = Field(min_length=1)
    semantic_run_id: str = Field(min_length=1)
    source_item_id: str = Field(min_length=1)
    content_type: str = Field(min_length=1)
    content_id: str = Field(min_length=1)
    source_snapshot_id: str = Field(min_length=1)
    source_snapshot_object_hash: str = Field(pattern=_SHA256_PATTERN)
    image_ordinal: int = Field(ge=1)
    dom_path: str = Field(min_length=1)
    image_url: str = Field(min_length=1)
    image_url_hash: str = Field(pattern=_SHA256_PATTERN)
    placeholder_paragraph_id: str = Field(min_length=1)
    placeholder_ordinal: int = Field(ge=1)
    preceding_paragraph_id: str | None = None
    preceding_paragraph_ordinal: int | None = Field(default=None, ge=1)
    preceding_text_object_hash: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    following_paragraph_id: str | None = None
    following_paragraph_ordinal: int | None = Field(default=None, ge=1)
    following_text_object_hash: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    affected_argument_unit_ids: list[str] = Field(default_factory=list)
    affected_argument_object_hashes: list[str] = Field(default_factory=list)
    status: ZhihuVisualInventoryStatus
    reason_codes: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_entry(self) -> ZhihuVisualInventoryEntry:
        if self.affected_argument_unit_ids != sorted(set(self.affected_argument_unit_ids)):
            raise ValueError("visual inventory affected AU ids must be sorted and unique")
        if len(self.affected_argument_object_hashes) != len(
            set(self.affected_argument_object_hashes)
        ):
            raise ValueError("visual inventory affected AU hashes must be unique")
        if len(self.affected_argument_unit_ids) != len(self.affected_argument_object_hashes):
            raise ValueError("visual inventory affected AU ids/hashes must align")
        if self.reason_codes != sorted(set(self.reason_codes)):
            raise ValueError("visual inventory reason codes must be sorted and unique")
        if self.status is ZhihuVisualInventoryStatus.READY_FOR_CAPTURE:
            if self.reason_codes:
                raise ValueError("ready visual inventory entry cannot carry reason codes")
            if (
                self.preceding_paragraph_id is None
                or self.preceding_paragraph_ordinal is None
                or self.preceding_text_object_hash is None
                or self.following_paragraph_id is None
                or self.following_paragraph_ordinal is None
                or self.following_text_object_hash is None
                or not self.affected_argument_unit_ids
            ):
                raise ValueError(
                    "ready visual inventory entry requires both context sides and AU lineage"
                )
        elif not self.reason_codes:
            raise ValueError("blocked visual inventory entry requires reason codes")
        return self


class ZhihuVisualInventoryManifest(AStockModel):
    schema_version: str = "zhihu-visual-inventory-manifest-v1"
    manifest_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    author_source_id: str = Field(min_length=1)
    semantic_run_id: str = Field(min_length=1)
    semantic_pipeline_version: str = Field(min_length=1)
    source_content_count: int = Field(ge=0)
    image_reference_count: int = Field(ge=0)
    ready_for_capture_count: int = Field(ge=0)
    blocked_count: int = Field(ge=0)
    entries: list[ZhihuVisualInventoryEntry]
    formal_committee_weight_allowed: Literal[False] = False

    @model_validator(mode="after")
    def validate_manifest(self) -> ZhihuVisualInventoryManifest:
        if self.image_reference_count != len(self.entries):
            raise ValueError("visual inventory count does not match entries")
        ready = sum(
            item.status is ZhihuVisualInventoryStatus.READY_FOR_CAPTURE for item in self.entries
        )
        if ready != self.ready_for_capture_count:
            raise ValueError("visual inventory ready count drift")
        if self.blocked_count != self.image_reference_count - ready:
            raise ValueError("visual inventory blocked count drift")
        placement_ids = [item.placement_id for item in self.entries]
        if len(placement_ids) != len(set(placement_ids)):
            raise ValueError("visual inventory placement ids must be unique")
        return self


class ZhihuVisualPacketReference(AStockModel):
    placement_id: str = Field(min_length=1)
    packet_artifact_id: str = Field(min_length=1)
    packet_object_hash: str = Field(pattern=_SHA256_PATTERN)
    image_object_hash: str = Field(pattern=_SHA256_PATTERN)
    packet_status: ZhihuVisualPacketStatus
    visual_type: ZhihuVisualType
    ocr_status: str = Field(min_length=1)


class VisualEvidencePack(AStockModel):
    schema_version: str = "visual-evidence-pack-v1"
    pack_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    author_source_id: str = Field(min_length=1)
    semantic_run_id: str = Field(min_length=1)
    inventory_artifact_id: str = Field(min_length=1)
    inventory_object_hash: str = Field(pattern=_SHA256_PATTERN)
    source_snapshot_ids: list[str]
    source_snapshot_object_hashes: list[str] = Field(default_factory=list)
    image_reference_count: int = Field(ge=0)
    placement_count: int = Field(ge=0)
    unique_asset_count: int = Field(ge=0)
    ready_count: int = Field(ge=0)
    needs_review_count: int = Field(ge=0)
    blocked_count: int = Field(ge=0)
    status: ZhihuVisualPackStatus
    reason_counts: dict[str, int] = Field(default_factory=dict)
    packet_references: list[ZhihuVisualPacketReference]
    formal_committee_weight_allowed: Literal[False] = False

    @model_validator(mode="after")
    def validate_pack(self) -> VisualEvidencePack:
        if self.source_snapshot_ids != sorted(set(self.source_snapshot_ids)):
            raise ValueError("visual pack source snapshot ids must be sorted and unique")
        if self.source_snapshot_object_hashes != sorted(set(self.source_snapshot_object_hashes)):
            raise ValueError("visual pack source snapshot hashes must be sorted and unique")
        if self.placement_count != len(self.packet_references):
            raise ValueError("visual pack placement count does not match packet references")
        ready = sum(
            item.packet_status is ZhihuVisualPacketStatus.READY for item in self.packet_references
        )
        review = self.placement_count - ready
        if ready != self.ready_count or review != self.needs_review_count:
            raise ValueError("visual pack packet status count drift")
        if self.image_reference_count != self.placement_count + self.blocked_count:
            raise ValueError("visual pack inventory accounting does not balance")
        if self.status is ZhihuVisualPackStatus.READY and (
            self.blocked_count or self.needs_review_count
        ):
            raise ValueError("ready visual pack cannot contain unresolved placements")
        if self.status is ZhihuVisualPackStatus.NEEDS_INFO and self.blocked_count == 0:
            raise ValueError("NEEDS_INFO visual pack requires blocked inventory")
        if (
            self.status is ZhihuVisualPackStatus.NEEDS_REVIEW
            and (self.blocked_count or self.needs_review_count == 0)
        ):
            raise ValueError("NEEDS_REVIEW visual pack requires only reviewable packets")
        return self


class ZhihuVisualPipelineReport(AStockModel):
    schema_version: str = "zhihu-visual-pipeline-report-v1"
    run_id: str = Field(min_length=1)
    author_source_id: str = Field(min_length=1)
    semantic_run_id: str = Field(min_length=1)
    inventory_artifact_id: str = Field(min_length=1)
    inventory_object_hash: str = Field(pattern=_SHA256_PATTERN)
    processed_count: int = Field(ge=0)
    captured_count: int = Field(ge=0)
    skipped_existing_count: int = Field(ge=0)
    blocked_fetch_count: int = Field(ge=0)
    pack_artifact_id: str | None = None
    pack_object_hash: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    pack_status: ZhihuVisualPackStatus | None = None
    next_index: int = Field(ge=0)
    complete: bool
    formal_committee_weight_allowed: Literal[False] = False
