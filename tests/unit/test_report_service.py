from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from astock.core.object_store import ObjectStore
from astock.core.state import StateStore
from astock.reports.paths import ReportPathResolver
from astock.reports.preferences import PresentationPreferencesRepository
from astock.reports.service import ReportPublishError, ReportService
from astock.reports.validation import validate_docx
from astock.schemas.presentation import ResearchNarrativeBundle, ResponseTaskType
from astock.schemas.reports import (
    AssetManifest,
    AssetRights,
    CitationLevel,
    PdfConverterCapability,
    PreferenceKey,
    PrivacyLevel,
    ReportAsset,
    ReportDirectoryPolicy,
    ReportFormat,
    ReportManifest,
    ReportPublishResult,
    ReportRequest,
    ReportStatus,
)
from astock.settings import ProjectPaths

PROJECT_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def _isolated_report_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ASTOCK_REPORT_ROOT", str(tmp_path / "published"))


def _paths(tmp_path: Path) -> ProjectPaths:
    runtime = tmp_path / "runtime"
    return ProjectPaths(
        root=PROJECT_ROOT,
        runtime=runtime,
        objects=runtime / "objects" / "sha256",
        parquet=runtime / "data" / "parquet",
        manifests=runtime / "manifests",
        state_db=runtime / "state.sqlite",
    )


def _service(
    tmp_path: Path,
    *,
    path_resolver: ReportPathResolver | None = None,
) -> tuple[ReportService, StateStore, ObjectStore]:
    paths = _paths(tmp_path)
    paths.ensure_directories()
    state = StateStore(paths.state_db, PROJECT_ROOT / "migrations")
    state.migrate()
    objects = ObjectStore(paths.objects)
    return ReportService(paths, state, objects, path_resolver=path_resolver), state, objects


def _request(**overrides: object) -> ReportRequest:
    payload: dict[str, object] = {
        "request_id": "req-1",
        "title": "贵州茅台研究报告",
        "narrative": ResearchNarrativeBundle(
            subject="贵州茅台（600519）",
            task_type=ResponseTaskType.DEEP_RESEARCH,
            headline="当前估值缺少明显安全边际，更适合等待。",
            valuation_or_odds=["估值参考 1400 元"],
            reasons=["现金流稳定", "品牌优势仍在"],
            risks=["需求低于预期会压低盈利与估值"],
            change_conditions=["盈利持续超预期且估值回落"],
            data_as_of=datetime(2026, 8, 31, 15, 0, tzinfo=UTC),
            citations=["[S1]"],
        ),
    }
    payload.update(overrides)
    return ReportRequest.model_validate(payload)


def test_default_docx_publish_and_recovery(tmp_path: Path) -> None:
    service, _state, objects = _service(tmp_path)
    first = service.publish(_request())
    second = service.publish(_request())

    assert first.status is ReportStatus.PUBLISHED
    assert first.published_format is ReportFormat.DOCX
    assert first.safe_file_name and first.safe_file_name.endswith(".docx")
    assert first.public_reference is not None
    assert first.public_reference.file_name == first.safe_file_name
    assert second.recovered_existing is True
    assert second.output_sha256 == first.output_sha256
    assert first.manifest.template_version == "report-template-v1"
    assert first.manifest.privacy_level is PrivacyLevel.INTERNAL_PRIVATE
    assert first.manifest.citation_level is CitationLevel.SUMMARY
    assert first.manifest.manifest_object_hash is not None
    assert objects.verify(first.manifest.manifest_object_hash)

    output = Path(os.environ["ASTOCK_REPORT_ROOT"]) / first.safe_file_name
    validation = validate_docx(output)
    assert validation.valid is True
    assert validation.heading_count >= 4
    assert validation.paragraph_count >= 8


def test_docx_failure_falls_back_to_markdown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, _state, _objects = _service(tmp_path)

    def fail(*_args: object, **_kwargs: object) -> bytes:
        raise RuntimeError("renderer failed")

    monkeypatch.setattr(service, "_render_docx_bytes", fail)
    result = service.publish(_request(request_id="req-md"))
    assert result.status is ReportStatus.DEGRADED
    assert result.published_format is ReportFormat.MD
    assert result.degradation_reason == "DOCX_RENDER_FAILED:RuntimeError"
    assert result.safe_file_name and result.safe_file_name.endswith(".md")


def test_pdf_missing_never_claims_pdf_success(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, _state, _objects = _service(tmp_path)
    monkeypatch.setattr(service, "_probe_pdf_converter", lambda: None)
    result = service.publish(_request(request_id="req-pdf", preferred_format=ReportFormat.PDF))
    assert result.status is ReportStatus.DEGRADED
    assert result.published_format is ReportFormat.DOCX
    assert result.degradation_reason == "PDF_CONVERTER_UNAVAILABLE"


def test_pdf_converter_success_is_verified(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    service, _state, _objects = _service(tmp_path)
    capability = PdfConverterCapability(
        probe_id="fixture-probe",
        converter_id="fixture-converter",
        converter_version="fixture-probe-v1",
        probed_at=datetime(2026, 9, 1, 12, 0, tzinfo=UTC),
        probe_ok=True,
    )
    monkeypatch.setattr(service, "_probe_pdf_converter", lambda: capability)

    def fake_convert(_converter: str, _docx: Path, pdf: Path) -> str:
        pdf.write_bytes(b"%PDF-1.7\nfixture-pdf-content\n%%EOF")
        return "fixture-converter-v1"

    monkeypatch.setattr(service, "_convert_pdf", fake_convert)
    result = service.publish(_request(request_id="req-pdf-ok", preferred_format=ReportFormat.PDF))
    assert result.status is ReportStatus.PUBLISHED
    assert result.published_format is ReportFormat.PDF
    assert result.manifest.converter_version == "fixture-converter-v1"


def test_saved_default_format_applies_when_request_omits_format(tmp_path: Path) -> None:
    service, state, _objects = _service(tmp_path)
    PresentationPreferencesRepository(state).set(
        PreferenceKey.DEFAULT_REPORT_FORMAT,
        ReportFormat.MD,
    )
    result = service.publish(_request(request_id="req-pref-format"))
    assert result.published_format is ReportFormat.MD


def test_saved_privacy_and_citation_preferences_apply_when_request_omits_them(
    tmp_path: Path,
) -> None:
    service, state, _objects = _service(tmp_path)
    repository = PresentationPreferencesRepository(state)
    repository.set(PreferenceKey.PRIVACY_DEFAULT, PrivacyLevel.CONFIDENTIAL)
    repository.set(PreferenceKey.CITATION_LEVEL, CitationLevel.NONE)

    result = service.publish(_request(request_id="req-pref-policy"))
    assert result.manifest.privacy_level is PrivacyLevel.CONFIDENTIAL
    assert result.manifest.citation_level is CitationLevel.NONE


def test_preference_base_override_delete_and_reset_survive_repository_reopen(
    tmp_path: Path,
) -> None:
    _service_obj, state, _objects = _service(tmp_path)
    first = PresentationPreferencesRepository(state)
    first.set(PreferenceKey.DEFAULT_REPORT_FORMAT, ReportFormat.MD)
    first.override(PreferenceKey.DEFAULT_REPORT_FORMAT, ReportFormat.DOCX)

    second = PresentationPreferencesRepository(state)
    assert second.get().default_format is ReportFormat.DOCX
    assert second.delete(PreferenceKey.DEFAULT_REPORT_FORMAT).default_format is ReportFormat.MD
    assert second.reset(PreferenceKey.DEFAULT_REPORT_FORMAT).default_format is ReportFormat.DOCX

    second.override(PreferenceKey.DEFAULT_REPORT_FORMAT, ReportFormat.MD)
    assert PresentationPreferencesRepository(state).get().default_format is ReportFormat.MD
    assert second.delete(PreferenceKey.DEFAULT_REPORT_FORMAT).default_format is ReportFormat.DOCX


def test_presentation_preference_migration_matches_key_layer_contract(tmp_path: Path) -> None:
    _service_obj, state, _objects = _service(tmp_path)
    with state.connect() as connection:
        columns = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(presentation_preference)").fetchall()
        }
    assert columns == {"key", "base_value_json", "override_value_json", "updated_at"}


def test_output_name_traversal_is_sanitized(tmp_path: Path) -> None:
    service, _state, _objects = _service(tmp_path)
    result = service.publish(_request(request_id="req-safe", output_name_hint="../../私密/报告"))
    assert result.safe_file_name is not None
    assert ".." not in result.safe_file_name
    assert "/" not in result.safe_file_name
    assert "\\" not in result.safe_file_name


def test_registered_artifact_is_verified(tmp_path: Path) -> None:
    service, state, objects = _service(tmp_path)
    stored = objects.put_bytes(b"frozen research")
    state.register_artifact(
        artifact_id="research:test",
        artifact_type="TEST",
        schema_version="v1",
        object_hash=stored.sha256,
        input_hashes=[],
    )
    result = service.publish(
        _request(request_id="req-artifact", input_artifact_ids=["research:test"])
    )
    assert stored.sha256 in result.manifest.input_artifact_hashes


def test_unknown_or_corrupt_artifact_fails_closed(tmp_path: Path) -> None:
    service, _state, _objects = _service(tmp_path)
    with pytest.raises(ReportPublishError, match="registered report input is unavailable"):
        service.publish(_request(request_id="req-bad", input_artifact_ids=["missing"]))


def test_unapproved_asset_is_recorded_but_not_embedded(tmp_path: Path) -> None:
    service, _state, _objects = _service(tmp_path)
    image = tmp_path / "private.png"
    image.write_bytes(b"not-an-image")
    result = service.publish(
        _request(
            request_id="req-asset",
            include_assets=True,
            assets=AssetManifest(
                assets=[
                    ReportAsset(
                        asset_id="private-asset",
                        local_path=str(image),
                        rights=AssetRights.UNKNOWN,
                    )
                ]
            ),
        )
    )
    assert "private-asset" in result.manifest.excluded_asset_ids
    assert result.manifest.assets.assets[0].object_hash is None


def test_fair_use_asset_is_not_treated_as_embedding_permission(tmp_path: Path) -> None:
    service, _state, _objects = _service(tmp_path)
    image = tmp_path / "fair-use.png"
    image.write_bytes(b"not-read-when-excluded")
    result = service.publish(
        _request(
            request_id="req-fair-use",
            include_assets=True,
            assets=AssetManifest(
                assets=[
                    ReportAsset(
                        asset_id="fair-use-asset",
                        local_path=str(image),
                        rights=AssetRights.FAIR_USE,
                    )
                ]
            ),
        )
    )
    assert result.manifest.assets.assets[0].excluded is True
    assert result.manifest.assets.assets[0].exclusion_reason == "RIGHTS_NOT_APPROVED"


def test_checkpoint_manifest_contains_no_public_absolute_path(tmp_path: Path) -> None:
    service, _state, _objects = _service(tmp_path)
    result = service.publish(_request(request_id="req-manifest"))
    dumped = json.loads(result.manifest.model_dump_json())
    assert dumped["output_relative_ref"] == result.safe_file_name
    assert result.public_reference is not None
    assert str(tmp_path) not in result.public_reference.file_name
    assert str(tmp_path) not in result.model_dump_json()


def test_unwritable_configured_root_falls_back_to_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _paths(tmp_path)
    paths.ensure_directories()
    state = StateStore(paths.state_db, PROJECT_ROOT / "migrations")
    state.migrate()
    objects = ObjectStore(paths.objects)
    resolver = ReportPathResolver(
        controlled_root=paths.reports / "output",
        known_folder_resolver=lambda: None,
    )
    service = ReportService(paths, state, objects, path_resolver=resolver)

    invalid = tmp_path / "not-a-directory"
    invalid.write_text("occupied", encoding="utf-8")
    monkeypatch.setenv("ASTOCK_REPORT_ROOT", str(invalid))
    result = service.publish(_request(request_id="req-fallback"))
    assert result.manifest.destination_policy is ReportDirectoryPolicy.CONTROLLED_DIRECTORY
    assert result.safe_file_name is not None
    assert (paths.reports / "output" / result.safe_file_name).is_file()


def test_atomic_publish_interruption_preserves_staged_checkpoint_and_recovers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, state, _objects = _service(tmp_path)
    original_publish = service._atomic_publish
    attempts = 0

    def fail_once(staged: Path, final: Path, *, expected_hash: str) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("simulated publish interruption")
        original_publish(staged, final, expected_hash=expected_hash)

    monkeypatch.setattr(service, "_atomic_publish", fail_once)
    with pytest.raises(OSError, match="simulated publish interruption"):
        service.publish(_request(request_id="req-interrupt"))

    checkpoint = state.get_checkpoint("report", "req-interrupt")
    assert checkpoint is not None
    assert checkpoint["status"] == ReportStatus.STAGED.value
    cursor = checkpoint["cursor"]
    assert isinstance(cursor, dict)
    staged_path = Path(str(cursor["staged_path"]))
    assert staged_path.is_file()
    staged_hash = str(cursor["output_sha256"])

    recovered = service.recover("req-interrupt")
    assert recovered.recovered_existing is True
    assert recovered.output_sha256 == staged_hash
    assert recovered.status is ReportStatus.PUBLISHED
    assert not staged_path.exists()


def test_post_replace_interruption_recovers_existing_final_without_rerender(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service, state, _objects = _service(tmp_path)
    original_finalize = service._finalize
    attempts = 0

    def fail_once(
        staged_manifest: ReportManifest,
        final_path: Path,
        *,
        recovered: bool,
    ) -> ReportPublishResult:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("simulated finalize interruption")
        return original_finalize(staged_manifest, final_path, recovered=recovered)

    monkeypatch.setattr(service, "_finalize", fail_once)
    with pytest.raises(OSError, match="simulated finalize interruption"):
        service.publish(_request(request_id="req-finalize-interrupt"))

    checkpoint = state.get_checkpoint("report", "req-finalize-interrupt")
    assert checkpoint is not None
    assert checkpoint["status"] == ReportStatus.STAGED.value
    cursor = checkpoint["cursor"]
    assert isinstance(cursor, dict)
    final_path = Path(str(cursor["final_path"]))
    assert final_path.is_file()
    expected_hash = str(cursor["output_sha256"])

    recovered = service.recover("req-finalize-interrupt")
    assert recovered.recovered_existing is True
    assert recovered.output_sha256 == expected_hash
    assert recovered.status is ReportStatus.PUBLISHED


def test_effective_format_is_part_of_idempotency_identity(tmp_path: Path) -> None:
    service, state, _objects = _service(tmp_path)
    first = service.publish(_request(request_id="req-effective-format"))
    assert first.published_format is ReportFormat.DOCX

    PresentationPreferencesRepository(state).set(
        PreferenceKey.DEFAULT_REPORT_FORMAT,
        ReportFormat.MD,
    )
    conflict = service.publish(_request(request_id="req-effective-format"))
    assert conflict.status is ReportStatus.CONFLICT
    assert conflict.degradation_reason == "REPORT_KEY_CONTENT_CONFLICT"
