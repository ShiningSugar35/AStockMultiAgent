from __future__ import annotations

import json
from pathlib import Path

from scripts.live_smoke_bse_reference import (
    REQUEST_CONTRACT_PATH,
    BseReferenceSmokeRequest,
    _request_from_contract,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_bse_live_smoke_request_is_production_safe_and_schema_valid() -> None:
    assert REQUEST_CONTRACT_PATH == (
        REPO_ROOT / "configs" / "validation" / "bse_reference_smoke_request.json"
    )
    payload = json.loads(REQUEST_CONTRACT_PATH.read_text(encoding="utf-8"))
    serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True).lower()

    for forbidden in (
        "localhost",
        "127.0.0.1",
        "example.com",
        "fixture://",
        "file://",
    ):
        assert forbidden not in serialized

    request = _request_from_contract()
    assert isinstance(request, BseReferenceSmokeRequest)
    assert request.market.value == "BJSE"
    assert request.live is True
    assert request.read_only is True
    assert request.broker_execution_allowed is False
    assert request.purpose == "INSTRUMENT_MASTER"
