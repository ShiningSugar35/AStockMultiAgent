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

6. **Distill only reviewed reusable methods**
   - Candidate methods must retain source references, applicability, required evidence, signals/counter-signals and boundaries.
   - Review/approve/reject is append-only and auditable.
   - Published Knowledge Skills remain research methods/context; they do not automatically create company facts, Committee votes, target prices or weights.

7. **Publish through the Knowledge registry**
   - Research Runtime consumes knowledge only through the published `KnowledgeSkillProvider` registry and exact release/hash validation.
   - Do not query internal knowledge tables directly from the runtime.

## Output

Report source coverage, confirmed gaps, review state, published Skill counts and registry identity without exposing private plaintext/path data unnecessarily.

## Stop conditions

- Never store cookies, browser profiles, API secrets or private source text in Git/log output.
- Never bypass captchas/access controls/rate limits.
- Failed acquisition is not proof of no content.
- Knowledge method popularity never becomes trading authority by itself.
