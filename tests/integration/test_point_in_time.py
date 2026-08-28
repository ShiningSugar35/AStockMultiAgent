from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from astock.core.object_store import ObjectStore
from astock.pit import PointInTimeRepository, PointInTimeService
from astock.schemas import AvailabilityBasis, PointInTimeMetadata, PointInTimeStatus


def _service(tmp_path: Path, state) -> PointInTimeService:
    return PointInTimeService(
        PointInTimeRepository(state),
        state,
        ObjectStore(tmp_path / "objects"),
    )


def _create(
    service: PointInTimeService,
    source_id: str,
    available: datetime,
    *,
    supersedes: str | None = None,
    status: PointInTimeStatus = PointInTimeStatus.CERTIFIED,
    basis: AvailabilityBasis = AvailabilityBasis.OFFICIAL_PUBLICATION_TIMESTAMP,
):
    return service.create(
        source_id=source_id,
        period_end=date(2025, 12, 31),
        published_at=available,
        effective_at=available,
        ingested_at=available,
        available_to_system_at=available,
        revised_at=available if supersedes else None,
        supersedes_source_id=supersedes,
        point_in_time_status=status,
        availability_basis=basis,
    )


def test_pit_timeline_validation_rejects_impossible_availability() -> None:
    with pytest.raises(ValidationError, match="ingested_at"):
        PointInTimeMetadata(
            pit_id="pit:fixture",
            source_id="fixture",
            published_at=datetime(2026, 1, 1, tzinfo=UTC),
            ingested_at=datetime(2026, 1, 1, tzinfo=UTC),
            available_to_system_at=datetime(2026, 1, 2, tzinfo=UTC),
            point_in_time_status=PointInTimeStatus.CERTIFIED,
            availability_basis=AvailabilityBasis.OFFICIAL_PUBLICATION_TIMESTAMP,
        )


def test_certified_fetch_observed_uses_snapshot_lineage_without_fake_publication_time() -> None:
    observed = datetime(2026, 1, 10, tzinfo=UTC)
    metadata = PointInTimeMetadata(
        pit_id="pit:fetch-observed",
        source_id="official-index-snapshot",
        source_snapshot_id="snapshot:official-index",
        ingested_at=observed,
        available_to_system_at=observed,
        point_in_time_status=PointInTimeStatus.CERTIFIED,
        availability_basis=AvailabilityBasis.FETCH_OBSERVED,
    )
    assert metadata.published_at is None

    with pytest.raises(ValidationError, match="source_snapshot_id"):
        PointInTimeMetadata(
            pit_id="pit:fetch-missing-snapshot",
            source_id="official-index-snapshot",
            ingested_at=observed,
            available_to_system_at=observed,
            point_in_time_status=PointInTimeStatus.CERTIFIED,
            availability_basis=AvailabilityBasis.FETCH_OBSERVED,
        )
    with pytest.raises(ValidationError, match="published_at"):
        PointInTimeMetadata(
            pit_id="pit:publication-missing-time",
            source_id="official-filing",
            ingested_at=observed,
            available_to_system_at=observed,
            point_in_time_status=PointInTimeStatus.CERTIFIED,
            availability_basis=AvailabilityBasis.OFFICIAL_PUBLICATION_TIMESTAMP,
        )


def test_revision_chain_is_append_only_and_point_in_time_safe(tmp_path: Path, state) -> None:
    service = _service(tmp_path, state)
    first_time = datetime(2026, 1, 10, tzinfo=UTC)
    revision_time = datetime(2026, 2, 1, tzinfo=UTC)
    first = _create(service, "filing:v1", first_time)
    repeated = _create(service, "filing:v1", first_time)
    assert first == repeated
    revised = _create(service, "filing:v2", revision_time, supersedes="filing:v1")

    chain = service.repository.revision_chain("filing:v2")
    assert [item.source_id for item in chain] == ["filing:v1", "filing:v2"]
    assert service.assert_usable(first, datetime(2026, 1, 15, tzinfo=UTC)) == first
    with pytest.raises(ValueError, match="not yet available"):
        service.assert_usable(revised, datetime(2026, 1, 15, tzinfo=UTC))
    assert service.assert_usable(revised, datetime(2026, 2, 2, tzinfo=UTC)) == revised
    with state.connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM point_in_time_metadata").fetchone()[0] == 2


def test_unknown_revision_predecessor_is_rejected(tmp_path: Path, state) -> None:
    service = _service(tmp_path, state)
    with pytest.raises(ValueError, match="Unknown superseded"):
        _create(
            service,
            "filing:v2",
            datetime(2026, 2, 1, tzinfo=UTC),
            supersedes="filing:missing",
        )


def test_not_pit_safe_is_excluded_from_formal_evaluation(tmp_path: Path, state) -> None:
    service = _service(tmp_path, state)
    observed = datetime(2026, 1, 10, tzinfo=UTC)
    metadata = _create(
        service,
        "provider:current-value",
        observed,
        status=PointInTimeStatus.NOT_PIT_SAFE,
        basis=AvailabilityBasis.PROVIDER_CURRENT_VALUE,
    )
    as_of = datetime(2026, 1, 11, tzinfo=UTC)
    with pytest.raises(ValueError, match="not allowed"):
        service.assert_usable(metadata, as_of)
    assert service.assert_usable(metadata, as_of, formal_historical=False) == metadata


def test_approximated_requires_explicit_opt_in_and_future_effect_is_blocked(
    tmp_path: Path, state
) -> None:
    service = _service(tmp_path, state)
    published = datetime(2026, 1, 10, tzinfo=UTC)
    effective = datetime(2026, 2, 1, tzinfo=UTC)
    metadata = service.create(
        source_id="estimated:future-effective",
        published_at=published,
        effective_at=effective,
        ingested_at=published,
        available_to_system_at=published,
        point_in_time_status=PointInTimeStatus.APPROXIMATED,
        availability_basis=AvailabilityBasis.USER_DECLARED,
    )
    with pytest.raises(ValueError, match="not yet effective"):
        service.assert_usable(
            metadata,
            datetime(2026, 1, 15, tzinfo=UTC),
            allow_approximated=True,
        )
    after_effect = datetime(2026, 2, 2, tzinfo=UTC)
    with pytest.raises(ValueError, match="not allowed"):
        service.assert_usable(metadata, after_effect)
    assert service.assert_usable(metadata, after_effect, allow_approximated=True) == metadata
