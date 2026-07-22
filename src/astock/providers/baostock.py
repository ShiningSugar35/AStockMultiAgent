"""BaoStock 0.8.9 reference adapter with immutable raw SDK envelopes."""

from __future__ import annotations

import importlib
import json
import re
import threading
import time as monotonic_time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

from pydantic import ValidationError

from astock.core.hashing import canonical_json_bytes, content_hash
from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.providers.symbols import baostock_code
from astock.schemas import BaoStockRawEnvelopeV1, FetchStatus, Market, SourceSnapshot

_SESSION_LOCK = threading.Lock()
_ALLOWED_REQUEST_KEYS = {"symbol", "market", "start", "end", "exchange", "adjustflag"}
_LEASE_KEY = "provider:baostock-reference:sdk-session"
_LEASE_TTL = timedelta(seconds=30)
_LEASE_WAIT_SECONDS = 30.0
_LEASE_HEARTBEAT_SECONDS = 5.0


class BaoStockCaptureError(ValueError):
    """A strict envelope failure whose immutable raw snapshot was retained."""

    def __init__(self, failure_code: str, snapshot: SourceSnapshot) -> None:
        super().__init__(failure_code)
        self.failure_code = failure_code
        self.snapshot = snapshot


class BaoStockReferenceProvider:
    provider_id = "baostock-reference"
    sdk_version = "0.8.9"
    mime = "application/vnd.astock.baostock-envelope+json"

    def __init__(self, objects: ObjectStore, state: StateStore, fixture_root: Path) -> None:
        self.objects = objects
        self.state = state
        self.fixture_root = fixture_root.resolve()

    def fetch(
        self,
        capability: str,
        request: dict[str, str],
        *,
        live: bool = False,
    ) -> tuple[BaoStockRawEnvelopeV1, SourceSnapshot]:
        safe_request = _validate_request(request)
        raw_envelope = (
            self._fetch_live_raw(capability, safe_request)
            if live
            else self._fetch_recorded_raw(capability, safe_request)
        )
        raw = canonical_json_bytes(raw_envelope)
        object_ref = self.objects.put_bytes(raw)
        snapshot_id = f"{self.provider_id}:{object_ref.sha256}"
        finished = _safe_timestamp(raw_envelope.get("request_finished_at"))
        rows = raw_envelope.get("rows")
        complete = raw_envelope.get("complete") is True
        status = FetchStatus.SUCCEEDED if complete else (
            FetchStatus.PARTIAL if isinstance(rows, list) and rows else FetchStatus.FETCH_FAILED
        )
        snapshot = SourceSnapshot(
            created_at=finished,
            snapshot_id=snapshot_id,
            source_id=self.provider_id,
            object_sha256=object_ref.sha256,
            fetched_at=finished,
            available_to_system_at=finished,
            source_url=f"baostock://{capability}",
            mime=self.mime,
            byte_size=object_ref.byte_size,
            headers_hash=content_hash({"sdk_version": self.sdk_version, "capability": capability}),
            fetch_status=status,
            rights_status="PUBLIC_REFERENCE_DATA",
        )
        self.state.register_snapshot(snapshot)
        try:
            envelope = BaoStockRawEnvelopeV1.model_validate(raw_envelope)
        except ValidationError as exc:
            raise BaoStockCaptureError("BAOSTOCK_RAW_ENVELOPE_INVALID", snapshot) from exc
        return envelope, snapshot

    def _fetch_recorded_raw(
        self, capability: str, request: dict[str, str]
    ) -> dict[str, Any]:
        name = capability.replace(".", "_") + ".json"
        path = (self.fixture_root / name).resolve()
        if not path.is_relative_to(self.fixture_root) or not path.is_file():
            raise ValueError("Missing recorded BaoStock fixture")
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("Recorded BaoStock fixture root must be an object")
        payload["request"] = request
        payload["created_at"] = payload.get("request_finished_at", datetime.now(UTC).isoformat())
        if (
            isinstance(payload.get("row_contexts"), list)
            and all(not item for item in payload["row_contexts"])
            and isinstance(payload.get("rows"), list)
        ):
            payload["row_contexts"] = [{} for _ in payload["rows"]]
        return payload

    def _fetch_live_raw(self, capability: str, request: dict[str, str]) -> dict[str, Any]:
        owner = uuid4().hex
        token: int | None = None
        deadline = monotonic_time.monotonic() + _LEASE_WAIT_SECONDS
        with _SESSION_LOCK:
            while token is None and monotonic_time.monotonic() < deadline:
                now = datetime.now(UTC)
                token = self.state.acquire_reference_provider_lease(
                    _LEASE_KEY,
                    owner,
                    now=now,
                    lease_until=now + _LEASE_TTL,
                )
                if token is None:
                    threading.Event().wait(0.05)
            started = datetime.now(UTC)
            if token is None:
                return _failure_envelope(
                    self.sdk_version,
                    capability,
                    request,
                    started,
                    "LEASE_BUSY",
                )

            def renew_once() -> bool:
                now = datetime.now(UTC)
                return self.state.renew_reference_provider_lease(
                    _LEASE_KEY,
                    owner,
                    token,
                    now=now,
                    lease_until=now + _LEASE_TTL,
                )

            heartbeat_stop = threading.Event()
            heartbeat_lost = threading.Event()

            def heartbeat() -> None:
                while not heartbeat_stop.wait(_LEASE_HEARTBEAT_SECONDS):
                    try:
                        if not renew_once():
                            heartbeat_lost.set()
                            return
                    except Exception:
                        heartbeat_lost.set()
                        return

            heartbeat_thread = threading.Thread(
                target=heartbeat,
                name="astock-baostock-lease-heartbeat",
                daemon=True,
            )
            heartbeat_thread.start()

            def renew() -> bool:
                if heartbeat_lost.is_set():
                    return False
                try:
                    renewed = renew_once()
                except Exception:
                    heartbeat_lost.set()
                    return False
                if not renewed:
                    heartbeat_lost.set()
                return renewed

            try:
                return self._run_sdk_raw(capability, request, started, renew)
            finally:
                heartbeat_stop.set()
                heartbeat_thread.join(timeout=_LEASE_HEARTBEAT_SECONDS + 1.0)
                self.state.release_reference_provider_lease(
                    _LEASE_KEY,
                    owner,
                    token,
                    now=datetime.now(UTC),
                )

    def _run_sdk_raw(
        self,
        capability: str,
        request: dict[str, str],
        started: datetime,
        renew: Callable[[], bool],
    ) -> dict[str, Any]:
        login_code = "IMPORT_FAILED"
        login_message = "SDK import failed"
        result_code = "NOT_RUN"
        result_message = "query was not run"
        fields: list[str] = []
        rows: list[list[str]] = []
        row_contexts: list[dict[str, str]] = []
        complete = False
        bs: Any | None = None
        login_attempted = False
        try:
            if not renew():
                raise _SdkCaptureFailure("LEASE_LOST", "provider lease was lost")
            try:
                bs = importlib.import_module("baostock")
            except Exception as exc:
                raise _SdkCaptureFailure("IMPORT_FAILED", _exception_class(exc)) from exc
            try:
                login_attempted = True
                login = bs.login()
                login_code = str(login.error_code)
                login_message = _safe_message(login.error_msg)
            except Exception as exc:
                raise _SdkCaptureFailure("LOGIN_EXCEPTION", _exception_class(exc)) from exc
            if login_code != "0":
                result_code = "LOGIN_FAILED"
                result_message = "login did not succeed"
            else:
                complete = True
                for query, context in _query_specs(bs, capability, request):
                    if not renew():
                        raise _SdkCaptureFailure("LEASE_LOST", "provider lease was lost")
                    try:
                        result = query()
                    except Exception as exc:
                        raise _SdkCaptureFailure("QUERY_EXCEPTION", _exception_class(exc)) from exc
                    try:
                        result_code = str(result.error_code)
                        result_message = _safe_message(result.error_msg)
                        current_fields = [str(item) for item in result.fields]
                    except Exception as exc:
                        raise _SdkCaptureFailure(
                            "RESULT_SHAPE_EXCEPTION", _exception_class(exc)
                        ) from exc
                    if fields and current_fields != fields:
                        complete = False
                        result_code = "FIELD_DRIFT"
                        result_message = "SDK field layout changed between queries"
                        break
                    fields = current_fields
                    if result_code != "0":
                        complete = False
                        break
                    while True:
                        if not renew():
                            raise _SdkCaptureFailure("LEASE_LOST", "provider lease was lost")
                        try:
                            has_next = bool(result.next())
                        except Exception as exc:
                            raise _SdkCaptureFailure(
                                "NEXT_EXCEPTION", _exception_class(exc)
                            ) from exc
                        if not has_next:
                            break
                        try:
                            row = [str(item) for item in result.get_row_data()]
                        except Exception as exc:
                            raise _SdkCaptureFailure(
                                "GET_ROW_EXCEPTION", _exception_class(exc)
                            ) from exc
                        rows.append(row)
                        row_contexts.append(context)
                        if len(row) != len(fields):
                            raise _SdkCaptureFailure("ROW_WIDTH_MISMATCH", "SDK row width changed")
                    try:
                        result_code = str(result.error_code)
                        result_message = _safe_message(result.error_msg)
                    except Exception as exc:
                        raise _SdkCaptureFailure(
                            "RESULT_FINALIZE_EXCEPTION", _exception_class(exc)
                        ) from exc
                    if result_code != "0":
                        complete = False
                        break
        except _SdkCaptureFailure as exc:
            complete = False
            result_code = exc.code
            result_message = exc.safe_message
        finally:
            if bs is not None and login_attempted:
                try:
                    bs.logout()
                except Exception as exc:
                    complete = False
                    result_code = "LOGOUT_FAILED"
                    result_message = _exception_class(exc)
        if not renew():
            complete = False
            result_code = "LEASE_LOST"
            result_message = "provider lease was lost"
        return {
            "schema_version": "baostock-raw-envelope-v1",
            "created_at": datetime.now(UTC).isoformat(),
            "sdk_version": self.sdk_version,
            "capability": capability,
            "request": request,
            "request_started_at": started.isoformat(),
            "request_finished_at": datetime.now(UTC).isoformat(),
            "login_error_code": login_code,
            "login_error_message": login_message,
            "result_error_code": result_code,
            "result_error_message": result_message,
            "fields": fields,
            "rows": rows,
            "row_contexts": row_contexts,
            "complete": complete,
        }


class _SdkCaptureFailure(Exception):
    def __init__(self, code: str, safe_message: str) -> None:
        super().__init__(code)
        self.code = code
        self.safe_message = safe_message


def _query_specs(
    bs: Any, capability: str, request: dict[str, str]
) -> list[tuple[Callable[[], Any], dict[str, str]]]:
    symbol = request.get("symbol")
    market = Market(request["market"]) if request.get("market") else None
    code = baostock_code(symbol, market) if symbol and market else None
    if capability == "instrument.master":
        return [(lambda: bs.query_stock_basic(code=code or ""), {})]
    if capability == "market.calendar":
        return [(
            lambda: bs.query_trade_dates(start_date=request["start"], end_date=request["end"]),
            {},
        )]
    if capability == "market.daily_unadjusted":
        if request.get("adjustflag", "3") != "3":
            raise _SdkCaptureFailure("INVALID_QUERY", "daily query requires adjustflag=3")
        if not code:
            raise _SdkCaptureFailure("INVALID_QUERY", "daily query requires an instrument")
        return [(
            lambda: bs.query_history_k_data_plus(
                code,
                "date,code,open,high,low,close,preclose,volume,amount,adjustflag,tradestatus,isST",
                start_date=request["start"],
                end_date=request["end"],
                frequency="d",
                adjustflag="3",
            ),
            {},
        )]
    if capability == "corporate_actions.structured_hint":
        if not code:
            raise _SdkCaptureFailure(
                "INVALID_QUERY", "corporate-action query requires an instrument"
            )
        start_year = int(request["start"][:4])
        end_year = int(request["end"][:4])
        return [
            (
                lambda year=year: bs.query_dividend_data(
                    code=code, year=str(year), yearType="report"
                ),
                {"report_period": str(year)},
            )
            for year in range(start_year, end_year + 1)
        ]
    raise _SdkCaptureFailure("UNSUPPORTED_CAPABILITY", "unsupported capability")


def _failure_envelope(
    sdk_version: str,
    capability: str,
    request: dict[str, str],
    started: datetime,
    code: str,
) -> dict[str, Any]:
    finished = datetime.now(UTC)
    return {
        "schema_version": "baostock-raw-envelope-v1",
        "created_at": finished.isoformat(),
        "sdk_version": sdk_version,
        "capability": capability,
        "request": request,
        "request_started_at": started.isoformat(),
        "request_finished_at": finished.isoformat(),
        "login_error_code": code,
        "login_error_message": "provider session unavailable",
        "result_error_code": "NOT_RUN",
        "result_error_message": "query was not run",
        "fields": [],
        "rows": [],
        "row_contexts": [],
        "complete": False,
    }


def _safe_timestamp(value: object) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is not None:
            return parsed.astimezone(UTC)
    except ValueError:
        pass
    return datetime.now(UTC)


def _validate_request(request: dict[str, str]) -> dict[str, str]:
    if set(request).difference(_ALLOWED_REQUEST_KEYS):
        raise ValueError("BaoStock request contains a forbidden field")
    result = {str(key): str(value) for key, value in request.items()}
    for value in result.values():
        if len(value) > 64 or re.search(r"(?i)(cookie|token|secret|password|profile)", value):
            raise ValueError("Unsafe BaoStock request value")
    return result


def _safe_message(value: object) -> str:
    text = str(value)[:256]
    return re.sub(
        r"(?i)(token|cookie|secret|password)\s*[=:]\s*[^\s;,]+",
        r"\1=[REDACTED]",
        text,
    )


def _exception_class(exc: Exception) -> str:
    return type(exc).__name__[:128]


__all__ = ["BaoStockCaptureError", "BaoStockReferenceProvider"]
