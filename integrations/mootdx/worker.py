"""Fixed Mootdx TCP worker. The application owns its process tree and deadline.

This file runs only in runtime/tool-environments/mootdx, never the main interpreter.
Requests select data operations, not Python modules, executables or filesystem paths.
"""
from __future__ import annotations

import contextlib
import importlib.util
import ipaddress
import json
import os
import sys
import time
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SDK_OPERATIONS = {"quote", "bars"}
CATEGORIES = {"5m": 0, "60m": 3}


def _now() -> str:
    return datetime.now(UTC).isoformat()


class PaginationCycleError(RuntimeError):
    """A bounded page request repeated data instead of advancing."""


def _failure(exc: BaseException) -> str:
    cause = getattr(exc, "original_exception", exc)
    if isinstance(cause, (ImportError, PackageNotFoundError)):
        return "CAPABILITY_UNAVAILABLE"
    if isinstance(cause, PaginationCycleError):
        return "PAGINATION_CYCLE"
    if isinstance(cause, TimeoutError) or "timeout" in type(cause).__name__.lower():
        return "TIMEOUT"
    if isinstance(cause, (OSError, ConnectionError)) or "Connection" in type(cause).__name__:
        return "NETWORK"
    return "INVALID_RESPONSE"


def _validate(request: object) -> dict:
    if not isinstance(request, dict) or request.get("operation") not in SDK_OPERATIONS:
        raise ValueError("invalid SDK operation")
    symbol = request.get("symbol")
    if (
        not isinstance(symbol, str) or len(symbol) != 6
        or not symbol.isascii() or not symbol.isdigit()
    ):
        raise ValueError("invalid symbol")
    if request.get("market") not in (0, 1):
        raise ValueError("only explicitly mapped Shanghai/Shenzhen is supported")
    if request["operation"] == "bars" and request.get("frequency") not in CATEGORIES:
        raise ValueError("only raw 5m/60m bars are supported")
    if not 0 < float(request["timeout_seconds"]) <= 120:
        raise ValueError("invalid timeout")
    if not 0 < float(request["connect_timeout_seconds"]) <= float(request["timeout_seconds"]):
        raise ValueError("invalid connection timeout")
    if not 1 <= int(request["page_size"]) <= 800 or not 1 <= int(request["max_pages"]) <= 32:
        raise ValueError("invalid bounded pagination")
    if request.get("protocol", "STANDARD") not in {"STANDARD", "MAC_COMPAT"}:
        raise ValueError("invalid protocol selection")
    if not 1024 <= int(request["max_output_bytes"]) <= 16777216:
        raise ValueError("invalid output byte budget")
    servers = request.get("servers")
    if not isinstance(servers, list) or not 1 <= len(servers) <= 8:
        raise ValueError("invalid server list")
    for server in servers:
        if (
            not ipaddress.ip_address(server["host"]).is_global
            or not 1 <= int(server["port"]) <= 65535
        ):
            raise ValueError("non-public server rejected")
    return request


def _sdk_rows(value: object) -> list[dict]:
    """Validate the SDK's unannotated result before using it as market rows."""
    if value is None:
        raise ConnectionError("SDK returned no response")
    if not isinstance(value, list) or any(not isinstance(row, dict) for row in value):
        raise ValueError("SDK response must be a list of row objects")
    return value


def capture(request: dict) -> dict:
    started = _now()
    end = time.monotonic() + float(request["timeout_seconds"])
    # Path.home() is evaluated by Mootdx at import time. Only this child receives
    # a project-owned USERPROFILE; no system/parent home configuration is changed.
    home = Path(os.environ.get("USERPROFILE", "")).resolve()
    if not home.is_relative_to(ROOT / ".scratchpad") or Path.home().resolve() != home:
        raise ValueError("Mootdx profile must be an owned project scratch directory")
    settings = home / ".mootdx"
    settings.mkdir(parents=True, exist_ok=True)
    servers = request["servers"]
    (settings / "config.json").write_text(json.dumps({
        "SERVER": {"HQ": [["configured", item["host"], item["port"]] for item in servers],
                   "EX": [], "GP": []},
        "BESTIP": {"HQ": [servers[0]["host"], servers[0]["port"]], "EX": "", "GP": ""},
        "TDXDIR": str(settings),
    }), encoding="utf-8")

    from mootdx.quotes import Quotes, StdQuotes

    compatibility = None
    if request.get("protocol") == "MAC_COMPAT":
        spec = importlib.util.spec_from_file_location(
            "astock_mootdx_compat", ROOT / "integrations/mootdx/transport_compat.py"
        )
        if spec is None or spec.loader is None:
            raise ValueError("fixed compatibility module unavailable")
        compatibility = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(compatibility)
        compatibility.install()

    envelope = {
        "schema_version": "mootdx-raw-v1", "capture": "SDK_RETURN_ENVELOPE",
        "sdk": "mootdx", "sdk_version": version("mootdx"),
        "transport_sdk": "tdxpy", "transport_sdk_version": version("tdxpy"),
        "protocol": request.get("protocol", "STANDARD"),
        "transport_patch": compatibility.PATCH_VERSION if compatibility else None,
        "transport_patch_source": compatibility.UPSTREAM_COMMIT if compatibility else None,
        "upstream": "TDX", "adjustment_mode": "NONE",
        "request": request, "request_started_at": started,
        "attempts": [], "rows": [], "complete": False,
    }
    for server in servers:
        if time.monotonic() >= end:
            envelope["failure_class"] = "TIMEOUT"
            break
        client = None
        raw_socket = None
        attempt = {"server": server, "started_at": _now(), "pages": [], "row_count": 0}
        envelope["attempts"].append(attempt)
        if compatibility is not None:
            attempt["wire_frame_start"] = len(compatibility.FRAMES)
        try:
            remaining = end - time.monotonic()
            client = Quotes.factory(
                market="std", server=(server["host"], int(server["port"])),
                timeout=min(float(request["connect_timeout_seconds"]), remaining),
                bestip=False, heartbeat=False, auto_retry=False, raise_exception=True,
                multithread=False, quiet=True,
            )
            raw_socket = client.client.client
            if not isinstance(client, StdQuotes):
                raise ValueError("Mootdx factory returned a nonstandard market session")
            if raw_socket is None:
                raise ConnectionError("SDK did not establish a connection")
            rows = []
            seen_pages = set()
            complete = False
            stop_reason = "PAGE_BUDGET"
            count = int(request["page_size"])
            for page in range(1 if request["operation"] == "quote" else int(request["max_pages"])):
                remaining = end - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("request deadline")
                raw_socket.settimeout(min(float(request["connect_timeout_seconds"]), remaining))
                if request["operation"] == "quote":
                    result = _sdk_rows(client.client.get_security_quotes([
                        (request["market"], request["symbol"])
                    ]))
                else:
                    result = _sdk_rows(client.client.get_security_bars(
                        CATEGORIES[request["frequency"]], request["market"], request["symbol"],
                        page * count, count,
                    ))
                signature = json.dumps(result, sort_keys=True, allow_nan=False)
                if result and signature in seen_pages:
                    raise PaginationCycleError("Mootdx repeated a page")
                seen_pages.add(signature)
                attempt["pages"].append({"offset": page * count, "rows": result})
                rows.extend(result)
                attempt["row_count"] = len(rows)
                if not result:
                    stop_reason = "EMPTY" if not rows else "HISTORY_EXHAUSTED"
                    complete = bool(rows)
                    break
                if request["operation"] == "quote":
                    complete, stop_reason = True, "EXACT_QUOTE"
                    break
                oldest = min(str(row["datetime"]) for row in result)
                start = datetime.fromisoformat(request["requested_start"])
                # SDK labels are Shanghai wall time. Parent performs authoritative
                # numeric/identity/time normalization after storing this raw envelope.
                from zoneinfo import ZoneInfo
                start_local = start.astimezone(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M")
                if oldest <= start_local:
                    complete, stop_reason = True, "WINDOW_START_REACHED"
                    break
                if len(result) < count:
                    complete, stop_reason = True, "HISTORY_EXHAUSTED"
                    break
            attempt.update({"stop_reason": stop_reason, "finished_at": _now()})
            if rows:
                envelope.update({
                    "rows": rows, "server": server, "complete": complete,
                    "stop_reason": stop_reason,
                })
                break
            attempt["failure_class"] = "CAPABILITY_UNAVAILABLE"
            envelope["failure_class"] = "CAPABILITY_UNAVAILABLE"
        except Exception as exc:
            attempt.update({"failure_class": _failure(exc), "error_type": type(exc).__name__,
                            "finished_at": _now()})
            envelope["failure_class"] = attempt["failure_class"]
            if attempt["pages"] and attempt["row_count"]:
                envelope.update({
                    "rows": [row for page in attempt["pages"] for row in page["rows"]],
                    "server": server, "complete": False, "stop_reason": "TRANSPORT_PARTIAL",
                })
                break
        finally:
            if compatibility is not None:
                attempt["wire_frame_end"] = len(compatibility.FRAMES)
            if client is not None:
                try:
                    client.close()
                except Exception:
                    attempt["sdk_close_failed"] = True
            # tdxpy's disconnect can throw before closing; own the socket explicitly.
            if raw_socket is not None:
                raw_socket.close()
    if compatibility is not None:
        envelope["wire_frames"] = compatibility.FRAMES
    envelope["request_finished_at"] = _now()
    if envelope["rows"] and envelope.get("failure_class") != "PAGINATION_CYCLE":
        envelope.pop("failure_class", None)
    return envelope


def main() -> int:
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if callable(reconfigure):
        reconfigure(encoding="utf-8")
    try:
        raw = sys.stdin.buffer.read(65537)
        if len(raw) > 65536:
            raise ValueError("request too large")
        request = _validate(json.loads(raw))
        with open(os.devnull, "w", encoding="utf-8") as sink:
            with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
                result = capture(request)
        text = json.dumps(result, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        if len(text.encode("utf-8")) > int(request["max_output_bytes"]):
            raise ValueError("SDK response exceeds output budget")
        print(text)
        return 0
    except Exception as exc:
        print(json.dumps({"failure_class": _failure(exc), "error_type": type(exc).__name__}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
