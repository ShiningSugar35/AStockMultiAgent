"""Additive CLI registration for Knowledge Completion."""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import Annotated, Any, cast

import typer

from astock.knowledge.completion_repository import KnowledgeCompletionRepository
from astock.knowledge.completion_service import (
    KnowledgeCompletionService,
    ZhihuVisualCompletionService,
)
from astock.knowledge.provider import RepositoryKnowledgeSkillProvider
from astock.knowledge.skill_audit import KnowledgeSkillAuditService
from astock.knowledge.visual_pipeline import ZhihuVisualPipelineService
from astock.knowledge.visual_skill_service import VisualSkillService
from astock.schemas.knowledge_completion import (
    KnowledgeProviderReadiness,
    KnowledgeSkillQuery,
)
from astock.schemas.knowledge_skill_audit import KnowledgeSkillAuditVerdict


def register_knowledge_completion_commands(
    app: typer.Typer,
    services: Callable[[], tuple[Any, Any, Any]],
    emit: Callable[[Any], None],
) -> None:
    def skill_audit_service() -> KnowledgeSkillAuditService:
        paths, state, objects = services()
        return KnowledgeSkillAuditService(state, objects, paths.root)

    @app.command("knowledge-completion-review-plan")
    def review_plan(
        review_file: Annotated[
            Path,
            typer.Argument(exists=True, file_okay=True, dir_okay=False, resolve_path=True),
        ],
    ) -> None:
        _, state, objects = services()
        service = KnowledgeCompletionService(state, objects)
        emit(service.review_plan(service.load_review_batch(review_file)))

    @app.command("knowledge-completion-review-apply")
    def review_apply(
        review_file: Annotated[
            Path,
            typer.Argument(exists=True, file_okay=True, dir_okay=False, resolve_path=True),
        ],
    ) -> None:
        _, state, objects = services()
        service = KnowledgeCompletionService(state, objects)
        emit(service.apply_review_file(review_file))

    @app.command("knowledge-completion-finalize")
    def completion_finalize(
        review_file: Annotated[
            Path,
            typer.Argument(exists=True, file_okay=True, dir_okay=False, resolve_path=True),
        ],
    ) -> None:
        _, state, objects = services()
        service = KnowledgeCompletionService(state, objects)
        batch = service.load_review_batch(review_file)
        preflight = service.audit(batch.run_id, require_registry=False)
        if preflight["status"] != "PASS":
            raise RuntimeError(
                f"knowledge completion preflight failed: {preflight['findings']}"
            )
        review = service.apply_review_batch(batch)
        release = service.publish_registry(batch.run_id)
        audit = service.audit(batch.run_id)
        provider = RepositoryKnowledgeSkillProvider(
            KnowledgeCompletionRepository(state),
            objects,
        )
        provider_status = provider.status(batch.run_id)
        if audit["status"] != "PASS":
            raise RuntimeError(
                f"knowledge completion audit failed: {audit['findings']}"
            )
        if provider_status.status is not KnowledgeProviderReadiness.READY:
            raise RuntimeError(
                f"knowledge provider is not ready: {provider_status.reason_code}"
            )
        report = cast(dict[str, Any], service.report(batch.run_id))
        source_chain = dict(report["skill_source_chain"])
        source_chain.pop("skills", None)
        emit(
            {
                "preflight": preflight,
                "review": review,
                "registry": {
                    "release": release.release.model_dump(
                        mode="json",
                        exclude={"decision_ids", "members"},
                    ),
                    "decision_count": len(release.release.decision_ids),
                    "member_count": len(release.release.members),
                    "object_hash": release.object_hash,
                    "idempotent_replay": release.idempotent_replay,
                },
                "audit": audit,
                "provider": provider_status.model_dump(mode="json"),
                "report": {
                    "schema_version": report["schema_version"],
                    "run_id": report["run_id"],
                    "status": report["status"],
                    "direct_source_coverage": report["direct_source_coverage"],
                    "skill_source_chain": source_chain,
                    "decision_count": len(report["decisions"]),
                    "registry": report["registry"],
                    "visual_completion": report["visual_completion"],
                    "formal_committee_weight_allowed": False,
                },
                "formal_committee_weight_allowed": False,
            }
        )

    @app.command("knowledge-completion-status")
    def completion_status(run_id: Annotated[str, typer.Argument()]) -> None:
        _, state, objects = services()
        emit(KnowledgeCompletionService(state, objects).status(run_id))

    @app.command("knowledge-completion-publish")
    def completion_publish(run_id: Annotated[str, typer.Argument()]) -> None:
        _, state, objects = services()
        emit(KnowledgeCompletionService(state, objects).publish_registry(run_id))

    @app.command("knowledge-completion-audit")
    def completion_audit(run_id: Annotated[str, typer.Argument()]) -> None:
        _, state, objects = services()
        emit(KnowledgeCompletionService(state, objects).audit(run_id))

    @app.command("knowledge-completion-report")
    def completion_report(run_id: Annotated[str, typer.Argument()]) -> None:
        _, state, objects = services()
        emit(KnowledgeCompletionService(state, objects).report(run_id))

    @app.command("knowledge-provider-status")
    def provider_status(run_id: Annotated[str, typer.Argument()]) -> None:
        _, state, objects = services()
        provider = RepositoryKnowledgeSkillProvider(
            KnowledgeCompletionRepository(state),
            objects,
        )
        emit(provider.status(run_id))

    @app.command("knowledge-provider-select")
    def provider_select(
        run_id: Annotated[str, typer.Argument()],
        query_text: Annotated[str, typer.Argument()],
        top_k: Annotated[
            int,
            typer.Option("--top-k", min=1, max=50),
        ] = 5,
    ) -> None:
        _, state, objects = services()
        provider = RepositoryKnowledgeSkillProvider(
            KnowledgeCompletionRepository(state),
            objects,
        )
        query = KnowledgeSkillQuery(query=query_text, top_k=top_k)
        emit(provider.select(run_id, query))

    @app.command("knowledge-skill-audit-plan")
    def knowledge_skill_audit_plan(
        source_run_id: Annotated[str | None, typer.Option("--source-run-id")] = None,
    ) -> None:
        emit(skill_audit_service().plan(source_run_id))

    @app.command("knowledge-skill-audit-run")
    def knowledge_skill_audit_run(audit_run_id: Annotated[str, typer.Argument()]) -> None:
        emit(skill_audit_service().run(audit_run_id))

    @app.command("knowledge-skill-audit-status")
    def knowledge_skill_audit_status(
        audit_run_id: Annotated[str | None, typer.Option("--audit-run-id")] = None,
    ) -> None:
        emit(skill_audit_service().status(audit_run_id))

    @app.command("knowledge-skill-audit-decisions")
    def knowledge_skill_audit_decisions(
        audit_run_id: Annotated[str, typer.Argument()],
        verdict: Annotated[KnowledgeSkillAuditVerdict | None, typer.Option()] = None,
    ) -> None:
        emit(skill_audit_service().decision_list(audit_run_id, verdict))

    @app.command("knowledge-skill-audit")
    def knowledge_skill_audit(audit_run_id: Annotated[str, typer.Argument()]) -> None:
        report = skill_audit_service().audit(audit_run_id)
        emit(report)
        if report.status != "PASS":
            raise typer.Exit(code=3)

    @app.command("knowledge-skill-audit-publish")
    def knowledge_skill_audit_publish(audit_run_id: Annotated[str, typer.Argument()]) -> None:
        release = skill_audit_service().publish(audit_run_id)
        emit(
            {
                "status": "PUBLISHED",
                "release_id": release.release_id,
                "release_artifact_id": release.release_artifact_id,
                "source_skill_count": release.source_skill_count,
                "decision_count": release.decision_count,
                "keep_count": release.keep_count,
                "keep_scoped_count": release.keep_scoped_count,
                "revise_count": release.revise_count,
                "retire_count": release.retire_count,
                "curated_count": release.curated_count,
                "active_skill_count": release.active_skill_count,
                "source_registry_object_hash": release.source_registry_object_hash,
            }
        )

    @app.command("knowledge-skill-prune-retired")
    def knowledge_skill_prune_retired(
        audit_run_id: Annotated[str | None, typer.Option("--audit-run-id")] = None,
        confirm: Annotated[bool, typer.Option("--confirm")] = False,
    ) -> None:
        emit(skill_audit_service().prune_retired(audit_run_id, confirm=confirm))

    @app.command("knowledge-zhihu-visual-capture")
    def visual_capture(
        request_file: Annotated[
            Path,
            typer.Argument(exists=True, file_okay=True, dir_okay=False, resolve_path=True),
        ],
        image_file: Annotated[
            Path,
            typer.Argument(exists=True, file_okay=True, dir_okay=False, resolve_path=True),
        ],
    ) -> None:
        _, state, objects = services()
        service = ZhihuVisualCompletionService(state, objects)
        emit(service.capture_file(request_file, image_file))

    @app.command("knowledge-zhihu-visual-status")
    def visual_status() -> None:
        _, state, objects = services()
        emit(ZhihuVisualCompletionService(state, objects).status())

    @app.command("knowledge-zhihu-visual-plan")
    def visual_plan(author_source_id: Annotated[str, typer.Argument()]) -> None:
        _, state, objects = services()
        pipeline = ZhihuVisualPipelineService(state, objects)
        manifest = pipeline.plan(author_source_id)
        artifact_id = f"ZhihuVisualInventoryManifest:{manifest.manifest_id}"
        record = state.artifact_record(artifact_id)
        reason_counts = Counter(
            reason for entry in manifest.entries for reason in entry.reason_codes
        )
        emit(
            {
                "run_id": manifest.run_id,
                "author_source_id": manifest.author_source_id,
                "semantic_run_id": manifest.semantic_run_id,
                "inventory_artifact_id": artifact_id,
                "inventory_object_hash": record["object_hash"] if record else None,
                "source_content_count": manifest.source_content_count,
                "image_reference_count": manifest.image_reference_count,
                "ready_for_capture_count": manifest.ready_for_capture_count,
                "blocked_count": manifest.blocked_count,
                "reason_counts": dict(sorted(reason_counts.items())),
                "formal_committee_weight_allowed": False,
            }
        )

    @app.command("knowledge-zhihu-visual-run")
    def visual_run(
        author_source_id: Annotated[str, typer.Argument()],
        max_images: Annotated[int | None, typer.Option("--max-images", min=1)] = None,
        request_interval_seconds: Annotated[
            float,
            typer.Option("--request-interval-seconds", min=0.0),
        ] = 0.0,
        workers: Annotated[int, typer.Option("--workers", min=1, max=8)] = 4,
    ) -> None:
        _, state, objects = services()
        emit(
            ZhihuVisualPipelineService(state, objects).run(
                author_source_id,
                max_images=max_images,
                request_interval_seconds=request_interval_seconds,
                workers=workers,
            )
        )

    @app.command("knowledge-zhihu-visual-run-status")
    def visual_run_status(author_source_id: Annotated[str, typer.Argument()]) -> None:
        _, state, objects = services()
        emit(ZhihuVisualPipelineService(state, objects).status(author_source_id))

    @app.command("knowledge-visual-skill-generate")
    def visual_skill_generate(base_run_id: Annotated[str, typer.Argument()]) -> None:
        _, state, objects = services()
        emit(VisualSkillService(state, objects).generate(base_run_id))

    @app.command("knowledge-visual-skill-review")
    def visual_skill_review(base_run_id: Annotated[str, typer.Argument()]) -> None:
        _, state, objects = services()
        emit(VisualSkillService(state, objects).review_all(base_run_id))

    @app.command("knowledge-visual-skill-publish")
    def visual_skill_publish(base_run_id: Annotated[str, typer.Argument()]) -> None:
        _, state, objects = services()
        emit(VisualSkillService(state, objects).publish(base_run_id))

    @app.command("knowledge-visual-skill-audit")
    def visual_skill_audit(base_run_id: Annotated[str, typer.Argument()]) -> None:
        _, state, objects = services()
        emit(VisualSkillService(state, objects).audit(base_run_id))

    @app.command("knowledge-visual-skill-status")
    def visual_skill_status(base_run_id: Annotated[str, typer.Argument()]) -> None:
        _, state, objects = services()
        emit(VisualSkillService(state, objects).status(base_run_id))

    @app.command("knowledge-visual-skill-finalize")
    def visual_skill_finalize(base_run_id: Annotated[str, typer.Argument()]) -> None:
        _, state, objects = services()
        service = VisualSkillService(state, objects)
        generation = service.generate(base_run_id)
        preflight = service.audit(base_run_id)
        if preflight["status"] != "PASS":
            raise RuntimeError(f"visual Skill preflight failed: {preflight['findings']}")
        review = service.review_all(base_run_id)
        release = service.publish(base_run_id)
        audit = service.audit(base_run_id)
        provider = RepositoryKnowledgeSkillProvider(
            KnowledgeCompletionRepository(state),
            objects,
        ).status(base_run_id)
        if audit["status"] != "PASS":
            raise RuntimeError(f"visual Skill final audit failed: {audit['findings']}")
        if provider.status is not KnowledgeProviderReadiness.READY:
            raise RuntimeError(f"composite provider is not READY: {provider.reason_code}")
        emit(
            {
                "generation": generation,
                "preflight": preflight,
                "review": review,
                "release": release,
                "audit": audit,
                "provider": provider.model_dump(mode="json"),
                "formal_committee_weight_allowed": False,
            }
        )


__all__ = ["register_knowledge_completion_commands"]
