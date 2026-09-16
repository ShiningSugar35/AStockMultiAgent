from __future__ import annotations

import json
import os
from pathlib import Path

from typer.testing import CliRunner

from astock.core.credentials import CredentialSource, resolve_credential


def test_project_dotenv_fallback_supports_quotes_comments_and_export(tmp_path, monkeypatch):
    monkeypatch.delenv("TUSHARE_TOKEN", raising=False)
    (tmp_path / ".env").write_text(
        "# local only\nexport TUSHARE_TOKEN = 'unit#dotenv' # comment\n"
        "FINNHUB_API_KEY=unit-news # comment\n",
        encoding="utf-8",
    )
    tushare = resolve_credential("TUSHARE_TOKEN", project_root=tmp_path)
    finnhub = resolve_credential("FINNHUB_API_KEY", project_root=tmp_path)
    assert tushare.source is CredentialSource.PROJECT_DOTENV
    assert tushare.value == "unit#dotenv"
    assert finnhub.value == "unit-news"
    assert "TUSHARE_TOKEN" not in os.environ


def test_process_environment_overrides_project_dotenv(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text("TUSHARE_TOKEN=unit-dotenv\n", encoding="utf-8")
    monkeypatch.setenv("TUSHARE_TOKEN", "unit-process")
    result = resolve_credential("TUSHARE_TOKEN", project_root=tmp_path)
    assert result.source is CredentialSource.PROCESS_ENV
    assert result.value == "unit-process"


def test_blank_duplicate_or_invalid_dotenv_never_exposes_a_value(tmp_path, monkeypatch):
    monkeypatch.delenv("FINNHUB_API_KEY", raising=False)
    (tmp_path / ".env").write_text("FINNHUB_API_KEY=\n", encoding="utf-8")
    blank = resolve_credential("FINNHUB_API_KEY", project_root=tmp_path)
    assert blank.source is CredentialSource.MISSING and blank.value is None

    (tmp_path / ".env").write_text(
        "FINNHUB_API_KEY=first\nFINNHUB_API_KEY=second\n", encoding="utf-8"
    )
    duplicate = resolve_credential("FINNHUB_API_KEY", project_root=tmp_path)
    assert duplicate.source is CredentialSource.INVALID_DOTENV
    assert duplicate.problem == "DUPLICATE_KEY"
    assert duplicate.value is None
    assert "first" not in json.dumps(duplicate.public_status())
    assert "second" not in json.dumps(duplicate.public_status())

    (tmp_path / ".env").write_text("FINNHUB_API_KEY='unterminated\n", encoding="utf-8")
    invalid = resolve_credential("FINNHUB_API_KEY", project_root=tmp_path)
    assert invalid.source is CredentialSource.INVALID_DOTENV
    assert invalid.problem == "UNTERMINATED_QUOTE"
    assert invalid.value is None


def test_root_dotenv_contract_is_git_ignored_and_example_has_only_blank_slots():
    root = Path(__file__).resolve().parents[2]
    ignore = (root / ".gitignore").read_text(encoding="utf-8")
    example = (root / ".env.example").read_text(encoding="utf-8")
    assert ".env\n" in ignore and "!.env.example" in ignore
    assert "TUSHARE_TOKEN=\n" in example
    assert "FINNHUB_API_KEY=\n" in example


def test_provider_credentials_status_never_returns_values(monkeypatch):
    from astock.cli import app

    monkeypatch.setenv("TUSHARE_TOKEN", "unit-status-tushare")
    monkeypatch.setenv("FINNHUB_API_KEY", "unit-status-finnhub")
    result = CliRunner().invoke(app, ["provider-credentials-status"])
    assert result.exit_code == 0, result.output
    assert "provider-credential-status-v1" in result.output
    assert "PROCESS_ENV" in result.output
    assert "unit-status-tushare" not in result.output
    assert "unit-status-finnhub" not in result.output
    assert "secret_values_returned" in result.output


def test_supplemental_service_factory_and_adapter_share_project_root(tmp_path):
    from astock.providers.supplemental import SupplementalEvidenceService
    from tests.unit.test_current_research_continuation import PROJECT_ROOT, _runtime

    _, state, objects = _runtime(tmp_path / "runtime")
    service = SupplementalEvidenceService(PROJECT_ROOT, state, objects)
    assert service.factory.project_root == PROJECT_ROOT.resolve()
    provider = service.factory.create("finnhub-news-hints")
    assert provider.project_root == service.factory.project_root
