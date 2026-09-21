"""Bounded execution of existing discovery recovery capabilities.

This layer never turns a successful data fetch into a semantic discovery claim.
It runs the governed current-research acquisition graph, records provider
fallback evidence, and tells the reviewer which discovery roles can now be
reviewed locally versus which still require authoritative public research.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from time import perf_counter
from typing import Protocol

from astock.candidates.discovery_runtime import (
    DiscoveryEvidenceNeed,
    DiscoveryRuntimeCompanyContext,
)
from astock.core.hashing import content_hash
from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.research.acquisition import CurrentResearchAcquisitionService
from astock.schemas.market import Market
from astock.schemas.research_acquisition import (
    AcquisitionAttemptStatus,
    CurrentResearchAcquisitionReport,
)
from astock.settings import ProjectPaths


@dataclass(frozen=True)
class DiscoveryCompanyRecovery:
    company_id: str
    requested_capabilities: tuple[str, ...]
    acquisition_report_id: str
    acquisition_status: str
    attempted_capabilities: tuple[str, ...]
    succeeded_capabilities: tuple[str, ...]
    fallback_capabilities: tuple[str, ...]
    external_research_capabilities: tuple[str, ...]
    local_review_ready_roles: tuple[str, ...]
    public_research_required_roles: tuple[str, ...]


@dataclass(frozen=True)
class DiscoveryRecoveryBatch:
    artifact_id: str
    company_recoveries: tuple[DiscoveryCompanyRecovery, ...]
    no_existing_capability_roles: tuple[tuple[str, str], ...]
    unattempted_roles: tuple[tuple[str, str], ...]
    recovery_budget_seconds: int
    elapsed_seconds: float
    budget_exhausted: bool

    @property
    def acquisition_report_ids(self) -> tuple[str, ...]:
        return tuple(item.acquisition_report_id for item in self.company_recoveries)


class _AcquisitionService(Protocol):
    def acquire(
        self,
        company_id: str,
        market: Market,
    ) -> CurrentResearchAcquisitionReport: ...


class DiscoveryRecoveryService:
    """Execute existing provider/fallback capabilities before public-source escalation."""

    def __init__(
        self,
        paths: ProjectPaths,
        state: StateStore,
        objects: ObjectStore,
        *,
        acquisition_factory: Callable[
            [ProjectPaths, StateStore, ObjectStore], _AcquisitionService
        ] = CurrentResearchAcquisitionService,
        monotonic: Callable[[], float] = perf_counter,
    ) -> None:
        self.paths = paths
        self.state = state
        self.objects = objects
        self.acquisition_factory = acquisition_factory
        self.monotonic = monotonic

    def execute(
        self,
        *,
        contexts: Sequence[DiscoveryRuntimeCompanyContext],
        evidence_needs: Sequence[DiscoveryEvidenceNeed],
        recovery_budget_seconds: int,
        max_companies: int,
    ) -> DiscoveryRecoveryBatch:
        if not 60 <= recovery_budget_seconds <= 1800:
            raise ValueError("discovery recovery budget must be in 60..1800 seconds")
        if not 1 <= max_companies <= 12:
            raise ValueError("discovery recovery company budget must be in 1..12")

        context_by_company = {item.company_id: item for item in contexts}
        needs_by_company: dict[str, list[DiscoveryEvidenceNeed]] = {}
        no_existing: list[tuple[str, str]] = []
        for need in evidence_needs:
            if need.company_id not in context_by_company:
                raise ValueError("discovery recovery need has no matching company context")
            needs_by_company.setdefault(need.company_id, []).append(need)
            if not need.existing_capabilities:
                no_existing.append((need.company_id, need.role))

        eligible_company_ids = sorted(
            company_id
            for company_id, needs in needs_by_company.items()
            if any(need.existing_capabilities for need in needs)
        )[:max_companies]

        started = self.monotonic()
        deadline = started + recovery_budget_seconds
        company_recoveries: list[DiscoveryCompanyRecovery] = []
        attempted_companies: set[str] = set()
        service = self.acquisition_factory(self.paths, self.state, self.objects)

        for company_id in eligible_company_ids:
            if self.monotonic() >= deadline:
                break
            context = context_by_company[company_id]
            company_needs = needs_by_company[company_id]
            requested = tuple(
                sorted(
                    {
                        capability
                        for need in company_needs
                        for capability in need.existing_capabilities
                    }
                )
            )
            report = service.acquire(context.company_id, context.market)
            attempted_companies.add(company_id)
            attempt_by_capability = {item.capability.value: item for item in report.attempts}
            succeeded = tuple(
                sorted(
                    capability
                    for capability in requested
                    if capability in attempt_by_capability
                    and attempt_by_capability[capability].status
                    is AcquisitionAttemptStatus.SUCCEEDED
                )
            )
            fallback = tuple(
                sorted(
                    capability
                    for capability in requested
                    if capability in attempt_by_capability
                    and attempt_by_capability[capability].fallback_used
                )
            )
            external = tuple(
                sorted(
                    {
                        item.capability.value
                        for item in report.external_research_needs
                        if item.capability.value in requested
                    }
                )
            )
            local_roles: list[str] = []
            public_roles: list[str] = []
            for need in company_needs:
                required = set(need.existing_capabilities)
                if required and required.issubset(succeeded):
                    local_roles.append(need.role)
                elif required:
                    public_roles.append(need.role)
            company_recoveries.append(
                DiscoveryCompanyRecovery(
                    company_id=company_id,
                    requested_capabilities=requested,
                    acquisition_report_id=report.report_id,
                    acquisition_status=report.status.value,
                    attempted_capabilities=tuple(sorted(attempt_by_capability)),
                    succeeded_capabilities=succeeded,
                    fallback_capabilities=fallback,
                    external_research_capabilities=external,
                    local_review_ready_roles=tuple(sorted(set(local_roles))),
                    public_research_required_roles=tuple(sorted(set(public_roles))),
                )
            )

        finished = self.monotonic()
        unattempted = sorted(
            (company_id, need.role)
            for company_id, needs in needs_by_company.items()
            if company_id not in attempted_companies
            for need in needs
            if need.existing_capabilities
        )
        budget_exhausted = bool(unattempted) and finished >= deadline

        payload = {
            "schema_version": "discovery-existing-recovery-v1",
            "company_recoveries": [
                {
                    "company_id": item.company_id,
                    "requested_capabilities": list(item.requested_capabilities),
                    "acquisition_report_id": item.acquisition_report_id,
                    "acquisition_status": item.acquisition_status,
                    "attempted_capabilities": list(item.attempted_capabilities),
                    "succeeded_capabilities": list(item.succeeded_capabilities),
                    "fallback_capabilities": list(item.fallback_capabilities),
                    "external_research_capabilities": list(item.external_research_capabilities),
                    "local_review_ready_roles": list(item.local_review_ready_roles),
                    "public_research_required_roles": list(
                        item.public_research_required_roles
                    ),
                }
                for item in company_recoveries
            ],
            "no_existing_capability_roles": [list(item) for item in sorted(set(no_existing))],
            "unattempted_roles": [list(item) for item in unattempted],
            "recovery_budget_seconds": recovery_budget_seconds,
            "elapsed_seconds": max(0.0, finished - started),
            "budget_exhausted": budget_exhausted,
        }
        ref = self.objects.put_json(payload)
        artifact_id = f"DiscoveryExistingRecovery:{content_hash(payload)}"
        if self.state.artifact_record(artifact_id) is None:
            input_hashes: list[str] = []
            for item in company_recoveries:
                record = self.state.artifact_record(item.acquisition_report_id)
                if record is None:
                    raise ValueError("discovery recovery acquisition report is not registered")
                input_hashes.append(str(record["object_hash"]))
            self.state.register_artifact(
                artifact_id=artifact_id,
                artifact_type="DiscoveryExistingRecovery",
                schema_version="discovery-existing-recovery-v1",
                object_hash=ref.sha256,
                input_hashes=sorted(set(input_hashes)),
            )
        self.state.set_checkpoint(
            scope_type="verified-discovery-recovery",
            scope_key="latest",
            cursor={
                "artifact_id": artifact_id,
                "acquisition_report_ids": [
                    item.acquisition_report_id for item in company_recoveries
                ],
                "local_review_ready_role_count": sum(
                    len(item.local_review_ready_roles) for item in company_recoveries
                ),
                "public_research_required_role_count": (
                    len(set(no_existing))
                    + sum(
                        len(item.public_research_required_roles)
                        for item in company_recoveries
                    )
                ),
                "unattempted_role_count": len(unattempted),
                "budget_exhausted": budget_exhausted,
            },
            status="BUDGET_EXHAUSTED" if budget_exhausted else "REVIEW_REQUIRED",
            object_hash=ref.sha256,
        )
        return DiscoveryRecoveryBatch(
            artifact_id=artifact_id,
            company_recoveries=tuple(company_recoveries),
            no_existing_capability_roles=tuple(sorted(set(no_existing))),
            unattempted_roles=tuple(unattempted),
            recovery_budget_seconds=recovery_budget_seconds,
            elapsed_seconds=max(0.0, finished - started),
            budget_exhausted=budget_exhausted,
        )


__all__ = [
    "DiscoveryCompanyRecovery",
    "DiscoveryRecoveryBatch",
    "DiscoveryRecoveryService",
]
