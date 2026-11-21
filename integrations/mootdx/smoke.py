"""Reproducible, low-frequency public Mootdx smoke; evidence stays in the project."""
from __future__ import annotations

import argparse
import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from astock.core.errors import AStockError
from astock.core.hashing import canonical_json_bytes
from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.market_data.quality import validate_batch
from astock.providers.mootdx_fallback import MootdxFallbackProvider, MootdxServer, MootdxSettings
from astock.schemas import BarRequest, Frequency, Market

ROOT = Path(__file__).resolve().parents[2]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", default="600519")
    parser.add_argument("--days", type=int, choices=range(1, 366), default=7)
    parser.add_argument("--limit", type=int, choices=range(1, 10001), default=800)
    parser.add_argument("--market", choices=["XSHG", "XSHE"], default="XSHG")
    parser.add_argument(
        "--server", action="append", default=[], help="Explicit public IP:port; max two"
    )
    parser.add_argument("--protocol", choices=["STANDARD", "MAC_COMPAT"], default=None)
    args = parser.parse_args()
    if len(args.server) > 2:
        parser.error("At most two explicit candidates per smoke")
    started = datetime.now(UTC)
    directory = (
        ROOT / "runtime" / "provider-probes" / "mootdx" / started.strftime("%Y%m%dT%H%M%S%fZ")
    )
    directory.mkdir(parents=True)
    objects = ObjectStore(directory / "objects")
    state = StateStore(directory / "state.sqlite", ROOT / "migrations")
    state.migrate()
    provider = MootdxFallbackProvider(objects, state, project_root=ROOT)
    if args.server:
        configured = provider.settings.model_dump()
        configured["servers"] = [
            MootdxServer(
                host=value.rsplit(":", 1)[0], port=int(value.rsplit(":", 1)[1])
            ).model_dump()
            for value in args.server
        ]
        provider.settings = MootdxSettings.model_validate(configured)
    if args.protocol:
        provider.settings = provider.settings.model_copy(update={"protocol": args.protocol})
    inputs = [
        "src/astock/providers/mootdx_fallback.py", "integrations/mootdx/worker.py",
        "integrations/mootdx/requirements.lock", "configs/mootdx_fallback.yaml",
        "integrations/mootdx/transport_compat.py",
    ]
    report: dict = {
        "schema_version": "mootdx-live-smoke-v1",
        "started_at": started.isoformat(), "symbol": args.symbol, "market": args.market,
        "input_sha256": {
            name: hashlib.sha256((ROOT / name).read_bytes()).hexdigest() for name in inputs
        },
        "servers": [server.model_dump() for server in provider.settings.servers],
        "results": [],
    }
    for operation in ("quote", "60m", "5m"):
        outcome: dict[str, Any] = {"operation": operation}
        try:
            if operation == "quote":
                quote, snapshot = provider.fetch_quote(args.symbol, Market(args.market))
                outcome.update(
                    status="PASS", record_count=1, price=str(quote["price"]),
                    freshness_verified=quote["freshness_verified"],
                    source_time_of_day=quote["source_time_of_day"],
                    snapshot_id=snapshot.snapshot_id,
                )
            else:
                batch = provider.fetch_bars(BarRequest(
                    symbol=args.symbol, market=Market(args.market), frequency=Frequency(operation),
                    requested_start=started - timedelta(days=args.days), requested_end=started,
                    limit=args.limit,
                ))
                quality = validate_batch(batch)
                outcome.update(
                    status=(
                        "PASS" if batch.bar_count and quality.quality_status.value != "FAIL"
                        else "FAIL"
                    ),
                    record_count=batch.bar_count, quality=quality.quality_status.value,
                    actual_start=batch.actual_start, actual_end=batch.actual_end,
                    snapshot_id=batch.raw_snapshot_id,
                )
            digest = str(outcome["snapshot_id"]).split(":", 1)[1]
            raw = json.loads(objects.get_bytes(digest))
            outcome.update(
                sdk=raw.get("sdk"), sdk_version=raw.get("sdk_version"),
                transport_sdk=raw.get("transport_sdk"),
                protocol=raw.get("protocol"), transport_patch=raw.get("transport_patch"),
                server=raw.get("server"), stop_reason=raw.get("stop_reason"),
            )
        except AStockError as exc:
            outcome.update(
                status="FAIL", failure_class=exc.failure_class.value, details=exc.details
            )
        report["results"].append(outcome)
        print(json.dumps(outcome, ensure_ascii=False, default=str), flush=True)
    report["finished_at"] = datetime.now(UTC).isoformat()
    report_path = directory / "report.json"
    report_path.write_bytes(canonical_json_bytes(report))
    print(json.dumps({"report_path": str(report_path)}, ensure_ascii=False))
    return 0 if all(item["status"] == "PASS" for item in report["results"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
