"""Scheduled prices must replay canonical identity/value/PIT, not just registry hashes."""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

from astock.investor_orchestration.scheduled_input_coverage import ScheduledInputFamily
from astock.schemas.institutional_research import MarketPriceAnchor
from astock.schemas.reference_data import DailyBarObservation
from tests.integration.test_scheduled_input_coverage import context as context


def _canonical_anchor(context, **changes):
    service, request = context
    record, payload = service.views.load(request.market_references[0])
    bar = DailyBarObservation.model_validate(payload)
    anchor = MarketPriceAnchor(
        price=bar.close,
        observed_at=bar.session_close_at,
        available_to_system_at=bar.available_to_system_at,
        source_artifact_id=record["parent_artifact_id"],
        source_object_hash=record["object_hash"],
        created_at=request.as_of,
    ).model_copy(update=changes)
    return _register_anchor(service, anchor), anchor


def _register_anchor(service, anchor, *, lineage=True):
    reference = service.objects.put_json(anchor.model_dump(mode="json"))
    identity = f"MarketPriceAnchor:source-identity:{uuid4()}"
    service.state.register_artifact(
        artifact_id=identity,
        artifact_type="MarketPriceAnchor",
        schema_version=anchor.schema_version,
        object_hash=reference.sha256,
        input_hashes=[anchor.source_object_hash] if lineage else [],
    )
    return identity


def _market_check(context, identity, **request_changes):
    service, request = context
    report = service.build(request.model_copy(update={
        "market_references": (identity,), **request_changes,
    }))
    return next(check for check in report.checks if check.family is ScheduledInputFamily.MARKET)


def test_source_identity_canonical_anchor_remains_usable(context):
    identity, anchor = _canonical_anchor(context)
    check = _market_check(context, identity)
    assert check.status == "CHECKED"
    assert check.checked_through == anchor.observed_at


def test_source_identity_anchor_cannot_cover_another_security(context):
    identity, _ = _canonical_anchor(context)
    _, request = context
    scope = request.scope.model_copy(update={"instrument_id": "XSHE:000001"})
    assert _market_check(context, identity, scope=scope).status == "INVALID"


@pytest.mark.parametrize("mutation", ["price", "observation_time", "created_at", "availability"])
def test_source_identity_rehashed_anchor_cannot_change_canonical_facts(context, mutation):
    _, request = context
    changes = {
        "price": {"price": Decimal("123.45")},
        "observation_time": {"observed_at": request.as_of - timedelta(seconds=1)},
        "created_at": {"created_at": request.as_of + timedelta(days=1)},
        "availability": {"available_to_system_at": request.interval_start},
    }
    identity, _ = _canonical_anchor(context, **changes[mutation])
    assert _market_check(context, identity).status == "INVALID"


def test_source_identity_anchor_requires_registered_parent_lineage(context):
    service, _ = context
    _, anchor = _canonical_anchor(context)
    identity = _register_anchor(service, anchor, lineage=False)
    assert _market_check(context, identity).status == "INVALID"


def test_source_identity_previous_close_cannot_certify_current_intraday(context):
    from astock.investor_orchestration.models import ScheduledWindow
    from astock.investor_orchestration.scheduled_input_coverage import (
        ScheduledInputCoverageService,
        load_input_audit_policy,
    )

    service, request = context
    identity, _ = _canonical_anchor(context)
    audit = ScheduledInputCoverageService(service.store, load_input_audit_policy())
    selected = request.model_copy(update={
        "window": ScheduledWindow.INTRADAY, "market_references": (identity,),
    })
    check = audit.build(selected).checks[0]
    assert check.status == "STALE"
    assert check.reason == "MARKET_OBSERVATION_EXPIRED"


def test_source_identity_parent_is_replayed_once_per_audit_not_cached_between_audits(
    context, monkeypatch,
):
    service, request = context
    first, _ = _canonical_anchor(context)
    second, _ = _canonical_anchor(context)
    calls = []
    original = service.views._daily_rows

    def counted(release_id):
        calls.append(release_id)
        return original(release_id)

    monkeypatch.setattr(service.views, "_daily_rows", counted)
    selected = request.model_copy(update={"market_references": (first, second)})
    for _ in range(2):
        assert service.build(selected).checks[0].status == "CHECKED"
    assert len(calls) == 2
    assert calls[0] == calls[1]


def test_source_identity_self_registered_generic_source_is_not_canonical(context):
    service, request = context
    source = service.objects.put_json({
        "instrument_id": request.scope.instrument_id,
        "price": "10", "observed_at": request.as_of.isoformat(),
    })
    source_id = f"MarketReferenceRelease:self-certified:{uuid4()}"
    service.state.register_artifact(
        artifact_id=source_id, artifact_type="MarketReferenceRelease",
        schema_version="self-certified", object_hash=source.sha256, input_hashes=[],
    )
    anchor = MarketPriceAnchor(
        price=Decimal("10"), observed_at=request.as_of,
        available_to_system_at=request.as_of, created_at=request.as_of,
        source_artifact_id=source_id, source_object_hash=source.sha256,
    )
    identity = _register_anchor(service, anchor)
    assert _market_check(context, identity).status == "INVALID"
