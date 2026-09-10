"""One immutable current-decision clock bound to a question and captured inputs.

The original question and its first preflight are never rewritten. The child
request preserves civil-date interpretation while freezing its evidence cutoff
only after registered inputs have been observed and checked. This boundary
admits read-only research inputs, not economic operations or research quality.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from datetime import datetime
from typing import Literal

from pydantic import Field

from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.investor_orchestration.models import (
    InvestorRequestEnvelope,
    SideEffectClass,
    StrictModel,
)
from astock.investor_orchestration.store import InvestorOrchestrationStore
from astock.investor_orchestration.utils import content_hash, utc_now


class DecisionInputBinding(StrictModel):
    artifact_id: str = Field(min_length=1)
    artifact_type: str = Field(min_length=1)
    object_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class InvestorDecisionFreeze(StrictModel):
    schema_version: Literal["investor-decision-freeze-v1"] = "investor-decision-freeze-v1"
    original_request: InvestorRequestEnvelope
    decision_request: InvestorRequestEnvelope
    input_bindings: tuple[DecisionInputBinding, ...] = Field(min_length=1, max_length=256)
    capability_bindings: dict[str, tuple[str, ...]] = Field(default_factory=dict)
    frozen_at: datetime

    @property
    def source_request(self) -> InvestorRequestEnvelope:
        return self.original_request

    @property
    def execution_request(self) -> InvestorRequestEnvelope:
        return self.decision_request

    @property
    def decision_as_of(self) -> datetime:
        return self.frozen_at

    @property
    def boundary_id(self) -> str:
        identity = self.decision_request.decision_freeze_artifact_id
        if not identity:
            raise ValueError("decision freeze lacks its registered boundary identity")
        return identity


class DecisionFreezeService:
    def __init__(
        self, store: InvestorOrchestrationStore, objects: ObjectStore | None = None
    ) -> None:
        self.store = store
        self.state = StateStore(store.path)
        self.objects = objects or ObjectStore(store.path.parent / "objects" / "sha256")

    @staticmethod
    def _freeze_id(request_id: str) -> str:
        # The request ID, not mutable request text, owns the unique freeze slot.
        return f"InvestorDecisionFreeze:{content_hash({'request_id': request_id})}"

    @staticmethod
    def _validate_parent(original: InvestorRequestEnvelope) -> None:
        from astock.investor_orchestration.capabilities import validate_request_permissions

        if original.research_mode != "CURRENT":
            raise ValueError("only CURRENT requests can freeze post-question acquisition")
        if original.decision_time is not None:
            raise ValueError("a frozen decision request cannot be silently refrozen")
        if original.side_effect not in {
            SideEffectClass.READ,
            SideEffectClass.META,
            SideEffectClass.NONE,
        }:
            raise ValueError(
                "current input freeze is read-only and cannot authorize economic writes"
            )
        validate_request_permissions(original)
        if original.question_time > utc_now():
            raise ValueError("a current question cannot have a future timestamp")

    @staticmethod
    def _capabilities(
        bindings: Mapping[str, tuple[str, ...]] | None, artifact_ids: tuple[str, ...]
    ) -> dict[str, tuple[str, ...]]:
        if bindings is None:
            return {}
        from astock.investor_orchestration.capabilities import _BASE_NODES
        from astock.investor_orchestration.output_validation import _LOCAL_OUTPUTS

        if not bindings or set(bindings) & _LOCAL_OUTPUTS:
            raise ValueError("input bindings cannot replace built-in safety boundaries")
        if set(bindings) - _BASE_NODES.keys():
            raise ValueError("unknown capability input binding")
        selected: dict[str, tuple[str, ...]] = {}
        for capability, values in sorted(bindings.items()):
            if isinstance(values, str) or not values or len(values) > 256:
                raise ValueError("capability inputs must be non-empty bounded collections")
            if any(not isinstance(item, str) or not item.strip() for item in values):
                raise ValueError("capability inputs contain invalid artifact identities")
            if len(set(values)) != len(values):
                raise ValueError("duplicate capability inputs are not independent evidence")
            selected[capability] = tuple(sorted(values))
        if {item for values in selected.values() for item in values} != set(artifact_ids):
            raise ValueError("capability bindings do not equal the frozen artifact input set")
        return selected

    def _load(self, artifact_id: str) -> InvestorDecisionFreeze | None:
        record = self.state.artifact_record(artifact_id)
        if record is None:
            return None
        if record["type"] != "InvestorDecisionFreeze":
            raise ValueError("decision freeze identity refers to another artifact type")
        frozen = InvestorDecisionFreeze.model_validate_json(
            self.objects.get_bytes(str(record["object_hash"]))
        )
        if record["schema_version"] != frozen.schema_version:
            raise ValueError("decision freeze schema does not match its registry record")
        expected_inputs = sorted(
            {
                content_hash(frozen.original_request),
                *(binding.object_hash for binding in frozen.input_bindings),
            }
        )
        if record["input_hashes"] != expected_inputs or frozen.boundary_id != artifact_id:
            raise ValueError("decision freeze input lineage or boundary identity was changed")
        return frozen

    def _bindings(
        self, artifact_ids: tuple[str, ...], *, cutoff: datetime | None = None
    ) -> tuple[DecisionInputBinding, ...]:
        from astock.investor_orchestration.output_validation import (
            RegisteredOutputVerifier,
            output_model,
        )

        if (
            not artifact_ids
            or len(artifact_ids) > 256
            or len(set(artifact_ids)) != len(artifact_ids)
        ):
            raise ValueError("decision freeze requires a bounded unique set of captured artifacts")
        verifier = RegisteredOutputVerifier(self.store, self.objects)
        observed = cutoff or utc_now()
        bindings = []
        for artifact_id in sorted(artifact_ids):
            record = self.state.artifact_record(artifact_id)
            if record is None:
                raise ValueError("decision input is not registered or is unavailable")
            model = output_model(str(record["type"]))
            value = verifier.load(artifact_id, model)
            verifier._check_time(value.model_dump(mode="json"), observed)
            registered_at = datetime.fromisoformat(str(record["created_at"]).replace("Z", "+00:00"))
            if registered_at.tzinfo is None or registered_at > observed:
                raise ValueError("decision input registration has an invalid availability time")
            bindings.append(
                DecisionInputBinding(
                    artifact_id=artifact_id,
                    artifact_type=str(record["type"]),
                    object_hash=str(record["object_hash"]),
                )
            )
        return tuple(bindings)

    def freeze(
        self,
        original: InvestorRequestEnvelope,
        *,
        artifact_ids: tuple[str, ...],
        capability_bindings: Mapping[str, tuple[str, ...]] | None = None,
    ) -> InvestorRequestEnvelope:
        original = InvestorRequestEnvelope.model_validate(original.model_dump())
        self._validate_parent(original)
        selected = self._capabilities(capability_bindings, artifact_ids)
        bindings = self._bindings(artifact_ids)
        identity = content_hash(original)
        freeze_id = self._freeze_id(original.request_id)
        existing = self._load(freeze_id)
        if existing is not None:
            return self._reuse(existing, original, bindings, selected)
        frozen_at = utc_now()
        values = original.model_dump()
        values.update(
            {
                "request_id": f"decision-{uuid.uuid5(uuid.NAMESPACE_URL, identity)}",
                "parent_request_id": original.request_id,
                "decision_time": frozen_at,
                "decision_freeze_artifact_id": freeze_id,
                "idempotency_key": f"decision:{identity}",
            }
        )
        decision = InvestorRequestEnvelope.model_validate(values)
        receipt = InvestorDecisionFreeze(
            original_request=original,
            decision_request=decision,
            input_bindings=bindings,
            capability_bindings=selected,
            frozen_at=frozen_at,
        )
        reference = self.objects.put_json(receipt.model_dump(mode="json"))
        try:
            self.state.register_artifact(
                artifact_id=freeze_id,
                artifact_type="InvestorDecisionFreeze",
                schema_version=receipt.schema_version,
                object_hash=reference.sha256,
                input_hashes=sorted({identity, *(binding.object_hash for binding in bindings)}),
            )
        except ValueError:
            # Another identical freeze may win the atomic unique registry insert.
            # Return its actual clock, not the losing unregistered candidate.
            existing = self._load(freeze_id)
            if existing is None:
                raise
            return self._reuse(existing, original, bindings, selected)
        return decision

    def _reuse(
        self,
        existing: InvestorDecisionFreeze,
        original: InvestorRequestEnvelope,
        bindings: tuple[DecisionInputBinding, ...],
        capabilities: dict[str, tuple[str, ...]],
    ) -> InvestorRequestEnvelope:
        if (
            existing.original_request != original
            or existing.input_bindings != bindings
            or existing.capability_bindings != capabilities
        ):
            raise ValueError("decision freeze identity is already bound to different inputs")
        self.verify(existing.decision_request)
        return existing.decision_request

    def get(self, boundary_id: str) -> InvestorDecisionFreeze:
        frozen = self._load(boundary_id)
        if frozen is None:
            raise ValueError("registered decision freeze is unavailable")
        self.verify(frozen.decision_request)
        return frozen

    def verify(self, request: InvestorRequestEnvelope) -> None:
        request = InvestorRequestEnvelope.model_validate(request.model_dump())
        if request.decision_time is None:
            return
        freeze_id = request.decision_freeze_artifact_id
        frozen = self._load(freeze_id) if freeze_id else None
        if frozen is None or frozen.decision_request != request:
            raise ValueError("request does not match its registered decision freeze")
        parent = frozen.original_request
        self._validate_parent(parent)
        expected_values = parent.model_dump()
        identity = content_hash(parent)
        expected_values.update(
            {
                "request_id": f"decision-{uuid.uuid5(uuid.NAMESPACE_URL, identity)}",
                "parent_request_id": parent.request_id,
                "decision_time": frozen.frozen_at,
                "decision_freeze_artifact_id": self._freeze_id(parent.request_id),
                "idempotency_key": f"decision:{identity}",
            }
        )
        if request.model_dump() != expected_values:
            raise ValueError("decision freeze changed the original request semantics")
        if (
            frozen.frozen_at.tzinfo is None
            or frozen.frozen_at > utc_now()
            or frozen.frozen_at < parent.question_time
        ):
            raise ValueError("decision freeze has an invalid timestamp")
        ids = tuple(binding.artifact_id for binding in frozen.input_bindings)
        if self._bindings(ids, cutoff=frozen.frozen_at) != frozen.input_bindings:
            raise ValueError("decision freeze inputs no longer match their registered lineage")
        if frozen.capability_bindings:
            expected = self._capabilities(frozen.capability_bindings, ids)
            if expected != frozen.capability_bindings:
                raise ValueError("decision freeze capability binding is not canonical")
