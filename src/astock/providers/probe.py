"""Recorded-by-default provider health probes with durable, safe reports."""

from __future__ import annotations

import json
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from pydantic import ValidationError

from astock.core.errors import StorageError
from astock.core.hashing import canonical_json_bytes, content_hash, sha256_bytes
from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.providers.config import get_provider
from astock.schemas import (
    ProviderDefinition,
    ProviderHealthStatus,
    ProviderProbeFailureCode,
    ProviderProbeMode,
    ProviderProbeReport,
    ProviderRegistry,
    ProviderStatusReport,
)


@dataclass(frozen=True, slots=True)
class RawProbeResponse:
    status_code: int
    content: bytes
    content_type: str = "application/json"
    latency_ms: int = 0


ProbeTransport = Callable[[ProviderDefinition], RawProbeResponse]
_PROBE_LOCKS_GUARD = threading.Lock()
_PROBE_LOCKS: dict[tuple[str, str], threading.Lock] = {}
_PROBE_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


class ProviderProbeService:
    def __init__(
        self,
        *,
        project_root: Path,
        registry: ProviderRegistry,
        state: StateStore,
        objects: ObjectStore,
        live_transport: ProbeTransport | None = None,
    ) -> None:
        self.project_root = project_root.resolve()
        self.registry = registry
        self.state = state
        self.objects = objects
        self.live_transport = live_transport or self._live_request

    def list(self) -> list[ProviderStatusReport]:
        return [self.status(item.provider_id) for item in self.registry.providers]

    def status(self, provider_id: str) -> ProviderStatusReport:
        provider = get_provider(self.registry, provider_id)
        row, head = self.state.get_provider_probe_health_snapshot(provider_id)
        has_events = head is not None
        if row is None:
            return self._empty_status(
                provider,
                ProviderHealthStatus.CORRUPT if has_events else ProviderHealthStatus.NOT_PROBED,
            )

        pointer_fields = (
            "registry_version",
            "probe_mode",
            "report_artifact_id",
            "report_object_hash",
            "latest_probe_id",
        )
        populated = [bool(row.get(field)) for field in pointer_fields]
        if not any(populated):
            return self._empty_status(
                provider,
                ProviderHealthStatus.CORRUPT if has_events else ProviderHealthStatus.NOT_PROBED,
            )
        if not all(populated):
            return self._corrupt_status(provider, row)

        probe_id = str(row["latest_probe_id"])
        try:
            if (
                head is None
                or probe_id != head["latest_probe_id"]
                or int(row["failure_count"]) != int(head["failure_count"])
            ):
                raise ValueError("provider health is not the deterministic event head")
            report, object_hash = self._load_event_report(probe_id, provider_id)
            consecutive_failures = int(head["failure_count"])
            if (
                row["report_artifact_id"] != f"provider-probe:{report.probe_id}"
                or row["report_object_hash"] != object_hash
                or row["capability_hash"] != report.capability_hash
                or row["registry_version"] != report.registry_version
                or row["probe_mode"] != report.probe_mode.value
                or row["status"] != report.status.value
                or row["last_probe_at"] != report.completed_at.isoformat()
                or row["last_error_class"]
                != (report.failure_code.value if report.failure_code else None)
                or row["failure_code"]
                != (report.failure_code.value if report.failure_code else None)
                or int(row["failure_count"]) != consecutive_failures
            ):
                raise ValueError("provider report pointer mismatch")
        except (OSError, StorageError, ValueError, ValidationError):
            return self._corrupt_status(provider, row)

        current_capability_hash = content_hash(provider)
        if (
            report.registry_version != self.registry.registry_version
            or report.capability_hash != current_capability_hash
        ):
            return self._empty_status(
                provider,
                ProviderHealthStatus.NOT_PROBED,
                historical_report=report,
                historical_object_hash=object_hash,
            )
        try:
            self._validate_probe_contract(provider, report)
        except ValueError:
            return self._corrupt_status(provider, row)
        return self._status_from_report(
            provider,
            report,
            object_hash,
            failure_count=consecutive_failures,
        )

    def probe(
        self,
        provider_id: str,
        *,
        live: bool = False,
        probe_key: str | None = None,
        recorded_fixture: Path | None = None,
    ) -> ProviderStatusReport:
        provider = get_provider(self.registry, provider_id)
        mode = ProviderProbeMode.LIVE if live else ProviderProbeMode.RECORDED
        if live and not provider.live_supported:
            raise ValueError(f"Provider does not support live probes: {provider_id}")
        if live and probe_key is None:
            raise ValueError("live probes require a stable probe_key")
        if probe_key is not None and _PROBE_KEY.fullmatch(probe_key) is None:
            raise ValueError("probe_key has an invalid format")
        current = self.status(provider_id)
        if current.status == ProviderHealthStatus.CORRUPT:
            raise RuntimeError(f"Provider probe state is CORRUPT: {provider_id}")

        fixture_path = recorded_fixture or self.project_root / provider.recorded_fixture
        if live:
            identity_material = probe_key
            recorded_bytes: bytes | None = None
        else:
            locator = (
                str(fixture_path.absolute())
                if recorded_fixture is not None
                else provider.recorded_fixture
            )
            locator_hash = sha256_bytes(
                locator.encode("utf-8", errors="surrogatepass")
            )
            try:
                recorded_bytes = fixture_path.read_bytes()
            except OSError:
                recorded_bytes = None
            identity_material = {
                "fixture_locator_hash": locator_hash,
                "fixture_content_hash": (
                    sha256_bytes(recorded_bytes) if recorded_bytes is not None else None
                ),
                "fixture_read_status": (
                    "READABLE" if recorded_bytes is not None else "UNREADABLE"
                ),
                "probe_key": probe_key,
            }
        capability_hash = content_hash(provider)
        probe_id = content_hash(
            {
                "provider_id": provider_id,
                "registry_version": self.registry.registry_version,
                "capability_hash": capability_hash,
                "probe_mode": mode,
                "identity": identity_material,
            }
        )
        with _identity_lock(self.state.path, probe_id):
            existing = self.state.get_provider_probe_event(probe_id)
            if existing is not None:
                return self._load_existing_status(provider, probe_id)

            started_at = datetime.now(UTC)
            if live:
                response, transport_failure = self._capture_transport(provider)
            else:
                response, transport_failure = self._read_recorded(recorded_bytes)
            completed_at = datetime.now(UTC)
            status, failure_code, safe_metadata = self._classify(
                provider_id, response, transport_failure
            )
            checked_capabilities = _checked_capabilities(provider_id)
            unprobed = [
                capability
                for capability in provider.capabilities
                if capability not in checked_capabilities
            ]
            capability_gaps = _probe_capability_gaps(self.registry, provider)
            if status == ProviderHealthStatus.HEALTHY and unprobed:
                status = ProviderHealthStatus.DEGRADED
                failure_code = ProviderProbeFailureCode.CAPABILITY_NOT_PROBED
            report = ProviderProbeReport(
                probe_id=probe_id,
                provider_id=provider_id,
                registry_version=self.registry.registry_version,
                capability_hash=capability_hash,
                probe_mode=mode,
                started_at=started_at,
                completed_at=completed_at,
                latency_ms=response.latency_ms if response else 0,
                status=status,
                failure_code=failure_code,
                failure_count=0 if status == ProviderHealthStatus.HEALTHY else 1,
                checked_capabilities=checked_capabilities,
                capability_gaps=capability_gaps,
                safe_metadata=safe_metadata,
            )
            report_bytes = canonical_json_bytes(report.model_dump(mode="json"))
            object_ref = self.objects.put_bytes(report_bytes)
            if self.objects.get_bytes(object_ref.sha256) != report_bytes:
                raise RuntimeError("provider probe report verification failed")
            try:
                self.state.record_provider_probe(report, object_ref.sha256)
            except ValueError:
                return self._load_existing_status(provider, probe_id)
            return self._load_existing_status(provider, probe_id)

    def _load_existing_status(
        self,
        provider: ProviderDefinition,
        probe_id: str,
    ) -> ProviderStatusReport:
        """Load an exact immutable event, reducing every damaged-chain error safely."""

        try:
            report, object_hash = self._load_event_report(probe_id, provider.provider_id)
            self._validate_current_binding(provider, report)
            consecutive_failures = self.state.provider_probe_consecutive_failures(
                provider.provider_id,
                probe_id,
            )
            if consecutive_failures is None:
                raise ValueError("provider probe failure history is missing")
            return self._status_from_report(
                provider,
                report,
                object_hash,
                failure_count=consecutive_failures,
            )
        except (OSError, StorageError, ValidationError, ValueError):
            raise RuntimeError("provider probe state is CORRUPT") from None

    def _load_event_report(
        self, probe_id: str, provider_id: str
    ) -> tuple[ProviderProbeReport, str]:
        event = self.state.get_provider_probe_event(probe_id)
        if event is None:
            raise ValueError("provider probe event is missing")
        object_hash = str(event.get("report_object_hash") or "")
        raw = self.objects.get_bytes(object_hash)
        report = ProviderProbeReport.model_validate_json(raw)
        expected_artifact = f"provider-probe:{probe_id}"
        if (
            sha256_bytes(raw) != object_hash
            or report.probe_id != probe_id
            or report.provider_id != provider_id
            or event["provider_id"] != report.provider_id
            or event["registry_version"] != report.registry_version
            or event["capability_hash"] != report.capability_hash
            or event["probe_mode"] != report.probe_mode.value
            or event["status"] != report.status.value
            or event["completed_at"] != report.completed_at.isoformat()
            or event["failure_code"]
            != (report.failure_code.value if report.failure_code else None)
            or int(event["failure_count"]) != report.failure_count
            or event["report_artifact_id"] != expected_artifact
            or event["artifact_object_hash"] != object_hash
            or event["artifact_type"] != "ProviderProbeReport"
            or event["artifact_schema_version"] != report.schema_version
            or event["artifact_input_hashes_json"]
            != json.dumps([report.capability_hash], separators=(",", ":"))
        ):
            raise ValueError("provider probe event chain mismatch")
        return report, object_hash

    def _validate_current_binding(
        self, provider: ProviderDefinition, report: ProviderProbeReport
    ) -> None:
        if report.registry_version != self.registry.registry_version:
            raise ValueError("provider registry version drift")
        if report.capability_hash != content_hash(provider):
            raise ValueError("provider capability drift")
        self._validate_probe_contract(provider, report)

    def _validate_probe_contract(
        self, provider: ProviderDefinition, report: ProviderProbeReport
    ) -> None:
        expected_checked = _checked_capabilities(provider.provider_id)
        expected_gaps = _probe_capability_gaps(self.registry, provider)
        unprobed = set(provider.capabilities).difference(expected_checked)
        if report.checked_capabilities != expected_checked:
            raise ValueError("checked capability contract mismatch")
        if report.capability_gaps != expected_gaps:
            raise ValueError("capability gap contract mismatch")
        if unprobed and report.status == ProviderHealthStatus.HEALTHY:
            raise ValueError("unprobed declared capability cannot be HEALTHY")
        if (
            report.failure_code == ProviderProbeFailureCode.CAPABILITY_NOT_PROBED
            and (not unprobed or report.status != ProviderHealthStatus.DEGRADED)
        ):
            raise ValueError("CAPABILITY_NOT_PROBED outcome is inconsistent")

    def _status_from_report(
        self,
        provider: ProviderDefinition,
        report: ProviderProbeReport,
        object_hash: str,
        *,
        failure_count: int,
    ) -> ProviderStatusReport:
        return ProviderStatusReport(
            provider_id=provider.provider_id,
            registry_version=self.registry.registry_version,
            capabilities=provider.capabilities,
            checked_capabilities=report.checked_capabilities,
            capability_gaps=report.capability_gaps,
            transport=provider.transport,
            officiality=provider.officiality,
            live_supported=provider.live_supported,
            status=report.status,
            last_probe_at=report.completed_at,
            probe_mode=report.probe_mode,
            report_artifact_id=f"provider-probe:{report.probe_id}",
            report_object_hash=object_hash,
            failure_code=report.failure_code,
            failure_count=failure_count,
        )

    def _empty_status(
        self,
        provider: ProviderDefinition,
        status: ProviderHealthStatus,
        *,
        historical_report: ProviderProbeReport | None = None,
        historical_object_hash: str | None = None,
    ) -> ProviderStatusReport:
        gaps = _unique([*self.registry.capability_gaps, *provider.capabilities])
        return ProviderStatusReport(
            provider_id=provider.provider_id,
            registry_version=self.registry.registry_version,
            capabilities=provider.capabilities,
            checked_capabilities=[],
            capability_gaps=gaps,
            transport=provider.transport,
            officiality=provider.officiality,
            live_supported=provider.live_supported,
            status=status,
            last_probe_at=historical_report.completed_at if historical_report else None,
            probe_mode=historical_report.probe_mode if historical_report else None,
            report_artifact_id=(
                f"provider-probe:{historical_report.probe_id}" if historical_report else None
            ),
            report_object_hash=historical_object_hash,
            failure_count=0,
        )

    def _corrupt_status(
        self, provider: ProviderDefinition, row: dict[str, Any]
    ) -> ProviderStatusReport:
        probe_mode = _safe_enum(ProviderProbeMode, row.get("probe_mode"))
        failure_code = _safe_enum(ProviderProbeFailureCode, row.get("failure_code"))
        object_hash = str(row.get("report_object_hash") or "")
        if re.fullmatch(r"[0-9a-f]{64}", object_hash) is None:
            object_hash = ""
        artifact_id = row.get("report_artifact_id")
        return ProviderStatusReport(
            provider_id=provider.provider_id,
            registry_version=self.registry.registry_version,
            capabilities=provider.capabilities,
            checked_capabilities=[],
            capability_gaps=_unique([*self.registry.capability_gaps, *provider.capabilities]),
            transport=provider.transport,
            officiality=provider.officiality,
            live_supported=provider.live_supported,
            status=ProviderHealthStatus.CORRUPT,
            last_probe_at=_parse_datetime(row.get("last_probe_at")),
            probe_mode=probe_mode,
            report_artifact_id=str(artifact_id) if artifact_id else None,
            report_object_hash=object_hash or None,
            failure_code=failure_code,
            failure_count=_safe_nonnegative_int(row.get("failure_count")),
        )

    def _read_recorded(
        self, raw: bytes | None
    ) -> tuple[RawProbeResponse | None, ProviderProbeFailureCode | None]:
        if raw is None:
            return None, ProviderProbeFailureCode.MALFORMED_RESPONSE
        try:
            value = json.loads(raw)
            if not isinstance(value, dict):
                raise ValueError("fixture root must be an object")
            simulated = value.get("simulated_failure")
            if simulated is not None:
                return None, ProviderProbeFailureCode(str(simulated))
            allowed = {"status_code", "content_type", "body", "latency_ms"}
            if set(value).difference(allowed):
                raise ValueError("unknown recorded fixture fields")
            return (
                RawProbeResponse(
                    status_code=int(value["status_code"]),
                    content=canonical_json_bytes(value.get("body")),
                    content_type=str(value.get("content_type", "application/json")),
                    latency_ms=int(value.get("latency_ms", 0)),
                ),
                None,
            )
        except (UnicodeError, json.JSONDecodeError, KeyError, TypeError, ValueError):
            return None, ProviderProbeFailureCode.MALFORMED_RESPONSE

    def _capture_transport(
        self, provider: ProviderDefinition
    ) -> tuple[RawProbeResponse | None, ProviderProbeFailureCode | None]:
        try:
            return self.live_transport(provider), None
        except httpx.TimeoutException:
            return None, ProviderProbeFailureCode.TIMEOUT
        except httpx.HTTPError:
            return None, ProviderProbeFailureCode.NETWORK
        except (OSError, TimeoutError):
            return None, ProviderProbeFailureCode.NETWORK

    def _live_request(self, provider: ProviderDefinition) -> RawProbeResponse:
        started = time.perf_counter()
        if provider.provider_id == "baostock-reference":
            return _live_baostock_probe(started)
        with httpx.Client(
            timeout=provider.timeout_seconds,
            follow_redirects=True,
            headers={
                "User-Agent": "AStockResearch/provider-probe-v1",
                "Accept": "application/json",
            },
        ) as client:
            if provider.provider_id == "eastmoney-5m":
                response = client.get(
                    "https://push2his.eastmoney.com/api/qt/stock/kline/get",
                    params={
                        "secid": "1.600519",
                        "klt": 5,
                        "fqt": 0,
                        "lmt": 1,
                        "fields1": "f1,f2",
                        "fields2": "f51,f52,f53,f54,f55,f56,f57",
                    },
                )
            elif provider.provider_id == "sina-5m":
                response = client.get(
                    "https://quotes.sina.cn/cn/api/json_v2.php/CN_MarketDataService.getKLineData",
                    params={"symbol": "sh600519", "scale": 5, "ma": "no", "datalen": 1},
                )
            elif provider.provider_id == "cninfo-disclosures":
                response = client.post(
                    "https://www.cninfo.com.cn/new/hisAnnouncement/query",
                    data={
                        "pageNum": "1",
                        "pageSize": "1",
                        "column": "szse",
                        "tabName": "fulltext",
                        "plate": "",
                        "stock": "",
                        "searchkey": "",
                        "secid": "",
                        "category": "",
                        "trade": "",
                        "seDate": "",
                        "sortName": "",
                        "sortType": "",
                        "isHLtitle": "true",
                    },
                    headers={
                        "Referer": "https://www.cninfo.com.cn/new/disclosure",
                        "Origin": "https://www.cninfo.com.cn",
                        "X-Requested-With": "XMLHttpRequest",
                    },
                )
            elif provider.provider_id == "eastmoney-reference":
                response = client.get(
                    "https://push2.eastmoney.com/api/qt/clist/get",
                    params={
                        "pn": 1,
                        "pz": 1,
                        "fs": "m:0+t:81+s:2048",
                        "fields": "f12,f13,f14",
                    },
                )
            else:
                raise ValueError(f"No live probe adapter for {provider.provider_id}")
        latency_ms = round((time.perf_counter() - started) * 1000)
        return RawProbeResponse(
            status_code=response.status_code,
            content=response.content,
            content_type=response.headers.get("content-type", "application/octet-stream"),
            latency_ms=latency_ms,
        )

    @staticmethod
    def _classify(
        provider_id: str,
        response: RawProbeResponse | None,
        transport_failure: ProviderProbeFailureCode | None,
    ) -> tuple[ProviderHealthStatus, ProviderProbeFailureCode | None, dict[str, Any]]:
        if transport_failure is not None:
            status = (
                ProviderHealthStatus.DEGRADED
                if transport_failure
                in {
                    ProviderProbeFailureCode.MALFORMED_RESPONSE,
                    ProviderProbeFailureCode.DATA_QUALITY,
                }
                else ProviderHealthStatus.UNAVAILABLE
            )
            return status, transport_failure, {"response_received": False}
        assert response is not None
        safe = {
            "response_received": True,
            "status_code": response.status_code,
            "content_type": _safe_content_type(response.content_type),
            "byte_size": len(response.content),
        }
        http_failures = {
            401: ProviderProbeFailureCode.HTTP_401,
            403: ProviderProbeFailureCode.HTTP_403,
            429: ProviderProbeFailureCode.HTTP_429,
        }
        if response.status_code in http_failures:
            return ProviderHealthStatus.UNAVAILABLE, http_failures[response.status_code], safe
        if response.status_code < 200 or response.status_code >= 300:
            return ProviderHealthStatus.UNAVAILABLE, ProviderProbeFailureCode.NETWORK, safe
        try:
            payload = json.loads(response.content)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return (
                ProviderHealthStatus.DEGRADED,
                ProviderProbeFailureCode.MALFORMED_RESPONSE,
                safe,
            )
        quality, record_count = _validate_payload(provider_id, payload)
        safe["record_count"] = record_count
        if not quality:
            return ProviderHealthStatus.DEGRADED, ProviderProbeFailureCode.DATA_QUALITY, safe
        return ProviderHealthStatus.HEALTHY, None, safe


def _validate_payload(provider_id: str, payload: object) -> tuple[bool, int]:
    if provider_id == "eastmoney-5m":
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), dict):
            return False, 0
        rows = payload["data"].get("klines")
        if not isinstance(rows, list):
            return False, 0
        quality = bool(rows) and all(
            isinstance(row, str) and len(row.split(",")) >= 7 for row in rows
        )
        return quality, len(rows)
    if provider_id == "sina-5m":
        if not isinstance(payload, list):
            return False, 0
        required = {"day", "open", "high", "low", "close", "volume"}
        quality = bool(payload) and all(
            isinstance(row, dict) and required <= row.keys() for row in payload
        )
        return quality, len(payload)
    if provider_id == "cninfo-disclosures":
        if not isinstance(payload, dict) or not isinstance(payload.get("announcements"), list):
            return False, 0
        rows = payload["announcements"]
        return True, len(rows)
    if provider_id == "baostock-reference":
        if not isinstance(payload, dict):
            return False, 0
        fields = payload.get("fields")
        rows = payload.get("rows")
        quality = (
            payload.get("sdk_version") == "0.8.9"
            and payload.get("capability") == "instrument.master"
            and payload.get("complete") is True
            and payload.get("login_error_code") == "0"
            and payload.get("result_error_code") == "0"
            and isinstance(fields, list)
            and isinstance(rows, list)
            and bool(rows)
            and all(isinstance(row, list) and len(row) == len(fields) for row in rows)
        )
        return quality, len(rows) if isinstance(rows, list) else 0
    if provider_id == "eastmoney-reference":
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), dict):
            return False, 0
        rows = payload["data"].get("diff")
        quality = isinstance(rows, list) and bool(rows) and all(
            isinstance(row, dict) and {"f12", "f13", "f14"} <= row.keys() for row in rows
        )
        return quality, len(rows) if isinstance(rows, list) else 0
    return False, 0


def _checked_capabilities(provider_id: str) -> list[str]:
    checked = {
        "eastmoney-5m": ["market.raw_5m"],
        "sina-5m": ["market.raw_5m"],
        "cninfo-disclosures": ["disclosures.search"],
        "baostock-reference": ["instrument.master"],
        "eastmoney-reference": ["instrument.bjse_coverage"],
    }
    try:
        return list(checked[provider_id])
    except KeyError as exc:
        raise ValueError(f"No probe capability contract for {provider_id}") from exc


def _probe_capability_gaps(
    registry: ProviderRegistry, provider: ProviderDefinition
) -> list[str]:
    checked = set(_checked_capabilities(provider.provider_id))
    unprobed = [item for item in provider.capabilities if item not in checked]
    return _unique([*registry.capability_gaps, *unprobed])


def _unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))


def _identity_lock(state_path: Path, probe_id: str) -> threading.Lock:
    key = (str(state_path.resolve()), probe_id)
    with _PROBE_LOCKS_GUARD:
        return _PROBE_LOCKS.setdefault(key, threading.Lock())


def _parse_datetime(value: object) -> datetime | None:
    if value is None:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def _safe_content_type(value: str) -> str:
    media_type = value.split(";", maxsplit=1)[0].strip().lower()
    allowed = {
        "application/json",
        "application/octet-stream",
        "application/pdf",
        "text/json",
    }
    return media_type if media_type in allowed else "other"


def _safe_enum[T](enum_type: type[T], value: object) -> T | None:
    try:
        return enum_type(value)  # type: ignore[call-arg]
    except (TypeError, ValueError):
        return None


def _safe_nonnegative_int(value: object) -> int:
    try:
        return max(0, int(str(value)))
    except (TypeError, ValueError):
        return 0


def _live_baostock_probe(started: float) -> RawProbeResponse:
    import baostock as bs

    login = bs.login()
    fields: list[str] = []
    rows: list[list[str]] = []
    result_error_code = "LOGIN_NOT_SUCCESSFUL"
    complete = False
    try:
        if str(login.error_code) == "0":
            result = bs.query_stock_basic(code="sh.600519")
            fields = [str(item) for item in result.fields]
            result_error_code = str(result.error_code)
            while result.error_code == "0" and result.next():
                rows.append([str(item) for item in result.get_row_data()])
            complete = result_error_code == "0"
    finally:
        bs.logout()
    body = canonical_json_bytes(
        {
            "sdk_version": str(getattr(bs, "__version__", "0.8.9")),
            "capability": "instrument.master",
            "fields": fields,
            "rows": rows,
            "complete": complete,
            "login_error_code": str(login.error_code),
            "result_error_code": result_error_code,
        }
    )
    return RawProbeResponse(
        status_code=200,
        content=body,
        latency_ms=round((time.perf_counter() - started) * 1000),
    )


__all__ = ["ProviderProbeService", "ProbeTransport", "RawProbeResponse"]
