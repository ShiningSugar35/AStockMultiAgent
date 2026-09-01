"""Low-cost research seeds from market snapshots, existing candidates, and expert Skills."""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from math import log1p
from pathlib import Path
from threading import Lock
from typing import Any, Protocol, cast

import yaml
from pydantic import ValidationError

from astock.candidates.repository import CandidateRepository
from astock.core.errors import AStockError
from astock.core.hashing import content_hash
from astock.core.object_store import ObjectStore
from astock.core.source_resilience import (
    SourceCircuitBreaker,
    SourceFailureClass,
    classify_source_error,
    scoped_source_capability,
)
from astock.core.state import StateStore
from astock.knowledge.visual_skill_repository import VisualSkillRepository
from astock.providers.config import load_provider_registry
from astock.schemas.evidence import FetchStatus, SourceSnapshot
from astock.schemas.market import CompletenessSemantics, Market
from astock.schemas.provider import ProviderOfficiality, ProviderRegistry
from astock.schemas.reference_data import (
    DatasetReleaseManifest,
    ReferenceCoverageStatus,
    ReferenceDatasetKind,
)
from astock.schemas.research_seeds import (
    ExpertDomainEvidence,
    ExpertDomainProfile,
    ResearchSeed,
    ResearchSeedOrigin,
    ResearchSeedReport,
    ResearchSeedRequest,
    ResearchSeedStatus,
    ResearchUniverseCoverageStatus,
)
from astock.schemas.universe_coverage import (
    MarketCoverageReconciliation,
    UniverseCoverageLevel,
    UniverseCoverageProof,
    UniverseDenominatorAuthority,
    universe_coverage_proof_id,
)

_CURRENT_LIVE_TOLERANCE = timedelta(minutes=15)
_FULL_MARKET_COVERAGE_RATIO = 0.995
_EQUITY_MARKETS = (Market.XSHG, Market.XSHE, Market.BJSE)


def _coverage_capability_for_market(market: Market) -> str:
    return "instrument.bjse_coverage" if market is Market.BJSE else "instrument.master"


class SeedSnapshotProvider(Protocol):
    def fetch_seed_snapshot(
        self, market: Market, *, live: bool = False
    ) -> tuple[dict[str, object], Any]: ...


class UniverseCoverageProvider(Protocol):
    provider_id: str

    def fetch_master(
        self, market: Market, *, live: bool = False
    ) -> tuple[dict[str, object], SourceSnapshot]: ...


class SeedMarketProvider(SeedSnapshotProvider, Protocol):
    def fetch_industry_boards(self, *, live: bool = False) -> tuple[dict[str, object], Any]: ...

    def fetch_industry_constituents(
        self, board_code: str, *, live: bool = False
    ) -> tuple[dict[str, object], Any]: ...


class ResearchSeedProviderRouter:
    """Keep Blind Market discovery alive when the primary market snapshot provider fails."""

    def __init__(
        self,
        primary: SeedMarketProvider | None = None,
        fallback: SeedSnapshotProvider | None = None,
        *,
        providers: Sequence[SeedSnapshotProvider] | None = None,
        minimum_rows_by_market: dict[Market, int],
        state: StateStore | None = None,
        objects: ObjectStore | None = None,
        coverage_providers: dict[Market, UniverseCoverageProvider] | None = None,
        cache_freshness: timedelta = _CURRENT_LIVE_TOLERANCE,
    ) -> None:
        configured: list[SeedSnapshotProvider] = (
            list(providers)
            if providers is not None
            else [provider for provider in (primary, fallback) if provider is not None]
        )
        if not configured and (state is None or objects is None):
            raise ValueError("Research seed routing requires a provider or a durable local cache")
        if coverage_providers and (state is None or objects is None):
            raise ValueError(
                "Official Universe coverage binding requires durable state and objects"
            )
        self.providers = tuple(configured)
        self.primary = primary or (configured[0] if configured else None)
        self.fallback = fallback or (
            configured[1] if len(configured) > 1 else (configured[0] if configured else None)
        )
        self.minimum_rows_by_market = dict(minimum_rows_by_market)
        self.state = state
        self.objects = objects
        self.source_breaker = SourceCircuitBreaker(state) if state is not None else None
        self.coverage_providers = dict(coverage_providers or {})
        self.cache_freshness = cache_freshness
        self._coverage_lock = Lock()
        self._coverage_cache: dict[Market, tuple[dict[str, object], SourceSnapshot]] = {}

    def fetch_seed_snapshot(
        self, market: Market, *, live: bool = False
    ) -> tuple[dict[str, object], Any]:
        last_error: Exception | None = None
        best_partial: tuple[dict[str, object], Any] | None = None
        best_partial_ratio = -1.0
        if live:
            cached = self._fresh_cached_seed_snapshot(market)
            if cached is not None:
                cached_ratio = _seed_payload_coverage_ratio(cached[0], market)
                if cached_ratio is not None and cached_ratio >= _FULL_MARKET_COVERAGE_RATIO:
                    return cached
                best_partial = cached
                best_partial_ratio = cached_ratio if cached_ratio is not None else -1.0
        coverage_proof = self._coverage_proof(market, live=live) if live else None
        breaker_capability = scoped_source_capability("market.seed_snapshot", market.value)
        for provider in self.providers:
            provider_id = str(getattr(provider, "provider_id", "")).strip()
            if (
                live
                and self.source_breaker is not None
                and provider_id
                and not self.source_breaker.claim_attempt(provider_id, breaker_capability)
            ):
                last_error = ValueError(f"MARKET_SEED_CIRCUIT_OPEN:{provider_id}")
                continue
            try:
                payload, snapshot = provider.fetch_seed_snapshot(market, live=live)
                if not live:
                    return payload, snapshot
                if coverage_proof is not None:
                    payload, snapshot = self._bind_official_coverage(
                        payload,
                        snapshot,
                        market,
                        coverage_proof,
                    )
                if _seed_payload_row_count(payload) < self.minimum_rows_by_market[market]:
                    if self.source_breaker is not None and provider_id:
                        self.source_breaker.record_failure(
                            provider_id,
                            breaker_capability,
                            SourceFailureClass.COVERAGE_INCOMPLETE,
                        )
                    last_error = ValueError("MARKET_SEED_BELOW_MINIMUM_COVERAGE")
                    continue
                ratio = _seed_payload_coverage_ratio(payload, market)
                if ratio is not None and ratio >= _FULL_MARKET_COVERAGE_RATIO:
                    if self.source_breaker is not None and provider_id:
                        self.source_breaker.record_success(provider_id, breaker_capability)
                    return payload, snapshot
                if self.source_breaker is not None and provider_id:
                    self.source_breaker.record_failure(
                        provider_id,
                        breaker_capability,
                        SourceFailureClass.COVERAGE_INCOMPLETE,
                    )
                ranked_ratio = ratio if ratio is not None else -1.0
                if best_partial is None or ranked_ratio > best_partial_ratio:
                    best_partial = (payload, snapshot)
                    best_partial_ratio = ranked_ratio
            except (AStockError, OSError, RuntimeError, ValueError) as exc:
                last_error = exc
                if self.source_breaker is not None and provider_id:
                    self.source_breaker.record_failure(
                        provider_id,
                        breaker_capability,
                        classify_source_error(exc),
                    )
        if live and best_partial is not None:
            return best_partial
        if last_error is not None:
            raise last_error
        raise RuntimeError("research seed provider route is empty")

    def _coverage_proof(
        self,
        market: Market,
        *,
        live: bool,
    ) -> tuple[dict[str, object], SourceSnapshot] | None:
        provider = self.coverage_providers.get(market)
        if provider is None or self.state is None or self.objects is None:
            return None
        with self._coverage_lock:
            cached = self._coverage_cache.get(market)
            if cached is not None:
                return cached
            capability = _coverage_capability_for_market(market)
            provider_id = provider.provider_id
            if (
                live
                and self.source_breaker is not None
                and not self.source_breaker.claim_attempt(provider_id, capability)
            ):
                return None
            try:
                payload, snapshot = provider.fetch_master(market, live=live)
                _official_coverage_symbols(payload, market)
                registered = self.state.get_snapshot(snapshot.snapshot_id)
                if (
                    registered is None
                    or registered.source_id != provider_id
                    or registered.object_sha256 != snapshot.object_sha256
                    or not self.objects.verify(snapshot.object_sha256)
                ):
                    raise ValueError("Official Universe coverage snapshot failed verification")
                if live and self.source_breaker is not None:
                    self.source_breaker.record_success(provider_id, capability)
            except (AStockError, OSError, RuntimeError, ValueError) as exc:
                if live and self.source_breaker is not None:
                    self.source_breaker.record_failure(
                        provider_id,
                        capability,
                        classify_source_error(exc),
                    )
                return None
            result = (payload, snapshot)
            self._coverage_cache[market] = result
            return result

    def _bind_official_coverage(
        self,
        payload: dict[str, object],
        snapshot: SourceSnapshot,
        market: Market,
        coverage_proof: tuple[dict[str, object], SourceSnapshot],
    ) -> tuple[dict[str, object], SourceSnapshot]:
        if self.state is None or self.objects is None:
            raise ValueError(
                "Official Universe coverage binding requires durable state and objects"
            )
        proof_payload, proof_snapshot = coverage_proof
        registered_market = self.state.get_snapshot(snapshot.snapshot_id)
        if (
            registered_market is None
            or registered_market.object_sha256 != snapshot.object_sha256
            or not self.objects.verify(snapshot.object_sha256)
        ):
            raise ValueError("Market seed snapshot failed verification")
        official_symbols = _official_coverage_symbols(proof_payload, market)
        observed_symbols = _seed_payload_symbols(payload, market)
        if not observed_symbols:
            raise ValueError("Market seed payload contains no valid market symbols")
        unknown_symbols = observed_symbols - official_symbols
        if unknown_symbols:
            raise ValueError("Market seed payload contains symbols outside official Universe")
        raw_proof_ids = proof_payload.get("page_snapshot_ids")
        proof_ids = [str(item) for item in raw_proof_ids] if isinstance(raw_proof_ids, list) else []
        proof_ids.append(proof_snapshot.snapshot_id)
        proof_ids = list(dict.fromkeys(proof_ids))
        proof_hashes: list[str] = []
        available_at = max(
            snapshot.available_to_system_at,
            proof_snapshot.available_to_system_at,
        )
        for proof_id in proof_ids:
            registered = self.state.get_snapshot(proof_id)
            if registered is None or not self.objects.verify(registered.object_sha256):
                raise ValueError("Official Universe coverage lineage failed verification")
            proof_hashes.append(registered.object_sha256)
            available_at = max(available_at, registered.available_to_system_at)
        decorated = dict(payload)
        decorated.update(
            {
                "coverage_denominator": len(official_symbols),
                "coverage_numerator": len(observed_symbols),
                "market_snapshot_id": snapshot.snapshot_id,
                "market_snapshot_object_hash": snapshot.object_sha256,
                "coverage_proof_source_id": proof_snapshot.source_id,
                "coverage_proof_capability": _coverage_capability_for_market(market),
                "coverage_proof_snapshot_ids": proof_ids,
                "coverage_proof_object_hashes": proof_hashes,
                "coverage_proof_complete": True,
            }
        )
        object_ref = self.objects.put_json(decorated)
        now = datetime.now(UTC)
        derived = SourceSnapshot(
            snapshot_id=(f"{snapshot.source_id}:official-covered:{object_ref.sha256}"),
            source_id=snapshot.source_id,
            object_sha256=object_ref.sha256,
            fetched_at=now,
            available_to_system_at=available_at,
            source_url=snapshot.source_url,
            mime="application/json",
            byte_size=object_ref.byte_size,
            headers_hash=content_hash(
                {
                    "market_snapshot_id": snapshot.snapshot_id,
                    "coverage_proof_snapshot_ids": proof_ids,
                }
            ),
            fetch_status=FetchStatus.SUCCEEDED,
            rights_status="PUBLIC_REFERENCE_DATA",
        )
        self.state.register_snapshot(derived)
        return decorated, derived

    def _fresh_cached_seed_snapshot(self, market: Market) -> tuple[dict[str, object], Any] | None:
        if self.state is None or self.objects is None:
            return None
        releases = self.state.list_market_reference_releases("INSTRUMENT_MASTER", market.value)
        now = datetime.now(UTC)
        for release in releases:
            manifest_hash = str(release.get("manifest_object_hash") or "")
            if not manifest_hash or not self.objects.verify(manifest_hash):
                continue
            try:
                manifest = DatasetReleaseManifest.model_validate_json(
                    self.objects.get_bytes(manifest_hash)
                )
            except (OSError, ValidationError, ValueError):
                continue
            release_identity = {
                "dataset_kind": manifest.dataset_kind.value,
                "scope_key": manifest.scope_key,
                "provider_id": manifest.provider_id,
                "batch_id": manifest.batch_id,
                "content_hash": manifest.content_hash,
                "previous_release_id": manifest.previous_release_id,
                "available_to_system_at": manifest.available_to_system_at.isoformat(),
            }
            if (
                manifest.dataset_kind is not ReferenceDatasetKind.INSTRUMENT_MASTER
                or manifest.scope_key != market.value
                or not manifest.provider_id
                or str(release.get("provider_id") or "") != manifest.provider_id
                or manifest.coverage.status is not ReferenceCoverageStatus.COMPLETE
                or manifest.coverage.record_count < self.minimum_rows_by_market[market]
                or str(release.get("release_id") or "") != manifest.release_id
                or manifest.release_id != content_hash(release_identity)
            ):
                continue
            age = now - manifest.available_to_system_at.astimezone(UTC)
            if age < timedelta(0) or age > self.cache_freshness:
                continue
            for snapshot_id in reversed(manifest.raw_snapshot_ids):
                snapshot = self.state.get_snapshot(snapshot_id)
                if (
                    snapshot is None
                    or snapshot.source_id != manifest.provider_id
                    or not self.objects.verify(snapshot.object_sha256)
                ):
                    continue
                try:
                    payload = json.loads(self.objects.get_bytes(snapshot.object_sha256))
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
                if not isinstance(payload, dict):
                    continue
                request = payload.get("_astock_request")
                if (
                    isinstance(request, dict)
                    and request.get("market") == market.value
                    and _seed_payload_row_count(payload) >= self.minimum_rows_by_market[market]
                ):
                    # Reuse any fresh, integrity-checked local Instrument Master whose
                    # captured payload is compatible with seed parsing. Formal FULL is
                    # still decided later by _seed_payload_coverage_ratio; a legacy
                    # COMPLETE manifest or a pagination floor never manufactures 99.5%.
                    return payload, snapshot
        return None

    def fetch_industry_boards(self, *, live: bool = False) -> tuple[dict[str, object], Any]:
        last_error: Exception | None = None
        for provider in self.providers:
            fetch = getattr(provider, "fetch_industry_boards", None)
            if not callable(fetch):
                continue
            try:
                board_fetch = cast(Callable[..., tuple[dict[str, object], Any]], fetch)
                return board_fetch(live=live)
            except (AStockError, OSError, RuntimeError, ValueError) as exc:
                last_error = exc
        if last_error is not None:
            raise last_error
        raise RuntimeError("No routed provider exposes industry-board discovery")

    def fetch_industry_constituents(
        self, board_code: str, *, live: bool = False
    ) -> tuple[dict[str, object], Any]:
        last_error: Exception | None = None
        for provider in self.providers:
            fetch = getattr(provider, "fetch_industry_constituents", None)
            if not callable(fetch):
                continue
            try:
                constituent_fetch = cast(Callable[..., tuple[dict[str, object], Any]], fetch)
                return constituent_fetch(board_code, live=live)
            except (AStockError, OSError, RuntimeError, ValueError) as exc:
                last_error = exc
        if last_error is not None:
            raise last_error
        raise RuntimeError("No routed provider exposes industry-constituent discovery")


def _safe_float(value: object) -> float:
    if not isinstance(value, (str, int, float)):
        return 0.0
    try:
        return float(value)
    except ValueError:
        return 0.0


def _sina_activity_unavailable(payload: dict[str, object]) -> bool:
    if payload.get("_astock_source") != "SINA_MARKET_CENTER":
        return False
    rows = payload.get("rows")
    if not isinstance(rows, list) or not rows:
        return False
    valid = [item for item in rows if isinstance(item, dict)]
    if len(valid) < max(1, int(len(rows) * 0.95)):
        return False
    threshold = len(valid) * 0.95
    zero_amount = sum(_safe_float(item.get("amount")) <= 0 for item in valid)
    zero_turnover = sum(_safe_float(item.get("turnoverratio")) <= 0 for item in valid)
    zero_trade = sum(_safe_float(item.get("trade")) <= 0 for item in valid)
    positive_settlement = sum(_safe_float(item.get("settlement")) > 0 for item in valid)
    return (
        zero_amount >= threshold
        and zero_turnover >= threshold
        and zero_trade >= threshold
        and positive_settlement >= threshold
    )


def _official_coverage_symbols(
    payload: dict[str, object],
    market: Market,
) -> set[str]:
    if payload.get("_astock_source") != "BSE_OFFICIAL_LIST" or market is not Market.BJSE:
        raise ValueError("Unsupported official Universe coverage proof")
    request = payload.get("_astock_request")
    rows = payload.get("rows")
    if (
        not isinstance(request, dict)
        or request.get("market") != market.value
        or payload.get("complete") is not True
        or not isinstance(rows, list)
        or any(not isinstance(item, dict) for item in rows)
    ):
        raise ValueError("Official Universe coverage proof is malformed")
    try:
        total = int(str(payload["total"]))
        denominator = int(str(payload["coverage_denominator"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("Official Universe coverage denominator is malformed") from exc
    symbols = {
        str(item.get("code") or "")
        for item in rows
        if isinstance(item, dict)
        and len(str(item.get("code") or "")) == 6
        and str(item.get("code") or "").isdigit()
        and bool(str(item.get("name") or "").strip())
    }
    if total <= 0 or denominator != total or len(rows) != total or len(symbols) != total:
        raise ValueError("Official Universe coverage proof is incomplete")
    return symbols


def _seed_payload_symbols(payload: dict[str, object], market: Market) -> set[str]:
    request = payload.get("_astock_request")
    if not isinstance(request, dict) or request.get("market") != market.value:
        return set()
    if payload.get("_astock_source") == "SINA_MARKET_CENTER":
        rows = payload.get("rows")
        prefix = {Market.XSHG: "sh", Market.XSHE: "sz", Market.BJSE: "bj"}.get(market)
        if not isinstance(rows, list) or prefix is None:
            return set()
        return {
            str(item.get("code") or "")
            for item in rows
            if isinstance(item, dict)
            and len(str(item.get("code") or "")) == 6
            and str(item.get("code") or "").isdigit()
            and bool(str(item.get("name") or "").strip())
            and str(item.get("symbol") or "") == f"{prefix}{item.get('code')}"
        }
    data = payload.get("data")
    if not isinstance(data, dict):
        return set()
    diff = data.get("diff")
    rows = list(diff.values()) if isinstance(diff, dict) else diff
    if not isinstance(rows, list):
        return set()
    return {
        str(item.get("f12") or "")
        for item in rows
        if isinstance(item, dict)
        and len(str(item.get("f12") or "")) == 6
        and str(item.get("f12") or "").isdigit()
        and bool(str(item.get("f14") or "").strip())
    }


def _seed_payload_row_count(payload: dict[str, object]) -> int:
    if payload.get("_astock_source") == "SINA_MARKET_CENTER":
        rows = payload.get("rows")
        request = payload.get("_astock_request")
        if not isinstance(rows, list) or not isinstance(request, dict):
            return 0
        market = str(request.get("market") or "")
        prefix = {"XSHG": "sh", "XSHE": "sz", "BJSE": "bj"}.get(market)
        if prefix is None:
            return 0
        return sum(
            1
            for item in rows
            if isinstance(item, dict)
            and len(str(item.get("code") or "")) == 6
            and str(item.get("code") or "").isdigit()
            and bool(str(item.get("name") or "").strip())
            and str(item.get("symbol") or "") == f"{prefix}{item.get('code')}"
        )
    data = payload.get("data")
    if not isinstance(data, dict):
        return 0
    diff = data.get("diff")
    if isinstance(diff, dict):
        rows = list(diff.values())
    elif isinstance(diff, list):
        rows = diff
    else:
        return 0
    return sum(
        1
        for item in rows
        if isinstance(item, dict)
        and len(str(item.get("f12") or "")) == 6
        and str(item.get("f12") or "").isdigit()
        and bool(str(item.get("f14") or "").strip())
    )


def _seed_payload_coverage_ratio(payload: dict[str, object], market: Market) -> float | None:
    """Return auditable market coverage; a row-count floor alone never proves FULL."""

    request = payload.get("_astock_request")
    if not isinstance(request, dict) or request.get("market") != market.value:
        return None
    valid_rows = _seed_payload_row_count(payload)
    if payload.get("coverage_proof_complete") is True:
        raw_ids = payload.get("coverage_proof_snapshot_ids")
        raw_hashes = payload.get("coverage_proof_object_hashes")
        try:
            numerator = int(str(payload["coverage_numerator"]))
            denominator = int(str(payload["coverage_denominator"]))
        except (KeyError, TypeError, ValueError):
            return None
        if (
            not isinstance(payload.get("coverage_proof_source_id"), str)
            or payload.get("coverage_proof_capability") != _coverage_capability_for_market(market)
            or not isinstance(raw_ids, list)
            or not raw_ids
            or not isinstance(raw_hashes, list)
            or len(raw_ids) != len(raw_hashes)
            or not isinstance(payload.get("market_snapshot_id"), str)
            or not isinstance(payload.get("market_snapshot_object_hash"), str)
            or denominator <= 0
            or numerator != valid_rows
            or numerator > denominator
        ):
            return None
        return min(1.0, numerator / denominator)
    if payload.get("_astock_source") == "SINA_MARKET_CENTER":
        rows = payload.get("rows")
        if payload.get("complete") is not True or not isinstance(rows, list) or not rows:
            return None
        # Sina's market-center list has no authoritative Universe total. Pagination
        # exhaustion plus the legacy row-count floor is useful truncation defence, but
        # it cannot prove >=99.5% formal market coverage. Only admit a ratio when an
        # explicit auditable denominator is present in the captured payload.
        raw_total = payload.get("coverage_denominator")
        try:
            if isinstance(raw_total, bool):
                return None
            total = int(raw_total)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return None
        if total <= 0 or len(rows) > total:
            return None
        return min(1.0, valid_rows / total)

    data = payload.get("data")
    if not isinstance(data, dict):
        return None
    raw_total = data.get("total")
    try:
        if isinstance(raw_total, bool):
            return None
        total = int(raw_total)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    diff = data.get("diff")
    if isinstance(diff, dict):
        observed = len(diff)
    elif isinstance(diff, list):
        observed = len(diff)
    else:
        return None
    if total <= 0 or observed > total:
        return None
    return min(1.0, valid_rows / total)


def _seed_payload_coverage_counts(
    payload: dict[str, object], market: Market
) -> tuple[int, int] | None:
    request = payload.get("_astock_request")
    if not isinstance(request, dict) or request.get("market") != market.value:
        return None
    numerator = _seed_payload_row_count(payload)
    raw_denominator: object | None = None
    if payload.get("coverage_proof_complete") is True:
        raw_numerator = payload.get("coverage_numerator")
        raw_denominator = payload.get("coverage_denominator")
        try:
            if isinstance(raw_numerator, bool) or int(str(raw_numerator)) != numerator:
                return None
        except (TypeError, ValueError):
            return None
    elif payload.get("_astock_source") == "SINA_MARKET_CENTER":
        if payload.get("complete") is not True:
            return None
        raw_denominator = payload.get("coverage_denominator")
    else:
        data = payload.get("data")
        if not isinstance(data, dict):
            return None
        raw_denominator = data.get("total")
    try:
        if isinstance(raw_denominator, bool):
            return None
        denominator = int(raw_denominator)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if denominator <= 0:
        return None
    return numerator, denominator


def _provider_denominator_authority(
    registry: ProviderRegistry,
    provider_id: str,
    capability: str,
) -> UniverseDenominatorAuthority:
    definition = next(
        (item for item in registry.providers if item.provider_id == provider_id),
        None,
    )
    if definition is None:
        return UniverseDenominatorAuthority.UNKNOWN
    formally_declared = (
        capability in definition.capabilities
        and capability in definition.formal_capabilities
        and definition.completeness_semantics.get(capability) is CompletenessSemantics.FULL_UNIVERSE
    )
    if definition.officiality is ProviderOfficiality.PRIMARY_OFFICIAL and formally_declared:
        return UniverseDenominatorAuthority.PRIMARY_OFFICIAL
    return UniverseDenominatorAuthority.SECONDARY_SELF_REPORTED


def _market_coverage_reconciliation(
    payload: dict[str, object],
    snapshot: SourceSnapshot,
    market: Market,
    *,
    state: StateStore,
    objects: ObjectStore,
    registry: ProviderRegistry,
) -> MarketCoverageReconciliation:
    counts = _seed_payload_coverage_counts(payload, market)
    numerator = _seed_payload_row_count(payload)
    source_ids = {snapshot.snapshot_id}
    available_at = snapshot.available_to_system_at
    observed_at = snapshot.fetched_at
    denominator_source_id = snapshot.source_id
    denominator_capability: str | None = None
    denominator_object_hash: str | None = None
    source_version = snapshot.snapshot_id
    numerator_object_hash = (
        snapshot.object_sha256 if objects.verify(snapshot.object_sha256) else None
    )
    authority = UniverseDenominatorAuthority.UNKNOWN
    reason_codes: set[str] = set()

    if payload.get("coverage_proof_complete") is True:
        proof_source_id = payload.get("coverage_proof_source_id")
        proof_capability = payload.get("coverage_proof_capability")
        raw_ids = payload.get("coverage_proof_snapshot_ids")
        raw_hashes = payload.get("coverage_proof_object_hashes")
        market_snapshot_id = payload.get("market_snapshot_id")
        market_snapshot_object_hash = payload.get("market_snapshot_object_hash")
        if (
            not isinstance(proof_source_id, str)
            or proof_capability != _coverage_capability_for_market(market)
            or not isinstance(raw_ids, list)
            or not isinstance(raw_hashes, list)
            or not raw_ids
            or len(raw_ids) != len(raw_hashes)
            or not isinstance(market_snapshot_id, str)
            or not isinstance(market_snapshot_object_hash, str)
        ):
            counts = None
            reason_codes.add("COVERAGE_PROOF_LINEAGE_MALFORMED")
        else:
            denominator_source_id = proof_source_id
            denominator_capability = str(proof_capability)
            registered_derived = state.get_snapshot(snapshot.snapshot_id)
            registered_market = state.get_snapshot(market_snapshot_id)
            if (
                registered_derived is None
                or registered_derived.object_sha256 != snapshot.object_sha256
                or not objects.verify(snapshot.object_sha256)
                or registered_market is None
                or registered_market.object_sha256 != market_snapshot_object_hash
                or registered_market.source_id != snapshot.source_id
                or not objects.verify(market_snapshot_object_hash)
            ):
                counts = None
                reason_codes.add("MARKET_NUMERATOR_LINEAGE_UNVERIFIED")
            else:
                source_ids.add(registered_market.snapshot_id)
                numerator_object_hash = registered_market.object_sha256
                available_at = max(available_at, registered_market.available_to_system_at)
                observed_at = max(observed_at, registered_market.fetched_at)

            proof_snapshots: list[SourceSnapshot] = []
            for raw_id, raw_hash in zip(raw_ids, raw_hashes, strict=True):
                if not isinstance(raw_id, str) or not isinstance(raw_hash, str):
                    proof_snapshots = []
                    break
                registered = state.get_snapshot(raw_id)
                if (
                    registered is None
                    or registered.object_sha256 != raw_hash
                    or not objects.verify(raw_hash)
                ):
                    proof_snapshots = []
                    break
                proof_snapshots.append(registered)
            if not proof_snapshots:
                counts = None
                reason_codes.add("COVERAGE_PROOF_LINEAGE_UNVERIFIED")
            else:
                source_ids.update(item.snapshot_id for item in proof_snapshots)
                available_at = max(
                    [available_at, *(item.available_to_system_at for item in proof_snapshots)]
                )
                observed_at = max([observed_at, *(item.fetched_at for item in proof_snapshots)])
                denominator_candidates = [
                    item for item in proof_snapshots if item.source_id == proof_source_id
                ]
                if denominator_candidates:
                    denominator_snapshot = denominator_candidates[-1]
                    denominator_object_hash = denominator_snapshot.object_sha256
                    source_version = denominator_snapshot.snapshot_id
                else:
                    counts = None
                    reason_codes.add("DENOMINATOR_SOURCE_LINEAGE_MISSING")
                authority = _provider_denominator_authority(
                    registry,
                    proof_source_id,
                    str(proof_capability),
                )
                if authority is UniverseDenominatorAuthority.PRIMARY_OFFICIAL:
                    reason_codes.add("PRIMARY_OFFICIAL_DENOMINATOR_RECONCILED")
                else:
                    reason_codes.add("DECORATED_DENOMINATOR_NOT_OFFICIAL")
    elif counts is not None:
        # A total embedded in the same market payload is self-reported. Provider
        # metadata alone cannot turn it into an independently reconciled denominator.
        authority = UniverseDenominatorAuthority.SECONDARY_SELF_REPORTED
        denominator_capability = "market.seed_snapshot"
        denominator_object_hash = snapshot.object_sha256
        reason_codes.add("SECONDARY_DENOMINATOR_SELF_REPORTED")
    else:
        reason_codes.add("DENOMINATOR_UNAVAILABLE")

    if counts is None:
        return MarketCoverageReconciliation(
            created_at=available_at,
            market=market,
            coverage_level=(
                UniverseCoverageLevel.PARTIAL if numerator else UniverseCoverageLevel.UNAVAILABLE
            ),
            denominator_source_id=denominator_source_id,
            denominator_capability=denominator_capability,
            denominator_authority=authority,
            numerator_count=numerator,
            source_version=source_version,
            observed_at=observed_at,
            available_to_system_at=available_at,
            numerator_object_hash=numerator_object_hash,
            source_snapshot_ids=sorted(source_ids),
            reason_codes=sorted(reason_codes),
        )

    numerator, denominator = counts
    ratio = min(1.0, numerator / denominator)
    missing_count = max(denominator - numerator, 0)
    extra_count = max(numerator - denominator, 0)
    if extra_count:
        level = UniverseCoverageLevel.PARTIAL
        reason_codes.add("MARKET_UNIVERSE_EXTRA_ROWS")
    elif ratio >= _FULL_MARKET_COVERAGE_RATIO:
        level = (
            UniverseCoverageLevel.OFFICIAL_DENOMINATOR_RECONCILED
            if authority
            in {
                UniverseDenominatorAuthority.PRIMARY_OFFICIAL,
                UniverseDenominatorAuthority.QUALIFIED_AUTHORIZED_MASTER,
            }
            and denominator_object_hash is not None
            else UniverseCoverageLevel.ENGINEERING_HIGH_COVERAGE
        )
    else:
        level = UniverseCoverageLevel.PARTIAL
        reason_codes.add("MARKET_UNIVERSE_PARTIAL")
    return MarketCoverageReconciliation(
        created_at=available_at,
        market=market,
        coverage_level=level,
        denominator_source_id=denominator_source_id,
        denominator_capability=denominator_capability,
        denominator_authority=authority,
        denominator_count=denominator,
        numerator_count=numerator,
        missing_count=missing_count,
        extra_count=extra_count,
        coverage_ratio=ratio,
        source_version=source_version,
        observed_at=observed_at,
        available_to_system_at=available_at,
        denominator_object_hash=denominator_object_hash,
        numerator_object_hash=numerator_object_hash,
        source_snapshot_ids=sorted(source_ids),
        reason_codes=sorted(reason_codes),
    )


def _universe_coverage_proof(
    *,
    as_of: datetime,
    reconciliations: dict[Market, MarketCoverageReconciliation],
) -> UniverseCoverageProof:
    complete: list[MarketCoverageReconciliation] = []
    for market in _EQUITY_MARKETS:
        item = reconciliations.get(market)
        if item is None:
            item = MarketCoverageReconciliation(
                created_at=as_of,
                market=market,
                coverage_level=UniverseCoverageLevel.UNAVAILABLE,
                numerator_count=0,
                reason_codes=["MARKET_UNIVERSE_UNAVAILABLE"],
            )
        complete.append(item)
    complete.sort(key=lambda item: item.market.value)
    levels = {item.coverage_level for item in complete}
    aggregate = (
        UniverseCoverageLevel.OFFICIAL_DENOMINATOR_RECONCILED
        if levels == {UniverseCoverageLevel.OFFICIAL_DENOMINATOR_RECONCILED}
        else (
            UniverseCoverageLevel.ENGINEERING_HIGH_COVERAGE
            if all(
                item.coverage_level
                in {
                    UniverseCoverageLevel.ENGINEERING_HIGH_COVERAGE,
                    UniverseCoverageLevel.OFFICIAL_DENOMINATOR_RECONCILED,
                }
                for item in complete
            )
            else (
                UniverseCoverageLevel.UNAVAILABLE
                if levels == {UniverseCoverageLevel.UNAVAILABLE}
                else UniverseCoverageLevel.PARTIAL
            )
        )
    )
    reasons = sorted({code for item in complete for code in item.reason_codes})
    proof_id = universe_coverage_proof_id(
        as_of=as_of,
        coverage_level=aggregate,
        market_reconciliations=complete,
        reason_codes=reasons,
    )
    return UniverseCoverageProof(
        created_at=as_of,
        proof_id=proof_id,
        as_of=as_of,
        coverage_level=aggregate,
        market_reconciliations=complete,
        reason_codes=reasons,
    )


@dataclass(frozen=True, slots=True)
class _RawMarketRow:
    company_id: str
    market: Market
    name: str
    price: float
    amount_cny: float
    turnover_rate: float
    float_market_cap_cny: float
    snapshot_id: str


@dataclass(frozen=True, slots=True)
class _MarketRow:
    company_id: str
    market: Market
    name: str
    price: float
    amount_cny: float
    turnover_rate: float
    float_market_cap_cny: float
    market_score: float
    snapshot_id: str


@dataclass(slots=True)
class _SeedAccumulator:
    company_id: str
    market: Market
    name: str
    priority: float = 0.0
    market_score: float | None = None
    price: float | None = None
    amount_cny: float | None = None
    turnover_rate: float | None = None
    float_market_cap_cny: float | None = None
    candidate_version_id: str | None = None
    candidate_strength: str | None = None
    origins: set[ResearchSeedOrigin] = field(default_factory=set)
    authors: set[str] = field(default_factory=set)
    domains: set[str] = field(default_factory=set)
    support_skills: set[str] = field(default_factory=set)
    reasons: set[str] = field(default_factory=set)
    snapshot_ids: set[str] = field(default_factory=set)


@dataclass(frozen=True, slots=True)
class _DomainAliasGroup:
    board_contains: tuple[str, ...]
    skill_terms: tuple[str, ...]


class ResearchSeedService:
    """Build research-only seeds without creating candidates, decisions, or orders."""

    def __init__(
        self,
        *,
        project_root: Path,
        state: StateStore,
        objects: ObjectStore,
        provider: SeedMarketProvider,
    ) -> None:
        self.project_root = project_root
        self.state = state
        self.objects = objects
        self.provider = provider
        self.provider_registry = load_provider_registry(
            project_root / "configs" / "provider_registry.yaml"
        )
        self.candidates = CandidateRepository(state)
        self.visual_skills = VisualSkillRepository(state)
        self.alias_groups = self._load_alias_groups(
            project_root / "configs" / "research_seed_domains.yaml"
        )
        self.author_names = self._load_author_names(
            project_root / "configs" / "knowledge_sources.yaml"
        )

    def generate(self, request: ResearchSeedRequest) -> ResearchSeedReport:
        if request.live and not self._is_current(request.as_of):
            return self._persist(
                self._empty_report(
                    request,
                    warnings=["LIVE_RESEARCH_SEEDS_REQUIRE_CURRENT_AS_OF"],
                )
            )

        warnings: set[str] = set()
        source_snapshots: dict[str, str] = {}
        source_hashes: set[str] = set()
        cutoff = request.as_of
        market_rows: dict[str, _MarketRow] = {}
        activity_proxy_markets: set[Market] = set()
        accumulators: dict[str, _SeedAccumulator] = {}

        def fetch_market(
            market: Market,
        ) -> tuple[
            Market,
            dict[str, object],
            SourceSnapshot,
            list[_MarketRow],
            bool,
            MarketCoverageReconciliation,
        ]:
            payload, raw_snapshot = self.provider.fetch_seed_snapshot(market, live=request.live)
            snapshot = cast(SourceSnapshot, raw_snapshot)
            raw = self._parse_market_rows(payload, market, snapshot.snapshot_id)
            sina_activity_proxy = request.live and _sina_activity_unavailable(payload)
            scored = self._score_market_rows(
                raw,
                minimum_amount=0.0 if sina_activity_proxy else request.minimum_amount_cny,
                minimum_float_cap=request.minimum_float_market_cap_cny,
            )
            reconciliation = _market_coverage_reconciliation(
                payload,
                snapshot,
                market,
                state=self.state,
                objects=self.objects,
                registry=self.provider_registry,
            )
            return market, payload, snapshot, scored, sina_activity_proxy, reconciliation

        workers = min(request.market_fetch_workers, len(_EQUITY_MARKETS))
        market_coverage_ratios: dict[Market, float] = {}
        market_reconciliations: dict[Market, MarketCoverageReconciliation] = {}
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {executor.submit(fetch_market, market): market for market in _EQUITY_MARKETS}
            for future in as_completed(futures):
                market = futures[future]
                try:
                    _, payload, snapshot, scored, activity_proxy, reconciliation = future.result()
                    market_reconciliations[market] = reconciliation
                    cutoff = max(
                        cutoff,
                        reconciliation.available_to_system_at or snapshot.available_to_system_at,
                    )
                    for source_snapshot_id in reconciliation.source_snapshot_ids:
                        registered = self.state.get_snapshot(source_snapshot_id)
                        if registered is None or not self.objects.verify(registered.object_sha256):
                            continue
                        source_snapshots[source_snapshot_id] = registered.object_sha256
                        source_hashes.add(registered.object_sha256)
                    market_rows.update({item.company_id: item for item in scored})
                    coverage_ratio = reconciliation.coverage_ratio
                    if coverage_ratio is not None:
                        market_coverage_ratios[market] = coverage_ratio
                    if (
                        reconciliation.coverage_level
                        is UniverseCoverageLevel.ENGINEERING_HIGH_COVERAGE
                    ):
                        warnings.add(f"MARKET_SEED_ENGINEERING_COVERAGE_ONLY:{market.value}")
                    elif reconciliation.coverage_level is UniverseCoverageLevel.PARTIAL:
                        suffix = f":{coverage_ratio:.6f}" if coverage_ratio is not None else ""
                        warnings.add(f"MARKET_SEED_UNIVERSE_PARTIAL:{market.value}{suffix}")
                    elif reconciliation.coverage_level is UniverseCoverageLevel.UNAVAILABLE:
                        warnings.add(f"MARKET_SEED_COVERAGE_UNPROVEN:{market.value}")
                    if activity_proxy:
                        activity_proxy_markets.add(market)
                        warnings.add(f"SINA_ACTIVITY_PROXY_USED:{market.value}")
                except (AStockError, OSError, RuntimeError, ValueError):
                    warnings.add(f"MARKET_SEED_SNAPSHOT_UNAVAILABLE:{market.value}")

        universe_coverage_proof = _universe_coverage_proof(
            as_of=cutoff,
            reconciliations=market_reconciliations,
        )
        if (
            universe_coverage_proof.coverage_level
            is UniverseCoverageLevel.ENGINEERING_HIGH_COVERAGE
        ):
            warnings.add("FORMAL_UNIVERSE_DENOMINATOR_NOT_RECONCILED")

        for row in sorted(
            market_rows.values(),
            key=lambda item: (-item.market_score, item.company_id),
        )[: request.max_market_seeds]:
            accumulator = self._accumulator(accumulators, row)
            accumulator.origins.add(ResearchSeedOrigin.MARKET)
            accumulator.priority = max(accumulator.priority, 0.60 + 0.25 * row.market_score)
            accumulator.reasons.add(
                "SINA_ACTIVITY_PROXY_RESEARCH_SEED"
                if row.market in activity_proxy_markets
                else "LIQUID_SCALE_RESEARCH_SEED"
            )
            accumulator.snapshot_ids.add(row.snapshot_id)

        if request.include_existing_candidates:
            for row in self.candidates.research_ready_records(
                as_of=cutoff,
                limit=request.max_total_seeds,
            ):
                company_id = str(row["company_id"])
                market_row = market_rows.get(company_id)
                market = (
                    market_row.market
                    if market_row is not None
                    else self._market_from_instrument_id(str(row["instrument_id"]))
                )
                name = market_row.name if market_row is not None else company_id
                accumulator = accumulators.setdefault(
                    company_id,
                    _SeedAccumulator(company_id=company_id, market=market, name=name),
                )
                if market_row is not None:
                    self._apply_market(accumulator, market_row)
                strength = str(row["strength"])
                accumulator.origins.add(ResearchSeedOrigin.EXISTING_CANDIDATE)
                accumulator.candidate_version_id = str(row["candidate_version_id"])
                accumulator.candidate_strength = strength
                accumulator.priority = max(
                    accumulator.priority,
                    0.99 if strength == "STRONG" else 0.94,
                )
                accumulator.reasons.add("EXISTING_RESEARCH_READY_CANDIDATE")
                record_hash = str(row["record_object_hash"])
                if self.objects.verify(record_hash):
                    source_hashes.add(record_hash)

        release = self.visual_skills.latest_release_any()
        profiles: list[ExpertDomainProfile] = []
        if release is None:
            warnings.add("EXPERT_SKILL_REGISTRY_UNAVAILABLE")
        else:
            release_hash = str(release["release_object_hash"])
            release_id = str(release["release_id"])
            if not self.objects.verify(release_hash):
                warnings.add("EXPERT_SKILL_REGISTRY_OBJECT_UNAVAILABLE")
                release = None
            else:
                source_hashes.add(release_hash)
                try:
                    board_payload, board_snapshot = self.provider.fetch_industry_boards(
                        live=request.live
                    )
                    cutoff = max(cutoff, board_snapshot.available_to_system_at)
                    source_snapshots[board_snapshot.snapshot_id] = board_snapshot.object_sha256
                    source_hashes.add(board_snapshot.object_sha256)
                    boards = self._parse_boards(board_payload)
                    profiles = self._expert_profiles(
                        release_id=release_id,
                        rows=self.visual_skills.overlay_skill_rows(release_id),
                        boards=boards,
                        request=request,
                    )
                    self._apply_expert_seeds(
                        profiles=profiles,
                        market_rows=market_rows,
                        accumulators=accumulators,
                        request=request,
                        source_snapshots=source_snapshots,
                        source_hashes=source_hashes,
                    )
                except (AStockError, OSError, RuntimeError, ValueError):
                    warnings.add("EXPERT_DOMAIN_MARKET_TAXONOMY_UNAVAILABLE")

        all_seeds = [self._finalize_seed(item, request.as_of) for item in accumulators.values()]
        all_seeds.sort(key=lambda item: (-item.research_priority_score, item.company_id))
        blind = [item for item in all_seeds if ResearchSeedOrigin.MARKET in item.origins][
            : min(request.max_market_seeds, request.max_total_seeds)
        ]
        selected_ids = {item.seed_id for item in blind}
        fill = [item for item in all_seeds if item.seed_id not in selected_ids]
        seeds = [*blind, *fill[: max(0, request.max_total_seeds - len(blind))]]
        seeds.sort(key=lambda item: (-item.research_priority_score, item.company_id))
        coverage_level = universe_coverage_proof.coverage_level
        engineering_full_universe = coverage_level in {
            UniverseCoverageLevel.ENGINEERING_HIGH_COVERAGE,
            UniverseCoverageLevel.OFFICIAL_DENOMINATOR_RECONCILED,
        }
        formal_full_universe = universe_coverage_proof.formal_full_market_coverage_allowed
        universe_coverage_status = (
            ResearchUniverseCoverageStatus.FULL
            if engineering_full_universe
            else (
                ResearchUniverseCoverageStatus.PARTIAL
                if coverage_level is UniverseCoverageLevel.PARTIAL
                else ResearchUniverseCoverageStatus.UNAVAILABLE
            )
        )
        status = (
            ResearchSeedStatus.READY
            if seeds
            else (
                ResearchSeedStatus.EMPTY
                if engineering_full_universe
                else ResearchSeedStatus.NEEDS_INFO
            )
        )
        if universe_coverage_status is ResearchUniverseCoverageStatus.UNAVAILABLE:
            warnings.add("CURRENT_MARKET_SEED_UNIVERSE_UNAVAILABLE")
        elif not market_rows and engineering_full_universe:
            warnings.add("CURRENT_MARKET_SCAN_ZERO_ELIGIBLE_CANDIDATES")
        elif not market_rows:
            warnings.add("CURRENT_MARKET_PARTIAL_UNIVERSE_NO_ELIGIBLE_OBSERVATIONS")
        if release is not None and not profiles:
            warnings.add("NO_EXPERT_DOMAIN_PASSED_SKILL_COUNT_GATE")

        report_id = "research-seeds:" + content_hash(
            {
                "request": request.model_dump(mode="json", exclude={"created_at"}),
                "data_cutoff_at": cutoff.isoformat(),
                "registry_release_id": str(release["release_id"]) if release else None,
                "source_hashes": sorted(source_hashes),
                "market_coverage_ratios": {
                    market.value: market_coverage_ratios[market]
                    for market in sorted(market_coverage_ratios, key=lambda item: item.value)
                },
                "universe_coverage_proof_id": universe_coverage_proof.proof_id,
                "universe_coverage_level": coverage_level.value,
                "universe_coverage_status": universe_coverage_status.value,
                "seed_ids": [item.seed_id for item in seeds],
                "profiles": [item.model_dump(mode="json") for item in profiles],
            }
        )
        report = ResearchSeedReport(
            report_id=report_id,
            as_of=request.as_of,
            data_cutoff_at=cutoff,
            status=status,
            registry_release_id=str(release["release_id"]) if release else None,
            registry_release_object_hash=(str(release["release_object_hash"]) if release else None),
            profiles=profiles,
            seeds=seeds,
            source_snapshot_ids=sorted(source_snapshots),
            source_object_hashes=sorted(source_hashes),
            warning_codes=sorted(warnings),
            market_coverage_ratios=market_coverage_ratios,
            universe_coverage_level=coverage_level,
            universe_coverage_proof=universe_coverage_proof,
            universe_coverage_status=universe_coverage_status,
            formal_full_market_coverage_allowed=formal_full_universe,
            market_seed_count=sum(ResearchSeedOrigin.MARKET in item.origins for item in seeds),
            expert_seed_count=sum(
                ResearchSeedOrigin.EXPERT_SKILL in item.origins for item in seeds
            ),
            existing_candidate_seed_count=sum(
                ResearchSeedOrigin.EXISTING_CANDIDATE in item.origins for item in seeds
            ),
            created_at=request.as_of,
        )
        return self._persist(report)

    def status(self) -> dict[str, object]:
        checkpoint = self.state.get_checkpoint("research-seeds", "latest")
        if checkpoint is None:
            return {"status": "NOT_RUN"}
        return {
            "status": checkpoint["status"],
            "artifact_id": checkpoint["cursor"].get("artifact_id"),
            "report_id": checkpoint["cursor"].get("report_id"),
            "object_hash": checkpoint.get("object_hash"),
        }

    def audit(self, artifact_id: str) -> dict[str, object]:
        findings: set[str] = set()
        record = self.state.artifact_record(artifact_id)
        if record is None or str(record["type"]) != "ResearchSeedReport":
            return {
                "status": "FAIL",
                "artifact_id": artifact_id,
                "finding_codes": ["UNKNOWN_RESEARCH_SEED_REPORT"],
            }
        object_hash = str(record["object_hash"])
        if not self.objects.verify(object_hash):
            findings.add("RESEARCH_SEED_REPORT_OBJECT_UNAVAILABLE")
            report = None
        else:
            report = ResearchSeedReport.model_validate_json(self.objects.get_bytes(object_hash))
        if report is not None:
            proof = report.universe_coverage_proof
            if proof is not None:
                expected_proof_id = universe_coverage_proof_id(
                    as_of=proof.as_of,
                    coverage_level=proof.coverage_level,
                    market_reconciliations=proof.market_reconciliations,
                    reason_codes=proof.reason_codes,
                    policy_version=proof.policy_version,
                    engineering_min_ratio=proof.engineering_min_ratio,
                )
                if proof.proof_id != expected_proof_id:
                    findings.add("UNIVERSE_COVERAGE_PROOF_ID_DRIFT")
                for reconciliation in proof.market_reconciliations:
                    linked_hashes: set[str] = set()
                    for proof_snapshot_id in reconciliation.source_snapshot_ids:
                        proof_snapshot = self.state.get_snapshot(proof_snapshot_id)
                        if proof_snapshot is not None:
                            linked_hashes.add(proof_snapshot.object_sha256)
                    for proof_hash in (
                        reconciliation.denominator_object_hash,
                        reconciliation.numerator_object_hash,
                    ):
                        if proof_hash is not None and proof_hash not in linked_hashes:
                            findings.add("UNIVERSE_COVERAGE_PROOF_HASH_LINEAGE_DRIFT")
            for snapshot_id in report.source_snapshot_ids:
                snapshot = self.state.get_snapshot(snapshot_id)
                if snapshot is None:
                    findings.add("RESEARCH_SEED_SOURCE_SNAPSHOT_MISSING")
                    continue
                if (
                    snapshot.object_sha256 not in report.source_object_hashes
                    or not self.objects.verify(snapshot.object_sha256)
                ):
                    findings.add("RESEARCH_SEED_SOURCE_SNAPSHOT_DRIFT")
            if report.registry_release_id is not None:
                release = self.visual_skills.release(report.registry_release_id)
                if (
                    release is None
                    or str(release["release_object_hash"]) != report.registry_release_object_hash
                ):
                    findings.add("RESEARCH_SEED_REGISTRY_RELEASE_DRIFT")
            profile_authors = {item.author_source_id for item in report.profiles}
            profile_domains = {
                domain.board_name for profile in report.profiles for domain in profile.domains
            }
            profile_skill_ids = {
                skill_id
                for profile in report.profiles
                for domain in profile.domains
                for skill_id in domain.support_skill_ids
            }
            report_snapshot_ids = set(report.source_snapshot_ids)
            for seed in report.seeds:
                if not set(seed.source_snapshot_ids).issubset(report_snapshot_ids):
                    findings.add("RESEARCH_SEED_SNAPSHOT_LINEAGE_DRIFT")
                if ResearchSeedOrigin.EXPERT_SKILL in seed.origins:
                    if not set(seed.expert_author_source_ids).issubset(profile_authors):
                        findings.add("RESEARCH_SEED_EXPERT_AUTHOR_DRIFT")
                    if not set(seed.expert_domain_names).issubset(profile_domains):
                        findings.add("RESEARCH_SEED_EXPERT_DOMAIN_DRIFT")
                    if not set(seed.expert_domain_support_skill_ids).issubset(profile_skill_ids):
                        findings.add("RESEARCH_SEED_EXPERT_SKILL_DRIFT")
                if ResearchSeedOrigin.EXISTING_CANDIDATE in seed.origins:
                    if seed.candidate_version_id is None:
                        findings.add("RESEARCH_SEED_CANDIDATE_LINEAGE_MISSING")
                    else:
                        candidate = self.candidates.get_candidate_version(seed.candidate_version_id)
                        if (
                            candidate is None
                            or str(candidate["company_id"]) != seed.company_id
                            or str(candidate["lifecycle_status"]) != "RESEARCH_READY"
                            or str(candidate["record_object_hash"])
                            not in report.source_object_hashes
                        ):
                            findings.add("RESEARCH_SEED_CANDIDATE_LINEAGE_DRIFT")
            if report.recommendation_allowed or report.paper_ledger_write_allowed:
                findings.add("RESEARCH_SEED_AUTHORITY_DRIFT")
        for input_hash in record["input_hashes"]:
            if not self.objects.verify(str(input_hash)):
                findings.add("RESEARCH_SEED_INPUT_OBJECT_UNAVAILABLE")
        return {
            "status": "PASS" if not findings else "FAIL",
            "artifact_id": artifact_id,
            "object_hash": object_hash,
            "finding_codes": sorted(findings),
            "recommendation_allowed": False,
            "paper_ledger_write_allowed": False,
            "broker_execution_allowed": False,
        }

    def _apply_expert_seeds(
        self,
        *,
        profiles: list[ExpertDomainProfile],
        market_rows: dict[str, _MarketRow],
        accumulators: dict[str, _SeedAccumulator],
        request: ResearchSeedRequest,
        source_snapshots: dict[str, str],
        source_hashes: set[str],
    ) -> None:
        constituent_cache: dict[str, tuple[list[dict[str, object]], str]] = {}
        for profile in profiles:
            candidates: dict[str, tuple[float, ExpertDomainEvidence, str]] = {}
            max_count = max((item.matched_skill_count for item in profile.domains), default=1)
            for domain in profile.domains:
                if domain.board_code not in constituent_cache:
                    payload, snapshot = self.provider.fetch_industry_constituents(
                        domain.board_code,
                        live=request.live,
                    )
                    source_snapshots[snapshot.snapshot_id] = snapshot.object_sha256
                    source_hashes.add(snapshot.object_sha256)
                    constituent_cache[domain.board_code] = (
                        self._constituent_rows(payload, domain.board_code),
                        snapshot.snapshot_id,
                    )
                rows, snapshot_id = constituent_cache[domain.board_code]
                domain_strength = min(1.0, domain.matched_skill_count / max_count)
                for raw in rows:
                    company_id = str(raw.get("f12") or "")
                    market_row = market_rows.get(company_id)
                    if market_row is None:
                        continue
                    if (
                        market_row.amount_cny < request.minimum_amount_cny
                        or market_row.float_market_cap_cny < request.minimum_float_market_cap_cny
                    ):
                        continue
                    blind_score = 0.60 + 0.25 * market_row.market_score
                    score = min(
                        1.0,
                        blind_score + request.expert_overlay_max_priority_bonus * domain_strength,
                    )
                    previous = candidates.get(company_id)
                    if previous is None or score > previous[0]:
                        candidates[company_id] = (score, domain, snapshot_id)
            ranked = sorted(candidates.items(), key=lambda item: (-item[1][0], item[0]))[
                : request.max_expert_seeds_per_author
            ]
            for company_id, (score, domain, snapshot_id) in ranked:
                market_row = market_rows[company_id]
                accumulator = self._accumulator(accumulators, market_row)
                accumulator.origins.add(ResearchSeedOrigin.EXPERT_SKILL)
                accumulator.priority = max(accumulator.priority, score)
                accumulator.authors.add(profile.author_source_id)
                accumulator.domains.add(domain.board_name)
                accumulator.support_skills.update(domain.support_skill_ids)
                accumulator.reasons.add("EXPERT_SKILL_DOMAIN_RESEARCH_SEED")
                accumulator.snapshot_ids.update({market_row.snapshot_id, snapshot_id})

    def _expert_profiles(
        self,
        *,
        release_id: str,
        rows: list[dict[str, Any]],
        boards: list[tuple[str, str]],
        request: ResearchSeedRequest,
    ) -> list[ExpertDomainProfile]:
        skills_by_author: dict[str, list[tuple[str, str]]] = defaultdict(list)
        for row in rows:
            skill = json.loads(str(row["skill_json"]))
            author = str(skill["author_source_id"])
            text = self._skill_text(skill)
            skills_by_author[author].append((str(skill["final_skill_id"]), text))
        profiles: list[ExpertDomainProfile] = []
        for author, skills in sorted(skills_by_author.items()):
            domains: list[ExpertDomainEvidence] = []
            for board_code, board_name in boards:
                terms = self._terms_for_board(board_name)
                support = sorted(
                    skill_id
                    for skill_id, text in skills
                    if any(term and term in text for term in terms)
                )
                share = len(support) / len(skills)
                if len(support) < request.minimum_domain_skill_count:
                    continue
                domains.append(
                    ExpertDomainEvidence(
                        board_code=board_code,
                        board_name=board_name,
                        matched_skill_count=len(support),
                        author_skill_count=len(skills),
                        skill_share=share,
                        support_skill_ids=support[:30],
                        created_at=request.as_of,
                    )
                )
            domains.sort(
                key=lambda item: (-item.matched_skill_count, -item.skill_share, item.board_name)
            )
            deduplicated: list[ExpertDomainEvidence] = []
            support_signatures: set[tuple[str, ...]] = set()
            for domain in domains:
                signature = tuple(domain.support_skill_ids)
                if signature in support_signatures:
                    continue
                support_signatures.add(signature)
                deduplicated.append(domain)
                if len(deduplicated) >= request.max_domains_per_author:
                    break
            domains = deduplicated
            confidence = min(1.0, sum(item.skill_share for item in domains)) if domains else 0.0
            profiles.append(
                ExpertDomainProfile(
                    author_source_id=author,
                    display_name=self.author_names.get(author, author),
                    registry_release_id=release_id,
                    total_admitted_skill_count=len(skills),
                    domains=domains,
                    profile_confidence=confidence,
                    created_at=request.as_of,
                )
            )
        return profiles

    def _parse_market_rows(
        self,
        payload: dict[str, object],
        market: Market,
        snapshot_id: str,
    ) -> list[_RawMarketRow]:
        request = payload.get("_astock_request")
        if not isinstance(request, dict) or request.get("market") != market.value:
            raise ValueError("research-seed market snapshot provenance mismatch")
        rows = self._payload_rows(payload)
        result: list[_RawMarketRow] = []
        sina_market_center = payload.get("_astock_source") == "SINA_MARKET_CENTER"
        for row in rows:
            if sina_market_center:
                company_id = str(row.get("code") or "")
                name = str(row.get("name") or "").strip()
                trade_price = self._number(row.get("trade"))
                settlement_price = self._number(row.get("settlement"))
                price = trade_price if trade_price > 0 else settlement_price
                amount = self._number(row.get("amount"))
                turnover = self._number(row.get("turnoverratio"))
                raw_float_cap = self._number(row.get("nmc"))
                float_cap = raw_float_cap * 10_000 if raw_float_cap >= 0 else -1.0
            else:
                company_id = str(row.get("f12") or "")
                name = str(row.get("f14") or "").strip()
                price = self._number(row.get("f2"))
                amount = self._number(row.get("f6"))
                turnover = self._number(row.get("f8"))
                float_cap = self._number(row.get("f21"))
            if len(company_id) != 6 or not company_id.isdigit() or not name:
                continue
            if self._excluded_name(name):
                continue
            if price <= 0 or amount < 0 or turnover < 0 or float_cap < 0:
                continue
            result.append(
                _RawMarketRow(
                    company_id=company_id,
                    market=market,
                    name=name,
                    price=price,
                    amount_cny=amount,
                    turnover_rate=turnover,
                    float_market_cap_cny=float_cap,
                    snapshot_id=snapshot_id,
                )
            )
        return result

    @staticmethod
    def _score_market_rows(
        rows: list[_RawMarketRow],
        *,
        minimum_amount: float,
        minimum_float_cap: float,
    ) -> list[_MarketRow]:
        eligible = [
            row
            for row in rows
            if row.amount_cny >= minimum_amount and row.float_market_cap_cny >= minimum_float_cap
        ]
        if not eligible:
            return []
        amount_values = [log1p(row.amount_cny) for row in eligible]
        cap_values = [log1p(row.float_market_cap_cny) for row in eligible]
        turnover_values = [min(row.turnover_rate, 20.0) for row in eligible]
        amount_rank = ResearchSeedService._percentile_ranks(amount_values)
        cap_rank = ResearchSeedService._percentile_ranks(cap_values)
        turnover_rank = ResearchSeedService._percentile_ranks(turnover_values)
        result: list[_MarketRow] = []
        for index, row in enumerate(eligible):
            score = 0.50 * amount_rank[index] + 0.35 * cap_rank[index] + 0.15 * turnover_rank[index]
            result.append(
                _MarketRow(
                    company_id=row.company_id,
                    market=row.market,
                    name=row.name,
                    price=row.price,
                    amount_cny=row.amount_cny,
                    turnover_rate=row.turnover_rate,
                    float_market_cap_cny=row.float_market_cap_cny,
                    market_score=min(1.0, max(0.0, score)),
                    snapshot_id=row.snapshot_id,
                )
            )
        return result

    @staticmethod
    def _percentile_ranks(values: list[float]) -> list[float]:
        if len(values) == 1:
            return [1.0]
        ordered = sorted((value, index) for index, value in enumerate(values))
        ranks = [0.0] * len(values)
        position = 0
        while position < len(ordered):
            end = position + 1
            while end < len(ordered) and ordered[end][0] == ordered[position][0]:
                end += 1
            average_rank = (position + end - 1) / 2
            percentile = average_rank / (len(values) - 1)
            for _, index in ordered[position:end]:
                ranks[index] = percentile
            position = end
        return ranks

    @staticmethod
    def _parse_boards(payload: dict[str, object]) -> list[tuple[str, str]]:
        request = payload.get("_astock_request")
        if not isinstance(request, dict) or request.get("purpose") != "EXPERT_DOMAIN_TAXONOMY":
            raise ValueError("expert-domain taxonomy provenance mismatch")
        boards: list[tuple[str, str]] = []
        for row in ResearchSeedService._payload_rows(payload):
            code = str(row.get("f12") or "")
            name = str(row.get("f14") or "").strip()
            if code.startswith("BK") and code[2:].isdigit() and len(name) >= 2:
                boards.append((code, name))
        if not boards:
            raise ValueError("industry-board taxonomy is empty")
        return sorted(set(boards), key=lambda item: item[0])

    @staticmethod
    def _constituent_rows(
        payload: dict[str, object],
        expected_board_code: str,
    ) -> list[dict[str, object]]:
        request = payload.get("_astock_request")
        if (
            not isinstance(request, dict)
            or request.get("purpose") != "EXPERT_DOMAIN_CONSTITUENTS"
            or request.get("board_code") != expected_board_code
        ):
            raise ValueError("expert-domain constituent provenance mismatch")
        return ResearchSeedService._payload_rows(payload)

    @staticmethod
    def _payload_rows(payload: dict[str, object]) -> list[dict[str, object]]:
        if payload.get("_astock_source") == "SINA_MARKET_CENTER":
            rows = payload.get("rows")
            if not isinstance(rows, list) or any(not isinstance(item, dict) for item in rows):
                raise ValueError("Sina seed payload contains malformed rows")
            return rows
        if payload.get("rc") != 0:
            raise ValueError("EastMoney seed request failed")
        data = payload.get("data")
        if not isinstance(data, dict):
            raise ValueError("EastMoney seed payload lacks data")
        diff = data.get("diff")
        if isinstance(diff, dict):
            values = list(diff.values())
        elif isinstance(diff, list):
            values = diff
        else:
            raise ValueError("EastMoney seed payload lacks diff rows")
        if any(not isinstance(item, dict) for item in values):
            raise ValueError("EastMoney seed payload contains malformed rows")
        return values

    def _terms_for_board(self, board_name: str) -> set[str]:
        normalized = self._normalize(board_name)
        terms = {normalized}
        for group in self.alias_groups:
            if any(self._normalize(token) in normalized for token in group.board_contains):
                terms.update(self._normalize(token) for token in group.skill_terms)
        return {item for item in terms if len(item) >= 2}

    @staticmethod
    def _skill_text(skill: dict[str, object]) -> str:
        values: list[str] = []
        for key in (
            "skill_name",
            "decision_question",
            "core_principle",
            "applicable_conditions",
            "required_evidence",
            "positive_signals",
            "negative_signals",
        ):
            value = skill.get(key)
            if isinstance(value, str):
                values.append(value)
            elif isinstance(value, list):
                values.extend(str(item) for item in value)
        return ResearchSeedService._normalize(" ".join(values))

    @staticmethod
    def _normalize(value: str) -> str:
        return "".join(value.casefold().split()).replace("-", "").replace("_", "")

    @staticmethod
    def _number(value: object) -> float:
        if value is None:
            return -1.0
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            normalized = value.strip()
            if normalized in {"", "-", "--"}:
                return -1.0
            try:
                return float(normalized)
            except ValueError:
                return -1.0
        return -1.0

    @staticmethod
    def _excluded_name(name: str) -> bool:
        normalized = name.upper().replace(" ", "")
        return "ST" in normalized or "退" in normalized

    @staticmethod
    def _market_from_instrument_id(instrument_id: str) -> Market:
        prefix = instrument_id.split(":", 1)[0]
        return Market(prefix)

    @staticmethod
    def _apply_market(accumulator: _SeedAccumulator, row: _MarketRow) -> None:
        accumulator.name = row.name
        accumulator.market = row.market
        accumulator.market_score = row.market_score
        accumulator.price = row.price
        accumulator.amount_cny = row.amount_cny
        accumulator.turnover_rate = row.turnover_rate
        accumulator.float_market_cap_cny = row.float_market_cap_cny
        accumulator.snapshot_ids.add(row.snapshot_id)

    @classmethod
    def _accumulator(
        cls,
        accumulators: dict[str, _SeedAccumulator],
        row: _MarketRow,
    ) -> _SeedAccumulator:
        accumulator = accumulators.setdefault(
            row.company_id,
            _SeedAccumulator(company_id=row.company_id, market=row.market, name=row.name),
        )
        cls._apply_market(accumulator, row)
        return accumulator

    @staticmethod
    def _finalize_seed(
        accumulator: _SeedAccumulator,
        created_at: datetime,
    ) -> ResearchSeed:
        boost = 0.04 * max(0, len(accumulator.origins) - 1) + 0.02 * max(
            0, len(accumulator.authors) - 1
        )
        priority = min(1.0, accumulator.priority + boost)
        seed_id = "research-seed:" + content_hash(
            {
                "company_id": accumulator.company_id,
                "origins": sorted(item.value for item in accumulator.origins),
                "candidate_version_id": accumulator.candidate_version_id,
                "authors": sorted(accumulator.authors),
                "domains": sorted(accumulator.domains),
                "support_skills": sorted(accumulator.support_skills),
                "snapshots": sorted(accumulator.snapshot_ids),
            }
        )
        return ResearchSeed(
            seed_id=seed_id,
            company_id=accumulator.company_id,
            market=accumulator.market,
            name=accumulator.name,
            origins=sorted(accumulator.origins, key=lambda item: item.value),
            research_priority_score=priority,
            market_liquidity_score=accumulator.market_score,
            current_price=accumulator.price,
            amount_cny=accumulator.amount_cny,
            turnover_rate=accumulator.turnover_rate,
            float_market_cap_cny=accumulator.float_market_cap_cny,
            candidate_version_id=accumulator.candidate_version_id,
            candidate_strength=accumulator.candidate_strength,
            expert_author_source_ids=sorted(accumulator.authors),
            expert_domain_names=sorted(accumulator.domains),
            expert_domain_support_skill_ids=sorted(accumulator.support_skills),
            reason_codes=sorted(accumulator.reasons),
            source_snapshot_ids=sorted(accumulator.snapshot_ids),
            created_at=created_at,
        )

    def _persist(self, report: ResearchSeedReport) -> ResearchSeedReport:
        ref = self.objects.put_json(report.model_dump(mode="json"))
        artifact_id = f"ResearchSeedReport:{report.report_id}"
        inputs = sorted(set(report.source_object_hashes))
        existing = self.state.artifact_record(artifact_id)
        if existing is not None:
            if (
                str(existing["type"]) != "ResearchSeedReport"
                or str(existing["object_hash"]) != ref.sha256
                or sorted(existing["input_hashes"]) != inputs
            ):
                raise ValueError("research-seed report identity collision")
        else:
            self.state.register_artifact(
                artifact_id=artifact_id,
                artifact_type="ResearchSeedReport",
                schema_version=report.schema_version,
                object_hash=ref.sha256,
                input_hashes=inputs,
            )
        self.state.set_checkpoint(
            scope_type="research-seeds",
            scope_key="latest",
            cursor={"artifact_id": artifact_id, "report_id": report.report_id},
            status=report.status.value,
            object_hash=ref.sha256,
        )
        return report

    @staticmethod
    def _empty_report(
        request: ResearchSeedRequest,
        *,
        warnings: list[str],
    ) -> ResearchSeedReport:
        return ResearchSeedReport(
            report_id="research-seeds:"
            + content_hash(
                {
                    "request": request.model_dump(mode="json", exclude={"created_at"}),
                    "warnings": sorted(set(warnings)),
                }
            ),
            as_of=request.as_of,
            data_cutoff_at=request.as_of,
            status=ResearchSeedStatus.NEEDS_INFO,
            profiles=[],
            seeds=[],
            source_snapshot_ids=[],
            source_object_hashes=[],
            warning_codes=sorted(set(warnings)),
            market_seed_count=0,
            expert_seed_count=0,
            existing_candidate_seed_count=0,
            created_at=request.as_of,
        )

    @staticmethod
    def _is_current(as_of: datetime) -> bool:
        return abs(datetime.now(UTC) - as_of.astimezone(UTC)) <= _CURRENT_LIVE_TOLERANCE

    @staticmethod
    def _load_alias_groups(path: Path) -> tuple[_DomainAliasGroup, ...]:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or payload.get("schema_version") != (
            "research-seed-domain-aliases-v1"
        ):
            raise ValueError("research-seed domain alias configuration is invalid")
        groups = payload.get("alias_groups")
        if not isinstance(groups, list):
            raise ValueError("research-seed domain aliases are missing")
        result: list[_DomainAliasGroup] = []
        for item in groups:
            if not isinstance(item, dict):
                raise ValueError("research-seed alias group is invalid")
            board_contains = item.get("board_contains")
            skill_terms = item.get("skill_terms")
            if (
                not isinstance(board_contains, list)
                or not isinstance(skill_terms, list)
                or not board_contains
                or not skill_terms
            ):
                raise ValueError("research-seed alias group is incomplete")
            result.append(
                _DomainAliasGroup(
                    board_contains=tuple(str(value) for value in board_contains),
                    skill_terms=tuple(str(value) for value in skill_terms),
                )
            )
        return tuple(result)

    @staticmethod
    def _load_author_names(path: Path) -> dict[str, str]:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or not isinstance(payload.get("sources"), list):
            return {}
        return {
            str(item["source_id"]): str(item.get("display_name") or item["source_id"])
            for item in payload["sources"]
            if isinstance(item, dict) and item.get("source_id")
        }


__all__ = ["ResearchSeedService", "SeedMarketProvider"]
