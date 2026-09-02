from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from astock.core.hashing import canonical_json_bytes, content_hash
from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.research.observability import AgentObservabilityService
from astock.schemas.agent_observability import AgentTaskObservationRequest, AgentTaskStatus

PROJECT_ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 8, 17, 7, 30, tzinfo=UTC)


def _service(tmp_path: Path) -> AgentObservabilityService:
    state = StateStore(tmp_path / "state.sqlite", PROJECT_ROOT / "migrations")
    assert "0058" in state.migrate()
    return AgentObservabilityService(
        state,
        ObjectStore(tmp_path / "objects"),
        project_root=PROJECT_ROOT,
        manifest_root=tmp_path / "manifests",
    )


def _write_manifest(tmp_path: Path, name: str, *, status: str, coverage: float | None) -> None:
    payload: dict[str, object] = {
        "schema_version": "1.4",
        "market": "XSHG",
        "instrument_type": "STOCK",
        "symbol": name,
        "frequency": "60m",
        "adjustment_mode": "NONE",
        "canonical_batch_id": f"batch-{name}",
        "source_batch_ids": [f"a-{name}", f"b-{name}"],
        "source_snapshot_ids": [f"s1-{name}", f"s2-{name}"],
        "selected_provider": "eastmoney-5m",
        "quality_report_id": f"quality-{name}",
        "replay_quality": "PROVIDER_1H_APPROX",
        "quality_status": status,
        "quality_metrics": {
            "common_bar_count": 10,
            "common_window_union_count": 10,
            "coverage_ratio": coverage,
            "close_relative_p95": 0.002,
            "ohlc_relative_p95": 0.003,
            "ohlc_relative_max": 0.004,
            "volume_relative_p95": 0.01,
        },
        "actual_start": NOW.isoformat(),
        "actual_end": NOW.isoformat(),
        "bar_count": 10,
        "files": [],
        "file_hashes": {},
        "year_content_hashes": {},
    }
    payload["content_hash"] = content_hash(payload)
    path = tmp_path / "manifests" / "canonical" / "XSHG" / "60m" / f"{name}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_json_bytes(payload))


def test_agent_observability_tracks_routing_duration_and_alignment(tmp_path: Path) -> None:
    service = _service(tmp_path)
    _write_manifest(tmp_path, "600001", status="PASS", coverage=0.98)
    _write_manifest(tmp_path, "600002", status="PARTIAL", coverage=0.80)
    _write_manifest(tmp_path, "600003", status="PASS", coverage=None)
    first = service.register(
        AgentTaskObservationRequest(
            task_id="task-1",
            task_status=AgentTaskStatus.COMPLETED,
            eligible_skill_ids=[
                "company-deep-research",
                "evidence-investigation",
                "financial-integrity-audit",
            ],
            selected_skill_ids=["company-deep-research", "financial-integrity-audit"],
            completed_skill_ids=["company-deep-research"],
            expected_skill_ids=["company-deep-research", "evidence-investigation"],
            duration_ms=100,
            created_at=NOW,
        )
    )
    assert first.observation_id.startswith("agent-task-observation:")
    assert (
        service.register(
            AgentTaskObservationRequest(
                task_id="task-1",
                task_status=AgentTaskStatus.COMPLETED,
                eligible_skill_ids=[
                    "company-deep-research",
                    "evidence-investigation",
                    "financial-integrity-audit",
                ],
                selected_skill_ids=["company-deep-research", "financial-integrity-audit"],
                completed_skill_ids=["company-deep-research"],
                expected_skill_ids=["company-deep-research", "evidence-investigation"],
                duration_ms=100,
                created_at=NOW + timedelta(seconds=1),
            )
        ).observation_id
        == first.observation_id
    )
    service.register(
        AgentTaskObservationRequest(
            task_id="task-2",
            task_status=AgentTaskStatus.NEEDS_INFO,
            eligible_skill_ids=["company-deep-research", "evidence-investigation"],
            selected_skill_ids=["evidence-investigation"],
            completed_skill_ids=["evidence-investigation"],
            duration_ms=300,
            created_at=NOW,
        )
    )

    report = service.report(lookback_days=0)

    assert report.routing_labeled_task_count == 1
    assert report.routing_micro_precision == 0.5
    assert report.routing_micro_recall == 0.5
    assert report.selected_skill_slot_count == 3
    assert report.completed_skill_slot_count == 2
    assert report.skill_execution_hit_rate == 2 / 3
    assert report.task_performance.observed_task_count == 2
    assert report.task_performance.mean_duration_ms == 200
    assert report.task_performance.p50_duration_ms == 100
    assert report.task_performance.p95_duration_ms == 300
    assert report.data_alignment.canonical_manifest_count == 3
    assert report.data_alignment.dual_source_evaluable_count == 2
    assert report.data_alignment.dual_source_pass_count == 1
    assert report.data_alignment.data_alignment_pass_rate == 0.5
    assert report.data_alignment.mean_timestamp_coverage_ratio == 0.89
    company = next(
        item for item in report.skill_summaries if item.skill_id == "company-deep-research"
    )
    assert company.eligible_task_count == 2
    assert company.selected_task_count == 1
    assert company.completed_task_count == 1
    assert company.labeled_precision == 1.0
    assert company.labeled_recall == 1.0
    assert service.audit(report.report_id)["status"] == "PASS"


def test_agent_observation_rejects_unknown_or_conflicting_task_record(tmp_path: Path) -> None:
    service = _service(tmp_path)
    request = AgentTaskObservationRequest(
        task_id="task-fixed",
        task_status=AgentTaskStatus.COMPLETED,
        eligible_skill_ids=["company-deep-research"],
        selected_skill_ids=["company-deep-research"],
        completed_skill_ids=["company-deep-research"],
        duration_ms=10,
        created_at=NOW,
    )
    service.register(request)

    changed = request.model_copy(update={"duration_ms": 11})
    try:
        service.register(changed)
    except ValueError as exc:
        assert "different durable observation" in str(exc)
    else:
        raise AssertionError("conflicting task observation must fail closed")

    unknown = request.model_copy(
        update={
            "task_id": "task-unknown",
            "eligible_skill_ids": ["not-a-skill"],
            "selected_skill_ids": [],
            "completed_skill_ids": [],
        }
    )
    try:
        service.register(unknown)
    except ValueError as exc:
        assert "unknown canonical Agent Skills" in str(exc)
    else:
        raise AssertionError("unknown Agent Skill must fail closed")
