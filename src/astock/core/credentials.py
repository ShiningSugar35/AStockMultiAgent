"""Local provider credential resolution without mutating process environment."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from astock.core.project_root import resolve_project_root

_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class CredentialSource(StrEnum):
    PROCESS_ENV = "PROCESS_ENV"
    PROJECT_DOTENV = "PROJECT_DOTENV"
    MISSING = "MISSING"
    INVALID_DOTENV = "INVALID_DOTENV"


@dataclass(frozen=True, slots=True)
class CredentialResolution:
    name: str
    value: str | None = field(repr=False)
    source: CredentialSource
    problem: str | None = None

    @property
    def configured(self) -> bool:
        return self.value is not None and self.source in {
            CredentialSource.PROCESS_ENV,
            CredentialSource.PROJECT_DOTENV,
        }

    def public_status(self) -> dict[str, object]:
        return {
            "environment": self.name,
            "configured": self.configured,
            "source": self.source.value,
            "problem": self.problem,
        }


def _parse_value(raw: str) -> tuple[str | None, str | None]:
    value = raw.strip()
    if not value:
        return None, None
    if value[0] in {'"', "'"}:
        quote = re.escape(value[0])
        match = re.fullmatch(rf"{quote}(.*?){quote}(?:\s+#.*)?", value)
        if match is None:
            return None, "UNTERMINATED_QUOTE"
        return (match.group(1) or None), None
    match = re.search(r"\s+#", value)
    if match:
        value = value[: match.start()].rstrip()
    return (value or None), None


def resolve_credential(
    name: str,
    *,
    project_root: Path | None = None,
) -> CredentialResolution:
    """Resolve one credential from process env first, then project-root .env."""

    if not _KEY.fullmatch(name):
        raise ValueError("credential environment name is invalid")
    process_value = os.environ.get(name, "").strip()
    if process_value:
        return CredentialResolution(name, process_value, CredentialSource.PROCESS_ENV)

    root = project_root or resolve_project_root(module_file=Path(__file__))
    dotenv = root / ".env"
    if not dotenv.is_file():
        return CredentialResolution(name, None, CredentialSource.MISSING)

    matches: list[tuple[str | None, str | None]] = []
    try:
        lines = dotenv.read_text(encoding="utf-8-sig").splitlines()
    except (OSError, UnicodeError):
        return CredentialResolution(
            name, None, CredentialSource.INVALID_DOTENV, "DOTENV_UNREADABLE"
        )
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("export "):
            stripped = stripped[7:].lstrip()
        if "=" not in stripped:
            continue
        key, raw_value = stripped.split("=", 1)
        key = key.strip()
        if key != name:
            continue
        matches.append(_parse_value(raw_value))

    if len(matches) > 1:
        return CredentialResolution(
            name, None, CredentialSource.INVALID_DOTENV, "DUPLICATE_KEY"
        )
    if not matches:
        return CredentialResolution(name, None, CredentialSource.MISSING)
    value, problem = matches[0]
    if problem:
        return CredentialResolution(name, None, CredentialSource.INVALID_DOTENV, problem)
    if not value:
        return CredentialResolution(name, None, CredentialSource.MISSING)
    return CredentialResolution(name, value, CredentialSource.PROJECT_DOTENV)


def credential_is_configured(name: str, *, project_root: Path | None = None) -> bool:
    return resolve_credential(name, project_root=project_root).configured


__all__ = [
    "CredentialResolution",
    "CredentialSource",
    "credential_is_configured",
    "resolve_credential",
]
