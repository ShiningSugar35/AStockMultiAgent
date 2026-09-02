"""Qlib Recorder pattern benchmark.

Simulates Qlib's experiment/run/metrics/artifacts recording approach
and compares it against AStockMultiAgent's existing patterns.
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from typing import Any

from experiments.external_quant_patterns.fixtures.sample_data import MockRecorderRun


@dataclass(frozen=True)
class QlibRecorderEntry:
    """Single recorded entry following Qlib's Recorder semantics."""
    entry_id: str
    entry_type: str  # "experiment" | "run" | "metric" | "artifact"
    parent_id: str | None
    params: dict[str, Any]
    metrics: dict[str, float]
    artifact_hashes: dict[str, str]
    object_hash: str


@dataclass(frozen=True)
class QlibRecorderReport:
    """Aggregated report from Qlib-style recording."""
    entries: tuple[QlibRecorderEntry, ...]
    total_runs: int
    unique_params: int
    unique_artifacts: int
    object_hash: str


@dataclass(frozen=True)
class AStockObservationEntry:
    """AStockMultiAgent's agent-observation-register equivalent."""
    observation_id: str
    eligible_skills: tuple[str, ...]
    selected_skill: str | None
    completed: bool
    duration_ms: int
    wall_time_ms: int
    provider_call_count: int
    cache_hit_count: int
    object_hash: str


@dataclass(frozen=True)
class PatternComparisonResult:
    """Quantitative comparison of two recording approaches."""
    qlib_entry_count: int
    astock_entry_count: int
    unique_param_count: int
    unique_artifact_count: int
    qlib_granularity: str  # "experiment>run>metric>artifact"
    astock_granularity: str  # "observation>checkpoint>performance"
    overlap_description: str
    marginal_value: str
    recommendation: str  # "ADOPT" | "ADAPT" | "REJECT" | "WATCH"


def _hash_content(content: str) -> str:
    return sha256(content.encode("utf-8")).hexdigest()


def build_qlib_recorder_entries(
    runs: list[MockRecorderRun],
) -> list[QlibRecorderEntry]:
    """Build Qlib-style recorder entries from mock runs."""
    entries: list[QlibRecorderEntry] = []
    for run in runs:
        # Experiment entry
        exp_id = _hash_content(f"exp-{run.run_id}")
        entries.append(QlibRecorderEntry(
            entry_id=exp_id,
            entry_type="experiment",
            parent_id=None,
            params=run.params,
            metrics={},
            artifact_hashes={},
            object_hash=_hash_content(f"exp-obj-{run.run_id}"),
        ))
        # Run entry
        run_entry_id = _hash_content(f"run-entry-{run.run_id}")
        entries.append(QlibRecorderEntry(
            entry_id=run_entry_id,
            entry_type="run",
            parent_id=exp_id,
            params=run.params,
            metrics=run.metrics,
            artifact_hashes={},
            object_hash=_hash_content(f"run-obj-{run.run_id}"),
        ))
        # Metric entries
        for metric_name, metric_value in run.metrics.items():
            metric_id = _hash_content(f"metric-{run.run_id}-{metric_name}")
            entries.append(QlibRecorderEntry(
                entry_id=metric_id,
                entry_type="metric",
                parent_id=run_entry_id,
                params={metric_name: metric_value},
                metrics={metric_name: metric_value},
                artifact_hashes={},
                object_hash=_hash_content(f"metric-obj-{run.run_id}-{metric_name}"),
            ))
        # Artifact entries
        for art_name, art_hash in run.artifact_hashes.items():
            art_id = _hash_content(f"artifact-{run.run_id}-{art_name}")
            entries.append(QlibRecorderEntry(
                entry_id=art_id,
                entry_type="artifact",
                parent_id=run_entry_id,
                params={},
                metrics={},
                artifact_hashes={art_name: art_hash},
                object_hash=art_hash,
            ))
    return entries


def build_astock_observations() -> list[AStockObservationEntry]:
    """Build AStockMultiAgent-style observation entries."""
    skills = ["candidate-scan", "company-deep-research", "portfolio-manager"]
    observations: list[AStockObservationEntry] = []
    for i, skill in enumerate(skills):
        obs_id = _hash_content(f"obs-{skill}")
        observations.append(AStockObservationEntry(
            observation_id=obs_id,
            eligible_skills=tuple(skills),
            selected_skill=skill,
            completed=True,
            duration_ms=1000 + i * 500,
            wall_time_ms=1200 + i * 600,
            provider_call_count=2 + i,
            cache_hit_count=1,
            object_hash=_hash_content(f"obs-obj-{skill}"),
        ))
    return observations


def compare_recorder_patterns(
    runs: list[MockRecorderRun],
) -> PatternComparisonResult:
    """Run the Qlib Recorder vs AStock observation benchmark."""
    qlib_entries = build_qlib_recorder_entries(runs)
    astock_entries = build_astock_observations()

    run_entries = [e for e in qlib_entries if e.entry_type == "run"]
    unique_params = len({frozenset(e.params.items()) for e in run_entries})
    unique_artifacts = len(
        {h for e in qlib_entries for h in e.artifact_hashes.values()}
    )

    return PatternComparisonResult(
        qlib_entry_count=len(qlib_entries),
        astock_entry_count=len(astock_entries),
        unique_param_count=unique_params,
        unique_artifact_count=unique_artifacts,
        qlib_granularity="experiment>run>metric>artifact",
        astock_granularity="observation>checkpoint>performance",
        overlap_description=(
            "Both record per-run metrics, params, and artifacts. "
            "Qlib uses nested experiment>run hierarchy; AStock uses flat observation register."
        ),
        marginal_value=(
            "Qlib Recorder adds experiment-level grouping (multiple runs under one experiment). "
            "AStock agent-observation-register covers per-skill selection/completion/duration. "
            "Marginal value: experiment-level grouping could help multi-iteration factor research."
        ),
        recommendation="ADAPT_PATTERN",
    )
