CREATE TABLE report_manifest (
    report_key TEXT PRIMARY KEY,
    request_hash TEXT NOT NULL,
    input_hashes_json TEXT NOT NULL DEFAULT '[]',
    template_version TEXT NOT NULL,
    renderer TEXT NOT NULL,
    renderer_version TEXT NOT NULL,
    converter_json TEXT,
    output_format TEXT,
    privacy_level TEXT NOT NULL,
    citation_level TEXT NOT NULL,
    citations_json TEXT NOT NULL DEFAULT '{}',
    assets_json TEXT NOT NULL DEFAULT '{}',
    output_file_name TEXT,
    output_relative_ref TEXT,
    output_sha256 TEXT,
    output_byte_size INTEGER CHECK (output_byte_size IS NULL OR output_byte_size >= 0),
    publish_status TEXT NOT NULL CHECK (
        publish_status IN ('PENDING','STAGED','PUBLISHED','DEGRADED','FAILED','CONFLICT')
    ),
    degradation_reason TEXT,
    publish_attempts INTEGER NOT NULL DEFAULT 1 CHECK (publish_attempts >= 1),
    destination_policy TEXT NOT NULL,
    recovered_existing INTEGER NOT NULL DEFAULT 0 CHECK (recovered_existing IN (0,1)),
    created_at TEXT NOT NULL,
    published_at TEXT,
    manifest_artifact_id TEXT,
    manifest_object_hash TEXT,
    manifest_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX idx_report_manifest_status
ON report_manifest(publish_status, created_at);

CREATE INDEX idx_report_manifest_request_hash
ON report_manifest(request_hash);
