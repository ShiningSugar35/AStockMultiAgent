from __future__ import annotations

from datetime import UTC, datetime, timedelta

from hypothesis import given
from hypothesis import strategies as st

from astock.pit.temporal import TemporalValidityService, truncation_invariance_probe
from astock.schemas.temporal_validity import (
    TemporalAuditStatus,
    TemporalNonInterferenceRequest,
    TemporalOperationKind,
    TemporalPipelineNode,
)

DECISION = datetime(2026, 8, 18, 1, 0, tzinfo=UTC)


def _trailing_sum(values):
    running = 0
    output = []
    for value in values:
        running += value
        output.append(running)
    return output


@given(
    prefix=st.lists(st.integers(-1000, 1000), min_size=1, max_size=40),
    future=st.lists(st.integers(-1000, 1000), max_size=20),
)
def test_future_suffix_cannot_change_causal_prefix(prefix: list[int], future: list[int]) -> None:
    rows = [*prefix, *future]
    result = truncation_invariance_probe(rows, _trailing_sum, cutoffs=[len(prefix)])
    assert result.invariant
    assert result.drift_cutoffs == []


@given(offsets=st.lists(st.integers(0, 3600), min_size=1, max_size=50))
def test_value_independent_chain_before_decision_is_temporally_safe(offsets: list[int]) -> None:
    ordered_offsets = sorted(offsets, reverse=True)
    nodes = []
    previous: str | None = None
    for index, offset in enumerate(ordered_offsets):
        node_id = f"node-{index}"
        nodes.append(
            TemporalPipelineNode(
                node_id=node_id,
                operation_kind=(
                    TemporalOperationKind.SOURCE
                    if previous is None
                    else TemporalOperationKind.TRANSFORM
                ),
                dependency_ids=[] if previous is None else [previous],
                reference_time=DECISION - timedelta(seconds=offset + 1),
                available_at=DECISION - timedelta(seconds=offset),
            )
        )
        previous = node_id

    request = TemporalNonInterferenceRequest(
        pipeline_id="property:clean-chain",
        decision_time=DECISION,
        nodes=nodes,
        output_node_ids=[nodes[-1].node_id],
    )
    service = TemporalValidityService.__new__(TemporalValidityService)
    result = service.audit_non_interference(request, persist=False)
    assert result.status is TemporalAuditStatus.PASS


@given(delay_seconds=st.integers(1, 3600))
def test_future_dependency_always_taints_decision(delay_seconds: int) -> None:
    request = TemporalNonInterferenceRequest(
        pipeline_id="property:future-taint",
        decision_time=DECISION,
        nodes=[
            TemporalPipelineNode(
                node_id="future",
                operation_kind=TemporalOperationKind.SOURCE,
                dependency_ids=[],
                reference_time=DECISION,
                available_at=DECISION + timedelta(seconds=delay_seconds),
            ),
            TemporalPipelineNode(
                node_id="decision",
                operation_kind=TemporalOperationKind.DECISION,
                dependency_ids=["future"],
                reference_time=DECISION,
                available_at=DECISION,
            ),
        ],
        output_node_ids=["decision"],
    )
    service = TemporalValidityService.__new__(TemporalValidityService)
    result = service.audit_non_interference(request, persist=False)
    assert result.status is TemporalAuditStatus.FAIL
    assert "FUTURE_VISIBLE_AT_DECISION" in result.finding_codes
