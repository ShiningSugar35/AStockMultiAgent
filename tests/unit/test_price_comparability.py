"""Independent corporate-action comparability regressions for reference outcomes."""

from datetime import date
from pathlib import Path

from astock.research.debate_evaluation import load_quality_policy
from astock.research.outcome_feedback import summarize_feedback
from tests.unit.test_outcome_feedback import run

ROOT = Path(__file__).resolve().parents[2]


def test_unknown_corporate_actions_never_claim_direction_correctness() -> None:
    result = run(corporate_action_dates=None)
    assert result["status"] == "PRICE_ONLY_UNQUALIFIED"
    assert result["price_comparability"] == "UNKNOWN"
    assert result["direction_correct"] is None
    assert result["gross_price_return"]  # The descriptive price series remains visible.


def test_in_window_corporate_action_blocks_direction_hit_count() -> None:
    policy = load_quality_policy(ROOT / "configs/research_quality.yaml")
    unknown = run(corporate_action_dates=None)
    affected = run(corporate_action_dates={date(2026, 11, 4)})
    qualified = run(corporate_action_dates=set())
    assert affected["status"] == "PRICE_ONLY_UNQUALIFIED"
    assert affected["price_comparability"] == "ACTION_IN_WINDOW"
    assert affected["direction_correct"] is None
    assert qualified["status"] == "MATURE"
    assert qualified["price_comparability"] == "VERIFIED_NO_ACTION"
    assert qualified["direction_correct"] is True
    summary = summarize_feedback((unknown, affected, qualified), policy, context_id="sample")
    assert summary["mature_sample_count"] == 1
    assert summary["direction_correct_count"] == 1


def test_non_window_actions_do_not_invalidate_full_verified_coverage() -> None:
    result = run(corporate_action_dates={date(2026, 10, 30)})
    assert result["status"] == "MATURE"
    assert result["direction_correct"] is True
