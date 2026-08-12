# Workflow — Committee & Trade Plan

## When to use

Use after a company research case is sufficiently frozen and the user asks for a formal decision, paper-simulation eligibility, or explicit entry/exit/stop rules.

Primary skills: `$company-deep-research` and `$astock-research-orchestrator`.

## Flow

1. **Require frozen research inputs**
   - Evidence, FinancialIntegrity, BaseCase, specialist/Knowledge deltas, ResearchMemo and institutional model/context must be frozen as required by the current research path.
   - Committee cannot browse, call providers, or create its own evidence.

2. **Resolve exact Committee inputs**
   - Use `committee-input-resolve` with exact registered artifacts.
   - Validate request shape/policy through `committee-schema` and `committee-plan` before durable decision creation.

3. **Create and audit the decision**
   - Run the decision path and `committee-audit`.
   - Formal outcomes retain exact meanings: `REJECT`, `NEEDS_INFO`, `WATCH`, `APPROVE_SIMULATION`.
   - No narrative is allowed to override a hard formal state.

4. **Execution classification is a separate gate**
   - A Committee `TradeProtocol` is only a committee protocol draft.
   - Resolve/freeze TradingClassification only after the required current market/rule/corporate-action evidence is available and auditable.
   - If classification is incomplete, the research view may still be discussed, but an executable simulated protocol is not certified.

5. **Freeze final ClassifiedTradeProtocol**
   - The final protocol must bind the exact DecisionPack, committee protocol and TradingClassification artifact/object hashes.
   - Paper-only eligibility remains distinct from real brokerage execution.

6. **Render entry/exit rules only when supported**
   - Run `uv run astock trade-plan-view <ClassifiedTradeProtocol_artifact_id>`.
   - Explain frozen entry zone, stop, take-profit, invalidation and monitoring rules exactly as available.
   - Scenario price ranges are scenario outputs, not guaranteed target prices.

7. **Optional paper preparation**
   - If the user explicitly wants a simulated order, continue into [Paper Trading](workflow-paper-trading.md).
   - Order direction/quantity/limit cannot be inferred from free-text narrative when the formal protocol does not specify them.

## Output

For investors, show the verdict, confidence/evidence maturity, key thesis/counter-case, scenario range and actionable rules. Internal artifact identities are shown only for audit/debug requests.

## Stop conditions

- `REJECT / WATCH / NEEDS_INFO` are hard stops for opening a new simulated position.
- Missing exact classification blocks final executable protocol, not necessarily the underlying company research explanation.
- No real broker API or automatic real order exists.
