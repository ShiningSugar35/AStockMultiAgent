"""Deterministic evidence and composition policy for Serenity method contracts."""

from __future__ import annotations

from datetime import datetime

from astock.core.state import StateStore
from astock.evidence.repository import EvidenceRepository
from astock.schemas import (
    EvidenceConflict,
    EvidenceGrade,
    FactStatus,
    FrozenEvidencePack,
    PointInTimeStatus,
)
from astock.schemas.serenity_v2 import (
    DailyTrendHealthContractV2,
    EventToAlphaContractV2,
    GrowthProbabilityContractV2,
    GrowthValuationContractV2,
    IndustryBottleneckContractV2,
    JuglarCycleDimension,
    JuglarCycleStageContractV1,
    SerenityMethodContractV2,
)

_EVIDENCE_GRADE_STRENGTH = {
    EvidenceGrade.COMMUNITY_LEAD: 0,
    EvidenceGrade.SECONDARY: 1,
    EvidenceGrade.PRIVATE_PRIMARY: 2,
    EvidenceGrade.PRIMARY_OFFICIAL: 3,
}


def validate_serenity_method_evidence(
    state: StateStore,
    evidence_repository: EvidenceRepository,
    method_contract: SerenityMethodContractV2,
    *,
    evidence_pack: FrozenEvidencePack,
    base_as_of: datetime,
) -> None:
    """Apply the common frozen-scope, grade, PIT and conflict gates to one method contract."""

    evidence_requirements = serenity_method_evidence_requirements(method_contract)
    if not evidence_requirements:
        raise ValueError("Serenity method contract requires evidence on every method node")

    evidence_scope = set(evidence_pack.evidence_ids)
    open_conflict_evidence = _open_conflict_evidence(state, evidence_pack.open_conflict_ids)
    for node_evidence, required_grade, role in evidence_requirements:
        unknown = sorted(set(node_evidence) - evidence_scope)
        if unknown:
            raise ValueError("Serenity method evidence references evidence outside the frozen pack")
        for evidence_id in node_evidence:
            pit_status = evidence_pack.pit_status_by_evidence_id.get(evidence_id)
            if pit_status not in {
                PointInTimeStatus.CERTIFIED,
                PointInTimeStatus.DOCUMENT_RECONSTRUCTED,
            }:
                raise ValueError("Serenity method evidence requires certified or reconstructed PIT")
            grade = evidence_pack.evidence_grade_by_id.get(evidence_id)
            if grade is None:
                raise ValueError("Serenity method evidence grade is unavailable")
            if _EVIDENCE_GRADE_STRENGTH[grade] < _EVIDENCE_GRADE_STRENGTH[required_grade]:
                raise ValueError(
                    f"Serenity method evidence for {role} requires {required_grade.value}"
                )
            evidence = evidence_repository.get_evidence(evidence_id)
            if evidence is None:
                raise ValueError("Serenity method evidence record is unavailable")
            if evidence.available_to_system_at > base_as_of:
                raise ValueError("Serenity method evidence is future relative to the BaseCase")
            if evidence.valid_from is not None and evidence.valid_from > base_as_of:
                raise ValueError("Serenity method evidence is not yet valid at the BaseCase as_of")
            if evidence.valid_to is not None and evidence.valid_to < base_as_of:
                raise ValueError("Serenity method evidence is stale at the BaseCase as_of")
            if evidence.fact_status in {FactStatus.CONFLICTED, FactStatus.UNVERIFIED}:
                raise ValueError("Serenity method evidence cannot be conflicted or unverified")
            if evidence_id in open_conflict_evidence:
                raise ValueError("Serenity method evidence cannot participate in an open conflict")


def serenity_method_evidence_requirements(
    contract: SerenityMethodContractV2,
) -> list[tuple[list[str], EvidenceGrade, str]]:
    """Return the minimum evidence grade for every typed Serenity method-node role."""

    official = EvidenceGrade.PRIMARY_OFFICIAL
    secondary = EvidenceGrade.SECONDARY

    if isinstance(contract, IndustryBottleneckContractV2):
        nodes = [
            contract.system_change,
            *contract.chain_nodes,
            contract.candidate_universe,
            contract.necessary_link,
            *contract.scarcity,
            *contract.substitutions,
            *contract.value_capture,
            *contract.invalidation_conditions,
        ]
        return [(node.evidence_ids, official, "industry method node") for node in nodes]

    if isinstance(contract, EventToAlphaContractV2):
        official_nodes = [
            contract.event,
            contract.business_purity,
            *contract.transmission_steps,
            contract.scale_elasticity,
            *contract.validation_checkpoints,
            contract.falsifier,
        ]
        requirements = [
            (node.evidence_ids, official, "event fact/transmission node") for node in official_nodes
        ]
        if contract.market_misclassification is not None:
            requirements.append(
                (
                    contract.market_misclassification.evidence_ids,
                    secondary,
                    "event market-misclassification node",
                )
            )
        return requirements

    if isinstance(contract, GrowthProbabilityContractV2):
        method_input = contract.input
        requirements = [
            (node.evidence_ids, official, "growth hypothesis/likelihood node")
            for node in (*method_input.hypotheses, *method_input.likelihood_updates)
        ]
        requirements.append(
            (method_input.prior_basis.evidence_ids, secondary, "growth prior basis")
        )
        if method_input.consensus is not None:
            requirements.append(
                (method_input.consensus.evidence_ids, secondary, "growth consensus")
            )
        return requirements

    if isinstance(contract, GrowthValuationContractV2):
        requirements = [
            (node.evidence_ids, official, "valuation quality factor")
            for node in contract.quality_factors
        ]
        if contract.tam_runway is not None:
            requirements.append((contract.tam_runway.evidence_ids, official, "valuation TAM"))
        if contract.peg is not None:
            requirements.append((contract.peg.evidence_ids, secondary, "valuation PEG"))
        if contract.consensus is not None:
            requirements.append((contract.consensus.evidence_ids, secondary, "valuation consensus"))
        return requirements

    if isinstance(contract, DailyTrendHealthContractV2):
        return [
            (contract.daily_series.evidence_ids, secondary, "daily series"),
            *(
                (node.evidence_ids, secondary, "daily moving average")
                for node in contract.moving_averages
            ),
            *(
                (node.evidence_ids, official, "daily fundamental growth")
                for node in contract.fundamental_growth
            ),
            *(
                (node.evidence_ids, secondary, "daily estimate revision")
                for node in contract.estimate_revisions
            ),
        ]

    if isinstance(contract, JuglarCycleStageContractV1):
        requirements = [
            (
                node.evidence_ids,
                secondary
                if node.dimension is JuglarCycleDimension.CAPITAL_MARKET_REACTION
                else official,
                f"Juglar dimension {node.dimension.value}",
            )
            for node in contract.dimension_scores
        ]
        requirements.extend(
            (node.evidence_ids, official, "Juglar counter-evidence")
            for node in contract.counterevidence
        )
        requirements.extend(
            (node.evidence_ids, official, "Juglar migration signal")
            for node in contract.migration_signals
        )
        return requirements

    raise ValueError("unsupported Serenity v2 method contract")


def _open_conflict_evidence(state: StateStore, conflict_ids: list[str]) -> set[str]:
    if not conflict_ids:
        return set()
    placeholders = ",".join("?" for _ in conflict_ids)
    with state.connect() as connection:
        rows = connection.execute(
            f"SELECT conflict_json FROM evidence_conflict WHERE conflict_id IN ({placeholders})",
            conflict_ids,
        ).fetchall()
    if len(rows) != len(conflict_ids):
        raise ValueError("Serenity method frozen evidence conflict record is unavailable")
    return {
        evidence_id
        for row in rows
        for evidence_id in EvidenceConflict.model_validate_json(row["conflict_json"]).evidence_ids
    }


__all__ = ["serenity_method_evidence_requirements", "validate_serenity_method_evidence"]
