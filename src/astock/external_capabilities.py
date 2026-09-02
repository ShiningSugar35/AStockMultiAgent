"""Deterministic qualification and revocation service for optional external capabilities."""

from __future__ import annotations

from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

import yaml

from astock.core.object_store import ObjectStore
from astock.core.state import StateStore, utc_now_text
from astock.schemas.external_capabilities import (
    CapabilityQualificationReport,
    CapabilityRevocation,
    ExternalCapabilityDefinition,
    ExternalCapabilityRegistry,
    ExternalCapabilityStage,
)


class ExternalCapabilityQualificationError(ValueError):
    pass


def load_external_capability_registry(path: Path) -> ExternalCapabilityRegistry:
    try:
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise ExternalCapabilityQualificationError(
            f"invalid external capability registry: {path}"
        ) from exc
    if not isinstance(payload, dict):
        raise ExternalCapabilityQualificationError("external capability registry must be a mapping")
    try:
        return ExternalCapabilityRegistry.model_validate(payload)
    except ValueError as exc:
        raise ExternalCapabilityQualificationError(
            "external capability registry failed validation"
        ) from exc


class ExternalCapabilityService:
    def __init__(
        self,
        project_root: Path,
        state: StateStore,
        objects: ObjectStore,
        *,
        registry: ExternalCapabilityRegistry | None = None,
    ) -> None:
        self.project_root = project_root.resolve()
        self.state = state
        self.objects = objects
        self.registry = registry or load_external_capability_registry(
            self.project_root / "configs" / "external_capabilities.yaml"
        )
        self._definitions = {item.capability_id: item for item in self.registry.capabilities}

    def definition(self, capability_id: str) -> ExternalCapabilityDefinition:
        try:
            return self._definitions[capability_id]
        except KeyError as exc:
            raise ExternalCapabilityQualificationError(
                f"unknown external capability: {capability_id}"
            ) from exc

    def register_qualification(
        self, report: CapabilityQualificationReport
    ) -> CapabilityQualificationReport:
        definition = self.definition(report.capability_id)
        self._validate_report_against_definition(report, definition)
        for object_hash in report.evidence_object_hashes:
            if not self.objects.verify(object_hash):
                raise ExternalCapabilityQualificationError(
                    "qualification evidence is missing or corrupt"
                )
        stored = self.objects.put_json(report.model_dump(mode="json"))
        artifact_id = f"external-capability-qualification:{report.report_id}"
        self.state.register_artifact(
            artifact_id=artifact_id,
            artifact_type="CapabilityQualificationReport",
            schema_version=report.schema_version,
            object_hash=stored.sha256,
            input_hashes=report.evidence_object_hashes,
        )
        with self.state.transaction() as connection:
            existing = connection.execute(
                "SELECT capability_id,candidate_version,admitted_stage,valid_from,expires_at,"
                "report_artifact_id,report_object_hash FROM external_capability_qualification "
                "WHERE report_id=?",
                (report.report_id,),
            ).fetchone()
            expected = (
                report.capability_id,
                report.candidate_version,
                report.admitted_stage.value,
                report.valid_from.isoformat(),
                report.expires_at.isoformat(),
                artifact_id,
                stored.sha256,
            )
            if existing is not None:
                if tuple(existing) != expected:
                    raise ExternalCapabilityQualificationError("qualification identity collision")
                return report
            connection.execute(
                "INSERT INTO external_capability_qualification("
                "report_id,capability_id,candidate_version,admitted_stage,valid_from,expires_at,"
                "report_artifact_id,report_object_hash,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (*((report.report_id,) + expected), utc_now_text()),
            )
        return report

    def revoke(self, revocation: CapabilityRevocation) -> CapabilityRevocation:
        with closing(self.state.connect()) as connection:
            row = connection.execute(
                "SELECT capability_id,report_object_hash FROM external_capability_qualification "
                "WHERE report_id=?",
                (revocation.report_id,),
            ).fetchone()
        if row is None or str(row["capability_id"]) != revocation.capability_id:
            raise ExternalCapabilityQualificationError("revocation target is unknown or mismatched")
        report_object_hash = str(row["report_object_hash"])
        if not self.objects.verify(report_object_hash):
            raise ExternalCapabilityQualificationError(
                "revocation target report object is unavailable"
            )
        stored = self.objects.put_json(revocation.model_dump(mode="json"))
        artifact_id = f"external-capability-revocation:{revocation.revocation_id}"
        self.state.register_artifact(
            artifact_id=artifact_id,
            artifact_type="CapabilityRevocation",
            schema_version=revocation.schema_version,
            object_hash=stored.sha256,
            input_hashes=[report_object_hash],
        )
        with self.state.transaction() as connection:
            existing = connection.execute(
                "SELECT capability_id,report_id,revoked_at,reason,artifact_id,object_hash "
                "FROM external_capability_revocation WHERE revocation_id=?",
                (revocation.revocation_id,),
            ).fetchone()
            expected = (
                revocation.capability_id,
                revocation.report_id,
                revocation.revoked_at.isoformat(),
                revocation.reason,
                artifact_id,
                stored.sha256,
            )
            if existing is not None:
                if tuple(existing) != expected:
                    raise ExternalCapabilityQualificationError("revocation identity collision")
                return revocation
            connection.execute(
                "INSERT INTO external_capability_revocation("
                "revocation_id,capability_id,report_id,revoked_at,reason,artifact_id,object_hash,"
                "created_at) VALUES(?,?,?,?,?,?,?,?)",
                (revocation.revocation_id, *expected, utc_now_text()),
            )
        return revocation

    def active_report(
        self,
        capability_id: str,
        *,
        at: datetime | None = None,
    ) -> CapabilityQualificationReport | None:
        self.definition(capability_id)
        now = (at or datetime.now(UTC)).astimezone(UTC)
        with closing(self.state.connect()) as connection:
            rows = connection.execute(
                "SELECT report_id,capability_id,candidate_version,admitted_stage,"
                "report_artifact_id,report_object_hash,valid_from,expires_at "
                "FROM external_capability_qualification WHERE capability_id=? "
                "AND valid_from<=? AND expires_at>? ORDER BY valid_from DESC,report_id DESC",
                (capability_id, now.isoformat(), now.isoformat()),
            ).fetchall()
            for row in rows:
                revoked = connection.execute(
                    "SELECT 1 FROM external_capability_revocation WHERE report_id=? "
                    "AND revoked_at<=? LIMIT 1",
                    (str(row["report_id"]), now.isoformat()),
                ).fetchone()
                if revoked is not None:
                    continue
                report = self._verified_report_row(row)
                if report is not None:
                    return report
        return None

    def effective_stage(
        self, capability_id: str, *, at: datetime | None = None
    ) -> ExternalCapabilityStage:
        definition = self.definition(capability_id)
        if definition.maximum_stage is ExternalCapabilityStage.REJECT:
            return ExternalCapabilityStage.REJECT
        report = self.active_report(capability_id, at=at)
        return report.admitted_stage if report is not None else definition.default_stage

    def production_backup_allowed(
        self,
        capability_id: str,
        logical_capability: str,
        *,
        primary_available: bool,
        at: datetime | None = None,
    ) -> bool:
        definition = self.definition(capability_id)
        if primary_available or logical_capability not in definition.logical_capabilities:
            return False
        report = self.active_report(capability_id, at=at)
        return bool(
            report is not None
            and report.admitted_stage is ExternalCapabilityStage.PRODUCTION_BACKUP
            and report.source_class_ceiling is definition.source_class_ceiling
            and report.completeness_ceiling is definition.completeness_ceiling
        )

    def status(self, capability_id: str, *, at: datetime | None = None) -> dict[str, object]:
        definition = self.definition(capability_id)
        report = self.active_report(capability_id, at=at)
        return {
            "capability_id": capability_id,
            "kind": definition.kind.value,
            "default_stage": definition.default_stage.value,
            "maximum_stage": definition.maximum_stage.value,
            "effective_stage": (
                report.admitted_stage.value
                if report is not None
                else definition.default_stage.value
            ),
            "active_report_id": report.report_id if report is not None else None,
            "candidate_version": report.candidate_version if report is not None else None,
            "expires_at": report.expires_at.isoformat() if report is not None else None,
        }

    def _validate_report_against_definition(
        self,
        report: CapabilityQualificationReport,
        definition: ExternalCapabilityDefinition,
    ) -> None:
        if definition.maximum_stage is ExternalCapabilityStage.REJECT:
            if report.admitted_stage is not ExternalCapabilityStage.REJECT:
                raise ExternalCapabilityQualificationError("rejected capability cannot be admitted")
            return
        stage_rank = {
            ExternalCapabilityStage.DISCOVERY_ONLY: 0,
            ExternalCapabilityStage.SHADOW: 1,
            ExternalCapabilityStage.PRODUCTION_BACKUP: 2,
            ExternalCapabilityStage.REJECT: -1,
        }
        if stage_rank[report.admitted_stage] > stage_rank[report.requested_stage]:
            raise ExternalCapabilityQualificationError(
                "qualification cannot exceed requested stage"
            )
        if stage_rank[report.admitted_stage] > stage_rank[definition.maximum_stage]:
            raise ExternalCapabilityQualificationError(
                "qualification exceeds registry maximum stage"
            )
        if report.source_class_ceiling is not definition.source_class_ceiling:
            raise ExternalCapabilityQualificationError(
                "qualification cannot change source authority ceiling"
            )
        if report.completeness_ceiling is not definition.completeness_ceiling:
            raise ExternalCapabilityQualificationError(
                "qualification cannot change completeness ceiling"
            )
        maximum_expiry = report.valid_from + timedelta(days=definition.qualification_validity_days)
        if report.expires_at > maximum_expiry:
            raise ExternalCapabilityQualificationError(
                "qualification exceeds configured validity window"
            )

    def _verified_report_row(self, row: object) -> CapabilityQualificationReport | None:
        try:
            report_id = str(row["report_id"])  # type: ignore[index]
            artifact_id = str(row["report_artifact_id"])  # type: ignore[index]
            object_hash = str(row["report_object_hash"])  # type: ignore[index]
            artifact = self.state.artifact_record(artifact_id)
            if artifact is None or str(artifact.get("object_hash") or "") != object_hash:
                return None
            if not self.objects.verify(object_hash):
                return None
            report = CapabilityQualificationReport.model_validate_json(
                self.objects.get_bytes(object_hash)
            )
            if (
                report.report_id != report_id
                or str(row["capability_id"]) != report.capability_id  # type: ignore[index]
                or str(row["candidate_version"]) != report.candidate_version  # type: ignore[index]
                or str(row["admitted_stage"]) != report.admitted_stage.value  # type: ignore[index]
                or str(row["valid_from"]) != report.valid_from.isoformat()  # type: ignore[index]
                or str(row["expires_at"]) != report.expires_at.isoformat()  # type: ignore[index]
            ):
                return None
            definition = self.definition(report.capability_id)
            self._validate_report_against_definition(report, definition)
            if any(not self.objects.verify(value) for value in report.evidence_object_hashes):
                return None
            return report
        except (KeyError, TypeError, ValueError, OSError):
            return None


__all__ = [
    "ExternalCapabilityQualificationError",
    "ExternalCapabilityService",
    "load_external_capability_registry",
]
