"""Read-only verification of capability references in the canonical artifact store."""

from __future__ import annotations

from datetime import datetime
from importlib import import_module
from typing import Any

from pydantic import BaseModel

from astock import schemas
from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.investor_orchestration.domain_contracts import DomainContractAudit
from astock.investor_orchestration.models import (
    CapabilityCoverageReceipt,
    CapabilityExecutionPlan,
    CapabilityNode,
    CapabilityRequirement,
    CapabilityRunStatus,
    InvestorRequestEnvelope,
    InvestorSessionPreflightReceipt,
    RequestIntent,
    ResearchSubjectEvent,
)
from astock.investor_orchestration.store import InvestorOrchestrationStore
from astock.investor_orchestration.utils import content_hash, utc_now

_SCHEMA_MODULES = {
    "MarketPriceAnchor": "astock.schemas.institutional_research",
    "ClassifiedTradeProtocol": "astock.schemas.research_runtime",
    "ResearchRoleOutput": "astock.schemas.research_team",
    "RecommendationReadinessReport": "astock.schemas.research_team",
    "ExternalAccountProjection": "astock.schemas.external_accounts",
    "ExternalAccountEvent": "astock.schemas.external_accounts",
    "ExternalAccountOperationReceipt": "astock.schemas.external_accounts",
    "PaperPreparationReceipt": "astock.schemas.paper",
    "ReplayExecutionReport": "astock.schemas.paper",
    "ResearchNarrativeBundle": "astock.schemas.presentation",
    "PortfolioAnalysisReport": "astock.schemas.portfolio",
    "ETFResearchMetrics": "astock.schemas.portfolio_decision",
    "ResearchTeamPlan": "astock.schemas.research_team",
    "ResearchRoleResult": "astock.schemas.research_team",
    "ResearchSeedReport": "astock.schemas.research_seeds",
}
_LOCAL_OUTPUTS = frozenset(
    {
        "REQUEST_TIME",
        "ENTITY_IDENTITY",
        "SESSION_PREFLIGHT",
        "MARKET_REGIME",
        "SUBJECT_REGISTRY",
        "RESPONSE_GATEWAY",
    }
)


def output_model(name: str) -> type[BaseModel]:
    module = import_module(_SCHEMA_MODULES[name]) if name in _SCHEMA_MODULES else schemas
    model = getattr(module, name, None)
    if not isinstance(model, type) or not issubclass(model, BaseModel):
        raise ValueError(f"no canonical output contract for {name}")
    return model


class RegisteredOutputVerifier:
    """An identifier is only a reference; it does not certify research completion."""

    def __init__(
        self, store: InvestorOrchestrationStore, objects: ObjectStore | None = None
    ) -> None:
        self.store = store
        self.state = StateStore(store.path)
        self.objects = objects or ObjectStore(store.path.parent / "objects" / "sha256")

    def load(self, artifact_id: str, model: type[BaseModel]) -> BaseModel:
        record = self.state.artifact_record(artifact_id)
        if record is None or record["type"] != model.__name__:
            raise ValueError("output is not registered with the required artifact type")
        result = model.model_validate_json(self.objects.get_bytes(str(record["object_hash"])))
        version = getattr(result, "schema_version", None)
        if version is not None and version != record["schema_version"]:
            raise ValueError("artifact registry and payload schema versions differ")
        return result

    def verify(
        self,
        node: CapabilityNode,
        artifact_ids: tuple[str, ...],
        request: InvestorRequestEnvelope,
        preflight: InvestorSessionPreflightReceipt,
    ) -> None:
        request = InvestorRequestEnvelope.model_validate(request.model_dump())
        if not artifact_ids or any(not value.strip() for value in artifact_ids):
            raise ValueError("required typed output is missing")
        if len(artifact_ids) != len(set(artifact_ids)):
            raise ValueError("duplicate output identifiers are not independent coverage")
        allowed_inputs: set[str] | None = None
        if request.decision_time is not None:
            from astock.investor_orchestration.decision_freeze import DecisionFreezeService

            freezes = DecisionFreezeService(self.store, self.objects)
            freezes.verify(request)
            frozen = freezes._load(request.decision_freeze_artifact_id or "")
            if frozen is None:
                raise ValueError("registered decision freeze is unavailable")
            allowed_inputs = {binding.artifact_id for binding in frozen.input_bindings}
            if preflight.as_of != request.evidence_cutoff:
                raise ValueError("preflight does not use the authenticated decision cutoff")
        if self._verify_local(node.capability_id, artifact_ids, request, preflight):
            return
        if allowed_inputs is not None and not set(artifact_ids) <= allowed_inputs:
            raise ValueError("capability output is not bound to the frozen decision inputs")
        model = output_model(node.output_schema)
        for artifact_id in artifact_ids:
            output = self.load(artifact_id, model)
            payload = output.model_dump(mode="json")
            self._check_time(payload, request.evidence_cutoff, node.freshness_seconds)
            self._verify_linked_hashes(payload)
            if payload.get("request_id") not in (None, request.request_id):
                raise ValueError("output belongs to another request")
            if request.account_id is not None and payload.get("account_id") not in (
                None,
                request.account_id,
            ):
                raise ValueError("output belongs to another account")
            entity = payload.get("instrument_id", payload.get("company_id"))
            if (
                entity is not None
                and request.entity_ids
                and not any(
                    self._same_identity(str(entity), target) for target in request.entity_ids
                )
            ):
                raise ValueError("output belongs to another security")
            status = str(payload.get("status", payload.get("coverage_status", "")))
            safe_prepare_needs_info = (
                node.output_schema == "PaperPreparationReceipt"
                and request.normalized_intent is RequestIntent.PAPER_PREPARE
                and status == "NEEDS_INFO"
            )
            if (
                status in {"FAILED", "BLOCKED", "NEEDS_INFO", "DEGRADED", "INCOMPLETE"}
                and not safe_prepare_needs_info
            ):
                raise ValueError("output did not pass its domain-specific completion gate")
            if payload.get("broker_execution_allowed", False):
                raise ValueError("research output cannot authorize broker execution")
            DomainContractAudit(self).check(node, artifact_id, output, request)

    @staticmethod
    def _payload_mappings(payload: dict[str, Any]) -> list[dict[str, Any]]:
        mappings: list[dict[str, Any]] = []
        stack: list[tuple[Any, int]] = [(payload, 0)]
        visited = 0
        while stack:
            value, depth = stack.pop()
            visited += 1
            if depth > 64 or visited > 100_000:
                raise ValueError("artifact lineage exceeds the bounded inspection budget")
            if isinstance(value, dict):
                mappings.append(value)
                stack.extend((item, depth + 1) for item in value.values())
            elif isinstance(value, (list, tuple)):
                stack.extend((item, depth + 1) for item in value)
        return mappings

    def _verify_reference(self, artifact_id: str, digest: str) -> None:
        if not isinstance(artifact_id, str) or not isinstance(digest, str):
            raise ValueError("artifact lineage id/hash must be strings")
        record = self.state.artifact_record(artifact_id)
        if record is None or record["object_hash"] != digest:
            raise ValueError("artifact lineage does not match the canonical registry")
        self.objects.get_bytes(digest)

    def _verify_linked_hashes(self, payload: dict[str, Any]) -> None:
        for part in self._payload_mappings(payload):
            stems = {"source", "decision_pack", "committee_protocol", "trading_classification"}
            stems.update(
                key.removesuffix("_artifact_id")
                for key in part
                if key.endswith("_artifact_id")
                and key.removesuffix("_artifact_id") + "_object_hash" in part
            )
            for stem in stems:
                artifact_id = part.get(f"{stem}_artifact_id")
                digest = part.get(f"{stem}_object_hash")
                if artifact_id is None and digest is None:
                    continue
                if not artifact_id or not digest:
                    raise ValueError("artifact lineage id/hash binding is incomplete")
                self._verify_reference(artifact_id, digest)
            ids = part.get("source_artifact_ids", [])
            hashes = part.get("source_object_hashes", [])
            if not isinstance(ids, (list, tuple)) or not isinstance(hashes, (list, tuple)):
                raise ValueError("source lineage collections must be explicit arrays")
            if any(not isinstance(value, str) or not value for value in (*ids, *hashes)):
                raise ValueError("source lineage collections contain invalid references")
            if len(ids) != len(set(ids)):
                raise ValueError("source lineage identifiers must not be duplicated")
            # Canonical models sort IDs and hashes independently; some additionally
            # bind source snapshots. Do not zip the two lists or invent a pairing.
            for artifact_id in ids:
                record = self.state.artifact_record(artifact_id)
                if record is None or record["object_hash"] not in hashes:
                    raise ValueError("source artifact is not bound to its declared lineage hashes")
            for digest in set(hashes):
                self.objects.get_bytes(digest)
            bindings = part.get("artifact_object_hashes", {})
            if not isinstance(bindings, dict):
                raise ValueError("artifact lineage hash bindings must be a mapping")
            for artifact_id, digest in bindings.items():
                self._verify_reference(artifact_id, digest)

    @staticmethod
    def _same_identity(left: str, right: str) -> bool:
        def split(value: str) -> tuple[str | None, str] | None:
            if len(value) == 6 and value.isascii() and value.isdigit():
                return None, value
            if value.count(":") == 1 and "." not in value:
                market, code = value.split(":")
            elif value.count(".") == 1 and ":" not in value:
                code, market = value.split(".")
            else:
                return None
            if market not in {"XSHG", "XSHE", "BJSE"}:
                return None
            if len(code) != 6 or not code.isascii() or not code.isdigit():
                return None
            return market, code

        left_parts, right_parts = split(left), split(right)
        if left_parts is None or right_parts is None:
            return False
        left_market, left_code = left_parts
        right_market, right_code = right_parts
        return left_code == right_code and (
            left_market is None or right_market is None or left_market == right_market
        )

    @staticmethod
    def _check_time(
        payload: dict[str, Any], cutoff: datetime, freshness_seconds: int | None = None
    ) -> None:
        now = utc_now()
        if cutoff.tzinfo is None or cutoff.utcoffset() is None or cutoff > now:
            raise ValueError("frozen decision time must be aware and not in the future")
        observed: datetime | None = None
        root_has_time = False
        fields = (
            "as_of",
            "data_as_of",
            "data_cutoff_at",
            "available_at",
            "available_to_system_at",
            "observed_at",
            "created_at",
            "completed_at",
        )
        for part in RegisteredOutputVerifier._payload_mappings(payload):
            for key in fields:
                raw = part.get(key)
                if raw is None:
                    continue
                timestamp = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
                if timestamp.tzinfo is None or timestamp.utcoffset() is None:
                    raise ValueError("output has an ambiguous timestamp")
                if timestamp > now or timestamp > cutoff:
                    raise ValueError("output was not available at the frozen decision time")
                if part is payload:
                    root_has_time = True
                    if key in {"observed_at", "as_of", "data_as_of"}:
                        observed = timestamp
        if not root_has_time:
            raise ValueError("output lacks a verifiable availability timestamp")
        # Forecast years, effective dates and expiry windows describe the model's
        # target, not when its input became available. They are intentionally not
        # interpreted as observed evidence timestamps.
        if freshness_seconds is not None and (
            observed is None
            or freshness_seconds < 0
            or (cutoff - observed).total_seconds() > freshness_seconds
        ):
            raise ValueError("output is stale under the capability freshness policy")

    def authenticated_preflight(self, preflight: InvestorSessionPreflightReceipt) -> bool:
        stored = self.store.get_preflight_for_request(preflight.request_id)
        fields = {"receipt_id", "receipt_hash", "built_from_cache"}
        body = preflight.model_dump(exclude=fields)
        # Preserve the existing receipt producer's nested typed serialization.
        # Flattening nested AwareDatetime fields changes Z/+00:00 representations.
        body["context"] = preflight.context
        body["regime"] = preflight.regime
        return (
            stored is not None
            and stored.model_dump(exclude={"built_from_cache"})
            == preflight.model_dump(exclude={"built_from_cache"})
            and content_hash(body) == preflight.receipt_hash
        )

    def _verify_local(
        self,
        capability_id: str,
        artifact_ids: tuple[str, ...],
        request: InvestorRequestEnvelope,
        preflight: InvestorSessionPreflightReceipt,
    ) -> bool:
        if capability_id == "REQUEST_TIME":
            expected = (f"request-time:{request.question_time.isoformat()}",)
        elif capability_id == "ENTITY_IDENTITY":
            expected = tuple(f"entity:{item}" for item in request.entity_ids) or ("entity:none",)
        elif capability_id == "SESSION_PREFLIGHT":
            if not self.authenticated_preflight(preflight):
                raise ValueError("preflight is not the persisted request receipt")
            expected = (preflight.receipt_id,)
        elif capability_id == "MARKET_REGIME":
            snapshot = self.store.latest_valid_regime(request.evidence_cutoff)
            if snapshot is None or snapshot.snapshot_id != preflight.regime.snapshot_id:
                raise ValueError("no valid persisted market regime for this request")
            expected = (snapshot.snapshot_id,)
        elif capability_id == "SUBJECT_REGISTRY":
            if not request.entity_ids:
                expected = ("subject-registry:no-entity",)
            else:
                # Primary-key reads are proportional to this request, not the
                # user's entire cross-session research history.
                instruments: set[str] = set()
                with self.store.connect() as connection:
                    for artifact_id in artifact_ids:
                        row = connection.execute(
                            "SELECT payload_json FROM research_subject_events WHERE event_id=?",
                            (artifact_id,),
                        ).fetchone()
                        if row is None:
                            raise ValueError("research subject event is unavailable")
                        event = ResearchSubjectEvent.model_validate_json(str(row[0]))
                        if (
                            event.event_id != artifact_id
                            or event.request_id != request.request_id
                            or event.available_at > request.evidence_cutoff
                        ):
                            raise ValueError("research subject receipt belongs to another request")
                        instruments.add(event.instrument_id)
                if instruments != set(request.entity_ids):
                    raise ValueError("research subject receipts do not cover the resolved entities")
                expected = artifact_ids
        elif capability_id == "RESPONSE_GATEWAY":
            # An obligation, not a claim that a not-yet-rendered answer passed.
            expected = ("gateway:required",)
        else:
            return False
        if artifact_ids != expected:
            raise ValueError("local capability output does not match its deterministic contract")
        return True

    def authenticated_coverage(self, request_id: str, receipt_id: str, receipt_hash: str) -> bool:
        with self.store.connect() as connection:
            row = connection.execute(
                "SELECT c.payload_json, p.payload_json FROM capability_coverage_receipts c "
                "JOIN capability_execution_plans p ON p.plan_id=c.plan_id WHERE c.receipt_id=?",
                (receipt_id,),
            ).fetchone()
        if row is None:
            return False
        receipt = CapabilityCoverageReceipt.model_validate_json(str(row[0]))
        plan = CapabilityExecutionPlan.model_validate_json(str(row[1]))
        if (
            receipt.request_id != request_id
            or plan.request_id != request_id
            or receipt.receipt_hash != receipt_hash
            or not receipt.outputs_verified
            or content_hash(receipt.model_dump(exclude={"receipt_id", "receipt_hash"}))
            != receipt_hash
            or content_hash(plan.model_dump(exclude={"plan_id", "plan_hash"})) != plan.plan_hash
        ):
            return False
        nodes = {node.capability_id: node for node in plan.nodes}
        records = {record.capability_id: record for record in receipt.records}
        if (
            len(nodes) != len(plan.nodes)
            or len(records) != len(receipt.records)
            or nodes.keys() != records.keys()
        ):
            return False
        required = {
            key for key, node in nodes.items() if node.requirement is CapabilityRequirement.REQUIRED
        }
        successful = {
            key
            for key, record in records.items()
            if record.status in {CapabilityRunStatus.COMPLETED, CapabilityRunStatus.REUSED}
        }
        if (
            not required
            or required - successful
            or not receipt.coverage_complete
            or receipt.required_capability_coverage != 1.0
            or receipt.prohibited_call_count != 0
            or receipt.unresolved_conflicts
        ):
            return False
        for key, node in nodes.items():
            record = records[key]
            if record.status in {CapabilityRunStatus.FAILED, CapabilityRunStatus.DEGRADED}:
                return False
            if node.requirement is CapabilityRequirement.PROHIBITED and record.status not in {
                CapabilityRunStatus.PROHIBITED,
                CapabilityRunStatus.SKIPPED,
            }:
                return False
            if key in successful and (
                not record.artifact_ids or any(not item.strip() for item in record.artifact_ids)
            ):
                return False
        for key in successful - _LOCAL_OUTPUTS:
            for artifact_id in records[key].artifact_ids:
                self.load(artifact_id, output_model(nodes[key].output_schema))
        return True
