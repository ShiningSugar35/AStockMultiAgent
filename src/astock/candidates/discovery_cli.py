"""Standalone deterministic CLI for the skill-driven discovery runtime.

The normal chat/MCP agent owns semantic research and Web recovery. This module
provides the typed persistence boundary: register reviewed evidence-backed
condition claims, refresh verified theses, publish immutable discovery Seed
releases, and expose recovery needs. It never edits SQLite directly.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Annotated, Any

import typer
from pydantic import ValidationError

from astock.candidates.discovery_recovery import DiscoveryRecoveryService
from astock.candidates.discovery_runtime import (
    DiscoveryConditionClaimDraft,
    DiscoveryRuntimeCompanyContext,
    DiscoveryRuntimeService,
)
from astock.candidates.discovery_theses import DiscoveryChannel
from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.schemas.discovery_runtime import (
    DiscoveryConditionClaimAdmissionRequest,
    DiscoveryRefreshRequest,
)
from astock.schemas.knowledge_completion import KnowledgeConditionState
from astock.settings import ProjectPaths

app = typer.Typer(add_completion=False, no_args_is_help=True)


def _services() -> tuple[ProjectPaths, StateStore, ObjectStore]:
    paths = ProjectPaths.discover()
    paths.ensure_directories()
    state = StateStore(paths.state_db, paths.root / "migrations")
    state.migrate()
    return paths, state, ObjectStore(paths.objects)


def _emit(value: Any) -> None:
    typer.echo(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2))


def _result_payload(result: Any) -> dict[str, object]:
    return {
        "company_id": result.company_id,
        "family_id": result.family_id,
        "channel": result.channel.value,
        "thesis_id": result.thesis_id,
        "state": result.state.value,
        "coverage": result.coverage,
        "conditions": [item.model_dump(mode="json") for item in result.conditions],
        "method_refs": [list(item) for item in result.method_refs],
        "satisfied_families": list(result.satisfied_families),
        "dependency_fingerprint": result.dependency_fingerprint,
    }


@app.command("schema")
def schema() -> None:
    """Print the typed request schemas used by the standalone discovery boundary."""

    _emit(
        {
            "refresh": DiscoveryRefreshRequest.model_json_schema(),
            "admit": DiscoveryConditionClaimAdmissionRequest.model_json_schema(),
        }
    )


@app.command("admit")
def admit(
    request_file: Annotated[
        Path,
        typer.Argument(exists=True, dir_okay=False, readable=True),
    ],
) -> None:
    """Register one reviewed evidence-backed positive/negative discovery condition."""

    try:
        request = DiscoveryConditionClaimAdmissionRequest.model_validate_json(
            request_file.read_text(encoding="utf-8")
        )
        paths, state, objects = _services()
        bundle = DiscoveryRuntimeService(paths.root, state, objects).admit_condition_claim(
            DiscoveryConditionClaimDraft(
                company_id=request.company_id,
                industry_id=request.industry_id,
                channel=DiscoveryChannel(request.channel),
                role=request.role,
                state=KnowledgeConditionState(request.state),
                subject_id=request.subject_id,
                evidence_ids=tuple(request.evidence_ids),
                confidence=request.confidence,
                metadata=request.metadata,
                claim_type=request.claim_type,
            ),
            as_of=request.as_of,
        )
    except (OSError, RuntimeError, ValidationError, ValueError) as exc:
        _emit({"status": "FAILED", "failure_code": "DISCOVERY_CONDITION_ADMISSION_FAILED"})
        raise typer.Exit(code=2) from exc

    _emit(
        {
            "status": "ADMITTED",
            "claim": bundle.claim.model_dump(mode="json"),
            "links": [item.model_dump(mode="json") for item in bundle.links],
        }
    )


@app.command("refresh")
def refresh(
    request_file: Annotated[
        Path,
        typer.Argument(exists=True, dir_okay=False, readable=True),
    ],
) -> None:
    """Evaluate current reviewed claims and publish the bounded verified-discovery tranche."""

    try:
        request = DiscoveryRefreshRequest.model_validate_json(
            request_file.read_text(encoding="utf-8")
        )
        paths, state, objects = _services()
        service = DiscoveryRuntimeService(paths.root, state, objects)
        contexts = tuple(
            DiscoveryRuntimeCompanyContext(
                company_id=item.company_id,
                market=item.market,
                name=item.name,
                industry_id=item.industry_id,
                business_profile=item.business_profile,
                industry_label=item.industry_label,
            )
            for item in request.contexts
        )
        outcome = service.refresh(
            as_of=request.as_of,
            contexts=contexts,
            max_verified_discovery=request.max_verified_discovery,
        )
        recovery = None
        if request.execute_existing_recovery and outcome.evidence_needs:
            recovery = DiscoveryRecoveryService(paths, state, objects).execute(
                contexts=contexts,
                evidence_needs=outcome.evidence_needs,
                recovery_budget_seconds=request.recovery_budget_seconds,
                max_companies=request.recovery_max_companies,
            )
            if recovery.company_recoveries:
                outcome = service.refresh(
                    as_of=request.as_of,
                    contexts=contexts,
                    max_verified_discovery=request.max_verified_discovery,
                )
    except (OSError, RuntimeError, ValidationError, ValueError) as exc:
        _emit({"status": "FAILED", "failure_code": "DISCOVERY_REFRESH_FAILED"})
        raise typer.Exit(code=2) from exc

    if outcome.finding_codes:
        status = "PARTIAL"
    elif outcome.evidence_needs:
        status = "RECOVERY_REQUIRED"
    else:
        status = "READY"
    _emit(
        {
            "status": status,
            "release": (
                outcome.release.model_dump(mode="json") if outcome.release is not None else None
            ),
            "results": [_result_payload(item) for item in outcome.results],
            "evidence_needs": [asdict(item) for item in outcome.evidence_needs],
            "evaluation_artifact_ids": list(outcome.evaluation_artifact_ids),
            "finding_codes": list(outcome.finding_codes),
            "recovery": asdict(recovery) if recovery is not None else None,
            "next_action": (
                "Review any locally acquired evidence first; for unresolved roles continue with "
                "authoritative public-source research, register immutable evidence, admit only "
                "reviewed positive/negative conditions, then rerun refresh."
                if outcome.evidence_needs
                else None
            ),
        }
    )
    if outcome.finding_codes:
        raise typer.Exit(code=3)


@app.command("status")
def status() -> None:
    """Read the latest refresh/release checkpoints without creating synthetic results."""

    _paths, state, _objects = _services()
    refresh_checkpoint = state.get_checkpoint("verified-discovery-refresh", "latest")
    release_checkpoint = state.get_checkpoint("verified-discovery-seeds", "latest")
    evidence_need_count = (
        int(refresh_checkpoint["cursor"].get("evidence_need_count", 0))
        if refresh_checkpoint is not None
        else 0
    )
    overall_status = (
        "RECOVERY_REQUIRED"
        if evidence_need_count > 0
        else str(refresh_checkpoint["status"])
        if refresh_checkpoint is not None
        else "READY"
        if release_checkpoint is not None
        else "NOT_READY"
    )
    _emit(
        {
            "status": overall_status,
            "refresh": refresh_checkpoint,
            "release": release_checkpoint,
        }
    )


if __name__ == "__main__":
    app()
