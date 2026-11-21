from datetime import UTC, datetime, timedelta

import pytest
import typer
from typer.testing import CliRunner

from astock.experiments.cli import register_commands
from astock.experiments.cross_validation import (
    CPCVObservation,
    CPCVRequest,
    build_grouped_cpcv_report,
)


def panel(**updates) -> CPCVRequest:
    start = datetime(2024, 1, 1, 1, tzinfo=UTC)
    observations = tuple(
        CPCVObservation(
            instrument_id=instrument,
            prediction_at=start + timedelta(days=day),
            feature_available_at=start + timedelta(days=day, hours=-1),
            label_end_at=start + timedelta(days=day, hours=1),
            label_available_at=start + timedelta(days=day, hours=2),
            responses={"a": 0.01 + day / 10000, "b": -0.01 + day / 10000},
        )
        for day in range(16)
        for instrument in ("XSHG:600001", "XSHE:000001", "XSHE:300001")
    )
    return CPCVRequest.model_validate({
        "as_of": start + timedelta(days=30),
        "candidate_ids": ("a", "b"), "observations": observations,
        "n_groups": 4, "n_test_groups": 2,
        **updates,
    })


def test_cpcv_keeps_each_trading_date_together_and_is_not_deployment():
    request = panel()
    report = build_grouped_cpcv_report(request)
    assert report == build_grouped_cpcv_report(request)
    assert report["combinations"] == 6
    assert report["status"] == "COMPUTED"
    assert report["authority"] == "CALLER_SUPPLIED_DIAGNOSTIC"
    assert not report["historical_pit_verified"]
    assert not report["source_integrity_verified"]
    assert not report["formal_strategy_admission_allowed"]
    assert not report["publication_authority"]
    assert not report["deployment_oos_claim"]
    assert not report["model_fitting_performed"]
    assert any(fold["contains_post_test_training"] for fold in report["folds"])
    for fold in report["folds"]:
        assert set(fold["train_dates"]).isdisjoint(fold["test_dates"])
        test = set(fold["test_indices"])
        train = set(fold["train_indices"])
        for group in report["date_groups"]:
            indices = set(group["row_indices"])
            assert not (indices & test) or indices <= test
            assert not (indices & train) or indices <= train


def test_actual_crossing_label_purges_the_whole_security_date_group():
    request = panel()
    observations = list(request.observations)
    old = observations[10]  # second instrument on date 3
    observations[10] = old.model_copy(update={
        "label_end_at": observations[15].prediction_at,
        "label_available_at": observations[15].label_available_at,
    })
    report = build_grouped_cpcv_report(request.model_copy(update={
        "observations": tuple(observations),
    }))
    fold = report["folds"][3]  # dates 4..11 are held out
    assert {9, 10, 11} <= set(fold["purged_indices"])
    assert not ({9, 10, 11} & set(fold["train_indices"]))


def test_closed_label_endpoint_and_post_label_embargo_are_distinct():
    request = panel()
    observations = list(request.observations)
    observations[10] = observations[10].model_copy(update={
        "label_end_at": observations[12].prediction_at,
        "label_available_at": observations[12].label_available_at,
    })
    report = build_grouped_cpcv_report(request.model_copy(update={
        "observations": tuple(observations),
    }))
    assert {9, 10, 11} <= set(report["folds"][3]["purged_indices"])
    embargoed = build_grouped_cpcv_report(panel(embargo_seconds=2 * 86400))["folds"][0]
    assert set(range(24, 30)) <= set(embargoed["embargoed_indices"])
    assert not set(range(24, 30)) & set(embargoed["train_indices"])


@pytest.mark.parametrize(
    "field,change",
    [
        ("label_available_at", timedelta(days=40)),
        ("feature_available_at", timedelta(hours=1)),
        ("label_end_at", timedelta(hours=-1)),
    ],
)
def test_future_information_and_reversed_labels_are_rejected(field, change):
    payload = panel().model_dump(mode="python")
    row = payload["observations"][0]
    row[field] = row["prediction_at"] + change
    with pytest.raises(ValueError):
        CPCVRequest.model_validate(payload)


def test_no_full_sample_preprocessing_and_selection_uses_only_fold_training():
    request = panel()
    payload = request.model_dump(mode="python")
    payload["preprocessing_scope"] = "FULL_SAMPLE"
    with pytest.raises(ValueError, match="NONE_FIXED_TRIAL_RESPONSES"):
        CPCVRequest.model_validate(payload)
    original = build_grouped_cpcv_report(request)
    test_indices = set(original["folds"][0]["test_indices"])
    changed = tuple(
        row.model_copy(update={"responses": {"a": -900.0, "b": 900.0}})
        if index in test_indices else row
        for index, row in enumerate(request.observations)
    )
    report = build_grouped_cpcv_report(request.model_copy(update={"observations": changed}))
    assert original["folds"][0]["selected_candidate"] == "a"
    assert report["folds"][0]["selected_candidate"] == "a"
    assert report["folds"][0]["training_scores"] == original["folds"][0]["training_scores"]
    assert report["input_hash"] != original["input_hash"]


def test_duplicate_date_security_missing_trial_and_combination_budget_are_rejected():
    request = panel()
    with pytest.raises(ValueError, match="repeat an instrument"):
        panel(observations=request.observations + (request.observations[0],))
    rows = list(request.observations)
    rows[0] = rows[0].model_copy(update={"responses": {"a": 0.01}})
    with pytest.raises(ValueError, match="every frozen trial"):
        build_grouped_cpcv_report(request.model_copy(update={"observations": tuple(rows)}))
    with pytest.raises(ValueError, match="combination budget"):
        panel(n_groups=8, n_test_groups=4, max_combinations=20)


def test_all_training_labels_purged_is_not_a_zero_performance_result():
    request = panel()
    rows = tuple(row.model_copy(update={
        "label_end_at": request.observations[-1].label_end_at,
        "label_available_at": request.observations[-1].label_available_at,
    }) for row in request.observations)
    result = build_grouped_cpcv_report(request.model_copy(update={"observations": rows}))
    assert result["status"] == "NOT_EVALUABLE"
    assert result["evaluated_folds"] == 0
    assert all(fold["test_mean_response"] is None for fold in result["folds"])


def test_real_cpcv_cli_consumes_panel_and_emits_report_without_state(tmp_path):
    request_path = tmp_path / "cpcv.json"
    request_path.write_text(panel().model_dump_json(), encoding="utf-8")
    output = []
    app = typer.Typer()

    def no_services():
        raise AssertionError("read-only response diagnostics must not create a state store")

    register_commands(app, no_services, output.append)
    before = set(tmp_path.iterdir())
    result = CliRunner().invoke(app, ["strategy-validation-cpcv", str(request_path)])
    assert result.exit_code == 0, result.output
    assert set(tmp_path.iterdir()) == before
    assert len(output) == 1
    assert output[0]["combinations"] == 6
    assert output[0]["persistent_writes"] is False
    assert output[0]["authority"] == "CALLER_SUPPLIED_DIAGNOSTIC"
