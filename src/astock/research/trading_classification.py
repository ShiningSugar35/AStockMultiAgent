"""Immutable point-in-time trading classification releases for research runtime."""

from __future__ import annotations

from datetime import datetime
from typing import Literal, TypedDict

from astock.core.hashing import canonical_json_bytes, sha256_bytes
from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.schemas.research_runtime import (
    TradingClassificationDraft,
    TradingClassificationRecord,
    TradingClassificationRelease,
    TradingClassificationStatus,
)


class TradingClassificationStatusReport(TypedDict):
    status: Literal["READY", "NEEDS_INFO"]
    reason_codes: list[str]
    artifact_id: str | None
    release_id: str | None
    broker_execution_allowed: bool


class TradingClassificationAuditReport(TypedDict):
    status: Literal["PASS", "FAIL"]
    finding_codes: list[str]
    artifact_id: str | None
    release_id: str | None
    broker_execution_allowed: bool


class TradingClassificationService:
    """Freeze and verify board/risk/suspension/special-regime classification."""

    def __init__(self, state: StateStore, objects: ObjectStore) -> None:
        self.state = state
        self.objects = objects

    def freeze(self, draft: TradingClassificationDraft) -> TradingClassificationRecord:
        source_hashes: list[str] = []
        for artifact_id in draft.source_artifact_ids:
            record = self.state.artifact_record(artifact_id)
            if record is None:
                raise ValueError(f"unknown classification source artifact: {artifact_id}")
            object_hash = str(record["object_hash"])
            if not self.objects.verify(object_hash):
                raise ValueError(f"classification source object unavailable: {artifact_id}")
            source_hashes.append(object_hash)
        if draft.corporate_action_baseline_artifact_id is not None:
            baseline = self.state.artifact_record(draft.corporate_action_baseline_artifact_id)
            if baseline is None:
                raise ValueError("corporate-action baseline artifact is unknown")
            baseline_hash = str(baseline["object_hash"])
            if not self.objects.verify(baseline_hash):
                raise ValueError("corporate-action baseline object is unavailable")
            if draft.corporate_action_baseline_artifact_id not in draft.source_artifact_ids:
                raise ValueError("corporate-action baseline must be one of the frozen sources")
        source_hashes = sorted(set(source_hashes))

        identity_payload = {
            "schema_version": "trading-classification-release-v1",
            "company_id": draft.company_id,
            "market": draft.market.value,
            "symbol": draft.symbol,
            "as_of": draft.as_of.isoformat(),
            "effective_from": draft.effective_from.isoformat(),
            "valid_until": draft.valid_until.isoformat(),
            "classification": draft.classification.model_dump(mode="json"),
            "special_no_price_limit": draft.special_no_price_limit,
            "corporate_action_baseline_artifact_id": draft.corporate_action_baseline_artifact_id,
            "source_artifact_ids": draft.source_artifact_ids,
            "source_object_hashes": sorted(source_hashes),
            "status": draft.status.value,
            "reason_codes": sorted(set(draft.reason_codes)),
            "broker_execution_allowed": False,
        }
        identity = sha256_bytes(canonical_json_bytes(identity_payload))
        release = TradingClassificationRelease(
            release_id=f"trading-classification:{identity}",
            company_id=draft.company_id,
            market=draft.market,
            symbol=draft.symbol,
            as_of=draft.as_of,
            effective_from=draft.effective_from,
            valid_until=draft.valid_until,
            classification=draft.classification,
            special_no_price_limit=draft.special_no_price_limit,
            corporate_action_baseline_artifact_id=draft.corporate_action_baseline_artifact_id,
            source_artifact_ids=draft.source_artifact_ids,
            source_object_hashes=sorted(set(source_hashes)),
            status=draft.status,
            reason_codes=sorted(set(draft.reason_codes)),
            created_at=draft.created_at,
        )
        object_ref = self.objects.put_json(release.model_dump(mode="json"))
        artifact_id = f"TradingClassificationRelease:{release.release_id}"
        existing = self.state.artifact_record(artifact_id)
        replay = existing is not None
        if existing is not None:
            if (
                str(existing["type"]) != "TradingClassificationRelease"
                or str(existing["schema_version"]) != release.schema_version
                or str(existing["object_hash"]) != object_ref.sha256
                or sorted(existing["input_hashes"]) != sorted(source_hashes)
            ):
                raise ValueError("trading classification release identity collision")
        else:
            self.state.register_artifact(
                artifact_id=artifact_id,
                artifact_type="TradingClassificationRelease",
                schema_version=release.schema_version,
                object_hash=object_ref.sha256,
                input_hashes=sorted(source_hashes),
            )
        return TradingClassificationRecord(
            release=release,
            artifact_id=artifact_id,
            object_hash=object_ref.sha256,
            idempotent_replay=replay,
        )

    def load(self, artifact_id: str) -> TradingClassificationRecord:
        record = self.state.artifact_record(artifact_id)
        if record is None or str(record["type"]) != "TradingClassificationRelease":
            raise ValueError("unknown trading classification release artifact")
        object_hash = str(record["object_hash"])
        if not self.objects.verify(object_hash):
            raise ValueError("trading classification release object is unavailable")
        release = TradingClassificationRelease.model_validate_json(
            self.objects.get_bytes(object_hash)
        )
        return TradingClassificationRecord(
            release=release,
            artifact_id=artifact_id,
            object_hash=object_hash,
            idempotent_replay=True,
        )

    def status(
        self,
        artifact_id: str,
        *,
        as_of: datetime | None = None,
    ) -> TradingClassificationStatusReport:
        try:
            record = self.load(artifact_id)
        except ValueError as exc:
            return {
                "status": "NEEDS_INFO",
                "reason_codes": [str(exc)],
                "artifact_id": None,
                "release_id": None,
                "broker_execution_allowed": False,
            }
        release = record.release
        current = as_of or release.as_of
        reasons = list(release.reason_codes)
        if current < release.effective_from or current > release.valid_until:
            reasons.append("TRADING_CLASSIFICATION_OUTSIDE_VALIDITY")
        if release.status is not TradingClassificationStatus.READY:
            reasons.append("TRADING_CLASSIFICATION_NOT_READY")
        if not release.classification.suspension_status_verified:
            reasons.append("SUSPENSION_STATUS_NOT_VERIFIED")
        return {
            "status": "READY" if not reasons else "NEEDS_INFO",
            "artifact_id": artifact_id,
            "release_id": release.release_id,
            "reason_codes": sorted(set(reasons)),
            "broker_execution_allowed": False,
        }

    def audit(self, artifact_id: str) -> TradingClassificationAuditReport:
        findings: list[str] = []
        try:
            record = self.load(artifact_id)
        except ValueError as exc:
            return {
                "status": "FAIL",
                "finding_codes": [str(exc)],
                "artifact_id": None,
                "release_id": None,
                "broker_execution_allowed": False,
            }
        release = record.release
        registry = self.state.artifact_record(artifact_id)
        assert registry is not None
        if sorted(registry["input_hashes"]) != release.source_object_hashes:
            findings.append("CLASSIFICATION_SOURCE_HASH_DRIFT")
        actual_source_hashes: list[str] = []
        for source_id in release.source_artifact_ids:
            source = self.state.artifact_record(source_id)
            if source is None:
                findings.append("CLASSIFICATION_SOURCE_ARTIFACT_DRIFT")
                continue
            source_hash = str(source["object_hash"])
            actual_source_hashes.append(source_hash)
            if not self.objects.verify(source_hash):
                findings.append("CLASSIFICATION_SOURCE_OBJECT_MISSING")
        if sorted(set(actual_source_hashes)) != release.source_object_hashes:
            findings.append("CLASSIFICATION_SOURCE_ARTIFACT_DRIFT")
        if self.status(artifact_id, as_of=release.as_of)["status"] != "READY":
            findings.append("CLASSIFICATION_NOT_READY_AT_AS_OF")
        return {
            "status": "PASS" if not findings else "FAIL",
            "artifact_id": artifact_id,
            "release_id": release.release_id,
            "finding_codes": sorted(set(findings)),
            "broker_execution_allowed": False,
        }


__all__ = ["TradingClassificationService"]
