---
name: astock-research-orchestrator
description: Route broad or multi-step A-share research requests across candidate discovery, company research, persisted local holdings, portfolio transition/hedging, paper orders/fills, evidence, and recovery. Use for natural-language questions about a company, whether a stock is worth buying, planned-purchase portfolio completion, holding changes, entry/exit planning, stock recommendations, or any task spanning more than one project skill.
---

# A股研究总控

1. 对**每一个投资类请求**先恢复用户态：若 paper account 存在，先 `local-portfolio-sync-paper`；随后读取 `local-portfolio-status`、`portfolio-local-snapshot` 和 `continuous-monitor-status`。对现有持仓先由 `$holding-monitor` 消费 material delta，对 watched target 由 `$continuous-investment-monitor` 消费增量；不要把排队 task 当成已完成分析。
2. 如果用户在当前消息中明确陈述一笔**已经发生的外部交易**，并同时给出可唯一识别的标的/市场、买卖方向、数量、价格和实际成交时间，必须在同一轮先通过 `$holding-monitor` 的 `portfolio-import-declared-trade` 路径落入本机事实源，再继续投资分析。若缺字段，只询问真正缺失的成交事实；不得用当前价格或提问时间替代。已经落库的购买时间/数量/成本后续不要重复询问。
3. 若 open simulated order 跨越离线区间，按需补 `60m` 并执行 `paper-replay`；只有路径歧义才使用 5m。submitted order 不是 position，只有 fill 才改变模拟持仓。
4. 解释用户意图并路由：
   - named company / “现在能不能买” → `$company-deep-research`；
   - **准备买 X，同时希望配其他股票/对冲/组合** → `PLANNED_PURCHASE_PORTFOLIO_COMPLETION`，由 `$portfolio-manager` 走 `workflow-portfolio-transition-and-hedging.md`；
   - **已有持仓要不要加仓/减仓/退出/调组合** → `HELD_POSITION_REBALANCE_REVIEW`，由 `$holding-monitor` + `$portfolio-manager` 走 `workflow-holding-rebalance-decision.md`；
   - broad “推荐几只/有什么好买” → 必须先 `research-team-plan` + 全市场 Research Team，`$candidate-scan` 只是 blind discovery；
   - accounting credibility → `$financial-integrity-audit`；evidence gap → `$evidence-investigation`；paper recovery → `$paper-trading-recovery`；技术侦察 → `$research-tech-scout`。
5. Planned-purchase 组合补全不是新的荐股捷径。先确认 anchor X 的 current formal result 仍成立，再分析 `CURRENT → ANCHOR_ONLY` 增加的风险 gap；股票互补候选仍只能来自可证明 Universe/ResearchSeed/Candidate → 公司深研 → Committee。不能从 Web/新闻临时挑一只“负相关股票”代替正式候选链。
6. 组合/持仓请求必须恢复或确认用户约束：可用资本/现金、投资期限、最大总暴露、单股/行业/回撤/流动性/换手和锁定持仓。缺关键约束时允许风险诊断和条件方案，但不得伪造总资产、现金或风险承受。具体数量区间只有在 NAV、价格和工具交易单位都确定时生成。
7. 组合里的“对冲”用语必须通过 `$portfolio-manager` 的正式分类。低相关只能叫分散化；`NATURAL_HEDGE` 需要机制证据、PIT 正常/压力期复算和已验证成本。当前 long-only 股票/ETF 链不支持 `EXPLICIT_HEDGE`；ETF 可做正式研究/组合评估，并具备独立但默认关闭的 paper execution policy，只有精确有效的 instrument rule + 既有确认/replay 链才可模拟；融券/期货/期权/真实券商均不可用。
8. 用户明确指示模拟买入/卖出/加减仓时，其指令覆盖模型意见但不覆盖市场机械约束：现金/可用股数、交易单位、可交易状态、价格限制、账户确认和 replay 必须成立。AI 主动模拟下单仍要求正式研究允许模拟、typed entry 已满足和本地自动模拟设置；下单不等于成交。
9. 不要在每个投资问题前跑 `probe`。`probe` 仅为开发/恢复诊断；完整 SQLite 体检也只在显式诊断/发布门执行。
10. Existing stable discovery/execution-readiness route remains: `research-seeds --live` → `research-seeds-promote ... --live` → Candidate；`RESEARCH_READY` 仍无 BUY 权威。`JuglarCycleStageSkill` 等 audited Skills 只能影响 bounded research priority。正式执行条件继续由 `ClassifiedTradeProtocol` 和 `trade-plan-view` 解释，不能被 portfolio/holding 新链绕过。
11. Broad current stock-picking 继续遵守全市场 workflow：Research Team → current Universe/seeds → blind tranche → bounded Expert overlay → company Fundamental/Financial/Catalyst/Market → Valuation → independent Bull/Bear → Reviewer → Committee → Portfolio → `FullResearchInputReadiness` → Mandatory Research DAG → `RecommendationResearchReceipt` → Publication Gate。工程 `coverage_ratio >= 99.5%` 只能说明高覆盖；正式全市场推荐还要求 XSHG/XSHE/BJSE 均有 ObjectStore 可验证的 `UniverseCoverageProof` 且达到 `OFFICIAL_DENOMINATOR_RECONCILED`。Universe 不可证明或只有二级源自报覆盖时 fail closed，不可用 Web/news 人工补股票名单。
12. 新 current named-stock opinion 先 `research-acquire-current <company_id> --market <market>`。CURRENT 只要求本轮 bounded acquisition/recovery 结束后冻结的最新可信事实、来源 lineage、财务/基本面和最终发布门；不得用用户提问时刻做 anti-lookahead cutoff，也不得要求旧 reference release 继续保持历史 PIT head。历史模式如存在，必须独立显式启用，不能反向约束 CURRENT。Planner 只能改变 optional work，不能删掉公司质量、财务/治理、估值、风险或 Publication Gate。
13. **任何 CURRENT `NEEDS_INFO` 都先视为内部恢复信号，而不是用户终态。** 先按缺口语义自动处理：对象/registry/schema 漂移 → 从可信源重抓并重建 canonical artifact；结构化 provider 覆盖不足 → 耗尽 allowlisted fallback、transient retry、validated Recovery/SchemaRepair；公开事实缺失或冲突 → `$evidence-investigation` 做 bounded authoritative Web 多源交叉验证并把结果捕获回既有 Evidence/ObjectStore。修复后回到原节点重跑并继续同一请求。只有公共自动恢复预算和权威 Web 都真实耗尽，且剩余信息只能来自用户私人事实时，才可一次性请求必要资料；provider 名称和后台故障不进入 INVESTOR_MODE。
14. 正式公司研究使用机构级基本面链：evidence sufficiency → industry/company economics → driver tree → Bull/Base/Bear forecast → valuation/sensitivity → decision context → BaseCase → bounded Specialists/Knowledge → committee。Forecast/Valuation 数值由 deterministic Python 复算。
15. 分离 research sufficiency 与 execution readiness。交易规则/公司行动细节可以阻断模拟执行但不一定阻断中长期基本面结论；精确 entry/exit mechanics 只使用 typed TradePlan，不猜价格。
16. 对 material formal decision 保持 registered/audited path；Committee 不联网找证据。组合 transition、holding proposal、hedge effectiveness 都是只读研究工件，不直接写 paper ledger 或真实券商。
17. Prospective 方法比较继续遵守 Phase 7/8。一次回测或一个更高 Sharpe 不自动改变 allocator、Skill 权重或 paper ledger。
18. 项目任务结束后按 AGENTS 记录恰好一个 `agent-observation-register` 观测；普通运行不自行伪造 expected labels。
19. Build every user-visible investment answer from one canonical `ResearchNarrativeBundle` and render it through `ResponseGateway`. Keep `research-investor-view` and `research-acquisition-investor-view` as stable machine JSON; use `research-public-view` and `research-acquisition-public-view` for audited public output. Only affirmative diagnostic intent or an explicit mode may enable Developer Mode. Negated diagnostic requests, ordinary “why” questions, and system error text remain Investor Mode. Length reduction may remove only non-critical reasons; mandatory content must survive or use a no-echo safe fallback.
20. **Same-request terminal contract:** 所有荐股、买卖判断、持仓处置和组合决策统一归一为 `FULL_RESEARCH_RECOMMENDATION`，不得把 Seed、Candidate、acquisition gap、team nonterminal、未完成 Committee/Portfolio、上游 `FullResearchInputReadinessReport` 或未封存的研究结果当作用户终局。必须继续调用本 Skill 路由出的全部适用 Skills，直到 `InvestmentRequestClosurePolicy` 为当前 canonical capability plan 返回 `READY_FOR_INVESTOR_VIEW`，且最终 `RecommendationResearchReceipt + Publication Gate` 允许发布；若返回 `CONTINUE_AUTOMATICALLY`，继续本轮工作而不是回复“下一步可以继续……”。只有公共自动恢复预算与权威 Web 都真实耗尽且缺口只能来自私人输入时，才可停止为 `NEEDS_USER_INPUT`，且此时不得形成 BUY/组合建议。
21. broad recommendation 的 bounded shortlist 不是最终答案。每个入围标的都必须在同一请求中进入 `$company-deep-research`，之后再完成 cross-name Red Team / Committee / `$portfolio-manager` / Mandatory Research DAG / `RecommendationResearchReceipt` / Publication Gate；被淘汰标的也要保留简洁的淘汰理由，避免幸存者偏差。用户资本/NAV 未知时采用明确标注的模型组合假设并允许持有现金，不得把缺失的真实账户事实伪造成已知；只有资本、价格和交易单位具备有效证据时才生成合法交易数量。

## Workflows

- [`docs/workflows/workflow-full-market-research-team.md`](../../../docs/workflows/workflow-full-market-research-team.md)
- [`docs/workflows/workflow-current-company-research.md`](../../../docs/workflows/workflow-current-company-research.md)
- [`docs/workflows/workflow-portfolio-transition-and-hedging.md`](../../../docs/workflows/workflow-portfolio-transition-and-hedging.md)
- [`docs/workflows/workflow-holding-rebalance-decision.md`](../../../docs/workflows/workflow-holding-rebalance-decision.md)
- [`docs/workflows/workflow-candidate-discovery.md`](../../../docs/workflows/workflow-candidate-discovery.md)
- [`docs/workflows/workflow-holding-monitoring.md`](../../../docs/workflows/workflow-holding-monitoring.md)
- [`docs/workflows/workflow-paper-trading.md`](../../../docs/workflows/workflow-paper-trading.md)
- [`docs/workflows/workflow-portfolio-construction.md`](../../../docs/workflows/workflow-portfolio-construction.md)
- [`docs/workflows/workflow-prospective-evaluation.md`](../../../docs/workflows/workflow-prospective-evaluation.md)
- [`docs/workflows/workflow-adaptive-edge.md`](../../../docs/workflows/workflow-adaptive-edge.md)
- [`docs/architecture/public-response-contract-v1.md`](../../../docs/architecture/public-response-contract-v1.md)

## Compatibility and hard contracts

- Stable commands remain invoked as `uv run astock ...`; current named-stock acquisition uses `uv run astock research-acquire-current`. Current research is **not** cut off at the user's question timestamp/question-time; it freezes after the bounded acquisition round according to `current-research-policy`.
- For material registered work use `uv run astock codex-run-init --require-registered-output` and `uv run astock codex-run-audit`. Committee routing still uses `committee-input-resolve / committee-plan / committee-decide / committee-audit`. **The committee never performs the search itself**.
- Automatic current-research fallback keeps `ProviderRecoveryProposal` and raw-first `SchemaRepair`/Schema Repair. Only after allowlisted automated/provider paths **and** authoritative Web search are exhausted may one manual checklist be requested. Provider/retry/artifact details are internal and never become the investor answer; normal output must pass `research-investor-answer-audit`.
- Do **not** run `uv run astock probe` before every investment question.
- Phase 7/8 isolation remains unchanged: `ELIGIBLE_RULE_STATE_MACHINE_RESEARCH` → `AWAITING_EXPLICIT_RULE_RESEARCH_APPROVAL` → explicit rule-research approval. **Do not change weights or the paper ledger** from shadow/adaptive results.
- Formal WATCH/APPROVE_SIMULATION targets continue to enroll the continuous monitor with reason `RECOMMENDED`.
- **A submitted order is never called a position until the fill ledger confirms it**.

## Output

Lead with the investment answer, not the process. 对 material investment request 输出一份**完整、一次性、闭环**的投资者报告：**主体/排序 → 结论与强度 → 当前价格与估值/赔率 → 公司质量与盈利驱动 → 行业/宏观位置 → 财务完整性与治理 → 催化与时点 → 独立 Bull/Bear/Reviewer/Red Team 的决定性分歧 → Committee/交易分类结果的自然语言翻译 → 组合权重/风险预算 → 入场条件 → 退出/止盈止损/基本面失效条件 → 数据截止与必要引用**。组合任务额外给候选淘汰理由、相关性/集中度和 CURRENT/ANCHOR/TARGET 差异，持仓任务额外给 HOLD/ADD/TRIM/EXIT；只有一般性非决策事实问题才压缩为 2–4 个理由。正常回复必须经过 `ResponseGateway` 和 investor-answer audit；压缩只能删除非关键理由，强制内容超预算时安全降级，不展示或回显 CLI、artifact、Schema、reason code、task/daemon、数据库细节或被拒绝草稿。

## Prohibitions

- Do not ask for persisted trade facts again unless the local state is missing/corrupt or the user reports a correction.
- Do not stop at the first provider failure when an automatic fallback or bounded authoritative Web search is available.
- Do not expose internal Agent/committee/runtime vocabulary, artifact/hash, SQL, provider diagnostics or command transcripts in a normal investor answer.
- Do not weaken current-source provenance, identity, or freshness checks. Historical anti-lookahead/PIT-head constraints apply only to explicit HISTORICAL/replay work and must not be imported into CURRENT recommendation as request-time cutoffs.
- Do not turn a Candidate directly into a recommendation, portfolio weight or filled position.
- Do not call low historical correlation an explicit hedge.
- Do not treat an unfilled simulated order as a holding.
- Do not claim ETF paper execution is generally enabled: it remains default-off and requires a valid independent instrument rule plus the existing confirmation/replay chain. Shorting, futures, options, margin and real-broker execution remain unavailable.
- Do not create or send a real brokerage order.
