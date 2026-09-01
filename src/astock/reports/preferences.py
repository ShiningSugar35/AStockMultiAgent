"""Cross-session presentation preferences with base and temporary override layers."""

from __future__ import annotations

import json
from contextlib import closing
from datetime import UTC, datetime
from enum import StrEnum
from typing import overload

from astock.core.hashing import canonical_json_bytes
from astock.core.state import StateStore
from astock.schemas.reports import (
    CitationLevel,
    PdfPreference,
    PreferenceKey,
    PreferenceLength,
    PresentationPreferences,
    PrivacyLevel,
    ReportDirectoryPolicy,
    ReportFormat,
)

_FIELD_BY_KEY: dict[PreferenceKey, str] = {
    PreferenceKey.DEFAULT_LENGTH: "default_length",
    PreferenceKey.DEFAULT_REPORT_FORMAT: "default_report_format",
    PreferenceKey.REPORT_DIRECTORY_POLICY: "report_directory_policy",
    PreferenceKey.CITATION_LEVEL: "citation_level",
    PreferenceKey.PRIVACY_DEFAULT: "privacy_default",
    PreferenceKey.PDF_PREFERENCE: "pdf_preference",
}
_ENUM_BY_KEY: dict[PreferenceKey, type[StrEnum]] = {
    PreferenceKey.DEFAULT_LENGTH: PreferenceLength,
    PreferenceKey.DEFAULT_REPORT_FORMAT: ReportFormat,
    PreferenceKey.REPORT_DIRECTORY_POLICY: ReportDirectoryPolicy,
    PreferenceKey.CITATION_LEVEL: CitationLevel,
    PreferenceKey.PRIVACY_DEFAULT: PrivacyLevel,
    PreferenceKey.PDF_PREFERENCE: PdfPreference,
}


class PresentationPreferencesRepository:
    """Persist only presentation choices; no ledger or portfolio table is referenced."""

    def __init__(
        self,
        state: StateStore,
        *,
        defaults: PresentationPreferences | None = None,
    ) -> None:
        self.state = state
        self.defaults = defaults or PresentationPreferences()

    def get_all(self) -> PresentationPreferences:
        payload = self.defaults.model_dump(mode="python")
        with closing(self.state.connect()) as connection:
            rows = connection.execute(
                "SELECT key,base_value_json,override_value_json FROM presentation_preference"
            ).fetchall()
        for row in rows:
            key = PreferenceKey(str(row["key"]))
            selected = row["override_value_json"] or row["base_value_json"]
            if selected is None:
                continue
            payload[_FIELD_BY_KEY[key]] = _parse_value(key, str(selected))
        payload["profile_id"] = "default"
        return PresentationPreferences.model_validate(payload)

    def get(self, profile_id: str = "default") -> PresentationPreferences:
        if profile_id != "default":
            raise ValueError("only the canonical default presentation profile is supported")
        return self.get_all()

    @overload
    def set(self, key: PreferenceKey | str, value: object) -> PresentationPreferences: ...

    @overload
    def set(
        self,
        key: PresentationPreferences,
        value: None = None,
    ) -> PresentationPreferences: ...

    def set(
        self,
        key: PreferenceKey | str | PresentationPreferences,
        value: object | None = None,
    ) -> PresentationPreferences:
        if isinstance(key, PresentationPreferences):
            if value is not None:
                raise ValueError("bulk preference set does not accept a second value")
            for preference_key, field_name in _FIELD_BY_KEY.items():
                selected = getattr(key, field_name)
                if selected is not None:
                    self._write(preference_key, selected, override=False)
            return self.get_all()
        preference_key = PreferenceKey(key)
        parsed = _coerce_value(preference_key, value)
        self._write(preference_key, parsed, override=False)
        return self.get_all()

    def override(self, key: PreferenceKey | str, value: object) -> PresentationPreferences:
        preference_key = PreferenceKey(key)
        parsed = _coerce_value(preference_key, value)
        self._write(preference_key, parsed, override=True)
        return self.get_all()

    def update(self, profile_id: str = "default", **overrides: object) -> PresentationPreferences:
        if profile_id != "default":
            raise ValueError("only the canonical default presentation profile is supported")
        for field_name, value in overrides.items():
            matching = [
                key
                for key, persisted_field in _FIELD_BY_KEY.items()
                if persisted_field == field_name
            ]
            if not matching:
                raise ValueError(f"unsupported presentation preference: {field_name}")
            self.set(matching[0], value)
        return self.get_all()

    def delete(self, key: PreferenceKey | str) -> PresentationPreferences:
        """Delete only the temporary override, exposing the durable base value again."""

        preference_key = PreferenceKey(key)
        with self.state.transaction() as connection:
            row = connection.execute(
                "SELECT base_value_json FROM presentation_preference WHERE key=?",
                (preference_key.value,),
            ).fetchone()
            if row is not None and row["base_value_json"] is None:
                connection.execute(
                    "DELETE FROM presentation_preference WHERE key=?",
                    (preference_key.value,),
                )
            else:
                connection.execute(
                    "UPDATE presentation_preference SET override_value_json=NULL,updated_at=? "
                    "WHERE key=?",
                    (datetime.now(UTC).isoformat(), preference_key.value),
                )
        return self.get_all()

    def reset(self, key: PreferenceKey | str | None = None) -> PresentationPreferences:
        with self.state.transaction() as connection:
            if key is None:
                connection.execute("DELETE FROM presentation_preference")
            else:
                connection.execute(
                    "DELETE FROM presentation_preference WHERE key=?",
                    (PreferenceKey(key).value,),
                )
        return self.get_all()

    def export_safe(self) -> dict[str, object]:
        return self.get_all().model_dump(mode="json")

    def _write(self, key: PreferenceKey, value: StrEnum, *, override: bool) -> None:
        encoded = canonical_json_bytes(value.value).decode("utf-8")
        now = datetime.now(UTC).isoformat()
        with self.state.transaction() as connection:
            row = connection.execute(
                "SELECT base_value_json FROM presentation_preference WHERE key=?",
                (key.value,),
            ).fetchone()
            if row is None:
                connection.execute(
                    "INSERT INTO presentation_preference(key,base_value_json,override_value_json,"
                    "updated_at) VALUES(?,?,?,?)",
                    (key.value, None if override else encoded, encoded if override else None, now),
                )
            elif override:
                connection.execute(
                    "UPDATE presentation_preference SET override_value_json=?,updated_at=? "
                    "WHERE key=?",
                    (encoded, now, key.value),
                )
            else:
                connection.execute(
                    "UPDATE presentation_preference SET base_value_json=?,updated_at=? WHERE key=?",
                    (encoded, now, key.value),
                )


def _coerce_value(key: PreferenceKey, value: object) -> StrEnum:
    enum_type = _ENUM_BY_KEY[key]
    candidate: object = value.value if isinstance(value, StrEnum) else value
    if not isinstance(candidate, str):
        raise ValueError(f"preference {key.value} requires a string enum value")
    parsed = enum_type(candidate)
    if key is PreferenceKey.REPORT_DIRECTORY_POLICY and parsed is ReportDirectoryPolicy.CUSTOM:
        raise ValueError("CUSTOM report roots are request-scoped and are not persisted")
    return parsed


def _parse_value(key: PreferenceKey, encoded: str) -> StrEnum:
    return _coerce_value(key, json.loads(encoded))


__all__ = ["PresentationPreferencesRepository"]
