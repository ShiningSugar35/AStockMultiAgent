"""Append-only external-account repository, deterministic replay, and atomic imports."""

from __future__ import annotations

import csv
import io
import json
import sqlite3
from collections.abc import Iterable, Sequence
from contextlib import closing
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal, Protocol, cast

import yaml

from astock.core.atomic import atomic_write_text
from astock.core.hashing import sha256_bytes
from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.schemas.external_accounts import (
    ExternalAccountEvent,
    ExternalAccountEventDraft,
    ExternalAccountEventType,
    ExternalAccountIdentity,
    ExternalAccountImportFormat,
    ExternalAccountImportPreview,
    ExternalAccountImportReceipt,
    ExternalAccountKind,
    ExternalAccountPosition,
    ExternalAccountProjection,
    ExternalAccountStatus,
    bind_external_account_event,
    external_import_batch_id,
)
from astock.schemas.market import Market

_SECRET_FIELD_NAMES = {
    "authorization",
    "broker_password",
    "cookie",
    "credential",
    "credentials",
    "password",
    "secret",
    "token",
    "api_key",
    "apikey",
}


class ExternalAccountConflictError(ValueError):
    """Raised when an append/import conflicts with immutable account facts."""


class LegacyLocalTradeSource(Protocol):
    """Minimal compatibility surface for migrating the former single-account lane."""

    def trade_facts(self) -> list[dict[str, Any]]: ...


class ExternalAccountRepository:
    """SQLite-backed authoritative external-account event service."""

    def __init__(self, state: StateStore, objects: ObjectStore) -> None:
        self.state = state
        self.objects = objects

    def create_account(
        self,
        *,
        account_id: str,
        display_name: str,
        account_kind: ExternalAccountKind = ExternalAccountKind.MANUAL,
        base_currency: Literal["CNY"] = "CNY",
        status: ExternalAccountStatus = ExternalAccountStatus.ACTIVE,
        created_at: datetime | None = None,
    ) -> ExternalAccountIdentity:
        now = created_at or datetime.now(UTC)
        identity = ExternalAccountIdentity(
            account_id=account_id,
            display_name=display_name,
            account_kind=account_kind,
            base_currency=base_currency,
            status=status,
            created_at=now,
            updated_at=now,
        )
        with self.state.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM external_account WHERE account_id=?",
                (identity.account_id,),
            ).fetchone()
            if row is not None:
                existing = self._account_from_row(row)
                if existing.model_dump(mode="json") != identity.model_dump(mode="json"):
                    stable_existing = existing.model_dump(
                        mode="json", exclude={"created_at", "updated_at"}
                    )
                    stable_new = identity.model_dump(
                        mode="json", exclude={"created_at", "updated_at"}
                    )
                    if stable_existing != stable_new:
                        raise ExternalAccountConflictError(
                            f"external account identity conflict: {account_id}"
                        )
                return existing
            connection.execute(
                """
                INSERT INTO external_account(
                    account_id, display_name, account_kind, base_currency,
                    status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    identity.account_id,
                    identity.display_name,
                    identity.account_kind.value,
                    identity.base_currency,
                    identity.status.value,
                    identity.created_at.isoformat(),
                    identity.updated_at.isoformat(),
                ),
            )
        return identity

    def get_account(self, account_id: str) -> ExternalAccountIdentity | None:
        with closing(self.state.connect()) as connection:
            row = connection.execute(
                "SELECT * FROM external_account WHERE account_id=?",
                (account_id,),
            ).fetchone()
        return self._account_from_row(row) if row is not None else None

    def list_accounts(self) -> list[ExternalAccountIdentity]:
        with closing(self.state.connect()) as connection:
            rows = connection.execute(
                "SELECT * FROM external_account ORDER BY account_id"
            ).fetchall()
        return [self._account_from_row(row) for row in rows]

    def append_drafts(
        self,
        drafts: Sequence[ExternalAccountEventDraft],
    ) -> tuple[list[str], list[str]]:
        prepared = [
            item if item.sequence_no is not None else item.model_copy(update={"sequence_no": index})
            for index, item in enumerate(drafts)
        ]
        events = [bind_external_account_event(item) for item in prepared]
        with self.state.transaction() as connection:
            return self._validate_and_insert(connection, events)

    def append_events(
        self,
        events: Sequence[ExternalAccountEvent],
    ) -> tuple[list[str], list[str]]:
        with self.state.transaction() as connection:
            return self._validate_and_insert(connection, events)

    def list_events(
        self,
        account_id: str,
        *,
        as_of: datetime | None = None,
    ) -> list[ExternalAccountEvent]:
        parameters: list[object] = [account_id]
        where = "account_id=?"
        if as_of is not None:
            where += " AND available_to_system_at<=?"
            parameters.append(as_of.isoformat())
        with closing(self.state.connect()) as connection:
            rows = connection.execute(
                f"""
                SELECT payload_json
                FROM external_account_event
                WHERE {where}
                ORDER BY occurred_at, sequence_no, available_to_system_at, event_id
                """,
                tuple(parameters),
            ).fetchall()
        return [ExternalAccountEvent.model_validate_json(str(row["payload_json"])) for row in rows]

    def projection(
        self,
        account_id: str,
        *,
        as_of: datetime | None = None,
    ) -> ExternalAccountProjection:
        cutoff = as_of or datetime.now(UTC)
        if self.get_account(account_id) is None:
            raise ValueError(f"external account does not exist: {account_id}")
        events = self.list_events(account_id, as_of=cutoff)
        return replay_external_account_events(account_id, events, as_of=cutoff)

    def migrate_legacy_default_account(
        self,
        source: LegacyLocalTradeSource,
        *,
        migrated_at: datetime | None = None,
    ) -> dict[str, object]:
        """Import the former single-account IMPORT lane exactly once into ``default``.

        PAPER_FILL rows stay in the paper ledger lane. The migration uses the legacy
        trade id as its stable idempotency key, so a later retry never rewrites event
        history merely because the migration timestamp changed.
        """

        now = migrated_at or datetime.now(UTC)
        if self.get_account("default") is None:
            self.create_account(
                account_id="default",
                display_name="默认外部账户",
                account_kind=ExternalAccountKind.MANUAL,
                created_at=now,
            )
        existing_events = self.list_events("default")
        existing_by_key = {item.idempotency_key: item for item in existing_events}
        existing_by_user_declared_note = {
            item.note: item
            for item in existing_events
            if item.note.startswith("user-declared:")
        }
        existing_by_economic_key = {
            (
                item.market,
                item.symbol,
                item.side,
                item.quantity,
                item.price_cny,
                item.occurred_at.astimezone(UTC),
            ): item
            for item in existing_events
            if item.event_type is ExternalAccountEventType.TRADE
            and item.market is not None
            and item.symbol is not None
            and item.side is not None
            and item.quantity is not None
            and item.price_cny is not None
        }
        drafts: list[ExternalAccountEventDraft] = []
        duplicate_event_ids: list[str] = []
        skipped_paper = 0
        for index, trade in enumerate(source.trade_facts(), start=1):
            trade_source = str(trade.get("source") or "").strip().upper()
            if trade_source == "PAPER_FILL":
                skipped_paper += 1
                continue
            if trade_source != "IMPORT":
                raise ValueError(f"unsupported legacy local trade source: {trade_source}")
            trade_id = str(trade.get("trade_id") or "").strip()
            if not trade_id:
                raise ValueError("legacy local trade is missing trade_id")
            legacy_note = str(trade.get("note") or "").strip()
            represented = existing_by_user_declared_note.get(legacy_note)
            if represented is not None:
                duplicate_event_ids.append(represented.event_id)
                continue
            idempotency_key = f"legacy-local:{trade_id}"
            existing = existing_by_key.get(idempotency_key)
            if existing is not None:
                duplicate_event_ids.append(existing.event_id)
                continue
            occurred_at = datetime.fromisoformat(str(trade.get("occurred_at")))
            if occurred_at.tzinfo is None or occurred_at.utcoffset() is None:
                raise ValueError("legacy local trade occurred_at must be timezone-aware")
            raw_side = str(trade.get("side") or "").strip().upper()
            if raw_side not in {"BUY", "SELL"}:
                raise ValueError("legacy local trade side must be BUY or SELL")
            side = cast(Literal["BUY", "SELL"], raw_side)
            market = Market(str(trade.get("market") or "").strip().upper())
            symbol = str(trade.get("symbol") or "").strip()
            quantity = int(trade.get("quantity") or 0)
            price_cny = Decimal(str(trade.get("price_cny")))
            economic_key = (
                market,
                symbol,
                side,
                quantity,
                price_cny,
                occurred_at.astimezone(UTC),
            )
            represented_economically = existing_by_economic_key.get(economic_key)
            if represented_economically is not None:
                duplicate_event_ids.append(represented_economically.event_id)
                continue
            available_at = max(now, occurred_at)
            drafts.append(
                ExternalAccountEventDraft(
                    account_id="default",
                    event_type=ExternalAccountEventType.TRADE,
                    occurred_at=occurred_at,
                    sequence_no=int(trade.get("ordinal") or index),
                    available_to_system_at=available_at,
                    market=market,
                    symbol=symbol,
                    side=side,
                    quantity=quantity,
                    price_cny=price_cny,
                    idempotency_key=idempotency_key,
                    note=f"legacy-local-projection:{trade_id}",
                    created_at=now,
                )
            )
        inserted, duplicates = self.append_drafts(drafts) if drafts else ([], [])
        return {
            "status": "MIGRATED" if inserted else "ALREADY_MIGRATED",
            "account_id": "default",
            "inserted_event_ids": sorted(inserted),
            "duplicate_event_ids": sorted({*duplicate_event_ids, *duplicates}),
            "skipped_paper_fill_trade_count": skipped_paper,
            "broker_execution_allowed": False,
            "paper_ledger_write_allowed": False,
        }

    def write_markdown_projection(
        self,
        project_root: Path,
        *,
        as_of: datetime | None = None,
    ) -> dict[str, object]:
        """Write a Git-ignored human-readable projection; SQLite remains authoritative."""

        cutoff = as_of or datetime.now(UTC)
        root = project_root.resolve()
        target = (root / "user_state" / "external_accounts.md").resolve()
        if not target.is_relative_to(root):
            raise ValueError("external account projection escaped the project root")
        accounts = self.list_accounts()
        projections = [self.projection(item.account_id, as_of=cutoff) for item in accounts]
        payload = {
            "schema_version": "astock-external-account-projection-v1",
            "updated_at": cutoff.isoformat(),
            "accounts": [item.model_dump(mode="json") for item in projections],
        }
        lines = [
            "# 外部账户本地投影",
            "",
            (
                "此文件仅保存在本机、不进入 Git，是 SQLite `external_account_event` "
                "的只读投影；不得反向编辑制造交易事实。"
            ),
            "",
        ]
        names = {item.account_id: item.display_name for item in accounts}
        for projection in projections:
            display_name = names.get(projection.account_id, projection.account_id)
            lines.extend(
                [
                    f"## {projection.account_id} · {display_name}",
                    "",
                    (
                        f"现金：{projection.cash_cny} CNY"
                        if projection.cash_known
                        else "现金：未知（尚无足够现金事实）"
                    ),
                    "",
                ]
            )
            if projection.positions:
                lines.extend(
                    [
                        "| 市场 | 标的 | 数量 | 平均成本(CNY) |",
                        "|---|---|---:|---:|",
                    ]
                )
                for position in projection.positions:
                    lines.append(
                        f"| {position.market.value} | {position.symbol} | {position.quantity} | "
                        f"{position.average_cost_cny} |"
                    )
            else:
                lines.append("当前无持仓。")
            lines.append("")
        front = yaml.safe_dump(payload, allow_unicode=True, sort_keys=False).rstrip()
        body = "\n".join(lines).rstrip()
        target.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(target, f"---\n{front}\n---\n\n{body}\n")
        return {
            "status": "PROJECTED",
            "relative_path": "user_state/external_accounts.md",
            "account_count": len(accounts),
            "as_of": cutoff.isoformat(),
        }

    def preview_import(
        self,
        source_path: Path,
        *,
        source_format: ExternalAccountImportFormat | None = None,
        previewed_at: datetime | None = None,
    ) -> ExternalAccountImportPreview:
        path = source_path.resolve()
        raw = path.read_bytes()
        raw_ref = self.objects.put_bytes(raw)
        format_value = source_format or _infer_import_format(path)
        existing = self._preview_by_source(format_value, raw_ref.sha256)
        if existing is not None:
            return existing
        rows = _parse_import_rows(raw, format_value)
        now = previewed_at or datetime.now(UTC)
        events = [
            bind_external_account_event(
                _draft_from_import_row(
                    row,
                    row_number=index,
                    source_artifact_hash=raw_ref.sha256,
                    available_at=now,
                )
            )
            for index, row in enumerate(rows, start=1)
        ]
        _ensure_unique_batch_events(events)
        account_ids = sorted({item.account_id for item in events})
        missing_accounts = [item for item in account_ids if self.get_account(item) is None]
        if missing_accounts:
            raise ValueError(
                "external import references unknown accounts: " + ", ".join(missing_accounts)
            )
        normalized_ref = self.objects.put_json([item.model_dump(mode="json") for item in events])
        batch_id = external_import_batch_id(
            source_format=format_value,
            source_object_hash=raw_ref.sha256,
            normalized_object_hash=normalized_ref.sha256,
            events=events,
        )
        preview = ExternalAccountImportPreview(
            batch_id=batch_id,
            source_format=format_value,
            source_object_hash=raw_ref.sha256,
            normalized_object_hash=normalized_ref.sha256,
            row_count=len(events),
            account_ids=account_ids,
            event_ids=sorted(item.event_id for item in events),
            created_at=now,
        )
        with self.state.transaction() as connection:
            row = connection.execute(
                "SELECT preview_json, status FROM external_account_import_batch WHERE batch_id=?",
                (batch_id,),
            ).fetchone()
            if row is None:
                connection.execute(
                    """
                    INSERT INTO external_account_import_batch(
                        batch_id, source_format, source_object_hash,
                        normalized_object_hash, row_count, status,
                        preview_json, created_at, imported_at
                    ) VALUES (?, ?, ?, ?, ?, 'PREVIEWED', ?, ?, NULL)
                    """,
                    (
                        batch_id,
                        format_value.value,
                        raw_ref.sha256,
                        normalized_ref.sha256,
                        len(events),
                        preview.model_dump_json(),
                        now.isoformat(),
                    ),
                )
            else:
                stored = ExternalAccountImportPreview.model_validate_json(str(row["preview_json"]))
                return stored.model_copy(update={"already_imported": row["status"] == "IMPORTED"})
        return preview

    def confirm_import(
        self,
        batch_id: str,
        *,
        source_path: Path | None = None,
        confirmed_at: datetime | None = None,
    ) -> ExternalAccountImportReceipt:
        with closing(self.state.connect()) as connection:
            row = connection.execute(
                "SELECT * FROM external_account_import_batch WHERE batch_id=?",
                (batch_id,),
            ).fetchone()
        if row is None:
            raise ValueError(f"external import preview does not exist: {batch_id}")
        source_hash = str(row["source_object_hash"])
        normalized_hash = str(row["normalized_object_hash"])
        if source_path is not None:
            current_hash = sha256_bytes(source_path.resolve().read_bytes())
            if current_hash != source_hash:
                raise ExternalAccountConflictError("external import source changed after preview")
        raw_events = json.loads(self.objects.get_bytes(normalized_hash).decode("utf-8"))
        if not isinstance(raw_events, list):
            raise RuntimeError("external import normalized object is not an event list")
        events = [ExternalAccountEvent.model_validate(item) for item in raw_events]
        preview = ExternalAccountImportPreview.model_validate_json(str(row["preview_json"]))
        if sorted(item.event_id for item in events) != preview.event_ids:
            raise RuntimeError("external import normalized event identity drift")
        now = confirmed_at or datetime.now(UTC)
        with self.state.transaction() as connection:
            locked = connection.execute(
                "SELECT status FROM external_account_import_batch WHERE batch_id=?",
                (batch_id,),
            ).fetchone()
            if locked is None:
                raise RuntimeError("external import preview disappeared")
            if locked["status"] == "IMPORTED":
                return ExternalAccountImportReceipt(
                    batch_id=batch_id,
                    status="DUPLICATE",
                    source_object_hash=source_hash,
                    normalized_object_hash=normalized_hash,
                    inserted_event_ids=[],
                    duplicate_event_ids=preview.event_ids,
                    created_at=now,
                )
            inserted, duplicates = self._validate_and_insert(connection, events)
            connection.execute(
                """
                UPDATE external_account_import_batch
                SET status='IMPORTED', imported_at=?
                WHERE batch_id=? AND status='PREVIEWED'
                """,
                (now.isoformat(), batch_id),
            )
        return ExternalAccountImportReceipt(
            batch_id=batch_id,
            status="IMPORTED" if inserted else "DUPLICATE",
            source_object_hash=source_hash,
            normalized_object_hash=normalized_hash,
            inserted_event_ids=sorted(inserted),
            duplicate_event_ids=sorted(duplicates),
            created_at=now,
        )

    def audit(self, account_id: str) -> dict[str, object]:
        events = self.list_events(account_id)
        findings: list[str] = []
        by_id = {item.event_id: item for item in events}
        if len(by_id) != len(events):
            findings.append("DUPLICATE_EVENT_ID")
        for event in events:
            for target_id in (event.reverses_event_id, event.replaces_event_id):
                if target_id is None:
                    continue
                target = by_id.get(target_id)
                if target is None:
                    findings.append("CORRECTION_TARGET_MISSING")
                elif target.account_id != event.account_id:
                    findings.append("CORRECTION_TARGET_ACCOUNT_MISMATCH")
        try:
            projection = replay_external_account_events(account_id, events)
        except ValueError:
            projection = None
            findings.append("EVENT_REPLAY_FAILED")
        return {
            "status": "PASS" if not findings else "FAIL",
            "account_id": account_id,
            "event_count": len(events),
            "findings": sorted(set(findings)),
            "projection": projection.model_dump(mode="json") if projection is not None else None,
        }

    def _preview_by_source(
        self,
        source_format: ExternalAccountImportFormat,
        source_object_hash: str,
    ) -> ExternalAccountImportPreview | None:
        with closing(self.state.connect()) as connection:
            row = connection.execute(
                """
                SELECT preview_json, status
                FROM external_account_import_batch
                WHERE source_format=? AND source_object_hash=?
                ORDER BY created_at DESC
                LIMIT 1
                """,
                (source_format.value, source_object_hash),
            ).fetchone()
        if row is None:
            return None
        preview = ExternalAccountImportPreview.model_validate_json(str(row["preview_json"]))
        return preview.model_copy(update={"already_imported": row["status"] == "IMPORTED"})

    def _validate_and_insert(
        self,
        connection: sqlite3.Connection,
        events: Sequence[ExternalAccountEvent],
    ) -> tuple[list[str], list[str]]:
        _ensure_unique_batch_events(events)
        existing_events = self._all_events(connection)
        existing_by_id = {item.event_id: item for item in existing_events}
        existing_by_key = {
            (item.account_id, item.idempotency_key): item for item in existing_events
        }
        pending_by_id = {item.event_id: item for item in events}
        inserted: list[str] = []
        duplicates: list[str] = []
        new_events: list[ExternalAccountEvent] = []

        account_ids = sorted({item.account_id for item in events})
        if account_ids:
            placeholders = ",".join("?" for _ in account_ids)
            query = (
                "SELECT account_id, status FROM external_account "
                f"WHERE account_id IN ({placeholders})"
            )
            rows = connection.execute(query, tuple(account_ids)).fetchall()
            statuses = {str(row["account_id"]): str(row["status"]) for row in rows}
            missing = [item for item in account_ids if item not in statuses]
            if missing:
                raise ValueError("external accounts do not exist: " + ", ".join(missing))
            closed = [
                item for item in account_ids if statuses[item] != ExternalAccountStatus.ACTIVE.value
            ]
            if closed:
                raise ExternalAccountConflictError(
                    "external accounts are not active: " + ", ".join(closed)
                )

        for event in events:
            existing = existing_by_id.get(event.event_id)
            keyed = existing_by_key.get((event.account_id, event.idempotency_key))
            if existing is not None:
                duplicates.append(event.event_id)
                continue
            if keyed is not None:
                identity = f"{event.account_id}/{event.idempotency_key}"
                raise ExternalAccountConflictError(
                    f"external account idempotency conflict: {identity}"
                )
            new_events.append(event)

        for event in new_events:
            self._reject_paper_fill_conflict(connection, event)

        combined_by_id = {**existing_by_id, **pending_by_id}
        corrected_targets = {
            target_id
            for item in existing_events
            for target_id in (item.reverses_event_id, item.replaces_event_id)
            if target_id is not None
        }
        pending_corrections: set[str] = set()
        for event in new_events:
            target_id = event.reverses_event_id or event.replaces_event_id
            if target_id is None:
                continue
            target = combined_by_id.get(target_id)
            if target is None:
                raise ExternalAccountConflictError(f"correction target does not exist: {target_id}")
            if target.account_id != event.account_id:
                raise ExternalAccountConflictError("correction target belongs to another account")
            if target.event_type is ExternalAccountEventType.REVERSAL:
                raise ExternalAccountConflictError("a correction cannot target a REVERSAL event")
            if event.available_to_system_at < target.available_to_system_at:
                raise ExternalAccountConflictError(
                    "correction cannot become available before its target"
                )
            if target_id in corrected_targets or target_id in pending_corrections:
                raise ExternalAccountConflictError("event already has a correction")
            pending_corrections.add(target_id)
        _reject_correction_cycles(new_events, combined_by_id)

        by_account: dict[str, list[ExternalAccountEvent]] = {}
        for event in [*existing_events, *new_events]:
            by_account.setdefault(event.account_id, []).append(event)
        for account_id in account_ids:
            replay_external_account_events(account_id, by_account.get(account_id, []))

        for event in _correction_safe_insert_order(new_events):
            payload_json = event.model_dump_json()
            connection.execute(
                """
                INSERT INTO external_account_event(
                    event_id, account_id, event_type, occurred_at, sequence_no,
                    available_to_system_at, market, symbol, side, quantity,
                    price_cny, amount_cny, currency, reverses_event_id,
                    replaces_event_id, source_artifact_hash, idempotency_key,
                    payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.event_id,
                    event.account_id,
                    event.event_type.value,
                    event.occurred_at.isoformat(),
                    event.sequence_no,
                    event.available_to_system_at.isoformat(),
                    event.market.value if event.market is not None else None,
                    event.symbol,
                    event.side,
                    event.quantity,
                    str(event.price_cny) if event.price_cny is not None else None,
                    str(event.amount_cny) if event.amount_cny is not None else None,
                    event.currency,
                    event.reverses_event_id,
                    event.replaces_event_id,
                    event.source_artifact_hash,
                    event.idempotency_key,
                    payload_json,
                    event.created_at.isoformat(),
                ),
            )
            inserted.append(event.event_id)
        return inserted, duplicates

    @staticmethod
    def _reject_paper_fill_conflict(
        connection: sqlite3.Connection,
        event: ExternalAccountEvent,
    ) -> None:
        """Fail closed when an external TRADE duplicates an existing paper fill."""

        if event.event_type is not ExternalAccountEventType.TRADE:
            return
        assert event.market is not None
        assert event.symbol is not None
        assert event.side is not None
        assert event.quantity is not None
        assert event.price_cny is not None
        price_fen = event.price_cny * Decimal("100")
        if price_fen != price_fen.to_integral_value():
            return
        rows = connection.execute(
            """
            SELECT f.qty, f.price_fen, f.occurred_at, o.side, o.symbol, b.market
            FROM fill f
            JOIN order_record o ON o.order_id=f.order_id
            LEFT JOIN paper_order_rule_binding b ON b.order_id=o.order_id
            WHERE o.symbol=? AND o.side=? AND f.qty=? AND f.price_fen=?
            """,
            (
                event.symbol,
                event.side,
                event.quantity,
                int(price_fen),
            ),
        ).fetchall()
        event_time = event.occurred_at.astimezone(UTC)
        for row in rows:
            bound_market = row["market"]
            if bound_market is not None and str(bound_market) != event.market.value:
                continue
            fill_time = datetime.fromisoformat(str(row["occurred_at"]))
            if fill_time.tzinfo is None or fill_time.utcoffset() is None:
                continue
            if fill_time.astimezone(UTC) == event_time:
                raise ExternalAccountConflictError(
                    "external trade conflicts with an existing paper fill"
                )

    @staticmethod
    def _all_events(connection: sqlite3.Connection) -> list[ExternalAccountEvent]:
        rows = connection.execute(
            "SELECT payload_json FROM external_account_event "
            "ORDER BY occurred_at, sequence_no, event_id"
        ).fetchall()
        return [ExternalAccountEvent.model_validate_json(str(row["payload_json"])) for row in rows]

    @staticmethod
    def _account_from_row(row: sqlite3.Row) -> ExternalAccountIdentity:
        base_currency = str(row["base_currency"])
        if base_currency != "CNY":
            raise ValueError("external account base currency is not supported")
        return ExternalAccountIdentity(
            account_id=str(row["account_id"]),
            display_name=str(row["display_name"]),
            account_kind=ExternalAccountKind(str(row["account_kind"])),
            base_currency="CNY",
            status=ExternalAccountStatus(str(row["status"])),
            created_at=datetime.fromisoformat(str(row["created_at"])),
            updated_at=datetime.fromisoformat(str(row["updated_at"])),
        )


def _correction_safe_insert_order(
    events: Sequence[ExternalAccountEvent],
) -> list[ExternalAccountEvent]:
    """Insert correction targets before referencing rows without changing replay order."""

    by_id = {item.event_id: item for item in events}
    depth_cache: dict[str, int] = {}
    visiting: set[str] = set()

    def depth(event: ExternalAccountEvent) -> int:
        cached = depth_cache.get(event.event_id)
        if cached is not None:
            return cached
        if event.event_id in visiting:
            raise ExternalAccountConflictError("external account correction cycle detected")
        visiting.add(event.event_id)
        target_id = event.reverses_event_id or event.replaces_event_id
        target = by_id.get(target_id) if target_id is not None else None
        value = 0 if target is None else depth(target) + 1
        visiting.remove(event.event_id)
        depth_cache[event.event_id] = value
        return value

    indexed = list(enumerate(events))
    indexed.sort(key=lambda pair: (depth(pair[1]), pair[0]))
    return [event for _, event in indexed]


def _reject_correction_cycles(
    new_events: Sequence[ExternalAccountEvent],
    all_events: dict[str, ExternalAccountEvent],
) -> None:
    target_by_event = {
        item.event_id: target_id
        for item in all_events.values()
        for target_id in (item.reverses_event_id or item.replaces_event_id,)
        if target_id is not None
    }
    for event in new_events:
        seen: set[str] = set()
        current = event.event_id
        while current in target_by_event:
            if current in seen:
                raise ExternalAccountConflictError("external account correction cycle detected")
            seen.add(current)
            current = target_by_event[current]


def replay_external_account_events(
    account_id: str,
    events: Sequence[ExternalAccountEvent],
    *,
    as_of: datetime | None = None,
) -> ExternalAccountProjection:
    cutoff = as_of or datetime.now(UTC)
    relevant = sorted(
        (
            item
            for item in events
            if item.account_id == account_id and item.available_to_system_at <= cutoff
        ),
        key=lambda item: (
            item.occurred_at,
            item.sequence_no,
            item.available_to_system_at,
            item.event_id,
        ),
    )
    correction_for_target = {
        target: item.event_id
        for item in relevant
        for target in (item.reverses_event_id, item.replaces_event_id)
        if target is not None
    }
    effectiveness: dict[str, bool] = {}
    visiting: set[str] = set()

    def is_effective(event_id: str) -> bool:
        cached = effectiveness.get(event_id)
        if cached is not None:
            return cached
        if event_id in visiting:
            raise ValueError("external account correction cycle detected during replay")
        visiting.add(event_id)
        correction_id = correction_for_target.get(event_id)
        result = True if correction_id is None else not is_effective(correction_id)
        visiting.remove(event_id)
        effectiveness[event_id] = result
        return result

    active = [
        item
        for item in relevant
        if item.event_type is not ExternalAccountEventType.REVERSAL and is_effective(item.event_id)
    ]
    cash_known = any(
        item.event_type
        in {
            ExternalAccountEventType.CASH_DEPOSIT,
            ExternalAccountEventType.CASH_WITHDRAWAL,
            ExternalAccountEventType.CASH_ADJUSTMENT,
        }
        for item in active
    )
    cash = Decimal("0") if cash_known else None
    positions: dict[tuple[Market, str], dict[str, Any]] = {}

    for event in active:
        if event.event_type is ExternalAccountEventType.TRADE:
            assert event.market is not None
            assert event.symbol is not None
            assert event.side is not None
            assert event.quantity is not None
            assert event.price_cny is not None
            key = (event.market, event.symbol)
            if event.side == "BUY":
                _increase_position(positions, key, event.quantity, event.price_cny, event)
                if cash is not None:
                    cash -= event.price_cny * event.quantity
            else:
                _decrease_position(positions, key, event.quantity, event)
                if cash is not None:
                    cash += event.price_cny * event.quantity
            continue
        if event.event_type is ExternalAccountEventType.SECURITY_TRANSFER_IN:
            assert event.market is not None
            assert event.symbol is not None
            assert event.quantity is not None
            assert event.price_cny is not None
            _increase_position(
                positions,
                (event.market, event.symbol),
                event.quantity,
                event.price_cny,
                event,
            )
            continue
        if event.event_type is ExternalAccountEventType.SECURITY_TRANSFER_OUT:
            assert event.market is not None
            assert event.symbol is not None
            assert event.quantity is not None
            _decrease_position(
                positions,
                (event.market, event.symbol),
                event.quantity,
                event,
            )
            continue
        assert event.amount_cny is not None
        if event.event_type is ExternalAccountEventType.CASH_DEPOSIT:
            assert cash is not None
            cash += event.amount_cny
        elif event.event_type is ExternalAccountEventType.CASH_WITHDRAWAL:
            assert cash is not None
            cash -= event.amount_cny
        elif event.event_type is ExternalAccountEventType.CASH_ADJUSTMENT:
            assert cash is not None
            cash += event.amount_cny

    projected_positions = [
        ExternalAccountPosition(
            account_id=account_id,
            market=market,
            symbol=symbol,
            quantity=int(value["quantity"]),
            average_cost_cny=Decimal(str(value["average_cost_cny"])),
            opened_at=value["opened_at"],
            last_event_at=value["last_event_at"],
            created_at=cutoff,
        )
        for (market, symbol), value in sorted(
            positions.items(), key=lambda item: (item[0][0].value, item[0][1])
        )
        if int(value["quantity"]) > 0
    ]
    return ExternalAccountProjection(
        account_id=account_id,
        as_of=cutoff,
        positions=projected_positions,
        cash_cny=cash,
        cash_known=cash_known,
        active_event_count=len(active),
        total_event_count=len(relevant),
        created_at=cutoff,
    )


def _increase_position(
    positions: dict[tuple[Market, str], dict[str, Any]],
    key: tuple[Market, str],
    quantity: int,
    price: Decimal,
    event: ExternalAccountEvent,
) -> None:
    current = positions.get(key)
    if current is None:
        positions[key] = {
            "quantity": quantity,
            "average_cost_cny": price,
            "opened_at": event.occurred_at,
            "last_event_at": event.occurred_at,
        }
        return
    old_quantity = int(current["quantity"])
    old_average = Decimal(str(current["average_cost_cny"]))
    new_quantity = old_quantity + quantity
    current["quantity"] = new_quantity
    current["average_cost_cny"] = ((old_average * old_quantity) + (price * quantity)) / new_quantity
    current["last_event_at"] = event.occurred_at


def _decrease_position(
    positions: dict[tuple[Market, str], dict[str, Any]],
    key: tuple[Market, str],
    quantity: int,
    event: ExternalAccountEvent,
) -> None:
    current = positions.get(key)
    if current is None or int(current["quantity"]) < quantity:
        raise ValueError(
            f"external account sell/transfer exceeds current holding: {key[0].value}:{key[1]}"
        )
    current["quantity"] = int(current["quantity"]) - quantity
    current["last_event_at"] = event.occurred_at
    if current["quantity"] == 0:
        del positions[key]


def _infer_import_format(path: Path) -> ExternalAccountImportFormat:
    suffix = path.suffix.lower()
    if suffix == ".csv":
        return ExternalAccountImportFormat.CSV
    if suffix == ".json":
        return ExternalAccountImportFormat.JSON
    raise ValueError("external import format must be CSV or JSON")


def _parse_import_rows(
    raw: bytes,
    source_format: ExternalAccountImportFormat,
) -> list[dict[str, object]]:
    text = raw.decode("utf-8-sig")
    if source_format is ExternalAccountImportFormat.CSV:
        rows: object = list(csv.DictReader(io.StringIO(text)))
    else:
        rows = json.loads(text)
        if isinstance(rows, dict):
            rows = rows.get("events")
    if not isinstance(rows, list):
        raise ValueError("external import must contain a list of event rows")
    result: list[dict[str, object]] = []
    for index, row in enumerate(rows, start=1):
        if not isinstance(row, dict):
            raise ValueError(f"external import row {index} is not an object")
        lowered = {str(key).strip().lower() for key in row}
        forbidden = sorted(lowered & _SECRET_FIELD_NAMES)
        if forbidden:
            names = ", ".join(forbidden)
            raise ValueError(
                f"external import row {index} contains forbidden broker secret fields: {names}"
            )
        result.append({str(key).strip(): value for key, value in row.items()})
    return result


def _draft_from_import_row(
    row: dict[str, object],
    *,
    row_number: int,
    source_artifact_hash: str,
    available_at: datetime,
) -> ExternalAccountEventDraft:
    normalized = {
        str(key).strip().lower(): value for key, value in row.items() if value not in (None, "")
    }
    allowed = {
        "account_id",
        "event_type",
        "occurred_at",
        "sequence_no",
        "available_to_system_at",
        "market",
        "symbol",
        "side",
        "quantity",
        "price_cny",
        "amount_cny",
        "currency",
        "reverses_event_id",
        "replaces_event_id",
        "idempotency_key",
        "note",
    }
    unknown = sorted(set(normalized) - allowed)
    if unknown:
        raise ValueError(
            f"external import row {row_number} has unknown fields: {', '.join(unknown)}"
        )
    if "account_id" not in normalized or "event_type" not in normalized:
        raise ValueError(f"external import row {row_number} requires account_id and event_type")
    occurred_raw = normalized.get("occurred_at")
    if occurred_raw is None:
        raise ValueError(f"external import row {row_number} requires occurred_at")
    occurred_at = _parse_aware_datetime(occurred_raw, row_number=row_number, field="occurred_at")
    availability_raw = normalized.get("available_to_system_at")
    availability = (
        _parse_aware_datetime(
            availability_raw,
            row_number=row_number,
            field="available_to_system_at",
        )
        if availability_raw is not None
        else available_at
    )
    idempotency_key = str(
        normalized.get("idempotency_key") or f"file:{source_artifact_hash}:row:{row_number}"
    )
    payload: dict[str, object] = {
        "account_id": str(normalized["account_id"]),
        "event_type": str(normalized["event_type"]).strip().upper(),
        "occurred_at": occurred_at,
        "sequence_no": normalized.get("sequence_no", row_number - 1),
        "available_to_system_at": availability,
        "source_artifact_hash": source_artifact_hash,
        "idempotency_key": idempotency_key,
        "created_at": availability,
    }
    for key in (
        "market",
        "symbol",
        "side",
        "quantity",
        "price_cny",
        "amount_cny",
        "currency",
        "reverses_event_id",
        "replaces_event_id",
        "note",
    ):
        if key not in normalized:
            continue
        value = normalized[key]
        if key in {"market", "side", "currency"}:
            value = str(value).strip().upper()
        payload[key] = value
    try:
        return ExternalAccountEventDraft.model_validate(payload)
    except Exception as exc:
        raise ValueError(f"external import row {row_number} is invalid: {exc}") from exc


def _parse_aware_datetime(value: object, *, row_number: int, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"external import row {row_number} has invalid {field}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"external import row {row_number} {field} must include timezone")
    return parsed


def _ensure_unique_batch_events(events: Iterable[ExternalAccountEvent]) -> None:
    event_ids: set[str] = set()
    idempotency_keys: set[tuple[str, str]] = set()
    for event in events:
        if event.event_id in event_ids:
            raise ExternalAccountConflictError(
                f"external import contains duplicate event id: {event.event_id}"
            )
        event_ids.add(event.event_id)
        key = (event.account_id, event.idempotency_key)
        if key in idempotency_keys:
            identity = f"{event.account_id}/{event.idempotency_key}"
            raise ExternalAccountConflictError(
                f"external import contains duplicate idempotency key: {identity}"
            )
        idempotency_keys.add(key)


__all__ = [
    "ExternalAccountConflictError",
    "ExternalAccountRepository",
    "replay_external_account_events",
]
