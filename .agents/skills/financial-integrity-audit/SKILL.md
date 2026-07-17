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

Validate a `FinancialAuditRequest` and run `uv run astock financial-audit <request.json>`. Produce `FinancialIntegrityEvidencePack`; missing, future, conflicting, weakly sourced, or insufficient model inputs must produce `status=NEEDS_INFO`, explicit evidence gaps, and manual tasks rather than fabricated numbers.

M3.1 statement reconciliation, M3.2 cross-period/peer/score calculations, and M3.3 robust Z-score, Isolation Forest, and PyOD ECOD are implemented. Describe an anomaly model as executed only when the evidence pack contains its frozen dataset and versioned model artifact; otherwise report `AVAILABLE_M3_3_NOT_REQUESTED` or the explicit data gap.

## Prohibitions

- Do not infer fraud from an anomaly score.
- Do not use a later enforcement action as an earlier historical input.
- Do not change the ledger or a risk hard block.
