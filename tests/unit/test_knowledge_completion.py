from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from typing import Any, cast

import pytest
from pydantic import ValidationError

from astock.core.hashing import canonical_json_bytes, sha256_bytes
from astock.knowledge.completion_repository import KnowledgeCompletionRepository
from astock.knowledge.completion_service import (
    KnowledgeCompletionService,
    ZhihuVisualCompletionService,
)
from astock.knowledge.provider import RepositoryKnowledgeSkillProvider
from astock.knowledge.visual_skill_service import VisualSkillService
from astock.schemas.knowledge_completion import (
    DirectKnowledgeSkillReviewBatch,
    DirectKnowledgeSkillReviewSpec,
    KnowledgeProviderMode,
    KnowledgeProviderReadiness,
    KnowledgeReviewDecision,
    KnowledgeSkillQuery,
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
)
from astock.schemas.knowledge_visual import (
    VisualEvidencePack,
    ZhihuVisualPacketReference,
    ZhihuVisualPackStatus,
)


def _seed_minimal_direct_run(state, object_store) -> str:
    run_id = "direct-source-v4:test-completion"
    now = "2026-08-09T04:00:00+00:00"
    source_file_hash = object_store.put_json({"source": "fixture"}).sha256
    run_manifest_hash = object_store.put_json({"manifest": "fixture"}).sha256
    dedup_hash = object_store.put_json({"dedup": "fixture"}).sha256
    source_slice_hash = sha256_bytes(b"source evidence")
    fragment_hash = object_store.put_bytes(b"source evidence with context").sha256
    shadow_payload = {
        "run_id": run_id,
        "all_skill_ids": ["skill-ready", "skill-review"],
        "shadow_skill_ids": ["skill-ready"],
    }
    shadow_json = canonical_json_bytes(shadow_payload).decode("utf-8")
    shadow_hash = object_store.put_bytes(shadow_json.encode("utf-8")).sha256

    with state.transaction() as connection:
        connection.execute(
            "INSERT INTO knowledge_direct_run("
            "run_id,input_hash,pipeline_version,stage,frozen_source_count,frozen_batch_count,"
            "manifest_object_hash,manifest_json,formal_committee_weight_allowed,created_at,"
            "updated_at,finalized_at) VALUES(?,?,?,?,?,?,?,?,0,?,?,?)",
            (
                run_id,
                sha256_bytes(b"direct-run-input"),
                "direct-source-v4-test",
                "BATCHES_IMPORTED",
                1,
                1,
                run_manifest_hash,
                json.dumps({"fixture": True}),
                now,
                now,
                None,
            ),
        )
        connection.execute(
            "INSERT INTO knowledge_direct_source("
            "run_id,source_id,source_kind,source_file_hash,created_at) VALUES(?,?,?,?,?)",
            (run_id, "source-1", "PDF", source_file_hash, now),
        )
        connection.execute(
            "INSERT INTO knowledge_direct_chapter_batch("
            "run_id,batch_id,source_id,chapter_unit_id,batch_ordinal,stage,"
            "created_at,updated_at) VALUES(?,?,?,?,?,'FROZEN',?,?)",
            (run_id, "batch-1", "source-1", "chapter-1", 1, now, now),
        )
        connection.execute(
            "INSERT INTO knowledge_direct_chapter_fragment("
            "run_id,batch_id,fragment_id,context_role,fragment_ordinal,object_hash,source_kind,"
            "unit_index,start_offset,end_offset,locator_json) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                run_id,
                "batch-1",
                "fragment-1",
                "CURRENT",
                1,
                fragment_hash,
                "PDF",
                1,
                0,
                15,
                json.dumps({"page": 1}),
            ),
        )
        connection.execute(
            "INSERT INTO knowledge_direct_sol_confirmed_dedup_manifest("
            "run_id,manifest_id,manifest_hash,manifest_object_hash,embedding_usage,sol_confirmed,"
            "sol_version,sol_version_hash,manifest_json,created_at) VALUES(?,?,?,?,?,1,?,?,?,?)",
            (
                run_id,
                "dedup-1",
                dedup_hash,
                dedup_hash,
                "POST_GENERATION_ASSIST_ONLY",
                "GPT-5.6 Sol test",
                sha256_bytes(b"sol-test-version"),
                json.dumps({"fixture": True}),
                now,
            ),
        )
        for final_skill_id, status, name, uncertainty in (
            (
                "skill-ready",
                "READY_FOR_SHADOW",
                "Ready evidence discipline",
                None,
            ),
            (
                "skill-review",
                "NEEDS_USER_REVIEW",
                "Pending judgment discipline",
                "requires explicit human judgment",
            ),
        ):
            source_ref_payload = {
                "batch_id": "batch-1",
                "source_id": "source-1",
                "source_file_hash": source_file_hash,
                "chapter_unit_id": "chapter-1",
                "fragment_id": "fragment-1",
                "fragment_object_hash": fragment_hash,
                "source_object_hash": source_slice_hash,
                "slice_hash": source_slice_hash,
                "locator": {
                    "source_kind": "PDF",
                    "unit_index": 1,
                    "start_offset": 0,
                    "end_offset": 15,
                },
                "original_locator": "page=1",
                "paragraph_head": "source evidence",
                "visual_evidence_ids": [],
            }
            skill_payload = {
                "final_skill_id": final_skill_id,
                "candidate_ids": [f"candidate:{final_skill_id}"],
                "skill_name": name,
                "primary_module": "FUNDAMENTAL_RESEARCH",
                "secondary_modules": [],
                "decision_question": "What evidence should be required?",
                "core_principle": "Use immutable evidence before acting.",
                "applicable_conditions": [],
                "reasoning_steps": ["verify source"],
                "required_evidence": [],
                "positive_signals": [],
                "negative_signals": [],
                "invalidation_conditions": [],
                "failure_modes": [],
                "confidence": 0.8,
                "status": status,
                "uncertainty_reason": uncertainty,
                "source_refs": [source_ref_payload],
                "visual_refs": [],
                "formal_committee_weight_allowed": False,
            }
            skill_json = canonical_json_bytes(skill_payload).decode("utf-8")
            skill_hash = object_store.put_bytes(skill_json.encode("utf-8")).sha256
            connection.execute(
                "INSERT INTO knowledge_direct_final_skill("
                "run_id,manifest_id,final_skill_id,status,skill_name,primary_module,"
                "secondary_modules_json,secondary_module_count,decision_question,core_principle,"
                "applicable_conditions_json,reasoning_steps_json,reasoning_step_count,"
                "required_evidence_json,positive_signals_json,negative_signals_json,"
                "invalidation_conditions_json,failure_modes_json,confidence,module_count,"
                "contribution_count,source_ref_count,visual_ref_count,uncertainty_reason,"
                "formal_committee_weight_allowed,skill_object_hash,skill_json,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    run_id,
                    "dedup-1",
                    final_skill_id,
                    status,
                    name,
                    "FUNDAMENTAL_RESEARCH",
                    "[]",
                    0,
                    "What evidence should be required?",
                    "Use immutable evidence before acting.",
                    "[]",
                    '["verify source"]',
                    1,
                    "[]",
                    "[]",
                    "[]",
                    "[]",
                    "[]",
                    0.8,
                    1,
                    1,
                    1,
                    0,
                    uncertainty,
                    0,
                    skill_hash,
                    skill_json,
                    now,
                ),
            )
            connection.execute(
                "INSERT INTO knowledge_direct_final_source_ref("
                "run_id,final_skill_id,ref_ordinal,batch_id,source_id,source_file_hash,"
                "chapter_unit_id,fragment_id,fragment_object_hash,source_object_hash,slice_hash,"
                "source_kind,unit_index,start_offset,end_offset,locator_json,original_locator,"
                "paragraph_head,visual_evidence_ids_json) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    run_id,
                    final_skill_id,
                    1,
                    "batch-1",
                    "source-1",
                    source_file_hash,
                    "chapter-1",
                    "fragment-1",
                    fragment_hash,
                    source_slice_hash,
                    source_slice_hash,
                    "PDF",
                    1,
                    0,
                    15,
                    json.dumps({"page": 1}),
                    "page=1",
                    "source evidence",
                    "[]",
                ),
            )
        connection.execute(
            "INSERT INTO knowledge_direct_shadow_bundle("
            "run_id,manifest_id,bundle_id,all_skill_ids_json,shadow_skill_ids_json,all_skill_count,"
            "shadow_skill_count,non_ready_skill_count,formal_committee_weight_allowed,"
            "bundle_object_hash,bundle_json,created_at) VALUES(?,?,?,?,?,2,1,1,0,?,?,?)",
            (
                run_id,
                "dedup-1",
                "shadow-bundle:test",
                json.dumps(["skill-ready", "skill-review"]),
                json.dumps(["skill-ready"]),
                shadow_hash,
                shadow_json,
                now,
            ),
        )
        connection.execute(
            "UPDATE knowledge_direct_run SET stage='FINALIZED',updated_at=?,finalized_at=? "
            "WHERE run_id=?",
            (now, now, run_id),
        )
    return run_id


def _review_batch(run_id: str) -> DirectKnowledgeSkillReviewBatch:
    return DirectKnowledgeSkillReviewBatch(
        run_id=run_id,
        actor="recorded human-review fixture",
        reviewed_at=datetime(2026, 8, 9, 4, 30, tzinfo=UTC),
        expected_pending_count=1,
        decisions=[
            DirectKnowledgeSkillReviewSpec(
                skill_name="Pending judgment discipline",
                decision=KnowledgeReviewDecision.APPROVE,
                reason="Recorded fixture explicitly approves this test skill only.",
            )
        ],
    )


def test_review_is_fail_closed_append_only_and_registry_is_immutable(
    state,
    object_store,
) -> None:
    run_id = _seed_minimal_direct_run(state, object_store)
    service = KnowledgeCompletionService(state, object_store)
    provider = RepositoryKnowledgeSkillProvider(
        KnowledgeCompletionRepository(state),
        object_store,
    )

    before = service.status(run_id)
    assert before.pending_review_count == 1
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        with state.transaction() as connection:
            connection.execute(
                "UPDATE knowledge_direct_final_skill SET skill_name=? "
                "WHERE run_id=? AND final_skill_id=?",
                ("mutated final skill", run_id, "skill-ready"),
            )
    assert before.registry_release_id is None
    blocked = provider.status(run_id)
    assert blocked.status is KnowledgeProviderReadiness.NEEDS_INFO
    assert blocked.mode is KnowledgeProviderMode.BLOCKED
    assert blocked.reason_code == "DIRECT_REVIEW_PENDING"
    assert blocked.eligible_skill_count == 0
    with pytest.raises(ValueError, match="review remains open"):
        service.publish_registry(run_id)

    first = cast(dict[str, Any], service.apply_review_batch(_review_batch(run_id)))
    second = cast(dict[str, Any], service.apply_review_batch(_review_batch(run_id)))
    assert first["status"] == "REVIEW_CLOSED"
    assert second["receipts"][0]["idempotent_replay"] is True
    assert service.audit(run_id, require_registry=False)["status"] == "PASS"
    pre_registry_audit = cast(dict[str, Any], service.audit(run_id))
    assert "REGISTRY_NOT_PUBLISHED" in pre_registry_audit["findings"]

    with pytest.raises(sqlite3.IntegrityError, match="append-only"):
        with state.transaction() as connection:
            connection.execute(
                "UPDATE knowledge_direct_review_decision SET reason=? WHERE run_id=?",
                ("mutated review decision", run_id),
            )

    release = service.publish_registry(run_id)
    replay = service.publish_registry(run_id)
    assert release.release.admitted_skill_count == 2
    assert release.release.created_at == _review_batch(run_id).reviewed_at
    assert release.release.formal_committee_weight_allowed is False
    assert replay.idempotent_replay is True

    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        with state.transaction() as connection:
            connection.execute(
                "UPDATE knowledge_skill_registry_release SET registry_version=? WHERE run_id=?",
                ("mutated", run_id),
            )

    ready = provider.status(run_id)
    assert ready.status is KnowledgeProviderReadiness.READY
    assert ready.mode is KnowledgeProviderMode.REGISTRY_RELEASE
    assert ready.eligible_skill_count == 2
    query = KnowledgeSkillQuery(query="evidence", top_k=2)
    selection = provider.select(run_id, query)
    cached = provider.select(run_id, query)
    assert selection.selected_count == 2
    assert selection.context_bytes <= query.max_context_bytes
    assert selection.estimated_tokens <= query.max_estimated_tokens
    assert cached.cache_hit is True
    assert service.audit(run_id)["status"] == "PASS"

    report = cast(dict[str, Any], service.report(run_id))
    assert report["direct_source_coverage"]["frozen_source_count"] == 1
    assert report["skill_source_chain"]["skill_count"] == 2
    assert report["skill_source_chain"]["skills_with_source_refs"] == 2
    assert len(report["skill_source_chain"]["binding_hash"]) == 64
    assert report["registry"]["published"] is True
    assert report["visual_completion"]["real_visual_completion_claimed"] is False

    object_store.path_for(release.object_hash).unlink()
    blocked_after_cache = provider.select(run_id, query)
    assert (
        blocked_after_cache.provider_status.status
        is KnowledgeProviderReadiness.NEEDS_INFO
    )
    assert blocked_after_cache.reason_code == "REGISTRY_OBJECT_MISSING"
    assert blocked_after_cache.selected_count == 0


def _seed_source_snapshot(state, object_store) -> tuple[str, str, str]:
    snapshot_hash = object_store.put_json({"html": "recorded Zhihu fixture"}).sha256
    previous_hash = object_store.put_json({"argument": "before visual"}).sha256
    rebuilt_hash = object_store.put_json({"argument": "after visual"}).sha256
    with state.transaction() as connection:
        connection.execute(
            "INSERT INTO source_snapshot_index("
            "snapshot_id,source_id,object_hash,fetched_at,availability_at,fetch_status) "
            "VALUES(?,?,?,?,?,?)",
            (
                "snapshot:zhihu:test",
                "zhihu-author:test",
                snapshot_hash,
                "2026-08-09T04:00:00+00:00",
                "2026-08-09T04:00:00+00:00",
                "SUCCESS",
            ),
        )
    return "snapshot:zhihu:test", previous_hash, rebuilt_hash


def _visual_request(
    snapshot_id: str,
    previous_hash: str,
    rebuilt_hash: str,
) -> ZhihuVisualCaptureRequest:
    return ZhihuVisualCaptureRequest(
        placement_id="zhihu-placement:test",
        source_snapshot_id=snapshot_id,
        source_item_id="answer:test",
        author_source_id="zhihu-author:test",
        content_id="content:test",
        image_url="https://pic1.zhimg.com/v2-recorded-test.png",
        response_mime="image/png",
        dom_locator=ZhihuDomImageLocator(dom_path="article/p[2]/img[1]", image_ordinal=1),
        ocr=ZhihuOcrAttempt(
            status=ZhihuVisualOcrStatus.NO_TEXT,
            engine_version="recorded-ocr-v1",
            failure_reason="No readable text in decorative recorded fixture.",
        ),
        classification=ZhihuVisualClassification(
            visual_type=ZhihuVisualType.DECORATIVE,
            classifier_version="recorded-classifier-v1",
            confidence=1.0,
        ),
        preceding_context=ZhihuParagraphContext(
            paragraph_id="paragraph:before",
            paragraph_ordinal=1,
            text="Recorded paragraph before the image.",
        ),
        following_context=ZhihuParagraphContext(
            paragraph_id="paragraph:after",
            paragraph_ordinal=2,
            text="Recorded paragraph after the image.",
        ),
        affected_argument_rebuilds=[
            ZhihuAffectedArgumentRebuild(
                argument_unit_id="argument:test",
                previous_argument_object_hash=previous_hash,
                rebuilt_argument_object_hash=rebuilt_hash,
                status=ZhihuArgumentRebuildStatus.READY,
            )
        ],
    )


def test_zhihu_visual_contract_is_object_first_two_sided_and_fail_closed(
    state,
    object_store,
) -> None:
    snapshot_id, previous_hash, rebuilt_hash = _seed_source_snapshot(state, object_store)
    service = ZhihuVisualCompletionService(state, object_store)
    request = _visual_request(snapshot_id, previous_hash, rebuilt_hash)
    image_bytes = b"\x89PNG\r\n\x1a\nrecorded-fixture"

    first = service.capture(request, image_bytes)
    second = service.capture(request, image_bytes)

    assert first.packet_status is ZhihuVisualPacketStatus.READY
    assert first.merge_policy == "MERGE_WITH_BOTH"
    assert first.standalone is False
    assert object_store.verify(first.image_object_hash)
    assert second.idempotent_replay is True
    status = service.status()
    assert status["asset_count"] == 1
    assert status["placement_count"] == 1

    coverage = KnowledgeCompletionRepository(state).visual_author_coverage()
    assert coverage == [
        {
            "author_source_id": "zhihu-author:test",
            "source_item_count": 1,
            "placement_count": 1,
            "asset_count": 1,
            "ready_count": 1,
            "needs_review_count": 0,
            "unresolved_count": 0,
        }
    ]

    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        with state.transaction() as connection:
            connection.execute(
                "UPDATE knowledge_zhihu_visual_placement SET dom_path=? WHERE placement_id=?",
                ("mutated", request.placement_id),
            )


def test_zhihu_visual_url_allowlist_rejects_host_confusion() -> None:
    with pytest.raises(ValidationError):
        ZhihuVisualCaptureRequest(
            placement_id="zhihu-placement:bad-host",
            source_snapshot_id="snapshot:test",
            source_item_id="answer:test",
            author_source_id="author:test",
            content_id="content:test",
            image_url="https://evilzhimg.com/image.png",
            response_mime="image/png",
            dom_locator=ZhihuDomImageLocator(dom_path="article/img[1]", image_ordinal=1),
            ocr=ZhihuOcrAttempt(
                status=ZhihuVisualOcrStatus.NO_TEXT,
                engine_version="recorded-ocr-v1",
                failure_reason="No text in invalid-host validation fixture.",
            ),
            classification=ZhihuVisualClassification(
                visual_type=ZhihuVisualType.DECORATIVE,
                classifier_version="recorded-classifier-v1",
                confidence=1.0,
            ),
            preceding_context=ZhihuParagraphContext(
                paragraph_id="before",
                paragraph_ordinal=1,
                text="Before.",
            ),
            following_context=ZhihuParagraphContext(
                paragraph_id="after",
                paragraph_ordinal=2,
                text="After.",
            ),
            affected_argument_rebuilds=[
                ZhihuAffectedArgumentRebuild(
                    argument_unit_id="argument:test",
                    previous_argument_object_hash="0" * 64,
                    rebuilt_argument_object_hash="1" * 64,
                    status=ZhihuArgumentRebuildStatus.READY,
                )
            ],
        )


def _seed_visual_overlay_author(state, object_store, author: str, suffix: str) -> None:
    anchor = datetime(2026, 8, 10, 2, 0, tzinfo=UTC)
    now = anchor.isoformat()
    snapshot_ref = object_store.put_json({"author": author, "body": f"fixture-{suffix}"})
    snapshot_id = f"snapshot:visual-overlay:{suffix}"
    semantic_run_id = f"knowledge-semantic-run:visual-overlay:{suffix}"
    item_id = f"semantic-item:visual-overlay:{suffix}"
    argument_id = f"argument-unit:visual-overlay:{suffix}"
    content_id = f"content-{suffix}"
    argument_text = (
        f"[1|CLAIM] {suffix}估值时不能只看单一倍数，需要把现金流质量、"
        "行业位置与图表证据共同核对。\n"
        f"[2|CONCLUSION] {suffix}若图表与正文结论不一致，应回到原始披露并降低判断置信度。"
    )
    argument_text_ref = object_store.put_bytes(argument_text.encode("utf-8"))
    normalized_ref = object_store.put_json({"normalized": suffix})
    unit_payload = {
        "argument_unit_id": argument_id,
        "method_categories": ["VALUATION"],
        "reason_codes": ["VISUAL_TEST_METHOD"],
    }
    previous_ref = object_store.put_json(unit_payload)
    rebuilt_ref = object_store.put_json({"rebuilt": suffix, "merge_policy": "MERGE_WITH_BOTH"})
    with state.transaction() as connection:
        connection.execute(
            "INSERT INTO source_snapshot_index("
            "snapshot_id,source_id,object_hash,fetched_at,availability_at,fetch_status) "
            "VALUES(?,?,?,?,?,?)",
            (snapshot_id, author, snapshot_ref.sha256, now, now, "SUCCEEDED"),
        )
        connection.execute(
            "INSERT INTO knowledge_semantic_run("
            "run_id,author_source_id,input_manifest_hash,pipeline_version,stage,run_json,"
            "started_at,finished_at) VALUES(?,?,?,?,?,?,?,?)",
            (
                semantic_run_id,
                author,
                sha256_bytes(f"manifest-{suffix}".encode()),
                "knowledge-semantic-funnel-three-view-v3",
                "ARGUMENT_UNITS_BUILT",
                json.dumps({"content_item_count": 1}, separators=(",", ":")),
                now,
                now,
            ),
        )
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
                content_id,
                f"version-{suffix}",
                snapshot_id,
                snapshot_ref.sha256,
                normalized_ref.sha256,
                0,
                json.dumps({"item_id": item_id}, separators=(",", ":")),
                now,
            ),
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
                content_id,
                1,
                2,
                argument_text_ref.sha256,
                "READY",
                0.9,
                0.9,
                json.dumps(unit_payload, separators=(",", ":")),
                now,
            ),
        )

    visual_service = ZhihuVisualCompletionService(state, object_store)
    request = ZhihuVisualCaptureRequest(
        placement_id=f"zhihu-placement:visual-overlay:{suffix}",
        source_snapshot_id=snapshot_id,
        source_item_id=item_id,
        author_source_id=author,
        content_id=content_id,
        image_url=f"https://pic1.zhimg.com/v2-visual-overlay-{suffix}.png",
        response_mime="image/png",
        dom_locator=ZhihuDomImageLocator(dom_path="visible-block[1]/img", image_ordinal=1),
        ocr=ZhihuOcrAttempt(
            status=ZhihuVisualOcrStatus.SUCCEEDED,
            engine_version="recorded-ocr-v1",
            text=f"{suffix} 估值 图表 现金流",
            confidence=0.95,
        ),
        classification=ZhihuVisualClassification(
            visual_type=ZhihuVisualType.TABLE,
            classifier_version="recorded-classifier-v1",
            confidence=0.9,
        ),
        preceding_context=ZhihuParagraphContext(
            paragraph_id=f"paragraph-before-{suffix}",
            paragraph_ordinal=1,
            text=f"{suffix}估值前文。",
        ),
        following_context=ZhihuParagraphContext(
            paragraph_id=f"paragraph-after-{suffix}",
            paragraph_ordinal=2,
            text=f"{suffix}估值后文。",
        ),
        affected_argument_rebuilds=[
            ZhihuAffectedArgumentRebuild(
                argument_unit_id=argument_id,
                previous_argument_object_hash=previous_ref.sha256,
                rebuilt_argument_object_hash=rebuilt_ref.sha256,
                status=ZhihuArgumentRebuildStatus.READY,
            )
        ],
    )
    capture = visual_service.capture(
        request,
        b"\x89PNG\r\n\x1a\n" + f"visual-overlay-{suffix}".encode(),
    )
    inventory_ref = object_store.put_json({"inventory": suffix})
    pack = VisualEvidencePack(
        pack_id=f"visual-evidence-pack:test:{suffix}",
        run_id=f"zhihu-visual-run:test:{suffix}",
        author_source_id=author,
        semantic_run_id=semantic_run_id,
        inventory_artifact_id=f"inventory:test:{suffix}",
        inventory_object_hash=inventory_ref.sha256,
        source_snapshot_ids=[snapshot_id],
        source_snapshot_object_hashes=[snapshot_ref.sha256],
        image_reference_count=1,
        placement_count=1,
        unique_asset_count=1,
        ready_count=1,
        needs_review_count=0,
        blocked_count=0,
        status=ZhihuVisualPackStatus.READY,
        packet_references=[
            ZhihuVisualPacketReference(
                placement_id=request.placement_id,
                packet_artifact_id=capture.packet_artifact_id,
                packet_object_hash=capture.packet_object_hash,
                image_object_hash=capture.image_object_hash,
                packet_status=ZhihuVisualPacketStatus.READY,
                visual_type=ZhihuVisualType.TABLE,
                ocr_status=ZhihuVisualOcrStatus.SUCCEEDED.value,
                created_at=anchor,
            )
        ],
        created_at=anchor,
    )
    pack_ref = object_store.put_json(pack.model_dump(mode="json"))
    state.register_artifact(
        artifact_id=f"VisualEvidencePack:{pack.pack_id}",
        artifact_type="VisualEvidencePack",
        schema_version=pack.schema_version,
        object_hash=pack_ref.sha256,
        input_hashes=[inventory_ref.sha256, capture.packet_object_hash],
    )


def test_visual_skill_overlay_publishes_and_provider_reads_composite_registry(
    state,
    object_store,
) -> None:
    base_run_id = _seed_minimal_direct_run(state, object_store)
    completion = KnowledgeCompletionService(state, object_store)
    completion.apply_review_batch(_review_batch(base_run_id))
    completion.publish_registry(base_run_id)
    authors = (
        ("zhihu:mr-dang-77", "dang"),
        ("zhihu:huang-wei-yan-30", "huang"),
        ("zhihu:xiao-peng-61-47", "xiao"),
    )
    for author, suffix in authors:
        _seed_visual_overlay_author(state, object_store, author, suffix)

    visual = VisualSkillService(state, object_store)
    generated = visual.generate(base_run_id)
    assert generated["candidate_count"] == 3
    assert generated["no_skill_count"] == 0
    assert visual.audit(base_run_id)["status"] == "PASS"
    reviewed = visual.review_all(base_run_id)
    assert reviewed["review"] == {"approved": 3, "rejected": 0, "pending": 0}
    published = visual.publish(base_run_id)
    assert published["release"]["base_admitted_skill_count"] == 2
    assert published["release"]["overlay_admitted_skill_count"] == 3
    assert published["release"]["composite_admitted_skill_count"] == 5
    assert visual.audit(base_run_id)["status"] == "PASS"

    provider = RepositoryKnowledgeSkillProvider(
        KnowledgeCompletionRepository(state),
        object_store,
    )
    provider_status = provider.status(base_run_id)
    assert provider_status.status is KnowledgeProviderReadiness.READY
    assert provider_status.reason_code == "COMPOSITE_REGISTRY_READY"
    assert provider_status.eligible_skill_count == 5
    query = KnowledgeSkillQuery(query="估值", top_k=5)
    selection = provider.select(base_run_id, query)
    assert selection.selected_count == 3
    assert all(item.final_skill_id.startswith("visual-skill:") for item in selection.skills)
    assert provider.select(base_run_id, query).cache_hit is True

    report = cast(dict[str, Any], completion.report(base_run_id))
    assert report["visual_completion"]["real_visual_completion_claimed"] is True
    assert report["registry"]["composite_release"]["composite_admitted_skill_count"] == 5
