"""Credential-safe Streamable HTTP initialize/tools-list probe; never queries market data."""
from __future__ import annotations

import json
import re
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from astock.core.errors import FailureClass, ProviderError
from astock.core.hashing import content_hash
from astock.providers.tushare_structured import TushareStructuredProvider, _error

_PROTOCOL = "2025-03-26"
_MAX_BYTES = 262144


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _response_json(response: Any, expected_id: int | None, deadline: float) -> dict[str, Any]:
    if expected_id is None:
        return {}
    if "text/event-stream" not in response.headers.get("Content-Type", "").lower():
        body = response.read(_MAX_BYTES + 1)
        if len(body) > _MAX_BYTES:
            raise ValueError("MCP response oversized")
        value = json.loads(body)
    else:
        size, event = 0, []
        value = None
        while size <= _MAX_BYTES and time.monotonic() < deadline:
            line = response.readline(_MAX_BYTES + 1)
            size += len(line)
            if not line:
                break
            text = line.decode("utf-8").rstrip("\r\n")
            if text.startswith("data:"):
                event.append(text[5:].lstrip())
            elif not text and event:
                candidate = json.loads("\n".join(event))
                event = []
                if isinstance(candidate, dict) and candidate.get("id") == expected_id:
                    value = candidate
                    break
        if size > _MAX_BYTES or value is None:
            raise ValueError("MCP event did not contain a bounded matching response")
    if (
        not isinstance(value, dict) or value.get("jsonrpc") != "2.0"
        or value.get("id") != expected_id or "error" in value
        or not isinstance(value.get("result"), dict)
    ):
        raise ValueError("MCP response contract mismatch")
    return value["result"]


def probe_tushare_mcp(provider: TushareStructuredProvider) -> dict[str, Any]:
    """Three control messages plus session close; HTTP data remains the only query route."""
    token = provider.credential()
    encoded = quote(token, safe="")
    url = "https://api.tushare.pro/mcp/token=" + encoded
    opener = build_opener(_NoRedirect())
    deadline = time.monotonic() + provider.timeout_seconds
    session: str | None = None
    negotiated = _PROTOCOL
    messages = 0

    def call(
        method: str, request_id: int | None, params: dict[str, Any] | None = None,
        *, http_method: str = "POST",
    ) -> dict[str, Any]:
        nonlocal session, messages
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise _error("Tushare MCP elapsed budget expired", FailureClass.TIMEOUT)
        payload: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if request_id is not None:
            payload["id"] = request_id
        if params is not None:
            payload["params"] = params
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": negotiated,
        }
        if session is not None:
            headers["Mcp-Session-Id"] = session
        fingerprint = content_hash({"transport": "MCP_CONTROL", "method": method})
        with provider.quota.reserve("__mcp_control__", fingerprint):
            request = Request(
                url, data=json.dumps(payload).encode() if http_method == "POST" else None,
                headers=headers, method=http_method,
            )
            try:
                with opener.open(request, timeout=min(remaining, 10)) as response:
                    messages += 1
                    returned_session = response.headers.get("Mcp-Session-Id")
                    if returned_session is not None:
                        if (
                            len(returned_session) > 1024
                            or "\r" in returned_session or "\n" in returned_session
                        ):
                            raise ValueError("Invalid MCP session header")
                        session = returned_session
                    result = _response_json(response, request_id, deadline)
                    serialized = json.dumps(result, ensure_ascii=False)
                    if token in serialized or encoded in serialized:
                        raise ValueError("MCP echoed authentication")
                    return result
            except HTTPError as exc:
                if http_method == "DELETE" and exc.code in {404, 405}:
                    exc.close()
                    return {}
                status = exc.code
                location = exc.headers.get("Location", "")
                target = urlsplit(urljoin(url, location)) if location else None
                original = urlsplit(url)
                safe_metadata = {
                    "status_code": status, "redirect_present": bool(location),
                    "redirect_same_origin": bool(
                        target and target.scheme == "https" and target.netloc == original.netloc
                    ),
                    "canonical_trailing_slash": bool(
                        target and target.scheme == "https" and target.netloc == original.netloc
                        and not target.query and target.path != original.path
                        and target.path in {original.path + "/", original.path.rstrip("/")}
                    ),
                }
                failure = (
                    FailureClass.RATE_LIMITED if status == 429 else
                    FailureClass.AUTH_REQUIRED if status == 401 else
                    FailureClass.ACCESS_RESTRICTED if status == 403 else
                    FailureClass.POLICY_REJECTED if 300 <= status < 400 else
                    FailureClass.CAPABILITY_UNAVAILABLE
                )
                exc.close()
                raise _error(
                    "Tushare MCP HTTP capability unavailable", failure, **safe_metadata
                ) from None
            except TimeoutError:
                raise _error("Tushare MCP timed out", FailureClass.TIMEOUT) from None
            except URLError:
                raise _error("Tushare MCP connection failed", FailureClass.NETWORK) from None
            except (ValueError, UnicodeError, TypeError):
                raise _error(
                    "Tushare MCP contract invalid", FailureClass.INVALID_RESPONSE
                ) from None

    try:
        initialized = call("initialize", 1, {
            "protocolVersion": _PROTOCOL, "capabilities": {},
            "clientInfo": {"name": "AStockMultiAgent", "version": "1"},
        })
        protocol = initialized.get("protocolVersion")
        if (
            not isinstance(protocol, str)
            or re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", protocol) is None
        ):
            raise _error("Tushare MCP protocol negotiation invalid", FailureClass.INVALID_RESPONSE)
        negotiated = protocol
        call("notifications/initialized", None)
        listing = call("tools/list", 2, {})
        tools = listing.get("tools")
        if not isinstance(tools, list) or len(tools) > 1000:
            raise _error("Tushare MCP tool list invalid", FailureClass.INVALID_RESPONSE)
        names = []
        for tool in tools:
            name = tool.get("name") if isinstance(tool, dict) else None
            if not isinstance(name, str) or re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", name) is None:
                raise _error("Tushare MCP tool identity invalid", FailureClass.INVALID_RESPONSE)
            names.append(name)
        result = {
            "schema_version": "tushare-mcp-probe-v1", "status": "CAPABILITY_AVAILABLE",
            "source_id": "tushare", "transport": "STREAMABLE_HTTP",
            "endpoint": "https://api.tushare.pro/mcp/<redacted>",
            "protocol_version": negotiated, "tools": sorted(names),
            "tool_count": len(names), "tools_list_complete": not bool(listing.get("nextCursor")),
            "data_queries_executed": 0, "market_endpoint_permissions_proven": False,
            "shared_control_quota": True, "mcp_data_query_route_enabled": False,
            "quota_scope": "THIS_PROJECT_STATESTORE; EXTERNAL_CLIENTS_NOT_CONTROLLED",
            "observed_at": provider.clock().isoformat(),
        }
    finally:
        if session is not None and deadline > time.monotonic():
            try:
                call("close", None, http_method="DELETE")
            except ProviderError:
                pass
    result["control_messages"] = messages
    ref = provider.objects.put_json(result)
    artifact_id = "TushareMcpCapabilityProbe:" + ref.sha256
    provider.state.register_artifact(
        artifact_id=artifact_id, artifact_type="TushareMcpCapabilityProbe",
        schema_version="tushare-mcp-probe-v1", object_hash=ref.sha256, input_hashes=[],
    )
    result["artifact_id"] = artifact_id
    return result
