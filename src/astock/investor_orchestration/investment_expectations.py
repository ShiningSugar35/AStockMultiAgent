"""Resolve research objectives; never infer account cash or permission to trade."""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from astock.core.errors import StorageError
from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.investor_orchestration.models import InvestorRequestEnvelope
from astock.investor_orchestration.utils import content_hash
from astock.schemas.full_research import (
    FullResearchRequestContract,
    InvestmentExpectationValueSource,
    RecommendationResearchReceipt,
)

_LOG = logging.getLogger(__name__)
_NUMBER = r"[-+]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?"
_CAPITAL = re.compile(
    rf"(?:本金|可用资金|投入资金|资金|预算|投入|我有)\s*(?:为|是|有|约|大约|:|=)?\s*"
    rf"(?P<value>{_NUMBER})\s*(?P<unit>万元?|[wW]|千元?|[kK]|元|人民币)"
)
_ANNUAL = re.compile(
    rf"(?:预期年化|年化预期|目标年化|年化目标|年化收益(?:率)?|年化回报(?:率)?|年化)\s*"
    rf"(?:目标|预期|为|是|达到|希望|约|大约|=|:)*\s*(?P<value>{_NUMBER})\s*%"
)
_NONCURRENT = re.compile(
    r"(?:不(?:是|要|按|用)|并非|假设|例如|比如|去年|过去|此前|历史|曾经|他说|朋友)"
)
_RECOMMENDATION_WORDS = re.compile(r"推荐|荐股|投资组合|买入|持仓|本金|资金|年化")
_FOREIGN_CURRENCY_SUFFIX = re.compile(
    r"^(?:美元|美金|港元|港币|日元|欧元|英镑|澳元|加元|新加坡元|韩元)"
)


@dataclass(frozen=True, slots=True)
class ResolvedInvestmentExpectation:
    capital_rmb: Decimal
    target_annual_return: Decimal
    capital_source: InvestmentExpectationValueSource
    target_annual_return_source: InvestmentExpectationValueSource
    history_references: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()


def _decimal(value: Any) -> Decimal | None:
    if isinstance(value, bool) or len(str(value)) > 64:
        return None
    try:
        parsed = Decimal(str(value).replace(",", ""))
    except (InvalidOperation, TypeError, ValueError):
        return None
    # Computational bound, not an investment-admission or return-promise threshold.
    return parsed if parsed.is_finite() and parsed.adjusted() <= 18 else None


def _positive_decimal(value: Any) -> Decimal | None:
    parsed = _decimal(value)
    return parsed if parsed is not None and parsed > 0 else None


def _annual_return_decimal(value: Any, *, percent: bool = False) -> Decimal | None:
    if isinstance(value, str) and value.strip().endswith(("%", "％")):
        value, percent = value.strip()[:-1], True
    parsed = _decimal(value)
    if parsed is not None and percent:
        parsed /= Decimal("100")
    return parsed if parsed is not None and parsed >= 0 else None


def _text_value(text: str, pattern: re.Pattern[str], *, annual: bool) -> Decimal | None:
    found: list[Decimal] = []
    text = unicodedata.normalize("NFKC", text)
    for match in pattern.finditer(text):
        prefix = text[max(0, match.start() - 12):match.start()]
        clause_prefix = re.split(r"[，,；;。\n]", prefix)[-1]
        if _NONCURRENT.search(clause_prefix):
            continue
        if not annual and _FOREIGN_CURRENCY_SUFFIX.search(text[match.end():]):
            raise ValueError("本金币种不是人民币，不能静默按人民币换算。")
        value = (
            _annual_return_decimal(match["value"], percent=True)
            if annual else _positive_decimal(match["value"])
        )
        if value is None:
            # Explicit invalid amounts cannot turn into a fictitious default bankroll.
            raise ValueError("明确的本金或年化目标不是有效数值，请核对该数值。")
        if not annual:
            unit = match["unit"].lower()
            if unit.startswith("万") or unit == "w":
                value *= Decimal("10000")
            elif unit.startswith("千") or unit == "k":
                value *= Decimal("1000")
        found.append(value)
    # A later explicit correction takes precedence over an earlier amount in the same message.
    return found[-1] if found else None


def current_expectation_values(
    raw_text: str, supplied: Mapping[str, Any] | None
) -> tuple[Decimal | None, Decimal | None]:
    """Typed metadata is the semantic escape hatch; common Chinese text is a fallback."""
    supplied = supplied or {}
    capital = _text_value(raw_text, _CAPITAL, annual=False)
    target = _text_value(raw_text, _ANNUAL, annual=True)
    for field, parser in (
        ("capital_rmb", _positive_decimal),
        ("target_annual_return", _annual_return_decimal),
    ):
        if field not in supplied or supplied[field] is None:
            continue
        if (field == "capital_rmb" and capital is not None) or (
            field == "target_annual_return" and target is not None
        ):
            continue
        parsed = parser(supplied[field])
        if parsed is None:
            raise ValueError("明确的本金或年化目标不是有效数值，请核对该数值。")
        # The current user text wins over an accidentally retained metadata value.
        if field == "capital_rmb" and capital is None:
            capital = parsed
        elif field == "target_annual_return" and target is None:
            target = parsed
    if target is None and supplied.get("target_annual_return_pct") is not None:
        target = _annual_return_decimal(supplied["target_annual_return_pct"], percent=True)
        if target is None:
            raise ValueError("明确的年化目标不是有效数值，请核对该数值。")
    return capital, target


def _recorded_values(assumptions: Mapping[str, Any]) -> tuple[Decimal | None, Decimal | None]:
    constraints = assumptions.get("user_constraints")
    constraints = constraints if isinstance(constraints, Mapping) else {}
    values: list[Decimal | None] = []
    for field, source_key, parser in (
        ("capital_rmb", "capital_source", _positive_decimal),
        ("target_annual_return", "target_annual_return_source", _annual_return_decimal),
    ):
        source = assumptions.get(source_key)
        explicit = source in {"CURRENT_USER", "RECENT_HISTORY"}
        legacy = source is None and field in constraints
        values.append(parser(assumptions.get(field)) if explicit or legacy else None)
    return values[0], values[1]


type HistoryItem = tuple[datetime, str | None, str, Decimal | None, Decimal | None]


def _history_item(payload: Any, record: Mapping[str, Any]) -> HistoryItem | None:
    """Accept typed registered requests/contracts and self-authenticating full receipts."""
    kind = record["type"]
    if kind == "InvestorRequestEnvelope":
        request = InvestorRequestEnvelope.model_validate(payload)
        if record["artifact_id"] != f"InvestorRequestEnvelope:{content_hash(request.request_id)}":
            return None
        if not _RECOMMENDATION_WORDS.search(request.raw_text):
            return None
        supplied = request.metadata.get("portfolio_assumptions")
        capital, target = current_expectation_values(
            request.raw_text, supplied if isinstance(supplied, Mapping) else None
        )
        return request.question_time, request.account_id, request.request_id, capital, target
    if kind == "RecommendationResearchReceipt":
        receipt = RecommendationResearchReceipt.model_validate(payload)
        digest = content_hash(receipt.model_dump(exclude={"receipt_id", "receipt_hash"}))
        if (
            receipt.receipt_hash != digest or receipt.receipt_id != record["artifact_id"]
            or receipt.receipt_id != f"RecommendationResearchReceipt:{digest}"
        ):
            return None
        contract = receipt.request_contract
    elif kind == "FullResearchRequestContract":
        contract = FullResearchRequestContract.model_validate(payload)
        identity = f"FullResearchRequestContract:{content_hash(contract.request_id)}"
        if record["artifact_id"] != identity:
            return None
    else:
        return None
    capital, target = _recorded_values(contract.portfolio_assumptions.model_dump(mode="json"))
    return (
        contract.as_of_timestamp, contract.account_id, contract.request_id, capital, target
    )


def resolve_investment_expectation(
    *,
    raw_text: str,
    supplied_assumptions: Mapping[str, Any] | None,
    defaults: Mapping[str, Any],
    state: StateStore | None,
    objects: ObjectStore | None,
    request_id: str | None = None,
    account_id: str | None = None,
    as_of: datetime | None = None,
    history_limit: int = 16,
    registry_scan_limit: int = 1024,
    max_history_object_bytes: int = 524288,
) -> ResolvedInvestmentExpectation:
    """Current values > newest recorded user values > disclosed research defaults."""
    capital, target = current_expectation_values(raw_text, supplied_assumptions)
    current_capital, current_target = capital is not None, target is not None
    refs: list[str] = []
    notes: list[str] = []
    candidates = []
    cutoff = as_of or datetime.now(UTC)
    if (capital is None or target is None) and state is not None and objects is not None:
        try:
            records = state.recent_artifact_records(
                (
                    "InvestorRequestEnvelope", "FullResearchRequestContract",
                    "RecommendationResearchReceipt",
                ),
                limit=history_limit,
                scan_limit=registry_scan_limit,
            )
        except (sqlite3.Error, OSError, ValueError):
            records = ()
            notes.append("历史投资预期暂不可核实，缺失项按明确标注的模型默认值分析。")
            _LOG.warning("Investment expectation history unavailable; using disclosed fallback")
        for record in records:
            try:
                digest = str(record["object_hash"])
                if objects.path_for(digest).stat().st_size > max_history_object_bytes:
                    continue
                item = _history_item(json.loads(objects.get_bytes(digest)), record)
                if item is None:
                    continue
                at, account, historical_id, old_capital, old_target = item
                if account != account_id or historical_id == request_id or at > cutoff:
                    continue
                candidates.append((at, str(record["artifact_id"]), old_capital, old_target))
            except (StorageError, OSError, ValueError, TypeError, KeyError):
                continue
        candidates.sort(key=lambda x: (x[0], x[1]), reverse=True)
        for _, ref, old_capital, old_target in candidates:
            used = False
            if capital is None and old_capital is not None:
                capital, used = old_capital, True
            if target is None and old_target is not None:
                target, used = old_target, True
            if used:
                refs.append(ref)
            if capital is not None and target is not None:
                break
    capital_source = _value_source(current_capital, capital)
    target_source = _value_source(current_target, target)
    if capital is None:
        capital = _positive_decimal(defaults.get("capital_rmb"))
    if target is None:
        target = _annual_return_decimal(defaults.get("target_annual_return"))
    if capital is None or target is None:
        raise ValueError("investment expectation policy defaults are invalid")
    return ResolvedInvestmentExpectation(
        capital, target, capital_source, target_source, tuple(refs), tuple(notes)
    )


def _value_source(current: bool, value: Decimal | None) -> InvestmentExpectationValueSource:
    if current:
        return InvestmentExpectationValueSource.CURRENT_USER
    if value is not None:
        return InvestmentExpectationValueSource.RECENT_HISTORY
    return InvestmentExpectationValueSource.DEFAULT
