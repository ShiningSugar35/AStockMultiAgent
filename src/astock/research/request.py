"""Research request contract construction."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pyarrow.parquet as pq
from pydantic import ValidationError

from astock.core.hashing import content_hash
from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.schemas import (
    DatasetReleaseManifest,
    InstrumentRecord,
    InstrumentType,
    Market,
    ReferenceDatasetKind,
    ResearchRequest,
    ResearchRequestModule,
)


_REQUEST_SCOPE_KEYS = tuple(
    market.value for market in Market if market is not Market.INDEX
) + ("ALL",)


@dataclass(frozen=True, slots=True)
class ResearchRequestExecution:
    request: ResearchRequest
    artifact_id: str
    object_sha256: str
    reused_existing: bool


class ResearchRequestService:
    def __init__(
        self,
        state: StateStore,
        objects: ObjectStore,
        reference_parquet_root: Path,
    ) -> None:
        self.state = state
        self.objects = objects
        self.reference_parquet_root = reference_parquet_root

    def create_request(
        self,
        company_or_name: str,
        *,
        requested_modules: list[ResearchRequestModule | str] | None = None,
    ) -> ResearchRequestExecution:
        input_value = (company_or_name or "").strip()
        if not input_value:
            raise ValueError("company input must not be empty")

        now = datetime.now(UTC)
        request = self._resolve_request(input_value, now, requested_modules)
        request_id = self._request_identity(request)
        artifact_id = f"ResearchRequest:{request_id}"
        existing_object_hash = self._artifact_object_hash(artifact_id)
        if existing_object_hash is not None:
            # Reuse exact same identity artifact if all previous writes are intact.
            return ResearchRequestExecution(
                request,
                artifact_id=artifact_id,
                object_sha256=existing_object_hash,
                reused_existing=True,
            )
        object_ref = self.objects.put_json(request.model_dump(mode="json"))
        self.state.register_artifact(
            artifact_id=artifact_id,
            artifact_type="ResearchRequest",
            schema_version=request.schema_version,
            object_hash=object_ref.sha256,
            input_hashes=[request_id],
        )
        return ResearchRequestExecution(
            request,
            artifact_id=artifact_id,
            object_sha256=object_ref.sha256,
            reused_existing=False,
        )

    @staticmethod
    def _request_identity(request: ResearchRequest) -> str:
        return content_hash(
            {
                "company": request.company,
                "ticker": request.ticker,
                "market": request.market,
                "requested_modules": [item.value for item in request.requested_modules],
            }
        )

    def _resolve_request(
        self,
        company_or_name: str,
        as_of: datetime,
        requested_modules: list[ResearchRequestModule | str] | None,
    ) -> ResearchRequest:
        resolved = self._resolve_company(company_or_name, as_of)
        kwargs = {
            "company": resolved["company"],
            "ticker": resolved["ticker"],
        }
        if requested_modules is None:
            return ResearchRequest(**kwargs)
        modules = []
        for module in requested_modules:
            modules.append(
                module if isinstance(module, ResearchRequestModule) else ResearchRequestModule(module)
            )
        kwargs["requested_modules"] = modules
        return ResearchRequest(**kwargs)

    def _resolve_company(self, company_or_name: str, as_of: datetime) -> dict[str, str]:
        if company_or_name.isdigit() and len(company_or_name) == 6:
            return self._resolve_by_ticker(company_or_name, as_of)
        return self._resolve_by_name(company_or_name, as_of)

    def _resolve_by_ticker(self, ticker: str, as_of: datetime) -> dict[str, str]:
        records = self._resolve_records(as_of, predicate=lambda item: item.symbol == ticker)
        if len(records) == 1:
            selected = records[0]
        elif not records:
            raise ValueError(f"unknown stock code: {ticker}")
        else:
            raise ValueError(f"stock code matches multiple markets: {ticker}")
        return {"company": selected.name, "ticker": selected.symbol}

    def _resolve_by_name(self, company_name: str, as_of: datetime) -> dict[str, str]:
        normalized = self._normalize_name(company_name)
        records = self._resolve_records(
            as_of, predicate=lambda item: self._normalize_name(item.name) == normalized
        )
        if len(records) == 1:
            selected = records[0]
        elif not records:
            raise ValueError(f"unknown company name: {company_name}")
        else:
            raise ValueError(f"company name is ambiguous: {company_name}")
        return {"company": selected.name, "ticker": selected.symbol}

    def _resolve_records(
        self,
        as_of: datetime,
        predicate,
    ) -> list[InstrumentRecord]:
        matched: dict[str, InstrumentRecord] = {}
        for scope in _REQUEST_SCOPE_KEYS:
            row = self.state.get_market_reference_release(
                ReferenceDatasetKind.INSTRUMENT_MASTER.value,
                scope,
                as_of=as_of,
            )
            if row is None:
                continue
            manifest = self._read_manifest(row)
            for record in self._read_instruments(manifest):
                if (
                    record.instrument_type is InstrumentType.STOCK
                    and predicate(record)
                ):
                    matched.setdefault(record.instrument_id, record)
        return sorted(matched.values(), key=lambda item: item.instrument_id)

    def _read_manifest(self, row: dict[str, object]) -> DatasetReleaseManifest:
        object_hash = str(row["manifest_object_hash"])
        try:
            manifest = DatasetReleaseManifest.model_validate_json(
                self.objects.get_bytes(object_hash)
            )
        except (OSError, ValidationError, ValueError) as exc:
            raise ValueError("instrument release manifest is unavailable") from exc
        if manifest.dataset_kind is not ReferenceDatasetKind.INSTRUMENT_MASTER:
            raise ValueError("instrument release dataset kind mismatch")
        if manifest.release_id != str(row["release_id"]):
            raise ValueError("instrument release identity mismatch")
        if manifest.scope_key != str(row["scope_key"]):
            raise ValueError("instrument release scope mismatch")
        return manifest

    def _read_instruments(
        self, manifest: DatasetReleaseManifest
    ) -> list[InstrumentRecord]:
        records: list[InstrumentRecord] = []
        for descriptor in manifest.canonical_files:
            path = (self.reference_parquet_root / descriptor.path).resolve()
            if not path.is_relative_to(self.reference_parquet_root):
                raise ValueError("instrument path escapes reference parquet root")
            try:
                raw_rows = (
                    pq.ParquetFile(path)
                    .read(columns=["record_json"])
                    .column(0)
                    .to_pylist()
                )
            except (OSError, ValueError) as exc:
                raise ValueError("instrument master records are unavailable") from exc
            for row in raw_rows:
                try:
                    records.append(InstrumentRecord.model_validate_json(row))
                except ValidationError as exc:
                    raise ValueError("instrument record is malformed") from exc
        return records

    def _artifact_object_hash(self, artifact_id: str) -> str | None:
        with self.state.connect() as connection:
            row = connection.execute(
                "SELECT object_hash FROM artifact_registry WHERE artifact_id=?",
                (artifact_id,),
            ).fetchone()
        if row is None:
            return None
        object_hash = str(row["object_hash"])
        return object_hash if self.objects.verify(object_hash) else None

    @staticmethod
    def _normalize_name(value: str) -> str:
        return value.strip().casefold()
