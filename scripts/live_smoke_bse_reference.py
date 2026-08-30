from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, model_validator

from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.providers.bse_official_reference import BseOfficialReferenceProvider
from astock.schemas.reference_data import Market

REPO_ROOT = Path(__file__).resolve().parents[1]
REQUEST_CONTRACT_PATH = REPO_ROOT / "configs" / "validation" / "bse_reference_smoke_request.json"


class BseReferenceSmokeRequest(BaseModel):
    schema_version: Literal["bse-reference-smoke-request-v1"]
    market: Market
    live: Literal[True]
    purpose: Literal["INSTRUMENT_MASTER"]
    read_only: Literal[True]
    broker_execution_allowed: Literal[False]

    @model_validator(mode="after")
    def validate_market(self) -> BseReferenceSmokeRequest:
        if self.market is not Market.BJSE:
            raise ValueError("BSE reference smoke supports BJSE only")
        return self


def _request_from_contract() -> BseReferenceSmokeRequest:
    return BseReferenceSmokeRequest.model_validate_json(
        REQUEST_CONTRACT_PATH.read_text(encoding="utf-8")
    )


def _content_sha256(payload: dict[str, object]) -> str:
    serialized = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return sha256(serialized).hexdigest()


def run_smoke(runtime: Path) -> dict[str, object]:
    request = _request_from_contract()
    runtime.mkdir(parents=True, exist_ok=True)
    objects = ObjectStore(runtime / "objects" / "sha256")
    state = StateStore(runtime / "state.sqlite", REPO_ROOT / "migrations")
    state.migrate()
    provider = BseOfficialReferenceProvider(
        objects,
        state,
        REPO_ROOT / "tests" / "fixtures" / "reference" / "bse",
    )

    started = datetime.now(UTC)
    payload, aggregate_snapshot = provider.fetch_master(request.market, live=request.live)
    finished = datetime.now(UTC)

    rows = payload.get("rows")
    total = payload.get("total")
    coverage_denominator = payload.get("coverage_denominator")
    page_count = payload.get("page_count")
    page_snapshot_ids = payload.get("page_snapshot_ids")
    if not isinstance(rows, list) or not rows:
        raise RuntimeError("BSE official provider returned no listed-stock rows")
    if not isinstance(total, int) or total != len(rows):
        raise RuntimeError("BSE official provider returned an inconsistent total")
    if coverage_denominator != total:
        raise RuntimeError("BSE official coverage denominator is inconsistent")
    if not isinstance(page_count, int) or page_count <= 0:
        raise RuntimeError("BSE official provider returned no pagination proof")
    if not isinstance(page_snapshot_ids, list) or len(page_snapshot_ids) != page_count:
        raise RuntimeError("BSE official raw-page snapshot lineage is incomplete")
    if payload.get("_astock_source") != "BSE_OFFICIAL_LIST":
        raise RuntimeError("BSE official provider response lacks official provenance")
    if payload.get("complete") is not True:
        raise RuntimeError("BSE official provider did not prove complete coverage")

    verified_page_snapshots: list[str] = []
    for snapshot_id in page_snapshot_ids:
        if not isinstance(snapshot_id, str):
            raise RuntimeError("BSE official page snapshot id is malformed")
        snapshot = state.get_snapshot(snapshot_id)
        if snapshot is None or not objects.verify(snapshot.object_sha256):
            raise RuntimeError("BSE official raw-page snapshot is unavailable or corrupt")
        verified_page_snapshots.append(snapshot_id)
    if not objects.verify(aggregate_snapshot.object_sha256):
        raise RuntimeError("BSE official aggregate snapshot is unavailable or corrupt")

    return {
        "schema_version": "bse-live-reference-smoke-v1",
        "started_at": started.isoformat(),
        "finished_at": finished.isoformat(),
        "duration_ms": round((finished - started).total_seconds() * 1000),
        "provider_id": provider.provider_id,
        "request": request.model_dump(mode="json"),
        "response_summary": {
            "content_sha256": _content_sha256(payload),
            "record_count": len(rows),
            "coverage_denominator": coverage_denominator,
            "page_count": page_count,
            "page_snapshot_ids": verified_page_snapshots,
            "aggregate_snapshot_id": aggregate_snapshot.snapshot_id,
            "aggregate_object_sha256": aggregate_snapshot.object_sha256,
        },
        "official_bse_provenance": True,
        "complete_coverage": True,
        "raw_page_lineage_verified": True,
        "read_only": True,
        "broker_execution_allowed": False,
        "passed": True,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Read-only production smoke for the official BSE reference adapter."
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--runtime",
        type=Path,
        help="Persistent smoke-only ObjectStore/StateStore directory.",
    )
    args = parser.parse_args()

    runtime = args.runtime or args.output.parent / "bse-reference-smoke-runtime"
    report = run_smoke(runtime)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
