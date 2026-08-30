---
name: governance-management-quality
description: Build a point-in-time governance and management-quality assessment for an A-share company. Use for Research Team GOVERNANCE tasks, ownership/control, board and incentives, related-party transactions, capital allocation, auditor changes/opinions, pledges, dilution, succession, or disclosure quality.
---

# 治理与管理层质量

## Inputs and authority

- Consume only the ready `governance-management-quality` task from the existing `ResearchTeamPlan`; do not create a parallel plan or a free-form personality score.
- Preserve company identity, `as_of`, acquisition lineage and the typed `GovernanceManagementQualityPack` contract.
- Primary authority is exchange/CNINFO/issuer charters, annual/governance reports, resolutions, ownership/pledge filings, audit reports, regulator/court/enforcement records and formal transaction documents. Interviews and media are secondary context.

## Procedure

1. Inspect `uv run astock research-team-status <plan_id>` and confirm the GOVERNANCE task is ready.
2. Separate governance facts from management interpretation. Freeze control structure, board independence, incentives, related-party transactions, capital allocation, financing/dilution, dividends/buybacks, M&A, auditor/opinion history, disclosure corrections, pledges, succession and execution against prior disclosed targets.
3. Use a point-in-time evidence table and compare actions with stated policy. Record favorable and adverse evidence symmetrically.
4. Connect each material finding once to forecast/valuation mechanisms: cost of capital, dilution, reinvestment return, minority-shareholder risk, cash conversion, control risk or scenario probabilities.
5. Detect duplicate treatment where the same governance risk is already embedded in forecast, probability or valuation.

## Required output contract

- Register a typed `GovernanceManagementQualityPack` through `uv run astock research-team-role-output REQUEST.json`.
- Complete `governance-management-quality` through `uv run astock research-team-task-result REQUEST.json`.
- Set `GOVERNANCE_QUALITY=true` only when material ownership/control, related-party, audit/opinion, capital-allocation, disclosure and falsifier evidence is current and frozen.

## Output

Return the registered typed `GovernanceManagementQualityPack` artifact ID, control and incentive facts, capital-allocation record, audit/disclosure quality, execution evidence, quantified thesis impacts, unresolved conflicts and falsifiers. Exclude personality judgments.

## Gates and abstention

Abstain and leave `GOVERNANCE_QUALITY=false` when ownership/control lineage is incomplete, material related-party or audit records conflict, or an allegation lacks a formal source. Escalate the evidence gap; never label fraud, intent or character from an anomaly, rumor or communication style alone.

## Verification

- Re-run `uv run astock research-team-status <plan_id>` and verify the typed artifact, COMPLETE task and readiness evidence.
- Verify later enforcement outcomes did not leak into an earlier decision and each governance impact is counted only once.
- Preserve `broker_execution_allowed=false`; this Skill creates no recommendation or execution authority.

## Workflows

- [`docs/workflows/workflow-governance-management-quality.md`](../../../docs/workflows/workflow-governance-management-quality.md)
- [`docs/workflows/workflow-current-company-research.md`](../../../docs/workflows/workflow-current-company-research.md)

## Prohibitions

- Do not infer fraud, intent or character from an anomaly, allegation, education, nationality, charisma or communication style.
- Do not use a later enforcement outcome in an earlier point-in-time decision.
- Do not double count governance risk across forecast, probability and valuation.
- Do not create recommendation, portfolio, or execution authority.
