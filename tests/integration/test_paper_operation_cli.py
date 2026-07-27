from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from typer.testing import CliRunner

from astock.cli import app
from astock.core.hashing import content_hash
from astock.paper_trading import paper_request_hash
from astock.schemas import (
    CommitteeEntryOrderType,
    CommitteeProtocolStatus,
    CommitteeVerdict,
    Market,
    OrderSide,
    PaperExecutionRequest,
    PaperOperationReport,
    PaperOperationRequest,
    PaperOperationStatus,
    PaperOrderValidity,
    PaperPlaceOrderPayload,
    PaperUserConfirmation,
    TradeProtocol,
)

RUNNER = CliRunner()


def _protocol(
    *,
    symbol: str = "300750",
    status: CommitteeProtocolStatus = CommitteeProtocolStatus.ACTIVE,
    broker_execution_allowed: bool = False,
    ledger_write_allowed: bool = False,
    verdict: CommitteeVerdict = CommitteeVerdict.PAPER_ELIGIBLE,
) -> TradeProtocol:
    now = datetime.now(UTC)
    blocking_codes: list[str] = []
    if status is CommitteeProtocolStatus.BLOCKED:
        blocking_codes.append("VERDICT_BLOCKED")
    return TradeProtocol(
        protocol_id="trade-protocol:fixture",
        decision_id="decision:fixture",
        decision_sha256="a" * 64,
        company_id=symbol,
        verdict=verdict,
        protocol_status=status,
        blocking_codes=blocking_codes,
        strategy_id="strategy-fixture",
        skill_versions={"fixture": "v1"},
        signal_time=now,
        earliest_executable_time=now + timedelta(hours=1),
        holding_horizon_days=30,
        entry_rule="test entry",
        entry_order_type=CommitteeEntryOrderType.PAPER_LIMIT,
        position_size_rule="test position-size",
        price_stop_rule="test price stop",
        volatility_stop_rule="test vol stop",
        trailing_stop_rule="test trail stop",
        time_stop_rule="test time stop",
        thesis_invalidation_rule="test invalidation",
        take_profit_rule="test take profit",
        review_events=["ANNUAL_REPORT"],
        max_holding_period_days=180,
        cost_model_version="v-cost",
        fill_model_version="v-fill",
        evidence_snapshot_id="evidence-pack:fixture",
        evidence_ids=["evidence:fixture"],
        effective_from=now + timedelta(hours=1),
        requires_user_confirmation=True,
        broker_execution_allowed=broker_execution_allowed,
        ledger_write_allowed=ledger_write_allowed,
        created_at=now,
    )


class _FakeCommitteeService:
    def __init__(self, protocol: TradeProtocol) -> None:
        self.repository = type(
            "_Repo",
            (),
            {
                "get_protocol": lambda _self, protocol_id: (
                    protocol if protocol_id == protocol.protocol_id else None
                ),
            },
        )()


def _fake_cli_paths(tmp_path: Path, monkeypatch) -> None:
    project_root = Path(__file__).resolve().parents[2]
    runtime = tmp_path / "paper-committee-cli-runtime"
    monkeypatch.setenv("ASTOCK_PROJECT_ROOT", str(project_root))
    monkeypatch.setenv("ASTOCK_RUNTIME_ROOT", str(runtime))


def _write_request_files(
    tmp_path: Path,
    *,
    symbol: str,
    operation_id: str,
    price: int = 10000,
    confirmation_id: str = "c" * 64,
) -> tuple[Path, Path, PaperOperationRequest, PaperUserConfirmation]:
    operation = PaperOperationRequest(
        operation_id=operation_id,
        account_id="paper",
        idempotency_key="committee-place-order",
        requested_at=datetime.now(UTC),
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        payload=PaperPlaceOrderPayload(
            market=Market.XSHG,
            symbol=symbol,
            side=OrderSide.BUY,
            qty=100,
            limit_price_fen=price,
            validity=PaperOrderValidity.DAY,
            calendar_release_id="1" * 64,
            instrument_release_id="2" * 64,
            daily_release_id="3" * 64,
            fee_rule_version="cn-a-share-paper-2026-07-13",
        ),
    )
    operation = operation.model_copy(update={"operation_id": paper_request_hash(operation)})
    confirmation = PaperUserConfirmation(
        operation_id=operation.operation_id,
        request_hash=operation.operation_id,
        account_id=operation.account_id,
        operation_type=operation.payload.operation_type,
        confirmation_id=confirmation_id,
        confirmed_at=operation.requested_at + timedelta(minutes=1),
        expires_at=operation.requested_at + timedelta(hours=1),
        nonce="non-committee-execute",
        key_id="test-ed25519",
        signature_algorithm="ED25519",
        signature_base64="A" * 16,
    )
    request_path = tmp_path / "paper-operation-request.json"
    confirmation_path = tmp_path / "paper-confirmation.json"
    request_path.write_text(operation.model_dump_json(), encoding="utf-8")
    confirmation_path.write_text(confirmation.model_dump_json(), encoding="utf-8")
    return request_path, confirmation_path, operation, confirmation


class _FakePaperExecutionService:
    def __init__(self) -> None:
        self.write_count = 0
        self.seen: dict[str, PaperOperationReport] = {}

    def execute(
        self,
        request: PaperOperationRequest,
        confirmation: Any,
        *,
        expected_operation_type: str,
    ) -> PaperOperationReport:
        assert expected_operation_type == "PLACE_ORDER"
        if request.operation_id in self.seen:
            return self.seen[request.operation_id]
        self.write_count += 1
        report = PaperOperationReport(
            operation_id=request.operation_id,
            operation_type=request.payload.operation_type,
            account_id=request.account_id,
            status=PaperOperationStatus.COMPLETE,
            request_hash=request.operation_id,
            confirmation_id=confirmation.confirmation_id,
            result={"status": "committed"},
            completed_at=request.requested_at,
        )
        self.seen[request.operation_id] = report
        return report


def test_paper_committee_execute_command_is_registered() -> None:
    assert RUNNER.invoke(app, ["paper-committee-execute", "--help"]).exit_code == 0


def test_paper_committee_execute_refuses_without_confirmation(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _fake_cli_paths(tmp_path, monkeypatch)
    service = _FakePaperExecutionService()
    protocol = _protocol(
        status=CommitteeProtocolStatus.ACTIVE,
        broker_execution_allowed=True,
        ledger_write_allowed=True,
    )
    monkeypatch.setattr(
        "astock.cli._committee_service",
        lambda *args, **kwargs: _FakeCommitteeService(protocol),
    )
    monkeypatch.setattr("astock.cli._paper_operation_service", lambda *args, **kwargs: service)
    request_path, _operation_path, operation, _confirmation = _write_request_files(
        tmp_path, symbol="300750", operation_id="0" * 64
    )
    execution = PaperExecutionRequest(
        trade_protocol_id="TradeProtocol:trade-protocol:fixture",
        user_confirmation_id=content_hash({"commit": "no-confirm"}),
        account_id="paper",
        created_at=datetime.now(UTC),
        paper_operation_request_id=operation.operation_id,
        symbol="300750",
        qty=100,
        limit_price_fen=10000,
    )
    execution_path = tmp_path / "paper-execution-request.json"
    execution_path.write_text(execution.model_dump_json(), encoding="utf-8")

    result = RUNNER.invoke(
        app,
        ["paper-committee-execute", str(execution_path), str(request_path)],
    )
    assert result.exit_code == 2
    payload = json.loads(result.output)
    assert payload["status"] == "REJECTED"
    assert service.write_count == 0


def test_paper_committee_execute_rejects_non_exec_verdicts(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _fake_cli_paths(tmp_path, monkeypatch)
    service = _FakePaperExecutionService()

    rejected_protocols = {
        "committee_reject": _protocol(
            verdict=CommitteeVerdict.PAPER_HOLD,
            status=CommitteeProtocolStatus.ACTIVE,
            broker_execution_allowed=False,
            ledger_write_allowed=False,
        ),
        "needs_info": _protocol(
            verdict=CommitteeVerdict.NEEDS_INFO,
            status=CommitteeProtocolStatus.BLOCKED,
            broker_execution_allowed=False,
            ledger_write_allowed=False,
        ),
    }
    for case, protocol in rejected_protocols.items():
        user_confirmation_id = content_hash({"commit": case})
        request_path, confirmation_path, operation, _confirmation = _write_request_files(
            tmp_path,
            symbol="300750",
            operation_id=content_hash({"case": case}),
            confirmation_id=user_confirmation_id,
        )
        execution = PaperExecutionRequest(
            trade_protocol_id="TradeProtocol:trade-protocol:fixture",
            user_confirmation_id=user_confirmation_id,
            account_id="paper",
            created_at=datetime.now(UTC),
            paper_operation_request_id=operation.operation_id,
            symbol="300750",
            qty=100,
            limit_price_fen=10000,
        )
        execution_path = tmp_path / f"committee-exec-{case}.json"
        execution_path.write_text(execution.model_dump_json(), encoding="utf-8")

        monkeypatch.setattr(
            "astock.cli._committee_service",
            lambda *args, _protocol=protocol, **kwargs: _FakeCommitteeService(_protocol),
        )
        monkeypatch.setattr(
            "astock.cli._paper_operation_service",
            lambda *args, **kwargs: service,
        )
        result = RUNNER.invoke(
            app,
            [
                "paper-committee-execute",
                str(execution_path),
                str(request_path),
                "--confirmation",
                str(confirmation_path),
            ],
        )
        assert result.exit_code == 2
        assert json.loads(result.output)["status"] == "REJECTED"


def test_paper_committee_execute_rejects_when_ledger_gate_closed(
    tmp_path: Path,
    monkeypatch,
) -> None:
    _fake_cli_paths(tmp_path, monkeypatch)
    service = _FakePaperExecutionService()
    protocol = _protocol(
        status=CommitteeProtocolStatus.ACTIVE,
        broker_execution_allowed=True,
        ledger_write_allowed=False,
    )
    confirmation_id = content_hash({"commit": "ledger-close"})
    request_path, confirmation_path, operation, _ = _write_request_files(
        tmp_path,
        symbol="300750",
        operation_id="7" * 64,
        confirmation_id=confirmation_id,
    )
    execution = PaperExecutionRequest(
        trade_protocol_id="TradeProtocol:trade-protocol:fixture",
        user_confirmation_id=confirmation_id,
        account_id="paper",
        created_at=datetime.now(UTC),
        paper_operation_request_id=operation.operation_id,
        symbol="300750",
        qty=100,
        limit_price_fen=10000,
    )
    execution_path = tmp_path / "ledger-close-exec.json"
    execution_path.write_text(execution.model_dump_json(), encoding="utf-8")

    monkeypatch.setattr(
        "astock.cli._committee_service",
        lambda *args, **kwargs: _FakeCommitteeService(protocol),
    )
    monkeypatch.setattr(
        "astock.cli._paper_operation_service",
        lambda *args, **kwargs: service,
    )

    result = RUNNER.invoke(
        app,
        [
            "paper-committee-execute",
            str(execution_path),
            str(request_path),
            "--confirmation",
            str(confirmation_path),
        ],
    )
    assert result.exit_code == 2
    assert json.loads(result.output)["status"] == "REJECTED"
    assert service.write_count == 0


def test_paper_committee_execute_success_and_idempotent(tmp_path: Path, monkeypatch) -> None:
    _fake_cli_paths(tmp_path, monkeypatch)
    service = _FakePaperExecutionService()
    protocol = _protocol(
        status=CommitteeProtocolStatus.ACTIVE,
        broker_execution_allowed=True,
        ledger_write_allowed=True,
    )
    confirmation_id = content_hash({"commit": "success"})
    request_path, confirmation_path, operation, _ = _write_request_files(
        tmp_path, symbol="300750", operation_id="9" * 64, confirmation_id=confirmation_id
    )
    execution = PaperExecutionRequest(
        trade_protocol_id="TradeProtocol:trade-protocol:fixture",
        user_confirmation_id=confirmation_id,
        account_id="paper",
        created_at=datetime.now(UTC),
        paper_operation_request_id=operation.operation_id,
        symbol="300750",
        qty=100,
        limit_price_fen=10000,
    )
    execution_path = tmp_path / "ok-exec.json"
    execution_path.write_text(execution.model_dump_json(), encoding="utf-8")

    monkeypatch.setattr(
        "astock.cli._committee_service",
        lambda *args, **kwargs: _FakeCommitteeService(protocol),
    )
    monkeypatch.setattr(
        "astock.cli._paper_operation_service",
        lambda *args, **kwargs: service,
    )

    first = RUNNER.invoke(
        app,
        [
            "paper-committee-execute",
            str(execution_path),
            str(request_path),
            "--confirmation",
            str(confirmation_path),
        ],
    )
    second = RUNNER.invoke(
        app,
        [
            "paper-committee-execute",
            str(execution_path),
            str(request_path),
            "--confirmation",
            str(confirmation_path),
        ],
    )

    assert first.exit_code == 0, first.output
    assert second.exit_code == 0, second.output
    first_payload = json.loads(first.output)
    second_payload = json.loads(second.output)
    assert first_payload["status"] == "COMPLETE"
    assert second_payload["status"] == "COMPLETE"
    assert first_payload["report"] == second_payload["report"]
    assert service.write_count == 1
