"""Content-addressed validation proof cache for immutable research artifacts."""

from __future__ import annotations

from collections.abc import Callable

from astock.core.hashing import content_hash
from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.schemas.research_sla import ResearchValidationProof

ValidationCallback = Callable[[], None]


class ResearchValidationCache:
    """Persist successful validation without weakening invalidation semantics.

    The cache key binds the immutable artifact bytes, validator implementation,
    policy, identity scope and mutable fact-state fingerprint. A cache hit therefore
    skips repeated parsing/lineage work only when every correctness input is unchanged.
    Failed validation is never cached.
    """

    def __init__(self, state: StateStore, objects: ObjectStore) -> None:
        self.state = state
        self.objects = objects

    def verify_once(
        self,
        *,
        artifact_id: str,
        validator_hash: str,
        policy_hash: str,
        identity_scope: str,
        fact_state_hash: str,
        validate: ValidationCallback,
    ) -> tuple[ResearchValidationProof, bool]:
        record = self.state.artifact_record(artifact_id)
        if record is None:
            raise ValueError("validation cache requires a registered artifact")
        object_hash = str(record["object_hash"])
        self.objects.get_bytes(object_hash)
        cache_key = content_hash(
            {
                "artifact_id": artifact_id,
                "object_hash": object_hash,
                "validator_hash": validator_hash,
                "policy_hash": policy_hash,
                "identity_scope": identity_scope,
                "fact_state_hash": fact_state_hash,
            }
        )
        cached = self._load(cache_key)
        if cached is not None:
            return cached, True

        validate()
        proof = ResearchValidationProof(
            proof_id=f"research-validation-proof:{cache_key}",
            cache_key=cache_key,
            artifact_id=artifact_id,
            object_hash=object_hash,
            validator_hash=validator_hash,
            policy_hash=policy_hash,
            identity_scope=identity_scope,
            fact_state_hash=fact_state_hash,
        )
        ref = self.objects.put_json(proof.model_dump(mode="json"))
        with self.state.transaction() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO research_validation_proof_cache("
                "cache_key,artifact_id,object_hash,validator_hash,policy_hash,identity_scope,"
                "fact_state_hash,proof_object_hash,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    cache_key,
                    artifact_id,
                    object_hash,
                    validator_hash,
                    policy_hash,
                    identity_scope,
                    fact_state_hash,
                    ref.sha256,
                    proof.created_at.isoformat(),
                ),
            )
            row = connection.execute(
                "SELECT artifact_id,object_hash,validator_hash,policy_hash,identity_scope,"
                "fact_state_hash,proof_object_hash FROM research_validation_proof_cache "
                "WHERE cache_key=?",
                (cache_key,),
            ).fetchone()
        if row is None:
            raise RuntimeError("validation proof cache write did not persist")
        if (
            str(row["artifact_id"]) != artifact_id
            or str(row["object_hash"]) != object_hash
            or str(row["validator_hash"]) != validator_hash
            or str(row["policy_hash"]) != policy_hash
            or str(row["identity_scope"]) != identity_scope
            or str(row["fact_state_hash"]) != fact_state_hash
        ):
            raise ValueError("validation proof cache identity collision")
        stored = ResearchValidationProof.model_validate_json(
            self.objects.get_bytes(str(row["proof_object_hash"]))
        )
        if (
            stored.cache_key != cache_key
            or stored.artifact_id != artifact_id
            or stored.object_hash != object_hash
            or stored.validator_hash != validator_hash
            or stored.policy_hash != policy_hash
            or stored.identity_scope != identity_scope
            or stored.fact_state_hash != fact_state_hash
        ):
            raise ValueError("validation proof cache payload collision")
        # Concurrent validators may finish milliseconds apart.  The first writer's
        # created_at is canonical; later writers reuse that proof rather than treating
        # the timestamp difference as a semantic collision.
        return stored, False

    def _load(self, cache_key: str) -> ResearchValidationProof | None:
        with self.state.connect() as connection:
            row = connection.execute(
                "SELECT proof_object_hash FROM research_validation_proof_cache WHERE cache_key=?",
                (cache_key,),
            ).fetchone()
        if row is None:
            return None
        return ResearchValidationProof.model_validate_json(
            self.objects.get_bytes(str(row["proof_object_hash"]))
        )


__all__ = ["ResearchValidationCache"]
