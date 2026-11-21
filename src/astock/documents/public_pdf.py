"""Bounded official PDF download with DNS-pinned public destinations at every hop."""
from __future__ import annotations

import http.client
import ipaddress
import socket
import ssl
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin, urlsplit

from astock.core.request_budget import (
    RequestBudgetExhausted,
    RequestExecutionBudget,
    current_request_budget,
)
from astock.core.source_policy_gate import SourcePolicyGate
from astock.schemas import AgentSourceProposal
from astock.schemas.web_tools import public_web_url


@dataclass(frozen=True)
class PublicPdfDownload:
    content: bytes
    final_url: str
    redirect_chain: tuple[str, ...]


def _connect(
    host: str, port: int, address: tuple[Any, ...], timeout: float,
) -> http.client.HTTPConnection:
    family, socktype, protocol, _, target = address
    raw = socket.socket(family, socktype, protocol)
    try:
        raw.settimeout(timeout)
        raw.connect(target)
        if raw.getpeername()[0] != target[0]:
            raise ValueError("PDF connection peer differs from the admitted address")
        secured = ssl.create_default_context().wrap_socket(raw, server_hostname=host)
    except BaseException:
        raw.close()
        raise
    connection = http.client.HTTPSConnection(host, port=port, timeout=timeout)
    connection.sock = secured
    return connection


def _read_pdf_body(
    connection: http.client.HTTPConnection, response: http.client.HTTPResponse, *,
    started: float, timeout_seconds: float, max_bytes: int,
    budget: RequestExecutionBudget | None,
) -> bytes:
    size = response.getheader("Content-Length")
    if size is not None and (int(size) < 0 or int(size) > max_bytes):
        raise ValueError("Official PDF exceeds the byte budget")
    content = bytearray()
    while True:
        remaining = timeout_seconds - (time.monotonic() - started)
        if budget is not None:
            remaining = min(remaining, budget.remaining_seconds())
        if remaining <= 0:
            raise TimeoutError("Official PDF elapsed budget exhausted")
        if connection.sock is not None:
            connection.sock.settimeout(remaining)
        chunk = response.read1(min(65536, max_bytes + 1 - len(content)))
        if not chunk:
            break
        content.extend(chunk)
        if len(content) > max_bytes:
            raise ValueError("Official PDF exceeds the byte budget")
    if not content.lstrip().startswith(b"%PDF-"):
        raise ValueError("Official download is not a PDF")
    return bytes(content)


def fetch_official_pdf(
    proposal: AgentSourceProposal, *, gate: SourcePolicyGate,
    timeout_seconds: float, max_bytes: int, max_redirects: int = 3,
    before_hop: Callable[[str], None] | None = None,
    resolver: Callable[..., list[tuple[Any, ...]]] = socket.getaddrinfo,
    connector: Callable[
        [str, int, tuple[Any, ...], float], http.client.HTTPConnection
    ] = _connect,
) -> PublicPdfDownload:
    """No proxy, credential or private-network fallback; raw bytes stay local.

    Socket phase timeouts and elapsed checks are not a hard wall-clock claim for DNS/header
    resolution. The owning research worker supplies cancellation/deadline termination.
    """
    if not 0 < timeout_seconds <= 120 or not 1024 <= max_bytes <= 67108864:
        raise ValueError("Invalid official PDF resource bound")
    if not 0 <= max_redirects <= 5 or proposal.candidate_url is None:
        raise ValueError("Invalid official PDF redirect bound or URL")
    started = time.monotonic()
    current = str(proposal.candidate_url)
    redirects: list[str] = []
    for _ in range(max_redirects + 1):
        public_web_url(current)
        parsed = urlsplit(current)
        if parsed.scheme != "https" or parsed.hostname is None or parsed.port not in {None, 443}:
            raise ValueError("Official PDF download requires HTTPS")
        hop = proposal.model_copy(update={"candidate_url": current})
        decision = gate.validate(AgentSourceProposal.model_validate(hop.model_dump()))
        if not decision.allowed or not decision.formal_eligible:
            raise ValueError("PDF redirect left the admitted official source policy")
        if before_hop is not None:
            before_hop(current)
        budget = current_request_budget()
        if budget is not None and (
            budget.remaining_seconds() <= 0 or budget.remaining_attempts() == 0
        ):
            raise RequestBudgetExhausted("Official PDF request budget exhausted before DNS")
        remaining = timeout_seconds - (time.monotonic() - started)
        if remaining <= 0:
            raise TimeoutError("Official PDF elapsed budget exhausted")
        # Resolve once, reject mixed public/private answers, connect the numeric result.
        # TLS still verifies the original hostname; no second name resolution can rebind it.
        addresses = resolver(parsed.hostname, 443, type=socket.SOCK_STREAM)
        if not addresses or any(
            not ipaddress.ip_address(item[4][0]).is_global for item in addresses
        ):
            raise ValueError("Official PDF DNS resolved to a non-public destination")
        remaining = timeout_seconds - (time.monotonic() - started)
        if remaining <= 0:
            raise TimeoutError("Official PDF elapsed budget exhausted during DNS")
        if budget is not None:
            budget.reserve_attempt()
            remaining = min(remaining, budget.remaining_seconds())
        connection = connector(parsed.hostname, 443, addresses[0], remaining)
        try:
            target = parsed.path or "/"
            if parsed.query:
                target += "?" + parsed.query
            connection.request("GET", target, headers={
                "Host": parsed.netloc, "Accept": "application/pdf",
                "Accept-Encoding": "identity", "User-Agent": "AStockMultiAgent/official-pdf",
            })
            response = connection.getresponse()
            redirects.append(current)
            if response.status in {301, 302, 303, 307, 308}:
                location = response.getheader("Location")
                if not location:
                    raise ValueError("Official PDF redirect lacks Location")
                current = urljoin(current, location)
                if current in redirects:
                    raise ValueError("Official PDF redirect loop")
                continue
            if response.status != 200:
                raise ValueError(f"Official PDF HTTP status {response.status}")
            content = _read_pdf_body(
                connection, response, started=started, timeout_seconds=timeout_seconds,
                max_bytes=max_bytes, budget=budget,
            )
            return PublicPdfDownload(content, current, tuple(redirects))
        finally:
            connection.close()
    raise ValueError("Official PDF redirect budget exhausted")
