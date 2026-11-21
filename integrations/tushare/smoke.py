"""Low-frequency official Tushare HTTP entitlement smoke using the shared runtime quota."""
from __future__ import annotations

import argparse
import hashlib
import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from astock.core.errors import ProviderError
from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.providers.tushare_structured import TushareQueryRequest, TushareStructuredProvider
from astock.settings import ProjectPaths

ROOT = Path(__file__).resolve().parents[2]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mcp", action="store_true")
    parser.add_argument("--record-fixture", action="store_true")
    args = parser.parse_args()
    paths = ProjectPaths.discover(ROOT)
    if paths.root != ROOT or not paths.runtime.resolve().is_relative_to(ROOT):
        raise SystemExit("Tushare smoke requires the project-owned normal runtime")
    state = StateStore(paths.state_db, ROOT / "migrations")
    state.migrate()
    provider = TushareStructuredProvider(ObjectStore(paths.objects), state, project_root=ROOT)
    today = datetime.now(UTC).date()
    trading_day = today - timedelta(days=1)
    while trading_day.weekday() > 4:
        trading_day -= timedelta(days=1)
    last_month_end = today.replace(day=1) - timedelta(days=1)
    quarter_start = date(today.year, ((today.month - 1) // 3) * 3 + 1, 1)
    last_quarter_end = quarter_start - timedelta(days=1)
    common = {"ts_code": "600519.SH"}
    requests = [
        TushareQueryRequest(endpoint="stock_basic", params={**common, "list_status": "L"}),
        *[TushareQueryRequest(endpoint=endpoint, params={
            **common, "start_date": (trading_day - timedelta(days=31)).strftime("%Y%m%d"),
            "end_date": trading_day.strftime("%Y%m%d"),
        }) for endpoint in ("daily", "weekly", "monthly")],
        TushareQueryRequest(
            endpoint="daily_basic", params={**common, "trade_date": trading_day.strftime("%Y%m%d")}
        ),
        *[TushareQueryRequest(endpoint=endpoint, params={
            **common, "period": last_quarter_end.strftime("%Y%m%d"),
        }) for endpoint in ("income", "balancesheet", "cashflow")],
        TushareQueryRequest(endpoint="cn_gdp", params={
            "q": f"{last_quarter_end.year}Q{(last_quarter_end.month - 1) // 3 + 1}"
        }),
        TushareQueryRequest(endpoint="cn_cpi", params={"m": last_month_end.strftime("%Y%m")}),
    ]
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    out = paths.runtime / "provider-probes" / "tushare" / stamp
    out.mkdir(parents=True, exist_ok=False)
    report = {
        "schema_version": "tushare-live-smoke-v1", "observed_at": datetime.now(UTC).isoformat(),
        "input_sha256": {
            path: hashlib.sha256((ROOT / path).read_bytes()).hexdigest() for path in (
                "src/astock/providers/tushare_structured.py",
                "src/astock/providers/tushare_mcp.py",
                "src/astock/providers/supplemental.py",
                "configs/tushare_access.yaml", "configs/provider_registry.yaml",
            )
        },
        "results": [],
    }
    captures = []
    try:
        for request in requests:
            try:
                result = provider.query(request)
                captures.append(result)
                item = {
                    "endpoint": request.endpoint, "status": "PASS" if result["rows"] else "EMPTY",
                    "record_count": result["record_count"], "cache_status": result["cache_status"],
                    "source_snapshot_id": result["source_snapshot_id"],
                    "artifact_id": result["artifact_id"],
                    "permission_observation": result["permission_observation"],
                    "semantics": result["semantics"],
                }
            except ProviderError as exc:
                item = {
                    "endpoint": request.endpoint, "status": "UNAVAILABLE",
                    "failure_class": exc.failure_class.value,
                    "source_snapshot_id": exc.details.get("snapshot_id"),
                }
            report["results"].append(item)
            print(json.dumps(item, ensure_ascii=True), flush=True)
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
            capture = next(
                (item for item in captures if item["request"]["endpoint"] == "stock_basic"), None
            )
            if capture is None or not capture["rows"]:
                raise SystemExit("No accepted stock_basic capture to record")
            fixture = {
                "schema_version": "provider-self-probe-v1", "provider_id": "tushare-structured",
                "operation": "tushare-structured", "record_count": len(capture["rows"]),
                "quality": True, "checked_capabilities": ["tushare.structured.query"],
                "recorded_evidence": capture, "permissions_proven": ["stock_basic"],
                "other_endpoint_permissions_proven": False,
            }
            (ROOT / "tests/fixtures/providers/tushare-structured-success.json").write_text(
                json.dumps(fixture, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
    finally:
        provider.close()
        (out / "report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    print(json.dumps({"report": str(out / "report.json")}), flush=True)
    return 0 if all(item["status"] == "PASS" for item in report["results"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
