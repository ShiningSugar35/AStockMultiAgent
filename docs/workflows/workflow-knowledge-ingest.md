# Workflow — Knowledge Ingest

## When to use

Use for an explicitly approved local PDF/DOCX or preserved immutable knowledge evidence. Online community author collection is retired. Primary skill: `$knowledge-ingest`.

## Flow

1. Resolve the approved file/snapshot and configured source identity. Check prior hash/version to avoid duplicate ingestion. If a necessary local file is absent, identify that private input; do not fall back to browser/MCP/HTTP author collection.
2. Inspect `private-pdf-ingest --help` / `private-docx-ingest --help`, then ingest through the existing service. Keep bytes immutable, parsing scope explicit and source identity registered. A local export proves only its declared local coverage.
3. Preserve SourceItem → ParagraphUnit → ArgumentUnit lineage. Images require byte snapshots, page/bbox or preserved DOM locators, separate OCR and adjacent argument context. Low-confidence or incomplete interpretation remains reviewable.
4. Distill only complete arguments with explicit premises, applicability, evidence and counter-signals. Time-specific market calls and actor-intent claims remain historical cases unless separately established as general methods.
5. Run the existing Knowledge Skill Audit before activation. Every method binds its original immutable object/hash and the required independent authoritative evidence, receiving one `KEEP / KEEP_SCOPED / REVISE / RETIRE` result. Revision never overwrites the original.
6. Publish through the audited registry consumed by `KnowledgeSkillProvider`; do not query internal knowledge tables as a parallel runtime API. Community popularity cannot admit a method; alpha-like rules remain subject to prospective evidence and `formal_committee_weight_allowed=false`.
7. For retirement, use existing reference closure and lifecycle GC. Active evidence, protected FK parents and retention obligations remain. Any needed cold archive must pass manifest/hash/row-count/restore checks before deleting rows; an archive is not a blanket reason to keep every obsolete payload or create a backup tree.

## Output

Report the ingestion manifest, source snapshot/hash, actual parse/local coverage, review gaps and audited registry identity. Existing `knowledge-local-coverage` / `knowledge-coverage-audit` may inspect preserved evidence. An historical `AuthorCollectionCoverageReport` remains readable when present; it is not a required output for every local ingestion or evidence that fresh online enumeration occurred.

## Stop conditions

- Do not restart online author collection, pagination, login capture or a capture scheduler.
- Do not expose cookies, credentials, private filenames/paths or source text in Git/logs.
- Do not interpret failed/empty parsing as proof of no content, or local coverage as complete online history.
- Do not activate a method solely because it is reviewed, visually grounded, popular or successful in one backtest.
