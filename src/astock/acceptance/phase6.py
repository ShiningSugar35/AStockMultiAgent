"""Recorded 300750 vertical slice for the Phase 6 decision and paper boundary."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from astock.committee import CommitteeService, load_committee_rules
from astock.core.hashing import content_hash
from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.documents import DocumentPageRepository, DocumentRepository
from astock.evidence import ClaimEvidenceService, EvidenceRepository
from astock.financial_integrity import FinancialIntegrityService
from astock.market_data import MarketReferenceService, ReferenceParquetStore
from astock.pit import PointInTimeRepository, PointInTimeService
from astock.research import (
    EvidenceCollectionTaskService,
    EvidencePackService,
    FormalResearchPreparationService,
    ResearchCoreService,
    ResearchDiagnosticsService,
    ResearchRequestService,
    ResearchSkillService,
    load_research_core_config,
    load_research_diagnostic_config,
    load_research_skill_registry,
)
from astock.schemas import (
    BASE_CASE_SECTIONS,
    AvailabilityBasis,
    BaseCaseBuildRequest,
    BaseCaseDraft,
    ClaimStatus,
    ClaimType,
    CommitteeAccessPolicy,
    CommitteeAssessment,
    CommitteeCoverageMetrics,
    CommitteeDecisionRequest,
    CommitteeDecisionScope,
    CommitteeEntryOrderType,
    CommitteeMemberBinding,
    CommitteeMemberRole,
    CommitteePortfolioRiskState,
    CommitteeProtocolDraft,
    CommitteeRatioRange,
    DailyBarObservation,
    DecisionPack,
    DocumentPage,
    DocumentType,
    EvidenceAttachment,
    EvidenceCollectionRun,
    EvidenceCollectionRunStatus,
    EvidenceGrade,
    EvidenceRelation,
    FactStatus,
    FetchStatus,
    FinancialAuditRequest,
    FinancialDurationSemantics,
    FinancialFact,
    FinancialFieldCode,
    FinancialIndustryProfile,
    FinancialPeriodType,
    FinancialStatementType,
    FinancialUnit,
    InstrumentRecord,
    InstrumentType,
    Market,
    PageExtractionMethod,
    PaperReferencePack,
    PaperTradingClassification,
    Phase6ClosureReport,
    Phase6RunStatus,
    PointInTimeStatus,
    ResearchFindingInput,
    ResearchFindingType,
    ResearchMemoArtifact,
    ResearchMemoComposeRequest,
    ResearchPreparationRequest,
    ResearchPreparationStatus,
    SourceDocument,
    SourceSnapshot,
    SpecialistDeltaBuildRequest,
    SpecialistRouteRequest,
    TradeProtocol,
    TradingSession,
)

_SHANGHAI = ZoneInfo("Asia/Shanghai")
_COMPANY_ID = "300750"
_COMPANY_NAME = "宁德时代"
_AS_OF = datetime(2026, 7, 27, 9, 0, tzinfo=_SHANGHAI)
_ORDER_TIME = datetime(2026, 7, 27, 10, 0, tzinfo=_SHANGHAI)
_AVAILABLE_AT = datetime(2026, 7, 24, 16, 0, tzinfo=_SHANGHAI)
_DISCLAIMER = (
    "CONTROLLED RECORDED SOFTWARE ACCEPTANCE FIXTURE; "
    "not current company research, investment advice, or authorization for live trading."
)

_FINANCIAL_VALUES: dict[FinancialFieldCode, Decimal] = {
    FinancialFieldCode.TOTAL_ASSETS: Decimal("1000"),
    FinancialFieldCode.TOTAL_LIABILITIES: Decimal("600"),
    FinancialFieldCode.TOTAL_EQUITY: Decimal("400"),
    FinancialFieldCode.CASH_BEGINNING: Decimal("100"),
    FinancialFieldCode.NET_CASH_OPERATING: Decimal("200"),
    FinancialFieldCode.NET_CASH_INVESTING: Decimal("-80"),
    FinancialFieldCode.NET_CASH_FINANCING: Decimal("30"),
    FinancialFieldCode.EXCHANGE_EFFECT: Decimal("0"),
    FinancialFieldCode.CASH_ENDING: Decimal("250"),
    FinancialFieldCode.NET_PROFIT_INCOME: Decimal("100"),
    FinancialFieldCode.NET_PROFIT_CASH_FLOW: Decimal("100"),
    FinancialFieldCode.REVENUE: Decimal("1000"),
    FinancialFieldCode.OPERATING_COST: Decimal("600"),
    FinancialFieldCode.ACCOUNTS_RECEIVABLE: Decimal("100"),
    FinancialFieldCode.INVENTORY: Decimal("120"),
    FinancialFieldCode.PREPAYMENTS: Decimal("20"),
    FinancialFieldCode.OTHER_RECEIVABLES: Decimal("10"),
}

_BALANCE_FIELDS = {
    FinancialFieldCode.TOTAL_ASSETS,
    FinancialFieldCode.TOTAL_LIABILITIES,
    FinancialFieldCode.TOTAL_EQUITY,
    FinancialFieldCode.ACCOUNTS_RECEIVABLE,
    FinancialFieldCode.INVENTORY,
    FinancialFieldCode.PREPAYMENTS,
    FinancialFieldCode.OTHER_RECEIVABLES,
}
_CASH_FLOW_FIELDS = {
    FinancialFieldCode.CASH_BEGINNING,
    FinancialFieldCode.NET_CASH_OPERATING,
    FinancialFieldCode.NET_CASH_INVESTING,
    FinancialFieldCode.NET_CASH_FINANCING,
    FinancialFieldCode.EXCHANGE_EFFECT,
    FinancialFieldCode.CASH_ENDING,
    FinancialFieldCode.NET_PROFIT_CASH_FLOW,
}


@dataclass(frozen=True, slots=True)
class Phase6RecordedExecution:
    report: Phase6ClosureReport
    research_memo: ResearchMemoArtifact
    committee_decision: DecisionPack
    trade_protocol: TradeProtocol
    paper_reference_pack: PaperReferencePack
    reused_existing: bool


@dataclass(frozen=True, slots=True)
class _RecordedEvidence:
    claim_id: str
    evidence_id: str
    facts: list[FinancialFact]
    snapshot_id: str


class Phase6RecordedService:
    """Run and audit the explicitly non-production 300750 acceptance case."""

    def __init__(
        self,
        project_root: Path,
        state: StateStore,
        objects: ObjectStore,
        parquet_root: Path,
    ) -> None:
        self.project_root = project_root.resolve()
        self.state = state
        self.objects = objects
        self.parquet_root = parquet_root.resolve()

    def run(self, company_id: str) -> Phase6RecordedExecution:
        if company_id != _COMPANY_ID:
            raise ValueError("the recorded Phase 6 acceptance case only covers 300750")
        self._sync_recorded_instruments()
        evidence = self._recorded_evidence()
        financial = self._financial_audit(evidence.facts)

        request = ResearchRequestService(
            self.state,
            self.objects,
            self.parquet_root,
        ).create_request(company_id)
        task = EvidenceCollectionTaskService(self.state, self.objects).create_task(
            request.artifact_id
        )
        run_artifact_id = self._completed_evidence_run(
            task.artifact_id,
            task.object_sha256,
            f"ClaimEvidenceBundle:{evidence.claim_id}",
        )
        evidence_pack = EvidencePackService(self.state, self.objects).create_pack(run_artifact_id)
        preparation = FormalResearchPreparationService(
            self.state,
            self.objects,
            load_research_core_config(self.project_root / "configs" / "research_core.yaml"),
        ).prepare(
            ResearchPreparationRequest(
                research_request_artifact_id=request.artifact_id,
                evidence_pack_artifact_id=evidence_pack.artifact_id,
                financial_audit_run_id=financial.audit_run_id,
                claim_ids=[evidence.claim_id],
                as_of=_AS_OF,
                formal_historical=True,
                allow_approximated=False,
                created_at=_AS_OF,
            )
        )
        if (
            preparation.manifest.status is not ResearchPreparationStatus.READY_FOR_BASE_CASE
            or preparation.manifest.frozen_evidence_pack_id is None
        ):
            raise ValueError("recorded Phase 6 evidence did not pass formal preparation")

        core = ResearchCoreService(
            self.state,
            self.objects,
            load_research_core_config(self.project_root / "configs" / "research_core.yaml"),
        )
        base = core.build_base_case(
            BaseCaseBuildRequest(
                evidence_pack_id=preparation.manifest.frozen_evidence_pack_id,
                draft=self._base_case_draft(evidence.evidence_id),
                created_at=_AS_OF,
            )
        )
        recorded_registry = load_research_skill_registry(
            self.project_root / "configs" / "phase6_recorded_skills.yaml"
        )
        skills = ResearchSkillService(
            self.state,
            self.objects,
            recorded_registry,
        )
        route = skills.route(
            SpecialistRouteRequest(
                base_case_id=base.pack.base_case_id,
                thesis_tags=["phase6_recorded"],
                industry_tags=[],
                event_tags=[],
                horizon="long",
                available_inputs=["recorded_official_evidence"],
                available_frequencies=[],
                explicit_skill_ids=[
                    "SerenityRecordedSkill",
                    "ZhihuExpertRecordedSkill",
                ],
                created_at=_AS_OF,
            )
        )
        serenity = skills.build_delta(
            self._delta_request(
                base.pack.base_case_id,
                route.plan.route_plan_id,
                evidence.evidence_id,
                skill_id="SerenityRecordedSkill",
                skill_version="serenity-recorded-v1",
                statement=(
                    "Recorded Serenity delta: test the frozen industry thesis against "
                    "substitution and value-capture failure modes."
                ),
                confidence_delta=0.02,
            )
        )
        zhihu = skills.build_delta(
            self._delta_request(
                base.pack.base_case_id,
                route.plan.route_plan_id,
                evidence.evidence_id,
                skill_id="ZhihuExpertRecordedSkill",
                skill_version="zhihu-expert-recorded-v1",
                statement=(
                    "Recorded Zhihu expert delta: challenge narrative extrapolation and "
                    "retain an explicit evidence invalidation condition."
                ),
                confidence_delta=-0.01,
            )
        )
        diagnostics = ResearchDiagnosticsService(
            self.state,
            self.objects,
            recorded_registry,
            load_research_diagnostic_config(
                self.project_root / "configs" / "research_diagnostics.yaml"
            ),
        )
        memo = diagnostics.compose_memo(
            ResearchMemoComposeRequest(
                base_case_id=base.pack.base_case_id,
                route_plan_id=route.plan.route_plan_id,
                delta_ids=sorted([serenity.delta.delta_id, zhihu.delta.delta_id]),
                created_at=_AS_OF,
            )
        )
        committee = CommitteeService(
            self.state,
            self.objects,
            load_committee_rules(self.project_root / "configs" / "committee_rules.yaml"),
        )
        committee_request = self._committee_request(
            committee,
            preparation.manifest.frozen_evidence_pack_artifact_id,
            base.pack.base_case_id,
            route.plan.route_plan_id,
            serenity.delta.delta_id,
            zhihu.delta.delta_id,
            memo.memo.memo_id,
            financial.audit_run_id,
            memo.memo.evidence_ids,
            route.plan.selected,
        )
        decision = committee.decide_investment(committee_request)
        reference_pack, reference_artifact_id, reference_hash = self._paper_reference_pack(evidence)
        artifacts = {
            "research_request": request.artifact_id,
            "frozen": preparation.manifest.frozen_evidence_pack_artifact_id,
            "base": f"BaseCasePack:{base.pack.base_case_id}",
            "route": f"SpecialistRoutePlan:{route.plan.route_plan_id}",
            "serenity": f"SpecialistDelta:{serenity.delta.delta_id}",
            "zhihu": f"SpecialistDelta:{zhihu.delta.delta_id}",
            "financial": f"FinancialIntegrityEvidencePack:{financial.audit_run_id}",
            "memo": f"ResearchMemoArtifact:{memo.memo.memo_id}",
            "decision": f"DecisionPack:{decision.decision.decision_id}",
            "protocol": f"TradeProtocol:{decision.protocol.protocol_id}",
            "reference": reference_artifact_id,
        }
        object_hashes = [self._artifact_hash(value) for value in artifacts.values()]
        run_id = content_hash(
            {
                "case": "phase6-recorded-300750-v1",
                "company_id": company_id,
                "input_object_hashes": sorted(object_hashes),
            }
        )
        report = Phase6ClosureReport(
            run_id=run_id,
            company_id=company_id,
            company_name=_COMPANY_NAME,
            data_mode="RECORDED_ACCEPTANCE",
            status=Phase6RunStatus.AWAITING_USER_CONFIRMATION,
            research_request_artifact_id=artifacts["research_request"],
            frozen_evidence_pack_artifact_id=artifacts["frozen"],
            base_case_artifact_id=artifacts["base"],
            specialist_route_artifact_id=artifacts["route"],
            specialist_delta_artifact_ids={
                "SERENITY": artifacts["serenity"],
                "ZHIHU_EXPERT": artifacts["zhihu"],
            },
            financial_integrity_artifact_id=artifacts["financial"],
            research_memo_artifact_id=artifacts["memo"],
            committee_decision_artifact_id=artifacts["decision"],
            trade_protocol_artifact_id=artifacts["protocol"],
            paper_reference_pack_artifact_id=reference_artifact_id,
            trade_protocol_outcome=decision.protocol.outcome,
            input_object_hashes=sorted(object_hashes),
            disclaimer=_DISCLAIMER,
            created_at=_AS_OF,
        )
        reused = self._register_report(
            report,
            input_hash=run_id,
            reference_hash=reference_hash,
        )
        return Phase6RecordedExecution(
            report=report,
            research_memo=memo.memo,
            committee_decision=decision.decision,
            trade_protocol=decision.protocol,
            paper_reference_pack=reference_pack,
            reused_existing=reused,
        )

    def status(self, company_id: str = _COMPANY_ID) -> dict[str, object]:
        with self.state.connect() as connection:
            row = connection.execute(
                "SELECT * FROM phase6_run_index WHERE company_id=? "
                "ORDER BY created_at DESC,run_id DESC LIMIT 1",
                (company_id,),
            ).fetchone()
            if row is None:
                return {"status": "NOT_RUN", "company_id": company_id}
            protocol_id = str(row["protocol_artifact_id"]).removeprefix("TradeProtocol:")
            execution = connection.execute(
                "SELECT p.execution_request_id,p.status,p.operation_id,b.order_id "
                "FROM paper_execution_request_index p "
                "LEFT JOIN paper_order_rule_binding b ON b.operation_id=p.operation_id "
                "WHERE p.trade_protocol_id=? "
                "ORDER BY p.created_at DESC,p.execution_request_id DESC LIMIT 1",
                (protocol_id,),
            ).fetchone()
        execution_payload = dict(execution) if execution is not None else None
        paper_order_id = (
            str(execution["order_id"])
            if execution is not None and execution["order_id"] is not None
            else None
        )
        return {
            "status": "AVAILABLE",
            "closure_status": (
                Phase6RunStatus.PAPER_ORDER_CREATED
                if paper_order_id is not None
                else Phase6RunStatus.AWAITING_USER_CONFIRMATION
            ),
            "paper_order_id": paper_order_id,
            "paper_execution": execution_payload,
            **dict(row),
        }

    def audit(self, run_id: str) -> dict[str, object]:
        with self.state.connect() as connection:
            row = connection.execute(
                "SELECT * FROM phase6_run_index WHERE run_id=?",
                (run_id,),
            ).fetchone()
        if row is None:
            return {
                "status": "NOT_RUN",
                "run_id": run_id,
                "finding_codes": ["PHASE6_RUN_NOT_FOUND"],
            }
        findings: set[str] = set()
        object_hash = str(row["report_object_hash"])
        try:
            report = Phase6ClosureReport.model_validate_json(self.objects.get_bytes(object_hash))
        except (OSError, ValueError):
            return {
                "status": "PARTIAL",
                "run_id": run_id,
                "finding_codes": ["PHASE6_REPORT_INVALID"],
            }
        for artifact_id in (
            report.research_request_artifact_id,
            report.frozen_evidence_pack_artifact_id,
            report.base_case_artifact_id,
            report.specialist_route_artifact_id,
            *report.specialist_delta_artifact_ids.values(),
            report.financial_integrity_artifact_id,
            report.research_memo_artifact_id,
            report.committee_decision_artifact_id,
            report.trade_protocol_artifact_id,
            report.paper_reference_pack_artifact_id,
        ):
            try:
                self._artifact_hash(artifact_id)
            except ValueError:
                findings.add("PHASE6_ARTIFACT_MISSING_OR_CORRUPT")
        decision_id = report.committee_decision_artifact_id.removeprefix("DecisionPack:")
        committee = CommitteeService(
            self.state,
            self.objects,
            load_committee_rules(self.project_root / "configs" / "committee_rules.yaml"),
        )
        if committee.audit(decision_id)["status"] != "PASS":
            findings.add("COMMITTEE_AUDIT_FAILED")
        core = ResearchCoreService(
            self.state,
            self.objects,
            load_research_core_config(self.project_root / "configs" / "research_core.yaml"),
        )
        if core.audit(report.company_id)["status"] != "PASS":
            findings.add("RESEARCH_AUDIT_FAILED")
        skills = ResearchSkillService(
            self.state,
            self.objects,
            load_research_skill_registry(
                self.project_root / "configs" / "phase6_recorded_skills.yaml"
            ),
        )
        base_id = report.base_case_artifact_id.removeprefix("BaseCasePack:")
        if skills.audit(base_id)["status"] != "PASS":
            findings.add("SPECIALIST_AUDIT_FAILED")
        return {
            "status": "PASS" if not findings else "PARTIAL",
            "run_id": run_id,
            "company_id": report.company_id,
            "trade_protocol_outcome": report.trade_protocol_outcome,
            "external_access": {
                "network": False,
                "browser": False,
                "mcp": False,
                "broker": False,
            },
            "finding_codes": sorted(findings),
        }

    def _sync_recorded_instruments(self) -> None:
        report = MarketReferenceService(
            self.state,
            self.objects,
            ReferenceParquetStore(self.parquet_root),
            self.project_root / "tests" / "fixtures" / "reference",
        ).sync_instruments(live=False)
        if report.release_id is None:
            raise ValueError("recorded instrument master is unavailable")

    def _recorded_evidence(self) -> _RecordedEvidence:
        text = "\n".join(
            [
                "PHASE6 RECORDED SOFTWARE ACCEPTANCE FIXTURE FOR 300750.",
                "This controlled source is not a current company filing.",
                *(
                    f"{field.value}: {value}"
                    for field, value in sorted(
                        _FINANCIAL_VALUES.items(),
                        key=lambda item: item[0].value,
                    )
                ),
                "Research chain statement: citations, gaps, and invalidation stay frozen.",
            ]
        )
        raw = self.objects.put_bytes(text.encode("utf-8"))
        source_id = "acceptance:phase6:300750:v1"
        snapshot = SourceSnapshot(
            snapshot_id=f"{source_id}:{raw.sha256}",
            source_id=source_id,
            object_sha256=raw.sha256,
            fetched_at=_AVAILABLE_AT,
            available_to_system_at=_AVAILABLE_AT,
            source_url="acceptance://phase6/300750/v1",
            mime="text/plain",
            byte_size=raw.byte_size,
            fetch_status=FetchStatus.SUCCEEDED,
            rights_status="CONTROLLED_ACCEPTANCE_FIXTURE",
            created_at=_AVAILABLE_AT,
        )
        self.state.register_snapshot(snapshot)
        document = SourceDocument(
            document_id=f"document:{source_id}",
            title="Phase 6 controlled 300750 acceptance fixture",
            publisher="CONTROLLED_ACCEPTANCE",
            document_type=DocumentType.ANNUAL_REPORT,
            company_ids=[_COMPANY_ID],
            published_at=_AVAILABLE_AT,
            effective_at=_AVAILABLE_AT,
            disclosure_id=source_id,
            source_url="acceptance://phase6/300750/v1",
            rights_status="CONTROLLED_ACCEPTANCE_FIXTURE",
            created_at=_AVAILABLE_AT,
        )
        documents = DocumentRepository(self.state)
        documents.register(document, snapshot)
        pages = DocumentPageRepository(self.state)
        page_model = DocumentPage(
            page_id=f"page:{content_hash({'snapshot_id': snapshot.snapshot_id, 'page': 1})}",
            document_id=document.document_id,
            snapshot_id=snapshot.snapshot_id,
            page_number=1,
            width_points=595,
            height_points=842,
            native_text_char_count=len(text),
            text_char_count=len(text),
            text_sha256=raw.sha256,
            text_object_sha256=raw.sha256,
            extraction_method=PageExtractionMethod.NATIVE_TEXT,
            ocr_applied=False,
            parser_name="phase6-recorded-text",
            parser_version="phase6-recorded-text-v1",
            section_path=["RECORDED_ACCEPTANCE"],
            created_at=_AVAILABLE_AT,
        )
        pages.register_page(page_model)
        parsed_text = text
        claim_service = ClaimEvidenceService(
            self.objects,
            self.state,
            pages,
            documents,
            EvidenceRepository(self.state),
        )
        evidence = claim_service.create_page_evidence(
            page_id=page_model.page_id,
            char_start=0,
            char_end=len(parsed_text),
            evidence_grade=EvidenceGrade.PRIMARY_OFFICIAL,
            fact_status=FactStatus.DIRECT,
            entity_ids=[_COMPANY_ID],
        )
        claim = claim_service.create_claim(
            subject_id=_COMPANY_ID,
            predicate="phase6_recorded_acceptance_lineage",
            object_json={"case_id": "phase6-recorded-300750-v1"},
            as_of=_AVAILABLE_AT + timedelta(seconds=1),
            claim_type=ClaimType.FACT,
            confidence=0.9,
            status=ClaimStatus.VALIDATED,
            attachments=[
                EvidenceAttachment(
                    evidence_id=evidence.evidence_id,
                    relation=EvidenceRelation.SUPPORT,
                    created_at=_AVAILABLE_AT,
                )
            ],
        )
        pit = PointInTimeService(
            PointInTimeRepository(self.state),
            self.state,
            self.objects,
        ).create(
            source_id=source_id,
            source_document_id=document.document_id,
            source_snapshot_id=snapshot.snapshot_id,
            period_end=date(2025, 12, 31),
            published_at=_AVAILABLE_AT,
            effective_at=_AVAILABLE_AT,
            ingested_at=_AVAILABLE_AT,
            available_to_system_at=_AVAILABLE_AT,
            point_in_time_status=PointInTimeStatus.DOCUMENT_RECONSTRUCTED,
            availability_basis=AvailabilityBasis.FETCH_OBSERVED,
        )
        facts = [
            FinancialFact(
                fact_id=f"fact:phase6:300750:{field.value}",
                company_id=_COMPANY_ID,
                period_start=(None if field in _BALANCE_FIELDS else date(2025, 1, 1)),
                period_end=date(2025, 12, 31),
                period_type=FinancialPeriodType.ANNUAL,
                duration_semantics=(
                    FinancialDurationSemantics.INSTANT
                    if field in _BALANCE_FIELDS
                    else FinancialDurationSemantics.REPORTED_PERIOD
                ),
                statement_type=self._statement_type(field),
                field_code=field,
                reported_value=value,
                unit=FinancialUnit.TEN_THOUSAND_CNY,
                source_snapshot_id=snapshot.snapshot_id,
                pit_id=pit.pit_id,
                evidence_ids=[evidence.evidence_id],
                created_at=_AVAILABLE_AT,
            )
            for field, value in _FINANCIAL_VALUES.items()
        ]
        return _RecordedEvidence(
            claim_id=claim.claim.claim_id,
            evidence_id=evidence.evidence_id,
            facts=facts,
            snapshot_id=snapshot.snapshot_id,
        )

    def _financial_audit(self, facts: list[FinancialFact]):
        return (
            FinancialIntegrityService(
                self.state,
                self.objects,
                rule_config_path=self.project_root / "configs" / "financial_rules.yaml",
                industry_profile_path=(
                    self.project_root / "configs" / "financial_industry_profiles.yaml"
                ),
            )
            .run(
                FinancialAuditRequest(
                    company_id=_COMPANY_ID,
                    as_of=_AVAILABLE_AT + timedelta(hours=1),
                    industry_profile=FinancialIndustryProfile.GENERAL_INDUSTRIAL,
                    facts=facts,
                    created_at=_AVAILABLE_AT,
                )
            )
            .pack
        )

    def _completed_evidence_run(
        self,
        task_artifact_id: str,
        task_object_hash: str,
        collected_artifact_id: str,
    ) -> str:
        run = EvidenceCollectionRun(
            task_artifact_id=task_artifact_id,
            status=EvidenceCollectionRunStatus.COMPLETED,
            started_at=_AVAILABLE_AT,
            completed_at=_AVAILABLE_AT,
            collected_items=[collected_artifact_id],
            missing_items=[],
            created_at=_AVAILABLE_AT,
        )
        artifact_id = "EvidenceCollectionRun:" + content_hash(
            {
                "task_artifact_id": task_artifact_id,
                "collected_items": run.collected_items,
            }
        )
        object_ref = self.objects.put_json(run.model_dump(mode="json"))
        self.state.register_artifact(
            artifact_id=artifact_id,
            artifact_type="EvidenceCollectionRun",
            schema_version=run.schema_version,
            object_hash=object_ref.sha256,
            input_hashes=[task_object_hash],
        )
        return artifact_id

    @staticmethod
    def _base_case_draft(evidence_id: str) -> BaseCaseDraft:
        return BaseCaseDraft(
            company_id=_COMPANY_ID,
            as_of=_AS_OF,
            findings_by_section={
                section: [
                    ResearchFindingInput(
                        statement=(
                            f"Recorded acceptance BaseCase section {section.value}; "
                            "not a live factual conclusion."
                        ),
                        finding_type=ResearchFindingType.VERIFIED_FACT,
                        confidence=0.8,
                        critical=True,
                        evidence_ids=[evidence_id],
                        created_at=_AS_OF,
                    )
                ]
                for section in BASE_CASE_SECTIONS
            },
            evidence_gaps=[],
            specialist_tags=["phase6_recorded"],
            requested_base_confidence=0.85,
            created_at=_AS_OF,
        )

    @staticmethod
    def _delta_request(
        base_case_id: str,
        route_plan_id: str,
        evidence_id: str,
        *,
        skill_id: str,
        skill_version: str,
        statement: str,
        confidence_delta: float,
    ) -> SpecialistDeltaBuildRequest:
        return SpecialistDeltaBuildRequest(
            base_case_id=base_case_id,
            route_plan_id=route_plan_id,
            skill_id=skill_id,
            skill_version=skill_version,
            incremental_findings=[
                ResearchFindingInput(
                    statement=statement,
                    finding_type=ResearchFindingType.ANALYST_INFERENCE,
                    confidence=0.7,
                    critical=False,
                    evidence_ids=[evidence_id],
                    created_at=_AS_OF,
                )
            ],
            base_case_corrections=[],
            industry_specific_metrics=[],
            additional_evidence_requests=[],
            failure_modes=["RECORDED_FIXTURE_NOT_LIVE_RESEARCH"],
            confidence_delta=confidence_delta,
            valuation_adjustments=[],
            risk_adjustments=[],
            coverage_delta={},
            created_at=_AS_OF,
        )

    def _committee_request(
        self,
        committee: CommitteeService,
        frozen_artifact_id: str | None,
        base_case_id: str,
        route_plan_id: str,
        serenity_delta_id: str,
        zhihu_delta_id: str,
        memo_id: str,
        financial_run_id: str,
        evidence_ids: list[str],
        selected_skills,
    ) -> CommitteeDecisionRequest:
        if frozen_artifact_id is None:
            raise ValueError("recorded Phase 6 frozen artifact is missing")
        artifact_ids = [
            frozen_artifact_id,
            f"BaseCasePack:{base_case_id}",
            f"SpecialistRoutePlan:{route_plan_id}",
            f"SpecialistDelta:{serenity_delta_id}",
            f"SpecialistDelta:{zhihu_delta_id}",
            f"ResearchMemoArtifact:{memo_id}",
            f"FinancialIntegrityEvidencePack:{financial_run_id}",
        ]
        references = sorted(
            (committee.resolve_reference(item) for item in artifact_ids),
            key=lambda item: item.artifact_id,
        )
        reference_by_id = {item.artifact_id: item for item in references}
        members = sorted(
            [
                CommitteeMemberBinding(
                    role=CommitteeMemberRole.BASE_CASE,
                    artifact_id=f"BaseCasePack:{base_case_id}",
                    object_sha256=reference_by_id[f"BaseCasePack:{base_case_id}"].object_sha256,
                    created_at=_AS_OF,
                ),
                CommitteeMemberBinding(
                    role=CommitteeMemberRole.SERENITY_DELTA,
                    artifact_id=f"SpecialistDelta:{serenity_delta_id}",
                    object_sha256=reference_by_id[
                        f"SpecialistDelta:{serenity_delta_id}"
                    ].object_sha256,
                    created_at=_AS_OF,
                ),
                CommitteeMemberBinding(
                    role=CommitteeMemberRole.ZHIHU_EXPERT_DELTA,
                    artifact_id=f"SpecialistDelta:{zhihu_delta_id}",
                    object_sha256=reference_by_id[
                        f"SpecialistDelta:{zhihu_delta_id}"
                    ].object_sha256,
                    created_at=_AS_OF,
                ),
                CommitteeMemberBinding(
                    role=CommitteeMemberRole.FINANCIAL_INTEGRITY,
                    artifact_id=f"FinancialIntegrityEvidencePack:{financial_run_id}",
                    object_sha256=reference_by_id[
                        f"FinancialIntegrityEvidencePack:{financial_run_id}"
                    ].object_sha256,
                    created_at=_AS_OF,
                ),
            ],
            key=lambda item: item.role.value,
        )
        skill_versions = {item.skill_id: item.skill_version for item in selected_skills}
        skill_versions["ResearchMemoComposer"] = "research-memo-composer-v1"
        assessment = CommitteeAssessment(
            company_id=_COMPANY_ID,
            scope=CommitteeDecisionScope.NEW_CANDIDATE,
            as_of=_AS_OF,
            expected_return_range=CommitteeRatioRange(
                lower=Decimal("0.12"),
                upper=Decimal("0.20"),
                evidence_ids=evidence_ids,
                created_at=_AS_OF,
            ),
            downside_range=CommitteeRatioRange(
                lower=Decimal("-0.20"),
                upper=Decimal("-0.05"),
                evidence_ids=evidence_ids,
                created_at=_AS_OF,
            ),
            confidence=Decimal("0.80"),
            coverage=CommitteeCoverageMetrics(
                data_coverage=Decimal("1"),
                evidence_coverage=Decimal("1"),
                specialist_coverage=Decimal("1"),
                pit_coverage=Decimal("1"),
                liquidity_score=Decimal("1"),
                evidence_ids=evidence_ids,
                created_at=_AS_OF,
            ),
            portfolio_risk=CommitteePortfolioRiskState(
                current_total_exposure=Decimal("0"),
                post_decision_total_exposure=Decimal("0.04"),
                current_industry_exposure=Decimal("0"),
                post_decision_industry_exposure=Decimal("0.04"),
                max_abs_correlation=Decimal("0.20"),
                portfolio_drawdown=Decimal("0"),
                consecutive_loss_count=0,
                evidence_ids=evidence_ids,
                created_at=_AS_OF,
            ),
            tradable=True,
            market_data_quality_pass=True,
            current_position=Decimal("0"),
            requested_position=Decimal("0.04"),
            holding_horizon_days=180,
            review_at=_AS_OF + timedelta(days=7),
            support_evidence_ids=evidence_ids,
            signal_evidence_ids={},
            optional_narrative_requested=False,
            protocol=CommitteeProtocolDraft(
                strategy_id="phase6-recorded-paper-only-v1",
                skill_versions=dict(sorted(skill_versions.items())),
                earliest_executable_time=_AS_OF + timedelta(minutes=35),
                entry_rule="User supplies a separate paper-only limit-order request.",
                entry_order_type=CommitteeEntryOrderType.PAPER_LIMIT,
                position_size_rule="Never exceed the frozen committee maximum position.",
                price_stop_rule="Review the recorded thesis; no automatic order.",
                volatility_stop_rule="Review only; no automatic order.",
                trailing_stop_rule="Review only; no automatic order.",
                time_stop_rule="Review at the frozen committee date.",
                thesis_invalidation_rule="Block simulation when frozen evidence changes.",
                take_profit_rule="Review only; no automatic order.",
                review_events=["FROZEN_EVIDENCE_CHANGED", "USER_REVIEW_DATE"],
                max_holding_period_days=365,
                cost_model_version="cn-a-share-paper-2026-07-13",
                fill_model_version="paper-fill-v1",
                evidence_snapshot_id=frozen_artifact_id.removeprefix("FrozenEvidencePack:"),
                evidence_ids=evidence_ids,
                created_at=_AS_OF,
            ),
            created_at=_AS_OF,
        )
        return CommitteeDecisionRequest(
            artifact_references=references,
            member_bindings=members,
            assessment=assessment,
            access_policy=CommitteeAccessPolicy(
                frozen_artifact_hashes=sorted(item.object_sha256 for item in references),
                created_at=_AS_OF,
            ),
            created_at=_AS_OF,
        )

    def _paper_reference_pack(
        self,
        evidence: _RecordedEvidence,
    ) -> tuple[PaperReferencePack, str, str]:
        available = _AVAILABLE_AT.astimezone(UTC)
        order_time = _ORDER_TIME.astimezone(UTC)
        sessions = [
            TradingSession(
                exchange=Market.XSHE,
                session_date=date(2026, 7, 24),
                is_open=True,
                source_snapshot_id=evidence.snapshot_id,
                available_to_system_at=available,
                created_at=available,
            ),
            TradingSession(
                exchange=Market.XSHE,
                session_date=date(2026, 7, 27),
                is_open=True,
                source_snapshot_id=evidence.snapshot_id,
                available_to_system_at=available,
                created_at=available,
            ),
        ]
        instrument = InstrumentRecord(
            instrument_id="XSHE:300750",
            market=Market.XSHE,
            symbol=_COMPANY_ID,
            name=_COMPANY_NAME,
            instrument_type=InstrumentType.STOCK,
            tradable=True,
            status_date=date(2026, 7, 24),
            is_st=False,
            listing_date=date(2018, 6, 11),
            source_snapshot_id=evidence.snapshot_id,
            available_to_system_at=available,
            created_at=available,
        )
        daily = [
            DailyBarObservation(
                observation_id=content_hash({"case": "phase6-300750", "session": "2026-07-24"}),
                instrument_id=instrument.instrument_id,
                market=Market.XSHE,
                symbol=_COMPANY_ID,
                session_date=date(2026, 7, 24),
                session_close_at=datetime(2026, 7, 24, 15, 0, tzinfo=_SHANGHAI),
                open=Decimal("198"),
                high=Decimal("205"),
                low=Decimal("197"),
                close=Decimal("200"),
                previous_close=Decimal("198"),
                volume=Decimal("1000000"),
                amount=Decimal("200000000"),
                is_st=False,
                source_snapshot_id=evidence.snapshot_id,
                available_to_system_at=datetime(2026, 7, 24, 15, 1, tzinfo=_SHANGHAI),
                created_at=available,
            )
        ]
        calendar_release_id = content_hash([item.model_dump(mode="json") for item in sessions])
        instrument_release_id = content_hash(instrument.model_dump(mode="json"))
        daily_release_id = content_hash([item.model_dump(mode="json") for item in daily])
        pack_identity = {
            "case": "phase6-recorded-300750-paper-reference-v1",
            "calendar_release_id": calendar_release_id,
            "instrument_release_id": instrument_release_id,
            "daily_release_id": daily_release_id,
        }
        pack = PaperReferencePack(
            pack_id=f"paper-reference:{content_hash(pack_identity)}",
            data_mode="RECORDED_ACCEPTANCE",
            market=Market.XSHE,
            symbol=_COMPANY_ID,
            visible_at=order_time,
            calendar_release_id=calendar_release_id,
            instrument_release_id=instrument_release_id,
            daily_release_id=daily_release_id,
            sessions=sessions,
            instrument=instrument,
            daily_bars=daily,
            classification=PaperTradingClassification(
                instrument_id=instrument.instrument_id,
                board="CHINEXT",
                risk_status="NORMAL",
                fixed_price_limit_eligible=True,
                suspension_status_verified=True,
                suspended=False,
                evidence_id=evidence.evidence_id,
                created_at=available,
            ),
            source_snapshot_ids=[evidence.snapshot_id],
            created_at=available,
        )
        object_ref = self.objects.put_json(pack.model_dump(mode="json"))
        artifact_id = f"PaperReferencePack:{pack.pack_id}"
        self.state.register_artifact(
            artifact_id=artifact_id,
            artifact_type="PaperReferencePack",
            schema_version=pack.schema_version,
            object_hash=object_ref.sha256,
            input_hashes=[evidence.snapshot_id],
        )
        return pack, artifact_id, object_ref.sha256

    def _register_report(
        self,
        report: Phase6ClosureReport,
        *,
        input_hash: str,
        reference_hash: str,
    ) -> bool:
        object_ref = self.objects.put_json(report.model_dump(mode="json"))
        artifact_id = f"Phase6ClosureReport:{report.run_id}"
        self.state.register_artifact(
            artifact_id=artifact_id,
            artifact_type="Phase6ClosureReport",
            schema_version=report.schema_version,
            object_hash=object_ref.sha256,
            input_hashes=report.input_object_hashes,
        )
        with self.state.transaction() as connection:
            existing = connection.execute(
                "SELECT report_object_hash,input_hash FROM phase6_run_index WHERE run_id=?",
                (report.run_id,),
            ).fetchone()
            if existing is not None:
                if (
                    str(existing["report_object_hash"]) != object_ref.sha256
                    or str(existing["input_hash"]) != input_hash
                ):
                    raise ValueError("Phase 6 recorded run identity collision")
                return True
            connection.execute(
                "INSERT INTO phase6_run_index("
                "run_id,company_id,data_mode,research_request_artifact_id,"
                "memo_artifact_id,decision_artifact_id,protocol_artifact_id,"
                "paper_reference_pack_artifact_id,report_object_hash,input_hash,created_at"
                ") VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    report.run_id,
                    report.company_id,
                    report.data_mode,
                    report.research_request_artifact_id,
                    report.research_memo_artifact_id,
                    report.committee_decision_artifact_id,
                    report.trade_protocol_artifact_id,
                    report.paper_reference_pack_artifact_id,
                    object_ref.sha256,
                    input_hash,
                    report.created_at.astimezone(UTC).isoformat(),
                ),
            )
        if reference_hash not in report.input_object_hashes:
            raise ValueError("Phase 6 report omitted its paper reference object")
        return False

    def _artifact_hash(self, artifact_id: str) -> str:
        with self.state.connect() as connection:
            row = connection.execute(
                "SELECT object_hash FROM artifact_registry WHERE artifact_id=?",
                (artifact_id,),
            ).fetchone()
        if row is None:
            raise ValueError(f"Phase 6 artifact is missing: {artifact_id}")
        object_hash = str(row["object_hash"])
        if not self.objects.verify(object_hash):
            raise ValueError(f"Phase 6 artifact object is corrupt: {artifact_id}")
        return object_hash

    @staticmethod
    def _statement_type(field: FinancialFieldCode) -> FinancialStatementType:
        if field in _BALANCE_FIELDS:
            return FinancialStatementType.BALANCE_SHEET
        if field in _CASH_FLOW_FIELDS:
            return FinancialStatementType.CASH_FLOW_STATEMENT
        return FinancialStatementType.INCOME_STATEMENT


def load_phase6_report(objects: ObjectStore, object_hash: str) -> Phase6ClosureReport:
    return Phase6ClosureReport.model_validate_json(objects.get_bytes(object_hash))


__all__ = [
    "Phase6RecordedExecution",
    "Phase6RecordedService",
    "load_phase6_report",
]
