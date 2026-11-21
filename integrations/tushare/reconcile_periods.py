"""Compare returned weekly/monthly native units against daily data, without generalizing history."""
from __future__ import annotations

import argparse
import hashlib
import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from astock.core.errors import ProviderError
from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.providers.tushare_structured import TushareQueryRequest, TushareStructuredProvider
from astock.settings import ProjectPaths

ROOT = Path(__file__).resolve().parents[2]


def reconcile(
    daily: list[dict[str, Any]], aggregate: list[dict[str, Any]], frequency: str,
    start: date, end: date,
) -> list[dict[str, Any]]:
    results = []
    for row in aggregate:
        period_end = date.fromisoformat(str(row["trade_date"]))
        if frequency == "weekly":
            period_start = period_end - timedelta(days=period_end.weekday())
        else:
            period_start = period_end.replace(day=1)
        if period_start < start or period_end > end:
            continue
        days = [
            item for item in daily
            if period_start <= date.fromisoformat(str(item["trade_date"])) <= period_end
        ]
        if not days:
            continue
        shares = sum(Decimal(str(item["vol"])) * 100 for item in days)
        yuan = sum(Decimal(str(item["amount"])) * 1000 for item in days)
        returned_volume, returned_amount = Decimal(str(row["vol"])), Decimal(str(row["amount"]))
        expected_return = Decimal(str(row["close"])) / Decimal(str(row["pre_close"])) - 1
        actual_return = Decimal(str(row["pct_chg"]))
        volume_pass = abs(shares - returned_volume) <= 1
        amount_pass = abs(yuan - returned_amount) <= 1
        return_pass = abs(expected_return - actual_return) <= Decimal("0.00006")
        results.append({
            "period_end": period_end.isoformat(), "daily_count": len(days),
            "daily_sum_shares": str(shares), "aggregate_volume_native": str(returned_volume),
            "daily_sum_cny": str(yuan), "aggregate_amount_native": str(returned_amount),
            "expected_decimal_return": str(expected_return),
            "aggregate_pct_chg_native": str(actual_return),
            "matches_shares": volume_pass, "matches_cny": amount_pass,
            "matches_decimal_return": return_pass,
            "status": (
                "MATCHED_SAMPLE" if volume_pass and amount_pass and return_pass else "MISMATCH"
            ),
        })
    return results


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", default="2026-09-01")
    parser.add_argument("--end", default="2026-11-20")
    parser.add_argument("--record-fixture", action="store_true")
    parser.add_argument("--mcp", action="store_true")
    args = parser.parse_args()
    start, end = date.fromisoformat(args.start), date.fromisoformat(args.end)
    paths = ProjectPaths.discover(ROOT)
    if paths.root != ROOT or not paths.runtime.resolve().is_relative_to(ROOT):
        raise SystemExit("Reconciliation requires the normal project-owned runtime")
    state, objects = StateStore(paths.state_db, ROOT / "migrations"), ObjectStore(paths.objects)
    state.migrate()
    provider = TushareStructuredProvider(objects, state, project_root=ROOT)
    captures, results = {}, []
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    out = paths.runtime / "provider-probes" / "tushare" / stamp
    out.mkdir(parents=True, exist_ok=False)
    report: dict[str, Any] = {
        "schema_version": "tushare-period-unit-observation-v1",
        "observed_at": datetime.now(UTC).isoformat(),
        "window": {"start": start.isoformat(), "end": end.isoformat()},
        "historical_contract_proven": False,
        "normalization_allowed_by_this_observation": False,
        "scope": "ONLY_RECORDED_SYMBOLS_AND_PERIODS; UPSTREAM_UNITS_NOT_EXPLICIT_IN_DOCS",
        "input_sha256": {
            path: hashlib.sha256((ROOT / path).read_bytes()).hexdigest() for path in (
                "src/astock/providers/tushare_structured.py",
                "src/astock/providers/tushare_mcp.py", "integrations/tushare/reconcile_periods.py",
            )
        },
        "results": results,
    }
    try:
        for symbol in ("600519.SH", "000001.SZ"):
            retrieved = {}
            for endpoint in ("daily", "weekly", "monthly"):
                request = TushareQueryRequest.model_validate({
                    "endpoint": endpoint,
                    "params": {"ts_code": symbol, "start_date": start.strftime("%Y%m%d"),
                               "end_date": end.strftime("%Y%m%d")},
                })
                result = provider.query(request)
                retrieved[endpoint] = result["rows"]
                captures[symbol + ":" + endpoint] = {
                    "source_snapshot_id": result["source_snapshot_id"],
                    "source_object_hash": result["source_object_hash"],
                    "request": result["request"],
                    "raw": json.loads(objects.get_bytes(result["source_object_hash"])),
                }
            for frequency in ("weekly", "monthly"):
                items = reconcile(retrieved["daily"], retrieved[frequency], frequency, start, end)
                results.append({
                    "symbol": symbol, "frequency": frequency, "period_count": len(items),
                    "status": "PASS" if len(items) >= 2 and all(
                        item["status"] == "MATCHED_SAMPLE" for item in items
                    ) else "MISMATCH_OR_INSUFFICIENT",
                    "periods": items,
                })
        if args.mcp:
            from astock.providers.tushare_mcp import probe_tushare_mcp

            try:
                report["mcp"] = probe_tushare_mcp(provider)
            except ProviderError as exc:
                report["mcp"] = {
                    "status": "UNAVAILABLE", "failure_class": exc.failure_class.value,
                    **{key: exc.details[key] for key in (
                        "status_code", "redirect_present", "redirect_same_origin",
                        "canonical_trailing_slash",
                    ) if key in exc.details},
                }
        if args.record_fixture:
            fixture = {**report, "captures": captures}
            (ROOT / "tests/fixtures/providers/tushare-period-units.json").write_text(
                json.dumps(fixture, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
    finally:
        provider.close()
        (out / "report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    print(json.dumps({
        "report": str(out / "report.json"),
        "results": [{key: item[key] for key in ("symbol", "frequency", "period_count", "status")}
                    for item in results],
        "mcp": report.get("mcp"),
    }), flush=True)
    return 0 if len(results) == 4 and all(item["status"] == "PASS" for item in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
