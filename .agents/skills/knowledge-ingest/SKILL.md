---
name: knowledge-ingest
description: Ingest approved local PDF/DOCX documents and process preserved knowledge evidence for audited methods. Use for local investment books, user-approved files or existing immutable snapshots. Online community author collection is retired.
---

# 本地知识摄取

1. Confirm an approved local file or existing immutable snapshot and its configured source identity. Missing local material is not permission to restart online author collection.
2. Use existing commands: `uv run astock private-pdf-ingest --help` or `uv run astock private-docx-ingest --help`, then supply the approved file, source identity and immutable version. Freeze bytes before parsing and preserve source/hash lineage; partial parsing must not claim complete coverage.
3. Reuse `knowledge-local-coverage` / `knowledge-coverage-audit` for preserved material when needed. Report known local coverage and unresolved gaps; historical author metadata is compatibility evidence, not a mandate to enumerate or refresh online histories.
4. Retain SourceItem → ParagraphUnit → ArgumentUnit lineage. Freeze image bytes and their location before OCR; keep OCR separate and preserve adjacent context. An image between a claim and conclusion uses `MERGE_WITH_BOTH`; unknown/low-confidence/incomplete context remains `NEEDS_REVIEW`. Do not distill an isolated paragraph.
5. Review candidate methods with premises, falsifiers, applicability and exact source references. Before activation use the existing Knowledge Skill Audit: immutable input/hash, independent authoritative support and one `KEEP / KEEP_SCOPED / REVISE / RETIRE` decision. `REVISE` creates a new object. Source review alone does not grant production authority.
6. Publish only the audited active registry through existing commands. Keep market calls, named-company forecasts and unverifiable actor intent as historical cases; popularity does not admit a method. `formal_committee_weight_allowed=false` remains binding; alpha-like rules require their own prospective qualification.
7. Let existing lifecycle/knowledge GC handle unreferenced retired material after dependency and retention checks. Protect active evidence and minimum referenced parents; do not create backup directories or preserve every historical payload indefinitely.

## Workflows

- [Local knowledge and preserved evidence](../../../docs/workflows/workflow-knowledge-ingest.md)

## Output

Return the local ingestion manifest, snapshot/hash references, actual parse/coverage state, unresolved review items and audited registry identity/count when relevant. Do not require an `AuthorCollectionCoverageReport` for every online author/content type or claim a full online history from a local file. Keep private paths/text out of logs and ordinary user output.

## Prohibitions

- Do not use logged-in browsers, MCP, HTTP recovery, pagination or capture schedulers to collect community author histories; that online lane is retired.
- Do not treat empty/failed parsing as proof of absent content, incomplete coverage as complete, or an audited source as an active method without admission.
- Do not overwrite original evidence, export private text without authorization, or admit a social opinion/backtest directly into weights or the ledger.
