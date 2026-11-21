from __future__ import annotations

import socket
from datetime import UTC, datetime, timedelta
from http.client import HTTPConnection
from typing import cast

import pytest
from pydantic import HttpUrl

from astock.core.request_budget import RequestBudgetExhausted, RequestExecutionBudget
from astock.core.source_policy_gate import SourcePolicyGate
from astock.documents.public_pdf import fetch_official_pdf
from astock.schemas import AgentSourceProposal, SourceClass

URL = "https://www.sse.com.cn/disclosure/listedinfo/report.pdf"


def proposal(url=URL):
    return AgentSourceProposal(
        requested_capability="financial.official_document", query="official report",
        candidate_url=HttpUrl(url), expected_fact="published annual financial statements",
        preferred_source_class=SourceClass.PRIMARY_OFFICIAL_WEB, formal_use=True,
        require_complete=False, reason="bounded original evidence",
    )


def addresses(host, port, **kwargs):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]


class Response:
    def __init__(self, status=200, body=b"%PDF-1.4\n%%EOF", headers=None):
        self.status, self.body, self.headers = status, body, headers or {}
    def getheader(self, name):
        return self.headers.get(name)
    def read1(self, count):
        result, self.body = self.body[:count], self.body[count:]
        return result


class Connection:
    sock = None
    def __init__(self, response):
        self.response, self.closed, self.requests = response, False, []
    def request(self, *args, **kwargs):
        self.requests.append((args, kwargs))
    def getresponse(self):
        return self.response
    def close(self):
        self.closed = True


def test_dns_is_resolved_once_and_numeric_admitted_address_is_connected():
    resolved, connected = [], []
    connection = Connection(Response())
    def resolve(*args, **kwargs):
        resolved.append(args[0])
        return addresses(*args, **kwargs)
    def connect(host, port, address, timeout):
        connected.append(address[4][0])
        return cast(HTTPConnection, connection)
    result = fetch_official_pdf(proposal(), gate=SourcePolicyGate(), timeout_seconds=5,
                                max_bytes=1024, resolver=resolve, connector=connect)
    assert result.content.startswith(b"%PDF-")
    assert resolved == ["www.sse.com.cn"] and connected == ["93.184.216.34"]
    assert connection.closed and result.redirect_chain == (URL,)


def test_mixed_private_dns_answer_rejected_without_connection():
    calls = []
    def resolve(*args, **kwargs):
        return addresses(*args, **kwargs) + [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))]
    def connect(*args):
        calls.append(args)
        raise AssertionError("private DNS answer must not reach a connection")
    with pytest.raises(ValueError, match="non-public"):
        fetch_official_pdf(proposal(), gate=SourcePolicyGate(), timeout_seconds=5, max_bytes=1024,
                           resolver=resolve, connector=connect)
    assert calls == []


@pytest.mark.parametrize("destination", ["https://127.0.0.1/private.pdf",
                                        "https://example.com/forged.pdf"])
def test_each_redirect_is_re_admitted_and_private_or_nonofficial_hop_never_connects(destination):
    connection = Connection(Response(302, headers={"Location": destination}))
    calls = []
    def connect(*args):
        calls.append(args)
        return cast(HTTPConnection, connection)
    with pytest.raises(ValueError):
        fetch_official_pdf(proposal(), gate=SourcePolicyGate(), timeout_seconds=5, max_bytes=1024,
                           resolver=addresses, connector=connect)
    assert len(calls) == 1 and connection.closed


@pytest.mark.parametrize("response", [
    Response(body=b"not PDF"),
    Response(body=b"%PDF-" + b"x" * 1024),
    Response(headers={"Content-Length": "2048"}),
])
def test_non_pdf_and_byte_budget_failure_close_owned_connection(response):
    connection = Connection(response)
    with pytest.raises(ValueError):
        fetch_official_pdf(proposal(), gate=SourcePolicyGate(), timeout_seconds=5, max_bytes=1024,
                           resolver=addresses,
                           connector=lambda *args: cast(HTTPConnection, connection))
    assert connection.closed


@pytest.mark.parametrize("expired", [False, True])
def test_exhausted_request_starts_neither_dns_nor_pdf_connection(state, expired):
    budget = RequestExecutionBudget(
        state, "pdf-exhausted", datetime.now(UTC) + timedelta(seconds=-1 if expired else 30), 1,
    )
    if not expired:
        budget.reserve_attempt()
    dns, connections = [], []
    def resolve(*args, **kwargs):
        dns.append(args)
        raise AssertionError("exhausted request must not resolve DNS")
    def connect(*args):
        connections.append(args)
        raise AssertionError("exhausted request must not connect")
    with budget.bind(), pytest.raises(RequestBudgetExhausted):
        fetch_official_pdf(
            proposal(), gate=SourcePolicyGate(), timeout_seconds=5, max_bytes=1024,
            resolver=resolve, connector=connect,
        )
    assert dns == [] and connections == []



def test_redirect_checks_owned_domain_capacity_before_second_dns_or_connection():
    next_url = "https://static.sse.com.cn/disclosure/listedinfo/next.pdf"
    first = Connection(Response(302, headers={"Location": next_url}))
    admitted, resolved, connected = [], [], []

    def before_hop(url):
        admitted.append(url)
        if url == next_url:
            raise ValueError("OWNED_DOMAIN_CONCURRENCY")

    def resolve(*args, **kwargs):
        resolved.append(args[0])
        return addresses(*args, **kwargs)

    def connect(*args):
        connected.append(args[0])
        return cast(HTTPConnection, first)

    with pytest.raises(ValueError, match="OWNED_DOMAIN_CONCURRENCY"):
        fetch_official_pdf(
            proposal(), gate=SourcePolicyGate(), timeout_seconds=5, max_bytes=1024,
            resolver=resolve, connector=connect, before_hop=before_hop,
        )
    assert admitted == [URL, next_url]
    assert resolved == connected == ["www.sse.com.cn"]
    assert first.closed
