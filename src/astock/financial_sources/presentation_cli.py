"""CHAT_ORCHESTRATED financial semantic task entrypoints; no model API or parallel data store."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Annotated, Any

import typer
from pydantic import ValidationError

from astock.financial_sources.presentation import FinancialPresentationService
from astock.financial_sources.service import FinancialSourceService
from astock.financial_sources.storage import FinancialSourceParquetStore
from astock.schemas import FinancialPeriodType, Market
from astock.schemas.financial_presentation import (
    FinancialPresentationProposal,
    FinancialPresentationReview,
    FinancialPresentationRulePreview,
    FinancialStandardAdmission,
)
from astock.settings import ProjectPaths


def register_financial_presentation_commands(
    app: typer.Typer,
    services: Callable[[], tuple[ProjectPaths, Any, Any]],
    emit: Callable[[Any], None],
) -> None:
    def _source() -> FinancialPresentationService:
        paths, state, objects = services()
        return FinancialPresentationService(
            FinancialSourceService(
                state, objects,
                FinancialSourceParquetStore(paths.parquet / "financial_sources"),
                paths.root,
            )
        )

    @app.command("financial-presentation-schema")
    def financial_presentation_schema() -> None:
        """Read LLM contract; advisory proposal does not certify a financial number."""
        emit({
            "proposal": FinancialPresentationProposal.model_json_schema(),
            "review": FinancialPresentationReview.model_json_schema(),
            "rule_preview": FinancialPresentationRulePreview.model_json_schema(),
            "production_writes": False,
        })

    @app.command("financial-presentation-context")
    def financial_presentation_context(
        company_id: Annotated[str, typer.Argument(help="Six-digit issuer code.")],
        period_end: Annotated[str, typer.Option(help="Official reporting period YYYY-MM-DD.")],
        period_type: Annotated[FinancialPeriodType, typer.Option(case_sensitive=False)] = (
            FinancialPeriodType.QUARTERLY
        ),
        as_of: Annotated[str | None, typer.Option(help="Aware ISO PIT cutoff.")] = None,
        page: Annotated[
            list[int] | None, typer.Option("--page", help="Exact official page number.")
        ] = None,
        max_pages: Annotated[int, typer.Option(min=1, max=12)] = 8,
        market: Annotated[Market | None, typer.Option(case_sensitive=False)] = None,
        allow_capture: Annotated[bool, typer.Option("--allow-capture")] = False,
    ) -> None:
        """Provide certified text, or a clearly uncertified, admitted offline PDF capture."""
        try:
            timestamp = datetime.fromisoformat(as_of) if as_of else datetime.now(UTC)
            if timestamp.utcoffset() is None:
                raise ValueError("as_of must be timezone aware")
            result = _source().context(
                company_id, date.fromisoformat(period_end), period_type, as_of=timestamp,
                page_numbers=page, max_pages=max_pages,
                market=market, allow_uncertified_capture=allow_capture,
            )
        except (OSError, RuntimeError, ValueError, ValidationError) as exc:
            emit({"status": "INCONCLUSIVE", "failure_code": "PRESENTATION_CONTEXT_NOT_VERIFIED"})
            raise typer.Exit(code=3) from exc
        emit(result)

    @app.command("financial-presentation-review")
    def financial_presentation_review(
        proposal_file: Annotated[
            Path, typer.Argument(exists=True, file_okay=True, dir_okay=False, readable=True),
        ],
        persist: Annotated[bool, typer.Option("--persist")] = False,
    ) -> None:
        """Check model quote anchors and official rows; optionally register shadow artifacts."""
        try:
            proposal = FinancialPresentationProposal.model_validate_json(
                proposal_file.read_bytes(),
            )
            result = _source().review(proposal, persist=persist)
        except (OSError, RuntimeError, ValueError, ValidationError) as exc:
            emit({"status": "REJECTED", "failure_code": "PRESENTATION_PROPOSAL_INVALID"})
            raise typer.Exit(code=2) from exc
        emit(result)
        if result.status.value == "REJECTED":
            raise typer.Exit(code=3)


    @app.command("financial-presentation-rule-preview")
    def financial_presentation_rule_preview(
        proposal_file: Annotated[
            Path, typer.Argument(exists=True, file_okay=True, dir_okay=False, readable=True),
        ],
    ) -> None:
        """Preview rule applicability from a cited LLM claim; NEVER apply it."""
        try:
            proposal = FinancialPresentationProposal.model_validate_json(
                proposal_file.read_bytes(),
            )
            preview = _source().rule_preview(proposal)
        except (OSError, RuntimeError, ValueError, ValidationError) as exc:
            emit({"status": "INCONCLUSIVE", "failure_code": "PRESENTATION_PREVIEW_INVALID"})
            raise typer.Exit(code=3) from exc
        emit(preview)



    @app.command("financial-presentation-admit")
    def financial_presentation_admit(
        proposal_file: Annotated[
            Path, typer.Argument(exists=True, file_okay=True, dir_okay=False, readable=True),
        ],
        reviewed_by: Annotated[str, typer.Option("--reviewed-by")],
        review_note: Annotated[str, typer.Option("--review-note")],
        approve: Annotated[bool, typer.Option("--approve")] = False,
    ) -> None:
        """Approve a verified issuer-specific adoption for production rule selection."""
        try:
            proposal = FinancialPresentationProposal.model_validate_json(
                proposal_file.read_bytes()
            )
            admission_id, admitted = _source().admit_standard(
                proposal,
                independently_reviewed_by=reviewed_by,
                independent_review_note=review_note,
                approve=approve,
            )
        except (OSError, RuntimeError, ValueError, ValidationError) as exc:
            emit({"status": "REJECTED", "failure_code": "STANDARD_ADOPTION_NOT_ADMITTED"})
            raise typer.Exit(code=3) from exc
        emit({
            "status": "ACTIVE_EVIDENCE_GATED",
            "admission_id": admission_id,
            "standard_policy": admitted.policy_id,
            "industry_profile": admitted.industry_profile.value,
            "financial_certification_granted": False,
            "portfolio_permission_granted": False,
        })

    @app.command("financial-presentation-production-audit")
    def financial_presentation_production_audit(
        admission_id: Annotated[str, typer.Argument()],
        as_of: Annotated[str | None, typer.Option(help="Aware ISO PIT cutoff.")] = None,
    ) -> None:
        """Run existing deterministic financial audit with approved standard context."""
        try:
            service = _source()
            recorded = service.state.artifact_record(admission_id)
            if recorded is None or recorded["type"] != "FinancialStandardAdmission":
                raise ValueError("unknown admitted issuer standard")
            admission = FinancialStandardAdmission.model_validate_json(
                service.objects.get_bytes(str(recorded["object_hash"]))
            )
            timestamp = datetime.fromisoformat(as_of) if as_of else datetime.now(UTC)
            if timestamp.utcoffset() is None:
                raise ValueError("as_of must have a timezone")
            pack = service.sources.run_audit(
                admission.company_id,
                admission.period_end,
                admission.period_type,
                as_of=timestamp,
                industry_profile=admission.industry_profile,
                standard_admission_id=admission_id,
            )
        except (OSError, RuntimeError, ValueError, ValidationError) as exc:
            emit({"status": "REJECTED", "failure_code": "ADMITTED_FINANCIAL_AUDIT_FAILED"})
            raise typer.Exit(code=3) from exc
        emit({
            "schema_version": "financial-presentation-production-audit-v1",
            "standard_admission_id": admission_id,
            "pack": pack,
        })

__all__ = ["register_financial_presentation_commands"]
