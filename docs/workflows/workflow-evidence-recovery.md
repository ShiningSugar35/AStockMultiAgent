# Workflow — Evidence Recovery

## When to use

Use when one bounded decision-relevant fact is unsupported, conflicting, stale, provider-blocked, or returned as a Committee/Research `NEEDS_INFO` gap.

Primary skill: `$evidence-investigation`.

## Flow

1. **Name the exact gap and decision impact**
   - Do not begin with unrestricted browsing.
   - If this is a committee task, inspect `committee-task-status` and keep the investigation scoped to that reason.

2. **Reuse existing frozen evidence**
   - Check registered snapshots/artifacts and their audit status before fetching again.
   - Resolve snapshot aliases to the canonical source/content identity before establishing downstream foreign keys; preserve each fetch attempt as an append-only observation rather than replacing the canonical source fact.
   - For admitted official reports, reuse by company/period/document type and capability-scoped freshness only when raw/admission/hash/identity/completeness still verify; unrelated plan changes must not force a full refetch.
   - A valid existing source is preferred to duplicate collection.

3. **Diagnose, repair and fallback deterministically**
   - Current-investment recovery uses the active Current Research policy's automatic budget (currently 1800 seconds), not a second Workflow constant.
   - Diagnose whether the failure is transport, rate-limit/access, provider schema drift, request-binding drift, or data-quality failure; preserve that detail internally.
   - SourceAccessRouter ranks automated options by officiality/capability/health/freshness/latency/cost/auth/retryability; strong official evidence retains hard priority and Manual remains last.
   - Retry only retryable transient errors with bounded attempts/circuit breaking. Health and breaker state are keyed by provider/source + capability; an OPEN breaker or active HALF_OPEN claim moves directly to an approved fallback, while a stale claim may be reclaimed without allowing concurrent probes. If adaptive provider recovery is needed, Agent may submit `ProviderRecoveryProposal`, but only `adaptive-recovery-validate` may approve registered, capability-compatible, health-eligible paths.
   - Provider status may be trusted only when pointer → event → artifact → object → typed probe report verifies end-to-end. A damaged pointer/event/artifact/object/report is `UNAVAILABLE/CORRUPT`, never “probably healthy”.
   - Unknown provider schema must remain raw-first. Preserve the SourceSnapshot, then use the Schema Repair path in `workflow-adaptive-edge.md`; AI mapping cannot directly enter formal facts or mutate the active dialect.
   - Do not interpret a provider failure as evidence against the underlying fact. Record separately whether the external request was sent, what the Provider actually returned, and whether resilience handled that result correctly.

4. **Authoritative Web fallback**
   - If local/provider paths remain insufficient, automatically search the Web. Agent/Search may propose a query/domain/URL, but deterministic `SourcePolicyGate` decides capability, authority class and formal admission; the proposal itself is never evidence.
   - Priority: exchange → CNINFO/legal disclosure → issuer IR → regulator → reputable professional sources for corroboration.
   - Prefer pages/files that require no login/API key/user intervention and freeze the exact admitted source into an immutable snapshot before formal use.
   - Discovery/Search cannot prove exhaustive absence. `disclosure.enumerate` / corporate-action negative proof requires a route with complete pagination, terminal proof and complete snapshot lineage; duplicate/truncated/terminal-page contradictions fail closed. An `OFFICIAL_WEB_EXACT_ITEM_ADMISSION` proves only one known admitted document and must never be reinterpreted as exhaustive enumeration. If no exhaustive route is available, remain `ENUMERATION_INCOMPLETE / NEEDS_INFO`.
   - For material contested facts, cross-check independent authoritative sources where practical.

5. **Freeze evidence when an importer exists**
   - When the repository supports deterministic capture/import, store raw source material immutably before advancing the chain.
   - Preserve exact page/section/DOM/snapshot locator and source availability.
   - Web material that cannot yet enter a formal artifact may support a clearly labelled provisional explanation, but cannot silently satisfy a frozen formal gate.

6. **Manual intervention is last**
   - Exhaust provider and authoritative Web recovery before escalation. Request manual user help only for explicitly identified private input or user authorization; a remaining public gap is an unresolved public-data result.
   - Aggregate all remaining requests into one checklist: exact document/action, why it matters, and what will resume after it is supplied.

7. **Close the named gap only**
   - If a new frozen artifact resolves a Committee task, link it with `committee-task-resolve`; the Committee still needs a new formal plan/decision.
   - Do not broaden one resolved fact into a full research rerun unless the change invalidates downstream artifacts.

## Output

Normal investor-facing output should say the resolved fact or plain-language remaining uncertainty and its decision impact. Provider IDs, exception classes, artifact IDs/hashes and command logs stay in diagnostics unless debugging is explicitly requested.

## Stop conditions

- Never bypass access controls, captchas, signatures or rate limits.
- Never call “no search result” proof of nonexistence.
- Never upgrade community content into statutory/official authority.
- Never ask the user to fetch a public authoritative source the Agent can obtain automatically.

## Recovery and optional source supplement

Before returning any public-data `NEEDS_INFO`/unavailable result, the Agent must attempt the existing canonical acquisition, applicable alternate providers, and authoritative Web recovery within the current request budget. Use `research-supplemental-schema` and `research-supplemental-acquire <request.json> --live` for `market.reference.hint` (AKShare/Tushare) or `news.global.lead` (Finnhub) when relevant. Missing optional keys are a source availability issue, not missing investor input. BaoStock stays on the established reference route. Register the returned capture, inspect original-source provenance, and bring verified facts back through the failed canonical node; hint captures are not financial certification, independent EASTMONEY cross-checks, complete Universe proof, or publication permission.

After public recovery is genuinely exhausted, `PUBLIC_DATA_UNAVAILABLE` records a bounded unresolved attempt with no private-material demand. Preserve acquired evidence and describe only the remaining uncertainty. Ask for user input only when it is actually private or requires their authorization. Do not let an unavailable source, incomplete intermediate artifact or exhausted budget trigger an endless full-graph rerun. LLM query/source/parse proposals remain allowed; truth, accounting, historical PIT and execution checks remain deterministic.

Current implementation and validation boundaries: `docs/architecture/adaptive-recovery-and-validation-v1.md`; canonical snapshot/report identity, native document reuse and gate-efficiency specifics: `docs/architecture/evidence-gate-efficiency-v1.md`.
