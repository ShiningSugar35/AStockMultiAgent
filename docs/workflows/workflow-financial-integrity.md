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
   - A primary-provider failure should trigger capability-routed backup-provider fallback when the requested period/table can still be located safely; provider names must not be a deep-research admission condition.
   - A strictly research-safe PARTIAL financial pack may keep a candidate in bounded investigation, but cannot become COMPLETE, precise valuation input or formal recommendation evidence. The research-team gate accepts `FINANCIAL_INTEGRITY=true` only from an ObjectStore-verified typed `FinancialIntegrityEvidencePack` with `status=SUCCEEDED / coverage_status=COMPLETE`; when that check is false, precise `VALUATION=true` must also be rejected.
   - Never fabricate missing statement scope, currency, unit or period semantics to make a provider parse succeed.

3. **Return to official evidence**
   - Locate the exact annual/quarterly/semiannual report from CNINFO, exchange, issuer IR or another registered official publisher. When CNINFO is used for exact report recovery, enumerate all pages and fail closed on duplicate/truncated/ambiguous/terminal pagination; a first-page miss is not negative proof. After a CNINFO `ProviderError`, record that failure once and continue through an approved official exact-item path rather than repeatedly striking the same failed capability.
   - Freeze the official PDF/source snapshot before certifying values. The financial release must persist typed `official_lineage_kind`, all `official_lineage_snapshot_ids`, and `official_exhaustive_proof_allowed`; every referenced snapshot/object must verify.
   - `CNINFO_EXHAUSTIVE_ENUMERATION` may support full-page proof. `OFFICIAL_WEB_EXACT_ITEM_ADMISSION` proves only the known admitted report and must keep exhaustive proof false.
   - The evidence must prove table, consolidated/parent scope, period column, subject, value and unit.

4. **Normalize only proven units and semantics**
   - Convert units deterministically only when the provider/report explicitly states them.
   - Keep quarterly duration semantics explicit; do not silently turn YTD into single-quarter or TTM numbers.
   - Conflicting official values remain conflicts until resolved.

5. **Run the audit**
   - Use `financial-audit` / `financial-audit-status` and existing FinancialIntegrity services.
   - Recalculate balance/cash identities and approved ratios from frozen evidence.
   - Apply industry-conditioned rules only when the company profile supports them.

6. **Classify gaps by actual accounting obligation and report period**
   - Missing an official report, invalid issuer/period/statement unit, unusable source lineage, a required core identity field, or a disputed official value remains `NEEDS_INFO`; it is never zero or a presumed PASS.
   - The active versioned `configs/financial_rules.yaml` owns each rule's applicability. `conditional_missing_periods` plus optional `conditional_missing_fields` specify report-period-specific *nonblocking absences*, not which facts an LLM may fabricate. When a conditional field is not verified, emit `NOT_EVALUATED` with `not_evaluated_fields` and an explicit reason; this does **not** assert that the issuer omitted the disclosure, and is not a successful check. Verified inputs always run the original formula, including high-severity accounting discrepancies. An explicitly requested rule always requires its own inputs.
   - Balance-sheet and cash-flow identities retain strict numerical reconciliation when all required verified inputs are available; missing a core field is not silently replaced by zero or waived. Descriptive ratios (e.g. receivables, inventory, prepayments) may remain unevaluated when they lack an optional input; this cannot suppress other factual gaps.
   - Critical identities are derived from versioned rule metadata (`default_enabled` + `IDENTITY` + `HIGH`), not security-specific names. All currently registered industries, including `RESOURCE_MINING`, must retain these equations; a profile exclusion or conditional-missing switch cannot turn a high-severity identity into `NOT_APPLICABLE`/`NOT_EVALUATED`. This cross-rule schema check concerns only the indispensable accounting equations; report-period-specific supplementary checks and nonbinding descriptors remain adaptive. The v3 rule-registry identity causes recomputation instead of silently reusing an older decision under changed applicability.
   - Provider schema drift or a changed PDF layout is a provider/normalization diagnostic, not automatically company misconduct. Use `configs/financial_field_mappings.yaml` with explicit **reviewed exact official-label aliases**, including their original value/period/unit evidence, rather than writing an ever-looser regex. Aliases are distinct from fuzzy search: conflicting label matches, duplicate equivalent rows, wrong currency or scope remain unadmitted. The dividend cash-paid field cannot accept generic dividend-plus-interest aliases.
   - An open genuine official conflict remains unresolved; do not choose the convenient value. A secondary-vendor-to-official disagreement remains a source quality review signal even when the official number is certified and used for deterministic equations.

7. **Feed formal research**
   - Only an ObjectStore-verified `FinancialIntegrityEvidencePack` with `status=SUCCEEDED / coverage_status=COMPLETE` and the separately required typed accounting-quality evidence can pass the formal financial input gate. COMPLETE here means that *applicable required checks* have no unresolved blocking gaps, **not** that every optional ratio, footnote or anomaly method ran.
   - An LLM may identify missing sources, propose a new field synonym or choose an industry-specific research method, but may not declare a source verified, change the financial period, override a real accounting discrepancy, or publish a recommendation without the existing independent research/Committee/Portfolio publication conditions.
   - Secondary hints may still support provisional investor explanation when clearly labelled and independently corroborated, but cannot masquerade as certified FinancialIntegrity.

## Output

Explain in investor language: which statements reconcile, cash-flow/earnings quality, material anomalies, whether evidence is official and complete, and what remains uncertain. Do not accuse fraud from a model anomaly alone.

## Stop conditions

- Community content cannot be the source of a reported financial fact.
- Unsupported ratios/peer percentiles/anomaly scores stay unavailable rather than estimated.
- No financial audit can authorize a trade or bypass Committee/portfolio risk controls.
