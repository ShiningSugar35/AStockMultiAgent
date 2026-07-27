from __future__ import annotations

import pytest

from astock.schemas import ResearchRequest, ResearchRequestModule


def test_research_request_defaults() -> None:
    request = ResearchRequest(company="Ningde Times", ticker="300750")
    assert request.company == "Ningde Times"
    assert request.ticker == "300750"
    assert request.market == "CN"
    assert request.requested_modules == [
        ResearchRequestModule.FINANCIAL,
        ResearchRequestModule.EVIDENCE,
        ResearchRequestModule.RESEARCH,
    ]


def test_research_request_rejects_empty_company() -> None:
    with pytest.raises(ValueError):
        ResearchRequest(company="", ticker="300750")


def test_research_request_rejects_invalid_ticker() -> None:
    with pytest.raises(ValueError):
        ResearchRequest(company="Ningde", ticker="0000")


def test_research_request_dedupes_and_orders_modules() -> None:
    request = ResearchRequest(
        company="Ningde Times",
        ticker="300750",
        requested_modules=[
            ResearchRequestModule.RESEARCH,
            ResearchRequestModule.FINANCIAL,
            ResearchRequestModule.FINANCIAL,
            ResearchRequestModule.EVIDENCE,
        ],
    )
    assert request.requested_modules == [
        ResearchRequestModule.FINANCIAL,
        ResearchRequestModule.EVIDENCE,
        ResearchRequestModule.RESEARCH,
    ]
