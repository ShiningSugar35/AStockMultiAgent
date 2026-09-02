"""R-01 official-denominator gate regressions.

These tests lock the invariant that official full-market / official-report /
official-company-action coverage is only marked AVAILABLE where an implemented,
registry-backed official adapter proves it.  A capability that is merely
declared on an authority domain but has no working exact-item adapter must stay
honestly UNAVAILABLE (AGENTS.md: 未实现的能力必须如实标记不可用).
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from typing import cast

import yaml

from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.financial_sources.config import FinancialSourceConfig, load_financial_source_config
from astock.market_data import MarketReferenceService, ReferenceParquetStore
from astock.market_data.reference_config import (
    MarketReferenceConfig,
    load_market_reference_config,
)
from astock.pit import PointInTimeRepository
from astock.providers.config import load_provider_registry
from astock.providers.exchange_official_reference import (
    SseOfficialReferenceProvider,
    SzseOfficialReferenceProvider,
)
from astock.providers.macro_authority import (
    MacroAuthorityReleaseError,
    MofFiscalPolicyReleaseProvider,
    NbsStatisticalReleaseProvider,
    NdrcPricingPolicyReleaseProvider,
    PbocMonetaryPolicyReleaseProvider,
)
from astock.providers.runtime import ProviderFactory, load_transport_profiles
from astock.schemas import (
    AvailabilityBasis,
    CompletenessSemantics,
    Market,
    PointInTimeStatus,
    ProviderRegistry,
    UniverseCoverageLevel,
    UniverseDenominatorAuthority,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _reference() -> tuple[MarketReferenceConfig, ProviderRegistry]:
    registry = load_provider_registry(PROJECT_ROOT / "configs" / "provider_registry.yaml")
    reference = load_market_reference_config(
        PROJECT_ROOT / "configs" / "market_reference.yaml",
        registry,
    )
    return reference, registry


def test_instrument_master_official_coverage_only_where_denominator_is_proven() -> None:
    reference, registry = _reference()

    for market, provider_id in (
        (Market.XSHG, "sse-official-reference"),
        (Market.XSHE, "szse-official-reference"),
        (Market.BJSE, "bse-official-reference"),
    ):
        assert reference.official_coverage("instrument.master", market) == "AVAILABLE"
        definition = next(item for item in registry.providers if item.provider_id == provider_id)
        assert definition.officiality.value == "PRIMARY_OFFICIAL"
        assert "instrument.master" in definition.formal_capabilities
        assert (
            definition.completeness_semantics["instrument.master"]
            is CompletenessSemantics.FULL_UNIVERSE
        )


def test_sse_szse_recorded_official_denominators_are_self_contained(
    state: StateStore,
    object_store: ObjectStore,
) -> None:
    registry = load_provider_registry(PROJECT_ROOT / "configs" / "provider_registry.yaml")
    profiles = load_transport_profiles(PROJECT_ROOT / "configs" / "transport_profiles.yaml")
    factory = ProviderFactory(
        registry,
        profiles,
        object_store,
        state,
        PROJECT_ROOT / "tests" / "fixtures",
    )
    for adapter_type, market, expected_total in (
        (SseOfficialReferenceProvider, Market.XSHG, 2316),
        (SzseOfficialReferenceProvider, Market.XSHE, 2899),
    ):
        provider = factory.create(adapter_type.provider_id)
        assert isinstance(provider, adapter_type)
        payload, snapshot = provider.fetch_master(market)
        rows = payload["rows"]
        assert payload["complete"] is True
        assert payload["total"] == expected_total
        assert payload["coverage_denominator"] == expected_total
        assert isinstance(rows, list)
        assert len(rows) == expected_total
        assert "raw_snapshot_ids" not in payload
        assert [str(item["code"]) for item in rows] == sorted(
            str(item["code"]) for item in rows
        )
        assert object_store.verify(snapshot.object_sha256)
        assert snapshot.source_id == adapter_type.provider_id
        assert snapshot.available_to_system_at == datetime.fromisoformat(
            str(payload["captured_at"])
        )


def test_reference_service_reconciles_sse_szse_against_official_denominators(
    tmp_path: Path,
) -> None:
    state = StateStore(tmp_path / "state.sqlite", PROJECT_ROOT / "migrations")
    state.migrate()
    objects = ObjectStore(tmp_path / "objects" / "sha256")
    service = MarketReferenceService(
        state,
        objects,
        ReferenceParquetStore(tmp_path / "parquet"),
        PROJECT_ROOT / "tests" / "fixtures" / "reference",
    )

    for market, provider_id, expected_total in (
        (Market.XSHG, "sse-official-reference", 2316),
        (Market.XSHE, "szse-official-reference", 2899),
    ):
        report = service.sync_instruments(market)
        assert report.status.value == "COMPLETE"
        assert report.provider_id == provider_id
        assert report.coverage.record_count == expected_total
        assert len(report.market_coverage_reconciliations) == 1
        reconciliation = report.market_coverage_reconciliations[0]
        assert reconciliation.market is market
        assert (
            reconciliation.coverage_level
            is UniverseCoverageLevel.OFFICIAL_DENOMINATOR_RECONCILED
        )
        assert (
            reconciliation.denominator_authority
            is UniverseDenominatorAuthority.PRIMARY_OFFICIAL
        )
        assert reconciliation.denominator_source_id == provider_id
        assert reconciliation.denominator_count == expected_total
        assert reconciliation.numerator_count == expected_total
        assert reconciliation.coverage_ratio == 1.0
        assert reconciliation.denominator_object_hash is not None
        assert objects.verify(reconciliation.denominator_object_hash)
        assert "PRIMARY_OFFICIAL_DENOMINATOR_RECONCILED" in reconciliation.reason_codes


def test_bjse_official_company_action_coverage_stays_honestly_unavailable() -> None:
    reference, _registry = _reference()

    # The disclosure enumeration layer only proves SSE/SZSE windows. BJSE has
    # a separate exact-item capture path, but no exhaustive enumeration or
    # negative-proof authority; market-level coverage must therefore stay unavailable.
    assert reference.official_coverage("corporate_actions.official_evidence", Market.BJSE) == (
        "UNAVAILABLE"
    )
    assert reference.official_coverage("corporate_actions.official_evidence", Market.XSHG) == (
        "AVAILABLE"
    )
    assert reference.official_coverage("corporate_actions.official_evidence", Market.XSHE) == (
        "AVAILABLE"
    )


def test_bjse_official_financial_report_exact_item_path_is_enabled() -> None:
    reference, _registry = _reference()

    assert reference.official_coverage("financial.official_report", Market.BJSE) == "AVAILABLE"
    assert reference.official_coverage("financial.official_report", Market.XSHG) == "AVAILABLE"
    assert reference.official_coverage("financial.official_report", Market.XSHE) == "AVAILABLE"


def test_financial_sources_enables_bjse_exact_item_official_reports() -> None:
    config: FinancialSourceConfig = load_financial_source_config(
        PROJECT_ROOT / "configs" / "financial_sources.yaml"
    )

    assert config.official_market_coverage[Market.BJSE] == "AVAILABLE"
    assert config.official_market_coverage[Market.XSHG] == "AVAILABLE"
    assert config.official_market_coverage[Market.XSHE] == "AVAILABLE"


def test_no_second_macro_router_in_market_reference_config() -> None:
    # Macro provider capability routing must live in the single source of truth
    # (provider_registry.yaml).  market_reference.yaml is the equity market-data
    # reference router and must not carry an unconsumed macro_authority_routes
    # second-router block.
    raw = yaml.safe_load(
        (PROJECT_ROOT / "configs" / "market_reference.yaml").read_text(encoding="utf-8")
    )
    assert "macro_authority_routes" not in raw


def test_macro_official_coverage_on_provider_definitions_is_exact_item() -> None:
    registry = load_provider_registry(PROJECT_ROOT / "configs" / "provider_registry.yaml")
    macro_ids = {
        "nbs-statistical-release",
        "pboc-monetary-policy-release",
        "mof-fiscal-policy-release",
        "ndrc-pricing-policy-release",
    }
    for item in registry.providers:
        if item.provider_id not in macro_ids:
            continue
        # Recorded-first macro authorities must never silently go live.
        assert item.live_supported is False
        macro_capabilities = [
            capability
            for capability in item.capabilities
            if capability.startswith("macro.")
            and item.completeness_semantics.get(capability) is CompletenessSemantics.EXACT_ITEM
        ]
        assert macro_capabilities


def test_macro_providers_construct_via_factory_and_fetch_recorded_fixtures(
    state: StateStore,
    object_store: ObjectStore,
) -> None:
    registry = load_provider_registry(PROJECT_ROOT / "configs" / "provider_registry.yaml")
    profiles = load_transport_profiles(PROJECT_ROOT / "configs" / "transport_profiles.yaml")
    factory = ProviderFactory(
        registry,
        profiles,
        object_store,
        state,
        PROJECT_ROOT / "tests" / "fixtures",
    )

    cases = [
        (NbsStatisticalReleaseProvider, "gdp", date(2026, 6, 30)),
        (PbocMonetaryPolicyReleaseProvider, "m2", date(2026, 6, 30)),
        (MofFiscalPolicyReleaseProvider, "fiscal_revenue", date(2026, 6, 30)),
        (NdrcPricingPolicyReleaseProvider, "refined_oil_price", date(2026, 7, 1)),
    ]
    pit_repository = PointInTimeRepository(state)
    for adapter_type, indicator_code, expected_period_end in cases:
        provider = cast(
            NbsStatisticalReleaseProvider
            | PbocMonetaryPolicyReleaseProvider
            | MofFiscalPolicyReleaseProvider
            | NdrcPricingPolicyReleaseProvider,
            factory.create(adapter_type.provider_id),
        )
        assert isinstance(provider, adapter_type)
        payload, snapshot = provider.fetch_indicator(indicator_code)
        recorded_available = datetime.fromisoformat(str(payload["available_to_system_at"]))
        assert payload["indicator_code"] == indicator_code
        assert snapshot.source_id == adapter_type.provider_id
        assert snapshot.source_url == payload["source_url"]
        assert snapshot.available_to_system_at == recorded_available
        assert isinstance(payload.get("data_points"), list)
        assert payload["data_points"]
        assert object_store.verify(snapshot.object_sha256)
        pit_rows = pit_repository.for_snapshot(snapshot.snapshot_id)
        assert len(pit_rows) == 1
        pit = pit_rows[0]
        assert pit.source_id == (
            f"macro:{adapter_type.provider_id}:{indicator_code}:"
            f"{payload['observation_period']}:v{payload['revision_version']}"
        )
        assert pit.period_end == expected_period_end
        assert pit.published_at is None
        assert pit.available_to_system_at == snapshot.available_to_system_at
        assert pit.point_in_time_status is PointInTimeStatus.CERTIFIED
        assert pit.availability_basis is AvailabilityBasis.FETCH_OBSERVED


def test_macro_recorded_first_never_goes_live_unconditionally(
    state: StateStore,
    object_store: ObjectStore,
) -> None:
    registry = load_provider_registry(PROJECT_ROOT / "configs" / "provider_registry.yaml")
    profiles = load_transport_profiles(PROJECT_ROOT / "configs" / "transport_profiles.yaml")
    factory = ProviderFactory(
        registry,
        profiles,
        object_store,
        state,
        PROJECT_ROOT / "tests" / "fixtures",
    )
    for provider_id in (
        "nbs-statistical-release",
        "pboc-monetary-policy-release",
        "mof-fiscal-policy-release",
        "ndrc-pricing-policy-release",
    ):
        provider = factory.create(provider_id)
        if not isinstance(provider, NbsStatisticalReleaseProvider) and not isinstance(
            provider, PbocMonetaryPolicyReleaseProvider
        ):
            if not isinstance(provider, MofFiscalPolicyReleaseProvider) and not isinstance(
                provider, NdrcPricingPolicyReleaseProvider
            ):
                raise AssertionError(f"Unexpected macro adapter: {provider_id}")
        try:
            provider.fetch_indicator("unused", live=True)
        except MacroAuthorityReleaseError:
            pass
        else:
            raise AssertionError(
                f"{provider_id} must not support unconditional live fetch"
            )
