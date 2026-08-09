"""Knowledge-side implementation of the narrow research KnowledgeSkillProvider port."""

from __future__ import annotations

import json
import re
from time import perf_counter

from astock.core.hashing import canonical_json_bytes, sha256_bytes
from astock.core.object_store import ObjectStore
from astock.knowledge.completion_repository import KnowledgeCompletionRepository
from astock.schemas.direct_source_distillation import DirectSkillModule
from astock.schemas.knowledge_completion import (
    KnowledgeAdmissionBasis,
    KnowledgeProviderMode,
    KnowledgeProviderReadiness,
    KnowledgeProviderStatus,
    KnowledgeSkillQuery,
    KnowledgeSkillSelection,
    KnowledgeSkillSummary,
)

_TOKEN_RE = re.compile(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]")


def _terms(text: str) -> set[str]:
    return {item.casefold() for item in _TOKEN_RE.findall(text) if item.strip()}


def _estimated_tokens(byte_count: int) -> int:
    return (byte_count + 3) // 4


class RepositoryKnowledgeSkillProvider:
    """Bounded admitted-Skill retrieval over one immutable registry release."""

    def __init__(
        self,
        repository: KnowledgeCompletionRepository,
        objects: ObjectStore,
    ) -> None:
        self.repository = repository
        self.objects = objects
        self._cache: dict[str, KnowledgeSkillSelection] = {}
        self.call_count = 0

    def status(self, run_id: str) -> KnowledgeProviderStatus:
        completion = self.repository.completion_status(run_id)
        total = int(completion["total_skill_count"])
        ready = int(completion["ready_skill_count"])
        pending = int(completion["pending_review_count"])
        approved = int(completion["approved_count"])
        rejected = int(completion["rejected_count"])
        release = self.repository.registry_release(run_id)

        if str(completion["source_run_stage"]) != "FINALIZED":
            return KnowledgeProviderStatus(
                run_id=run_id,
                status=KnowledgeProviderReadiness.NEEDS_INFO,
                mode=KnowledgeProviderMode.BLOCKED,
                reason_code="DIRECT_RUN_NOT_FINALIZED",
                total_skill_count=total,
                ready_skill_count=ready,
                pending_review_count=pending,
                approved_count=approved,
                rejected_count=rejected,
                eligible_skill_count=0,
            )
        if pending > 0:
            return KnowledgeProviderStatus(
                run_id=run_id,
                status=KnowledgeProviderReadiness.NEEDS_INFO,
                mode=KnowledgeProviderMode.BLOCKED,
                reason_code="DIRECT_REVIEW_PENDING",
                total_skill_count=total,
                ready_skill_count=ready,
                pending_review_count=pending,
                approved_count=approved,
                rejected_count=rejected,
                eligible_skill_count=0,
            )
        if release is None:
            return KnowledgeProviderStatus(
                run_id=run_id,
                status=KnowledgeProviderReadiness.NEEDS_INFO,
                mode=KnowledgeProviderMode.BLOCKED,
                reason_code="REGISTRY_RELEASE_MISSING",
                total_skill_count=total,
                ready_skill_count=ready,
                pending_review_count=pending,
                approved_count=approved,
                rejected_count=rejected,
                eligible_skill_count=0,
            )
        eligible = int(release["admitted_skill_count"])
        if eligible != ready + approved:
            return KnowledgeProviderStatus(
                run_id=run_id,
                status=KnowledgeProviderReadiness.NEEDS_INFO,
                mode=KnowledgeProviderMode.BLOCKED,
                reason_code="REGISTRY_COUNT_DRIFT",
                total_skill_count=total,
                ready_skill_count=ready,
                pending_review_count=pending,
                approved_count=approved,
                rejected_count=rejected,
                eligible_skill_count=0,
            )
        release_artifact_id = str(release["release_artifact_id"])
        release_object_hash = str(release["release_object_hash"])
        if not self.objects.verify(release_object_hash):
            return KnowledgeProviderStatus(
                run_id=run_id,
                status=KnowledgeProviderReadiness.NEEDS_INFO,
                mode=KnowledgeProviderMode.BLOCKED,
                reason_code="REGISTRY_OBJECT_MISSING",
                total_skill_count=total,
                ready_skill_count=ready,
                pending_review_count=pending,
                approved_count=approved,
                rejected_count=rejected,
                eligible_skill_count=0,
            )
        if (
            self.repository.artifact_object_hash(release_artifact_id)
            != release_object_hash
        ):
            return KnowledgeProviderStatus(
                run_id=run_id,
                status=KnowledgeProviderReadiness.NEEDS_INFO,
                mode=KnowledgeProviderMode.BLOCKED,
                reason_code="REGISTRY_ARTIFACT_DRIFT",
                total_skill_count=total,
                ready_skill_count=ready,
                pending_review_count=pending,
                approved_count=approved,
                rejected_count=rejected,
                eligible_skill_count=0,
            )
        members = self.repository.registry_members(str(release["release_id"]))
        if len(members) != eligible:
            return KnowledgeProviderStatus(
                run_id=run_id,
                status=KnowledgeProviderReadiness.NEEDS_INFO,
                mode=KnowledgeProviderMode.BLOCKED,
                reason_code="REGISTRY_MEMBER_COUNT_DRIFT",
                total_skill_count=total,
                ready_skill_count=ready,
                pending_review_count=pending,
                approved_count=approved,
                rejected_count=rejected,
                eligible_skill_count=0,
            )
        if any(
            not self.objects.verify(str(member["skill_object_hash"]))
            for member in members
        ):
            return KnowledgeProviderStatus(
                run_id=run_id,
                status=KnowledgeProviderReadiness.NEEDS_INFO,
                mode=KnowledgeProviderMode.BLOCKED,
                reason_code="REGISTRY_MEMBER_OBJECT_MISSING",
                total_skill_count=total,
                ready_skill_count=ready,
                pending_review_count=pending,
                approved_count=approved,
                rejected_count=rejected,
                eligible_skill_count=0,
            )
        if any(
            self.repository.artifact_object_hash(str(member["skill_artifact_id"]))
            != str(member["skill_object_hash"])
            for member in members
        ):
            return KnowledgeProviderStatus(
                run_id=run_id,
                status=KnowledgeProviderReadiness.NEEDS_INFO,
                mode=KnowledgeProviderMode.BLOCKED,
                reason_code="REGISTRY_MEMBER_ARTIFACT_DRIFT",
                total_skill_count=total,
                ready_skill_count=ready,
                pending_review_count=pending,
                approved_count=approved,
                rejected_count=rejected,
                eligible_skill_count=0,
            )
        return KnowledgeProviderStatus(
            run_id=run_id,
            status=KnowledgeProviderReadiness.READY,
            mode=KnowledgeProviderMode.REGISTRY_RELEASE,
            reason_code="REGISTRY_READY",
            total_skill_count=total,
            ready_skill_count=ready,
            pending_review_count=pending,
            approved_count=approved,
            rejected_count=rejected,
            eligible_skill_count=eligible,
            registry_release_id=str(release["release_id"]),
            registry_artifact_id=str(release["release_artifact_id"]),
            registry_object_hash=str(release["release_object_hash"]),
        )

    def _blocked_selection(
        self,
        *,
        query: KnowledgeSkillQuery,
        provider_status: KnowledgeProviderStatus,
        cache_key: str,
        reason_code: str,
        started: float,
    ) -> KnowledgeSkillSelection:
        blocked_status = KnowledgeProviderStatus(
            run_id=provider_status.run_id,
            status=KnowledgeProviderReadiness.NEEDS_INFO,
            mode=KnowledgeProviderMode.BLOCKED,
            reason_code=reason_code,
            total_skill_count=provider_status.total_skill_count,
            ready_skill_count=provider_status.ready_skill_count,
            pending_review_count=provider_status.pending_review_count,
            approved_count=provider_status.approved_count,
            rejected_count=provider_status.rejected_count,
            eligible_skill_count=0,
        )
        result_hash = sha256_bytes(
            canonical_json_bytes(
                {
                    "cache_key": cache_key,
                    "reason_code": reason_code,
                    "skills": [],
                }
            )
        )
        selection = KnowledgeSkillSelection(
            query=query,
            provider_status=blocked_status,
            skills=[],
            candidate_count=0,
            selected_count=0,
            latency_ms=max(0, int((perf_counter() - started) * 1000)),
            context_bytes=0,
            estimated_tokens=0,
            cache_key=cache_key,
            cache_hit=False,
            result_hash=result_hash,
            reason_code=reason_code,
        )
        self._cache[cache_key] = selection
        return selection

    def select(self, run_id: str, query: KnowledgeSkillQuery) -> KnowledgeSkillSelection:
        started = perf_counter()
        self.call_count += 1
        provider_status = self.status(run_id)
        release_hash = provider_status.registry_object_hash or "0" * 64
        cache_key = sha256_bytes(
            canonical_json_bytes(
                {
                    "run_id": run_id,
                    "registry_object_hash": release_hash,
                    "query": query.model_dump(mode="json"),
                }
            )
        )
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached.model_copy(update={"cache_hit": True, "latency_ms": 0})
        if provider_status.status is not KnowledgeProviderReadiness.READY:
            return self._blocked_selection(
                query=query,
                provider_status=provider_status,
                cache_key=cache_key,
                reason_code=provider_status.reason_code,
                started=started,
            )

        requested_modules = set(query.modules)
        query_terms = _terms(query.query)
        filtered: list[tuple[dict[str, object], set[str]]] = []
        for raw_row in self.repository.eligible_skill_rows(run_id):
            row = dict(raw_row)
            skill_hash = str(row["skill_object_hash"])
            skill_json = str(row["skill_json"])
            if sha256_bytes(skill_json.encode("utf-8")) != skill_hash:
                return self._blocked_selection(
                    query=query,
                    provider_status=provider_status,
                    cache_key=cache_key,
                    reason_code="REGISTRY_SKILL_JSON_HASH_DRIFT",
                    started=started,
                )
            try:
                skill_payload = json.loads(skill_json)
                secondary_values = json.loads(str(row["secondary_modules_json"]))
                source_hash_values = json.loads(str(row["source_hashes_json"]))
            except (json.JSONDecodeError, TypeError):
                return self._blocked_selection(
                    query=query,
                    provider_status=provider_status,
                    cache_key=cache_key,
                    reason_code="REGISTRY_SKILL_JSON_INVALID",
                    started=started,
                )
            if (
                not isinstance(skill_payload, dict)
                or not isinstance(secondary_values, list)
                or not isinstance(source_hash_values, list)
                or not all(isinstance(item, str) for item in source_hash_values)
            ):
                return self._blocked_selection(
                    query=query,
                    provider_status=provider_status,
                    cache_key=cache_key,
                    reason_code="REGISTRY_SKILL_JSON_INVALID",
                    started=started,
                )
            member_source_hashes = sorted(set(source_hash_values))
            payload_source_hashes = sorted(
                {
                    str(item.get("source_object_hash"))
                    for item in skill_payload.get("source_refs", [])
                    if isinstance(item, dict) and item.get("source_object_hash")
                }
            )
            expected_status = (
                "READY_FOR_SHADOW"
                if str(row["admission_basis"]) == KnowledgeAdmissionBasis.READY.value
                else "NEEDS_USER_REVIEW"
            )
            if (
                skill_payload.get("final_skill_id") != str(row["final_skill_id"])
                or skill_payload.get("status") != str(row["status"])
                or str(row["status"]) != expected_status
                or skill_payload.get("skill_name") != str(row["skill_name"])
                or skill_payload.get("primary_module") != str(row["primary_module"])
                or skill_payload.get("secondary_modules") != secondary_values
                or skill_payload.get("decision_question")
                != str(row["decision_question"])
                or skill_payload.get("core_principle") != str(row["core_principle"])
                or skill_payload.get("formal_committee_weight_allowed") is not False
                or payload_source_hashes != member_source_hashes
            ):
                return self._blocked_selection(
                    query=query,
                    provider_status=provider_status,
                    cache_key=cache_key,
                    reason_code="REGISTRY_SKILL_BINDING_DRIFT",
                    started=started,
                )
            try:
                primary = DirectSkillModule(str(row["primary_module"]))
                secondary = {DirectSkillModule(str(item)) for item in secondary_values}
            except (TypeError, ValueError):
                return self._blocked_selection(
                    query=query,
                    provider_status=provider_status,
                    cache_key=cache_key,
                    reason_code="REGISTRY_SKILL_MODULE_INVALID",
                    started=started,
                )
            if requested_modules and not ({primary, *secondary} & requested_modules):
                continue
            searchable = " ".join(
                (
                    str(row["skill_name"]),
                    str(row["decision_question"]),
                    str(row["core_principle"]),
                )
            )
            filtered.append((row, _terms(searchable)))

        scored: list[tuple[int, str, dict[str, object]]] = []
        for row, skill_terms in filtered:
            overlap = len(query_terms & skill_terms)
            if query_terms and overlap == 0:
                continue
            scored.append((overlap, str(row["final_skill_id"]), row))
        scored.sort(key=lambda item: (-item[0], item[1]))
        candidate_count = len(scored)

        selected: list[KnowledgeSkillSummary] = []
        context_bytes = 0
        for _, _, row in scored:
            if len(selected) >= query.top_k:
                break
            source_hashes = sorted(set(json.loads(str(row["source_hashes_json"]))))
            summary = KnowledgeSkillSummary(
                final_skill_id=str(row["final_skill_id"]),
                skill_name=str(row["skill_name"]),
                primary_module=DirectSkillModule(str(row["primary_module"])),
                decision_question=str(row["decision_question"]),
                summary=str(row["core_principle"]),
                source_hashes=source_hashes,
                artifact_id=str(row["skill_artifact_id"]),
                object_hash=str(row["skill_object_hash"]),
                admission_basis=KnowledgeAdmissionBasis(str(row["admission_basis"])),
            )
            encoded = canonical_json_bytes(summary.model_dump(mode="json"))
            new_bytes = context_bytes + len(encoded)
            if new_bytes > query.max_context_bytes:
                continue
            if _estimated_tokens(new_bytes) > query.max_estimated_tokens:
                continue
            selected.append(summary)
            context_bytes = new_bytes

        reason_code = "SELECTION_READY" if selected else "NO_MATCHING_ADMITTED_SKILL"
        result_seed = {
            "cache_key": cache_key,
            "candidate_count": candidate_count,
            "skills": [item.model_dump(mode="json") for item in selected],
            "context_bytes": context_bytes,
            "estimated_tokens": _estimated_tokens(context_bytes),
            "reason_code": reason_code,
        }
        selection = KnowledgeSkillSelection(
            query=query,
            provider_status=provider_status,
            skills=selected,
            candidate_count=candidate_count,
            selected_count=len(selected),
            latency_ms=max(0, int((perf_counter() - started) * 1000)),
            context_bytes=context_bytes,
            estimated_tokens=_estimated_tokens(context_bytes),
            cache_key=cache_key,
            cache_hit=False,
            result_hash=sha256_bytes(canonical_json_bytes(result_seed)),
            reason_code=reason_code,
        )
        self._cache[cache_key] = selection
        return selection


__all__ = ["RepositoryKnowledgeSkillProvider"]
