from __future__ import annotations

import logging
from datetime import datetime

from astock.core.errors import StorageError
from astock.investor_orchestration.models import (
    CapabilityCoverageReceipt,
    InvestorAnswer,
    InvestorAnswerDraft,
    InvestorSessionPreflightReceipt,
)
from astock.investor_orchestration.output_validation import RegisteredOutputVerifier
from astock.investor_orchestration.store import InvestorOrchestrationStore
from astock.investor_orchestration.utils import content_hash, utc_now
from astock.research.capital_privacy import CapitalDisclosureError, capital_disclosure_findings
from astock.research.presentation import audit_public_answer

_LOG = logging.getLogger(__name__)


class InvestorDecisionAssembler:
    """Read a frozen answer bound to the exact verified decision inputs.

    This adapter deliberately has no free-text-to-admitted-answer write API. A
    domain's validated research exporter must register its immutable draft with
    the preflight and coverage hashes as inputs before public presentation.
    """

    def __init__(self, verifier: RegisteredOutputVerifier) -> None:
        self.verifier = verifier

    def assemble(
        self,
        artifact_id: str,
        preflight: InvestorSessionPreflightReceipt,
        coverage: CapabilityCoverageReceipt,
    ) -> InvestorAnswerDraft:
        record = self.verifier.state.artifact_record(artifact_id)
        if record is None:
            raise ValueError("answer is not registered")
        inputs = record["input_hashes"]
        if not {preflight.receipt_hash, coverage.receipt_hash}.issubset(set(inputs)):
            raise ValueError("answer is not bound to the verified decision inputs")
        loaded = self.verifier.load(artifact_id, InvestorAnswerDraft)
        draft = InvestorAnswerDraft.model_validate(loaded.model_dump())
        if draft.request_id != preflight.request_id:
            raise ValueError("answer belongs to another request")
        from astock.investor_orchestration.answer_projection import VerifiedAnswerProjector

        projector = VerifiedAnswerProjector(self.verifier)
        verified_inputs = projector.inputs(preflight, coverage)
        if not set(verified_inputs.input_hashes).issubset(set(inputs)):
            raise ValueError("answer does not bind every verified source object")
        if projector._derive(verified_inputs) != draft:
            raise ValueError("registered answer differs from its verified domain projection")
        return draft


class InvestorAnswerGateway:
    """Fail closed across the entire public response, not just its headline."""

    def __init__(self, store: InvestorOrchestrationStore | None = None) -> None:
        self.verifier = RegisteredOutputVerifier(store) if store is not None else None

    def publish_registered(
        self, *, draft_artifact_id: str, coverage_receipt_id: str
    ) -> InvestorAnswer:
        """Publish a completed frozen request without rerunning research or trades."""
        if self.verifier is None:
            raise ValueError("public output requires the canonical stores")
        with self.verifier.store.connect() as connection:
            row = connection.execute(
                "SELECT payload_json FROM capability_coverage_receipts WHERE receipt_id=?",
                (coverage_receipt_id,),
            ).fetchone()
        if row is None:
            raise ValueError("coverage receipt is unavailable")
        coverage = CapabilityCoverageReceipt.model_validate_json(str(row[0]))
        preflight = self.verifier.store.get_preflight_for_request(coverage.request_id)
        if preflight is None:
            raise ValueError("request preflight is unavailable")
        draft = InvestorDecisionAssembler(self.verifier).assemble(
            draft_artifact_id, preflight, coverage
        )
        return self.render(
            draft,
            preflight=preflight,
            coverage=coverage,
            draft_artifact_id=draft_artifact_id,
        )

    def publish_verified(self, *, coverage_receipt_id: str) -> InvestorAnswer:
        """Derive the public answer from source artifacts; no free-form draft input."""
        from astock.investor_orchestration.answer_projection import VerifiedAnswerProjector

        if self.verifier is None:
            raise ValueError("public output requires the canonical stores")
        with self.verifier.store.connect() as connection:
            row = connection.execute(
                "SELECT payload_json FROM capability_coverage_receipts WHERE receipt_id=?",
                (coverage_receipt_id,),
            ).fetchone()
        if row is None:
            raise ValueError("coverage receipt is unavailable")
        coverage = CapabilityCoverageReceipt.model_validate_json(str(row[0]))
        preflight = self.verifier.store.get_preflight_for_request(coverage.request_id)
        if preflight is None:
            raise ValueError("request preflight is unavailable")
        self._aware_not_future(preflight.as_of, utc_now())
        try:
            artifact_id, draft = VerifiedAnswerProjector(self.verifier).freeze(preflight, coverage)
        except (ValueError, OSError, StorageError) as exc:
            _LOG.warning("verified answer projection rejected: %s", type(exc).__name__)
            fallback = InvestorAnswerDraft(
                request_id=coverage.request_id,
                conclusion="关键资料尚未核实完整。",
                reasons=(),
                risks=(),
                actions=(),
                change_conditions=(),
                evidence_as_of=preflight.as_of,
            )
            return self._safe_answer(
                fallback, preflight, privacy_blocked=isinstance(exc, CapitalDisclosureError)
            )
        return self.render(
            draft,
            preflight=preflight,
            coverage=coverage,
            draft_artifact_id=artifact_id,
        )

    def _authenticated_inputs(
        self,
        preflight: InvestorSessionPreflightReceipt,
        coverage: CapabilityCoverageReceipt,
    ) -> bool:
        if self.verifier is None:
            return False
        try:
            return self.verifier.authenticated_preflight(preflight) and (
                self.verifier.authenticated_coverage(
                    preflight.request_id, coverage.receipt_id, coverage.receipt_hash
                )
            )
        except (ValueError, OSError, StorageError):
            return False

    @staticmethod
    def _aware_not_future(value: datetime, now: datetime) -> None:
        if value.tzinfo is None or value.utcoffset() is None or value > now:
            raise ValueError("public evidence time is ambiguous or in the future")

    def render(
        self,
        draft: InvestorAnswerDraft,
        *,
        preflight: InvestorSessionPreflightReceipt,
        coverage: CapabilityCoverageReceipt,
        draft_artifact_id: str | None = None,
    ) -> InvestorAnswer:
        # Frozen Pydantic objects can still be copied with unvalidated updates.
        draft = InvestorAnswerDraft.model_validate(draft.model_dump())
        preflight = InvestorSessionPreflightReceipt.model_validate(preflight.model_dump())
        coverage = CapabilityCoverageReceipt.model_validate(coverage.model_dump())
        if draft.request_id != preflight.request_id or draft.request_id != coverage.request_id:
            raise ValueError("draft, preflight and coverage request ids do not match")
        now = utc_now()
        self._aware_not_future(draft.evidence_as_of, now)
        self._aware_not_future(preflight.as_of, now)
        if draft.evidence_as_of > preflight.as_of:
            raise ValueError("answer evidence is later than the frozen decision time")
        if coverage.prohibited_call_count:
            raise ValueError("prohibited capability call reached public gateway")

        verified = (
            preflight.freshness == "FRESH"
            and coverage.coverage_complete
            and coverage.outputs_verified
            and not coverage.unresolved_conflicts
            and coverage.preflight_receipt_id == preflight.receipt_id
            and self._authenticated_inputs(preflight, coverage)
        )
        if (
            content_hash(coverage.model_dump(exclude={"receipt_id", "receipt_hash"}))
            != coverage.receipt_hash
        ):
            verified = False
        if verified and draft_artifact_id is not None and self.verifier is not None:
            try:
                frozen = InvestorDecisionAssembler(self.verifier).assemble(
                    draft_artifact_id, preflight, coverage
                )
                verified = frozen == draft
            except (ValueError, OSError, StorageError):
                verified = False
        else:
            verified = False
        if not verified:
            return self._safe_answer(draft, preflight)

        actual_section = (
            draft.actual_holding_section if preflight.context.actual.positions else None
        )
        paper_section = draft.paper_holding_section if preflight.context.paper.positions else None
        public_parts = [
            draft.conclusion,
            *draft.reasons,
            *draft.risks,
            *draft.actions,
            *draft.change_conditions,
            actual_section or "",
            paper_section or "",
        ]
        audit = audit_public_answer("\n".join(part for part in public_parts if part))
        if not audit.safe_to_send:
            _LOG.warning("investor output rejected: %s", audit.finding_codes)
            return self._safe_answer(draft, preflight)
        return InvestorAnswer(
            request_id=draft.request_id,
            conclusion=draft.conclusion,
            reasons=draft.reasons,
            risks=draft.risks,
            actions=draft.actions,
            change_conditions=draft.change_conditions,
            actual_holding_section=actual_section,
            paper_holding_section=paper_section,
            evidence_as_of=draft.evidence_as_of,
        )

    @staticmethod
    def _safe_answer(
        draft: InvestorAnswerDraft,
        preflight: InvestorSessionPreflightReceipt,
        *,
        privacy_blocked: bool = False,
    ) -> InvestorAnswer:
        visible = "\n".join(
            (
                draft.conclusion, *draft.reasons, *draft.risks, *draft.actions,
                *draft.change_conditions, draft.actual_holding_section or "",
                draft.paper_holding_section or "",
            )
        )
        if privacy_blocked or capital_disclosure_findings(visible):
            return InvestorAnswer(
                request_id=draft.request_id,
                conclusion="为保护账户隐私，本次暂不展示涉及账户信息的投资建议。",
                reasons=("对外展示采用仓位、收益和风险比例，不公开账户金额或持仓数量。",),
                risks=("尚未通过核验的投资结论不能作为买卖依据。",),
                actions=(),
                change_conditions=("完成隐私处理与必要核验后再展示投资建议。",),
                evidence_as_of=min(draft.evidence_as_of, preflight.as_of),
                degraded=True,
                degradation_reason="账户信息需要隐藏后再展示。",
            )
        # Never retain unchecked directions, numbers, dates or credentials from
        # any draft field in the fallback. Diagnostics stay out of the public model.
        return InvestorAnswer(
            request_id=draft.request_id,
            conclusion="现有材料尚不足以支持可靠的买卖或仓位判断，暂不形成正式结论。",
            reasons=("需要先核实会影响结论的关键事实及其来源。",),
            risks=("资料不完整时，具体价格和调整建议可能误导决策。",),
            actions=("核实关键事实后重新评估。",),
            change_conditions=("取得可核验的新证据，并完成相应研究。",),
            evidence_as_of=min(draft.evidence_as_of, preflight.as_of),
            degraded=True,
            degradation_reason="关键资料尚未核实完整。",
        )
