"""Provider runtime factory and versioned transport profiles.

The registry/configuration is the single source of truth for provider construction.
Core services ask for a capability or provider id; they do not know constructor details.
"""

from __future__ import annotations

import importlib
import inspect
import json
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, TypeVar

import httpx
import yaml

from astock.core.errors import AStockError
from astock.core.hashing import content_hash
from astock.core.object_store import ObjectStore
from astock.core.source_resilience import (
    SourceCircuitBreaker,
    SourceFailureClass,
    classify_source_error,
    load_source_resilience_policy,
)
from astock.core.source_router import SourceAccessRouter
from astock.core.state import StateStore
from astock.providers.config import get_provider, load_provider_registry
from astock.providers.dialects import ProviderDialect
from astock.providers.http_resilience import HttpClientLike, ResilientHttpClient
from astock.providers.self_probe import checked_capabilities as checked_capabilities_for_probe
from astock.schemas import (
    AccessTransport,
    CompletenessSemantics,
    ProviderDefinition,
    ProviderHealthStatus,
    ProviderProbeFailureCode,
    ProviderProbeReport,
    ProviderRegistry,
    ProviderTransport,
    SourceAccessRequest,
    TransportCapability,
)

_T = TypeVar("_T")


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
        self.source_breaker = SourceCircuitBreaker(state)
        self._instances: dict[str, object] = {}

    def capability_health_status(
        self,
        provider_id: str,
        capability: str,
    ) -> ProviderHealthStatus:
        """Interpret provider probes only for the capability they actually exercised."""

        row, head = self.state.get_provider_probe_health_snapshot(provider_id)
        if row is None:
            return (
                ProviderHealthStatus.CORRUPT
                if head is not None
                else ProviderHealthStatus.NOT_PROBED
            )
        try:
            row_status = ProviderHealthStatus(str(row.get("status")))
        except ValueError:
            return ProviderHealthStatus.CORRUPT
        if row_status is ProviderHealthStatus.CORRUPT:
            return ProviderHealthStatus.CORRUPT
        report_hash = str(row.get("report_object_hash") or "")
        latest_probe_id = str(row.get("latest_probe_id") or "")
        report_artifact_id = str(row.get("report_artifact_id") or "")
        if not report_hash and not latest_probe_id and not report_artifact_id:
            return (
                ProviderHealthStatus.CORRUPT
                if head is not None
                else ProviderHealthStatus.NOT_PROBED
            )
        if not report_hash or not latest_probe_id or not report_artifact_id or head is None:
            return ProviderHealthStatus.CORRUPT
        if str(head.get("latest_probe_id") or "") != latest_probe_id:
            return ProviderHealthStatus.CORRUPT
        try:
            artifact = self.state.artifact_record(report_artifact_id)
            event = self.state.get_provider_probe_event(latest_probe_id)
            if artifact is None or event is None or not self.objects.verify(report_hash):
                return ProviderHealthStatus.CORRUPT
            report = ProviderProbeReport.model_validate_json(self.objects.get_bytes(report_hash))
        except (AStockError, OSError, TypeError, ValueError):
            return ProviderHealthStatus.CORRUPT
        provider = get_provider(self.registry, provider_id)
        expected_checked = checked_capabilities_for_probe(provider)
        expected_gaps = list(
            dict.fromkeys(
                [
                    *self.registry.capability_gaps,
                    *[item for item in provider.capabilities if item not in expected_checked],
                ]
            )
        )
        expected_failure_code = report.failure_code.value if report.failure_code else None
        expected_inputs = [report.capability_hash]
        try:
            invalid_chain = (
                report.probe_id != latest_probe_id
                or report_artifact_id != f"provider-probe:{report.probe_id}"
                or report.provider_id != provider_id
                or report.registry_version != self.registry.registry_version
                or report.capability_hash != content_hash(provider)
                or report.checked_capabilities != expected_checked
                or report.capability_gaps != expected_gaps
                or (
                    bool(set(provider.capabilities).difference(expected_checked))
                    and report.status is ProviderHealthStatus.HEALTHY
                )
                or (
                    report.failure_code is ProviderProbeFailureCode.CAPABILITY_NOT_PROBED
                    and (
                        not set(provider.capabilities).difference(expected_checked)
                        or report.status is not ProviderHealthStatus.DEGRADED
                    )
                )
                or str(row.get("capability_hash") or "") != report.capability_hash
                or str(row.get("registry_version") or "") != report.registry_version
                or str(row.get("probe_mode") or "") != report.probe_mode.value
                or str(row.get("status") or "") != report.status.value
                or str(row.get("last_probe_at") or "") != report.completed_at.isoformat()
                or row.get("last_error_class") != expected_failure_code
                or row.get("failure_code") != expected_failure_code
                or int(row.get("failure_count") or 0)
                != int(head.get("failure_count") or 0)
                or str(artifact.get("artifact_id") or "") != report_artifact_id
                or str(artifact.get("type") or "") != "ProviderProbeReport"
                or str(artifact.get("schema_version") or "") != report.schema_version
                or str(artifact.get("object_hash") or "") != report_hash
                or artifact.get("input_hashes") != expected_inputs
                or str(event.get("provider_id") or "") != report.provider_id
                or str(event.get("registry_version") or "") != report.registry_version
                or str(event.get("capability_hash") or "") != report.capability_hash
                or str(event.get("probe_mode") or "") != report.probe_mode.value
                or str(event.get("status") or "") != report.status.value
                or str(event.get("completed_at") or "")
                != report.completed_at.isoformat()
                or event.get("failure_code") != expected_failure_code
                or int(event.get("failure_count") or 0) != report.failure_count
                or str(event.get("report_artifact_id") or "") != report_artifact_id
                or str(event.get("report_object_hash") or "") != report_hash
                or str(event.get("artifact_type") or "") != "ProviderProbeReport"
                or str(event.get("artifact_schema_version") or "")
                != report.schema_version
                or str(event.get("artifact_object_hash") or "") != report_hash
                or str(event.get("artifact_input_hashes_json") or "")
                != json.dumps(expected_inputs, separators=(",", ":"))
            )
        except (TypeError, ValueError):
            return ProviderHealthStatus.CORRUPT
        if invalid_chain:
            return ProviderHealthStatus.CORRUPT
        if capability not in report.checked_capabilities:
            return ProviderHealthStatus.NOT_PROBED
        if (
            report.status is ProviderHealthStatus.DEGRADED
            and report.failure_code is ProviderProbeFailureCode.CAPABILITY_NOT_PROBED
        ):
            return ProviderHealthStatus.HEALTHY
        return report.status

    def catalog_capabilities(self, capability: str) -> list[TransportCapability]:
        """Project provider-registry metadata into the shared capability-routing contract."""

        return [
            self._catalog_capability(definition, capability)
            for definition in self.registry.providers
            if capability in definition.capabilities
        ]

    def definitions_for_capability(
        self,
        capability: str,
        *,
        formal_use: bool = False,
        require_complete: bool = False,
        source_hint: str | None = None,
    ) -> list[ProviderDefinition]:
        """Return capability candidates ranked by SourceAccessRouter, not provider priority."""

        matches = {
            item.provider_id: item
            for item in self.registry.providers
            if capability in item.capabilities
        }
        if not matches:
            return []
        ranked = SourceAccessRouter(self.state).rank(
            SourceAccessRequest(
                source_id=source_hint,
                requested_capability=capability,
                formal_use=formal_use,
                require_complete=require_complete,
            ),
            self.catalog_capabilities(capability),
        )
        return [matches[item.source_id] for item in ranked if item.available]

    def create_for_capability(
        self,
        capability: str,
        expected_type: type[_T],
        *,
        formal_use: bool = False,
        require_complete: bool = False,
        source_hint: str | None = None,
    ) -> _T:
        """Create the highest-ranked compatible adapter for one logical capability."""

        definitions = self.definitions_for_capability(
            capability,
            formal_use=formal_use,
            require_complete=require_complete,
            source_hint=source_hint,
        )
        for definition in definitions:
            provider = self.create(definition.provider_id)
            if isinstance(provider, expected_type):
                return provider
        raise ValueError(
            f"No available provider adapter satisfies capability {capability} "
            f"and expected type {expected_type.__name__}"
        )

    def _catalog_capability(
        self,
        definition: ProviderDefinition,
        capability: str,
    ) -> TransportCapability:
        health = self.capability_health_status(definition.provider_id, capability)
        health_status = health.value
        available = health not in {
            ProviderHealthStatus.UNAVAILABLE,
            ProviderHealthStatus.CORRUPT,
        }
        semantics = definition.completeness_semantics.get(
            capability, CompletenessSemantics.NOT_APPLICABLE
        )
        completeness_score = (
            Decimal("1")
            if semantics
            in {
                CompletenessSemantics.EXACT_ITEM,
                CompletenessSemantics.WINDOW_EXHAUSTIVE,
                CompletenessSemantics.FULL_UNIVERSE,
                CompletenessSemantics.CONTINUOUS_SERIES,
            }
            else Decimal("0.25")
            if semantics is CompletenessSemantics.DISCOVERY_ONLY
            else Decimal("0.5")
        )
        # A provider-level latest snapshot does not prove that this specific capability
        # has a reusable local artifact. Capability services perform exact cache/release
        # validation before setting local availability; keep the generic catalog neutral
        # rather than letting an unrelated fresh snapshot distort routing.
        local_score = Decimal("0")
        freshness_score = Decimal("0.5")
        cost_efficiency = {
            "LOW": Decimal("1"),
            "MEDIUM": Decimal("0.5"),
            "HIGH": Decimal("0"),
        }[definition.cost_class]
        return TransportCapability(
            source_id=definition.provider_id,
            transport=AccessTransport.API,
            requested_capabilities=[capability],
            available=available,
            reason=(
                f"provider catalog {self.registry.registry_version}; "
                f"completeness={semantics.value}"
            ),
            officiality=definition.officiality.value,
            source_class=definition.source_class,
            formal_eligible=capability in definition.formal_capabilities,
            completeness_semantics=semantics,
            completeness_score=completeness_score,
            local_availability_score=local_score,
            independence_score=Decimal("1"),
            independence_group=definition.independence_group,
            health_status=health_status,
            freshness_score=freshness_score,
            latency_ms=0,
            cost_efficiency_score=cost_efficiency,
            auth_ease_score=Decimal("1"),
            retryable_failure=health_status == ProviderHealthStatus.DEGRADED.value,
        )

    def claim_capability_attempt(
        self,
        provider_id: str,
        capability: str,
        *,
        live: bool,
    ) -> bool:
        if not live:
            return True
        health = self.capability_health_status(provider_id, capability)
        if health in {
            ProviderHealthStatus.UNAVAILABLE,
            ProviderHealthStatus.CORRUPT,
        }:
            return False
        return self.source_breaker.claim_attempt(provider_id, capability)

    def record_capability_success(
        self,
        provider_id: str,
        capability: str,
        *,
        live: bool,
    ) -> None:
        if live:
            self.source_breaker.record_success(provider_id, capability)

    def record_capability_failure(
        self,
        provider_id: str,
        capability: str,
        error: BaseException | SourceFailureClass,
        *,
        live: bool,
    ) -> None:
        if not live:
            return
        failure_class = (
            error if isinstance(error, SourceFailureClass) else classify_source_error(error)
        )
        retry_after_seconds: int | None = None
        if isinstance(error, AStockError):
            raw_retry_after = error.details.get("retry_after_seconds")
            if isinstance(raw_retry_after, (int, float)) and not isinstance(raw_retry_after, bool):
                retry_after_seconds = max(0, int(raw_retry_after))
        self.source_breaker.record_failure(
            provider_id,
            capability,
            failure_class,
            retry_after_seconds=retry_after_seconds,
        )

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
        return _build_resilient_client(
            profile,
            elapsed_budget_seconds=float(
                self.source_breaker.policy.default_elapsed_budget_seconds
            ),
        )


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
    policy = load_source_resilience_policy(root / "configs" / "source_resilience.yaml")
    return _build_resilient_client(
        profile,
        elapsed_budget_seconds=float(policy.default_elapsed_budget_seconds),
    )


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


def _build_resilient_client(
    profile: TransportProfile,
    *,
    elapsed_budget_seconds: float,
) -> ResilientHttpClient:
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
        elapsed_budget_seconds=elapsed_budget_seconds,
    )


def _load_adapter_class(value: str) -> type[object]:
    module_name, _, class_name = value.rpartition(":")
    module = importlib.import_module(module_name)
    candidate: Any = getattr(module, class_name, None)
    if not inspect.isclass(candidate):
        raise ValueError(f"Provider adapter class is unavailable: {value}")
    return candidate


__all__ = ["ProviderFactory", "TransportProfile", "load_transport_profiles"]
