"""TradeProtocol-bound paper execution preparation, status, recovery, and audit."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import cast

from astock.committee.repository import CommitteeRepository
from astock.core.hashing import canonical_json_bytes, content_hash, sha256_bytes
from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.paper_trading.operation import (
    PaperInstrumentTradingFacts,
    PaperReferenceVerifier,
    paper_confirmation_hash,
    paper_confirmation_signature_valid,
    paper_request_hash,
)
from astock.schemas import (
    CorporateActionObservation,
    InstrumentRecord,
    Market,
    OrderSide,
    PaperExecutionRequest,
    PaperOperationReport,
    PaperOperationRequest,
    PaperOrderValidity,
    PaperPlaceOrderPayload,
    PaperReferencePack,
    PaperUserConfirmation,
    TradeProtocol,
    TradeProtocolOutcome,
    TradingSession,
)
from astock.schemas.reference_data import DailyBarObservation


@dataclass(frozen=True, slots=True)
class PaperExecutionPreparation:
    execution_request_id: str
    execution_artifact_id: str
    execution_object_sha256: str
    operation_artifact_id: str
    operation_object_sha256: str
    execution_request: PaperExecutionRequest
    operation_request: PaperOperationRequest
    reused_existing: bool


class RecordedPaperReferenceVerifier(PaperReferenceVerifier):
    """Read one immutable recorded acceptance pack and reject every other identity."""

    def __init__(self, pack: PaperReferencePack) -> None:
        self.pack = pack

    def calendar(
        self,
        market: Market,
        release_id: str,
        *,
        visible_at: datetime,
    ) -> list[TradingSession]:
        self._require(market, release_id, self.pack.calendar_release_id, visible_at)
        return self.pack.sessions

    def instrument(
        self,
        market: Market,
        symbol: str,
        release_id: str,
        *,
        visible_at: datetime,
    ) -> InstrumentRecord:
        self._require(market, release_id, self.pack.instrument_release_id, visible_at)
        if symbol != self.pack.symbol:
            raise ValueError("recorded paper instrument symbol mismatch")
        return self.pack.instrument

    def daily(
        self,
        market: Market,
        symbol: str,
        release_id: str,
        *,
        visible_at: datetime,
    ) -> list[DailyBarObservation]:
        self._require(market, release_id, self.pack.daily_release_id, visible_at)
        if symbol != self.pack.symbol:
            raise ValueError("recorded paper daily symbol mismatch")
        return self.pack.daily_bars

    def corporate_actions(
        self,
        market: Market,
        release_id: str,
        *,
        visible_at: datetime,
    ) -> list[CorporateActionObservation]:
        raise ValueError("recorded Phase 6 pack does not authorize corporate actions")

    def trading_classification(
        self,
        instrument: InstrumentRecord,
        *,
        visible_at: datetime,
    ) -> PaperInstrumentTradingFacts:
        if (
            instrument.instrument_id != self.pack.instrument.instrument_id
            or visible_at > self.pack.visible_at
        ):
            raise ValueError("recorded paper classification identity mismatch")
        value = self.pack.classification
        return PaperInstrumentTradingFacts(
            board=value.board,
            risk_status=value.risk_status,
            fixed_price_limit_eligible=value.fixed_price_limit_eligible,
            suspension_status_verified=value.suspension_status_verified,
            suspended=value.suspended,
            evidence_id=value.evidence_id,
        )

    def _require(
        self,
        market: Market,
        release_id: str,
        expected_release_id: str,
        visible_at: datetime,
    ) -> None:
        if (
            market is not self.pack.market
            or release_id != expected_release_id
            or visible_at > self.pack.visible_at
        ):
            raise ValueError("recorded paper reference identity or visibility mismatch")


class PaperExecutionService:
    """Prepare a paper-only request without creating an order or consuming confirmation."""

    def __init__(self, state: StateStore, objects: ObjectStore) -> None:
        self.state = state
        self.objects = objects
        self.committee = CommitteeRepository(state, objects)

    def prepare(
        self,
        *,
        trade_protocol_id: str,
        reference_pack_artifact_id: str,
        account_id: str,
        idempotency_key: str,
        side: OrderSide,
        qty: int,
        limit_price_fen: int,
        requested_at: datetime,
        expires_at: datetime | None = None,
        fee_rule_version: str = "cn-a-share-paper-2026-07-13",
    ) -> PaperExecutionPreparation:
        if requested_at.tzinfo is None or requested_at.utcoffset() is None:
            raise ValueError("paper execution requested_at requires a timezone")
        requested_at = requested_at.astimezone(UTC)
        if expires_at is not None and (expires_at.tzinfo is None or expires_at.utcoffset() is None):
            raise ValueError("paper execution expires_at requires a timezone")
        expires_at = (expires_at or requested_at + timedelta(minutes=30)).astimezone(UTC)
        if expires_at <= requested_at:
            raise ValueError("paper execution expiry must follow requested_at")
        protocol_id = trade_protocol_id.removeprefix("TradeProtocol:")
        protocol, protocol_hash = self._load_protocol(protocol_id)
        reference_pack, reference_hash = self._load_reference_pack(reference_pack_artifact_id)
        self._validate_protocol(protocol, reference_pack, requested_at)

        input_hash = content_hash(
            {
                "trade_protocol_id": protocol.protocol_id,
                "trade_protocol_object_sha256": protocol_hash,
                "reference_pack_artifact_id": reference_pack_artifact_id,
                "reference_pack_object_sha256": reference_hash,
                "account_id": account_id,
                "idempotency_key": idempotency_key,
                "requested_at": requested_at,
                "expires_at": expires_at,
                "market": reference_pack.market.value,
                "symbol": reference_pack.symbol,
                "side": side.value,
                "qty": qty,
                "limit_price_fen": limit_price_fen,
                "fee_rule_version": fee_rule_version,
            }
        )
        existing = self._existing(account_id, idempotency_key)
        if existing is not None:
            if str(existing["input_hash"]) != input_hash:
                raise ValueError("paper execution idempotency key collision")
            return self._load_preparation(existing, reused_existing=True)

        operation = PaperOperationRequest(
            operation_id="0" * 64,
            account_id=account_id,
            idempotency_key=f"phase6:{idempotency_key}",
            requested_at=requested_at,
            expires_at=expires_at,
            payload=PaperPlaceOrderPayload(
                market=reference_pack.market,
                symbol=reference_pack.symbol,
                side=side,
                qty=qty,
                limit_price_fen=limit_price_fen,
                validity=PaperOrderValidity.DAY,
                calendar_release_id=reference_pack.calendar_release_id,
                instrument_release_id=reference_pack.instrument_release_id,
                daily_release_id=reference_pack.daily_release_id,
                fee_rule_version=fee_rule_version,
                created_at=requested_at,
            ),
            created_at=requested_at,
        )
        operation = operation.model_copy(update={"operation_id": paper_request_hash(operation)})
        execution = PaperExecutionRequest(
            trade_protocol_id=f"TradeProtocol:{protocol.protocol_id}",
            trade_protocol_object_sha256=protocol_hash,
            paper_reference_pack_artifact_id=reference_pack_artifact_id,
            paper_reference_pack_object_sha256=reference_hash,
            account_id=account_id,
            idempotency_key=idempotency_key,
            created_at=requested_at,
            paper_operation_request_id=operation.operation_id,
            market=reference_pack.market,
            symbol=reference_pack.symbol,
            side=side,
            qty=qty,
            limit_price_fen=limit_price_fen,
        )
        execution_artifact_id = f"PaperExecutionRequest:{input_hash}"
        operation_artifact_id = f"PaperOperationRequest:{operation.operation_id}"
        execution_ref = self.objects.put_json(execution.model_dump(mode="json"))
        operation_ref = self.objects.put_json(operation.model_dump(mode="json"))
        self.state.register_artifacts(
            [
                (
                    operation_artifact_id,
                    "PaperOperationRequest",
                    operation.schema_version,
                    operation_ref.sha256,
                    [protocol_hash, reference_hash],
                ),
                (
                    execution_artifact_id,
                    "PaperExecutionRequest",
                    execution.schema_version,
                    execution_ref.sha256,
                    [protocol_hash, reference_hash, operation_ref.sha256],
                ),
            ]
        )
        self._register(
            execution_request_id=input_hash,
            execution_artifact_id=execution_artifact_id,
            execution_object_hash=execution_ref.sha256,
            input_hash=input_hash,
            protocol=protocol,
            protocol_hash=protocol_hash,
            reference_pack_artifact_id=reference_pack_artifact_id,
            reference_hash=reference_hash,
            operation_artifact_id=operation_artifact_id,
            operation_hash=operation_ref.sha256,
            operation=operation,
            account_id=account_id,
            idempotency_key=idempotency_key,
            created_at=requested_at,
        )
        row = self._row(input_hash)
        assert row is not None
        return self._load_preparation(row, reused_existing=False)

    def load(self, execution_request_id: str) -> PaperExecutionPreparation:
        row = self._row(execution_request_id.removeprefix("PaperExecutionRequest:"))
        if row is None:
            raise ValueError("unknown paper execution request")
        return self._load_preparation(row, reused_existing=True)

    def reference_verifier(self, artifact_id: str) -> RecordedPaperReferenceVerifier:
        pack, _ = self._load_reference_pack(artifact_id)
        return RecordedPaperReferenceVerifier(pack)

    def mark_status(self, execution_request_id: str, status: str) -> None:
        allowed = {"COMPLETE", "REJECTED", "NEEDS_INFO", "INTERRUPTED"}
        if status not in allowed:
            raise ValueError("invalid paper execution terminal status")
        with self.state.transaction() as connection:
            normalized_id = execution_request_id.removeprefix("PaperExecutionRequest:")
            row = connection.execute(
                "SELECT status FROM paper_execution_request_index WHERE execution_request_id=?",
                (normalized_id,),
            ).fetchone()
            if row is None:
                raise ValueError("unknown paper execution request")
            current = str(row["status"])
            if current == status:
                return
            if current != "WAITING_USER_CONFIRMATION":
                raise ValueError("paper execution terminal status is immutable")
            updated = connection.execute(
                "UPDATE paper_execution_request_index SET status=? "
                "WHERE execution_request_id=? AND status='WAITING_USER_CONFIRMATION'",
                (status, normalized_id),
            ).rowcount
            if updated != 1:
                raise ValueError("paper execution status transition lost a race")

    def mark_operation_status(self, operation_id: str, status: str) -> bool:
        allowed = {"COMPLETE", "REJECTED", "NEEDS_INFO", "INTERRUPTED"}
        if status not in allowed:
            raise ValueError("invalid paper execution terminal status")
        with self.state.transaction() as connection:
            updated = connection.execute(
                "UPDATE paper_execution_request_index SET status=? WHERE operation_id=?",
                (status, operation_id),
            ).rowcount
        return updated == 1

    def status(self, execution_request_id: str) -> dict[str, object]:
        row = self._row(execution_request_id.removeprefix("PaperExecutionRequest:"))
        if row is None:
            return {"status": "NOT_RUN", "execution_request_id": execution_request_id}
        result = dict(row)
        operation_status = self._operation_status(str(row["operation_id"]))
        result["operation_status"] = operation_status
        return result

    def recover(self, execution_request_id: str) -> dict[str, object]:
        preparation = self.load(execution_request_id)
        row = self._row(preparation.execution_request_id)
        assert row is not None
        operation_status = self._operation_status(preparation.operation_request.operation_id)
        reconciled = False
        if str(row["status"]) == "WAITING_USER_CONFIRMATION" and operation_status in {
            "COMPLETE",
            "REJECTED",
            "NEEDS_INFO",
            "INTERRUPTED",
        }:
            self.mark_status(preparation.execution_request_id, operation_status)
            reconciled = True
        audit = self.audit(preparation.execution_request_id)
        return {
            "status": (
                "RECOVERED_OR_ALREADY_COMPLETE" if audit["status"] == "PASS" else "NEEDS_INFO"
            ),
            "execution_request_id": preparation.execution_request_id,
            "reconciled_terminal_status": reconciled,
            "audit": audit,
        }

    def audit(self, execution_request_id: str) -> dict[str, object]:
        normalized_id = execution_request_id.removeprefix("PaperExecutionRequest:")
        row = self._row(normalized_id)
        if row is None:
            return {
                "status": "NOT_RUN",
                "execution_request_id": normalized_id,
                "finding_codes": ["PAPER_EXECUTION_NOT_RUN"],
            }
        findings: set[str] = set()
        try:
            preparation = self._load_preparation(row, reused_existing=True)
        except (OSError, ValueError):
            return {
                "status": "PARTIAL",
                "execution_request_id": normalized_id,
                "finding_codes": ["PAPER_EXECUTION_ARTIFACT_INVALID"],
            }
        execution = preparation.execution_request
        operation = preparation.operation_request
        if paper_request_hash(operation) != operation.operation_id:
            findings.add("OPERATION_REQUEST_HASH_MISMATCH")
        if execution.paper_operation_request_id != operation.operation_id:
            findings.add("EXECUTION_OPERATION_BINDING_MISMATCH")
        try:
            protocol, protocol_hash = self._load_protocol(
                execution.trade_protocol_id.removeprefix("TradeProtocol:")
            )
            if (
                protocol_hash != execution.trade_protocol_object_sha256
                or protocol.outcome is not TradeProtocolOutcome.APPROVE_SIMULATION
                or protocol.broker_execution_allowed
            ):
                findings.add("TRADE_PROTOCOL_BINDING_MISMATCH")
        except (OSError, ValueError):
            findings.add("TRADE_PROTOCOL_BINDING_MISMATCH")
        try:
            _, reference_hash = self._load_reference_pack(
                cast(str, execution.paper_reference_pack_artifact_id)
            )
            if reference_hash != execution.paper_reference_pack_object_sha256:
                findings.add("REFERENCE_PACK_BINDING_MISMATCH")
        except (OSError, ValueError):
            findings.add("REFERENCE_PACK_BINDING_MISMATCH")
        operation_status = self._operation_status(operation.operation_id)
        confirmation_id: str | None = None
        authorization_key_id: str | None = None
        authorization_key_object_sha256: str | None = None
        order_id: str | None = None
        if str(row["status"]) == "COMPLETE":
            if operation_status != "COMPLETE":
                findings.add("PAPER_OPERATION_NOT_COMPLETE")
            with self.state.connect() as connection:
                committed = connection.execute(
                    "SELECT r.request_hash,r.request_object_hash,c.confirmation_id,"
                    "c.confirmation_hash,c.confirmation_object_hash,c.key_id,"
                    "e.report_object_hash,b.order_id,b.confirmation_id AS bound_confirmation_id,"
                    "b.confirmation_hash AS bound_confirmation_hash,"
                    "b.authorization_key_id,k.key_id AS frozen_key_id,"
                    "k.public_key_object_hash "
                    "FROM paper_operation_request r "
                    "LEFT JOIN paper_operation_confirmation c "
                    "ON c.operation_id=r.operation_id "
                    "LEFT JOIN paper_confirmation_key_binding k "
                    "ON k.confirmation_id=c.confirmation_id "
                    "LEFT JOIN paper_operation_execution e "
                    "ON e.operation_id=r.operation_id "
                    "LEFT JOIN paper_order_rule_binding b "
                    "ON b.operation_id=r.operation_id "
                    "WHERE r.operation_id=?",
                    (operation.operation_id,),
                ).fetchone()
            if committed is None:
                findings.add("PAPER_OPERATION_AUDIT_CHAIN_MISSING")
            else:
                confirmation_id = (
                    str(committed["confirmation_id"])
                    if committed["confirmation_id"] is not None
                    else None
                )
                authorization_key_id = (
                    str(committed["authorization_key_id"])
                    if committed["authorization_key_id"] is not None
                    else None
                )
                authorization_key_object_sha256 = (
                    str(committed["public_key_object_hash"])
                    if committed["public_key_object_hash"] is not None
                    else None
                )
                order_id = str(committed["order_id"]) if committed["order_id"] is not None else None
                if str(committed["request_hash"]) != operation.operation_id:
                    findings.add("PAPER_OPERATION_STORED_REQUEST_HASH_MISMATCH")
                try:
                    stored_request = PaperOperationRequest.model_validate_json(
                        self.objects.get_bytes(str(committed["request_object_hash"]))
                    )
                    if paper_request_hash(stored_request) != operation.operation_id:
                        findings.add("PAPER_OPERATION_STORED_REQUEST_INVALID")
                except (OSError, ValueError):
                    findings.add("PAPER_OPERATION_STORED_REQUEST_INVALID")
                confirmation: PaperUserConfirmation | None = None
                try:
                    confirmation = PaperUserConfirmation.model_validate_json(
                        self.objects.get_bytes(str(committed["confirmation_object_hash"]))
                    )
                    if (
                        paper_confirmation_hash(confirmation) != str(committed["confirmation_hash"])
                        or confirmation.confirmation_id != confirmation_id
                        or confirmation.operation_id != operation.operation_id
                        or confirmation.request_hash != operation.operation_id
                    ):
                        findings.add("PAPER_CONFIRMATION_BINDING_MISMATCH")
                except (OSError, ValueError):
                    findings.add("PAPER_CONFIRMATION_BINDING_MISMATCH")
                try:
                    if (
                        confirmation is None
                        or authorization_key_object_sha256 is None
                        or str(committed["frozen_key_id"]) != confirmation.key_id
                        or not self.objects.verify(authorization_key_object_sha256)
                        or not paper_confirmation_signature_valid(
                            confirmation,
                            self.objects.get_bytes(authorization_key_object_sha256),
                        )
                    ):
                        findings.add("PAPER_CONFIRMATION_SIGNATURE_INVALID")
                except OSError:
                    findings.add("PAPER_CONFIRMATION_SIGNATURE_INVALID")
                try:
                    report = PaperOperationReport.model_validate_json(
                        self.objects.get_bytes(str(committed["report_object_hash"]))
                    )
                    if (
                        report.operation_id != operation.operation_id
                        or report.confirmation_id != confirmation_id
                        or report.status.value != "COMPLETE"
                    ):
                        findings.add("PAPER_OPERATION_REPORT_BINDING_MISMATCH")
                except (OSError, ValueError):
                    findings.add("PAPER_OPERATION_REPORT_BINDING_MISMATCH")
                if order_id is None:
                    findings.add("PAPER_ORDER_BINDING_MISSING")
                if (
                    str(committed["bound_confirmation_id"]) != confirmation_id
                    or str(committed["bound_confirmation_hash"])
                    != str(committed["confirmation_hash"])
                    or authorization_key_id != str(committed["key_id"])
                ):
                    findings.add("PAPER_ORDER_AUTHORIZATION_BINDING_MISMATCH")
        return {
            "status": "PASS" if not findings else "PARTIAL",
            "execution_request_id": normalized_id,
            "operation_id": operation.operation_id,
            "execution_status": str(row["status"]),
            "operation_status": operation_status,
            "confirmation_id": confirmation_id,
            "authorization_key_id": authorization_key_id,
            "authorization_key_object_sha256": authorization_key_object_sha256,
            "paper_order_id": order_id,
            "finding_codes": sorted(findings),
        }

    def _load_protocol(self, protocol_id: str) -> tuple[TradeProtocol, str]:
        summary = self.committee.protocol_summary(protocol_id)
        protocol = self.committee.get_protocol(protocol_id)
        if summary is None or protocol is None:
            raise ValueError("trade protocol is unavailable")
        object_hash = str(summary["object_hash"])
        if not self.objects.verify(object_hash):
            raise ValueError("trade protocol object is unavailable")
        return protocol, object_hash

    def _load_reference_pack(
        self,
        artifact_id: str,
    ) -> tuple[PaperReferencePack, str]:
        with self.state.connect() as connection:
            row = connection.execute(
                "SELECT type,object_hash FROM artifact_registry WHERE artifact_id=?",
                (artifact_id,),
            ).fetchone()
        if row is None or str(row["type"]) != "PaperReferencePack":
            raise ValueError("paper reference pack artifact is unavailable")
        object_hash = str(row["object_hash"])
        if not self.objects.verify(object_hash):
            raise ValueError("paper reference pack object is unavailable")
        return (
            PaperReferencePack.model_validate_json(self.objects.get_bytes(object_hash)),
            object_hash,
        )

    @staticmethod
    def _validate_protocol(
        protocol: TradeProtocol,
        reference_pack: PaperReferencePack,
        requested_at: datetime,
    ) -> None:
        if protocol.outcome is not TradeProtocolOutcome.APPROVE_SIMULATION:
            raise ValueError("trade protocol does not approve simulation")
        if (
            not protocol.paper_simulation_allowed
            or not protocol.ledger_write_allowed
            or protocol.broker_execution_allowed
        ):
            raise ValueError("trade protocol execution gates are not paper-only")
        if protocol.company_id != reference_pack.symbol:
            raise ValueError("trade protocol company does not match reference pack")
        if requested_at < protocol.earliest_executable_time.astimezone(UTC):
            raise ValueError("paper execution precedes the protocol effective time")
        if requested_at > reference_pack.visible_at.astimezone(UTC):
            raise ValueError("paper execution is later than the recorded reference cutoff")

    def _register(
        self,
        *,
        execution_request_id: str,
        execution_artifact_id: str,
        execution_object_hash: str,
        input_hash: str,
        protocol: TradeProtocol,
        protocol_hash: str,
        reference_pack_artifact_id: str,
        reference_hash: str,
        operation_artifact_id: str,
        operation_hash: str,
        operation: PaperOperationRequest,
        account_id: str,
        idempotency_key: str,
        created_at: datetime,
    ) -> None:
        with self.state.transaction() as connection:
            existing = connection.execute(
                "SELECT input_hash FROM paper_execution_request_index "
                "WHERE account_id=? AND idempotency_key=?",
                (account_id, idempotency_key),
            ).fetchone()
            if existing is not None:
                if str(existing["input_hash"]) != input_hash:
                    raise ValueError("paper execution idempotency key collision")
                return
            connection.execute(
                "INSERT INTO paper_execution_request_index("
                "execution_request_id,artifact_id,object_hash,input_hash,trade_protocol_id,"
                "trade_protocol_object_hash,reference_pack_artifact_id,"
                "reference_pack_object_hash,operation_artifact_id,operation_object_hash,"
                "operation_id,account_id,idempotency_key,status,created_at"
                ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    execution_request_id,
                    execution_artifact_id,
                    execution_object_hash,
                    input_hash,
                    protocol.protocol_id,
                    protocol_hash,
                    reference_pack_artifact_id,
                    reference_hash,
                    operation_artifact_id,
                    operation_hash,
                    operation.operation_id,
                    account_id,
                    idempotency_key,
                    "WAITING_USER_CONFIRMATION",
                    created_at.isoformat(),
                ),
            )

    def _load_preparation(
        self,
        row,
        *,
        reused_existing: bool,
    ) -> PaperExecutionPreparation:
        execution_hash = str(row["object_hash"])
        operation_hash = str(row["operation_object_hash"])
        if not self.objects.verify(execution_hash) or not self.objects.verify(operation_hash):
            raise ValueError("paper execution preparation object is unavailable")
        execution = PaperExecutionRequest.model_validate_json(
            self.objects.get_bytes(execution_hash)
        )
        operation = PaperOperationRequest.model_validate_json(
            self.objects.get_bytes(operation_hash)
        )
        return PaperExecutionPreparation(
            execution_request_id=str(row["execution_request_id"]),
            execution_artifact_id=str(row["artifact_id"]),
            execution_object_sha256=execution_hash,
            operation_artifact_id=str(row["operation_artifact_id"]),
            operation_object_sha256=operation_hash,
            execution_request=execution,
            operation_request=operation,
            reused_existing=reused_existing,
        )

    def _row(self, execution_request_id: str):
        with self.state.connect() as connection:
            return connection.execute(
                "SELECT * FROM paper_execution_request_index WHERE execution_request_id=?",
                (execution_request_id,),
            ).fetchone()

    def _existing(self, account_id: str, idempotency_key: str):
        with self.state.connect() as connection:
            return connection.execute(
                "SELECT * FROM paper_execution_request_index "
                "WHERE account_id=? AND idempotency_key=?",
                (account_id, idempotency_key),
            ).fetchone()

    def _operation_status(self, operation_id: str) -> str | None:
        with self.state.connect() as connection:
            row = connection.execute(
                "SELECT status FROM paper_operation_execution WHERE operation_id=?",
                (operation_id,),
            ).fetchone()
        return str(row["status"]) if row is not None else None


def paper_reference_pack_hash(pack: PaperReferencePack) -> str:
    return sha256_bytes(canonical_json_bytes(pack.model_dump(mode="json")))


__all__ = [
    "PaperExecutionPreparation",
    "PaperExecutionService",
    "RecordedPaperReferenceVerifier",
    "paper_reference_pack_hash",
]
