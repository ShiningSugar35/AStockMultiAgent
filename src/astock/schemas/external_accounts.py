"""Append-only external account, event, import, and projection contracts."""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal
from enum import StrEnum
from typing import Literal

from pydantic import AwareDatetime, Field, field_validator, model_validator

from astock.core.hashing import canonical_json_bytes, sha256_bytes
from astock.schemas.base import AStockModel
from astock.schemas.market import Market

_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_ACCOUNT_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"


class ExternalAccountKind(StrEnum):
    MANUAL = "MANUAL"
    BROKERAGE_IMPORT = "BROKERAGE_IMPORT"
    OTHER = "OTHER"


class ExternalAccountStatus(StrEnum):
    ACTIVE = "ACTIVE"
    CLOSED = "CLOSED"


class ExternalAccountEventType(StrEnum):
    TRADE = "TRADE"
    CASH_DEPOSIT = "CASH_DEPOSIT"
    CASH_WITHDRAWAL = "CASH_WITHDRAWAL"
    CASH_ADJUSTMENT = "CASH_ADJUSTMENT"
    SECURITY_TRANSFER_IN = "SECURITY_TRANSFER_IN"
    SECURITY_TRANSFER_OUT = "SECURITY_TRANSFER_OUT"
    REVERSAL = "REVERSAL"


class ExternalAccountIdentity(AStockModel):
    schema_version: str = "external-account-identity-v1"
    account_id: str = Field(pattern=_ACCOUNT_ID_PATTERN)
    display_name: str = Field(min_length=1, max_length=200)
    account_kind: ExternalAccountKind = ExternalAccountKind.MANUAL
    base_currency: Literal["CNY"] = "CNY"
    status: ExternalAccountStatus = ExternalAccountStatus.ACTIVE
    updated_at: AwareDatetime


class ExternalAccountEventDraft(AStockModel):
    """Validated economic/correction input before deterministic event-id binding."""

    schema_version: str = "external-account-event-draft-v1"
    account_id: str = Field(pattern=_ACCOUNT_ID_PATTERN)
    event_type: ExternalAccountEventType
    occurred_at: AwareDatetime
    sequence_no: int | None = Field(default=None, ge=0)
    available_to_system_at: AwareDatetime
    market: Market | None = None
    symbol: str | None = Field(default=None, pattern=r"^\d{6}$")
    side: Literal["BUY", "SELL"] | None = None
    quantity: int | None = Field(default=None, gt=0)
    price_cny: Decimal | None = Field(default=None, gt=0)
    amount_cny: Decimal | None = None
    currency: Literal["CNY"] = "CNY"
    reverses_event_id: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    replaces_event_id: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    source_artifact_hash: str | None = Field(default=None, pattern=_SHA256_PATTERN)
    idempotency_key: str = Field(min_length=1, max_length=500)
    note: str = Field(default="", max_length=2000)

    @model_validator(mode="after")
    def validate_event_shape(self) -> ExternalAccountEventDraft:
        if self.available_to_system_at < self.occurred_at:
            raise ValueError("external account event cannot be available before it occurred")
        if self.reverses_event_id is not None and self.replaces_event_id is not None:
            raise ValueError("event cannot both reverse and replace another event")
        if self.event_type is ExternalAccountEventType.REVERSAL:
            if self.reverses_event_id is None:
                raise ValueError("REVERSAL requires reverses_event_id")
            if any(
                value is not None
                for value in (
                    self.market,
                    self.symbol,
                    self.side,
                    self.quantity,
                    self.price_cny,
                    self.amount_cny,
                    self.replaces_event_id,
                )
            ):
                raise ValueError("REVERSAL cannot carry independent economic fields")
            return self
        if self.reverses_event_id is not None:
            raise ValueError("only REVERSAL may set reverses_event_id")
        if self.event_type is ExternalAccountEventType.TRADE:
            if (
                self.market is None
                or self.symbol is None
                or self.side is None
                or self.quantity is None
                or self.price_cny is None
            ):
                raise ValueError("TRADE requires market, symbol, side, quantity and price_cny")
            if self.amount_cny is not None:
                raise ValueError("TRADE amount is derived from quantity and price_cny")
            return self
        if self.event_type in {
            ExternalAccountEventType.SECURITY_TRANSFER_IN,
            ExternalAccountEventType.SECURITY_TRANSFER_OUT,
        }:
            if self.market is None or self.symbol is None or self.quantity is None:
                raise ValueError("security transfer requires market, symbol and quantity")
            if self.side is not None or self.amount_cny is not None:
                raise ValueError("security transfer cannot carry side or cash amount")
            if (
                self.event_type is ExternalAccountEventType.SECURITY_TRANSFER_IN
                and self.price_cny is None
            ):
                raise ValueError("SECURITY_TRANSFER_IN requires price_cny cost basis")
            return self
        if self.event_type in {
            ExternalAccountEventType.CASH_DEPOSIT,
            ExternalAccountEventType.CASH_WITHDRAWAL,
            ExternalAccountEventType.CASH_ADJUSTMENT,
        }:
            if self.amount_cny is None or self.amount_cny == 0:
                raise ValueError("cash event requires a non-zero amount_cny")
            if (
                self.event_type
                in {
                    ExternalAccountEventType.CASH_DEPOSIT,
                    ExternalAccountEventType.CASH_WITHDRAWAL,
                }
                and self.amount_cny < 0
            ):
                raise ValueError("deposit/withdrawal amount_cny must be positive")
            if any(
                value is not None
                for value in (self.market, self.symbol, self.side, self.quantity, self.price_cny)
            ):
                raise ValueError("cash event cannot carry security fields")
            return self
        raise ValueError("unsupported external account event type")


class ExternalAccountEvent(ExternalAccountEventDraft):
    schema_version: str = "external-account-event-v1"
    event_id: str = Field(pattern=_SHA256_PATTERN)

    @model_validator(mode="after")
    def validate_event_id(self) -> ExternalAccountEvent:
        if self.sequence_no is None:
            raise ValueError("bound external account event requires sequence_no")
        if self.event_id != external_account_event_id(self):
            raise ValueError("external account event id does not match canonical identity")
        if self.event_id in {self.reverses_event_id, self.replaces_event_id}:
            raise ValueError("external account event cannot reference itself")
        return self


class ExternalAccountImportFormat(StrEnum):
    CSV = "CSV"
    JSON = "JSON"


class ExternalAccountImportPreview(AStockModel):
    schema_version: str = "external-account-import-preview-v1"
    batch_id: str = Field(pattern=_SHA256_PATTERN)
    source_format: ExternalAccountImportFormat
    source_object_hash: str = Field(pattern=_SHA256_PATTERN)
    normalized_object_hash: str = Field(pattern=_SHA256_PATTERN)
    row_count: int = Field(ge=0)
    account_ids: list[str]
    event_ids: list[str]
    already_imported: bool = False

    @field_validator("account_ids", "event_ids")
    @classmethod
    def sorted_unique(cls, value: list[str]) -> list[str]:
        if value != sorted(set(value)):
            raise ValueError("external import identity lists must be sorted and unique")
        return value

    @model_validator(mode="after")
    def validate_row_count(self) -> ExternalAccountImportPreview:
        if self.row_count != len(self.event_ids):
            raise ValueError("external import row count must equal event count")
        return self


class ExternalAccountImportReceipt(AStockModel):
    schema_version: str = "external-account-import-receipt-v1"
    batch_id: str = Field(pattern=_SHA256_PATTERN)
    status: Literal["IMPORTED", "DUPLICATE"]
    source_object_hash: str = Field(pattern=_SHA256_PATTERN)
    normalized_object_hash: str = Field(pattern=_SHA256_PATTERN)
    inserted_event_ids: list[str]
    duplicate_event_ids: list[str]
    broker_execution_allowed: Literal[False] = False
    paper_ledger_write_allowed: Literal[False] = False

    @field_validator("inserted_event_ids", "duplicate_event_ids")
    @classmethod
    def deterministic_ids(cls, value: list[str]) -> list[str]:
        if value != sorted(set(value)):
            raise ValueError("external import receipt ids must be sorted and unique")
        return value


class ExternalAccountPosition(AStockModel):
    schema_version: str = "external-account-position-v1"
    account_id: str = Field(pattern=_ACCOUNT_ID_PATTERN)
    market: Market
    symbol: str = Field(pattern=r"^\d{6}$")
    quantity: int = Field(gt=0)
    average_cost_cny: Decimal = Field(gt=0)
    opened_at: AwareDatetime
    last_event_at: AwareDatetime


class ExternalAccountProjection(AStockModel):
    schema_version: str = "external-account-projection-v1"
    account_id: str = Field(pattern=_ACCOUNT_ID_PATTERN)
    as_of: AwareDatetime
    positions: list[ExternalAccountPosition]
    cash_cny: Decimal | None = None
    cash_known: bool = False
    active_event_count: int = Field(ge=0)
    total_event_count: int = Field(ge=0)

    @model_validator(mode="after")
    def validate_cash_semantics(self) -> ExternalAccountProjection:
        if self.cash_known != (self.cash_cny is not None):
            raise ValueError("cash_known must match whether cash_cny is available")
        return self


def external_account_event_identity(
    event: ExternalAccountEventDraft | ExternalAccountEvent,
) -> dict[str, object]:
    return {
        "schema_version": "external-account-event-v1",
        "account_id": event.account_id,
        "event_type": event.event_type.value,
        "occurred_at": event.occurred_at.isoformat(),
        "sequence_no": event.sequence_no if event.sequence_no is not None else 0,
        "available_to_system_at": event.available_to_system_at.isoformat(),
        "market": event.market.value if event.market is not None else None,
        "symbol": event.symbol,
        "side": event.side,
        "quantity": event.quantity,
        "price_cny": str(event.price_cny) if event.price_cny is not None else None,
        "amount_cny": str(event.amount_cny) if event.amount_cny is not None else None,
        "currency": event.currency,
        "reverses_event_id": event.reverses_event_id,
        "replaces_event_id": event.replaces_event_id,
        "source_artifact_hash": event.source_artifact_hash,
        "idempotency_key": event.idempotency_key,
        "note": event.note,
    }


def external_account_event_id(
    event: ExternalAccountEventDraft | ExternalAccountEvent,
) -> str:
    return sha256_bytes(canonical_json_bytes(external_account_event_identity(event)))


def bind_external_account_event(draft: ExternalAccountEventDraft) -> ExternalAccountEvent:
    sequence_no = draft.sequence_no if draft.sequence_no is not None else 0
    prepared = draft.model_copy(update={"sequence_no": sequence_no})
    payload = prepared.model_dump(mode="python", exclude={"schema_version", "created_at"})
    return ExternalAccountEvent(
        **payload,
        event_id=external_account_event_id(prepared),
        created_at=draft.created_at,
    )


def external_import_batch_id(
    *,
    source_format: ExternalAccountImportFormat,
    source_object_hash: str,
    normalized_object_hash: str,
    events: Sequence[ExternalAccountEvent],
) -> str:
    return sha256_bytes(
        canonical_json_bytes(
            {
                "schema_version": "external-account-import-batch-v1",
                "source_format": source_format.value,
                "source_object_hash": source_object_hash,
                "normalized_object_hash": normalized_object_hash,
                "event_ids": [item.event_id for item in events],
            }
        )
    )


__all__ = [
    "ExternalAccountEvent",
    "ExternalAccountEventDraft",
    "ExternalAccountEventType",
    "ExternalAccountIdentity",
    "ExternalAccountImportFormat",
    "ExternalAccountImportPreview",
    "ExternalAccountImportReceipt",
    "ExternalAccountKind",
    "ExternalAccountPosition",
    "ExternalAccountProjection",
    "ExternalAccountStatus",
    "bind_external_account_event",
    "external_account_event_id",
    "external_account_event_identity",
    "external_import_batch_id",
]
