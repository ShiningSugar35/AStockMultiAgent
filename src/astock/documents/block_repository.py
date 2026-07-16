"""Durable metadata index for stable text blocks in reflowable documents."""

from __future__ import annotations

from astock.core.hashing import canonical_json_bytes, content_hash
from astock.core.state import StateStore, utc_now_text
from astock.schemas import DocumentBlock


class DocumentBlockRepository:
    def __init__(self, state: StateStore) -> None:
        self.state = state

    def get_block(
        self,
        snapshot_id: str,
        block_index: int,
        parser_version: str,
    ) -> DocumentBlock | None:
        with self.state.connect() as connection:
            row = connection.execute(
                "SELECT block_json FROM document_block WHERE snapshot_id=? AND block_index=? "
                "AND parser_version=?",
                (snapshot_id, block_index, parser_version),
            ).fetchone()
        return DocumentBlock.model_validate_json(row["block_json"]) if row else None

    def get_block_by_id(self, block_id: str) -> DocumentBlock | None:
        with self.state.connect() as connection:
            row = connection.execute(
                "SELECT block_json FROM document_block WHERE block_id=?", (block_id,)
            ).fetchone()
        return DocumentBlock.model_validate_json(row["block_json"]) if row else None

    def register_blocks(self, blocks: list[DocumentBlock]) -> None:
        with self.state.transaction() as connection:
            for block in blocks:
                block_json = canonical_json_bytes(block.model_dump(mode="json")).decode("utf-8")
                manifest_hash = content_hash(block)
                existing = connection.execute(
                    "SELECT block_manifest_hash FROM document_block WHERE snapshot_id=? "
                    "AND block_index=? AND parser_version=?",
                    (block.snapshot_id, block.block_index, block.parser_version),
                ).fetchone()
                if existing is not None:
                    if existing["block_manifest_hash"] != manifest_hash:
                        raise ValueError(
                            "Document block collision: "
                            f"{block.snapshot_id}:{block.block_index}:{block.parser_version}"
                        )
                    continue
                connection.execute(
                    "INSERT INTO document_block(block_id,document_id,snapshot_id,block_index,"
                    "part_kind,block_kind,parser_version,text_object_hash,text_sha256,"
                    "text_char_count,metadata_object_hash,block_manifest_hash,block_json,"
                    "created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        block.block_id,
                        block.document_id,
                        block.snapshot_id,
                        block.block_index,
                        block.part_kind.value,
                        block.block_kind.value,
                        block.parser_version,
                        block.text_object_sha256,
                        block.text_sha256,
                        block.text_char_count,
                        block.metadata_object_sha256,
                        manifest_hash,
                        block_json,
                        utc_now_text(),
                    ),
                )

    def blocks_for(
        self,
        snapshot_id: str,
        parser_version: str,
    ) -> list[DocumentBlock]:
        with self.state.connect() as connection:
            rows = connection.execute(
                "SELECT block_json FROM document_block WHERE snapshot_id=? AND parser_version=? "
                "ORDER BY block_index",
                (snapshot_id, parser_version),
            ).fetchall()
        return [DocumentBlock.model_validate_json(row["block_json"]) for row in rows]
