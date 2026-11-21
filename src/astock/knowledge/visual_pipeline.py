"""Offline visual inventory from preserved immutable source snapshots."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from html.parser import HTMLParser

from astock.core.errors import StorageError
from astock.core.hashing import canonical_json_bytes, sha256_bytes
from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.knowledge.archived_html import _ZhihuArticleHtmlParser
from astock.knowledge.visual_repository import ZhihuVisualRepository
from astock.schemas.knowledge_completion import (
    validate_zhimg_url,
)
from astock.schemas.knowledge_visual import (
    ZhihuVisualInventoryEntry,
    ZhihuVisualInventoryManifest,
    ZhihuVisualInventoryStatus,
)

_SEMANTIC_PIPELINE_VERSION = "knowledge-semantic-funnel-three-view-v3"
_VISUAL_PIPELINE_VERSION = "zhihu-visual-completion-v1"
_PLACEHOLDER = "[图片]"
_PLACEHOLDER_OBJECT_HASH = sha256_bytes(_PLACEHOLDER.encode("utf-8"))


@dataclass(frozen=True, slots=True)
class _ImageReference:
    url: str
    alt: str


class _ContentImageParser(HTMLParser):
    """Enumerate one logical image per visible image block in natural DOM order."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.images: list[_ImageReference] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "img":
            return
        values = {key.lower(): value for key, value in attrs}
        classes = set((values.get("class") or "").split())
        # Zhihu commonly emits an origin image and one lazy duplicate. The v3 paragraphizer
        # produced one [图片] block from the non-lazy image, so retain the same logical view.
        if "lazy" in classes:
            return
        source = (
            values.get("data-actualsrc")
            or values.get("data-original")
            or values.get("data-original-src")
            or values.get("src")
        )
        if not source:
            return
        try:
            validate_zhimg_url(source)
        except ValueError:
            return
        self.images.append(_ImageReference(source, values.get("alt") or ""))


class ZhihuVisualPipelineService:
    """Turn frozen Zhihu HTML image locators into immutable visual evidence packs."""

    def __init__(
        self,
        state: StateStore,
        objects: ObjectStore,
        *,
        repository: ZhihuVisualRepository | None = None,
    ) -> None:
        self.state = state
        self.objects = objects
        self.repository = repository or ZhihuVisualRepository(state)

    def plan(self, author_source_id: str) -> ZhihuVisualInventoryManifest:
        semantic = self.repository.select_semantic_run(
            author_source_id,
            pipeline_version=_SEMANTIC_PIPELINE_VERSION,
        )
        semantic_run_id = str(semantic["run_id"])
        anchor = datetime.fromisoformat(str(semantic["started_at"]).replace("Z", "+00:00"))
        items = self.repository.semantic_items(semantic_run_id)
        paragraphs_by_item = self.repository.paragraphs_by_item(semantic_run_id)
        bindings_by_paragraph = self.repository.argument_bindings_by_paragraph(semantic_run_id)
        entries: list[ZhihuVisualInventoryEntry] = []
        argument_object_cache: dict[str, str] = {}
        for item in items:
            entries.extend(
                self._inventory_item(
                    item,
                    semantic_run_id,
                    anchor,
                    argument_object_cache,
                    paragraphs_by_item.get(str(item["item_id"]), []),
                    bindings_by_paragraph,
                )
            )
        payload = {
            "pipeline_version": _VISUAL_PIPELINE_VERSION,
            "author_source_id": author_source_id,
            "semantic_run_id": semantic_run_id,
            "entries": [item.model_dump(mode="json", exclude={"created_at"}) for item in entries],
        }
        identity = sha256_bytes(canonical_json_bytes(payload))
        run_id = f"zhihu-visual-run:{identity}"
        manifest = ZhihuVisualInventoryManifest(
            manifest_id=f"zhihu-visual-inventory:{identity}",
            run_id=run_id,
            author_source_id=author_source_id,
            semantic_run_id=semantic_run_id,
            semantic_pipeline_version=_SEMANTIC_PIPELINE_VERSION,
            source_content_count=len(items),
            image_reference_count=len(entries),
            ready_for_capture_count=sum(
                item.status is ZhihuVisualInventoryStatus.READY_FOR_CAPTURE for item in entries
            ),
            blocked_count=sum(
                item.status is not ZhihuVisualInventoryStatus.READY_FOR_CAPTURE for item in entries
            ),
            entries=entries,
            created_at=anchor,
        )
        object_ref = self.objects.put_json(manifest.model_dump(mode="json"))
        artifact_id = f"ZhihuVisualInventoryManifest:{manifest.manifest_id}"
        source_hashes = sorted({item.source_snapshot_object_hash for item in entries})
        self._register_exact(
            artifact_id=artifact_id,
            artifact_type="ZhihuVisualInventoryManifest",
            schema_version=manifest.schema_version,
            object_hash=object_ref.sha256,
            input_hashes=source_hashes,
        )
        if self.state.get_checkpoint("knowledge-zhihu-visual-run", run_id) is None:
            self.state.set_checkpoint(
                scope_type="knowledge-zhihu-visual-run",
                scope_key=run_id,
                cursor={
                    "author_source_id": author_source_id,
                    "semantic_run_id": semantic_run_id,
                    "inventory_artifact_id": artifact_id,
                    "inventory_object_hash": object_ref.sha256,
                    "next_index": 0,
                    "pack_artifact_id": None,
                    "pack_status": None,
                },
                status="PLANNED",
                object_hash=object_ref.sha256,
            )
        return manifest

    def _inventory_item(
        self,
        item: dict[str, object],
        semantic_run_id: str,
        anchor: datetime,
        argument_object_cache: dict[str, str],
        paragraphs: list[dict[str, object]],
        bindings_by_paragraph: dict[str, list[dict[str, object]]],
    ) -> list[ZhihuVisualInventoryEntry]:
        source_snapshot_id = str(item["source_snapshot_id"])
        source_hash = str(item["source_snapshot_object_hash"])
        try:
            raw_source = self.objects.get_bytes(source_hash)
        except StorageError as exc:
            raise ValueError(f"VISUAL_SOURCE_SNAPSHOT_OBJECT_MISSING:{source_snapshot_id}") from exc
        html = self._content_html(str(item["content_type"]), raw_source)
        parser = _ContentImageParser()
        parser.feed(html)
        parser.close()
        images = parser.images
        placeholder_rows = [
            row for row in paragraphs if str(row["text_object_hash"]) == _PLACEHOLDER_OBJECT_HASH
        ]
        if len(images) != len(placeholder_rows):
            raise ValueError(
                "VISUAL_IMAGE_PLACEHOLDER_COUNT_MISMATCH:"
                f"{item['content_type']}:{item['content_id']}:"
                f"images={len(images)}:placeholders={len(placeholder_rows)}"
            )
        result: list[ZhihuVisualInventoryEntry] = []
        for image_ordinal, (image, placeholder) in enumerate(
            zip(images, placeholder_rows, strict=True), start=1
        ):
            placeholder_ordinal = int(str(placeholder["ordinal"]))
            preceding, following = self._context_pair(
                item,
                paragraphs,
                placeholder_ordinal,
            )
            bindings = bindings_by_paragraph.get(str(placeholder["paragraph_id"]), [])
            pairs: list[tuple[str, str]] = []
            for binding in bindings:
                argument_id = str(binding["argument_unit_id"])
                object_hash = argument_object_cache.get(argument_id)
                if object_hash is None:
                    payload = json.loads(str(binding["unit_json"]))
                    object_hash = self.objects.put_json(payload).sha256
                    argument_object_cache[argument_id] = object_hash
                pairs.append((argument_id, object_hash))
            pairs.sort(key=lambda pair: pair[0])
            reasons: list[str] = []
            status = ZhihuVisualInventoryStatus.READY_FOR_CAPTURE
            if not pairs:
                synthetic_seed = {
                    "semantic_run_id": semantic_run_id,
                    "source_item_id": str(item["item_id"]),
                    "placeholder_paragraph_id": str(placeholder["paragraph_id"]),
                    "preceding_paragraph_id": str(preceding["paragraph_id"]),
                    "following_paragraph_id": str(following["paragraph_id"]),
                }
                synthetic_id = "visual-context-argument:" + sha256_bytes(
                    canonical_json_bytes(synthetic_seed)
                )
                synthetic_payload = {
                    "schema_version": "zhihu-visual-context-anchor-v1",
                    "argument_unit_id": synthetic_id,
                    **synthetic_seed,
                    "preceding_text_object_hash": str(preceding["text_object_hash"]),
                    "following_text_object_hash": str(following["text_object_hash"]),
                    "status": "VISUAL_CONTEXT_ANCHOR_READY",
                    "standalone_distillable": False,
                    "merge_policy": "MERGE_WITH_BOTH",
                }
                synthetic_hash = self.objects.put_json(synthetic_payload).sha256
                pairs.append((synthetic_id, synthetic_hash))
            unit_payload = json.loads(str(placeholder["unit_json"]))
            locator = unit_payload.get("locator") if isinstance(unit_payload, dict) else None
            dom_path = locator.get("dom_path") if isinstance(locator, dict) else None
            if not isinstance(dom_path, str) or not dom_path.strip():
                dom_path = f"visible-block[{placeholder_ordinal - 1}]/img"
            image_url_hash = sha256_bytes(image.url.encode("utf-8"))
            placement_seed = {
                "pipeline_version": _VISUAL_PIPELINE_VERSION,
                "semantic_run_id": semantic_run_id,
                "source_item_id": str(item["item_id"]),
                "placeholder_paragraph_id": str(placeholder["paragraph_id"]),
                "image_url_hash": image_url_hash,
            }
            placement_id = "zhihu-visual-placement:" + sha256_bytes(
                canonical_json_bytes(placement_seed)
            )
            result.append(
                ZhihuVisualInventoryEntry(
                    placement_id=placement_id,
                    author_source_id=str(item["author_source_id"]),
                    semantic_run_id=semantic_run_id,
                    source_item_id=str(item["item_id"]),
                    content_type=str(item["content_type"]),
                    content_id=str(item["content_id"]),
                    source_snapshot_id=source_snapshot_id,
                    source_snapshot_object_hash=source_hash,
                    image_ordinal=image_ordinal,
                    dom_path=str(dom_path or f"unresolved-image[{image_ordinal}]"),
                    image_url=image.url,
                    image_url_hash=image_url_hash,
                    placeholder_paragraph_id=str(placeholder["paragraph_id"]),
                    placeholder_ordinal=placeholder_ordinal,
                    preceding_paragraph_id=(
                        str(preceding["paragraph_id"]) if preceding is not None else None
                    ),
                    preceding_paragraph_ordinal=(
                        int(str(preceding["ordinal"])) if preceding is not None else None
                    ),
                    preceding_text_object_hash=(
                        str(preceding["text_object_hash"]) if preceding is not None else None
                    ),
                    following_paragraph_id=(
                        str(following["paragraph_id"]) if following is not None else None
                    ),
                    following_paragraph_ordinal=(
                        int(str(following["ordinal"])) if following is not None else None
                    ),
                    following_text_object_hash=(
                        str(following["text_object_hash"]) if following is not None else None
                    ),
                    affected_argument_unit_ids=[pair[0] for pair in pairs],
                    affected_argument_object_hashes=[pair[1] for pair in pairs],
                    status=status,
                    reason_codes=sorted(set(reasons)),
                    created_at=anchor,
                )
            )
        return result

    @staticmethod
    def _content_html(content_type: str, raw: bytes) -> str:
        text = raw.decode("utf-8")
        if content_type == "articles":
            parser = _ZhihuArticleHtmlParser()
            parser.feed(text)
            parser.close()
            if not parser.root_closed or parser.body_html is None:
                raise ValueError("VISUAL_ARTICLE_BODY_NOT_RECOVERABLE")
            return parser.body_html
        payload = json.loads(text)
        if not isinstance(payload, dict):
            raise ValueError("VISUAL_SOURCE_PAYLOAD_NOT_OBJECT")
        if content_type == "thoughts":
            content_html = payload.get("content_html")
            if isinstance(content_html, str):
                return content_html
            content = payload.get("content")
            if isinstance(content, dict) and isinstance(content.get("content_html"), str):
                return str(content["content_html"])
            raise ValueError("VISUAL_THOUGHT_HTML_NOT_RECOVERABLE")
        content = payload.get("content")
        if not isinstance(content, str):
            raise ValueError("VISUAL_ANSWER_HTML_NOT_RECOVERABLE")
        return content

    def _context_pair(
        self,
        item: dict[str, object],
        paragraphs: list[dict[str, object]],
        ordinal: int,
    ) -> tuple[dict[str, object], dict[str, object]]:
        real = [
            row for row in paragraphs if str(row["text_object_hash"]) != _PLACEHOLDER_OBJECT_HASH
        ]
        preceding = next(
            (row for row in reversed(real) if int(str(row["ordinal"])) < ordinal),
            None,
        )
        following = next(
            (row for row in real if int(str(row["ordinal"])) > ordinal),
            None,
        )
        if preceding is None:
            preceding = next((row for row in real if row is not following), None)
        if following is None:
            following = next((row for row in reversed(real) if row is not preceding), None)
        if preceding is None:
            preceding = self._synthetic_boundary_context(item, "PRECEDING", ordinal)
        if following is None or str(following["paragraph_id"]) == str(preceding["paragraph_id"]):
            following = self._synthetic_boundary_context(item, "FOLLOWING", ordinal)
        return preceding, following

    def _synthetic_boundary_context(
        self,
        item: dict[str, object],
        role: str,
        ordinal: int,
    ) -> dict[str, object]:
        seed = {
            "schema_version": "zhihu-visual-boundary-context-v1",
            "role": role,
            "author_source_id": str(item["author_source_id"]),
            "source_item_id": str(item["item_id"]),
            "content_type": str(item["content_type"]),
            "content_id": str(item["content_id"]),
            "source_snapshot_id": str(item["source_snapshot_id"]),
            "image_ordinal": ordinal,
        }
        identity = sha256_bytes(canonical_json_bytes(seed))
        text = (
            f"Source boundary context ({role.lower()}): "
            f"{item['content_type']} {item['content_id']} by {item['author_source_id']}."
        )
        text_hash = self.objects.put_bytes(text.encode("utf-8")).sha256
        return {
            "paragraph_id": f"zhihu-visual-boundary-context:{identity}",
            "ordinal": max(1, ordinal),
            "text_object_hash": text_hash,
        }

    def status(self, author_source_id: str) -> dict[str, object]:
        manifest = self.plan(author_source_id)
        checkpoint = self.state.get_checkpoint("knowledge-zhihu-visual-run", manifest.run_id)
        return {
            "run_id": manifest.run_id,
            "author_source_id": author_source_id,
            "semantic_run_id": manifest.semantic_run_id,
            "image_reference_count": manifest.image_reference_count,
            "ready_for_capture_count": manifest.ready_for_capture_count,
            "blocked_inventory_count": manifest.blocked_count,
            "checkpoint": checkpoint,
            "formal_committee_weight_allowed": False,
        }

    def _register_exact(
        self,
        *,
        artifact_id: str,
        artifact_type: str,
        schema_version: str,
        object_hash: str,
        input_hashes: list[str],
    ) -> None:
        expected_inputs = sorted(set(input_hashes))
        existing = self.state.artifact_record(artifact_id)
        if existing is not None:
            if (
                str(existing["type"]) != artifact_type
                or str(existing["schema_version"]) != schema_version
                or str(existing["object_hash"]) != object_hash
                or sorted(existing["input_hashes"]) != expected_inputs
            ):
                raise ValueError(f"visual artifact identity collision: {artifact_id}")
            return
        self.state.register_artifact(
            artifact_id=artifact_id,
            artifact_type=artifact_type,
            schema_version=schema_version,
            object_hash=object_hash,
            input_hashes=expected_inputs,
        )


__all__ = ["ZhihuVisualPipelineService"]
