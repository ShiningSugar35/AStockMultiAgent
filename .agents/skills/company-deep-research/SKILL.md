---
name: company-deep-research
description: Build a cited, point-in-time company research case from official documents and frozen evidence. Use for deep analysis of a named A-share company, its business model, valuation expectations, industry position, risks, or whether it merits a paper position.
---

# 公司深度研究

1. Run `uv run astock probe`, then `uv run astock context-plan` with only the frozen artifacts needed for this company.
2. Reuse the latest frozen EvidencePack and BaseCasePack when their as-of time is valid.
3. Fetch only incremental disclosures and market data through the source router.
4. Build common analysis once, then load no more than three necessary specialist deltas.
5. Resolve material conflicts or return `NEEDS_INFO` with an evidence investigation task.
6. Start durable work with `uv run astock codex-run-init`; submit only currently supported artifact Schemas with `uv run astock codex-run-import`.

## Output

Produce `BaseCasePack`, optional `SpecialistDelta[]`, and a validated `DecisionPack` only after those Schemas become available. During M1, return `RunManifest(status=NEEDS_INFO)` rather than simulating later-phase research.

## Prohibitions

- Do not reread entire source libraries when a precise evidence locator exists.
- Do not use community material as the sole support for a key fact.
- Do not bypass point-in-time or data-quality gates.
