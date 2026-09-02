from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
import typer
from typer.testing import CliRunner

from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.external_accounts import ExternalAccountRepository
from astock.local_portfolio import LocalPortfolioService, register_local_portfolio_commands
from astock.paper_trading import LedgerService

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_local_portfolio_replays_trades_and_preserves_review_state(tmp_path: Path) -> None:
    service = LocalPortfolioService(tmp_path)
    assert service.initialize()["status"] == "READY"

    service.record_trade(
        market="XSHG",
        symbol="600519",
        side="BUY",
        quantity=100,
        price_cny="100.00",
        source="IMPORT",
        occurred_at=datetime(2026, 8, 16, 1, 0, tzinfo=UTC),
    )
    service.record_trade(
        market="XSHG",
        symbol="600519",
        side="BUY",
        quantity=100,
        price_cny="120.00",
        source="IMPORT",
        occurred_at=datetime(2026, 8, 16, 2, 0, tzinfo=UTC),
    )
    service.record_review(
        market="XSHG",
        symbol="600519",
        action="HOLD",
        thesis_status="UNCHANGED",
        note="基本面未出现需要改变判断的事实。",
        reviewed_at=datetime(2026, 8, 16, 3, 0, tzinfo=UTC),
    )

    status = service.status()
    assert status["status"] == "READY"
    assert status["position_count"] == 1
    positions = cast(list[dict[str, object]], status["positions"])
    position = positions[0]
    assert position["quantity"] == 200
    assert position["average_cost_cny"] == "110.0000"
    assert position["last_action"] == "HOLD"
    assert service.audit()["status"] == "PASS"

    rebuilt = service.rebuild()
    assert rebuilt["status"] == "REBUILT"
    rebuilt_positions = cast(list[dict[str, object]], rebuilt["positions"])
    assert rebuilt_positions[0]["last_action"] == "HOLD"


def test_local_portfolio_import_trade_keeps_mechanical_validity(
    tmp_path: Path,
) -> None:
    service = LocalPortfolioService(tmp_path)
    service.initialize()
    result = service.record_trade(
        market="XSHE",
        symbol="000001",
        side="BUY",
        quantity=100,
        price_cny="10.50",
        source="IMPORT",
    )
    trade = cast(dict[str, object], result["trade"])
    assert trade["source"] == "IMPORT"

    with pytest.raises(ValueError, match="sell exceeds current holding"):
        service.record_trade(
            market="XSHE",
            symbol="000001",
            side="SELL",
            quantity=200,
            price_cny="11.00",
            source="IMPORT",
        )

    with pytest.raises(ValueError, match="source must be one of"):
        service.record_trade(
            market="XSHE",
            symbol="000001",
            side="BUY",
            quantity=100,
            price_cny="10.60",
            source="USER",
        )


def test_local_portfolio_is_markdown_with_yaml_frontmatter(tmp_path: Path) -> None:
    service = LocalPortfolioService(tmp_path)
    service.initialize()
    for path in (service.portfolio_path, service.orders_path, service.trades_path):
        text = path.read_text(encoding="utf-8")
        assert text.startswith("---\n")
        assert "此文件仅保存在本机，不进入 Git" in text


def test_local_portfolio_can_sync_empty_paper_account(tmp_path: Path) -> None:
    state = StateStore(tmp_path / "state.sqlite", PROJECT_ROOT / "migrations")
    state.migrate()
    objects = ObjectStore(tmp_path / "objects")
    LedgerService(state, objects).initialize_account("paper", 1_000_000)
    service = LocalPortfolioService(tmp_path, state)

    result = service.sync_from_paper("paper")

    assert result["status"] == "SYNCED_FROM_PAPER"
    assert result["open_order_count"] == 0
    assert result["fill_count"] == 0
    assert result["position_count"] == 0
    assert service.status()["status"] == "READY"


def test_local_portfolio_auto_selects_default_paper_account(tmp_path: Path) -> None:
    state = StateStore(tmp_path / "state.sqlite", PROJECT_ROOT / "migrations")
    state.migrate()
    objects = ObjectStore(tmp_path / "objects")
    LedgerService(state, objects).initialize_account("default", 1_000_000)
    service = LocalPortfolioService(tmp_path, state)

    result = service.sync_from_paper()

    assert result["account_id"] == "default"
    assert result["position_count"] == 0


def test_legacy_import_cli_writes_canonical_external_event_before_markdown(tmp_path: Path) -> None:
    state = StateStore(tmp_path / "state.sqlite", PROJECT_ROOT / "migrations")
    state.migrate()
    objects = ObjectStore(tmp_path / "objects")
    app = typer.Typer()
    paths = SimpleNamespace(root=tmp_path)
    register_local_portfolio_commands(app, lambda: (paths, state, objects))
    runner = CliRunner()
    args = [
        "local-portfolio-import-trade",
        "BUY",
        "XSHG",
        "600519",
        "100",
        "10",
        "--occurred-at",
        "2026-08-20T02:00:00+00:00",
    ]

    first = runner.invoke(app, args)
    second = runner.invoke(app, args)

    assert first.exit_code == 0, first.output
    assert second.exit_code == 0, second.output
    repository = ExternalAccountRepository(state, objects)
    events = repository.list_events("default")
    assert len(events) == 1
    migrated = repository.migrate_legacy_default_account(
        LocalPortfolioService(tmp_path, state),
        migrated_at=datetime(2026, 8, 20, 4, 0, tzinfo=UTC),
    )
    assert migrated["inserted_event_ids"] == []
    assert len(repository.list_events("default")) == 1
    projection = repository.projection(
        "default",
        as_of=datetime(2026, 8, 20, 3, 0, tzinfo=UTC),
    )
    assert projection.positions[0].quantity == 100
    assert projection.positions[0].average_cost_cny == 10
    assert LocalPortfolioService(tmp_path, state).status()["trade_count"] == 1
