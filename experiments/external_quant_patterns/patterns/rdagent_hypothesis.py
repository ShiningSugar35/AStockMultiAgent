"""RD-Agent hypothesis iteration loop benchmark.

Simulates RD-Agent's factor hypothesis → implementation → backtest → feedback
cycle and compares it against AStockMultiAgent's prospective/shadow framework.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256

from experiments.external_quant_patterns.fixtures.sample_data import (
    MockHypothesis,
    MockHypothesisResult,
)


@dataclass(frozen=True)
class HypothesisIteration:
    """Single iteration in an RD-Agent-style hypothesis loop."""
    iteration: int
    hypothesis_id: str
    description: str
    factor_code: str
    expected_ic: float
    actual_ic: float
    passed: bool
    feedback: str
    improvement_over_parent: float | None
    object_hash: str


@dataclass(frozen=True)
class HypothesisLoopReport:
    """Report from an RD-Agent-style hypothesis iteration loop."""
    iterations: tuple[HypothesisIteration, ...]
    total_iterations: int
    passed_count: int
    best_ic: float
    convergence_improvement: float  # best - first
    loop_completed: bool
    object_hash: str


@dataclass(frozen=True)
class ShadowProspectiveEntry:
    """AStockMultiAgent's prospective/shadow equivalent."""
    study_id: str
    trial_id: str
    hypothesis: str
    pre_registered_endpoint: str
    status: str  # "COLLECTING" | "EVALUATED"
    formal_event_count: int
    independence_sample_floor: int
    object_hash: str


@dataclass(frozen=True)
class LoopComparisonResult:
    """Quantitative comparison of hypothesis iteration approaches."""
    rdagent_iterations: int
    astock_prospective_entries: int
    rdagent_convergence: float
    astock_formal_events: int
    overlap_description: str
    marginal_value: str
    recommendation: str


def _hash_content(content: str) -> str:
    return sha256(content.encode("utf-8")).hexdigest()


def build_hypothesis_iterations(
    hypotheses: list[MockHypothesis],
    results: list[MockHypothesisResult],
) -> list[HypothesisIteration]:
    """Build RD-Agent-style hypothesis iterations from fixtures."""
    result_map = {r.hypothesis_id: r for r in results}
    hyp_map = {h.hypothesis_id: h for h in hypotheses}
    iterations: list[HypothesisIteration] = []
    for hyp in hypotheses:
        res = result_map[hyp.hypothesis_id]
        parent_ic = None
        if hyp.parent_id and hyp.parent_id in hyp_map:
            parent_res = result_map.get(hyp.parent_id)
            if parent_res:
                parent_ic = parent_res.actual_ic
        improvement = res.actual_ic - parent_ic if parent_ic is not None else None
        iterations.append(HypothesisIteration(
            iteration=hyp.iteration,
            hypothesis_id=hyp.hypothesis_id,
            description=hyp.description,
            factor_code=hyp.factor_code,
            expected_ic=hyp.expected_ic,
            actual_ic=res.actual_ic,
            passed=res.passed,
            feedback=res.feedback,
            improvement_over_parent=improvement,
            object_hash=_hash_content(f"iter-{hyp.hypothesis_id}"),
        ))
    return iterations


def build_hypothesis_loop_report(
    iterations: list[HypothesisIteration],
) -> HypothesisLoopReport:
    """Aggregate hypothesis iterations into a loop report."""
    passed = [i for i in iterations if i.passed]
    best_ic = max((i.actual_ic for i in iterations), default=0.0)
    first_ic = iterations[0].actual_ic if iterations else 0.0
    return HypothesisLoopReport(
        iterations=tuple(iterations),
        total_iterations=len(iterations),
        passed_count=len(passed),
        best_ic=best_ic,
        convergence_improvement=best_ic - first_ic,
        loop_completed=len(passed) >= 2,
        object_hash=_hash_content(f"loop-{len(iterations)}"),
    )


def build_shadow_prospective_entries() -> list[ShadowProspectiveEntry]:
    """Build AStockMultiAgent-style prospective entries."""
    return [
        ShadowProspectiveEntry(
            study_id=_hash_content("shadow-study-1"),
            trial_id=_hash_content("trial-h1"),
            hypothesis="Momentum 20d has predictive power",
            pre_registered_endpoint="20d_sector_adjusted_return",
            status="COLLECTING",
            formal_event_count=0,
            independence_sample_floor=100,
            object_hash=_hash_content("shadow-1"),
        ),
        ShadowProspectiveEntry(
            study_id=_hash_content("shadow-study-1"),
            trial_id=_hash_content("trial-h2"),
            hypothesis="Volume-price divergence is alpha-positive",
            pre_registered_endpoint="20d_sector_adjusted_return",
            status="COLLECTING",
            formal_event_count=0,
            independence_sample_floor=100,
            object_hash=_hash_content("shadow-2"),
        ),
    ]


def compare_hypothesis_loops(
    hypotheses: list[MockHypothesis],
    results: list[MockHypothesisResult],
) -> LoopComparisonResult:
    """Compare the modeled hypothesis loop with AStock prospective governance."""
    iterations = build_hypothesis_iterations(hypotheses, results)
    loop_report = build_hypothesis_loop_report(iterations)
    shadow_entries = build_shadow_prospective_entries()

    return LoopComparisonResult(
        rdagent_iterations=loop_report.total_iterations,
        astock_prospective_entries=len(shadow_entries),
        rdagent_convergence=loop_report.convergence_improvement,
        astock_formal_events=sum(e.formal_event_count for e in shadow_entries),
        overlap_description=(
            "Both track hypothesis→evaluation cycles. RD-Agent does rapid automated iteration "
            "with LLM-generated factors; AStock uses prospective governance with formal "
            "pre-registration, independence requirements, and 100-event threshold."
        ),
        marginal_value=(
            "The modeled rapid-iteration pattern may help hypothesis discovery, but the "
            "synthetic fixture cannot establish RD-Agent runtime performance. Any future "
            "automation must remain behind AStock's prospective and model-risk admission "
            "gates to control overfitting."
        ),
        recommendation="SHADOW_EXPERIMENT",
    )
