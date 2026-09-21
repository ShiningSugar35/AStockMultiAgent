"""Domain validation for the currently wired company-run takeover CLI."""

from __future__ import annotations

from collections.abc import Callable

from astock.core.hashing import content_hash
from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.schemas.research_runtime import ResearchRunAudit, ResearchRunReport, ResearchRunRequest
from astock.schemas.research_sla import ResearchSchedulerTask


def validate_research_run_takeover(
    artifact_id: str,
    *,
    task: ResearchSchedulerTask,
    state: StateStore,
    objects: ObjectStore,
    audit_run: Callable[[str], ResearchRunAudit],
) -> None:
    """Accept only a canonical report for the exact failed company request.

    Other node contracts need their own domain validator before the CLI can apply
    their repair. A registered type name alone is not evidence of a valid result.
    This validator does not turn NEEDS_INFO into a useful/complete research answer.
    """
    record = state.artifact_record(artifact_id)
    if record is None or str(record["type"]) != "ResearchRunReport":
        raise ValueError("no configured CLI takeover validator for this output contract")
    if not task.node_id.startswith("company-run-") or task.candidate_id is None:
        raise ValueError("no configured CLI takeover validator for this task node")
    object_hash = str(record["object_hash"])
    if not objects.verify(object_hash):
        raise ValueError("takeover report object failed integrity verification")
    report = ResearchRunReport.model_validate_json(objects.get_bytes(object_hash))
    if artifact_id != f"ResearchRunReport:{report.report_id}":
        raise ValueError("takeover report artifact identity does not match its payload")
    if task.candidate_id not in {
        f"XSHG:{report.company_id}",
        f"XSHE:{report.company_id}",
        f"BJSE:{report.company_id}",
    }:
        raise ValueError("takeover report belongs to another company")
    request_record = state.artifact_record(report.request_artifact_id)
    if (
        request_record is None
        or str(request_record["type"]) != "ResearchRunRequest"
        or str(request_record["object_hash"]) != report.request_object_hash
        or not objects.verify(report.request_object_hash)
    ):
        raise ValueError("takeover report request provenance is unavailable or changed")
    request = ResearchRunRequest.model_validate_json(objects.get_bytes(report.request_object_hash))
    if (
        request.company_id != report.company_id
        or content_hash(request.model_dump(mode="json", exclude={"created_at"}))
        != task.input_fingerprint
    ):
        raise ValueError("takeover report does not match the failed task input contract")
    audit = audit_run(report.run_id)
    if audit.status != "PASS" or audit.latest_report_artifact_id != artifact_id:
        raise ValueError("takeover report is not the audited canonical result for its research run")
