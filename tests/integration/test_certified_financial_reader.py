from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from astock.schemas import FinancialPeriodType, FinancialSourceReleaseStatus, Market
from tests.integration.test_financial_sources import (
    PERIOD_END,
    _capture_existing_annual,
    _service,
)


def test_certified_reader_obeys_actual_availability_and_replays_raw_lineage(tmp_path, monkeypatch):
    service = _service(tmp_path)
    capture = _capture_existing_annual(service)
    for provider in service.providers.values():
        monkeypatch.setattr(provider, "fetch",
                            lambda *args, **kwargs: (_ for _ in ()).throw(ValueError("offline")))
    report = service.sync(
        "000001", Market.XSHE, PERIOD_END, FinancialPeriodType.ANNUAL,
        live=True, allow_network=False,
    )
    assert report.status is FinancialSourceReleaseStatus.CERTIFIED
    after = datetime.now(UTC) + timedelta(seconds=1)
    verified = service.read_certified_release(
        "000001", PERIOD_END, FinancialPeriodType.ANNUAL, as_of=after
    )
    assert verified is not None and len(verified.facts) == report.coverage.certified_fact_count
    assert service.read_certified_release(
        "000001", PERIOD_END, FinancialPeriodType.ANNUAL,
        as_of=verified.manifest.available_to_system_at - timedelta(microseconds=1),
    ) is None
    service.objects.path_for(capture.object_sha256).write_bytes(b"corrupt official original")
    with pytest.raises(ValueError, match="snapshot chain"):
        service.read_certified_release(
            "000001", PERIOD_END, FinancialPeriodType.ANNUAL, as_of=after
        )
