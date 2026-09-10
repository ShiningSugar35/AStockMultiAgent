"""Safe public orchestration receipts for paper-order preparation."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.investor_orchestration.store import InvestorOrchestrationStore
from astock.schemas.paper import PaperOperationRequest, PaperPreparationReceipt


class PaperPreparationReceiptService:
    """Freeze prepare/needs-info state without creating orders or positions."""

    def __init__(
        self,
        store: InvestorOrchestrationStore,
        objects: ObjectStore | None = None,
    ) -> None:
        self.store = store
        self.state = StateStore(store.path)
        self.objects = objects or ObjectStore(store.path.parent / "objects" / "sha256")

    def needs_info(
        self,
        *,
        request_id: str,
        account_id: str,
        as_of: datetime,
        instrument_id: str | None,
        side: Literal["BUY", "SELL"] | None,
        quantity: int | None,
        missing_fields: list[str],
    ) -> tuple[str, PaperPreparationReceipt]:
        receipt = PaperPreparationReceipt(
            request_id=request_id,
            account_id=account_id,
            status="NEEDS_INFO",
            instrument_id=instrument_id,
            side=side,
            quantity=quantity,
            missing_fields=sorted(set(missing_fields)),
            created_at=as_of,
        )
        return self._freeze(receipt)

    def ready(
        self,
        *,
        request_id: str,
        as_of: datetime,
        operation_artifact_id: str,
        operation_request: PaperOperationRequest,
    ) -> tuple[str, PaperPreparationReceipt]:
        record = self.state.artifact_record(operation_artifact_id)
        if record is None or str(record["type"]) != "PaperOperationRequest":
            raise ValueError("paper preparation operation artifact is unavailable")
        object_hash = str(record["object_hash"])
        stored = PaperOperationRequest.model_validate_json(self.objects.get_bytes(object_hash))
        if stored != operation_request:
            raise ValueError("paper preparation operation bytes differ from the supplied request")
        if operation_request.requested_at > as_of or operation_request.created_at > as_of:
            raise ValueError("paper operation was not available at the preparation cutoff")
        payload = operation_request.payload
        instrument_id = None
        side = None
        quantity = None
        if payload.operation_type == "PLACE_ORDER":
            instrument_id = f"{payload.market.value}:{payload.symbol}"
            side = payload.side.value
            quantity = payload.qty
        receipt = PaperPreparationReceipt(
            request_id=request_id,
            account_id=operation_request.account_id,
            status="READY_FOR_CONFIRMATION",
            instrument_id=instrument_id,
            side=side,
            quantity=quantity,
            operation_artifact_id=operation_artifact_id,
            operation_object_hash=object_hash,
            created_at=as_of,
        )
        return self._freeze(receipt, input_hashes=[object_hash])

    def _freeze(
        self,
        receipt: PaperPreparationReceipt,
        *,
        input_hashes: list[str] | None = None,
    ) -> tuple[str, PaperPreparationReceipt]:
        ref = self.objects.put_json(receipt.model_dump(mode="json"))
        artifact_id = f"PaperPreparationReceipt:{ref.sha256}"
        existing = self.state.artifact_record(artifact_id)
        if existing is None:
            self.state.register_artifact(
                artifact_id=artifact_id,
                artifact_type="PaperPreparationReceipt",
                schema_version=receipt.schema_version,
                object_hash=ref.sha256,
                input_hashes=sorted(input_hashes or []),
            )
        elif (
            str(existing["type"]) != "PaperPreparationReceipt"
            or str(existing["schema_version"]) != receipt.schema_version
            or str(existing["object_hash"]) != ref.sha256
            or sorted(existing["input_hashes"]) != sorted(input_hashes or [])
        ):
            raise ValueError("paper preparation receipt identity collision")
        return artifact_id, receipt


__all__ = ["PaperPreparationReceiptService"]
