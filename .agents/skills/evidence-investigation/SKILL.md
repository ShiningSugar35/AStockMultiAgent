---
name: evidence-investigation
description: Resolve a specific, decision-relevant evidence gap using the audited source priority and precise citations. Use when a claim is unsupported or conflicting, the committee returns NEEDS_INFO, an official fact needs verification, or a manual investigation task must be created.
---

# 证据调查

1. Run `uv run astock probe`, then state one claim or conflict and its decision impact.
2. For a committee gap, inspect `uv run astock committee-task-status <task_id>` and investigate only its named reason; reuse existing snapshots before requesting any source.
3. Follow API/local -> MCP -> browser -> manual priority and record the SourceAccessDecision.
4. Save raw material immutably and cite an exact page, section, DOM locator, or snapshot.
5. Use `uv run astock codex-run-init` for a durable investigation; import only an artifact Schema reported as supported by the probe.
6. When a genuinely new frozen artifact resolves the named gap, link it with `uv run astock committee-task-resolve <task_id> <artifact_id>`. This only closes the task index; a new `committee-plan`/`committee-decide` request is still required.
7. Return support, refutation, context, conflict, or still-missing status.

## Output

Produce `ManualInvestigationTask`/committee task status when evidence remains missing, or versioned Evidence/Claim links plus a frozen resolution artifact when resolved. Never silently turn a lead into a fact.

## Prohibitions

- Do not broaden a precise gap into unrestricted browsing.
- Do not let the committee fetch its own evidence.
- Do not duplicate a source already sufficient for the claim.
