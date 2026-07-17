"""Safe SQLite indexes for frozen research artifacts."""

from __future__ import annotations

from datetime import UTC

from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.schemas import (
    BaseCasePack,
    FrozenEvidencePack,
    ResearchMemoArtifact,
    ResearchSkillRegistry,
    SpecialistDelta,
    SpecialistDiagnosticReport,
    SpecialistRoutePlan,
)


class ResearchRepository:
    def __init__(self, state: StateStore, object_store: ObjectStore) -> None:
        self.state = state
        self.object_store = object_store

    def get_evidence_pack(self, pack_id: str) -> FrozenEvidencePack | None:
        with self.state.connect() as connection:
            row = connection.execute(
                "SELECT object_hash FROM frozen_evidence_pack_index WHERE pack_id=?",
                (pack_id,),
            ).fetchone()
        if row is None:
            return None
        return FrozenEvidencePack.model_validate_json(
            self.object_store.get_bytes(str(row["object_hash"]))
        )

    def evidence_pack_object_hash(self, pack_id: str) -> str | None:
        with self.state.connect() as connection:
            row = connection.execute(
                "SELECT object_hash FROM frozen_evidence_pack_index WHERE pack_id=?",
                (pack_id,),
            ).fetchone()
        return str(row["object_hash"]) if row else None

    def register_evidence_pack(
        self,
        pack: FrozenEvidencePack,
        *,
        object_hash: str,
        request_hash: str,
    ) -> FrozenEvidencePack:
        with self.state.transaction() as connection:
            row = connection.execute(
                "SELECT object_hash,request_hash FROM frozen_evidence_pack_index "
                "WHERE pack_id=?",
                (pack.pack_id,),
            ).fetchone()
            if row is not None:
                if str(row["request_hash"]) != request_hash:
                    raise ValueError(f"frozen evidence pack collision: {pack.pack_id}")
                return FrozenEvidencePack.model_validate_json(
                    self.object_store.get_bytes(str(row["object_hash"]))
                )
            connection.execute(
                "INSERT INTO frozen_evidence_pack_index("
                "pack_id,company_id,as_of,formal_historical,allow_approximated,"
                "coverage_status,claim_count,evidence_count,open_conflict_count,object_hash,"
                "request_hash,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    pack.pack_id,
                    pack.company_id,
                    pack.as_of.astimezone(UTC).isoformat(),
                    int(pack.formal_historical),
                    int(pack.allow_approximated),
                    pack.coverage_status.value,
                    len(pack.claim_ids),
                    len(pack.evidence_ids),
                    len(pack.open_conflict_ids),
                    object_hash,
                    request_hash,
                    pack.created_at.isoformat(),
                ),
            )
        return pack

    def latest_evidence_pack_summary(self, company_id: str) -> dict[str, object] | None:
        with self.state.connect() as connection:
            row = connection.execute(
                "SELECT pack_id,company_id,as_of,formal_historical,allow_approximated,"
                "coverage_status,claim_count,evidence_count,open_conflict_count,object_hash,"
                "created_at FROM frozen_evidence_pack_index WHERE company_id=? "
                "ORDER BY as_of DESC,created_at DESC,pack_id DESC LIMIT 1",
                (company_id,),
            ).fetchone()
        return dict(row) if row else None

    def get_base_case(self, base_case_id: str) -> BaseCasePack | None:
        with self.state.connect() as connection:
            row = connection.execute(
                "SELECT object_hash FROM base_case_pack_index WHERE base_case_id=?",
                (base_case_id,),
            ).fetchone()
        if row is None:
            return None
        return BaseCasePack.model_validate_json(
            self.object_store.get_bytes(str(row["object_hash"]))
        )

    def base_case_object_hash(self, base_case_id: str) -> str | None:
        with self.state.connect() as connection:
            row = connection.execute(
                "SELECT object_hash FROM base_case_pack_index WHERE base_case_id=?",
                (base_case_id,),
            ).fetchone()
        return str(row["object_hash"]) if row else None

    def register_base_case(
        self,
        pack: BaseCasePack,
        *,
        object_hash: str,
        draft_hash: str,
    ) -> BaseCasePack:
        finding_count = sum(len(values) for values in pack.findings_by_section.values())
        with self.state.transaction() as connection:
            row = connection.execute(
                "SELECT object_hash,draft_hash FROM base_case_pack_index WHERE base_case_id=?",
                (pack.base_case_id,),
            ).fetchone()
            if row is not None:
                if str(row["draft_hash"]) != draft_hash:
                    raise ValueError(f"BaseCase identity collision: {pack.base_case_id}")
                return BaseCasePack.model_validate_json(
                    self.object_store.get_bytes(str(row["object_hash"]))
                )
            connection.execute(
                "INSERT INTO base_case_pack_index("
                "base_case_id,evidence_pack_id,company_id,as_of,kernel_version,coverage_status,"
                "finding_count,evidence_count,gap_count,object_hash,draft_hash,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    pack.base_case_id,
                    pack.evidence_pack_id,
                    pack.company_id,
                    pack.as_of.astimezone(UTC).isoformat(),
                    pack.kernel_version,
                    pack.coverage_status.value,
                    finding_count,
                    len(pack.evidence_ids),
                    len(pack.evidence_gaps),
                    object_hash,
                    draft_hash,
                    pack.created_at.isoformat(),
                ),
            )
        return pack

    def latest_base_case_summary(self, company_id: str) -> dict[str, object] | None:
        with self.state.connect() as connection:
            row = connection.execute(
                "SELECT base_case_id,evidence_pack_id,company_id,as_of,kernel_version,"
                "coverage_status,finding_count,evidence_count,gap_count,object_hash,created_at "
                "FROM base_case_pack_index WHERE company_id=? "
                "ORDER BY as_of DESC,created_at DESC,base_case_id DESC LIMIT 1",
                (company_id,),
            ).fetchone()
        return dict(row) if row else None

    def get_skill_registry(self, registry_version: str) -> ResearchSkillRegistry | None:
        row = self.skill_registry_summary(registry_version)
        if row is None:
            return None
        return ResearchSkillRegistry.model_validate_json(
            self.object_store.get_bytes(str(row["object_hash"]))
        )

    def skill_registry_summary(self, registry_version: str) -> dict[str, object] | None:
        with self.state.connect() as connection:
            row = connection.execute(
                "SELECT registry_version,skill_count,specialist_count,max_specialists,"
                "object_hash,config_hash,created_at FROM research_skill_registry_index "
                "WHERE registry_version=?",
                (registry_version,),
            ).fetchone()
        return dict(row) if row else None

    def register_skill_registry(
        self,
        registry: ResearchSkillRegistry,
        *,
        object_hash: str,
        config_hash: str,
    ) -> ResearchSkillRegistry:
        with self.state.transaction() as connection:
            row = connection.execute(
                "SELECT object_hash,config_hash FROM research_skill_registry_index "
                "WHERE registry_version=?",
                (registry.registry_version,),
            ).fetchone()
            if row is not None:
                if str(row["config_hash"]) != config_hash:
                    raise ValueError(
                        f"research Skill registry version collision: {registry.registry_version}"
                    )
                return ResearchSkillRegistry.model_validate_json(
                    self.object_store.get_bytes(str(row["object_hash"]))
                )
            connection.execute(
                "INSERT INTO research_skill_registry_index("
                "registry_version,skill_count,specialist_count,max_specialists,object_hash,"
                "config_hash,created_at) VALUES(?,?,?,?,?,?,?)",
                (
                    registry.registry_version,
                    len(registry.skills),
                    sum(item.counts_as_specialist for item in registry.skills),
                    registry.max_specialists,
                    object_hash,
                    config_hash,
                    registry.created_at.isoformat(),
                ),
            )
        return registry

    def get_route_plan(self, route_plan_id: str) -> SpecialistRoutePlan | None:
        with self.state.connect() as connection:
            row = connection.execute(
                "SELECT object_hash FROM specialist_route_plan_index WHERE route_plan_id=?",
                (route_plan_id,),
            ).fetchone()
        if row is None:
            return None
        return SpecialistRoutePlan.model_validate_json(
            self.object_store.get_bytes(str(row["object_hash"]))
        )

    def route_plan_object_hash(self, route_plan_id: str) -> str | None:
        with self.state.connect() as connection:
            row = connection.execute(
                "SELECT object_hash FROM specialist_route_plan_index WHERE route_plan_id=?",
                (route_plan_id,),
            ).fetchone()
        return str(row["object_hash"]) if row else None

    def register_route_plan(
        self,
        plan: SpecialistRoutePlan,
        *,
        object_hash: str,
        request_hash: str,
    ) -> SpecialistRoutePlan:
        with self.state.transaction() as connection:
            row = connection.execute(
                "SELECT object_hash,request_hash FROM specialist_route_plan_index "
                "WHERE route_plan_id=?",
                (plan.route_plan_id,),
            ).fetchone()
            if row is not None:
                if str(row["request_hash"]) != request_hash:
                    raise ValueError(f"specialist route identity collision: {plan.route_plan_id}")
                return SpecialistRoutePlan.model_validate_json(
                    self.object_store.get_bytes(str(row["object_hash"]))
                )
            connection.execute(
                "INSERT INTO specialist_route_plan_index("
                "route_plan_id,base_case_id,evidence_pack_id,registry_version,coverage_status,"
                "confidence_cap,selected_count,unavailable_count,degradation_count,object_hash,"
                "request_hash,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    plan.route_plan_id,
                    plan.base_case_id,
                    plan.evidence_pack_id,
                    plan.registry_version,
                    plan.coverage_status.value,
                    plan.confidence_cap,
                    len(plan.selected),
                    len(plan.unavailable),
                    len(plan.degradation_codes),
                    object_hash,
                    request_hash,
                    plan.created_at.isoformat(),
                ),
            )
        return plan

    def latest_route_plan_summary(self, base_case_id: str) -> dict[str, object] | None:
        with self.state.connect() as connection:
            row = connection.execute(
                "SELECT route_plan_id,base_case_id,evidence_pack_id,registry_version,"
                "coverage_status,confidence_cap,selected_count,unavailable_count,"
                "degradation_count,object_hash,created_at FROM specialist_route_plan_index "
                "WHERE base_case_id=? ORDER BY created_at DESC,route_plan_id DESC LIMIT 1",
                (base_case_id,),
            ).fetchone()
        return dict(row) if row else None

    def get_specialist_delta(self, delta_id: str) -> SpecialistDelta | None:
        with self.state.connect() as connection:
            row = connection.execute(
                "SELECT object_hash FROM specialist_delta_index WHERE delta_id=?",
                (delta_id,),
            ).fetchone()
        if row is None:
            return None
        return SpecialistDelta.model_validate_json(
            self.object_store.get_bytes(str(row["object_hash"]))
        )

    def specialist_delta_object_hash(self, delta_id: str) -> str | None:
        with self.state.connect() as connection:
            row = connection.execute(
                "SELECT object_hash FROM specialist_delta_index WHERE delta_id=?",
                (delta_id,),
            ).fetchone()
        return str(row["object_hash"]) if row else None

    def register_specialist_delta(
        self,
        delta: SpecialistDelta,
        *,
        object_hash: str,
        request_hash: str,
    ) -> SpecialistDelta:
        with self.state.transaction() as connection:
            row = connection.execute(
                "SELECT object_hash,request_hash FROM specialist_delta_index WHERE delta_id=?",
                (delta.delta_id,),
            ).fetchone()
            if row is not None:
                if str(row["request_hash"]) != request_hash:
                    raise ValueError(f"specialist delta identity collision: {delta.delta_id}")
                return SpecialistDelta.model_validate_json(
                    self.object_store.get_bytes(str(row["object_hash"]))
                )
            connection.execute(
                "INSERT INTO specialist_delta_index("
                "delta_id,base_case_id,route_plan_id,skill_id,skill_version,"
                "incremental_finding_count,correction_count,metric_count,"
                "evidence_request_count,evidence_count,confidence_delta,object_hash,"
                "request_hash,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    delta.delta_id,
                    delta.base_case_id,
                    delta.route_plan_id,
                    delta.skill_id,
                    delta.skill_version,
                    len(delta.incremental_findings),
                    len(delta.base_case_corrections),
                    len(delta.industry_specific_metrics),
                    len(delta.additional_evidence_requests),
                    len(delta.evidence_ids),
                    delta.confidence_delta,
                    object_hash,
                    request_hash,
                    delta.created_at.isoformat(),
                ),
            )
        return delta

    def specialist_delta_summaries(self, route_plan_id: str) -> list[dict[str, object]]:
        with self.state.connect() as connection:
            rows = connection.execute(
                "SELECT delta_id,base_case_id,route_plan_id,skill_id,skill_version,"
                "incremental_finding_count,correction_count,metric_count,"
                "evidence_request_count,evidence_count,confidence_delta,object_hash,created_at "
                "FROM specialist_delta_index WHERE route_plan_id=? "
                "ORDER BY skill_id,skill_version,delta_id",
                (route_plan_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_diagnostic_report(
        self,
        diagnostic_id: str,
    ) -> SpecialistDiagnosticReport | None:
        with self.state.connect() as connection:
            row = connection.execute(
                "SELECT object_hash FROM specialist_diagnostic_index WHERE diagnostic_id=?",
                (diagnostic_id,),
            ).fetchone()
        if row is None:
            return None
        return SpecialistDiagnosticReport.model_validate_json(
            self.object_store.get_bytes(str(row["object_hash"]))
        )

    def diagnostic_report_object_hash(self, diagnostic_id: str) -> str | None:
        with self.state.connect() as connection:
            row = connection.execute(
                "SELECT object_hash FROM specialist_diagnostic_index WHERE diagnostic_id=?",
                (diagnostic_id,),
            ).fetchone()
        return str(row["object_hash"]) if row else None

    def register_diagnostic_report(
        self,
        report: SpecialistDiagnosticReport,
        *,
        object_hash: str,
    ) -> SpecialistDiagnosticReport:
        with self.state.transaction() as connection:
            row = connection.execute(
                "SELECT object_hash,input_hash,config_hash FROM specialist_diagnostic_index "
                "WHERE diagnostic_id=?",
                (report.diagnostic_id,),
            ).fetchone()
            if row is not None:
                if (
                    str(row["input_hash"]) != report.input_sha256
                    or str(row["config_hash"]) != report.config_sha256
                ):
                    raise ValueError(
                        f"specialist diagnostic identity collision: {report.diagnostic_id}"
                    )
                return SpecialistDiagnosticReport.model_validate_json(
                    self.object_store.get_bytes(str(row["object_hash"]))
                )
            version_row = connection.execute(
                "SELECT diagnostic_id,config_hash FROM specialist_diagnostic_index "
                "WHERE route_plan_id=? AND skill_id=? AND skill_version=? "
                "AND diagnostics_version=? AND input_hash=?",
                (
                    report.route_plan_id,
                    report.skill_id,
                    report.skill_version,
                    report.diagnostics_version,
                    report.input_sha256,
                ),
            ).fetchone()
            if version_row is not None:
                if str(version_row["config_hash"]) != report.config_sha256:
                    raise ValueError(
                        "research diagnostic configuration changed without a version bump"
                    )
                raise ValueError(
                    "research diagnostic output changed without a rule version bump"
                )
            connection.execute(
                "INSERT INTO specialist_diagnostic_index("
                "diagnostic_id,base_case_id,route_plan_id,delta_id,skill_id,skill_version,"
                "diagnostics_version,status,signal_count,degradation_count,metric_count,"
                "evidence_request_count,evidence_count,object_hash,input_hash,config_hash,"
                "created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    report.diagnostic_id,
                    report.base_case_id,
                    report.route_plan_id,
                    report.delta_id,
                    report.skill_id,
                    report.skill_version,
                    report.diagnostics_version,
                    report.status.value,
                    len(report.signal_codes),
                    len(report.degradation_codes),
                    len(report.metric_names),
                    len(report.evidence_request_codes),
                    len(report.evidence_ids),
                    object_hash,
                    report.input_sha256,
                    report.config_sha256,
                    report.created_at.isoformat(),
                ),
            )
        return report

    def diagnostic_report_summaries(self, base_case_id: str) -> list[dict[str, object]]:
        with self.state.connect() as connection:
            rows = connection.execute(
                "SELECT diagnostic_id,base_case_id,route_plan_id,delta_id,skill_id,"
                "skill_version,diagnostics_version,status,signal_count,degradation_count,"
                "metric_count,evidence_request_count,evidence_count,object_hash,config_hash,"
                "created_at "
                "FROM specialist_diagnostic_index WHERE base_case_id=? "
                "ORDER BY skill_id,skill_version,diagnostic_id",
                (base_case_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_research_memo(self, memo_id: str) -> ResearchMemoArtifact | None:
        with self.state.connect() as connection:
            row = connection.execute(
                "SELECT object_hash FROM research_memo_index WHERE memo_id=?",
                (memo_id,),
            ).fetchone()
        if row is None:
            return None
        return ResearchMemoArtifact.model_validate_json(
            self.object_store.get_bytes(str(row["object_hash"]))
        )

    def research_memo_object_hash(self, memo_id: str) -> str | None:
        with self.state.connect() as connection:
            row = connection.execute(
                "SELECT object_hash FROM research_memo_index WHERE memo_id=?",
                (memo_id,),
            ).fetchone()
        return str(row["object_hash"]) if row else None

    def register_research_memo(
        self,
        memo: ResearchMemoArtifact,
        *,
        object_hash: str,
        input_hash: str,
    ) -> ResearchMemoArtifact:
        with self.state.transaction() as connection:
            row = connection.execute(
                "SELECT object_hash,input_hash FROM research_memo_index WHERE memo_id=?",
                (memo.memo_id,),
            ).fetchone()
            if row is not None:
                if str(row["input_hash"]) != input_hash:
                    raise ValueError(f"research memo identity collision: {memo.memo_id}")
                return ResearchMemoArtifact.model_validate_json(
                    self.object_store.get_bytes(str(row["object_hash"]))
                )
            connection.execute(
                "INSERT INTO research_memo_index("
                "memo_id,base_case_id,route_plan_id,company_id,as_of,registry_version,"
                "coverage_status,delta_count,missing_selected_count,gap_count,"
                "degradation_count,evidence_count,object_hash,input_hash,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    memo.memo_id,
                    memo.base_case_id,
                    memo.route_plan_id,
                    memo.company_id,
                    memo.as_of.astimezone(UTC).isoformat(),
                    memo.registry_version,
                    memo.coverage_status.value,
                    len(memo.delta_references),
                    len(memo.missing_selected_skill_ids),
                    len(memo.open_gap_codes),
                    len(memo.degradation_codes),
                    len(memo.evidence_ids),
                    object_hash,
                    input_hash,
                    memo.created_at.isoformat(),
                ),
            )
        return memo

    def latest_research_memo_summary(self, base_case_id: str) -> dict[str, object] | None:
        with self.state.connect() as connection:
            row = connection.execute(
                "SELECT memo_id,base_case_id,route_plan_id,company_id,as_of,registry_version,"
                "coverage_status,delta_count,missing_selected_count,gap_count,"
                "degradation_count,evidence_count,object_hash,created_at "
                "FROM research_memo_index WHERE base_case_id=? "
                "ORDER BY created_at DESC,memo_id DESC LIMIT 1",
                (base_case_id,),
            ).fetchone()
        return dict(row) if row else None


__all__ = ["ResearchRepository"]
