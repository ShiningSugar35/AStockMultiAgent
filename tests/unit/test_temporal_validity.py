from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.pit.temporal import TemporalValidityService, truncation_invariance_probe
from astock.schemas.temporal_validity import (
    KnowledgeCutoffAlphaPeriod,
    KnowledgeCutoffDiagnosticRequest,
    KnowledgeCutoffDiagnosticStatus,
    TemporalAuditStatus,
    TemporalNonInterferenceRequest,
    TemporalOperationKind,
    TemporalPipelineNode,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DECISION = datetime(2026, 8, 18, 1, 0, tzinfo=UTC)


def _service(tmp_path: Path) -> TemporalValidityService:
    state = StateStore(tmp_path / "state.sqlite", PROJECT_ROOT / "migrations")
    state.migrate()
    return TemporalValidityService(state, ObjectStore(tmp_path / "objects"))


def _node(
    node_id: str,
    *,
    available_at: datetime,
    dependencies: list[str] | None = None,
    kind: TemporalOperationKind = TemporalOperationKind.TRANSFORM,
    value_independent: bool = True,
) -> TemporalPipelineNode:
    return TemporalPipelineNode(
        node_id=node_id,
        operation_kind=kind,
        dependency_ids=dependencies or [],
        reference_time=available_at - timedelta(minutes=1),
        available_at=available_at,
        value_independent_availability=value_independent,
        created_at=DECISION + timedelta(minutes=1),
    )


def test_temporal_non_interference_persists_a_linear_time_pass_report(tmp_path: Path) -> None:
    service = _service(tmp_path)
    request = TemporalNonInterferenceRequest(
        pipeline_id="pipeline:clean",
        decision_time=DECISION,
        nodes=[
            _node(
                "source",
                available_at=DECISION - timedelta(minutes=20),
                kind=TemporalOperationKind.SOURCE,
            ),
            _node(
                "window",
                available_at=DECISION - timedelta(minutes=15),
                dependencies=["source"],
                kind=TemporalOperationKind.WINDOW,
            ),
            _node(
                "decision",
                available_at=DECISION - timedelta(minutes=10),
                dependencies=["window"],
                kind=TemporalOperationKind.DECISION,
            ),
        ],
        output_node_ids=["decision"],
        created_at=DECISION + timedelta(minutes=2),
    )

    report = service.audit_non_interference(request)

    assert report.status is TemporalAuditStatus.PASS
    assert report.node_count == 3
    assert report.edge_count == 2
    assert report.linear_time_contract
    assert report.checked_value_independent_fragment
    assert report.finding_codes == []
    assert not report.production_admission_allowed
    assert service.audit_non_interference(request) == report
    assert service.audit_artifact(report.report_id)["status"] == "PASS"


def test_temporal_request_identity_ignores_nested_parser_timestamps() -> None:
    payload = {
        "pipeline_id": "pipeline:stable-identity",
        "decision_time": DECISION.isoformat(),
        "nodes": [
            {
                "node_id": "source",
                "operation_kind": "SOURCE",
                "dependency_ids": [],
                "reference_time": (DECISION - timedelta(minutes=2)).isoformat(),
                "available_at": (DECISION - timedelta(minutes=1)).isoformat(),
            }
        ],
        "output_node_ids": ["source"],
        "created_at": DECISION.isoformat(),
    }
    first_payload = dict(payload)
    first_payload["nodes"] = [
        {**payload["nodes"][0], "created_at": (DECISION + timedelta(seconds=1)).isoformat()}
    ]
    second_payload = dict(payload)
    second_payload["nodes"] = [
        {**payload["nodes"][0], "created_at": (DECISION + timedelta(seconds=2)).isoformat()}
    ]
    first = TemporalNonInterferenceRequest.model_validate(first_payload)
    second = TemporalNonInterferenceRequest.model_validate(second_payload)
    service = TemporalValidityService.__new__(TemporalValidityService)
    first_report = service.audit_non_interference(first, persist=False)
    second_report = service.audit_non_interference(second, persist=False)
    assert first_report.report_id == second_report.report_id
    assert first_report == second_report


def test_temporal_non_interference_fails_future_and_dependency_backdating(tmp_path: Path) -> None:
    service = _service(tmp_path)
    request = TemporalNonInterferenceRequest(
        pipeline_id="pipeline:leaky",
        decision_time=DECISION,
        nodes=[
            _node(
                "future-source",
                available_at=DECISION + timedelta(minutes=1),
                kind=TemporalOperationKind.SOURCE,
            ),
            _node(
                "decision",
                available_at=DECISION - timedelta(minutes=5),
                dependencies=["future-source"],
                kind=TemporalOperationKind.DECISION,
            ),
        ],
        output_node_ids=["decision"],
        created_at=DECISION + timedelta(minutes=2),
    )

    report = service.audit_non_interference(request)

    assert report.status is TemporalAuditStatus.FAIL
    assert "FUTURE_VISIBLE_AT_DECISION" in report.finding_codes
    assert "NODE_AVAILABLE_BEFORE_DEPENDENCY" in report.finding_codes
    decision = next(item for item in report.node_audits if item.node_id == "decision")
    assert decision.effective_available_at == DECISION + timedelta(minutes=1)


def test_temporal_non_interference_fails_unknown_dependency_and_cycle(tmp_path: Path) -> None:
    service = _service(tmp_path)
    unknown = service.audit_non_interference(
        TemporalNonInterferenceRequest(
            pipeline_id="pipeline:unknown",
            decision_time=DECISION,
            nodes=[
                _node(
                    "decision",
                    available_at=DECISION,
                    dependencies=["missing"],
                    kind=TemporalOperationKind.DECISION,
                )
            ],
            output_node_ids=["decision"],
        )
    )
    assert unknown.status is TemporalAuditStatus.FAIL
    assert "UNKNOWN_DEPENDENCY" in unknown.finding_codes

    cycle = service.audit_non_interference(
        TemporalNonInterferenceRequest(
            pipeline_id="pipeline:cycle",
            decision_time=DECISION,
            nodes=[
                _node("a", available_at=DECISION, dependencies=["b"]),
                _node("b", available_at=DECISION, dependencies=["a"]),
            ],
            output_node_ids=["a"],
        )
    )
    assert cycle.status is TemporalAuditStatus.FAIL
    assert "DEPENDENCY_CYCLE" in cycle.finding_codes


def test_temporal_non_interference_ignores_unreachable_future_node(tmp_path: Path) -> None:
    report = _service(tmp_path).audit_non_interference(
        TemporalNonInterferenceRequest(
            pipeline_id="pipeline:unreachable-future",
            decision_time=DECISION,
            nodes=[
                _node(
                    "source",
                    available_at=DECISION - timedelta(minutes=2),
                    kind=TemporalOperationKind.SOURCE,
                ),
                _node(
                    "decision",
                    available_at=DECISION - timedelta(minutes=1),
                    dependencies=["source"],
                    kind=TemporalOperationKind.DECISION,
                ),
                _node(
                    "unused-future",
                    available_at=DECISION + timedelta(days=1),
                    kind=TemporalOperationKind.SOURCE,
                ),
            ],
            output_node_ids=["decision"],
        )
    )

    assert report.status is TemporalAuditStatus.PASS
    assert report.node_count == 2
    assert {item.node_id for item in report.node_audits} == {"source", "decision"}


def test_temporal_non_interference_does_not_certify_value_dependent_availability(
    tmp_path: Path,
) -> None:
    report = _service(tmp_path).audit_non_interference(
        TemporalNonInterferenceRequest(
            pipeline_id="pipeline:value-dependent",
            decision_time=DECISION,
            nodes=[
                _node(
                    "retrieval",
                    available_at=DECISION - timedelta(minutes=1),
                    kind=TemporalOperationKind.RETRIEVAL,
                    value_independent=False,
                )
            ],
            output_node_ids=["retrieval"],
        )
    )

    assert report.status is TemporalAuditStatus.FAIL
    assert not report.checked_value_independent_fragment
    assert "VALUE_DEPENDENT_AVAILABILITY_UNPROVEN" in report.finding_codes


def test_knowledge_cutoff_diagnostic_is_descriptive_only(tmp_path: Path) -> None:
    cutoff = datetime(2024, 1, 1, tzinfo=UTC)
    request = KnowledgeCutoffDiagnosticRequest(
        method_id="method:test",
        model_id="model:test",
        knowledge_cutoff=cutoff,
        periods=[
            KnowledgeCutoffAlphaPeriod(
                period_id="p1",
                period_start=cutoff - timedelta(days=60),
                period_end=cutoff - timedelta(days=31),
                alpha=0.04,
                independent_decision_count=10,
            ),
            KnowledgeCutoffAlphaPeriod(
                period_id="p2",
                period_start=cutoff - timedelta(days=30),
                period_end=cutoff - timedelta(days=1),
                alpha=0.02,
                independent_decision_count=30,
            ),
            KnowledgeCutoffAlphaPeriod(
                period_id="p3-cross",
                period_start=cutoff - timedelta(days=1),
                period_end=cutoff + timedelta(days=1),
                alpha=0.50,
                independent_decision_count=100,
            ),
            KnowledgeCutoffAlphaPeriod(
                period_id="p4",
                period_start=cutoff + timedelta(days=1),
                period_end=cutoff + timedelta(days=30),
                alpha=0.01,
                independent_decision_count=20,
            ),
        ],
    )

    service = _service(tmp_path)
    report = service.knowledge_cutoff_diagnostic(request)

    assert report.status is KnowledgeCutoffDiagnosticStatus.EVALUABLE
    assert report.pre_cutoff_weighted_alpha == pytest.approx(0.025)
    assert report.post_cutoff_weighted_alpha == pytest.approx(0.01)
    assert report.alpha_decay_pre_minus_post == pytest.approx(0.015)
    assert report.alpha_retention_ratio == pytest.approx(0.4)
    assert report.crossing_cutoff_period_count == 1
    assert "CROSS_CUTOFF_PERIOD_EXCLUDED" in report.finding_codes
    assert not report.deployment_claim_allowed
    assert not report.production_admission_allowed
    assert service.knowledge_cutoff_diagnostic(request) == report
    assert service.audit_artifact(report.report_id)["status"] == "PASS"


def test_knowledge_cutoff_without_post_cutoff_sample_is_not_evaluable(tmp_path: Path) -> None:
    cutoff = datetime(2024, 1, 1, tzinfo=UTC)
    report = _service(tmp_path).knowledge_cutoff_diagnostic(
        KnowledgeCutoffDiagnosticRequest(
            method_id="method:test",
            model_id="model:test",
            knowledge_cutoff=cutoff,
            periods=[
                KnowledgeCutoffAlphaPeriod(
                    period_id="pre",
                    period_start=cutoff - timedelta(days=30),
                    period_end=cutoff - timedelta(days=1),
                    alpha=0.02,
                    independent_decision_count=10,
                )
            ],
        )
    )

    assert report.status is KnowledgeCutoffDiagnosticStatus.NOT_EVALUABLE
    assert report.alpha_decay_pre_minus_post is None
    assert "NO_POST_CUTOFF_PERIOD" in report.finding_codes


def test_truncation_probe_detects_future_dependent_transform() -> None:
    rows = [1, 2, 3, 4, 5]

    def trailing(values):
        return [sum(values[max(0, index - 1) : index + 1]) for index in range(len(values))]

    def peeking(values):
        return [
            values[index] + values[index + 1] if index + 1 < len(values) else values[index]
            for index in range(len(values))
        ]

    causal = truncation_invariance_probe(rows, trailing)
    assert causal.invariant
    assert causal.exhaustive
    leaky = truncation_invariance_probe(rows, peeking)
    assert not leaky.invariant
    assert leaky.exhaustive
    assert leaky.drift_cutoffs == [1, 2, 3, 4]


def test_truncation_probe_bounds_default_cost_for_long_series() -> None:
    rows = list(range(1000))

    def cumulative(values):
        running = 0
        output = []
        for value in values:
            running += value
            output.append(running)
        return output

    result = truncation_invariance_probe(rows, cumulative)
    assert result.invariant
    assert result.checked_cutoff_count == 64
    assert not result.exhaustive
