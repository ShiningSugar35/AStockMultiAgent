"""Regression for official paragraph scope and append-only parser/mode editions."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from astock.investor_orchestration.macro import ImmutableRawObjectStore, OfficialMacroCaptureService
from astock.investor_orchestration.store import InvestorOrchestrationStore
from tests.unit.test_macro_vintage_semantics import AT, FAMILY, _capture, _spec

TEXT = (
    "<p>2026年8月中国采购经理指数运行情况</p>"
    "<h2>一、制造业采购经理指数运行情况</h2>"
    "<p>2026年8月，制造业采购经理指数（PMI）为49.8%。</p>"
    "<p>生产指数为50.4%。新订单指数为50.6%。</p>"
    "<h2>二、非制造业商务活动指数运行情况</h2>"
    "<p>新订单指数为44.1%。建筑业新订单指数为42.4%。服务业新订单指数为44.5%。</p>"
)


@pytest.fixture
def service(tmp_path: Path) -> OfficialMacroCaptureService:
    store = InvestorOrchestrationStore(tmp_path / "state.sqlite")
    store.initialize()
    return OfficialMacroCaptureService(
        store, object_store=ImmutableRawObjectStore(tmp_path / "objects")
    )


def _nbs(service: OfficialMacroCaptureService, text: str):
    return service.capture(
        service.spec("NBS", "manufacturing-pmi-current"),
        recorded_content=text.encode(),
        recorded_headers={
            "content-type": "text/html; charset=utf-8",
            "last-modified": "Mon, 31 Aug 2026 01:30:00 GMT",
        },
        captured_at=AT,
    )


@pytest.mark.parametrize(
    "heading",
    (
        "二、非制造业商务活动指数运行情况",
        "二、中国非制造业采购经理指数运行情况",
    ),
)
def test_nbs_manufacturing_does_not_mix_other_sector_new_orders(service, heading: str) -> None:
    release = _nbs(service, (TEXT.replace("二、非制造业商务活动指数运行情况", heading)) * 2)
    assert release.parse_status == "PASS" and not release.warnings
    assert {item.series_key: str(item.value) for item in release.observations} == {
        "manufacturing_pmi": "49.8",
        "manufacturing_production_index": "50.4",
        "manufacturing_new_orders_index": "50.6",
    }
    assert {item.observation_period for item in release.observations} == {"2026-08"}


def test_missing_manufacturing_metric_is_not_borrowed_from_other_sector(service) -> None:
    release = _nbs(service, TEXT.replace("新订单指数为50.6%。", ""))
    assert release.parse_status != "PASS"
    assert "manufacturing_new_orders_index" not in {
        item.series_key for item in release.observations
    }


def test_in_scope_conflicting_values_still_fail_closed(service) -> None:
    release = _nbs(
        service, TEXT.replace("新订单指数为50.6%。", "新订单指数为50.6%。新订单指数为48.0%。")
    )
    assert release.parse_status != "PASS"
    assert any("CONFLICTING_OBSERVATION" in value for value in release.warnings)
    assert "manufacturing_new_orders_index" not in {
        item.series_key for item in release.observations
    }


def test_scope_period_is_read_from_source_not_static_configuration(service) -> None:
    release = _nbs(service, TEXT.replace("2026年8月", "2026年7月"))
    assert release.parse_status == "PASS"
    assert {item.observation_period for item in release.observations} == {"2026-07"}


def test_parser_edition_appends_without_overwriting_same_raw_source(service) -> None:
    first = _capture(service, "period=2026-08; value=50.1")
    old_json = first.model_dump_json()
    second = _capture(
        service, "period=2026-08; value=50.1", offset=1, spec=_spec(unit="index_points")
    )
    assert second.release_id != first.release_id
    assert second.capture_policy_hash != first.capture_policy_hash
    assert second.source_hash == first.source_hash
    assert second.observations[0].available_to_system_at == AT + timedelta(minutes=1)
    assert len(service.store.macro_releases(authority="NBS", release_family=FAMILY)) == 2
    historical = service.store.macro_releases(
        authority="NBS", release_family=FAMILY, available_at=AT
    )
    assert len(historical) == 1 and historical[0].model_dump_json() == old_json
    repeated = _capture(
        service, "period=2026-08; value=50.1", offset=2, spec=_spec(unit="index_points")
    )
    assert repeated == second
    with pytest.raises(ValueError, match="ambiguous"):
        service.store.macro_release_by_source(
            authority="NBS", release_family=FAMILY, source_hash=first.source_hash
        )


def test_identical_recorded_and_mock_live_are_separate_capture_editions(service) -> None:
    raw = b"period=2026-08; value=50.1"
    first = _capture(service, raw.decode())
    client = httpx.Client(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                content=raw,
                request=request,
                headers={"last-modified": "Tue, 01 Sep 2026 01:00:00 GMT"},
            )
        )
    )
    try:
        service.http_client = client
        second = service.capture(_spec(), live=True)
    finally:
        client.close()
    assert first.capture_mode == "RECORDED" and second.capture_mode == "LIVE"
    assert second.release_id != first.release_id and second.source_hash == first.source_hash
    assert second.captured_at > first.captured_at
    assert second.captured_at <= datetime.now(UTC)
    assert len(service.store.macro_releases(authority="NBS", release_family=FAMILY)) == 2
