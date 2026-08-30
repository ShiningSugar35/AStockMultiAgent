# Research specialist Skills crosswalk

Date: 2026-08-29

This document binds each specialist research-team role to one canonical Repo Skill. The
configuration supplies deterministic DAG dependencies and typed-output admission; the Skill
supplies the Agent's evidence-search and research method. Neither layer is sufficient alone.

| Team role | Canonical Skill | Primary method anchors | Required hand-off | Formal gate |
|---|---|---|---|---|
| `macro`, `policy` | `.agents/skills/macro-policy-regime/SKILL.md` | Official data vintages; central-bank/statistical/ministry documents; IMF/BIS/OECD method context | Separate typed macro and policy outputs with regime facts, policy stage, transmission and scenarios | Material exposure or policy stage unresolved |
| `industry` | `.agents/skills/industry-value-chain/SKILL.md` | Official/issuer operating data; value-chain and profit-pool analysis; unit-economics reconciliation | Typed industry output with market boundary, capacity/demand, competitive structure and company KPI bridge | Market identity, capacity or peer comparability unresolved |
| `catalyst` | `.agents/skills/catalyst-event-research/SKILL.md` | Official event lifecycle; expectation baseline; event-study design and confound controls | Typed catalyst output with stage, timing, probability, causal bridge, priced-in limits and falsifiers | No official event object, expectation baseline or transmission channel |
| `governance` | `.agents/skills/governance-management-quality/SKILL.md` | G20/OECD governance principles; applicable CSRC/exchange rules; issuer control/audit/transaction records | Typed governance output with control, oversight, incentives, disclosure, related-party and capital-allocation mechanisms | Controller, audit, cash leakage or minority-treatment risk unresolved |
| `reviewer` | `.agents/skills/investment-red-team/SKILL.md` | Independent reproduction; base rates; alternative explanations; pre-mortem and kill criteria | Typed `InvestmentRedTeamReport` after separate frozen `bull-case` and `bear-case` outputs with distinct contexts | Critical fact not reproduced, independence not proven, or material downside outside the admitted range |
| `model-risk` | `.agents/skills/model-risk-backtest-validation/SKILL.md` | Fed SR 26-2 risk-based model governance; NIST AI RMF; PIT/leakage, multiple-testing, cost/capacity and stability validation | Typed model-risk output with reproduction, leakage, design, stress, monitoring and fallback verdict | Leakage, non-reproducibility, unrealistic costs/capacity or unstable holdout |

## Runtime binding

1. The current-company workflow starts or restores one `CurrentResearchContinuation`.
2. The Agent automatically resolves public official-evidence gaps and freezes exact documents.
3. `ResearchTeamService` exposes only dependency-ready tasks.
4. The Agent loads the canonical Skill for each ready role, executes it against frozen artifacts,
   and registers the exact typed output.
5. The committee reads only admitted typed outputs. It cannot bypass a specialist role or consume
   unregistered free-form prose.
6. A complete team may finish as `READY_FOR_INVESTOR_VIEW` or
   `OBSERVATION_ONLY_FOR_INVESTOR_VIEW`; intermediate states block an investment conclusion.

## Source and implementation boundary

The method-source catalogue is
`docs/scouting/2026-08-29-authoritative-agent-investment-research.md`. Search and secondary
material remain discovery/context. Python owns deterministic identity, PIT, budgets, schemas,
lineage and gates. The Agent owns discovery, interpretation and research judgement. Broker execution remains disabled.
