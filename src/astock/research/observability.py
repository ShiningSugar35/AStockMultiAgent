"""Agent routing, runtime, and cross-provider data observability."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from astock.core.hashing import content_hash
from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.schemas.agent_observability import (
    AgentDataAlignmentSummary,
    AgentObservabilityReport,
    AgentSkillRoutingSummary,
    AgentTaskObservation,
    AgentTaskObservationRequest,
    AgentTaskPerformanceSummary,
    AgentTaskStatus,
)
from astock.schemas.research_runtime import ResearchRunReport, ResearchRunStatus


class AgentObservabilityService:
    """Aggregate explicit Agent observations with deterministic runtime telemetry."""

    def __init__(
        self,
        state: StateStore,
        objects: ObjectStore,
        *,
        project_root: Path,
        manifest_root: Path,
    ) -> None:
        self.state = state
        self.objects = objects
        self.project_root = project_root.resolve()
        self.manifest_root = manifest_root.resolve()

    def register(self, request: AgentTaskObservationRequest) -> AgentTaskObservation:
        known_skills = set(self._repo_skill_ids())
        referenced = {
            *request.eligible_skill_ids,
            *request.selected_skill_ids,
            *request.completed_skill_ids,
            *request.expected_skill_ids,
        }
        unknown = sorted(referenced - known_skills)
        if unknown:
            raise ValueError(f"unknown canonical Agent Skills: {unknown}")
        semantic = request.model_dump(mode="json", exclude={"schema_version", "created_at"})
        observation_id = f"agent-task-observation:{content_hash(semantic)}"
        observation = AgentTaskObservation(
            **request.model_dump(exclude={"schema_version"}),
            observation_id=observation_id,
        )
        event_hash = content_hash(semantic)
        with self.state.connect() as connection:
            existing = connection.execute(
                "SELECT observation_id,event_hash,object_hash FROM agent_task_observation_index "
                "WHERE observation_id=? OR task_id=?",
                (observation.observation_id, observation.task_id),
            ).fetchone()
        if existing is not None:
            if (
                str(existing["observation_id"]) != observation.observation_id
                or str(existing["event_hash"]) != event_hash
            ):
                raise ValueError("Agent task already has a different durable observation")
            return AgentTaskObservation.model_validate_json(
                self.objects.get_bytes(str(existing["object_hash"]))
            )
        precision, recall = _routing_precision_recall(observation)
        object_ref = self.objects.put_json(observation.model_dump(mode="json"))
        with self.state.transaction() as connection:
            connection.execute(
                "INSERT INTO agent_task_observation_index("
                "observation_id,task_id,task_status,duration_ms,eligible_skill_count,"
                "selected_skill_count,completed_skill_count,expected_skill_count,"
                "routing_precision,routing_recall,object_hash,event_hash,created_at"
                ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    observation.observation_id,
                    observation.task_id,
                    observation.task_status.value,
                    observation.duration_ms,
                    len(observation.eligible_skill_ids),
                    len(observation.selected_skill_ids),
                    len(observation.completed_skill_ids),
                    len(observation.expected_skill_ids),
                    precision,
                    recall,
                    object_ref.sha256,
                    event_hash,
                    observation.created_at.astimezone(UTC).isoformat(),
                ),
            )
        self.state.register_artifact(
            artifact_id=observation.observation_id,
            artifact_type="AgentTaskObservation",
            schema_version=observation.schema_version,
            object_hash=object_ref.sha256,
            input_hashes=[],
        )
        return observation

    def report(self, *, lookback_days: int = 30) -> AgentObservabilityReport:
        if lookback_days < 0:
            raise ValueError("lookback_days must be non-negative")
        now = datetime.now(UTC)
        since = now - timedelta(days=lookback_days) if lookback_days else None
        observations, observation_hashes = self._observations(since)
        skill_summaries = self._skill_summaries(observations)
        labeled = [item for item in observations if item.expected_skill_ids]
        true_positive = sum(
            len(set(item.selected_skill_ids) & set(item.expected_skill_ids)) for item in labeled
        )
        selected_labeled = sum(len(item.selected_skill_ids) for item in labeled)
        expected_labeled = sum(len(item.expected_skill_ids) for item in labeled)
        micro_precision = true_positive / selected_labeled if selected_labeled else None
        micro_recall = true_positive / expected_labeled if expected_labeled else None
        selected_slots = sum(len(item.selected_skill_ids) for item in observations)
        completed_slots = sum(len(item.completed_skill_ids) for item in observations)
        production_count, production_useful = self._production_usage(since)
        research_reports, research_hashes = self._research_reports(since)
        alignment, alignment_hashes, alignment_findings = self._alignment_summary()
        task_performance = self._task_performance(observations, research_reports)
        findings = list(alignment_findings)
        if not observations:
            findings.append("NO_AGENT_TASK_OBSERVATIONS")
        if not labeled:
            findings.append("NO_LABELED_SKILL_ROUTING_TASKS")
        if not research_reports:
            findings.append("NO_RESEARCH_RUNS_IN_WINDOW")
        if not alignment.dual_source_evaluable_count:
            findings.append("NO_DUAL_SOURCE_ALIGNMENT_SAMPLE")
        findings = sorted(set(findings))
        input_hashes = sorted({*observation_hashes, *research_hashes, *alignment_hashes})
        semantic = {
            "lookback_days": lookback_days,
            "window_start": since.isoformat() if since else None,
            "window_end": now.isoformat(),
            "observation_hashes": observation_hashes,
            "research_hashes": research_hashes,
            "alignment_hashes": alignment_hashes,
        }
        report = AgentObservabilityReport(
            report_id=f"agent-observability:{content_hash(semantic)}",
            lookback_days=lookback_days,
            task_window_start=since.isoformat() if since else None,
            task_window_end=now.isoformat(),
            routing_labeled_task_count=len(labeled),
            routing_micro_precision=micro_precision,
            routing_micro_recall=micro_recall,
            selected_skill_slot_count=selected_slots,
            completed_skill_slot_count=completed_slots,
            skill_execution_hit_rate=(completed_slots / selected_slots if selected_slots else 0.0),
            production_skill_usage_event_count=production_count,
            production_skill_useful_event_count=production_useful,
            production_skill_useful_hit_rate=(
                production_useful / production_count if production_count else 0.0
            ),
            skill_summaries=skill_summaries,
            task_performance=task_performance,
            data_alignment=alignment,
            finding_codes=findings,
            created_at=now,
        )
        object_ref = self.objects.put_json(report.model_dump(mode="json"))
        self.state.register_artifact(
            artifact_id=report.report_id,
            artifact_type="AgentObservabilityReport",
            schema_version=report.schema_version,
            object_hash=object_ref.sha256,
            input_hashes=input_hashes,
        )
        return report

    def audit(self, report_id: str) -> dict[str, object]:
        record = self.state.artifact_record(report_id)
        if record is None:
            return {"status": "NOT_FOUND", "report_id": report_id, "finding_codes": ["MISSING"]}
        findings: list[str] = []
        if str(record["type"]) != "AgentObservabilityReport":
            findings.append("WRONG_ARTIFACT_TYPE")
        object_hash = str(record["object_hash"])
        if not self.objects.verify(object_hash):
            findings.append("MISSING_OR_INVALID_OBJECT")
        else:
            report = AgentObservabilityReport.model_validate_json(
                self.objects.get_bytes(object_hash)
            )
            if report.report_id != report_id:
                findings.append("REPORT_ID_MISMATCH")
        return {
            "status": "PASS" if not findings else "FAIL",
            "report_id": report_id,
            "finding_codes": sorted(findings),
        }

    def _repo_skill_ids(self) -> list[str]:
        root = self.project_root / ".agents" / "skills"
        if not root.is_dir():
            return []
        return sorted(
            path.name for path in root.iterdir() if path.is_dir() and (path / "SKILL.md").is_file()
        )

    def _observations(self, since: datetime | None) -> tuple[list[AgentTaskObservation], list[str]]:
        query = "SELECT object_hash FROM agent_task_observation_index"
        params: tuple[str, ...] = ()
        if since is not None:
            query += " WHERE created_at>=?"
            params = (since.isoformat(),)
        query += " ORDER BY created_at,observation_id"
        with self.state.connect() as connection:
            rows = connection.execute(query, params).fetchall()
        observations = [
            AgentTaskObservation.model_validate_json(
                self.objects.get_bytes(str(row["object_hash"]))
            )
            for row in rows
        ]
        return observations, [str(row["object_hash"]) for row in rows]

    def _skill_summaries(
        self, observations: list[AgentTaskObservation]
    ) -> list[AgentSkillRoutingSummary]:
        skill_ids = self._repo_skill_ids()
        counters: dict[str, dict[str, int]] = {
            skill_id: {
                "eligible": 0,
                "selected": 0,
                "completed": 0,
                "expected": 0,
                "selected_labeled": 0,
                "true_positive": 0,
            }
            for skill_id in skill_ids
        }
        for item in observations:
            for key, values in (
                ("eligible", item.eligible_skill_ids),
                ("selected", item.selected_skill_ids),
                ("completed", item.completed_skill_ids),
            ):
                for skill_id in values:
                    counter = counters.get(skill_id)
                    if counter is not None:
                        counter[key] += 1
            if not item.expected_skill_ids:
                continue
            expected = set(item.expected_skill_ids)
            selected = set(item.selected_skill_ids)
            for skill_id in expected:
                counter = counters.get(skill_id)
                if counter is not None:
                    counter["expected"] += 1
            for skill_id in selected:
                counter = counters.get(skill_id)
                if counter is not None:
                    counter["selected_labeled"] += 1
            for skill_id in selected & expected:
                counter = counters.get(skill_id)
                if counter is not None:
                    counter["true_positive"] += 1

        summaries: list[AgentSkillRoutingSummary] = []
        for skill_id in skill_ids:
            counter = counters[skill_id]
            eligible = counter["eligible"]
            selected = counter["selected"]
            completed = counter["completed"]
            expected = counter["expected"]
            selected_labeled = counter["selected_labeled"]
            true_positive = counter["true_positive"]
            summaries.append(
                AgentSkillRoutingSummary(
                    skill_id=skill_id,
                    eligible_task_count=eligible,
                    selected_task_count=selected,
                    completed_task_count=completed,
                    expected_task_count=expected,
                    selection_rate=(selected / eligible if eligible else 0.0),
                    execution_hit_rate=(completed / selected if selected else 0.0),
                    labeled_precision=(
                        true_positive / selected_labeled if selected_labeled else None
                    ),
                    labeled_recall=(true_positive / expected if expected else None),
                )
            )
        return summaries

    def _production_usage(self, since: datetime | None) -> tuple[int, int]:
        query = (
            "SELECT COUNT(*) AS total,"
            "COALESCE(SUM(CASE WHEN corrected_claim OR found_gap OR changed_driver "
            "OR provided_falsifier OR changed_ic_state THEN 1 ELSE 0 END),0) AS useful "
            "FROM skill_usage_event_index"
        )
        params: tuple[str, ...] = ()
        if since is not None:
            query += " WHERE created_at>=?"
            params = (since.isoformat(),)
        with self.state.connect() as connection:
            row = connection.execute(query, params).fetchone()
        if row is None:
            return 0, 0
        return int(row["total"]), int(row["useful"])

    def _research_reports(
        self, since: datetime | None
    ) -> tuple[list[ResearchRunReport], list[str]]:
        query = "SELECT object_hash FROM artifact_registry WHERE type='ResearchRunReport'"
        params: tuple[str, ...] = ()
        if since is not None:
            query += " AND created_at>=?"
            params = (since.isoformat(),)
        query += " ORDER BY created_at,artifact_id"
        with self.state.connect() as connection:
            rows = connection.execute(query, params).fetchall()
        latest: dict[str, tuple[ResearchRunReport, str]] = {}
        for row in rows:
            object_hash = str(row["object_hash"])
            report = ResearchRunReport.model_validate_json(self.objects.get_bytes(object_hash))
            current = latest.get(report.run_id)
            if current is None or report.created_at > current[0].created_at:
                latest[report.run_id] = (report, object_hash)
        ordered = [latest[key] for key in sorted(latest)]
        return [item[0] for item in ordered], [item[1] for item in ordered]

    def _task_performance(
        self,
        observations: list[AgentTaskObservation],
        research_reports: list[ResearchRunReport],
    ) -> AgentTaskPerformanceSummary:
        durations = [item.duration_ms for item in observations]
        research_wall = [item.performance.wall_time_ms for item in research_reports]
        checkpoint_count = sum(len(item.checkpoints) for item in research_reports)
        cache_hits = sum(item.performance.cache_hit_count for item in research_reports)
        return AgentTaskPerformanceSummary(
            observed_task_count=len(observations),
            completed_task_count=sum(
                item.task_status is AgentTaskStatus.COMPLETED for item in observations
            ),
            needs_info_task_count=sum(
                item.task_status is AgentTaskStatus.NEEDS_INFO for item in observations
            ),
            failed_task_count=sum(
                item.task_status is AgentTaskStatus.FAILED for item in observations
            ),
            completion_rate=(
                sum(item.task_status is AgentTaskStatus.COMPLETED for item in observations)
                / len(observations)
                if observations
                else 0.0
            ),
            mean_duration_ms=_mean_int(durations),
            p50_duration_ms=_percentile_int(durations, 0.50),
            p95_duration_ms=_percentile_int(durations, 0.95),
            research_run_count=len(research_reports),
            research_run_complete_count=sum(
                item.status is ResearchRunStatus.COMPLETE for item in research_reports
            ),
            research_run_mean_wall_time_ms=_mean_int(research_wall),
            research_run_p95_wall_time_ms=_percentile_int(research_wall, 0.95),
            research_run_mean_provider_calls=(
                sum(item.performance.provider_call_count for item in research_reports)
                / len(research_reports)
                if research_reports
                else 0.0
            ),
            research_run_cache_hit_rate=(
                cache_hits / checkpoint_count if checkpoint_count else 0.0
            ),
        )

    def _alignment_summary(
        self,
    ) -> tuple[AgentDataAlignmentSummary, list[str], list[str]]:
        root = self.manifest_root / "canonical"
        manifests: list[dict[str, object]] = []
        hashes: list[str] = []
        findings: list[str] = []
        for path in sorted(root.rglob("*.json")) if root.exists() else []:
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                findings.append("CANONICAL_MANIFEST_UNREADABLE")
                continue
            if not isinstance(value, dict):
                findings.append("CANONICAL_MANIFEST_INVALID")
                continue
            payload = dict(value)
            stored_hash = payload.pop("content_hash", None)
            computed = content_hash(payload)
            if stored_hash != computed:
                findings.append("CANONICAL_MANIFEST_HASH_MISMATCH")
                continue
            hashes.append(str(stored_hash))
            manifests.append(value)
        evaluable: list[dict[str, object]] = []
        passing = 0
        for item in manifests:
            raw_metrics = item.get("quality_metrics")
            if not isinstance(raw_metrics, dict):
                continue
            typed_metrics = {str(key): value for key, value in raw_metrics.items()}
            if (
                _safe_int(typed_metrics.get("common_window_union_count")) <= 0
                or typed_metrics.get("coverage_ratio") is None
            ):
                continue
            evaluable.append(typed_metrics)
            if item.get("quality_status") == "PASS":
                passing += 1
        coverage = [_safe_float(item.get("coverage_ratio")) for item in evaluable]
        close_p95 = [_safe_float(item.get("close_relative_p95")) for item in evaluable]
        ohlc_p95 = [_safe_float(item.get("ohlc_relative_p95")) for item in evaluable]
        volume_p95 = [_safe_float(item.get("volume_relative_p95")) for item in evaluable]
        return (
            AgentDataAlignmentSummary(
                canonical_manifest_count=len(manifests),
                dual_source_evaluable_count=len(evaluable),
                dual_source_pass_count=passing,
                data_alignment_pass_rate=(passing / len(evaluable) if evaluable else 0.0),
                mean_timestamp_coverage_ratio=_mean_float(coverage),
                mean_close_relative_p95=_mean_float(close_p95),
                mean_ohlc_relative_p95=_mean_float(ohlc_p95),
                mean_volume_relative_p95=_mean_float(volume_p95),
                worst_close_relative_p95=max(close_p95, default=0.0),
            ),
            hashes,
            findings,
        )


def _routing_precision_recall(
    observation: AgentTaskObservation,
) -> tuple[float | None, float | None]:
    if not observation.expected_skill_ids:
        return None, None
    selected = set(observation.selected_skill_ids)
    expected = set(observation.expected_skill_ids)
    true_positive = len(selected & expected)
    precision = true_positive / len(selected) if selected else 0.0
    recall = true_positive / len(expected) if expected else 0.0
    return precision, recall


def _mean_int(values: list[int]) -> int:
    return round(sum(values) / len(values)) if values else 0


def _mean_float(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _percentile_int(values: list[int], fraction: float) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    rank = max(1, min(len(ordered), int((len(ordered) * fraction) + 0.999999)))
    return ordered[rank - 1]


def _safe_int(value: object) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return max(0, value)
    if isinstance(value, float):
        return max(0, int(value))
    if isinstance(value, str):
        try:
            return max(0, int(float(value)))
        except ValueError:
            return 0
    return 0


def _safe_float(value: object) -> float:
    if isinstance(value, bool):
        return float(value)
    if isinstance(value, (int, float)):
        return max(0.0, float(value))
    if isinstance(value, str):
        try:
            return max(0.0, float(value))
        except ValueError:
            return 0.0
    return 0.0


__all__ = ["AgentObservabilityService"]
