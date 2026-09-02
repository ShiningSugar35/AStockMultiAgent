---
name: report-visual-qa
description: Review rendered report visual QA, manifest integrity, privacy, citations, assets, and delivery failures without changing research facts.
---

# Report Visual QA

1. Read `uv run astock report-policy-status` and `uv run astock report-schema` before reviewing output so format, privacy, citation, asset-rights and converter requirements come from the existing report contract.
2. Review only products emitted by the existing report service. Keep the report source artifact, CitationManifest, AssetManifest and ReportManifest as the authoritative delivery lineage; this Skill does not create a second report facts store.
3. Verify deterministic checks first: terminal report status, output hash/manifest identity, citation references, asset-rights status, privacy audit result, requested output format and converter capability. Fail closed on a missing or corrupt artifact.
4. When a rendered PDF/DOCX is available, inspect the actual rendered pages for clipping, missing glyphs, table truncation, orphaned headings, broken images and unreadable layout. A text extraction success is not evidence that visual rendering passed.
5. If visual inspection is unavailable in the current environment, report the visual gate as incomplete and keep this Skill in `SHADOW`; do not invent a PASS from source text or file existence.
6. On a failed visual check, preserve the original source and report artifacts, record the violation, and use the existing report recovery/publish path rather than rewriting research content inside the QA Skill.
7. Record selection/completion through `uv run astock agent-observation-register <request.json>` and inspect `uv run astock agent-observability-report --lookback-days 30` for Skill routing telemetry.

## Output

Return the ReportManifest identity, output hash, deterministic integrity findings, actual rendered-page findings when performed, privacy/citation/asset-rights findings, and an explicit `PASS`, `FAIL`, or `INCOMPLETE` visual verdict. The result must state whether page-level visual inspection actually occurred.

## Prohibitions

- Do not claim visual QA from a parser-only, source-text-only, or file-exists check.
- Do not alter investment facts, citations, page content or source artifacts to make a visual check pass.
- Do not treat a Skill result as new source authority or bypass ReportService/ObjectStore lineage.
- Do not copy images with unknown rights into a report or suppress privacy findings.
- Do not enable broker execution; `broker_execution_allowed=false` remains unchanged.
