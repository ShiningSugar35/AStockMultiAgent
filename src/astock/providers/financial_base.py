"""Raw-first HTTP foundations shared by secondary financial providers."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

import httpx

from astock.core.hashing import content_hash
from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.providers.dialects import ProviderDialect, load_provider_dialects
from astock.providers.runtime import build_provider_http_client
from astock.schemas import FetchStatus, Market, SourceSnapshot


@dataclass(frozen=True, slots=True)
class FinancialProviderPayload:
    provider_id: str
    request_company_id: str
    request_market: Market
    request_period_end: str
    tables: dict[str, list[dict[str, object]]]
    snapshots_by_statement: dict[str, SourceSnapshot]
    request_hashes_by_statement: dict[str, str]

    @property
    def snapshots(self) -> list[SourceSnapshot]:
        by_id = {
            item.snapshot_id: item for item in self.snapshots_by_statement.values()
        }
        return list(by_id.values())


class FinancialRawCaptureError(ValueError):
    def __init__(self, failure_code: str, snapshots: list[SourceSnapshot]) -> None:
        super().__init__(failure_code)
        self.failure_code = failure_code
        self.snapshots = snapshots


class FinancialProviderBase:
    provider_id: str
    fixture_name: str

    def __init__(
        self,
        objects: ObjectStore,
        state: StateStore,
        fixture_root: Path,
        *,
        client: httpx.Client | None = None,
        dialect: ProviderDialect | None = None,
    ) -> None:
        self.objects = objects
        self.state = state
        self.fixture_root = fixture_root.resolve()
        if dialect is None:
            dialects = load_provider_dialects(
                Path(__file__).resolve().parents[3] / "configs" / "provider_dialects.yaml"
            )
            try:
                dialect = dialects[self.provider_id]
            except KeyError as exc:
                raise ValueError(f"No provider dialect configured for {self.provider_id}") from exc
        if dialect.provider_id != self.provider_id:
            raise ValueError("Financial provider dialect identity mismatch")
        self.dialect = dialect
        self.client = client or build_provider_http_client(self.provider_id)

    def fetch(
        self,
        company_id: str,
        market: Market,
        period_end: date,
        *,
        live: bool = False,
    ) -> FinancialProviderPayload:
        raise NotImplementedError

    def _recorded_json(
        self,
        company_id: str,
        market: Market,
        period_end: str,
    ) -> tuple[dict[str, object], SourceSnapshot]:
        path = (self.fixture_root / self.fixture_name).resolve()
        if not path.is_relative_to(self.fixture_root) or not path.is_file():
            raise ValueError("Recorded financial fixture is unavailable")
        raw = path.read_bytes()
        snapshot = self._persist(
            raw,
            source_url=f"recorded://{self.provider_id}",
            content_type="application/json",
            observed_at=_recorded_available(raw),
            request={
                "company_id": company_id,
                "market": market.value,
                "period_end": period_end,
                "mode": "recorded",
            },
        )
        try:
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                raise ValueError
        except (json.JSONDecodeError, TypeError, UnicodeError, ValueError) as exc:
            raise FinancialRawCaptureError(
                "FINANCIAL_RAW_INVALID", [snapshot]
            ) from exc
        return payload, snapshot

    def _capture_json(
        self,
        url: str,
        *,
        params: dict[str, str | int],
        request_context: dict[str, object],
    ) -> tuple[dict[str, object], SourceSnapshot]:
        try:
            response = self.client.get(url, params=params)
        except (httpx.HTTPError, OSError) as exc:
            raise FinancialRawCaptureError("FINANCIAL_NETWORK_FAILED", []) from exc
        snapshot = self._persist(
            response.content,
            source_url=str(response.request.url),
            content_type=response.headers.get("content-type", "application/octet-stream"),
            observed_at=datetime.now(UTC),
            succeeded=200 <= response.status_code < 300,
            request={"url": url, "params": params, "context": request_context},
        )
        if response.status_code < 200 or response.status_code >= 300:
            raise FinancialRawCaptureError("FINANCIAL_HTTP_FAILED", [snapshot])
        try:
            payload = json.loads(response.content)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise FinancialRawCaptureError("FINANCIAL_RAW_INVALID", [snapshot]) from exc
        if not isinstance(payload, dict):
            raise FinancialRawCaptureError("FINANCIAL_RAW_INVALID", [snapshot])
        return payload, snapshot

    def _persist(
        self,
        raw: bytes,
        *,
        source_url: str,
        content_type: str,
        observed_at: datetime,
        request: dict[str, object],
        succeeded: bool = True,
    ) -> SourceSnapshot:
        object_ref = self.objects.put_bytes(raw)
        request_hash = content_hash(request)
        snapshot_identity = content_hash(
            {"raw": object_ref.sha256, "request": request}
        )
        snapshot_id = f"{self.provider_id}:{snapshot_identity}"
        existing = self.state.get_snapshot(snapshot_id)
        if existing is not None:
            if not self.objects.verify(existing.object_sha256):
                raise ValueError("Persisted financial snapshot object is corrupt")
            return existing
        snapshot = SourceSnapshot(
            created_at=observed_at,
            snapshot_id=snapshot_id,
            source_id=f"{self.provider_id}:{request_hash}",
            object_sha256=object_ref.sha256,
            fetched_at=observed_at,
            available_to_system_at=observed_at,
            source_url=source_url,
            mime=content_type.split(";", maxsplit=1)[0],
            byte_size=object_ref.byte_size,
            headers_hash=content_hash(
                {"content_type": content_type, "request": request}
            ),
            fetch_status=FetchStatus.SUCCEEDED if succeeded else FetchStatus.FETCH_FAILED,
            rights_status="PUBLIC_REFERENCE_DATA",
        )
        self.state.register_snapshot(snapshot)
        persisted = self.state.get_snapshot(snapshot_id)
        if persisted is None:
            raise ValueError("Financial snapshot registration failed")
        return persisted


def _recorded_available(raw: bytes) -> datetime:
    try:
        payload = json.loads(raw)
        value = payload["available_to_system_at"]
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError
        return parsed.astimezone(UTC)
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        raise ValueError("Recorded financial fixture lacks an aware availability") from exc


__all__ = [
    "FinancialProviderBase",
    "FinancialProviderPayload",
    "FinancialRawCaptureError",
]
