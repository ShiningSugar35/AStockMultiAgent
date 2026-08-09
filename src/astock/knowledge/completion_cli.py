"""Additive CLI registration for Knowledge Completion."""

from __future__ import annotations

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
from astock.schemas.knowledge_completion import (
    KnowledgeProviderReadiness,
    KnowledgeSkillQuery,
)


def register_knowledge_completion_commands(
    app: typer.Typer,
    services: Callable[[], tuple[Any, Any, Any]],
    emit: Callable[[Any], None],
) -> None:
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


__all__ = ["register_knowledge_completion_commands"]
