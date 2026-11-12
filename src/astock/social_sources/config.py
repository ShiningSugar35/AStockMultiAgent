"""Configuration for the local Chrome social-search bridge."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from astock.core.atomic import atomic_write_text


@dataclass(frozen=True, slots=True)
class SocialSearchConfig:
    enabled: bool
    bridge_host: str
    bridge_port: int
    extension_id: str
    bridge_key: str
    request_timeout_seconds: int
    poll_wait_seconds: int
    job_lease_seconds: int
    max_request_bytes: int
    lookback_days: int
    max_queries_per_platform: int
    max_scrolls: int
    max_items_per_query: int
    minimum_text_characters: int
    maximum_text_characters: int
    default_management_terms: tuple[str, ...]
    positive_hint_terms: tuple[str, ...]
    negative_hint_terms: tuple[str, ...]

    @property
    def bridge_url(self) -> str:
        return f"http://{self.bridge_host}:{self.bridge_port}"

    @property
    def extension_origin(self) -> str:
        return f"chrome-extension://{self.extension_id}"


def load_social_search_config(
    project_root: Path,
    *,
    create_local: bool = True,
) -> SocialSearchConfig:
    root = project_root.resolve()
    config_path = root / "configs" / "social_search.yaml"
    try:
        payload = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise ValueError("social search configuration is unavailable") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != "social-search-v1":
        raise ValueError("unsupported social search configuration")
    bridge = _mapping(payload, "bridge")
    search = _mapping(payload, "search")
    host = str(bridge.get("host", "127.0.0.1"))
    if host != "127.0.0.1":
        raise ValueError("social capture bridge must bind to loopback")
    port = int(bridge["port"])
    extension_id = str(bridge["extension_id"])
    if not 1024 <= port <= 65535 or not re.fullmatch(r"[a-p]{32}", extension_id):
        raise ValueError("social capture bridge endpoint is invalid")
    local_path = root / "runtime" / "local" / "social_capture.yaml"
    local: dict[str, Any] = {}
    if local_path.exists():
        try:
            loaded = yaml.safe_load(local_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, yaml.YAMLError) as exc:
            raise ValueError("local social capture settings are invalid") from exc
        if not isinstance(loaded, dict):
            raise ValueError("local social capture settings must be a mapping")
        local = loaded
    bridge_key = str(local.get("bridge_key", ""))
    if len(bridge_key) < 48:
        if not create_local:
            raise ValueError("local social capture bridge key is not configured")
        bridge_key = os.urandom(32).hex()
        local_path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(
            local_path,
            yaml.safe_dump(
                {"schema_version": "social-capture-local-v1", "bridge_key": bridge_key},
                allow_unicode=True,
                sort_keys=True,
            ),
        )
    result = SocialSearchConfig(
        enabled=bool(payload.get("enabled", True)),
        bridge_host=host,
        bridge_port=port,
        extension_id=extension_id,
        bridge_key=bridge_key,
        request_timeout_seconds=int(bridge.get("request_timeout_seconds", 45)),
        poll_wait_seconds=int(bridge.get("poll_wait_seconds", 15)),
        job_lease_seconds=int(bridge.get("job_lease_seconds", 180)),
        max_request_bytes=int(bridge.get("max_request_bytes", 2_097_152)),
        lookback_days=int(search.get("lookback_days", 7)),
        max_queries_per_platform=int(search.get("max_queries_per_platform", 4)),
        max_scrolls=int(search.get("max_scrolls", 4)),
        max_items_per_query=int(search.get("max_items_per_query", 80)),
        minimum_text_characters=int(search.get("minimum_text_characters", 12)),
        maximum_text_characters=int(search.get("maximum_text_characters", 12000)),
        default_management_terms=tuple(str(x) for x in search.get("default_management_terms", [])),
        positive_hint_terms=tuple(str(x) for x in search.get("positive_hint_terms", [])),
        negative_hint_terms=tuple(str(x) for x in search.get("negative_hint_terms", [])),
    )
    if not (10 <= result.request_timeout_seconds <= 180):
        raise ValueError("social capture request timeout must be within 10..180 seconds")
    if not (1 <= result.poll_wait_seconds <= 20):
        raise ValueError("social capture poll wait must be within 1..20 seconds")
    if not (30 <= result.job_lease_seconds <= 600):
        raise ValueError("social capture job lease must be within 30..600 seconds")
    if not (1 <= result.lookback_days <= 7):
        raise ValueError("social search lookback must be within 1..7 days")
    if not (1 <= result.max_queries_per_platform <= 8):
        raise ValueError("social query count must be within 1..8")
    if not (0 <= result.max_scrolls <= 10 and 1 <= result.max_items_per_query <= 200):
        raise ValueError("social page capture budget is invalid")
    if not (1 <= result.minimum_text_characters <= 200):
        raise ValueError("social minimum content length is invalid")
    if not (200 <= result.maximum_text_characters <= 16000):
        raise ValueError("social maximum content length is invalid")
    return result


def _mapping(payload: dict[str, Any], key: str) -> dict[str, Any]:
    value = payload.get(key)
    if not isinstance(value, dict):
        raise ValueError(f"social search {key} must be a mapping")
    return value
