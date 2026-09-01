CREATE TABLE presentation_preference (
    key TEXT PRIMARY KEY CHECK (
        key IN (
            'DEFAULT_LENGTH',
            'DEFAULT_REPORT_FORMAT',
            'REPORT_DIRECTORY_POLICY',
            'CITATION_LEVEL',
            'PRIVACY_DEFAULT',
            'PDF_PREFERENCE'
        )
    ),
    base_value_json TEXT,
    override_value_json TEXT,
    updated_at TEXT NOT NULL,
    CHECK (base_value_json IS NOT NULL OR override_value_json IS NOT NULL)
);
