---
name: evidence-investigation
description: Resolve a specific, decision-relevant evidence gap using audited local sources, provider fallback, authoritative Web research, and precise citations. Use when a claim is unsupported or conflicting, the committee returns NEEDS_INFO, an official fact needs verification, or a manual evidence gap must be resolved.
---

# 证据调查

1. Do not start with `uv run astock probe` unless debugging provider capability itself. State one bounded claim/conflict and why it matters to the investment decision. For committee gaps, inspect `uv run astock committee-task-status <task_id>` and investigate only the named issue.
2. Reuse valid existing snapshots first. Then spend the bounded automatic-recovery budget diagnosing deterministic local/API failures: classify transport/schema/data-quality faults, retry only transient errors, switch to a configured backup or a more reliable endpoint for the same capability, and preserve the failure internally. A preferred-provider failure is never a terminal research state by itself.
3. If deterministic sources remain insufficient, automatically continue with Web search before asking the user. Preferred order: exchange / CNINFO / issuer IR / regulator official sources, then reputable professional/public sources for corroboration. Prefer sources that do not require account creation, API keys, cookies, or user interaction. Current-investment investigations share the 1800-second automatic resolution budget defined by the orchestrator.
4. Cross-check material contested facts with independent authoritative sources where practical. Distinguish statutory facts, issuer statements, secondary structured hints, and inference. Do not turn absence of a search result into refutation.
5. Where a repository capture/import command exists, save the authoritative raw material immutably and cite an exact page/section/DOM/snapshot. If the external source cannot yet enter a formal repository artifact, it may support a clearly labelled provisional user explanation but cannot silently satisfy a formal frozen-evidence gate.
6. Only when local/API fallback and authoritative Web search are both exhausted may manual intervention be requested. Aggregate every remaining request into a single `ManualInvestigationTask`-style checklist; never interrupt the user repeatedly for separate source gaps.
7. Use `uv run astock codex-run-init <request> --artifact-id <artifact_id> --require-registered-output` and `codex-run-audit` when a durable registered investigation is required. When a genuinely new frozen artifact resolves a committee gap, link it with `uv run astock committee-task-resolve <task_id> <artifact_id>`; a new committee decision is still required.

## Workflows

- [`docs/workflows/workflow-evidence-recovery.md`](../../../docs/workflows/workflow-evidence-recovery.md)

## Output

For a normal investor conversation, return the resolved fact or plain-language remaining uncertainty and its decision impact. Keep provider error codes, task IDs, hashes, stack traces, command logs, and artifact internals out of the user-facing answer unless the user explicitly asks for diagnostics. When manual help is truly unavoidable, provide one consolidated checklist explaining exactly what document/action is needed and why.

## Prohibitions

- Do not stop at the first provider error when backup or authoritative Web research is available.
- Do not broaden a bounded gap into unrestricted browsing.
- Do not ask the committee to fetch its own evidence.
- Do not duplicate a source already sufficient for the claim.
- Do not ask the user for a source the Agent can obtain automatically from a public authoritative page.
- Do not silently turn a web lead into a frozen formal fact.
