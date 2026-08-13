"""Deterministic validation and freezing for Agent-native adaptive-edge proposals."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TypeVar

import yaml
from pydantic import BaseModel

from astock.core.hashing import content_hash
from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.financial_sources.config import load_financial_field_mappings
from astock.providers.config import get_provider, load_provider_registry
from astock.providers.dialects import load_provider_dialects
from astock.research.policy import load_default_current_research_policy
from astock.research.resource_policy import load_specialist_resource_policy
from astock.schemas.adaptation import (
    AdaptiveArtifactAudit,
    AdaptiveProposalStatus,
    ProviderDialectCandidateRelease,
    ProviderDialectRollbackRecord,
    ProviderRecoveryProposal,
    ProviderRecoveryValidation,
    ResearchModule,
    ResearchPlannerProposal,
    SchemaRepairProposal,
    SchemaRepairValidation,
    ValidatedResearchPlan,
)
from astock.schemas.provider import ProviderHealthStatus

_ModelT = TypeVar("_ModelT", bound=BaseModel)


@dataclass(frozen=True, slots=True)
class ResearchPlannerPolicy:
    policy_version: str
    mandatory_modules: tuple[ResearchModule, ...]
    module_order: tuple[ResearchModule, ...]
    module_dependencies: dict[ResearchModule, tuple[ResearchModule, ...]]


@dataclass(frozen=True, slots=True)
class SchemaRepairPolicy:
    policy_version: str
    minimum_raw_samples: int
    allowed_official_artifact_types: frozenset[str]
    require_explicit_approval_for_admission: bool


class AdaptiveEdgeService:
    """Validate Agent proposals while preserving deterministic-core safety boundaries."""

    def __init__(self, state: StateStore, objects: ObjectStore, project_root: Path) -> None:
        self.state = state
        self.objects = objects
        self.root = project_root.resolve()
        self.provider_registry = load_provider_registry(
            self.root / "configs" / "provider_registry.yaml"
        )
        self.current_research_policy = load_default_current_research_policy(self.root)
        self.resource_policy = load_specialist_resource_policy(
            self.root / "configs" / "specialist_resource_policy.yaml"
        )
        self.planner_policy = load_research_planner_policy(
            self.root / "configs" / "research_planner_policy.yaml"
        )
        self.schema_repair_policy = load_schema_repair_policy(
            self.root / "configs" / "schema_repair_policy.yaml"
        )
        self.dialects = load_provider_dialects(
            self.root / "configs" / "provider_dialects.yaml"
        )
        mappings = load_financial_field_mappings(
            self.root / "configs" / "financial_field_mappings.yaml"
        )
        self.canonical_financial_fields = {
            field
            for mapping in mappings
            for field in mapping.provider_fields.values()
        }

    def validate_research_plan(self, proposal: ResearchPlannerProposal) -> ValidatedResearchPlan:
        proposal_hash = self._freeze(proposal.proposal_id, proposal, [])
        requested = set(proposal.requested_modules)
        selected = set(self.planner_policy.mandatory_modules) | requested
        changed = True
        while changed:
            changed = False
            for module in tuple(selected):
                for dependency in self.planner_policy.module_dependencies[module]:
                    if dependency not in selected:
                        selected.add(dependency)
                        changed = True
        optional = set(self.planner_policy.module_order) - set(
            self.planner_policy.mandatory_modules
        )
        omitted = optional - selected
        missing_skip_reasons = sorted(
            module.value
            for module in omitted
            if not proposal.skipped_optional_reasons.get(module, "").strip()
        )
        if missing_skip_reasons:
            raise ValueError(
                "planner must explain every skipped optional module: "
                + ",".join(missing_skip_reasons)
            )
        specialist_budget = self.resource_policy.resolve(proposal.specialist_budget)
        acquisition = set(self.current_research_policy.core_capabilities)
        acquisition.update(proposal.requested_acquisition_capabilities)
        ordered_modules = [
            module for module in self.planner_policy.module_order if module in selected
        ]
        ordered_acquisition = sorted(acquisition, key=lambda item: item.value)
        identity = {
            "proposal_id": proposal.proposal_id,
            "planner_policy": self.planner_policy.policy_version,
            "modules": [item.value for item in ordered_modules],
            "acquisition": [item.value for item in ordered_acquisition],
            "specialist_budget": specialist_budget,
        }
        plan = ValidatedResearchPlan(
            created_at=proposal.created_at,
            plan_id=f"validated-research-plan:{content_hash(identity)}",
            proposal_id=proposal.proposal_id,
            company_id=proposal.company_id,
            market=proposal.market,
            ordered_modules=ordered_modules,
            mandatory_modules=list(self.planner_policy.mandatory_modules),
            acquisition_capabilities=ordered_acquisition,
            specialist_budget=specialist_budget,
            policy_version=self.planner_policy.policy_version,
        )
        self._freeze(plan.plan_id, plan, [proposal_hash])
        return plan

    def validate_recovery(
        self, proposal: ProviderRecoveryProposal
    ) -> ProviderRecoveryValidation:
        proposal_hash = self._freeze(proposal.proposal_id, proposal, [])
        rejection_codes: set[str] = set()
        diagnostics = {item.provider_id: item for item in proposal.diagnostics}
        for diagnostic in proposal.diagnostics:
            try:
                definition = get_provider(self.provider_registry, diagnostic.provider_id)
            except ValueError:
                rejection_codes.add(f"UNKNOWN_DIAGNOSTIC_PROVIDER:{diagnostic.provider_id}")
                continue
            if (
                diagnostic.transport_profile
                and diagnostic.transport_profile != definition.transport_profile
            ):
                rejection_codes.add(
                    f"TRANSPORT_PROFILE_MISMATCH:{diagnostic.provider_id}"
                )
        allowed: list[str] = []
        for provider_id in proposal.proposed_provider_ids:
            try:
                definition = get_provider(self.provider_registry, provider_id)
            except ValueError:
                rejection_codes.add(f"UNKNOWN_PROVIDER:{provider_id}")
                continue
            if proposal.requested_capability not in definition.capabilities:
                rejection_codes.add(f"CAPABILITY_MISMATCH:{provider_id}")
                continue
            row, _head = self.state.get_provider_probe_health_snapshot(provider_id)
            status = _health_status(row)
            if status in {ProviderHealthStatus.UNAVAILABLE, ProviderHealthStatus.CORRUPT}:
                rejection_codes.add(f"PROVIDER_HEALTH_BLOCKED:{provider_id}")
                continue
            diagnostic = diagnostics.get(provider_id)
            if diagnostic is not None and not diagnostic.retryable:
                rejection_codes.add(f"NON_RETRYABLE_PROVIDER_REUSE:{provider_id}")
                continue
            allowed.append(provider_id)
        if not allowed and not proposal.authority_fallbacks:
            rejection_codes.add("NO_ALLOWLISTED_RECOVERY_PATH")
        status = (
            AdaptiveProposalStatus.REJECTED
            if rejection_codes
            else AdaptiveProposalStatus.VALIDATED
        )
        identity = {
            "proposal_id": proposal.proposal_id,
            "allowed": allowed,
            "authorities": [item.value for item in proposal.authority_fallbacks],
            "rejections": sorted(rejection_codes),
        }
        validation = ProviderRecoveryValidation(
            created_at=proposal.created_at,
            validation_id=f"provider-recovery-validation:{content_hash(identity)}",
            proposal_id=proposal.proposal_id,
            requested_capability=proposal.requested_capability,
            allowed_provider_ids=allowed,
            authority_fallbacks=proposal.authority_fallbacks,
            rejection_codes=sorted(rejection_codes),
            status=status,
        )
        self._freeze(validation.validation_id, validation, [proposal_hash])
        return validation

    def validate_schema_repair(
        self, proposal: SchemaRepairProposal
    ) -> SchemaRepairValidation:
        proposal_hash = self._freeze(proposal.proposal_id, proposal, [])
        rejection_codes: set[str] = set()
        base = self.dialects.get(proposal.provider_id)
        if base is None:
            rejection_codes.add("PROVIDER_DIALECT_NOT_REGISTERED")
        elif base.dialect_version != proposal.base_dialect_version:
            rejection_codes.add("BASE_DIALECT_VERSION_MISMATCH")
        invalid_targets = sorted(
            set(proposal.candidate_field_mapping.values())
            - self.canonical_financial_fields
        )
        if invalid_targets:
            rejection_codes.add("UNKNOWN_CANONICAL_FIELD_TARGET")
        verified_snapshots: list[str] = []
        for snapshot_id in proposal.sample_snapshot_ids:
            snapshot = self.state.get_snapshot(snapshot_id)
            if snapshot is None or not self.objects.verify(snapshot.object_sha256):
                rejection_codes.add(f"INVALID_SAMPLE_SNAPSHOT:{snapshot_id}")
            else:
                verified_snapshots.append(snapshot_id)
        verified_official: list[str] = []
        for artifact_id in proposal.official_evidence_artifact_ids:
            record = self.state.artifact_record(artifact_id)
            if record is None or not self.objects.verify(str(record["object_hash"])):
                rejection_codes.add(f"INVALID_OFFICIAL_EVIDENCE:{artifact_id}")
                continue
            if str(record["type"]) not in self.schema_repair_policy.allowed_official_artifact_types:
                rejection_codes.add(f"NON_OFFICIAL_ARTIFACT_TYPE:{artifact_id}")
                continue
            verified_official.append(artifact_id)
        if len(verified_snapshots) < self.schema_repair_policy.minimum_raw_samples:
            rejection_codes.add("INSUFFICIENT_DIVERSE_RAW_SAMPLES")
        if not verified_official:
            rejection_codes.add("OFFICIAL_CROSS_CHECK_REQUIRED")
        for test_id in proposal.contract_test_ids:
            if not self._contract_test_exists(test_id):
                rejection_codes.add(f"CONTRACT_TEST_NOT_FOUND:{test_id}")
        status = (
            AdaptiveProposalStatus.REJECTED
            if rejection_codes
            else AdaptiveProposalStatus.VALIDATED
        )
        identity = {
            "proposal_id": proposal.proposal_id,
            "snapshots": verified_snapshots,
            "official": verified_official,
            "tests": proposal.contract_test_ids,
            "rejections": sorted(rejection_codes),
        }
        validation = SchemaRepairValidation(
            created_at=proposal.created_at,
            validation_id=f"schema-repair-validation:{content_hash(identity)}",
            proposal_id=proposal.proposal_id,
            provider_id=proposal.provider_id,
            verified_snapshot_ids=verified_snapshots,
            verified_official_artifact_ids=verified_official,
            verified_contract_test_ids=proposal.contract_test_ids,
            rejection_codes=sorted(rejection_codes),
            status=status,
        )
        self._freeze(validation.validation_id, validation, [proposal_hash])
        return validation

    def admit_schema_repair(
        self,
        validation_id: str,
        *,
        explicit_approval: bool,
    ) -> ProviderDialectCandidateRelease:
        if not explicit_approval:
            raise ValueError("schema repair admission requires explicit approval")
        validation, validation_hash = self._load(validation_id, SchemaRepairValidation)
        if validation.status is not AdaptiveProposalStatus.VALIDATED:
            raise ValueError("only a validated schema repair may be admitted")
        proposal, proposal_hash = self._load(validation.proposal_id, SchemaRepairProposal)
        material = {
            "provider_id": proposal.provider_id,
            "base_dialect": proposal.base_dialect_version,
            "field_mapping": proposal.candidate_field_mapping,
            "response_paths": proposal.candidate_response_paths,
            "snapshots": validation.verified_snapshot_ids,
            "official": validation.verified_official_artifact_ids,
            "tests": validation.verified_contract_test_ids,
        }
        suffix = content_hash(material)[:12]
        release = ProviderDialectCandidateRelease(
            created_at=validation.created_at,
            release_id=f"provider-dialect-candidate:{proposal.provider_id}:{suffix}",
            proposal_id=proposal.proposal_id,
            validation_id=validation.validation_id,
            provider_id=proposal.provider_id,
            candidate_dialect_version=(
                f"{proposal.base_dialect_version}-candidate-{suffix}"
            ),
            candidate_field_mapping=proposal.candidate_field_mapping,
            candidate_response_paths=proposal.candidate_response_paths,
        )
        self._freeze(
            release.release_id,
            release,
            [proposal_hash, validation_hash],
        )
        return release

    def audit_artifact(self, artifact_id: str) -> AdaptiveArtifactAudit:
        record = self.state.artifact_record(artifact_id)
        findings: list[str] = []
        artifact_type: str | None = None
        object_hash: str | None = None
        if record is None:
            findings.append("ARTIFACT_NOT_REGISTERED")
        else:
            artifact_type = str(record["type"])
            object_hash = str(record["object_hash"])
            if not self.objects.verify(object_hash):
                findings.append("ARTIFACT_OBJECT_UNAVAILABLE_OR_CORRUPT")
        identity = {
            "artifact_id": artifact_id,
            "artifact_type": artifact_type,
            "object_hash": object_hash,
            "findings": findings,
        }
        audit = AdaptiveArtifactAudit(
            audit_id=f"adaptive-artifact-audit:{content_hash(identity)}",
            artifact_id=artifact_id,
            artifact_type=artifact_type,
            object_hash=object_hash,
            status="FAIL" if findings else "PASS",
            finding_codes=findings,
        )
        self._freeze(
            audit.audit_id,
            audit,
            [object_hash] if object_hash is not None else [],
        )
        return audit

    def rollback_dialect_candidate(
        self, release_id: str
    ) -> ProviderDialectRollbackRecord:
        release, release_hash = self._load(release_id, ProviderDialectCandidateRelease)
        active = self.dialects.get(release.provider_id)
        if active is None:
            raise ValueError("cannot roll back to an unregistered active dialect")
        identity = {
            "candidate_release_id": release.release_id,
            "provider_id": release.provider_id,
            "candidate": release.candidate_dialect_version,
            "restored": active.dialect_version,
        }
        rollback = ProviderDialectRollbackRecord(
            created_at=release.created_at,
            rollback_id=f"provider-dialect-rollback:{content_hash(identity)}",
            candidate_release_id=release.release_id,
            provider_id=release.provider_id,
            rejected_candidate_dialect_version=release.candidate_dialect_version,
            restored_active_dialect_version=active.dialect_version,
        )
        self._freeze(rollback.rollback_id, rollback, [release_hash])
        return rollback

    def _contract_test_exists(self, test_id: str) -> bool:
        relative = test_id.split("::", maxsplit=1)[0].replace("\\", "/")
        if not relative.startswith("tests/"):
            return False
        path = (self.root / relative).resolve()
        return path.is_relative_to(self.root / "tests") and path.is_file()

    def _freeze(self, artifact_id: str, model: BaseModel, input_hashes: list[str]) -> str:
        ref = self.objects.put_json(model.model_dump(mode="json"))
        schema_version = str(getattr(model, "schema_version", "1.0"))
        self.state.register_artifact(
            artifact_id=artifact_id,
            artifact_type=type(model).__name__,
            schema_version=schema_version,
            object_hash=ref.sha256,
            input_hashes=input_hashes,
        )
        return ref.sha256

    def _load(self, artifact_id: str, model_type: type[_ModelT]) -> tuple[_ModelT, str]:
        record = self.state.artifact_record(artifact_id)
        if record is None or str(record["type"]) != model_type.__name__:
            raise ValueError(f"adaptive-edge artifact not found: {artifact_id}")
        object_hash = str(record["object_hash"])
        if not self.objects.verify(object_hash):
            raise ValueError(f"adaptive-edge artifact object is unavailable: {artifact_id}")
        return model_type.model_validate_json(self.objects.get_bytes(object_hash)), object_hash


def load_research_planner_policy(path: Path) -> ResearchPlannerPolicy:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise ValueError(f"Invalid research planner policy: {path}") from exc
    if not isinstance(raw, dict) or raw.get("schema_version") != "research-planner-policy-v1":
        raise ValueError("Unsupported research planner policy")
    mandatory_raw = raw.get("mandatory_modules")
    order_raw = raw.get("module_order")
    dependencies_raw = raw.get("module_dependencies")
    if not isinstance(mandatory_raw, list) or not isinstance(order_raw, list):
        raise ValueError("research planner modules must be lists")
    if not isinstance(dependencies_raw, dict):
        raise ValueError("research planner dependencies must be an object")
    mandatory = tuple(ResearchModule(str(item)) for item in mandatory_raw)
    order = tuple(ResearchModule(str(item)) for item in order_raw)
    if len(order) != len(set(order)) or set(mandatory) - set(order):
        raise ValueError("research planner module order is invalid")
    dependencies: dict[ResearchModule, tuple[ResearchModule, ...]] = {}
    for raw_module, raw_values in dependencies_raw.items():
        module = ResearchModule(str(raw_module))
        if not isinstance(raw_values, list):
            raise ValueError("research planner dependency values must be lists")
        dependencies[module] = tuple(ResearchModule(str(item)) for item in raw_values)
    if set(dependencies) != set(order):
        raise ValueError("research planner dependencies must cover every module")
    position = {module: index for index, module in enumerate(order)}
    for module, values in dependencies.items():
        if any(position[dependency] >= position[module] for dependency in values):
            raise ValueError("research planner dependency order is cyclic or reversed")
    if raw.get("paper_ledger_write_allowed") is not False:
        raise ValueError("research planner cannot enable paper ledger writes")
    if raw.get("broker_execution_allowed") is not False:
        raise ValueError("research planner cannot enable broker execution")
    return ResearchPlannerPolicy(
        policy_version=str(raw["schema_version"]),
        mandatory_modules=mandatory,
        module_order=order,
        module_dependencies=dependencies,
    )


def load_schema_repair_policy(path: Path) -> SchemaRepairPolicy:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise ValueError(f"Invalid schema repair policy: {path}") from exc
    if not isinstance(raw, dict) or raw.get("schema_version") != "schema-repair-policy-v1":
        raise ValueError("Unsupported schema repair policy")
    allowed = raw.get("allowed_official_artifact_types")
    if not isinstance(allowed, list) or not allowed:
        raise ValueError("schema repair official artifact types must be non-empty")
    minimum = int(raw["minimum_raw_samples"])
    if not 2 <= minimum <= 20:
        raise ValueError("schema repair minimum_raw_samples must be in 2..20")
    if raw.get("require_explicit_approval_for_admission") is not True:
        raise ValueError("schema repair admission must require explicit approval")
    if raw.get("formal_fact_write_allowed") is not False:
        raise ValueError("schema repair policy cannot permit formal fact writes")
    if raw.get("active_runtime_mutation_allowed") is not False:
        raise ValueError("schema repair policy cannot mutate active runtime")
    return SchemaRepairPolicy(
        policy_version=str(raw["schema_version"]),
        minimum_raw_samples=minimum,
        allowed_official_artifact_types=frozenset(str(item) for item in allowed),
        require_explicit_approval_for_admission=True,
    )


def _health_status(row: dict[str, object] | None) -> ProviderHealthStatus:
    if row is None or not row.get("status"):
        return ProviderHealthStatus.NOT_PROBED
    try:
        return ProviderHealthStatus(str(row["status"]))
    except ValueError:
        return ProviderHealthStatus.CORRUPT


__all__ = [
    "AdaptiveEdgeService",
    "ResearchPlannerPolicy",
    "SchemaRepairPolicy",
    "load_research_planner_policy",
    "load_schema_repair_policy",
]
