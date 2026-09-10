"""Immutable receipts over canonical external-account events and provisional assertions."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Literal, cast

from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.investor_orchestration.models import ProvisionalPositionAssertion
from astock.investor_orchestration.store import InvestorOrchestrationStore
from astock.investor_orchestration.utils import content_hash
from astock.schemas.external_accounts import ExternalAccountEvent, ExternalAccountOperationReceipt

_ASSERTION_SCHEMA = "provisional-position-assertion-v1"
_OperationStatus = Literal["RECORDED", "PROVISIONAL", "NO_CHANGE"]
_DatePrecisionValue = Literal["EXACT", "DATE_ONLY", "MONTH_ONLY", "UNKNOWN"]
_ALLOWED_OPERATION_STATUSES = frozenset({"RECORDED", "PROVISIONAL", "NO_CHANGE"})
_ALLOWED_DATE_PRECISIONS = frozenset({"EXACT", "DATE_ONLY", "MONTH_ONLY", "UNKNOWN"})


def _operation_status(value: str) -> _OperationStatus:
    normalized = value.strip().upper()
    if normalized not in _ALLOWED_OPERATION_STATUSES:
        raise ValueError("unsupported external-account operation status")
    return cast(_OperationStatus, normalized)


def _date_precision(value: str) -> _DatePrecisionValue:
    if value not in _ALLOWED_DATE_PRECISIONS:
        raise ValueError("unsupported external-account date precision")
    return cast(_DatePrecisionValue, value)


class ExternalAccountOperationReceiptService:
    """Register references without creating a second account/position fact store."""

    def __init__(
        self,
        store: InvestorOrchestrationStore,
        objects: ObjectStore | None = None,
    ) -> None:
        self.store = store
        self.state = StateStore(store.path)
        self.objects = objects or ObjectStore(store.path.parent / "objects" / "sha256")

    def freeze(
        self,
        *,
        request_id: str,
        as_of: datetime,
        status: str,
        events: Sequence[ExternalAccountEvent] = (),
        assertions: Sequence[ProvisionalPositionAssertion] = (),
        duplicate_event_ids: Sequence[str] = (),
    ) -> tuple[str, ExternalAccountOperationReceipt]:
        if as_of.tzinfo is None or as_of.utcoffset() is None:
            raise ValueError("external-account operation cutoff must be timezone-aware")
        if not request_id.strip():
            raise ValueError("external-account operation request_id is required")

        bindings: dict[str, str] = {}
        event_artifact_ids: list[str] = []
        assertion_artifact_ids: list[str] = []
        accounts: set[str] = set()
        precisions: set[_DatePrecisionValue] = set()

        for event in events:
            if event.available_to_system_at > as_of or event.created_at > as_of:
                raise ValueError(
                    "exact external-account event was not available at the operation cutoff"
                )
            artifact_id = f"ExternalAccountEvent:{event.event_id}"
            digest = self._register_model(
                artifact_id,
                "ExternalAccountEvent",
                event.schema_version,
                event.model_dump(mode="json"),
            )
            event_artifact_ids.append(artifact_id)
            bindings[artifact_id] = digest
            accounts.add(event.account_id)
            precisions.add(_date_precision(event.occurred_at_precision))

        for assertion in assertions:
            if assertion.asserted_at.tzinfo is None or assertion.asserted_at.utcoffset() is None:
                raise ValueError("provisional position assertion timestamp must be timezone-aware")
            if assertion.asserted_at > as_of:
                raise ValueError(
                    "provisional position assertion is later than the operation cutoff"
                )
            if not assertion.source_text.strip():
                raise ValueError("provisional position assertion must preserve its source text")
            artifact_id = f"ProvisionalPositionAssertion:{assertion.assertion_id}"
            digest = self._register_model(
                artifact_id,
                "ProvisionalPositionAssertion",
                _ASSERTION_SCHEMA,
                assertion.model_dump(mode="json"),
            )
            assertion_artifact_ids.append(artifact_id)
            bindings[artifact_id] = digest
            accounts.add(assertion.account_id)
            precisions.add(_date_precision(assertion.date_precision.value))

        if not accounts:
            raise ValueError(
                "external-account operation receipt requires a canonical account reference"
            )
        normalized_status = _operation_status(status)
        seed = {
            "schema_version": "external-account-operation-receipt-v1",
            "request_id": request_id,
            "as_of": as_of.isoformat(),
            "status": normalized_status,
            "account_ids": sorted(accounts),
            "event_artifact_ids": sorted(event_artifact_ids),
            "provisional_assertion_artifact_ids": sorted(assertion_artifact_ids),
            "duplicate_event_ids": sorted(set(duplicate_event_ids)),
            "date_precisions": sorted(precisions),
            "artifact_object_hashes": dict(sorted(bindings.items())),
        }
        receipt = ExternalAccountOperationReceipt(
            operation_id=f"external-account-operation:{content_hash(seed)}",
            request_id=request_id,
            as_of=as_of,
            status=normalized_status,
            account_ids=sorted(accounts),
            event_artifact_ids=sorted(event_artifact_ids),
            provisional_assertion_artifact_ids=sorted(assertion_artifact_ids),
            duplicate_event_ids=sorted(set(duplicate_event_ids)),
            date_precisions=sorted(precisions),
            artifact_object_hashes=dict(sorted(bindings.items())),
            created_at=as_of,
        )
        artifact_id = f"ExternalAccountOperationReceipt:{receipt.operation_id}"
        self._register_model(
            artifact_id,
            "ExternalAccountOperationReceipt",
            receipt.schema_version,
            receipt.model_dump(mode="json"),
            input_hashes=sorted(bindings.values()),
        )
        return artifact_id, receipt

    def _register_model(
        self,
        artifact_id: str,
        artifact_type: str,
        schema_version: str,
        payload: object,
        *,
        input_hashes: list[str] | None = None,
    ) -> str:
        ref = self.objects.put_json(payload)
        existing = self.state.artifact_record(artifact_id)
        if existing is not None:
            if (
                str(existing["type"]) != artifact_type
                or str(existing["schema_version"]) != schema_version
                or str(existing["object_hash"]) != ref.sha256
                or sorted(existing["input_hashes"]) != sorted(input_hashes or [])
            ):
                raise ValueError("external-account operation artifact identity collision")
            return ref.sha256
        self.state.register_artifact(
            artifact_id=artifact_id,
            artifact_type=artifact_type,
            schema_version=schema_version,
            object_hash=ref.sha256,
            input_hashes=sorted(input_hashes or []),
        )
        return ref.sha256


__all__ = ["ExternalAccountOperationReceiptService"]
