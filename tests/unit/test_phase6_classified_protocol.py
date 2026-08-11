from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from astock.schemas import PaperExecutionRequest

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _execution_payload() -> dict[str, object]:
    return {
        "trade_protocol_id": "TradeProtocol:trade-protocol:test",
        "trade_protocol_object_sha256": "a" * 64,
        "account_id": "paper",
        "idempotency_key": "classified-contract-test",
        "paper_operation_request_id": "b" * 64,
        "symbol": "300750",
        "qty": 100,
        "limit_price_fen": 10000,
    }


def test_paper_execution_v3_keeps_legacy_protocol_compatible() -> None:
    request = PaperExecutionRequest.model_validate(_execution_payload())

    assert request.schema_version == "paper-execution-request-v3"
    assert request.committee_protocol_artifact_id is None
    assert request.trading_classification_artifact_id is None
    assert request.requires_user_confirmation


def test_classified_paper_execution_requires_exact_committee_and_classification_lineage() -> None:
    payload = {
        **_execution_payload(),
        "trade_protocol_id": ("ClassifiedTradeProtocol:classified-trade-protocol:" + "c" * 64),
        "trade_protocol_object_sha256": "d" * 64,
    }
    with pytest.raises(ValidationError, match="committee protocol lineage"):
        PaperExecutionRequest.model_validate(payload)

    request = PaperExecutionRequest.model_validate(
        {
            **payload,
            "committee_protocol_artifact_id": "TradeProtocol:trade-protocol:test",
            "committee_protocol_object_sha256": "e" * 64,
            "trading_classification_artifact_id": (
                "TradingClassificationRelease:trading-classification:" + "f" * 64
            ),
            "trading_classification_object_sha256": "1" * 64,
        }
    )
    assert request.committee_protocol_artifact_id == "TradeProtocol:trade-protocol:test"
    assert request.trading_classification_object_sha256 == "1" * 64


def test_paper_and_shadow_consumers_verify_classified_protocol_lineage() -> None:
    paper_source = (PROJECT_ROOT / "src" / "astock" / "paper_trading" / "execution.py").read_text(
        encoding="utf-8"
    )
    shadow_source = (PROJECT_ROOT / "src" / "astock" / "shadow" / "service.py").read_text(
        encoding="utf-8"
    )

    for required in (
        "ClassifiedTradeProtocol",
        "TradingClassificationRelease",
        "classified protocol committee lineage drift",
        "classified protocol classification lineage drift",
        "CLASSIFIED_PROTOCOL_LINEAGE_MISMATCH",
    ):
        assert required in paper_source
    for required in (
        "ClassifiedTradeProtocol",
        "TradingClassificationRelease",
        "shadow classified protocol committee lineage mismatch",
        "shadow classified protocol classification lineage mismatch",
        "shadow full committee arm must freeze the exact classified protocol",
        "shadow full committee arm must freeze the exact classification",
    ):
        assert required in shadow_source
