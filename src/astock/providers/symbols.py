"""Explicit A-share symbol mappings for provider-specific identifiers."""

from __future__ import annotations

from astock.schemas import BarRequest, Market


def eastmoney_secid(request: BarRequest) -> str:
    if request.market == Market.XSHG:
        prefix = "1"
    elif request.market in {Market.XSHE, Market.BJSE}:
        prefix = "0"
    elif request.market == Market.INDEX:
        prefix = "0" if request.symbol.startswith("399") else "1"
    else:  # pragma: no cover - enum makes this defensive only
        raise ValueError(f"Unsupported market: {request.market}")
    return f"{prefix}.{request.symbol}"


def sina_symbol(request: BarRequest) -> str:
    if request.market == Market.XSHG:
        prefix = "sh"
    elif request.market == Market.XSHE:
        prefix = "sz"
    elif request.market == Market.BJSE:
        prefix = "bj"
    elif request.market == Market.INDEX:
        prefix = "sz" if request.symbol.startswith("399") else "sh"
    else:  # pragma: no cover
        raise ValueError(f"Unsupported market: {request.market}")
    return f"{prefix}{request.symbol}"
