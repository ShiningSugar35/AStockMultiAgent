"""The registered feature consumer must honor visibility and consolidated scope."""
import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

import httpx
import pymupdf
import pytest
from pydantic import HttpUrl
from typer.testing import CliRunner

from astock.candidates.discovery_cli import app
from astock.candidates.registered_features import (
    OfficialGuidanceReference,
    PeerValuationReference,
    SupplementalFeatureRequest,
    build_registered_features,
)
from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.documents import (
    DocumentPageRepository,
    DocumentRepository,
    OfficialWebDocumentCaptureService,
)
from astock.documents.pdf_parser import PdfParseService
from astock.financial_sources.service import FinancialSourceService
from astock.providers.tushare_reference import TushareReferenceProvider
from astock.providers.tushare_structured import DEFAULT_FIELDS
from astock.schemas import AgentSourceProposal, DocumentType, Market, SourceClass
from astock.schemas.financial import (
    FinancialFact,
    FinancialFieldCode,
    FinancialIndustryProfile,
)
from astock.schemas.financial_sources import FinancialStatementScope
from astock.settings import ProjectPaths
from tests.unit.test_discovery_features import corpus, fact
from tests.unit.test_sampled_forecasts import capture, report

AS_OF = datetime(2026, 8, 31, tzinfo=UTC)
ROOT = Path(__file__).resolve().parents[2]
CAPTURED = datetime(2026, 11, 21, 7, tzinfo=UTC)


def service_with_releases(
    *, parent_scope=False, future=False, wrong_snapshot=False,
    information_at: datetime = AS_OF, company_id: str = "600001",
    extra_facts: list[FinancialFact] | None = None,
):
    service = MagicMock(spec=FinancialSourceService)
    rows = corpus(company_id) + (extra_facts or [])

    def read(company, stamp, period, *, as_of):
        assert company == company_id and as_of == information_at
        facts = tuple(item for item in rows if item.period_end == stamp)
        if not facts:
            return None
        available = information_at + timedelta(days=1) if future else information_at
        manifest = SimpleNamespace(
            company_id=company, available_to_system_at=available,
            official_snapshot_id=(
                "wrong-snapshot" if wrong_snapshot else facts[0].source_snapshot_id
            ),
            release_id=f"registered:{stamp}",
            model_dump=lambda **_: {
                "period": stamp.isoformat(), "available": available.isoformat()
            },
        )
        observations = tuple(SimpleNamespace(
            field_code=item.field_code, statement_type=item.statement_type,
            period_end=item.period_end, period_type=item.period_type,
            duration_semantics=item.duration_semantics,
            statement_scope=(
                FinancialStatementScope.PARENT_COMPANY if parent_scope
                else FinancialStatementScope.CONSOLIDATED
            ),
        ) for item in facts)
        return SimpleNamespace(manifest=manifest, facts=facts, observations=observations)

    service.read_certified_release.side_effect = read
    return service


def build(service, *, as_of: datetime = AS_OF):
    return build_registered_features(
        service, company_id="600001", period_end=date(2026, 6, 30), as_of=as_of,
        industry_profile=FinancialIndustryProfile.GENERAL_INDUSTRIAL,
        business_group_id="industrial-pumps",
    )


def test_registered_reader_is_bounded_and_feature_sources_remain_explainable():
    service = service_with_releases()
    result = build(service)
    features = {row["metric_id"]: row for row in result["features"]}
    assert features["REVENUE_TTM"]["value"] == "1200"
    assert features["REVENUE_TTM"]["source_fact_ids"]
    assert result["releases"] and result["missing_periods"]
    assert service.read_certified_release.call_count <= 8
    assert not result["historical_point_in_time_claim"]
    assert not result["blind_tranche_mutation_allowed"]
    assert not result["research_budget_mutation_allowed"]
    assert result["independent_vote_weight"] == 0


def test_parent_scope_is_not_silently_mixed_into_consolidated_features():
    result = build(service_with_releases(parent_scope=True))
    assert result["unbound_scope_fact_ids"]
    assert all(row["value"] is None for row in result["features"])
    assert all(row["missing_policy"] == "UNKNOWN_NOT_ZERO" for row in result["features"])


@pytest.mark.parametrize("changes", [{"future": True}, {"wrong_snapshot": True}])
def test_future_or_misbound_certified_source_cannot_become_a_feature(changes):
    with pytest.raises(ValueError, match="visibility|provenance"):
        build(service_with_releases(**changes))


def test_naive_boundary_is_rejected_before_any_reader_call():
    service = service_with_releases()
    with pytest.raises(ValueError, match="aware"):
        build(service, as_of=AS_OF.replace(tzinfo=None))
    service.read_certified_release.assert_not_called()


def valuation_capture(state, objects, monkeypatch, company="600519", **values):
    monkeypatch.setenv("TUSHARE_TOKEN", "feature-test-token")
    row: dict[str, object] = {field: None for field in DEFAULT_FIELDS["daily_basic"]}
    row.update(
        ts_code=f"{company}.SH", trade_date="20261120", close="10",
        pe_ttm="20", pb="2", dv_ttm="3.5",
    )
    row.update(values)
    payload = {"code": 0, "data": {"fields": list(row), "items": [list(row.values())]}}
    with httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload)),
    ) as client:
        provider = TushareReferenceProvider(
            objects, state, project_root=ROOT, client=client, clock=lambda: CAPTURED,
        )
        return provider.valuation_metrics(
            company, Market.XSHG, datetime.strptime(str(row["trade_date"]), "%Y%m%d").date(),
        )


def official_guidance(
    state, objects, *, wrong_company=False, document_type=DocumentType.ANNOUNCEMENT,
):
    code = "600001" if wrong_company else "600519"
    text = f"证券代码：{code}\n2026年度业绩预告\n业绩预告类型：预增\n"
    with pymupdf.open() as pdf:
        page = pdf.new_page()
        page.insert_text((40, 60), text, fontname="china-s", fontsize=12)
        content = pdf.tobytes()
    proposal = AgentSourceProposal(
        requested_capability="disclosure.document", query="official earnings guidance",
        candidate_url=HttpUrl("https://www.sse.com.cn/disclosure/listedinfo/guidance.pdf"),
        expected_fact="one issuer earnings guidance category",
        preferred_source_class=SourceClass.PRIMARY_OFFICIAL_WEB,
        formal_use=True, require_complete=False, reason="registered guidance feature",
    )
    frozen = OfficialWebDocumentCaptureService(state, objects).capture(
        proposal, content, title="2026年度业绩预告", company_ids=["600519"],
        published_at=CAPTURED - timedelta(days=1), document_type=document_type,
        observed_at=CAPTURED,
    )
    documents = DocumentRepository(state)
    document = documents.get_model(frozen.document_id)
    snapshot = documents.snapshot(frozen.snapshot_id)
    assert document is not None and snapshot is not None
    parsed = PdfParseService(objects, state, DocumentPageRepository(state)).parse(
        document, snapshot, page_numbers=[1], ocr_enabled=False,
    )
    return OfficialGuidanceReference(
        capture_artifact_id=f"OfficialWebDocumentCapture:{frozen.capture_id}",
        page_id=parsed.page_ids[0], period_start=date(2026, 1, 1), period_end=date(2026, 12, 31),
    )


def supplemental_service(state, objects, as_of: datetime, *, cash=True):
    dividends = (
        [fact(FinancialFieldCode.CASH_DIVIDENDS_PAID, value, year, month, company="600519")
         for year, month, value in ((2025, 12, 40), (2025, 6, 15), (2026, 6, 25))]
        if cash else []
    )
    service = service_with_releases(
        information_at=as_of, company_id="600519", extra_facts=dividends,
    )
    service.state = StateStore(state.path, ROOT / "migrations", read_only=True)
    service.objects = objects
    return service


def supplemental_build(service, request: SupplementalFeatureRequest, as_of: datetime):
    return build_registered_features(
        service, company_id="600519", period_end=date(2026, 6, 30), as_of=as_of,
        industry_profile=FinancialIndustryProfile.GENERAL_INDUSTRIAL,
        business_group_id="industrial-pumps", supplemental=request,
    )


def test_features_cli_consumes_registered_sources_without_any_writes(
    state, object_store, tmp_path, monkeypatch,
):
    target = valuation_capture(state, object_store, monkeypatch)
    peers = tuple(
        PeerValuationReference(
            company_id=f"6001{index:02}", market=Market.XSHG,
            capture_artifact_id=valuation_capture(
                state, object_store, monkeypatch, company=f"6001{index:02}", pe_ttm=str(index),
            ).artifact_id,
            industry_profile=FinancialIndustryProfile.GENERAL_INDUSTRIAL,
            business_group_id="industrial-pumps",
        )
        for index in range(1, 10)
    )
    prior, _ = capture(object_store, state, [report("OLD", "A", 2)])
    current, _ = capture(object_store, state, [
        {**report("NEW", "A", 3, "2026-11-03"), "predictNextYearEps": "4.5"},
        report("NEWB", "B", 1000, "2026-11-03"),
    ])
    guidance = official_guidance(state, object_store)
    as_of = datetime.now(UTC) + timedelta(seconds=1)
    request = SupplementalFeatureRequest(
        market=Market.XSHG, valuation_capture_id=target.artifact_id, peer_valuations=peers,
        forecast_capture_ids=(current.capture_id,), prior_forecast_capture_ids=(prior.capture_id,),
        forecast_fiscal_year=2026, prior_forecast_as_of=prior.available_to_system_at,
        official_guidance=guidance,
    )
    request_file = tmp_path / "supplemental.json"
    request_file.write_text(request.model_dump_json(), encoding="utf-8")
    service = supplemental_service(state, object_store, as_of)
    paths = ProjectPaths(
        root=ROOT, runtime=state.path.parent, objects=object_store.root,
        parquet=tmp_path / "parquet", manifests=tmp_path / "manifests", state_db=state.path,
    )
    monkeypatch.setattr(ProjectPaths, "discover", lambda: paths)

    def certified_reader(actual_service, company, stamp, period, *, as_of):
        assert actual_service.state.read_only
        return service.read_certified_release(company, stamp, period, as_of=as_of)

    monkeypatch.setattr(FinancialSourceService, "read_certified_release", certified_reader)
    for method in ("migrate", "transaction", "register_artifact"):
        monkeypatch.setattr(
            StateStore, method, Mock(side_effect=AssertionError("read-only request")),
        )
    for method in ("put_bytes", "put_json"):
        monkeypatch.setattr(
            ObjectStore, method, Mock(side_effect=AssertionError("no object writes")),
        )
    monkeypatch.setattr(httpx.Client, "stream", Mock(side_effect=AssertionError("no network")))
    result = CliRunner().invoke(app, [
        "features", "600519", "2026-06-30", as_of.isoformat(), "GENERAL_INDUSTRIAL",
        "industrial-pumps", "--supplemental-file", str(request_file),
    ])
    assert result.exit_code == 0, str(result.exception)
    payload = json.loads(result.stdout)
    features = {item["metric_id"]: item for item in payload["features"]}
    assert features["DIVIDEND_CASH_COVERAGE"]["value"] == "3.4"
    assert features["VENDOR_PE_TTM"]["value"] == "20"
    assert features["VENDOR_DIVIDEND_YIELD_TTM"]["unit"] == "PERCENT"
    assert features["VENDOR_DIVIDEND_YIELD_TTM"]["value"] == "3.5"
    assert features["VENDOR_PE_TTM_PEER_PERCENTILE"]["value"] == "0.95"
    assert features["SAMPLED_EPS_NEXT_YEAR_GROWTH"]["value"] == "0.5"
    revision = features["SAMPLED_EPS_OBSERVED_REVISION"]
    assert revision["value"] == "0.5" and revision["inputs"]["sample_count"] == 1
    assert revision["inputs"]["unpaired_current_institutions"] == ["B"]
    assert features["OFFICIAL_EARNINGS_GUIDANCE_DIRECTION"]["value"] == "预增"
    assert payload["read_only"] and not payload["historical_point_in_time_claim"]
    assert not payload["blind_tranche_mutation_allowed"]
    assert not payload["research_budget_mutation_allowed"]
    assert all(item["independent_vote_weight"] == 0 for item in features.values())


def test_forecast_next_year_conflict_cannot_be_resolved_by_report_hash(
    state, object_store,
):
    current, _ = capture(object_store, state, [
        {**report("FIRST", "A", 1), "predictNextYearEps": "1.2"},
        {**report("SECOND", "A", 1), "predictNextYearEps": "1.8"},
    ])
    as_of = datetime.now(UTC) + timedelta(seconds=1)
    request = SupplementalFeatureRequest(
        market=Market.XSHG, forecast_capture_ids=(current.capture_id,), forecast_fiscal_year=2026,
    )
    result = supplemental_build(supplemental_service(state, object_store, as_of), request, as_of)
    growth = next(item for item in result["features"]
                  if item["metric_id"] == "SAMPLED_EPS_NEXT_YEAR_GROWTH")
    assert growth["value"] is None
    assert growth["inputs"]["growth_conflicting_institutions"] == ["A"]
    assert growth["inputs"]["sample_count"] == 0


def test_recent_forecast_cannot_be_backfilled_as_a_historical_revision(state, object_store):
    current, _ = capture(object_store, state, [report("FIRST", "A", 1)])
    as_of = datetime.now(UTC) + timedelta(seconds=1)
    request = SupplementalFeatureRequest(
        market=Market.XSHG, forecast_capture_ids=(current.capture_id,),
        prior_forecast_capture_ids=(current.capture_id,), forecast_fiscal_year=2026,
        prior_forecast_as_of=current.available_to_system_at - timedelta(seconds=1),
    )
    with pytest.raises(ValueError, match="cutoff"):
        supplemental_build(supplemental_service(state, object_store, as_of), request, as_of)


@pytest.mark.parametrize("mode", ["wrong-company", "future"])
def test_valuation_consumer_rejects_identity_or_future_capture(
    state, object_store, monkeypatch, mode,
):
    captured = valuation_capture(
        state, object_store, monkeypatch, company="600001" if mode == "wrong-company" else "600519",
    )
    as_of = CAPTURED - timedelta(seconds=1) if mode == "future" else CAPTURED
    request = SupplementalFeatureRequest(
        market=Market.XSHG, valuation_capture_id=captured.artifact_id,
    )
    with pytest.raises(ValueError, match="subject|cutoff"):
        supplemental_build(supplemental_service(state, object_store, as_of), request, as_of)


def test_negative_multiple_and_missing_field_are_not_zero_scores(state, object_store, monkeypatch):
    captured = valuation_capture(state, object_store, monkeypatch, pe_ttm="-5", pb=None, dv_ttm="0")
    request = SupplementalFeatureRequest(
        market=Market.XSHG, valuation_capture_id=captured.artifact_id,
    )
    result = supplemental_build(
        supplemental_service(state, object_store, CAPTURED), request, CAPTURED,
    )
    features = {item["metric_id"]: item for item in result["features"]}
    assert features["VENDOR_PE_TTM"]["status"] == "NOT_APPLICABLE"
    assert features["VENDOR_PE_TTM"]["value"] is None
    assert features["VENDOR_PB"]["value"] is None
    assert features["VENDOR_DIVIDEND_YIELD_TTM"]["value"] == "0"


def test_guidance_body_identity_cannot_be_supplied_by_capture_metadata(state, object_store):
    reference = official_guidance(state, object_store, wrong_company=True)
    request = SupplementalFeatureRequest(market=Market.XSHG, official_guidance=reference)
    result = supplemental_build(
        supplemental_service(state, object_store, CAPTURED), request, CAPTURED,
    )
    value = next(item for item in result["features"]
                 if item["metric_id"] == "OFFICIAL_EARNINGS_GUIDANCE_DIRECTION")
    assert value["value"] is None and "GUIDANCE_ISSUER_NOT_EXPLICIT" in value["reason_codes"]


def test_financial_report_recalling_a_guidance_is_not_a_current_guidance(state, object_store):
    reference = official_guidance(state, object_store, document_type=DocumentType.ANNUAL_REPORT)
    request = SupplementalFeatureRequest(market=Market.XSHG, official_guidance=reference)
    with pytest.raises(ValueError, match="dedicated announcement"):
        supplemental_build(supplemental_service(state, object_store, CAPTURED), request, CAPTURED)


def test_feature_schema_and_missing_status_do_not_create_a_runtime(tmp_path, monkeypatch):
    paths = ProjectPaths(
        root=ROOT, runtime=tmp_path / "absent-runtime", objects=tmp_path / "absent-objects",
        parquet=tmp_path / "absent-parquet", manifests=tmp_path / "absent-manifests",
        state_db=tmp_path / "absent-runtime" / "state.sqlite",
    )
    monkeypatch.setattr(ProjectPaths, "discover", lambda: paths)
    monkeypatch.setattr(
        ProjectPaths, "ensure_directories", Mock(side_effect=AssertionError("read-only request")),
    )
    result = CliRunner().invoke(app, ["status"])
    assert result.exit_code == 0
    assert json.loads(result.stdout) == {"status": "NOT_READY", "refresh": None, "release": None}
    assert not paths.runtime.exists()
    schema = CliRunner().invoke(app, ["schema"])
    assert schema.exit_code == 0
    assert "features_supplemental" in json.loads(schema.stdout)


def test_registered_metric_unit_cannot_override_original_response(state, object_store, monkeypatch):
    captured = valuation_capture(state, object_store, monkeypatch)
    altered = captured.model_copy(update={"metrics": tuple(
        item.model_copy(update={"unit": "PERCENT"}) if item.metric == "pe_ttm" else item
        for item in captured.metrics
    )})
    frozen = object_store.put_json(altered.model_dump(mode="json"))
    state.register_artifact(
        artifact_id=altered.artifact_id, artifact_type="TushareMetricCapture",
        schema_version=altered.schema_version, object_hash=frozen.sha256,
        input_hashes=[altered.source_object_hash],
    )
    request = SupplementalFeatureRequest(
        market=Market.XSHG, valuation_capture_id=altered.artifact_id,
    )
    with pytest.raises(ValueError, match="normalization"):
        supplemental_build(supplemental_service(state, object_store, CAPTURED), request, CAPTURED)
