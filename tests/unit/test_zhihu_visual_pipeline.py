from __future__ import annotations

import json
from datetime import UTC, datetime

import httpx
import pytest

from astock.documents.ocr import OcrResult
from astock.knowledge.visual_pipeline import ZhihuVisualPipelineService
from astock.schemas.knowledge_visual import ZhihuVisualPackStatus


class _RecordedOcr:
    name = "recorded-ocr"
    version = "1"

    def recognize(self, image_bytes: bytes) -> OcrResult:
        assert image_bytes.startswith(b"\x89PNG\r\n\x1a\n")
        return OcrResult(text="收入 100\n利润 20", average_confidence=0.95)


def _seed_semantic_image_case(state, object_store, *, image_count: int = 1) -> str:
    author = "zhihu:test-author"
    semantic_run_id = "knowledge-semantic-run:test-visual-v3"
    image_tags = "".join(
        (
            f'<img src="https://pic1.zhimg.com/v2-recorded-{index}_r.png" '
            'class="origin_image zh-lightbox-thumb">'
            f'<img src="https://pic1.zhimg.com/50/v2-recorded-{index}_720w.png" '
            'class="origin_image zh-lightbox-thumb lazy">'
        )
        for index in range(1, image_count + 1)
    )
    raw = json.dumps(
        {"content": f"<p>Before evidence.</p>{image_tags}<p>After conclusion.</p>"},
        ensure_ascii=False,
    ).encode("utf-8")
    raw_ref = object_store.put_bytes(raw)
    snapshot_id = f"{author}:answers:chrome:{raw_ref.sha256}"
    before_ref = object_store.put_bytes(b"Before evidence.")
    placeholder_ref = object_store.put_bytes("[图片]".encode())
    after_ref = object_store.put_bytes(b"After conclusion.")
    normalized_ref = object_store.put_bytes(b"normalized")
    now = datetime(2026, 7, 26, 9, 0, tzinfo=UTC).isoformat()
    item_id = "semantic-item:test-visual"
    with state.transaction() as connection:
        connection.execute(
            "INSERT INTO source_snapshot_index("
            "snapshot_id,source_id,object_hash,fetched_at,availability_at,fetch_status) "
            "VALUES(?,?,?,?,?,?)",
            (snapshot_id, f"{author}:answers:chrome", raw_ref.sha256, now, now, "SUCCEEDED"),
        )
        connection.execute(
            "INSERT INTO knowledge_semantic_run("
            "run_id,author_source_id,input_manifest_hash,pipeline_version,stage,run_json,"
            "started_at,finished_at) VALUES(?,?,?,?,?,?,?,?)",
            (
                semantic_run_id,
                author,
                "1" * 64,
                "knowledge-semantic-funnel-three-view-v3",
                "DEEPSEEK_PACKET_READY",
                json.dumps({"content_item_count": 1}, separators=(",", ":")),
                now,
                now,
            ),
        )
        paragraph_ids = ["paragraph:before"]
        paragraph_ids.extend(f"paragraph:image:{index}" for index in range(1, image_count + 1))
        paragraph_ids.append("paragraph:after")
        connection.execute(
            "INSERT INTO knowledge_semantic_content_item("
            "item_id,run_id,author_source_id,content_type,content_id,content_version_id,"
            "source_snapshot_id,source_object_hash,normalized_object_hash,paragraph_count,"
            "item_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                item_id,
                semantic_run_id,
                author,
                "answers",
                "123",
                "version:test",
                snapshot_id,
                raw_ref.sha256,
                normalized_ref.sha256,
                len(paragraph_ids),
                json.dumps({"item_id": item_id}, separators=(",", ":")),
                now,
            ),
        )
        rows = [
            (
                "paragraph:before",
                1,
                before_ref.sha256,
                {"locator": {"dom_path": "visible-block[0]/p"}},
            )
        ]
        rows.extend(
            (
                f"paragraph:image:{index}",
                index + 1,
                placeholder_ref.sha256,
                {"locator": {"dom_path": f"visible-block[{index}]/img"}},
            )
            for index in range(1, image_count + 1)
        )
        rows.append(
            (
                "paragraph:after",
                image_count + 2,
                after_ref.sha256,
                {"locator": {"dom_path": f"visible-block[{image_count + 1}]/p"}},
            )
        )
        for paragraph_id, ordinal, text_hash, payload in rows:
            connection.execute(
                "INSERT INTO knowledge_paragraph_unit("
                "paragraph_id,run_id,item_id,author_source_id,content_id,ordinal,"
                "text_object_hash,primary_role,standalone_distillable,merge_action,"
                "unit_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    paragraph_id,
                    semantic_run_id,
                    item_id,
                    author,
                    "123",
                    ordinal,
                    text_hash,
                    "EVIDENCE" if "image" in paragraph_id else "CLAIM",
                    0,
                    "MERGE_WITH_BOTH" if "image" in paragraph_id else "KEEP_STANDALONE",
                    json.dumps(payload, separators=(",", ":")),
                    now,
                ),
            )
        for index in range(1, image_count + 1):
            argument_id = f"argument:test:{index}"
            argument_payload = {
                "argument_unit_id": argument_id,
                "paragraph_ids": [
                    "paragraph:before",
                    f"paragraph:image:{index}",
                    "paragraph:after",
                ],
            }
            argument_text = object_store.put_bytes(
                json.dumps(argument_payload, separators=(",", ":")).encode()
            )
            connection.execute(
                "INSERT INTO knowledge_argument_unit("
                "argument_unit_id,run_id,item_id,author_source_id,content_id,start_ordinal,"
                "end_ordinal,text_object_hash,status,topic_relevance,"
                "methodological_completeness,unit_json,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    argument_id,
                    semantic_run_id,
                    item_id,
                    author,
                    "123",
                    index + 1,
                    index + 1,
                    argument_text.sha256,
                    "NEEDS_REVIEW",
                    0.8,
                    0.8,
                    json.dumps(argument_payload, separators=(",", ":")),
                    now,
                ),
            )
            connection.execute(
                "INSERT INTO knowledge_argument_unit_paragraph_ref("
                "argument_unit_id,ordinal,paragraph_id,rhetorical_role) VALUES(?,?,?,?)",
                (argument_id, 1, f"paragraph:image:{index}", "EVIDENCE"),
            )
    return author


def _png_bytes() -> bytes:
    # The capture contract identifies PNG from the signature. The remaining bytes are a
    # deterministic recorded fixture; OCR is stubbed in these unit tests.
    return b"\x89PNG\r\n\x1a\nrecorded-visual-fixture"


def test_visual_plan_maps_frozen_image_to_placeholder_exactly(state, object_store) -> None:
    author = _seed_semantic_image_case(state, object_store)
    service = ZhihuVisualPipelineService(state, object_store, ocr_engine=_RecordedOcr())

    first = service.plan(author)
    second = service.plan(author)

    assert first.manifest_id == second.manifest_id
    assert first.image_reference_count == 1
    assert first.ready_for_capture_count == 1
    assert first.blocked_count == 0
    entry = first.entries[0]
    assert entry.placeholder_paragraph_id == "paragraph:image:1"
    assert entry.dom_path == "visible-block[1]/img"
    assert entry.preceding_paragraph_id == "paragraph:before"
    assert entry.following_paragraph_id == "paragraph:after"
    assert entry.affected_argument_unit_ids == ["argument:test:1"]


def test_visual_pipeline_captures_and_publishes_pack(state, object_store) -> None:
    author = _seed_semantic_image_case(state, object_store)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host.endswith("zhimg.com")
        return httpx.Response(200, headers={"content-type": "image/png"}, content=_png_bytes())

    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)
    service = ZhihuVisualPipelineService(
        state,
        object_store,
        client=client,
        ocr_engine=_RecordedOcr(),
    )

    first = service.run(author)
    second = service.run(author)

    assert first.complete is True
    assert first.pack_status is ZhihuVisualPackStatus.READY
    assert first.captured_count == 1
    assert first.pack_artifact_id is not None
    assert first.pack_object_hash is not None
    assert second.captured_count == 0
    assert second.skipped_existing_count == 1
    assert second.pack_artifact_id == first.pack_artifact_id
    assert second.pack_object_hash == first.pack_object_hash
    status = service.capture_service.status()
    assert status["asset_count"] == 1
    assert status["placement_count"] == 1


def test_visual_pipeline_pauses_on_access_restriction(state, object_store) -> None:
    author = _seed_semantic_image_case(state, object_store)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, headers={"content-type": "text/html"}, content=b"blocked")

    service = ZhihuVisualPipelineService(
        state,
        object_store,
        client=httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False),
        ocr_engine=_RecordedOcr(),
    )

    report = service.run(author)

    assert report.complete is False
    assert report.pack_status is ZhihuVisualPackStatus.NEEDS_INFO
    assert report.blocked_fetch_count == 1
    assert service.capture_service.status()["placement_count"] == 0


def test_visual_plan_fails_closed_on_image_placeholder_drift(state, object_store) -> None:
    author = _seed_semantic_image_case(state, object_store, image_count=2)
    with state.transaction() as connection:
        connection.execute(
            "DELETE FROM knowledge_argument_unit_paragraph_ref WHERE paragraph_id=?",
            ("paragraph:image:2",),
        )
        connection.execute(
            "DELETE FROM knowledge_paragraph_unit WHERE paragraph_id=?",
            ("paragraph:image:2",),
        )

    with pytest.raises(ValueError, match="VISUAL_IMAGE_PLACEHOLDER_COUNT_MISMATCH"):
        ZhihuVisualPipelineService(state, object_store, ocr_engine=_RecordedOcr()).plan(author)
