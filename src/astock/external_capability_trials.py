"""Evidence-backed E-02 trials compiled into the canonical M-06 qualification service."""

from __future__ import annotations

from pathlib import Path

from astock.core.hashing import canonical_json_bytes
from astock.core.object_store import ObjectStore
from astock.external_capabilities import ExternalCapabilityService
from astock.schemas.external_capabilities import CapabilityQualificationReport
from astock.schemas.external_capability_trials import CapabilityQualificationEvidence


def load_capability_qualification_evidence(path: Path) -> CapabilityQualificationEvidence:
    return CapabilityQualificationEvidence.model_validate_json(path.read_bytes())


def register_capability_qualification_evidence(
    service: ExternalCapabilityService,
    objects: ObjectStore,
    path: Path,
) -> CapabilityQualificationReport:
    """Freeze a tracked evidence bundle, compile it to M-06, and register it fail-closed."""

    raw = path.read_bytes()
    evidence = CapabilityQualificationEvidence.model_validate_json(raw)
    definition = service.definition(evidence.capability_id)
    if evidence.source_class_ceiling is not definition.source_class_ceiling:
        raise ValueError("qualification evidence source authority ceiling does not match registry")
    if evidence.completeness_ceiling is not definition.completeness_ceiling:
        raise ValueError("qualification evidence completeness ceiling does not match registry")
    if evidence.admitted_stage not in {definition.default_stage, definition.maximum_stage}:
        raise ValueError("qualification evidence admitted stage is outside registry contract")
    canonical_evidence = canonical_json_bytes(
        evidence.model_dump(mode="json", exclude_unset=True)
    )
    ref = objects.put_bytes(canonical_evidence)
    report = evidence.to_report(ref.sha256)
    return service.register_qualification(report)


__all__ = [
    "load_capability_qualification_evidence",
    "register_capability_qualification_evidence",
]
