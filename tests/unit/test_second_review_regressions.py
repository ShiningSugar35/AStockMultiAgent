"""Independent regressions for second defect-first quality review."""

import json
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest
from typer.testing import CliRunner

from astock.research.debate_evaluation import (
    CrossResponse,
    QualityError,
    compile_debate,
)
from astock.research.outcome_feedback import summarize_feedback
from astock.research.session_brief import build_session_brief
from astock.schemas.market import Market
from astock.technical import TechnicalAnalysisService
from tests.integration.test_research_quality_cli import app_for
from tests.unit.test_debate_evaluation import inputs
from tests.unit.test_outcome_feedback import run
from tests.unit.test_session_brief import setup
from tests.unit.test_technical_analysis import _bars, _service


def test_cross_response_exceeding_declared_generation_budget_is_rejected() -> None:
    policy, bull, bear, source, proposal = inputs()
    first = compile_debate(bull, bear, source, proposal, policy)
    kwargs = dict(
        debate_input_hash=first["input_hash"],
        round_number=1,
        side="BULL",
        independent_context_id="fresh-bull",
        issue_ids=first["unresolved_issue_ids"],
        evidence_ids=["e1"],
    )
    ok = CrossResponse.model_validate({**kwargs, "response": "A" * policy.cross_response_limit})
    assert (
        compile_debate(bull, bear, source, proposal, policy, responses=(ok,))[
            "incremental_generation_budget"
        ]
        <= 2 * policy.cross_response_limit
    )
    too_long = ok.model_copy(update={"response": "A" * (policy.cross_response_limit + 1)})
    with pytest.raises(QualityError, match="INPUT_LIMIT"):
        compile_debate(bull, bear, source, proposal, policy, responses=(too_long,))


def test_economic_outcome_is_deduplicated_across_evaluation_times() -> None:
    from pathlib import Path

    from astock.research.debate_evaluation import load_quality_policy

    policy = load_quality_policy(
        Path(__file__).resolve().parents[2] / "configs/research_quality.yaml"
    )
    first = run(evaluated_at=datetime(2026, 11, 9, 8, tzinfo=UTC))
    later = run(evaluated_at=datetime(2026, 11, 10, 8, tzinfo=UTC))
    assert first["result_id"] != later["result_id"]
    assert first["visible_bar_inputs"] == later["visible_bar_inputs"]
    summary = summarize_feedback((first, later), policy, context_id="same-economics")
    assert summary["mature_sample_count"] == 1
    assert summary["direction_correct_count"] == 1

    from tests.unit.test_outcome_feedback import bar

    modified_bars = tuple(
        bar(f"2026-11-{d:02d}", close=("12" if d == 9 else "11")) for d in (3, 4, 5, 6, 9)
    )
    divergent = run(stock=modified_bars)
    assert divergent["status"] == "MATURE"
    with pytest.raises(QualityError, match="FEEDBACK_SAMPLE_CONFLICT"):
        summarize_feedback((first, divergent), policy, context_id="conflict")


def test_same_closed_market_facts_with_new_calculation_time_are_one_semantic_brief() -> None:

    policy, calendar = setup()
    sessions = calendar.open_dates(Market.XSHG, date(2026, 9, 1), date(2026, 10, 31))
    assert sessions is not None and sessions[-1] == date(2026, 10, 30)
    days = sessions[-40:]
    initial = _bars([100.0 + i * 0.1 for i in range(len(days))], symbol="600000")
    tz = ZoneInfo("Asia/Shanghai")
    bars = []
    for bar, day in zip(initial, days, strict=True):
        close = datetime.combine(day, time(15, 0), tzinfo=tz)
        bars.append(
            bar.model_copy(
                update={
                    "session_date": day,
                    "session_close_at": close,
                    "available_to_system_at": close + timedelta(minutes=1),
                }
            )
        )
    first_time = datetime(2026, 11, 2, 9, 32, tzinfo=tz)
    second_time = datetime(2026, 11, 2, 9, 36, tzinfo=tz)
    service: TechnicalAnalysisService = _service()
    first = service.build(
        bars,
        as_of=first_time,
        source_artifact_id="daily:" + "1" * 64,
        source_object_hash="2" * 64,
        expected_session_dates=days,
        corporate_action_dates=set(),
    )
    second = service.build(
        bars,
        as_of=second_time,
        source_artifact_id="daily:" + "1" * 64,
        source_object_hash="2" * 64,
        expected_session_dates=days,
        corporate_action_dates=set(),
    )
    assert first.snapshot_id != second.snapshot_id
    one = build_session_brief(
        instrument_id="XSHG:600000",
        account_id=None,
        context_id="semantic-rebrief",
        calendar=calendar,
        policy=policy,
        now=first_time,
        technical=first,
    )
    two = build_session_brief(
        instrument_id="XSHG:600000",
        account_id=None,
        context_id="semantic-rebrief",
        calendar=calendar,
        policy=policy,
        now=second_time,
        technical=second,
        previous_fingerprint=one["semantic_fingerprint"],
    )
    assert one["source_facts"]["technical"] is not None
    assert two["source_facts"]["technical"] is not None
    assert two["semantic_fingerprint"] == one["semantic_fingerprint"]
    assert two["changed"] is False


def test_registered_debate_rejects_evidence_not_bound_to_both_roles(tmp_path) -> None:
    policy, bull, bear, source, proposal = inputs()
    app, state, objects = app_for(tmp_path)
    expected_sha = {}

    for artifact_id, model in (
        ("bull", bull),
        ("bear", bear),
        ("evidence", source),
    ):
        ref = objects.put_json(model.model_dump(mode="json"))
        expected_sha[artifact_id] = ref.sha256
        state.rows[artifact_id] = {
            "type": type(model).__name__,
            "schema_version": model.schema_version,
            "object_hash": ref.sha256,
            "input_hashes": (),
        }

    request = json.dumps(
        {
            "bull_artifact_id": "bull",
            "bear_artifact_id": "bear",
            "evidence_artifact_id": "evidence",
            "proposal": proposal.model_dump(mode="json"),
        },
        ensure_ascii=False,
    )
    missing = CliRunner().invoke(app, ["research-debate-evaluate", request])
    assert missing.exit_code == 2
    assert json.loads(missing.stdout)["code"] == "RESEARCH_EVIDENCE_LINEAGE_MISMATCH"

    state.rows["bull"]["input_hashes"] = (expected_sha["evidence"],)
    state.rows["bear"]["input_hashes"] = (expected_sha["evidence"],)
    valid = CliRunner().invoke(app, ["research-debate-evaluate", request])
    assert valid.exit_code == 0, valid.stdout
    assert json.loads(valid.stdout)["result"]["status"] == "DISPUTED"

    state.rows["bear"]["input_hashes"] = ("f" * 64,)
    foreign = CliRunner().invoke(app, ["research-debate-evaluate", request])
    assert foreign.exit_code == 2
    assert json.loads(foreign.stdout)["code"] == "RESEARCH_EVIDENCE_LINEAGE_MISMATCH"
