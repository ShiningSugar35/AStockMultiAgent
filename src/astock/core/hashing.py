"""Canonical JSON and SHA-256 helpers."""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any

from pydantic import BaseModel


def _json_default(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json", exclude_none=False)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, set):
        return sorted(value)
    raise TypeError(f"Unsupported canonical JSON value: {type(value)!r}")


def canonical_json_bytes(value: Any) -> bytes:
    """Serialize a value deterministically as UTF-8 JSON."""

    return json.dumps(
        value,
        default=_json_default,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def content_hash(value: Any) -> str:
    """Hash semantic content while excluding volatile production timestamps."""

    return sha256_bytes(canonical_json_bytes(_content_projection(value)))


_VOLATILE_CONTENT_KEYS = {
    "created_at",
    "last_probe_at",
    "fetched_at",
    "available_to_system_at",
    "request_started_at",
    "request_finished_at",
}


def _content_projection(value: Any) -> Any:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="python", exclude_none=False)
    if isinstance(value, dict):
        return {
            str(key): _content_projection(child)
            for key, child in value.items()
            if str(key) not in _VOLATILE_CONTENT_KEYS
        }
    if isinstance(value, (list, tuple)):
        return [_content_projection(child) for child in value]
    if isinstance(value, set):
        return sorted(_content_projection(child) for child in value)
    return value
