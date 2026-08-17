"""Verifiable temporal-validity diagnostics for research and backtesting pipelines."""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Sequence
from datetime import datetime

from astock.core.hashing import canonical_json_bytes, content_hash, sha256_bytes
from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.schemas.base import AStockModel
from astock.schemas.temporal_validity import (
    KnowledgeCutoffAlphaPeriod,
    KnowledgeCutoffDiagnosticReport,
    KnowledgeCutoffDiagnosticRequest,
    KnowledgeCutoffDiagnosticStatus,
    TemporalAuditStatus,
    TemporalNodeAudit,
    TemporalNonInterferenceReport,
    TemporalNonInterferenceRequest,
    TemporalPipelineNode,
    TruncationInvarianceResult,
)


class TemporalValidityService:
    """Audit the value-independent PIT fragment without granting trading authority."""

    def __init__(self, state: StateStore, objects: ObjectStore) -> None:
        self.state = state
        self.objects = objects

    def audit_non_interference(
        self,
        request: TemporalNonInterferenceRequest,
        *,
        persist: bool = True,
    ) -> TemporalNonInterferenceReport:
        nodes = {item.node_id: item for item in request.nodes}
        active, unknown_dependencies = _active_dependency_closure(
            nodes,
            request.output_node_ids,
        )
        findings: list[str] = []
        if unknown_dependencies:
            findings.append("UNKNOWN_DEPENDENCY")

        indegree = {node_id: 0 for node_id in active}
        dependents: dict[str, list[str]] = {node_id: [] for node_id in active}
        edge_count = 0
        for node_id in sorted(active):
            node = nodes[node_id]
            for dependency_id in node.dependency_ids:
                if dependency_id not in active:
                    continue
                indegree[node_id] += 1
                dependents[dependency_id].append(node_id)
                edge_count += 1

        queue = deque(sorted(node_id for node_id, degree in indegree.items() if degree == 0))
        effective_availability: dict[str, datetime] = {}
        node_findings: dict[str, list[str]] = {node_id: [] for node_id in active}
        ordered: list[str] = []
        while queue:
            node_id = queue.popleft()
            node = nodes[node_id]
            ordered.append(node_id)
            dependency_times = [
                effective_availability[dependency_id]
                for dependency_id in node.dependency_ids
                if dependency_id in effective_availability
            ]
            dependency_max = max(dependency_times, default=node.available_at)
            effective = max(node.available_at, dependency_max)
            effective_availability[node_id] = effective
            if not node.value_independent_availability:
                node_findings[node_id].append("VALUE_DEPENDENT_AVAILABILITY_UNPROVEN")
            if dependency_times and node.available_at < dependency_max:
                node_findings[node_id].append("NODE_AVAILABLE_BEFORE_DEPENDENCY")
            if effective > request.decision_time:
                node_findings[node_id].append("FUTURE_VISIBLE_AT_DECISION")
            for dependent_id in sorted(dependents[node_id]):
                indegree[dependent_id] -= 1
                if indegree[dependent_id] == 0:
                    queue.append(dependent_id)

        cyclic = sorted(active - set(ordered))
        if cyclic:
            findings.append("DEPENDENCY_CYCLE")
            for node_id in cyclic:
                node_findings[node_id].append("DEPENDENCY_CYCLE")
                effective_availability[node_id] = nodes[node_id].available_at

        for node_id, dependency_id in unknown_dependencies:
            node_findings[node_id].append(f"UNKNOWN_DEPENDENCY:{dependency_id}")

        audits = [
            TemporalNodeAudit(
                node_id=node_id,
                operation_kind=nodes[node_id].operation_kind,
                reference_time=nodes[node_id].reference_time,
                declared_available_at=nodes[node_id].available_at,
                effective_available_at=effective_availability[node_id],
                dependency_count=len(nodes[node_id].dependency_ids),
                finding_codes=sorted(set(node_findings[node_id])),
                created_at=request.created_at,
            )
            for node_id in sorted(active)
        ]
        findings.extend(code.split(":", 1)[0] for item in audits for code in item.finding_codes)
        findings = sorted(set(findings))
        checked_fragment = all(nodes[node_id].value_independent_availability for node_id in active)
        status = TemporalAuditStatus.PASS if not findings else TemporalAuditStatus.FAIL
        request_payload = _canonical_request_payload(request)
        request_hash = sha256_bytes(canonical_json_bytes(request_payload))
        report_id = f"temporal-non-interference:{request_hash}"
        report = TemporalNonInterferenceReport(
            report_id=report_id,
            pipeline_id=request.pipeline_id,
            decision_time=request.decision_time,
            status=status,
            node_count=len(active),
            edge_count=edge_count,
            checked_value_independent_fragment=checked_fragment,
            node_audits=audits,
            finding_codes=findings,
            created_at=request.created_at,
        )
        if persist:
            request_ref = self.objects.put_json(request_payload)
            if request_ref.sha256 != request_hash:
                raise ValueError("temporal request object hash does not match its identity")
            self._persist(
                report_id,
                "TemporalNonInterferenceReport",
                report,
                [request_ref.sha256],
            )
        return report

    def knowledge_cutoff_diagnostic(
        self,
        request: KnowledgeCutoffDiagnosticRequest,
    ) -> KnowledgeCutoffDiagnosticReport:
        pre = [item for item in request.periods if item.period_end <= request.knowledge_cutoff]
        post = [item for item in request.periods if item.period_start > request.knowledge_cutoff]
        crossing = [
            item
            for item in request.periods
            if item.period_start <= request.knowledge_cutoff < item.period_end
        ]
        findings: list[str] = []
        if not pre:
            findings.append("NO_PRE_CUTOFF_PERIOD")
        if not post:
            findings.append("NO_POST_CUTOFF_PERIOD")
        if crossing:
            findings.append("CROSS_CUTOFF_PERIOD_EXCLUDED")
        pre_alpha = _weighted_alpha(pre)
        post_alpha = _weighted_alpha(post)
        decay: float | None = None
        retention: float | None = None
        if pre_alpha is not None and post_alpha is not None:
            decay = pre_alpha - post_alpha
            if pre_alpha != 0:
                retention = post_alpha / pre_alpha
            else:
                findings.append("ZERO_PRE_CUTOFF_ALPHA")
        status = (
            KnowledgeCutoffDiagnosticStatus.EVALUABLE
            if pre and post
            else KnowledgeCutoffDiagnosticStatus.NOT_EVALUABLE
        )
        request_payload = _canonical_request_payload(request)
        request_hash = sha256_bytes(canonical_json_bytes(request_payload))
        report_id = f"knowledge-cutoff-diagnostic:{request_hash}"
        report = KnowledgeCutoffDiagnosticReport(
            report_id=report_id,
            method_id=request.method_id,
            model_id=request.model_id,
            knowledge_cutoff=request.knowledge_cutoff,
            status=status,
            pre_cutoff_period_count=len(pre),
            post_cutoff_period_count=len(post),
            crossing_cutoff_period_count=len(crossing),
            pre_cutoff_decision_count=sum(item.independent_decision_count for item in pre),
            post_cutoff_decision_count=sum(item.independent_decision_count for item in post),
            pre_cutoff_weighted_alpha=pre_alpha,
            post_cutoff_weighted_alpha=post_alpha,
            alpha_decay_pre_minus_post=decay,
            alpha_retention_ratio=retention,
            finding_codes=sorted(set(findings)),
            created_at=request.created_at,
        )
        request_ref = self.objects.put_json(request_payload)
        if request_ref.sha256 != request_hash:
            raise ValueError("knowledge-cutoff request object hash does not match its identity")
        self._persist(
            report_id,
            "KnowledgeCutoffDiagnosticReport",
            report,
            [request_ref.sha256],
        )
        return report

    def audit_artifact(self, artifact_id: str) -> dict[str, object]:
        record = self.state.artifact_record(artifact_id)
        if record is None:
            return {"status": "NOT_FOUND", "artifact_id": artifact_id, "finding_codes": ["MISSING"]}
        artifact_type = str(record["type"])
        supported = {"TemporalNonInterferenceReport", "KnowledgeCutoffDiagnosticReport"}
        findings: list[str] = []
        if artifact_type not in supported:
            findings.append("WRONG_ARTIFACT_TYPE")
        object_hash = str(record["object_hash"])
        if not self.objects.verify(object_hash):
            findings.append("MISSING_OR_INVALID_OBJECT")
        input_hashes = record.get("input_hashes", [])
        if not isinstance(input_hashes, list) or not all(
            isinstance(item, str) for item in input_hashes
        ):
            findings.append("INVALID_INPUT_HASHES")
        elif any(not self.objects.verify(item) for item in input_hashes):
            findings.append("MISSING_OR_INVALID_INPUT_OBJECT")
        return {
            "status": "PASS" if not findings else "FAIL",
            "artifact_id": artifact_id,
            "artifact_type": artifact_type,
            "finding_codes": sorted(findings),
        }

    def _persist(
        self,
        artifact_id: str,
        artifact_type: str,
        value: AStockModel,
        input_hashes: list[str],
    ) -> None:
        object_ref = self.objects.put_json(value.model_dump(mode="json"))
        self.state.register_artifact(
            artifact_id=artifact_id,
            artifact_type=artifact_type,
            schema_version=value.schema_version,
            object_hash=object_ref.sha256,
            input_hashes=input_hashes,
        )


def truncation_invariance_probe[T, U](
    rows: Sequence[T],
    transform: Callable[[Sequence[T]], Sequence[U]],
    *,
    cutoffs: Sequence[int] | None = None,
) -> TruncationInvarianceResult:
    """Check prefix stability; default to at most 64 well-spread cutoffs."""

    full_output = list(transform(rows))
    if len(full_output) != len(rows):
        raise ValueError("truncation probe requires a row-aligned transform")
    selected = sorted(
        set(cutoffs if cutoffs is not None else _default_truncation_cutoffs(len(rows)))
    )
    if any(cutoff < 1 or cutoff > len(rows) for cutoff in selected):
        raise ValueError("truncation cutoffs must be within the row sequence")
    exhaustive = selected == list(range(1, len(rows) + 1))
    drift: list[int] = []
    for cutoff in selected:
        prefix_output = list(transform(rows[:cutoff]))
        if len(prefix_output) != cutoff:
            raise ValueError("truncation probe requires row-aligned prefix outputs")
        if content_hash(prefix_output) != content_hash(full_output[:cutoff]):
            drift.append(cutoff)
    return TruncationInvarianceResult(
        checked_cutoff_count=len(selected),
        exhaustive=exhaustive,
        drift_cutoffs=drift,
        invariant=not drift,
    )


def _default_truncation_cutoffs(length: int, *, max_checks: int = 64) -> list[int]:
    if length <= 0:
        return []
    if length <= max_checks:
        return list(range(1, length + 1))
    return sorted(
        {
            1 + round(index * (length - 1) / (max_checks - 1))
            for index in range(max_checks)
        }
    )


def _active_dependency_closure(
    nodes: dict[str, TemporalPipelineNode],
    output_node_ids: Sequence[str],
) -> tuple[set[str], list[tuple[str, str]]]:
    active: set[str] = set()
    unknown: list[tuple[str, str]] = []
    stack = list(output_node_ids)
    while stack:
        node_id = stack.pop()
        if node_id in active:
            continue
        node = nodes[node_id]
        active.add(node_id)
        for dependency_id in node.dependency_ids:
            if dependency_id not in nodes:
                unknown.append((node_id, dependency_id))
            else:
                stack.append(dependency_id)
    return active, sorted(set(unknown))


def _canonical_request_payload(value: AStockModel) -> dict[str, object]:
    payload = value.model_dump(mode="json")
    root_created_at = payload.get("created_at")
    normalized = {
        str(key): _strip_nested_created_at(child)
        for key, child in payload.items()
        if str(key) != "created_at"
    }
    if root_created_at is not None:
        normalized["created_at"] = root_created_at
    return normalized


def _strip_nested_created_at(value: object) -> object:
    if isinstance(value, dict):
        return {
            str(key): _strip_nested_created_at(child)
            for key, child in value.items()
            if str(key) != "created_at"
        }
    if isinstance(value, list):
        return [_strip_nested_created_at(child) for child in value]
    return value


def _weighted_alpha(periods: Sequence[KnowledgeCutoffAlphaPeriod]) -> float | None:
    total = sum(item.independent_decision_count for item in periods)
    if total == 0:
        return None
    return sum(item.alpha * item.independent_decision_count for item in periods) / total


__all__ = ["TemporalValidityService", "truncation_invariance_probe"]
