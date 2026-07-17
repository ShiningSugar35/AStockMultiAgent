---
name: company-deep-research
description: Build a cited, point-in-time company research case from official documents and frozen evidence. Use for deep analysis of a named A-share company, its business model, valuation expectations, industry position, risks, or whether it merits a paper position.
---

# 公司深度研究

1. Run `uv run astock probe`, then inspect `uv run astock research-chain-status <company_id>` and `research-chain-audit` before opening sources.
2. Reuse the latest frozen EvidencePack and BaseCasePack when their as-of time is valid; fetch only incremental disclosures and market data through the source router.
3. If required, run `research-evidence-freeze` and `research-base-case-build`; build the common analysis once.
4. Run `research-specialist-route`, no more than three selected diagnostics through `research-specialist-diagnose`, and finish the reference union with `research-memo-compose`.
5. Resolve material conflicts or return `NEEDS_INFO` with an evidence investigation task; require `research-chain-audit` PASS before presenting a completed Phase 4 research chain.
6. Run `uv run astock context-plan --artifact-id <memo_artifact_id>`, then `codex-run-init <request> --artifact-id <memo_artifact_id> --require-registered-output`, import the exact registered memo with `codex-run-import`, and finish with `codex-run-audit`.

## Output

Produce `BaseCasePack`, `SpecialistDelta[]`, diagnostic reports, and `ResearchMemoArtifact` with exact evidence references. `DecisionPack` belongs to Phase 6 and is unavailable until its deterministic committee gates are implemented.

## Prohibitions

- Do not reread entire source libraries when a precise evidence locator exists.
- Do not use community material as the sole support for a key fact.
- Do not bypass point-in-time or data-quality gates.
