# Portfolio & Holding Decision Architecture v1

> 状态：v0.3.0 ETF 深度事实与独立 paper policy 扩展已正式发布；release baseline 为 `5567cab46195a5073a9bf5a4fc16acfe1ff35066`，annotated tag `v0.3.0` 不移动。历史 v0.2.0 release baseline `b776d0054b60d96f5c4da41b10926c1e583bc699` 与 tag `v0.2.0` 保持不变
> 日期：2026-09-02
> 边界：A 股 long-only 投研、组合研究、持仓复核和模拟交易建议；真实券商执行永久关闭。ETF 已有独立、effective-dated paper execution policy，但仓库默认关闭，且绝不复用 STOCK 交易规则。

## 1. 目标

本架构闭合两条投资者自然语言链：

1. **计划买入 X 后补全组合**：当前组合 → 加入 X → 识别新增风险 → 风险缺口驱动候选 → 正式研究/准入 → 稳健组合 → 成本/no-trade band → 分散化/自然对冲准确分类。
2. **已有持仓后的加减仓/退出/调组合**：首次既成交易事实持久化 → 跨会话恢复 → 增量事件研究 → 单股 thesis/valuation + 组合风险双层裁决 → HOLD/ADD/TRIM/EXIT + target band → 后续持续监控。

本轮不建立第二套 Portfolio、Evidence、Monitor、Ledger 或用户状态事实源。

## 2. 事实源

### 2.1 外部真实账户事实

外部真实账户与模拟盘彻底分 lane。SQLite `external_account / external_account_event` 是真实账户事实的权威源：事件只追加、不覆写，按 `account_id` 隔离账户；`TRADE / CASH_* / SECURITY_TRANSFER_* / REVERSAL` 与 replacement 共同覆盖真实交易、现金、转托管/非交易过户和历史更正。`user_state/*.md` 只做人类可读投影或旧单账户兼容镜像，不得反向制造事实。

用户明确陈述已经发生的真实交易时：

```text
UserDeclaredTradeCapture
  → exact field validation + visible instrument identity
  → ValidatedExternalTradeImport
  → ExternalAccountEvent(default / TRADE)
  → paper-fill economic conflict guard
  → legacy LocalPortfolio compatibility projection refresh
  → external_accounts.md / portfolio.md (human-readable projections)
  → ExternalTradeImportReceipt
```

批量/多账户入口固定为 `CSV/JSON → raw ObjectStore → schema/timezone/account/secret/correction validation → ExternalAccountImportPreview → explicit confirm + source-hash recheck → 单一 SQLite transaction → append-only events → external_accounts.md`。preview 失败不写 batch/event；confirm 后整批成功或整批回滚。

硬边界：

- 完整字段为 market/security identity、BUY/SELL、quantity、price、actual occurred_at；
- 缺字段不得用当前价格或声明时间猜测；
- exact economic duplicate 幂等；
- 与 PAPER_FILL 的同一经济事实冲突时阻断双计；
- external-account event 永不写 SQLite paper ledger，也不保存券商密码、Token、Cookie 或 API key；
- 更正只能引用同账户目标，历史行永不 UPDATE/DELETE；同批 correction 即使排在 target 前，也按依赖拓扑安全插入；
- 旧 `trades.md` 的 `IMPORT` 可显式幂等迁移到 `default`，`PAPER_FILL` 永远留在 paper lane；legacy Markdown 刷新失败时 canonical event 仍成立，重试只补投影。

`ExternalAccountProjection` 按账户确定性回放 cash-known/cash、持仓数量与平均成本，多账户不会互串；`UserPortfolioSnapshot` 继续冻结默认持仓研究边界、最近 review 与 open orders。外部现金尚未形成账户事件时 `cash_known=false`，不推断 NAV。

### 2.2 模拟交易

SQLite paper ledger 继续唯一拥有 simulated account/order/fill/position/T+1/corporate-action application。proposal/order 都不等于 position，只有 replay fill 改变持仓。

PaperOperation/PaperReplay 按 instrument-specific policy 路由。STOCK 继续使用既有规则；ETF 只在目标证券存在有效 `ETFInstrumentExecutionRule`、`execution_enabled=true`、费用仍满足 confirmation gate，并经过既有 prepare/人工确认链时才允许模拟。ETF lot/tick/limit/fee/settlement 在订单准备时冻结到 binding；缺规则、过期、费用不确定或 execution 关闭均 fail closed。只有确认订单后的 fill/replay fill 改变持仓。

卖出冻结、reservation、position recovery 必须按 canonical instrument identity（`market:symbol`）匹配，不能只按 6 位代码查持仓；例如 XSHG:600519 不得被 XSHE:600519 的卖单占用。`instrument_type` 不作为 `paper_position_identity` 的数据库主键，而是由正式 Reference 解析并冻结在每笔 `paper_order_rule_binding`，STOCK/ETF 的 lot/tick/fee/settlement 因而仍必须按订单绑定分别执行、不得串用。ETF 订单/成交价格采用 milli-yuan 精度，跨会话 LocalPortfolio 恢复和投影也必须保留该精度；只有面向 fen 的最终展示/汇总边界才允许确定性舍入，禁止在持仓平均成本恢复时先截断为股票 0.01 元精度。

## 3. PIT 公司行动调整研究序列

执行价格仍使用原始未复权行情。组合统计新增本地派生 gross total-return research series：

```text
immutable DAILY_UNADJUSTED
 + visible CorporateActionObservation
 + status == TERMS_VERIFIED
 + available_to_system_at <= as_of
 → TotalReturnResearchSeries
```

ex-date 的单股经济财富按正式条款计算：

`close_ex * (1 + stock_ratio) + gross_cash_per_share`

再链式构造研究 pseudo-price。未来可见公司行动不能改变过去序列；未验证 action 不应用并产生 degradation warning。若正式 corporate-action release 不存在，风险分析可降级到 raw series，但必须标记 `TOTAL_RETURN_RESEARCH_SERIES_PARTIAL`，不能静默宣称已调整。

## 4. 组合迁移核心

现有四个 allocator 保持不变：

- `EQUAL_WEIGHT_CONSTRAINED` — 生产比较基准；
- `INVERSE_VOLATILITY`；
- `HIERARCHICAL_RISK`；
- `SHRINKAGE_MIN_VARIANCE` — Ledoit-Wolf covariance。

新增 `PortfolioTransitionRequest / PortfolioTransitionReport`，将静态 target 变成 current-to-target transition：

```text
PortfolioAnalysisReport (CURRENT)
 + PortfolioConstructionReport (formal admitted target)
 + PortfolioIntentProfile
 + current quantities / optional NAV
 + implementation costs
 + instrument trading-unit rules
 + optional evidence-bound ETF overlay
 → CURRENT / ANCHOR_ONLY / TARGET
 → PortfolioRiskGap
 → PositionTargetBand
 → turnover / cost / warnings
```

所有正式股票 target 仍由 `PortfolioConstructionReport` 的 Committee-approved candidates 提供；PortfolioDecision 不接收 Candidate/ResearchSeed 直接变权重。

## 5. Target band 与 no-trade region

`portfolio-decision-v1` 配置维护 versioned band policy：

- minimum/maximum band；
- unverified-cost wider band；
- cost-to-band multiplier；
- volatility widening；
- material weight threshold。

目标不是强制回到点权重。当前 weight 落在 `[target_lower, target_upper]` 时保持 HOLD。只有越界或正式事件/硬约束要求动作时，才进入 ADD/TRIM/EXIT 候选。

数量转换针对**交易增量**应用 instrument trading-unit rule，而不是强迫总持仓成为整手。因此外部既成的 odd-lot/零股持仓可以被正确保留；全部退出是否允许 odd lot 由工具规则决定。

## 6. 对冲治理

类型固定为：

- `DIVERSIFICATION`：组合风险改善但没有指定风险抵消权威；
- `NATURAL_HEDGE`：经济机制 + PIT 数据 + 压力期证明指定风险改善达到版本阈值，同时已验证往返实施成本不超过独立成本上限；风险指标与费用不做跨量纲相减；
- `EXPLICIT_HEDGE`：当前系统不准入；
- `UNPROVEN`：证据不足或目标风险不支持。

`HedgeEffectivenessRequest` 不接受调用方自行宣称有效性作为正式结果。`PortfolioDecisionService.evaluate_hedge()` 使用当前 portfolio 的 PIT returns 和候选工具 returns 重新计算：

- normal correlation；
- worst-tail stress correlation；
- 指定 risk metric 加入前/后；
- gross risk reduction；
- verified round-trip implementation cost (bps) 与独立成本可接受门；
- gross risk reduction（同一风险指标前后相对改善，不和费用跨量纲相减）；
- basis/model risk codes。

当前 deterministic 指标支持：

- `REDUCE_MARKET_BETA`；
- `REDUCE_VOLATILITY`；
- `DIVERSIFY`；
- `REDUCE_CONCENTRATION`。

`REDUCE_INDUSTRY_EXPOSURE / PROTECT_SCENARIO` 在缺 typed factor/scenario shock 合同时保持 `UNPROVEN`，不能用相关性代替。

即使 ETF 形成 `NATURAL_HEDGE`，当前 long-only system 也不会生成 `EXPLICIT_HEDGE`。融券、股指期货、期权、margin、inverse/leveraged execution 均不在本版本。

## 7. ETF 深度事实与独立模拟执行合同

新增：

- `InstrumentType.ETF / FUND / INDEX` 的产品身份与基础分类；
- `ETFProductProfile` / `FundProductProfile` / `IndexProductProfile`：分别冻结 ETF、非 ETF 基金与指数的官方身份和低频产品事实；ETF profile 额外保存 tracking benchmark、费率、规模/份额与交易规则；
- `ETFResearchMetrics`：同一 `as_of` 的平均日成交额、年化波动、tracking error 与费率视图，并冻结行情/产品/策略 lineage；
- `ETFNavSighting` / `ETFMarketPriceSighting` / `ETFPremiumDiscountValuation`：分别冻结 NAV/iNAV、市场价以及基于 iNAV 的溢折价，全部带真实 `as_of`、`available_to_system_at` 与 artifact/hash provenance；
- `ProductConstituentSnapshot`：冻结 ETF/FUND/INDEX 的 PIT 成分与权重，并区分 COMPLETE/PARTIAL 覆盖语义；
- `InstrumentTradingUnitRule`、`ETFCategory`、`SettlementCycle`；
- migration `0066` 与 `configs/etf_paper_trading_rules.yaml`：精确 instrument + effective date 的 ETF execution policy，独立保存 lot/tick/price-limit/fee/settlement；
- EastMoney/Sina intraday capability 对 ETF request 的显式声明；
- official `PRIMARY_OFFICIAL_WEB` source artifact/hash 与 source-time gate；
- target portfolio supplemental ETF overlay；
- hedge-effectiveness evaluation。

产品/NAV/iNAV 等正式值必须来自具备 PIT、source-time 与不可变 artifact/hash 的已准入产品事实；缺 NAV/iNAV 时不得计算伪溢折价，VERIFIED 级产品缺关键字段直接 fail closed。当前仓库对 ETF paper execution **默认关闭**；只有显式开启且目标证券存在当时有效的精确规则时，PaperOperation/PaperReplay 才消费独立 ETF binding。primary-market creation/redemption 继续不在模拟执行范围；关闭 execution 不影响只读 ETF 研究、组合补全或 hedge evaluation。

## 8. 持仓事件与动作

`HoldingReviewRequest` 新增：

- `event_severity`；
- `portfolio_effect_codes`；
- typed `HoldingTargetBandInput`。

事件类型：

- `THESIS_INVALIDATING`；
- `THESIS_WEAKENING`；
- `THESIS_STRENGTHENING`；
- `VALUATION_ONLY`；
- `PORTFOLIO_RISK_ONLY`；
- `TEMPORARY_NOISE`；
- `UNVERIFIED_LEAD`。

硬规则：

- `UNVERIFIED_LEAD` 只能 REVIEW；
- material thesis/valuation severity 必须有本轮 evidence；
- legacy rule-triggered ADD 仍要求**ADD rule 自身**绑定 evidence；
- new event/target-band ADD 至少要求新增正式 evidence；
- unresolved conflict / invalidated evidence 继续优先 REVIEW；
- `PORTFOLIO_RISK_ONLY` 可以在 thesis unchanged 的情况下 TRIM。

`HoldingReviewPack / PositionActionProposal` 现在携带 current quantity/weight、target band、target quantity range、implementation cost、portfolio effects、preconditions、reversal conditions 和 event severity。

## 9. Skill / Workflow 路由

新增跨 Skill Workflow：

- `workflow-portfolio-transition-and-hedging.md`；
- `workflow-holding-rebalance-decision.md`。

更新 canonical Skills：

- `$portfolio-manager`：planned anchor、risk gap、target transition、hedge semantics；
- `$holding-monitor`：same-turn trade persistence、incremental event/portfolio action；
- `$continuous-investment-monitor`：material event → holding + batched portfolio delta；
- `$astock-research-orchestrator`：明确区分 planned-purchase portfolio completion 与 held-position rebalance review。

Broad stock complements 仍从证明过的 A-share Universe/Research Team 中发现，不通过 Web/news 手选股票。ETF 只有已注册 official product profile 可以进入评估。

## 10. 外部方法依据与采用边界

- Markowitz (1952), *Portfolio Selection*: 协方差与分散化；不采用噪声均值驱动的无约束优化。
- Ledoit & Wolf (2004), shrinkage covariance: 继续用于生产 minimum-variance challenger。
- DeMiguel, Garlappi & Uppal (2009), *Optimal Versus Naive Diversification*: 支持保留受约束等权作为稳健比较基准。
- Davis & Norman (1990), transaction-cost portfolio selection: 支持 no-trade region/目标带设计。
- Almgren & Chriss (2001), optimal execution: 采用实施成本/流动性思想，不建立自动执行器。
- CFA Institute 2026 Asset Allocation: 用户目标、风险承受、期限、流动性和交易成本先于优化器。
- SSE trading rules / ETF official guidance: 工具特定交易单位、价格单位和结算必须正式版本化；本版本不把 ETF 套入 A 股股票 paper mechanics。

## 11. 永久安全边界

- no real broker connector/order；
- `broker_execution_allowed=false`；
- portfolio/hedge/holding report 不直接写 paper ledger；
- Candidate/ResearchSeed 不能直接产生 portfolio weight；
- Web/Search 不证明 Universe、连续行情、negative proof 或 hedge effectiveness；正式 ETF 产品/机制 capture 还必须满足 `observed_at <= as_of`；
- verified industry taxonomy 尚未落地时，显式用户行业上限不能被 caller-supplied risk group 冒充，transition 会 fail closed；
- ETF NAV/iNAV 未进入正式 PIT reference 前，不输出正式折溢价；
- 用户成本价不替代 valuation；
- 新闻 sentiment 不直接产生 position action；
- unknown cash/NAV/risk tolerance remains unknown；
- explicit hedge/short/futures/options remain unavailable until a separate fully specified instrument/margin/settlement/ledger project is admitted。
