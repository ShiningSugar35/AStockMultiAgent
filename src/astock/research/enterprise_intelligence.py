"""Deterministic resolution of enterprise/personnel event evidence."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal

from astock.core.hashing import content_hash
from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.evidence.repository import EvidenceRepository
from astock.schemas.enterprise_intelligence import (
    EnterpriseEventType,
    EnterpriseIntelligenceObservation,
    EnterpriseIntelligenceResolution,
    EnterpriseResolutionStatus,
)
from astock.schemas.evidence import Evidence, EvidenceGrade, FactStatus
from astock.schemas.full_research import EventEvidenceClass, NewsEvent
from astock.schemas.market import SourceClass

_EVENT_CATEGORY = {
    EnterpriseEventType.MANAGEMENT_APPOINTMENT: "management",
    EnterpriseEventType.MANAGEMENT_RESIGNATION: "management",
    EnterpriseEventType.PUBLIC_OFFICE_APPOINTMENT: "key_personnel_external_appointment",
    EnterpriseEventType.CONTROLLER_CHANGE: "ownership_control",
    EnterpriseEventType.LEGAL_REPRESENTATIVE_CHANGE: "business_registration",
    EnterpriseEventType.BUSINESS_REGISTRATION_CHANGE: "business_registration",
    EnterpriseEventType.EQUITY_PLEDGE: "equity_pledge_freeze",
    EnterpriseEventType.EQUITY_FREEZE: "equity_pledge_freeze",
    EnterpriseEventType.LITIGATION: "litigation",
    EnterpriseEventType.ENFORCEMENT: "credit_enforcement",
    EnterpriseEventType.CREDIT_RISK: "credit_enforcement",
    EnterpriseEventType.ADMINISTRATIVE_PENALTY: "investigation",
    EnterpriseEventType.SUBSIDIARY_CHANGE: "supply_chain",
    EnterpriseEventType.PARTNER_CHANGE: "supply_chain",
    EnterpriseEventType.KEY_PARTNER_PERSONNEL: "key_partner_personnel",
}


class EnterpriseIntelligenceService:
    def __init__(self, state: StateStore, objects: ObjectStore) -> None:
        self.state = state
        self.objects = objects
        self.evidence = EvidenceRepository(state)

    def register_observation(
        self,
        observation: EnterpriseIntelligenceObservation,
    ) -> str:
        evidence = self.evidence.get_evidence(observation.evidence_id)
        if evidence is None:
            raise ValueError("enterprise intelligence references unknown Evidence")
        if (
            evidence.snapshot_id != observation.source_snapshot_id
            or evidence.excerpt_object_sha256 != observation.evidence_object_hash
            or evidence.available_to_system_at > observation.available_to_system_at
            or not self.objects.verify(observation.evidence_object_hash)
        ):
            raise ValueError("enterprise intelligence Evidence lineage drift")
        entity_ids = set(evidence.entity_ids)
        expected_entities = {
            observation.company_id,
            observation.instrument_id,
            *observation.related_entity_ids,
        }
        if not entity_ids.intersection(expected_entities):
            raise ValueError("enterprise intelligence Evidence lacks entity linkage")
        snapshot = self.state.get_snapshot(observation.source_snapshot_id)
        if (
            snapshot is None
            or snapshot.source_id != observation.source_id
            or not self.objects.verify(snapshot.object_sha256)
            or snapshot.available_to_system_at > observation.available_to_system_at
        ):
            raise ValueError("enterprise intelligence source snapshot lineage drift")
        self._validate_fact_evidence(evidence)
        ref = self.objects.put_json(observation.model_dump(mode="json"))
        artifact_id = f"EnterpriseIntelligenceObservation:{observation.observation_id}"
        existing = self.state.artifact_record(artifact_id)
        if existing is not None:
            if (
                str(existing["type"]) != "EnterpriseIntelligenceObservation"
                or str(existing["object_hash"]) != ref.sha256
            ):
                raise ValueError("enterprise intelligence observation identity collision")
            return artifact_id
        self.state.register_artifact(
            artifact_id=artifact_id,
            artifact_type="EnterpriseIntelligenceObservation",
            schema_version=observation.schema_version,
            object_hash=ref.sha256,
            input_hashes=sorted(
                {observation.evidence_object_hash, snapshot.object_sha256}
            ),
        )
        return artifact_id

    def resolve(
        self,
        observations: list[EnterpriseIntelligenceObservation],
        *,
        resolved_at: datetime | None = None,
    ) -> EnterpriseIntelligenceResolution:
        if not observations:
            raise ValueError("enterprise intelligence resolution requires observations")
        company_ids = {item.company_id for item in observations}
        fact_keys = {item.fact_key for item in observations}
        if len(company_ids) != 1 or len(fact_keys) != 1:
            raise ValueError("enterprise intelligence resolution must cover one company fact")
        for item in observations:
            self.register_observation(item)
        ordered = sorted(
            observations,
            key=lambda item: (item.available_to_system_at, item.observation_id),
        )
        timestamp = (
            resolved_at or max(item.available_to_system_at for item in ordered)
        ).astimezone(UTC)
        if any(item.available_to_system_at > timestamp for item in ordered):
            raise ValueError(
                "enterprise intelligence resolution cannot include future observations"
            )
        values = {self._normalize_value(item.fact_value) for item in ordered}
        source_ids = tuple(sorted({item.source_id for item in ordered}))
        independence_groups = {
            self._source_independence_group(item) for item in ordered
        }
        if len(values) == 1:
            status = EnterpriseResolutionStatus.CONFIRMED
            preferred = self._preferred_observation(ordered).observation_id
            conflict_codes: tuple[str, ...] = ()
            investigation_required = False
        else:
            status = EnterpriseResolutionStatus.CONFLICTED
            preferred = None
            conflict_codes = ("MATERIAL_CROSS_SOURCE_FACT_CONFLICT",)
            investigation_required = True
        identity = {
            "authority_policy": "canonical-direct-evidence-v1",
            "company_id": ordered[0].company_id,
            "fact_key": ordered[0].fact_key,
            "observation_ids": sorted(item.observation_id for item in ordered),
            "values": sorted(values),
            "resolved_at": timestamp.isoformat(),
        }
        resolution = EnterpriseIntelligenceResolution(
            resolution_id="enterprise-resolution:" + content_hash(identity),
            company_id=ordered[0].company_id,
            fact_key=ordered[0].fact_key,
            observation_ids=tuple(sorted(item.observation_id for item in ordered)),
            status=status,
            preferred_observation_id=preferred,
            cross_source_confirmed=(
                status is EnterpriseResolutionStatus.CONFIRMED
                and len(source_ids) > 1
                and len(independence_groups) > 1
            ),
            investigation_required=investigation_required,
            conflict_reason_codes=conflict_codes,
            source_ids=source_ids,
            source_independence_groups=tuple(sorted(independence_groups)),
            evidence_ids=tuple(sorted({item.evidence_id for item in ordered})),
            source_snapshot_ids=tuple(sorted({item.source_snapshot_id for item in ordered})),
            resolved_at=timestamp,
            created_at=timestamp,
        )
        ref = self.objects.put_json(resolution.model_dump(mode="json"))
        artifact_id = f"EnterpriseIntelligenceResolution:{resolution.resolution_id}"
        inputs: set[str] = set()
        for item in ordered:
            record = self.state.artifact_record(
                f"EnterpriseIntelligenceObservation:{item.observation_id}"
            )
            if record is None:
                raise ValueError(
                    "enterprise intelligence observation disappeared before resolution"
                )
            inputs.add(str(record["object_hash"]))
        existing = self.state.artifact_record(artifact_id)
        if existing is not None:
            if str(existing["object_hash"]) != ref.sha256:
                raise ValueError("enterprise intelligence resolution identity collision")
            return resolution
        self.state.register_artifact(
            artifact_id=artifact_id,
            artifact_type="EnterpriseIntelligenceResolution",
            schema_version=resolution.schema_version,
            object_hash=ref.sha256,
            input_hashes=sorted(inputs),
        )
        return resolution

    def project_news_event(
        self,
        resolution: EnterpriseIntelligenceResolution,
        observations: list[EnterpriseIntelligenceObservation],
        *,
        expected_direction: Literal[
            "POSITIVE", "NEGATIVE", "MIXED", "NEUTRAL", "UNKNOWN"
        ] = "UNKNOWN",
        impact_horizon: str = "90D",
        already_priced_probability: Decimal = Decimal("0.5"),
    ) -> NewsEvent:
        resolution_artifact_id = (
            f"EnterpriseIntelligenceResolution:{resolution.resolution_id}"
        )
        resolution_record = self.state.artifact_record(resolution_artifact_id)
        if (
            resolution_record is None
            or str(resolution_record["type"]) != "EnterpriseIntelligenceResolution"
            or not self.objects.verify(str(resolution_record["object_hash"]))
        ):
            raise ValueError("enterprise intelligence resolution is not registered")
        stored_resolution = EnterpriseIntelligenceResolution.model_validate_json(
            self.objects.get_bytes(str(resolution_record["object_hash"]))
        )
        if stored_resolution != resolution:
            raise ValueError("enterprise intelligence resolution object drift")
        by_id = {item.observation_id: item for item in observations}
        if set(by_id) != set(resolution.observation_ids):
            raise ValueError("enterprise intelligence observations do not match resolution")
        for item in observations:
            self.register_observation(item)
        if resolution.status is not EnterpriseResolutionStatus.CONFIRMED:
            raise ValueError("conflicted enterprise intelligence cannot become a formal event")
        preferred = by_id.get(resolution.preferred_observation_id or "")
        if preferred is None:
            raise ValueError("enterprise resolution preferred observation is unavailable")
        evidence_class = (
            EventEvidenceClass.OFFICIAL_FILING
            if self._effective_source_class(preferred) is SourceClass.PRIMARY_OFFICIAL_WEB
            else EventEvidenceClass.SECONDARY_STRUCTURED
        )
        independent_sources = (
            len({item.source_id for item in observations}) > 1
            and len({self._source_independence_group(item) for item in observations}) > 1
        )
        return NewsEvent(
            event_id="enterprise-event:" + content_hash(
                {
                    "resolution_id": resolution.resolution_id,
                    "observation_id": preferred.observation_id,
                }
            ),
            instrument_id=preferred.instrument_id,
            event_timestamp=datetime.combine(
                preferred.event_date,
                datetime.min.time(),
                tzinfo=preferred.available_to_system_at.tzinfo,
            ),
            source_id=preferred.source_id,
            category=_EVENT_CATEGORY[preferred.event_type],
            related_entity_ids=preferred.related_entity_ids,
            person_names=preferred.person_names,
            relation_scope=preferred.relation_scope.value,
            evidence_class=evidence_class,
            confidence=(
                Decimal("1")
                if resolution.cross_source_confirmed and independent_sources
                else Decimal("0.8")
            ),
            expected_direction=expected_direction,
            impact_horizon=impact_horizon,
            already_priced_probability=already_priced_probability,
            summary=preferred.summary,
            created_at=resolution.resolved_at,
        )

    @staticmethod
    def _validate_fact_evidence(evidence: Evidence) -> None:
        # This service projects public enterprise facts, not community leads or
        # analyst inferences. Those remain in the canonical Evidence repository
        # for investigation; no observation or confirmed event is minted here.
        if evidence.fact_status is not FactStatus.DIRECT or evidence.evidence_grade not in {
            EvidenceGrade.PRIMARY_OFFICIAL,
            EvidenceGrade.SECONDARY,
        }:
            raise ValueError(
                "enterprise intelligence requires direct public-fact Evidence; "
                "retain the existing Evidence for investigation"
            )

    def _effective_source_class(
        self,
        observation: EnterpriseIntelligenceObservation,
    ) -> SourceClass:
        evidence = self.evidence.get_evidence(observation.evidence_id)
        if evidence is None:
            raise ValueError("enterprise intelligence references unknown Evidence")
        self._validate_fact_evidence(evidence)
        # An input label may understate authority but can never upgrade the
        # canonical evidence grade. Legacy overstated labels degrade to secondary.
        if (
            evidence.evidence_grade is EvidenceGrade.PRIMARY_OFFICIAL
            and observation.source_class is SourceClass.PRIMARY_OFFICIAL_WEB
        ):
            return SourceClass.PRIMARY_OFFICIAL_WEB
        return SourceClass.SECONDARY_STRUCTURED

    @staticmethod
    def _normalize_value(value: str) -> str:
        return " ".join(value.strip().lower().split())

    @staticmethod
    def _source_independence_group(
        observation: EnterpriseIntelligenceObservation,
    ) -> str:
        canonical_vendor_groups = {
            "qcc-enterprise-intelligence-": "QCC",
            "tianyancha-enterprise-intelligence-": "TIANYANCHA",
        }
        normalized_source = observation.source_id.lower()
        for prefix, group in canonical_vendor_groups.items():
            if normalized_source.startswith(prefix):
                return group
        return observation.source_independence_group or observation.source_id

    def _preferred_observation(
        self,
        observations: list[EnterpriseIntelligenceObservation],
    ) -> EnterpriseIntelligenceObservation:
        official = [
            item
            for item in observations
            if self._effective_source_class(item) is SourceClass.PRIMARY_OFFICIAL_WEB
        ]
        pool = official or observations
        return max(pool, key=lambda item: (item.available_to_system_at, item.observation_id))


__all__ = ["EnterpriseIntelligenceService"]
