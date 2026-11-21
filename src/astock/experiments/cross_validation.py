"""Bounded grouped-label CPCV for caller-supplied fixed strategy responses.

This is a selection diagnostic. It neither fits a predictive model nor verifies
the origin, point-in-time availability, or market executability of its inputs.
Chronological WalkForward and untouched holdout seals retain their authority.
"""

from __future__ import annotations

from bisect import bisect_right
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from itertools import combinations
from math import comb, fsum
from typing import Any, Literal
from zoneinfo import ZoneInfo

from pydantic import Field, field_validator, model_validator

from astock.core.hashing import canonical_json_bytes
from astock.experiments.validation import Budget, Frozen, digest

_SHANGHAI = ZoneInfo("Asia/Shanghai")


class CPCVObservation(Frozen):
    instrument_id: str = Field(min_length=1, max_length=128)
    prediction_at: datetime
    feature_available_at: datetime
    label_end_at: datetime
    label_available_at: datetime
    responses: dict[str, float] = Field(min_length=1, max_length=128)

    @field_validator(
        "prediction_at", "feature_available_at", "label_end_at", "label_available_at"
    )
    @classmethod
    def aware_times(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("CPCV times require an explicit timezone")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def information_order(self) -> CPCVObservation:
        if self.feature_available_at > self.prediction_at:
            raise ValueError("feature information was unavailable at prediction")
        if self.label_end_at < self.prediction_at:
            raise ValueError("label information ends before prediction")
        if self.label_available_at < self.label_end_at:
            raise ValueError("label was marked available before its information ended")
        return self


class CPCVRequest(Frozen):
    schema_version: Literal["grouped-label-cpcv-request-v1"] = "grouped-label-cpcv-request-v1"
    as_of: datetime
    candidate_ids: tuple[str, ...] = Field(min_length=2, max_length=128)
    observations: tuple[CPCVObservation, ...] = Field(min_length=4, max_length=10000)
    n_groups: int = Field(default=4, ge=2, le=16, strict=True)
    n_test_groups: int = Field(default=2, ge=1, le=15, strict=True)
    purge_seconds: int = Field(default=0, ge=0, le=2678400, strict=True)
    embargo_seconds: int = Field(default=0, ge=0, le=2678400, strict=True)
    max_combinations: int = Field(default=64, ge=1, le=256, strict=True)
    preprocessing_scope: Literal["NONE_FIXED_TRIAL_RESPONSES"] = "NONE_FIXED_TRIAL_RESPONSES"
    source_references: tuple[str, ...] = Field(default=(), max_length=256)

    @field_validator("as_of")
    @classmethod
    def aware_cutoff(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("CPCV cutoff requires an explicit timezone")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def complete_panel(self) -> CPCVRequest:
        candidates = set(self.candidate_ids)
        if len(candidates) != len(self.candidate_ids) or any(
            not value.strip() or len(value) > 128 for value in self.candidate_ids
        ):
            raise ValueError("CPCV candidate identifiers must be bounded and unique")
        if self.n_test_groups >= self.n_groups:
            raise ValueError("CPCV must leave at least one training group")
        if comb(self.n_groups, self.n_test_groups) > self.max_combinations:
            raise ValueError("CPCV combination budget exceeded")
        identities: set[tuple[date, str]] = set()
        for row in self.observations:
            identity = (row.prediction_at.astimezone(_SHANGHAI).date(), row.instrument_id)
            if identity in identities:
                raise ValueError("CPCV cannot repeat an instrument within a trading date")
            identities.add(identity)
            if set(row.responses) != candidates:
                raise ValueError("every observation must include every frozen trial response")
            if row.label_available_at > self.as_of:
                raise ValueError("CPCV cannot consume a future or immature label")
        if len({day for day, _ in identities}) < self.n_groups:
            raise ValueError("CPCV has fewer trading dates than groups")
        work = len(self.observations) * len(candidates) * comb(
            self.n_groups, self.n_test_groups
        )
        if (work > 20_000_000 or len(self.observations) * comb(
            self.n_groups, self.n_test_groups
        ) > 250_000):
            raise ValueError("CPCV observation-work budget exceeded")
        return self


@dataclass(frozen=True, slots=True)
class _DateGroup:
    day: date
    rows: tuple[int, ...]
    prediction_at: datetime
    label_end_at: datetime
    responses: dict[str, float]


def _date_groups(request: CPCVRequest) -> list[_DateGroup]:
    by_date: dict[date, list[int]] = {}
    for index, row in enumerate(request.observations):
        day = row.prediction_at.astimezone(_SHANGHAI).date()
        by_date.setdefault(day, []).append(index)
    result: list[_DateGroup] = []
    for day, indices in sorted(by_date.items()):
        rows = [request.observations[index] for index in indices]
        result.append(_DateGroup(
            day=day, rows=tuple(indices),
            prediction_at=min(row.prediction_at for row in rows),
            label_end_at=max(row.label_end_at for row in rows),
            responses={
                candidate: fsum(row.responses[candidate] / len(rows) for row in rows)
                for candidate in sorted(request.candidate_ids)
            },
        ))
    return result


def _merged_intervals(
    intervals: list[tuple[datetime, datetime]],
) -> tuple[list[datetime], list[datetime]]:
    merged: list[tuple[datetime, datetime]] = []
    for start, end in sorted(intervals):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    return [start for start, _ in merged], [end for _, end in merged]


def _overlaps(
    start: datetime, end: datetime, intervals: tuple[list[datetime], list[datetime]],
) -> bool:
    starts, ends = intervals
    index = bisect_right(starts, end) - 1
    return index >= 0 and ends[index] >= start


def _split_date_blocks(count: int, n_groups: int) -> list[tuple[int, ...]]:
    width, remainder = divmod(count, n_groups)
    cursor = 0
    blocks = []
    for index in range(n_groups):
        stop = cursor + width + int(index < remainder)
        blocks.append(tuple(range(cursor, stop)))
        cursor = stop
    return blocks


def _fold_dates(
    groups: list[_DateGroup], test: tuple[int, ...], request: CPCVRequest,
) -> tuple[list[int], list[int], list[int]]:
    padding = timedelta(seconds=request.purge_seconds)
    embargo = timedelta(seconds=request.embargo_seconds)
    try:
        intervals = _merged_intervals([
            (groups[i].prediction_at - padding, groups[i].label_end_at + padding)
            for i in test
        ])
        embargo_intervals = _merged_intervals([
            (groups[i].label_end_at, groups[i].label_end_at + embargo)
            for i in test
        ]) if request.embargo_seconds else ([], [])
    except OverflowError as exc:
        raise ValueError("CPCV interval padding exceeds the datetime range") from exc
    excluded = set(test)
    train, purged, embargoed = [], [], []
    for index, group in enumerate(groups):
        if index in excluded:
            continue
        if _overlaps(group.prediction_at, group.label_end_at, intervals):
            purged.append(index)
        elif _overlaps(group.prediction_at, group.prediction_at, embargo_intervals):
            embargoed.append(index)
        else:
            train.append(index)
    return train, purged, embargoed


def _fold_report(
    number: int, groups: list[_DateGroup], test: tuple[int, ...], request: CPCVRequest,
) -> dict[str, Any]:
    train, purged, embargoed = _fold_dates(groups, test, request)

    def rows(indices: Sequence[int]) -> list[int]:
        return sorted(row for i in indices for row in groups[i].rows)

    result: dict[str, Any] = {
        "fold": number,
        "train_indices": rows(train), "test_indices": rows(test),
        "purged_indices": rows(purged), "embargoed_indices": rows(embargoed),
        "train_dates": [groups[i].day.isoformat() for i in train],
        "test_dates": [groups[i].day.isoformat() for i in test],
        "contains_post_test_training": bool(
            train and max(groups[i].day for i in train) > min(groups[i].day for i in test)
        ),
        "status": "COMPUTED" if train else "NOT_EVALUABLE",
        "reason_codes": [] if train else ["PURGE_EMBARGO_REMOVED_ALL_TRAINING_DATES"],
        "selected_candidate": None,
        "tied_training_winners": [],
        "training_scores": {},
        "test_mean_response": None,
    }
    if not train:
        return result
    scores = {
        candidate: fsum(groups[i].responses[candidate] / len(train) for i in train)
        for candidate in sorted(request.candidate_ids)
    }
    winner = min(scores, key=lambda candidate: (-scores[candidate], candidate))
    result.update({
        "selected_candidate": winner, "training_scores": scores,
        "tied_training_winners": sorted(
            candidate for candidate, value in scores.items() if value == scores[winner]
        ),
        "test_mean_response": fsum(groups[i].responses[winner] / len(test) for i in test),
    })
    return result


def build_grouped_cpcv_report(request: CPCVRequest) -> dict[str, Any]:
    """Select frozen response streams per purged fold; never fit on all rows."""
    request = CPCVRequest.model_validate_json(request.model_dump_json())
    budget = Budget(10.0)
    groups = _date_groups(request)
    blocks = _split_date_blocks(len(groups), request.n_groups)
    folds = []
    for number, selected in enumerate(
        combinations(range(request.n_groups), request.n_test_groups), start=1
    ):
        budget.check()
        test = tuple(i for block in selected for i in blocks[block])
        folds.append(_fold_report(number, groups, test, request))
    budget.check()
    evaluated = sum(f["status"] == "COMPUTED" for f in folds)
    result: dict[str, Any] = {
        "schema_version": "grouped-label-cpcv-report-v1",
        "authority": "CALLER_SUPPLIED_DIAGNOSTIC",
        "input_hash": digest(request),
        "source_references": list(request.source_references),
        "source_integrity_verified": False, "historical_pit_verified": False,
        "formal_strategy_admission_allowed": False, "publication_authority": False,
        "deployment_oos_claim": False, "account_write_allowed": False,
        "persistent_writes": False, "model_fitting_performed": False,
        "producer_lookahead_verified": False,
        "label_interval_convention": "CLOSED_INCLUSIVE",
        "date_group_timezone": "Asia/Shanghai",
        "date_interval_rule": "MIN_PREDICTION_MAX_LABEL_END_WHOLE_DATE_PURGE",
        "selection_metric": "MEAN_OF_DAILY_CROSS_SECTIONAL_MEAN_RESPONSES",
        "tie_rule": "LEXICAL_CANDIDATE_WITH_ALL_TIES_REPORTED",
        "preprocessing_scope": request.preprocessing_scope,
        "observation_count": len(request.observations),
        "date_group_count": len(groups), "candidate_count": len(request.candidate_ids),
        "combinations": len(folds), "evaluated_folds": evaluated,
        "status": "COMPUTED" if evaluated == len(folds)
        else "PARTIAL_NOT_EVALUABLE" if evaluated else "NOT_EVALUABLE",
        "date_groups": [
            {"date": group.day.isoformat(), "row_indices": list(group.rows),
             "prediction_at": group.prediction_at.isoformat(),
             "label_end_at": group.label_end_at.isoformat()}
            for group in groups
        ],
        "warning_codes": [
            "CPCV_TRAINING_MAY_CONTAIN_TIMES_AFTER_TEST",
            "OVERLAPPING_LABEL_RESPONSES_ARE_NOT_COMPOUNDED",
            "SOURCE_TIMING_UNIVERSE_AND_FULL_TRIAL_COVERAGE_ARE_CALLER_DECLARED",
            "NO_MODEL_OR_FEATURE_FIT_PERFORMED",
            "WALK_FORWARD_AND_UNTOUCHED_HOLDOUT_REMAIN_REQUIRED",
        ],
        "folds": folds,
    }
    result["report_hash"] = digest(result)
    if len(canonical_json_bytes(result)) > 2_097_152:
        raise ValueError("CPCV report exceeds the existing artifact byte budget")
    return result
