"""Read-only replay-result auditing over the existing canonical ledger."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from astock.core.hashing import content_hash
from astock.schemas.paper import ReplayCheckpoint, ReplayExecutionReport

if TYPE_CHECKING:
    from astock.investor_orchestration.models import InvestorRequestEnvelope
    from astock.investor_orchestration.output_validation import RegisteredOutputVerifier


def _time(value: str | datetime) -> datetime:
    result = datetime.fromisoformat(value) if isinstance(value, str) else value
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("replay evidence requires aware timestamps")
    return result.astimezone(UTC)


def _check_scope(
    verifier: RegisteredOutputVerifier,
    report: ReplayExecutionReport,
    request: InvestorRequestEnvelope,
) -> ReplayCheckpoint:
    cutoff = _time(request.evidence_cutoff)
    previous = _time(report.previous_cursor) if report.previous_cursor is not None else None
    cursor = _time(report.requested_cursor)
    instrument = f"{report.market.value}:{report.symbol}"
    if _time(report.created_at) > cutoff or cursor > cutoff:
        raise ValueError("replay report is outside the request time")
    if request.entity_ids and not any(
        verifier._same_identity(instrument, value) for value in request.entity_ids
    ):
        raise ValueError("replay report belongs to another security")
    if request.account_id is not None and report.account_id != request.account_id:
        raise ValueError("replay report belongs to another account")
    checkpoint = report.checkpoint
    if checkpoint is None or checkpoint.coverage_start is None or checkpoint.coverage_end is None:
        raise ValueError("replay result lacks a committed coverage checkpoint")
    end = _time(checkpoint.coverage_end)
    if (
        checkpoint.account_id != report.account_id
        or checkpoint.symbol != report.symbol
        or checkpoint.market != report.market
        or checkpoint.instrument_id != instrument
        or checkpoint.replay_quality != report.replay_quality
        or checkpoint.missing_bars != 0
        or not _time(checkpoint.coverage_start) <= end <= cursor
        or not checkpoint.market_cursor
        or _time(checkpoint.market_cursor) != end
        or (previous is not None and previous > end)
    ):
        raise ValueError("replay checkpoint scope or coverage differs")
    quality = {"60m": "PROVIDER_1H_APPROX", "5m": "DUAL_SOURCE_5M_VERIFIED"}
    if quality.get(checkpoint.actual_resolution) != report.replay_quality.value:
        raise ValueError("replay quality differs from the recorded resolution")
    if len(report.fill_ids) != len(set(report.fill_ids)):
        raise ValueError("replay report duplicates a fill identity")
    return checkpoint


def _commits(
    verifier: RegisteredOutputVerifier,
    connection: sqlite3.Connection,
    report: ReplayExecutionReport,
    checkpoint: ReplayCheckpoint,
) -> list[tuple[datetime, dict[str, Any]]]:
    end = checkpoint.coverage_end
    assert end is not None
    previous = _time(report.previous_cursor) if report.previous_cursor is not None else None
    rows = connection.execute(
        "SELECT * FROM paper_replay_bar_commit "
        "WHERE account_id=? AND market=? AND instrument_id=? AND symbol=? "
        "AND julianday(committed_at)<=julianday(?) "
        "AND julianday(json_extract(checkpoint_json,'$.coverage_end'))<=julianday(?) "
        "AND (? IS NULL OR julianday(json_extract(checkpoint_json,'$.coverage_end'))"
        ">=julianday(?)) ORDER BY committed_at,commit_id LIMIT 10001",
        (
            report.account_id,
            report.market.value,
            f"{report.market.value}:{report.symbol}",
            report.symbol,
            _time(report.created_at).isoformat(),
            _time(end).isoformat(),
            previous.isoformat() if previous else None,
            previous.isoformat() if previous else None,
        ),
    ).fetchall()
    if len(rows) > 10000:
        raise ValueError("replay audit exceeds the bounded commit budget")
    result: list[tuple[datetime, dict[str, Any]]] = []
    anchored = False
    for row in rows:
        payload = json.loads(verifier.objects.get_bytes(str(row["commit_object_hash"])))
        if not isinstance(payload, dict):
            raise ValueError("replay commit is not a canonical object")
        identity = {
            key: row[key]
            for key in (
                "account_id",
                "market",
                "instrument_id",
                "symbol",
                "bar_observation_id",
            )
        }
        if row["commit_id"] != content_hash(identity):
            raise ValueError("replay commit identity differs from account and bar")
        if any(payload.get(key) != row[key] for key in (*identity, "commit_id", "input_hash")):
            raise ValueError("replay commit index differs from immutable bytes")
        frozen = ReplayCheckpoint.model_validate(payload.get("checkpoint"))
        if frozen != ReplayCheckpoint.model_validate_json(str(row["checkpoint_json"])):
            raise ValueError("replay checkpoint index differs from immutable bytes")
        if payload.get("fill_ids") != json.loads(str(row["fill_ids_json"])):
            raise ValueError("replay fill index differs from immutable bytes")
        if frozen.coverage_end is None:
            raise ValueError("replay commit has no coverage end")
        at = _time(frozen.coverage_end)
        # The table reader recreates the model's created_at on every read. It is
        # object construction metadata, not the committed coverage/event time.
        anchored = anchored or frozen.model_dump(exclude={"created_at"}) == checkpoint.model_dump(
            exclude={"created_at"}
        )
        if previous is None or at > previous:
            result.append((at, payload))
    if not anchored or len(result) != report.processed_bars:
        raise ValueError("replay checkpoint or processed count lacks exact committed evidence")
    return result


def _check_fills(
    connection: sqlite3.Connection,
    report: ReplayExecutionReport,
    commits: list[tuple[datetime, dict[str, Any]]],
) -> None:
    fills: list[str] = []
    orders: set[str] = set()
    for at, payload in commits:
        plans, identities = payload.get("fill_plans"), payload.get("fill_ids")
        if not isinstance(plans, list) or not isinstance(identities, list):
            raise ValueError("replay commit has malformed fill plans")
        if not all(isinstance(plan, dict) for plan in plans):
            raise ValueError("replay commit has malformed fill plan items")
        if identities != [plan.get("fill_id") for plan in plans]:
            raise ValueError("replay plans and fill identities differ")
        for plan in plans:
            row = connection.execute(
                "SELECT f.*,o.account_id,o.symbol,b.market,b.instrument_id,"
                "b.fee_rule_version,b.fee_schedule_hash,b.confirmation_hash,"
                "c.confirmation_hash AS verified_confirmation_hash "
                "FROM fill f JOIN order_record o ON o.order_id=f.order_id "
                "JOIN paper_order_rule_binding b ON b.order_id=o.order_id "
                "JOIN paper_operation_confirmation c ON c.confirmation_id=b.confirmation_id "
                "WHERE f.fill_id=?",
                (plan["fill_id"],),
            ).fetchone()
            if row is None or any(
                row[key] != plan.get(key)
                for key in (
                    "fill_id",
                    "order_id",
                    "qty",
                    "price_fen",
                )
            ):
                raise ValueError("replay fill differs from its committed plan")
            if (
                row["account_id"] != report.account_id
                or row["symbol"] != report.symbol
                or row["market"] != report.market.value
                or row["instrument_id"] != f"{report.market.value}:{report.symbol}"
                or row["fee_rule_version"] != report.fee_rule_version
                or row["fee_schedule_hash"] != payload.get("fee_schedule_hash")
                or not row["confirmation_hash"]
                or row["confirmation_hash"] != row["verified_confirmation_hash"]
                or _time(str(row["occurred_at"])) != at
                or row["replay_quality"] != report.replay_quality.value
                or row["price_milli_yuan"] != plan.get("price_milli_yuan")
            ):
                raise ValueError("replay fill identity, time, fees or confirmation differs")
            fills.append(str(row["fill_id"]))
            orders.add(str(row["order_id"]))
    if len(fills) != len(set(fills)) or set(fills) != set(report.fill_ids):
        raise ValueError("reported fills differ from the committed interval")
    if len(orders) != report.matched_orders:
        raise ValueError("reported matched orders differ from committed fills")


def verify_replay_output(
    verifier: RegisteredOutputVerifier,
    report: ReplayExecutionReport,
    request: InvestorRequestEnvelope,
) -> None:
    """Check immutable facts in one read snapshot; never run matching or write state."""
    checkpoint = _check_scope(verifier, report, request)
    with closing(verifier.state.connect()) as connection:
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")
        accounts = [
            str(row[0])
            for row in connection.execute(
                "SELECT account_id FROM paper_account ORDER BY account_id"
            )
        ]
        if report.account_id not in accounts:
            raise ValueError("replay account is unavailable")
        if request.account_id is None and accounts != [report.account_id]:
            raise ValueError("replay account selection is ambiguous")
        commits = _commits(verifier, connection, report, checkpoint)
        _check_fills(connection, report, commits)
