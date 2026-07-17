"""Recoverable metadata repository for deterministic financial audits."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import uuid4

from astock.core.hashing import canonical_json_bytes
from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.schemas import FinancialIntegrityEvidencePack, FinancialPeerCohort, RunStatus


@dataclass(frozen=True, slots=True)
class FinancialAuditRunRecord:
    audit_run_id: str
    request_hash: str
    company_id: str
    as_of: str
    industry_profile: str
    status: RunStatus
    rule_registry_version: str
    industry_profile_version: str
    request_object_hash: str
    report_object_hash: str | None
    checkpoint_step: str
    created_at: datetime


class FinancialIntegrityRepository:
    def __init__(self, state: StateStore, object_store: ObjectStore) -> None:
        self.state = state
        self.object_store = object_store

    def ensure_run(
        self,
        *,
        audit_run_id: str,
        request_hash: str,
        company_id: str,
        as_of: str,
        industry_profile: str,
        rule_registry_version: str,
        industry_profile_version: str,
        request_object_hash: str,
    ) -> FinancialAuditRunRecord:
        now = datetime.now(UTC).isoformat()
        with self.state.transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM financial_audit_run WHERE audit_run_id=?", (audit_run_id,)
            ).fetchone()
            if existing is None:
                connection.execute(
                    "INSERT INTO financial_audit_run(audit_run_id,request_hash,company_id,as_of,"
                    "industry_profile,status,rule_registry_version,industry_profile_version,"
                    "request_object_hash,checkpoint_step,created_at,updated_at) "
                    "VALUES(?,?,?,?,?,'PENDING',?,?,?,'INPUT_REGISTERED',?,?)",
                    (
                        audit_run_id,
                        request_hash,
                        company_id,
                        as_of,
                        industry_profile,
                        rule_registry_version,
                        industry_profile_version,
                        request_object_hash,
                        now,
                        now,
                    ),
                )
            else:
                expected = (
                    request_hash,
                    company_id,
                    as_of,
                    industry_profile,
                    rule_registry_version,
                    industry_profile_version,
                    request_object_hash,
                )
                actual = tuple(
                    existing[key]
                    for key in (
                        "request_hash",
                        "company_id",
                        "as_of",
                        "industry_profile",
                        "rule_registry_version",
                        "industry_profile_version",
                        "request_object_hash",
                    )
                )
                if actual != expected:
                    raise ValueError(f"financial audit identity collision: {audit_run_id}")
        record = self.get_run(audit_run_id)
        if record is None:  # pragma: no cover - one transaction inserted the row
            raise RuntimeError(f"financial audit run disappeared: {audit_run_id}")
        return record

    def get_run(self, audit_run_id: str) -> FinancialAuditRunRecord | None:
        with self.state.connect() as connection:
            row = connection.execute(
                "SELECT * FROM financial_audit_run WHERE audit_run_id=?", (audit_run_id,)
            ).fetchone()
        if row is None:
            return None
        return FinancialAuditRunRecord(
            audit_run_id=str(row["audit_run_id"]),
            request_hash=str(row["request_hash"]),
            company_id=str(row["company_id"]),
            as_of=str(row["as_of"]),
            industry_profile=str(row["industry_profile"]),
            status=RunStatus(str(row["status"])),
            rule_registry_version=str(row["rule_registry_version"]),
            industry_profile_version=str(row["industry_profile_version"]),
            request_object_hash=str(row["request_object_hash"]),
            report_object_hash=(
                str(row["report_object_hash"]) if row["report_object_hash"] else None
            ),
            checkpoint_step=str(row["checkpoint_step"]),
            created_at=datetime.fromisoformat(str(row["created_at"])),
        )

    def get_pack(self, audit_run_id: str) -> FinancialIntegrityEvidencePack | None:
        record = self.get_run(audit_run_id)
        if record is None or record.report_object_hash is None:
            return None
        payload = self.object_store.get_bytes(record.report_object_hash)
        return FinancialIntegrityEvidencePack.model_validate_json(payload)

    def start_attempt(self, audit_run_id: str) -> str:
        attempt_id = uuid4().hex
        now = datetime.now(UTC).isoformat()
        with self.state.transaction() as connection:
            row = connection.execute(
                "SELECT 1 FROM financial_audit_run WHERE audit_run_id=?", (audit_run_id,)
            ).fetchone()
            if row is None:
                raise ValueError(f"unknown financial audit run: {audit_run_id}")
            connection.execute(
                "UPDATE financial_audit_attempt SET ended_at=?,"
                "error_class='INTERRUPTED_RECOVERED',retryable=1 "
                "WHERE audit_run_id=? AND ended_at IS NULL",
                (now, audit_run_id),
            )
            connection.execute(
                "INSERT INTO financial_audit_attempt(attempt_id,audit_run_id,started_at) "
                "VALUES(?,?,?)",
                (attempt_id, audit_run_id, now),
            )
            connection.execute(
                "UPDATE financial_audit_run SET status='RUNNING',checkpoint_step='RUNNING',"
                "last_error_class=NULL,started_at=COALESCE(started_at,?),updated_at=? "
                "WHERE audit_run_id=?",
                (now, now, audit_run_id),
            )
        return attempt_id

    def checkpoint(self, audit_run_id: str, step: str) -> None:
        with self.state.transaction() as connection:
            updated = connection.execute(
                "UPDATE financial_audit_run SET checkpoint_step=?,updated_at=? "
                "WHERE audit_run_id=?",
                (step, datetime.now(UTC).isoformat(), audit_run_id),
            ).rowcount
            if updated != 1:
                raise ValueError(f"unknown financial audit run: {audit_run_id}")

    def complete(
        self,
        *,
        audit_run_id: str,
        attempt_id: str,
        status: RunStatus,
        report_object_hash: str,
        pack: FinancialIntegrityEvidencePack,
    ) -> None:
        if status not in {RunStatus.SUCCEEDED, RunStatus.NEEDS_INFO}:
            raise ValueError(f"invalid completed financial audit status: {status.value}")
        now = datetime.now(UTC).isoformat()
        with self.state.transaction() as connection:
            attempt_updated = connection.execute(
                "UPDATE financial_audit_attempt SET ended_at=?,error_class=NULL,retryable=0 "
                "WHERE attempt_id=? AND audit_run_id=? AND ended_at IS NULL",
                (now, attempt_id, audit_run_id),
            ).rowcount
            if attempt_updated != 1:
                raise ValueError(f"unknown or closed financial audit attempt: {attempt_id}")
            run_updated = connection.execute(
                "UPDATE financial_audit_run SET status=?,report_object_hash=?,"
                "checkpoint_step='COMPLETE',last_error_class=NULL,completed_at=?,updated_at=? "
                "WHERE audit_run_id=?",
                (status.value, report_object_hash, now, now, audit_run_id),
            ).rowcount
            if run_updated != 1:
                raise ValueError(f"unknown financial audit run: {audit_run_id}")
            for task in pack.manual_tasks:
                task_json = canonical_json_bytes(task.model_dump(mode="json")).decode("utf-8")
                connection.execute(
                    "INSERT INTO financial_manual_task(task_id,audit_run_id,status,reason_code,"
                    "task_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?) "
                    "ON CONFLICT(task_id) DO UPDATE SET task_json=excluded.task_json,"
                    "updated_at=excluded.updated_at",
                    (
                        task.task_id,
                        audit_run_id,
                        task.status,
                        task.reason_code,
                        task_json,
                        task.created_at.isoformat(),
                        now,
                    ),
                )

    def fail(self, audit_run_id: str, attempt_id: str, error_class: str) -> None:
        now = datetime.now(UTC).isoformat()
        with self.state.transaction() as connection:
            connection.execute(
                "UPDATE financial_audit_attempt SET ended_at=?,error_class=?,retryable=1 "
                "WHERE attempt_id=? AND audit_run_id=? AND ended_at IS NULL",
                (now, error_class, attempt_id, audit_run_id),
            )
            connection.execute(
                "UPDATE financial_audit_run SET status='FAILED',last_error_class=?,"
                "checkpoint_step='FAILED',updated_at=? WHERE audit_run_id=?",
                (error_class, now, audit_run_id),
            )

    def attempt_count(self, audit_run_id: str) -> int:
        with self.state.connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) FROM financial_audit_attempt WHERE audit_run_id=?",
                (audit_run_id,),
            ).fetchone()
        return int(row[0]) if row else 0

    def register_peer_cohort(
        self,
        *,
        audit_run_id: str,
        cohort: FinancialPeerCohort,
        object_hash: str,
    ) -> None:
        now = datetime.now(UTC).isoformat()
        with self.state.transaction() as connection:
            connection.execute(
                "INSERT INTO financial_peer_cohort_manifest("
                "audit_run_id,cohort_id,industry_profile,metric_id,formula_version,as_of,"
                "minimum_sample_size,sample_count,object_hash,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(audit_run_id,cohort_id) DO UPDATE SET "
                "industry_profile=excluded.industry_profile,metric_id=excluded.metric_id,"
                "formula_version=excluded.formula_version,as_of=excluded.as_of,"
                "minimum_sample_size=excluded.minimum_sample_size,"
                "sample_count=excluded.sample_count,object_hash=excluded.object_hash",
                (
                    audit_run_id,
                    cohort.cohort_id,
                    cohort.industry_profile.value,
                    cohort.metric_id,
                    cohort.formula_version,
                    cohort.as_of.isoformat(),
                    cohort.minimum_sample_size,
                    len(cohort.observations),
                    object_hash,
                    now,
                ),
            )
