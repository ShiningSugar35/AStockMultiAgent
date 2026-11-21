"""Bounded raw intraday fallback using the real, isolated Mootdx SDK."""

from __future__ import annotations

import ipaddress
import json
import math
import os
import subprocess
import tempfile
import threading
import time
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from astock.core.errors import FailureClass, ProviderError
from astock.core.hashing import canonical_json_bytes, content_hash
from astock.core.object_store import ObjectStore
from astock.core.project_root import resolve_project_root
from astock.core.request_budget import RequestBudgetExhausted, current_request_budget
from astock.core.state import StateStore
from astock.providers.base import MarketDataProvider
from astock.research.process_worker import KillableResearchWorker, WorkerDeadlineError
from astock.schemas import (
    AdjustmentMode,
    AmountUnit,
    BarRequest,
    DataProviderCapability,
    Frequency,
    InstrumentType,
    Market,
    MarketBar,
    MarketDataBatch,
    ProviderStatus,
    SourceSnapshot,
    TimestampSemantics,
    VolumeUnit,
)

_SHANGHAI = ZoneInfo("Asia/Shanghai")
_CONCURRENCY = threading.BoundedSemaphore(2)
_ENVIRONMENT = Path("runtime/tool-environments/mootdx")
_WORKER = Path("integrations/mootdx/worker.py")


class MootdxServer(BaseModel):
    model_config = ConfigDict(extra="forbid")
    host: str
    port: int = Field(default=7709, ge=1, le=65535)

    @model_validator(mode="after")
    def public_server(self) -> MootdxServer:
        if not ipaddress.ip_address(self.host).is_global:
            raise ValueError("Mootdx server must be an explicitly configured public IP")
        return self


class MootdxSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: str = "mootdx-fallback-v1"
    enabled: bool = True
    protocol: Literal["STANDARD", "MAC_COMPAT"] = "STANDARD"
    servers: list[MootdxServer] = Field(min_length=1, max_length=8)
    timeout_seconds: float = Field(default=30, gt=0, le=120)
    connect_timeout_seconds: float = Field(default=3, gt=0, le=10)
    max_pages: int = Field(default=8, ge=1, le=32)
    page_size: int = Field(default=800, ge=1, le=800)
    max_output_bytes: int = Field(default=4194304, ge=1024, le=16777216)

    @model_validator(mode="after")
    def bounded(self) -> MootdxSettings:
        if self.schema_version != "mootdx-fallback-v1":
            raise ValueError("Unknown Mootdx configuration version")
        if self.connect_timeout_seconds > self.timeout_seconds:
            raise ValueError("Connect timeout exceeds the total request budget")
        endpoints = {(server.host, server.port) for server in self.servers}
        if len(endpoints) != len(self.servers):
            raise ValueError("Duplicate Mootdx endpoints would add implicit retries")
        return self


class MootdxFallbackProvider:
    """Secondary quotes and unadjusted bars; no SDK import in the application's environment."""

    provider_id = "mootdx-fallback"
    fallback_only = True

    def __init__(
        self,
        objects: ObjectStore,
        state: StateStore,
        *,
        project_root: Path | None = None,
        timeout_seconds: float | None = None,
        settings: MootdxSettings | None = None,
    ) -> None:
        self.objects, self.state = objects, state
        self.project_root = (
            project_root.resolve()
            if project_root is not None
            else resolve_project_root(module_file=Path(__file__))
        )
        if settings is None:
            config = self.project_root / "configs" / "mootdx_fallback.yaml"
            settings = MootdxSettings.model_validate(
                yaml.safe_load(config.read_text(encoding="utf-8"))
            )
        self.settings = settings
        self.timeout_seconds = min(
            settings.timeout_seconds,
            settings.timeout_seconds if timeout_seconds is None else timeout_seconds,
        )
        if not 0 < self.timeout_seconds <= 120:
            raise ValueError("Mootdx timeout must be between zero and 120 seconds")

    @property
    def isolated_python(self) -> Path:
        executable = Path("Scripts/python.exe") if os.name == "nt" else Path("bin/python")
        return self.project_root / _ENVIRONMENT / executable

    def availability(self) -> tuple[bool, str]:
        if not self.settings.enabled:
            return False, "CONFIG_DISABLED"
        for path in (self.isolated_python, self.project_root / _WORKER):
            if not path.resolve().is_relative_to(self.project_root):
                return False, "UNOWNED_TOOL_PATH"
            if not path.is_file():
                return False, "ISOLATED_SDK_NOT_INSTALLED"
        return True, "ISOLATED_MOOTDX_READY"

    def capability(self) -> DataProviderCapability:
        available, reason = self.availability()
        return DataProviderCapability(
            provider_id=self.provider_id,
            markets=[Market.XSHG, Market.XSHE],
            instrument_types=[InstrumentType.STOCK],
            frequencies=[Frequency.M5, Frequency.H1],
            adjustment_modes=[AdjustmentMode.NONE],
            amount_supported=True,
            timestamp_semantics=TimestampSemantics.BAR_END,
            session_rules="A-share continuous auction; Shanghai-local bar-end labels",
            volume_unit=(
                VolumeUnit.SHARE if self.settings.protocol == "MAC_COMPAT"
                else VolumeUnit.LOT_100_SHARES
            ),
            rate_limit="At most two owned attempts; bounded public servers and pages; no retries",
            last_probe_at=datetime.now(UTC),
            quality_score=Decimal("0.65") if available else Decimal("0"),
            status=ProviderStatus.PARTIAL if available else ProviderStatus.UNAVAILABLE,
            limitations=[
                reason, "Secondary public TDX data; history coverage is checked downstream",
                "Quote source update time is preserved; freshness needs caller validation",
                "Only XSHG/XSHE stocks, raw 60m and 5m; no financial or adjustment capability",
            ],
        )

    def fetch_bars(self, request: BarRequest) -> MarketDataBatch:
        _identity(request.symbol, request.market)
        if (
            request.instrument_type is not InstrumentType.STOCK
            or request.frequency not in {Frequency.M5, Frequency.H1}
            or request.adjustment_mode is not AdjustmentMode.NONE
        ):
            raise _unavailable("Only raw XSHG/XSHE stock 5m and 60m bars are supported")
        wire = self._wire_request("bars", request.symbol, request.market)
        wire.update(
            frequency=request.frequency.value,
            requested_start=request.requested_start.isoformat(),
            requested_end=request.requested_end.isoformat(),
            max_pages=min(self.settings.max_pages, math.ceil(request.limit / wire["page_size"])),
        )
        payload, snapshot, latency = self._capture(wire)
        bars = _normalize_bars(payload, request, snapshot.snapshot_id)
        status = ProviderStatus.UNAVAILABLE
        if bars:
            status = (
                ProviderStatus.AVAILABLE if payload.get("complete") is True
                else ProviderStatus.PARTIAL
            )
        return MarketDataBatch(
            batch_id=content_hash({
                "provider": self.provider_id, "request": request,
                "observations": [bar.observation_id for bar in bars],
            }),
            provider_id=self.provider_id,
            request=request,
            requested_start=request.requested_start,
            requested_end=request.requested_end,
            actual_start=bars[0].timestamp if bars else None,
            actual_end=bars[-1].timestamp if bars else None,
            bar_count=len(bars),
            bars=bars,
            raw_snapshot_id=snapshot.snapshot_id,
            cursor=bars[-1].timestamp.isoformat() if bars else None,
            provider_latency_ms=latency,
            provider_status=status,
        )

    def fetch_quote(self, symbol: str, market: Market) -> tuple[dict[str, Any], SourceSnapshot]:
        _identity(symbol, market)
        payload, snapshot, _latency = self._capture(self._wire_request("quote", symbol, market))
        rows = payload["rows"]
        if len(rows) != 1 or not isinstance(rows[0], dict):
            raise _invalid("Mootdx did not return exactly one quote", snapshot.snapshot_id)
        row = rows[0]
        if str(row.get("code", "")) != symbol or row.get("market") != _identity(symbol, market):
            raise _invalid("Mootdx quote identity mismatch", snapshot.snapshot_id)
        prices = {name: _number(row.get(name), snapshot.snapshot_id) for name in (
            "price", "last_close", "open", "high", "low"
        )}
        if prices["price"] <= 0 or prices["last_close"] <= 0:
            raise _invalid("Mootdx quote lacks a positive price", snapshot.snapshot_id)
        if prices["high"] < max(prices["open"], prices["price"], prices["low"]):
            raise _invalid("Mootdx quote has an inconsistent high price", snapshot.snapshot_id)
        if prices["low"] > min(prices["open"], prices["price"], prices["high"]):
            raise _invalid("Mootdx quote has an inconsistent low price", snapshot.snapshot_id)
        source_timestamp = None
        if payload.get("protocol") == "MAC_COMPAT":
            prices = {key: value.quantize(Decimal("0.01")) for key, value in prices.items()}
            try:
                source_timestamp = datetime.strptime(
                    f"{int(row['source_date']):08}{int(row['source_time']):06}", "%Y%m%d%H%M%S"
                ).replace(tzinfo=_SHANGHAI)
            except (KeyError, TypeError, ValueError) as exc:
                raise _invalid(
                    "Mootdx source update time is invalid", snapshot.snapshot_id
                ) from exc
        return {
            "symbol": symbol, "market": market.value, **prices,
            "volume": _number(row.get("vol"), snapshot.snapshot_id),
            "volume_unit": VolumeUnit.LOT_100_SHARES.value,
            "amount": _number(row.get("amount"), snapshot.snapshot_id),
            "amount_unit": AmountUnit.CNY.value,
            "adjustment_mode": AdjustmentMode.NONE.value,
            "source_time_of_day": str(row.get("servertime", "")),
            "source_timestamp": source_timestamp,
            "timestamp_semantics": (
                "SOURCE_UPDATE_TIME" if source_timestamp else "TIME_OF_DAY_WITHOUT_DATE"
            ),
            "freshness_verified": False,
            "observed_at": snapshot.fetched_at,
            "raw_snapshot_id": snapshot.snapshot_id,
        }, snapshot

    def _wire_request(self, operation: str, symbol: str, market: Market) -> dict[str, Any]:
        return {
            "operation": operation, "symbol": symbol, "market": _identity(symbol, market),
            "protocol": self.settings.protocol,
            "timeout_seconds": self.timeout_seconds,
            "connect_timeout_seconds": min(
                self.settings.connect_timeout_seconds, self.timeout_seconds
            ),
            "max_pages": 1,
            "page_size": min(self.settings.page_size, 700) if self.settings.protocol == "MAC_COMPAT"
            else self.settings.page_size,
            "max_output_bytes": self.settings.max_output_bytes,
            "servers": [server.model_dump() for server in self.settings.servers],
        }

    def _capture(self, wire: dict[str, Any]) -> tuple[dict[str, Any], SourceSnapshot, int]:
        available, reason = self.availability()
        if not available:
            raise _unavailable(reason)
        started = time.perf_counter()
        budget = current_request_budget()
        timeout = (
            min(self.timeout_seconds, budget.remaining_seconds())
            if budget
            else self.timeout_seconds
        )
        if timeout <= 0:
            raise RequestBudgetExhausted("request elapsed budget exhausted")
        deadline = datetime.now(UTC) + timedelta(seconds=timeout)
        acquired = _CONCURRENCY.acquire(timeout=timeout)
        if not acquired:
            raise ProviderError(
                "Mootdx worker budget exhausted", failure_class=FailureClass.TIMEOUT
            )
        try:
            # One logical SDK launch; the SDK's internal TCP packets are not observable here.
            if budget is not None:
                budget.reserve_attempt()
                deadline = min(
                    deadline, datetime.now(UTC) + timedelta(seconds=budget.remaining_seconds())
                )
            remaining = (deadline - datetime.now(UTC)).total_seconds()
            if remaining <= 0:
                raise RequestBudgetExhausted("request elapsed budget exhausted")
            wire = {
                **wire, "timeout_seconds": remaining,
                "connect_timeout_seconds": min(float(wire["connect_timeout_seconds"]), remaining),
            }
            worker = KillableResearchWorker(
                _isolated_capture,
                (str(self.project_root), str(self.objects.root), wire),
                deadline_at=deadline,
                project_root=self.project_root,
                scratch_root=self.project_root / ".scratchpad" / "mootdx",
            )
            try:
                digest = worker(None)
            except WorkerDeadlineError as exc:
                raise ProviderError(
                    "Mootdx request deadline expired", failure_class=FailureClass.TIMEOUT,
                    retryable=True,
                ) from exc
            except RuntimeError as exc:
                raise ProviderError(
                    "Mootdx owned worker failed", failure_class=FailureClass.INVALID_RESPONSE,
                ) from exc
        finally:
            _CONCURRENCY.release()
        raw = self.objects.get_bytes(digest)
        snapshot = SourceSnapshot(
            snapshot_id=f"{self.provider_id}:{digest}",
            source_id=self.provider_id, object_sha256=digest,
            fetched_at=datetime.now(UTC), available_to_system_at=datetime.now(UTC),
            source_url=f"tdx://configured-public-servers/{wire['operation']}/{wire['market']}/{wire['symbol']}",
            mime="application/json", byte_size=len(raw),
            headers_hash=content_hash({"transport": "SDK_TCP", "sdk": "mootdx"}),
        )
        self.state.register_snapshot(snapshot)
        try:
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                raise ValueError("Expected object")
            if payload.get("failure_class") and (
                not payload.get("rows") or payload["failure_class"] == "PAGINATION_CYCLE"
            ):
                failure = FailureClass(str(payload["failure_class"]))
                raise ProviderError(
                    "Mootdx SDK capture failed", failure_class=failure,
                    retryable=failure in {FailureClass.NETWORK, FailureClass.TIMEOUT},
                    details={"snapshot_id": snapshot.snapshot_id},
                )
            if (
                payload.get("schema_version") != "mootdx-raw-v1"
                or payload.get("sdk") != "mootdx"
                or payload.get("sdk_version") != "0.11.7"
                or payload.get("transport_sdk") != "tdxpy"
                or payload.get("transport_sdk_version") != "0.2.7"
                or payload.get("protocol", "STANDARD") != wire["protocol"]
                or (
                    wire["protocol"] == "MAC_COMPAT"
                    and payload.get("transport_patch") != "astock-mootdx-mac-v1"
                )
                or payload.get("adjustment_mode") != "NONE"
                or payload.get("request") != wire
                or not isinstance(payload.get("rows"), list)
            ):
                raise ValueError("Mootdx provenance or response contract mismatch")
        except (json.JSONDecodeError, ValueError, TypeError) as exc:
            raise _invalid("Mootdx raw envelope is invalid", snapshot.snapshot_id) from exc
        return payload, snapshot, round((time.perf_counter() - started) * 1000)


def build_intraday_fallback_providers(
    objects: ObjectStore,
    state: StateStore,
    *,
    primary_providers: Sequence[MarketDataProvider],
    project_root: Path | None = None,
) -> list[MarketDataProvider]:
    """One construction point; absent SDK is a classified source failure on first use."""
    providers = list(primary_providers)
    if not any(
        provider.provider_id == MootdxFallbackProvider.provider_id for provider in providers
    ):
        fallback = MootdxFallbackProvider(objects, state, project_root=project_root)
        if fallback.settings.enabled:
            providers.append(fallback)
    return providers


def _identity(symbol: str, market: Market) -> int:
    if len(symbol) != 6 or not symbol.isascii() or not symbol.isdigit():
        raise _unavailable("Mootdx requires an exact six-digit exchange-qualified symbol")
    if market not in {Market.XSHG, Market.XSHE}:
        raise _unavailable("Mootdx adapter only supports XSHG and XSHE")
    return 1 if market is Market.XSHG else 0


def _normalize_bars(
    payload: dict[str, Any], request: BarRequest, snapshot_id: str
) -> list[MarketBar]:
    bars: list[MarketBar] = []
    mac_compat = payload.get("protocol") == "MAC_COMPAT"
    volume_unit = VolumeUnit.SHARE if mac_compat else VolumeUnit.LOT_100_SHARES
    seen: dict[datetime, MarketBar] = {}
    for row in payload["rows"]:
        if not isinstance(row, dict):
            raise _invalid("Mootdx bar must be an object", snapshot_id)
        try:
            timestamp = datetime.strptime(str(row["datetime"]), "%Y-%m-%d %H:%M").replace(
                tzinfo=_SHANGHAI
            )
            prices = {
                name: _number(row[name], snapshot_id) for name in ("open", "high", "low", "close")
            }
            if mac_compat:
                # MAC transmits float32 prices. A-share stock ticks are CNY 0.01;
                # retain the original floats and wire bytes in the raw snapshot.
                prices = {key: value.quantize(Decimal("0.01")) for key, value in prices.items()}
            if min(prices.values()) <= 0:
                raise ValueError("Nonpositive price")
            if prices["high"] < max(prices.values()) or prices["low"] > min(prices.values()):
                raise ValueError("Invalid OHLC relation")
            if not request.requested_start <= timestamp <= request.requested_end:
                continue
            bar = MarketBar(
                observation_id=content_hash({
                    "provider": MootdxFallbackProvider.provider_id, "symbol": request.symbol,
                    "market": request.market, "frequency": request.frequency,
                    "adjustment_mode": request.adjustment_mode, "row": row,
                }),
                provider_id=MootdxFallbackProvider.provider_id,
                symbol=request.symbol, market=request.market, frequency=request.frequency,
                timestamp=timestamp, timestamp_semantics=TimestampSemantics.BAR_END,
                open=prices["open"], high=prices["high"],
                low=prices["low"], close=prices["close"],
                volume=_number(row["vol"], snapshot_id), volume_unit=volume_unit,
                amount=_number(row["amount"], snapshot_id), amount_unit=AmountUnit.CNY,
                adjustment_mode=AdjustmentMode.NONE,
            )
            if timestamp in seen:
                if seen[timestamp].model_dump(exclude={"created_at"}) != bar.model_dump(
                    exclude={"created_at"}
                ):
                    raise ValueError("Conflicting duplicate timestamp")
                continue
            seen[timestamp] = bar
            bars.append(bar)
        except (KeyError, TypeError, ValueError) as exc:
            raise _invalid("Mootdx returned an invalid raw bar", snapshot_id) from exc
    return sorted(bars, key=lambda bar: bar.timestamp)[-request.limit:]


def _number(value: object, snapshot_id: str) -> Decimal:
    try:
        number = Decimal(str(value))
        if not number.is_finite() or number < 0:
            raise ValueError("Nonfinite or negative number")
        return number
    except (InvalidOperation, ValueError) as exc:
        raise _invalid("Mootdx returned an invalid numeric field", snapshot_id) from exc


def _invalid(message: str, snapshot_id: str) -> ProviderError:
    return ProviderError(
        message, failure_class=FailureClass.INVALID_RESPONSE,
        details={"snapshot_id": snapshot_id},
    )


def _unavailable(message: str) -> ProviderError:
    return ProviderError(message, failure_class=FailureClass.CAPABILITY_UNAVAILABLE)


def _isolated_capture(project_root: str, object_root: str, request: dict[str, Any]) -> str:
    """Fixed subprocess target inside the existing owned Windows job; only a hash crosses IPC."""
    root = Path(project_root).resolve()
    scratch = Path(tempfile.gettempdir()).resolve()
    if not scratch.is_relative_to(root / ".scratchpad"):
        raise ValueError("Mootdx scratch is not project-owned")
    profile = scratch / "sdk-home"
    profile.mkdir()
    executable = root / _ENVIRONMENT / (
        "Scripts/python.exe" if os.name == "nt" else "bin/python"
    )
    worker = root / _WORKER
    for path in (executable, worker):
        if not path.resolve().is_relative_to(root):
            raise ValueError("Mootdx executable escaped the project")
    env = {
        key: value for key, value in os.environ.items()
        if key.upper() in {"PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "LANG"}
    }
    env.update({
        "USERPROFILE": str(profile), "TMP": str(scratch), "TEMP": str(scratch),
        "TMPDIR": str(scratch), "PYTHONDONTWRITEBYTECODE": "1", "PYTHONIOENCODING": "utf-8",
    })
    output = scratch / "mootdx-response.json"
    try:
        with output.open("wb") as stdout:
            subprocess.run(
                [str(executable), "-B", "-I", str(worker)],
                input=canonical_json_bytes(request), stdout=stdout, stderr=subprocess.DEVNULL,
                timeout=float(request["timeout_seconds"]), cwd=root, env=env, check=False,
            )
        if output.stat().st_size > int(request["max_output_bytes"]):
            raise ValueError("Mootdx output exceeded its budget")
        raw = output.read_bytes()
        if not raw:
            raise ValueError("Mootdx worker returned no envelope")
    except subprocess.TimeoutExpired:
        raw = canonical_json_bytes({"failure_class": "TIMEOUT"})
    except FileNotFoundError:
        raw = canonical_json_bytes({"failure_class": "CAPABILITY_UNAVAILABLE"})
    except (OSError, ValueError):
        raw = canonical_json_bytes({"failure_class": "INVALID_RESPONSE"})
    return ObjectStore(Path(object_root)).put_bytes(raw).sha256
