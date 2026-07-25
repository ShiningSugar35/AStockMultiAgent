from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from typer.testing import CliRunner

from astock.cli import app
from astock.core.state import StateStore
from astock.paper_trading import paper_request_hash
from astock.schemas import PaperOperationRequest, PaperRecoverPayload

PROJECT_ROOT = Path(__file__).resolve().parents[2]
RUNNER = CliRunner()


def test_paper_operation_commands_are_registered() -> None:
    for command in (
        "paper-order-place",
        "paper-order-cancel",
        "paper-settle",
        "paper-mark",
        "paper-recover",
    ):
        result = RUNNER.invoke(app, [command, "--help"])
        assert result.exit_code == 0, result.output


def test_paper_recover_cli_fails_closed_without_confirmation(
    tmp_path: Path, monkeypatch
) -> None:
    runtime = tmp_path / "paper-operation-runtime"
    monkeypatch.setenv("ASTOCK_PROJECT_ROOT", str(PROJECT_ROOT))
    monkeypatch.setenv("ASTOCK_RUNTIME_ROOT", str(runtime))
    initialized = RUNNER.invoke(app, ["init", "--initial-cash-yuan", "10000"])
    assert initialized.exit_code == 0, initialized.output

    now = datetime.now(UTC)
    request = PaperOperationRequest(
        operation_id="0" * 64,
        account_id="default",
        idempotency_key="cli-recover-no-confirmation",
        requested_at=now,
        expires_at=now + timedelta(hours=1),
        payload=PaperRecoverPayload(as_of=now),
    )
    request = request.model_copy(update={"operation_id": paper_request_hash(request)})
    request_path = tmp_path / "recover-request.json"
    request_path.write_text(request.model_dump_json(), encoding="utf-8")

    rejected = RUNNER.invoke(app, ["paper-recover", str(request_path)])
    assert rejected.exit_code == 3
    payload = json.loads(rejected.output)
    assert payload["status"] == "REJECTED"
    assert not payload["ledger_write_allowed"]
    state = StateStore(runtime / "state.sqlite", PROJECT_ROOT / "migrations")
    with state.connect() as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM paper_operation_request"
        ).fetchone()[0] == 0
