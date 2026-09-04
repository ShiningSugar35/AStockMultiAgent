# Current Research Performance & Agent Skills v1

## Scope

This architecture record closes the 2026-08 current-company continuation, request-reuse and method-Skill gap without adding a second Router, state store, evidence model, research-team scheduler or execution ledger. The existing `CurrentResearchAcquisitionService`, `CurrentResearchContinuationService`, `ResearchTeamService`, ObjectStore, SourceSnapshot and checkpoint contracts remain the single deterministic core.

## Measured baseline and result

The pre-optimization continuation path reran the complete five-capability current-acquisition schedule after one resolvable evidence gap. A deterministic request-count benchmark in `tests/unit/test_current_research_productization.py::test_same_request_reuse_reruns_only_failed_capability_and_preserves_lineage` records:

| Metric | Before | After | Change |
|---|---:|---:|---:|
| First acquisition capabilities executed | 5 | 5 | unchanged |
| Continuation capabilities executed after one annual-financial gap | 5 | 1 | **80% fewer** |
| Verified successful capabilities reused | 0 | 4 | +4 |
| Formal schedule/readiness gates removed | 0 | 0 | unchanged |

The benchmark exceeds the development target of at least 30% fewer external calls on a high-frequency named-company continuation. It deliberately measures request count rather than claiming a stable live wall-time percentage: external-source latency varies by network and provider. Reused attempts record `latency_ms=0` and `SAME_REQUEST_VERIFIED_REUSE`; the previous report object hash is an input of the new report, so the optimization remains auditable.

## Verified same-request reuse contract

`CurrentResearchAcquisitionService.acquire(..., reuse_report_artifact_id=...)` reuses an attempt only when all of the following remain true:

1. The previous artifact is a verified `CurrentResearchAcquisitionReport` for the same company and market.
2. Policy hash, planner lineage, lookback and the complete schedule contract match.
3. The prior decision boundary is not in the future, remains within the active automatic-resolution budget and falls on the same Shanghai acquisition-date boundary.
4. The attempt status is `SUCCEEDED`; PARTIAL, FAILED, BLOCKED or missing capabilities always execute again.
5. Every source snapshot exists, was available before the new acquisition start and has an ObjectStore hash that still verifies.
6. All declared schedule dependencies were themselves reusable.

A tampered or missing snapshot therefore produces a cache miss and fresh acquisition, not a false success. Reuse does not bypass current quote freshness, official financial lineage, PIT, conflict resolution or Recommendation Gate checks.

## Same-request continuation

`CurrentResearchContinuationService.run_to_terminal` is the deterministic driver for an Agent-owned external resolver and team executor:

`ACQUIRE → AUTO_RESOLUTION_REQUIRED → REACQUIRE/ADMIT → TEAM_RESEARCH_REQUIRED → READY_FOR_INVESTOR_VIEW | OBSERVATION_ONLY_FOR_INVESTOR_VIEW | NEEDS_USER_INPUT`.

The core owns the total round/time budget, immutable automatic-resolution artifacts, capture verification, acquisition reuse, checkpoints, no-progress detection, Research Team dependencies, readiness status and safety authority. The Agent owns authoritative-source discovery and role judgement. Public evidence gaps remain internal while they are automatically resolvable; user input is permitted only after bounded public-channel exhaustion or for genuinely private material.

The CLI-equivalent recovery path remains durable through `research-current-continuation-start/status/resolve/resume/advance`; `bind` resumes later user-supplied private material on the same lineage. Recovery has three explicit invariants: (1) a successful capture obtained on the configured final automatic round is deterministically reacquired and consumed before round exhaustion can escalate; this consumption does not grant an extra resolver round, and a remaining gap escalates immediately; (2) after a crash or reconnect, an `EVIDENCE_BOUND` task is consumed by `resume()` before any resolver is called again, while only `PENDING` tasks may trigger fresh external research; and (3) raw ingest capture ids and canonical `OfficialWebDocumentCapture:*` artifact ids resolve to the same verified artifact and contribute the same capture object hash to continuation `input_hashes`. Typed `research-team-role-output` and `research-team-task-result` registration remains mandatory. No intermediate state can enable a formal recommendation or broker execution.

## Complexity and redundancy reductions

- The continuation reuses the same ObjectStore/StateStore lineage and passes the previous acquisition report directly into the next acquisition; it does not reconstruct a parallel evidence cache.
- Successful immutable capability results are reused at the acquisition boundary instead of repeatedly validating/fetching identity, market context and corporate-action inputs after an unrelated financial gap.
- Financial-period discovery is skipped when both financial capabilities are verified reusable.
- Research Team work advances only ready DAG tasks and rejects an executor that makes no durable progress, preventing silent loops.
- Unconditional `probe` calls were removed from normal candidate, financial-audit and knowledge-ingest paths. `probe` remains an explicit capability diagnostic, not a per-investor-request tax.
- BaseCase and frozen specialist outputs remain shared inputs. New method Skills do not reread or fork the raw corpus when their upstream typed profile is already available.
- The formal full-market funnel retains policy-bounded breadth (`max_total_seeds=40`, resource-class deep-candidate limits and controlled provider/Agent worker counts) rather than expanding every company into the full institutional DAG.

## Multi-Agent method coverage

The six new canonical Skills bind directly to existing Research Team tasks, output contracts and readiness checks:

| Skill | Existing task/role | Typed output | Formal check |
|---|---|---|---|
| `$macro-policy-regime` | `macro-regime` / `policy-regime` | `MacroRegimeProfile` / `PolicyRegimeProfile` | `MACRO_REGIME` / `POLICY_REGIME` |
| `$industry-value-chain` | `industry-value-chain` | `IndustryValueChainProfile` | `INDUSTRY_PROFILE` |
| `$catalyst-event-research` | `company-catalyst` | `CatalystRiskPack` | `CATALYST_RISK` |
| `$governance-management-quality` | `governance-management-quality` | `GovernanceManagementQualityPack` | `GOVERNANCE_QUALITY` |
| `$investment-red-team` | `investment-red-team` reviewer | `InvestmentRedTeamReport` | `INDEPENDENT_REVIEW` |
| `$model-risk-backtest-validation` | `model-risk-validation` | `ModelRiskValidationReport` | `MODEL_RISK_VALIDATION` |

Each Skill specifies primary-source priority, PIT boundaries, method steps, exact CLI registration, abstention conditions and prohibitions. They are method contracts, not personality prompts. Bull and Bear remain independent contexts; the red team starts only after both are frozen. The model-risk validator cannot promote a model from a single backtest or mutate paper/production weights.

## Prospective admission boundaries vs. development completion

Phase 7/8 forward evaluation and Phase 11/12 Skill efficiency are implemented as disabled-by-default runtime boundaries, not as perpetual software-development work. The active Phase 8 contract remains fail-closed until its configured prospective sample/time/fold/regime gates are genuinely satisfied; production Skill auto-modification/retirement likewise stays disabled without prospective `SkillUsageEvent`/outcome evidence. A lack of real samples therefore means `NOT_ADMITTED`/`INSUFFICIENT_PROSPECTIVE_EVIDENCE`, not “unfinished code”. Unless a dedicated validation campaign is explicitly opened, these sample-accumulation conditions must not remain in `开发计划.md`, and no fixture, historical replay, arbitrary watch target or owner override may be used to manufacture admission.

Shadow-study lookup is scoped to the active configured policy version: a later study created under a different/foreign policy cannot silently replace the current policy’s study in Phase status or Adaptive Edge status. Deterministic efficiency reports are content-addressed and idempotently reused only when type, inputs, lineage and ObjectStore identity all match; repeated identical computation must not fail on report-identity collisions or create a semantically divergent duplicate.

## BJSE stability evidence

The BJSE official reference Provider keeps slow-changing official membership separate from fast-changing quotes. The durable run recorded ten corrected live rounds, each with 339 securities, 17 pages, explicit terminal proof and no duplicate members; latency was 9.057–13.023 seconds with a 9.485-second median. Recorded fault tests cover total drift, duplicate securities, terminal contradiction, malformed/empty pages, official-source failure and route fallback. The official member denominator is never inferred from Web search or a quote subset.

## Quality and safety invariants

The optimization intentionally does not relax:

- three-market Universe completeness and official BJSE denominator proof;
- current quote freshness and independent conflict handling;
- official financial statement lineage and `FINANCIAL_INTEGRITY` coverage;
- Bull/Base/Bear forecast, valuation and market-price anchor;
- independent Bull/Bear, Reviewer, model-risk and Committee tasks;
- historical/prospective PIT and source-availability boundaries;
- paper-order confirmation/fill separation; or
- global `broker_execution_allowed=false`.

A failed formal check ends in an explicit observation-only view after the complete team, not a fabricated formal recommendation.

## Verification commands

- `uv run pytest tests/unit/test_current_research_productization.py -q`
- `uv run pytest tests/unit/test_current_research_continuation.py tests/unit/test_research_team.py -q`
- `uv run pytest tests/unit/test_repo_skills.py tests/unit/test_workflow_docs.py -q`
- `uv run ruff check .`
- `uv run pyright`
- `uv run pytest`
