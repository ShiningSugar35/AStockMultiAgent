from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from astock.investor_orchestration.macro import OfficialMacroCaptureService
from astock.investor_orchestration.store import InvestorOrchestrationStore

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_nbs_inline_html_and_pubdate_are_parsed_without_backdating_availability(
    tmp_path: Path,
) -> None:
    store = InvestorOrchestrationStore(tmp_path / "state.sqlite")
    store.initialize()
    service = OfficialMacroCaptureService(
        store,
        config_path=PROJECT_ROOT / "configs/macro_current_releases_v1.yaml",
    )
    spec = service.spec("NBS", "manufacturing-pmi-current")
    captured_at = datetime(2026, 9, 8, 3, 30, tzinfo=UTC)
    content = """
    <html>
      <head>
        <title>2026年8月中国采购经理指数运行情况 - 国家统计局</title>
        <meta name="PubDate" content="2026/08/31 09:30">
      </head>
      <body>
        <p>8月份，制造业采购经理指数（<span>PMI</span>）为<span>49.8%</span>。</p>
        <p>生产指数为<span>50.4%</span>。</p>
        <p>新订单指数为<span>50.6%</span>。</p>
      </body>
    </html>
    """.encode()

    snapshot = service.capture(
        spec,
        recorded_content=content,
        recorded_headers={"content-type": "text/html; charset=utf-8"},
        captured_at=captured_at,
    )

    assert snapshot.parse_status == "PASS"
    observations = {item.series_key: item for item in snapshot.observations}
    assert observations["manufacturing_pmi"].value == Decimal("49.8")
    assert observations["manufacturing_production_index"].value == Decimal("50.4")
    assert observations["manufacturing_new_orders_index"].value == Decimal("50.6")
    assert observations["manufacturing_pmi"].observation_period == "2026-08"
    assert snapshot.published_at == datetime(2026, 8, 31, 1, 30, tzinfo=UTC)
    assert all(item.available_to_system_at == captured_at for item in snapshot.observations)
    assert all(item.first_release_verified is False for item in snapshot.observations)
    assert "PUBLISHED_AT_FALLBACK_TO_CAPTURE_TIME" not in snapshot.warnings


def test_mof_current_config_uses_the_official_https_fiscal_data_index(tmp_path: Path) -> None:
    store = InvestorOrchestrationStore(tmp_path / "state.sqlite")
    store.initialize()
    service = OfficialMacroCaptureService(
        store,
        config_path=PROJECT_ROOT / "configs/macro_current_releases_v1.yaml",
    )

    spec = service.spec("MOF", "fiscal-data-releases")

    assert spec.url == "https://www.mof.gov.cn/gkml/caizhengshuju/"
    service._assert_official_url(spec)
