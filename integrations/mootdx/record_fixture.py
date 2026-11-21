"""Create a compact recorded fixture from a successful owned Mootdx smoke."""
from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

from astock.core.object_store import ObjectStore

ROOT = Path(__file__).resolve().parents[2]
DESTINATION = ROOT / "tests/fixtures/providers/mootdx-fallback-success.json"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report")
    args = parser.parse_args()
    source = Path(args.report).resolve()
    if not source.is_relative_to(ROOT / "runtime/provider-probes/mootdx"):
        raise ValueError("Fixture source must be an owned Mootdx probe report")
    report = json.loads(source.read_bytes())
    results = report["results"]
    if {row["operation"] for row in results} != {"quote", "60m", "5m"}:
        raise ValueError("All three real operations are required")
    if not all(row["status"] == "PASS" and row["protocol"] == "MAC_COMPAT" for row in results):
        raise ValueError("Cannot record an unsuccessful probe as successful")
    objects = ObjectStore(source.parent / "objects")
    captures = {}
    semantic_check = {}
    for result in results:
        snapshot_id = result["snapshot_id"]
        raw = json.loads(objects.get_bytes(snapshot_id.split(":", 1)[1]))
        operation = result["operation"]
        captures[operation] = {
            "snapshot_id": snapshot_id, "request": raw["request"], "server": raw["server"],
            "frames": raw["wire_frames"], "raw_record_count": len(raw["rows"]),
            "protocol": raw["protocol"],
        }
        if operation == "quote":
            quote = raw["rows"][0]
            semantic_check["quote_volume_lots"] = quote["vol"]
            semantic_check["quote_amount_cny"] = quote["amount"]
            semantic_check["source_date"] = quote["source_date"]
        else:
            latest = max(row["datetime"][:10] for row in raw["rows"])
            rows = [row for row in raw["rows"] if row["datetime"].startswith(latest)]
            semantic_check[operation] = {
                "date": latest, "record_count": len(rows),
                "volume_shares": sum(row["vol"] for row in rows),
                "amount_cny": sum(row["amount"] for row in rows),
            }
    duration = (
        datetime.fromisoformat(report["finished_at"])
        - datetime.fromisoformat(report["started_at"])
    )
    fixture = {
        "status_code": 200, "content_type": "application/json",
        "latency_ms": round(duration.total_seconds() * 1000),
        "body": {
            "schema_version": "provider-self-probe-v1", "provider_id": "mootdx-fallback",
            "operation": "mootdx-intraday",
            "record_count": sum(row["record_count"] for row in results),
            "quality": True,
            "checked_capabilities": ["market.raw_60m", "market.raw_5m", "market.quote"],
        },
        "recorded_evidence": {
            "captured_at": report["finished_at"], "sdk": "mootdx", "sdk_version": "0.11.7",
            "transport_sdk": "tdxpy", "transport_sdk_version": "0.2.7",
            "transport_patch": "astock-mootdx-mac-v1",
            "source_report": source.relative_to(ROOT).as_posix(),
            "input_sha256": report["input_sha256"], "captures": captures,
            "semantic_check": semantic_check,
        },
    }
    DESTINATION.write_text(
        json.dumps(fixture, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(
        {"fixture": str(DESTINATION), "semantic_check": semantic_check}, ensure_ascii=False
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
