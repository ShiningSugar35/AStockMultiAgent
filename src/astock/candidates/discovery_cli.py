"""Standalone deterministic CLI for the skill-driven discovery runtime.

The normal chat/MCP agent owns semantic research and Web recovery. This module
provides the typed persistence boundary: register reviewed evidence-backed
condition claims, refresh verified theses, publish immutable discovery Seed
releases, and expose recovery needs. It never edits SQLite directly.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import date, datetime
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
from astock.schemas.financial import FinancialIndustryProfile
from astock.schemas.knowledge_completion import KnowledgeConditionState
from astock.settings import ProjectPaths

app = typer.Typer(add_completion=False, no_args_is_help=True)


def _services(*, read_only: bool = False) -> tuple[ProjectPaths, StateStore, ObjectStore]:
    paths = ProjectPaths.discover()
    if not read_only:
        paths.ensure_directories()
    state = StateStore(paths.state_db, paths.root / "migrations", read_only=read_only)
    if not read_only:
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
    from astock.candidates.registered_factor_diagnostics import (
        FactorFreezeRequest,
        FactorReportRequest,
    )
    from astock.candidates.registered_features import SupplementalFeatureRequest

    _emit(
        {
            "refresh": DiscoveryRefreshRequest.model_json_schema(),
            "admit": DiscoveryConditionClaimAdmissionRequest.model_json_schema(),
            "features_supplemental": SupplementalFeatureRequest.model_json_schema(),
            "factor_freeze": FactorFreezeRequest.model_json_schema(),
            "factor_report": FactorReportRequest.model_json_schema(),
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

    _paths, state, _objects = _services(read_only=True)
    if not state.path.is_file():
        _emit({"status": "NOT_READY", "refresh": None, "release": None})
        return
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



@app.command("features")
def features(
    company_id: str,
    period_end: str,
    as_of: str,
    industry_profile: FinancialIndustryProfile,
    business_group_id: str,
    supplemental_file: Annotated[
        Path | None, typer.Option(exists=True, dir_okay=False, readable=True),
    ] = None,
) -> None:
    """Explain local source-bound features without changing candidates, budgets or artifacts."""
    from astock.candidates.registered_features import (
        SupplementalFeatureRequest,
        build_registered_features,
    )
    from astock.financial_sources.service import FinancialSourceService
    from astock.financial_sources.storage import FinancialSourceParquetStore

    try:
        supplemental = (
            SupplementalFeatureRequest.model_validate_json(
                supplemental_file.read_text(encoding="utf-8")
            ) if supplemental_file is not None else None
        )
        paths, state, objects = _services(read_only=True)
        service = FinancialSourceService(
            state, objects, FinancialSourceParquetStore(paths.parquet / "financial_sources"),
            paths.root,
        )
        result = build_registered_features(
            service, company_id=company_id, period_end=date.fromisoformat(period_end),
            as_of=datetime.fromisoformat(as_of), industry_profile=industry_profile,
            business_group_id=business_group_id, supplemental=supplemental,
        )
    except (OSError, RuntimeError, ValidationError, ValueError) as exc:
        _emit({"status": "UNAVAILABLE", "failure_code": "CERTIFIED_FEATURE_INPUT_UNAVAILABLE"})
        raise typer.Exit(code=2) from exc
    _emit(result)



@app.command("factor-freeze")
def factor_freeze(
    request_file: Annotated[Path, typer.Argument(exists=True, dir_okay=False, readable=True)],
) -> None:
    """Freeze a prospective factor population from actual registered feature readers."""
    from astock.candidates.registered_factor_diagnostics import (
        FactorFreezeRequest,
        freeze_factor_panel,
    )
    from astock.financial_sources.service import FinancialSourceService
    from astock.financial_sources.storage import FinancialSourceParquetStore
    from astock.research.debate_evaluation import load_quality_policy

    try:
        paths = ProjectPaths.discover()
        policy = load_quality_policy(paths.root / "configs/research_quality.yaml")
        with request_file.open("rb") as handle:
            raw = handle.read(policy.max_request_bytes + 1)
        if len(raw) > policy.max_request_bytes:
            raise ValueError("factor request byte budget exceeded")
        request = FactorFreezeRequest.model_validate_json(raw)
        paths, state, objects = _services()
        service = FinancialSourceService(
            state, objects, FinancialSourceParquetStore(paths.parquet / "financial_sources"),
            paths.root,
        )
        result = freeze_factor_panel(service, request, policy)
    except (OSError, RuntimeError, ValidationError, ValueError) as exc:
        _emit({"status": "FAILED", "failure_code": "FACTOR_FREEZE_FAILED"})
        raise typer.Exit(code=2) from exc
    _emit(result)


@app.command("factor-report")
def factor_report(
    request_file: Annotated[Path, typer.Argument(exists=True, dir_okay=False, readable=True)],
) -> None:
    """Read frozen factors and canonical outcomes without changing research or accounts."""
    from astock.candidates.registered_factor_diagnostics import (
        FactorReportRequest,
        evaluate_registered_factor_panels,
    )
    from astock.market_data.official_calendar import load_official_trading_calendar
    from astock.research.debate_evaluation import QualityError, load_quality_policy

    try:
        paths, state, objects = _services(read_only=True)
        policy = load_quality_policy(paths.root / "configs/research_quality.yaml")
        with request_file.open("rb") as handle:
            raw = handle.read(policy.max_request_bytes + 1)
        if len(raw) > policy.max_request_bytes:
            raise ValueError("factor request byte budget exceeded")
        request = FactorReportRequest.model_validate_json(raw)
        if not state.path.is_file():
            _emit({"status": "NOT_READY", "persistent_writes": False})
            return
        result = evaluate_registered_factor_panels(
            state, objects, paths.parquet,
            load_official_trading_calendar(paths.root / "configs/official_trading_calendar.yaml"),
            policy, request,
        )
    except (OSError, RuntimeError, ValidationError, ValueError, QualityError) as exc:
        _emit({"status": "UNAVAILABLE", "failure_code": "FACTOR_REPORT_INPUT_UNAVAILABLE"})
        raise typer.Exit(code=2) from exc
    _emit(result)


if __name__ == "__main__":
    app()
