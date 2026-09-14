from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from astock.investor_orchestration.full_research_assembly import FullResearchReceiptAssembler
from astock.investor_orchestration.store import InvestorOrchestrationStore


def _registered_sources(
    assembler: FullResearchReceiptAssembler,
) -> tuple[str, str]:
    ref_a = assembler.objects.put_json({"source": "a"})
    ref_z = assembler.objects.put_json({"source": "z"})
    assembler.state.register_artifact(
        artifact_id="a-artifact",
        artifact_type="TestSource",
        schema_version="test-source-v1",
        object_hash=ref_a.sha256,
        input_hashes=[],
    )
    assembler.state.register_artifact(
        artifact_id="z-artifact",
        artifact_type="TestSource",
        schema_version="test-source-v1",
        object_hash=ref_z.sha256,
        input_hashes=[],
    )
    return ref_a.sha256, ref_z.sha256


def test_valuation_source_binding_map_recovers_legacy_unordered_hashes(
    tmp_path: Path,
) -> None:
    store = InvestorOrchestrationStore(tmp_path / "legacy-source-binding" / "state.sqlite")
    store.initialize()
    assembler = FullResearchReceiptAssembler(store)
    hash_a, hash_z = _registered_sources(assembler)

    bindings = assembler._source_binding_map(
        ("a-artifact", "z-artifact"),
        (hash_z, hash_a),
        fallback_artifact_id="fallback",
        fallback_object_hash="0" * 64,
    )

    assert bindings == {"a-artifact": hash_a, "z-artifact": hash_z}


def test_valuation_source_binding_map_rejects_noncanonical_hash_set(
    tmp_path: Path,
) -> None:
    store = InvestorOrchestrationStore(tmp_path / "mismatched-source-binding" / "state.sqlite")
    store.initialize()
    assembler = FullResearchReceiptAssembler(store)
    hash_a, _hash_z = _registered_sources(assembler)

    with pytest.raises(ValueError, match="differs from canonical registry"):
        assembler._source_binding_map(
            ("a-artifact", "z-artifact"),
            (hash_a, "0" * 64),
            fallback_artifact_id="fallback",
            fallback_object_hash="0" * 64,
        )


def test_valuation_source_binding_map_verifies_fallback_artifact(
    tmp_path: Path,
) -> None:
    store = InvestorOrchestrationStore(tmp_path / "fallback-source-binding" / "state.sqlite")
    store.initialize()
    assembler = FullResearchReceiptAssembler(store)
    fallback = assembler.objects.put_json({"source": "fallback"})
    assembler.state.register_artifact(
        artifact_id="fallback",
        artifact_type="TestSource",
        schema_version="test-source-v1",
        object_hash=fallback.sha256,
        input_hashes=[],
    )

    assert assembler._source_binding_map(
        (),
        (),
        fallback_artifact_id="fallback",
        fallback_object_hash=fallback.sha256,
    ) == {"fallback": fallback.sha256}


def test_full_research_assembler_project_root_is_independent_of_state_db_path(
    tmp_path: Path,
) -> None:
    assembler = FullResearchReceiptAssembler(
        InvestorOrchestrationStore(tmp_path / "isolated-state" / "state.sqlite")
    )

    assert assembler.project_root == Path(__file__).resolve().parents[2]


def test_daily_release_resolution_uses_registered_market_scope_not_code_prefix(
    tmp_path: Path,
    monkeypatch,
) -> None:
    assembler = FullResearchReceiptAssembler(
        InvestorOrchestrationStore(tmp_path / "market-scope" / "state.sqlite")
    )
    calls: list[str] = []

    def fake_release(dataset_kind: str, scope: str, *, as_of: datetime):
        calls.append(scope)
        if scope == "BJSE:430001":
            return {"release_id": "bjse-release"}
        return None

    monkeypatch.setattr(assembler.state, "get_market_reference_release", fake_release)

    release = assembler._current_daily_release(
        "430001", datetime(2026, 9, 13, 12, 0, tzinfo=UTC)
    )

    assert release == {"release_id": "bjse-release"}
    assert calls == ["XSHG:430001", "XSHE:430001", "BJSE:430001"]
