---
name: financial-integrity-audit
description: Audit financial consistency, recalculated metrics, document conflicts, and industry-conditioned red flags for an A-share company. Use for financial-statement checks, suspicious accounting signals, cash-flow quality, audit opinions, governance concerns, or requests to verify whether reported figures reconcile.
---

# 财务可信度审计

1. Run `uv run astock probe` and `uv run astock context-plan`, then confirm source versions, units, periods, and point-in-time availability.
2. Run deterministic statement identities and metric recalculation before anomaly models.
3. Select the correct industry profile; never apply industrial-company thresholds mechanically to banks or insurers.
4. Bind every finding to evidence or label it as a data gap.
5. Escalate document conflicts; do not label a company fraudulent.

## Output

For M3.1 deterministic audits, validate a `FinancialAuditRequest` and run `uv run astock financial-audit <request.json>`. Produce `FinancialIntegrityEvidencePack`; missing, future, conflicting, or weakly sourced facts must produce `status=NEEDS_INFO`, explicit evidence gaps, and manual tasks rather than fabricated numbers.

M3.1 enables only statement reconciliation, descriptive recalculation, industry exclusions, evidence/PIT gates, and recoverable persistence. Time-series anomalies, peer percentiles, Isolation Forest, and PyOD remain capability-disabled until M3.2/M3.3; never describe them as executed.

## Prohibitions

- Do not infer fraud from an anomaly score.
- Do not use a later enforcement action as an earlier historical input.
- Do not change the ledger or a risk hard block.
