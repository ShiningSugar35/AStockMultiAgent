from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from astock.cli import app
from astock.knowledge.completion_service import KnowledgeCompletionService
from astock.research.runtime import ResearchRunService
from astock.research.trading_classification import TradingClassificationService
from astock.schemas.market import Market
from astock.schemas.paper import PaperTradingClassification
from astock.schemas.research_runtime import (
    ResearchRunRequest,
    ResearchRunStage,
    TradingClassificationDraft,
)

ROOT = Path(__file__).resolve().parents[2]


class _UnusedKnowledgeProvider:
    call_count = 0

    def status(self, run_id: str):  # pragma: no cover - plan does not call it
        raise AssertionError(run_id)

    def select(self, run_id: str, query):  # pragma: no cover - plan does not call it
        raise AssertionError((run_id, query))


def test_final_completion_cli_commands_are_registered() -> None:
    commands = {command.name for command in app.registered_commands if command.name}
    expected = {
        "knowledge-completion-review-plan",
        "knowledge-completion-review-apply",
        "knowledge-completion-finalize",
        "knowledge-completion-status",
        "knowledge-completion-publish",
        "knowledge-completion-audit",
        "knowledge-completion-report",
        "knowledge-provider-status",
        "knowledge-provider-select",
        "knowledge-zhihu-visual-capture",
        "knowledge-zhihu-visual-status",
        "research-plan",
        "research-run-plan",
        "research-run-company",
        "research-run",
        "research-run-status",
        "research-status",
        "research-run-audit",
        "research-audit",
        "research-run-recover",
        "research-recover",
        "research-run-benchmark",
        "trading-classification-resolve",
        "trading-classification-freeze",
        "phase7-study-ensure",
        "phase8-status",
        "trading-classification-status",
        "trading-classification-audit",
        "research-runtime-readiness",
        "holding-due",
        "holding-prepare",
        "paper-replay-checkpoint",
        "paper-recovery-plan",
    }
    assert expected <= commands


def test_direct_review_requires_an_explicit_caller_supplied_batch() -> None:
    service_source = (
        ROOT / "src" / "astock" / "knowledge" / "completion_service.py"
    ).read_text(encoding="utf-8")

    assert "direct_knowledge_review_20260809.json" not in service_source
    assert "apply_review_file" in service_source
    assert "apply_review_batch" in service_source


def test_knowledge_completion_finalize_orders_preflight_before_append_only_review() -> None:
    source = (
        ROOT / "src" / "astock" / "knowledge" / "completion_cli.py"
    ).read_text(encoding="utf-8")

    preflight = source.index(
        "preflight = service.audit(batch.run_id, require_registry=False)"
    )
    apply_review = source.index("review = service.apply_review_batch(batch)")
    publish = source.index("release = service.publish_registry(batch.run_id)")
    postflight = source.index("audit = service.audit(batch.run_id)", preflight + 1)
    provider = source.index("provider_status = provider.status(batch.run_id)")
    assert preflight < apply_review < publish < postflight < provider


def test_delegated_review_file_locks_the_twenty_final_decisions() -> None:
    batch = KnowledgeCompletionService.load_review_batch(
        ROOT / "configs" / "direct_knowledge_review_20260809.json"
    )
    approved = {
        item.skill_name
        for item in batch.decisions
        if item.decision.value == "APPROVE"
    }

    assert batch.expected_pending_count == 20
    assert len(batch.decisions) == 20
    assert batch.actor == "GPT-5.6 Sol / explicitly delegated by user"
    assert approved == {
        "Official catalyst and defense-supply-chain verification",
        (
            "Treat policy narratives and company forecasts as hypotheses requiring "
            "source verification"
        ),
        "Ignore cancellable opening-auction indications",
        "Treat seasonal-theme calendars as hypotheses requiring validation",
        (
            "Use pre-calculated marketable limit orders only after "
            "execution-risk review"
        ),
        "Regulated niche moat and payout-sustainability test",
        (
            "Demand an independently testable edge before using a marketed "
            "trading tactic"
        ),
        "Staged-project valuation with completion haircut",
    }


def test_research_runtime_source_stays_outside_knowledge_storage() -> None:
    source = (ROOT / "src" / "astock" / "research" / "runtime.py").read_text(
        encoding="utf-8"
    )

    forbidden = (
        "knowledge_direct_",
        "knowledge_skill_registry",
        "StateStore.connect",
        "SELECT ",
        "INSERT ",
        "UPDATE ",
        "DELETE FROM",
    )
    assert all(item not in source for item in forbidden)
    assert "KnowledgeSkillProvider" in source


def test_final_trade_protocol_is_frozen_only_after_classification_audit() -> None:
    source = (ROOT / "src" / "astock" / "research" / "runtime.py").read_text(
        encoding="utf-8"
    )
    committee_draft = source.index('outputs["committee_protocol_draft"]')
    classification_status = source.index("classification_status =", committee_draft)
    classification_audit = source.index("classification_audit =", classification_status)
    final_protocol = source.index("self._freeze_classified_protocol(", classification_audit)
    paper_decision = source.index('outputs["paper_decision"]', final_protocol)

    assert committee_draft < classification_status < classification_audit < final_protocol
    assert final_protocol < paper_decision


def test_classified_protocol_schema_binds_exact_classification_hash() -> None:
    source = (
        ROOT / "src" / "astock" / "schemas" / "research_runtime.py"
    ).read_text(encoding="utf-8")
    start = source.index("class ClassifiedTradeProtocol")
    end = source.index("class ResearchPaperDecision", start)
    contract = source[start:end]

    assert "trading_classification_artifact_id" in contract
    assert "trading_classification_object_hash" in contract
    assert "broker_execution_allowed: Literal[False]" in contract
    assert "paper_ledger_write_allowed: Literal[False]" in contract


def test_research_run_plan_fails_closed_from_company_and_as_of_only(
    state,
    object_store,
    tmp_path,
) -> None:
    service = ResearchRunService(
        project_root=ROOT,
        state=state,
        objects=object_store,
        reference_parquet_root=tmp_path / "parquet",
        knowledge_provider=_UnusedKnowledgeProvider(),
    )
    request = ResearchRunRequest(
        company_id="300750",
        as_of=datetime(2026, 8, 9, 4, 0, tzinfo=UTC),
    )

    plan = service.plan(request)

    assert plan.next_stage is ResearchRunStage.EVIDENCE
    assert "EVIDENCE_PACK_REQUIRED" in plan.missing_codes
    assert "FINANCIAL_AUDIT_REQUIRED" in plan.missing_codes
    assert "BASE_CASE_DRAFT_REQUIRED" in plan.missing_codes
    assert "KNOWLEDGE_PROVIDER_INPUT_REQUIRED" in plan.missing_codes
    assert "COMMITTEE_ASSESSMENT_REQUIRED" in plan.missing_codes
    assert "TRADING_CLASSIFICATION_REQUIRED" in plan.missing_codes
    assert plan.ledger_write_planned is False
    assert plan.broker_execution_planned is False


def test_trading_classification_is_pit_frozen_and_expires(
    state,
    object_store,
) -> None:
    source_ref = object_store.put_json({"source": "recorded-exchange-classification"})
    state.register_artifact(
        artifact_id="classification-source:test",
        artifact_type="RecordedClassificationSource",
        schema_version="recorded-source-v1",
        object_hash=source_ref.sha256,
        input_hashes=[],
    )
    as_of = datetime(2026, 8, 9, 4, 0, tzinfo=UTC)
    draft = TradingClassificationDraft(
        company_id="300750",
        market=Market.XSHE,
        symbol="300750",
        as_of=as_of,
        effective_from=as_of - timedelta(days=1),
        valid_until=as_of + timedelta(days=1),
        classification=PaperTradingClassification(
            instrument_id="XSHE:300750",
            board="CHINEXT",
            risk_status="NORMAL",
            fixed_price_limit_eligible=True,
            suspension_status_verified=True,
            suspended=False,
            evidence_id="classification-source:test",
        ),
        special_no_price_limit=False,
        source_artifact_ids=["classification-source:test"],
    )
    service = TradingClassificationService(state, object_store)

    first = service.freeze(draft)
    second = service.freeze(draft)

    assert first.artifact_id == second.artifact_id
    assert first.object_hash == second.object_hash
    assert second.idempotent_replay is True
    assert service.status(first.artifact_id, as_of=as_of)["status"] == "READY"
    expired = service.status(first.artifact_id, as_of=as_of + timedelta(days=2))
    assert expired["status"] == "NEEDS_INFO"
    assert "TRADING_CLASSIFICATION_OUTSIDE_VALIDITY" in expired["reason_codes"]
    assert service.audit(first.artifact_id)["status"] == "PASS"


def test_trading_classification_audit_detects_source_binding_drift(
    state,
    object_store,
) -> None:
    first_source = object_store.put_json({"source": "first"})
    second_source = object_store.put_json({"source": "second"})
    state.register_artifact(
        artifact_id="classification-source:drift",
        artifact_type="RecordedClassificationSource",
        schema_version="recorded-source-v1",
        object_hash=first_source.sha256,
        input_hashes=[],
    )
    as_of = datetime(2026, 8, 9, 4, 0, tzinfo=UTC)
    service = TradingClassificationService(state, object_store)
    record = service.freeze(
        TradingClassificationDraft(
            company_id="300750",
            market=Market.XSHE,
            symbol="300750",
            as_of=as_of,
            effective_from=as_of,
            valid_until=as_of + timedelta(days=1),
            classification=PaperTradingClassification(
                instrument_id="XSHE:300750",
                board="CHINEXT",
                risk_status="NORMAL",
                fixed_price_limit_eligible=True,
                suspension_status_verified=True,
                suspended=False,
                evidence_id="classification-source:drift",
            ),
            special_no_price_limit=False,
            source_artifact_ids=["classification-source:drift"],
        )
    )
    with state.transaction() as connection:
        connection.execute(
            "UPDATE artifact_registry SET object_hash=? WHERE artifact_id=?",
            (second_source.sha256, "classification-source:drift"),
        )

    audit = service.audit(record.artifact_id)

    assert audit["status"] == "FAIL"
    assert "CLASSIFICATION_SOURCE_ARTIFACT_DRIFT" in audit["finding_codes"]
