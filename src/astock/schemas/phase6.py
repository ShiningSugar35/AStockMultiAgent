"""Durable public report for the recorded Phase 6 investment-decision closure."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import Field, model_validator

from astock.schemas.base import AStockModel
from astock.schemas.committee import TradeProtocolOutcome


class Phase6RunStatus(StrEnum):
    AWAITING_USER_CONFIRMATION = "AWAITING_USER_CONFIRMATION"
    PAPER_ORDER_CREATED = "PAPER_ORDER_CREATED"


class Phase6ClosureReport(AStockModel):
    schema_version: str = "phase6-closure-report-v1"
    run_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    company_id: str = Field(pattern=r"^\d{6}$")
    company_name: str = Field(min_length=1)
    data_mode: Literal["RECORDED_ACCEPTANCE"]
    status: Phase6RunStatus
    research_request_artifact_id: str = Field(min_length=1)
    frozen_evidence_pack_artifact_id: str = Field(min_length=1)
    base_case_artifact_id: str = Field(min_length=1)
    specialist_route_artifact_id: str = Field(min_length=1)
    specialist_delta_artifact_ids: dict[str, str]
    financial_integrity_artifact_id: str = Field(min_length=1)
    research_memo_artifact_id: str = Field(min_length=1)
    committee_decision_artifact_id: str = Field(min_length=1)
    trade_protocol_artifact_id: str = Field(min_length=1)
    paper_reference_pack_artifact_id: str = Field(min_length=1)
    trade_protocol_outcome: TradeProtocolOutcome
    requires_user_confirmation: Literal[True] = True
    broker_execution_allowed: Literal[False] = False
    paper_order_id: str | None = None
    input_object_hashes: list[str] = Field(min_length=1)
    disclaimer: str = Field(min_length=1)

    @model_validator(mode="after")
    def _validate_closure(self) -> Phase6ClosureReport:
        if set(self.specialist_delta_artifact_ids) != {"SERENITY", "ZHIHU_EXPERT"}:
            raise ValueError("Phase 6 report requires Serenity and Zhihu expert deltas")
        if self.input_object_hashes != sorted(set(self.input_object_hashes)):
            raise ValueError("Phase 6 input object hashes must be sorted and unique")
        if self.status is Phase6RunStatus.PAPER_ORDER_CREATED and not self.paper_order_id:
            raise ValueError("completed Phase 6 reports require a paper order id")
        if (
            self.status is Phase6RunStatus.AWAITING_USER_CONFIRMATION
            and self.paper_order_id is not None
        ):
            raise ValueError("unconfirmed Phase 6 reports cannot claim a paper order")
        return self


__all__ = ["Phase6ClosureReport", "Phase6RunStatus"]
