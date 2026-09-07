# ADR-0002: 市场状态是风险预算覆盖层，不是个股结论替代器

> 状态：PROPOSED
> 日期：2026-09-07
> 决策者：待下一步实现评审
> 关联设计：`docs/architecture/market-regime-control-v1.md`

## Context

用户需要系统在健康牛市中允许更积极的仓位和更多合格推荐，在熊市或恐慌状态中收紧风控和推荐数量。当前 `market-regime-v1` 位于 shadow 评估链，只有趋势、宽度、波动和回撤阈值，不具备：

- 当前投资请求的统一市场状态快照；
- 概率、置信度、状态滞回和转折不确定性；
- 宏观、信用、流动性、估值与盈利维度；
- 到总风险、单股风险、推荐供给和安全边际的版本化映射；
- 对映射本身的 PIT walk-forward 与 prospective shadow 验证。

直接把“牛市”当作降低公司质量或估值门槛的理由，会把市场情绪错误传导为基本面结论，并放大高波动牛市末期风险。

## Decision

拟议实现遵循以下原则：

1. 市场状态输出是带概率、覆盖度、分歧和有效期的 `MarketRegimeSnapshotV2`，不是单一绝对标签。
2. 经济/政策状态、市场价格状态和组合风险状态分层建模，最终由 `RegimeDecisionPolicy` 形成风险覆盖层。
3. 健康牛市可以在用户既定风险上限内提高可用总风险、候选槽位和建仓节奏；熊市/恐慌必须降低这些预算。
4. 高波动或投机性牛市不能沿用健康牛市的宽松预算，应因尾部风险收紧单股和流动性约束。
5. 无论市场状态如何，以下硬门保持不变：证据/PIT、公司质量、财务完整性、治理、估值事实、交易规则、人工确认和 `broker_execution_allowed=false`。
6. 当前 deterministic 规则作为基线；Markov/HMM 等概率模型只能作为 challenger，完成样本外、成本和 prospective shadow 验证后才可申请接管。
7. 状态不确定、数据覆盖不足或模型冲突时，回退到中性或更保守策略，不允许模型自信补全。

## Consequences

### Positive

- “牛市更积极、熊市更保守”变成可审计、可回测、可回滚的风险规则；
- 避免把行情判断偷换为公司基本面判断；
- 可以衡量风险改善、机会成本、换手和错误分类代价；
- 多个 Agent 共享同一时点状态，不再各自口头判断牛熊。

### Costs

- 需要补齐 official live 宏观数据、市场宽度、估值和盈利扩散的 PIT 数据链；
- 需要混频、修订、缺失与状态滞回处理；
- 上线前必须积累独立 shadow 证据，不能只凭历史回测启用。

### Risks

- 状态模型可能过拟合少数牛熊周期；
- 状态切换过快会增加交易成本，切换过慢会扩大回撤；
- 宏观数据存在发布滞后与修订，必须使用当时可得 vintage；
- 绝对仓位上限仍取决于用户目标和风险承受，市场状态只能在该边界内调整。

## Alternatives considered

### 只使用指数 200 日均线判断牛熊

保留为简单基线，不作为唯一生产方案。它易解释，但无法区分市场宽度背离、投机性高波动牛市和流动性压力。

### 完全由大模型阅读新闻后判断牛熊

不采用。缺少稳定的时点、数值、版本、校准和回滚合同，且容易把叙事强度误当成市场状态。

### 市场状态直接修改个股评分或目标价

不采用。市场状态只调整风险预算、推荐供给和入场节奏；个股事实、盈利与估值模型保持独立。

## Compliance and verification

在本 ADR 从 `PROPOSED` 变为 `ACCEPTED` 前，必须完成：

- `MarketRegimeSnapshotV2`、`RegimeDecisionPolicy` 和 lineage Schema；
- official live / recorded PIT 数据覆盖审计；
- transparent baseline、静态风险基线与概率 challenger 的冻结比较；
- 多阶段 walk-forward、修订敏感性、状态稳定性、成本和下游组合风险验收；
- prospective shadow；
- 一键回退到静态风险或 `market-regime-v1`，且不改变账本。

## References

- Hamilton (1989), regime-switching model: https://www.jstor.org/stable/1912559
- Chicago Fed NFCI methodology: https://www.chicagofed.org/research/data/nfci/about
- NBER, *Volatility Managed Portfolios*: https://www.nber.org/papers/w22208
