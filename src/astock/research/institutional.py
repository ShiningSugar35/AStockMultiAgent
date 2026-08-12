"""Institutional-grade evidence, economics, forecast, and valuation orchestration."""

from __future__ import annotations

from collections import defaultdict, deque
from datetime import timedelta
from decimal import Decimal
from typing import Any, TypeVar

from pydantic import BaseModel

from astock.core.hashing import content_hash
from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.evidence import EvidenceRepository
from astock.research.fundamental_analytics import (
    dcf_fcff_value,
    evaluate_driver_period,
    forecast_period_from_nodes,
    implied_terminal_growth,
    topological_order,
)
from astock.schemas.evidence import (
    ClaimEvidenceBundle,
    ClaimType,
    ConflictResolutionStatus,
    Evidence,
    EvidenceGrade,
    EvidenceRelation,
    FactStatus,
    ReviewerStatus,
    SourceSnapshot,
)
from astock.schemas.institutional_research import (
    ClaimDependencyEdge,
    ClaimSufficiencyAssessment,
    CompanyArchetype,
    CompanyEconomicsBuildRequest,
    CompanyEconomicsProfile,
    DriverAssumptionProvenance,
    DriverOperation,
    DriverTree,
    DriverTreeBuildRequest,
    EvidenceAuthorityTier,
    EvidenceDirectness,
    EvidenceExtractionConfidence,
    EvidenceFreshness,
    EvidenceQualityVector,
    EvidenceScopeMatch,
    EvidenceSufficiencyReport,
    EvidenceSufficiencyRequest,
    EvidenceSufficiencyState,
    ForecastBuildRequest,
    ForecastPack,
    ForecastScenario,
    ForecastScenarioPack,
    ForecastTemplate,
    FundamentalModelBundle,
    FundamentalModelBundleBuildRequest,
    IndustryProfile,
    IndustryProfileBuildRequest,
    InstitutionalArtifactStatus,
    InstitutionalClaimType,
    InstitutionalDecisionContext,
    InstitutionalDecisionContextBuildRequest,
    InstitutionalResearchFinalizeRequest,
    MarketImpliedExpectation,
    SourceEpistemicMetadata,
    TaxonomyStatus,
    ValuationBuildRequest,
    ValuationMethod,
    ValuationPack,
    ValuationScenarioResult,
    ValuationSensitivityPoint,
)
from astock.schemas.pit import PointInTimeStatus
from astock.schemas.research import FrozenEvidencePack

T = TypeVar("T", bound=BaseModel)

_REQUIRED_INDUSTRY_FIELDS = (
    "definition",
    "market_size_growth",
    "industry_profitability",
    "market_share_structure",
    "supply_capacity_demand",
    "pricing_mechanism",
    "competitive_dynamics",
    "regulation_external_drivers",
)
_REQUIRED_COMPANY_FIELDS = (
    "pricing_power",
    "competitive_position",
    "management_governance",
    "capital_allocation",
    "reinvestment_roic",
    "funding_dilution",
)
_DEPENDENCY_REQUIRED_TYPES = {
    InstitutionalClaimType.DERIVED_FACT,
    InstitutionalClaimType.CAUSAL_CLAIM,
    InstitutionalClaimType.FORECAST,
    InstitutionalClaimType.VALUATION_ASSUMPTION,
}
_TWO_SOURCE_TYPES = {
    InstitutionalClaimType.INDUSTRY_ESTIMATE,
    InstitutionalClaimType.INTERPRETATION,
    InstitutionalClaimType.CAUSAL_CLAIM,
}


class InstitutionalResearchService:
    """Build immutable professional research artifacts without expanding trading authority."""

    def __init__(self, state: StateStore, objects: ObjectStore) -> None:
        self.state = state
        self.objects = objects
        self.evidence = EvidenceRepository(state)

    def run_evidence_sufficiency(
        self,
        request: EvidenceSufficiencyRequest,
    ) -> EvidenceSufficiencyReport:
        pack, pack_hash = self._load(
            request.frozen_evidence_pack_artifact_id,
            "FrozenEvidencePack",
            FrozenEvidencePack,
        )
        if not set(request.material_claim_ids).issubset(pack.claim_ids):
            raise ValueError("material claims must be inside the frozen evidence pack")
        dependency_order = self._claim_dependency_order(
            request.material_claim_ids,
            request.dependencies,
        )
        explicit_metadata = {item.snapshot_id: item for item in request.source_metadata}
        assessments: dict[str, ClaimSufficiencyAssessment] = {}
        snapshot_hashes: set[str] = set()
        for claim_id in dependency_order:
            bundle = self.evidence.get_claim_bundle(claim_id)
            if bundle is None:
                raise ValueError(f"material claim is unavailable: {claim_id}")
            if bundle.claim.as_of > pack.as_of:
                raise ValueError("material claim was not available at the frozen as_of")
            links = [
                link
                for link in bundle.links
                if link.evidence_id in pack.evidence_ids and link.weight > 0
            ]
            quality_by_evidence: dict[str, EvidenceQualityVector] = {}
            for link in links:
                evidence = self.evidence.get_evidence(link.evidence_id)
                if evidence is None:
                    raise ValueError(f"frozen evidence is unavailable: {link.evidence_id}")
                if evidence.evidence_grade is not pack.evidence_grade_by_id[evidence.evidence_id]:
                    raise ValueError("frozen evidence grade drift")
                if evidence.available_to_system_at > pack.as_of:
                    raise ValueError("frozen evidence item is future-visible")
                if not self.objects.verify(evidence.excerpt_object_sha256):
                    raise ValueError("frozen evidence excerpt object is unavailable")
                snapshot = self.state.get_snapshot(evidence.snapshot_id)
                if snapshot is None or not self.objects.verify(snapshot.object_sha256):
                    raise ValueError(
                        f"evidence source snapshot is unavailable: {evidence.snapshot_id}"
                    )
                if snapshot.available_to_system_at > pack.as_of:
                    raise ValueError("evidence source snapshot is future-visible")
                snapshot_hashes.add(snapshot.object_sha256)
                metadata = explicit_metadata.get(snapshot.snapshot_id) or self._derived_metadata(
                    snapshot,
                    evidence.evidence_grade,
                )
                if metadata.system_ingested_at > pack.as_of:
                    raise ValueError("epistemic metadata is future-visible")
                if metadata.snapshot_id != snapshot.snapshot_id:
                    raise ValueError("epistemic metadata snapshot identity mismatch")
                self._validate_authority_compatibility(
                    evidence.evidence_grade,
                    metadata.authority_tier,
                )
                institutional_type = self._institutional_claim_type(
                    bundle.claim.claim_type,
                    request.claim_type_overrides.get(claim_id),
                )
                quality_by_evidence[link.evidence_id] = self._quality_vector(
                    bundle,
                    link.reviewer_status,
                    institutional_type,
                    evidence,
                    snapshot,
                    metadata,
                    pack.pit_status_by_evidence_id[link.evidence_id],
                    pack.as_of,
                )
            assessment = self._assess_claim(
                bundle=bundle,
                request=request,
                pack=pack,
                quality_by_evidence=quality_by_evidence,
                prior_assessments=assessments,
            )
            assessments[claim_id] = assessment

        ordered_assessments = [assessments[claim_id] for claim_id in request.material_claim_ids]
        blocking_codes = sorted(
            {
                code
                for item in ordered_assessments
                if item.state
                in {EvidenceSufficiencyState.CONFLICTED, EvidenceSufficiencyState.INSUFFICIENT}
                for code in item.reason_codes
            }
        )
        status = (
            InstitutionalArtifactStatus.NEEDS_INFO
            if blocking_codes
            else InstitutionalArtifactStatus.READY
        )
        identity = {
            "pack_hash": pack_hash,
            "request": request.model_dump(mode="json", exclude={"created_at"}),
            "assessments": [
                item.model_dump(mode="json", exclude={"created_at"}) for item in ordered_assessments
            ],
        }
        report_id = f"evidence-sufficiency:{content_hash(identity)}"
        report = EvidenceSufficiencyReport(
            report_id=report_id,
            company_id=pack.company_id,
            as_of=pack.as_of,
            frozen_evidence_pack_artifact_id=request.frozen_evidence_pack_artifact_id,
            frozen_evidence_pack_object_hash=pack_hash,
            status=status,
            material_claim_ids=request.material_claim_ids,
            assessments=ordered_assessments,
            dependencies=request.dependencies,
            blocking_codes=blocking_codes,
            source_artifact_ids=[request.frozen_evidence_pack_artifact_id],
            source_object_hashes=sorted({pack_hash, *snapshot_hashes}),
            created_at=pack.as_of,
        )
        self._persist(
            report,
            artifact_type="EvidenceSufficiencyReport",
            artifact_id=f"EvidenceSufficiencyReport:{report.report_id}",
            input_hashes=report.source_object_hashes,
            checkpoint_scope="institutional-evidence-sufficiency",
            checkpoint_key=pack.company_id,
            checkpoint_status=report.status.value,
        )
        return report

    def build_industry_profile(self, request: IndustryProfileBuildRequest) -> IndustryProfile:
        sufficiency, sufficiency_hash = self._load(
            request.evidence_sufficiency_artifact_id,
            "EvidenceSufficiencyReport",
            EvidenceSufficiencyReport,
        )
        self._require_company_as_of(request.company_id, request.as_of, sufficiency)
        missing = []
        if sufficiency.status is not InstitutionalArtifactStatus.READY:
            missing.append("EVIDENCE_SUFFICIENCY_NOT_READY")
        missing.extend(
            f"INDUSTRY_{field.upper()}_REQUIRED"
            for field in _REQUIRED_INDUSTRY_FIELDS
            if getattr(request.draft, field) is None
        )
        warnings: list[str] = []
        source_artifact_ids = [request.evidence_sufficiency_artifact_id]
        source_hashes = [sufficiency_hash]
        if request.draft.taxonomy_status is not TaxonomyStatus.CERTIFIED:
            warnings.append("PROVISIONAL_TAXONOMY")
        elif request.draft.taxonomy_artifact_id is None:
            missing.append("CERTIFIED_TAXONOMY_ARTIFACT_REQUIRED")
        if request.draft.taxonomy_artifact_id:
            taxonomy_record = self.state.artifact_record(request.draft.taxonomy_artifact_id)
            if taxonomy_record is None:
                if request.draft.taxonomy_status is TaxonomyStatus.CERTIFIED:
                    missing.append("CERTIFIED_TAXONOMY_ARTIFACT_REQUIRED")
                else:
                    warnings.append("TAXONOMY_ARTIFACT_UNREGISTERED")
            else:
                taxonomy_hash = str(taxonomy_record["object_hash"])
                if not self.objects.verify(taxonomy_hash):
                    raise ValueError("taxonomy artifact object is unavailable")
                source_artifact_ids.append(request.draft.taxonomy_artifact_id)
                source_hashes.append(taxonomy_hash)
        claim_ids, evidence_ids = self._collect_lineage(request.draft)
        missing.extend(self._lineage_gap_codes(sufficiency, claim_ids, evidence_ids))
        missing = sorted(set(missing))
        status = (
            InstitutionalArtifactStatus.NEEDS_INFO if missing else InstitutionalArtifactStatus.READY
        )
        profile_id = "industry-profile:" + content_hash(
            {
                "company_id": request.company_id,
                "as_of": request.as_of,
                "sufficiency_hash": sufficiency_hash,
                "draft": request.draft,
            }
        )
        profile = IndustryProfile(
            profile_id=profile_id,
            company_id=request.company_id,
            as_of=request.as_of,
            status=status,
            draft=request.draft,
            missing_codes=missing,
            warning_codes=sorted(set(warnings)),
            claim_ids=claim_ids,
            evidence_ids=evidence_ids,
            source_artifact_ids=sorted(set(source_artifact_ids)),
            source_object_hashes=sorted(set(source_hashes)),
            created_at=request.as_of,
        )
        self._persist(
            profile,
            artifact_type="IndustryProfile",
            artifact_id=f"IndustryProfile:{profile.profile_id}",
            input_hashes=profile.source_object_hashes,
            checkpoint_scope="institutional-industry-profile",
            checkpoint_key=request.company_id,
            checkpoint_status=profile.status.value,
        )
        return profile

    def build_company_economics(
        self,
        request: CompanyEconomicsBuildRequest,
    ) -> CompanyEconomicsProfile:
        sufficiency, sufficiency_hash = self._load(
            request.evidence_sufficiency_artifact_id,
            "EvidenceSufficiencyReport",
            EvidenceSufficiencyReport,
        )
        self._require_company_as_of(request.company_id, request.as_of, sufficiency)
        missing = []
        if sufficiency.status is not InstitutionalArtifactStatus.READY:
            missing.append("EVIDENCE_SUFFICIENCY_NOT_READY")
        missing.extend(
            f"COMPANY_{field.upper()}_REQUIRED"
            for field in _REQUIRED_COMPANY_FIELDS
            if getattr(request.draft, field) is None
        )
        if not request.draft.segments:
            missing.append("COMPANY_SEGMENT_ECONOMICS_REQUIRED")
        warnings: list[str] = []
        if request.draft.customer_concentration is None:
            warnings.append("CUSTOMER_CONCENTRATION_NOT_MODELED")
        if request.draft.supplier_dependency is None:
            warnings.append("SUPPLIER_DEPENDENCY_NOT_MODELED")
        claim_ids, evidence_ids = self._collect_lineage(request.draft)
        missing.extend(self._lineage_gap_codes(sufficiency, claim_ids, evidence_ids))
        missing = sorted(set(missing))
        status = (
            InstitutionalArtifactStatus.NEEDS_INFO if missing else InstitutionalArtifactStatus.READY
        )
        profile_id = "company-economics:" + content_hash(
            {
                "company_id": request.company_id,
                "as_of": request.as_of,
                "sufficiency_hash": sufficiency_hash,
                "draft": request.draft,
            }
        )
        profile = CompanyEconomicsProfile(
            profile_id=profile_id,
            company_id=request.company_id,
            as_of=request.as_of,
            status=status,
            draft=request.draft,
            missing_codes=missing,
            warning_codes=sorted(set(warnings)),
            claim_ids=claim_ids,
            evidence_ids=evidence_ids,
            source_artifact_ids=[request.evidence_sufficiency_artifact_id],
            source_object_hashes=[sufficiency_hash],
            created_at=request.as_of,
        )
        self._persist(
            profile,
            artifact_type="CompanyEconomicsProfile",
            artifact_id=f"CompanyEconomicsProfile:{profile.profile_id}",
            input_hashes=profile.source_object_hashes,
            checkpoint_scope="institutional-company-economics",
            checkpoint_key=request.company_id,
            checkpoint_status=profile.status.value,
        )
        return profile

    def build_driver_tree(self, request: DriverTreeBuildRequest) -> DriverTree:
        industry, industry_hash = self._load(
            request.industry_profile_artifact_id,
            "IndustryProfile",
            IndustryProfile,
        )
        company, company_hash = self._load(
            request.company_economics_artifact_id,
            "CompanyEconomicsProfile",
            CompanyEconomicsProfile,
        )
        self._require_company_as_of(request.company_id, request.as_of, industry)
        self._require_company_as_of(request.company_id, request.as_of, company)
        if industry.status is not InstitutionalArtifactStatus.READY:
            raise ValueError("driver tree requires a READY IndustryProfile")
        if company.status is not InstitutionalArtifactStatus.READY:
            raise ValueError("driver tree requires a READY CompanyEconomicsProfile")
        expected_template = self._forecast_template_for_archetype(company.draft.archetype)
        if request.draft.forecast_template is not expected_template:
            raise ValueError("driver tree forecast template does not match the company archetype")
        order = topological_order(request.draft.nodes)
        input_ids = {
            item.node_id for item in request.draft.nodes if item.operation is DriverOperation.INPUT
        }
        historical_input_ids = {
            item.node_id for item in request.draft.historical_points if item.node_id in input_ids
        }
        if historical_input_ids != input_ids:
            raise ValueError(
                "every forecast input driver requires at least one historical observation"
            )
        claim_ids, evidence_ids = self._collect_lineage(request.draft)
        allowed_claims = set(industry.claim_ids) | set(company.claim_ids)
        allowed_evidence = set(industry.evidence_ids) | set(company.evidence_ids)
        if not set(claim_ids).issubset(allowed_claims):
            raise ValueError("driver historical claims escape the institutional profiles")
        if not set(evidence_ids).issubset(allowed_evidence):
            raise ValueError("driver historical evidence escapes the institutional profiles")
        tree_id = "driver-tree:" + content_hash(
            {
                "company_id": request.company_id,
                "as_of": request.as_of,
                "industry_hash": industry_hash,
                "company_hash": company_hash,
                "draft": request.draft,
                "order": order,
            }
        )
        tree = DriverTree(
            tree_id=tree_id,
            company_id=request.company_id,
            as_of=request.as_of,
            draft=request.draft,
            evaluation_order=order,
            claim_ids=claim_ids,
            evidence_ids=evidence_ids,
            source_artifact_ids=sorted(
                {request.industry_profile_artifact_id, request.company_economics_artifact_id}
            ),
            source_object_hashes=sorted({industry_hash, company_hash}),
            created_at=request.as_of,
        )
        self._persist(
            tree,
            artifact_type="DriverTree",
            artifact_id=f"DriverTree:{tree.tree_id}",
            input_hashes=tree.source_object_hashes,
            checkpoint_scope="institutional-driver-tree",
            checkpoint_key=request.company_id,
            checkpoint_status="READY",
        )
        return tree

    def build_forecast(self, request: ForecastBuildRequest) -> ForecastPack:
        tree, tree_hash = self._load(
            request.driver_tree_artifact_id,
            "DriverTree",
            DriverTree,
        )
        sufficiency, sufficiency_hash = self._load(
            request.evidence_sufficiency_artifact_id,
            "EvidenceSufficiencyReport",
            EvidenceSufficiencyReport,
        )
        self._require_company_as_of(request.company_id, request.as_of, tree)
        self._require_company_as_of(request.company_id, request.as_of, sufficiency)
        if sufficiency.status is not InstitutionalArtifactStatus.READY:
            raise ValueError("forecast requires READY evidence sufficiency")
        input_node_ids = {
            item.node_id for item in tree.draft.nodes if item.operation is DriverOperation.INPUT
        }
        reference_periods: tuple[Any, ...] | None = None
        scenario_packs: list[ForecastScenarioPack] = []
        for scenario in sorted(request.scenarios, key=lambda item: item.scenario.value):
            grouped: dict[Any, dict[str, Decimal]] = defaultdict(dict)
            claim_ids: set[str] = set()
            evidence_ids: set[str] = set()
            notes: set[str] = set()
            for item in scenario.input_values:
                if item.node_id not in input_node_ids:
                    raise ValueError(
                        f"forecast assumption targets a non-input driver: {item.node_id}"
                    )
                if item.period_end <= request.as_of.date():
                    raise ValueError("forecast periods must be later than the research as_of date")
                grouped[item.period_end][item.node_id] = item.value
                claim_ids.update(item.claim_ids)
                evidence_ids.update(item.evidence_ids)
                notes.add(item.provenance_note)
                if item.provenance is DriverAssumptionProvenance.EVIDENCE:
                    gaps = self._lineage_gap_codes(
                        sufficiency,
                        sorted(item.claim_ids),
                        sorted(item.evidence_ids),
                    )
                    if gaps:
                        raise ValueError(
                            "evidence-based forecast assumption is not sufficiently supported"
                        )
            periods = tuple(sorted(grouped))
            if not periods:
                raise ValueError("forecast scenario has no future periods")
            if reference_periods is None:
                reference_periods = periods
            elif periods != reference_periods:
                raise ValueError("all forecast scenarios must use identical forecast periods")
            built_periods = []
            for period_end in periods:
                if set(grouped[period_end]) != input_node_ids:
                    raise ValueError("every forecast period requires every input driver")
                evaluated = evaluate_driver_period(tree, grouped[period_end])
                built_periods.append(forecast_period_from_nodes(tree, evaluated, period_end))
            scenario_packs.append(
                ForecastScenarioPack(
                    scenario=scenario.scenario,
                    periods=built_periods,
                    assumption_claim_ids=sorted(claim_ids),
                    assumption_evidence_ids=sorted(evidence_ids),
                    assumption_notes=sorted(notes),
                    created_at=request.as_of,
                )
            )
        forecast_id = "forecast-pack:" + content_hash(
            {
                "tree_hash": tree_hash,
                "sufficiency_hash": sufficiency_hash,
                "scenarios": [item.model_dump(mode="json") for item in scenario_packs],
            }
        )
        pack = ForecastPack(
            forecast_id=forecast_id,
            company_id=request.company_id,
            as_of=request.as_of,
            driver_tree_artifact_id=request.driver_tree_artifact_id,
            driver_tree_object_hash=tree_hash,
            forecast_template=tree.draft.forecast_template,
            status=InstitutionalArtifactStatus.READY,
            scenarios=scenario_packs,
            blocking_codes=[],
            source_artifact_ids=sorted(
                {request.driver_tree_artifact_id, request.evidence_sufficiency_artifact_id}
            ),
            source_object_hashes=sorted({tree_hash, sufficiency_hash}),
            created_at=request.as_of,
        )
        self._persist(
            pack,
            artifact_type="ForecastPack",
            artifact_id=f"ForecastPack:{pack.forecast_id}",
            input_hashes=pack.source_object_hashes,
            checkpoint_scope="institutional-forecast",
            checkpoint_key=request.company_id,
            checkpoint_status=pack.status.value,
        )
        return pack

    def build_valuation(self, request: ValuationBuildRequest) -> ValuationPack:
        forecast, forecast_hash = self._load(
            request.forecast_pack_artifact_id,
            "ForecastPack",
            ForecastPack,
        )
        sufficiency, sufficiency_hash = self._load(
            request.evidence_sufficiency_artifact_id,
            "EvidenceSufficiencyReport",
            EvidenceSufficiencyReport,
        )
        company, company_hash = self._load(
            request.company_economics_artifact_id,
            "CompanyEconomicsProfile",
            CompanyEconomicsProfile,
        )
        self._require_company_as_of(request.company_id, request.as_of, forecast)
        self._require_company_as_of(request.company_id, request.as_of, sufficiency)
        self._require_company_as_of(request.company_id, request.as_of, company)
        if forecast.status is not InstitutionalArtifactStatus.READY:
            raise ValueError("valuation requires READY forecast")
        if sufficiency.status is not InstitutionalArtifactStatus.READY:
            raise ValueError("valuation requires READY evidence sufficiency")
        if company.status is not InstitutionalArtifactStatus.READY:
            raise ValueError("valuation requires READY company economics")
        if request.archetype is not company.draft.archetype:
            raise ValueError("valuation archetype must match CompanyEconomicsProfile")
        forecast_by_scenario = {item.scenario: item for item in forecast.scenarios}
        market_price = None
        market_price_source_id: str | None = None
        market_price_source_hash: str | None = None
        if request.market_price_anchor is not None:
            anchor = request.market_price_anchor
            source = self.state.artifact_record(anchor.source_artifact_id)
            if (
                source is None
                or str(source["object_hash"]) != anchor.source_object_hash
                or not self.objects.verify(anchor.source_object_hash)
            ):
                raise ValueError("market price anchor source artifact is unavailable or drifted")
            market_price = anchor.price
            market_price_source_id = anchor.source_artifact_id
            market_price_source_hash = anchor.source_object_hash
        results: list[ValuationScenarioResult] = []
        claim_ids: set[str] = set()
        evidence_ids: set[str] = set()
        assumption_by_scenario = {item.scenario: item for item in request.scenario_assumptions}
        for scenario in ForecastScenario:
            assumption = assumption_by_scenario[scenario]
            self._validate_valuation_method(request.archetype, assumption.method)
            self._validate_valuation_template(forecast.forecast_template, assumption.method)
            if assumption.assumption_claim_ids or assumption.assumption_evidence_ids:
                gaps = self._lineage_gap_codes(
                    sufficiency,
                    sorted(assumption.assumption_claim_ids),
                    sorted(assumption.assumption_evidence_ids),
                )
                if gaps:
                    raise ValueError("valuation assumption lineage is not sufficiently supported")
            claim_ids.update(assumption.assumption_claim_ids)
            evidence_ids.update(assumption.assumption_evidence_ids)
            periods = forecast_by_scenario[scenario].periods
            enterprise_value: Decimal | None
            if assumption.method is ValuationMethod.DCF_FCFF:
                enterprise_value, equity_value = dcf_fcff_value(periods, assumption)
            elif assumption.method is ValuationMethod.MID_CYCLE_NORMALIZED:
                if assumption.normalized_metric is None or assumption.valuation_multiple is None:
                    raise ValueError("mid-cycle valuation requires normalized metric and multiple")
                enterprise_value = assumption.normalized_metric * assumption.valuation_multiple
                equity_value = enterprise_value - assumption.net_debt
            else:
                if assumption.explicit_equity_value is None:
                    raise ValueError(
                        f"{assumption.method.value} requires explicit equity value in v1"
                    )
                equity_value = assumption.explicit_equity_value
                enterprise_value = equity_value + assumption.net_debt
            per_share = equity_value / assumption.shares_outstanding
            expected_return = (
                per_share / market_price - Decimal("1") if market_price is not None else None
            )
            results.append(
                ValuationScenarioResult(
                    scenario=scenario,
                    method=assumption.method,
                    enterprise_value=enterprise_value,
                    equity_value=equity_value,
                    per_share_value=per_share,
                    expected_return=expected_return,
                    created_at=request.as_of,
                )
            )
        results.sort(key=lambda item: item.scenario.value)
        expectations = self._market_implied_expectations(
            request,
            forecast_by_scenario[ForecastScenario.BASE].periods,
            assumption_by_scenario[ForecastScenario.BASE],
            results,
        )
        sensitivity = self._sensitivity_table(
            request,
            forecast_by_scenario[ForecastScenario.BASE].periods,
            assumption_by_scenario[ForecastScenario.BASE],
        )
        values = [item.per_share_value for item in results]
        valuation_id = "valuation-pack:" + content_hash(
            {
                "forecast_hash": forecast_hash,
                "sufficiency_hash": sufficiency_hash,
                "company_hash": company_hash,
                "request": request.model_dump(mode="json", exclude={"created_at"}),
                "results": [item.model_dump(mode="json") for item in results],
                "expectations": [item.model_dump(mode="json") for item in expectations],
                "sensitivity": [item.model_dump(mode="json") for item in sensitivity],
            }
        )
        valuation_source_ids = {
            request.forecast_pack_artifact_id,
            request.evidence_sufficiency_artifact_id,
            request.company_economics_artifact_id,
        }
        valuation_source_hashes = {forecast_hash, sufficiency_hash, company_hash}
        if market_price_source_id is not None and market_price_source_hash is not None:
            valuation_source_ids.add(market_price_source_id)
            valuation_source_hashes.add(market_price_source_hash)
        pack = ValuationPack(
            valuation_id=valuation_id,
            company_id=request.company_id,
            as_of=request.as_of,
            archetype=request.archetype,
            forecast_pack_artifact_id=request.forecast_pack_artifact_id,
            forecast_pack_object_hash=forecast_hash,
            status=InstitutionalArtifactStatus.READY,
            results=results,
            value_range_low=min(values),
            value_range_high=max(values),
            market_implied_expectations=expectations,
            sensitivity_table=sensitivity,
            market_price_anchor=request.market_price_anchor,
            assumption_claim_ids=sorted(claim_ids),
            assumption_evidence_ids=sorted(evidence_ids),
            invalidation_conditions=request.invalidation_conditions,
            blocking_codes=[],
            source_artifact_ids=sorted(valuation_source_ids),
            source_object_hashes=sorted(valuation_source_hashes),
            created_at=request.as_of,
        )
        self._persist(
            pack,
            artifact_type="ValuationPack",
            artifact_id=f"ValuationPack:{pack.valuation_id}",
            input_hashes=pack.source_object_hashes,
            checkpoint_scope="institutional-valuation",
            checkpoint_key=request.company_id,
            checkpoint_status=pack.status.value,
        )
        return pack

    def build_bundle(self, request: FundamentalModelBundleBuildRequest) -> FundamentalModelBundle:
        components: list[tuple[str, str, BaseModel, str]] = []
        specs: tuple[tuple[str, str, type[BaseModel]], ...] = (
            (
                request.evidence_sufficiency_artifact_id,
                "EvidenceSufficiencyReport",
                EvidenceSufficiencyReport,
            ),
            (request.industry_profile_artifact_id, "IndustryProfile", IndustryProfile),
            (
                request.company_economics_artifact_id,
                "CompanyEconomicsProfile",
                CompanyEconomicsProfile,
            ),
            (request.driver_tree_artifact_id, "DriverTree", DriverTree),
            (request.forecast_pack_artifact_id, "ForecastPack", ForecastPack),
            (request.valuation_pack_artifact_id, "ValuationPack", ValuationPack),
        )
        for artifact_id, artifact_type, model_type in specs:
            model, object_hash = self._load(artifact_id, artifact_type, model_type)
            self._require_company_as_of(request.company_id, request.as_of, model)
            components.append((artifact_id, artifact_type, model, object_hash))
        evidence_report = components[0][2]
        industry = components[1][2]
        company = components[2][2]
        forecast = components[4][2]
        valuation = components[5][2]
        assert isinstance(evidence_report, EvidenceSufficiencyReport)
        assert isinstance(industry, IndustryProfile)
        assert isinstance(company, CompanyEconomicsProfile)
        assert isinstance(forecast, ForecastPack)
        assert isinstance(valuation, ValuationPack)
        blocking = []
        for label, status in (
            ("EVIDENCE", evidence_report.status),
            ("INDUSTRY", industry.status),
            ("COMPANY", company.status),
            ("FORECAST", forecast.status),
            ("VALUATION", valuation.status),
        ):
            if status is not InstitutionalArtifactStatus.READY:
                blocking.append(f"{label}_NOT_READY")
        if valuation.archetype is not company.draft.archetype:
            blocking.append("VALUATION_ARCHETYPE_DRIFT")
        warning_codes = sorted(set(industry.warning_codes + company.warning_codes))
        artifact_hashes = {
            artifact_id: object_hash for artifact_id, _, _, object_hash in components
        }
        claim_ids = sorted(
            set(evidence_report.material_claim_ids)
            | set(industry.claim_ids)
            | set(company.claim_ids)
            | set(valuation.assumption_claim_ids)
        )
        evidence_ids = sorted(
            {
                item.evidence_id
                for assessment in evidence_report.assessments
                for item in assessment.quality_vectors
            }
            | set(industry.evidence_ids)
            | set(company.evidence_ids)
            | set(valuation.assumption_evidence_ids)
        )
        bundle_id = "fundamental-model:" + content_hash(
            {
                "company_id": request.company_id,
                "as_of": request.as_of,
                "artifact_hashes": artifact_hashes,
            }
        )
        bundle = FundamentalModelBundle(
            bundle_id=bundle_id,
            company_id=request.company_id,
            as_of=request.as_of,
            status=(
                InstitutionalArtifactStatus.NEEDS_INFO
                if blocking
                else InstitutionalArtifactStatus.READY
            ),
            evidence_sufficiency_artifact_id=request.evidence_sufficiency_artifact_id,
            industry_profile_artifact_id=request.industry_profile_artifact_id,
            company_economics_artifact_id=request.company_economics_artifact_id,
            driver_tree_artifact_id=request.driver_tree_artifact_id,
            forecast_pack_artifact_id=request.forecast_pack_artifact_id,
            valuation_pack_artifact_id=request.valuation_pack_artifact_id,
            artifact_object_hashes=dict(sorted(artifact_hashes.items())),
            blocking_codes=sorted(set(blocking)),
            warning_codes=warning_codes,
            evidence_ids=evidence_ids,
            claim_ids=claim_ids,
            created_at=request.as_of,
        )
        self._persist(
            bundle,
            artifact_type="FundamentalModelBundle",
            artifact_id=f"FundamentalModelBundle:{bundle.bundle_id}",
            input_hashes=sorted(artifact_hashes.values()),
            checkpoint_scope="institutional-fundamental-model",
            checkpoint_key=request.company_id,
            checkpoint_status=bundle.status.value,
        )
        return bundle

    def build_decision_context(
        self,
        request: InstitutionalDecisionContextBuildRequest,
    ) -> InstitutionalDecisionContext:
        bundle, bundle_hash = self._load(
            request.fundamental_model_bundle_artifact_id,
            "FundamentalModelBundle",
            FundamentalModelBundle,
        )
        self._require_company_as_of(request.company_id, request.as_of, bundle)
        if bundle.status is not InstitutionalArtifactStatus.READY:
            raise ValueError("institutional decision context requires a READY model bundle")
        driver, _ = self._load(bundle.driver_tree_artifact_id, "DriverTree", DriverTree)
        driver_ids = {item.node_id for item in driver.draft.nodes}
        if not set(request.draft.key_driver_ids).issubset(driver_ids):
            raise ValueError("institutional decision context references unknown driver ids")
        claim_ids, evidence_ids = self._collect_lineage(request.draft)
        if not set(claim_ids).issubset(bundle.claim_ids):
            raise ValueError("institutional decision context claims escape the model bundle")
        if not set(evidence_ids).issubset(bundle.evidence_ids):
            raise ValueError("institutional decision context evidence escapes the model bundle")
        context_id = "institutional-context:" + content_hash(
            {
                "bundle_hash": bundle_hash,
                "company_id": request.company_id,
                "as_of": request.as_of,
                "draft": request.draft,
            }
        )
        context = InstitutionalDecisionContext(
            context_id=context_id,
            company_id=request.company_id,
            as_of=request.as_of,
            fundamental_model_bundle_artifact_id=request.fundamental_model_bundle_artifact_id,
            fundamental_model_bundle_object_hash=bundle_hash,
            draft=request.draft,
            claim_ids=claim_ids,
            evidence_ids=evidence_ids,
            source_artifact_ids=[request.fundamental_model_bundle_artifact_id],
            source_object_hashes=[bundle_hash],
            created_at=request.as_of,
        )
        self._persist(
            context,
            artifact_type="InstitutionalDecisionContext",
            artifact_id=f"InstitutionalDecisionContext:{context.context_id}",
            input_hashes=[bundle_hash],
            checkpoint_scope="institutional-decision-context",
            checkpoint_key=request.company_id,
            checkpoint_status="READY",
        )
        return context

    def finalize(self, request: InstitutionalResearchFinalizeRequest) -> FundamentalModelBundle:
        if request.evidence_sufficiency.frozen_evidence_pack_artifact_id == "":
            raise ValueError("institutional finalize requires a frozen evidence pack")
        evidence = self.run_evidence_sufficiency(request.evidence_sufficiency)
        evidence_artifact = f"EvidenceSufficiencyReport:{evidence.report_id}"
        industry = self.build_industry_profile(
            IndustryProfileBuildRequest(
                company_id=request.company_id,
                as_of=request.as_of,
                evidence_sufficiency_artifact_id=evidence_artifact,
                draft=request.industry_profile,
                created_at=request.as_of,
            )
        )
        company = self.build_company_economics(
            CompanyEconomicsBuildRequest(
                company_id=request.company_id,
                as_of=request.as_of,
                evidence_sufficiency_artifact_id=evidence_artifact,
                draft=request.company_economics,
                created_at=request.as_of,
            )
        )
        industry_artifact = f"IndustryProfile:{industry.profile_id}"
        company_artifact = f"CompanyEconomicsProfile:{company.profile_id}"
        if industry.status is not InstitutionalArtifactStatus.READY:
            raise ValueError(
                "institutional finalize cannot continue with incomplete IndustryProfile"
            )
        if company.status is not InstitutionalArtifactStatus.READY:
            raise ValueError(
                "institutional finalize cannot continue with incomplete CompanyEconomicsProfile"
            )
        driver = self.build_driver_tree(
            DriverTreeBuildRequest(
                company_id=request.company_id,
                as_of=request.as_of,
                industry_profile_artifact_id=industry_artifact,
                company_economics_artifact_id=company_artifact,
                draft=request.driver_tree,
                created_at=request.as_of,
            )
        )
        driver_artifact = f"DriverTree:{driver.tree_id}"
        forecast = self.build_forecast(
            ForecastBuildRequest(
                company_id=request.company_id,
                as_of=request.as_of,
                driver_tree_artifact_id=driver_artifact,
                evidence_sufficiency_artifact_id=evidence_artifact,
                scenarios=request.forecast_scenarios,
                created_at=request.as_of,
            )
        )
        forecast_artifact = f"ForecastPack:{forecast.forecast_id}"
        valuation = self.build_valuation(
            ValuationBuildRequest(
                company_id=request.company_id,
                as_of=request.as_of,
                archetype=request.valuation_archetype,
                forecast_pack_artifact_id=forecast_artifact,
                evidence_sufficiency_artifact_id=evidence_artifact,
                company_economics_artifact_id=company_artifact,
                market_price_anchor=request.market_price_anchor,
                scenario_assumptions=request.valuation_scenarios,
                invalidation_conditions=request.valuation_invalidation_conditions,
                created_at=request.as_of,
            )
        )
        return self.build_bundle(
            FundamentalModelBundleBuildRequest(
                company_id=request.company_id,
                as_of=request.as_of,
                evidence_sufficiency_artifact_id=evidence_artifact,
                industry_profile_artifact_id=industry_artifact,
                company_economics_artifact_id=company_artifact,
                driver_tree_artifact_id=driver_artifact,
                forecast_pack_artifact_id=forecast_artifact,
                valuation_pack_artifact_id=f"ValuationPack:{valuation.valuation_id}",
                created_at=request.as_of,
            )
        )

    def status(self, company_id: str) -> dict[str, Any]:
        checkpoint = self.state.get_checkpoint("institutional-fundamental-model", company_id)
        if checkpoint is None:
            return {
                "company_id": company_id,
                "status": "NOT_RUN",
                "artifact_id": None,
                "broker_execution_allowed": False,
            }
        artifact_id = str(checkpoint["cursor"].get("artifact_id", ""))
        return {
            "company_id": company_id,
            "status": checkpoint["status"],
            "artifact_id": artifact_id or None,
            "object_hash": checkpoint["object_hash"],
            "broker_execution_allowed": False,
        }

    def audit(self, artifact_id: str) -> dict[str, Any]:
        findings: list[str] = []
        record = self.state.artifact_record(artifact_id)
        if record is None:
            return {
                "status": "FAIL",
                "artifact_id": None,
                "finding_codes": ["INSTITUTIONAL_ARTIFACT_NOT_FOUND"],
                "broker_execution_allowed": False,
            }
        object_hash = str(record["object_hash"])
        if not self.objects.verify(object_hash):
            findings.append("INSTITUTIONAL_ARTIFACT_OBJECT_MISSING")
        artifact_type = str(record["type"])
        model_map: dict[str, type[BaseModel]] = {
            "EvidenceSufficiencyReport": EvidenceSufficiencyReport,
            "IndustryProfile": IndustryProfile,
            "CompanyEconomicsProfile": CompanyEconomicsProfile,
            "DriverTree": DriverTree,
            "ForecastPack": ForecastPack,
            "ValuationPack": ValuationPack,
            "FundamentalModelBundle": FundamentalModelBundle,
            "InstitutionalDecisionContext": InstitutionalDecisionContext,
        }
        model_type = model_map.get(artifact_type)
        if model_type is None:
            findings.append("INSTITUTIONAL_ARTIFACT_TYPE_UNSUPPORTED")
        elif not findings:
            try:
                model = model_type.model_validate_json(self.objects.get_bytes(object_hash))
                findings.extend(self._audit_model(model, record))
            except (OSError, TypeError, ValueError):
                findings.append("INSTITUTIONAL_ARTIFACT_CONTENT_INVALID")
        return {
            "status": "PASS" if not findings else "FAIL",
            "artifact_id": artifact_id,
            "object_hash": object_hash,
            "finding_codes": sorted(set(findings)),
            "broker_execution_allowed": False,
        }

    def _audit_model(self, model: BaseModel, record: dict[str, Any]) -> list[str]:
        findings: list[str] = []
        if isinstance(model, FundamentalModelBundle):
            for artifact_id, expected_hash in model.artifact_object_hashes.items():
                component = self.state.artifact_record(artifact_id)
                if component is None or str(component["object_hash"]) != expected_hash:
                    findings.append("FUNDAMENTAL_COMPONENT_LINEAGE_DRIFT")
                    continue
                if not self.objects.verify(expected_hash):
                    findings.append("FUNDAMENTAL_COMPONENT_OBJECT_MISSING")
            if sorted(record["input_hashes"]) != sorted(model.artifact_object_hashes.values()):
                findings.append("FUNDAMENTAL_BUNDLE_INPUT_HASH_DRIFT")
        else:
            source_hashes = getattr(model, "source_object_hashes", None)
            if source_hashes is not None and sorted(record["input_hashes"]) != sorted(
                source_hashes
            ):
                findings.append("INSTITUTIONAL_INPUT_HASH_DRIFT")
            source_ids = getattr(model, "source_artifact_ids", [])
            actual_source_hashes: list[str] = []
            for source_id in source_ids:
                source = self.state.artifact_record(source_id)
                if source is None:
                    findings.append("INSTITUTIONAL_SOURCE_LINEAGE_DRIFT")
                    continue
                source_hash = str(source["object_hash"])
                actual_source_hashes.append(source_hash)
                if not self.objects.verify(source_hash):
                    findings.append("INSTITUTIONAL_SOURCE_LINEAGE_DRIFT")
            if source_hashes is not None and sorted(actual_source_hashes) != sorted(source_hashes):
                findings.append("INSTITUTIONAL_SOURCE_HASH_MAPPING_DRIFT")
        return findings

    def _persist(
        self,
        model: BaseModel,
        *,
        artifact_type: str,
        artifact_id: str,
        input_hashes: list[str],
        checkpoint_scope: str,
        checkpoint_key: str,
        checkpoint_status: str,
    ) -> str:
        payload = model.model_dump(mode="json")
        ref = self.objects.put_json(payload)
        hashes = sorted(set(input_hashes))
        existing = self.state.artifact_record(artifact_id)
        if existing is not None:
            if (
                str(existing["type"]) != artifact_type
                or str(existing["schema_version"]) != str(payload["schema_version"])
                or str(existing["object_hash"]) != ref.sha256
                or sorted(existing["input_hashes"]) != hashes
            ):
                raise ValueError(f"{artifact_type} identity collision")
        else:
            self.state.register_artifact(
                artifact_id=artifact_id,
                artifact_type=artifact_type,
                schema_version=str(payload["schema_version"]),
                object_hash=ref.sha256,
                input_hashes=hashes,
            )
        self.state.set_checkpoint(
            scope_type=checkpoint_scope,
            scope_key=checkpoint_key,
            cursor={"artifact_id": artifact_id},
            status=checkpoint_status,
            object_hash=ref.sha256,
        )
        return artifact_id

    def _load(self, artifact_id: str, artifact_type: str, model_type: type[T]) -> tuple[T, str]:
        record = self.state.artifact_record(artifact_id)
        if record is None or str(record["type"]) != artifact_type:
            raise ValueError(f"unknown {artifact_type} artifact")
        object_hash = str(record["object_hash"])
        if not self.objects.verify(object_hash):
            raise ValueError(f"{artifact_type} object is unavailable")
        return model_type.model_validate_json(self.objects.get_bytes(object_hash)), object_hash

    @staticmethod
    def _require_company_as_of(company_id: str, as_of, model: BaseModel) -> None:
        if getattr(model, "company_id", None) != company_id:
            raise ValueError("institutional artifact company mismatch")
        if getattr(model, "as_of", None) != as_of:
            raise ValueError("institutional artifact as_of mismatch")

    def _assess_claim(
        self,
        *,
        bundle: ClaimEvidenceBundle,
        request: EvidenceSufficiencyRequest,
        pack: FrozenEvidencePack,
        quality_by_evidence: dict[str, EvidenceQualityVector],
        prior_assessments: dict[str, ClaimSufficiencyAssessment],
    ) -> ClaimSufficiencyAssessment:
        claim_id = bundle.claim.claim_id
        institutional_type = self._institutional_claim_type(
            bundle.claim.claim_type,
            request.claim_type_overrides.get(claim_id),
        )
        links = [
            link
            for link in bundle.links
            if link.evidence_id in pack.evidence_ids and link.weight > 0
        ]
        relation_ids = {
            relation: sorted(link.evidence_id for link in links if link.relation is relation)
            for relation in EvidenceRelation
        }
        support_groups = self._qualifying_independence_groups(
            institutional_type,
            relation_ids[EvidenceRelation.SUPPORT],
            quality_by_evidence,
        )
        refute_groups = self._qualifying_independence_groups(
            institutional_type,
            relation_ids[EvidenceRelation.REFUTE],
            quality_by_evidence,
        )
        unresolved_conflicts = []
        if (
            bundle.conflict is not None
            and bundle.conflict.resolution_status is ConflictResolutionStatus.OPEN
            and bundle.conflict.conflict_id in pack.open_conflict_ids
        ):
            unresolved_conflicts.append(bundle.conflict.conflict_id)
        dependency_ids = sorted(
            edge.prerequisite_claim_id
            for edge in request.dependencies
            if edge.dependent_claim_id == claim_id
        )
        missing_dependencies = sorted(
            claim
            for claim in dependency_ids
            if claim not in prior_assessments
            or prior_assessments[claim].state is not EvidenceSufficiencyState.SUPPORTED
        )
        threshold = 2 if institutional_type in _TWO_SOURCE_TYPES else 1
        dependency_graph_missing = (
            institutional_type in _DEPENDENCY_REQUIRED_TYPES and not dependency_ids
        )
        reasons: list[str] = []
        if claim_id in request.not_applicable_claim_ids:
            state = EvidenceSufficiencyState.NOT_APPLICABLE
        elif unresolved_conflicts or (support_groups and refute_groups):
            state = EvidenceSufficiencyState.CONFLICTED
            reasons.append("MATERIAL_EVIDENCE_CONFLICT")
        elif len(refute_groups) >= threshold:
            state = EvidenceSufficiencyState.REFUTED
        else:
            if (
                len(support_groups) >= threshold
                and not missing_dependencies
                and not dependency_graph_missing
            ):
                state = EvidenceSufficiencyState.SUPPORTED
                if institutional_type is InstitutionalClaimType.MANAGEMENT_ASSERTION:
                    reasons.append("MANAGEMENT_ASSERTION_ONLY_PROVES_ASSERTION_WAS_MADE")
            else:
                state = EvidenceSufficiencyState.INSUFFICIENT
                if len(support_groups) < threshold:
                    reasons.append("INDEPENDENT_SUPPORT_INSUFFICIENT")
                if missing_dependencies:
                    reasons.append("CLAIM_DEPENDENCY_INSUFFICIENT")
                if dependency_graph_missing:
                    reasons.append("CLAIM_DEPENDENCY_REQUIRED")
                if relation_ids[EvidenceRelation.SUPPORT] and not support_groups:
                    reasons.append("SUPPORT_EVIDENCE_QUALITY_INSUFFICIENT")
                if (
                    not relation_ids[EvidenceRelation.SUPPORT]
                    and not relation_ids[EvidenceRelation.REFUTE]
                ):
                    reasons.append("NO_DECISION_RELEVANT_EVIDENCE")
        return ClaimSufficiencyAssessment(
            claim_id=claim_id,
            legacy_claim_type=bundle.claim.claim_type,
            institutional_claim_type=institutional_type,
            state=state,
            support_evidence_ids=relation_ids[EvidenceRelation.SUPPORT],
            refute_evidence_ids=relation_ids[EvidenceRelation.REFUTE],
            context_evidence_ids=relation_ids[EvidenceRelation.CONTEXT],
            support_independence_groups=support_groups,
            refute_independence_groups=refute_groups,
            unresolved_conflict_ids=sorted(unresolved_conflicts),
            missing_dependency_claim_ids=sorted(set(missing_dependencies)),
            quality_vectors=sorted(quality_by_evidence.values(), key=lambda item: item.evidence_id),
            reason_codes=sorted(set(reasons)),
            created_at=pack.as_of,
        )

    @staticmethod
    def _institutional_claim_type(
        legacy: ClaimType,
        override: InstitutionalClaimType | None,
    ) -> InstitutionalClaimType:
        if override is not None:
            return override
        if legacy is ClaimType.FACT:
            return InstitutionalClaimType.OBSERVED_FACT
        return InstitutionalClaimType.INTERPRETATION

    def _quality_vector(
        self,
        bundle: ClaimEvidenceBundle,
        reviewer_status: ReviewerStatus,
        claim_type: InstitutionalClaimType,
        evidence: Evidence,
        snapshot: SourceSnapshot,
        metadata: SourceEpistemicMetadata,
        pit_status: PointInTimeStatus | None,
        as_of,
    ) -> EvidenceQualityVector:
        return EvidenceQualityVector(
            evidence_id=evidence.evidence_id,
            evidence_grade=evidence.evidence_grade,
            fact_status=evidence.fact_status,
            pit_status=pit_status,
            authority_tier=metadata.authority_tier,
            directness=self._directness(claim_type),
            independence_group=metadata.source_independence_group,
            freshness=self._freshness(evidence, metadata, as_of),
            scope_match=self._scope_match(bundle, evidence),
            extraction_confidence=self._extraction_confidence(
                evidence.fact_status, reviewer_status
            ),
            snapshot_id=snapshot.snapshot_id,
            metadata_basis=metadata.metadata_basis,
            created_at=as_of,
        )

    @staticmethod
    def _derived_metadata(
        snapshot: SourceSnapshot,
        grade: EvidenceGrade,
    ) -> SourceEpistemicMetadata:
        authority = InstitutionalResearchService._authority_tier(
            snapshot.source_id,
            grade,
        )
        return SourceEpistemicMetadata(
            snapshot_id=snapshot.snapshot_id,
            authority_tier=authority,
            source_independence_group=snapshot.source_id,
            first_publicly_available_at=None,
            system_ingested_at=snapshot.available_to_system_at,
            metadata_basis="CONSERVATIVE_DERIVED",
            created_at=snapshot.available_to_system_at,
        )

    @staticmethod
    def _validate_authority_compatibility(
        grade: EvidenceGrade,
        tier: EvidenceAuthorityTier,
    ) -> None:
        allowed = {
            EvidenceGrade.PRIMARY_OFFICIAL: {
                EvidenceAuthorityTier.A_STATUTORY_PRIMARY,
                EvidenceAuthorityTier.B_OFFICIAL_ADMIN_MACRO,
                EvidenceAuthorityTier.C_MARKET_INFRASTRUCTURE,
            },
            EvidenceGrade.PRIVATE_PRIMARY: {EvidenceAuthorityTier.D_ISSUER_INTERPRETIVE},
            EvidenceGrade.SECONDARY: {EvidenceAuthorityTier.E_SECONDARY_PROFESSIONAL},
            EvidenceGrade.COMMUNITY_LEAD: {EvidenceAuthorityTier.F_ALTERNATIVE_COMMUNITY},
        }[grade]
        if tier not in allowed:
            raise ValueError("epistemic authority tier is incompatible with evidence grade")

    @staticmethod
    def _authority_tier(source_id: str, grade: EvidenceGrade | None) -> EvidenceAuthorityTier:
        normalized = source_id.casefold()
        if grade is EvidenceGrade.COMMUNITY_LEAD:
            return EvidenceAuthorityTier.F_ALTERNATIVE_COMMUNITY
        if grade is EvidenceGrade.SECONDARY:
            return EvidenceAuthorityTier.E_SECONDARY_PROFESSIONAL
        if grade is EvidenceGrade.PRIVATE_PRIMARY:
            return EvidenceAuthorityTier.D_ISSUER_INTERPRETIVE
        if any(token in normalized for token in ("nbs", "pboc", "safe", "chinabond", "ministry")):
            return EvidenceAuthorityTier.B_OFFICIAL_ADMIN_MACRO
        if any(
            token in normalized
            for token in ("market-reference", "baostock", "calendar", "exchange-rule", "trading")
        ):
            return EvidenceAuthorityTier.C_MARKET_INFRASTRUCTURE
        return EvidenceAuthorityTier.A_STATUTORY_PRIMARY

    @staticmethod
    def _directness(claim_type: InstitutionalClaimType) -> EvidenceDirectness:
        if claim_type is InstitutionalClaimType.OBSERVED_FACT:
            return EvidenceDirectness.DIRECT
        if claim_type is InstitutionalClaimType.DERIVED_FACT:
            return EvidenceDirectness.DERIVED
        if claim_type is InstitutionalClaimType.MANAGEMENT_ASSERTION:
            return EvidenceDirectness.ASSERTION
        return EvidenceDirectness.INTERPRETIVE

    @staticmethod
    def _freshness(
        evidence: Evidence,
        metadata: SourceEpistemicMetadata,
        as_of,
    ) -> EvidenceFreshness:
        if metadata.effective_to is not None and metadata.effective_to < as_of:
            return EvidenceFreshness.STALE
        if evidence.valid_to is not None and evidence.valid_to < as_of:
            return EvidenceFreshness.STALE
        age = as_of - evidence.available_to_system_at
        if age <= timedelta(days=180):
            return EvidenceFreshness.CURRENT
        if age <= timedelta(days=730):
            return EvidenceFreshness.AGING
        return EvidenceFreshness.UNKNOWN

    @staticmethod
    def _scope_match(bundle: ClaimEvidenceBundle, evidence: Evidence) -> EvidenceScopeMatch:
        if bundle.claim.subject_id in evidence.entity_ids:
            return EvidenceScopeMatch.EXACT
        if evidence.entity_ids:
            return EvidenceScopeMatch.PARTIAL
        return EvidenceScopeMatch.UNKNOWN

    @staticmethod
    def _extraction_confidence(
        fact_status: FactStatus,
        reviewer_status: ReviewerStatus,
    ) -> EvidenceExtractionConfidence:
        if reviewer_status is ReviewerStatus.HUMAN_APPROVED:
            return EvidenceExtractionConfidence.VERIFIED
        if fact_status is FactStatus.DIRECT and reviewer_status is ReviewerStatus.AUTO_VALIDATED:
            return EvidenceExtractionConfidence.HIGH
        if fact_status is FactStatus.DIRECT:
            return EvidenceExtractionConfidence.HIGH
        if fact_status is FactStatus.INFERRED:
            return EvidenceExtractionConfidence.MEDIUM
        if fact_status is FactStatus.CONFLICTED:
            return EvidenceExtractionConfidence.LOW
        return EvidenceExtractionConfidence.UNKNOWN

    @staticmethod
    def _qualifying_independence_groups(
        claim_type: InstitutionalClaimType,
        evidence_ids: list[str],
        quality_by_evidence: dict[str, EvidenceQualityVector],
    ) -> list[str]:
        groups: set[str] = set()
        for evidence_id in evidence_ids:
            quality = quality_by_evidence[evidence_id]
            if quality.authority_tier is EvidenceAuthorityTier.F_ALTERNATIVE_COMMUNITY:
                continue
            if quality.pit_status not in {
                PointInTimeStatus.CERTIFIED,
                PointInTimeStatus.DOCUMENT_RECONSTRUCTED,
            }:
                continue
            if quality.freshness is EvidenceFreshness.STALE:
                continue
            if quality.extraction_confidence in {
                EvidenceExtractionConfidence.LOW,
                EvidenceExtractionConfidence.UNKNOWN,
            }:
                continue
            if claim_type in {
                InstitutionalClaimType.OBSERVED_FACT,
                InstitutionalClaimType.DERIVED_FACT,
            } and quality.authority_tier not in {
                EvidenceAuthorityTier.A_STATUTORY_PRIMARY,
                EvidenceAuthorityTier.B_OFFICIAL_ADMIN_MACRO,
                EvidenceAuthorityTier.C_MARKET_INFRASTRUCTURE,
            }:
                continue
            if claim_type is InstitutionalClaimType.OBSERVED_FACT and (
                quality.scope_match is not EvidenceScopeMatch.EXACT
            ):
                continue
            groups.add(quality.independence_group)
        return sorted(groups)

    @staticmethod
    def _claim_dependency_order(
        claim_ids: list[str],
        edges: list[ClaimDependencyEdge],
    ) -> list[str]:
        indegree = {claim_id: 0 for claim_id in claim_ids}
        children: dict[str, list[str]] = defaultdict(list)
        for edge in edges:
            indegree[edge.dependent_claim_id] += 1
            children[edge.prerequisite_claim_id].append(edge.dependent_claim_id)
        ready = deque(sorted(claim_id for claim_id, value in indegree.items() if value == 0))
        result: list[str] = []
        while ready:
            current = ready.popleft()
            result.append(current)
            for child in sorted(children[current]):
                indegree[child] -= 1
                if indegree[child] == 0:
                    insert_at = 0
                    while insert_at < len(ready) and ready[insert_at] < child:
                        insert_at += 1
                    ready.insert(insert_at, child)
        if len(result) != len(claim_ids):
            raise ValueError("claim dependency graph contains a cycle")
        return result

    @staticmethod
    def _collect_lineage(model: BaseModel) -> tuple[list[str], list[str]]:
        claims: set[str] = set()
        evidence: set[str] = set()

        def visit(value: Any) -> None:
            if isinstance(value, BaseModel):
                visit(value.model_dump(mode="python", exclude_none=True))
            elif isinstance(value, dict):
                for key, child in value.items():
                    if key == "claim_ids" and isinstance(child, list):
                        claims.update(str(item) for item in child)
                    elif key == "evidence_ids" and isinstance(child, list):
                        evidence.update(str(item) for item in child)
                    else:
                        visit(child)
            elif isinstance(value, (list, tuple)):
                for child in value:
                    visit(child)

        visit(model)
        return sorted(claims), sorted(evidence)

    @staticmethod
    def _lineage_gap_codes(
        sufficiency: EvidenceSufficiencyReport,
        claim_ids: list[str],
        evidence_ids: list[str],
    ) -> list[str]:
        assessment_by_id = {item.claim_id: item for item in sufficiency.assessments}
        known_evidence = {
            item.evidence_id
            for assessment in sufficiency.assessments
            for item in assessment.quality_vectors
        }
        gaps: list[str] = []
        for claim_id in claim_ids:
            assessment = assessment_by_id.get(claim_id)
            if assessment is None:
                gaps.append(f"CLAIM_OUTSIDE_SUFFICIENCY:{claim_id}")
            elif assessment.state is not EvidenceSufficiencyState.SUPPORTED:
                gaps.append(f"CLAIM_NOT_SUPPORTED:{claim_id}")
        for evidence_id in evidence_ids:
            if evidence_id not in known_evidence:
                gaps.append(f"EVIDENCE_OUTSIDE_SUFFICIENCY:{evidence_id}")
        return sorted(gaps)

    @staticmethod
    def _forecast_template_for_archetype(archetype: CompanyArchetype) -> ForecastTemplate:
        return {
            CompanyArchetype.STABLE_OPERATING: ForecastTemplate.OPERATING_FCFF,
            CompanyArchetype.MULTI_SEGMENT: ForecastTemplate.OPERATING_FCFF,
            CompanyArchetype.CYCLICAL: ForecastTemplate.OPERATING_FCFF,
            CompanyArchetype.FINANCIAL_INSTITUTION: ForecastTemplate.FINANCIAL_RESIDUAL_INCOME,
            CompanyArchetype.ASSET_HEAVY_SPECIAL: ForecastTemplate.ASSET_NAV,
            CompanyArchetype.HIGH_GROWTH: ForecastTemplate.OPERATING_FCFF,
            CompanyArchetype.PRE_PROFIT_OPTION_LIKE: ForecastTemplate.PRE_PROFIT_SCENARIO,
        }[archetype]

    @staticmethod
    def _validate_valuation_method(archetype: CompanyArchetype, method: ValuationMethod) -> None:
        allowed = {
            CompanyArchetype.STABLE_OPERATING: {ValuationMethod.DCF_FCFF},
            CompanyArchetype.MULTI_SEGMENT: {ValuationMethod.SOTP},
            CompanyArchetype.CYCLICAL: {
                ValuationMethod.MID_CYCLE_NORMALIZED,
                ValuationMethod.DCF_FCFF,
            },
            CompanyArchetype.FINANCIAL_INSTITUTION: {ValuationMethod.RESIDUAL_INCOME},
            CompanyArchetype.ASSET_HEAVY_SPECIAL: {ValuationMethod.NAV_REPLACEMENT},
            CompanyArchetype.HIGH_GROWTH: {
                ValuationMethod.DCF_FCFF,
                ValuationMethod.SCENARIO_VALUE,
            },
            CompanyArchetype.PRE_PROFIT_OPTION_LIKE: {ValuationMethod.SCENARIO_VALUE},
        }[archetype]
        if method not in allowed:
            raise ValueError(
                f"valuation method {method.value} is not admitted for {archetype.value}"
            )

    @staticmethod
    def _validate_valuation_template(template: ForecastTemplate, method: ValuationMethod) -> None:
        admitted = {
            ForecastTemplate.OPERATING_FCFF: {
                ValuationMethod.DCF_FCFF,
                ValuationMethod.SOTP,
                ValuationMethod.MID_CYCLE_NORMALIZED,
                ValuationMethod.SCENARIO_VALUE,
            },
            ForecastTemplate.FINANCIAL_RESIDUAL_INCOME: {ValuationMethod.RESIDUAL_INCOME},
            ForecastTemplate.ASSET_NAV: {ValuationMethod.NAV_REPLACEMENT},
            ForecastTemplate.PRE_PROFIT_SCENARIO: {ValuationMethod.SCENARIO_VALUE},
        }[template]
        if method not in admitted:
            raise ValueError("valuation method is inconsistent with the forecast template")

    @staticmethod
    def _market_implied_expectations(
        request: ValuationBuildRequest,
        base_periods,
        base_assumption,
        results: list[ValuationScenarioResult],
    ) -> list[MarketImpliedExpectation]:
        if request.market_price_anchor is None:
            return [
                MarketImpliedExpectation(
                    expectation="IMPLIED_FCFF_SCALE",
                    scenario=ForecastScenario.BASE,
                    implied_value=None,
                    status=InstitutionalArtifactStatus.NEEDS_INFO,
                    reason_codes=["MARKET_PRICE_ANCHOR_REQUIRED"],
                    created_at=request.as_of,
                )
            ]
        base_result = next(item for item in results if item.scenario is ForecastScenario.BASE)
        target_equity = request.market_price_anchor.price * base_assumption.shares_outstanding
        target_enterprise = target_equity + base_assumption.net_debt
        expectations: list[MarketImpliedExpectation] = []
        if base_assumption.method is ValuationMethod.DCF_FCFF:
            growth = implied_terminal_growth(base_periods, base_assumption, target_enterprise)
            expectations.append(
                MarketImpliedExpectation(
                    expectation="IMPLIED_TERMINAL_GROWTH",
                    scenario=ForecastScenario.BASE,
                    implied_value=growth,
                    status=(
                        InstitutionalArtifactStatus.READY
                        if growth is not None
                        else InstitutionalArtifactStatus.NEEDS_INFO
                    ),
                    reason_codes=[] if growth is not None else ["IMPLIED_GROWTH_NOT_SOLVABLE"],
                    created_at=request.as_of,
                )
            )
        scale = (
            target_enterprise / base_result.enterprise_value
            if base_result.enterprise_value is not None and base_result.enterprise_value > 0
            else None
        )
        expectations.append(
            MarketImpliedExpectation(
                expectation="IMPLIED_FCFF_SCALE",
                scenario=ForecastScenario.BASE,
                implied_value=scale,
                status=(
                    InstitutionalArtifactStatus.READY
                    if scale is not None
                    else InstitutionalArtifactStatus.NEEDS_INFO
                ),
                reason_codes=[] if scale is not None else ["IMPLIED_SCALE_NOT_SOLVABLE"],
                created_at=request.as_of,
            )
        )
        return expectations

    @staticmethod
    def _sensitivity_table(
        request: ValuationBuildRequest,
        base_periods,
        base_assumption,
    ) -> list[ValuationSensitivityPoint]:
        if base_assumption.method is not ValuationMethod.DCF_FCFF:
            return []
        if base_assumption.discount_rate is None or base_assumption.terminal_growth is None:
            return []
        result: list[ValuationSensitivityPoint] = []
        for rate_shift in (Decimal("-0.01"), Decimal("0"), Decimal("0.01")):
            for growth_shift in (Decimal("-0.005"), Decimal("0"), Decimal("0.005")):
                rate = base_assumption.discount_rate + rate_shift
                growth = base_assumption.terminal_growth + growth_shift
                if rate <= 0 or rate <= growth:
                    continue
                changed = base_assumption.model_copy(
                    update={"discount_rate": rate, "terminal_growth": growth}
                )
                _, equity_value = dcf_fcff_value(base_periods, changed)
                result.append(
                    ValuationSensitivityPoint(
                        discount_rate=rate,
                        terminal_growth=growth,
                        per_share_value=equity_value / base_assumption.shares_outstanding,
                        created_at=request.as_of,
                    )
                )
        return sorted(result, key=lambda item: (item.discount_rate, item.terminal_growth))


__all__ = ["InstitutionalResearchService"]
