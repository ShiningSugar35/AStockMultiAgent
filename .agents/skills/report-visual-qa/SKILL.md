---
name: report-visual-qa
description: DOCX/PDF rendering, pagination, table, font, reference, image rights, privacy, output hash verification; consumes report service products, does not establish second report facts source
---

# Report Visual QA

## Responsibility
Formal report of visual and delivery acceptance: DOCX/PDF rendering, pagination, widowed headings, table truncation, font fallback, Chinese display, references, image rights, privacy scan, output hash and Manifest reconciliation. This Skill consumes report service products and does not establish a second report facts source.

## Triggers
- **Negative**: When rendering produces visual artifacts (missing content, truncation, font fallback errors, unrendered Chinese characters, broken references, privacy redactions failures, hash mismatches), the skill returns `REJECTED` with specific placement violation codes.
- **Conflict routing**: When multiple report formats compete for the same output slot, the skill resolves through the existing document validation pipeline without creating a second facts source.
- **Fact/permission protection**: Ensures reported metadata (page counts, section headers, image captions) matches the source document; no fabrication or alteration of document content.
- **Recovery**: When a document fails visual QA, fallback to deterministic text extraction; the original document remains unchanged and authoritative.
- **Exit/Uninstall**: On skill unload, all visual QA reference data and hash manifests are preserved; document generation continues via existing deterministic paths.

## Workflow
1. Accept a rendered DOCX/PDF output and its source document metadata.
2. Verify pagination consistency (no orphan headings, all pages accounted for).
3. Verify table integrity (no truncation, all rows/columns present, data matches source).
4. Verify font rendering (Chinese fallback chains present, no missing glyph groups).
5. Verify reference integrity (all citations match source, no phantom references).
6. Verify image rights and privacy (no unauthorized image reuse, PII redaction complete).
7. Compute output hash (Manifest reconciliation with source artifact).
8. If all checks pass, return `ACCEPTED` with hash and violation count.
9. If any check fails, return `REJECTED` with specific violation codes; document generation continues via existing path.

## Observability
- Records placement violations, hash changes, and fallback triggers per `agent-observation-register`.
- Emits `agent-observability-report` compatible metrics via ResearchRun checkpoint.

## Hard Contracts
- Consumes report service products; does not establish second report facts source.
- All passing items enter existing `M-06 ExternalCapabilityRegistry/QualificationRelease` Router fallback, not a second bus.
- Execution-type broker MCP permanently rejected: `broker_execution_allowed=false` remains.
- Large dependency/lazy loading does not enter core minimal installation.
- Public live only read-only low-frequency: prohibited account/Secret/write operations.
- Does not bypass existing access flow to directly modify active production parser (see `schema-drift-recorder` exit contract).