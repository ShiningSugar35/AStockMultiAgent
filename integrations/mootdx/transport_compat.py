"""Narrow MAC command compatibility for Mootdx 0.11.7 / tdxpy 0.2.7.

Protocol layouts reviewed from clong365/easy_tdx at
4820b4a0496899ece0b8ca4d7f4d66a5159da7f8; see THIRD_PARTY_NOTICES.md.
Only two command constructors are replaced inside an owned SDK process.
Mootdx still creates, handshakes and closes the actual TDX connection.
"""
from __future__ import annotations

import math
import struct
import zlib
from contextlib import nullcontext
from datetime import datetime

PATCH_VERSION = "astock-mootdx-mac-v1"
UPSTREAM_COMMIT = "4820b4a0496899ece0b8ca4d7f4d66a5159da7f8"
FRAMES: list[dict] = []
_FIELDS = {
    0: ("last_close", "<f"), 1: ("open", "<f"), 2: ("high", "<f"),
    3: ("low", "<f"), 4: ("price", "<f"), 5: ("vol", "<I"),
    7: ("amount", "<f"), 19: ("source_date", "<I"), 20: ("source_time", "<I"),
}
_FIELD_MASK = sum(1 << bit for bit in _FIELDS)
_BITMAP = _FIELD_MASK.to_bytes(16, "little") + bytes(4)


def _frame(command: int, body: bytes) -> bytes:
    inner = struct.pack("<H", command) + body
    return struct.pack("<BIBHH", 0x1C, 0, 1, len(inner), len(inner)) + inner


def _exact(sock, size: int) -> bytes:
    data = bytearray()
    while len(data) < size:
        chunk = sock.recv(size - len(data))
        if not chunk:
            raise ConnectionError("Truncated TDX frame")
        data.extend(chunk)
    return bytes(data)


def _exchange(sock, request: bytes) -> bytes:
    sock.sendall(request)
    header = _exact(sock, 16)
    _first, _second, _third, zipped, unzipped = struct.unpack("<IIIHH", header)
    raw = _exact(sock, zipped)
    # These protocol fields are uint16; an attacker cannot inflate the buffer
    # beyond the advertised and independently verified 65535-byte frame limit.
    if zipped != unzipped:
        decompressor = zlib.decompressobj()
        body = decompressor.decompress(raw, unzipped + 1)
        if (
            len(body) != unzipped or not decompressor.eof
            or decompressor.unused_data or decompressor.unconsumed_tail
        ):
            raise ValueError("Invalid compressed TDX frame length")
    else:
        body = raw
    FRAMES.append({
        "request_hex": request.hex(), "response_header_hex": header.hex(),
        "response_payload_hex": raw.hex(), "response_body_hex": body.hex(),
    })
    return body


def _identity(market: int, symbol: str) -> None:
    if market not in (0, 1) or len(symbol) != 6 or not symbol.isascii() or not symbol.isdigit():
        raise ValueError("Only explicit Shanghai/Shenzhen six-digit identities are supported")


def bars_request(category: int, market: int, code: str, start: int, count: int) -> bytes:
    _identity(market, code)
    if category not in (0, 3) or not 0 <= start <= 25600 or not 1 <= count <= 800:
        raise ValueError("MAC command outside raw 5m/60m bounded scope")
    body = struct.pack(
        "<H22sHHIHHbbbbH4s", market, code.encode("ascii"), category, 1,
        start, count, 0, 1, 1, 0, 1, 0, b"",
    )
    return _frame(0x122E, body)


def parse_bars(
    body: bytes, category: int, market: int, code: str, maximum: int, requested_start: int = 0
) -> list[dict]:
    if len(body) < 33:
        raise ValueError("Truncated MAC bar header")
    actual_market, actual_code, category_flag, _flag, count, start = struct.unpack_from(
        "<H22sHBHI", body
    )
    if actual_market != market or actual_code.rstrip(bytes(1)).decode("ascii") != code:
        raise ValueError("MAC bar identity mismatch")
    if start != requested_start:
        raise ValueError("MAC bar response offset differs from the requested page")
    trailer_size = len(body) - (33 + count * 36)
    # Public servers append a 120-byte stock metadata block. Preserve it in
    # the recorded wire frame; accept only the observed zero/120-byte forms.
    if category_flag & 0xFF != category or count > maximum or trailer_size not in (0, 120):
        raise ValueError("MAC bar category/count/length mismatch")
    rows = []
    for position in range(count):
        values = struct.unpack_from("<II7f", body, 33 + 36 * position)
        ymd, seconds, open_, high, low, close, amount, volume, _float_shares = values
        if not 0 <= seconds < 86400 or seconds % 60:
            raise ValueError("MAC bar time is invalid")
        stamp = datetime(
            ymd // 10000, ymd // 100 % 100, ymd % 100, seconds // 3600, seconds // 60 % 60
        )
        numbers = [open_, high, low, close, amount, volume]
        if any(not math.isfinite(value) or value < 0 for value in numbers):
            raise ValueError("MAC bar contains nonfinite or negative values")
        rows.append({
            "datetime": stamp.strftime("%Y-%m-%d %H:%M"), "open": open_, "high": high,
            "low": low, "close": close, "amount": amount, "vol": volume,
            "category_flag": category_flag, "page_start": start,
        })
    return rows


def quote_request(stocks: list[tuple[int, str]]) -> bytes:
    if len(stocks) != 1:
        raise ValueError("Narrow MAC quote supports one stock per call")
    market, code = stocks[0]
    _identity(market, code)
    return _frame(0x122B, _BITMAP + struct.pack("<HH22s", 1, market, code.encode("ascii")))


def parse_quote(body: bytes, stocks: list[tuple[int, str]]) -> list[dict]:
    if len(body) < 26:
        raise ValueError("Truncated MAC quote header")
    mask = int.from_bytes(body[:16], "little")
    if mask != _FIELD_MASK:
        raise ValueError("MAC quote returned an unexpected field bitmap")
    total, count = struct.unpack_from("<IH", body, 20)
    row_size = 68 + len(_FIELDS) * 4
    if count > len(stocks) or total < count or len(body) != 26 + row_size * count:
        raise ValueError("MAC quote count/length mismatch")
    rows = []
    for position in range(count):
        start = 26 + row_size * position
        market, code, name = struct.unpack_from("<H22s44s", body, start)
        code = code.rstrip(bytes(1)).decode("ascii")
        if (market, code) not in stocks:
            raise ValueError("MAC quote identity mismatch")
        row = {"market": market, "code": code, "name": name.rstrip(bytes(1)).decode("gbk")}
        for index, (_bit, (key, fmt)) in enumerate(sorted(_FIELDS.items())):
            value = struct.unpack_from(fmt, body, start + 68 + index * 4)[0]
            if not math.isfinite(value) or value < 0:
                raise ValueError("MAC quote numeric value is invalid")
            row[key] = value
        raw_time = row["source_time"]
        row["servertime"] = f"{raw_time // 10000:02}:{raw_time // 100 % 100:02}:{raw_time % 100:02}"
        rows.append(row)
    return rows


class _MacCommand:
    """The concrete command interface consumed by tdxpy.hq, with bounded framing."""

    def __init__(self, client, lock=None) -> None:
        self.client = client
        self.lock = lock
        self.send_pkg = b""

    def call_api(self) -> list[dict]:
        if not self.send_pkg:
            raise ValueError("MAC command parameters have not been set")
        context = self.lock if self.lock is not None else nullcontext()
        with context:
            return self.parseResponse(_exchange(self.client, self.send_pkg))

    def parseResponse(self, body_buf: bytes) -> list[dict]:
        raise NotImplementedError


def install() -> None:
    import tdxpy.hq

    class MacBarsCommand(_MacCommand):
        def setParams(self, category, market, code, start, count):
            self.arguments = (category, market, code, count, start)
            self.send_pkg = bars_request(category, market, code, start, count)

        def parseResponse(self, body_buf: bytes) -> list[dict]:
            return parse_bars(body_buf, *self.arguments)

    class MacQuoteCommand(_MacCommand):
        def setParams(self, stocks):
            self.stocks = stocks
            self.send_pkg = quote_request(stocks)

        def parseResponse(self, body_buf: bytes) -> list[dict]:
            return parse_quote(body_buf, self.stocks)

    tdxpy.hq.GetSecurityBarsCmd = MacBarsCommand
    tdxpy.hq.GetSecurityQuotesCmd = MacQuoteCommand
    FRAMES.clear()
