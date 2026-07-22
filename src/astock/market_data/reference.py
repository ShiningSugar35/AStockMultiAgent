"""Recorded-by-default market-reference synchronization and release service."""

from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from decimal import Decimal
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import ValidationError

from astock.core.errors import AStockError, StorageError
from astock.core.hashing import canonical_json_bytes, content_hash
from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.documents import CninfoDisclosureProvider
from astock.market_data.reference_storage import ReferenceParquetStore
from astock.providers.baostock import BaoStockCaptureError, BaoStockReferenceProvider
from astock.providers.eastmoney_reference import EastMoneyReferenceProvider
from astock.providers.symbols import market_from_baostock_code
from astock.schemas import (
    AdjustmentMode,
    AmountUnit,
    CorporateActionObservation,
    CorporateActionStatus,
    DailyBarObservation,
    DatasetReleaseManifest,
    DisclosureCategory,
    DisclosureExchange,
    DisclosureSearchRequest,
    FetchStatus,
    InstrumentRecord,
    InstrumentType,
    Market,
    ReferenceBatch,
    ReferenceCoverage,
    ReferenceCoverageStatus,
    ReferenceDatasetKind,
    ReferencePitStatus,
    ReferenceSyncReport,
    SourceSnapshot,
    TradingSession,
    VolumeUnit,
)

_SHANGHAI = ZoneInfo("Asia/Shanghai")


@dataclass(frozen=True)
class _OfficialActionCandidate:
    announcement_id: str
    published_date: date
    report_period: str
    action_type: str
    document_snapshot_id: str
    source_url: str
    available_to_system_at: datetime


class MarketReferenceService:
    def __init__(
        self,
        state: StateStore,
        objects: ObjectStore,
        parquet: ReferenceParquetStore,
        fixture_root: Path,
    ) -> None:
        self.state = state
        self.objects = objects
        self.parquet = parquet
        self.baostock = BaoStockReferenceProvider(
            objects, state, fixture_root / "baostock"
        )
        self.eastmoney = EastMoneyReferenceProvider(
            objects, state, fixture_root / "eastmoney"
        )
        self.fixture_root = fixture_root.resolve()

    def sync_instruments(
        self, market: Market | None = None, *, live: bool = False
    ) -> ReferenceSyncReport:
        request = {"market": market.value} if market else {}
        envelope = None
        snapshot = None
        records: list[InstrumentRecord] = []
        reasons: list[str] = []
        bao_failed = False
        try:
            envelope, snapshot = self.baostock.fetch("instrument.master", request, live=live)
        except BaoStockCaptureError as exc:
            snapshot = exc.snapshot
            bao_failed = True
            reasons.append(exc.failure_code)
        if envelope is not None and envelope.complete:
            try:
                records = _parse_baostock_instruments(envelope, snapshot.snapshot_id, market)
            except (KeyError, ValueError, ValidationError):
                bao_failed = True
                reasons.append("BAOSTOCK_MALFORMED_INSTRUMENT_MASTER")
        else:
            bao_failed = True
            if "BAOSTOCK_RAW_ENVELOPE_INVALID" not in reasons:
                reasons.append("BAOSTOCK_INCOMPLETE")

        provider_id = self.baostock.provider_id
        snapshot_ids = [snapshot.snapshot_id] if snapshot is not None else []
        available_at = (
            snapshot.available_to_system_at if snapshot is not None else datetime.now(UTC)
        )
        east_failed = False
        if not records:
            try:
                payload, backup_snapshot = self.eastmoney.fetch_master(market, live=live)
                records = _parse_eastmoney_instruments(
                    payload,
                    backup_snapshot.snapshot_id,
                    backup_snapshot.available_to_system_at,
                    market,
                )
                snapshot_ids.append(backup_snapshot.snapshot_id)
                available_at = backup_snapshot.available_to_system_at
                provider_id = self.eastmoney.provider_id
                reasons.append("EASTMONEY_FALLBACK_USED")
            except (AStockError, KeyError, OSError, ValueError, ValidationError):
                east_failed = True
                reasons.append("EASTMONEY_FALLBACK_FAILED")

        scope = market.value if market else "ALL"
        return self._release(
            command="sync-instruments",
            dataset_kind=ReferenceDatasetKind.INSTRUMENT_MASTER,
            scope_key=scope,
            provider_id=provider_id,
            raw_snapshot_ids=snapshot_ids,
            records=records,
            requested_start=None,
            requested_end=None,
            available_at=available_at,
            complete=(
                envelope is not None
                and envelope.complete
                and provider_id == self.baostock.provider_id
            ),
            reasons=reasons,
            failed=not records and bao_failed and east_failed,
        )

    def sync_calendar(
        self,
        exchange: Market,
        start: date,
        end: date,
        *,
        live: bool = False,
    ) -> ReferenceSyncReport:
        if exchange is Market.INDEX:
            raise ValueError("INDEX is not an exchange calendar")
        request = {
            "exchange": exchange.value,
            "start": start.isoformat(),
            "end": end.isoformat(),
        }
        try:
            envelope, snapshot = self.baostock.fetch("market.calendar", request, live=live)
        except BaoStockCaptureError as exc:
            return self._release(
                command="sync-calendar",
                dataset_kind=ReferenceDatasetKind.TRADING_CALENDAR,
                scope_key=exchange.value,
                provider_id=self.baostock.provider_id,
                raw_snapshot_ids=[exc.snapshot.snapshot_id],
                records=[],
                requested_start=start,
                requested_end=end,
                available_at=exc.snapshot.available_to_system_at,
                complete=False,
                reasons=[exc.failure_code],
                failed=True,
            )
        reasons: list[str] = []
        try:
            records = _parse_baostock_calendar(
                envelope,
                snapshot.snapshot_id,
                exchange,
                snapshot.available_to_system_at,
                start,
                end,
            )
        except (KeyError, ValueError, ValidationError):
            records = []
            reasons.append("BAOSTOCK_MALFORMED_CALENDAR")
        expected_dates = (end - start).days + 1
        complete = envelope.complete and len(records) == expected_dates
        if not complete:
            reasons.append("CALENDAR_RANGE_INCOMPLETE")
        return self._release(
            command="sync-calendar",
            dataset_kind=ReferenceDatasetKind.TRADING_CALENDAR,
            scope_key=exchange.value,
            provider_id=self.baostock.provider_id,
            raw_snapshot_ids=[snapshot.snapshot_id],
            records=records,
            requested_start=start,
            requested_end=end,
            available_at=snapshot.available_to_system_at,
            complete=complete,
            reasons=reasons,
            failed=not records and not envelope.complete,
        )

    def sync_daily(
        self,
        symbol: str,
        market: Market,
        start: date,
        end: date,
        *,
        live: bool = False,
    ) -> ReferenceSyncReport:
        request = {
            "symbol": symbol,
            "market": market.value,
            "start": start.isoformat(),
            "end": end.isoformat(),
            "adjustflag": "3",
        }
        envelope = None
        snapshot = None
        records: list[DailyBarObservation] = []
        reasons: list[str] = []
        bao_failed = False
        try:
            envelope, snapshot = self.baostock.fetch(
                "market.daily_unadjusted", request, live=live
            )
        except BaoStockCaptureError as exc:
            snapshot = exc.snapshot
            bao_failed = True
            reasons.append(exc.failure_code)
        if envelope is not None and envelope.complete:
            try:
                records = _parse_baostock_daily(
                    envelope, snapshot.snapshot_id, symbol, market, start, end
                )
            except (KeyError, ValueError, ValidationError):
                bao_failed = True
                reasons.append("BAOSTOCK_MALFORMED_DAILY")
        else:
            bao_failed = True
            if "BAOSTOCK_RAW_ENVELOPE_INVALID" not in reasons:
                reasons.append("BAOSTOCK_INCOMPLETE")
        provider_id = self.baostock.provider_id
        snapshot_ids = [snapshot.snapshot_id] if snapshot is not None else []
        available_at = (
            snapshot.available_to_system_at if snapshot is not None else datetime.now(UTC)
        )
        east_failed = False
        if not records:
            try:
                payload, backup_snapshot = self.eastmoney.fetch_daily(
                    symbol, market, start.isoformat(), end.isoformat(), live=live
                )
                records = _parse_eastmoney_daily(
                    payload,
                    backup_snapshot.snapshot_id,
                    backup_snapshot.available_to_system_at,
                    symbol,
                    market,
                    start,
                    end,
                )
                snapshot_ids.append(backup_snapshot.snapshot_id)
                available_at = backup_snapshot.available_to_system_at
                provider_id = self.eastmoney.provider_id
                reasons.append("EASTMONEY_FALLBACK_USED")
            except (AStockError, KeyError, OSError, ValueError, ValidationError):
                east_failed = True
                reasons.append("EASTMONEY_FALLBACK_FAILED")
        return self._release(
            command="sync-daily",
            dataset_kind=ReferenceDatasetKind.DAILY_UNADJUSTED,
            scope_key=f"{market.value}:{symbol}",
            provider_id=provider_id,
            raw_snapshot_ids=snapshot_ids,
            records=records,
            requested_start=start,
            requested_end=end,
            available_at=available_at,
            complete=(
                envelope is not None
                and envelope.complete
                and provider_id == self.baostock.provider_id
            ),
            reasons=reasons,
            failed=not records and bao_failed and east_failed,
        )

    def sync_corporate_actions(
        self,
        symbol: str,
        market: Market,
        start: date,
        end: date,
        *,
        live: bool = False,
    ) -> ReferenceSyncReport:
        request = {
            "symbol": symbol,
            "market": market.value,
            "start": start.isoformat(),
            "end": end.isoformat(),
        }
        try:
            envelope, hint_snapshot = self.baostock.fetch(
                "corporate_actions.structured_hint", request, live=live
            )
        except BaoStockCaptureError as exc:
            return self._release(
                command="sync-corporate-actions",
                dataset_kind=ReferenceDatasetKind.CORPORATE_ACTION,
                scope_key=f"{market.value}:{symbol}",
                provider_id=self.baostock.provider_id,
                raw_snapshot_ids=[exc.snapshot.snapshot_id],
                records=[],
                requested_start=start,
                requested_end=end,
                available_at=exc.snapshot.available_to_system_at,
                complete=False,
                reasons=[exc.failure_code],
                failed=True,
            )
        reasons: list[str] = []
        try:
            records = _parse_baostock_actions(
                envelope, hint_snapshot.snapshot_id, symbol, market
            )
            records = [
                item
                for item in records
                if (item.announcement_date is None or start <= item.announcement_date <= end)
                and (item.ex_date is None or start <= item.ex_date <= end)
            ]
        except (KeyError, ValueError, ValidationError):
            records = []
            reasons.append("BAOSTOCK_MALFORMED_CORPORATE_ACTION_HINT")
        snapshot_ids = [hint_snapshot.snapshot_id]
        available_at = hint_snapshot.available_to_system_at

        if market is Market.BJSE:
            reasons.append("OFFICIAL_EVIDENCE_UNAVAILABLE")
        elif market in {Market.XSHG, Market.XSHE} and records:
            try:
                official_candidates, official_snapshot_ids, official_available = (
                    self._official_actions_live(symbol, market, start, end)
                    if live
                    else self._official_actions_recorded(symbol, market)
                )
                snapshot_ids.extend(official_snapshot_ids)
                available_at = max(available_at, official_available)
                records, linked_count, match_reasons = _link_official_actions(
                    records, official_candidates
                )
                reasons.extend(match_reasons)
                if linked_count:
                    reasons.append("TERMS_NOT_VERIFIED")
                if linked_count == 0 and not official_candidates:
                    reasons.append("OFFICIAL_DOCUMENT_NOT_FOUND")
            except (AStockError, OSError, ValueError):
                reasons.append("OFFICIAL_EVIDENCE_LOOKUP_FAILED")
        elif market is Market.INDEX:
            reasons.append("CORPORATE_ACTION_NOT_APPLICABLE_TO_INDEX")

        # Hints and linked PDFs stay non-ledger-ready until terms are parsed and verified.
        return self._release(
            command="sync-corporate-actions",
            dataset_kind=ReferenceDatasetKind.CORPORATE_ACTION,
            scope_key=f"{market.value}:{symbol}",
            provider_id=self.baostock.provider_id,
            raw_snapshot_ids=list(dict.fromkeys(snapshot_ids)),
            records=records,
            requested_start=start,
            requested_end=end,
            available_at=available_at,
            complete=False,
            reasons=reasons,
            failed=not records and not envelope.complete,
        )

    def _official_actions_recorded(
        self, symbol: str, market: Market
    ) -> tuple[list[_OfficialActionCandidate], list[str], datetime]:
        fixture = self.fixture_root / "cninfo" / "corporate_action_official.json"
        raw = fixture.read_bytes()
        payload = json.loads(raw)
        available_text = str(payload["available_to_system_at"]).replace("Z", "+00:00")
        available = datetime.fromisoformat(available_text)
        index_ref = self.objects.put_bytes(raw)
        index_snapshot = SourceSnapshot(
            created_at=available,
            snapshot_id=f"cninfo-disclosures:index:{index_ref.sha256}",
            source_id="cninfo-disclosures:index",
            object_sha256=index_ref.sha256,
            fetched_at=available,
            available_to_system_at=available,
            source_url="https://www.cninfo.com.cn/new/hisAnnouncement/query",
            mime="application/json",
            byte_size=index_ref.byte_size,
            fetch_status=FetchStatus.SUCCEEDED,
            rights_status="PUBLIC_DISCLOSURE",
        )
        self.state.register_snapshot(index_snapshot)
        matches = [
            item
            for item in payload.get("announcements", [])
            if item.get("symbol") == symbol and item.get("market") == market.value
        ]
        candidates: list[_OfficialActionCandidate] = []
        snapshot_ids = [index_snapshot.snapshot_id]
        for selected in matches:
            pdf = base64.b64decode(str(selected["pdf_base64"]), validate=True)
            if not pdf.startswith(b"%PDF-"):
                raise ValueError("recorded official corporate-action document is not a PDF")
            pdf_ref = self.objects.put_bytes(pdf)
            document_snapshot = SourceSnapshot(
                created_at=available,
                snapshot_id=f"cninfo-disclosures:document:{pdf_ref.sha256}",
                source_id="cninfo-disclosures:document",
                object_sha256=pdf_ref.sha256,
                fetched_at=available,
                available_to_system_at=available,
                source_url=str(selected["source_url"]),
                mime="application/pdf",
                byte_size=pdf_ref.byte_size,
                fetch_status=FetchStatus.SUCCEEDED,
                rights_status="PUBLIC_DISCLOSURE",
            )
            self.state.register_snapshot(document_snapshot)
            snapshot_ids.append(document_snapshot.snapshot_id)
            candidates.append(
                _OfficialActionCandidate(
                    announcement_id=str(selected["announcement_id"]),
                    published_date=date.fromisoformat(str(selected["published_date"])),
                    report_period=str(selected["report_period"]),
                    action_type=str(selected["action_type"]),
                    document_snapshot_id=document_snapshot.snapshot_id,
                    source_url=str(selected["source_url"]),
                    available_to_system_at=available,
                )
            )
        return candidates, list(dict.fromkeys(snapshot_ids)), available

    def _official_actions_live(
        self, symbol: str, market: Market, start: date, end: date
    ) -> tuple[list[_OfficialActionCandidate], list[str], datetime]:
        exchange = DisclosureExchange.SSE if market is Market.XSHG else DisclosureExchange.SZSE
        provider = CninfoDisclosureProvider(self.objects, self.state)
        batch = provider.search(
            DisclosureSearchRequest(
                symbol=symbol,
                exchange=exchange,
                start_date=start,
                end_date=end,
                category=DisclosureCategory.ALL,
                keyword="利润分配",
                page_size=100,
            )
        )
        snapshot_ids = [batch.raw_snapshot_id]
        candidates = [
            item
            for item in batch.announcements
            if any(key in item.title for key in ("分红", "权益分派", "利润分配"))
        ]
        if not candidates:
            index = self.state.get_snapshot(batch.raw_snapshot_id)
            assert index is not None
            return [], snapshot_ids, index.available_to_system_at
        official: list[_OfficialActionCandidate] = []
        available = datetime.min.replace(tzinfo=UTC)
        for announcement in candidates:
            report_period = _extract_report_period(announcement.title)
            action_type = _official_action_type(announcement.title)
            if report_period is None or action_type is None:
                continue
            downloaded = provider.download(announcement)
            snapshot_ids.append(downloaded.snapshot.snapshot_id)
            available = max(available, downloaded.snapshot.available_to_system_at)
            official.append(
                _OfficialActionCandidate(
                    announcement_id=announcement.announcement_id,
                    published_date=announcement.published_at.astimezone(_SHANGHAI).date(),
                    report_period=report_period,
                    action_type=action_type,
                    document_snapshot_id=downloaded.snapshot.snapshot_id,
                    source_url=announcement.source_url,
                    available_to_system_at=downloaded.snapshot.available_to_system_at,
                )
            )
        if available == datetime.min.replace(tzinfo=UTC):
            index = self.state.get_snapshot(batch.raw_snapshot_id)
            assert index is not None
            available = index.available_to_system_at
        return official, list(dict.fromkeys(snapshot_ids)), available

    def status(
        self,
        dataset_kind: ReferenceDatasetKind,
        scope_key: str,
        *,
        as_of: datetime | None = None,
    ) -> dict[str, Any]:
        row = self.state.get_market_reference_release(
            dataset_kind.value, scope_key, as_of=as_of
        )
        if row is None:
            return {
                "schema_version": "reference-status-v1",
                "dataset_kind": dataset_kind.value,
                "scope_key": scope_key,
                "status": "NOT_AVAILABLE",
            }
        try:
            if _is_legacy_release_row(row):
                self._verify_legacy_release(row)
                return {
                    "schema_version": "reference-status-v1",
                    "dataset_kind": dataset_kind.value,
                    "scope_key": scope_key,
                    "status": "UNVERIFIED_LEGACY",
                    "release_id": row["release_id"],
                    "pit_status": ReferencePitStatus.UNVERIFIED.value,
                }
            manifest = self._verified_manifest(row)
        except (OSError, StorageError, ValueError, ValidationError):
            return {
                "schema_version": "reference-status-v1",
                "dataset_kind": dataset_kind.value,
                "scope_key": scope_key,
                "status": "CORRUPT",
                "release_id": row["release_id"],
            }
        return {
            "schema_version": "reference-status-v1",
            "dataset_kind": dataset_kind.value,
            "scope_key": scope_key,
            "status": "AVAILABLE",
            "release": manifest.model_dump(mode="json"),
        }

    def audit(self) -> dict[str, Any]:
        rows = self.state.list_market_reference_releases()
        corrupt: list[str] = []
        manifests: dict[str, DatasetReleaseManifest] = {}
        legacy_release_ids: set[str] = set()
        for row in rows:
            try:
                if _is_legacy_release_row(row):
                    self._verify_legacy_release(row)
                    legacy_release_ids.add(str(row["release_id"]))
                else:
                    manifest = self._verified_manifest(row)
                    manifests[manifest.release_id] = manifest
            except (OSError, StorageError, ValueError, ValidationError):
                corrupt.append(str(row["release_id"]))
        graph_corrupt, reason_codes = self._audit_release_graph(
            rows, manifests, legacy_release_ids
        )
        corrupt.extend(graph_corrupt)
        corrupt = list(dict.fromkeys(corrupt))
        return {
            "schema_version": "reference-audit-v1",
            "release_count": len(rows),
            "corrupt_release_ids": corrupt,
            "reason_codes": reason_codes,
            "status": "PASS" if not corrupt else "FAIL",
            "ledger_writes": 0,
        }

    def _audit_release_graph(
        self,
        rows: list[dict[str, Any]],
        manifests: dict[str, DatasetReleaseManifest],
        legacy_release_ids: set[str],
    ) -> tuple[list[str], list[str]]:
        corrupt: list[str] = []
        reasons: list[str] = []
        by_scope: dict[tuple[str, str], set[str]] = {}
        rows_by_id = {str(row["release_id"]): row for row in rows}
        for row in rows:
            key = (str(row["dataset_kind"]), str(row["scope_key"]))
            by_scope.setdefault(key, set()).add(str(row["release_id"]))
        with self.state.connect() as connection:
            integrity = connection.execute("PRAGMA integrity_check").fetchone()
            if integrity is None or str(integrity[0]) != "ok":
                reasons.append("SQLITE_INTEGRITY_FAILED")
                corrupt.extend(str(row["release_id"]) for row in rows)
            if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
                reasons.append("SQLITE_FOREIGN_KEY_FAILED")
                corrupt.extend(str(row["release_id"]) for row in rows)
            heads = connection.execute(
                "SELECT dataset_kind,scope_key,release_id FROM market_reference_head"
            ).fetchall()
            checkpoints = {
                str(row["scope_key"]): row
                for row in connection.execute(
                    "SELECT scope_key,cursor_json,object_hash,status FROM checkpoint "
                    "WHERE scope_type='market-reference'"
                ).fetchall()
            }
        head_map = {
            (str(row["dataset_kind"]), str(row["scope_key"])): str(row["release_id"])
            for row in heads
        }
        if set(head_map) != set(by_scope):
            reasons.append("HEAD_SCOPE_MISMATCH")
            corrupt.extend(str(row["release_id"]) for row in rows)
        for scope, release_ids in by_scope.items():
            head_id = head_map.get(scope)
            if head_id not in release_ids:
                reasons.append("HEAD_POINTER_INVALID")
                corrupt.extend(release_ids)
                continue
            visited: list[str] = []
            cursor = head_id
            last_available: datetime | None = None
            while cursor is not None:
                if cursor in visited or cursor not in release_ids:
                    reasons.append("RELEASE_CHAIN_INVALID")
                    corrupt.extend(release_ids)
                    break
                visited.append(cursor)
                manifest = manifests.get(cursor)
                row = rows_by_id[cursor]
                if manifest is not None:
                    available = manifest.available_to_system_at
                    previous = manifest.previous_release_id
                elif cursor in legacy_release_ids:
                    available = datetime.fromisoformat(str(row["available_to_system_at"]))
                    previous = (
                        str(row["previous_release_id"])
                        if row["previous_release_id"] is not None
                        else None
                    )
                    reasons.append("LEGACY_UNVERIFIED_RELEASE")
                else:
                    break
                if (
                    last_available is not None
                    and available > last_available
                ):
                    reasons.append("RELEASE_AVAILABILITY_ORDER_INVALID")
                    corrupt.extend(release_ids)
                    break
                last_available = available
                cursor = previous
            if set(visited) != release_ids:
                reasons.append("RELEASE_CHAIN_INCOMPLETE")
                corrupt.extend(release_ids)
            manifest = manifests.get(head_id)
            checkpoint_key = f"{scope[0]}:{scope[1]}"
            checkpoint = checkpoints.get(checkpoint_key)
            if checkpoint is None:
                reasons.append("CHECKPOINT_MISSING")
                corrupt.extend(release_ids)
                continue
            head_row = rows_by_id[head_id]
            expected_cursor = canonical_json_bytes(
                {
                    "release_id": head_id,
                    "content_hash": str(head_row["content_hash"]),
                }
            ).decode("utf-8")
            expected_object = next(
                str(row["manifest_object_hash"])
                for row in rows
                if str(row["release_id"]) == head_id
            )
            if (
                checkpoint["cursor_json"] != expected_cursor
                or checkpoint["object_hash"] != expected_object
                or checkpoint["status"] != "SUCCEEDED"
            ):
                reasons.append("CHECKPOINT_INVALID")
                corrupt.extend(release_ids)
        return list(dict.fromkeys(corrupt)), list(dict.fromkeys(reasons))

    def _verify_legacy_release(self, row: dict[str, Any]) -> None:
        """Validate a migrated v1 release without making its facts consumable."""

        if not _is_legacy_release_row(row):
            raise ValueError("market-reference release is not a migrated legacy row")
        raw = self.objects.get_bytes(str(row["manifest_object_hash"]))
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError("legacy market-reference manifest root is invalid")
        raw_snapshot_ids = payload.get("raw_snapshot_ids")
        if not isinstance(raw_snapshot_ids, list) or not all(
            isinstance(item, str) and item for item in raw_snapshot_ids
        ):
            raise ValueError("legacy market-reference snapshot chain is invalid")
        expected_raw_json = canonical_json_bytes(raw_snapshot_ids).decode("utf-8")
        try:
            payload_available = datetime.fromisoformat(
                str(payload.get("available_to_system_at")).replace("Z", "+00:00")
            )
            row_available = datetime.fromisoformat(str(row["available_to_system_at"]))
        except ValueError as exc:
            raise ValueError("legacy market-reference availability is invalid") from exc
        release_identity = {
            "dataset_kind": row["dataset_kind"],
            "scope_key": row["scope_key"],
            "provider_id": row["provider_id"],
            "batch_id": row["batch_id"],
            "content_hash": row["content_hash"],
            "previous_release_id": row["previous_release_id"],
            "available_to_system_at": row["available_to_system_at"],
        }
        legacy_marker = json.loads(str(row["coverage_json"]))
        if (
            legacy_marker.get("legacy_0038") is not True
            or row["pit_status"] != ReferencePitStatus.UNVERIFIED.value
            or row["artifact_type"] != "DatasetReleaseManifest"
            or row["manifest_object_hash"] != row["artifact_object_hash"]
            or row["manifest_schema_version"] != row["artifact_schema_version"]
            or payload.get("schema_version") != row["manifest_schema_version"]
            or payload.get("release_id") != row["release_id"]
            or payload.get("content_hash") != row["content_hash"]
            or payload.get("dataset_kind") != row["dataset_kind"]
            or payload.get("scope_key") != row["scope_key"]
            or payload.get("provider_id") != row["provider_id"]
            or payload.get("batch_id") != row["batch_id"]
            or payload.get("previous_release_id") != row["previous_release_id"]
            or payload_available != row_available
            or row["raw_snapshot_ids_json"] != expected_raw_json
            or row["input_hashes_json"]
            != json.dumps(
                [*raw_snapshot_ids, str(row["content_hash"])], separators=(",", ":")
            )
            or str(row["release_id"]) != content_hash(release_identity)
        ):
            raise ValueError("legacy market-reference release chain mismatch")
        release_available = row_available
        for snapshot_id in raw_snapshot_ids:
            snapshot = self.state.get_snapshot(snapshot_id)
            if (
                snapshot is None
                or snapshot.available_to_system_at > release_available
                or not self.objects.verify(snapshot.object_sha256)
            ):
                raise ValueError("legacy market-reference raw snapshot chain is invalid")

    def _verified_manifest(self, row: dict[str, Any]) -> DatasetReleaseManifest:
        raw = self.objects.get_bytes(str(row["manifest_object_hash"]))
        manifest = DatasetReleaseManifest.model_validate_json(raw)
        _verify_release_row(row, manifest)
        expected_inputs = json.dumps(
            [*manifest.raw_snapshot_ids, manifest.content_hash], separators=(",", ":")
        )
        if row["input_hashes_json"] != expected_inputs:
            raise ValueError("market-reference artifact inputs do not match manifest")
        for snapshot_id in manifest.raw_snapshot_ids:
            snapshot = self.state.get_snapshot(snapshot_id)
            if (
                snapshot is None
                or snapshot.available_to_system_at > manifest.available_to_system_at
                or not self.objects.verify(snapshot.object_sha256)
            ):
                raise ValueError("market-reference raw snapshot chain is invalid")
        for descriptor in [*manifest.observation_files, *manifest.canonical_files]:
            if not self.parquet.verify_descriptor(
                descriptor,
                dataset_kind=manifest.dataset_kind.value,
                scope_key=manifest.scope_key,
                provider_id=manifest.provider_id,
                batch_id=manifest.batch_id,
                available_to_system_at=manifest.available_to_system_at,
                expected_row_count=manifest.coverage.record_count,
            ):
                raise ValueError("manifest references invalid Parquet")
        return manifest

    def _release(
        self,
        *,
        command: str,
        dataset_kind: ReferenceDatasetKind,
        scope_key: str,
        provider_id: str,
        raw_snapshot_ids: list[str],
        records: list[Any],
        requested_start: date | None,
        requested_end: date | None,
        available_at: datetime,
        complete: bool,
        reasons: list[str],
        failed: bool = False,
    ) -> ReferenceSyncReport:
        actual_dates = [_record_date(item) for item in records]
        status = (
            ReferenceCoverageStatus.COMPLETE
            if records and complete
            else (
                ReferenceCoverageStatus.PARTIAL
                if records
                else (
                    ReferenceCoverageStatus.FAILED
                    if failed
                    else ReferenceCoverageStatus.EMPTY
                )
            )
        )
        coverage = ReferenceCoverage(
            created_at=available_at,
            requested_start=requested_start,
            requested_end=requested_end,
            actual_start=min(actual_dates) if actual_dates else None,
            actual_end=max(actual_dates) if actual_dates else None,
            record_count=len(records),
            status=status,
            reason_codes=list(dict.fromkeys(reasons)),
        )
        pit = ReferencePitStatus.RECONSTRUCTED
        if not records:
            return ReferenceSyncReport(
                created_at=available_at,
                command=command,
                status=status,
                dataset_kind=dataset_kind,
                scope_key=scope_key,
                provider_id=provider_id,
                raw_snapshot_ids=raw_snapshot_ids,
                coverage=coverage,
                pit_status=ReferencePitStatus.UNVERIFIED,
                reason_codes=coverage.reason_codes,
            )
        record_payloads = [item.model_dump(mode="json", exclude={"created_at"}) for item in records]
        batch_id = content_hash(
            {
                "dataset_kind": dataset_kind.value,
                "scope_key": scope_key,
                "provider_id": provider_id,
                "raw_snapshot_ids": raw_snapshot_ids,
                "records": record_payloads,
            }
        )
        batch = ReferenceBatch(
            created_at=available_at,
            batch_id=batch_id,
            dataset_kind=dataset_kind,
            scope_key=scope_key,
            provider_id=provider_id,
            raw_snapshot_ids=raw_snapshot_ids,
            records=records,
            coverage=coverage,
            pit_status=pit,
            available_to_system_at=available_at,
        )
        observation_path = self.parquet.write_observation(batch)
        canonical_path, records_hash = self.parquet.write_canonical(batch)
        observation_descriptor = self.parquet.describe(
            observation_path,
            logical_content_hash=records_hash,
            created_at=available_at,
        )
        canonical_descriptor = self.parquet.describe(
            canonical_path,
            logical_content_hash=records_hash,
            created_at=available_at,
        )
        for descriptor in (observation_descriptor, canonical_descriptor):
            if not self.parquet.verify_descriptor(
                descriptor,
                dataset_kind=dataset_kind.value,
                scope_key=scope_key,
                provider_id=provider_id,
                batch_id=batch_id,
                available_to_system_at=available_at,
                expected_row_count=len(records),
            ):
                raise ValueError("Reference Parquet failed pre-publish verification")
        current = self.state.get_market_reference_release(dataset_kind.value, scope_key)
        if current is not None:
            if _is_legacy_release_row(current):
                self._verify_legacy_release(current)
            else:
                current_manifest = self._verified_manifest(current)
                if (
                    current_manifest.provider_id == provider_id
                    and current_manifest.batch_id == batch_id
                    and current_manifest.content_hash == records_hash
                    and current_manifest.raw_snapshot_ids == raw_snapshot_ids
                    and current_manifest.available_to_system_at == available_at
                    and current_manifest.coverage == coverage
                    and current_manifest.pit_status is pit
                    and current_manifest.observation_files == [observation_descriptor]
                    and current_manifest.canonical_files == [canonical_descriptor]
                ):
                    return ReferenceSyncReport(
                        created_at=current_manifest.available_to_system_at,
                        command=command,
                        status=current_manifest.coverage.status,
                        dataset_kind=current_manifest.dataset_kind,
                        scope_key=current_manifest.scope_key,
                        provider_id=current_manifest.provider_id,
                        release_id=current_manifest.release_id,
                        manifest_object_hash=str(current["manifest_object_hash"]),
                        raw_snapshot_ids=current_manifest.raw_snapshot_ids,
                        coverage=current_manifest.coverage,
                        pit_status=current_manifest.pit_status,
                        reason_codes=[
                            *current_manifest.coverage.reason_codes,
                            "IDEMPOTENT_EXISTING_RELEASE",
                        ],
                    )
        previous = str(current["release_id"]) if current is not None else None
        identity = {
            "dataset_kind": dataset_kind.value,
            "scope_key": scope_key,
            "provider_id": provider_id,
            "batch_id": batch_id,
            "content_hash": records_hash,
            "previous_release_id": previous,
            "available_to_system_at": available_at.isoformat(),
        }
        release_id = content_hash(identity)
        manifest = DatasetReleaseManifest(
            created_at=available_at,
            release_id=release_id,
            content_hash=records_hash,
            dataset_kind=dataset_kind,
            scope_key=scope_key,
            provider_id=provider_id,
            batch_id=batch_id,
            previous_release_id=previous,
            raw_snapshot_ids=raw_snapshot_ids,
            observation_files=[observation_descriptor],
            canonical_files=[canonical_descriptor],
            coverage=coverage,
            pit_status=pit,
            available_to_system_at=available_at,
        )
        object_ref = self.objects.put_bytes(canonical_json_bytes(manifest))
        if not self.objects.verify(object_ref.sha256):
            raise RuntimeError("market-reference manifest object verification failed")
        self.state.publish_market_reference_release(manifest, object_ref.sha256)
        return ReferenceSyncReport(
            created_at=available_at,
            command=command,
            status=status,
            dataset_kind=dataset_kind,
            scope_key=scope_key,
            provider_id=provider_id,
            release_id=release_id,
            manifest_object_hash=object_ref.sha256,
            raw_snapshot_ids=raw_snapshot_ids,
            coverage=coverage,
            pit_status=pit,
            reason_codes=coverage.reason_codes,
        )


def _parse_baostock_instruments(
    envelope: Any, snapshot_id: str, requested_market: Market | None
) -> list[InstrumentRecord]:
    fields = {name: index for index, name in enumerate(envelope.fields)}
    available = envelope.request_finished_at
    result: list[InstrumentRecord] = []
    for raw in envelope.rows:
        code = raw[fields["code"]]
        kind = raw[fields["type"]]
        market = market_from_baostock_code(code, instrument_type=kind)
        if requested_market is not None and market is not requested_market:
            continue
        symbol = code.split(".", maxsplit=1)[1]
        name = raw[fields["code_name"]]
        instrument_type = InstrumentType.INDEX if kind == "2" else InstrumentType.STOCK
        status = raw[fields["status"]]
        result.append(
            InstrumentRecord(
                created_at=available,
                instrument_id=f"{market.value}:{symbol}",
                market=market,
                symbol=symbol,
                name=name,
                instrument_type=instrument_type,
                tradable=instrument_type is InstrumentType.STOCK and status == "1",
                status_date=available.astimezone(_SHANGHAI).date(),
                is_st=_is_st_name(name),
                listing_date=_optional_date(raw[fields["ipoDate"]]),
                delisting_date=_optional_date(raw[fields["outDate"]]),
                source_snapshot_id=snapshot_id,
                available_to_system_at=available,
            )
        )
    return result


def _parse_eastmoney_instruments(
    payload: dict[str, object],
    snapshot_id: str,
    available: datetime,
    requested_market: Market | None,
) -> list[InstrumentRecord]:
    if payload.get("rc") != 0:
        raise ValueError("EastMoney instrument request failed")
    request = payload.get("_astock_request")
    if not isinstance(request, dict) or request.get("market") != (
        requested_market.value if requested_market else "ALL"
    ):
        raise ValueError("EastMoney instrument request provenance mismatch")
    data = payload["data"]
    if not isinstance(data, dict) or not isinstance(data.get("diff"), list):
        raise ValueError("invalid EastMoney instrument payload")
    result: list[InstrumentRecord] = []
    for item in data["diff"]:
        if not isinstance(item, dict):
            raise ValueError("invalid EastMoney instrument row")
        # The endpoint/query board is the explicit market boundary; never infer BJSE by code.
        market_value = item.get("market") or (
            requested_market.value if requested_market else ""
        )
        market = Market(str(market_value))
        if requested_market is not None and market is not requested_market:
            continue
        symbol = str(item["f12"])
        default_kind = "INDEX" if requested_market is Market.INDEX else "STOCK"
        kind = InstrumentType(str(item.get("instrument_type", default_kind)))
        name = str(item["f14"])
        result.append(
            InstrumentRecord(
                created_at=available,
                instrument_id=f"{market.value}:{symbol}",
                market=market,
                symbol=symbol,
                name=name,
                instrument_type=kind,
                tradable=kind is InstrumentType.STOCK,
                status_date=available.astimezone(_SHANGHAI).date(),
                is_st=_is_st_name(name),
                source_snapshot_id=snapshot_id,
                available_to_system_at=available,
            )
        )
    return result


def _parse_baostock_calendar(
    envelope: Any,
    snapshot_id: str,
    exchange: Market,
    available: datetime,
    start: date,
    end: date,
) -> list[TradingSession]:
    fields = {name: index for index, name in enumerate(envelope.fields)}
    return [
        TradingSession(
            created_at=available,
            exchange=exchange,
            session_date=date.fromisoformat(row[fields["calendar_date"]]),
            is_open=row[fields["is_trading_day"]] == "1",
            source_snapshot_id=snapshot_id,
            available_to_system_at=available,
        )
        for row in envelope.rows
        if start <= date.fromisoformat(row[fields["calendar_date"]]) <= end
    ]


def _parse_baostock_daily(
    envelope: Any,
    snapshot_id: str,
    requested_symbol: str,
    requested_market: Market,
    start: date,
    end: date,
) -> list[DailyBarObservation]:
    fields = {name: index for index, name in enumerate(envelope.fields)}
    available = envelope.request_finished_at
    result: list[DailyBarObservation] = []
    for row in envelope.rows:
        if row[fields["adjustflag"]] != "3":
            raise ValueError("adjusted BaoStock daily row rejected")
        code = row[fields["code"]]
        market = market_from_baostock_code(
            code, instrument_type="2" if requested_market is Market.INDEX else "1"
        )
        symbol = code.split(".", maxsplit=1)[1]
        if market is not requested_market or symbol != requested_symbol:
            raise ValueError("BaoStock daily row crossed the explicit market boundary")
        session = date.fromisoformat(row[fields["date"]])
        if session < start or session > end:
            continue
        payload = {
            "market": market.value,
            "symbol": symbol,
            "date": session.isoformat(),
            "open": row[fields["open"]],
            "high": row[fields["high"]],
            "low": row[fields["low"]],
            "close": row[fields["close"]],
            "volume": row[fields["volume"]],
            "amount": row[fields["amount"]],
        }
        result.append(
            DailyBarObservation(
                created_at=available,
                observation_id=content_hash(payload),
                instrument_id=f"{market.value}:{symbol}",
                market=market,
                symbol=symbol,
                session_date=session,
                session_close_at=datetime.combine(session, time(15, 0), tzinfo=_SHANGHAI),
                open=Decimal(row[fields["open"]]),
                high=Decimal(row[fields["high"]]),
                low=Decimal(row[fields["low"]]),
                close=Decimal(row[fields["close"]]),
                previous_close=_optional_decimal(row[fields["preclose"]]),
                volume=Decimal(row[fields["volume"]]),
                volume_unit=VolumeUnit.SHARE,
                amount=_optional_decimal(row[fields["amount"]]),
                amount_unit=AmountUnit.CNY,
                adjustment_mode=AdjustmentMode.NONE,
                is_st=row[fields["isST"]] == "1",
                source_snapshot_id=snapshot_id,
                available_to_system_at=available,
            )
        )
    return result


def _parse_baostock_actions(
    envelope: Any, snapshot_id: str, requested_symbol: str, requested_market: Market
) -> list[CorporateActionObservation]:
    fields = {name: index for index, name in enumerate(envelope.fields)}
    available = envelope.request_finished_at
    result: list[CorporateActionObservation] = []
    for row_index, row in enumerate(envelope.rows):
        code = row[fields["code"]]
        market = market_from_baostock_code(code)
        symbol = code.split(".", maxsplit=1)[1]
        if market is not requested_market or symbol != requested_symbol:
            continue
        terms = {
            key: row[index]
            for key, index in fields.items()
            if key != "code" and row[index] != ""
        }
        cash = Decimal(terms.get("dividCashPsBeforeTax", "0") or "0")
        stock = Decimal(terms.get("dividStocksPs", "0") or "0")
        reserve = Decimal(terms.get("dividReserveToStockPs", "0") or "0")
        if cash > 0 and stock == 0 and reserve == 0:
            action_type = "CASH_DIVIDEND_HINT"
        elif stock > 0 or reserve > 0:
            action_type = "STOCK_DISTRIBUTION_HINT"
        else:
            action_type = "DISTRIBUTION_HINT"
        announcement = _optional_date(terms.get("dividPlanAnnounceDate", ""))
        ex_date = _optional_date(terms.get("dividOperateDate", ""))
        context = envelope.row_contexts[row_index] if envelope.row_contexts else {}
        report_period = context.get("report_period")
        identity = {
            "instrument_id": f"{market.value}:{symbol}",
            "action_type": action_type,
            "report_period": report_period,
            "announcement_date": announcement,
            "ex_date": ex_date,
            "structured_terms": terms,
        }
        result.append(
            CorporateActionObservation(
                created_at=available,
                observation_id=content_hash(identity),
                instrument_id=f"{market.value}:{symbol}",
                market=market,
                symbol=symbol,
                action_type=action_type,
                report_period=report_period,
                announcement_date=announcement,
                ex_date=ex_date,
                status=CorporateActionStatus.DISCOVERED_STRUCTURED,
                structured_terms=terms,
                ledger_eligible=False,
                source_snapshot_id=snapshot_id,
                available_to_system_at=available,
            )
        )
    return result


def _link_official_actions(
    records: list[CorporateActionObservation],
    candidates: list[_OfficialActionCandidate],
) -> tuple[list[CorporateActionObservation], int, list[str]]:
    """Link only unique exact candidates; every announcement may satisfy one hint."""

    linked: list[CorporateActionObservation] = []
    linked_count = 0
    reasons: list[str] = []

    def matches(
        item: CorporateActionObservation, candidate: _OfficialActionCandidate
    ) -> bool:
        return (
            item.announcement_date == candidate.published_date
            and item.report_period == candidate.report_period
            and item.action_type == candidate.action_type
        )

    candidate_match_counts = {
        candidate.announcement_id: sum(matches(item, candidate) for item in records)
        for candidate in candidates
    }
    for item in records:
        item_matches = [
            candidate
            for candidate in candidates
            if matches(item, candidate)
        ]
        if (
            len(item_matches) != 1
            or candidate_match_counts.get(item_matches[0].announcement_id) != 1
        ):
            linked.append(item)
            reasons.append(
                "OFFICIAL_MATCH_NOT_UNIQUE"
                if item_matches
                else "OFFICIAL_DOCUMENT_NOT_FOUND"
            )
            continue
        candidate = item_matches[0]
        linked.append(
            CorporateActionObservation.model_validate(
                {
                    **item.model_dump(mode="python"),
                    "created_at": candidate.available_to_system_at,
                    "status": CorporateActionStatus.OFFICIAL_DOCUMENT_LINKED,
                    "official_document_snapshot_id": candidate.document_snapshot_id,
                    "official_document_url": candidate.source_url,
                    "official_announcement_id": candidate.announcement_id,
                    "ledger_eligible": False,
                    "available_to_system_at": candidate.available_to_system_at,
                }
            )
        )
        linked_count += 1
    return linked, linked_count, list(dict.fromkeys(reasons))


def _extract_report_period(title: str) -> str | None:
    years = re.findall(r"(?<!\d)(20\d{2})(?!\d)", title)
    return years[0] if len(set(years)) == 1 else None


def _official_action_type(title: str) -> str | None:
    if any(key in title for key in ("送股", "转增")):
        return "STOCK_DISTRIBUTION_HINT"
    if any(key in title for key in ("现金", "派息", "现金红利")):
        return "CASH_DIVIDEND_HINT"
    return None


def _parse_eastmoney_daily(
    payload: dict[str, object],
    snapshot_id: str,
    available: datetime,
    symbol: str,
    market: Market,
    start: date,
    end: date,
) -> list[DailyBarObservation]:
    if payload.get("rc") != 0:
        raise ValueError("EastMoney daily request failed")
    request = payload.get("_astock_request")
    if (
        not isinstance(request, dict)
        or request.get("symbol") != symbol
        or request.get("market") != market.value
        or request.get("start") != start.isoformat()
        or request.get("end") != end.isoformat()
        or request.get("fqt") != 0
        or request.get("volume_unit") != "LOT_100_SHARES"
    ):
        raise ValueError("EastMoney daily request provenance mismatch")
    data = payload["data"]
    if not isinstance(data, dict) or not isinstance(data.get("klines"), list):
        raise ValueError("invalid EastMoney daily payload")
    raw_market = str(data.get("market"))
    expected_market_values = {
        Market.XSHG: {"1", "XSHG"},
        Market.XSHE: {"0", "XSHE"},
        Market.BJSE: {"0", "BJSE"},
        Market.INDEX: ({"0", "INDEX"} if symbol.startswith("399") else {"1", "INDEX"}),
    }[market]
    if str(data.get("code")) != symbol or raw_market not in expected_market_values:
        raise ValueError("EastMoney daily payload crossed the explicit instrument boundary")
    result: list[DailyBarObservation] = []
    previous: Decimal | None = None
    seen_dates: set[date] = set()
    for raw in data["klines"]:
        values = str(raw).split(",")
        if len(values) < 7:
            raise ValueError("malformed EastMoney daily row")
        session = date.fromisoformat(values[0])
        if session < start or session > end or session in seen_dates:
            raise ValueError("EastMoney daily date coverage is invalid")
        if seen_dates and session <= max(seen_dates):
            raise ValueError("EastMoney daily rows are not strictly ordered")
        seen_dates.add(session)
        open_, close, high, low = map(Decimal, values[1:5])
        identity = {
            "market": market.value,
            "symbol": symbol,
            "date": values[0],
            "row": values,
        }
        result.append(
            DailyBarObservation(
                created_at=available,
                observation_id=content_hash(identity),
                instrument_id=f"{market.value}:{symbol}",
                market=market,
                symbol=symbol,
                session_date=session,
                session_close_at=datetime.combine(session, time(15, 0), tzinfo=_SHANGHAI),
                open=open_,
                high=high,
                low=low,
                close=close,
                previous_close=previous,
                volume=Decimal(values[5]) * Decimal("100"),
                volume_unit=VolumeUnit.SHARE,
                amount=Decimal(values[6]),
                amount_unit=AmountUnit.CNY,
                adjustment_mode=AdjustmentMode.NONE,
                source_snapshot_id=snapshot_id,
                available_to_system_at=available,
            )
        )
        previous = close
    return result


def _verify_release_row(row: dict[str, Any], manifest: DatasetReleaseManifest) -> None:
    if (
        row["release_id"] != manifest.release_id
        or row["content_hash"] != manifest.content_hash
        or row["dataset_kind"] != manifest.dataset_kind.value
        or row["scope_key"] != manifest.scope_key
        or row["provider_id"] != manifest.provider_id
        or row["batch_id"] != manifest.batch_id
        or row["previous_release_id"] != manifest.previous_release_id
        or row["manifest_object_hash"] != row["artifact_object_hash"]
        or row["artifact_type"] != "DatasetReleaseManifest"
        or row["artifact_schema_version"] != manifest.schema_version
        or row["manifest_schema_version"] != manifest.schema_version
        or row["raw_snapshot_ids_json"]
        != canonical_json_bytes(manifest.raw_snapshot_ids).decode("utf-8")
        or row["observation_files_json"]
        != canonical_json_bytes(manifest.observation_files).decode("utf-8")
        or row["canonical_files_json"]
        != canonical_json_bytes(manifest.canonical_files).decode("utf-8")
        or row["coverage_json"] != canonical_json_bytes(manifest.coverage).decode("utf-8")
        or row["available_to_system_at"] != manifest.available_to_system_at.isoformat()
        or row["coverage_status"] != manifest.coverage.status.value
        or row["pit_status"] != manifest.pit_status.value
    ):
        raise ValueError("market-reference release chain mismatch")


def _is_legacy_release_row(row: dict[str, Any]) -> bool:
    try:
        marker = json.loads(str(row.get("coverage_json", "")))
    except (TypeError, ValueError, json.JSONDecodeError):
        return False
    return (
        isinstance(marker, dict)
        and marker.get("legacy_0038") is True
        and row.get("pit_status") == ReferencePitStatus.UNVERIFIED.value
        and row.get("manifest_schema_version") != DatasetReleaseManifest.model_fields[
            "schema_version"
        ].default
    )


def _record_date(record: Any) -> date:
    return (
        getattr(record, "session_date", None)
        or getattr(record, "status_date", None)
        or getattr(record, "announcement_date", None)
        or getattr(record, "ex_date", None)
        or date.min
    )


def _is_st_name(name: str) -> bool:
    normalized = name.upper().lstrip("*")
    return normalized.startswith("ST")


def _optional_date(value: str) -> date | None:
    return date.fromisoformat(value) if value else None


def _optional_decimal(value: str) -> Decimal | None:
    return Decimal(value) if value else None


__all__ = ["MarketReferenceService"]
