"""Fail-closed financial coverage policy for research-candidate admission."""

from __future__ import annotations

from astock.schemas.financial import (
    FinancialCoverageStatus,
    FinancialFieldCode,
    FinancialFindingStatus,
    FinancialIntegrityEvidencePack,
    FinancialRiskLevel,
    FinancialSeverity,
)
from astock.schemas.runs import RunStatus

_CORE_RESEARCH_FIELDS = frozenset(
    {
        FinancialFieldCode.TOTAL_ASSETS,
        FinancialFieldCode.TOTAL_LIABILITIES,
        FinancialFieldCode.TOTAL_EQUITY,
        FinancialFieldCode.REVENUE,
        FinancialFieldCode.NET_PROFIT_INCOME,
        FinancialFieldCode.NET_CASH_OPERATING,
    }
)
_MATERIAL_SEVERITIES = {FinancialSeverity.MEDIUM, FinancialSeverity.HIGH}


def financial_pack_is_candidate_eligible(pack: FinancialIntegrityEvidencePack) -> bool:
    """Allow safe partial packs into research only; never upgrade their typed coverage."""

    if (
        pack.status is RunStatus.SUCCEEDED
        and pack.coverage_status is FinancialCoverageStatus.COMPLETE
    ):
        return True
    if (
        pack.status is not RunStatus.NEEDS_INFO
        or pack.coverage_status is not FinancialCoverageStatus.PARTIAL
        or pack.risk_level is not FinancialRiskLevel.LOW
    ):
        return False

    verified_fields = {item.field_code for item in pack.verified_numbers}
    if not _CORE_RESEARCH_FIELDS.issubset(verified_fields):
        return False
    if pack.document_conflicts:
        return False
    if any(
        finding.status is FinancialFindingStatus.CONFLICTED
        or (
            finding.status is FinancialFindingStatus.FLAG
            and finding.severity in _MATERIAL_SEVERITIES
        )
        for finding in [*pack.rule_findings, *pack.governance_findings]
    ):
        return False
    if any(
        anomaly.is_anomaly and anomaly.severity in _MATERIAL_SEVERITIES
        for anomaly in [*pack.time_series_anomalies, *pack.peer_anomalies]
    ):
        return False
    material_gap_fields = {
        field_code
        for gap in pack.evidence_gaps
        for field_code in gap.field_codes
        if field_code in _CORE_RESEARCH_FIELDS
    }
    return not material_gap_fields


__all__ = ["financial_pack_is_candidate_eligible"]
