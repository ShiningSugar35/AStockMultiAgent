"""Recoverable real Zhihu visual completion from already-frozen source snapshots."""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
import threading
import time
from collections import Counter
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from typing import Protocol
from urllib.parse import urljoin

import httpx

from astock.core.errors import StorageError
from astock.core.hashing import canonical_json_bytes, sha256_bytes
from astock.core.object_store import ObjectStore
from astock.core.project_root import resolve_project_root
from astock.core.state import StateStore
from astock.documents.ocr import OcrResult, RapidOcrEngine
from astock.knowledge.adapter import _ZhihuArticleHtmlParser
from astock.knowledge.completion_service import ZhihuVisualCompletionService
from astock.knowledge.visual_repository import ZhihuVisualRepository
from astock.schemas.knowledge_completion import (
    ZhihuAffectedArgumentRebuild,
    ZhihuArgumentRebuildStatus,
    ZhihuDomImageLocator,
    ZhihuOcrAttempt,
    ZhihuParagraphContext,
    ZhihuVisualCaptureRequest,
    ZhihuVisualClassification,
    ZhihuVisualOcrStatus,
    ZhihuVisualPacketStatus,
    ZhihuVisualType,
    validate_zhimg_url,
)
from astock.schemas.knowledge_visual import (
    VisualEvidencePack,
    ZhihuVisualInventoryEntry,
    ZhihuVisualInventoryManifest,
    ZhihuVisualInventoryStatus,
    ZhihuVisualPacketReference,
    ZhihuVisualPackStatus,
    ZhihuVisualPipelineReport,
)

_SEMANTIC_PIPELINE_VERSION = "knowledge-semantic-funnel-three-view-v3"
_VISUAL_PIPELINE_VERSION = "zhihu-visual-completion-v1"
_PLACEHOLDER = "[图片]"
_PLACEHOLDER_OBJECT_HASH = sha256_bytes(_PLACEHOLDER.encode("utf-8"))
_MAX_REDIRECTS = 5


class _OcrEngine(Protocol):
    name: str
    version: str

    def recognize(self, image_bytes: bytes) -> OcrResult: ...


@dataclass(frozen=True, slots=True)
class _ImageReference:
    url: str
    alt: str


@dataclass(frozen=True, slots=True)
class _FetchedImage:
    image_bytes: bytes
    mime: str
    redirect_chain: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _FetchedVisual:
    entry: ZhihuVisualInventoryEntry
    fetched: _FetchedImage
    image_object_hash: str


@dataclass(frozen=True, slots=True)
class _PreparedVisual:
    entry: ZhihuVisualInventoryEntry
    fetched: _FetchedImage
    ocr: ZhihuOcrAttempt
    classification: ZhihuVisualClassification
    rebuilds: tuple[ZhihuAffectedArgumentRebuild, ...]


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
        capture_service: ZhihuVisualCompletionService | None = None,
        client: httpx.Client | None = None,
        ocr_engine: _OcrEngine | None = None,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        self.state = state
        self.objects = objects
        self.repository = repository or ZhihuVisualRepository(state)
        self.capture_service = capture_service or ZhihuVisualCompletionService(state, objects)
        self.client = client or httpx.Client(
            timeout=30.0,
            follow_redirects=False,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 Chrome/138 Safari/537.36"
                ),
                "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
            },
        )
        self._ocr_engine = ocr_engine
        self._ocr_local = threading.local()
        self.sleeper = sleeper

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

    def run(
        self,
        author_source_id: str,
        *,
        max_images: int | None = None,
        request_interval_seconds: float = 0.0,
        workers: int = 4,
    ) -> ZhihuVisualPipelineReport:
        if max_images is not None and max_images < 1:
            raise ValueError("max_images must be positive")
        if request_interval_seconds < 0:
            raise ValueError("request_interval_seconds cannot be negative")
        if workers < 1 or workers > 8:
            raise ValueError("workers must be between 1 and 8")
        manifest = self.plan(author_source_id)
        inventory_artifact_id = f"ZhihuVisualInventoryManifest:{manifest.manifest_id}"
        inventory_record = self.state.artifact_record(inventory_artifact_id)
        assert inventory_record is not None
        inventory_hash = str(inventory_record["object_hash"])
        ready_entries = [
            entry
            for entry in manifest.entries
            if entry.status is ZhihuVisualInventoryStatus.READY_FOR_CAPTURE
        ]
        pending: list[tuple[int, ZhihuVisualInventoryEntry]] = []
        skipped = 0
        processed = 0
        next_index = 0
        for index, entry in enumerate(ready_entries):
            if self.repository.packet_for_placement(entry.placement_id) is not None:
                skipped += 1
                processed += 1
                next_index = index + 1
                continue
            pending.append((index, entry))
        if max_images is not None:
            pending = pending[:max_images]

        captured = 0
        blocked_fetch = 0
        fetch_reasons: Counter[str] = Counter()
        stop_for_policy = False
        batch_size = max(32, workers * 8)
        for offset in range(0, len(pending), batch_size):
            batch = pending[offset : offset + batch_size]
            prepared_items, failures = self._prepare_batch(batch, workers=workers)
            for index, failure in failures:
                next_index = index
                blocked_fetch += 1
                fetch_reasons[failure.reason_code] += 1
                stop_for_policy = stop_for_policy or failure.stop_pipeline
                self._checkpoint(
                    manifest,
                    inventory_artifact_id,
                    inventory_hash,
                    next_index=index,
                    status="NEEDS_INFO",
                    reason_code=failure.reason_code,
                )
                processed += 1
                next_index = index + 1
            if stop_for_policy:
                break
            for index, prepared in prepared_items:
                request = self._capture_request(
                    prepared.entry,
                    prepared.fetched,
                    prepared.ocr,
                    prepared.classification,
                    list(prepared.rebuilds),
                )
                self.capture_service.capture(request, prepared.fetched.image_bytes)
                captured += 1
                processed += 1
                next_index = index + 1
                self._checkpoint(
                    manifest,
                    inventory_artifact_id,
                    inventory_hash,
                    next_index=next_index,
                    status="RUNNING",
                    reason_code=None,
                )
            if request_interval_seconds and offset + batch_size < len(pending):
                self.sleeper(request_interval_seconds)

        all_packets_present = all(
            self.repository.packet_for_placement(entry.placement_id) is not None
            for entry in ready_entries
        )
        complete_iteration = all_packets_present and not stop_for_policy
        if not complete_iteration and max_images is not None and not stop_for_policy:
            return ZhihuVisualPipelineReport(
                run_id=manifest.run_id,
                author_source_id=manifest.author_source_id,
                semantic_run_id=manifest.semantic_run_id,
                inventory_artifact_id=inventory_artifact_id,
                inventory_object_hash=inventory_hash,
                processed_count=processed,
                captured_count=captured,
                skipped_existing_count=skipped,
                blocked_fetch_count=blocked_fetch,
                next_index=next_index,
                complete=False,
            )
        pack, pack_artifact_id, pack_hash = self._build_pack(
            manifest,
            inventory_artifact_id,
            inventory_hash,
            fetch_reasons,
        )
        checkpoint_status = (
            "SUCCEEDED" if pack.status is ZhihuVisualPackStatus.READY else pack.status.value
        )
        self._checkpoint(
            manifest,
            inventory_artifact_id,
            inventory_hash,
            next_index=next_index,
            status=checkpoint_status,
            reason_code=None,
            pack_artifact_id=pack_artifact_id,
            pack_status=pack.status.value,
            pack_object_hash=pack_hash,
        )
        return ZhihuVisualPipelineReport(
            run_id=manifest.run_id,
            author_source_id=manifest.author_source_id,
            semantic_run_id=manifest.semantic_run_id,
            inventory_artifact_id=inventory_artifact_id,
            inventory_object_hash=inventory_hash,
            processed_count=processed,
            captured_count=captured,
            skipped_existing_count=skipped,
            blocked_fetch_count=blocked_fetch,
            pack_artifact_id=pack_artifact_id,
            pack_object_hash=pack_hash,
            pack_status=pack.status,
            next_index=next_index,
            complete=complete_iteration,
        )

    def _prepare_visual(self, entry: ZhihuVisualInventoryEntry) -> _PreparedVisual:
        fetched = self._fetch_image(entry)
        image_ref = self.objects.put_bytes(fetched.image_bytes)
        ocr = self._ocr(fetched.image_bytes)
        classification = self._classify(fetched.image_bytes, ocr)
        rebuilds = self._rebuild_arguments(entry, image_ref.sha256, ocr, classification)
        return _PreparedVisual(
            entry=entry,
            fetched=fetched,
            ocr=ocr,
            classification=classification,
            rebuilds=tuple(rebuilds),
        )

    def _prepare_batch(
        self,
        batch: list[tuple[int, ZhihuVisualInventoryEntry]],
        *,
        workers: int,
    ) -> tuple[list[tuple[int, _PreparedVisual]], list[tuple[int, _VisualFetchBlocked]]]:
        fetched_items: list[tuple[int, _FetchedVisual]] = []
        failures: list[tuple[int, _VisualFetchBlocked]] = []
        with ThreadPoolExecutor(
            max_workers=workers,
            thread_name_prefix="zhihu-visual-fetch",
        ) as pool:
            futures = [(index, pool.submit(self._fetch_visual, entry)) for index, entry in batch]
            for index, future in futures:
                try:
                    fetched_items.append((index, future.result()))
                except _VisualFetchBlocked as exc:
                    failures.append((index, exc))
        fetched_items.sort(key=lambda item: item[0])
        ocr_by_placement = self._batch_ocr([item[1] for item in fetched_items])
        prepared: list[tuple[int, _PreparedVisual]] = []
        for index, fetched_visual in fetched_items:
            ocr = ocr_by_placement[fetched_visual.entry.placement_id]
            classification = self._classify(fetched_visual.fetched.image_bytes, ocr)
            rebuilds = self._rebuild_arguments(
                fetched_visual.entry,
                fetched_visual.image_object_hash,
                ocr,
                classification,
            )
            prepared.append(
                (
                    index,
                    _PreparedVisual(
                        entry=fetched_visual.entry,
                        fetched=fetched_visual.fetched,
                        ocr=ocr,
                        classification=classification,
                        rebuilds=tuple(rebuilds),
                    ),
                )
            )
        return prepared, failures

    def _fetch_visual(self, entry: ZhihuVisualInventoryEntry) -> _FetchedVisual:
        fetched = self._fetch_image(entry)
        image_ref = self.objects.put_bytes(fetched.image_bytes)
        return _FetchedVisual(
            entry=entry,
            fetched=fetched,
            image_object_hash=image_ref.sha256,
        )

    def _batch_ocr(self, items: list[_FetchedVisual]) -> dict[str, ZhihuOcrAttempt]:
        if not items:
            return {}
        if self._ocr_engine is not None:
            return {item.entry.placement_id: self._ocr(item.fetched.image_bytes) for item in items}
        windows_results = self._windows_ocr_batch(items)
        result: dict[str, ZhihuOcrAttempt] = {}
        for item in items:
            attempt = windows_results.get(item.entry.placement_id)
            if attempt is None:
                attempt = self._ocr(item.fetched.image_bytes)
            result[item.entry.placement_id] = attempt
        return result

    def _windows_ocr_batch(self, items: list[_FetchedVisual]) -> dict[str, ZhihuOcrAttempt]:
        script = (
            resolve_project_root(module_file=Path(__file__)) / "scripts" / "windows_ocr_batch.ps1"
        )
        if not script.is_file():
            return {}
        manifest = [
            {
                "id": item.entry.placement_id,
                "path": str(self.objects.path_for(item.image_object_hash)),
            }
            for item in items
        ]
        manifest_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                suffix=".json",
                delete=False,
            ) as handle:
                json.dump(manifest, handle, ensure_ascii=False, separators=(",", ":"))
                manifest_path = Path(handle.name)
            completed = subprocess.run(
                [
                    "powershell",
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(script),
                    "-ManifestPath",
                    str(manifest_path),
                ],
                check=True,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=120,
            )
            payload = json.loads(completed.stdout)
        except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
            return {}
        finally:
            if manifest_path is not None:
                manifest_path.unlink(missing_ok=True)
        rows = payload if isinstance(payload, list) else [payload]
        result: dict[str, ZhihuOcrAttempt] = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            placement_id = row.get("id")
            if not isinstance(placement_id, str):
                continue
            text = str(row.get("text") or "").strip()
            language = str(row.get("language") or "unknown")
            if row.get("status") == "SUCCEEDED" and text:
                result[placement_id] = ZhihuOcrAttempt(
                    status=ZhihuVisualOcrStatus.SUCCEEDED,
                    engine_version=f"windows-media-ocr:{language}:confidence-unavailable",
                    text=text,
                    confidence=0.5,
                )
            elif row.get("status") == "SUCCEEDED":
                result[placement_id] = ZhihuOcrAttempt(
                    status=ZhihuVisualOcrStatus.NO_TEXT,
                    engine_version=f"windows-media-ocr:{language}",
                    failure_reason="Windows OCR completed without readable text.",
                )
        return result

    def _fetch_image(self, entry: ZhihuVisualInventoryEntry) -> _FetchedImage:
        current = validate_zhimg_url(entry.image_url)
        redirect_chain: list[str] = []
        seen = {current}
        for _ in range(_MAX_REDIRECTS + 1):
            try:
                response = self.client.get(
                    current,
                    headers={"Referer": "https://www.zhihu.com/"},
                )
            except httpx.TimeoutException as exc:
                raise _VisualFetchBlocked("VISUAL_IMAGE_FETCH_TIMEOUT", False) from exc
            except httpx.HTTPError as exc:
                raise _VisualFetchBlocked("VISUAL_IMAGE_FETCH_NETWORK", False) from exc
            if response.status_code in {301, 302, 303, 307, 308}:
                location = response.headers.get("location")
                if not location:
                    raise _VisualFetchBlocked("VISUAL_IMAGE_REDIRECT_MISSING", True)
                target = validate_zhimg_url(urljoin(current, location))
                if target in seen:
                    raise _VisualFetchBlocked("VISUAL_IMAGE_REDIRECT_CYCLE", True)
                redirect_chain.append(target)
                seen.add(target)
                current = target
                continue
            if response.status_code in {401, 403, 429}:
                raise _VisualFetchBlocked(
                    f"VISUAL_IMAGE_ACCESS_{response.status_code}",
                    True,
                )
            if response.status_code != 200:
                raise _VisualFetchBlocked(
                    f"VISUAL_IMAGE_HTTP_{response.status_code}",
                    response.status_code >= 400,
                )
            if not response.content:
                raise _VisualFetchBlocked("VISUAL_IMAGE_EMPTY_BODY", False)
            detected = _detect_image_mime(response.content)
            if detected is None:
                raise _VisualFetchBlocked("VISUAL_IMAGE_UNSUPPORTED_MIME", False)
            return _FetchedImage(response.content, detected, tuple(redirect_chain))
        raise _VisualFetchBlocked("VISUAL_IMAGE_REDIRECT_LIMIT", True)

    def _ocr(self, image_bytes: bytes) -> ZhihuOcrAttempt:
        engine = self._ocr_engine
        if engine is None:
            engine = getattr(self._ocr_local, "engine", None)
        if engine is None:
            try:
                engine = RapidOcrEngine(
                    max_side_len=640,
                    intra_op_num_threads=1,
                    inter_op_num_threads=1,
                )
            except Exception as exc:  # pragma: no cover - environment-specific initialization
                return ZhihuOcrAttempt(
                    status=ZhihuVisualOcrStatus.NO_TEXT,
                    engine_version="rapidocr-unavailable",
                    failure_reason=(
                        "OCR engine unavailable; visual classification continues without text: "
                        f"{type(exc).__name__}"
                    ),
                )
            self._ocr_local.engine = engine
        engine_version = f"{engine.name}:{engine.version}"
        try:
            result = engine.recognize(image_bytes)
        except Exception as exc:  # pragma: no cover - defensive provider boundary
            return ZhihuOcrAttempt(
                status=ZhihuVisualOcrStatus.NO_TEXT,
                engine_version=engine_version,
                failure_reason=(
                    "OCR execution produced no reliable text; visual classification continues: "
                    f"{type(exc).__name__}"
                ),
            )
        text = result.text.strip()
        if not text:
            return ZhihuOcrAttempt(
                status=ZhihuVisualOcrStatus.NO_TEXT,
                engine_version=engine_version,
                failure_reason="OCR completed without readable text.",
            )
        confidence = result.average_confidence if result.average_confidence is not None else 0.0
        return ZhihuOcrAttempt(
            status=ZhihuVisualOcrStatus.SUCCEEDED,
            engine_version=engine_version,
            text=text,
            confidence=max(0.0, min(1.0, confidence)),
        )

    @staticmethod
    def _classify(
        image_bytes: bytes,
        ocr: ZhihuOcrAttempt,
    ) -> ZhihuVisualClassification:
        width, height = _image_dimensions(image_bytes)
        text = ocr.text or ""
        compact = re.sub(r"\s+", "", text)
        numeric = sum(char.isdigit() for char in compact)
        numeric_ratio = numeric / max(1, len(compact))
        lines = [line for line in text.splitlines() if line.strip()]
        if not compact:
            if width is not None and height is not None and width <= 128 and height <= 128:
                return ZhihuVisualClassification(
                    visual_type=ZhihuVisualType.DECORATIVE,
                    classifier_version="zhihu-visual-mechanical-v2",
                    confidence=0.95,
                )
            visual_type = (
                ZhihuVisualType.SCREENSHOT
                if width is not None and height is not None and max(width, height) >= 720
                else ZhihuVisualType.DIAGRAM
            )
            return ZhihuVisualClassification(
                visual_type=visual_type,
                classifier_version="zhihu-visual-mechanical-v2",
                confidence=0.60,
            )
        if len(lines) >= 6 and numeric_ratio >= 0.18:
            visual_type = ZhihuVisualType.TABLE
            confidence = 0.72
        elif (
            width is not None
            and height is not None
            and width >= int(height * 1.2)
            and (numeric_ratio >= 0.10 or "%" in text)
        ):
            visual_type = ZhihuVisualType.CHART
            confidence = 0.70
        elif len(compact) >= 100:
            visual_type = ZhihuVisualType.SCREENSHOT
            confidence = 0.66
        else:
            visual_type = ZhihuVisualType.DIAGRAM
            confidence = 0.55
        return ZhihuVisualClassification(
            visual_type=visual_type,
            classifier_version="zhihu-visual-mechanical-v2",
            confidence=confidence,
        )

    def _rebuild_arguments(
        self,
        entry: ZhihuVisualInventoryEntry,
        image_object_hash: str,
        ocr: ZhihuOcrAttempt,
        classification: ZhihuVisualClassification,
    ) -> list[ZhihuAffectedArgumentRebuild]:
        result: list[ZhihuAffectedArgumentRebuild] = []
        for argument_id, previous_hash in zip(
            entry.affected_argument_unit_ids,
            entry.affected_argument_object_hashes,
            strict=True,
        ):
            rebuilt_payload = {
                "schema_version": "zhihu-visual-argument-rebuild-v1",
                "argument_unit_id": argument_id,
                "previous_argument_object_hash": previous_hash,
                "placement_id": entry.placement_id,
                "image_object_hash": image_object_hash,
                "ocr": ocr.model_dump(mode="json", exclude={"created_at"}),
                "classification": classification.model_dump(mode="json", exclude={"created_at"}),
                "preceding_paragraph_id": entry.preceding_paragraph_id,
                "preceding_text_object_hash": entry.preceding_text_object_hash,
                "following_paragraph_id": entry.following_paragraph_id,
                "following_text_object_hash": entry.following_text_object_hash,
                "standalone_distillable": False,
                "merge_policy": "MERGE_WITH_BOTH",
                "visual_interpretation_is_company_fact": False,
            }
            rebuilt_hash = self.objects.put_json(rebuilt_payload).sha256
            result.append(
                ZhihuAffectedArgumentRebuild(
                    argument_unit_id=argument_id,
                    previous_argument_object_hash=previous_hash,
                    rebuilt_argument_object_hash=rebuilt_hash,
                    status=ZhihuArgumentRebuildStatus.READY,
                )
            )
        return result

    def _capture_request(
        self,
        entry: ZhihuVisualInventoryEntry,
        fetched: _FetchedImage,
        ocr: ZhihuOcrAttempt,
        classification: ZhihuVisualClassification,
        rebuilds: list[ZhihuAffectedArgumentRebuild],
    ) -> ZhihuVisualCaptureRequest:
        assert entry.preceding_paragraph_id is not None
        assert entry.preceding_paragraph_ordinal is not None
        assert entry.preceding_text_object_hash is not None
        assert entry.following_paragraph_id is not None
        assert entry.following_paragraph_ordinal is not None
        assert entry.following_text_object_hash is not None
        preceding_text = self.objects.get_bytes(entry.preceding_text_object_hash).decode("utf-8")
        following_text = self.objects.get_bytes(entry.following_text_object_hash).decode("utf-8")
        return ZhihuVisualCaptureRequest(
            placement_id=entry.placement_id,
            source_snapshot_id=entry.source_snapshot_id,
            source_item_id=entry.source_item_id,
            author_source_id=entry.author_source_id,
            content_id=entry.content_id,
            image_url=entry.image_url,
            redirect_chain=list(fetched.redirect_chain),
            response_mime=fetched.mime,
            dom_locator=ZhihuDomImageLocator(
                dom_path=entry.dom_path,
                image_ordinal=entry.image_ordinal,
            ),
            ocr=ocr,
            classification=classification,
            preceding_context=ZhihuParagraphContext(
                paragraph_id=entry.preceding_paragraph_id,
                paragraph_ordinal=entry.preceding_paragraph_ordinal,
                text=preceding_text,
            ),
            following_context=ZhihuParagraphContext(
                paragraph_id=entry.following_paragraph_id,
                paragraph_ordinal=entry.following_paragraph_ordinal,
                text=following_text,
            ),
            affected_argument_rebuilds=rebuilds,
        )

    def _build_pack(
        self,
        manifest: ZhihuVisualInventoryManifest,
        inventory_artifact_id: str,
        inventory_hash: str,
        fetch_reasons: Counter[str],
    ) -> tuple[VisualEvidencePack, str, str]:
        packet_refs: list[ZhihuVisualPacketReference] = []
        missing_count = 0
        reason_counts = Counter(fetch_reasons)
        image_hashes: set[str] = set()
        for entry in manifest.entries:
            if entry.status is not ZhihuVisualInventoryStatus.READY_FOR_CAPTURE:
                missing_count += 1
                reason_counts.update(entry.reason_codes)
                continue
            packet = self.repository.packet_for_placement(entry.placement_id)
            if packet is None:
                missing_count += 1
                if not fetch_reasons:
                    reason_counts["VISUAL_PACKET_NOT_CAPTURED"] += 1
                continue
            image_hash = str(packet["image_object_hash"])
            image_hashes.add(image_hash)
            packet_refs.append(
                ZhihuVisualPacketReference(
                    placement_id=entry.placement_id,
                    packet_artifact_id=str(packet["packet_artifact_id"]),
                    packet_object_hash=str(packet["packet_object_hash"]),
                    image_object_hash=image_hash,
                    packet_status=ZhihuVisualPacketStatus(str(packet["packet_status"])),
                    visual_type=ZhihuVisualType(str(packet["visual_type"])),
                    ocr_status=str(packet["ocr_status"]),
                    created_at=manifest.created_at,
                )
            )
        packet_refs.sort(key=lambda item: item.placement_id)
        ready_count = sum(
            item.packet_status is ZhihuVisualPacketStatus.READY for item in packet_refs
        )
        needs_review = len(packet_refs) - ready_count
        if needs_review:
            raise ValueError(
                f"VISUAL_PIPELINE_FORBIDS_NEEDS_REVIEW:{manifest.author_source_id}:{needs_review}"
            )
        status = ZhihuVisualPackStatus.NEEDS_INFO if missing_count else ZhihuVisualPackStatus.READY
        seed = {
            "schema_version": "visual-evidence-pack-v1",
            "run_id": manifest.run_id,
            "inventory_object_hash": inventory_hash,
            "packets": [item.packet_object_hash for item in packet_refs],
            "missing_count": missing_count,
            "reason_counts": dict(sorted(reason_counts.items())),
        }
        identity = sha256_bytes(canonical_json_bytes(seed))
        pack = VisualEvidencePack(
            pack_id=f"visual-evidence-pack:{identity}",
            run_id=manifest.run_id,
            author_source_id=manifest.author_source_id,
            semantic_run_id=manifest.semantic_run_id,
            inventory_artifact_id=inventory_artifact_id,
            inventory_object_hash=inventory_hash,
            source_snapshot_ids=sorted({item.source_snapshot_id for item in manifest.entries}),
            source_snapshot_object_hashes=sorted(
                {item.source_snapshot_object_hash for item in manifest.entries}
            ),
            image_reference_count=manifest.image_reference_count,
            placement_count=len(packet_refs),
            unique_asset_count=len(image_hashes),
            ready_count=ready_count,
            needs_review_count=needs_review,
            blocked_count=missing_count,
            status=status,
            reason_counts=dict(sorted(reason_counts.items())),
            packet_references=packet_refs,
            created_at=manifest.created_at,
        )
        object_ref = self.objects.put_json(pack.model_dump(mode="json"))
        artifact_id = f"VisualEvidencePack:{pack.pack_id}"
        input_hashes = sorted(
            {
                inventory_hash,
                *(item.packet_object_hash for item in packet_refs),
            }
        )
        self._register_exact(
            artifact_id=artifact_id,
            artifact_type="VisualEvidencePack",
            schema_version=pack.schema_version,
            object_hash=object_ref.sha256,
            input_hashes=input_hashes,
        )
        return pack, artifact_id, object_ref.sha256

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

    def _checkpoint(
        self,
        manifest: ZhihuVisualInventoryManifest,
        inventory_artifact_id: str,
        inventory_hash: str,
        *,
        next_index: int,
        status: str,
        reason_code: str | None,
        pack_artifact_id: str | None = None,
        pack_status: str | None = None,
        pack_object_hash: str | None = None,
    ) -> None:
        self.state.set_checkpoint(
            scope_type="knowledge-zhihu-visual-run",
            scope_key=manifest.run_id,
            cursor={
                "author_source_id": manifest.author_source_id,
                "semantic_run_id": manifest.semantic_run_id,
                "inventory_artifact_id": inventory_artifact_id,
                "inventory_object_hash": inventory_hash,
                "next_index": next_index,
                "reason_code": reason_code,
                "pack_artifact_id": pack_artifact_id,
                "pack_status": pack_status,
                "pack_object_hash": pack_object_hash,
            },
            status=status,
            object_hash=pack_object_hash or inventory_hash,
        )

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


class _VisualFetchBlocked(RuntimeError):
    def __init__(self, reason_code: str, stop_pipeline: bool) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code
        self.stop_pipeline = stop_pipeline


def _detect_image_mime(data: bytes) -> str | None:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def _image_dimensions(data: bytes) -> tuple[int | None, int | None]:
    if data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 24:
        return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
    if data[:6] in {b"GIF87a", b"GIF89a"} and len(data) >= 10:
        return int.from_bytes(data[6:8], "little"), int.from_bytes(data[8:10], "little")
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP" and len(data) >= 30:
        chunk = data[12:16]
        if chunk == b"VP8X":
            return (
                1 + int.from_bytes(data[24:27], "little"),
                1 + int.from_bytes(data[27:30], "little"),
            )
    if data.startswith(b"\xff\xd8"):
        index = 2
        while index + 9 < len(data):
            if data[index] != 0xFF:
                index += 1
                continue
            marker = data[index + 1]
            index += 2
            if marker in {0xD8, 0xD9}:
                continue
            if index + 2 > len(data):
                break
            segment_length = int.from_bytes(data[index : index + 2], "big")
            if segment_length < 2 or index + segment_length > len(data):
                break
            if (
                marker
                in {
                    0xC0,
                    0xC1,
                    0xC2,
                    0xC3,
                    0xC5,
                    0xC6,
                    0xC7,
                    0xC9,
                    0xCA,
                    0xCB,
                    0xCD,
                    0xCE,
                    0xCF,
                }
                and segment_length >= 7
            ):
                height = int.from_bytes(data[index + 3 : index + 5], "big")
                width = int.from_bytes(data[index + 5 : index + 7], "big")
                return width, height
            index += segment_length
    return None, None


__all__ = ["ZhihuVisualPipelineService"]
