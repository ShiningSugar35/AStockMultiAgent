"""Optional live investor social handler wiring and bounded behavior."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import cast

from astock.investor_orchestration.models import (
    InvestorRequestEnvelope,
    InvestorSessionPreflightReceipt,
    RequestIntent,
    SideEffectClass,
)
from astock.investor_orchestration.store import InvestorOrchestrationStore
from astock.social_sources import investor_handler


def _request(*identities: str) -> InvestorRequestEnvelope:
    return InvestorRequestEnvelope(
        request_id="social-handler-test",
        question_time=datetime(2026, 11, 11, 1, 0, tzinfo=UTC),
        user_timezone="Asia/Shanghai",
        raw_text="股票分析",
        normalized_intent=RequestIntent.RESEARCH,
        side_effect=SideEffectClass.READ,
        entity_ids=identities,
        idempotency_key="social-handler-test",
        metadata={
            "social_company_names": ["立讯精密"],
            "social_industry_keywords": ["连接器"],
            "social_management_keywords": ["董事长"],
        },
    )


def test_unpaired_browser_does_not_register_live_capability(
    tmp_path: Path,
) -> None:
    store = InvestorOrchestrationStore(tmp_path / "state.sqlite")
    store.initialize()
    assert investor_handler.paired_social_handler(store, tmp_path) is None


def test_paired_handler_searches_canonical_named_company_once(
    tmp_path: Path, monkeypatch,
) -> None:
    store = InvestorOrchestrationStore(tmp_path / "state.sqlite")
    store.initialize()
    monkeypatch.setattr(
        investor_handler,
        "load_social_search_config",
        lambda *args, **kwargs: SimpleNamespace(enabled=True),
    )
    requests = []
    ingested = []

    class FakeProvider:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def search_checked(self, **kwargs):
            requests.append(kwargs)
            return SimpleNamespace(
                result_artifact_id="SocialSearchResult:recorded",
                complete=True,
            )

    class FakeWatch:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def ingest(self, result):
            ingested.append(result.result_artifact_id)

    import astock.social_sources.monitor

    monkeypatch.setattr(investor_handler, "SocialBrowserSearchProvider", FakeProvider)
    monkeypatch.setattr(
        astock.social_sources.monitor, "SocialWatchIngestionService", FakeWatch
    )
    handler = investor_handler.paired_social_handler(store, tmp_path)
    assert handler is not None
    result = handler(_request("XSHE:002475"), cast(InvestorSessionPreflightReceipt, None))
    assert result.artifact_ids == ("SocialSearchResult:recorded",)
    assert not result.degraded_reason
    assert ingested == ["SocialSearchResult:recorded"]
    assert len(requests) == 1
    assert requests[0]["symbol"] == "002475"
    assert requests[0]["company_names"] == ("立讯精密",)
    assert requests[0]["industry_keywords"] == ("连接器",)
    assert requests[0]["management_keywords"] == ("董事长",)


def test_multiple_subjects_remain_bounded(
    tmp_path: Path, monkeypatch,
) -> None:
    store = InvestorOrchestrationStore(tmp_path / "state.sqlite")
    store.initialize()
    monkeypatch.setattr(
        investor_handler,
        "load_social_search_config",
        lambda *args, **kwargs: SimpleNamespace(enabled=True),
    )
    seen = []

    class FakeProvider:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def search_checked(self, **kwargs):
            seen.append(kwargs["symbol"])
            return SimpleNamespace(
                result_artifact_id=f"SocialSearchResult:{kwargs['symbol']}",
                complete=True,
            )

    class FakeWatch:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def ingest(self, result):
            return {}

    import astock.social_sources.monitor

    monkeypatch.setattr(investor_handler, "SocialBrowserSearchProvider", FakeProvider)
    monkeypatch.setattr(
        astock.social_sources.monitor, "SocialWatchIngestionService", FakeWatch
    )
    handler = investor_handler.paired_social_handler(store, tmp_path)
    assert handler is not None
    result = handler(
        _request("XSHE:002475", "XSHG:600521", "XSHE:002001", "XSHG:600519"),
        cast(InvestorSessionPreflightReceipt, None),
    )
    assert seen == ["002475", "600521"]
    # The two collected subjects may be used as leads, but the other two do
    # not transform an optional missing source into an investment failure.
    assert not result.degraded_reason
    assert len(result.artifact_ids) == 2
