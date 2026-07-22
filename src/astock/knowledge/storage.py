"""Immutable Parquet index for normalized knowledge metadata, never full bodies."""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import quote
from uuid import uuid4

import pyarrow as pa
import pyarrow.parquet as pq

from astock.schemas import ZhihuCommentNode, ZhihuContentRecord

_KNOWLEDGE_SCHEMA = pa.schema(
    [
        ("version_id", pa.string()),
        ("author_source_id", pa.string()),
        ("platform_author_id", pa.string()),
        ("content_id", pa.string()),
        ("content_type", pa.string()),
        ("canonical_url", pa.string()),
        ("title", pa.string()),
        ("question_id", pa.string()),
        ("question_title", pa.string()),
        ("published_at", pa.timestamp("us", tz="UTC")),
        ("updated_at", pa.timestamp("us", tz="UTC")),
        ("collected_at", pa.timestamp("us", tz="UTC")),
        ("body_object_sha256", pa.string()),
        ("metadata_sha256", pa.string()),
        ("raw_source_snapshot_id", pa.string()),
        ("content_completeness", pa.string()),
        ("previous_version_id", pa.string()),
    ]
)

_COMMENT_SCHEMA = pa.schema(
    [
        ("version_id", pa.string()),
        ("author_source_id", pa.string()),
        ("content_type", pa.string()),
        ("content_id", pa.string()),
        ("comment_id", pa.string()),
        ("platform_author_id", pa.string()),
        ("author_url_token", pa.string()),
        ("parent_comment_id", pa.string()),
        ("reply_to_comment_id", pa.string()),
        ("root_comment_id", pa.string()),
        ("published_at", pa.timestamp("us", tz="UTC")),
        ("updated_at", pa.timestamp("us", tz="UTC")),
        ("collected_at", pa.timestamp("us", tz="UTC")),
        ("like_count", pa.int64()),
        ("child_comment_count", pa.int64()),
        ("is_target_author", pa.bool_()),
        ("body_object_sha256", pa.string()),
        ("metadata_sha256", pa.string()),
        ("raw_source_snapshot_id", pa.string()),
        ("previous_version_id", pa.string()),
    ]
)


class ParquetKnowledgeStore:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    def write(self, record: ZhihuContentRecord) -> Path:
        year = record.published_at.year if record.published_at else record.collected_at.year
        path = (
            self.root
            / "knowledge_content"
            / f"author={_partition(record.author_source_id)}"
            / f"content_type={_partition(record.content_type.value)}"
            / f"year={year}"
            / f"{_partition(record.version_id)}.parquet"
        )
        if path.exists():
            return path
        path.parent.mkdir(parents=True, exist_ok=True)
        table = pa.Table.from_pylist(
            [
                {
                    "version_id": record.version_id,
                    "author_source_id": record.author_source_id,
                    "platform_author_id": record.platform_author_id,
                    "content_id": record.content_id,
                    "content_type": record.content_type.value,
                    "canonical_url": record.canonical_url,
                    "title": record.title,
                    "question_id": record.question_id,
                    "question_title": record.question_title,
                    "published_at": record.published_at,
                    "updated_at": record.updated_at,
                    "collected_at": record.collected_at,
                    "body_object_sha256": record.body_object_sha256,
                    "metadata_sha256": record.metadata_sha256,
                    "raw_source_snapshot_id": record.raw_source_snapshot_id,
                    "content_completeness": record.content_completeness.value,
                    "previous_version_id": record.previous_version_id,
                }
            ],
            schema=_KNOWLEDGE_SCHEMA,
        )
        temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        try:
            pq.write_table(table, temporary, compression="zstd")
            with temporary.open("rb+") as handle:
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
        return path

    def write_comment(self, record: ZhihuCommentNode) -> Path:
        year = record.published_at.year if record.published_at else record.collected_at.year
        path = (
            self.root
            / "knowledge_comments"
            / f"author={_partition(record.author_source_id)}"
            / f"content_type={_partition(record.content_type.value)}"
            / f"year={year}"
            / f"{_partition(record.version_id)}.parquet"
        )
        if path.exists():
            return path
        path.parent.mkdir(parents=True, exist_ok=True)
        table = pa.Table.from_pylist(
            [
                {
                    "version_id": record.version_id,
                    "author_source_id": record.author_source_id,
                    "content_type": record.content_type.value,
                    "content_id": record.content_id,
                    "comment_id": record.comment_id,
                    "platform_author_id": record.platform_author_id,
                    "author_url_token": record.author_url_token,
                    "parent_comment_id": record.parent_comment_id,
                    "reply_to_comment_id": record.reply_to_comment_id,
                    "root_comment_id": record.root_comment_id,
                    "published_at": record.published_at,
                    "updated_at": record.updated_at,
                    "collected_at": record.collected_at,
                    "like_count": record.like_count,
                    "child_comment_count": record.child_comment_count,
                    "is_target_author": record.is_target_author,
                    "body_object_sha256": record.body_object_sha256,
                    "metadata_sha256": record.metadata_sha256,
                    "raw_source_snapshot_id": record.raw_source_snapshot_id,
                    "previous_version_id": record.previous_version_id,
                }
            ],
            schema=_COMMENT_SCHEMA,
        )
        temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        try:
            pq.write_table(table, temporary, compression="zstd")
            with temporary.open("rb+") as handle:
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
        return path


def _partition(value: str) -> str:
    return quote(value, safe="-_.")


__all__ = ["ParquetKnowledgeStore"]
