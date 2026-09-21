from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

import astock.candidates.discovery_cli as discovery_cli
from astock.candidates.discovery_runtime import (
    DiscoveryEvidenceNeed,
    DiscoveryRefreshOutcome,
)
from astock.schemas.discovery_runtime import (
    DiscoveryConditionClaimAdmissionRequest,
    DiscoveryRefreshRequest,
)
from astock.schemas.market import Market
from astock.settings import ProjectPaths

NOW = datetime(2026, 9, 21, 1, 30, tzinfo=UTC)


def _paths(tmp_path: Path) -> ProjectPaths:
    runtime = tmp_path / "runtime"
    return ProjectPaths(
        root=Path(__file__).resolve().parents[2],
        runtime=runtime,
        objects=runtime / "objects" / "sha256",
        parquet=runtime / "data" / "parquet",
        manifests=runtime / "manifests",
        state_db=runtime / "state.sqlite",
    )


def test_refresh_request_requires_sorted_unique_contexts() -> None:
    payload = {
        "as_of": NOW.isoformat(),
        "contexts": [
            {
                "company_id": "600002",
                "market": "XSHG",
                "name": "B",
                "industry_id": "industry:b",
                "business_profile": "GENERAL_INDUSTRIAL",
            },
            {
                "company_id": "600001",
                "market": "XSHG",
                "name": "A",
                "industry_id": "industry:a",
                "business_profile": "GENERAL_INDUSTRIAL",
            },
        ],
    }
    with pytest.raises(ValueError, match="sorted and unique"):
        DiscoveryRefreshRequest.model_validate(payload)


def test_condition_admission_requires_sorted_unique_evidence_ids() -> None:
    payload = {
        "as_of": NOW.isoformat(),
        "company_id": "600001",
        "industry_id": "industry:a",
        "channel": "EVENT",
        "role": "event_occurred",
        "state": "SATISFIED",
        "subject_id": "600001",
        "evidence_ids": ["evidence:b", "evidence:a"],
        "confidence": 0.9,
    }
    with pytest.raises(ValueError, match="sorted unique"):
        DiscoveryConditionClaimAdmissionRequest.model_validate(payload)


def test_refresh_cli_surfaces_recovery_plan_without_fabricating_ready(
    tmp_path: Path,
    state,
    object_store,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _paths(tmp_path)
    request_path = tmp_path / "refresh.json"
    request_path.write_text(
        json.dumps(
            {
                "as_of": NOW.isoformat(),
                "contexts": [
                    {
                        "company_id": "600001",
                        "market": Market.XSHG.value,
                        "name": "Example",
                        "industry_id": "industry:a",
                        "business_profile": "GENERAL_INDUSTRIAL",
                    }
                ],
                "max_verified_discovery": 8,
                "execute_existing_recovery": False,
            }
        ),
        encoding="utf-8",
    )
    need = DiscoveryEvidenceNeed(
        company_id="600001",
        channel="EVENT",
        role="event_occurred",
        thesis_family_id="reviewed-method:event",
        research_question="Did the event occur?",
        existing_capabilities=("CORPORATE_ACTIONS",),
        preferred_authorities=("ISSUER_IR", "EXCHANGE_OFFICIAL"),
    )
    outcome = DiscoveryRefreshOutcome(
        release=None,
        results=(),
        evidence_needs=(need,),
        evaluation_artifact_ids=(),
        finding_codes=(),
    )

    class FakeService:
        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def refresh(self, **_kwargs):
            return outcome

    monkeypatch.setattr(discovery_cli, "_services", lambda: (paths, state, object_store))
    monkeypatch.setattr(discovery_cli, "DiscoveryRuntimeService", FakeService)

    result = CliRunner().invoke(discovery_cli.app, ["refresh", str(request_path)])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["status"] == "RECOVERY_REQUIRED"
    assert payload["release"] is None
    assert payload["recovery"] is None
    assert payload["evidence_needs"][0]["recovery_order"] == [
        "LOCAL_REGISTERED_EVIDENCE",
        "CURRENT_RESEARCH_ACQUISITION",
        "PROVIDER_FALLBACK",
        "WEB_SEARCH_OFFICIAL",
    ]
    assert payload["next_action"]


def test_status_cli_does_not_upgrade_recovery_checkpoint_to_ready(
    tmp_path: Path,
    state,
    object_store,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _paths(tmp_path)
    state.set_checkpoint(
        scope_type="verified-discovery-seeds",
        scope_key="latest",
        cursor={"release_id": "release:test"},
        status="READY",
        object_hash="a" * 64,
    )
    state.set_checkpoint(
        scope_type="verified-discovery-refresh",
        scope_key="latest",
        cursor={"release_id": "release:test", "evidence_need_count": 2},
        status="READY",
        object_hash="b" * 64,
    )
    monkeypatch.setattr(discovery_cli, "_services", lambda: (paths, state, object_store))

    result = CliRunner().invoke(discovery_cli.app, ["status"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["status"] == "RECOVERY_REQUIRED"
    assert payload["release"]["status"] == "READY"
    assert payload["refresh"]["status"] == "READY"
    assert payload["refresh"]["cursor"]["evidence_need_count"] == 2
