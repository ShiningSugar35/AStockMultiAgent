"""Phase 11 all-trials registration and prospective statistical governance."""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from contextlib import closing
from datetime import UTC, datetime
from typing import Any

from astock.core.hashing import content_hash
from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.schemas.prospective import (
    EndpointRole,
    ProspectiveAllTrialsReport,
    ProspectiveEndpointDefinition,
    ProspectiveFunnelStage,
    ProspectiveGovernanceConfig,
    ProspectiveStatisticsPlan,
    ProspectiveTrialRecord,
    ProspectiveTrialRecordRequest,
    PurgedFoldDefinition,
    SelectionBiasDiagnostic,
    SelectionBiasDiagnosticStatus,
    TrialClusterType,
)
from astock.schemas.shadow import ShadowEvaluationPolicy, ShadowStudyMode
from astock.shadow.repository import ShadowRepository
from astock.shadow.statistics import (
    deflated_sharpe_probability,
    time_fold_probability_of_backtest_overfitting,
)


def _default_config_version(study_id: str) -> str:
    return "prospective-governance-v1:" + content_hash({"study_id": study_id})[:16]


def default_prospective_governance_config(
    *,
    study_id: str,
    effective_from: datetime,
    shadow_policy: ShadowEvaluationPolicy,
    created_at: datetime,
) -> ProspectiveGovernanceConfig:
    endpoints = [
        ProspectiveEndpointDefinition(
            endpoint_id="20D_SECTOR_ADJUSTED_RETURN",
            role=EndpointRole.PRIMARY,
            horizon_days=20,
            adjustment="SECTOR_RETURN",
            higher_is_better=True,
            created_at=created_at,
        ),
        ProspectiveEndpointDefinition(
            endpoint_id="5D_RETURN",
            role=EndpointRole.DIAGNOSTIC,
            horizon_days=5,
            adjustment="RAW_NET_RETURN",
            higher_is_better=True,
            created_at=created_at,
        ),
        ProspectiveEndpointDefinition(
            endpoint_id="60D_BENCHMARK_ADJUSTED_RETURN",
            role=EndpointRole.PRIMARY,
            horizon_days=60,
            adjustment="BENCHMARK_RETURN",
            higher_is_better=True,
            created_at=created_at,
        ),
        ProspectiveEndpointDefinition(
            endpoint_id="60D_MAE",
            role=EndpointRole.PRIMARY,
            horizon_days=60,
            adjustment="MAXIMUM_ADVERSE_EXCURSION",
            higher_is_better=False,
            created_at=created_at,
        ),
        ProspectiveEndpointDefinition(
            endpoint_id="DECISION_CALIBRATION",
            role=EndpointRole.PRIMARY,
            horizon_days=60,
            adjustment="FROZEN_DECISION_CALIBRATION",
            higher_is_better=True,
            created_at=created_at,
        ),
        ProspectiveEndpointDefinition(
            endpoint_id="PROCESS_GROUNDING",
            role=EndpointRole.PRIMARY,
            horizon_days=None,
            adjustment="EVIDENCE_AND_FALSIFIER_GROUNDING",
            higher_is_better=True,
            created_at=created_at,
        ),
    ]
    return ProspectiveGovernanceConfig(
        config_id=f"prospective-governance:{study_id}",
        config_version=_default_config_version(study_id),
        study_id=study_id,
        effective_from=effective_from,
        independence_contract_version="cluster-independence-v1",
        market_regime_rule_version=shadow_policy.regime_rule_version,
        statistics_version="prospective-cluster-statistics-v1",
        endpoints=endpoints,
        walk_forward_folds=shadow_policy.minimum_walk_forward_folds,
        minimum_independence_units=shadow_policy.minimum_independent_decisions,
        purge_horizon_sessions=shadow_policy.final_horizon_days,
        embargo_sessions=5,
        cluster_bootstrap_replicates=shadow_policy.bootstrap_replicates,
        created_at=created_at,
    )


class ProspectiveGovernanceService:
    """Persist all research funnel trials without mutating formal forward-event counts."""

    def __init__(
        self,
        state: StateStore,
        objects: ObjectStore,
        shadow_policy: ShadowEvaluationPolicy,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.state = state
        self.objects = objects
        self.shadow_policy = shadow_policy
        self.shadow = ShadowRepository(state, objects)
        self.clock = clock or (lambda: datetime.now(UTC))

    def register_default_config(self, study_id: str) -> ProspectiveGovernanceConfig:
        study = self.shadow.get_study(study_id)
        if study is None:
            raise ValueError(f"unknown shadow study: {study_id}")
        if study.mode is not ShadowStudyMode.FORWARD_FORMAL:
            raise ValueError("prospective governance config requires a formal forward study")
        config_version = _default_config_version(study_id)
        existing = self._config_row(config_version)
        if existing is not None:
            return self._load_config(config_version)
        created_at = self.clock()
        config = default_prospective_governance_config(
            study_id=study_id,
            effective_from=max(created_at, study.effective_from),
            shadow_policy=self.shadow_policy,
            created_at=created_at,
        )
        return self.register_config(config)

    def register_config(self, config: ProspectiveGovernanceConfig) -> ProspectiveGovernanceConfig:
        study = self.shadow.get_study(config.study_id)
        if study is None or study.mode is not ShadowStudyMode.FORWARD_FORMAL:
            raise ValueError("prospective governance config requires a formal forward study")
        if config.effective_from < study.effective_from:
            raise ValueError("prospective governance config cannot predate its formal study")
        if config.created_at > config.effective_from:
            raise ValueError("prospective governance config must be frozen before effective_from")
        if config.market_regime_rule_version != self.shadow_policy.regime_rule_version:
            raise ValueError("prospective market regime rule must match the frozen shadow policy")
        if config.statistics_version == self.shadow_policy.statistics_version:
            raise ValueError("Phase 11 statistics require an explicit new statistics version")
        config_hash = content_hash(config.model_dump(mode="json", exclude={"created_at"}))
        existing = self._config_row(config.config_version)
        if existing is not None:
            if str(existing["config_hash"]) != config_hash:
                raise ValueError("prospective governance config changed without a version bump")
            return ProspectiveGovernanceConfig.model_validate_json(
                self.objects.get_bytes(str(existing["object_hash"]))
            )
        object_ref = self.objects.put_json(config.model_dump(mode="json"))
        with self.state.transaction() as connection:
            connection.execute(
                "INSERT INTO prospective_governance_config_index("
                "config_version,config_id,study_id,effective_from,independence_contract_version,"
                "market_regime_rule_version,statistics_version,object_hash,config_hash,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    config.config_version,
                    config.config_id,
                    config.study_id,
                    config.effective_from.astimezone(UTC).isoformat(),
                    config.independence_contract_version,
                    config.market_regime_rule_version,
                    config.statistics_version,
                    object_ref.sha256,
                    config_hash,
                    config.created_at.astimezone(UTC).isoformat(),
                ),
            )
        self.state.register_artifact(
            artifact_id=self.config_artifact_id(config.config_version),
            artifact_type="ProspectiveGovernanceConfig",
            schema_version=config.schema_version,
            object_hash=object_ref.sha256,
            input_hashes=[content_hash(study), content_hash(self.shadow_policy)],
        )
        return config

    def register_trial(self, request: ProspectiveTrialRecordRequest) -> ProspectiveTrialRecord:
        config = self._load_config_by_artifact(
            request.governance_config_artifact_id,
            request.governance_config_object_hash,
        )
        if request.study_id != config.study_id:
            raise ValueError("prospective trial study does not match its governance config")
        if request.decision_time < config.effective_from:
            raise ValueError("prospective trial predates its preregistered governance config")
        if request.market_regime_rule_version not in {None, config.market_regime_rule_version}:
            raise ValueError(
                "prospective trial cannot relabel the preregistered market regime rule"
            )
        self._verify_frozen_inputs(request)
        if request.formal_assignment_id is not None:
            assignment = self.shadow.get_assignment(request.formal_assignment_id)
            if assignment is None or assignment.study_id != request.study_id:
                raise ValueError("prospective assignment link does not resolve inside the study")
        input_set_hash = content_hash(
            [
                {
                    "artifact_id": item.artifact_id,
                    "artifact_type": item.artifact_type,
                    "object_sha256": item.object_sha256,
                    "available_at": item.available_at.isoformat(),
                }
                for item in request.frozen_inputs
            ]
        )
        event_identity = {
            "study_id": request.study_id,
            "research_trial_id": request.research_trial_id,
            "funnel_event_id": request.funnel_event_id,
            "decision_time": request.decision_time.isoformat(),
            "stage": request.stage.value,
            "outcome": request.outcome.value,
            "independence_unit_id": request.independence_unit_id,
            "config_hash": request.governance_config_object_hash,
            "input_set_hash": input_set_hash,
        }
        trial_event_hash = content_hash(event_identity)
        trial_event_id = f"prospective-trial-event:{trial_event_hash}"
        existing = self._trial_row(trial_event_id)
        if existing is not None:
            if str(existing["trial_event_hash"]) != trial_event_hash:
                raise ValueError("prospective trial event identity collision")
            return ProspectiveTrialRecord.model_validate_json(
                self.objects.get_bytes(str(existing["object_hash"]))
            )
        duplicate = self._trial_by_funnel_event(request.study_id, request.funnel_event_id)
        if duplicate is not None:
            raise ValueError("funnel event is already registered to a different trial identity")
        registered_at = self.clock()
        record = ProspectiveTrialRecord(
            **request.model_dump(),
            trial_event_id=trial_event_id,
            frozen_input_set_sha256=input_set_hash,
            trial_event_sha256=trial_event_hash,
            registered_at=registered_at,
        )
        object_ref = self.objects.put_json(record.model_dump(mode="json"))
        with self.state.transaction() as connection:
            connection.execute(
                "INSERT INTO prospective_trial_event_index("
                "trial_event_id,study_id,config_version,research_trial_id,funnel_event_id,"
                "company_id,decision_time,stage,outcome,independence_unit_id,formal_assignment_id,"
                "formal_trade_event,frozen_input_set_hash,object_hash,trial_event_hash,created_at,"
                "registered_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    record.trial_event_id,
                    record.study_id,
                    config.config_version,
                    record.research_trial_id,
                    record.funnel_event_id,
                    record.company_id,
                    record.decision_time.astimezone(UTC).isoformat(),
                    record.stage.value,
                    record.outcome.value,
                    record.independence_unit_id,
                    record.formal_assignment_id,
                    0,
                    record.frozen_input_set_sha256,
                    object_ref.sha256,
                    record.trial_event_sha256,
                    record.created_at.astimezone(UTC).isoformat(),
                    registered_at.isoformat(),
                ),
            )
            for cluster_type, cluster_ids in sorted(
                record.cluster_ids.items(), key=lambda item: item[0].value
            ):
                for cluster_id in cluster_ids:
                    connection.execute(
                        "INSERT INTO prospective_trial_cluster_index("
                        "trial_event_id,cluster_type,cluster_id) VALUES(?,?,?)",
                        (record.trial_event_id, cluster_type.value, cluster_id),
                    )
            for reference in record.frozen_inputs:
                connection.execute(
                    "INSERT INTO prospective_trial_input_index("
                    "trial_event_id,artifact_id,artifact_type,object_hash,available_at) "
                    "VALUES(?,?,?,?,?)",
                    (
                        record.trial_event_id,
                        reference.artifact_id,
                        reference.artifact_type,
                        reference.object_sha256,
                        reference.available_at.astimezone(UTC).isoformat(),
                    ),
                )
        self.state.register_artifact(
            artifact_id=record.trial_event_id,
            artifact_type="ProspectiveTrialRecord",
            schema_version=record.schema_version,
            object_hash=object_ref.sha256,
            input_hashes=[
                request.governance_config_object_hash,
                *sorted(item.object_sha256 for item in record.frozen_inputs),
            ],
        )
        return record

    def all_trials_report(self, study_id: str) -> ProspectiveAllTrialsReport:
        rows = self._trial_rows(study_id)
        if not rows:
            config = self.register_default_config(study_id)
            config_artifact = self.config_artifact_id(config.config_version)
            config_row = self._config_row(config.config_version)
            assert config_row is not None
            input_hashes = [str(config_row["object_hash"])]
        else:
            config = self._load_config(str(rows[0]["config_version"]))
            config_artifact = self.config_artifact_id(config.config_version)
            input_hashes = [str(row["object_hash"]) for row in rows]
        records = [
            ProspectiveTrialRecord.model_validate_json(
                self.objects.get_bytes(str(row["object_hash"]))
            )
            for row in rows
        ]
        stage_counts = Counter(item.stage for item in records)
        outcome_counts = Counter(item.outcome for item in records)
        clusters: dict[TrialClusterType, set[str]] = {item: set() for item in TrialClusterType}
        for record in records:
            for cluster_type, ids in record.cluster_ids.items():
                clusters[cluster_type].update(ids)
        findings: set[str] = set()
        independent = {item.independence_unit_id for item in records}
        if len(independent) < config.minimum_independence_units:
            findings.add("INDEPENDENCE_SAMPLE_FLOOR_NOT_REACHED")
        if any(item.market_regime_id is None for item in records):
            findings.add("MARKET_REGIME_NOT_FROZEN_FOR_ALL_TRIALS")
        report = ProspectiveAllTrialsReport(
            report_id="prospective-all-trials:"
            + content_hash(
                {
                    "study_id": study_id,
                    "config": config.config_version,
                    "trial_hashes": sorted(item.trial_event_sha256 for item in records),
                }
            ),
            study_id=study_id,
            governance_config_artifact_id=config_artifact,
            event_count=len(records),
            research_trial_count=len({item.research_trial_id for item in records}),
            independence_unit_count=len(independent),
            stage_counts={stage: stage_counts.get(stage, 0) for stage in ProspectiveFunnelStage},
            outcome_counts=dict(sorted(outcome_counts.items(), key=lambda item: item[0].value)),
            cluster_counts={key: len(value) for key, value in clusters.items()},
            formal_assignment_link_count=sum(
                item.formal_assignment_id is not None for item in records
            ),
            finding_codes=sorted(findings),
            input_trial_event_sha256s=sorted(item.trial_event_sha256 for item in records),
            created_at=self.clock(),
        )
        self._persist_generic_report(report, "ProspectiveAllTrialsReport", input_hashes)
        return report

    def statistics_plan(self, study_id: str) -> ProspectiveStatisticsPlan:
        rows = self._trial_rows(study_id)
        if not rows:
            config = self.register_default_config(study_id)
        else:
            config = self._load_config(str(rows[0]["config_version"]))
        records = [
            ProspectiveTrialRecord.model_validate_json(
                self.objects.get_bytes(str(row["object_hash"]))
            )
            for row in rows
        ]
        earliest: dict[str, datetime] = {}
        for item in records:
            earliest[item.independence_unit_id] = min(
                earliest.get(item.independence_unit_id, item.decision_time),
                item.decision_time,
            )
        units = sorted(earliest, key=lambda key: (earliest[key], key))
        folds = self._purged_folds(units, earliest, config)
        cluster_sets: dict[TrialClusterType, set[str]] = {kind: set() for kind in TrialClusterType}
        for record in records:
            for kind, values in record.cluster_ids.items():
                cluster_sets[kind].update(values)
        primary = sorted(
            item.endpoint_id for item in config.endpoints if item.role is EndpointRole.PRIMARY
        )
        diagnostic = sorted(
            item.endpoint_id for item in config.endpoints if item.role is EndpointRole.DIAGNOSTIC
        )
        findings: set[str] = set()
        floor = len(units) >= config.minimum_independence_units
        if not floor:
            findings.add("INDEPENDENCE_SAMPLE_FLOOR_NOT_REACHED")
        if len(folds) < config.walk_forward_folds:
            findings.add("PURGED_FOLD_COUNT_INSUFFICIENT")
        input_hash = content_hash(
            {
                "config": content_hash(config),
                "events": sorted(item.trial_event_sha256 for item in records),
                "unit_times": {key: earliest[key].isoformat() for key in units},
            }
        )
        plan = ProspectiveStatisticsPlan(
            plan_id="prospective-statistics-plan:"
            + content_hash({"study_id": study_id, "input": input_hash}),
            study_id=study_id,
            governance_config_artifact_id=self.config_artifact_id(config.config_version),
            primary_endpoint_ids=primary,
            diagnostic_endpoint_ids=diagnostic,
            folds=folds,
            independence_unit_count=len(units),
            cluster_counts={key: len(value) for key, value in cluster_sets.items()},
            independence_sample_floor_reached=floor,
            finding_codes=sorted(findings),
            created_at=self.clock(),
        )
        object_ref = self.objects.put_json(plan.model_dump(mode="json"))
        config_row = self._config_row(config.config_version)
        assert config_row is not None
        with self.state.transaction() as connection:
            existing = connection.execute(
                "SELECT object_hash,input_hash FROM prospective_statistics_plan_index "
                "WHERE plan_id=?",
                (plan.plan_id,),
            ).fetchone()
            if existing is None:
                connection.execute(
                    "INSERT INTO prospective_statistics_plan_index("
                    "plan_id,study_id,config_version,independence_unit_count,"
                    "independence_sample_floor_reached,object_hash,input_hash,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (
                        plan.plan_id,
                        study_id,
                        config.config_version,
                        len(units),
                        int(floor),
                        object_ref.sha256,
                        input_hash,
                        plan.created_at.astimezone(UTC).isoformat(),
                    ),
                )
            elif str(existing["input_hash"]) != input_hash:
                raise ValueError("prospective statistics plan identity collision")
        self.state.register_artifact(
            artifact_id=plan.plan_id,
            artifact_type="ProspectiveStatisticsPlan",
            schema_version=plan.schema_version,
            object_hash=object_ref.sha256,
            input_hashes=[
                str(config_row["object_hash"]),
                *sorted(str(row["object_hash"]) for row in rows),
            ],
        )
        return plan

    def selection_bias_diagnostic(
        self,
        strategy_returns: dict[str, list[float]],
        *,
        created_at: datetime | None = None,
    ) -> SelectionBiasDiagnostic:
        if not strategy_returns:
            raise ValueError("selection-bias diagnostic requires at least one candidate")
        names = sorted(strategy_returns)
        selection_count = len(names)
        observation_counts = {len(strategy_returns[name]) for name in names}
        observation_count = min(observation_counts) if observation_counts else 0
        timestamp = created_at or self.clock()
        if selection_count == 1:
            return SelectionBiasDiagnostic(
                diagnostic_id="selection-bias:"
                + content_hash({"candidates": names, "returns": strategy_returns}),
                selection_candidate_count=1,
                observation_count=observation_count,
                status=SelectionBiasDiagnosticStatus.NOT_APPLICABLE,
                finding_codes=["NO_REPEATED_MODEL_OR_PARAMETER_SELECTION"],
                created_at=timestamp,
            )
        if len(observation_counts) != 1:
            raise ValueError("selection-bias candidate return series must have equal length")
        winner_name = max(
            names,
            key=lambda name: (
                sum(strategy_returns[name]) / max(len(strategy_returns[name]), 1),
                name,
            ),
        )
        dsr = deflated_sharpe_probability(
            strategy_returns[winner_name],
            selection_candidate_count=selection_count,
        )
        pbo = time_fold_probability_of_backtest_overfitting(
            [strategy_returns[name] for name in names],
            fold_count=min(5, max(2, observation_count // 2)) if observation_count >= 4 else 2,
        )
        if dsr is None or pbo is None:
            status = SelectionBiasDiagnosticStatus.INSUFFICIENT_INPUT
            findings = ["SELECTION_BIAS_DIAGNOSTIC_INSUFFICIENT_INPUT"]
        else:
            status = SelectionBiasDiagnosticStatus.COMPUTED
            findings = ["DSR_PBO_DIAGNOSTIC_ONLY"]
        return SelectionBiasDiagnostic(
            diagnostic_id="selection-bias:"
            + content_hash({"candidates": names, "returns": strategy_returns}),
            selection_candidate_count=selection_count,
            observation_count=observation_count,
            status=status,
            deflated_sharpe_ratio=dsr,
            probability_of_backtest_overfitting=pbo,
            finding_codes=findings,
            created_at=timestamp,
        )

    def audit(self, artifact_id: str) -> dict[str, Any]:
        record = self.state.artifact_record(artifact_id)
        allowed = {
            "ProspectiveGovernanceConfig",
            "ProspectiveTrialRecord",
            "ProspectiveAllTrialsReport",
            "ProspectiveStatisticsPlan",
        }
        if record is None or str(record["type"]) not in allowed:
            return {
                "status": "FAIL",
                "artifact_id": artifact_id,
                "finding_codes": ["UNKNOWN_ARTIFACT"],
            }
        findings: set[str] = set()
        if not self.objects.verify(str(record["object_hash"])):
            findings.add("OBJECT_UNAVAILABLE")
        for input_hash in record["input_hashes"]:
            if len(str(input_hash)) == 64 and not self.objects.verify(str(input_hash)):
                # Content identities need not be object hashes; only flag registered objects.
                with closing(self.state.connect()) as connection:
                    object_row = connection.execute(
                        "SELECT 1 FROM artifact_registry WHERE object_hash=? LIMIT 1",
                        (str(input_hash),),
                    ).fetchone()
                if object_row is not None:
                    findings.add("INPUT_OBJECT_UNAVAILABLE")
        return {
            "status": "PASS" if not findings else "FAIL",
            "artifact_id": artifact_id,
            "finding_codes": sorted(findings),
            "formal_forward_count_mutation_allowed": False,
            "automatic_admission_allowed": False,
            "paper_ledger_write_allowed": False,
            "broker_execution_allowed": False,
        }

    @staticmethod
    def config_artifact_id(config_version: str) -> str:
        return f"ProspectiveGovernanceConfig:{config_version}"

    def _load_config_by_artifact(
        self, artifact_id: str, object_hash: str
    ) -> ProspectiveGovernanceConfig:
        record = self.state.artifact_record(artifact_id)
        if record is None or str(record["type"]) != "ProspectiveGovernanceConfig":
            raise ValueError("prospective trial requires a registered governance config")
        if str(record["object_hash"]) != object_hash or not self.objects.verify(object_hash):
            raise ValueError("prospective governance config hash is unavailable or mismatched")
        return ProspectiveGovernanceConfig.model_validate_json(self.objects.get_bytes(object_hash))

    def _load_config(self, config_version: str) -> ProspectiveGovernanceConfig:
        row = self._config_row(config_version)
        if row is None:
            raise ValueError(f"unknown prospective governance config: {config_version}")
        return ProspectiveGovernanceConfig.model_validate_json(
            self.objects.get_bytes(str(row["object_hash"]))
        )

    def _verify_frozen_inputs(self, request: ProspectiveTrialRecordRequest) -> None:
        for reference in request.frozen_inputs:
            record = self.state.artifact_record(reference.artifact_id)
            if record is None:
                raise ValueError(
                    f"prospective input artifact is unregistered: {reference.artifact_id}"
                )
            if str(record["type"]) != reference.artifact_type:
                raise ValueError("prospective input artifact type mismatch")
            if str(record["object_hash"]) != reference.object_sha256:
                raise ValueError("prospective input artifact hash mismatch")
            if not self.objects.verify(reference.object_sha256):
                raise ValueError("prospective input object is unavailable")
            registered_at = datetime.fromisoformat(str(record["created_at"]))
            if registered_at.astimezone(UTC) > request.decision_time.astimezone(UTC):
                raise ValueError("prospective input was registered after the decision time")

    def _purged_folds(
        self,
        units: list[str],
        earliest: dict[str, datetime],
        config: ProspectiveGovernanceConfig,
    ) -> list[PurgedFoldDefinition]:
        if len(units) < config.walk_forward_folds:
            return []
        unique_dates = sorted({earliest[unit].date() for unit in units})
        date_index = {date: index for index, date in enumerate(unique_dates)}
        boundaries = [
            round(index * len(units) / config.walk_forward_folds)
            for index in range(config.walk_forward_folds + 1)
        ]
        folds: list[PurgedFoldDefinition] = []
        for fold in range(config.walk_forward_folds):
            start, end = boundaries[fold], boundaries[fold + 1]
            test = units[start:end]
            if not test:
                continue
            test_dates = [date_index[earliest[unit].date()] for unit in test]
            first_test_date = min(test_dates)
            last_test_date = max(test_dates)
            purged: set[str] = set()
            embargoed: set[str] = set()
            train: set[str] = set(units) - set(test)
            for unit in list(train):
                index = date_index[earliest[unit].date()]
                if first_test_date - config.purge_horizon_sessions <= index < first_test_date:
                    purged.add(unit)
                elif last_test_date < index <= last_test_date + config.embargo_sessions:
                    embargoed.add(unit)
            train -= purged | embargoed
            folds.append(
                PurgedFoldDefinition(
                    fold_number=fold + 1,
                    test_independence_unit_ids=sorted(test),
                    train_independence_unit_ids=sorted(train),
                    purged_independence_unit_ids=sorted(purged),
                    embargoed_independence_unit_ids=sorted(embargoed),
                )
            )
        return folds

    def _persist_generic_report(
        self, report: Any, artifact_type: str, input_hashes: list[str]
    ) -> None:
        object_ref = self.objects.put_json(report.model_dump(mode="json"))
        self.state.register_artifact(
            artifact_id=report.report_id,
            artifact_type=artifact_type,
            schema_version=report.schema_version,
            object_hash=object_ref.sha256,
            input_hashes=sorted(set(input_hashes)),
        )

    def _config_row(self, config_version: str) -> dict[str, Any] | None:
        with closing(self.state.connect()) as connection:
            row = connection.execute(
                "SELECT * FROM prospective_governance_config_index WHERE config_version=?",
                (config_version,),
            ).fetchone()
        return dict(row) if row is not None else None

    def _trial_row(self, trial_event_id: str) -> dict[str, Any] | None:
        with closing(self.state.connect()) as connection:
            row = connection.execute(
                "SELECT * FROM prospective_trial_event_index WHERE trial_event_id=?",
                (trial_event_id,),
            ).fetchone()
        return dict(row) if row is not None else None

    def _trial_by_funnel_event(self, study_id: str, funnel_event_id: str) -> dict[str, Any] | None:
        with closing(self.state.connect()) as connection:
            row = connection.execute(
                "SELECT * FROM prospective_trial_event_index "
                "WHERE study_id=? AND funnel_event_id=?",
                (study_id, funnel_event_id),
            ).fetchone()
        return dict(row) if row is not None else None

    def _trial_rows(self, study_id: str) -> list[dict[str, Any]]:
        with closing(self.state.connect()) as connection:
            rows = connection.execute(
                "SELECT * FROM prospective_trial_event_index WHERE study_id=? "
                "ORDER BY decision_time,trial_event_id",
                (study_id,),
            ).fetchall()
        return [dict(row) for row in rows]


__all__ = [
    "ProspectiveGovernanceService",
    "default_prospective_governance_config",
]
