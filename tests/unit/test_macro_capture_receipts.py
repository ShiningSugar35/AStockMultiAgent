"""A repeated official fetch is a new check, not a rewrite of first visibility."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.investor_orchestration.macro import MacroReleaseSpec, OfficialMacroCaptureService
from astock.investor_orchestration.store import InvestorOrchestrationStore


@pytest.fixture
def service(tmp_path: Path) -> OfficialMacroCaptureService:
    state = InvestorOrchestrationStore(tmp_path / "state.sqlite")
    state.initialize()
    return OfficialMacroCaptureService(state)


def spec() -> MacroReleaseSpec:
    return MacroReleaseSpec(
        authority="NBS",
        release_family="RECORDING_CAPTURE_TEST",
        url="https://www.stats.gov.cn/recorded-capture",
        parser="document-only",
    )


def test_identical_raw_fetches_retain_both_checks_without_rewriting_first_visibility(
    service,
) -> None:
    at = datetime.now(UTC) - timedelta(hours=2)
    first = service.capture_checked(
        spec(), recorded_content=b"<html>same official body</html>", captured_at=at
    )
    later = service.capture_checked(
        spec(),
        recorded_content=b"<html>same official body</html>",
        captured_at=at + timedelta(hours=1),
    )
    assert first.release == later.release
    assert first.release.captured_at == at
    assert first.capture_artifact_id != later.capture_artifact_id
    first_check = service.verified_capture(first.capture_artifact_id)
    later_check = service.verified_capture(later.capture_artifact_id)
    assert first_check.checked_at == at
    assert later_check.checked_at == at + timedelta(hours=1)
    assert first_check.source_hash == later_check.source_hash == first.release.source_hash
    state = StateStore(service.store.path)
    old = state.get_snapshot(first_check.source_snapshot_id)
    new = state.get_snapshot(later_check.source_snapshot_id)
    assert old is not None and new is not None
    assert old.snapshot_id != new.snapshot_id
    assert old.object_sha256 == new.object_sha256
    with service.store.connect() as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM macro_release_snapshots_v2").fetchone()[0] == 1
        )


def test_checked_capture_keeps_legacy_release_return_contract(service) -> None:
    at = datetime.now(UTC) - timedelta(hours=1)
    body = b"<html>recorded unchanged policy</html>"
    ordinary = service.capture(spec(), recorded_content=body, captured_at=at)
    checked = service.capture_checked(spec(), recorded_content=body, captured_at=at)
    assert ordinary == checked.release
    assert service.verified_capture(checked.capture_artifact_id).release_id == ordinary.release_id


def test_forged_receipt_family_does_not_match_its_source_capture(service) -> None:
    from astock.investor_orchestration.utils import content_hash

    at = datetime.now(UTC) - timedelta(hours=1)
    checked = service.capture_checked(
        spec(), recorded_content=b"<html>policy</html>", captured_at=at
    )
    receipt = service.verified_capture(checked.capture_artifact_id)
    wrong = receipt.model_copy(update={"release_family": "A_DIFFERENT_POLICY_SCOPE"})
    objects = ObjectStore(service.object_store.root)
    reference = objects.put_json(wrong.model_dump(mode="json"))
    identifier = f"MacroCaptureReceipt:{content_hash(wrong)}"
    state = StateStore(service.store.path)
    state.register_artifact(
        artifact_id=identifier,
        artifact_type="MacroCaptureReceipt",
        schema_version=wrong.schema_version,
        object_hash=reference.sha256,
        input_hashes=[wrong.source_hash, wrong.capture_policy_hash],
    )
    with pytest.raises(ValueError, match="capture|scope|family|lineage|identity"):
        service.verified_capture(identifier)
