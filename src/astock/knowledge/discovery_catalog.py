"""Deterministic compiler for reviewed Skill discovery dispositions.

This module deliberately does not infer investment logic from module names or free text.
A reviewer/LLM may propose disposition specs, but compilation succeeds only when the
reviewed batch covers the exact active audited inventory and binds to its immutable
registry identity.
"""

from __future__ import annotations

from astock.core.hashing import canonical_json_bytes, sha256_bytes
from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.schemas.knowledge_completion import (
    KnowledgeDiscoveryCatalog,
    KnowledgeDiscoveryCatalogEntry,
    KnowledgeDiscoveryCatalogRecord,
    KnowledgeDiscoveryDispositionBatch,
    KnowledgeProviderReadiness,
    KnowledgeSkillInventorySnapshot,
)


class KnowledgeDiscoveryCatalogCompiler:
    """Compile one exact reviewed disposition batch against one active inventory."""

    def __init__(self, state: StateStore, objects: ObjectStore) -> None:
        self.state = state
        self.objects = objects

    def compile(
        self,
        inventory: KnowledgeSkillInventorySnapshot,
        batch: KnowledgeDiscoveryDispositionBatch,
    ) -> KnowledgeDiscoveryCatalogRecord:
        status = inventory.provider_status
        if status.status is not KnowledgeProviderReadiness.READY:
            raise ValueError("knowledge discovery compilation requires a READY inventory")
        if status.registry_release_id is None or status.registry_object_hash is None:
            raise ValueError("knowledge discovery compilation requires immutable registry identity")
        if batch.run_id != inventory.run_id:
            raise ValueError("knowledge discovery batch belongs to another run")
        if batch.registry_object_hash != status.registry_object_hash:
            raise ValueError("knowledge discovery batch registry hash does not match inventory")

        member_by_id = {item.final_skill_id: item for item in inventory.members}
        spec_by_id = {item.final_skill_id: item for item in batch.specs}
        if set(spec_by_id) != set(member_by_id):
            missing = sorted(set(member_by_id) - set(spec_by_id))
            unknown = sorted(set(spec_by_id) - set(member_by_id))
            raise ValueError(
                "knowledge discovery disposition coverage mismatch: "
                f"missing={missing[:5]} unknown={unknown[:5]}"
            )

        batch_ref = self.objects.put_json(batch.model_dump(mode="json"))
        batch_artifact_id = (
            f"KnowledgeDiscoveryDispositionBatch:{inventory.run_id}:{batch_ref.sha256}"
        )
        self.state.register_artifact(
            artifact_id=batch_artifact_id,
            artifact_type="KnowledgeDiscoveryDispositionBatch",
            schema_version=batch.schema_version,
            object_hash=batch_ref.sha256,
            input_hashes=[inventory.result_hash, status.registry_object_hash],
        )

        entries: list[KnowledgeDiscoveryCatalogEntry] = []
        for final_skill_id in sorted(member_by_id):
            member = member_by_id[final_skill_id]
            spec = spec_by_id[final_skill_id]
            entries.append(
                KnowledgeDiscoveryCatalogEntry(
                    final_skill_id=member.final_skill_id,
                    skill_name=member.skill_name,
                    primary_module=member.primary_module,
                    source_hashes=member.source_hashes,
                    skill_object_hash=member.object_hash,
                    skill_origin=member.skill_origin,
                    disposition=spec.disposition,
                    reason_codes=sorted(spec.reason_codes),
                    conditions=sorted(spec.conditions, key=lambda item: item.condition_id),
                )
            )

        seed = {
            "schema_version": "knowledge-discovery-catalog-v1",
            "run_id": inventory.run_id,
            "registry_release_id": status.registry_release_id,
            "registry_object_hash": status.registry_object_hash,
            "compiler_version": batch.compiler_version,
            "disposition_batch_hash": batch_ref.sha256,
            "entries": [item.model_dump(mode="json") for item in entries],
            "member_count": len(entries),
            "formal_committee_weight_allowed": False,
        }
        result_hash = sha256_bytes(canonical_json_bytes(seed))
        catalog = KnowledgeDiscoveryCatalog(
            **seed,
            result_hash=result_hash,
        )
        catalog_ref = self.objects.put_json(catalog.model_dump(mode="json"))
        artifact_id = f"KnowledgeDiscoveryCatalog:{inventory.run_id}:{result_hash}"
        self.state.register_artifact(
            artifact_id=artifact_id,
            artifact_type="KnowledgeDiscoveryCatalog",
            schema_version=catalog.schema_version,
            object_hash=catalog_ref.sha256,
            input_hashes=[
                inventory.result_hash,
                status.registry_object_hash,
                batch_ref.sha256,
            ],
        )
        return KnowledgeDiscoveryCatalogRecord(
            catalog=catalog,
            artifact_id=artifact_id,
            object_hash=catalog_ref.sha256,
            disposition_batch_artifact_id=batch_artifact_id,
            disposition_batch_object_hash=batch_ref.sha256,
        )


__all__ = ["KnowledgeDiscoveryCatalogCompiler"]
