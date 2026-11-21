"""Bounded Tushare HTTP queries sharing the canonical SQLite quota and raw store."""
from __future__ import annotations

import json
import math
import re
import sqlite3
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4
from zoneinfo import ZoneInfo

import httpx
import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from astock.core.credentials import CredentialSource, resolve_credential
from astock.core.errors import FailureClass, ProviderError
from astock.core.hashing import canonical_json_bytes, content_hash, sha256_bytes
from astock.core.object_store import ObjectStore
from astock.core.project_root import resolve_project_root
from astock.core.source_resilience import parse_retry_after_seconds
from astock.core.state import StateStore
from astock.providers.http_resilience import configure_owned_http_client
from astock.schemas import SourceSnapshot

Endpoint = Literal[
    "stock_basic", "daily", "weekly", "monthly", "daily_basic",
    "income", "balancesheet", "cashflow", "cn_gdp", "cn_cpi",
]
FINANCIAL = frozenset({"income", "balancesheet", "cashflow"})
MARKET = frozenset({"daily", "weekly", "monthly", "daily_basic"})
SHANGHAI = ZoneInfo("Asia/Shanghai")
HTTP_ENDPOINT = "https://api.tushare.pro"
FIELD_NAME = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
TS_CODE = re.compile(r"^[0-9]{6}\.(?:SH|SZ|BJ)$")
PARAMETERS: dict[str, frozenset[str]] = {
    "stock_basic": frozenset({"ts_code", "name", "market", "list_status", "exchange", "is_hs"}),
    **{key: frozenset({"ts_code", "trade_date", "start_date", "end_date"})
       for key in MARKET},
    **{key: frozenset({
        "ts_code", "ann_date", "start_date", "end_date", "period", "report_type", "comp_type",
    }) for key in FINANCIAL},
    "cn_gdp": frozenset({"q", "start_q", "end_q"}),
    "cn_cpi": frozenset({"m", "start_m", "end_m"}),
}
PARAMETERS["income"] = PARAMETERS["income"] | {"f_ann_date"}
IDENTITY_FIELDS: dict[str, tuple[str, ...]] = {
    "stock_basic": ("ts_code", "symbol", "name", "list_status", "list_date", "delist_date"),
    **{key: ("ts_code", "trade_date") for key in MARKET},
    **{key: ("ts_code", "ann_date", "f_ann_date", "end_date", "report_type", "comp_type")
       for key in FINANCIAL},
    "cn_gdp": ("quarter",),
    "cn_cpi": ("month",),
}
DEFAULT_FIELDS: dict[str, tuple[str, ...]] = {
    "stock_basic": (
        *IDENTITY_FIELDS["stock_basic"], "area", "industry", "market", "exchange", "curr_type",
    ),
    **{key: ("ts_code", "trade_date", "open", "high", "low", "close",
             "pre_close", "change", "pct_chg", "vol", "amount")
       for key in ("daily", "weekly", "monthly")},
    "daily_basic": (
        "ts_code", "trade_date", "close", "turnover_rate", "turnover_rate_f", "volume_ratio",
        "pe", "pe_ttm", "pb", "ps", "ps_ttm", "dv_ratio", "dv_ttm",
        "total_share", "float_share", "free_share", "total_mv", "circ_mv",
    ),
    # Empty fields asks the official API for all default financial columns.
    **{key: () for key in FINANCIAL},
    "cn_gdp": ("quarter", "gdp", "gdp_yoy", "pi", "pi_yoy", "si", "si_yoy", "ti", "ti_yoy"),
    "cn_cpi": (
        "month", "nt_val", "nt_yoy", "nt_mom", "nt_accu", "town_val", "town_yoy",
        "town_mom", "town_accu", "cnt_val", "cnt_yoy", "cnt_mom", "cnt_accu",
    ),
}


def _error(message: str, failure: FailureClass, **details: Any) -> ProviderError:
    return ProviderError(message, failure_class=failure, details=details)


class EndpointAccess(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool
    capability: str
    minimum_points: int = Field(ge=0)
    requests_per_minute: int = Field(ge=1, le=200)
    requests_per_day: int = Field(ge=1, le=100000)
    cache_ttl_seconds: int = Field(ge=0, le=86400)
    permission_status: Literal["UNVERIFIED", "VERIFIED", "DENIED"] = "UNVERIFIED"
    quota_basis: str
    documentation_url: str


class TushareAccess(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal["tushare-access-v1"]
    source_id: Literal["tushare"]
    reported_points: int = Field(ge=0)
    global_requests_per_minute: int = Field(ge=1, le=200)
    default_endpoint_requests_per_day: int = Field(ge=1, le=100000)
    financial_requests_per_minute: int = Field(ge=1, le=80)
    financial_quota_basis: str
    max_concurrency: int = Field(ge=1, le=2)
    timeout_seconds: float = Field(gt=0, le=20)
    max_response_bytes: int = Field(ge=1024, le=4194304)
    documentation: dict[str, Any]
    endpoints: dict[str, EndpointAccess]

    @model_validator(mode="after")
    def allowed_endpoints(self) -> TushareAccess:
        if not self.endpoints or not set(self.endpoints).issubset(PARAMETERS):
            raise ValueError("Tushare configuration contains an unreviewed endpoint")
        return self


def load_tushare_access(root: Path) -> TushareAccess:
    return TushareAccess.model_validate(
        yaml.safe_load((root / "configs/tushare_access.yaml").read_text(encoding="utf-8"))
    )


class TushareQueryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    endpoint: Endpoint
    params: dict[str, str] = Field(default_factory=dict)
    fields: tuple[str, ...] = ()
    max_rows: int = Field(default=6000, ge=1, le=10000)
    refresh: bool = False

    @model_validator(mode="after")
    def bounded_request(self) -> TushareQueryRequest:
        if set(self.params) - PARAMETERS[self.endpoint]:
            raise ValueError("Unreviewed Tushare query parameter")
        if any(len(value) > 100 or "\x00" in value for value in self.params.values()):
            raise ValueError("Tushare query parameter is too long or invalid")
        if len(self.fields) > 200 or len(set(self.fields)) != len(self.fields):
            raise ValueError("Tushare field list is excessive or duplicated")
        if any(not FIELD_NAME.fullmatch(value) for value in self.fields):
            raise ValueError("Invalid Tushare field identifier")
        code = self.params.get("ts_code")
        if code and not TS_CODE.fullmatch(code):
            raise ValueError("Tushare requires one exchange-qualified security code")
        if self.endpoint in FINANCIAL and not code:
            raise ValueError("The permitted financial endpoints require one ts_code")
        for name in ("trade_date", "start_date", "end_date", "ann_date", "f_ann_date", "period"):
            if self.params.get(name):
                _date(self.params[name])
        if "period" in self.params and self.params["period"][4:] not in {
            "0331", "0630", "0930", "1231",
        }:
            raise ValueError("Financial period must be a calendar quarter end")
        if "start_date" in self.params or "end_date" in self.params:
            start = _date(self.params.get("start_date", ""))
            end = _date(self.params.get("end_date", ""))
            if not 0 <= (end - start).days <= 3660:
                raise ValueError("Tushare date range must be ordered and at most ten years")
        if self.endpoint in MARKET:
            if not self.params.get("trade_date") and not (
                code and self.params.get("start_date") and self.params.get("end_date")
            ):
                raise ValueError(
                    "Market queries require one date or a bounded single-security range"
                )
        if self.endpoint in FINANCIAL and not any(
            self.params.get(name) for name in ("period", "ann_date", "f_ann_date", "start_date")
        ):
            raise ValueError("Financial queries require a report period or announcement window")
        if self.endpoint == "stock_basic":
            if self.params.get("list_status", "L") not in {"L", "D", "P", "G", "UN"}:
                raise ValueError("Unknown listing status")
            if self.params.get("exchange", "") not in {"", "SSE", "SZSE", "BSE"}:
                raise ValueError("Unknown exchange")
        if self.endpoint in {"cn_gdp", "cn_cpi"}:
            token = "q" if self.endpoint == "cn_gdp" else "m"
            _validate_macro_window(self.params, token)
        return self

    def wire_fields(self) -> tuple[str, ...]:
        fields = self.fields or DEFAULT_FIELDS[self.endpoint]
        return tuple(sorted(set((*IDENTITY_FIELDS[self.endpoint], *fields)))) if fields else ()

    def identity(self) -> dict[str, Any]:
        params = dict(self.params)
        if self.endpoint == "stock_basic":
            params.setdefault("list_status", "L")
        return {"endpoint": self.endpoint, "params": params, "fields": list(self.wire_fields())}


def _date(value: Any) -> date:
    if not isinstance(value, str) or re.fullmatch(r"[0-9]{8}", value) is None:
        raise ValueError("Expected YYYYMMDD")
    return datetime.strptime(value, "%Y%m%d").date()


def _validate_macro_window(params: dict[str, str], token: str) -> None:
    def ordinal(value: str) -> int:
        if token == "q":
            if re.fullmatch(r"[12][0-9]{3}Q[1-4]", value) is None:
                raise ValueError("Expected a single YYYYQn quarter")
            return int(value[:4]) * 4 + int(value[-1])
        if re.fullmatch(r"[12][0-9]{5}", value) is None:
            raise ValueError("Expected a single YYYYMM month")
        return date(int(value[:4]), int(value[4:]), 1).year * 12 + int(value[4:])

    if params.get(token):
        ordinal(params[token])
    else:
        start = ordinal(params.get("start_" + token, ""))
        end = ordinal(params.get("end_" + token, ""))
        if not 0 <= end - start <= 120:
            raise ValueError("Macro range must be ordered and bounded to 120 periods")


class TushareQuota:
    """Atomic rolling-minute, Shanghai-day and two-slot leases in the existing StateStore."""

    def __init__(
        self, state: StateStore, config: TushareAccess, clock: Callable[[], datetime],
    ) -> None:
        self.state, self.config, self.clock = state, config, clock

    @contextmanager
    def reserve(self, endpoint: str, fingerprint: str) -> Iterator[None]:
        now = self.clock().astimezone(UTC)
        epoch = now.timestamp()
        day = now.astimezone(SHANGHAI).date().isoformat()
        policy = self.config.endpoints.get(endpoint)
        if policy is None and endpoint != "__mcp_control__":
            raise _error("Unreviewed quota endpoint", FailureClass.CAPABILITY_UNAVAILABLE)
        rpm = policy.requests_per_minute if policy else self.config.global_requests_per_minute
        daily = (
            policy.requests_per_day if policy else self.config.default_endpoint_requests_per_day
        )
        owner = uuid4().hex
        request_key = "tushare:request:" + fingerprint
        lease_until = (now + timedelta(seconds=self.config.timeout_seconds * 2 + 5)).isoformat()
        slot_key = ""
        with self.state.transaction() as connection:
            duplicate = connection.execute(
                "SELECT 1 FROM lease_lock WHERE lock_key=? AND lease_until>?",
                (request_key, now.isoformat()),
            ).fetchone()
            if duplicate is not None:
                raise _error("Equivalent Tushare query is in flight", FailureClass.CONFLICT)
            for index in range(self.config.max_concurrency):
                candidate = f"tushare:concurrency:{index}"
                occupied = connection.execute(
                    "SELECT 1 FROM lease_lock WHERE lock_key=? AND lease_until>?",
                    (candidate, now.isoformat()),
                ).fetchone()
                if occupied is None:
                    slot_key = candidate
                    break
            if not slot_key:
                raise _error("Tushare concurrency budget exhausted", FailureClass.RATE_LIMITED)
            row = connection.execute(
                "SELECT value_json FROM cursor_state WHERE provider_id='tushare' AND scope='quota'",
            ).fetchone()
            value = json.loads(row[0]) if row else {}
            recent = [
                item for item in value.get("recent", [])
                if isinstance(item, list) and len(item) == 2 and float(item[0]) > epoch - 60
            ]
            counts = value.get("daily", {}) if value.get("day") == day else {}
            if (
                len(recent) >= self.config.global_requests_per_minute
                or sum(item[1] == endpoint for item in recent) >= rpm
                or int(counts.get(endpoint, 0)) >= daily
                or (
                    endpoint in FINANCIAL
                    and sum(item[1] in FINANCIAL for item in recent)
                    >= self.config.financial_requests_per_minute
                )
            ):
                raise _error(
                    "Tushare shared quota exhausted", FailureClass.RATE_LIMITED,
                    retry_after_seconds=60, source_id="tushare",
                )
            recent.append([epoch, endpoint])
            counts[endpoint] = int(counts.get(endpoint, 0)) + 1
            _write_cursor(connection, "quota", {"day": day, "daily": counts, "recent": recent}, now)
            for key in (request_key, slot_key):
                connection.execute(
                    "INSERT INTO lease_lock(lock_key,owner_run_id,lease_until) VALUES(?,?,?) "
                    "ON CONFLICT(lock_key) DO UPDATE SET owner_run_id=excluded.owner_run_id,"
                    "lease_until=excluded.lease_until",
                    (key, owner, lease_until),
                )
        try:
            yield
        finally:
            for key in (request_key, slot_key):
                self.state.release_lock(key, owner)


def _write_cursor(
    connection: sqlite3.Connection, scope: str, value: dict[str, Any], now: datetime,
) -> None:
    connection.execute(
        "INSERT INTO cursor_state(cursor_id,provider_id,scope,value_json,updated_at) "
        "VALUES(?,?,?,?,?) ON CONFLICT(provider_id,scope) DO UPDATE SET "
        "value_json=excluded.value_json,updated_at=excluded.updated_at",
        (sha256_bytes(f"tushare:{scope}".encode()), "tushare", scope,
         canonical_json_bytes(value).decode(), now.isoformat()),
    )


class TushareStructuredProvider:
    """One-query adapter. Raw evidence is secondary; entitlement is observed per endpoint."""

    provider_id = "tushare-structured"

    def __init__(
        self, objects: ObjectStore, state: StateStore, *,
        project_root: Path | None = None, client: httpx.Client | None = None,
        timeout_seconds: float = 20, config: TushareAccess | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.objects, self.state = objects, state
        self.root = project_root or resolve_project_root(module_file=Path(__file__))
        self.config = config or load_tushare_access(self.root)
        self.clock = clock or (lambda: datetime.now(UTC))
        self.timeout_seconds = min(timeout_seconds, self.config.timeout_seconds)
        self.client = client or httpx.Client(
            timeout=self.timeout_seconds, follow_redirects=False,
            headers={"Accept": "application/json", "User-Agent": "AStockMultiAgent"},
        )
        self.owns_client = client is None
        self.quota = TushareQuota(state, self.config, self.clock)

    def close(self) -> None:
        if self.owns_client:
            self.client.close()

    def credential(self) -> str:
        credential = resolve_credential("TUSHARE_TOKEN", project_root=self.root)
        if credential.configured and credential.value is not None:
            return credential.value
        failure = (
            FailureClass.DATA_QUALITY if credential.source is CredentialSource.INVALID_DOTENV
            else FailureClass.AUTH_REQUIRED
        )
        raise _error("Tushare credential is unavailable", failure)

    def schema(self, endpoint: str | None = None) -> dict[str, Any]:
        names = [endpoint] if endpoint is not None else list(self.config.endpoints)
        if any(name not in self.config.endpoints for name in names):
            raise _error("Tushare endpoint is not allowed", FailureClass.CAPABILITY_UNAVAILABLE)
        credential = resolve_credential("TUSHARE_TOKEN", project_root=self.root)
        auth_scope = (
            sha256_bytes(credential.value.encode())
            if credential.configured and credential.value is not None else None
        )
        observed: dict[str, dict[str, Any]] = {}
        for name in names:
            row = self.state.get_cursor(
                "tushare", "permission:" + auth_scope + ":" + name
            ) if auth_scope is not None else None
            observation = row["value"] if row is not None else {}
            observed[name] = {
                "status": observation.get("status", "UNKNOWN"),
                "observed_at": observation.get("observed_at"),
                "failure_class": observation.get("failure_class"),
                "transport": observation.get("transport"),
                "source_snapshot_id": observation.get("source_snapshot_id"),
                "applies_to_current_configured_credential": auth_scope is not None,
                "permanent_entitlement_proven": False,
            }
        return {
            "schema_version": "tushare-schema-v1",
            "source_id": "tushare", "reported_points": self.config.reported_points,
            "points_are_not_permission_proof": True,
            "request_schema": TushareQueryRequest.model_json_schema(),
            "global_requests_per_minute": self.config.global_requests_per_minute,
            "financial_group_requests_per_minute": self.config.financial_requests_per_minute,
            "day_timezone": "Asia/Shanghai", "max_concurrency": self.config.max_concurrency,
            "http_and_mcp_data_queries_share_transport": False,
            "quota_scope": "THIS_PROJECT_STATESTORE; EXTERNAL_CLIENTS_NOT_CONTROLLED",
            "mcp_support": "CAPABILITY_PROBE_ONLY; NO_DATA_QUERY_BYPASS",
            "endpoints": {
                name: {
                    **self.config.endpoints[name].model_dump(mode="json"),
                    "observed_permission": observed[name],
                    "parameters": sorted(PARAMETERS[name]),
                    "identity_fields": IDENTITY_FIELDS[name],
                    "default_fields": DEFAULT_FIELDS[name] or "ALL_OFFICIAL_DEFAULT_FIELDS",
                    "semantics": _semantics(name),
                } for name in names
            },
        }

    def query(self, request: TushareQueryRequest) -> dict[str, Any]:
        request = TushareQueryRequest.model_validate(request.model_dump())
        policy = self.config.endpoints.get(request.endpoint)
        if policy is None or not policy.enabled or policy.permission_status == "DENIED":
            raise _error("Tushare endpoint is not enabled", FailureClass.CAPABILITY_UNAVAILABLE)
        identity = request.identity()
        token = self.credential()
        auth_scope = sha256_bytes(token.encode())
        fingerprint = content_hash({
            "contract": "tushare-query-v2", "credential_scope": auth_scope, **identity,
        })
        permission_scope = "permission:" + auth_scope + ":" + request.endpoint
        if not request.refresh:
            cached = self._cached(fingerprint, policy.cache_ttl_seconds, request.max_rows)
            if cached is not None:
                return cached
        observed = self.state.get_cursor("tushare", permission_scope)
        if observed is not None:
            value = observed["value"]
            if (
                value.get("failure_class") in {"AUTH_REQUIRED", "ACCESS_RESTRICTED", "RATE_LIMITED"}
                and float(value.get("retry_at", 0)) > self.clock().timestamp()
            ):
                raise _error(
                    "Tushare endpoint is in observed cooldown",
                    FailureClass(value["failure_class"]),
                )
        snapshot_id: str | None = None
        try:
            with self.quota.reserve(request.endpoint, fingerprint):
                # Recheck after reservation: a just-completed peer may have filled the cache.
                if not request.refresh:
                    cached = self._cached(fingerprint, policy.cache_ttl_seconds, request.max_rows)
                    if cached is not None:
                        return cached
                body, status, retry_after = self._http_body({
                    "api_name": request.endpoint, "token": token,
                    "params": identity["params"], "fields": ",".join(identity["fields"]),
                })
                if token.encode() in body:
                    raise _error(
                        "Tushare response echoed authentication", FailureClass.INVALID_RESPONSE
                    )
                snapshot = self._snapshot(body, request.endpoint)
                snapshot_id = snapshot.snapshot_id
                payload = _decode(body, status, retry_after)
                rows = _rows(payload, request, identity["fields"])
                permission = "RESPONSE_ACCEPTED" if rows else "EMPTY_RESPONSE_ACCEPTED"
                result = {
                    "schema_version": "tushare-query-result-v1",
                    "source_id": "tushare", "provider_id": self.provider_id, "transport": "HTTP",
                    "request": identity, "request_fingerprint": fingerprint,
                    "rows": rows, "record_count": len(rows),
                    "source_snapshot_id": snapshot.snapshot_id,
                    "source_object_hash": snapshot.object_sha256,
                    "fetched_at": snapshot.fetched_at.isoformat(),
                    "available_to_system_at": snapshot.available_to_system_at.isoformat(),
                    "permission_observation": permission, "cache_status": "MISS",
                    "full_universe_proven": False, "formal_use_allowed": False,
                    "quota_scope": "THIS_PROJECT_STATESTORE; EXTERNAL_CLIENTS_NOT_CONTROLLED",
                    "coverage_status": "BOUNDED_RESPONSE_WITHOUT_COMPLETENESS_PROOF",
                    "semantics": _semantics(request.endpoint),
                }
                ref = self.objects.put_json(result)
                artifact_id = "TushareStructuredCapture:" + ref.sha256
                self.state.register_artifact(
                    artifact_id=artifact_id, artifact_type="TushareStructuredCapture",
                    schema_version="tushare-query-result-v1", object_hash=ref.sha256,
                    input_hashes=[snapshot.object_sha256],
                )
                self.state.set_cursor(
                    "tushare", "cache:" + fingerprint,
                    {"object_hash": ref.sha256, "fetched_epoch": self.clock().timestamp()},
                )
                self.state.set_cursor(
                    "tushare", permission_scope,
                    {"status": permission, "observed_at": self.clock().isoformat(),
                     "source_snapshot_id": snapshot.snapshot_id, "transport": "HTTP"},
                )
                result["artifact_id"] = artifact_id
                return result
        except ProviderError as exc:
            if snapshot_id is not None:
                exc.details["snapshot_id"] = snapshot_id
            cooldown = (
                max(60, int(exc.details.get("retry_after_seconds", 60)))
                if exc.failure_class is FailureClass.RATE_LIMITED else
                3600 if exc.failure_class in {
                    FailureClass.AUTH_REQUIRED, FailureClass.ACCESS_RESTRICTED,
                } else 0
            )
            self.state.set_cursor(
                "tushare", permission_scope,
                {"status": "FAILED", "failure_class": exc.failure_class.value,
                 "observed_at": self.clock().isoformat(),
                 "retry_at": self.clock().timestamp() + cooldown, "transport": "HTTP",
                 "source_snapshot_id": snapshot_id},
            )
            raise

    def _cached(self, fingerprint: str, ttl: int, maximum: int) -> dict[str, Any] | None:
        cached = self.state.get_cursor("tushare", "cache:" + fingerprint)
        if cached is None:
            return None
        value = cached["value"]
        age = self.clock().timestamp() - float(value["fetched_epoch"])
        if ttl <= 0 or not 0 <= age < ttl:
            return None
        digest = value["object_hash"]
        if not self.objects.verify(digest):
            return None
        result = json.loads(self.objects.get_bytes(digest))
        if result.get("request_fingerprint") != fingerprint:
            raise _error("Tushare cache identity mismatch", FailureClass.DATA_QUALITY)
        if len(result["rows"]) > maximum:
            raise _error("Tushare cached rows exceed the request budget", FailureClass.DATA_QUALITY)
        result["cache_status"] = "HIT"
        result["artifact_id"] = "TushareStructuredCapture:" + digest
        return result

    def _http_body(self, payload: dict[str, Any]) -> tuple[bytes, int, int | None]:
        from astock.core.network_ownership import owned_network_request

        with owned_network_request(
            self.state, self.provider_id, HTTP_ENDPOINT, timeout_seconds=self.timeout_seconds,
        ):
            return self._owned_http_body(payload)

    def _owned_http_body(self, payload: dict[str, Any]) -> tuple[bytes, int, int | None]:
        from astock.core.request_budget import current_request_budget

        started = time.monotonic()
        timeout = self.timeout_seconds
        budget = current_request_budget()
        if budget is not None:
            timeout = min(timeout, budget.remaining_seconds())
        configure_owned_http_client(self.client)
        try:
            with self.client.stream(
                "POST", HTTP_ENDPOINT, json=payload, follow_redirects=False,
                timeout=timeout,
            ) as response:
                size = response.headers.get("content-length")
                if size is not None and int(size) > self.config.max_response_bytes:
                    raise _error("Tushare response is oversized", FailureClass.INVALID_RESPONSE)
                content = bytearray()
                for chunk in response.iter_bytes():
                    if time.monotonic() - started > timeout:
                        raise _error("Tushare elapsed budget expired", FailureClass.TIMEOUT)
                    if len(content) + len(chunk) > self.config.max_response_bytes:
                        raise _error("Tushare response is oversized", FailureClass.INVALID_RESPONSE)
                    content.extend(chunk)
                retry = parse_retry_after_seconds(response.headers.get("Retry-After"))
                return bytes(content), response.status_code, retry
        except httpx.TimeoutException:
            raise _error("Tushare request timed out", FailureClass.TIMEOUT) from None
        except httpx.HTTPError:
            raise _error("Tushare network request failed", FailureClass.NETWORK) from None
        except (ValueError, UnicodeError):
            raise _error(
                "Tushare transport metadata is invalid", FailureClass.INVALID_RESPONSE
            ) from None

    def _snapshot(self, body: bytes, endpoint: str) -> SourceSnapshot:
        ref = self.objects.put_bytes(body)
        now = self.clock().astimezone(UTC)
        snapshot = SourceSnapshot(
            snapshot_id=f"{self.provider_id}:{ref.sha256}", source_id=self.provider_id,
            object_sha256=ref.sha256, fetched_at=now, available_to_system_at=now,
            source_url=HTTP_ENDPOINT + "#api=" + endpoint, mime="application/json",
            byte_size=ref.byte_size, headers_hash=content_hash({"transport": "HTTP_POST"}),
        )
        self.state.register_snapshot(snapshot)
        return snapshot


def _decode(body: bytes, status: int, retry: int | None) -> dict[str, Any]:
    if status == 429:
        raise _error(
            "Tushare HTTP quota rejected", FailureClass.RATE_LIMITED,
            retry_after_seconds=retry or 60,
        )
    if status == 401:
        raise _error("Tushare HTTP authentication rejected", FailureClass.AUTH_REQUIRED)
    if status == 403:
        raise _error("Tushare HTTP access rejected", FailureClass.ACCESS_RESTRICTED)
    if 300 <= status < 400:
        raise _error("Tushare redirects are not allowed", FailureClass.POLICY_REJECTED)
    if status >= 500:
        raise _error("Tushare upstream unavailable", FailureClass.NETWORK)
    if status != 200:
        raise _error("Tushare HTTP response invalid", FailureClass.INVALID_RESPONSE)
    try:
        payload = json.loads(body)
    except (ValueError, UnicodeError):
        raise _error("Tushare JSON is invalid", FailureClass.INVALID_RESPONSE) from None
    if not isinstance(payload, dict):
        raise _error("Tushare response must be an object", FailureClass.INVALID_RESPONSE)
    if type(payload.get("code")) is not int:
        raise _error("Tushare return code is invalid", FailureClass.INVALID_RESPONSE)
    if payload["code"] != 0:
        message = str(payload.get("msg", "")).lower()
        if any(word in message for word in (
            "每分钟", "每天最多", "访问频率", "rate limit", "频次",
        )):
            failure = FailureClass.RATE_LIMITED
        elif any(word in message for word in ("token", "认证", "凭证")):
            failure = FailureClass.AUTH_REQUIRED
        elif payload.get("code") == 2002 or any(word in message for word in ("权限", "积分")):
            failure = FailureClass.ACCESS_RESTRICTED
        else:
            failure = FailureClass.INVALID_RESPONSE
        raise _error("Tushare API rejected the request", failure, retry_after_seconds=60)
    return payload


def _rows(
    payload: dict[str, Any], request: TushareQueryRequest, requested_fields: list[str],
) -> list[dict[str, Any]]:
    data = payload.get("data")
    if not isinstance(data, dict):
        raise _error("Tushare data envelope is invalid", FailureClass.INVALID_RESPONSE)
    fields, items = data.get("fields"), data.get("items")
    if (
        not isinstance(fields, list) or not fields
        or not all(isinstance(item, str) and FIELD_NAME.fullmatch(item) for item in fields)
        or len(fields) != len(set(fields)) or len(fields) > 300
        or not set(IDENTITY_FIELDS[request.endpoint]).issubset(fields)
        or (requested_fields and not set(requested_fields).issubset(fields))
        or not isinstance(items, list) or len(items) > request.max_rows
    ):
        raise _error(
            "Tushare response columns or row budget changed", FailureClass.INVALID_RESPONSE
        )
    rows = []
    try:
        for item in items:
            if not isinstance(item, list) or len(item) != len(fields):
                raise ValueError("row shape")
            row = dict(zip(fields, item, strict=True))
            if any(
                isinstance(value, (dict, list, bool))
                or (isinstance(value, float) and not math.isfinite(value))
                for value in row.values()
            ):
                raise ValueError("non scalar or nonfinite cell")
            _validate_row(row, request)
            rows.append(row)
    except (ValueError, TypeError, KeyError, ArithmeticError):
        raise _error(
            "Tushare identity, date or numeric contract failed", FailureClass.DATA_QUALITY
        ) from None
    return rows


def _validate_row(row: dict[str, Any], request: TushareQueryRequest) -> None:
    params = request.params
    if request.endpoint not in {"cn_gdp", "cn_cpi"}:
        code = row["ts_code"]
        if not isinstance(code, str) or TS_CODE.fullmatch(code) is None:
            raise ValueError("security identity")
        if params.get("ts_code") and code != params["ts_code"]:
            raise ValueError("requested security identity")
    if request.endpoint == "stock_basic":
        if row["symbol"] != row["ts_code"][:6]:
            raise ValueError("symbol identity")
        for key in ("list_date", "delist_date"):
            if row.get(key):
                _date(row[key])
        if row.get("list_status") != params.get("list_status", "L"):
            raise ValueError("listing status")
        if params.get("exchange") and row.get("exchange") != params["exchange"]:
            raise ValueError("exchange filter")
    elif request.endpoint in MARKET:
        _finite_cells(row, {"ts_code", "trade_date"})
        day = _date(row["trade_date"])
        if params.get("trade_date") and day != _date(params["trade_date"]):
            raise ValueError("requested trade date")
        if params.get("start_date") and not (
            _date(params["start_date"]) <= day <= _date(params["end_date"])
        ):
            raise ValueError("trade date window")
        for key in ("open", "high", "low", "close", "vol", "amount"):
            if key in row:
                number = Decimal(str(row[key]))
                if (
                    not number.is_finite() or number < 0
                    or (key not in {"vol", "amount"} and number == 0)
                ):
                    raise ValueError("invalid market number")
        if all(key in row for key in ("open", "high", "low", "close")):
            op, high, low, close = (
                Decimal(str(row[key])) for key in ("open", "high", "low", "close")
            )
            if high < max(op, low, close) or low > min(op, high, close):
                raise ValueError("OHLC")
    elif request.endpoint in FINANCIAL:
        end = _date(row["end_date"])
        if params.get("period") and end != _date(params["period"]):
            raise ValueError("report period")
        announcement = row.get("f_ann_date") or row.get("ann_date")
        if announcement:
            published = _date(announcement)
            if published < end:
                raise ValueError("announcement before report period")
        for name in ("ann_date", "f_ann_date"):
            if row.get(name):
                _date(row[name])
        if params.get("report_type") and str(row["report_type"]) != params["report_type"]:
            raise ValueError("report type")
        for key in ("ann_date", "f_ann_date"):
            if params.get(key) and row.get(key) != params[key]:
                raise ValueError("requested announcement date")
        if params.get("start_date") and row.get("ann_date") and not (
            _date(params["start_date"]) <= _date(row["ann_date"]) <= _date(params["end_date"])
        ):
            raise ValueError("announcement date window")
        _finite_cells(row, set(IDENTITY_FIELDS[request.endpoint]))
        # Preserve every update/report type: never collapse restatements to one end_date.
    else:
        key, token = ("quarter", "q") if request.endpoint == "cn_gdp" else ("month", "m")
        period = str(row[key])
        _finite_cells(row, {key})
        _validate_macro_window({token: period}, token)
        if params.get(token) and period != params[token]:
            raise ValueError("macro period")
        if params.get("start_" + token) and not (
            params["start_" + token] <= period <= params["end_" + token]
        ):
            raise ValueError("macro period window")


def _finite_cells(row: dict[str, Any], identifiers: set[str]) -> None:
    for key, value in row.items():
        if key not in identifiers and value is not None:
            if not Decimal(str(value)).is_finite():
                raise ValueError("nonfinite structured number")


def _semantics(endpoint: str) -> dict[str, Any]:
    common: dict[str, Any] = {
        "authority": "SECONDARY_STRUCTURED", "issuer_original_document": False,
        "null_is_not_zero": True, "source_units_preserved": True,
    }
    if endpoint == "daily":
        common.update(
            price="CNY_UNADJUSTED", vol="LOT_100_SHARES", amount="THOUSAND_CNY",
            pct_chg="PERCENT",
            pre_close="SOURCE_EX_RIGHTS_REFERENCE; DO_NOT_USE_AS_RAW_PREVIOUS_CLOSE",
            trade_date="DATE_ONLY; NOT_INTRADAY_PUBLICATION_TIMESTAMP",
        )
    elif endpoint in {"weekly", "monthly"}:
        common.update(
            price="CNY_UNADJUSTED",
            vol="SOURCE_NATIVE_UNVERIFIED", amount="SOURCE_NATIVE_UNVERIFIED",
            pct_chg=(
                "DECIMAL_RETURN" if endpoint == "weekly"
                else "SOURCE_NATIVE_UNVERIFIED; CURRENT_SAMPLES_DECIMAL_RETURN"
            ),
            pre_close="PREVIOUS_PERIOD_CLOSE",
            trade_date="SOURCE_PERIOD_END_DATE_ONLY; NOT_PUBLICATION_TIMESTAMP",
            unit_evidence=(
                "CURRENT_SAMPLES_DIFFER_FROM_DAILY; USE_RECORDED_CROSS_PERIOD_RECONCILIATION"
            ),
            historical_unit_contract_verified=False,
            automatic_unit_conversion=False,
        )
    elif endpoint == "daily_basic":
        common.update(
            total_share="TEN_THOUSAND_SHARES", float_share="TEN_THOUSAND_SHARES",
            free_share="TEN_THOUSAND_SHARES", total_mv="TEN_THOUSAND_CNY",
            circ_mv="TEN_THOUSAND_CNY", ratios="PERCENT_OR_SOURCE_RATIO_AS_DOCUMENTED",
        )
    elif endpoint in FINANCIAL:
        common.update(
            currency="SOURCE_CURRENCY; VERIFY_FIELD_DOCUMENTATION",
            total_share="SHARES" if endpoint == "balancesheet" else "FIELD_SPECIFIC",
            publication="F_ANN_DATE_OR_ANN_DATE; DATE_ONLY; SAME_DAY_NOT_INTRADAY_SAFE",
            revisions="ALL_RETURNED_REPORT_TYPES_AND_UPDATE_FLAGS_RETAINED",
            report_period="END_DATE_IS_NOT_PUBLICATION_DATE",
            historical_point_in_time_verified=False,
        )
    elif endpoint == "stock_basic":
        common.update(
            temporal_scope="CURRENT_SNAPSHOT; NOT_A_HISTORICAL_SURVIVORSHIP_FREE_UNIVERSE",
        )
    elif endpoint == "cn_gdp":
        common.update(
            gdp="CUMULATIVE_100_MILLION_CNY", growth="PERCENT",
            publication="UNKNOWN; OBSERVATION_QUARTER_IS_NOT_PUBLICATION_DATE",
            vintage_history_verified=False,
        )
    else:
        common.update(
            value="SOURCE_INDEX_LEVEL", growth="PERCENT",
            publication="UNKNOWN; OBSERVATION_MONTH_IS_NOT_PUBLICATION_DATE",
            vintage_history_verified=False,
        )
    return common
