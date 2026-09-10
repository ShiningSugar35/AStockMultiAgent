from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel

from astock.investor_orchestration.models import (
    CapabilityCoverageReceipt,
    CapabilityExecutionPlan,
    ControlledLiveCheck,
    EstimatedCostBasisRange,
    FeatureActivationReceipt,
    InvestorSessionPreflightReceipt,
    MacroReleaseSnapshot,
    MarketRegimeSnapshotV2,
    MaterialChangeDigest,
    ProvisionalPositionAssertion,
    RegimeDecisionOverlay,
    ResearchSubjectEvent,
    ScheduledResearchPolicy,
    ScheduledRunReceipt,
    ScheduledRunRequest,
    ScheduledTaskBinding,
    ShadowObservation,
    WatchlistAnalysisRevision,
)
from astock.investor_orchestration.utils import canonical_json, content_hash, utc_now

ModelT = TypeVar("ModelT", bound=BaseModel)


def default_state_path() -> Path:
    configured = os.environ.get("ASTOCK_STATE_DB")
    return Path(configured) if configured else Path("runtime/state.sqlite")


class InvestorOrchestrationStore:
    """Append-oriented persistence for investor orchestration metadata.

    Existing account and paper ledgers remain authoritative. This store only owns
    request receipts, research-subject metadata, regime snapshots and scheduled
    research receipts.
    """

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path is not None else default_state_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._active_connection: ContextVar[sqlite3.Connection | None] = ContextVar(
            "investor_store_transaction", default=None
        )

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        active = self._active_connection.get()
        if active is not None:
            yield active
            return
        connection = sqlite3.connect(self.path, timeout=30.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        connection.execute("PRAGMA synchronous = FULL")
        connection.execute("PRAGMA temp_store = MEMORY")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self) -> None:
        # Canonical migrations own DDL; retain the 0067 legacy checksum for audit.
        from astock.core.state import StateStore

        StateStore(self.path).migrate()
        migration = (
            Path(__file__).resolve().parents[3] / "migrations" / "0067_investor_orchestration.sql"
        )
        checksum = hashlib.sha256(migration.read_bytes()).hexdigest()
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT checksum FROM orchestration_schema_migrations WHERE version=?",
                ("0067_investor_orchestration",),
            ).fetchone()
            if row is not None and row[0] != checksum:
                raise ValueError("migration checksum mismatch for 0067_investor_orchestration")
            if row is None:
                connection.execute(
                    "INSERT INTO orchestration_schema_migrations VALUES(?,?,?)",
                    ("0067_investor_orchestration", checksum, utc_now().isoformat()),
                )

    @contextmanager
    def transaction(self, *, read_only: bool = False) -> Iterator[sqlite3.Connection]:
        """Join one atomic metadata transaction or coherent read snapshot."""
        if self._active_connection.get() is not None:
            with self.connect() as connection:
                yield connection
            return
        with self.connect() as connection:
            if read_only:
                connection.execute("PRAGMA query_only=ON")
            connection.execute("BEGIN" if read_only else "BEGIN IMMEDIATE")
            marker = self._active_connection.set(connection)
            try:
                yield connection
            finally:
                self._active_connection.reset(marker)

    def apply_schema_migration(self, version: str, payload: bytes) -> bool:
        """Apply one immutable SQL migration atomically and record its checksum."""
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", version):
            raise ValueError("migration version contains unsupported characters")
        checksum = hashlib.sha256(payload).hexdigest()
        sql = payload.decode("utf-8")
        statement_buffer = ""
        for line in sql.splitlines(keepends=True):
            statement_buffer += line
            if not sqlite3.complete_statement(statement_buffer):
                continue
            without_comments = re.sub(
                r"(?:--[^\n]*(?:\n|$)|/\*.*?\*/)",
                " ",
                statement_buffer,
                flags=re.DOTALL,
            ).lstrip()
            first_token = re.match(r"[A-Za-z]+", without_comments)
            if first_token and first_token.group(0).upper() in {"BEGIN", "COMMIT", "ROLLBACK"}:
                raise ValueError("migration payload must not manage its own transaction")
            statement_buffer = ""
        if statement_buffer.strip():
            raise ValueError("migration payload ends with an incomplete SQL statement")
        with self.connect() as connection:
            migration_table_exists = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='orchestration_schema_migrations'"
            ).fetchone()
            if migration_table_exists:
                row = connection.execute(
                    "SELECT checksum FROM orchestration_schema_migrations WHERE version=?",
                    (version,),
                ).fetchone()
                if row is not None:
                    if str(row[0]) != checksum:
                        raise ValueError(f"migration checksum mismatch for {version}")
                    return False
            applied_at = utc_now().isoformat().replace("'", "''")
            wrapped = (
                "BEGIN IMMEDIATE;\n" + sql + "\nINSERT INTO orchestration_schema_migrations"
                "(version, checksum, applied_at) VALUES "
                f"('{version}', '{checksum}', '{applied_at}');\n"
                "COMMIT;"
            )
            connection.executescript(wrapped)
        return True

    def table_exists(self, table: str) -> bool:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (table,),
            ).fetchone()
        return row is not None

    def table_columns(self, table: str) -> tuple[str, ...]:
        with self.connect() as connection:
            rows = connection.execute(f'PRAGMA table_info("{table}")').fetchall()
        return tuple(str(row[1]) for row in rows)

    def list_tables(self) -> tuple[str, ...]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            ).fetchall()
        return tuple(str(row[0]) for row in rows)

    @staticmethod
    def _revision_facts(
        connection: sqlite3.Connection,
        tables: Sequence[str],
    ) -> list[dict[str, Any]]:
        facts: list[dict[str, Any]] = []
        for table in sorted(set(tables)):
            if not connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (table,),
            ).fetchone():
                facts.append({"table": table, "missing": True})
                continue
            escaped_table = table.replace('"', '""')
            columns = {
                str(row[1])
                for row in connection.execute(f'PRAGMA table_info("{escaped_table}")').fetchall()
            }
            count = int(connection.execute(f'SELECT COUNT(*) FROM "{escaped_table}"').fetchone()[0])
            revision_column = next(
                (
                    candidate
                    for candidate in (
                        "updated_at",
                        "created_at",
                        "available_at",
                        "completed_at",
                        "event_id",
                        "revision",
                        "id",
                    )
                    if candidate in columns
                ),
                None,
            )
            maximum: Any = None
            if revision_column is not None and count:
                escaped_column = revision_column.replace('"', '""')
                maximum = connection.execute(
                    f'SELECT MAX("{escaped_column}") FROM "{escaped_table}"'
                ).fetchone()[0]
            facts.append(
                {
                    "table": table,
                    "count": count,
                    "revision_column": revision_column,
                    "maximum": maximum,
                }
            )
        return facts

    def revisions_for_table_groups(
        self,
        groups: Mapping[str, Sequence[str]],
    ) -> dict[str, str]:
        with self.connect() as connection:
            return {
                name: content_hash(self._revision_facts(connection, tables))
                for name, tables in groups.items()
            }

    def revision_for_tables(self, tables: Sequence[str]) -> str:
        return self.revisions_for_table_groups({"revision": tables})["revision"]

    def _insert_model(
        self,
        table: str,
        columns: Sequence[str],
        values: Sequence[Any],
        *,
        ignore_conflict: bool = False,
    ) -> bool:
        verb = "INSERT OR IGNORE" if ignore_conflict else "INSERT"
        placeholders = ",".join("?" for _ in columns)
        names = ",".join(f'"{column}"' for column in columns)
        with self.connect() as connection:
            cursor = connection.execute(
                f'{verb} INTO "{table}" ({names}) VALUES ({placeholders})',
                tuple(values),
            )
            return cursor.rowcount > 0

    def _load_model(
        self,
        model: type[ModelT],
        table: str,
        where: str,
        parameters: Sequence[Any],
    ) -> ModelT | None:
        with self.connect() as connection:
            row = connection.execute(
                f'SELECT payload_json FROM "{table}" WHERE {where}',
                tuple(parameters),
            ).fetchone()
        if row is None:
            return None
        return model.model_validate_json(str(row[0]))

    def save_preflight(self, receipt: InvestorSessionPreflightReceipt) -> bool:
        return self._insert_model(
            "investor_request_receipts",
            (
                "receipt_id",
                "request_id",
                "as_of",
                "source_revision_hash",
                "receipt_hash",
                "payload_json",
                "created_at",
            ),
            (
                receipt.receipt_id,
                receipt.request_id,
                receipt.as_of.isoformat(),
                content_hash(receipt.source_revision_vector),
                receipt.receipt_hash,
                canonical_json(receipt),
                utc_now().isoformat(),
            ),
            ignore_conflict=True,
        )

    def get_preflight_for_request(self, request_id: str) -> InvestorSessionPreflightReceipt | None:
        return self._load_model(
            InvestorSessionPreflightReceipt,
            "investor_request_receipts",
            "request_id=?",
            (request_id,),
        )

    def save_or_get_preflight(
        self,
        receipt: InvestorSessionPreflightReceipt,
    ) -> InvestorSessionPreflightReceipt:
        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO investor_request_receipts(
                    receipt_id, request_id, as_of, source_revision_hash,
                    receipt_hash, payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    receipt.receipt_id,
                    receipt.request_id,
                    receipt.as_of.isoformat(),
                    content_hash(receipt.source_revision_vector),
                    receipt.receipt_hash,
                    canonical_json(receipt),
                    utc_now().isoformat(),
                ),
            )
            if cursor.rowcount > 0:
                return receipt
            row = connection.execute(
                "SELECT payload_json FROM investor_request_receipts WHERE request_id=?",
                (receipt.request_id,),
            ).fetchone()
        if row is None:
            raise RuntimeError("preflight conflict without an existing receipt")
        existing = InvestorSessionPreflightReceipt.model_validate_json(str(row[0]))
        if existing.receipt_hash != receipt.receipt_hash:
            raise ValueError("request_id already has a different immutable preflight receipt")
        return existing

    def append_subject_event(self, event: ResearchSubjectEvent) -> ResearchSubjectEvent:
        inserted = self._insert_model(
            "research_subject_events",
            (
                "event_id",
                "instrument_id",
                "event_type",
                "lane",
                "available_at",
                "request_id",
                "artifact_id",
                "reason",
                "idempotency_key",
                "payload_json",
                "created_at",
            ),
            (
                event.event_id,
                event.instrument_id,
                event.event_type.value,
                event.lane.value,
                event.available_at.isoformat(),
                event.request_id,
                event.artifact_id,
                event.reason,
                event.idempotency_key,
                canonical_json(event),
                utc_now().isoformat(),
            ),
            ignore_conflict=True,
        )
        if inserted:
            return event
        existing = self._load_model(
            ResearchSubjectEvent,
            "research_subject_events",
            "idempotency_key=?",
            (event.idempotency_key,),
        )
        if existing is None:
            raise RuntimeError("subject event conflict without existing row")
        if existing != event:
            raise ValueError("subject event idempotency key reused with different payload")
        return existing

    def subject_events(
        self,
        *,
        instrument_id: str | None = None,
        event_types: Sequence[str] | None = None,
    ) -> tuple[ResearchSubjectEvent, ...]:
        clauses: list[str] = []
        parameters: list[Any] = []
        if instrument_id is not None:
            clauses.append("instrument_id=?")
            parameters.append(instrument_id)
        if event_types:
            placeholders = ",".join("?" for _ in event_types)
            clauses.append(f"event_type IN ({placeholders})")
            parameters.extend(event_types)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT payload_json FROM research_subject_events"
                + where
                + " ORDER BY available_at, event_id",
                tuple(parameters),
            ).fetchall()
        return tuple(ResearchSubjectEvent.model_validate_json(str(row[0])) for row in rows)

    def latest_subject_events(self) -> dict[str, ResearchSubjectEvent]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT payload_json
                FROM (
                    SELECT payload_json,
                           ROW_NUMBER() OVER (
                               PARTITION BY instrument_id
                               ORDER BY available_at DESC, event_id DESC
                           ) AS row_number
                    FROM research_subject_events
                )
                WHERE row_number = 1
                """
            ).fetchall()
        events = [ResearchSubjectEvent.model_validate_json(str(row[0])) for row in rows]
        return {event.instrument_id: event for event in events}

    def save_provisional_position(
        self, assertion: ProvisionalPositionAssertion
    ) -> ProvisionalPositionAssertion:
        inserted = self._insert_model(
            "provisional_position_assertions",
            (
                "assertion_id",
                "account_id",
                "lane",
                "instrument_id",
                "quantity",
                "asserted_at",
                "date_precision",
                "source_text",
                "status",
                "idempotency_key",
                "payload_json",
                "created_at",
            ),
            (
                assertion.assertion_id,
                assertion.account_id,
                assertion.lane.value,
                assertion.instrument_id,
                str(assertion.quantity),
                assertion.asserted_at.isoformat(),
                assertion.date_precision.value,
                assertion.source_text,
                assertion.status,
                assertion.idempotency_key,
                canonical_json(assertion),
                utc_now().isoformat(),
            ),
            ignore_conflict=True,
        )
        if inserted:
            return assertion
        existing = self._load_model(
            ProvisionalPositionAssertion,
            "provisional_position_assertions",
            "idempotency_key=?",
            (assertion.idempotency_key,),
        )
        if existing is None:
            raise RuntimeError("provisional position conflict without existing row")
        if existing != assertion:
            raise ValueError("provisional position idempotency key reused with different payload")
        return existing

    def save_estimated_cost(self, estimate: EstimatedCostBasisRange) -> bool:
        return self._insert_model(
            "estimated_cost_basis_ranges",
            (
                "estimate_id",
                "assertion_id",
                "low",
                "high",
                "currency",
                "method",
                "as_of",
                "source_artifact_id",
                "status",
                "payload_json",
                "created_at",
            ),
            (
                estimate.estimate_id,
                estimate.assertion_id,
                str(estimate.low),
                str(estimate.high),
                estimate.currency,
                estimate.method,
                estimate.as_of.isoformat(),
                estimate.source_artifact_id,
                estimate.status,
                canonical_json(estimate),
                utc_now().isoformat(),
            ),
            ignore_conflict=True,
        )

    def save_capability_plan(self, plan: CapabilityExecutionPlan) -> bool:
        return self._insert_model(
            "capability_execution_plans",
            (
                "plan_id",
                "request_id",
                "policy_version",
                "plan_hash",
                "payload_json",
                "created_at",
            ),
            (
                plan.plan_id,
                plan.request_id,
                plan.policy_version,
                plan.plan_hash,
                canonical_json(plan),
                utc_now().isoformat(),
            ),
            ignore_conflict=True,
        )

    def save_coverage_receipt(self, receipt: CapabilityCoverageReceipt) -> bool:
        return self._insert_model(
            "capability_coverage_receipts",
            (
                "receipt_id",
                "request_id",
                "plan_id",
                "policy_version",
                "receipt_hash",
                "coverage_complete",
                "required_coverage",
                "prohibited_call_count",
                "payload_json",
                "created_at",
            ),
            (
                receipt.receipt_id,
                receipt.request_id,
                receipt.plan_id,
                receipt.policy_version,
                receipt.receipt_hash,
                int(receipt.coverage_complete),
                receipt.required_capability_coverage,
                receipt.prohibited_call_count,
                canonical_json(receipt),
                utc_now().isoformat(),
            ),
            ignore_conflict=True,
        )

    def save_macro_release(self, release: MacroReleaseSnapshot) -> bool:
        if release.capture_mode not in {"LIVE", "RECORDED"}:
            raise ValueError("new macro releases require an explicit capture mode")
        if not release.capture_policy_hash:
            raise ValueError("new macro releases require a capture policy hash")
        return self._insert_model(
            "macro_release_snapshots_v2",
            (
                "release_id",
                "authority",
                "release_family",
                "source_url",
                "source_hash",
                "captured_at",
                "published_at",
                "parse_status",
                "payload_json",
                "created_at",
                "capture_mode",
                "capture_policy_hash",
            ),
            (
                release.release_id,
                release.authority,
                release.release_family,
                release.source_url,
                release.source_hash,
                release.captured_at.isoformat(),
                release.published_at.isoformat(),
                release.parse_status,
                canonical_json(release),
                utc_now().isoformat(),
                release.capture_mode,
                release.capture_policy_hash,
            ),
            ignore_conflict=True,
        )

    def macro_release_by_source(
        self,
        *,
        authority: str,
        release_family: str,
        source_hash: str,
        capture_mode: str | None = None,
        capture_policy_hash: str | None = None,
    ) -> MacroReleaseSnapshot | None:
        if (capture_mode is None) != (capture_policy_hash is None):
            raise ValueError(
                "macro release edition identity requires mode and policy hash together"
            )
        clauses = ["authority=?", "release_family=?", "source_hash=?"]
        parameters: list[Any] = [authority, release_family, source_hash]
        if capture_mode is not None and capture_policy_hash is not None:
            if capture_mode not in {"LIVE", "RECORDED"} or not capture_policy_hash:
                raise ValueError("invalid macro release edition identity")
            clauses.extend(("capture_mode=?", "capture_policy_hash=?"))
            parameters.extend((capture_mode, capture_policy_hash))
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT payload_json FROM macro_release_snapshots_v2 WHERE "
                + " AND ".join(clauses)
                + " ORDER BY captured_at, release_id",
                tuple(parameters),
            ).fetchall()
        if not rows:
            return None
        if len(rows) != 1:
            raise ValueError("ambiguous macro release source; edition identity is required")
        return MacroReleaseSnapshot.model_validate_json(str(rows[0][0]))

    def macro_releases(
        self,
        *,
        authority: str | None = None,
        release_family: str | None = None,
        available_at: datetime | None = None,
    ) -> tuple[MacroReleaseSnapshot, ...]:
        clauses: list[str] = []
        parameters: list[Any] = []
        if authority is not None:
            clauses.append("authority=?")
            parameters.append(authority)
        if release_family is not None:
            clauses.append("release_family=?")
            parameters.append(release_family)
        if available_at is not None:
            clauses.append("captured_at<=?")
            parameters.append(available_at.isoformat())
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT payload_json FROM macro_release_snapshots_v2"
                + where
                + " ORDER BY captured_at, published_at, release_id",
                tuple(parameters),
            ).fetchall()
        return tuple(MacroReleaseSnapshot.model_validate_json(str(row[0])) for row in rows)

    def save_regime_snapshot(self, snapshot: MarketRegimeSnapshotV2) -> bool:
        return self._insert_model(
            "market_regime_snapshots_v2",
            (
                "snapshot_id",
                "feature_snapshot_id",
                "as_of",
                "selected_state",
                "confidence",
                "coverage",
                "valid_from",
                "expires_at",
                "policy_version",
                "model_version",
                "payload_json",
                "created_at",
            ),
            (
                snapshot.snapshot_id,
                snapshot.feature_snapshot_id,
                snapshot.as_of.isoformat(),
                snapshot.selected_state.value,
                snapshot.confidence,
                snapshot.coverage,
                snapshot.valid_from.isoformat(),
                snapshot.expires_at.isoformat(),
                snapshot.policy_version,
                snapshot.model_version,
                canonical_json(snapshot),
                utc_now().isoformat(),
            ),
            ignore_conflict=True,
        )

    def latest_valid_regime(self, as_of: datetime) -> MarketRegimeSnapshotV2 | None:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT payload_json
                FROM market_regime_snapshots_v2
                WHERE valid_from <= ? AND expires_at >= ?
                ORDER BY as_of DESC, created_at DESC
                LIMIT 1
                """,
                (as_of.isoformat(), as_of.isoformat()),
            ).fetchone()
        if row is None:
            return None
        return MarketRegimeSnapshotV2.model_validate_json(str(row[0]))

    def save_regime_overlay(self, overlay: RegimeDecisionOverlay) -> bool:
        return self._insert_model(
            "regime_decision_overlays",
            (
                "overlay_id",
                "regime_snapshot_id",
                "policy_version",
                "payload_json",
                "created_at",
            ),
            (
                overlay.overlay_id,
                overlay.regime_snapshot_id,
                overlay.policy_version,
                canonical_json(overlay),
                utc_now().isoformat(),
            ),
            ignore_conflict=True,
        )

    def save_scheduled_policy(
        self, policy: ScheduledResearchPolicy, *, active: bool = True
    ) -> bool:
        return self._insert_model(
            "scheduled_research_policies",
            (
                "policy_id",
                "version",
                "policy_hash",
                "active",
                "payload_json",
                "created_at",
            ),
            (
                policy.policy_id,
                policy.version,
                policy.policy_hash,
                int(active),
                canonical_json(policy),
                utc_now().isoformat(),
            ),
            ignore_conflict=True,
        )

    def get_scheduled_policy(
        self, policy_id: str, version: str | None = None
    ) -> ScheduledResearchPolicy | None:
        if version is not None:
            return self._load_model(
                ScheduledResearchPolicy,
                "scheduled_research_policies",
                "policy_id=? AND version=?",
                (policy_id, version),
            )
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT payload_json
                FROM scheduled_research_policies
                WHERE policy_id=? AND active=1
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (policy_id,),
            ).fetchone()
        return None if row is None else ScheduledResearchPolicy.model_validate_json(str(row[0]))

    def save_binding(self, binding: ScheduledTaskBinding) -> bool:
        return self._insert_model(
            "scheduled_task_bindings",
            (
                "binding_id",
                "platform_task_id",
                "creation_mode",
                "execution_surface",
                "schedule_expression",
                "timezone",
                "policy_id",
                "policy_hash",
                "active",
                "payload_json",
                "created_at",
            ),
            (
                binding.binding_id,
                binding.platform_task_id,
                binding.creation_mode,
                binding.execution_surface,
                binding.schedule_expression,
                binding.timezone,
                binding.policy_id,
                binding.policy_hash,
                int(binding.active),
                canonical_json(binding),
                utc_now().isoformat(),
            ),
            ignore_conflict=True,
        )

    def get_binding(self, binding_id: str) -> ScheduledTaskBinding | None:
        return self._load_model(
            ScheduledTaskBinding,
            "scheduled_task_bindings",
            "binding_id=?",
            (binding_id,),
        )

    @staticmethod
    def _scheduled_checkpoint_scope_key(binding_id: str, schedule_bucket: str) -> str:
        return content_hash({"binding_id": binding_id, "schedule_bucket": schedule_bucket})

    @staticmethod
    def _scheduled_job_identity(request: ScheduledRunRequest) -> tuple[str, str]:
        input_hash = content_hash(
            {
                "binding_id": request.binding_id,
                "schedule_bucket": request.schedule_bucket,
                "window": request.window,
                "domains": tuple(sorted(domain.value for domain in request.domains)),
                "policy_hash": request.policy_hash,
                "run_id": request.run_id,
                "idempotency_key": request.idempotency_key,
            }
        )
        return f"scheduled-job:{input_hash}", input_hash

    def get_scheduled_checkpoint(
        self, binding_id: str, schedule_bucket: str
    ) -> dict[str, Any] | None:
        scope_key = self._scheduled_checkpoint_scope_key(binding_id, schedule_bucket)
        with self.connect() as connection:
            row = connection.execute(
                "SELECT job_id,status,cursor_json,object_hash,committed_at FROM checkpoint "
                "WHERE scope_type='scheduled-research-run' AND scope_key=?",
                (scope_key,),
            ).fetchone()
        if row is None:
            return None
        cursor = json.loads(str(row["cursor_json"]))
        request = ScheduledRunRequest.model_validate(cursor["request"])
        return {
            "job_id": None if row["job_id"] is None else str(row["job_id"]),
            "status": str(row["status"]),
            "request": request,
            "degradation_reasons": tuple(cursor.get("degradation_reasons", ())),
            "final_receipt_id": cursor.get("final_receipt_id"),
            "object_hash": None if row["object_hash"] is None else str(row["object_hash"]),
            "committed_at": str(row["committed_at"]),
        }

    def save_scheduled_checkpoint(
        self,
        request: ScheduledRunRequest,
        *,
        status: str,
        degradation_reasons: Sequence[str] = (),
        final_receipt_id: str | None = None,
        job_status: str = "PENDING",
    ) -> str:
        request = ScheduledRunRequest.model_validate(request)
        allowed_checkpoint_statuses = {"PENDING_INPUTS", "READY", "SUCCEEDED", "BLOCKED"}
        allowed_job_statuses = {"PENDING", "SUCCEEDED", "BLOCKED_MANUAL", "PERMANENT_FAILED"}
        if status not in allowed_checkpoint_statuses:
            raise ValueError("unsupported scheduled checkpoint status")
        if job_status not in allowed_job_statuses:
            raise ValueError("unsupported scheduled job status")
        if status == "SUCCEEDED" and not final_receipt_id:
            raise ValueError("successful scheduled checkpoint requires a final receipt")
        job_id, input_hash = self._scheduled_job_identity(request)
        scope_key = self._scheduled_checkpoint_scope_key(
            request.binding_id, request.schedule_bucket
        )
        checkpoint_id = content_hash(
            {"scope_type": "scheduled-research-run", "scope_key": scope_key}
        )
        cursor = {
            "request": request.model_dump(mode="json"),
            "degradation_reasons": list(degradation_reasons),
            "final_receipt_id": final_receipt_id,
        }
        now = utc_now().isoformat()
        object_hash = content_hash(cursor)
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT type,input_hash,status FROM job WHERE job_id=?",
                (job_id,),
            ).fetchone()
            if row is None:
                connection.execute(
                    "INSERT INTO job(job_id,type,status,priority,input_hash,created_at,updated_at) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (job_id, "SCHEDULED_RESEARCH", job_status, 0, input_hash, now, now),
                )
            else:
                if str(row["type"]) != "SCHEDULED_RESEARCH" or str(row["input_hash"]) != input_hash:
                    raise ValueError("scheduled job identity collision")
                connection.execute(
                    "UPDATE job SET status=?,updated_at=? WHERE job_id=?",
                    (job_status, now, job_id),
                )
            connection.execute(
                "INSERT INTO checkpoint("
                "checkpoint_id,job_id,scope_type,scope_key,cursor_json,status,object_hash,committed_at"
                ") VALUES(?,?,?,?,?,?,?,?) "
                "ON CONFLICT(scope_type,scope_key) DO UPDATE SET "
                "job_id=excluded.job_id,cursor_json=excluded.cursor_json,status=excluded.status,"
                "object_hash=excluded.object_hash,committed_at=excluded.committed_at",
                (
                    checkpoint_id,
                    job_id,
                    "scheduled-research-run",
                    scope_key,
                    json.dumps(cursor, ensure_ascii=False, separators=(",", ":"), sort_keys=True),
                    status,
                    object_hash,
                    now,
                ),
            )
        return checkpoint_id

    def completed_schedule_buckets(self, binding_id: str) -> set[str]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT schedule_bucket
                FROM scheduled_research_runs
                WHERE binding_id=?
                  AND outcome IN ('MATERIAL_CHANGE','NO_MATERIAL_CHANGE','BLOCKED','FAILED')
                """,
                (binding_id,),
            ).fetchall()
        return {str(row[0]) for row in rows}

    def save_scheduled_attempt(
        self,
        receipt: ScheduledRunReceipt,
        *,
        idempotency_key: str,
        attempt_hash: str,
    ) -> bool:
        if receipt.outcome.value != "DEGRADED":
            raise ValueError("only degraded scheduled runs belong in the attempt log")
        return self._insert_model(
            "scheduled_research_attempts",
            (
                "attempt_id",
                "run_id",
                "binding_id",
                "schedule_bucket",
                "idempotency_key",
                "outcome",
                "request_fingerprint",
                "attempt_hash",
                "payload_json",
                "attempted_at",
            ),
            (
                receipt.receipt_id,
                receipt.run_id,
                receipt.binding_id,
                receipt.schedule_bucket,
                idempotency_key,
                receipt.outcome.value,
                receipt.request_fingerprint,
                attempt_hash,
                canonical_json(receipt),
                receipt.completed_at.isoformat(),
            ),
            ignore_conflict=True,
        )

    def get_scheduled_attempt(self, attempt_hash: str) -> ScheduledRunReceipt | None:
        return self._load_model(
            ScheduledRunReceipt,
            "scheduled_research_attempts",
            "attempt_hash=?",
            (attempt_hash,),
        )

    def scheduled_attempts(
        self, binding_id: str, schedule_bucket: str
    ) -> tuple[ScheduledRunReceipt, ...]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT payload_json FROM scheduled_research_attempts "
                "WHERE binding_id=? AND schedule_bucket=? ORDER BY attempted_at,attempt_id",
                (binding_id, schedule_bucket),
            ).fetchall()
        return tuple(ScheduledRunReceipt.model_validate_json(str(row[0])) for row in rows)

    def latest_scheduled_receipt(self, binding_id: str) -> ScheduledRunReceipt | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT payload_json FROM scheduled_research_runs WHERE binding_id=? "
                "ORDER BY completed_at DESC,receipt_id DESC LIMIT 1",
                (binding_id,),
            ).fetchone()
        return None if row is None else ScheduledRunReceipt.model_validate_json(str(row[0]))

    def pending_scheduled_requests(self, binding_id: str) -> tuple[ScheduledRunRequest, ...]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT cursor_json FROM checkpoint WHERE scope_type='scheduled-research-run' "
                "AND status IN ('PENDING_INPUTS','READY') "
                "AND json_extract(cursor_json,'$.request.binding_id')=? "
                "ORDER BY committed_at,checkpoint_id",
                (binding_id,),
            ).fetchall()
        return tuple(
            ScheduledRunRequest.model_validate(json.loads(str(row[0]))["request"]) for row in rows
        )

    def get_scheduled_receipt(
        self,
        *,
        idempotency_key: str | None = None,
        binding_id: str | None = None,
        schedule_bucket: str | None = None,
    ) -> ScheduledRunReceipt | None:
        if idempotency_key is not None:
            return self._load_model(
                ScheduledRunReceipt,
                "scheduled_research_runs",
                "idempotency_key=?",
                (idempotency_key,),
            )
        if binding_id is None or schedule_bucket is None:
            raise ValueError("binding_id and schedule_bucket are required together")
        return self._load_model(
            ScheduledRunReceipt,
            "scheduled_research_runs",
            "binding_id=? AND schedule_bucket=?",
            (binding_id, schedule_bucket),
        )

    def save_scheduled_receipt(self, receipt: ScheduledRunReceipt, *, idempotency_key: str) -> bool:
        return self._insert_model(
            "scheduled_research_runs",
            (
                "receipt_id",
                "run_id",
                "binding_id",
                "schedule_bucket",
                "idempotency_key",
                "outcome",
                "next_watermark",
                "notification_required",
                "economic_write_count",
                "receipt_hash",
                "payload_json",
                "completed_at",
            ),
            (
                receipt.receipt_id,
                receipt.run_id,
                receipt.binding_id,
                receipt.schedule_bucket,
                idempotency_key,
                receipt.outcome.value,
                receipt.next_watermark,
                int(receipt.notification_required),
                receipt.economic_write_count,
                receipt.receipt_hash,
                canonical_json(receipt),
                receipt.completed_at.isoformat(),
            ),
            ignore_conflict=True,
        )

    def save_watchlist_revision(self, revision: WatchlistAnalysisRevision) -> bool:
        return self._insert_model(
            "watchlist_analysis_revisions",
            (
                "revision_id",
                "instrument_id",
                "as_of",
                "thesis_status",
                "previous_revision_id",
                "revision_hash",
                "payload_json",
                "created_at",
            ),
            (
                revision.revision_id,
                revision.instrument_id,
                revision.as_of.isoformat(),
                revision.thesis_status,
                revision.previous_revision_id,
                revision.revision_hash,
                canonical_json(revision),
                utc_now().isoformat(),
            ),
            ignore_conflict=True,
        )

    def latest_watchlist_revision(self, instrument_id: str) -> WatchlistAnalysisRevision | None:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT payload_json
                FROM watchlist_analysis_revisions
                WHERE instrument_id=?
                ORDER BY as_of DESC, revision_id DESC
                LIMIT 1
                """,
                (instrument_id,),
            ).fetchone()
        return None if row is None else WatchlistAnalysisRevision.model_validate_json(str(row[0]))

    def save_material_digest(self, digest: MaterialChangeDigest) -> bool:
        return self._insert_model(
            "material_change_digests",
            (
                "digest_id",
                "run_id",
                "domain",
                "instrument_id",
                "as_of",
                "severity",
                "action",
                "payload_json",
                "created_at",
            ),
            (
                digest.digest_id,
                digest.run_id,
                digest.domain.value,
                digest.instrument_id,
                digest.as_of.isoformat(),
                digest.severity,
                digest.action,
                canonical_json(digest),
                utc_now().isoformat(),
            ),
            ignore_conflict=True,
        )

    def save_shadow_observation(self, observation: ShadowObservation) -> bool:
        return self._insert_model(
            "orchestration_shadow_observations",
            (
                "observation_id",
                "feature_id",
                "observed_at",
                "request_or_run_id",
                "label_change_only_action",
                "required_capability_coverage",
                "prohibited_call_count",
                "economic_write_count",
                "material_error",
                "payload_json",
                "created_at",
            ),
            (
                observation.observation_id,
                observation.feature_id,
                observation.observed_at.isoformat(),
                observation.request_or_run_id,
                int(observation.label_change_only_action),
                observation.required_capability_coverage,
                observation.prohibited_call_count,
                observation.economic_write_count,
                int(observation.material_error),
                canonical_json(observation),
                utc_now().isoformat(),
            ),
            ignore_conflict=True,
        )

    def shadow_observations(self, feature_id: str) -> tuple[ShadowObservation, ...]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT payload_json
                FROM orchestration_shadow_observations
                WHERE feature_id=?
                ORDER BY observed_at, observation_id
                """,
                (feature_id,),
            ).fetchall()
        return tuple(ShadowObservation.model_validate_json(str(row[0])) for row in rows)

    def save_controlled_live_check(self, check: ControlledLiveCheck) -> bool:
        return self._insert_model(
            "controlled_live_checks",
            (
                "check_id",
                "check_type",
                "checked_at",
                "status",
                "payload_json",
                "created_at",
            ),
            (
                check.check_id,
                check.check_type,
                check.checked_at.isoformat(),
                check.status,
                canonical_json(check),
                utc_now().isoformat(),
            ),
            ignore_conflict=True,
        )

    def latest_controlled_live_checks(self) -> dict[str, ControlledLiveCheck]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT payload_json
                FROM (
                    SELECT payload_json,
                           ROW_NUMBER() OVER (
                               PARTITION BY check_type
                               ORDER BY checked_at DESC, check_id DESC
                           ) AS row_number
                    FROM controlled_live_checks
                )
                WHERE row_number=1
                """
            ).fetchall()
        checks = [ControlledLiveCheck.model_validate_json(str(row[0])) for row in rows]
        return {check.check_type: check for check in checks}

    def save_activation_receipt(self, receipt: FeatureActivationReceipt) -> bool:
        return self._insert_model(
            "feature_activation_receipts",
            (
                "receipt_id",
                "feature_id",
                "previous_status",
                "new_status",
                "changed_at",
                "assessment_id",
                "owner_approval_id",
                "ledger_write_count",
                "receipt_hash",
                "payload_json",
                "created_at",
            ),
            (
                receipt.receipt_id,
                receipt.feature_id,
                receipt.previous_status.value,
                receipt.new_status.value,
                receipt.changed_at.isoformat(),
                receipt.assessment_id,
                receipt.owner_approval_id,
                receipt.ledger_write_count,
                receipt.receipt_hash,
                canonical_json(receipt),
                utc_now().isoformat(),
            ),
            ignore_conflict=True,
        )

    def latest_activation_receipt(self, feature_id: str) -> FeatureActivationReceipt | None:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT payload_json
                FROM feature_activation_receipts
                WHERE feature_id=?
                ORDER BY changed_at DESC, receipt_id DESC
                LIMIT 1
                """,
                (feature_id,),
            ).fetchone()
        return None if row is None else FeatureActivationReceipt.model_validate_json(str(row[0]))

    def audit_integrity(self) -> dict[str, Any]:
        required_tables = {
            "orchestration_schema_migrations",
            "investor_request_receipts",
            "research_subject_events",
            "provisional_position_assertions",
            "estimated_cost_basis_ranges",
            "capability_execution_plans",
            "capability_coverage_receipts",
            "macro_release_snapshots_v2",
            "market_regime_snapshots_v2",
            "regime_decision_overlays",
            "scheduled_research_policies",
            "scheduled_task_bindings",
            "scheduled_task_binding_events",
            "scheduled_research_runs",
            "watchlist_analysis_revisions",
            "scheduled_notifications",
            "orchestration_shadow_observations",
            "controlled_live_checks",
            "feature_activation_receipts",
            "material_change_digests",
        }
        required_triggers = {
            "research_subject_events_no_update",
            "research_subject_events_no_delete",
            "provisional_position_assertions_no_update",
            "provisional_position_assertions_no_delete",
            "estimated_cost_basis_ranges_no_update",
            "estimated_cost_basis_ranges_no_delete",
            "scheduled_task_binding_events_no_update",
            "scheduled_task_binding_events_no_delete",
            "watchlist_analysis_revisions_no_update",
            "watchlist_analysis_revisions_no_delete",
        }
        migration = (
            Path(__file__).resolve().parents[3] / "migrations" / "0067_investor_orchestration.sql"
        )
        expected_checksum = hashlib.sha256(migration.read_bytes()).hexdigest()
        with self.connect() as connection:
            quick_rows = connection.execute("PRAGMA quick_check").fetchall()
            quick_check = tuple(str(row[0]) for row in quick_rows)
            foreign_key_violations = tuple(
                tuple(row) for row in connection.execute("PRAGMA foreign_key_check").fetchall()
            )
            objects = connection.execute(
                "SELECT type,name FROM sqlite_master WHERE type IN ('table','trigger')"
            ).fetchall()
            tables = {str(row[1]) for row in objects if str(row[0]) == "table"}
            triggers = {str(row[1]) for row in objects if str(row[0]) == "trigger"}
            migration_row = connection.execute(
                "SELECT checksum FROM orchestration_schema_migrations WHERE version=?",
                ("0067_investor_orchestration",),
            ).fetchone()
            conflicting_policies = tuple(
                str(row[0])
                for row in connection.execute(
                    "SELECT policy_id FROM scheduled_research_policies "
                    "GROUP BY policy_id HAVING SUM(active) > 1"
                ).fetchall()
            )
            scheduled_write_violations = int(
                connection.execute(
                    "SELECT COUNT(*) FROM scheduled_research_runs WHERE economic_write_count <> 0"
                ).fetchone()[0]
            )
            activation_write_violations = int(
                connection.execute(
                    "SELECT COUNT(*) FROM feature_activation_receipts WHERE ledger_write_count <> 0"
                ).fetchone()[0]
            )
        missing_tables = tuple(sorted(required_tables - tables))
        missing_triggers = tuple(sorted(required_triggers - triggers))
        recorded_checksum = None if migration_row is None else str(migration_row[0])
        checks = {
            "quick_check": quick_check == ("ok",),
            "foreign_keys": not foreign_key_violations,
            "required_tables": not missing_tables,
            "append_only_triggers": not missing_triggers,
            "migration_checksum": recorded_checksum == expected_checksum,
            "single_active_policy": not conflicting_policies,
            "scheduled_economic_writes": scheduled_write_violations == 0,
            "activation_ledger_writes": activation_write_violations == 0,
        }
        return {
            "database": str(self.path),
            "status": "PASS" if all(checks.values()) else "FAIL",
            "checks": checks,
            "details": {
                "quick_check": quick_check,
                "foreign_key_violations": foreign_key_violations,
                "missing_tables": missing_tables,
                "missing_triggers": missing_triggers,
                "recorded_migration_checksum": recorded_checksum,
                "expected_migration_checksum": expected_checksum,
                "conflicting_active_policies": conflicting_policies,
                "scheduled_write_violations": scheduled_write_violations,
                "activation_write_violations": activation_write_violations,
            },
        }

    def export_summary(self) -> dict[str, Any]:
        tracked = (
            "investor_request_receipts",
            "research_subject_events",
            "provisional_position_assertions",
            "capability_execution_plans",
            "capability_coverage_receipts",
            "macro_release_snapshots_v2",
            "market_regime_snapshots_v2",
            "scheduled_task_bindings",
            "scheduled_research_runs",
            "scheduled_notifications",
            "orchestration_shadow_observations",
            "controlled_live_checks",
            "feature_activation_receipts",
            "watchlist_analysis_revisions",
        )
        summary: dict[str, Any] = {"database": str(self.path), "tables": {}}
        with self.connect() as connection:
            for table in tracked:
                exists = connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                    (table,),
                ).fetchone()
                summary["tables"][table] = (
                    int(connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
                    if exists
                    else None
                )
        summary["summary_hash"] = content_hash(summary["tables"])
        return json.loads(json.dumps(summary, ensure_ascii=False))
