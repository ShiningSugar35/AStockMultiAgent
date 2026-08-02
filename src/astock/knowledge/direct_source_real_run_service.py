# ruff: noqa: E501
"""Prepare and verify a private-safe direct-source run without database writes.

The direct run reads current-source contracts directly.  Historical protection
is limited to one fixed semantic-run row fingerprint; no reviewed child data is
read or used to interpret a Skill.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import NoReturn, cast

from astock.core.atomic import atomic_create_bytes
from astock.core.errors import DataQualityError, FailureClass
from astock.core.hashing import canonical_json_bytes, sha256_bytes
from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.schemas.direct_source_distillation import DirectRunInitManifest
from astock.schemas.direct_source_real_run import (
    DirectSourceRealRunAuditedArticleBoundaryMarker,
    DirectSourceRealRunAuditedEmptyUnit,
    DirectSourceRealRunDocxBlockContract,
    DirectSourceRealRunImportPlan,
    DirectSourceRealRunVisualAdjudication,
)

_SOURCE_SEMANTIC_RUN_ID = (
    "semantic-run:e0605aa01c8ddc0dffe69a81d88389d546e52b39aa89d19fc8a2012835e06ea5"
)
_DOCX_PARSER_VERSION = "wordprocessingml-ecma376+rules-v1"
_DOCX_LOCATOR_SCHEME = "ooxml-body-paragraph-1based"
_DOCX_TITLE_ANCHOR = r"^\d{4}-\d{2}-\d{2}_"
_EMPTY_TEXT_OBJECT_HASH = sha256_bytes(b"")
_LEGACY_SOURCE_SEMANTIC_STAGE = "DEEPSEEK_PACKET_READY"
_PREPARE_VERSION = "direct-source-real-run-prepare-v1"
_LEGACY_FREEZE_VERSION = "direct-source-legacy-freeze-v3"
_IMPORT_PLAN_VERSION = "direct-source-real-run-import-plan-v1"
_PIPELINE_VERSION = "direct-pipeline-v1"
_PDF_BATCHES: tuple[tuple[str, int, int], ...] = (
    ("b01", 1, 2),
    ("b02", 3, 4),
    ("b03", 5, 7),
    ("b04", 8, 24),
    ("b05", 25, 33),
    ("b06", 34, 39),
    ("b07.1", 40, 56),
    ("b07.2", 57, 70),
    ("b08.1", 71, 87),
    ("b08.2", 88, 109),
    ("b09", 110, 122),
    ("b10", 123, 135),
    ("b11", 136, 145),
    ("b12", 146, 169),
    ("b13", 170, 177),
    ("b14", 178, 188),
    ("b15", 189, 193),
    ("b16", 194, 205),
    ("b17", 206, 218),
    ("b18", 219, 222),
    ("b19", 223, 249),
)
_ACCEPTED_PDF_BATCH_IDS = tuple(batch_id for batch_id, _, end in _PDF_BATCHES if end <= 169)


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _reject_non_finite(value: str) -> None:
    raise ValueError(f"non-finite JSON number is forbidden: {value}")


def _strict_json_bytes(data: bytes) -> object:
    try:
        return json.loads(
            data.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_non_finite,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise DataQualityError(
            f"invalid strict UTF-8 JSON: {exc}",
            failure_class=FailureClass.DATA_QUALITY,
            details={"failure_code": "INVALID_STRICT_JSON"},
        ) from exc


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
    except OSError as exc:
        raise DataQualityError(
            "current source file cannot be read",
            failure_class=FailureClass.DATA_QUALITY,
            details={"failure_code": "CURRENT_SOURCE_FILE_UNREADABLE"},
        ) from exc
    return digest.hexdigest()


def _canonical_hash(value: object) -> str:
    return sha256_bytes(canonical_json_bytes(value))


def _json_digest(value: str | None) -> str | None:
    return sha256_bytes(value.encode("utf-8")) if value is not None else None


class DirectSourceRealRunService:
    """Build deterministic release files from current immutable source snapshots."""

    def __init__(
        self,
        state: StateStore,
        object_store: ObjectStore,
        project_root: Path,
    ) -> None:
        self.state = state
        self.object_store = object_store
        self.project_root = project_root.resolve()

    @staticmethod
    def load_json_file(path: Path) -> object:
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise DataQualityError(
                "cannot read direct-source real-run file",
                failure_class=FailureClass.DATA_QUALITY,
                details={"failure_code": "REAL_RUN_FILE_UNREADABLE"},
            ) from exc
        return _strict_json_bytes(data)

    @contextmanager
    def _read_only_connection(self) -> Iterator[sqlite3.Connection]:
        database = self.state.path.as_posix()
        connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA query_only=ON")
            connection.execute("BEGIN")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _reject(code: str, message: str, **details: object) -> NoReturn:
        raise DataQualityError(
            message,
            failure_class=FailureClass.DATA_QUALITY,
            details={"failure_code": code, **details},
        )

    def _require_object(self, object_hash: str, *, role: str) -> None:
        if not self.object_store.verify(object_hash):
            self._reject(
                "IMMUTABLE_OBJECT_MISSING",
                "required immutable object is absent or corrupt",
                role=role,
                object_hash=object_hash,
            )

    def _source_state(self, state_payload: Mapping[str, object], kind: str) -> Mapping[str, object]:
        hashes = state_payload.get("source_hashes")
        if not isinstance(hashes, Mapping):
            self._reject("STATE_SOURCE_HASHES_MISSING", "source hash state is missing")
        source = hashes.get(kind.lower())
        if not isinstance(source, Mapping):
            self._reject("STATE_SOURCE_MISSING", "frozen source state is missing", source_kind=kind)
        return cast(Mapping[str, object], source)

    def _state_count(self, source: Mapping[str, object], key: str) -> int:
        value = source.get(key)
        if not isinstance(value, int) or value < 1:
            self._reject(
                "STATE_COVERAGE_CONTRACT_DRIFT",
                "frozen source coverage count is invalid",
                field=key,
            )
        return value

    def _frozen_docx_contract(
        self,
        state_payload: Mapping[str, object],
        contract_file: Path,
    ) -> list[dict[str, object]]:
        """Load only pre-frozen article boundaries, never infer them from headings."""

        payload = self.load_json_file(contract_file)
        if not isinstance(payload, Mapping):
            self._reject("DOCX_CONTRACT_NOT_OBJECT", "frozen DOCX contract must be a JSON object")
        fingerprints = payload.get("source_fingerprints")
        if not isinstance(fingerprints, Mapping):
            self._reject(
                "DOCX_CONTRACT_FINGERPRINT_MISSING",
                "frozen DOCX contract has no source fingerprints",
            )
        state_docx = self._source_state(state_payload, "DOCX")
        contract_docx = fingerprints.get("docx")
        if not isinstance(contract_docx, Mapping):
            self._reject("DOCX_CONTRACT_SOURCE_MISSING", "frozen DOCX contract has no DOCX source")
        for state_key, contract_keys in (
            ("sha256", ("sha256",)),
            ("body_paragraph_count", ("body_paragraph_count", "paragraphs")),
            ("article_count", ("article_count", "sections")),
        ):
            state_value = state_docx.get(state_key)
            contract_value = next(
                (contract_docx.get(key) for key in contract_keys if key in contract_docx), None
            )
            if state_value != contract_value:
                self._reject(
                    "DOCX_CONTRACT_SOURCE_DRIFT",
                    "frozen DOCX boundary contract does not match the direct checkpoint",
                    field=state_key,
                )
        if contract_docx.get("paragraph_locator_scheme") not in (
            _DOCX_LOCATOR_SCHEME,
            "direct-ooxml-body-w:p-1based",
        ):
            self._reject(
                "DOCX_CONTRACT_LOCATOR_SCHEME_INVALID",
                "frozen DOCX contract uses an unsupported paragraph locator scheme",
            )
        anchor_rule = contract_docx.get("title_anchor_rule")
        if (
            anchor_rule is not None
            and anchor_rule != "trim(concatenated descendant w:t) matches ^\\d{4}-\\d{2}-\\d{2}_"
        ):
            self._reject(
                "DOCX_CONTRACT_TITLE_RULE_INVALID",
                "frozen DOCX contract title-anchor rule is not the approved rule",
            )
        raw_markers = contract_docx.get("audited_article_boundary_markers")
        if not isinstance(raw_markers, list):
            self._reject(
                "DOCX_AUDITED_BOUNDARY_MARKER_MISSING",
                "frozen DOCX contract omits its exact audited boundary marker",
            )
        try:
            markers = [
                DirectSourceRealRunAuditedArticleBoundaryMarker.model_validate(item)
                for item in raw_markers
            ]
        except ValueError as exc:
            self._reject(
                "DOCX_AUDITED_BOUNDARY_MARKER_INVALID",
                "frozen DOCX audited boundary marker is invalid",
                reason=str(exc),
            )
        if len(markers) != 1:
            self._reject(
                "DOCX_AUDITED_BOUNDARY_MARKER_COUNT_DRIFT",
                "exactly one frozen DOCX audited boundary marker is permitted",
                observed_count=len(markers),
            )
        marker = markers[0]
        expected_marker = {
            "article_index": 34,
            "block_index": 726,
            "title_hash": "2c95c8cccf30d56224f751a3e3c04388294dac09e2666a7520c0801fd1a5c0b6",
            "title_anchor_matches": True,
            "is_heading": False,
            "style_id": None,
            "heading_level": None,
            "metadata_object_hash": "4c45060cffadf8128a47334dd9060b17dac12a41223ab0b7fcc5ca717f0cbda0",
            "parser_version": _DOCX_PARSER_VERSION,
        }
        if marker.model_dump(mode="json") != expected_marker:
            self._reject(
                "DOCX_AUDITED_BOUNDARY_MARKER_DRIFT",
                "frozen DOCX audited boundary marker differs from the approved exact fact",
            )
        raw_batches = payload.get("docx_batches")
        if not isinstance(raw_batches, list) or len(raw_batches) != 123:
            self._reject(
                "DOCX_CONTRACT_ARTICLE_COUNT_DRIFT",
                "frozen DOCX contract must contain exactly 123 article ranges",
            )
        expected_start = 1
        articles: list[dict[str, object]] = []
        for ordinal, raw in enumerate(raw_batches, start=1):
            if not isinstance(raw, Mapping):
                self._reject(
                    "DOCX_CONTRACT_ARTICLE_INVALID", "frozen DOCX article range is invalid"
                )
            article_index = raw.get("article_index")
            start = raw.get("start_paragraph")
            end = raw.get("end_paragraph")
            count = raw.get("paragraph_count")
            title = raw.get("title")
            if (
                article_index != ordinal
                or not isinstance(start, int)
                or not isinstance(end, int)
                or not isinstance(count, int)
                or not isinstance(title, str)
                or not title.strip()
                or raw.get("paragraph_locator_scheme") != _DOCX_LOCATOR_SCHEME
                or start != expected_start
                or end < start
                or count != end - start + 1
            ):
                self._reject(
                    "DOCX_CONTRACT_BOUNDARY_INVALID",
                    "frozen DOCX article ranges are missing, overlapping, or inconsistent",
                    article_ordinal=ordinal,
                )
            expected_start = end + 1
            articles.append(
                {
                    "ordinal": ordinal,
                    "start": start,
                    "end": end,
                    "title_hash": sha256_bytes(title.strip().encode("utf-8")),
                }
            )
        if expected_start != 2033:
            self._reject(
                "DOCX_CONTRACT_BOUNDARY_COVERAGE_DRIFT",
                "frozen DOCX article ranges do not cover exactly 2032 paragraphs",
            )
        adjacent_ranges = [
            (articles[index]["ordinal"], articles[index]["start"], articles[index]["end"])
            for index in (32, 33, 34)
        ]
        if adjacent_ranges != [(33, 717, 725), (34, 726, 728), (35, 729, 741)]:
            self._reject(
                "DOCX_AUDITED_BOUNDARY_ADJACENCY_DRIFT",
                "article 34 and its frozen article 33/35 neighbors have drifted",
            )
        if articles[33]["title_hash"] != marker.title_hash:
            self._reject(
                "DOCX_AUDITED_BOUNDARY_TITLE_HASH_DRIFT",
                "article 34 title hash differs from its exact audited marker",
            )
        articles[33]["audited_boundary_marker"] = marker.model_dump(mode="json")
        return articles

    def _frozen_visual_contract(
        self,
        state_payload: Mapping[str, object],
        contract_file: Path,
    ) -> Mapping[str, object]:
        payload = self.load_json_file(contract_file)
        if not isinstance(payload, Mapping):
            self._reject(
                "VISUAL_CONTRACT_NOT_OBJECT", "frozen visual contract must be a JSON object"
            )
        fingerprints = payload.get("source_fingerprints")
        if not isinstance(fingerprints, Mapping):
            self._reject(
                "VISUAL_CONTRACT_FINGERPRINT_MISSING",
                "frozen visual contract has no source fingerprints",
            )
        pdf = fingerprints.get("pdf")
        visual = fingerprints.get("visual_reuse")
        if not isinstance(pdf, Mapping) or not isinstance(visual, Mapping):
            self._reject(
                "VISUAL_CONTRACT_SOURCE_MISSING",
                "frozen visual contract has no PDF and visual lineage bindings",
            )
        state_pdf = self._source_state(state_payload, "PDF")
        if (
            state_pdf.get("sha256") != pdf.get("sha256")
            or state_pdf.get("sha256") != visual.get("pdf_sha256")
            or state_pdf.get("page_count") != pdf.get("pages")
            or state_pdf.get("page_count") != visual.get("page_count")
        ):
            self._reject(
                "VISUAL_CONTRACT_SOURCE_DRIFT",
                "frozen visual lineage does not bind the current direct PDF source exactly",
            )
        required_ids = ("book_manifest_id", "book_report_id", "visual_run_id", "semantic_run_id")
        if any(not isinstance(visual.get(key), str) or not visual.get(key) for key in required_ids):
            self._reject(
                "VISUAL_CONTRACT_IDENTITY_MISSING",
                "frozen visual contract omits an immutable lineage identity",
            )
        if visual.get("semantic_run_id") == _SOURCE_SEMANTIC_RUN_ID:
            self._reject(
                "VISUAL_CONTRACT_SEMANTIC_RUN_DRIFT",
                "current visual contract must bind its independent current semantic run",
            )
        if (
            visual.get("image_page_count") != 56
            or visual.get("placement_count") != 73
            or visual.get("semantic_ref_count") != 71
            or visual.get("coverage_status") != "COMPLETE"
            or visual.get("quality_status") != "REVIEW_REQUIRED"
        ):
            self._reject(
                "VISUAL_CONTRACT_COVERAGE_DRIFT",
                "frozen visual contract differs from accepted current 56/73/71 coverage",
            )
        raw_adjudications = visual.get("adjudications")
        if not isinstance(raw_adjudications, list):
            self._reject(
                "VISUAL_CONTRACT_ADJUDICATION_MISSING",
                "frozen visual contract omits exact non-semantic adjudications",
            )
        try:
            adjudications = [
                DirectSourceRealRunVisualAdjudication.model_validate(item).model_dump(mode="json")
                for item in raw_adjudications
            ]
        except ValueError as exc:
            self._reject(
                "VISUAL_CONTRACT_ADJUDICATION_INVALID",
                "frozen visual adjudication is not strict or complete",
                reason=str(exc),
            )
        if (
            len(adjudications) != 2
            or {(item["page_number"], item["placement_ordinal"]) for item in adjudications}
            != {(78, 36), (97, 46)}
            or any(item["action"] != "NON_SEMANTIC_EXCLUDE" for item in adjudications)
        ):
            self._reject(
                "VISUAL_CONTRACT_ADJUDICATION_DRIFT",
                "only the two approved exact non-semantic adjudications are permitted",
            )
        visual = {**visual, "adjudications": adjudications}
        return cast(Mapping[str, object], visual)

    def _frozen_pdf_empty_units(
        self,
        state_payload: Mapping[str, object],
        contract_file: Path,
    ) -> dict[int, str]:
        payload = self.load_json_file(contract_file)
        if not isinstance(payload, Mapping):
            self._reject("PDF_EMPTY_CONTRACT_NOT_OBJECT", "frozen contract must be an object")
        fingerprints = payload.get("source_fingerprints")
        pdf = fingerprints.get("pdf") if isinstance(fingerprints, Mapping) else None
        if not isinstance(pdf, Mapping):
            self._reject("PDF_EMPTY_CONTRACT_MISSING", "frozen PDF contract is missing")
        state_pdf = self._source_state(state_payload, "PDF")
        if pdf.get("sha256") != state_pdf.get("sha256"):
            self._reject(
                "PDF_EMPTY_CONTRACT_SOURCE_DRIFT",
                "audited empty PDF units are not bound to the current source hash",
            )
        raw_units = pdf.get("audited_empty_units")
        if not isinstance(raw_units, list):
            self._reject(
                "PDF_EMPTY_CONTRACT_MISSING", "frozen PDF audited empty units are missing"
            )
        try:
            units = [DirectSourceRealRunAuditedEmptyUnit.model_validate(item) for item in raw_units]
        except ValueError as exc:
            self._reject(
                "PDF_EMPTY_CONTRACT_INVALID",
                "frozen PDF audited empty unit is invalid",
                reason=str(exc),
            )
        if len(units) != 1 or units[0].source_kind != "PDF" or units[0].unit_index != 2:
            self._reject(
                "PDF_EMPTY_CONTRACT_DRIFT",
                "current PDF contract must contain only audited empty page 2",
            )
        return {unit.unit_index: unit.object_hash for unit in units}

    def _current_source_hashes(self, state_payload: Mapping[str, object]) -> dict[str, str]:
        result: dict[str, str] = {}
        for kind in ("PDF", "DOCX"):
            source = self._source_state(state_payload, kind)
            expected = source.get("sha256")
            location = source.get("path")
            if not isinstance(expected, str) or len(expected) != 64:
                self._reject(
                    "STATE_SOURCE_HASH_INVALID", "frozen source hash is invalid", source_kind=kind
                )
            if not isinstance(location, str) or not location:
                self._reject(
                    "STATE_SOURCE_PATH_MISSING", "frozen source path is missing", source_kind=kind
                )
            relative = Path(location)
            if relative.is_absolute():
                self._reject(
                    "STATE_SOURCE_PATH_POLICY",
                    "source state must use a relative path",
                    source_kind=kind,
                )
            observed = _sha256_file(self.project_root / relative)
            if observed != expected:
                self._reject(
                    "CURRENT_SOURCE_HASH_DRIFT",
                    "current source bytes differ from the frozen source hash",
                    source_kind=kind,
                    expected_hash=expected,
                    observed_hash=observed,
                )
            if not self.object_store.verify(expected):
                self._reject(
                    "CURRENT_SOURCE_OBJECT_MISSING",
                    "current source hash is not an immutable registered object",
                    source_kind=kind,
                    source_file_hash=expected,
                )
            result[kind] = expected
        return result

    @staticmethod
    def _single_source_manifest(
        connection: sqlite3.Connection,
        source_hash: str,
        source_kind: str,
    ) -> sqlite3.Row:
        rows = connection.execute(
            "SELECT manifest_id,document_id,snapshot_id,file_sha256,source_page_count "
            "FROM book_source_manifest WHERE file_sha256=? "
            "ORDER BY created_at DESC,manifest_id DESC",
            (source_hash,),
        ).fetchall()
        if not rows:
            DirectSourceRealRunService._reject(
                "CURRENT_SOURCE_REGISTRATION_MISSING",
                "current source object has not completed immutable source registration",
                source_kind=source_kind,
                source_file_hash=source_hash,
            )
        return rows[0]

    def _pdf_source(
        self,
        connection: sqlite3.Connection,
        source_hash: str,
        expected_pages: int,
        frozen_visual: Mapping[str, object],
        audited_empty_units: Mapping[int, str],
    ) -> tuple[dict[int, dict[str, object]], dict[int, list[str]], dict[str, int]]:
        manifest = self._single_source_manifest(connection, source_hash, "PDF")
        if int(manifest["source_page_count"]) != expected_pages:
            self._reject(
                "CURRENT_PDF_PAGE_COUNT_DRIFT",
                "current PDF registration page count differs from frozen state",
                expected_page_count=expected_pages,
                observed_page_count=int(manifest["source_page_count"]),
            )
        pages = connection.execute(
            "SELECT page_number,text_object_hash,text_char_count FROM document_page "
            "WHERE document_id=? AND snapshot_id=? ORDER BY page_number",
            (manifest["document_id"], manifest["snapshot_id"]),
        ).fetchall()
        if [int(row["page_number"]) for row in pages] != list(range(1, expected_pages + 1)):
            self._reject(
                "CURRENT_PDF_PAGE_LOCATORS_INVALID",
                "current PDF pages are incomplete or non-contiguous",
            )
        page_map: dict[int, dict[str, object]] = {}
        observed_empty_units: dict[int, str] = {}
        for row in pages:
            object_hash = str(row["text_object_hash"])
            length = int(row["text_char_count"])
            page_number = int(row["page_number"])
            if length < 0:
                self._reject(
                    "CURRENT_PDF_NEGATIVE_TEXT_LENGTH",
                    "current PDF page text length cannot be negative",
                    source_kind="PDF",
                    unit_index=page_number,
                )
            if length == 0:
                expected_empty_hash = audited_empty_units.get(page_number)
                if expected_empty_hash is None or object_hash != expected_empty_hash:
                    self._reject(
                        "CURRENT_SOURCE_FRAGMENT_EMPTY",
                        "current PDF contains an unaudited or drifted empty page",
                        source_kind="PDF",
                        unit_index=page_number,
                    )
                observed_empty_units[page_number] = object_hash
            self._require_object(object_hash, role="current_pdf_page_text")
            page_map[page_number] = {"hash": object_hash, "length": length}
        if observed_empty_units != dict(audited_empty_units):
            self._reject(
                "CURRENT_PDF_AUDITED_EMPTY_DRIFT",
                "current PDF audited empty coverage differs from the frozen contract",
                expected_empty_pages=sorted(audited_empty_units),
                observed_empty_pages=sorted(observed_empty_units),
            )

        visual = self._current_pdf_visuals(
            connection, str(manifest["manifest_id"]), expected_pages, frozen_visual
        )
        return page_map, visual[0], visual[1]

    def _current_pdf_visuals(
        self,
        connection: sqlite3.Connection,
        manifest_id: str,
        expected_pages: int,
        frozen_visual: Mapping[str, object],
    ) -> tuple[dict[int, list[str]], dict[str, int]]:
        expected_run_id = frozen_visual.get("visual_run_id")
        expected_manifest_id = frozen_visual.get("book_manifest_id")
        expected_report_id = frozen_visual.get("book_report_id")
        if (
            not isinstance(expected_run_id, str)
            or not expected_run_id
            or not isinstance(expected_manifest_id, str)
            or not expected_manifest_id
            or not isinstance(expected_report_id, str)
            or not expected_report_id
        ):
            self._reject(
                "CURRENT_VISUAL_CONTRACT_MISSING",
                "frozen source contract has no audited visual run identity",
            )
        if manifest_id != expected_manifest_id:
            self._reject(
                "CURRENT_VISUAL_MANIFEST_ID_DRIFT",
                "current PDF source manifest differs from the frozen visual lineage binding",
            )
        visual_run = connection.execute(
            "SELECT run_id,source_page_count,image_page_count,image_placement_count,"
            "processed_placement_count,semantic_run_id,raw_object_hash,coverage_report_hash,"
            "run_object_hash "
            "FROM book_visual_run WHERE run_id=? AND source_manifest_id=? AND stage='AUDITED'",
            (expected_run_id, manifest_id),
        ).fetchone()
        if visual_run is None:
            self._reject(
                "CURRENT_VISUAL_RELINEAGE_MISSING",
                "current PDF has no audited frozen visual relineage run",
            )
        if str(visual_run["semantic_run_id"]) != str(frozen_visual.get("semantic_run_id")):
            self._reject(
                "CURRENT_VISUAL_SEMANTIC_RUN_DRIFT",
                "current visual run semantic identity differs from the frozen binding",
            )
        coverage = connection.execute(
            "SELECT report_id,coverage_status,quality_status,report_object_hash FROM book_visual_coverage_report "
            "WHERE run_id=?",
            (visual_run["run_id"],),
        ).fetchone()
        if (
            coverage is None
            or str(coverage["report_id"]) != expected_report_id
            or str(coverage["coverage_status"]) != str(frozen_visual.get("coverage_status"))
            or str(coverage["quality_status"]) != str(frozen_visual.get("quality_status"))
            or str(visual_run["coverage_report_hash"]) != str(coverage["report_object_hash"])
        ):
            self._reject(
                "CURRENT_VISUAL_COVERAGE_UNAUDITED",
                "current visual report identity, coverage, quality, or hash has drifted",
            )
        for object_hash, role in (
            (str(visual_run["raw_object_hash"]), "current_pdf_visual_raw"),
            (str(visual_run["run_object_hash"]), "current_pdf_visual_run"),
            (str(coverage["report_object_hash"]), "current_pdf_visual_coverage"),
        ):
            self._require_object(object_hash, role=role)
        if int(visual_run["source_page_count"]) != expected_pages or int(
            visual_run["processed_placement_count"]
        ) != int(visual_run["image_placement_count"]):
            self._reject(
                "CURRENT_VISUAL_RELINEAGE_INCOMPLETE",
                "current PDF visual relineage is not complete",
                expected_page_count=expected_pages,
                source_page_count=int(visual_run["source_page_count"]),
                image_placement_count=int(visual_run["image_placement_count"]),
                processed_placement_count=int(visual_run["processed_placement_count"]),
            )
        expected_image_pages = frozen_visual.get("image_page_count")
        expected_placements = frozen_visual.get("placement_count")
        if (
            not isinstance(expected_image_pages, int)
            or not isinstance(expected_placements, int)
            or int(visual_run["image_page_count"]) != expected_image_pages
            or int(visual_run["image_placement_count"]) != expected_placements
        ):
            self._reject(
                "CURRENT_VISUAL_COVERAGE_DRIFT",
                "current visual counts differ from the frozen audited coverage contract",
            )
        evidence_rows = connection.execute(
            "SELECT evidence_id,page_number,placement_index,placement_ordinal,bbox_json,"
            "image_object_hash,duplicate_of_evidence_id,evidence_object_hash "
            "FROM book_image_evidence WHERE run_id=? ORDER BY placement_ordinal,evidence_id",
            (visual_run["run_id"],),
        ).fetchall()
        if len(evidence_rows) != int(visual_run["image_placement_count"]):
            self._reject(
                "CURRENT_VISUAL_EVIDENCE_COUNT_DRIFT",
                "current visual evidence rows do not match its completed run",
            )
        if [int(row["placement_ordinal"]) for row in evidence_rows] != list(
            range(1, int(visual_run["image_placement_count"]) + 1)
        ):
            self._reject(
                "CURRENT_VISUAL_PLACEMENT_ORDER_INVALID",
                "current visual placement ordinals are incomplete or non-contiguous",
            )
        evidence_by_id = {str(row["evidence_id"]): row for row in evidence_rows}
        raw_by_page: dict[int, list[str]] = {}
        for row in evidence_rows:
            page_number = int(row["page_number"])
            if page_number < 1 or page_number > expected_pages:
                self._reject(
                    "CURRENT_VISUAL_LOCATOR_INVALID", "current visual page locator is out of range"
                )
            current = row
            visited: set[str] = set()
            while current["image_object_hash"] is None:
                current_id = str(current["evidence_id"])
                if current_id in visited:
                    self._reject(
                        "CURRENT_VISUAL_DUPLICATE_CYCLE",
                        "current visual evidence duplicate chain cycles",
                    )
                visited.add(current_id)
                duplicate = current["duplicate_of_evidence_id"]
                if duplicate is None or str(duplicate) not in evidence_by_id:
                    self._reject(
                        "CURRENT_VISUAL_OBJECT_MISSING",
                        "current visual evidence has no immutable image object",
                    )
                current = evidence_by_id[str(duplicate)]
            self._require_object(str(current["image_object_hash"]), role="current_pdf_visual")
            self._require_object(
                str(row["evidence_object_hash"]), role="current_pdf_visual_evidence"
            )
            raw_by_page.setdefault(page_number, []).append(str(row["evidence_id"]))
        if len(raw_by_page) != int(visual_run["image_page_count"]):
            self._reject(
                "CURRENT_VISUAL_IMAGE_PAGE_COUNT_DRIFT",
                "current visual evidence page coverage differs from its audited run",
            )
        self._verify_visual_derivative_objects(
            connection,
            str(visual_run["run_id"]),
            tuple(evidence_by_id),
            role_prefix="current_pdf_visual",
        )
        chart_rows = connection.execute(
            "SELECT chart_unit_id,evidence_id,chart_type,decorative_excluded,"
            "review_reason_codes_json,unit_object_hash FROM book_chart_unit "
            "WHERE run_id=? ORDER BY evidence_id,chart_unit_id",
            (visual_run["run_id"],),
        ).fetchall()
        ocr_rows = connection.execute(
            "SELECT evidence_id,status,result_object_hash FROM book_image_ocr "
            "WHERE run_id=? ORDER BY evidence_id",
            (visual_run["run_id"],),
        ).fetchall()
        chart_by_evidence = {str(row["evidence_id"]): row for row in chart_rows}
        ocr_by_evidence = {str(row["evidence_id"]): row for row in ocr_rows}
        if set(chart_by_evidence) != set(evidence_by_id) or set(ocr_by_evidence) != set(
            evidence_by_id
        ):
            self._reject(
                "CURRENT_VISUAL_DERIVATIVE_BINDING_DRIFT",
                "current chart/OCR rows do not cover every exact placement",
            )
        semantic_rows = connection.execute(
            "SELECT r.ref_id,r.chart_unit_id,r.semantic_run_id,r.ref_object_hash,c.evidence_id "
            "FROM book_visual_semantic_ref r JOIN book_chart_unit c "
            "ON c.chart_unit_id=r.chart_unit_id WHERE r.run_id=? "
            "ORDER BY c.evidence_id,r.ref_id",
            (visual_run["run_id"],),
        ).fetchall()
        expected_ref_count = frozen_visual.get("semantic_ref_count")
        if not isinstance(expected_ref_count, int) or len(semantic_rows) != expected_ref_count:
            self._reject(
                "CURRENT_VISUAL_SEMANTIC_REF_COUNT_DRIFT",
                "current visual semantic-ref count differs from the frozen binding",
                observed_count=len(semantic_rows),
            )
        semantic_by_evidence: dict[str, list[sqlite3.Row]] = {}
        for row in semantic_rows:
            if str(row["semantic_run_id"]) != str(frozen_visual.get("semantic_run_id")):
                self._reject(
                    "CURRENT_VISUAL_SEMANTIC_REF_RUN_DRIFT",
                    "a current visual semantic ref binds a different semantic run",
                )
            self._require_object(
                str(row["ref_object_hash"]), role="current_pdf_visual_semantic_ref"
            )
            semantic_by_evidence.setdefault(str(row["evidence_id"]), []).append(row)

        raw_adjudications = cast(Sequence[Mapping[str, object]], frozen_visual["adjudications"])
        adjudicated_ids: set[str] = set()
        for adjudication in raw_adjudications:
            evidence_id = str(adjudication["evidence_id"])
            evidence = evidence_by_id.get(evidence_id)
            chart = chart_by_evidence.get(evidence_id)
            ocr = ocr_by_evidence.get(evidence_id)
            if evidence is None or chart is None or ocr is None:
                self._reject(
                    "CURRENT_VISUAL_ADJUDICATION_TARGET_MISSING",
                    "an exact visual adjudication target is absent",
                    evidence_id=evidence_id,
                )
            bbox = _strict_json_bytes(str(evidence["bbox_json"]).encode("utf-8"))
            reasons = _strict_json_bytes(
                str(chart["review_reason_codes_json"]).encode("utf-8")
            )
            refs = semantic_by_evidence.get(evidence_id, [])
            expected_ref_id = adjudication.get("semantic_ref_id")
            expected_ref_hash = adjudication.get("semantic_ref_object_hash")
            ref_matches = (
                not refs
                if expected_ref_id is None
                else len(refs) == 1
                and str(refs[0]["ref_id"]) == expected_ref_id
                and str(refs[0]["ref_object_hash"]) == expected_ref_hash
            )
            if (
                int(evidence["page_number"]) != adjudication["page_number"]
                or int(evidence["placement_index"]) != adjudication["placement_index"]
                or int(evidence["placement_ordinal"]) != adjudication["placement_ordinal"]
                or bbox != adjudication["bbox"]
                or str(evidence["image_object_hash"]) != adjudication["image_object_hash"]
                or str(evidence["evidence_object_hash"])
                != adjudication["evidence_object_hash"]
                or str(chart["chart_unit_id"]) != adjudication["chart_unit_id"]
                or str(chart["chart_type"]) != adjudication["original_chart_type"]
                or bool(chart["decorative_excluded"])
                != adjudication["original_decorative_excluded"]
                or reasons != adjudication["original_review_reason_codes"]
                or str(ocr["status"]) != adjudication["ocr_status"]
                or str(ocr["result_object_hash"])
                != adjudication["ocr_result_object_hash"]
                or not ref_matches
            ):
                self._reject(
                    "CURRENT_VISUAL_ADJUDICATION_FACT_DRIFT",
                    "raw visual facts no longer match an exact non-semantic adjudication",
                    evidence_id=evidence_id,
                )
            self._require_object(
                str(ocr["result_object_hash"]),
                role="current_pdf_visual_adjudication_ocr_result",
            )
            adjudicated_ids.add(evidence_id)

        review_ids: set[str] = set()
        for evidence_id, chart in chart_by_evidence.items():
            reasons = _strict_json_bytes(
                str(chart["review_reason_codes_json"]).encode("utf-8")
            )
            if not isinstance(reasons, list) or any(not isinstance(item, str) for item in reasons):
                self._reject(
                    "CURRENT_VISUAL_REVIEW_REASON_INVALID",
                    "current visual review reasons are not a string list",
                    evidence_id=evidence_id,
                )
            if reasons or str(chart["chart_type"]) == "UNKNOWN":
                review_ids.add(evidence_id)
        review_ids.update(
            evidence_id
            for evidence_id, ocr in ocr_by_evidence.items()
            if str(ocr["status"]) != "SUCCESS"
        )
        residual_review_ids = sorted(review_ids - adjudicated_ids)
        if residual_review_ids:
            self._reject(
                "CURRENT_VISUAL_UNADJUDICATED_REVIEW",
                "current visual run retains review-required placements outside exact adjudications",
                evidence_ids=residual_review_ids,
            )

        by_page: dict[int, list[str]] = {}
        for row in semantic_rows:
            evidence_id = str(row["evidence_id"])
            if evidence_id in adjudicated_ids:
                continue
            page_number = int(evidence_by_id[evidence_id]["page_number"])
            by_page.setdefault(page_number, []).append(evidence_id)
        return by_page, {
            "image_page_count": int(visual_run["image_page_count"]),
            "image_placement_count": int(visual_run["image_placement_count"]),
            "semantic_ref_count": len(semantic_rows),
            "emitted_visual_count": sum(len(ids) for ids in by_page.values()),
            "residual_review_count": 0,
        }

    def _verify_visual_derivative_objects(
        self,
        connection: sqlite3.Connection,
        run_id: str,
        evidence_ids: Sequence[str],
        *,
        role_prefix: str,
        require_objects: bool = True,
    ) -> dict[str, object]:
        """Verify and fingerprint audited attempt, OCR, chart, and layout rows."""

        expected_ids = set(evidence_ids)
        attempts = connection.execute(
            "SELECT * "
            "FROM book_image_evidence_attempt WHERE evidence_id IN ("
            + ",".join("?" for _ in evidence_ids)
            + ") ORDER BY evidence_id,attempt_ordinal,attempt_id",
            tuple(evidence_ids),
        ).fetchall()
        if {str(row["evidence_id"]) for row in attempts} != expected_ids:
            self._reject(
                "VISUAL_ATTEMPT_COVERAGE_DRIFT",
                "every audited placement must retain an immutable extraction attempt",
            )
        for row in attempts:
            if require_objects:
                self._require_object(
                    str(row["attempt_object_hash"]), role=f"{role_prefix}_attempt"
                )
            if row["image_object_hash"] is not None:
                if require_objects:
                    self._require_object(
                        str(row["image_object_hash"]), role=f"{role_prefix}_attempt_image"
                    )

        ocr = connection.execute(
            "SELECT * FROM book_image_ocr WHERE run_id=? ORDER BY evidence_id",
            (run_id,),
        ).fetchall()
        if {str(row["evidence_id"]) for row in ocr} != expected_ids:
            self._reject(
                "VISUAL_OCR_COVERAGE_DRIFT",
                "every audited placement must retain one OCR result",
            )
        for row in ocr:
            if require_objects:
                self._require_object(str(row["result_object_hash"]), role=f"{role_prefix}_ocr")
            if row["text_object_hash"] is not None:
                if require_objects:
                    self._require_object(
                        str(row["text_object_hash"]), role=f"{role_prefix}_ocr_text"
                    )

        charts = connection.execute(
            "SELECT * FROM book_chart_unit WHERE run_id=? ORDER BY evidence_id,chart_unit_id",
            (run_id,),
        ).fetchall()
        if {str(row["evidence_id"]) for row in charts} != expected_ids:
            self._reject(
                "VISUAL_CHART_COVERAGE_DRIFT",
                "every audited placement must retain one chart classification object",
            )
        for row in charts:
            if require_objects:
                self._require_object(str(row["unit_object_hash"]), role=f"{role_prefix}_chart")

        atoms = connection.execute(
            "SELECT * FROM book_layout_atom WHERE run_id=? ORDER BY global_ordinal,atom_id",
            (run_id,),
        ).fetchall()
        image_atoms = [row for row in atoms if str(row["atom_kind"]) == "IMAGE_EVIDENCE"]
        if (
            len(image_atoms) != len(evidence_ids)
            or {str(row["evidence_id"]) for row in image_atoms} != expected_ids
        ):
            self._reject(
                "VISUAL_LAYOUT_COVERAGE_DRIFT",
                "every audited placement must retain one IMAGE_EVIDENCE layout atom",
            )
        for row in atoms:
            if require_objects:
                self._require_object(str(row["atom_object_hash"]), role=f"{role_prefix}_layout_atom")
            if row["text_object_hash"] is not None:
                if require_objects:
                    self._require_object(
                        str(row["text_object_hash"]), role=f"{role_prefix}_layout_text"
                    )
        return {
            "book_image_evidence_attempt_rows": self._fingerprint_complete_rows(attempts),
            "book_image_ocr_rows": self._fingerprint_complete_rows(ocr),
            "book_chart_unit_rows": self._fingerprint_complete_rows(charts),
            "book_layout_atom_rows": self._fingerprint_complete_rows(atoms),
        }

    def _docx_source(
        self,
        connection: sqlite3.Connection,
        source_hash: str,
        expected_paragraphs: int,
        expected_articles: int,
        frozen_articles: Sequence[Mapping[str, object]],
    ) -> tuple[list[dict[str, object]], dict[str, object]]:
        manifest = self._single_source_manifest(connection, source_hash, "DOCX")
        reports = connection.execute(
            "SELECT parser_version,coverage_status,report_object_hash "
            "FROM private_docx_parse_report WHERE manifest_id=? ORDER BY parser_version,docx_parse_report_id",
            (manifest["manifest_id"],),
        ).fetchall()
        if len(reports) != 1:
            self._reject(
                "CURRENT_DOCX_PARSE_MULTIPLE",
                "current DOCX must bind exactly one frozen parse report and parser version",
                observed_report_count=len(reports),
            )
        report = reports[0]
        if (
            str(report["parser_version"]) != _DOCX_PARSER_VERSION
            or str(report["coverage_status"]) != "COMPLETE"
        ):
            self._reject(
                "CURRENT_DOCX_PARSE_INCOMPLETE",
                "current DOCX does not have the approved complete parse report",
            )
        self._require_object(str(report["report_object_hash"]), role="current_docx_parse_report")
        parsers = connection.execute(
            "SELECT DISTINCT parser_version FROM document_block WHERE snapshot_id=? ORDER BY parser_version",
            (manifest["snapshot_id"],),
        ).fetchall()
        if [str(row["parser_version"]) for row in parsers] != [_DOCX_PARSER_VERSION]:
            self._reject(
                "CURRENT_DOCX_BLOCK_PARSER_MULTIPLE",
                "current DOCX paragraphs are not bound to exactly the approved parser version",
            )
        rows = connection.execute(
            "SELECT block_index,text_object_hash,text_char_count,parser_version,block_json,"
            "json_extract(block_json,'$.is_heading') AS is_heading "
            "FROM document_block WHERE snapshot_id=? AND parser_version=? ORDER BY block_index",
            (manifest["snapshot_id"], _DOCX_PARSER_VERSION),
        ).fetchall()
        indexes = [int(row["block_index"]) for row in rows]
        if indexes != list(range(1, expected_paragraphs + 1)):
            self._reject(
                "CURRENT_DOCX_PARAGRAPH_LOCATORS_INVALID",
                "current DOCX paragraphs are incomplete or non-contiguous",
            )
        block_contract_rows: list[dict[str, object]] = []
        empty_indexes: list[int] = []
        for row in rows:
            index = int(row["block_index"])
            length = int(row["text_char_count"])
            object_hash = str(row["text_object_hash"])
            if length < 0:
                self._reject(
                    "CURRENT_DOCX_NEGATIVE_TEXT_LENGTH",
                    "current DOCX paragraph has a negative text length",
                    source_kind="DOCX",
                    unit_index=index,
                )
            if length == 0:
                if object_hash != _EMPTY_TEXT_OBJECT_HASH:
                    self._reject(
                        "CURRENT_DOCX_EMPTY_OBJECT_HASH_DRIFT",
                        "an empty DOCX paragraph must reference the SHA-256 of empty UTF-8 bytes",
                        unit_index=index,
                        observed_hash=object_hash,
                    )
                empty_indexes.append(index)
            self._require_object(object_hash, role="current_docx_paragraph_text")
            block_contract_rows.append(
                {
                    "block_index": index,
                    "text_object_hash": object_hash,
                    "text_char_count": length,
                    "locator": {
                        "source_kind": "DOCX",
                        "unit_index": index,
                        "start_offset": 0,
                        "end_offset": length,
                    },
                    "is_empty": length == 0,
                    "is_heading": int(row["is_heading"] or 0) == 1,
                }
            )
        if len(frozen_articles) != expected_articles:
            self._reject(
                "DOCX_CONTRACT_ARTICLE_COUNT_DRIFT",
                "frozen DOCX contract does not match its expected article count",
            )
        by_index = {int(row["block_index"]): row for row in rows}
        articles: list[dict[str, object]] = []
        for frozen in frozen_articles:
            ordinal = cast(int, frozen["ordinal"])
            start = cast(int, frozen["start"])
            end = cast(int, frozen["end"])
            title_row = by_index.get(start)
            if title_row is None:
                self._reject(
                    "CURRENT_DOCX_ARTICLE_BOUNDARY_DRIFT",
                    "a frozen DOCX article start is missing",
                    article_ordinal=ordinal,
                )
            try:
                title = (
                    self.object_store.get_bytes(str(title_row["text_object_hash"]))
                    .decode("utf-8")
                    .strip()
                )
            except (UnicodeDecodeError, OSError) as exc:
                self._reject(
                    "CURRENT_DOCX_TITLE_OBJECT_INVALID",
                    "a frozen DOCX article title object cannot be decoded",
                    article_ordinal=ordinal,
                )
                raise AssertionError("unreachable") from exc
            if (
                not re.match(_DOCX_TITLE_ANCHOR, title)
                or sha256_bytes(title.encode("utf-8")) != frozen["title_hash"]
            ):
                self._reject(
                    "CURRENT_DOCX_TITLE_ANCHOR_DRIFT",
                    "a frozen DOCX article title no longer matches its title anchor and hash",
                    article_ordinal=ordinal,
                )
            marker = frozen.get("audited_boundary_marker")
            is_heading = int(title_row["is_heading"] or 0) == 1
            if marker is not None:
                if not isinstance(marker, Mapping):
                    self._reject(
                        "DOCX_AUDITED_BOUNDARY_MARKER_INVALID",
                        "frozen article boundary marker is not an object",
                    )
                self._validate_docx_audited_boundary_marker(
                    title_row,
                    title,
                    ordinal,
                    cast(Mapping[str, object], marker),
                )
            elif not is_heading:
                self._reject(
                    "CURRENT_DOCX_ARTICLE_BOUNDARY_DRIFT",
                    "a frozen DOCX article start is no longer a title boundary",
                    article_ordinal=ordinal,
                )
            articles.append(
                {
                    "ordinal": ordinal,
                    "start": start,
                    "end": end,
                    "units": [
                        {
                            "index": index,
                            "hash": str(by_index[index]["text_object_hash"]),
                            "length": int(by_index[index]["text_char_count"]),
                        }
                        for index in range(start, end + 1)
                    ],
                    "leading_context": [
                        {
                            "index": index,
                            "hash": str(by_index[index]["text_object_hash"]),
                            "length": int(by_index[index]["text_char_count"]),
                        }
                        for index in range(max(1, start - 2), start)
                    ],
                    "trailing_context": [
                        {
                            "index": index,
                            "hash": str(by_index[index]["text_object_hash"]),
                            "length": int(by_index[index]["text_char_count"]),
                        }
                        for index in range(end + 1, min(expected_paragraphs, end + 2) + 1)
                    ],
                }
            )
        if len(empty_indexes) != 123:
            self._reject(
                "CURRENT_DOCX_EMPTY_PARAGRAPH_COUNT_DRIFT",
                "current DOCX must retain exactly 123 legal empty body paragraphs",
                observed_empty_paragraph_count=len(empty_indexes),
            )
        block_contract = DirectSourceRealRunDocxBlockContract.model_validate(
            {
                "parser_version": _DOCX_PARSER_VERSION,
                "paragraph_locator_scheme": _DOCX_LOCATOR_SCHEME,
                "paragraph_count": expected_paragraphs,
                "article_count": expected_articles,
                "empty_paragraph_count": len(empty_indexes),
                "zero_length_representation": {
                    "source_kind": "DOCX",
                    "start_offset": 0,
                    "end_offset": 0,
                    "object_hash": _EMPTY_TEXT_OBJECT_HASH,
                },
                "block_rows": {
                    "count": len(block_contract_rows),
                    "sha256": _canonical_hash(block_contract_rows),
                },
                "empty_block_indexes": {
                    "count": len(empty_indexes),
                    "sha256": _canonical_hash(empty_indexes),
                },
                "article_boundaries": {
                    "count": len(frozen_articles),
                    "sha256": _canonical_hash(list(frozen_articles)),
                },
            }
        ).model_dump(mode="json")
        return articles, block_contract

    def _validate_docx_audited_boundary_marker(
        self,
        title_row: sqlite3.Row,
        title: str,
        ordinal: int,
        marker: Mapping[str, object],
    ) -> None:
        """Validate the sole parser-faithful non-heading article boundary."""

        block_payload = _strict_json_bytes(str(title_row["block_json"]).encode("utf-8"))
        if not isinstance(block_payload, Mapping):
            self._reject(
                "CURRENT_DOCX_AUDITED_BOUNDARY_BLOCK_INVALID",
                "audited boundary block metadata is not an object",
            )
        metadata_hash = block_payload.get("metadata_object_sha256")
        if not isinstance(metadata_hash, str):
            self._reject(
                "CURRENT_DOCX_AUDITED_BOUNDARY_METADATA_MISSING",
                "audited boundary block has no immutable metadata object",
            )
        self._require_object(metadata_hash, role="current_docx_audited_boundary_metadata")
        metadata_payload = _strict_json_bytes(self.object_store.get_bytes(metadata_hash))
        if not isinstance(metadata_payload, Mapping):
            self._reject(
                "CURRENT_DOCX_AUDITED_BOUNDARY_METADATA_INVALID",
                "audited boundary metadata object is not an object",
            )
        observed = {
            "article_index": ordinal,
            "block_index": int(title_row["block_index"]),
            "title_hash": sha256_bytes(title.encode("utf-8")),
            "title_anchor_matches": re.match(_DOCX_TITLE_ANCHOR, title) is not None,
            "is_heading": int(title_row["is_heading"] or 0) == 1,
            "style_id": metadata_payload.get("style_id"),
            "heading_level": metadata_payload.get("heading_level"),
            "metadata_object_hash": metadata_hash,
            "parser_version": str(title_row["parser_version"]),
        }
        if (
            observed != dict(marker)
            or block_payload.get("parser_version") != marker["parser_version"]
            or block_payload.get("paragraph_index") != marker["block_index"]
            or metadata_payload.get("paragraph_index") != marker["block_index"]
            or metadata_payload.get("block_kind") != "PARAGRAPH"
        ):
            self._reject(
                "CURRENT_DOCX_AUDITED_BOUNDARY_FACT_DRIFT",
                "current DOCX block no longer matches the exact audited boundary marker",
                article_ordinal=ordinal,
            )

    @staticmethod
    def _fragment(source_kind: str, unit: Mapping[str, object]) -> dict[str, object]:
        index = cast(int, unit["index"])
        length = cast(int, unit["length"])
        prefix = "pdf-page" if source_kind == "PDF" else "docx-paragraph"
        return {
            "fragment_id": f"{prefix}-{index:04d}",
            "object_hash": str(unit["hash"]),
            "locator": {
                "source_kind": source_kind,
                "unit_index": index,
                "start_offset": 0,
                "end_offset": length,
            },
        }

    @staticmethod
    def _non_empty_fragments(
        source_kind: str, units: Sequence[Mapping[str, object]]
    ) -> list[dict[str, object]]:
        return [
            DirectSourceRealRunService._fragment(source_kind, unit)
            for unit in units
            if cast(int, unit["length"]) > 0
        ]

    def _build_init_manifest(
        self,
        source_hashes: Mapping[str, str],
        page_map: Mapping[int, Mapping[str, object]],
        page_visuals: Mapping[int, Sequence[str]],
        articles: Sequence[Mapping[str, object]],
    ) -> dict[str, object]:
        batches: list[dict[str, object]] = []
        for ordinal, (batch_id, start, end) in enumerate(_PDF_BATCHES, start=1):
            current_units = [
                {"index": page, **page_map[page]} for page in range(start, end + 1)
            ]
            before_units = [
                {"index": page, **page_map[page]} for page in range(max(1, start - 2), start)
            ]
            after_units = [
                {"index": page, **page_map[page]}
                for page in range(end + 1, min(249, end + 2) + 1)
            ]
            batches.append(
                {
                    "batch_id": batch_id,
                    "source_id": "direct-pdf",
                    "chapter_unit_id": f"pdf-pages-{start:03d}-{end:03d}",
                    "ordinal": ordinal,
                    "source_unit_start": start,
                    "source_unit_end": end,
                    "audited_empty_units": [
                        {
                            "object_hash": str(unit["hash"]),
                            "locator": {
                                "source_kind": "PDF",
                                "unit_index": cast(int, unit["index"]),
                                "start_offset": 0,
                                "end_offset": 0,
                            },
                        }
                        for unit in current_units
                        if cast(int, unit["length"]) == 0
                    ],
                    "current_fragments": self._non_empty_fragments("PDF", current_units),
                    "context_before": self._non_empty_fragments("PDF", before_units),
                    "context_after": self._non_empty_fragments("PDF", after_units),
                    "visual_evidence_ids": [
                        evidence_id
                        for page in range(start, end + 1)
                        for evidence_id in page_visuals.get(page, ())
                    ],
                }
            )
        for article in articles:
            ordinal = cast(int, article["ordinal"])
            units = cast(Sequence[Mapping[str, object]], article["units"])
            leading_context = cast(Sequence[Mapping[str, object]], article["leading_context"])
            trailing_context = cast(Sequence[Mapping[str, object]], article["trailing_context"])
            batches.append(
                {
                    "batch_id": f"docx-{ordinal:03d}",
                    "source_id": "direct-docx",
                    "chapter_unit_id": f"docx-article-{ordinal:03d}",
                    "ordinal": len(_PDF_BATCHES) + ordinal,
                    "source_unit_start": cast(int, article["start"]),
                    "source_unit_end": cast(int, article["end"]),
                    "audited_empty_units": [
                        {
                            "object_hash": str(unit["hash"]),
                            "locator": {
                                "source_kind": "DOCX",
                                "unit_index": cast(int, unit["index"]),
                                "start_offset": 0,
                                "end_offset": 0,
                            },
                        }
                        for unit in units
                        if cast(int, unit["length"]) == 0
                    ],
                    "current_fragments": self._non_empty_fragments("DOCX", units),
                    "context_before": self._non_empty_fragments("DOCX", leading_context),
                    "context_after": self._non_empty_fragments("DOCX", trailing_context),
                    "visual_evidence_ids": [],
                }
            )
        init_core: dict[str, object] = {
            "schema_version": "direct-source-run-init-v1",
            "pipeline_version": _PIPELINE_VERSION,
            "sources": [
                {
                    "source_id": "direct-pdf",
                    "source_kind": "PDF",
                    "source_file_hash": source_hashes["PDF"],
                },
                {
                    "source_id": "direct-docx",
                    "source_kind": "DOCX",
                    "source_file_hash": source_hashes["DOCX"],
                },
            ],
            "batches": batches,
            "formal_committee_weight_allowed": False,
        }
        run_id = "direct-source-real:" + _canonical_hash(init_core)
        manifest = {"run_id": run_id, **init_core}
        return cast(
            dict[str, object],
            DirectRunInitManifest.model_validate(manifest).model_dump(mode="json"),
        )

    def _legacy_freeze(
        self,
        connection: sqlite3.Connection,
    ) -> dict[str, object]:
        """Freeze only the immutable top-level source semantic-run identity."""

        semantic = connection.execute(
            "SELECT run_id,author_source_id,input_manifest_hash,pipeline_version,stage,"
            "run_json,started_at,finished_at "
            "FROM knowledge_semantic_run WHERE run_id=?",
            (_SOURCE_SEMANTIC_RUN_ID,),
        ).fetchone()
        if semantic is None:
            self._reject(
                "LEGACY_SOURCE_SEMANTIC_RUN_MISSING", "approved source semantic run is missing"
            )
        if str(semantic["stage"]) != _LEGACY_SOURCE_SEMANTIC_STAGE:
            self._reject(
                "LEGACY_SOURCE_SEMANTIC_STAGE_DRIFT",
                "approved source semantic run differs from its exact frozen packet-ready stage",
                expected_stage=_LEGACY_SOURCE_SEMANTIC_STAGE,
                observed_stage=str(semantic["stage"]),
            )
        aggregate = {
            "run_id": str(semantic["run_id"]),
            "author_source_id": str(semantic["author_source_id"]),
            "input_manifest_hash": str(semantic["input_manifest_hash"]),
            "pipeline_version": str(semantic["pipeline_version"]),
            "stage": str(semantic["stage"]),
            "run_json_sha256": _json_digest(str(semantic["run_json"])),
            "started_at": str(semantic["started_at"]),
            "finished_at": (
                None if semantic["finished_at"] is None else str(semantic["finished_at"])
            ),
        }
        freeze: dict[str, object] = {
            "schema_version": _LEGACY_FREEZE_VERSION,
            "source_semantic_run": {
                "run_id": _SOURCE_SEMANTIC_RUN_ID,
                "status": _LEGACY_SOURCE_SEMANTIC_STAGE,
                "canonical_aggregate_fingerprint": _canonical_hash(aggregate),
            },
        }
        return {**freeze, "legacy_freeze_hash": _canonical_hash(freeze)}

    @staticmethod
    def _fingerprint_complete_rows(rows: Sequence[sqlite3.Row]) -> dict[str, object]:
        """Hash every selected column so valid-object substitutions still drift."""

        serializable = [
            {column: row[column] for column in row.keys()}
            for row in rows
        ]
        return {"count": len(serializable), "sha256": _canonical_hash(serializable)}

    def _collect_release(
        self,
        state_payload: Mapping[str, object],
        frozen_contract: Path,
    ) -> tuple[dict[str, object], dict[str, object], dict[str, object]]:
        source_hashes = self._current_source_hashes(state_payload)
        pdf_state = self._source_state(state_payload, "PDF")
        docx_state = self._source_state(state_payload, "DOCX")
        expected_pages = self._state_count(pdf_state, "page_count")
        expected_articles = self._state_count(docx_state, "article_count")
        expected_paragraphs = self._state_count(docx_state, "body_paragraph_count")
        if (expected_pages, expected_articles, expected_paragraphs) != (249, 123, 2032):
            self._reject(
                "STATE_COVERAGE_CONTRACT_DRIFT", "frozen source coverage contract is invalid"
            )
        frozen_articles = self._frozen_docx_contract(state_payload, frozen_contract)
        frozen_visual = self._frozen_visual_contract(state_payload, frozen_contract)
        frozen_pdf_empty_units = self._frozen_pdf_empty_units(state_payload, frozen_contract)
        with self._read_only_connection() as connection:
            page_map, page_visuals, current_visual_counts = self._pdf_source(
                connection,
                source_hashes["PDF"],
                expected_pages,
                frozen_visual,
                frozen_pdf_empty_units,
            )
            articles, current_docx_block_contract = self._docx_source(
                connection,
                source_hashes["DOCX"],
                expected_paragraphs,
                expected_articles,
                frozen_articles,
            )
            legacy = self._legacy_freeze(connection)
        legacy_core = {key: value for key, value in legacy.items() if key != "legacy_freeze_hash"}
        legacy_core["current_docx_block_contract"] = current_docx_block_contract
        legacy = {**legacy_core, "legacy_freeze_hash": _canonical_hash(legacy_core)}
        init_manifest = self._build_init_manifest(source_hashes, page_map, page_visuals, articles)
        if (
            current_visual_counts["image_placement_count"] != frozen_visual["placement_count"]
            or current_visual_counts["image_page_count"] != frozen_visual["image_page_count"]
            or current_visual_counts["semantic_ref_count"]
            != frozen_visual["semantic_ref_count"]
            or current_visual_counts["residual_review_count"] != 0
        ):
            self._reject(
                "CURRENT_VISUAL_COVERAGE_DRIFT",
                "current visual relineage does not preserve the accepted coverage count",
                image_page_count=current_visual_counts["image_page_count"],
                image_placement_count=current_visual_counts["image_placement_count"],
                semantic_ref_count=current_visual_counts["semantic_ref_count"],
                residual_review_count=current_visual_counts["residual_review_count"],
            )
        completed = state_payload.get("accepted_batch_ids")
        if not isinstance(completed, list) or tuple(completed) != _ACCEPTED_PDF_BATCH_IDS:
            self._reject(
                "PARTIAL_CHECKPOINT_DRIFT", "accepted PDF checkpoint does not match b01 through b12"
            )
        if state_payload.get("accepted_docx_sections") != []:
            self._reject(
                "PARTIAL_CHECKPOINT_DRIFT", "DOCX must remain wholly pending before direct reading"
            )
        init_batches = cast(Sequence[Mapping[str, object]], init_manifest["batches"])
        all_batch_ids = [str(batch["batch_id"]) for batch in init_batches]
        completed_ids = list(_ACCEPTED_PDF_BATCH_IDS)
        remaining_ids = [
            batch_id for batch_id in all_batch_ids if batch_id not in set(completed_ids)
        ]
        if (
            len(all_batch_ids) != 144
            or len(remaining_ids) != 130
            or len(remaining_ids[:7]) != 7
            or len(remaining_ids[-123:]) != 123
        ):
            self._reject(
                "BATCH_COVERAGE_CONTRACT_DRIFT",
                "direct run must contain 21 PDF and 123 DOCX batches",
            )
        init_hash = _canonical_hash(init_manifest)
        import_plan = DirectSourceRealRunImportPlan.model_validate(
            {
                "schema_version": _IMPORT_PLAN_VERSION,
                "run_id": init_manifest["run_id"],
                "init_manifest_sha256": init_hash,
                "total_batch_count": 144,
                "completed_only_batch_ids": completed_ids,
                "remaining_pdf_batch_ids": remaining_ids[:7],
                "remaining_docx_batch_ids": remaining_ids[7:],
                "completed_batch_count": len(completed_ids),
                "remaining_pdf_batch_count": 7,
                "remaining_docx_batch_count": 123,
                "formal_committee_weight_allowed": False,
            }
        ).model_dump(mode="json")
        return init_manifest, legacy, import_plan

    @staticmethod
    def _state_payload(path: Path) -> Mapping[str, object]:
        payload = DirectSourceRealRunService.load_json_file(path)
        if not isinstance(payload, Mapping):
            DirectSourceRealRunService._reject(
                "STATE_NOT_OBJECT", "direct source state must be a JSON object"
            )
        return cast(Mapping[str, object], payload)

    @staticmethod
    def _release_paths(output_dir: Path) -> dict[str, Path]:
        return {
            "init": output_dir / "direct-run-init.json",
            "legacy": output_dir / "legacy-freeze.json",
            "plan": output_dir / "import-plan.json",
        }

    def _publish_release(self, output_dir: Path, payloads: Mapping[str, object]) -> None:
        paths = self._release_paths(output_dir)
        encoded = {key: canonical_json_bytes(value) for key, value in payloads.items()}
        for key, path in paths.items():
            if path.exists() and path.read_bytes() != encoded[key]:
                self._reject(
                    "RELEASE_ARTIFACT_COLLISION",
                    "existing release artifact differs from the deterministic result",
                    artifact_name=path.name,
                )
        for key, path in paths.items():
            if atomic_create_bytes(path, encoded[key]):
                continue
            if path.read_bytes() != encoded[key]:
                self._reject(
                    "RELEASE_ARTIFACT_COLLISION",
                    "concurrent release artifact differs from the deterministic result",
                    artifact_name=path.name,
                )

    def prepare(
        self,
        state_file: Path,
        output_dir: Path,
        frozen_contract: Path,
    ) -> dict[str, object]:
        init_manifest, legacy, import_plan = self._collect_release(
            self._state_payload(state_file), frozen_contract
        )
        self._publish_release(
            output_dir,
            {"init": init_manifest, "legacy": legacy, "plan": import_plan},
        )
        return {
            "status": "PREPARED",
            "run_id": init_manifest["run_id"],
            "release_files": ["direct-run-init.json", "legacy-freeze.json", "import-plan.json"],
            "total_batch_count": 144,
            "completed_batch_count": 14,
            "remaining_pdf_batch_count": 7,
            "remaining_docx_batch_count": 123,
            "legacy_freeze_hash": legacy["legacy_freeze_hash"],
            "formal_committee_weight_allowed": False,
        }

    def verify(
        self, state_file: Path, output_dir: Path, frozen_contract: Path
    ) -> dict[str, object]:
        expected_init, expected_legacy, expected_plan = self._collect_release(
            self._state_payload(state_file), frozen_contract
        )
        paths = self._release_paths(output_dir)
        expected = {"init": expected_init, "legacy": expected_legacy, "plan": expected_plan}
        for key, path in paths.items():
            if not path.is_file():
                self._reject(
                    "RELEASE_ARTIFACT_MISSING",
                    "prepared release artifact is missing",
                    artifact_name=path.name,
                )
            observed = self.load_json_file(path)
            if observed != expected[key]:
                self._reject(
                    "RELEASE_ARTIFACT_DRIFT",
                    "prepared release artifact no longer matches immutable inputs",
                    artifact_name=path.name,
                )
        return {
            "status": "VERIFIED",
            "run_id": expected_init["run_id"],
            "total_batch_count": 144,
            "completed_batch_count": 14,
            "remaining_pdf_batch_count": 7,
            "remaining_docx_batch_count": 123,
            "legacy_freeze_hash": expected_legacy["legacy_freeze_hash"],
            "formal_committee_weight_allowed": False,
        }


__all__ = ["DirectSourceRealRunService"]
