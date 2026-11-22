# Current Research Performance & Agent Skills v1

## Scope

This architecture record closes the 2026-08 current-company continuation, request-reuse and method-Skill gap without adding a second Router, state store, evidence model, research-team scheduler or execution ledger. The existing `CurrentResearchAcquisitionService`, `CurrentResearchContinuationService`, `ResearchTeamService`, ObjectStore, SourceSnapshot and checkpoint contracts remain the single deterministic core.

## CURRENT means current-only forward research

The production recommendation/simulation path is **CURRENT-information-only and forward-looking**. It is not a historical backtest and does not reconstruct what the system could have known at the user's question timestamp. During one investor request the system may continue acquiring, repairing and cross-checking public information; valid data obtained later in that same bounded acquisition/recovery round is current input, not look-ahead leakage.

The CURRENT invariant is semantic rather than historical: identity must match; canonical objects must be readable and internally consistent; sources must remain attributable; conflicting material facts must be resolved; critical financial/governance vetoes and the final Publication Gate remain authoritative. Wrapper-field equality, source-snapshot ordering, an older release remaining the historical PIT head, or a legacy `NOT_PIT_SAFE` marker are not CURRENT stop conditions. Corrupt/missing canonical material triggers refetch/rebuild; public evidence gaps trigger provider fallback and then authoritative Web capture before any user-facing escalation.

The system still freezes one **decision snapshot after acquisition/recovery completes** so the final receipt is reproducible. That freeze is a current-decision boundary, not an anti-lookahead boundary. Historical/prospective modes may retain separate source-availability/PIT rules, but those rules must not leak back into CURRENT recommendation.

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

A tampered or missing snapshot therefore produces a cache miss and fresh acquisition, not a false success. Reuse does not bypass current quote freshness, official financial lineage, PIT, conflict resolution, Mandatory Research DAG or final Publication Gate checks.

## Same-request continuation

`CurrentResearchContinuationService.run_to_terminal` is the deterministic driver for an Agent-owned external resolver and team executor:

`ACQUIRE → AUTO_RESOLUTION_REQUIRED → REACQUIRE/ADMIT → TEAM_RESEARCH_REQUIRED → READY_FOR_INVESTOR_VIEW | OBSERVATION_ONLY_FOR_INVESTOR_VIEW | NEEDS_USER_INPUT`.

The core owns the total round/time budget, immutable automatic-resolution artifacts, capture verification, acquisition reuse, checkpoints, no-progress detection, Research Team dependencies, readiness status and safety authority. The Agent owns authoritative-source discovery and role judgement. Public evidence gaps remain internal while they are automatically resolvable. Public-route exhaustion, resource-budget exhaustion and genuinely private inputs are separate outcomes; only the private scope requests user input. Independent ready branches continue.

The CLI-equivalent recovery path remains durable through `research-current-continuation-start/status/resolve/resume/advance`; `bind` resumes later user-supplied private material on the same lineage. Recovery has three explicit invariants: (1) a successful capture obtained on the configured final automatic round is deterministically reacquired and consumed before round exhaustion can escalate; this consumption does not grant an extra resolver round or network budget; an unresolved gap records the applicable exhaustion reason; (2) after a crash or reconnect, an `EVIDENCE_BOUND` task is consumed by `resume()` before any resolver is called again, while only `PENDING` tasks may trigger fresh external research; and (3) raw ingest capture ids and canonical `OfficialWebDocumentCapture:*` artifact ids resolve to the same verified artifact and contribute the same capture object hash to continuation `input_hashes`. Typed `research-team-role-output` and `research-team-task-result` registration remains mandatory. No intermediate state can enable a formal recommendation or broker execution.

## Complexity and redundancy reductions

- The continuation reuses the same ObjectStore/StateStore lineage and passes the previous acquisition report directly into the next acquisition; it does not reconstruct a parallel evidence cache.
- Successful immutable capability results are reused at the acquisition boundary instead of repeatedly validating/fetching identity, market context and corporate-action inputs after an unrelated financial gap.
- Financial-period discovery is skipped when both financial capabilities are verified reusable.
- Research Team work advances only ready DAG tasks and rejects an executor that makes no durable progress, preventing silent loops.
- Unconditional `probe` calls were removed from normal candidate, financial-audit and knowledge-ingest paths. `probe` remains an explicit capability diagnostic, not a per-investor-request tax.
- BaseCase and frozen specialist outputs remain shared inputs. New method Skills do not reread or fork the raw corpus when their upstream typed profile is already available.
- The formal full-market funnel retains policy-bounded breadth (`max_total_seeds=40`, resource-class deep-candidate limits and controlled provider/Agent worker counts) rather than expanding every company into the full institutional DAG.

### Campaign Q3 readiness: reuse configuration, never reuse certification verdicts (2026-11-22)

`WatchCampaignService` now constructs its existing `FinancialSourceService` lazily once per service instance, instead of reconstructing provider/config objects for each missing-or-certified member in the `finalization_readiness()` loop. A new CLI invocation still gets a new service. Each member still goes through the sole `read_certified_release()` verifier, repository read, manifest identity, official lineage and immutable Parquet facts/observations; no cache of a `CERTIFIED` judgment or global financial-source singleton is introduced. The same instance is also available to `recover_missing_q3()` without introducing a second scheduler or relaxing the owner lease.

On the same 18-member canonical campaign, isolated real read-only `watch_campaign readiness` calls completed in **60.725s before** and **15.098s after** the change; both returned `q3_ready=true`, the identical set of 18 `q3_release_ids`, `missing_q3=[]` and `FORMAL_PORTFOLIO_REQUIRED`. This is a paired local observation, not a population latency estimate or token claim. The added red/green unit test asserts one service initialization per campaign instance and a fresh initialization for another instance. Platform automation enabled state and a formal research receipt are separate from source readiness; neither is inferred from this performance improvement.

`recover-q3` now takes the complete registered missing-company set without a per-round `maximum_members` argument or a 50-company campaign ceiling. A compact v2 checkpoint records the last actually attempted identity; rotation remains compatible with v1 attempted-member checkpoints and avoids any checkpoint string proportional to the number of companies. Finite wall-time, transport-attempt budget, source circuit/rate limits and generation-scoped owner leases continue to bound physical work. This quantity-limit removal does **not** grant more model-worker concurrency, waive financial certification, resume paused platform tasks, or imply the formal 18-company portfolio is ready.

## Multi-Agent method coverage

The six new canonical Skills bind directly to existing Research Team tasks, output contracts and readiness checks:

| Skill | Existing task/role | Typed output | Formal check |
|---|---|---|---|
| `$macro-policy-regime` | `macro-regime` / `policy-regime` | `MacroResearchOutcome` / `PolicyRegimeProfile` | `MACRO_REGIME` / `POLICY_REGIME` |
| `$industry-value-chain` | `industry-value-chain` | `IndustryResearchOutcome` | `INDUSTRY_PROFILE` |
| `$catalyst-event-research` | `company-catalyst` | `NewsEventResearchPack` | `CATALYST_RISK` |
| `$governance-management-quality` | `governance-management-quality` | `GovernanceAssessment` | `GOVERNANCE_QUALITY` |
| `$investment-red-team` | `investment-red-team` reviewer | `ChallengerAssessment` | `INDEPENDENT_REVIEW` |
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
- no real-broker execution permission field or real-broker execution implementation in the active research contract.

A failed formal check ends in an explicit observation-only view after the complete team, not a fabricated formal recommendation.

## Verification commands

Select affected tests with `scripts/plan_validation.py` from the actual changed paths, then execute through `.venv\\Scripts\\python.exe -B scripts/run_local_quality.py`. Record explicit pytest shards and stable input/owned-worker terminal receipts; coordinate at most two heavy jobs. Lint/type/import checks follow the affected contracts. Full-suite validation is required only for shared infrastructure/dependency changes or an unresolved cross-domain regression; documentation-only changes use the relevant document/Skill contracts.


## Read-only diagnostics and comparison (2026-11-21)

`agent-observability-report` and `research-efficiency-report` inspect an existing registered report by default; `--report-id` selects an exact snapshot. Only explicit `--materialize` generates a report. The lightweight alternative is `python -m astock.diagnostics report AgentObservabilityReport`. Missing state/report, an unavailable registered object, invalid JSON, mismatched identity and mismatched schema are distinct outcomes. Runtime overrides apply to both generation and inspection. Historical report flags describe that snapshot and do not authorize current policy changes.

Inspection uses `StateStore(read_only=True)`: it never creates the runtime or main database, migrates schema, writes business tables, registers policy/artifacts, writes objects/reports, or configures file logging. SQLite may still use or create WAL/SHM protocol sidecars when reading a WAL database; these are not business writes. Do not use `immutable=1` to hide active WAL or delete sidecars. Connections close explicitly. Tests verify committed WAL data remains visible and writes fail.

`research-request-performance-report` and `research-evaluation-report` compute read-only projections of existing request traces. Optional `ResearchResourceUsage` records measured host/provider/local usage; absent token, cost or memory measurements remain UNKNOWN, including paired deltas. Explicit measured zero remains zero. Skill efficiency v2 labels legacy token declarations separately, and its cache identity binds the actual registered Skill registry object as well as policy and usage. Old immutable reports are not rewritten.

`ResearchEvaluationContext` binds case, input/evidence/budget/rubric hashes, code/policy versions, host/model, variant, replicate and evidence mode. Pairing requires matching input, evidence, budget, rubric, host/model, cache mode and source group. RECORDED, LIVE and FAULT_INJECTION observations remain separate. Descriptive groups separate code/policy versions. Callback retries use the same canonical semantic trace identity; conflicting callbacks are excluded. Replicates and changed budgets are not additional independent inputs. The report exposes sample counts, p50/p95, matched deltas and unknown measurements, with `DIAGNOSTIC_ONLY` authority; causal quality still requires independent review.

The optional `configs/validation/golden_research_v1.yaml` selects 15 existing cases from the 68-case business matrix. `scripts/plan_validation.py --golden` binds the source manifest, actual parameterized test node IDs and their hashes. It creates no new fixtures, scheduler or mandatory gate; impact selection and financial/identity/publication negative cases remain authoritative. This recorded slice is not a live model-quality benchmark. The 2026-11-21 production inspection found no registered request traces for paired evaluation; historical observations cannot establish current token savings, latency improvements or research quality.

## Evidence consumption and host continuation

Resource exhaustion records `BUDGET_EXHAUSTED`, while exhausted applicable public routes retain `PUBLIC_DATA_UNAVAILABLE`. Previously bound official evidence can be consumed through the existing acquisition path with `allow_network=False` after elapsed/attempt budgets end. That path issues no provider, identity, market or disclosure requests and does not substitute recorded samples for current evidence. Official certification and release-lineage verification remain required. A private gap applies only to its capability and dependent tasks; it cannot stop unrelated public branches or transfer its scope to a remaining gap after completion.

`HostContinuationPolicy` schedules handoff only from a supplied actual platform deadline or interruption. Legacy 35/40/45-minute settings supply compatibility reserves, never an invented deadline. The optional `investor_orchestration/antigravity_hooks.py` adapter uses the official [Antigravity Hooks protocol](https://antigravity.google/docs/hooks). The existing continuous-monitor Skill and workflow describe installation and binding; other hosts and ordinary current-company continuations do not depend on it.

The installer merges the workspace `.agents/hooks.json` with existing hooks and writes the actual absolute project Python/root, with a bounded timeout. SessionStart records only a validated conversation UUID/workspace and injects that metadata through the documented `injectSteps/systemMessage` response. An explicit bind attaches it to an already owned campaign slot/round/lease generation. Only a fully idle Stop with `max_steps_exceeded` or `error`, matching that original lease, may call the existing PARTIAL checkpoint transition. Normal `model_stop`, unknown reasons, non-idle events and stale bindings do not release ownership, even after a long elapsed time. Stop and rejected input return `{}`; no callback forces another model turn. Transcript text, user prompts and error bodies are neither loaded nor retained. A binding that no longer matches a new round disables only this optional native observation; normal campaign begin/recovery remains available.

Workspace hook installation was actually executed successfully on 2026-11-21 with the project's absolute Python. Protocol fixtures and software entry-point tests are separate from a genuine host event. No native SessionStart/Stop trigger, host deadline, active instruction chain or loaded-Skill token measurement has been observed in this session. Existing leases, cancellation, finite tool budgets and the long-lived watcher remain independent.

## Durable external waves and shared transport limits

`resolve_external_wave` uses the existing SLA task tables and workers, with the original continuation deadline. Only public gaps whose required capabilities are resolved are ready; unrelated ready gaps share the configured NETWORK bound. Workers freeze `CurrentResearchAutomaticResolution:<object hash>` results. The continuation coordinator owns mutation under the existing same-host run ownership lock; it freezes the existing scheduler task/generation identity before dispatch, then validates the official capture, matching generation, operation identity and semantic result hash before one checkpoint binds the evidence. Identical callbacks are idempotent. Conflicting results for one external operation fail, and completed SLA results survive interruption before continuation commit.

A pre-existing RUNNING dispatch or an unconfirmed handler failure becomes `EXTERNAL_COMPLETION_UNKNOWN`. Tasks cancelled before dispatch remain `EXTERNAL_NOT_DISPATCHED`; typed completed failures remain known failures. Lease expiry alone does not replay an unknown outcome. Independent READY branches continue, and completed sibling evidence is consumed locally even after the request deadline without resetting the unknown generation. The caller first reconciles the original operation; a later valid official capture resumes the same continuation even after the time budget. This does not promise exactly-once external requests or zero repeated charges across an unobservable external completion window. The optional native adapter is installed; whether this installed host actually loads and triggers it remains unverified.

`core/request_budget.py` stores request-level physical transport attempts and the absolute deadline in the existing checkpoint table. Reservation happens before transport, is atomic across workers, and cannot be reset by reconstruction or a larger caller allowance. Acquisition, BSE pagination and both seed-pool submissions copy request context into their workers; each physical HTTP redirect/retry and direct Tushare/Web/PDF request and each owned supplemental/Mootdx SDK launch consume the same allowance. SDK-internal requests hidden by a third-party SDK are not individually observable. Fees continue to use the existing Web-tool request/day/month atomic reservation ledger; no second fee counter or retry library was added. Retry-After waits cannot exceed the remaining request/transport deadline. Local evidence consumption remains available after network allowance exhaustion.

The normal `research-current-continuation-start` and `resume` commands now call the shared `advance_current_web` application service. Start may receive `--host-capabilities`; resume may receive `--web-batch` containing actual host receipts and selected originals. An empty start prepares eligible requests and returns an explicit capability declaration requirement without charging a paid fallback. An empty resume still reacquires independent, unbound public gaps within the original budget, including DAILY_MARKET, which cannot be resolved by a financial PDF handler. The old single-original command delegates the same composition.

A prepared SLA task is not a host operation. The Web application binds `external_operation_id` only after verifying a durable cursor for the same continuation, task and frozen dispatch. Missing/stale host capability or no available tool creates no waiting binding. The core's waiting set uses this actual operation identity; acquisition receives `network_blocked_capabilities` for the waiting/unknown capabilities, while independent capabilities may continue and verified successes are reused. Global `allow_network=False` remains authoritative at budget exhaustion. Completed raw originals can still be admitted locally afterward.

Selection waves execute only the explicit currently bound task generations. An A-success/B-waiting partial resume retains B's original operation and generation so a genuine late callback remains valid. Rejected evidence or a known failed operation does not retain A's completed resolution forever: a later allowed acquisition/resolution generation can retry A, while its old callback is rejected and B is not redispatched. The generation key includes frozen acquisition/resolution lineage; an old READY node left in a sibling's earlier run is excluded from new dispatch. Unknown outcomes remain unreplayed until the original operation is reconciled.

### Canonical shared raw acquisition and owned network capacity

`core/network_ownership.py` is the single capacity kernel over the existing checkpoint table. Web Search/Fetch, official PDFs, normal `HttpProviderBase` HTTP, direct Tushare, report-forecast streams, GDELT and official macro HTTP share the configured global/provider/domain limits (currently 4/2/2). A process PID plus creation time, generation and bounded lease owns each slot. An expired lease with a still-live owner remains occupied; confirmed dead transient HTTP owners are reclaimed, including death after an earlier UNKNOWN observation. Ordinary HTTP rows are removed on terminal release. Canonical shared UNKNOWN generations remain durable and cannot be silently retried. There is no new scheduler, database, fee ledger or daemon.

The canonical Web/raw key includes normalized query or URL, capability and purpose, subject and reporting period, as-of scope, source-policy identity, authorization scope, actual wire parameters and freshness policy. Only a single owned transport reserves fees; followers receive separate request/operation/dispatch receipts without copying another request's callback identity or budget. The credential and transport policy used for the key are the same in-memory snapshots used for the physical call; secret values are never persisted. Known successful search raw is bounded by its observation time and the configured five-minute maximum age; official PDF raw has a one-hour maximum age and is rechecked against the current source policy, hash, size and PDF contract. A follower whose newly joined result is too old receives a known NOT_CHARGED stale result, without a hidden paid refresh.

Raw acquisition and interpretation remain separate. Every official-original consumer rebinds its own request/task/generation and reruns current admission/financial processing. Existing immutable bytes can repair a crash before the per-operation raw receipt without another fetch, including local consumption after that request's network deadline when the raw still meets its cache contract. Frozen result receipts repair a later mutable-head failure; a returned or failed local transport releases physical concurrency even when per-request publication failed. Unknown fee outcomes keep their reservation. A verified NOT_CHARGED receipt can settle an unsettled reservation once after owner recovery, and repeated recovery does not refund it again.

HTTPX request hooks check each physical redirect before sending, retain HTTPX's existing redirect/header policy and shrink phase timeouts to the remaining request and transport allowance. Official PDF redirects retain the separate source admission, public DNS and byte checks and transfer the same network slot to the admitted destination domain. These checks do not promise a hard wall-clock cutoff inside arbitrary DNS or streaming library code; the existing owned worker supplies process cancellation. A raw caller-created client without project StateStore is outside this pool. External host Search/Fetch concurrency and the internal connections of the owned AKShare/Mootdx SDK subprocesses are not observable by these HTTP hooks: those SDK launches retain their existing process/worker and logical-call budget bounds, without a claim that every SDK-internal packet is counted.

A registered successful empty Web callback is released only after its original raw response proves an empty container for that exact operation/dispatch. An empty admission list, rejected links, HTTP failure, UNKNOWN outcome or budget exhaustion is not an empty-query proof. The default `advance_current_web` path consumes this result before ordinary resume; independent acquisition can continue, while the existing public-route coverage evaluator alone determines whether all applicable routes have actually been exhausted.

## Process and development boundaries

The remaining AKShare supplemental and report converter subprocess paths now use the same `KillableResearchWorker` through `run_owned_command`: project-local scratch, bounded stdout, absolute deadline, Windows Job/process-group ownership, confirmed reap and own-directory cleanup. An intentionally persistent monitoring daemon retains its existing lifecycle. Local bounded Git inspection is not treated as an external SDK. Job ownership is not a filesystem permission sandbox.

Hard cancellation can bypass the runner's finalizer. Preserve its owner/log as INCOMPLETE when no result exists; do not manufacture PASS or a successful reap receipt. The existing lifecycle service classifies an exact dead owner with RUNNING/no-result metadata as ORPHAN and applies the configured 24-hour `quality_run_tmp` grace. Cleanup runs through the existing continuous-monitor daemon or an explicit `astock storage-lifecycle-auto --apply`; `automation.enabled=true` alone does not prove a daemon or operating-system schedule is running. At development closeout, reconcile owned interrupted runs through that service. Urgent earlier removal requires the same precise owner/tree review; internal links are removed without following their targets. Keep the small failure evidence while reclaiming the owned temporary tree. No extra cleanup daemon, timer or backup tree is needed.

The existing quality runner accepts an optional generated validation plan. It rejects directory, absolute-path and unsupported collection arguments under an impact plan, then recomputes static imports, package initializers, test and root conftests, versioned configuration, migration/fixture directory membership and explicit dynamic-read declarations before and after the command. Unknown reads/imports expand to the full workspace. Receipts distinguish validation-input fingerprints from full workspace snapshots: unrelated documents may change only when the narrower closure is actually known. This is an auditable conservative selector, not observed runtime read coverage or a PASS cache. The normal stable-tree gate remains the default. Full before/after read sets remain in the same result.json; command transport projects their counts and the receipt path so large closures cannot displace failure summaries.

Only Import Linter is added as a development dependency. Its pyproject contracts prohibit core/schema upward dependencies, new provider orchestration imports, and new research ledger/account imports. Six existing exact edges are explicitly named (two process-launcher edges and four historical ledger-operation edges); unmatched exemptions fail. It adds no runtime scheduler. `run_local_quality.py imports --no-cache --no-logo` runs the graph through the owned quality launcher. No Tach or blocking Vulture gate is added; dead-code suggestions remain manual review.

`scripts/check_repository.py` supplies the same blob checks to the staged pre-commit hook and merge-base CI: private credential/runtime paths, private-key material, oversized source, temporary/backup files and illegal paths. It reads the selected index/HEAD blob rather than unstaged content. Legitimate migration, fixture and archive paths remain valid. CI also ratchets Ruff C901 on changed production functions: new functions have a limit of 20, existing over-limit functions cannot increase above the exact merge-base value. The hook configuration is versioned; installing a user Git hook and a successful remote CI execution are separate observations.

References: [Import Linter forbidden contracts](https://import-linter.readthedocs.io/en/stable/contract_types/forbidden/), [Ruff C901](https://docs.astral.sh/ruff/rules/complex-structure/), [uv GitHub Actions integration](https://docs.astral.sh/uv/guides/integration/github/). Recorded fault tests exercise the engineering contracts; they are not native-host or model-quality evidence.

## Target-host observation and optional node-folding decision (2026-11-21)

The official CLI offers a non-inference [`/context` panel](https://antigravity.google/docs/cli/commands/context) for loaded customization/token categories and tracked files, and [`/skills` and `/hooks`](https://antigravity.google/docs/cli/reference) for discovered skills/hooks. This documents an observation path, not a measurement of this installation.

The local inspection found eight Antigravity desktop processes, one with a native window handle, but the authorized Windows UI bridge exposed zero windows. Neither `antigravity` nor `agy` was available through command lookup. The installed application had no public CLI in its root/bin; resources/bin contained only the language server and encoder. No private language-server RPC was guessed, no new model API was used, and no fabricated native event was submitted to claim success. Effective native cwd, instruction precedence, automatic Skill selection and loaded bytes/tokens therefore remain **NOT_OBSERVED**. Repo byte/navigation contracts and installed hook configuration establish only the software layer. WP-69's original native observation criterion is not met by process presence or protocol fixtures.

The node-folding decision for this change is **DO_NOT_ENABLE**. The read-only registered evaluation found zero usable paired native/model trajectories; the protected independent Bull/Bear/Reviewer roles cannot be merged on a static similarity argument. Existing deterministic preprocessing and immutable artifact reuse remain enabled. No paired quality/risk/citation/token/latency/memory experiment was run, and there is no new measured saving or non-inferiority claim. The optional folding experiment is not represented as a completed comparison. A later concrete candidate requires genuine matched-input/evidence/budget trajectories and independent quality review before any DAG switch is enabled; this does not add a permanent Agent or block ordinary research.
