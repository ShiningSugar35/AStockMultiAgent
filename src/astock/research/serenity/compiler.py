"""Deterministic compilers from canonical project artifacts into Serenity inputs."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Protocol

from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.evidence.repository import EvidenceRepository
from astock.research.serenity.policy import validate_serenity_method_evidence
from astock.schemas import (
    BarRequest,
    Frequency,
    FrozenEvidencePack,
    InstrumentType,
    MarketBar,
    QualityStatus,
)
from astock.schemas.institutional_research import FundamentalModelBundle
from astock.schemas.serenity.compiler import (
    CanonicalDailyTrendCompileRequest,
    CanonicalFundamentalBinding,
)
from astock.schemas.serenity_v2 import (
    DailySeriesV2,
    DailyTrendHealthContractV2,
    MovingAverageV2,
)

_DAILY_WINDOWS = (20, 50, 100, 200)


class CanonicalDailyStore(Protocol):
    def load_manifest(self, request: BarRequest) -> dict[str, object] | None: ...

    def read_bars(self, request: BarRequest) -> list[MarketBar]: ...


class SerenityInputCompiler:
    """Compile only mechanically derivable Serenity fields from immutable local facts."""

    def __init__(
        self,
        state: StateStore,
        objects: ObjectStore,
        canonical_market: CanonicalDailyStore,
    ) -> None:
        self.state = state
        self.objects = objects
        self.canonical_market = canonical_market
        self.evidence_repository = EvidenceRepository(state)

    def compile_daily_trend(
        self,
        request: CanonicalDailyTrendCompileRequest,
        *,
        evidence_pack: FrozenEvidencePack,
    ) -> DailyTrendHealthContractV2:
        """Build deterministic 20/50/100/200-day moving averages from one canonical D1 dataset."""

        if (
            evidence_pack.company_id != request.target_company_id
            or evidence_pack.as_of != request.as_of
        ):
            raise ValueError("canonical daily compile evidence pack company/as_of mismatch")
        bar_request = BarRequest(
            symbol=request.symbol,
            market=request.market,
            exchange=request.market,
            instrument_type=InstrumentType.STOCK,
            frequency=Frequency.D1,
            requested_start=request.requested_start,
            requested_end=request.as_of,
            adjustment_mode=request.adjustment_mode,
            limit=10000,
        )
        manifest = self.canonical_market.load_manifest(bar_request)
        if manifest is None:
            raise ValueError("canonical daily manifest is unavailable")
        self._validate_daily_manifest(manifest, request)
        bars = [
            bar
            for bar in self.canonical_market.read_bars(bar_request)
            if request.requested_start <= bar.timestamp <= request.as_of
        ]
        bars.sort(key=lambda item: item.timestamp)
        if len(bars) < max(_DAILY_WINDOWS):
            raise ValueError("canonical daily compile requires at least 200 frozen daily bars")
        if any(
            bar.frequency is not Frequency.D1
            or bar.market is not request.market
            or bar.symbol != request.symbol
            or bar.adjustment_mode is not request.adjustment_mode
            for bar in bars
        ):
            raise ValueError("canonical daily bars drift from the requested dataset identity")
        dataset_version = str(manifest.get("content_hash", ""))
        if len(dataset_version) != 64:
            raise ValueError("canonical daily manifest lacks a valid content hash")
        quality_report_id = str(manifest.get("quality_report_id", ""))
        if not quality_report_id:
            raise ValueError("canonical daily manifest lacks a quality report id")

        moving_averages = []
        for window in _DAILY_WINDOWS:
            selected = bars[-window:]
            mean_close = sum((bar.close for bar in selected), Decimal("0")) / Decimal(window)
            moving_averages.append(
                MovingAverageV2(
                    window=window,
                    value=mean_close,
                    close=bars[-1].close,
                    currency="CNY",
                    calculated_at=request.as_of,
                    bars_used=window,
                    dataset_version=dataset_version,
                    calculation_status="CANONICAL_DETERMINISTIC",
                    evidence_ids=request.daily_evidence_ids,
                )
            )

        contract = DailyTrendHealthContractV2(
            target_company_id=request.target_company_id,
            as_of=request.as_of,
            daily_series=DailySeriesV2(
                symbol=request.symbol,
                as_of=request.as_of,
                quality_report_id=quality_report_id,
                bar_count=len(bars),
                adjustment_mode=request.adjustment_mode,
                dataset_version=dataset_version,
                evidence_ids=request.daily_evidence_ids,
            ),
            moving_averages=moving_averages,
            fundamental_growth=request.fundamental_growth,
            estimate_revisions=request.estimate_revisions,
            evidence_ids=sorted(
                {
                    evidence_id
                    for node in (
                        request.daily_evidence_ids,
                        *(item.evidence_ids for item in request.fundamental_growth),
                        *(item.evidence_ids for item in request.estimate_revisions),
                    )
                    for evidence_id in node
                }
            ),
        )
        validate_serenity_method_evidence(
            self.state,
            self.evidence_repository,
            contract,
            evidence_pack=evidence_pack,
            base_as_of=request.as_of,
        )
        return contract

    def bind_fundamental_model(
        self,
        bundle_artifact_id: str,
        *,
        expected_company_id: str,
        expected_as_of: datetime,
    ) -> CanonicalFundamentalBinding:
        """Expose immutable Phase-9 artifact identity without creating a second valuation ledger."""

        with self.state.connect() as connection:
            row = connection.execute(
                "SELECT artifact_type, object_hash FROM artifact_registry WHERE artifact_id=?",
                (bundle_artifact_id,),
            ).fetchone()
        if row is None or str(row["artifact_type"]) != "FundamentalModelBundle":
            raise ValueError("fundamental model bundle artifact is unavailable")
        bundle_hash = str(row["object_hash"])
        bundle = FundamentalModelBundle.model_validate_json(self.objects.get_bytes(bundle_hash))
        if bundle.company_id != expected_company_id or bundle.as_of != expected_as_of:
            raise ValueError("fundamental model bundle company/as_of mismatch")
        valuation_hash = bundle.artifact_object_hashes.get(bundle.valuation_pack_artifact_id)
        if valuation_hash is None:
            raise ValueError("fundamental model bundle lacks its valuation hash")
        return CanonicalFundamentalBinding(
            company_id=bundle.company_id,
            as_of=bundle.as_of,
            fundamental_model_bundle_artifact_id=bundle_artifact_id,
            fundamental_model_bundle_object_hash=bundle_hash,
            valuation_pack_artifact_id=bundle.valuation_pack_artifact_id,
            valuation_pack_object_hash=valuation_hash,
            source_artifact_ids=sorted({bundle_artifact_id, *bundle.artifact_object_hashes.keys()}),
            source_object_hashes=sorted({bundle_hash, *bundle.artifact_object_hashes.values()}),
        )

    @staticmethod
    def _validate_daily_manifest(
        manifest: dict[str, object],
        request: CanonicalDailyTrendCompileRequest,
    ) -> None:
        expected = {
            "market": request.market.value,
            "symbol": request.symbol,
            "frequency": Frequency.D1.value,
            "adjustment_mode": request.adjustment_mode.value,
        }
        if any(str(manifest.get(key)) != value for key, value in expected.items()):
            raise ValueError("canonical daily manifest identity mismatch")
        if str(manifest.get("quality_status")) != QualityStatus.PASS.value:
            raise ValueError("canonical daily manifest must pass its quality gate")
        actual_end = manifest.get("actual_end")
        if actual_end is None:
            raise ValueError("canonical daily manifest has no actual end")
        parsed_end = datetime.fromisoformat(str(actual_end))
        if parsed_end > request.as_of:
            raise ValueError("canonical daily manifest contains future bars relative to as_of")


__all__ = ["SerenityInputCompiler"]
