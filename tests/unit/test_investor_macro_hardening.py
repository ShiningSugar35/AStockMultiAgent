"""Recorded transport/security regressions, never actual provider/live qualification."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from astock.core.errors import StorageError
from astock.core.object_store import ObjectStore
from astock.investor_orchestration.macro import (
    ImmutableRawObjectStore,
    MacroReleaseSpec,
    OfficialMacroCaptureService,
)
from astock.investor_orchestration.store import InvestorOrchestrationStore

BODY = b"<html><body>PMI=50.1; period=2026-08</body></html>"
URL = "https://www.stats.gov.cn/recorded-macro-security-test"


def _spec(url: str = URL) -> MacroReleaseSpec:
    return MacroReleaseSpec(
        authority="NBS",
        release_family="RECORDED_SECURITY_TEST",
        url=url,
        parser="regex-v1",
        regex_observations=(
            {
                "series_key": "recorded_pmi",
                "pattern": r"PMI=(?P<value>[0-9.]+)",
                "period": "2026-08",
                "unit": "index",
                "min_value": 0,
                "max_value": 100,
            },
        ),
    )


def _service(
    store: InvestorOrchestrationStore, client: httpx.Client | None = None
) -> OfficialMacroCaptureService:
    return OfficialMacroCaptureService(
        store,
        http_client=client,
        object_store=ImmutableRawObjectStore(store.path.parent / "objects"),
    )


@pytest.fixture
def store(tmp_path: Path) -> InvestorOrchestrationStore:
    result = InvestorOrchestrationStore(tmp_path / "state.sqlite")
    result.initialize()
    return result


def test_raw_macro_bytes_use_the_canonical_object_store(tmp_path: Path) -> None:
    root = tmp_path / "objects"
    raw = ImmutableRawObjectStore(root)
    object_id, path = raw.put(BODY, suffix=".html")
    digest = object_id.removeprefix("sha256:")
    assert path == ObjectStore(root).path_for(digest)
    assert ObjectStore(root).get_bytes(digest) == BODY


def test_existing_raw_object_corruption_is_not_silently_reused(tmp_path: Path) -> None:
    raw = ImmutableRawObjectStore(tmp_path / "objects")
    _, path = raw.put(BODY, suffix=".html")
    path.write_bytes(b"recorded corruption")
    with pytest.raises(StorageError, match="hash|Hash|mismatch|corrupt"):
        raw.put(BODY, suffix=".html")


@pytest.mark.parametrize(
    "url",
    [
        "ftp://www.stats.gov.cn/recorded",
        "https://recorded-user:recorded-password@www.stats.gov.cn/recorded",
        "https://www.stats.gov.cn:444/recorded",
    ],
)
def test_official_hostname_does_not_authorize_unsafe_url_forms(
    store: InvestorOrchestrationStore,
    url: str,
) -> None:
    with pytest.raises(ValueError):
        _service(store).capture(_spec(url), recorded_content=BODY)


def test_official_redirect_is_validated_before_following_the_destination(
    store: InvestorOrchestrationStore,
) -> None:
    visited: list[str] = []

    def transport(request: httpx.Request) -> httpx.Response:
        visited.append(str(request.url))
        if request.url.host == "www.stats.gov.cn":
            return httpx.Response(302, headers={"location": "https://untrusted.invalid/release"})
        return httpx.Response(200, content=BODY)

    with httpx.Client(transport=httpx.MockTransport(transport), follow_redirects=True) as client:
        with pytest.raises(ValueError, match="official|redirect|authority|allow|approved"):
            _service(store, client).capture(_spec(), live=True)
    assert visited == [URL], "untrusted redirect target must never receive a request"


def test_live_capture_cannot_be_backdated_by_its_caller(store: InvestorOrchestrationStore) -> None:
    calls = []

    def transport(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, content=BODY)

    with httpx.Client(transport=httpx.MockTransport(transport)) as client:
        with pytest.raises(ValueError, match="captured_at|time|backdate"):
            _service(store, client).capture(
                _spec(),
                live=True,
                captured_at=datetime.now(UTC) - timedelta(days=365),
            )
    assert calls == []


def test_recorded_payload_cannot_be_submitted_as_live_capture(
    store: InvestorOrchestrationStore,
) -> None:
    with httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, content=BODY))
    ) as client:
        with pytest.raises(ValueError, match="recorded|live"):
            _service(store, client).capture(_spec(), live=True, recorded_content=BODY)


def test_recorded_capture_requires_an_aware_available_time(
    store: InvestorOrchestrationStore,
) -> None:
    with pytest.raises(ValueError, match="aware|time"):
        _service(store).capture(_spec(), recorded_content=BODY, captured_at=datetime(2026, 9, 7))


def test_network_availability_is_frozen_after_the_response_is_observed(
    store: InvestorOrchestrationStore,
) -> None:
    observed: list[datetime] = []

    def transport(_: httpx.Request) -> httpx.Response:
        observed.append(datetime.now(UTC))
        return httpx.Response(200, content=BODY)

    with httpx.Client(transport=httpx.MockTransport(transport)) as client:
        snapshot = _service(store, client).capture(_spec(), live=True)
    assert snapshot.captured_at >= observed[0]
    assert snapshot.observations
    assert all(item.available_to_system_at >= observed[0] for item in snapshot.observations)
    assert snapshot.capture_mode == "LIVE"
    assert snapshot.final_source_url == URL
    assert snapshot.capture_policy_hash


def test_legitimate_same_authority_redirect_preserves_provenance(
    store: InvestorOrchestrationStore,
) -> None:
    final = "https://www.stats.gov.cn/recorded-final"
    visited = []

    def transport(request: httpx.Request) -> httpx.Response:
        visited.append(str(request.url))
        if str(request.url) == URL:
            return httpx.Response(302, headers={"location": "/recorded-final"})
        return httpx.Response(200, content=BODY)

    with httpx.Client(transport=httpx.MockTransport(transport), follow_redirects=True) as client:
        snapshot = _service(store, client).capture(_spec(), live=True)
    assert visited == [URL, final]
    assert snapshot.final_source_url == final
    assert snapshot.redirect_chain == (URL, final)
    assert snapshot.parse_status == "PASS"


def test_recorded_capture_is_not_evidence_of_official_first_release(
    store: InvestorOrchestrationStore,
) -> None:
    service = _service(store)
    at = datetime.now(UTC) - timedelta(minutes=1)
    first = service.capture(_spec(), recorded_content=BODY, captured_at=at)
    assert first.capture_mode == "RECORDED"
    assert first.observations[0].first_observed
    assert not first.observations[0].first_release
    as_of = datetime.now(UTC)
    assert service.observation_at(
        authority="NBS",
        release_family="RECORDED_SECURITY_TEST",
        series_key="recorded_pmi",
        observation_period="2026-08",
        as_of=as_of,
        vintage="FIRST_RELEASE",
    ) is None
    assert service.observation_at(
        authority="NBS",
        release_family="RECORDED_SECURITY_TEST",
        series_key="recorded_pmi",
        observation_period="2026-08",
        as_of=as_of,
        vintage="FIRST_OBSERVED",
    ) == first.observations[0]


def test_redirect_loop_is_bounded_before_repeated_fetch(store: InvestorOrchestrationStore) -> None:
    calls = []

    def transport(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(302, headers={"location": URL})

    with httpx.Client(transport=httpx.MockTransport(transport), follow_redirects=True) as client:
        with pytest.raises(ValueError, match="loop"):
            _service(store, client).capture(_spec(), live=True)
    assert calls == [URL]
