"""Optional public hints using canonical routing, circuits and evidence stores.

A hint is not a certified financial fact, full-universe proof or recommendation.
AKShare preserves its EASTMONEY origin; optional credentials never enter URLs.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from astock.core.credentials import CredentialSource, resolve_credential
from astock.core.errors import FailureClass, ProviderError
from astock.core.hashing import canonical_json_bytes, content_hash
from astock.core.object_store import ObjectStore
from astock.core.project_root import resolve_project_root
from astock.core.state import StateStore
from astock.providers.base import HttpProviderBase
from astock.providers.config import load_provider_registry
from astock.providers.http_resilience import HttpClientLike
from astock.providers.runtime import ProviderFactory, TransportProfile, load_transport_profiles
from astock.schemas import Market, SourceSnapshot


class SupplementalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    capability: Literal[
        "market.reference.hint",
        "news.discovery.lead",
        "news.global.lead",
    ] = "market.reference.hint"
    symbol: str = Field(default="", pattern=r"^(?:\d{6})?$")
    market: Market = Market.XSHG
    start: date
    end: date

    @model_validator(mode="after")
    def validate_window(self) -> SupplementalRequest:
        if self.capability == "market.reference.hint" and (
            not self.symbol or self.market not in {Market.XSHG, Market.XSHE, Market.BJSE}
        ):
            raise ValueError("reference hints require a symbol and equity market")
        if self.end < self.start or (self.end - self.start).days > 366:
            raise ValueError("supplemental window must be ordered and bounded to 366 days")
        return self

    @property
    def ts_code(self) -> str:
        suffix = {Market.XSHG: "SH", Market.XSHE: "SZ", Market.BJSE: "BJ"}[self.market]
        return f"{self.symbol}.{suffix}"


class _HintPersistence:
    provider_id: str
    object_store: ObjectStore
    state: StateStore

    def persist_envelope(self, payload: object, source_url: str) -> SourceSnapshot:
        ref = self.object_store.put_bytes(canonical_json_bytes(payload))
        now = datetime.now(UTC)
        snapshot = SourceSnapshot(
            snapshot_id=f"{self.provider_id}:{ref.sha256}",
            source_id=self.provider_id,
            object_sha256=ref.sha256,
            fetched_at=now,
            available_to_system_at=now,
            source_url=source_url,
            mime="application/json",
            byte_size=ref.byte_size,
            headers_hash=content_hash({"capture": "SDK_RETURN_ENVELOPE"}),
        )
        self.state.register_snapshot(snapshot)
        return snapshot


def _invalid(message: str) -> ProviderError:
    return ProviderError(message, failure_class=FailureClass.INVALID_RESPONSE)


def _sdk_environment(*, trust_env: bool = True) -> dict[str, str]:
    root = resolve_project_root(module_file=Path(__file__))
    local = root / ".ai-bridge" / "provider-sdk" / "akshare"
    local.mkdir(parents=True, exist_ok=True)
    allowed = {
        "SYSTEMROOT", "WINDIR", "COMSPEC", "PATH", "PATHEXT", "PROCESSOR_ARCHITECTURE",
        "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "SSL_CERT_FILE",
        "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE",
    }
    env = {key: value for key, value in os.environ.items() if key.upper() in allowed}
    if not trust_env:
        env = {key: value for key, value in env.items() if not key.upper().endswith("_PROXY")}
        # NO_PROXY=* also suppresses requests' Windows system-proxy discovery.
        # This child-only environment never changes the user's proxy configuration.
        env["NO_PROXY"] = "*"
    env.update({key: str(local) for key in (
        "TEMP", "TMP", "TMPDIR", "XDG_CACHE_HOME", "MPLCONFIGDIR",
    )})
    env.update({"PYTHONDONTWRITEBYTECODE": "1", "PYTHONIOENCODING": "utf-8"})
    return env


class AKShareHintProvider(_HintPersistence):
    provider_id = "akshare-reference-hints"

    def __init__(
        self, objects: ObjectStore, state: StateStore, timeout_seconds: float = 20,
        transport_profile: TransportProfile | None = None,
    ) -> None:
        self.object_store, self.state = objects, state
        self.timeout_seconds = timeout_seconds
        self.transport_profile = transport_profile

    def fetch_hints(
        self, request: SupplementalRequest,
    ) -> tuple[list[dict[str, str]], SourceSnapshot]:
        if request.capability != "market.reference.hint":
            raise ValueError("AKShare hint capability mismatch")
        payload = self._fetch_payload(request)
        snapshot = self.persist_envelope(payload, "akshare://stock_zh_a_hist/" + request.symbol)
        if (
            payload.get("upstream") != "EASTMONEY"
            or payload.get("operation") != "stock_zh_a_hist"
            or payload.get("adjust") != ""
            or not payload.get("sdk_version")
        ):
            raise _invalid("AKShare SDK operation provenance changed")
        return normalize_daily(payload["rows"], request, kind="akshare"), snapshot

    def _fetch_payload(self, request: SupplementalRequest) -> dict[str, Any]:
        profile = self.transport_profile
        lanes = profile.lane_trust_env if profile else (True,)
        limit = profile.max_attempts if profile else 1
        deadline = time.monotonic() + self.timeout_seconds
        last_error = ProviderError(
            "AKShare recovery budget exhausted", failure_class=FailureClass.TIMEOUT,
        )
        for attempt in range(limit):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            # Reserve a bounded share for subsequent lanes; SDK imports are included.
            timeout = min(
                profile.timeout_seconds if profile else self.timeout_seconds,
                remaining / (limit - attempt),
            )
            trust_env = lanes[attempt % len(lanes)]
            try:
                payload = self._worker_once(request, timeout=timeout, trust_env=trust_env)
                payload["transport_lane"] = "ENV" if trust_env else "DIRECT"
                payload["transport_attempt"] = attempt + 1
                return payload
            except ProviderError as exc:
                last_error = exc
                if exc.failure_class not in {FailureClass.NETWORK, FailureClass.TIMEOUT}:
                    raise
                if profile and attempt + 1 < limit:
                    delay = profile.backoff_seconds * (2 ** attempt)
                    if delay >= deadline - time.monotonic():
                        break
                    if delay > 0:
                        time.sleep(delay)
        raise last_error

    @staticmethod
    def _worker_once(
        request: SupplementalRequest, *, timeout: float, trust_env: bool,
    ) -> dict[str, Any]:
        try:
            result = subprocess.run(
                [sys.executable, "-B", "-m", "astock.providers.supplemental_worker",
                 request.symbol, request.start.isoformat(), request.end.isoformat(), str(timeout)],
                capture_output=True, text=True, encoding="utf-8", errors="strict",
                timeout=timeout, check=False, env=_sdk_environment(trust_env=trust_env),
            )
        except subprocess.TimeoutExpired:
            raise ProviderError(
                "AKShare bounded worker timed out", failure_class=FailureClass.TIMEOUT,
            ) from None
        if result.returncode != 0:
            try:
                failure = FailureClass(json.loads(result.stdout)["failure_class"])
            except (ValueError, KeyError, TypeError):
                failure = FailureClass.CAPABILITY_UNAVAILABLE
            raise ProviderError("AKShare public-data worker failed", failure_class=failure)
        try:
            payload = json.loads(result.stdout)
        except (ValueError, UnicodeError):
            raise _invalid("AKShare SDK returned invalid JSON") from None
        if not isinstance(payload, dict) or not isinstance(payload.get("rows"), list):
            raise _invalid("AKShare SDK returned invalid envelope")
        return payload


class _CredentialHttpProvider(HttpProviderBase):
    def __init__(
        self,
        object_store: ObjectStore,
        state: StateStore,
        *,
        client: HttpClientLike | None = None,
        timeout_seconds: float = 20.0,
        project_root: Path | None = None,
    ) -> None:
        super().__init__(
            object_store,
            state,
            client=client,
            timeout_seconds=timeout_seconds,
        )
        self.project_root = project_root or resolve_project_root(module_file=Path(__file__))

    def credential(self, name: str) -> str:
        return _credential(name, project_root=self.project_root)

    def credentials(self, name: str) -> tuple[str, ...]:
        return _credentials(name, project_root=self.project_root)


class TushareHintProvider(_CredentialHttpProvider):
    provider_id = "tushare-reference-hints"

    def fetch_hints(
        self, request: SupplementalRequest,
    ) -> tuple[list[dict[str, str]], SourceSnapshot]:
        if request.capability != "market.reference.hint":
            raise ValueError("Tushare hint capability mismatch")
        credential = self.credential("TUSHARE_TOKEN")
        response, _ = self._request(
            "POST", "https://api.tushare.pro",
            json={
                "api_name": "daily", "token": credential,
                "params": {
                    "ts_code": request.ts_code,
                    "start_date": request.start.strftime("%Y%m%d"),
                    "end_date": request.end.strftime("%Y%m%d"),
                },
                "fields": "ts_code,trade_date,open,high,low,close,vol,amount",
            },
        )
        if credential.encode() in response.content:
            raise _invalid("Tushare response echoed authentication")
        payload = response.json()
        if not isinstance(payload, dict) or payload.get("code") != 0:
            raise ProviderError(
                "Tushare interface entitlement or quota unavailable",
                failure_class=FailureClass.ACCESS_RESTRICTED,
            )
        snapshot = self._persist_response(response)
        data = payload.get("data")
        if (
            not isinstance(data, dict)
            or not isinstance(data.get("fields"), list)
            or not isinstance(data.get("items"), list)
        ):
            raise _invalid("Tushare data shape changed")
        fields = data["fields"]
        if not all(isinstance(key, str) for key in fields) or len(fields) != len(set(fields)):
            raise _invalid("Tushare duplicate or invalid columns")
        try:
            raw_rows = [dict(zip(fields, values, strict=True)) for values in data["items"]]
        except (ValueError, TypeError):
            raise _invalid("Tushare row shape changed") from None
        return normalize_daily(raw_rows, request, kind="tushare"), snapshot


class FinnhubNewsHintProvider(_CredentialHttpProvider):
    provider_id = "finnhub-news-hints"

    def fetch_hints(
        self, request: SupplementalRequest,
    ) -> tuple[list[dict[str, str]], SourceSnapshot]:
        if request.capability not in {"news.discovery.lead", "news.global.lead"}:
            raise ValueError("Finnhub is a global news lead, not certified A-share coverage")
        response = None
        last_access_error: ProviderError | None = None
        for credential in self.credentials("FINNHUB_API_KEY"):
            try:
                response, _ = self._request(
                    "GET", "https://finnhub.io/api/v1/news", params={"category": "general"},
                    headers={"X-Finnhub-Token": credential},
                )
            except ProviderError as exc:
                if exc.failure_class in {
                    FailureClass.ACCESS_RESTRICTED,
                    FailureClass.RATE_LIMITED,
                }:
                    last_access_error = exc
                    continue
                raise
            if credential.encode() in response.content:
                raise _invalid("Finnhub response echoed authentication")
            break
        if response is None:
            if last_access_error is not None:
                raise last_access_error
            raise ProviderError(
                "Finnhub has no usable configured credential",
                failure_class=FailureClass.AUTH_REQUIRED,
            )
        payload = response.json()
        if not isinstance(payload, list):
            raise _invalid("Finnhub news unavailable")
        snapshot = self._persist_response(response)
        rows = []
        for item in payload[:200]:
            if not isinstance(item, dict):
                continue
            url = str(item.get("url", ""))
            headline = str(item.get("headline", "")).strip()
            try:
                published = datetime.fromtimestamp(int(item["datetime"]), UTC)
            except (KeyError, TypeError, ValueError, OverflowError, OSError):
                continue
            if (
                not headline or not url.startswith("https://")
                or not request.start <= published.date() <= request.end
            ):
                continue
            rows.append({
                "title": headline, "url": url, "published_at": published.isoformat(),
                "publisher": str(item.get("source", "")),
            })
        return rows, snapshot


def _credential(name: str, *, project_root: Path | None = None) -> str:
    resolution = resolve_credential(name, project_root=project_root)
    if resolution.configured and resolution.value is not None:
        return resolution.value
    if resolution.source is CredentialSource.INVALID_DOTENV:
        raise ProviderError(
            "Optional provider credential configuration is invalid",
            failure_class=FailureClass.DATA_QUALITY,
        )
    raise ProviderError(
        "Optional provider is not configured", failure_class=FailureClass.AUTH_REQUIRED,
    )


def _credentials(name: str, *, project_root: Path | None = None) -> tuple[str, ...]:
    resolution = resolve_credential(name, project_root=project_root)
    if resolution.configured and resolution.value is not None:
        values = tuple(
            dict.fromkeys(
                item.strip() for item in resolution.value.split(",") if item.strip()
            )
        )
        if not values or len(values) > 8:
            raise ProviderError(
                "Optional provider credential list is invalid",
                failure_class=FailureClass.DATA_QUALITY,
            )
        return values
    if resolution.source is CredentialSource.INVALID_DOTENV:
        raise ProviderError(
            "Optional provider credential configuration is invalid",
            failure_class=FailureClass.DATA_QUALITY,
        )
    raise ProviderError(
        "Optional provider is not configured", failure_class=FailureClass.AUTH_REQUIRED,
    )


def normalize_daily(
    raw_rows: list[Any], request: SupplementalRequest, *, kind: str,
) -> list[dict[str, str]]:
    """Validate symbol, dates, finite OHLCV and explicit source units; keep raw extras."""
    keys = (
        ("日期", "开盘", "最高", "最低", "收盘", "成交量", "成交额") if kind == "akshare"
        else ("trade_date", "open", "high", "low", "close", "vol", "amount")
    )
    rows: dict[str, dict[str, str]] = {}
    try:
        for raw in raw_rows:
            if not isinstance(raw, dict):
                raise ValueError("row must be an object")
            identity = (
                str(raw.get("股票代码", "")) if kind == "akshare"
                else str(raw.get("ts_code", ""))
            )
            if identity != (request.symbol if kind == "akshare" else request.ts_code):
                raise ValueError("security identity mismatch")
            day = date.fromisoformat(str(raw[keys[0]]))
            if not request.start <= day <= request.end:
                raise ValueError("date outside requested window")
            numbers = [Decimal(str(raw[key])) for key in keys[1:]]
            if any(not number.is_finite() or number < 0 for number in numbers):
                raise ValueError("invalid finite positive numeric field")
            op, high, low, close, volume, amount = numbers
            if (
                min(op, high, low, close) <= 0
                or high < max(op, low, close) or low > min(op, high, close)
            ):
                raise ValueError("OHLC identity invalid")
            row = {
                "symbol": request.symbol, "market": request.market.value,
                "date": day.isoformat(), "open": str(op), "high": str(high),
                "low": str(low), "close": str(close), "volume_shares": str(volume * 100),
                "amount_yuan": str(amount * (1000 if kind == "tushare" else 1)),
                "adjustment": "UNADJUSTED",
            }
            existing = rows.get(row["date"])
            if existing is not None and existing != row:
                raise ValueError("conflicting duplicate dates")
            rows[row["date"]] = row
    except (KeyError, ValueError, TypeError, ArithmeticError):
        raise ProviderError(
            "Supplemental daily identity, units or OHLC validation failed",
            failure_class=FailureClass.DATA_QUALITY,
        ) from None
    return [rows[key] for key in sorted(rows)]


class SupplementalEvidenceService:
    """Agent-invoked bounded fallback through existing ProviderFactory and stores."""

    def __init__(self, root: Path, state: StateStore, objects: ObjectStore) -> None:
        self.root, self.state, self.objects = root, state, objects
        self.factory = ProviderFactory(
            load_provider_registry(root / "configs/provider_registry.yaml"),
            load_transport_profiles(root / "configs/transport_profiles.yaml"),
            objects,
            state,
            root / "tests/fixtures",
            project_root=root,
        )

    def collect(self, request: SupplementalRequest, *, live: bool = False) -> dict[str, Any]:
        request = SupplementalRequest.model_validate(request.model_dump())
        definitions = (
            self.factory.definitions_for_capability(request.capability) if live else
            [item for item in self.factory.registry.providers
             if request.capability in item.capabilities]
        )
        captures, attempts = [], []
        collect_all_independent_news_sources = request.capability == "news.discovery.lead"
        budget = float(self.factory.source_breaker.policy.default_elapsed_budget_seconds)
        deadline = time.monotonic() + budget
        for definition in definitions:
            if time.monotonic() >= deadline:
                break
            provider_id = definition.provider_id
            if live and deadline - time.monotonic() < definition.timeout_seconds:
                attempts.append({"provider_id": provider_id, "status": "BUDGET_RESERVED"})
                continue
            allowed = self.factory.claim_capability_attempt(
                provider_id, request.capability, live=live,
            )
            if not allowed:
                attempts.append({"provider_id": provider_id, "status": "COOLDOWN"})
                continue
            try:
                if live:
                    provider = self.factory.create(provider_id)
                    fetch = getattr(provider, "fetch_hints", None)
                    if not callable(fetch):
                        raise ValueError("provider hint interface missing")
                    capture = fetch(request)
                    if not isinstance(capture, tuple) or len(capture) != 2:
                        raise _invalid("Supplemental capture must contain rows and a snapshot")
                    records, snapshot = capture
                    if not isinstance(records, list) or not isinstance(snapshot, SourceSnapshot):
                        raise _invalid("Supplemental capture contract is invalid")
                else:
                    payload = json.loads(
                        (self.root / definition.recorded_fixture).read_text(encoding="utf-8"),
                    )
                    if payload.get("request") != request.model_dump(mode="json"):
                        raise ProviderError(
                            "Recorded fixture request mismatch",
                            failure_class=FailureClass.DATA_QUALITY,
                        )
                    records = payload["records"]
                    persistence = _HintPersistence()
                    persistence.provider_id = provider_id
                    persistence.object_store, persistence.state = self.objects, self.state
                    snapshot = persistence.persist_envelope(payload, "fixture://" + provider_id)
                if not records:
                    raise ProviderError(
                        "No requested supplemental records",
                        failure_class=FailureClass.DATA_QUALITY,
                    )
                envelope = {
                    "schema_version": "supplemental-evidence-v1", "provider_id": provider_id,
                    "independence_group": definition.independence_group,
                    "request": request.model_dump(mode="json"), "records": records,
                    "source_snapshot_id": snapshot.snapshot_id,
                    "source_object_hash": snapshot.object_sha256,
                    "mode": "LIVE" if live else "RECORDED", "formal_use_allowed": False,
                    "full_universe_proven": False,
                }
                ref = self.objects.put_json(envelope)
                artifact_id = "SupplementalEvidenceCapture:" + ref.sha256
                self.state.register_artifact(
                    artifact_id=artifact_id, artifact_type="SupplementalEvidenceCapture",
                    schema_version="supplemental-evidence-v1", object_hash=ref.sha256,
                    input_hashes=[snapshot.object_sha256],
                )
                captures.append({
                    "artifact_id": artifact_id, "provider_id": provider_id,
                    "record_count": len(records), "source_snapshot_id": snapshot.snapshot_id,
                    "independence_group": definition.independence_group,
                })
                self.factory.record_capability_success(provider_id, request.capability, live=live)
                attempts.append({"provider_id": provider_id, "status": "CAPTURED"})
                if live and not collect_all_independent_news_sources:
                    # Reference hints stop after one source; news discovery keeps all sources.
                    break
            except (ProviderError, ValueError, KeyError) as exc:
                error = exc if isinstance(exc, ProviderError) else _invalid(
                    "Supplemental response contract invalid",
                )
                self.factory.record_capability_failure(
                    provider_id, request.capability, error, live=live,
                )
                attempts.append({"provider_id": provider_id, "status": error.failure_class.value})
        return {
            "status": "REFERENCE_HINTS_CAPTURED" if captures else "PUBLIC_DATA_UNAVAILABLE",
            "captures": captures, "attempts": attempts, "manual_actions": [],
            "independence_groups": sorted(
                {str(item["independence_group"]) for item in captures}
            ),
            "multi_source_news_attempted": collect_all_independent_news_sources,
            "automatic_recovery_next": "OFFICIAL_WEB_AND_CANONICAL_ACQUISITION_VALIDATION",
            "formal_use_allowed": False,
        }
