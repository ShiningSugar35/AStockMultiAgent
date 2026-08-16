# Workflow — Knowledge Ingest

## When to use

Use when the user explicitly approves a local book/document or allowlisted author history for durable knowledge collection and later reviewed Skill distillation.

Primary skill: `$knowledge-ingest`.

## Flow

1. **Confirm source authority and scope**
   - Accept only approved private local material or configured allowlisted online authors/content types.
   - Do not silently expand to unrelated authors or sources.

2. **Prefer structured, auditable acquisition**
   - Use verified local/API transport first, then MCP/browser where required, then manual import only as fallback.
   - Persist each raw response/object before advancing collection checkpoints.

3. **Enumerate to an explicit terminal condition**
   - Traverse each configured content type/page/cursor from the known boundary to terminal state.
   - Preserve failures separately from confirmed-empty responses.
   - Pause on access restriction, login loss, security verification, rate limiting or unexpected schema rather than fabricating completeness.

4. **Handle documents and visuals with provenance**
   - PDF/DOCX content is parsed into immutable blocks/paragraphs with exact locators.
   - Freeze image bytes and placement before OCR.
   - OCR is a separate derived object; low-confidence/no-text/unknown visual content stays reviewable rather than becoming a method automatically.

5. **Build semantic units without losing source context**
   - Keep SourceItem → ParagraphUnit → complete ArgumentUnit lineage.
   - Do not distill an isolated image paragraph or mechanical heading into an investment rule.
   - Preserve context around chart/evidence/conclusion boundaries.

6. **Distill reviewed source methods, then audit generality**
   - Candidate methods must retain source references, applicability, required evidence, signals/counter-signals and boundaries.
   - Review/approve/reject is append-only and auditable, but source review alone does not make a Skill active production knowledge.
   - Before activation, every source Skill enters the Knowledge Skill Audit and receives exactly one `KEEP / KEEP_SCOPED / REVISE / RETIRE` verdict. The audit binds the original immutable Skill/object/hash and at least two external authoritative evidence IDs.
   - Same-premise conflicts cannot remain broad active rules. Different time horizons/assets/information assumptions may coexist only after their premises are explicit; otherwise prefer the proposition with stronger regulatory/accounting/peer-reviewed evidence.
   - Time-specific market calls, exact levels, named-company forecasts, direct trade instructions and unverifiable actor intent remain historical case evidence by default. `REVISE` creates a new object; original source objects are never overwritten.

7. **Publish only the audited Knowledge registry**
   - Research Runtime consumes knowledge only through the latest audited `KnowledgeSkillProvider` registry and exact release/hash validation. Historical composite releases remain available only for provenance/re-audit.
   - Community/social sources may identify missing capabilities but cannot admit a Skill. New curated Skills require multi-source official/peer-reviewed/primary-engineering evidence; alpha-like rules remain shadow/prospective-only until forward validation.
   - Do not query internal knowledge tables directly from the runtime.

8. **Move obsolete production history out of the hot store only after publication**
   - `knowledge-cold-archive-plan` must first compute the row-level FK parent closure required by all surviving hot tables. `knowledge-cold-archive-run --confirm` may archive only the unprotected historical Semantic/Distillation/Reviewed/Book/Private rows.
   - Every archive is zstd Parquet by source table plus an ObjectStore-backed manifest; require hash/row-count audit and a successful restore proof. Raw SourceSnapshot/Evidence/Zhihu content/comment versions and source ObjectStore bodies remain immutable and hot-addressable.
   - `knowledge-parquet-compact --confirm` may merge immutable one-row knowledge metadata files inside their existing author/content_type/year partition. Additive historical schema drift is a nullable union, not a rewritten record.
   - Run `state-vacuum --confirm` only after cold-archive and Parquet audits both pass.

## Output

Report source coverage, confirmed gaps, review state, published Skill counts and registry identity without exposing private plaintext/path data unnecessarily.

## Stop conditions

- Never store cookies, browser profiles, API secrets or private source text in Git/log output.
- Never bypass captchas/access controls/rate limits.
- Failed acquisition is not proof of no content.
- Knowledge method popularity never becomes trading authority by itself.
