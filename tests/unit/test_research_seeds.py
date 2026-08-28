from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from astock.candidates.seeds import ResearchSeedProviderRouter, ResearchSeedService, _RawMarketRow
from astock.core.hashing import content_hash
from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.schemas.evidence import SourceSnapshot
from astock.schemas.market import Market
from astock.schemas.research_seeds import (
    ResearchSeedOrigin,
    ResearchSeedRequest,
    ResearchSeedStatus,
    ResearchUniverseCoverageStatus,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 8, 11, 4, 0, tzinfo=UTC)


class _FakeVisualSkills:
    def __init__(self, release: dict[str, object], rows: list[dict[str, object]]) -> None:
        self.release_row = release
        self.rows = rows

    def latest_release_any(self) -> dict[str, object]:
        return self.release_row

    def overlay_skill_rows(self, release_id: str) -> list[dict[str, object]]:
        assert release_id == self.release_row["release_id"]
        return self.rows

    def release(self, release_id: str) -> dict[str, object] | None:
        return self.release_row if release_id == self.release_row["release_id"] else None


class _FakeSeedProvider:
    def __init__(self, state: StateStore, objects: ObjectStore) -> None:
        self.state = state
        self.objects = objects
        self.market_payloads = {
            Market.XSHG: self._market_payload(
                Market.XSHG,
                [
                    ("600001", "芯片甲", 20.0, 800_000_000.0, 3.0, 50_000_000_000.0, 9.0),
                    ("600002", "银行乙", 10.0, 500_000_000.0, 1.0, 100_000_000_000.0, -8.0),
                ],
            ),
            Market.XSHE: self._market_payload(
                Market.XSHE,
                [
                    ("000001", "芯片丙", 30.0, 650_000_000.0, 2.0, 40_000_000_000.0, -9.5),
                    ("300001", "消费丁", 15.0, 300_000_000.0, 5.0, 20_000_000_000.0, 11.0),
                ],
            ),
            Market.BJSE: self._market_payload(
                Market.BJSE,
                [("920001", "小盘戊", 12.0, 10_000_000.0, 3.0, 1_000_000_000.0, 15.0)],
            ),
        }
        self.boards = {
            "rc": 0,
            "data": {
                "diff": [
                    {"f12": "BK1036", "f14": "半导体"},
                    {"f12": "BK0475", "f14": "银行"},
                    {"f12": "BK0438", "f14": "食品饮料"},
                ]
            },
            "_astock_request": {"purpose": "EXPERT_DOMAIN_TAXONOMY"},
        }
        self.constituents = {
            "BK1036": self._constituent_payload("BK1036", ["600001", "000001"]),
            "BK0475": self._constituent_payload("BK0475", ["600002"]),
            "BK0438": self._constituent_payload("BK0438", ["300001"]),
        }

    @staticmethod
    def _market_payload(
        market: Market,
        rows: list[tuple[str, str, float, float, float, float, float]],
    ) -> dict[str, object]:
        return {
            "rc": 0,
            "data": {
                "total": len(rows),
                "diff": [
                    {
                        "f12": symbol,
                        "f14": name,
                        "f2": price,
                        "f6": amount,
                        "f8": turnover,
                        "f21": float_cap,
                        "f3": pct_change,
                    }
                    for symbol, name, price, amount, turnover, float_cap, pct_change in rows
                ]
            },
            "_astock_request": {"market": market.value, "purpose": "RESEARCH_SEED_ONLY"},
        }

    def _constituent_payload(self, board: str, symbols: list[str]) -> dict[str, object]:
        by_symbol: dict[str, dict[str, object]] = {}
        for payload in self.market_payloads.values():
            rows = cast(dict[str, object], payload["data"])["diff"]
            assert isinstance(rows, list)
            for row in rows:
                assert isinstance(row, dict)
                by_symbol[str(row["f12"])] = row
        return {
            "rc": 0,
            "data": {"diff": [by_symbol[item] for item in symbols]},
            "_astock_request": {
                "board_code": board,
                "purpose": "EXPERT_DOMAIN_CONSTITUENTS",
            },
        }

    def _snapshot(self, label: str, payload: dict[str, object]) -> SourceSnapshot:
        ref = self.objects.put_json(payload)
        snapshot = SourceSnapshot(
            snapshot_id=f"fake-seed:{label}:{ref.sha256}",
            source_id="fake-seed-provider",
            object_sha256=ref.sha256,
            fetched_at=NOW,
            available_to_system_at=NOW,
            source_url=f"https://example.invalid/{label}",
            mime="application/json",
            byte_size=ref.byte_size,
            rights_status="PUBLIC_RESEARCH_FIXTURE",
            created_at=NOW,
        )
        self.state.register_snapshot(snapshot)
        return snapshot

    def fetch_seed_snapshot(
        self, market: Market, *, live: bool = False
    ) -> tuple[dict[str, object], SourceSnapshot]:
        del live
        payload = self.market_payloads[market]
        return payload, self._snapshot(f"market-{market.value}", payload)

    def fetch_industry_boards(
        self, *, live: bool = False
    ) -> tuple[dict[str, object], SourceSnapshot]:
        del live
        return self.boards, self._snapshot("industry-boards", self.boards)

    def fetch_industry_constituents(
        self, board_code: str, *, live: bool = False
    ) -> tuple[dict[str, object], SourceSnapshot]:
        del live
        payload = self.constituents[board_code]
        return payload, self._snapshot(f"industry-{board_code}", payload)


def _skill(author: str, skill_id: str, text: str) -> dict[str, object]:
    payload = {
        "final_skill_id": skill_id,
        "author_source_id": author,
        "skill_name": text,
        "decision_question": f"如何判断{text}？",
        "core_principle": f"围绕{text}建立研究框架并回到公司事实验证。",
        "applicable_conditions": [text],
        "required_evidence": [f"{text}证据"],
        "positive_signals": [],
        "negative_signals": [],
    }
    return {"skill_json": json.dumps(payload, ensure_ascii=False)}


def _service(tmp_path: Path) -> tuple[ResearchSeedService, StateStore, ObjectStore]:
    state = StateStore(tmp_path / "state.sqlite", PROJECT_ROOT / "migrations")
    state.migrate()
    objects = ObjectStore(tmp_path / "objects")
    provider = _FakeSeedProvider(state, objects)
    service = ResearchSeedService(
        project_root=PROJECT_ROOT,
        state=state,
        objects=objects,
        provider=provider,
    )
    release_ref = objects.put_json({"release": "test-composite"})
    release: dict[str, object] = {
        "release_id": "knowledge-registry-v2:test",
        "release_object_hash": release_ref.sha256,
    }
    rows = [
        *[_skill("zhihu:expert-a", f"skill:semi:{index}", "半导体芯片") for index in range(5)],
        _skill("zhihu:expert-a", "skill:bank:a", "银行"),
        *[_skill("zhihu:expert-b", f"skill:bank:{index}", "银行") for index in range(4)],
        _skill("zhihu:expert-b", "skill:semi:b", "半导体"),
    ]
    cast(Any, service).visual_skills = _FakeVisualSkills(release, rows)
    service.author_names.update({"zhihu:expert-a": "专家A", "zhihu:expert-b": "专家B"})
    return service, state, objects


def test_market_seed_score_is_not_directional() -> None:
    rows = [
        _RawMarketRow(
            company_id="600001",
            market=Market.XSHG,
            name="上涨股",
            price=10,
            amount_cny=100_000_000,
            turnover_rate=2,
            float_market_cap_cny=10_000_000_000,
            snapshot_id="snap:a",
        ),
        _RawMarketRow(
            company_id="600002",
            market=Market.XSHG,
            name="下跌股",
            price=10,
            amount_cny=100_000_000,
            turnover_rate=2,
            float_market_cap_cny=10_000_000_000,
            snapshot_id="snap:b",
        ),
    ]

    scored = ResearchSeedService._score_market_rows(
        rows,
        minimum_amount=1,
        minimum_float_cap=1,
    )

    assert scored[0].market_score == scored[1].market_score


def test_expert_domains_are_derived_from_current_skill_text(tmp_path: Path) -> None:
    service, _, _ = _service(tmp_path)
    request = ResearchSeedRequest(
        as_of=NOW,
        minimum_domain_skill_count=3,
        created_at=NOW,
    )
    rows = cast(Any, service.visual_skills).rows

    profiles = service._expert_profiles(
        release_id="knowledge-registry-v2:test",
        rows=rows,
        boards=[("BK1036", "半导体"), ("BK0475", "银行")],
        request=request,
    )

    by_author = {item.author_source_id: item for item in profiles}
    assert [item.board_name for item in by_author["zhihu:expert-a"].domains] == ["半导体"]
    assert [item.board_name for item in by_author["zhihu:expert-b"].domains] == ["银行"]

    changed = [
        *[_skill("zhihu:expert-a", f"skill:bank-new:{index}", "银行") for index in range(5)],
        _skill("zhihu:expert-a", "skill:semi-old", "半导体"),
    ]
    changed_profiles = service._expert_profiles(
        release_id="knowledge-registry-v2:test2",
        rows=changed,
        boards=[("BK1036", "半导体"), ("BK0475", "银行")],
        request=request,
    )
    assert [item.board_name for item in changed_profiles[0].domains] == ["银行"]


def test_expert_domain_gate_uses_absolute_skill_count_not_author_share(tmp_path: Path) -> None:
    service, _, _ = _service(tmp_path)
    rows = [
        *[_skill("zhihu:dilution", f"skill:semi:{index}", "半导体芯片") for index in range(3)],
        *[
            _skill("zhihu:dilution", f"skill:unrelated:{index}", f"通用研究框架{index}")
            for index in range(300)
        ],
    ]
    request = ResearchSeedRequest(
        as_of=NOW,
        minimum_domain_skill_count=3,
        created_at=NOW,
    )

    profiles = service._expert_profiles(
        release_id="knowledge-registry-v2:dilution",
        rows=rows,
        boards=[("BK1036", "半导体")],
        request=request,
    )

    assert len(profiles) == 1
    assert [item.board_name for item in profiles[0].domains] == ["半导体"]
    assert profiles[0].domains[0].matched_skill_count == 3
    assert profiles[0].domains[0].skill_share < 0.015


def test_research_seed_report_merges_market_and_expert_sources_and_audits(
    tmp_path: Path,
) -> None:
    service, state, _ = _service(tmp_path)
    request = ResearchSeedRequest(
        as_of=NOW,
        max_total_seeds=10,
        max_market_seeds=3,
        max_expert_seeds_per_author=2,
        minimum_domain_skill_count=3,
        minimum_amount_cny=20_000_000,
        minimum_float_market_cap_cny=2_000_000_000,
        created_at=NOW,
    )

    report = service.generate(request)

    assert report.status.value == "READY"
    assert report.registry_release_id == "knowledge-registry-v2:test"
    assert report.seeds
    assert report.market_seed_count > 0
    assert report.expert_seed_count > 0
    assert report.universe_coverage_status is ResearchUniverseCoverageStatus.FULL
    assert report.formal_full_market_coverage_allowed
    assert report.market_coverage_ratios == {
        Market.XSHG: 1.0,
        Market.XSHE: 1.0,
        Market.BJSE: 1.0,
    }
    assert not report.recommendation_allowed
    assert not report.candidate_record_write_allowed
    assert not report.paper_ledger_write_allowed
    assert not report.broker_execution_allowed
    semi = next(item for item in report.seeds if item.company_id == "600001")
    assert ResearchSeedOrigin.MARKET in semi.origins
    assert ResearchSeedOrigin.EXPERT_SKILL in semi.origins
    assert "zhihu:expert-a" in semi.expert_author_source_ids
    assert "半导体" in semi.expert_domain_names
    assert semi.requires_candidate_evidence
    assert semi.requires_deep_research
    artifact_id = f"ResearchSeedReport:{report.report_id}"
    assert state.artifact_record(artifact_id) is not None
    assert service.audit(artifact_id)["status"] == "PASS"


def test_partial_market_coverage_is_observation_only(tmp_path: Path) -> None:
    service, _, _ = _service(tmp_path)
    payload = cast(Any, service.provider).market_payloads[Market.XSHG]
    cast(dict[str, object], payload["data"])["total"] = 3

    report = service.generate(ResearchSeedRequest(as_of=NOW, created_at=NOW))

    assert report.universe_coverage_status is ResearchUniverseCoverageStatus.PARTIAL
    assert not report.formal_full_market_coverage_allowed
    assert report.market_coverage_ratios[Market.XSHG] == 2 / 3
    assert any(
        item.startswith("MARKET_SEED_UNIVERSE_PARTIAL:XSHG:")
        for item in report.warning_codes
    )


def test_full_universe_with_zero_eligible_market_candidates_is_not_unavailable(
    tmp_path: Path,
) -> None:
    service, _, _ = _service(tmp_path)
    report = service.generate(
        ResearchSeedRequest(
            as_of=NOW,
            minimum_amount_cny=10_000_000_000_000,
            minimum_float_market_cap_cny=10_000_000_000_000,
            created_at=NOW,
        )
    )

    assert report.status is ResearchSeedStatus.EMPTY
    assert report.universe_coverage_status is ResearchUniverseCoverageStatus.FULL
    assert report.formal_full_market_coverage_allowed
    assert report.market_seed_count == 0
    assert "CURRENT_MARKET_SCAN_ZERO_ELIGIBLE_CANDIDATES" in report.warning_codes
    assert "CURRENT_MARKET_SEED_UNIVERSE_UNAVAILABLE" not in report.warning_codes


def test_unavailable_universe_is_distinct_from_zero_eligible_candidates(
    tmp_path: Path,
) -> None:
    service, _, _ = _service(tmp_path)

    def unavailable(*args, **kwargs):
        del args, kwargs
        raise ValueError("all market snapshot providers unavailable")

    cast(Any, service.provider).fetch_seed_snapshot = unavailable
    report = service.generate(ResearchSeedRequest(as_of=NOW, created_at=NOW))

    assert report.universe_coverage_status is ResearchUniverseCoverageStatus.UNAVAILABLE
    assert not report.formal_full_market_coverage_allowed
    assert "CURRENT_MARKET_SEED_UNIVERSE_UNAVAILABLE" in report.warning_codes
    assert "CURRENT_MARKET_SCAN_ZERO_ELIGIBLE_CANDIDATES" not in report.warning_codes


def test_live_seed_router_continues_past_partial_provider_to_full_fallback(
    tmp_path: Path,
) -> None:
    state = StateStore(tmp_path / "router.sqlite", PROJECT_ROOT / "migrations")
    state.migrate()
    objects = ObjectStore(tmp_path / "router-objects")
    primary = _FakeSeedProvider(state, objects)
    fallback = _FakeSeedProvider(state, objects)
    cast(dict[str, object], primary.market_payloads[Market.XSHG]["data"])["total"] = 3
    router = ResearchSeedProviderRouter(
        primary,
        fallback,
        minimum_rows_by_market={Market.XSHG: 1, Market.XSHE: 1, Market.BJSE: 1},
        state=state,
        objects=objects,
    )

    payload, _ = router.fetch_seed_snapshot(Market.XSHG, live=True)

    assert cast(dict[str, object], payload["data"])["total"] == 2


def test_seed_id_depends_on_skill_support_and_snapshots(tmp_path: Path) -> None:
    service, _, _ = _service(tmp_path)
    report = service.generate(
        ResearchSeedRequest(
            as_of=NOW,
            max_total_seeds=10,
            max_market_seeds=3,
            max_expert_seeds_per_author=2,
            minimum_domain_skill_count=3,
            created_at=NOW,
        )
    )
    seed = next(item for item in report.seeds if item.company_id == "600001")
    assert seed.seed_id.startswith("research-seed:")
    assert seed.seed_id != "research-seed:" + content_hash(seed.company_id)


def test_expert_overlay_priority_bonus_is_request_policy_driven(tmp_path: Path) -> None:
    service, _, _ = _service(tmp_path)

    def request(bonus: float) -> ResearchSeedRequest:
        return ResearchSeedRequest(
            as_of=NOW,
            max_total_seeds=10,
            max_market_seeds=3,
            max_expert_seeds_per_author=2,
            minimum_domain_skill_count=3,
            expert_overlay_max_priority_bonus=bonus,
            created_at=NOW,
        )

    without_overlay = service.generate(request(0.0))
    with_overlay = service.generate(request(0.20))

    low = next(item for item in without_overlay.seeds if item.company_id == "600001")
    high = next(item for item in with_overlay.seeds if item.company_id == "600001")
    assert high.research_priority_score > low.research_priority_score
