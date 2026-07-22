from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from astock.cli import app


@pytest.mark.live
def test_candidate_scan_live_release_is_explicit_and_count_agnostic() -> None:
    request_path = os.environ.get("ASTOCK_CANDIDATE_LIVE_REQUEST")
    if not request_path:
        pytest.skip("ASTOCK_CANDIDATE_LIVE_REQUEST is not configured")
    path = Path(request_path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["live"] is True
    result = CliRunner().invoke(app, ["candidate-scan", str(path)])
    assert result.exit_code in {0, 3}, result.output
    assert json.loads(result.output)["status"] in {"SUCCEEDED", "NEEDS_INFO"}
