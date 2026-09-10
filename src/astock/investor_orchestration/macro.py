from __future__ import annotations

import email.utils
import html as html_lib
import re
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Literal, NamedTuple, Protocol
from urllib.parse import urljoin, urlparse
from zoneinfo import ZoneInfo

import httpx
import yaml
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from astock.investor_orchestration.models import (
    MacroObservation,
    MacroReleaseSnapshot,
)
from astock.investor_orchestration.store import InvestorOrchestrationStore
from astock.investor_orchestration.utils import content_hash, utc_now


class MacroReleaseSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    authority: Literal["NBS", "PBOC", "MOF", "NDRC", "OTHER_OFFICIAL"]
    release_family: str
    url: str
    parser: Literal["document-only", "regex-v1"] = "document-only"
    max_age_days: int = 90
    regex_observations: tuple[dict[str, Any], ...] = ()


class HttpResponseLike(Protocol):
    @property
    def content(self) -> bytes: ...

    @property
    def headers(self) -> Mapping[str, str]: ...

    @property
    def status_code(self) -> int: ...

    def raise_for_status(self) -> object: ...


class HttpClientLike(Protocol):
    def get(self, url: str, *, follow_redirects: bool = False) -> HttpResponseLike: ...


class MacroCaptureReceipt(BaseModel):
    """A fresh fetch check over an immutable release; never a new data vintage."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    schema_version: Literal["macro-capture-receipt-v1"] = "macro-capture-receipt-v1"
    release_id: str
    authority: Literal["NBS", "PBOC", "MOF", "NDRC", "OTHER_OFFICIAL"]
    release_family: str
    source_url: str
    final_source_url: str
    redirect_chain: tuple[str, ...]
    checked_at: AwareDatetime
    source_snapshot_id: str
    source_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    capture_policy_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    headers_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    content_type: str
    capture_mode: Literal["LIVE", "RECORDED"]


class MacroCaptureResult(NamedTuple):
    release: MacroReleaseSnapshot
    capture_artifact_id: str


def _macro_capture_source_id(values: Mapping[str, Any]) -> str:
    fields = (
        "authority",
        "release_family",
        "source_url",
        "final_source_url",
        "redirect_chain",
        "checked_at",
        "source_hash",
        "capture_policy_hash",
        "headers_hash",
        "content_type",
        "capture_mode",
    )
    return "official-macro-check:" + content_hash({key: values[key] for key in fields})


class ImmutableRawObjectStore:
    def __init__(self, root: str | Path = "runtime/objects/sha256") -> None:
        self.root = Path(root)

    def put(self, payload: bytes, *, suffix: str = ".bin") -> tuple[str, Path]:
        """Keep the legacy return shape, but use canonical atomic/hash-checked storage.

        Content type lives on the release, not in a parallel suffix-based object
        layout. Existing legacy objects are left untouched for provenance.
        """
        from astock.core.object_store import ObjectStore

        reference = ObjectStore(self.root).put_bytes(payload)
        return f"sha256:{reference.sha256}", reference.path


class OfficialMacroCaptureService:
    """Raw-first official macro release capture.

    A document-only release is useful as a current official source, but it is not
    silently promoted to a structured macro observation. Structured values are
    emitted only when a versioned parser spec succeeds.
    """

    def __init__(
        self,
        store: InvestorOrchestrationStore,
        *,
        config_path: str | Path = "configs/macro_current_releases_v1.yaml",
        object_store: ImmutableRawObjectStore | None = None,
        http_client: HttpClientLike | None = None,
    ) -> None:
        self.store = store
        self.config_path = Path(config_path)
        self.config = yaml.safe_load(self.config_path.read_text(encoding="utf-8"))
        self.object_store = object_store or ImmutableRawObjectStore(
            store.path.parent / "objects" / "sha256"
        )
        self.http_client = http_client

    def specs(self) -> tuple[MacroReleaseSpec, ...]:
        return tuple(MacroReleaseSpec.model_validate(item) for item in self.config["release_specs"])

    def spec(self, authority: str, release_family: str) -> MacroReleaseSpec:
        for item in self.specs():
            if item.authority == authority and item.release_family == release_family:
                return item
        raise KeyError(f"unknown macro release spec: {authority}/{release_family}")

    def capture(
        self,
        spec: MacroReleaseSpec,
        *,
        live: bool = False,
        recorded_content: bytes | None = None,
        recorded_headers: Mapping[str, str] | None = None,
        captured_at: datetime | None = None,
    ) -> MacroReleaseSnapshot:
        return self.capture_checked(
            spec,
            live=live,
            recorded_content=recorded_content,
            recorded_headers=recorded_headers,
            captured_at=captured_at,
        ).release

    def capture_checked(
        self,
        spec: MacroReleaseSpec,
        *,
        live: bool = False,
        recorded_content: bytes | None = None,
        recorded_headers: Mapping[str, str] | None = None,
        captured_at: datetime | None = None,
    ) -> MacroCaptureResult:
        spec = MacroReleaseSpec.model_validate(spec.model_dump())
        self._assert_official_url(spec)
        if live and (recorded_content is not None or recorded_headers is not None):
            raise ValueError("recorded inputs cannot be submitted as live capture")
        if live and captured_at is not None:
            raise ValueError("live captured_at cannot be supplied or backdated by the caller")
        now = captured_at or utc_now()
        if now.tzinfo is None or now.utcoffset() is None or now > utc_now():
            raise ValueError("captured_at must be timezone-aware and not in the future")
        now = now.astimezone(UTC)
        warnings: list[str] = []
        redirect_chain = [spec.url]
        final_url = spec.url
        if live:
            transport = self.config["transport"]
            client = self.http_client or httpx.Client(
                follow_redirects=False,
                timeout=httpx.Timeout(
                    float(transport["timeout_seconds"]),
                    connect=float(transport["connect_timeout_seconds"]),
                ),
                headers={
                    "User-Agent": "AStockMultiAgent/0.3 official-macro-capture",
                    "Accept": "application/json,text/html,text/plain,application/pdf,*/*",
                },
            )
            close_after = self.http_client is None
            try:
                for _ in range(int(transport["maximum_redirects"]) + 1):
                    response = client.get(final_url, follow_redirects=False)
                    actual_url = str(getattr(response, "url", final_url))
                    self._assert_official_url(spec.model_copy(update={"url": actual_url}))
                    headers = {
                        str(key).lower(): str(value) for key, value in response.headers.items()
                    }
                    status = getattr(response, "status_code", 200)
                    if status in {301, 302, 303, 307, 308}:
                        if "location" not in headers:
                            raise ValueError("official redirect is missing its destination")
                        target = urljoin(final_url, headers["location"])
                        self._assert_official_url(spec.model_copy(update={"url": target}))
                        if (
                            urlparse(final_url).scheme == "https"
                            and urlparse(target).scheme != "https"
                        ):
                            raise ValueError("official redirect cannot downgrade HTTPS")
                        if target in redirect_chain:
                            raise ValueError("official redirect loop")
                        redirect_chain.append(target)
                        final_url = target
                        continue
                    response.raise_for_status()
                    content = bytes(response.content)
                    final_url = actual_url
                    break
                else:
                    raise ValueError("official redirect budget exceeded")
            finally:
                if close_after and isinstance(client, httpx.Client):
                    client.close()
        else:
            if recorded_content is None:
                raise ValueError("recorded_content is required when live=False")
            content = recorded_content
            headers = {
                str(key).lower(): str(value) for key, value in (recorded_headers or {}).items()
            }
        if not content:
            raise ValueError("official macro response is empty")
        if len(content) > int(self.config["transport"]["maximum_decoded_bytes"]):
            raise ValueError("official macro decoded response exceeds the parsing budget")
        content_type_header = headers.get("content-type", "application/octet-stream")
        content_type = content_type_header.split(";", maxsplit=1)[0].strip().lower()
        suffix = self._suffix_for(content_type)
        raw_object_id, _ = self.object_store.put(content, suffix=suffix)
        source_hash = raw_object_id.split(":", maxsplit=1)[1]
        if live:
            # Availability cannot precede the actual response and durable raw capture.
            now = utc_now()
        try:
            published_at, published_at_source = self._published_at(
                headers,
                content,
                content_type_header,
                now,
            )
        except (TypeError, ValueError):
            published_at = now
            published_at_source = "CAPTURE_TIME"
            warnings.append("INVALID_PUBLISHED_AT_FALLBACK_TO_CAPTURE_TIME")
        if published_at_source == "CAPTURE_TIME":
            warnings.append("PUBLISHED_AT_FALLBACK_TO_CAPTURE_TIME")
        observations, parse_warnings = self._parse_observations(
            spec,
            content,
            content_type_header,
            published_at=published_at,
            captured_at=now,
            source_hash=source_hash,
        )
        observations = self._assign_vintages(spec, observations, captured_at=now)
        warnings.extend(parse_warnings)
        if spec.parser == "document-only":
            parse_status: Literal["PASS", "PARTIAL", "FAILED"] = "PARTIAL"
            warnings.append("OFFICIAL_DOCUMENT_CAPTURED_WITHOUT_STRUCTURED_VALUES")
        elif not observations:
            parse_status = "FAILED"
        elif parse_warnings:
            parse_status = "PARTIAL"
        else:
            parse_status = "PASS"
        if (now - published_at).days > spec.max_age_days:
            warnings.append("OFFICIAL_RELEASE_IS_OLDER_THAN_POLICY_MAX_AGE")
        capture_mode: Literal["LIVE", "RECORDED"] = "LIVE" if live else "RECORDED"
        capture_policy_hash = content_hash({"config": self.config, "spec": spec})
        body = {
            "authority": spec.authority,
            "release_family": spec.release_family,
            "source_url": spec.url,
            "source_hash": source_hash,
            "captured_at": now,
            "published_at": published_at,
            "content_type": content_type,
            "raw_object_id": raw_object_id,
            "capture_mode": capture_mode,
            "final_source_url": final_url,
            "redirect_chain": tuple(redirect_chain),
            "capture_policy_hash": capture_policy_hash,
            "observations": observations,
            "parse_status": parse_status,
            "warnings": tuple(sorted(set(warnings))),
        }
        release_hash = content_hash(body)
        snapshot = MacroReleaseSnapshot(
            release_id=f"macro-{uuid.uuid5(uuid.NAMESPACE_URL, release_hash)}",
            **body,
        )

        def semantics(release: MacroReleaseSnapshot) -> tuple[object, ...]:
            return (
                release.parse_status,
                tuple(
                    sorted(
                        (
                            item.series_key,
                            item.observation_period,
                            str(item.value),
                            item.unit or "",
                        )
                        for item in release.observations
                    )
                ),
            )

        exact = self.store.macro_release_by_source(
            authority=spec.authority,
            release_family=spec.release_family,
            source_hash=source_hash,
            capture_mode=capture_mode,
            capture_policy_hash=capture_policy_hash,
        )
        if exact is not None:
            if semantics(exact) != semantics(snapshot):
                raise ValueError(
                    "macro parser semantics changed within one immutable parser edition"
                )
            snapshot = exact
        else:
            prior_source_editions = tuple(
                release
                for release in self.store.macro_releases(
                    authority=spec.authority,
                    release_family=spec.release_family,
                )
                if release.source_hash == source_hash
            )
            if snapshot.parse_status != "PASS" and any(
                release.parse_status == "PASS" for release in prior_source_editions
            ):
                raise ValueError(
                    "macro parser semantics changed; explicit valid versioned reparse required"
                )
            if not self.store.save_macro_release(snapshot):
                existing = self.store.macro_release_by_source(
                    authority=spec.authority,
                    release_family=spec.release_family,
                    source_hash=source_hash,
                    capture_mode=capture_mode,
                    capture_policy_hash=capture_policy_hash,
                )
                if existing is None:
                    raise RuntimeError("macro release conflict without an existing snapshot")
                if semantics(existing) != semantics(snapshot):
                    raise ValueError(
                        "macro parser semantics changed within one immutable parser edition"
                    )
                snapshot = existing
        return self._record_capture_check(snapshot, body, headers, len(content))

    def _record_capture_check(
        self,
        release: MacroReleaseSnapshot,
        capture: Mapping[str, Any],
        headers: Mapping[str, str],
        byte_size: int,
    ) -> MacroCaptureResult:
        from astock.core.object_store import ObjectStore
        from astock.core.state import StateStore
        from astock.schemas import FetchStatus, SourceSnapshot

        values = {
            "release_id": release.release_id,
            "authority": capture["authority"],
            "release_family": capture["release_family"],
            "source_url": capture["source_url"],
            "final_source_url": capture["final_source_url"],
            "redirect_chain": capture["redirect_chain"],
            "checked_at": capture["captured_at"],
            "source_hash": capture["source_hash"],
            "capture_policy_hash": capture["capture_policy_hash"],
            "headers_hash": content_hash(dict(headers)),
            "content_type": capture["content_type"],
            "capture_mode": capture["capture_mode"],
        }
        source_id = _macro_capture_source_id(values)
        snapshot_id = f"{source_id}:{release.source_hash}"
        state = StateStore(self.store.path)
        state.register_snapshot(
            SourceSnapshot(
                snapshot_id=snapshot_id,
                source_id=source_id,
                object_sha256=release.source_hash,
                fetched_at=capture["captured_at"],
                available_to_system_at=capture["captured_at"],
                source_url=capture["final_source_url"],
                mime=capture["content_type"],
                byte_size=byte_size,
                headers_hash=values["headers_hash"],
                fetch_status=FetchStatus.SUCCEEDED,
                rights_status=f"{capture['capture_mode']}_OFFICIAL_MACRO_CHECK",
            )
        )
        receipt = MacroCaptureReceipt(source_snapshot_id=snapshot_id, **values)
        artifact_id = f"MacroCaptureReceipt:{content_hash(receipt)}"
        objects = ObjectStore(self.object_store.root)
        reference = objects.put_json(receipt.model_dump(mode="json"))
        state.register_artifact(
            artifact_id=artifact_id,
            artifact_type="MacroCaptureReceipt",
            schema_version=receipt.schema_version,
            object_hash=reference.sha256,
            input_hashes=sorted({receipt.source_hash, receipt.capture_policy_hash}),
        )
        return MacroCaptureResult(release, artifact_id)

    def verified_capture(self, artifact_id: str) -> MacroCaptureReceipt:
        """Recheck a fetch receipt against raw provenance without another request."""
        from astock.core.object_store import ObjectStore
        from astock.core.state import StateStore
        from astock.schemas import FetchStatus

        state = StateStore(self.store.path)
        objects = ObjectStore(self.object_store.root)
        record = state.artifact_record(artifact_id)
        if record is None or record["type"] != "MacroCaptureReceipt":
            raise ValueError("macro capture receipt is unavailable")
        receipt = MacroCaptureReceipt.model_validate_json(objects.get_bytes(record["object_hash"]))
        if (
            record["schema_version"] != receipt.schema_version
            or artifact_id != f"MacroCaptureReceipt:{content_hash(receipt)}"
            or record["input_hashes"] != sorted({receipt.source_hash, receipt.capture_policy_hash})
        ):
            raise ValueError("macro capture receipt registry identity or lineage is invalid")
        if receipt.checked_at > utc_now():
            raise ValueError("macro capture receipt is in the future")
        source = state.get_snapshot(receipt.source_snapshot_id)
        expected_source_id = _macro_capture_source_id(receipt.model_dump())
        if (
            source is None
            or source.source_id != expected_source_id
            or source.snapshot_id != f"{expected_source_id}:{receipt.source_hash}"
            or source.fetch_status is not FetchStatus.SUCCEEDED
            or source.object_sha256 != receipt.source_hash
            or source.fetched_at != receipt.checked_at
            or source.available_to_system_at != receipt.checked_at
            or source.source_url != receipt.final_source_url
            or source.headers_hash != receipt.headers_hash
            or source.mime != receipt.content_type
            or source.rights_status != f"{receipt.capture_mode}_OFFICIAL_MACRO_CHECK"
        ):
            raise ValueError("macro capture scope does not match its canonical raw snapshot")
        if len(objects.get_bytes(receipt.source_hash)) != source.byte_size:
            raise ValueError("macro capture source byte size is invalid")
        release = self.store.macro_release_by_source(
            authority=receipt.authority,
            release_family=receipt.release_family,
            source_hash=receipt.source_hash,
            capture_mode=receipt.capture_mode,
            capture_policy_hash=receipt.capture_policy_hash,
        )
        if (
            release is None
            or release.release_id != receipt.release_id
            or release.captured_at > receipt.checked_at
        ):
            raise ValueError("macro capture release family or first-visibility lineage is invalid")
        if not receipt.redirect_chain:
            raise ValueError("macro capture redirect trace is missing")
        if str(httpx.URL(receipt.redirect_chain[0])) != str(httpx.URL(receipt.source_url)) or str(
            httpx.URL(receipt.redirect_chain[-1])
        ) != str(httpx.URL(receipt.final_source_url)):
            raise ValueError("macro capture redirect trace does not bind its endpoints")
        for url in receipt.redirect_chain:
            self._assert_official_url(
                MacroReleaseSpec(
                    authority=receipt.authority,
                    release_family=receipt.release_family,
                    url=url,
                    parser="document-only",
                )
            )
        return receipt

    def capture_configured(
        self,
        *,
        authority: str | None = None,
        live: bool = True,
    ) -> tuple[MacroReleaseSnapshot, ...]:
        results: list[MacroReleaseSnapshot] = []
        for spec in self.specs():
            if authority is not None and spec.authority != authority:
                continue
            results.append(self.capture(spec, live=live))
        return tuple(results)

    def observation_at(
        self,
        *,
        authority: str,
        release_family: str,
        series_key: str,
        observation_period: str,
        as_of: datetime,
        vintage: Literal["FIRST_RELEASE", "FIRST_OBSERVED", "LATEST"] = "LATEST",
    ) -> MacroObservation | None:
        if as_of.tzinfo is None or as_of.utcoffset() is None:
            raise ValueError("macro vintage as_of must be timezone-aware")
        if vintage not in {"FIRST_RELEASE", "FIRST_OBSERVED", "LATEST"}:
            raise ValueError("unknown macro vintage selection")
        as_of = as_of.astimezone(UTC)
        observation_period = self._normalize_period(observation_period)
        candidates = [
            observation
            for release in self.store.macro_releases(
                authority=authority,
                release_family=release_family,
                available_at=as_of,
            )
            for observation in release.observations
            if observation.series_key == series_key
            and observation.observation_period == observation_period
            and observation.available_to_system_at <= as_of
        ]
        if vintage == "FIRST_RELEASE":
            candidates = [
                item for item in candidates if item.first_release and item.first_release_verified
            ]
        if not candidates:
            return None

        # Availability filters what was known; publication orders source editions.
        # A late download of an older edition is not a new economic revision.
        def rank(item: MacroObservation) -> tuple[datetime, datetime]:
            if vintage == "FIRST_OBSERVED":
                return item.available_to_system_at, item.published_at
            return item.published_at, item.available_to_system_at

        selector = max if vintage == "LATEST" else min
        selected_rank = selector(rank(item) for item in candidates)
        selected = [item for item in candidates if rank(item) == selected_rank]
        if len({(item.value, item.unit) for item in selected}) != 1:
            raise ValueError("conflicting macro values have the same vintage timestamps")
        return min(selected, key=lambda item: item.observation_id)

    def latest_valid_snapshot(
        self,
        *,
        authority: str,
        release_family: str,
        as_of: datetime,
        require_structured: bool = True,
    ) -> MacroReleaseSnapshot | None:
        if as_of.tzinfo is None or as_of.utcoffset() is None:
            raise ValueError("macro snapshot as_of must be timezone-aware")
        as_of = as_of.astimezone(UTC)
        spec = self.spec(authority, release_family)
        cutoff = as_of - timedelta(days=spec.max_age_days)
        candidates = [
            release
            for release in self.store.macro_releases(
                authority=authority,
                release_family=release_family,
                available_at=as_of,
            )
            if release.parse_status != "FAILED"
            and release.captured_at <= as_of
            and release.published_at <= as_of
            and release.published_at >= cutoff
            and (release.observations or not require_structured)
        ]
        if not candidates:
            return None
        periods: dict[str, str] = {}
        frequencies: set[str] = set()
        for release in candidates:
            normalized = [
                self._normalize_period(item.observation_period) for item in release.observations
            ]
            frequencies.update(self._period_frequency(period) for period in normalized)
            periods[release.release_id] = max(normalized, default="")
        if len(frequencies) > 1:
            raise ValueError("macro frequency mismatch requires separate release families")
        # First choose the economic period, then its newest source edition visible
        # at the cutoff. Fetch order must not turn an old period into current data.
        best = max(
            (periods[item.release_id], item.published_at, item.captured_at) for item in candidates
        )
        selected = [
            item
            for item in candidates
            if (periods[item.release_id], item.published_at, item.captured_at) == best
        ]
        signatures = {
            tuple(
                sorted(
                    (obs.series_key, obs.observation_period, str(obs.value), obs.unit or "")
                    for obs in item.observations
                )
            )
            for item in selected
        }
        if len(signatures) != 1:
            raise ValueError("conflicting macro releases have the same period and vintage")
        return min(selected, key=lambda item: item.release_id)

    def _assert_official_url(self, spec: MacroReleaseSpec) -> None:
        parsed = urlparse(spec.url)
        if parsed.scheme not in {"http", "https"} or parsed.username or parsed.password:
            raise ValueError("official URL requires HTTP(S) without user information")
        if parsed.port not in {None, 443 if parsed.scheme == "https" else 80}:
            raise ValueError("official URL uses an unapproved port")
        if parsed.fragment or any(ord(char) < 33 for char in spec.url):
            raise ValueError("official URL contains an ambiguous fragment or control character")
        host = (parsed.hostname or "").lower()
        allowed = {
            str(item).lower() for item in self.config["official_hosts"].get(spec.authority, [])
        }
        if host not in allowed:
            raise ValueError(f"URL host {host!r} is not approved for authority {spec.authority}")

    def _parse_observations(
        self,
        spec: MacroReleaseSpec,
        content: bytes,
        content_type: str,
        *,
        published_at: datetime,
        captured_at: datetime,
        source_hash: str,
    ) -> tuple[tuple[MacroObservation, ...], tuple[str, ...]]:
        if spec.parser == "document-only":
            return (), ()
        text, decode_warning = self._decode_text(content, content_type)
        if "html" in content_type.lower():
            text = re.sub(r"(?is)<(?:script|style)\b.*?</(?:script|style)>", " ", text)
            text = html_lib.unescape(re.sub(r"(?s)<[^>]+>", " ", text))
            text = re.sub(r"\s+", " ", text)
        observations: dict[tuple[str, str], MacroObservation] = {}
        warnings: list[str] = [] if decode_warning is None else [decode_warning]
        conflicts: set[tuple[str, str]] = set()
        match_count = 0
        maximum_matches = int(self.config["transport"].get("maximum_observation_matches", 1000))
        for index, rule in enumerate(spec.regex_observations):
            pattern, series_key = rule.get("pattern"), rule.get("series_key")
            if not isinstance(pattern, str) or not isinstance(series_key, str) or not series_key:
                warnings.append(f"INVALID_REGEX_RULE:{index}")
                continue
            rule_text, inherited_period, scope_warnings = self._scoped_rule_text(
                rule, text, series_key
            )
            warnings.extend(scope_warnings)
            if scope_warnings and not rule_text:
                continue
            try:
                compiled = re.compile(pattern, flags=re.IGNORECASE | re.MULTILINE)
            except re.error:
                warnings.append(f"INVALID_REGEX_PATTERN:{series_key}")
                continue
            found = False
            for match in compiled.finditer(rule_text):
                found = True
                match_count += 1
                if match_count > maximum_matches:
                    return (), tuple(sorted({*warnings, "OBSERVATION_MATCH_BUDGET_EXCEEDED"}))
                try:
                    period, value, unit = self._parse_match(
                        rule, match, inherited_period=inherited_period
                    )
                except ValueError as exc:
                    warnings.append(f"{exc}:{series_key}")
                    continue
                key = (series_key, period)
                previous = observations.get(key)
                if previous is not None and (previous.value, previous.unit) != (value, unit):
                    conflicts.add(key)
                    observations.pop(key)
                    warnings.append(f"CONFLICTING_OBSERVATION:{series_key}:{period}")
                if key in conflicts or previous is not None:
                    continue
                observation_body = {
                    "authority": spec.authority,
                    "release_family": spec.release_family,
                    "series_key": series_key,
                    "observation_period": period,
                    "value": value,
                    "unit": unit,
                    "published_at": published_at,
                    "effective_at": published_at,
                    "ingested_at": captured_at,
                    "available_to_system_at": captured_at,
                    "revision": "pending-vintage",
                    "source_url": spec.url,
                    "source_hash": source_hash,
                    "first_release": False,
                    "first_release_verified": False,
                }
                observations[key] = MacroObservation(
                    observation_id=(
                        "macro-observation-"
                        f"{uuid.uuid5(uuid.NAMESPACE_URL, content_hash(observation_body))}"
                    ),
                    **observation_body,
                )
            if not found:
                warnings.append(f"OBSERVATION_NOT_FOUND:{series_key}")
        return tuple(observations.values()), tuple(sorted(set(warnings)))

    @staticmethod
    def _scoped_rule_text(
        rule: Mapping[str, Any], text: str, series_key: str
    ) -> tuple[str, str | None, tuple[str, ...]]:
        start_pattern = rule.get("scope_start_pattern")
        end_pattern = rule.get("scope_end_pattern")
        scope_period_pattern = rule.get("scope_period_pattern")
        document_period_pattern = rule.get("document_period_pattern")
        if (
            start_pattern is None
            and end_pattern is None
            and scope_period_pattern is None
            and document_period_pattern is None
        ):
            return text, None, ()
        if not isinstance(start_pattern, str) or not isinstance(end_pattern, str):
            return "", None, (f"INVALID_OBSERVATION_SCOPE:{series_key}",)
        try:
            start = re.compile(start_pattern, flags=re.IGNORECASE | re.MULTILINE).search(text)
            end_compiled = re.compile(end_pattern, flags=re.IGNORECASE | re.MULTILINE)
        except re.error:
            return "", None, (f"INVALID_OBSERVATION_SCOPE_REGEX:{series_key}",)
        if start is None:
            return "", None, (f"OBSERVATION_SCOPE_NOT_FOUND:{series_key}",)
        end = end_compiled.search(text, start.end())
        if end is None or end.start() <= start.end():
            return "", None, (f"OBSERVATION_SCOPE_END_NOT_FOUND:{series_key}",)
        scoped = text[start.end() : end.start()]
        period_pattern = scope_period_pattern or document_period_pattern
        if period_pattern is None:
            return scoped, None, ()
        if not isinstance(period_pattern, str):
            return "", None, (f"INVALID_OBSERVATION_PERIOD_SCOPE:{series_key}",)
        period_text = text if document_period_pattern is not None else scoped
        try:
            period_match = re.compile(period_pattern, flags=re.IGNORECASE | re.MULTILINE).search(
                period_text
            )
        except re.error:
            return "", None, (f"INVALID_OBSERVATION_PERIOD_REGEX:{series_key}",)
        if period_match is None:
            return "", None, (f"OBSERVATION_PERIOD_MISSING:{series_key}",)
        raw_period = period_match.groupdict().get("period")
        if raw_period is None:
            try:
                raw_period = period_match.group(1)
            except IndexError:
                return "", None, (f"OBSERVATION_PERIOD_MISSING:{series_key}",)
        if not str(raw_period).strip():
            return "", None, (f"OBSERVATION_PERIOD_MISSING:{series_key}",)
        return scoped, str(raw_period), ()

    @classmethod
    def _parse_match(
        cls,
        rule: Mapping[str, Any],
        match: re.Match[str],
        *,
        inherited_period: str | None = None,
    ) -> tuple[str, Decimal | str, str | None]:
        try:
            raw_value = match.groupdict().get("value") or match.group(1)
        except IndexError as exc:
            raise ValueError("VALUE_GROUP_MISSING") from exc
        value_type = str(rule.get("value_type", "decimal")).lower()
        unit = rule.get("unit")
        if unit is not None and (not isinstance(unit, str) or not unit.strip()):
            raise ValueError("OBSERVATION_UNIT_MISSING")
        if value_type == "text":
            value: Decimal | str = str(raw_value).strip()
            if not value:
                raise ValueError("EMPTY_TEXT_VALUE")
        elif value_type == "decimal":
            if unit is None:
                raise ValueError("OBSERVATION_UNIT_MISSING")
            try:
                value = Decimal(
                    str(raw_value).strip().replace(",", "").replace("，", "").replace("%", "")
                )
            except (InvalidOperation, ValueError) as exc:
                raise ValueError("INVALID_DECIMAL_VALUE") from exc
            if not value.is_finite():
                raise ValueError("NONFINITE_DECIMAL_VALUE")
            try:
                minimum = None if rule.get("min_value") is None else Decimal(str(rule["min_value"]))
                maximum = None if rule.get("max_value") is None else Decimal(str(rule["max_value"]))
            except InvalidOperation as exc:
                raise ValueError("INVALID_VALUE_BOUNDS") from exc
            if any(bound is not None and not bound.is_finite() for bound in (minimum, maximum)):
                raise ValueError("INVALID_VALUE_BOUNDS")
            if minimum is not None and maximum is not None and minimum > maximum:
                raise ValueError("INVALID_VALUE_BOUNDS")
            if (minimum is not None and value < minimum) or (
                maximum is not None and value > maximum
            ):
                raise ValueError("VALUE_OUT_OF_RANGE")
        else:
            raise ValueError("UNSUPPORTED_VALUE_TYPE")
        raw_period = match.groupdict().get("period") or rule.get("period") or inherited_period
        if raw_period is None or not str(raw_period).strip():
            raise ValueError("OBSERVATION_PERIOD_MISSING")
        try:
            period = cls._normalize_period(str(raw_period))
        except ValueError as exc:
            raise ValueError("INVALID_OBSERVATION_PERIOD") from exc
        return period, value, unit.strip() if unit is not None else None

    def _assign_vintages(
        self,
        spec: MacroReleaseSpec,
        observations: tuple[MacroObservation, ...],
        *,
        captured_at: datetime,
    ) -> tuple[MacroObservation, ...]:
        prior_keys = {
            (item.series_key, item.observation_period)
            for release in self.store.macro_releases(
                authority=spec.authority,
                release_family=spec.release_family,
                available_at=captured_at,
            )
            for item in release.observations
            if item.available_to_system_at <= captured_at
        }
        assigned: list[MacroObservation] = []
        for observation in observations:
            first_observed = (
                observation.series_key,
                observation.observation_period,
            ) not in prior_keys
            revision = (
                "first-observed" if first_observed else f"revision:{observation.source_hash[:12]}"
            )
            body = observation.model_dump(mode="python", exclude={"observation_id"})
            # First observed locally does not authenticate an issuer's first release.
            body.update(
                {
                    "first_release": False,
                    "first_release_verified": False,
                    "first_observed": first_observed,
                    "revision": revision,
                }
            )
            assigned.append(
                MacroObservation(
                    observation_id=(
                        f"macro-observation-{uuid.uuid5(uuid.NAMESPACE_URL, content_hash(body))}"
                    ),
                    **body,
                )
            )
        return tuple(assigned)

    @staticmethod
    def _decode_text(content: bytes, content_type: str) -> tuple[str, str | None]:
        charset_match = re.search(r"charset\s*=\s*['\"]?([^;'\"\s]+)", content_type, re.I)
        candidates = [charset_match.group(1)] if charset_match else []
        candidates.extend(["utf-8", "gb18030"])
        tried: set[str] = set()
        for candidate in candidates:
            normalized = candidate.lower()
            if normalized in tried:
                continue
            tried.add(normalized)
            try:
                return content.decode(candidate), None
            except (LookupError, UnicodeDecodeError):
                continue
        return content.decode("utf-8", errors="replace"), "CONTENT_DECODING_LOSSY"

    @staticmethod
    def _normalize_period(value: str) -> str:
        """Canonical economic period; publication time is never a substitute."""
        text = value.strip()
        month = re.fullmatch(r"(\d{4})\s*[年\-/]\s*(\d{1,2})\s*月?", text)
        if month:
            year, number = int(month.group(1)), int(month.group(2))
            datetime(year, number, 1)
            return f"{year:04d}-{number:02d}"
        quarter = re.fullmatch(r"(\d{4})(?:-Q([1-4])|\s*年?\s*(?:第)?([1-4])\s*季度)", text)
        if quarter:
            year = int(quarter.group(1))
            datetime(year, 1, 1)
            return f"{year:04d}-Q{quarter.group(2) or quarter.group(3)}"
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
            datetime.fromisoformat(text)
            return text
        if re.fullmatch(r"\d{4}", text):
            datetime(int(text), 1, 1)
            return text
        raise ValueError("invalid macro observation period")

    @staticmethod
    def _period_frequency(value: str) -> str:
        if "-Q" in value:
            return "QUARTER"
        return {4: "YEAR", 7: "MONTH", 10: "DAY"}[len(value)]

    def _published_at(
        self,
        headers: Mapping[str, str],
        content: bytes,
        content_type: str,
        fallback: datetime,
    ) -> tuple[datetime, Literal["LAST_MODIFIED", "HTML_META", "CAPTURE_TIME"]]:
        raw = headers.get("last-modified")
        if raw:
            try:
                parsed = email.utils.parsedate_to_datetime(raw)
            except (TypeError, ValueError):
                parsed = None
            if parsed is not None:
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=UTC)
                parsed = parsed.astimezone(UTC)
                if parsed <= fallback:
                    return parsed, "LAST_MODIFIED"

        if "html" in content_type.lower():
            text, _ = self._decode_text(content, content_type)
            for tag in re.findall(r"(?is)<meta\b[^>]*>", text):
                attributes: dict[str, str] = {}
                for match in re.finditer(
                    r"""(?is)([a-zA-Z_:][\w:.-]*)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))""",
                    tag,
                ):
                    value = next(
                        (group for group in match.groups()[1:] if group is not None),
                        "",
                    )
                    attributes[match.group(1).lower()] = html_lib.unescape(value).strip()
                metadata_name = (attributes.get("name") or attributes.get("property") or "").lower()
                if metadata_name not in {
                    "pubdate",
                    "publishdate",
                    "publicationdate",
                    "article:published_time",
                }:
                    continue
                value = attributes.get("content")
                if not value:
                    continue
                parsed = self._parse_publication_datetime(value)
                if parsed is not None and parsed <= fallback:
                    return parsed, "HTML_META"
        return fallback, "CAPTURE_TIME"

    def _parse_publication_datetime(self, value: str) -> datetime | None:
        text = value.strip()
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            parsed = None
        if parsed is None:
            for format_string in (
                "%Y/%m/%d %H:%M:%S",
                "%Y/%m/%d %H:%M",
                "%Y-%m-%d %H:%M:%S",
                "%Y-%m-%d %H:%M",
            ):
                try:
                    parsed = datetime.strptime(text, format_string)
                    break
                except ValueError:
                    continue
        if parsed is None:
            return None
        if parsed.tzinfo is None:
            publication_timezone = ZoneInfo(
                str(self.config.get("publication_timezone", "Asia/Shanghai"))
            )
            parsed = parsed.replace(tzinfo=publication_timezone)
        return parsed.astimezone(UTC)

    @staticmethod
    def _suffix_for(content_type: str) -> str:
        if "json" in content_type:
            return ".json"
        if "html" in content_type:
            return ".html"
        if "pdf" in content_type:
            return ".pdf"
        if content_type.startswith("text/"):
            return ".txt"
        return ".bin"
