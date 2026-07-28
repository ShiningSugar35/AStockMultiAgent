from __future__ import annotations

import base64
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import yaml
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from typer.testing import CliRunner

from astock.cli import app
from astock.paper_trading import (
    PaperExecutionService,
    paper_confirmation_hash,
    paper_confirmation_signing_bytes,
)
from astock.schemas import PaperUserConfirmation

PROJECT_ROOT = Path(__file__).resolve().parents[2]
RUNNER = CliRunner()
_SIGNING_KEY = Ed25519PrivateKey.from_private_bytes(b"\x06" * 32)


def _invoke(*args: str):
    result = RUNNER.invoke(app, list(args))
    assert result.exit_code == 0, result.output
    return json.loads(result.output)


def _write_rules(path: Path) -> None:
    rules = yaml.safe_load(
        (PROJECT_ROOT / "configs" / "paper_trading_rules.yaml").read_text(encoding="utf-8")
    )
    public_key = _SIGNING_KEY.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    rules["authorization_keys"] = [
        {
            "key_id": "phase6-recorded-test",
            "public_key_pem": public_key.decode("ascii"),
        }
    ]
    path.write_text(
        yaml.safe_dump(rules, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )


def _write_confirmation(path: Path, operation: dict[str, Any]) -> PaperUserConfirmation:
    requested_at = datetime.fromisoformat(str(operation["requested_at"]))
    assert operation["payload"]["operation_type"] == "PLACE_ORDER"
    confirmation = PaperUserConfirmation(
        confirmation_id="0" * 64,
        operation_id=str(operation["operation_id"]),
        request_hash=str(operation["operation_id"]),
        account_id=str(operation["account_id"]),
        operation_type="PLACE_ORDER",
        confirmed_at=requested_at + timedelta(minutes=1),
        expires_at=requested_at + timedelta(minutes=20),
        nonce=f"phase6-recorded-{operation['operation_id']}",
        key_id="phase6-recorded-test",
        signature_algorithm="ED25519",
        signature_base64="A" * 16,
    )
    signature = _SIGNING_KEY.sign(paper_confirmation_signing_bytes(confirmation))
    confirmation = confirmation.model_copy(
        update={"signature_base64": base64.b64encode(signature).decode("ascii")}
    )
    confirmation = confirmation.model_copy(
        update={"confirmation_id": paper_confirmation_hash(confirmation)}
    )
    path.write_text(confirmation.model_dump_json(indent=2), encoding="utf-8")
    return confirmation


def test_recorded_300750_research_committee_protocol_confirmation_and_paper_order(
    tmp_path: Path,
    monkeypatch,
) -> None:
    runtime = tmp_path / "phase6-runtime"
    monkeypatch.setenv("ASTOCK_PROJECT_ROOT", str(PROJECT_ROOT))
    monkeypatch.setenv("ASTOCK_RUNTIME_ROOT", str(runtime))

    analysis = _invoke("analyze", "300750")
    assert analysis["status"] == "AWAITING_USER_CONFIRMATION"
    assert analysis["ResearchMemo"]["company_id"] == "300750"
    assert analysis["CommitteeDecision"]["company_id"] == "300750"
    assert analysis["TradeProtocol"]["outcome"] == "APPROVE_SIMULATION"
    protocol = analysis["TradeProtocol"]["artifact"]
    assert protocol["requires_user_confirmation"]
    assert not protocol["broker_execution_allowed"]
    assert protocol["paper_simulation_allowed"]

    repeated_analysis = _invoke("analyze", "300750")
    assert repeated_analysis["closure_report"]["run_id"] == analysis["closure_report"]["run_id"]
    assert repeated_analysis["reused_existing"]
    assert _invoke("phase6-audit", analysis["closure_report"]["run_id"])["status"] == "PASS"

    _invoke(
        "init",
        "--account-id",
        "phase6-recorded",
        "--initial-cash-yuan",
        "1000000",
    )
    before = _invoke("paper-status", "--account-id", "phase6-recorded")
    assert before["open_orders"] == []

    prepare_args = (
        "paper-execution-prepare",
        protocol["protocol_id"],
        analysis["closure_report"]["paper_reference_pack_artifact_id"],
        "--requested-at",
        "2026-07-27T10:00:00+08:00",
        "--idempotency-key",
        "phase6-recorded-300750-order-v1",
        "--account-id",
        "phase6-recorded",
        "--side",
        "BUY",
        "--qty",
        "100",
        "--limit-price-fen",
        "20000",
    )
    prepared = _invoke(*prepare_args)
    assert prepared["status"] == "WAITING_USER_CONFIRMATION"
    assert not prepared["ledger_write_performed"]
    assert _invoke("paper-status", "--account-id", "phase6-recorded")["open_orders"] == []

    repeated_preparation = _invoke(*prepare_args)
    assert repeated_preparation["execution_request_id"] == prepared["execution_request_id"]
    assert repeated_preparation["reused_existing"]
    collision_args = list(prepare_args)
    collision_args[collision_args.index("100")] = "200"
    collision = RUNNER.invoke(app, collision_args)
    assert collision.exit_code == 2
    assert json.loads(collision.output)["error_code"] == "PAPER_EXECUTION_PREPARATION_REJECTED"

    rules_path = tmp_path / "paper-rules-with-test-key.yaml"
    confirmation_path = tmp_path / "manual-confirmation.json"
    _write_rules(rules_path)
    confirmation = _write_confirmation(
        confirmation_path,
        prepared["operation_request"],
    )

    confirm_args = (
        "paper-execution-confirm",
        prepared["execution_request_id"],
        "--confirmation",
        str(confirmation_path),
        "--paper-rules",
        str(rules_path),
    )
    original_mark_status = PaperExecutionService.mark_status
    with monkeypatch.context() as crash:

        def _simulate_checkpoint_crash(*args, **kwargs) -> None:
            raise ValueError("simulated post-ledger checkpoint crash")

        crash.setattr(PaperExecutionService, "mark_status", _simulate_checkpoint_crash)
        interrupted = RUNNER.invoke(app, list(confirm_args))
        assert interrupted.exit_code == 3

    recovery = _invoke("paper-execution-recover", prepared["execution_request_id"])
    assert recovery["status"] == "RECOVERED_OR_ALREADY_COMPLETE"
    assert recovery["reconciled_terminal_status"]
    assert PaperExecutionService.mark_status is original_mark_status

    completed = _invoke(*confirm_args)
    repeated_completion = _invoke(*confirm_args)
    assert completed["status"] == "COMPLETE"
    assert completed["report"] == repeated_completion["report"]
    assert completed["report"]["confirmation_id"] == confirmation.confirmation_id

    after = _invoke("paper-status", "--account-id", "phase6-recorded")
    assert len(after["open_orders"]) == 1
    assert after["open_orders"][0]["symbol"] == "300750"
    assert after["open_orders"][0]["side"] == "BUY"
    execution_audit = _invoke("paper-execution-audit", prepared["execution_request_id"])
    assert execution_audit["status"] == "PASS"
    assert execution_audit["confirmation_id"] == confirmation.confirmation_id
    assert execution_audit["authorization_key_id"] == "phase6-recorded-test"
    assert len(execution_audit["authorization_key_object_sha256"]) == 64
    assert execution_audit["paper_order_id"] == after["open_orders"][0]["order_id"]
    recovery = _invoke("paper-execution-recover", prepared["execution_request_id"])
    assert recovery["status"] == "RECOVERED_OR_ALREADY_COMPLETE"
    assert not recovery["reconciled_terminal_status"]

    phase6_status = _invoke("phase6-status", "300750")
    assert phase6_status["closure_status"] == "PAPER_ORDER_CREATED"
    assert phase6_status["paper_order_id"] == after["open_orders"][0]["order_id"]
