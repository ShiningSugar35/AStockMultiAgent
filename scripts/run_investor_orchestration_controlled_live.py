from __future__ import annotations

import argparse
import json
import uuid
from datetime import UTC, datetime
from pathlib import Path

import yaml

from astock.investor_orchestration.activation import (
    ActivationGateService,
)
from astock.investor_orchestration.macro import OfficialMacroCaptureService
from astock.investor_orchestration.models import (
    ControlledLiveCheck,
    ScheduledDomain,
    ScheduledRunRequest,
    ScheduledTaskBinding,
    ScheduledWindow,
)
from astock.investor_orchestration.paper_replay import CanonicalConfirmedPaperReplayAdapter
from astock.investor_orchestration.preflight import InvestorSessionPreflightService
from astock.investor_orchestration.scheduled import (
    ScheduledResearchService,
    SQLiteNotificationOutbox,
    policy_from_config,
)
from astock.investor_orchestration.scheduled_preparation import ScheduledSourcePreparationService
from astock.investor_orchestration.store import InvestorOrchestrationStore
from astock.investor_orchestration.utils import content_hash, utc_now


def _check(
    service: ActivationGateService,
    *,
    check_type: str,
    status: str,
    evidence_ids: tuple[str, ...] = (),
    details: str | None = None,
) -> ControlledLiveCheck:
    body = {
        "check_type": check_type,
        "checked_at": utc_now(),
        "status": status,
        "evidence_ids": evidence_ids,
        "details": details,
    }
    check = ControlledLiveCheck(
        check_id=f"controlled-live-{uuid.uuid5(uuid.NAMESPACE_URL, content_hash(body))}",
        **body,  # type: ignore[arg-type]
    )
    service.record_controlled_live(check)
    return check


def _matching_live_source_check(
    store: InvestorOrchestrationStore,
    request: ScheduledRunRequest,
) -> ControlledLiveCheck | None:
    check = store.latest_controlled_live_checks().get("SCHEDULED_SOURCE_PREPARATION")
    if (
        check is None
        or check.status != "PASS"
        or not request.source_coverage_artifact_ids
        or tuple(sorted(check.evidence_ids))
        != tuple(sorted(request.source_coverage_artifact_ids))
        or check.checked_at.tzinfo is None
        or check.checked_at < request.requested_at
        or check.checked_at > utc_now()
    ):
        return None
    return check


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--database", type=Path, default=Path(".ai-bridge/controlled-live/state.sqlite")
    )
    parser.add_argument(
        "--macro-authority",
        action="append",
        choices=("NBS", "PBOC", "MOF", "NDRC"),
        default=[],
        help="Explicit official authority to probe. Omit to keep macro gate NOT_RUN.",
    )
    parser.add_argument(
        "--allow-live-network",
        action="store_true",
        help="Required before any official macro network request is made.",
    )
    parser.add_argument(
        "--scheduled-request-file",
        type=Path,
        default=None,
        help=(
            "Prepared ScheduledRunRequest JSON already bound to this controlled DB. "
            "This proves recorded/prepared replay only unless a matching live-source check exists."
        ),
    )
    parser.add_argument(
        "--scheduled-binding-id",
        default=None,
        help="Existing controlled local binding whose pending bucket should be validated.",
    )
    parser.add_argument(
        "--schedule-bucket",
        default=None,
        help="Existing pending schedule bucket paired with --scheduled-binding-id.",
    )
    parser.add_argument(
        "--prepare-live-sources",
        action="store_true",
        help=(
            "Run the real bounded five-family source preparation for the selected pending bucket. "
            "Requires --allow-live-network and never fabricates semantic results."
        ),
    )
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    if not args.database.resolve().is_relative_to(root):
        parser.error("controlled validation database must stay inside the project")
    if bool(args.scheduled_binding_id) != bool(args.schedule_bucket):
        parser.error("scheduled-binding-id and schedule-bucket are required together")
    if args.scheduled_request_file is not None and args.scheduled_binding_id is not None:
        parser.error("choose a prepared request file or an existing pending bucket, not both")
    if args.prepare_live_sources and args.scheduled_binding_id is None:
        parser.error("prepare-live-sources requires an existing pending binding and bucket")
    if args.prepare_live_sources and not args.allow_live_network:
        parser.error("prepare-live-sources requires explicit --allow-live-network")
    if args.scheduled_request_file is not None:
        prepared_path = args.scheduled_request_file.resolve()
        if not prepared_path.is_relative_to(root) or not prepared_path.is_file():
            parser.error("prepared scheduled request must be an existing project-local file")

    store = InvestorOrchestrationStore(args.database)
    store.initialize()
    activation = ActivationGateService(store)
    checks: list[ControlledLiveCheck] = []
    controlled_started_at = utc_now()

    macro_evidence: list[str] = []
    macro_failures: list[str] = []
    if args.macro_authority and args.allow_live_network:
        macro = OfficialMacroCaptureService(store)
        for authority in args.macro_authority:
            structured_specs = [
                spec
                for spec in macro.specs()
                if spec.authority == authority and spec.parser != "document-only"
            ]
            if not structured_specs:
                macro_failures.append(f"{authority}:NO_STRUCTURED_CURRENT_FAMILY")
                continue
            for spec in structured_specs:
                try:
                    capture = macro.capture_checked(spec, live=True)
                    snapshot = capture.release
                    if (
                        snapshot.capture_mode == "LIVE"
                        and snapshot.parse_status == "PASS"
                        and snapshot.observations
                        and snapshot.capture_policy_hash
                    ):
                        macro_evidence.append(capture.capture_artifact_id)
                    else:
                        macro_failures.append(
                            f"{authority}/{spec.release_family}:CAPTURE_WITHOUT_CURRENT_STRUCTURED_PROOF"
                        )
                except Exception as exc:  # noqa: BLE001 - evidence is recorded, not hidden
                    macro_failures.append(
                        f"{authority}/{spec.release_family}:{type(exc).__name__}:{exc}"
                    )
        checks.append(
            _check(
                activation,
                check_type="CURRENT_MACRO",
                status="PASS" if macro_evidence and not macro_failures else "FAIL",
                evidence_ids=tuple(macro_evidence),
                details="; ".join(macro_failures) or "official raw-first capture succeeded",
            )
        )
    else:
        checks.append(
            _check(
                activation,
                check_type="CURRENT_MACRO",
                status="NOT_RUN",
                details="live network was not explicitly enabled",
            )
        )

    config = yaml.safe_load(
        Path("configs/scheduled_investor_tracking_v1.yaml").read_text(encoding="utf-8")
    )
    policy = policy_from_config(config)
    scheduled = ScheduledResearchService(
        store,
        InvestorSessionPreflightService(store),
        notification_sink=SQLiteNotificationOutbox(store),
        paper_replay_adapter=CanonicalConfirmedPaperReplayAdapter.from_store(store),
    )
    scheduled.register_policy(policy)
    now = datetime.now(UTC)
    live_source_check: ControlledLiveCheck | None = None
    scheduled_request_origin = "LOCAL_SMOKE"
    if args.scheduled_request_file is not None:
        prepared_request = ScheduledRunRequest.model_validate_json(
            args.scheduled_request_file.read_bytes()
        )
        prepared_binding = store.get_binding(prepared_request.binding_id)
        if prepared_binding is None or not prepared_binding.active:
            parser.error("prepared scheduled request references an unknown or inactive binding")
        if (
            prepared_binding.policy_id != policy.policy_id
            or prepared_binding.policy_hash != policy.policy_hash
            or prepared_request.policy_hash != policy.policy_hash
        ):
            parser.error("prepared scheduled request is not bound to the controlled policy")
        scheduled_request_origin = "PREPARED_REQUEST_FILE"
        live_source_check = _matching_live_source_check(store, prepared_request)
        receipt = scheduled.run(prepared_request)
    elif args.scheduled_binding_id is not None:
        checkpoint = store.get_scheduled_checkpoint(
            args.scheduled_binding_id,
            args.schedule_bucket,
        )
        if checkpoint is None:
            parser.error("selected controlled schedule bucket has no pending checkpoint")
        prepared_request = ScheduledRunRequest.model_validate(checkpoint["request"])
        prepared_binding = store.get_binding(prepared_request.binding_id)
        if prepared_binding is None or not prepared_binding.active:
            parser.error("selected controlled schedule bucket has an inactive binding")
        if (
            prepared_binding.policy_id != policy.policy_id
            or prepared_binding.policy_hash != policy.policy_hash
            or prepared_request.policy_hash != policy.policy_hash
        ):
            parser.error("selected controlled schedule bucket is not bound to the current policy")
        scheduled_request_origin = "PENDING_PRODUCT_BUCKET"
        if args.prepare_live_sources:
            try:
                preparation = ScheduledSourcePreparationService(store).prepare_pending(
                    prepared_request.binding_id,
                    prepared_request.schedule_bucket,
                    live=True,
                )
            except Exception as exc:  # noqa: BLE001 - persist the controlled failure class
                checks.append(
                    _check(
                        activation,
                        check_type="SCHEDULED_SOURCE_PREPARATION",
                        status="FAIL",
                        details=f"{type(exc).__name__}:{exc}",
                    )
                )
            else:
                prepared_request = preparation.request
                if preparation.logical_external_call_budget > 0:
                    live_source_check = _check(
                        activation,
                        check_type="SCHEDULED_SOURCE_PREPARATION",
                        status="PASS",
                        evidence_ids=preparation.source_report_ids,
                        details=(
                            "fresh bounded live five-family preparation completed for "
                            f"{len(preparation.subject_bindings)} exact domain/subject bindings; "
                            f"calls={preparation.logical_external_call_budget}; "
                            f"cutoff={preparation.evidence_cutoff.isoformat()}"
                        ),
                    )
                else:
                    live_source_check = _matching_live_source_check(store, prepared_request)
                    if live_source_check is None:
                        checks.append(
                            _check(
                                activation,
                                check_type="SCHEDULED_SOURCE_PREPARATION_REUSE",
                                status="BLOCKED",
                                details=(
                                    "existing prepared source reports have no matching prior "
                                    "controlled live source-preparation PASS"
                                ),
                            )
                        )
        else:
            live_source_check = _matching_live_source_check(store, prepared_request)
        receipt = scheduled.run(prepared_request)
    else:
        binding_key = content_hash(
            {"policy_hash": policy.policy_hash, "mode": "controlled-live-local"}
        )
        binding_id = f"controlled-live-{uuid.uuid5(uuid.NAMESPACE_URL, binding_key)}"
        existing_binding = store.get_binding(binding_id)
        binding = ScheduledTaskBinding(
            binding_id=binding_id,
            creation_mode="LOCAL_ONLY",
            execution_surface="LOCAL_DAEMON",
            schedule_expression="MANUAL_CONTROLLED_LIVE",
            timezone=policy.market_timezone,
            policy_id=policy.policy_id,
            policy_hash=policy.policy_hash,
            active=True,
            # Reuse the original confirmation, not a newly invented consent time.
            # register_binding still checks every other immutable field on replay.
            confirmed_at=existing_binding.confirmed_at if existing_binding is not None else now,
            consent_hash=content_hash("controlled-live-local-minimum-disclosure"),
        )
        scheduled.register_binding(binding)
        bucket = f"{now.date().isoformat()}:CONTROLLED_LIVE"
        run_key = content_hash(
            {
                "binding_id": binding.binding_id,
                "schedule_bucket": bucket,
                "policy_hash": policy.policy_hash,
            }
        )
        prepared_request = ScheduledRunRequest(
            run_id=f"controlled-run-{uuid.uuid5(uuid.NAMESPACE_URL, run_key)}",
            binding_id=binding.binding_id,
            schedule_bucket=bucket,
            window=ScheduledWindow.POST_CLOSE,
            domains=tuple(ScheduledDomain),
            requested_at=now,
            source_revision_set={},
            policy_hash=policy.policy_hash,
            idempotency_key=run_key,
        )
        receipt = scheduled.run(prepared_request)
    verified_coverage = (
        receipt.economic_write_count == 0
        and receipt.outcome.value in {"MATERIAL_CHANGE", "NO_MATERIAL_CHANGE"}
        and activation.evidence.valid_coverage(
            f"scheduled:{receipt.run_id}", receipt.capability_receipt_id
        )
    )
    completed_in_this_controlled_run = receipt.completed_at >= controlled_started_at
    schedule_pass = (
        verified_coverage
        and completed_in_this_controlled_run
        and live_source_check is not None
    )
    schedule_evidence = (
        (receipt.capability_receipt_id,)
        if schedule_pass and receipt.capability_receipt_id is not None
        else ()
    )
    if verified_coverage and not schedule_pass:
        recorded_evidence = (
            (receipt.capability_receipt_id,) if receipt.capability_receipt_id is not None else ()
        )
        checks.append(
            _check(
                activation,
                check_type="SCHEDULED_RESEARCH_RECORDED",
                status="PASS" if completed_in_this_controlled_run else "BLOCKED",
                evidence_ids=recorded_evidence if completed_in_this_controlled_run else (),
                details=(
                    f"origin={scheduled_request_origin}; authenticated scheduled result "
                    "is not controlled-live because no matching live source-preparation check "
                    "was bound to this finalization"
                    if completed_in_this_controlled_run
                    else (
                        "existing final receipt predates this controlled invocation; "
                        "replay is not a run"
                    )
                ),
            )
        )
    checks.append(
        _check(
            activation,
            check_type="SCHEDULED_RESEARCH",
            status="PASS" if schedule_pass else "BLOCKED",
            evidence_ids=schedule_evidence,
            details=(
                (
                    "fresh controlled finalization has authenticated coverage and a matching "
                    "prior/current live five-family source-preparation check"
                )
                if schedule_pass
                else (
                    f"NOT_CERTIFIED: origin={scheduled_request_origin}; "
                    f"outcome={receipt.outcome.value}; "
                    f"capability_receipt_id={receipt.capability_receipt_id}; "
                    f"completed_in_this_run={completed_in_this_controlled_run}; "
                    "live_source_check_id="
                    f"{None if live_source_check is None else live_source_check.check_id}"
                )
            ),
        )
    )
    # A manual smoke is not prospective observation evidence. No shadow row is
    # created here; the independent forward protocol must bind both real arms.

    print(
        json.dumps(
            {
                "database": str(args.database),
                "controlled_started_at": controlled_started_at.isoformat(),
                "scheduled_request_origin": scheduled_request_origin,
                "live_source_check_id": (
                    None if live_source_check is None else live_source_check.check_id
                ),
                "checks": [check.model_dump(mode="json") for check in checks],
                "scheduled_receipt": receipt.model_dump(mode="json"),
                "feature_activation_changed": False,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
