# Market Regime & Risk Budget Control v1

> 状态：CURRENT
> 是否已实现：是；PIT 特征、透明基线、概率 challenger、历史/注册完整性检查、风险预算与推荐供给只读覆盖层均已接入并通过稳定回归。正式生产准入仍保持关闭：prospective shadow、controlled-live 与 owner approval 是独立运行启用门，不得由 schema/阈值单测或历史回放替代。
> 更新日期：2026-09-09
> 关联 ADR：`docs/adr/0002-market-regime-as-risk-overlay.md`
> 关联计划：根目录 `开发计划.md`

## 1. 目标

建立一套可解释、可回测、可审计、可回滚的 A 股市场状态总控，使系统能够：

- 判断当前更接近健康牛市、投机性高波动牛市、中性震荡、风险收缩、趋势熊市或恐慌；
- 给出概率、置信度、状态变化驱动和数据覆盖，而不是只给一个标签；
- 在用户自身风险边界内，动态调整总风险预算、单股风险、推荐供给、建仓节奏、流动性要求和安全边际；
- 把宏观/政策、市场行情和组合风险分层，避免单一指数涨跌替代专业判断；
- 在数据不完整、模型冲突或状态切换不确定时自动回到中性或更保守策略；
- 通过 point-in-time walk-forward、成本后组合结果和 prospective shadow 证明它对决策有增量价值。

本设计不承诺准确预测牛熊顶底，也不允许市场状态替代公司、行业、财务、治理与估值研究。

## 2. 当前实现审查

### 2.1 现有 `market-regime-v1`

当前代码在 `src/astock/shadow/service.py` 中使用固定优先级规则，将 `MarketRegimeFeatures` 分类为：

- `PANIC`
- `HIGH_VOL_BULL`
- `TREND_BULL`
- `TREND_BEAR`
- `RANGE`
- `UNCLASSIFIED`

实际判定主要使用：日线趋势、小时趋势、市场宽度、波动分位和指数回撤；阈值来自 `configs/shadow_evaluation.yaml`。Schema 还包含 turnover、行业扩散和风格相对收益，但当前分类器没有使用这些字段。

### 2.2 当前适用边界

现有规则适合：

- shadow 研究样本分层；
- 检查前向评估在不同简单市场环境下是否失效；
- 作为透明且低复杂度的后续比较基线。

它不适合直接承担用户所需的牛熊总控，原因是：

- 没有状态概率、置信度和模型分歧；
- 没有 hysteresis、minimum dwell 或 change-point 处理；
- 没有增长、通胀、信用、流动性、政策、估值和盈利扩散；
- 没有混频、发布滞后和数据修订语义；
- 没有到投资组合风险预算的版本化映射；
- 单测只验证阈值和优先级，尚未验证状态是否改善样本外投资决策。

### 2.3 数据平面的关键缺口

旧 `src/astock/providers/macro_authority.py` authority adapter 仍保持 recorded-first、`live_supported=false`，用于既有 Provider/fixture 合同；新的 current 宏观数据面由 `src/astock/investor_orchestration/macro.py` 的 `OfficialMacroCaptureService` 独立承担 raw-first 官方抓取、最终 URL/host 校验、不可变原文与 capture receipt。当前 NBS `manufacturing-pmi-current` 与 PBOC `monetary-credit-social-financing-current` 已能在版本化 parser 成功时形成结构化 observation，MOF/NDRC 允许 document-only；这仍只是具名 family 覆盖，不能据此宣称 current macro 全面闭环。来源发布时间与 `available_to_system_at` 分离，HTML `PubDate`/HTTP `Last-Modified` 只能提供 publication time，系统历史可得时间不得早于真实抓取冻结时刻；first observed 也不得冒充已验证 first release。

## 3. 设计原则

1. **Risk overlay, not alpha oracle**：状态调整风险承受和研究供给，不直接创造股票收益预测。
2. **多层而非单标签**：经济、政策、市场和组合风险各有独立输出。
3. **概率而非伪确定性**：输出各状态概率、置信度、覆盖与冲突。
4. **PIT first**：只使用当时可得的数据、当时可见的修订版本和正式来源。
5. **透明基线优先**：先建可解释 scorecard，再让 HMM/Markov challenger 竞争。
6. **状态稳定与转折敏感平衡**：明确滞回、最短停留和紧急状态覆盖规则。
7. **成本后验证**：不以毛收益或事后标签作为唯一成功标准。
8. **更保守的失败模式**：缺数据、冲突或模型漂移时回退，不乐观补全。
9. **风险偏好先于市场状态**：用户风险上限是硬边界，牛市不能突破。
10. **无自动实盘执行**：状态服务只读，不直接写任何真实或模拟订单。

## 4. 分层状态模型

### 4.1 `EconomicRegimeSnapshot`

描述实体经济与名义周期，不直接叫“股市牛熊”。建议维度：

- growth level / momentum / diffusion；
- inflation level / momentum / composition；
- credit impulse / financing conditions；
- inventory and capacity cycle；
- employment and demand；
- external demand / exchange-rate pressure；
- policy stance and expected transmission。

输出可以是：

- `GROWTH_ACCELERATING`
- `GROWTH_STABLE`
- `GROWTH_SLOWING`
- `CONTRACTION_RISK`
- `INFLATION_RISING` / `DISINFLATION`
- `CREDIT_EASING` / `CREDIT_TIGHTENING`

这些是可组合轴，不强迫所有宏观信息压成一个离散状态。

### 4.2 `MarketRegimeSnapshotV2`

描述 A 股市场本身：

- `HEALTHY_BULL`：趋势为正、宽度广、波动/拥挤可控、流动性和盈利扩散支持；
- `SPECULATIVE_BULL`：价格上行但宽度窄、波动高、估值/拥挤/杠杆或主题集中恶化；
- `NEUTRAL_RANGE`：趋势和宽度没有稳定方向，风险可用但缺少广泛上行证据；
- `RISK_OFF_RANGE`：指数未形成趋势熊，但宽度、流动性、信用或尾部风险显著收缩；
- `TREND_BEAR`：中期趋势、宽度和行业扩散多数为负；
- `PANIC`：快速回撤、波动/相关性/流动性压力达到紧急门；
- `TRANSITION`：状态概率分散或近期发生结构变化；
- `UNCLASSIFIED`：关键覆盖/PIT/质量不满足。

每个快照必须携带：

- state probability vector；
- selected state；
- confidence and calibration version；
- feature coverage by family；
- baseline/challenger disagreement；
- previous state、transition time、dwell days；
- emergency override flag；
- top positive/negative drivers；
- valid from / expires at；
- source release/artifact/hash/PIT lineage；
- policy and model version。

### 4.3 `PortfolioRiskRegimeSnapshot`

同一市场状态对不同组合影响不同，另建组合风险层：

- market beta and factor concentration；
- pairwise/cluster correlation；
- downside beta、CVaR/CDaR；
- liquidity and implementation cost；
- industry/style/cap exposure；
- drawdown and loss budget consumption；
- actual/paper lane attribution；
- scenario stress under current regime。

该层决定某个用户组合是否已经用满风险预算，而不是简单复制市场标签。

### 4.4 `RegimeDecisionPolicy`

将市场和组合状态映射为约束，不改写研究事实。字段建议：

- gross/net-equity research budget multiplier；
- new-position budget multiplier；
- single-name risk multiplier；
- max concurrent new positions；
- minimum liquidity percentile；
- required valuation margin-of-safety adjustment；
- entry staging / tranche count；
- no-trade band widening；
- recommendation publication cap；
- required red-team depth；
- stop/exit review urgency；
- uncertainty fallback；
- user-profile cap references。

绝对仓位仍由用户目标、风险承受、期限、现金需求和正式组合策略决定；状态 policy 只能在这些上限内缩放。实现时复用现有 `PortfolioIntentProfile` 及其总仓位、单股、行业、相关性、回撤、波动、现金与换手约束；约束不完整时回到保守默认值，不新建与之竞争的第二套用户风险画像。

## 5. 特征体系

### 5.1 价格、趋势与相对强弱

至少覆盖沪深 300、中证 500、中证 1000、全 A 与主要风格/行业：

- 5/20/60/120/250 日收益和趋势斜率；
- 价格相对移动均线、突破/回撤；
- 多指数趋势一致度；
- 大小盘、价值/成长相对强弱；
- gap、涨跌停和隔夜压力；
- 趋势持续时间与加速度。

不得只用单一指数代表全 A。

### 5.2 市场宽度与扩散

- 上涨/下跌家数与成交额加权 breadth；
- 高于 20/60/120/250 日均线的股票比例；
- 52 周新高/新低；
- 行业正收益、盈利上修和相对强度扩散；
- 等权与市值加权指数背离；
- 涨跌停扩散、连续跌停与无法成交比例；
- 可交易 Universe 覆盖率。

### 5.3 波动、尾部与相关性

- realized volatility、downside semivolatility；
- rolling drawdown、recovery duration；
- VaR/CVaR/CDaR；
- cross-sectional dispersion；
- average correlation、tail correlation；
- volatility-of-volatility；
- skew、jump 和极端下跌广度。

高波动牛市必须能与健康牛市分开。

### 5.4 流动性与交易拥挤

- 全市场成交额/换手率及分位；
- price impact / Amihud 类指标；
- bid-ask 或可用近似指标及质量标记；
- 融资余额、信用交易或杠杆线索（只有正式可用时）；
- 主题/行业成交集中度；
- 小盘与低流动性资产的压力；
- 新股/涨停生态只作为风险线索，不作为牛市充分条件。

### 5.5 估值与风险溢价

- 宽基 PE/PB/FCF yield 的 PIT 分位；
- equity risk premium 的定义、无风险利率来源与版本；
- 行业和风格估值扩散；
- 价格相对盈利/现金流增长；
- 市场隐含增长与正式盈利预测差异。

估值昂贵不自动等于熊市，便宜也不自动等于买点；它影响赔率、脆弱性和安全边际。

### 5.6 盈利与基本面扩散

- 收入、利润、ROIC、现金流的同比/环比扩散；
- 正/负盈利修正广度；
- 财报质量与现金转化；
- 行业盈利周期；
- 资本开支与库存周期；
- 正式财报覆盖率和迟报状态。

预测值必须区分正式/外部估计与系统模型输出，不混成同一事实。

### 5.7 宏观、信用和政策

- NBS：PMI 及分项、工业、消费、固定资产、价格、利润等具名 release；
- PBOC：货币、社融、利率、信贷结构与政策 release；
- MOF：财政收支、专项债或具名财政政策；
- NDRC：价格、产业和重大政策；
- 交易所/证监会/国务院的市场制度与政策；
- 汇率、商品和外需只有在来源、时点与传导链明确时进入。

宏观数据发布频率和修订不同，必须按 `available_to_system_at` 混频，禁止用最终修订值回填过去。

## 6. 数据与来源架构

### 6.1 Source-of-truth

```text
official response/document
→ immutable SourceSnapshot
→ typed observation + release/vintage
→ PIT feature view
→ MarketRegimeFeatureSnapshot
→ baseline/challenger inference
→ RegimeDecisionPolicy overlay
```

任何特征都要记录 observation period、published/effective/ingested/available time、revision、source class、coverage 和 quality。

### 6.2 Live 宏观优先级

第一阶段不追求覆盖所有宏观指标，先闭合高价值、稳定且能做 PIT 的 release family：

1. NBS PMI 与主要月度/季度 release；
2. PBOC 货币信贷/社融与政策 release；
3. MOF/NDRC 以官方文档 capture 为主，不为结构化强依赖脆弱 API；
4. 任何新端点都走 `source-proposal-check`、recorded contract test、schema drift 和 rollback；
5. live 失败时可复用最新合法 snapshot，但必须报告 age，不能把旧数据标成当前。

### 6.3 Mixed frequency

- 日频市场数据不向前填充未发布的月度宏观值；
- 月度/季度值在实际可用时间后才生效；
- 采用 ragged-edge feature matrix；
- 每个 inference 记录 feature age 和 stale threshold；
- 缺失不统一填 0：数值、缺失原因、age 与 coverage 分开建模；
- revised series 同时保存 first-release 和 latest-vintage 视图，用于修订敏感性测试。

## 7. 模型设计

### 7.1 Baseline A：现有 `market-regime-v1`

完全保留，作为最低复杂度和回滚基线。不得在原配置上直接扩写到无法复现实验；新版本使用独立 Schema/config。

### 7.2 Baseline B：透明多维 scorecard

各特征先做 robust scaling / rolling percentile，再按预注册 family 合成：

- trend score；
- breadth score；
- volatility/tail score；
- liquidity score；
- valuation fragility score；
- earnings diffusion score；
- macro/credit score。

family 权重、缺失处理和状态边界写入 versioned config。权重不能使用 holdout 调到最优，且要提供每次分类的贡献分解。

### 7.3 Challenger：Markov/HMM

使用离散隐状态和状态转移概率，输出 filtered probability。用途：

- 捕捉非线性状态和持续性；
- 提供概率而非硬阈值；
- 与 transparent baseline 比较转折、稳定和下游价值。

限制：

- 状态语义必须通过冻结 mapping 解释，不能事后按收益给隐藏状态重新命名；
- 训练只使用 train window，参数和状态数在 dev 冻结；
- holdout 不参与选特征/权重/状态名；
- 复杂模型不过门时继续使用透明基线。

### 7.4 Optional challenger

可以研究 Bayesian change-point、dynamic factor 或 robust clustering，但一次评估只允许预注册少量候选，防止多重试验挑赢家。

## 8. 状态选择、置信度与滞回

### 8.1 置信度

综合：

- selected state probability；
- top-2 probability margin；
- feature family coverage；
- baseline/challenger agreement；
- recent data revisions；
- transition proximity；
- out-of-distribution score。

不得把模型 softmax/后验概率直接当完整可信度。

### 8.2 滞回

常规状态切换至少满足：

- 新状态概率连续达到进入门；
- 退出门低于进入门；
- minimum dwell 或累计证据；
- 数据 revision 不得无审计重写历史状态。

`PANIC` 可以绕过 minimum dwell，但必须由多个尾部/流动性条件交叉触发；解除 panic 仍需要更严格退出门。

### 8.3 冲突

- baseline bull、challenger bear：标记 `TRANSITION` 或使用更保守预算；
- 价格 bull、宽度/流动性风险：`SPECULATIVE_BULL`，不能选 `HEALTHY_BULL`；
- 宏观弱、市场强：保留两层事实，按估值/盈利/流动性决定脆弱性，不强制合成一致叙事；
- coverage 不足：`UNCLASSIFIED`，决策 policy 回到静态/保守基线。

## 9. 风险预算映射

### 9.1 不变硬门

以下项目不受牛熊放宽：

- 证券身份与可交易性；
- 当前证据与 PIT；
- 财务完整性和会计红旗；
- 治理、审计、质押、减持与资本配置；
- 公司/行业基本面；
- 估值模型的输入事实和数学；
- Committee/readiness；
- 工具交易规则、T+1、费用、流动性；
- 模拟盘人工确认；
- 无真实券商执行。

### 9.2 单调约束

在其他条件相同且用户上限不变时：

- 总风险预算：`PANIC ≤ TREND_BEAR ≤ RISK_OFF_RANGE ≤ NEUTRAL_RANGE ≤ HEALTHY_BULL`；
- 新建仓数量：同上；
- 单股风险上限：同上，但 `SPECULATIVE_BULL ≤ NEUTRAL_RANGE`；
- 最低流动性要求：panic/bear/speculative bull 不低于 neutral；
- 估值安全边际：bear/panic/speculative bull 不低于 neutral；healthy bull 也不得低于公司基础门；
- 推荐发布数量：由“通过正式研究的数量”和 regime cap 取较小值；
- 建仓节奏：风险越高，分批更多、单批更小；
- no-trade band：高波动状态可适度加宽以避免噪声换手，但 thesis invalidation 不受此保护。

### 9.3 健康牛市的“更乐观”定义

允许：

- 在用户风险上限内采用目标区间上沿；
- 对更多已通过硬门候选开展/发布正式研究；
- 在流动性和估值允许时提高建仓节奏；
- 对盈利扩散与趋势均支持的上行情景赋予更高但可审计权重。

不允许：

- 把低质量公司改成高质量；
- 忽略财务异常、治理风险或高估值；
- 因指数上涨降低证据要求；
- 用更多推荐掩盖候选不足；
- 把高波动窄幅上涨称为健康牛市。

### 9.4 熊市的“更保守”定义

- 降低组合和单股可用风险；
- 减少新推荐和并发新仓；
- 提高流动性、财务质量和安全边际要求；
- 更强调现金流、资产负债表、需求弹性与尾部情景；
- 对现有持仓优先检查 thesis invalidation、融资/质押/减持和流动性；
- 不因跌幅大就自动判定抄底；
- 已有高质量低估标的仍可研究，但动作更分段且需要更强证据。

### 9.5 防止顺周期强制交易与重复计数

- 市场状态变化本身只能触发风险复核，不能单独生成已有持仓的 `ADD/TRIM/EXIT`；动作必须同时命中冻结的 thesis、估值、组合风险、流动性或交易规则条件。
- regime overlay 不修改公司研究中的 canonical forecast、合理价值和会计事实。需要改变公司情景概率时，必须由独立、可验证的因果模型负责，不能把“市场牛熊”当作任意重写估值的理由。
- 同一宏观、行业、盈利或估值信号若已经进入公司 forecast/valuation，再进入 regime policy 时必须登记 feature lineage、作用通道和重复计数审计；不允许同一利好先抬高合理价值、再无审计地提高风险预算。
- 对既有持仓报告 `label_change_only_action_count`，正式环境必须为 0；同时报告 overlay 导致的换手、反转率和机会成本，防止状态切换制造追涨杀跌。

## 10. 与业务能力集成

- 单股买入/估值：regime 调整买入节奏和风险，不改公司合理价值；
- 持仓：与 thesis/valuation、组合风险共同形成 HOLD/ADD/TRIM/EXIT；
- 全市场荐股：先决定研究/发布预算，再让 Universe 和 Research Team选出合格证券；
- 行业：把宏观/政策和行业自身周期分开；
- ETF：评估是否改善当前组合风险，不能仅按“熊市买 ETF”泛化；
- 模拟订单：regime 结果可以限制准备，不得绕过确认或直接创建 fill；
- Continuous Monitor：状态显著变化产生持久事件，触发增量组合复核，不逐日生成无意义告警。

## 11. 验证设计

### 11.1 数据切分

- 使用最大可认证 PIT 历史，并采用分层样本：`CORE_LONG_HISTORY` 尽量覆盖 2015 年等早期极端阶段，只使用长历史可认证特征；`ENRICHED_HISTORY` 目标至少 8 年，加入后期才稳定可得的宽度、流动性、宏观 vintage 与盈利扩散特征。两层分别报告，不用短历史高级特征反向填补早期样本；
- 时间顺序 train/dev/holdout，不随机打乱；
- walk-forward，每一折只使用当时可得 vintage；
- 规则、特征、状态数和 mapping 在 holdout 前冻结；
- 模型选择与最终 downstream 评价分离；
- 明确缺失期，不把不完整历史补成完美面板；
- Universe 必须 point-in-time，纳入上市、退市与停牌，防止 survivorship bias；复权/未复权和 total-return 口径分离；主板、科创板、创业板、北交所、ST、涨跌停、注册制与交易规则变更按 effective date 入模或分层，不把制度断点误判为经济 regime。

### 11.2 状态质量

不采用单一“牛熊准确率”作为主指标。`Brier score`、`log loss` 和“概率校准”只有在**事前冻结的可观察 outcome 合同**存在时才允许计算，例如未来具名窗口的回撤/尾部损失、宽度恶化、流动性压力事件，或独立且冻结的专家状态标注协议。无观察标签的 HMM hidden-state posterior 只能报告熵、概率集中度、稳定性、转移和下游决策价值，不得把后验自洽性称为校准。

至少报告：

- 对已注册 observable outcome 的 probability calibration / Brier score / log loss（不适用时明确 `NOT_EVALUABLE`）；
- hidden-state posterior 的熵、概率集中度、稳定性与转移诊断；
- top-state stability、switch rate、median dwell；
- transition lead/lag distribution；
- false panic / missed panic；
- `UNCLASSIFIED` 率和各 feature family coverage；
- baseline/challenger disagreement；
- first-release vs latest-vintage revision sensitivity；
- OOD 和 schema drift。

任何事后牛熊标签都只能作为辅助参考，不能把未来收益直接泄露进当期状态。

### 11.3 决策增量价值

冻结一个不使用 regime 的静态风险策略，与各 overlay 做配对比较：

- net return、volatility、Sharpe/Sortino（只作辅助）；
- max drawdown、CVaR、CDaR；
- downside/upside capture；
- turnover、slippage、fee 和 market impact；
- recommendation count、precision、机会成本；
- concentration、liquidity 和 target-band violations；
- 持仓动作的稳定性与反转率。

正式接管至少要求：

1. 样本外风险指标相对静态基线有稳定改善，配对 bootstrap 区间不能仅由单一周期驱动；
2. 不能靠不可接受的收益牺牲或换手取得风险改善；
3. 多数 walk-forward 折方向一致，无单一折灾难性恶化；
4. 对当前 `market-regime-v1` 具有增量，或明确选择继续使用简单基线；
5. 所有结果按成本后、PIT、首次发布 vintage 重算；
6. 通过 prospective shadow 后才申请生产 policy。

具体数值阈值必须在看到基线结果前预注册，不能在 holdout 后为某个模型改门。

### 11.4 专项反例

- 指数上涨但仅少数大市值推动；
- 高换手、高波动、主题集中上涨；
- 指数横盘但大多数股票下跌；
- 宏观数据因修订从弱变强；
- 月度数据尚未发布时的 ragged edge；
- 快速恐慌后 V 型修复；
- 低波动但信用/流动性恶化；
- 估值极高但趋势仍强；
- 牛市中持有高财务风险公司；
- 熊市中高质量低估公司；
- 实际与模拟组合方向相反；
- 模型不一致与数据源中断。

## 12. 上线、监控与回滚

### 12.1 Feature flags

- `market_regime_v2_enabled=false` 默认；
- `regime_decision_overlay_enabled=false` 默认；
- `regime_challenger_shadow_only=true`；
- 配置、模型、特征和 policy 独立 version/hash。

### 12.2 运行监控

- snapshot freshness/coverage；
- state probability and transitions；
- feature drift/OOD；
- source/schema/revision events；
- recommendation/risk budget changes；
- downstream loss/turnover/opportunity cost；
- baseline/challenger disagreement；
- emergency override count；
- user-profile cap violations（必须为 0）。

### 12.3 回滚

回滚只切换 active policy，不删除历史快照、不改账本、不回写过去状态。顺序：

1. 停用 decision overlay；
2. 回到静态组合风险策略；
3. 状态服务保留只读/影子输出；
4. 必要时回到 `market-regime-v1`；
5. 冻结故障版本及输入，完成审计后再重启。

## 13. 实施工作包

1. official macro current data plane；
2. `MarketRegimeFeatureSnapshotV2` 与 PIT feature store；
3. transparent scorecard baseline；
4. HMM/Markov challenger；
5. confidence/hysteresis/conflict service；
6. `RegimeDecisionPolicy` 与用户风险上限组合；
7. portfolio/recommendation/holding integration；
8. walk-forward and cost-aware evaluation；
9. prospective shadow；
10. controlled activation and rollback drill。

详细依赖、交付物和验收门见 `开发计划.md`。

## 14. 外部研究依据

- Hamilton (1989) 提出以离散状态 Markov 过程建模不可直接观察的 regime，并通过概率滤波推断状态：https://www.jstor.org/stable/1912559
- Chicago Fed NFCI 将货币、债务、权益以及传统/影子银行等多类金融条件合成，说明专业“状态”不应只看一个价格指标：https://www.chicagofed.org/research/data/nfci/about
- IMF Growth-at-Risk 把当前金融条件连接到未来增长分布的下尾风险，支持把状态判断与尾部风险分开度量：https://www.imf.org/-/media/files/publications/wp/2018/wp18180.pdf
- Moreira & Muir 的 volatility-managed portfolio 研究支持把高波动映射为更低风险暴露，但本项目必须在 A 股 PIT、成本和样本外条件下独立验证：https://www.nber.org/papers/w22208
- NBS PMI 方法说明了扩散指数和领先监测用途；本项目只使用实际可得 release 和分项，不把 50 阈值机械等同于股市牛熊：https://www.stats.gov.cn/english/understanding/201311/t20131118_463794.html
