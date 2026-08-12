# Workflow — Financial Integrity

## When to use

Use for questions about reported financial credibility, cash-flow quality, accounting consistency, audit issues, abnormal ratios, or when formal company research requires certified financial inputs.

Primary skill: `$financial-integrity-audit` with `$evidence-investigation` for unresolved official facts.

## Flow

1. **Bind the company identity and period**
   - Use one explicit A-share identity and requested reporting period.
   - For current research, reuse the exact current instrument identity if available; historical audits require period-appropriate source availability.

2. **Acquire structured financial hints**
   - Run the current/recorded financial source path as appropriate.
   - EastMoney/Sina are `SECONDARY_STRUCTURED`: useful for locating/cross-checking values, never sufficient by themselves for statutory certification.
   - A primary-provider failure should trigger backup-provider fallback when the requested period/table can still be located safely.
   - Never fabricate missing statement scope, currency, unit or period semantics to make a provider parse succeed.

3. **Return to official evidence**
   - Locate the exact annual/quarterly/semiannual report from CNINFO, exchange, issuer IR or other accepted official publisher.
   - Freeze the official PDF/source snapshot before certifying values.
   - The evidence must prove table, consolidated/parent scope, period column, subject, value and unit.

4. **Normalize only proven units and semantics**
   - Convert units deterministically only when the provider/report explicitly states them.
   - Keep quarterly duration semantics explicit; do not silently turn YTD into single-quarter or TTM numbers.
   - Conflicting official values remain conflicts until resolved.

5. **Run the audit**
   - Use `financial-audit` / `financial-audit-status` and existing FinancialIntegrity services.
   - Recalculate balance/cash identities and approved ratios from frozen evidence.
   - Apply industry-conditioned rules only when the company profile supports them.

6. **Classify gaps correctly**
   - Missing official value/document → `NEEDS_INFO`, not zero.
   - Provider schema drift → provider/normalization diagnostic, not an accounting finding.
   - Open official conflict → explicit unresolved conflict; do not choose the more convenient value.

7. **Feed formal research**
   - Only a valid FinancialIntegrity artifact can become the formal financial input of the company/Committee chain.
   - Secondary hints may still support provisional investor explanation when clearly labelled and independently corroborated, but cannot masquerade as certified FinancialIntegrity.

## Output

Explain in investor language: which statements reconcile, cash-flow/earnings quality, material anomalies, whether evidence is official and complete, and what remains uncertain. Do not accuse fraud from a model anomaly alone.

## Stop conditions

- Community content cannot be the source of a reported financial fact.
- Unsupported ratios/peer percentiles/anomaly scores stay unavailable rather than estimated.
- No financial audit can authorize a trade or bypass Committee/portfolio risk controls.
