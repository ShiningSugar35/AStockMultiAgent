"""Explicit A-share symbol mappings for provider-specific identifiers."""

from __future__ import annotations

from astock.schemas import BarRequest, Market


def baostock_code(symbol: str, market: Market) -> str:
    prefixes = {Market.XSHG: "sh", Market.XSHE: "sz", Market.BJSE: "bj"}
    if market is Market.INDEX:
        prefix = "sz" if symbol.startswith("399") else "sh"
    else:
        try:
            prefix = prefixes[market]
        except KeyError as exc:  # pragma: no cover - defensive enum boundary
            raise ValueError(f"Unsupported BaoStock market: {market}") from exc
    return f"{prefix}.{symbol}"


def market_from_baostock_code(code: str, *, instrument_type: str = "1") -> Market:
    if instrument_type == "2":
        return Market.INDEX
    prefix, separator, _ = code.partition(".")
    if not separator:
        raise ValueError(f"Invalid BaoStock code: {code}")
    mapping = {"sh": Market.XSHG, "sz": Market.XSHE, "bj": Market.BJSE}
    try:
        return mapping[prefix.lower()]
    except KeyError as exc:
        raise ValueError(f"Unknown BaoStock market prefix: {prefix}") from exc


def market_from_eastmoney_market(value: str | int, symbol: str) -> Market:
    # f13 is an explicit market identifier. Code prefixes are not accepted as a substitute.
    mapping = {"1": Market.XSHG, "0": Market.XSHE, "2": Market.BJSE}
    try:
        market = mapping[str(value)]
    except KeyError as exc:
        raise ValueError(f"Unknown EastMoney market id for {symbol}: {value}") from exc
    return market


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
