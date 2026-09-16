# Adaptive public-data recovery and impact-based validation v1

## Ownership and scope

WP-27 changes the recovery boundary, optional research-source surface and development validation policy. It does not change WP-26 financial certification, full-research assembly, account state, historical PIT, Committee/Publications or broker execution permissions. Canonical components remain `CurrentResearchContinuationService`, `ProviderFactory`, `SourceAccessRouter`, `SourceCircuitBreaker`, `ObjectStore` and `StateStore`; there is no second provider registry or evidence database.

## Failure taxonomy and actual runtime changes

Public acquisition exhaustion and private missing input are distinct facts. New continuations use `PUBLIC_DATA_UNAVAILABLE` after their bounded public recovery is exhausted. They retain unresolved tasks and acquired evidence, set no private-material requirement and emit no manual actions. `NEEDS_USER_INPUT` remains available for explicit private material or user authorization. The schema still reads legacy v1 checkpoints; their immutable stored hashes are not rewritten, and the status view no longer authorizes a private-material request solely because a legacy checkpoint exhausted public recovery.

The automatic deadline now starts when acquisition actually starts, not when the user originally asked the question. A smaller positive budget permitted by the request schema is accepted consistently on start and resume; an above-policy budget is rejected before acquisition. Resolver transport timeouts/network errors produce a typed, sanitized attempt and continue within remaining rounds. Programming errors and invalid evidence bindings are not treated as successful recovery. The existing final-round evidence-consumption and real private-material continuation tests remain applicable.

`InvestmentRequestClosurePolicy` now distinguishes exhausted public evidence from `CONTINUE_AUTOMATICALLY`; an exhausted public attempt does not endlessly request another graph run. Formal recommendation remains blocked until its own required facts and publication gates pass. Public report normalization translates the new state rather than leaking the enum.

## Gate inventory, review coverage and dispositions

`scripts/audit_gate_inventory.py` scans Python exceptions/assertions/conditionals/pattern matches/schema declarations (including qualified decorators and fields), policy/config/workflow/skill terms, SQL constraints, CI workflows and root test configuration. Unreadable files and parse errors are explicit; symlink/junction traversal is excluded. It records path, line and source hash; it does not read runtime objects, user state or secrets. Candidate sites include ordinary branches and nonblocking declarations, not just genuine gates. Default output intentionally remains `semantic_audit_complete=false`. For closeout, the optional project-local `--semantic-review-index` binds every candidate site to a successful independent whole-file review receipt by exact file SHA. `semantic_audit_complete=true` is possible only when every candidate-bearing file has an exact-hash review, all review findings are explicitly adjudicated and the scan has no errors; a changed file becomes `STALE_FILE_REVIEW`, and open findings become `FILE_REVIEWED_FINDINGS_OPEN`. This closes review provenance per candidate site without pretending that static review proves every runtime branch combination.

| Domain / gate family | Decision and reason | Evidence / remaining work |
| --- | --- | --- |
| Current acquisition continuation, budgets and callback transport | Repair premature escalation; keep bounded retries and exact evidence binding | Regression demonstrates old failures and the new public/private distinction |
| Investment closure after exhausted acquisition | Stop empty looping, preserve missing-evidence facts and block unsupported conclusions | Exhausted-public closure regression |
| Provider setup, permissions, health and quota | Missing optional setup skips that route; capability-level circuits remain; another source or official Web comes next | Factory, adapter, source-resilience and negative tests |
| Source completeness, identity, corporate actions and historical PIT | Preserve exact identity/coverage/time semantics; recovery must acquire actual proof, not waive the gate | Existing market-reference/source-router/negative suites; no global relaxation |
| Financial scope/currency/period/accounting identity | Preserve mathematical and documentary proof; optional schema compatibility belongs to the separately owned WP-26 | Do not duplicate or override the concurrent financial fix |
| Committee, recommendations, portfolio, accounts, paper execution | Preserve publication receipts, consent and execution boundaries; missing public upstream inputs route to acquisition | No automatic credential/account/broker writes; unchanged formal permissions |
| Knowledge, books, documents, evidence and semantic retrieval | Preserve source rights, hashes, citations and invalidation; incomplete discovery is not certified coverage | Whole-file gate families are independently reviewed; runtime evidence completeness remains a separate invariant |
| Adaptive proposals and schema repair | Agent chooses query/source/repair proposals; deterministic validators still reject wrong identity, conflicting facts and unapproved code changes | Existing adaptive contract; no arbitrary agent code execution added |
| Reporting, monitoring, operational statuses | Preserve measured evidence, avoid literal backend terminology; public exhaustion must not be called private need-info | Public-state style and affected workflow tests |
| Tests, documentation, migrations and shared infrastructure | Risk determines required checks; actual impact determines breadth; unknown impact escalates explicitly | Versioned selection policy and selector negative tests |

Per-site closure is provenance-based rather than a second hand-maintained classification table: each candidate site inherits only the exact-hash whole-file semantic review that actually inspected its surrounding control flow, validators and thresholds. This avoids 31k duplicated labels drifting away from code. Static closure still cannot prove every dynamic fallback, compound invariant or external service; those remain subject to negative tests, integration checks and controlled live evidence. Further verified defects should be repaired at their originating boundary with a red/green regression, rather than by replacing every `raise`, `NEEDS_INFO` or `fail closed` string.

## Optional source integration and provenance

The Agent can use `research-supplemental-schema` and `research-supplemental-acquire <request.json> --live` when an applicable public-data gap remains. The command uses registry ranking, setup checks, capability circuits and immutable snapshots. Recorded mode is the default, fixture requests are exact-bound and all outputs are marked RECORDED. Live mode stops after the first sufficient hint capture; formal cross-checking is a separate existing stage. Failed or empty captures leave the next action as official Web plus canonical acquisition validation, with no demand for private investor information.

| Source | Implemented path | Important limitation |
| --- | --- | --- |
| BaoStock | Existing `baostock-reference` on canonical identity/calendar/daily routes; reused, not reimplemented | Availability still needs live observation; free access is not a delivery guarantee |
| AKShare | Optional installed package, fixed `stock_zh_a_hist` read-only worker, bounded process lifetime, raw SDK-return envelope plus normalized unadjusted daily hints | Origin is **EASTMONEY**, not an independent second source; M06 production-backup qualification is unchanged |
| Tushare Pro | Optional `TUSHARE_TOKEN`, HTTPS JSON request to `daily`, explicit source fields and unit conversion | Actual token entitlement/quota required; no account or free entitlement is fabricated, and no insecure HTTP fallback |
| Finnhub | Optional `FINNHUB_API_KEY`, `X-Finnhub-Token` header, bounded global-news leads | Not a claim of certified A-share coverage or free access to every endpoint |

AKShare volume is converted from lots to shares; Tushare volume likewise uses lots and its amount field uses thousands of yuan. Raw extra columns are retained, while exact symbol, requested date interval, finite nonnegative quantities and OHLC relationships are validated. Conflicting duplicate dates fail; identical rows collapse. A capture alone cannot prove exchange identity, a complete trading calendar, source independence or official financial certification. All three new adapters have empty `formal_capabilities`; they cannot enter a formal route just because data were returned.

HTTP JSON/header authentication uses the shared transport, with a one-attempt authenticated profile and redirects disabled. Tokens are not placed in source URLs. A response echoing the supplied credential is rejected before evidence persistence. Transport error metadata records the exception type, not arbitrary secret-bearing exception text. The AKShare worker receives an allowlisted transport/system environment, no unrelated API keys, and project-local temporary/cache paths; it never reads Chrome cookies or executes an Agent-provided program.

Official contracts checked for this implementation:

- AKShare stock history: <https://akshare.akfamily.xyz/data/stock/stock.html> (`stock_zh_a_hist`, unadjusted `adjust=""`, explicit timeout and units).
- BaoStock help: <https://www.baostock.com/helpDocsHome> (anonymous/free API access).
- Tushare HTTP/token: <https://tushare.pro/document/1?doc_id=40>; daily fields: <https://tushare.pro/document/2?doc_id=27>; permissions: <https://tushare.pro/document/1?doc_id=290>.
- Finnhub API: <https://finnhub.io/docs/api> (API authentication, news scope, rate-limit behavior).

Chrome automation was attempted through the authorized project connector, but project Windows control was disabled. No account registration, captcha, paid subscription or authorization change was performed. Optional adapters must be reported as unverified live until actual credentials/access and response validation succeed.

## Impact-based verification

`configs/validation_impact.yaml` is the sole mapping for the explanatory selector `scripts/plan_validation.py`. It maps documentation, current recovery, investment closure, public presentation, sources, financial processing and validation tooling to their relevant tests. Changed tests are included; shared helpers/fixtures without a reviewed domain rule, stale test targets, shared state/migrations/locks or unmapped changes trigger an explicit full-suite reason. Empty selections are errors. The output is a reviewed proposal, not a coverage certificate.

Example: a local financial-source parser edit selects the financial/domain/negative/integration suites rather than every unrelated book, portfolio and paper-trading test. A migration or common object-store change escalates to the full suite. Deterministic shards contain each selected test file exactly once and can be executed by the existing `scripts/run_local_quality.py`; a timeout or missing shard is incomplete, never PASS. Tests are supplemented with affected lint/type checks, controlled live observations and defect-first review. A release label alone does not silently turn a maintenance task into a new product release.

No performance multiplier is claimed from isolated timings: the verified improvement is removal of automatic whole-repository selection for explicitly mapped bounded changes. The selector records both selected and total file counts so breadth can be inspected. Existing verification receipts retain the source-tree hash and terminal result. Concurrent work is not reset, swept into a commit or terminated to create a global clean worktree.

## Rollback and remaining qualification

There is no database migration. Optional sources can be removed from the registry without affecting existing BaoStock or official routes. Public-state compatibility keeps old checkpoints readable; rollback must consider newly created public-unavailable checkpoints rather than rewriting them. The legacy internal method name `_escalate_manual` is intentionally retained to minimize internal-call churn, although new exhausted-public behavior no longer creates manual actions.

Release evidence belongs in the current progress record and durable run, not in this architecture document. A successful fixture test is not a successful live probe; an interrupted independent reviewer is not a PASS; a broad AST inventory is not complete semantic approval. These distinctions remain mandatory at closeout.

## Additional compatibility findings

The existing global CLI is part of an immutable method-adaptation release. Adding unrelated commands directly to that file caused a real `research-plan` regression. The supplemental commands now register through the existing `research/continuation_cli.py` extension point; the global CLI is byte-identical to its baseline, and no old audit/release/hash was rewritten or waived. This reduces coupling rather than weakening method provenance.

The shared quality runner now configures its own output as UTF-8 before printing diagnostics. Previously a non-GBK diagnostic could raise `UnicodeEncodeError` and prevent a terminal receipt from being written. The runner also fingerprints `.agents` bodies so changes to executed Skill contracts are not silently omitted from verification inputs. A global news-lead request does not require an unrelated stock code; equity reference requests still require a resolved security identity.

SDK transport exceptions retain a sanitized transport category rather than being mislabeled as a missing capability. AKShare now receives the same `TransportProfile` as the HTTP transports: the shared lane mapping, total deadline, bounded attempts and child-local proxy environment are reused. Only network/timeouts can move to a fallback lane; quota, access denial and invalid returned data are not routed around. Successful envelopes identify their actual lane/attempt, and the wrapper retains EASTMONEY as its true upstream.

A verified old NETWORK/TIMEOUT probe is no longer an eternal source veto. After the existing circuit-policy cooldown, `ProviderFactory.capability_health_status` returns `NOT_PROBED`, permitting a canonical guarded attempt without rewriting the failed probe or declaring the provider healthy. Corrupt evidence, access denial and capability mismatch retain their checks. Both reference routing and adapter acquisition consume this common factory decision.

Automatic callback results must name the exact task dispatched in the current iteration, not merely another existing task in the same request. Mismatches are rejected before resolution persistence. Bound public evidence still returns through the original continuation and canonical current-node acquisition.

Controlled live observation on 2026-09-16 confirmed that AKShare tried both ENV and DIRECT and still returned transport failures; BaoStock returned `NETWORK_PRECHECK_FAILED` with no rows. A real CNINFO annual-report PDF was then retrieved, hash-verified, registered and bound through the production evidence-binding code. The deliberately replayed acquisition-gap harness is not a full live investment-recommendation run. Tushare/Finnhub remain unconfigured in this environment; no signup, entitlement or live qualification is claimed.

The closeout semantic inventory scans 577 in-scope files, finds 515 candidate-bearing files and 31,716 candidate control sites, with zero scan errors. The exact-hash review index covers 515/515 files and 31,716/31,716 sites; all seven findings emitted across the frozen survey/retry/final passes have explicit adjudications, so the resulting `.ai-bridge/wp27/gate-inventory-reviewed-r2.json` reports `semantic_audit_complete=true`. This is review-provenance closure, not a claim that 31,716 sites are all defects or that static review replaces runtime validation.

## Boundaries confirmed in the family review

`ProductionCandidateInputVerifier` validates already frozen local artifacts; its invalid-object result is not a network failure to hide. Recovery must rebuild the upstream input and rerun that verifier, not accept corrupt bytes. `TradePlanViewService` likewise projects a frozen committee decision at its own `as_of`; a render must not silently introduce newly downloaded prices into that historical snapshot.

The legacy `EvidenceCollectionRunService` is an empty diagnostic collection skeleton, reachable from its legacy CLI but not used by the current canonical Skills. It must not be mistaken for automatic acquisition or forwarded as a request for private user material. Current investor research uses the acquisition/continuation workflow. The stable historical CLI and migration contracts are retained rather than deleted for cosmetic reduction.

DAY orders require their explicit expiry and GTC orders have no expiry in the stored order contract. Those complementary SQL constraints are intentional, not public-data gates. The activation acceptance scenario-ID set is an exact frozen release contract; adjacent Python ranges are disjoint and a set cannot duplicate their members. Historical migration 0054 changes bounded schema constraints on the registered migration chain; the isolated migration postconditions, integrity and foreign keys are checked, without rewriting already applied SQL or certifying arbitrary manually altered databases.

Review suggestions are candidate findings, not instructions to weaken constraints. Each accepted defect receives a reproduction and regression; contextual false positives and unsupported divergent-database hypotheses retain a written disposition. See SQLite's controlled schema-change procedures at <https://www.sqlite.org/lang_altertable.html> for the distinction between a guarded schema operation and an arbitrary schema mutation.
