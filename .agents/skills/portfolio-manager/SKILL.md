---
name: portfolio-manager
description: Evaluate portfolio risk, complete a planned purchase with complementary assets, assess evidence-bound hedge candidates, and build cost-aware A-share portfolio transitions only from formally admitted research. Use for portfolio review, allocation, diversification, hedging, risk contribution, current holdings, or requests to combine a recommended stock with other assets.
---

# 组合构建、风险互补与迁移

1. **先恢复真实用户态**。若 paper account 存在，先 `local-portfolio-sync-paper`；随后读取 `local-portfolio-status`、`portfolio-local-snapshot` 与 `continuous-monitor-status`。未成交订单不是持仓。外部既成交易以本机 `trades.md` 为事实源，SQLite paper ledger 只拥有模拟订单/成交事实。
2. Existing paper-portfolio diagnostics remain available through `portfolio-paper-evaluate`; a `RESEARCH_READY` Candidate still has **no** portfolio-weight authority until the formal company/Committee chain closes.
3. 区分两类任务：
   - **已有组合复核**：从当前持仓、现金约束和最近 review 分析集中度、相关性、beta/因子、回撤、CVaR/CDaR、流动性、实施成本和压力情景。
   - **计划买入 X 后补全组合**：先确认 X 的 current 正式研究仍可用，再比较 `CURRENT → ANCHOR_ONLY → TARGET`。先回答“加入 X 会新增什么风险”，再寻找互补资产，不要先列股票再事后解释。
4. 用户风险/资金约束是输入，不是模型产物。确认或恢复投资期限、可用资本/现金、最大总暴露、单股/行业、相关性、回撤、流动性、换手和必须保留/禁止持仓。关键约束未知时，可以给风险诊断和条件方案，但不得伪造现金、总资产或风险承受能力；数量区间只能在 NAV/价格/交易单位都可证明时给出。
5. 对当前持仓或 planned anchor 运行 `portfolio-evaluate`/既有正式研究链，使用公司行动调整后的 PIT 研究收益序列做组合风险；原始未复权价格仍只用于真实成交/数量换算。不要用除权跳变制造虚假相关或回撤。
6. **风险缺口驱动候选**：根据 concentration / market beta / industry / factor / cycle / liquidity / scenario 等 gap 定义候选特征。股票候选只能从可证明 Universe → ResearchSeed/Candidate → `$company-deep-research`/Research Team 进入，最终成员仍必须是 current Committee/Classification 允许的正式组合候选。Web/新闻可以解释风险机制或核验事实，不能临时手选股票替代 Universe lineage。
7. 候选数量保持 bounded：风险预筛通常 6–12 个，只有最有希望改善指定 gap 的 2–5 个进入完整公司深研。复用同一会话行情、因子、行业和协方差输入，不为每个 allocator 重复抓取。
8. 使用既有四个 allocator 比较稳健目标：`EQUAL_WEIGHT_CONSTRAINED`（生产基准）、inverse volatility（`INVERSE_VOLATILITY`）、hierarchical risk（`HIERARCHICAL_RISK`）、Ledoit-Wolf shrinkage minimum variance（`SHRINKAGE_MIN_VARIANCE`）。没有 prospective 证据前不得把复杂模型升级为默认，也不得用 LLM 直接给精确 expected return 做无约束 Max-Sharpe。
9. 使用 `portfolio-transition` 把“目标组合”转换成“从当前组合应该怎么走”：报告目标权重带、现金、换手、风险变化、交易成本、流动性、交易单位和 binding constraints。目标是区间，不是必须即时回到一个点；处于 no-trade band 的小偏离保持 HOLD。
10. **严格区分分散化与对冲**：
   - `DIVERSIFICATION`：降低非系统性/组合风险，不声称抵消指定风险；
   - `NATURAL_HEDGE`：经济机制 + PIT 历史 + 压力期 + 已验证成本共同证明指定风险净下降；
   - `EXPLICIT_HEDGE`：当前 long-only 股票/ETF 工具链尚未准入，不得输出已实现显式对冲。
   仅凭低/负相关不得称为 hedge。
11. 对 ETF 只允许使用已注册、具官方产品 lineage 的 `ETFProductProfile`，并先用 `portfolio-etf-metrics` 冻结同一 `as_of` 的成交额、波动、tracking error 与费率诊断，再运行 `portfolio-hedge-evaluate`。当前 ETF **研究与组合评估可用，但 paper order/replay 未准入**；融券、期货、期权、杠杆/反向工具不在当前系统能力内。若缺正式 ETF profile 或成本/机制证据，只能称“互补配置/分散化候选”。
12. 正式 `NATURAL_HEDGE` 必须由 `HedgeEffectivenessReport` 复算：至少比较正常期和压力期相关/敏感度、指定风险加入前后、风险改善阈值、独立的已验证成本上限和基差/模型风险；风险指标与费用不得跨量纲相减。调用方自行填写“降低 20% 风险”没有对冲权威。
13. 组合提案只读：不得写 paper ledger、不得把 proposal 当订单、不得把订单当 fill。若用户选择模拟执行，另走现有确认/订单/replay 流程；真实券商执行始终不存在。

## Workflows

- [`docs/workflows/workflow-portfolio-transition-and-hedging.md`](../../../docs/workflows/workflow-portfolio-transition-and-hedging.md)
- [`docs/workflows/workflow-portfolio-construction.md`](../../../docs/workflows/workflow-portfolio-construction.md)
- [`docs/workflows/workflow-holding-rebalance-decision.md`](../../../docs/workflows/workflow-holding-rebalance-decision.md)
- [`docs/workflows/workflow-holding-monitoring.md`](../../../docs/workflows/workflow-holding-monitoring.md)

## Stable command compatibility

Use `uv run astock portfolio-paper-evaluate` / `portfolio-evaluate` / `portfolio-construct` for the existing base reports, then `uv run astock portfolio-complement-screen`, `portfolio-hedge-evaluate`, or `portfolio-transition` only when the new task requires them.

## Output

计划买入后的组合补全：**当前/仅买 X/目标组合差异 → 入选标的与目标区间 → 风险改善和代价 → 最大失效条件**。已有组合：**总体风险一句话 → 最需要处理的 1–3 个暴露/持仓 → HOLD/ADD/TRIM/EXIT 或组合联动 → 下一复核条件**。只解释真正影响决策的指标；CVaR 等术语首次出现时用一句普通话解释。

## Prohibitions

- Do not turn a Candidate/ResearchSeed directly into a portfolio weight.
- Do not call a low-correlation stock an explicit hedge.
- Do not invent cash, NAV, risk tolerance, expected return, industry labels, ETF product facts or transaction costs.
- Do not optimize around a company `REJECT / WATCH / NEEDS_INFO` or stale formal result.
- Do not force small drift back to an exact point target when it remains inside the no-trade band.
- Do not claim ETF paper execution, shorting, futures, options, margin or real-broker execution is available.
- Do not create or send a real brokerage order.
