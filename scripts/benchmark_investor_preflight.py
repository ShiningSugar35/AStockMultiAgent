"""Reproducible, isolated canonical preflight benchmark (never production accounts).

Run from the repository root with:
  python -B -m scripts.benchmark_investor_preflight --profile all
Prefer scripts/run_local_quality.py python -m scripts.benchmark_investor_preflight
so interpreter caches, temporary files and independent source-tree evidence are local.

Cold means a new service/application cache, NOT an emptied OS filesystem cache.
Timing includes receipt persistence; fixture setup and assertion checks are separate.
Fixture setup reuses tests/unit/test_paper_operations.py recorded references and
its dev-only signing key with the unchanged PaperOperationService; fills use the
canonical ledger. These synthetic states are not market observations or live evidence.
Read timings do not certify import/startup, research, end-to-end replay or live trading.
Timer semantics: https://docs.python.org/3/library/time.html
Read snapshot semantics: https://sqlite.org/isolation.html
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import platform
import sys
import time
import tracemalloc
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4

from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.external_accounts import ExternalAccountRepository
from astock.investor_orchestration.models import (
    InvestorRequestEnvelope,
    InvestorSessionPreflightReceipt,
    RequestIntent,
    SideEffectClass,
)
from astock.investor_orchestration.preflight import InvestorSessionPreflightService
from astock.investor_orchestration.store import InvestorOrchestrationStore
from astock.paper_trading import PaperOperationService, load_fee_schedule, paper_request_hash
from astock.paper_trading.ledger import LedgerService
from astock.paper_trading.operation import PaperInstrumentTradingFacts
from astock.schemas import (
    InstrumentRecord,
    Order,
    OrderSide,
    PaperOperationRequest,
    PaperPlaceOrderPayload,
)
from astock.schemas.external_accounts import ExternalAccountEventDraft, ExternalAccountEventType
from astock.schemas.market import Market
from scripts.run_local_quality import fingerprint
from tests.unit.test_paper_operations import (
    _PUBLIC_KEY_PEM,
    _confirmation,
    _ReferenceFixture,
)
from tests.unit.test_paper_operations import (
    NOW as FIXTURE_NOW,
)

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / ".ai-bridge/reviews/preflight-perf-20260908/runs"
INITIAL_FEN = 1_000_000_000
P95_LIMIT_SECONDS = 2.0


@dataclass(frozen=True)
class Workload:
    name: str
    account_pairs: int
    positions_per_account: int
    actual_trades_per_account: int
    open_orders_per_account: int

    def __post_init__(self) -> None:
        bounds = (
            (self.account_pairs, 1, 16),
            (self.positions_per_account, 1, 50),
            (self.actual_trades_per_account, self.positions_per_account, 5000),
            (self.open_orders_per_account, 1, 500),
        )
        if any(type(value) is not int or not low <= value <= high for value, low, high in bounds):
            raise ValueError("workload size is outside the bounded fixture budget")
        if not self.name or not self.name.isascii() or not self.name.replace("-", "").isalnum():
            raise ValueError("workload name must be a bounded path component")


PROFILES = {
    "small": Workload("small", 1, 2, 20, 20),
    "scaled": Workload("scaled", 4, 8, 250, 50),
}


@dataclass
class Fixture:
    state: StateStore
    metadata: InvestorOrchestrationStore
    ledger: LedgerService
    external: ExternalAccountRepository
    actual_cash: dict[str, Decimal | None] = field(default_factory=dict)
    paper_cash: dict[str, Decimal] = field(default_factory=dict)
    actual_positions: dict[tuple[str, str], Decimal] = field(default_factory=dict)
    paper_positions: dict[tuple[str, str], Decimal] = field(default_factory=dict)
    orders: dict[str, str] = field(default_factory=dict)
    frozen_cash: Decimal = Decimal(0)
    operations: PaperOperationService | None = None


class _BenchmarkReferences(_ReferenceFixture):
    """Reuse recorded reference data, not success stubs; generated identities are explicit."""

    def trading_classification(
        self,
        instrument: InstrumentRecord,
        *,
        visible_at: datetime,
    ) -> PaperInstrumentTradingFacts:
        if instrument.market != Market.XSHG or not 600000 <= int(instrument.symbol) < 600050:
            raise ValueError("unregistered benchmark instrument")
        return PaperInstrumentTradingFacts(
            board="MAIN",
            risk_status="NORMAL",
            fixed_price_limit_eligible=True,
            suspension_status_verified=True,
            suspended=False,
            evidence_id="benchmark-explicit-recorded-main-board",
        )


def _confirmed_order(fixture: Fixture, account: str, symbol: str, qty: int, key: str) -> Order:
    """A dev-only signing fixture passes through the unchanged approval service."""
    from astock.paper_trading import load_paper_trading_rules

    if fixture.operations is None:
        fixture.operations = PaperOperationService(
            fixture.state,
            ObjectStore(fixture.state.path.parent / "objects/sha256"),
            fixture.ledger,
            _BenchmarkReferences(),
            load_fee_schedule(ROOT / "configs/fee_rules.yaml"),
            clock=lambda: FIXTURE_NOW + timedelta(minutes=2),
            trusted_confirmation_keys={"test-ed25519": _PUBLIC_KEY_PEM},
            trading_rules=load_paper_trading_rules(ROOT / "configs/paper_trading_rules.yaml"),
        )
    request = PaperOperationRequest(
        operation_id="0" * 64,
        account_id=account,
        idempotency_key=key,
        requested_at=FIXTURE_NOW,
        expires_at=FIXTURE_NOW + timedelta(minutes=30),
        payload=PaperPlaceOrderPayload(
            market=Market.XSHG,
            symbol=symbol,
            side=OrderSide.BUY,
            qty=qty,
            limit_price_fen=1000,
            calendar_release_id="1" * 64,
            instrument_release_id="2" * 64,
            daily_release_id="3" * 64,
            fee_rule_version="cn-a-share-paper-2026-07-13",
        ),
    )
    request = request.model_copy(update={"operation_id": paper_request_hash(request)})
    outcome = fixture.operations.execute(request, _confirmation(request))
    payload = outcome.result.get("order")
    if not isinstance(payload, dict) or "order_id" not in payload:
        raise ValueError("confirmed fixture operation did not produce a canonical order")
    return fixture.ledger.get_order(str(payload["order_id"]))


def create_run_directory(base: Path, *, root: Path = ROOT) -> Path:
    """Never reuse a DB or allow an output directory outside local bridge metadata."""
    root = root.resolve()
    base = base if base.is_absolute() else root / base
    allowed = root / ".ai-bridge"
    if not base.resolve().is_relative_to(allowed):
        raise ValueError("benchmark output must stay inside the project .ai-bridge directory")
    for part in (base, *base.parents):
        if part == root:
            break
        if part.is_symlink() or part.is_junction():
            raise ValueError("benchmark output must not traverse links or junctions")
    base.mkdir(parents=True, exist_ok=True)
    if not base.resolve().is_relative_to(allowed):
        raise ValueError("benchmark output changed during directory creation")
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    directory = base / f"{stamp}-{uuid4().hex[:12]}"
    directory.mkdir(exist_ok=False)
    return directory


def sample_stats(samples: Sequence[float]) -> dict[str, Any]:
    """Nearest-rank percentiles of individual completed requests, not batch time."""
    if not samples or any(not math.isfinite(value) or value < 0 for value in samples):
        raise ValueError("timings must be a non-empty finite non-negative sequence")
    ordered = sorted(samples)
    return {
        "count": len(samples),
        "p50_seconds": ordered[math.ceil(0.50 * len(ordered)) - 1],
        "p95_seconds": ordered[math.ceil(0.95 * len(ordered)) - 1],
        "max_seconds": ordered[-1],
        "sum_request_seconds": sum(samples),
        "samples_seconds": list(samples),
        "percentile_method": "nearest-rank",
    }


def qualification_failures(
    phases: Mapping[str, Mapping[str, Any]],
    checks: Mapping[str, bool],
    *,
    required_samples: int = 100,
) -> list[str]:
    failures = [f"CHECK_FAILED:{name}" for name, passed in checks.items() if passed is not True]
    for required in (
        "economic_tables_unchanged",
        "source_tree_stable",
        "canonical_outputs_verified",
        "revision_invalidated",
        "lane_isolation",
        "receipt_persistence_verified",
    ):
        if required not in checks:
            failures.append(f"CHECK_MISSING:{required}")
    for phase in ("cold", "warm", "concurrent", "after_revision"):
        result = phases.get(phase)
        if result is None:
            failures.append(f"PHASE_MISSING:{phase}")
            continue
        timings = result.get("samples_seconds", [])
        try:
            recomputed = sample_stats(timings)
        except (TypeError, ValueError):
            failures.append(f"INVALID_TIMINGS:{phase}")
            continue
        if any(result.get(key) != recomputed[key] for key in recomputed):
            failures.append(f"TIMING_SUMMARY_MISMATCH:{phase}")
        if phase in {"warm", "concurrent"}:
            if recomputed["count"] < required_samples:
                failures.append(f"INSUFFICIENT_SAMPLES:{phase}")
            if recomputed["p95_seconds"] > P95_LIMIT_SECONDS:
                failures.append(f"P95_EXCEEDED:{phase}")
    return failures


def _draft(account: str, at: datetime, key: str, **values: Any) -> ExternalAccountEventDraft:
    return ExternalAccountEventDraft(
        account_id=account,
        occurred_at=at,
        available_to_system_at=at,
        created_at=at,
        idempotency_key=key,
        **values,
    )


def setup_fixture(directory: Path, workload: Workload) -> Fixture:
    """Only called with a newly created child directory; no production path option."""
    directory.mkdir(exist_ok=False)
    state = StateStore(directory / "state.sqlite", ROOT / "migrations")
    state.migrate()
    metadata = InvestorOrchestrationStore(state.path)
    metadata.initialize()
    fixture = Fixture(
        state,
        metadata,
        LedgerService(state),
        ExternalAccountRepository(state, ObjectStore(directory / "objects/sha256")),
    )
    at = datetime.now(UTC) - timedelta(days=1)
    for index in range(workload.account_pairs):
        actual, paper = f"actual-{index}", f"paper-{index}"
        fixture.external.create_account(account_id=actual, display_name=actual, created_at=at)
        drafts = []
        # Half the accounts intentionally have unknown cash. No empty/zero substitution.
        cash = Decimal(INITIAL_FEN) / 100 if index % 2 == 0 else None
        fixture.actual_cash[actual] = cash
        if cash is not None:
            drafts.append(
                _draft(
                    actual,
                    at,
                    f"{actual}-deposit",
                    event_type=ExternalAccountEventType.CASH_DEPOSIT,
                    amount_cny=cash,
                )
            )
        for number in range(workload.actual_trades_per_account):
            symbol = f"{600000 + number % workload.positions_per_account:06d}"
            quantity = 100 * (index + 1)
            when = at + timedelta(milliseconds=number + 1)
            drafts.append(
                _draft(
                    actual,
                    when,
                    f"{actual}-trade-{number}",
                    event_type=ExternalAccountEventType.TRADE,
                    market=Market.XSHG,
                    symbol=symbol,
                    side="BUY",
                    quantity=quantity,
                    price_cny=Decimal(10),
                )
            )
            key = (actual, f"XSHG:{symbol}")
            fixture.actual_positions[key] = fixture.actual_positions.get(key, Decimal(0)) + quantity
            current_cash = fixture.actual_cash[actual]
            if current_cash is not None:
                fixture.actual_cash[actual] = current_cash - Decimal(quantity * 10)
        fixture.external.append_drafts(drafts)
        fixture.ledger.initialize_account(paper, INITIAL_FEN)
        fixture.paper_cash[paper] = Decimal(INITIAL_FEN) / 100
        for number in range(workload.positions_per_account):
            symbol = f"{600000 + number:06d}"
            quantity = 100 * (index + 1)
            order = _confirmed_order(fixture, paper, symbol, quantity, f"{paper}-filled-{number}")
            fixture.ledger.record_fill(
                fill_id=f"{paper}-fill-{number}",
                order_id=order.order_id,
                qty=quantity,
                price_fen=1000,
                occurred_at=datetime.now(UTC),
            )
            fixture.paper_positions[(paper, f"XSHG:{symbol}")] = Decimal(quantity)
            fixture.paper_cash[paper] -= Decimal(quantity * 10)
        for number in range(workload.open_orders_per_account):
            add_open_order(fixture, paper, f"{paper}-open-{number}")
    return fixture


def add_open_order(fixture: Fixture, account: str, key: str) -> None:
    order = _confirmed_order(fixture, account, "600000", 100, key)
    fixture.orders[order.order_id] = account
    reserve = Decimal(order.reserved_fen) / 100
    fixture.paper_cash[account] -= reserve
    fixture.frozen_cash += reserve


def verify_receipt(receipt: InvestorSessionPreflightReceipt, fixture: Fixture) -> None:
    actual, paper = receipt.context.actual, receipt.context.paper
    settlements: dict[tuple[str, str], Decimal] = {}
    for item in paper.pending_settlements:
        key = (item.account_id, item.instrument_id)
        settlements[key] = settlements.get(key, Decimal(0)) + item.quantity
    comparisons = {
        "pending_settlements": settlements == fixture.paper_positions,
        "available_quantity": all(item.available_quantity == 0 for item in paper.positions),
        "confirmed_orders": all(
            item.confirmed
            and item.lane == paper.lane
            and item.quantity == 100
            and item.filled_quantity == 0
            and item.limit_price == Decimal(10)
            and item.side == "BUY"
            and item.status == "ACCEPTED"
            and item.instrument_id == "XSHG:600000"
            for item in paper.open_orders
        ),
        "actual_accounts": set(actual.account_ids) == set(fixture.actual_cash),
        "paper_accounts": set(paper.account_ids) == set(fixture.paper_cash),
        "actual_cash": actual.cash_by_account == fixture.actual_cash,
        "paper_cash": paper.cash_by_account == fixture.paper_cash,
        "frozen_cash": paper.frozen_cash == fixture.frozen_cash,
        "orders": {item.order_id: item.account_id for item in paper.open_orders} == fixture.orders,
        "order_count": len(paper.open_orders) == len(fixture.orders),
        "lane_isolation": not actual.open_orders
        and set(actual.account_ids).isdisjoint(paper.account_ids),
        "audit": actual.audit_status == paper.audit_status == "PASS",
        "freshness": receipt.freshness == "FRESH",
    }
    for lane, expected, cash in (
        (actual, fixture.actual_positions, fixture.actual_cash),
        (paper, fixture.paper_positions, fixture.paper_cash),
    ):
        name = lane.lane.value
        comparisons[f"{name}_positions"] = (
            {(item.account_id, item.instrument_id): item.quantity for item in lane.positions}
            == expected
            and len(lane.positions) == len(expected)
            and all(
                item.average_cost == Decimal(10) and item.lane == lane.lane
                for item in lane.positions
            )
        )
        known = all(value is not None for value in cash.values())
        comparisons[f"{name}_unknown_cash"] = lane.unknown_cash is (not known)
        comparisons[f"{name}_total_cash"] = lane.known_cash == (
            sum((value for value in cash.values() if value is not None), Decimal(0))
            if known
            else None
        )
    if not all(comparisons.values()):
        raise ValueError(
            "canonical output mismatch: " + ",".join(k for k, ok in comparisons.items() if not ok)
        )


def economic_digest(state: StateStore) -> dict[str, Any]:
    """Stream economic rows in one read snapshot; include identities and bindings."""
    core = {
        "journal",
        "ledger_account",
        "ledger_entry",
        "order_record",
        "fill",
        "position",
        "position_settlement",
    }
    result = {}
    with closing(state.connect()) as connection:
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")
        tables = connection.execute(
            "SELECT name,sql FROM sqlite_master WHERE type='table' ORDER BY name"
        ).fetchall()
        for name, ddl in tables:
            if name not in core and not name.startswith(("external_account", "paper_")):
                continue
            quoted = '"' + name.replace('"', '""') + '"'
            info = connection.execute(f"PRAGMA table_info({quoted})").fetchall()
            keys = [row[1] for row in sorted(info, key=lambda row: row[5]) if row[5]]
            ordering = ",".join('"' + key.replace('"', '""') + '"' for key in keys) or "rowid"
            digest = hashlib.sha256(str(ddl).encode())
            count = 0
            for row in connection.execute(f"SELECT * FROM {quoted} ORDER BY {ordering}"):
                digest.update(
                    json.dumps(tuple(row), ensure_ascii=False, default=str).encode("utf-8") + b"\n"
                )
                count += 1
            result[name] = {"row_count": count, "sha256": digest.hexdigest()}
    if not core <= result.keys():
        raise ValueError("economic digest is missing a required canonical table")
    return result


def _request(key: str, at: datetime) -> InvestorRequestEnvelope:
    return InvestorRequestEnvelope(
        request_id=key,
        idempotency_key=key,
        question_time=at,
        user_timezone="Asia/Shanghai",
        raw_text="隔离基准：恢复账户，不执行任何交易。",
        normalized_intent=RequestIntent.PAPER_STATUS,
        side_effect=SideEffectClass.READ,
    )


def measure_phase(
    name: str,
    samples: int,
    build: Callable[[InvestorRequestEnvelope], InvestorSessionPreflightReceipt],
    fixture: Fixture,
    *,
    workers: int = 1,
    expected_cache_hits: int,
) -> dict[str, Any]:
    if name not in {"cold", "warm", "concurrent", "after-revision"}:
        raise ValueError("unregistered benchmark phase")
    at = datetime.now(UTC)
    requests = [_request(f"{name}-{uuid4().hex}", at) for _ in range(samples)]

    def invoke(request: InvestorRequestEnvelope) -> tuple[float, bool]:
        started = time.perf_counter_ns()
        receipt = build(request)
        elapsed = (time.perf_counter_ns() - started) / 1e9
        verify_receipt(receipt, fixture)
        return elapsed, receipt.built_from_cache

    cpu, wall = time.process_time(), time.perf_counter()
    if workers == 1:
        measured = [invoke(request) for request in requests]
    else:
        with ThreadPoolExecutor(max_workers=workers) as executor:
            measured = list(executor.map(invoke, requests))
    total, cpu_seconds = time.perf_counter() - wall, time.process_time() - cpu
    hits = sum(hit for _, hit in measured)
    result = {
        **sample_stats([elapsed for elapsed, _ in measured]),
        "wall_seconds": total,
        "process_cpu_seconds": cpu_seconds,
        "workers": workers,
        "cache_hits": hits,
        "expected_cache_hits": expected_cache_hits,
        "timing_boundary": "build + durable receipt; no request setup/assertion timing",
        "concurrency_boundary": "worker entry to return; includes lock wait, not executor queue",
    }
    with (fixture.state.path.parent / f"phase-{name}.json").open("x", encoding="utf-8") as output:
        output.write(json.dumps(result, indent=2) + "\n")
    if hits != expected_cache_hits:
        raise ValueError(f"{name} cache hits {hits} != expected {expected_cache_hits}")
    return result


def disk_footprint(directory: Path) -> dict[str, int]:
    files = [path for path in directory.rglob("*") if path.is_file()]
    return {"files": len(files), "bytes": sum(path.stat().st_size for path in files)}


def run_profile(
    directory: Path,
    workload: Workload,
    *,
    samples: int,
    cold_samples: int,
    workers: int,
) -> dict[str, Any]:
    fixture = setup_fixture(directory / workload.name, workload)
    before = economic_digest(fixture.state)
    disk_before = disk_footprint(fixture.state.path.parent)

    def cold_service(request: InvestorRequestEnvelope) -> InvestorSessionPreflightReceipt:
        return InvestorSessionPreflightService(fixture.metadata).build(request)

    phases = {
        "cold": measure_phase(
            "cold",
            cold_samples,
            cold_service,
            fixture,
            expected_cache_hits=0,
        )
    }
    service = InvestorSessionPreflightService(fixture.metadata)
    first = service.build(_request("warmup-" + uuid4().hex, datetime.now(UTC)))
    verify_receipt(first, fixture)
    phases["warm"] = measure_phase(
        "warm", samples, service.build, fixture, expected_cache_hits=samples
    )
    # Empty shared application cache tests coalescing under contention: exactly one miss.
    shared = InvestorSessionPreflightService(fixture.metadata)
    phases["concurrent"] = measure_phase(
        "concurrent",
        samples,
        shared.build,
        fixture,
        workers=workers,
        expected_cache_hits=samples - 1,
    )
    after = economic_digest(fixture.state)
    # Intentional fixture mutations, OUTSIDE the zero-write measured read phases.
    add_open_order(fixture, "paper-0", "revision-probe-order")
    at = datetime.now(UTC)
    fixture.external.append_drafts(
        [
            _draft(
                "actual-0",
                at,
                "revision-probe-deposit",
                event_type=ExternalAccountEventType.CASH_DEPOSIT,
                amount_cny=Decimal(100),
            )
        ]
    )
    old_cash = fixture.actual_cash["actual-0"]
    if old_cash is None:
        raise ValueError("revision fixture requires its known-cash account")
    fixture.actual_cash["actual-0"] = old_cash + 100
    mutated = economic_digest(fixture.state)
    phases["after_revision"] = measure_phase(
        "after-revision",
        1,
        service.build,
        fixture,
        expected_cache_hits=0,
    )
    latest = service.build(_request("revision-check-" + uuid4().hex, datetime.now(UTC)))
    verify_receipt(latest, fixture)
    revision_changed = all(
        latest.source_revision_vector[lane] != first.source_revision_vector[lane]
        for lane in ("actual", "paper")
    )
    # Memory instrumentation is separate so it cannot bias the timed p95 phases.
    tracemalloc.start()
    try:
        memory_receipt = service.build(_request("heap-probe-" + uuid4().hex, datetime.now(UTC)))
        verify_receipt(memory_receipt, fixture)
        heap_current, heap_peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    final = economic_digest(fixture.state)
    with closing(fixture.state.connect()) as connection:
        receipt_count = connection.execute(
            "SELECT COUNT(*) FROM investor_request_receipts"
        ).fetchone()[0]
        foreign_keys_ok = connection.execute("PRAGMA foreign_key_check").fetchone() is None
    expected_receipts = cold_samples + 2 * samples + 4
    return {
        "profile": asdict(workload),
        "phases": phases,
        "checks": {
            "economic_tables_unchanged": before == after and mutated == final,
            "canonical_outputs_verified": True,
            "lane_isolation": True,
            "revision_invalidated": revision_changed,
            "receipt_persistence_verified": receipt_count == expected_receipts,
            "foreign_keys_valid": foreign_keys_ok,
        },
        "economic_before": before,
        "economic_after_reads": after,
        "economic_after_intentional_fixture_mutation": mutated,
        "economic_final": final,
        "expected_receipts": expected_receipts,
        "observed_receipts": receipt_count,
        "disk_before_reads": disk_before,
        "disk_after_reads": disk_footprint(fixture.state.path.parent),
        "memory": {
            "python_traced_current_bytes": heap_current,
            "python_traced_peak_bytes": heap_peak,
            "scope": "one separate warm request, Python allocations only; not process RSS",
        },
        "scope": "canonical read benchmark only; not research/approval/trading E2E",
        "cold_semantics": "new service cache each request; existing process and OS caches",
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", choices=(*PROFILES, "all"), default="all")
    parser.add_argument("--samples", type=int, default=100)
    parser.add_argument("--cold-samples", type=int, default=10)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)
    if (
        not 1 <= args.samples <= 1000
        or not 1 <= args.cold_samples <= 100
        or not 2 <= args.workers <= 16
    ):
        parser.error("samples must be 1..1000, cold samples 1..100, and workers 2..16")
    directory = create_run_directory(args.output_dir)
    before = fingerprint()
    started = time.perf_counter()
    report: dict[str, Any] = {
        "schema_version": "canonical-preflight-benchmark-v1",
        "status": "RUNNING",
        "started_at": datetime.now(UTC).isoformat(),
        "samples": args.samples,
        "source_hash_before": before,
        "profiles": [],
        "environment": {
            "python": sys.version,
            "platform": platform.platform(),
            "cpu_count": os.cpu_count(),
        },
        "p95_limit_seconds": P95_LIMIT_SECONDS,
        "broker_execution_allowed": False,
        "network_requested": False,
        "measurement_scope": "isolated fixture; no import/setup timing or production data",
    }
    print(json.dumps({"benchmark_directory": str(directory.relative_to(ROOT))}), flush=True)
    failures: list[str] = []
    try:
        profiles = list(PROFILES.values()) if args.profile == "all" else [PROFILES[args.profile]]
        for workload in profiles:
            result = run_profile(
                directory,
                workload,
                samples=args.samples,
                cold_samples=args.cold_samples,
                workers=args.workers,
            )
            report["profiles"].append(result)
            print(
                json.dumps(
                    {
                        "finished_profile": workload.name,
                        "phases": {
                            name: {
                                key: value
                                for key, value in stats.items()
                                if key != "samples_seconds"
                            }
                            for name, stats in result["phases"].items()
                        },
                    }
                ),
                flush=True,
            )
    except Exception as exc:
        report["error"] = {"type": type(exc).__name__, "message": str(exc)}
        failures.append("BENCHMARK_EXCEPTION")
    after = fingerprint()
    stable = before == after
    for result in report["profiles"]:
        result["checks"]["source_tree_stable"] = stable
        result["qualification_failures"] = qualification_failures(
            result["phases"], result["checks"]
        )
        failures.extend(
            f"{result['profile']['name']}:{issue}" for issue in result["qualification_failures"]
        )
    if not stable:
        failures.append("SOURCE_TREE_CHANGED")
    report.update(
        source_hash_after=after,
        source_tree_stable=stable,
        finished_at=datetime.now(UTC).isoformat(),
        duration_seconds=time.perf_counter() - started,
        qualification_failures=failures,
        status="PASS"
        if not failures
        else (
            "SMOKE_ONLY"
            if all("INSUFFICIENT_SAMPLES" in failure for failure in failures)
            else "FAIL"
        ),
    )
    path = directory / "report.json"
    with path.open("x", encoding="utf-8", newline="\n") as output:
        output.write(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(
        json.dumps(
            {
                "status": report["status"],
                "report": str(path.relative_to(ROOT)),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "failures": failures,
            }
        ),
        flush=True,
    )
    return 0 if report["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
