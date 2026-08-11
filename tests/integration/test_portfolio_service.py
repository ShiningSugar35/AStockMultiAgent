from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from astock.committee.config import load_committee_rules
from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.market_data.reference import MarketReferenceService
from astock.market_data.reference_storage import ReferenceParquetStore
from astock.portfolio.service import PortfolioService, _History
from astock.schemas.committee import TradeProtocolOutcome
from astock.schemas.market import Market
from astock.schemas.paper import PaperTradingClassification
from astock.schemas.portfolio import (
    PortfolioAnalysisRequest,
    PortfolioAnalysisStatus,
    PortfolioCandidateInput,
    PortfolioConstructionRequest,
    PortfolioHoldingInput,
)
from astock.schemas.research_runtime import (
    ClassifiedTradeProtocol,
    TradingClassificationRelease,
    TradingClassificationStatus,
    TradingPriceLimitRegime,
    TradingSpecialRegime,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 7, 27, 8, 0, tzinfo=UTC)


class _RecordedPortfolioService(PortfolioService):
    def __init__(self, *args, histories: dict[str, _History], **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._recorded_histories = histories

    def _history(self, company_id: str, market: Market, **kwargs) -> _History:
        del market, kwargs
        return self._recorded_histories[company_id]


def _register(
    state: StateStore,
    objects: ObjectStore,
    artifact_id: str,
    artifact_type: str,
    payload: object,
    *,
    input_hashes: list[str] | None = None,
) -> str:
    ref = objects.put_json(payload)
    state.register_artifact(
        artifact_id=artifact_id,
        artifact_type=artifact_type,
        schema_version="1.0",
        object_hash=ref.sha256,
        input_hashes=input_hashes or [],
    )
    return ref.sha256


def _history(
    state: StateStore,
    objects: ObjectStore,
    company_id: str,
    market: Market,
    *,
    drift: float,
) -> _History:
    release_id = f"portfolio-recorded:{company_id}"
    object_hash = _register(
        state,
        objects,
        f"market-reference:{release_id}",
        "MarketReferenceRelease",
        {"company_id": company_id, "recorded": True},
    )
    start = datetime(2026, 3, 1, tzinfo=UTC)
    closes: dict[str, float] = {}
    price = 100.0 + drift * 10
    for index in range(100):
        day = (start + timedelta(days=index)).date().isoformat()
        price *= 1.0 + drift + ((index % 7) - 3) * 0.0004
        closes[day] = price
    return _History(
        company_id=company_id,
        market=market,
        closes_by_date=closes,
        latest_close_fen=int(round(price * 100)),
        release_id=release_id,
        release_object_hash=object_hash,
        cutoff_at=NOW,
    )


def _service(tmp_path: Path) -> tuple[_RecordedPortfolioService, StateStore, ObjectStore]:
    state = StateStore(tmp_path / "state.sqlite", PROJECT_ROOT / "migrations")
    state.migrate()
    objects = ObjectStore(tmp_path / "objects")
    reference = MarketReferenceService(
        state,
        objects,
        ReferenceParquetStore(tmp_path / "parquet"),
        PROJECT_ROOT / "tests" / "fixtures" / "reference",
    )
    histories = {
        "600001": _history(state, objects, "600001", Market.XSHG, drift=0.0008),
        "600002": _history(state, objects, "600002", Market.XSHG, drift=0.0004),
        "000300": _history(state, objects, "000300", Market.INDEX, drift=0.0005),
    }
    return (
        _RecordedPortfolioService(
            state,
            objects,
            reference,
            load_committee_rules(PROJECT_ROOT / "configs" / "committee_rules.yaml"),
            histories=histories,
        ),
        state,
        objects,
    )


def _approved_protocol(
    state: StateStore,
    objects: ObjectStore,
    company_id: str,
) -> str:
    decision_id = f"DecisionPack:portfolio:{company_id}"
    committee_id = f"TradeProtocol:portfolio:{company_id}"
    decision_hash = _register(
        state,
        objects,
        decision_id,
        "DecisionPack",
        {"company_id": company_id, "kind": "decision"},
    )
    committee_hash = _register(
        state,
        objects,
        committee_id,
        "TradeProtocol",
        {"company_id": company_id, "kind": "committee-protocol"},
    )
    source_id = f"PortfolioClassificationSource:{company_id}"
    source_hash = _register(
        state,
        objects,
        source_id,
        "PortfolioClassificationSource",
        {"company_id": company_id, "kind": "recorded"},
    )
    classification = TradingClassificationRelease(
        release_id=f"trading-classification:portfolio:{company_id}",
        company_id=company_id,
        market=Market.XSHG,
        symbol=company_id,
        as_of=NOW,
        effective_from=NOW - timedelta(minutes=1),
        valid_until=NOW + timedelta(hours=1),
        classification=PaperTradingClassification(
            instrument_id=f"XSHG:{company_id}",
            board="MAIN",
            risk_status="NORMAL",
            fixed_price_limit_eligible=True,
            suspension_status_verified=True,
            suspended=False,
            evidence_id=f"portfolio-classification:{company_id}",
            created_at=NOW,
        ),
        special_no_price_limit=False,
        special_regime=TradingSpecialRegime.ORDINARY,
        price_limit_regime=TradingPriceLimitRegime.FIXED,
        price_limit_rate_bps=1000,
        source_artifact_ids=[source_id],
        source_object_hashes=[source_hash],
        status=TradingClassificationStatus.READY,
        reason_codes=[],
        created_at=NOW,
    )
    classification_id = (
        "TradingClassificationRelease:" + classification.release_id
    )
    classification_hash = _register(
        state,
        objects,
        classification_id,
        "TradingClassificationRelease",
        classification.model_dump(mode="json"),
        input_hashes=[source_hash],
    )
    protocol = ClassifiedTradeProtocol(
        protocol_id="classified-trade-protocol:" + company_id * 10 + "abcd",
        company_id=company_id,
        as_of=NOW,
        decision_pack_artifact_id=decision_id,
        decision_pack_object_hash=decision_hash,
        committee_protocol_artifact_id=committee_id,
        committee_protocol_object_hash=committee_hash,
        trading_classification_artifact_id=classification_id,
        trading_classification_object_hash=classification_hash,
        committee_outcome=TradeProtocolOutcome.APPROVE_SIMULATION,
        final_outcome=TradeProtocolOutcome.APPROVE_SIMULATION,
        board="MAIN",
        risk_status="NORMAL",
        special_regime=TradingSpecialRegime.ORDINARY,
        price_limit_regime=TradingPriceLimitRegime.FIXED,
        price_limit_rate_bps=1000,
        blocking_codes=[],
        frozen_input_hashes=sorted(
            [decision_hash, committee_hash, classification_hash]
        ),
        paper_simulation_allowed=True,
        created_at=NOW,
    )
    artifact_id = f"ClassifiedTradeProtocol:{protocol.protocol_id}"
    _register(
        state,
        objects,
        artifact_id,
        "ClassifiedTradeProtocol",
        protocol.model_dump(mode="json"),
        input_hashes=protocol.frozen_input_hashes,
    )
    return artifact_id


def test_portfolio_analysis_persists_risk_metrics_and_audits(tmp_path: Path) -> None:
    service, _, _ = _service(tmp_path)
    report = service.analyze(
        PortfolioAnalysisRequest(
            portfolio_id="portfolio:recorded-analysis",
            as_of=NOW,
            holdings=[
                PortfolioHoldingInput(
                    company_id="600001",
                    market=Market.XSHG,
                    weight=0.10,
                    industry_tag="A",
                ),
                PortfolioHoldingInput(
                    company_id="600002",
                    market=Market.XSHG,
                    weight=0.10,
                    industry_tag="B",
                ),
            ],
            lookback_sessions=80,
            minimum_common_sessions=60,
        )
    )

    assert report.status is PortfolioAnalysisStatus.READY
    assert report.metrics is not None
    assert report.metrics.historical_cvar_95 >= report.metrics.historical_var_95
    assert report.metrics.historical_cdar_95 <= report.metrics.max_drawdown + 1e-12
    assert report.metrics.annualized_downside_deviation >= 0
    artifact_id = f"PortfolioAnalysisReport:{report.report_id}"
    assert service.audit(artifact_id)["status"] == "PASS"
    assert not report.broker_execution_allowed


def test_portfolio_construction_requires_approved_lineage_and_emits_four_proposals(
    tmp_path: Path,
) -> None:
    service, state, objects = _service(tmp_path)
    first = _approved_protocol(state, objects, "600001")
    second = _approved_protocol(state, objects, "600002")
    request = PortfolioConstructionRequest(
        portfolio_id="portfolio:recorded-construction",
        as_of=NOW,
        candidates=[
            PortfolioCandidateInput(
                company_id="600001",
                classified_protocol_artifact_id=first,
                risk_group="A",
            ),
            PortfolioCandidateInput(
                company_id="600002",
                classified_protocol_artifact_id=second,
                risk_group="B",
            ),
        ],
        target_total_exposure=0.20,
        lookback_sessions=80,
        minimum_common_sessions=60,
    )

    report = service.construct(request)

    assert report.status is PortfolioAnalysisStatus.READY
    assert len(report.proposals) == 4
    assert report.admitted_company_ids == ["600001", "600002"]
    for proposal in report.proposals:
        assert sum(proposal.weights.values()) == pytest.approx(0.20, abs=1e-8)
        assert proposal.max_single_weight <= 0.10 + 1e-9
        assert proposal.cash_weight == pytest.approx(0.80, abs=1e-8)
    artifact_id = f"PortfolioConstructionReport:{report.report_id}"
    assert service.audit(artifact_id)["status"] == "PASS"

    protocol_record = state.artifact_record(first)
    assert protocol_record is not None
    parent_id = ClassifiedTradeProtocol.model_validate_json(
        objects.get_bytes(str(protocol_record["object_hash"]))
    ).decision_pack_artifact_id
    parent = state.artifact_record(parent_id)
    assert parent is not None
    objects.path_for(str(parent["object_hash"])).write_bytes(b"tampered")
    blocked = service.construct(
        request.model_copy(update={"portfolio_id": "portfolio:tampered-parent"})
    )
    assert blocked.status is PortfolioAnalysisStatus.NEEDS_INFO
    assert "600001" in blocked.rejected_company_ids
