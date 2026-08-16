---
name: knowledge-ingest
description: Ingest an approved local document or allowlisted author's accessible history with immutable snapshots and resumable coverage. Use for the local investment book, approved PDFs, Zhihu answers, thoughts, articles, columns, collection coverage, retries, or incremental knowledge updates.
---

# 知识采集

1. Run `uv run astock probe`, then confirm the source is local private material or an allowlisted author the user can access.
2. Prefer a verified structured request, then MCP, then the logged-in browser, then manual HTML/Markdown.
3. Save every raw response to ObjectStore before advancing a checkpoint.
4. Enumerate each content type from its first page to an explicit terminal condition.
5. Record failures and gaps separately from confirmed empty results.
6. Pause on 403, 429, login loss, security verification, or unexpected structure.
7. For PDF or web images, freeze the image bytes and locator before OCR. Store OCR text
   separately, then attach the image Paragraph to its previous and following argument context.
8. Never distill an image Paragraph alone. A chart between a claim and conclusion must use
   `MERGE_WITH_BOTH`; failed, low-confidence, no-text, unknown, or incomplete context stays
   `NEEDS_REVIEW`.
9. A reviewed source Skill is **not automatically an active production method**. Before a new registry can become active, run the Knowledge Skill Audit: every Skill must bind its immutable original source/hash plus at least two external authoritative evidence IDs and receive exactly one `KEEP / KEEP_SCOPED / REVISE / RETIRE` verdict.
10. Treat time-specific market commentary, exact index/price levels, named-company forecasts, first-person trade calls and unverifiable actor intent as historical evidence/case material by default, not reusable Skill logic. A reusable method must be rewritten with explicit premises, falsifiers and stronger evidence; `REVISE` creates a new object and never overwrites the source Skill.
11. Community/social popularity may discover a missing method, but cannot admit it. New curated Skills require official, peer-reviewed or primary engineering evidence and remain `formal_committee_weight_allowed=false`; alpha-like rules stay shadow/prospective-only until forward evidence exists.

## Workflows

- [`docs/workflows/workflow-knowledge-ingest.md`](../../../docs/workflows/workflow-knowledge-ingest.md)

## Output

Produce `AuthorCollectionCoverageReport` for every author/content type. Later distillation must produce separate candidate-selection and position-lifecycle manifests with source snapshot IDs. A published source registry is historical provenance until the Knowledge Skill Audit publishes an audited active registry; report both historical and audited counts when they differ.

## Prohibitions

- Do not store plaintext cookies or browser profiles in Git or logs.
- Do not bypass captchas, signatures, access controls, or rate limits.
- Do not interpret a failed or empty response as proof that no content exists.
- Do not admit a social-media opinion, exact market call, or single successful backtest into active Knowledge solely because it was reviewed or visually grounded.
