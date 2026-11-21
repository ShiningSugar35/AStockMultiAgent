"""Read bounded recovery coverage from the existing registry/checkpoints, without acquisition."""
from __future__ import annotations

import json
from contextlib import closing
from datetime import datetime
from typing import Any

from astock.core.errors import StorageError
from astock.core.hashing import content_hash
from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.schemas.evidence import FetchStatus
from astock.schemas.research_acquisition import (
    AcquisitionAttempt,
    AcquisitionAttemptStatus,
    CurrentResearchAcquisitionReport,
    CurrentResearchSchedule,
)
from astock.schemas.research_continuation import (
    CurrentResearchContinuation,
    CurrentResearchExternalTask,
    ExternalResearchTaskStatus,
    PublicRecoveryCoverage,
    PublicRecoveryRoute,
)

_MAX_REPORTS = 32
_MAX_WEB_OPERATIONS = 128
_MAX_JSON_BYTES = 4 * 1024 * 1024
_BLOCKED_REASONS = (
    "EXCEPTION", "TIMEOUT", "DEADLINE", "BUDGET", "UNKNOWN", "UNVERIFIED",
    "RATE_LIMIT", "QUOTA", "CANCEL", "DISABLED", "DENIED", "FORBIDDEN",
)


def _json_object(objects: ObjectStore, digest: str) -> Any:
    try:
        if objects.path_for(digest).stat().st_size > _MAX_JSON_BYTES:
            raise ValueError("recovery response exceeds its byte budget")
        return json.loads(objects.get_bytes(digest))
    except (OSError, StorageError):
        raise ValueError("registered recovery response bytes are unavailable or corrupt") from None


def _artifact(
    state: StateStore, objects: ObjectStore, identity: str, kind: str,
) -> tuple[Any, str]:
    row = state.artifact_record(identity)
    if row is None or row["type"] != kind:
        raise ValueError("recovery evidence has no registered typed artifact")
    digest = str(row["object_hash"])
    return _json_object(objects, digest), digest


def _empty_response(raw: Any) -> bool:
    """Recognize successful explicit empty response containers, never absent/invalid fields."""
    if isinstance(raw, list):
        return not raw
    if not isinstance(raw, dict):
        return False
    if (raw.get("code", 0) != 0 or raw.get("rc", 0) != 0
            or raw.get("success", True) is not True
            or str(raw.get("status", "success")).lower() not in {
                "success", "succeeded", "ok", "0",
            }):
        return False
    data = raw.get("data")
    if isinstance(data, list):
        return not data
    if isinstance(data, dict):
        for key in ("items", "diff"):
            if isinstance(data.get(key), list):
                return not data[key] and data.get("total", 0) == 0
    if isinstance(raw.get("announcements"), list):
        return not raw["announcements"] and raw.get("totalAnnouncement", 0) == 0
    return False


def _verified_provider_sources(
    state: StateStore, objects: ObjectStore, attempt: AcquisitionAttempt,
    provider: str, cutoff: datetime,
) -> tuple[tuple[str, ...], bool]:
    hashes = []
    empty = []
    for identity in attempt.source_snapshot_ids:
        snapshot = state.get_snapshot(identity)
        if snapshot is None:
            raise ValueError("acquisition source snapshot is missing")
        if snapshot.source_id != provider and not snapshot.source_id.startswith(provider + ":"):
            continue
        if (snapshot.available_to_system_at > cutoff
                or snapshot.fetch_status is not FetchStatus.SUCCEEDED):
            raise ValueError("acquisition source response was not successfully visible")
        raw = _json_object(objects, snapshot.object_sha256)
        hashes.append(snapshot.object_sha256)
        empty.append(_empty_response(raw))
    return tuple(sorted(set(hashes))), bool(empty) and all(empty)


def _provider_route(
    state: StateStore, objects: ObjectStore, task: CurrentResearchExternalTask,
    provider: str, reports: tuple[tuple[CurrentResearchAcquisitionReport, str], ...],
) -> PublicRecoveryRoute:
    attempts = [
        (attempt, report, digest)
        for report, digest in reports for attempt in report.attempts
        if attempt.capability is task.capability and provider in attempt.provider_path
    ]
    kwargs: dict[str, Any] = {
        "capability": task.capability, "route_id": "provider:" + provider, "route_kind": "PROVIDER",
        "classification": "NOT_ATTEMPTED", "attempt_count": len(attempts),
        "uncovered_impact": "This configured source route has no verified completed response.",
    }
    if not attempts:
        return PublicRecoveryRoute(**kwargs)
    attempt, report, digest = attempts[-1]
    kwargs.update(
        observed_record_count=attempt.record_count, observed_at=report.decision_as_of,
        evidence_artifact_ids=tuple(sorted({item.report_id for _, item, _ in attempts})),
        response_codes=tuple(sorted({
            code for item, _, _ in attempts for code in item.internal_reason_codes
        }))[:128],
    )
    hashes = {digest}
    previous = set()
    try:
        for prior, parent, _ in attempts[:-1]:
            prior_hashes, _ = _verified_provider_sources(
                state, objects, prior, provider, parent.decision_as_of,
            )
            previous.update(prior_hashes)
            hashes.update(prior_hashes)
        observed, explicit_empty = _verified_provider_sources(
            state, objects, attempt, provider, report.decision_as_of,
        )
        parent = state.artifact_record(report.report_id)
        if parent is None or not set(observed).issubset(parent["input_hashes"]):
            raise ValueError("source response is outside its acquisition report lineage")
        hashes.update(observed)
        kwargs["new_source_object_count"] = len(set(observed) - previous)
        blocked = attempt.status in {
            AcquisitionAttemptStatus.SKIPPED, AcquisitionAttemptStatus.FAILED,
        } or any(
            marker in code.upper() for code in attempt.internal_reason_codes
            for marker in _BLOCKED_REASONS
        )
        if blocked:
            classification = "FAILED_OR_BLOCKED"
        elif attempt.record_count:
            classification = "EVIDENCE_RETURNED"
        elif explicit_empty:
            classification = "NO_NEW_EVIDENCE"
        else:
            classification = "FAILED_OR_BLOCKED"
        kwargs.update(classification=classification, uncovered_impact=(
            "Only this registered source's explicit bounded response was empty; "
            "no issuer-wide absence or complete public coverage is claimed."
            if classification == "NO_NEW_EVIDENCE" else
            "This route has unresolved acquisition, certification or local-consumption work."
        ))
    except (OSError, ValueError, TypeError):
        kwargs.update(classification="EVIDENCE_UNVERIFIABLE",
                      uncovered_impact="Source bytes or availability cannot be verified.")
    kwargs["source_object_hashes"] = tuple(sorted(hashes))
    return PublicRecoveryRoute(**kwargs)


def _web_cursors(state: StateStore, continuation_id: str) -> tuple[list[dict[str, Any]], bool]:
    with closing(state.connect()) as connection:
        rows = connection.execute(
            "SELECT cursor_json FROM checkpoint WHERE scope_type='web-tool-operation' "
            "AND json_extract(cursor_json,'$.request.continuation_id')=? "
            "ORDER BY committed_at,scope_key LIMIT ?",
            (continuation_id, _MAX_WEB_OPERATIONS + 1),
        ).fetchall()
    return [json.loads(row["cursor_json"]) for row in rows[:_MAX_WEB_OPERATIONS]], (
        len(rows) > _MAX_WEB_OPERATIONS
    )


def _matching_web(
    cursor: dict[str, Any], record: CurrentResearchContinuation, task: CurrentResearchExternalTask,
) -> bool:
    request = cursor.get("request", {})
    return (
        request.get("request_id") == record.request_id
        and request.get("continuation_id") == record.continuation_id
        and request.get("task_id") == task.task_id
        and request.get("continuation_dispatch_id") == task.dispatch_id
        and (task.external_operation_id is None
             or cursor.get("operation_id") == task.external_operation_id)
        and request.get("operation") == "SEARCH"
        and request.get("query") == task.research_question
        and request.get("expected_fact") == task.research_question
        and request.get("evidence_scope") == (
            f"{record.company_id}:{task.capability.value}:{task.task_id}"
        )
        and datetime.fromisoformat(request["fresh_after"]) >= record.started_at
    )


def verified_empty_web_response(payload: dict[str, Any], raw: Any) -> bool:
    if (payload.get("request", {}).get("operation") != "SEARCH"
            or payload.get("status") != "SUCCEEDED" or payload.get("hits")):
        return False
    if payload.get("admission", {}).get("discovery_allowed_urls"):
        return False
    if payload.get("source") == "host-included-web":
        return (
            isinstance(raw, dict) and raw.get("status") == "SUCCEEDED" and raw.get("hits") == []
            and raw.get("operation_id") == payload.get("operation_id")
            and raw.get("dispatch_id") == payload.get("dispatch_id")
        )
    return (
        payload.get("source") == "deepseek-tools"
        and isinstance(raw, dict) and raw.get("results") == []
    )


def _web_route(
    state: StateStore, objects: ObjectStore, record: CurrentResearchContinuation,
    task: CurrentResearchExternalTask, cursors: list[dict[str, Any]],
) -> PublicRecoveryRoute:
    matched = [cursor for cursor in cursors if _matching_web(cursor, record, task)]
    kwargs: dict[str, Any] = {
        "capability": task.capability, "route_kind": "WEB_QUERY",
        "route_id": "web-query:" + content_hash(task.research_question),
        "classification": "NOT_ATTEMPTED", "attempt_count": len(matched),
        "uncovered_impact": "The exact configured question has no completed Web response; "
                            "preferred authorities are search targets, not proven coverage.",
    }
    hashes: set[str] = set()
    artifacts: list[str] = []
    codes: set[str] = set()
    discovered: set[str] = set()
    latest = None
    try:
        for cursor in matched:
            if not cursor.get("artifact_id") or not cursor.get("result_hash"):
                latest = "COMPLETION_UNKNOWN"
                codes.add(str(cursor.get("reason_code", cursor.get("status", "UNKNOWN")))[:160])
                continue
            payload, digest = _artifact(state, objects, cursor["artifact_id"], "WebToolResult")
            if (
                digest != cursor["result_hash"] or payload.get("request") != cursor["request"]
                or payload.get("operation_id") != cursor.get("operation_id")
                or payload.get("dispatch_id") != cursor.get("dispatch_id")
                or datetime.fromisoformat(payload["observed_at"]) < record.started_at
            ):
                raise ValueError("Web recovery receipt has a different request or generation")
            parent = state.artifact_record(cursor["artifact_id"])
            if parent is None or payload["raw_object_hash"] not in parent["input_hashes"]:
                raise ValueError("Web raw response is outside its artifact lineage")
            raw = _json_object(objects, payload["raw_object_hash"])
            hashes.update((digest, payload["raw_object_hash"]))
            artifacts.append(cursor["artifact_id"])
            codes.add(str(payload.get("reason_code", payload["status"]))[:160])
            discovered.update(hit["url"] for hit in payload.get("hits", []))
            kwargs["observed_at"] = datetime.fromisoformat(payload["observed_at"])
            if verified_empty_web_response(payload, raw):
                latest = "NO_NEW_EVIDENCE"
            elif payload["status"] == "SUCCEEDED" and payload.get("hits"):
                latest = "EVIDENCE_RETURNED"
            else:
                latest = "FAILED_OR_BLOCKED"
        if discovered:
            # A later empty callback cannot erase previously discovered unconsumed originals.
            latest = "EVIDENCE_RETURNED"
        if task.last_failure_code == "EXTERNAL_COMPLETION_UNKNOWN":
            latest = "COMPLETION_UNKNOWN"
        if latest is not None:
            kwargs.update(classification=latest, observed_record_count=len(discovered),
                          new_source_object_count=len(hashes),
                          uncovered_impact=(
                "Only the exact bounded query returned no hits; no whole-Web absence is claimed."
                if latest == "NO_NEW_EVIDENCE" else
                "Unconsumed leads, failed calls or unknown completion still affect this gap."
            ))
    except (OSError, ValueError, KeyError, TypeError):
        kwargs.update(classification="EVIDENCE_UNVERIFIABLE",
                      uncovered_impact="Web raw bytes or exact request binding cannot be verified.")
    kwargs.update(source_object_hashes=tuple(sorted(hashes)),
                  evidence_artifact_ids=tuple(sorted(set(artifacts))),
                  response_codes=tuple(sorted(codes))[:128])
    return PublicRecoveryRoute(**kwargs)


def build_public_recovery_coverage(
    state: StateStore, objects: ObjectStore, record: CurrentResearchContinuation,
) -> PublicRecoveryCoverage:
    """Read the declared source routes and actual receipts for this exact continuation."""
    raw, latest_hash = _artifact(
        state, objects, record.current_acquisition_report_artifact_id,
        "CurrentResearchAcquisitionReport",
    )
    latest = CurrentResearchAcquisitionReport.model_validate(raw)
    schedule_raw, schedule_hash = _artifact(
        state, objects, str(latest.schedule_artifact_id), "CurrentResearchSchedule",
    )
    schedule = CurrentResearchSchedule.model_validate(schedule_raw)
    if (
        latest.report_id != record.current_acquisition_report_artifact_id
        or schedule.schedule_id != latest.schedule_artifact_id
        or latest.company_id != record.company_id or latest.market is not record.market
        or schedule.company_id != record.company_id or schedule.market is not record.market
        or latest.policy_hash != schedule.policy_hash
    ):
        raise ValueError("recovery acquisition schedule differs from the request")
    reports = []
    hashes = {latest_hash, schedule_hash}
    for identity in record.acquisition_report_artifact_ids[-_MAX_REPORTS:]:
        value, digest = _artifact(state, objects, identity, "CurrentResearchAcquisitionReport")
        report = CurrentResearchAcquisitionReport.model_validate(value)
        if (
            report.company_id != record.company_id or report.market is not record.market
            or report.started_at < record.started_at or report.policy_hash != schedule.policy_hash
        ):
            continue
        reports.append((report, digest))
        hashes.add(digest)
    cursors, truncated = _web_cursors(state, record.continuation_id)
    routes = []
    steps = {step.capability: step for step in schedule.steps}
    pending = [task for task in record.external_tasks
               if task.status is not ExternalResearchTaskStatus.RESOLVED
               and not task.private_material_required]
    for task in pending:
        step = steps.get(task.capability)
        providers = tuple(dict.fromkeys(
            [*step.provider_candidates, *step.degraded_provider_candidates] if step else []
        ))
        if not providers:
            # An absent configured provider route is missing coverage, not an empty response.
            providers = ("UNDECLARED_PROVIDER_ROUTE",)
        routes.extend(_provider_route(state, objects, task, provider, tuple(reports))
                      for provider in providers[:32])
        routes.append(_web_route(state, objects, record, task, cursors))
        truncated = truncated or len(providers) > 32
    for route in routes:
        hashes.update(route.source_object_hashes)
    return PublicRecoveryCoverage(
        request_id=record.request_id, continuation_id=record.continuation_id,
        company_id=record.company_id, market=record.market, started_at=record.started_at,
        acquisition_report_artifact_id=latest.report_id, policy_hash=schedule.policy_hash,
        missing_capabilities=tuple(sorted({task.capability for task in pending}, key=str)),
        routes=tuple(routes), source_object_hashes=tuple(sorted(hashes)),
        truncated=truncated or len(record.acquisition_report_artifact_ids) > _MAX_REPORTS,
    )


__all__ = ["build_public_recovery_coverage", "verified_empty_web_response"]
