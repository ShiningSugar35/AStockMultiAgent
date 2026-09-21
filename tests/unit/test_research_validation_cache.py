from __future__ import annotations

import threading
from pathlib import Path

import pytest

from astock.core.hashing import content_hash
from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.research.validation_cache import ResearchValidationCache

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _service(tmp_path: Path) -> tuple[StateStore, ObjectStore, ResearchValidationCache]:
    state = StateStore(tmp_path / "state.sqlite", PROJECT_ROOT / "migrations")
    state.migrate()
    objects = ObjectStore(tmp_path / "objects")
    payload = {"schema_version": "dummy-v1", "value": 1}
    ref = objects.put_json(payload)
    state.register_artifact(
        artifact_id="Dummy:1",
        artifact_type="Dummy",
        schema_version="dummy-v1",
        object_hash=ref.sha256,
        input_hashes=[],
    )
    return state, objects, ResearchValidationCache(state, objects)


def _hash(label: str) -> str:
    return content_hash({"label": label})


def test_validation_cache_reuses_only_exact_context(tmp_path: Path) -> None:
    _state, _objects, cache = _service(tmp_path)
    calls = 0

    def validate() -> None:
        nonlocal calls
        calls += 1

    first, first_hit = cache.verify_once(
        artifact_id="Dummy:1",
        validator_hash=_hash("validator-v1"),
        policy_hash=_hash("policy-v1"),
        identity_scope="company:002155",
        fact_state_hash=_hash("facts-v1"),
        validate=validate,
    )
    second, second_hit = cache.verify_once(
        artifact_id="Dummy:1",
        validator_hash=_hash("validator-v1"),
        policy_hash=_hash("policy-v1"),
        identity_scope="company:002155",
        fact_state_hash=_hash("facts-v1"),
        validate=validate,
    )
    assert calls == 1
    assert not first_hit and second_hit
    assert first == second

    _proof, changed_hit = cache.verify_once(
        artifact_id="Dummy:1",
        validator_hash=_hash("validator-v1"),
        policy_hash=_hash("policy-v1"),
        identity_scope="company:002155",
        fact_state_hash=_hash("facts-v2"),
        validate=validate,
    )
    assert not changed_hit
    assert calls == 2


def test_validation_cache_never_persists_failure(tmp_path: Path) -> None:
    _state, _objects, cache = _service(tmp_path)
    attempts = 0

    def fail() -> None:
        nonlocal attempts
        attempts += 1
        raise ValueError("bad artifact")

    for _ in range(2):
        with pytest.raises(ValueError, match="bad artifact"):
            cache.verify_once(
                artifact_id="Dummy:1",
                validator_hash=_hash("validator-v1"),
                policy_hash=_hash("policy-v1"),
                identity_scope="company:002155",
                fact_state_hash=_hash("facts-v1"),
                validate=fail,
            )
    assert attempts == 2


def test_validation_cache_concurrent_writers_share_one_canonical_proof(tmp_path: Path) -> None:
    _state, _objects, cache = _service(tmp_path)
    barrier = threading.Barrier(2)
    results = []
    errors = []

    def worker() -> None:
        try:
            def validate() -> None:
                barrier.wait(timeout=2)

            results.append(
                cache.verify_once(
                    artifact_id="Dummy:1",
                    validator_hash=_hash("validator-v1"),
                    policy_hash=_hash("policy-v1"),
                    identity_scope="company:002155",
                    fact_state_hash=_hash("facts-v1"),
                    validate=validate,
                )[0]
            )
        except Exception as exc:  # noqa: BLE001 - test captures concurrent failure
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)
    assert not errors
    assert len(results) == 2
    assert results[0] == results[1]
    with _state.connect() as connection:
        count = connection.execute(
            "SELECT COUNT(*) FROM research_validation_proof_cache"
        ).fetchone()[0]
    assert count == 1
