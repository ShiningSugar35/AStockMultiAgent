---
name: evidence-investigation
description: Resolve a specific, decision-relevant evidence gap using the audited source priority and precise citations. Use when a claim is unsupported or conflicting, the committee returns NEEDS_INFO, an official fact needs verification, or a manual investigation task must be created.
---

# 证据调查

1. State one claim or conflict and its decision impact.
2. Reuse existing snapshots before requesting any source.
3. Follow API/local -> MCP -> browser -> manual priority and record the SourceAccessDecision.
4. Save raw material immutably and cite an exact page, section, DOM locator, or snapshot.
5. Return support, refutation, context, conflict, or still-missing status.

## Output

Produce `ManualInvestigationTask` when evidence remains missing, or versioned Evidence/Claim links when resolved. Never silently turn a lead into a fact.

## Prohibitions

- Do not broaden a precise gap into unrestricted browsing.
- Do not let the committee fetch its own evidence.
- Do not duplicate a source already sufficient for the claim.
