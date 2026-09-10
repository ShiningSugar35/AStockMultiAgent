"""Benchmark admission and real isolated fixtures; not the 68 research scenarios."""

from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from astock.investor_orchestration.preflight import InvestorSessionPreflightService
from scripts.benchmark_investor_preflight import (
    Workload,
    _request,
    create_run_directory,
    economic_digest,
    qualification_failures,
    run_profile,
    sample_stats,
    setup_fixture,
    verify_receipt,
)


def _valid_evidence() -> tuple[dict[str, dict[str, Any]], dict[str, bool]]:
    phases = {
        name: sample_stats([0.1] * 100)
        for name in (
            "cold",
            "warm",
            "concurrent",
            "after_revision",
        )
    }
    checks: dict[str, bool] = dict.fromkeys(
        (
            "economic_tables_unchanged",
            "source_tree_stable",
            "canonical_outputs_verified",
            "revision_invalidated",
            "lane_isolation",
            "receipt_persistence_verified",
        ),
        True,
    )
    return phases, checks


def test_nearest_rank_percentiles_are_individual_not_batch_seconds() -> None:
    stats = sample_stats([number / 100 for number in range(1, 101)])
    assert stats["count"] == 100
    assert stats["p50_seconds"] == 0.5
    assert stats["p95_seconds"] == 0.95
    assert stats["max_seconds"] == 1.0
    assert stats["sum_request_seconds"] > 50
    phases, checks = _valid_evidence()
    phases["warm"]["wall_seconds"] = 999
    assert qualification_failures(phases, checks) == []


@pytest.mark.parametrize("samples", [[], [float("nan")], [float("inf")], [-0.001]])
def test_invalid_timing_samples_cannot_certify_performance(samples: list[float]) -> None:
    with pytest.raises(ValueError, match="finite"):
        sample_stats(samples)


def test_report_summary_is_recomputed_instead_of_trusting_reported_p95() -> None:
    phases, checks = _valid_evidence()
    phases["warm"]["samples_seconds"] = [3.0] * 100
    failures = qualification_failures(phases, checks)
    assert "P95_EXCEEDED:warm" in failures
    assert "TIMING_SUMMARY_MISMATCH:warm" in failures


def test_empty_phase_missing_safety_check_and_short_smoke_are_not_pass() -> None:
    phases, checks = _valid_evidence()
    del phases["cold"]
    del checks["economic_tables_unchanged"]
    phases["warm"] = sample_stats([0.01] * 3)
    failures = qualification_failures(phases, checks)
    assert "PHASE_MISSING:cold" in failures
    assert "CHECK_MISSING:economic_tables_unchanged" in failures
    assert "INSUFFICIENT_SAMPLES:warm" in failures


@pytest.mark.parametrize(
    "check", ["economic_tables_unchanged", "source_tree_stable", "lane_isolation"]
)
def test_fast_but_invalid_state_fails_qualification(check: str) -> None:
    phases, checks = _valid_evidence()
    checks[check] = False
    assert f"CHECK_FAILED:{check}" in qualification_failures(phases, checks)


@pytest.mark.parametrize("suffix", ["runtime", "user_state", ".ai-bridge/../../outside"])
def test_output_never_targets_production_or_parent_directories(tmp_path: Path, suffix: str) -> None:
    with pytest.raises(ValueError, match="inside the project"):
        create_run_directory(tmp_path / suffix, root=tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_each_run_has_a_fresh_directory_and_preserves_existing_files(tmp_path: Path) -> None:
    base = tmp_path / ".ai-bridge/benchmarks"
    first = create_run_directory(base, root=tmp_path)
    sentinel = first / "state.sqlite"
    sentinel.write_bytes(b"never overwrite existing state")
    second = create_run_directory(base, root=tmp_path)
    assert first != second
    assert sentinel.read_bytes() == b"never overwrite existing state"
    assert list(second.iterdir()) == []


@pytest.mark.parametrize(
    "values",
    [
        ("bad/child", 1, 1, 1, 1),
        ("oversize", 17, 1, 1, 1),
        ("empty", 1, 2, 1, 1),
        ("empty", 1, 1, 1, 0),
    ],
)
def test_fixture_budget_and_path_names_are_bounded(values: tuple[str, int, int, int, int]) -> None:
    with pytest.raises(ValueError):
        Workload(*values)


def test_real_multi_account_read_benchmark_preserves_both_lanes(tmp_path: Path) -> None:
    result = run_profile(
        tmp_path,
        Workload("smoke", 2, 2, 4, 2),
        samples=4,
        cold_samples=2,
        workers=2,
    )
    assert all(result["checks"].values())
    assert result["expected_receipts"] == result["observed_receipts"] == 14
    assert result["economic_before"] == result["economic_after_reads"]
    assert result["economic_after_intentional_fixture_mutation"] == result["economic_final"]
    assert result["economic_before"] != result["economic_final"]
    assert result["economic_before"]["external_account_event"]["row_count"] == 9
    assert result["economic_before"]["paper_account"]["row_count"] == 2
    assert result["phases"]["cold"]["cache_hits"] == 0
    assert result["phases"]["warm"]["cache_hits"] == 4
    assert result["phases"]["concurrent"]["cache_hits"] == 3
    assert result["phases"]["after_revision"]["cache_hits"] == 0
    result["checks"]["source_tree_stable"] = True
    assert set(qualification_failures(result["phases"], result["checks"])) == {
        "INSUFFICIENT_SAMPLES:warm",
        "INSUFFICIENT_SAMPLES:concurrent",
    }


def test_faster_incomplete_or_tampered_cash_projection_is_rejected(tmp_path: Path) -> None:
    fixture = setup_fixture(tmp_path / "fixture", Workload("tiny", 1, 1, 2, 1))
    service = InvestorSessionPreflightService(fixture.metadata)
    receipt = service.build(_request("unmodified", datetime.now(UTC)))
    verify_receipt(receipt, fixture)
    before = economic_digest(fixture.state)
    bad = receipt.model_copy(deep=True)
    bad.context.paper.cash_by_account["paper-0"] = Decimal(0)
    with pytest.raises(ValueError, match="paper_cash"):
        verify_receipt(bad, fixture)
    missing = receipt.model_copy(deep=True)
    object.__setattr__(missing.context.paper, "open_orders", ())
    with pytest.raises(ValueError, match="orders"):
        verify_receipt(missing, fixture)
    no_settlement = receipt.model_copy(deep=True)
    object.__setattr__(no_settlement.context.paper, "pending_settlements", ())
    with pytest.raises(ValueError, match="pending_settlements"):
        verify_receipt(no_settlement, fixture)
    unconfirmed = receipt.model_copy(deep=True)
    object.__setattr__(unconfirmed.context.paper.open_orders[0], "confirmed", False)
    with pytest.raises(ValueError, match="confirmed_orders"):
        verify_receipt(unconfirmed, fixture)
    assert economic_digest(fixture.state) == before
    assert deepcopy(receipt.context.paper.cash_by_account) == fixture.paper_cash
