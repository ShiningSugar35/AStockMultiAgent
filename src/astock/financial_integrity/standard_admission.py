"""Verify issuer-specific standard adoption for deterministic financial audits."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from astock.core.hashing import content_hash
from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.financial_integrity.standard_policies import load_financial_standard_policies
from astock.schemas.financial import FinancialAuditRequest, FinancialRuleRegistry
from astock.schemas.financial_presentation import (
    AdoptionClaim,
    FinancialPresentationProposal,
    FinancialPresentationReview,
    FinancialStandardAdmission,
    PresentationReviewStatus,
    PresentationSourceMode,
)


def verify_standard_admission(
    state: StateStore,
    objects: ObjectStore,
    financial_rules: FinancialRuleRegistry,
    standards_path: Path,
    request: FinancialAuditRequest,
) -> FinancialStandardAdmission | None:
    """Only an immutable independently approved record can select live context.

    An unreviewed LLM response, shadow preview or approval created after a
    historical PIT cutoff has no authority. All numerical checks stay intact.
    """
    identifier = request.standard_admission_id
    if identifier is None:
        return None
    row = state.artifact_record(identifier)
    if (
        row is None
        or row["type"] != "FinancialStandardAdmission"
        or row["schema_version"] != "financial-standard-admission-v1"
    ):
        raise ValueError("unrecognized financial standard production admission")
    admission = FinancialStandardAdmission.model_validate_json(
        objects.get_bytes(str(row["object_hash"]))
    )
    identity = {
        "proposal_hash": admission.proposal_hash,
        "policy_id": admission.policy_id,
        "independent_reviewer": admission.independently_reviewed_by,
        "attestation": admission.independent_review_note,
    }
    if identifier != f"financial-standard-admission:{content_hash(identity)}":
        raise ValueError("financial standard approval identity mismatch")
    if row["input_hashes"] != [
        admission.official_source_hash,
        admission.proposal_hash,
        admission.review_hash,
        admission.policy_registry_version,
    ]:
        raise ValueError("financial standard approval lineage mismatch")
    # Approval is not sufficient without its original frozen model proposal
    # and the actual independent review artifact. Validate both against the
    # same issuer, source, period, source hash and registered object lineage.
    proposal_row = state.artifact_record(
        f"financial-presentation-proposal:{admission.proposal_hash}"
    )
    review_row = state.artifact_record(admission.review_artifact_id)
    if (
        proposal_row is None or review_row is None
        or proposal_row["type"] != "FinancialPresentationProposal"
        or review_row["type"] != "FinancialPresentationReview"
    ):
        raise ValueError("financial standard admission proposal/review artifact is missing")
    proposal = FinancialPresentationProposal.model_validate_json(
        objects.get_bytes(str(proposal_row["object_hash"]))
    )
    review = FinancialPresentationReview.model_validate_json(
        objects.get_bytes(str(review_row["object_hash"]))
    )
    if (
        proposal_row["input_hashes"] != [admission.official_source_hash]
        or review_row["input_hashes"] != [
            proposal_row["object_hash"], admission.official_source_hash,
        ]
        or content_hash(proposal.model_dump(
            mode="json", exclude={"created_at"}, exclude_none=False,
        )) != admission.proposal_hash
        or content_hash(review.model_dump(mode="json"))
        != admission.review_artifact_id.removeprefix("financial-presentation-review:")
        or content_hash(review.model_dump(mode="json", exclude={"created_at"}))
        != admission.review_hash
        or review.proposal_hash != admission.proposal_hash
        or review.release_id != admission.financial_release_id
        or review.status is not PresentationReviewStatus.INCONCLUSIVE
        or review.reason_codes != ["ADOPTION_SEMANTICS_REQUIRE_INDEPENDENT_REVIEW"]
        or any(item.status is not PresentationReviewStatus.EVIDENCE_BOUND
               for item in review.line_reviews)
        or proposal.source_mode is not PresentationSourceMode.CERTIFIED_RELEASE
        or proposal.adoption is not AdoptionClaim.CONFIRMED
        or proposal.release_id != admission.financial_release_id
        or proposal.company_id != admission.company_id
        or proposal.instrument_id != admission.instrument_id
        or proposal.official_document_id != admission.official_document_id
        or proposal.official_snapshot_id != admission.official_snapshot_id
        or proposal.official_source_hash != admission.official_source_hash
        or proposal.period_end != admission.period_end
        or proposal.period_type is not admission.period_type
        or proposal.standard_version_candidate != admission.accounting_standard_version
        or proposal.framework is not admission.framework
        or proposal.industry_profile_candidate is not admission.industry_profile
        or proposal.adoption_effective_date != admission.adoption_effective_date
        or not proposal.adoption_evidence
    ):
        raise ValueError("financial standard admission independent review lineage mismatch")
    if (
        admission.company_id != request.company_id
        or admission.industry_profile is not request.industry_profile
        or admission.rule_registry_version != financial_rules.registry_version
        or admission.admitted_at > request.as_of
        or not any(fact.period_end == admission.period_end for fact in request.facts)
    ):
        raise ValueError("financial standard approval audit scope/PIT mismatch")
    snapshot = state.get_snapshot(admission.official_snapshot_id)
    if (
        snapshot is None
        or snapshot.object_sha256 != admission.official_source_hash
        or snapshot.available_to_system_at > request.as_of
        or not objects.verify(admission.official_source_hash)
    ):
        raise ValueError("financial standard approval raw source is invalid")
    with state.connect() as connection:
        release = connection.execute(
            "SELECT company_id,instrument_id,period_end,period_type,"
            "official_document_id,official_snapshot_id,status,available_to_system_at "
            "FROM financial_source_release WHERE release_id=?",
            (admission.financial_release_id,),
        ).fetchone()
    if (
        release is None
        or release["company_id"] != admission.company_id
        or release["instrument_id"] != admission.instrument_id
        or release["period_end"] != admission.period_end.isoformat()
        or release["period_type"] != admission.period_type.value
        or release["official_document_id"] != admission.official_document_id
        or release["official_snapshot_id"] != admission.official_snapshot_id
        or release["status"] != "CERTIFIED"
        or datetime.fromisoformat(str(release["available_to_system_at"])) > request.as_of
    ):
        raise ValueError("financial standard approval is not bound to certified release")
    registry = load_financial_standard_policies(standards_path, financial_rules)
    policy = next(
        (p for p in registry.profiles if p.standard_id == admission.policy_id), None
    )
    if (
        not registry.production_enabled
        or policy is None
        or not policy.eligible_for_certified_a_share
        or registry.registry_version != admission.policy_registry_version
        or policy.framework is not admission.framework
        or policy.standard_version != admission.accounting_standard_version
        or admission.adoption_effective_date < policy.first_possible_adoption
    ):
        raise ValueError("financial standard production policy is ineligible")
    return admission


__all__ = ["verify_standard_admission"]
