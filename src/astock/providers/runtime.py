"""Provider runtime factory and versioned transport profiles.

The registry/configuration is the single source of truth for provider construction.
Core services ask for a capability or provider id; they do not know constructor details.
"""

from __future__ import annotations

import importlib
import inspect
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import yaml

from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.providers.config import get_provider, load_provider_registry
from astock.providers.dialects import ProviderDialect
from astock.providers.http_resilience import HttpClientLike, ResilientHttpClient
from astock.schemas import ProviderDefinition, ProviderRegistry, ProviderTransport


@dataclass(frozen=True, slots=True)
class TransportProfile:
    profile_id: str
    timeout_seconds: float
    follow_redirects: bool
    trust_env: bool
    headers: dict[str, str]
    max_attempts: int = 2
    backoff_seconds: float = 0.25
    proxy_strategy: str = "ENV_ONLY"
    jitter_seconds: float = 0.0
    retry_status_codes: tuple[int, ...] = (502, 503, 504)
    retry_methods: tuple[str, ...] = ("GET", "HEAD")


class ProviderFactory:
    """Instantiate provider adapters from the versioned provider registry."""

    def __init__(
        self,
        registry: ProviderRegistry,
        profiles: dict[str, TransportProfile],
        objects: ObjectStore,
        state: StateStore,
        fixture_root: Path,
        *,
        fixture_scope: Path | None = None,
        dialects: dict[str, ProviderDialect] | None = None,
    ) -> None:
        self.registry = registry
        self.profiles = profiles
        self.objects = objects
        self.state = state
        self.fixture_root = fixture_root.resolve()
        self.fixture_scope = fixture_scope.resolve() if fixture_scope is not None else None
        self.dialects = dialects or {}
        self._instances: dict[str, object] = {}

    def definitions_for_capability(self, capability: str) -> list[ProviderDefinition]:
        matches = [item for item in self.registry.providers if capability in item.capabilities]
        return sorted(matches, key=lambda item: (-item.priority, item.provider_id))

    def create(self, provider_id: str) -> object:
        existing = self._instances.get(provider_id)
        if existing is not None:
            return existing
        definition = get_provider(self.registry, provider_id)
        adapter_type = _load_adapter_class(definition.adapter_class)
        kwargs = self._constructor_kwargs(adapter_type, definition)
        instance = adapter_type(**kwargs)
        actual_id = getattr(instance, "provider_id", None)
        if actual_id != provider_id:
            raise ValueError(
                f"Provider adapter identity mismatch: registry={provider_id}, adapter={actual_id}"
            )
        self._instances[provider_id] = instance
        return instance

    def _constructor_kwargs(
        self, adapter_type: type[object], definition: ProviderDefinition
    ) -> dict[str, object]:
        signature = inspect.signature(adapter_type)
        parameters = signature.parameters
        kwargs: dict[str, object] = {}
        if "objects" in parameters:
            kwargs["objects"] = self.objects
        if "object_store" in parameters:
            kwargs["object_store"] = self.objects
        if "state" in parameters:
            kwargs["state"] = self.state
        if "fixture_root" in parameters:
            subdir = Path(definition.fixture_subdir or "")
            if self.fixture_scope is not None and definition.fixture_subdir:
                fixture = (self.fixture_scope / subdir.name).resolve()
                allowed_root = self.fixture_scope
            else:
                fixture = (self.fixture_root / subdir).resolve()
                allowed_root = self.fixture_root
            if not fixture.is_relative_to(allowed_root):
                raise ValueError(
                    f"Provider fixture root escapes configured root: {definition.provider_id}"
                )
            kwargs["fixture_root"] = fixture
        if "dialect" in parameters and definition.provider_id in self.dialects:
            kwargs["dialect"] = self.dialects[definition.provider_id]
        if "client" in parameters and definition.transport is ProviderTransport.HTTP:
            kwargs["client"] = self._http_client(definition)
        elif "timeout_seconds" in parameters:
            kwargs["timeout_seconds"] = definition.timeout_seconds
        return kwargs

    def _http_client(self, definition: ProviderDefinition) -> HttpClientLike:
        if not definition.transport_profile:
            return httpx.Client(
                timeout=definition.timeout_seconds,
                follow_redirects=True,
            )
        try:
            profile = self.profiles[definition.transport_profile]
        except KeyError as exc:
            raise ValueError(
                f"Unknown transport profile for {definition.provider_id}: "
                f"{definition.transport_profile}"
            ) from exc
        return _build_resilient_client(profile)


def build_provider_http_client(
    provider_id: str,
    *,
    project_root: Path | None = None,
) -> HttpClientLike:
    root = project_root or Path(__file__).resolve().parents[3]
    registry = load_provider_registry(root / "configs" / "provider_registry.yaml")
    definition = get_provider(registry, provider_id)
    profiles = load_transport_profiles(root / "configs" / "transport_profiles.yaml")
    if not definition.transport_profile:
        return httpx.Client(timeout=definition.timeout_seconds, follow_redirects=True)
    try:
        profile = profiles[definition.transport_profile]
    except KeyError as exc:
        raise ValueError(
            f"Unknown transport profile for {provider_id}: {definition.transport_profile}"
        ) from exc
    return _build_resilient_client(profile)


def load_transport_profiles(path: Path) -> dict[str, TransportProfile]:
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise ValueError(f"Invalid transport profiles: {path}") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != "transport-profiles-v1":
        raise ValueError("Unsupported transport profile configuration")
    raw_profiles = payload.get("profiles")
    if not isinstance(raw_profiles, dict) or not raw_profiles:
        raise ValueError("Transport profile configuration is empty")
    result: dict[str, TransportProfile] = {}
    for profile_id, raw in raw_profiles.items():
        if not isinstance(raw, dict):
            raise ValueError("Transport profile must be an object")
        headers = raw.get("headers", {})
        if not isinstance(headers, dict):
            raise ValueError("Transport profile headers must be an object")
        trust_env = bool(raw.get("trust_env", True))
        profile = TransportProfile(
            profile_id=str(profile_id),
            timeout_seconds=float(raw["timeout_seconds"]),
            follow_redirects=bool(raw.get("follow_redirects", True)),
            trust_env=trust_env,
            headers={str(key): str(value) for key, value in headers.items()},
            max_attempts=int(raw.get("max_attempts", 2)),
            backoff_seconds=float(raw.get("backoff_seconds", 0.25)),
            proxy_strategy=str(
                raw.get("proxy_strategy", "ENV_ONLY" if trust_env else "DIRECT_ONLY")
            ),
            jitter_seconds=float(raw.get("jitter_seconds", 0.0)),
            retry_status_codes=tuple(
                int(item) for item in raw.get("retry_status_codes", [502, 503, 504])
            ),
            retry_methods=tuple(
                str(item).upper() for item in raw.get("retry_methods", ["GET", "HEAD"])
            ),
        )
        if not (0 < profile.timeout_seconds <= 120):
            raise ValueError("Transport timeout must be in (0, 120]")
        if not (1 <= profile.max_attempts <= 5):
            raise ValueError("Transport max_attempts must be in 1..5")
        if not (0 <= profile.backoff_seconds <= 10):
            raise ValueError("Transport backoff_seconds must be in 0..10")
        if profile.proxy_strategy not in {
            "ENV_ONLY",
            "DIRECT_ONLY",
            "ENV_THEN_DIRECT",
            "DIRECT_THEN_ENV",
        }:
            raise ValueError("Transport proxy_strategy is invalid")
        if not (0 <= profile.jitter_seconds <= 10):
            raise ValueError("Transport jitter_seconds must be in 0..10")
        if any(item < 500 or item > 599 for item in profile.retry_status_codes):
            raise ValueError("Transport retry_status_codes must contain only 5xx codes")
        if not profile.retry_methods or any(
            item not in {"GET", "HEAD", "POST"} for item in profile.retry_methods
        ):
            raise ValueError("Transport retry_methods must be a non-empty subset of GET/HEAD/POST")
        result[profile.profile_id] = profile
    return result


def _build_resilient_client(profile: TransportProfile) -> ResilientHttpClient:
    lanes = {
        "ENV_ONLY": (True,),
        "DIRECT_ONLY": (False,),
        "ENV_THEN_DIRECT": (True, False),
        "DIRECT_THEN_ENV": (False, True),
    }[profile.proxy_strategy]
    return ResilientHttpClient(
        timeout_seconds=profile.timeout_seconds,
        follow_redirects=profile.follow_redirects,
        headers=profile.headers,
        lane_trust_env=lanes,
        max_attempts=profile.max_attempts,
        backoff_seconds=profile.backoff_seconds,
        jitter_seconds=profile.jitter_seconds,
        retry_status_codes=profile.retry_status_codes,
        retry_methods=profile.retry_methods,
    )


def _load_adapter_class(value: str) -> type[object]:
    module_name, _, class_name = value.rpartition(":")
    module = importlib.import_module(module_name)
    candidate: Any = getattr(module, class_name, None)
    if not inspect.isclass(candidate):
        raise ValueError(f"Provider adapter class is unavailable: {value}")
    return candidate


__all__ = ["ProviderFactory", "TransportProfile", "load_transport_profiles"]
