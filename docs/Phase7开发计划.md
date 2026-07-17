# Phase 7 冻结权重影子评测开发计划

计划日期：2026-07-17

状态：开发前计划。Phase 6 已完成并通过终验；真实委员会决定、协议和调查任务仍为 0。Phase 5 的线上覆盖与人工 Skill 审核仍为 `PARTIAL`。本计划先冻结 Phase 7 的输入、比较口径、统计门槛和失败边界，再开始代码；不把合成测试样本写成真实投资证据。

## 1. 阶段目标与完成口径

Phase 7 不是寻找“近期最赚钱的专家”，而是回答一个更窄、可检验的问题：在同一批候选、同一时间点、同一 TradeProtocol、同一成本和成交假设下，加入某个专家或完整委员会后，是否在严格样本外对 BaseCase 产生了稳定、扣费后仍为正的增量价值。

主链为：

```text
冻结评测定义和权重
  -> 同一候选集的多臂影子分配
  -> 决策时点前 PIT/引用/版本核验
  -> 决策后不可变成交与结果观察
  -> 确定性市场状态标签
  -> 按独立决策聚类和成熟期过滤
  -> 逐时点 walk-forward 样本外折
  -> 成对指标、置信区间和稳定性检验
  -> ShadowEvaluationReport
  -> Phase8AdmissionReport
```

完成状态必须分成两层：

1. `IMPLEMENTED`：Schema、配置、存储、计算、CLI、审计、恢复和自动测试已交付。
2. `EVIDENCE_READY`：真实前向观察达到时间、样本、市场状态、PIT、成交质量和统计门槛。

代码完成不等于证据成熟。少于 6 个月只能是 `COLLECTING`；6～12 个月最多为 `PROVISIONAL`；Phase 8 的研究准入要求至少 12 个月。当前真实决定为 0，因此本阶段开发完成后的真实状态预期是 `NOT_RUN` 或 `COLLECTING/INSUFFICIENT_SAMPLE`，绝不能提前写成 `EVIDENCE_READY`。

## 2. 明确不做

- 不根据短期盈亏改权重，不在线学习，不人工回填“当时应该用的权重”，不删除表现差的臂或决定。
- 不实现规则动态权重、上下文多臂赌博机、PPO/A2C/RL；它们属于 Phase 8，且必须先通过本阶段准入门。
- 不用 `NOT_PIT_SAFE` 的当前 Provider 值冒充正式历史样本，不用今天的指数成分或幸存公司列表覆盖历史。
- 不读取未来公告、未来修订值、未来市场状态或未来收益来决定候选、权重、分组、排除项和协议。
- 不让不同臂使用不同候选集、成本、成交模型、信号时间或执行时点来制造优势。
- 不把 Phase 5 的 `PENDING/NOT_RUN` 候选 Skill 当作批准 Skill；未批准 Skill 只能在隔离的研究标签下运行，不能取得 Phase 8 准入。
- 不连接券商、不生成真实订单、不修改用户主模拟账户。影子“账户”是隔离的不可变评测臂与 NAV 工件，不复用真实/主模拟账本身份。
- 不让 Codex、外部 LLM 或自由文本计算收益、置信区间、市场状态、样本数、最大回撤或准入结论。
- 不把私有 PDF、DOCX、知乎正文、Cookie、浏览器 Profile、原始 runtime 或真实私密持仓写入 Git、测试夹具或报告正文。

## 3. 版本化配置和冻结研究定义

新增 `configs/shadow_evaluation.yaml`，首版至少冻结：

- `policy_version`、`engine_version`、`effective_from`、配置内容哈希；
- 初始资金、固定名义仓位、最大单股/行业/总暴露；
- 评测周期 5、20、60 个交易日，最终准入只使用已成熟的 60 日结果；
- 手续费、印花税、过户费、滑点、参与率、成交和公司行为版本；
- 市场状态规则版本、独立决策规则版本、统计方法版本和确定性随机种子；
- 最小样本、观察期、折数、每折样本、PIT、路径不确定、回撤和稳定性门槛；
- 正式、探索和隔离 Skill 的允许状态。

新增 `ShadowStudyManifest`。创建后固定 study ID、创建时间、观察起止边界、候选选择规则、候选集合快照规则、比较臂、权重、Skill 版本、BaseCase/委员会规则版本、成本/成交/公司行为版本、基准定义和配置哈希。任何字段变化必须创建新 study/version，旧研究只读。

权重只能是预先声明且总和严格等于 1 的 Decimal。`created_at/effective_from` 晚于某个决定时，该研究不得把该决定算作正式样本。历史重放即使 PIT 合格，只要规则/权重没有在结果发生前冻结，也只能标为 `EXPLORATORY_RETROSPECTIVE`。

## 4. 必需比较臂和公平性

每个正式研究至少配置以下逻辑影子账户：

1. `RULE_BASELINE`：只用确定性规则基准。
2. `BASE_CASE_ONLY`：只用冻结 BaseCase，不读取 SpecialistDelta。
3. `BASE_CASE_PLUS_SPECIALIST`：每个被评测专家单独一个臂；不得把多个专家混在“单专家”臂里。
4. `FULL_COMMITTEE`：使用 Phase 6 的完整决定和协议。
5. `APPROVED_SKILL`：只为已人工批准且版本冻结的重要 Skill 建独立臂；未批准候选只能使用 `RESEARCH_ISOLATED` 标签。
6. `CSI300_BENCHMARK` 或 `CHINA_ALL_BENCHMARK`：至少一个可在相同时点重建且成分/PIT 合格的指数基准。
7. `EQUAL_WEIGHT_CANDIDATE`：对当时可见的同一候选集做简单等权基准。

新增 `ShadowArmDefinition` 和 `FrozenWeightProfile`。所有可比较研究臂必须绑定相同：

- `candidate_set_id` 和当时可见的候选成员快照；
- `signal_time`、`earliest_executable_time` 和持有期；
- TradeProtocol 家族、成本、成交、公司行为、复核与退出模型；
- 初始资金、固定名义风险和再平衡日历；
- 股票停牌、涨跌停、T+1、100 股整数、退市和不可成交处理。

臂之间只允许研究组件不同。若候选、时点、协议或成交口径不相同，比较状态固定为 `NOT_COMPARABLE`，不能计算“专家增量”。

## 5. 决策分配、独立性和防止事后挑样本

新增 `ShadowDecisionAssignment`，在结果可见前登记每个 decision/candidate 应进入的全部臂。分配对象保存 decision、BaseCase、SpecialistDelta、DecisionPack、TradeProtocol、候选集、权重、Skill 版本和对象哈希。

同一个正式 study 中：

- 分配一旦登记不能删除；拒绝、`NEEDS_INFO`、`WATCH` 和“无动作”也必须保留，避免只统计买入样本。
- 同一候选必须同时进入所有有资格比较的臂；某臂缺输入时记录 `ARM_INPUT_UNAVAILABLE`，不能悄悄删掉该候选。
- 每个公司/研究事件使用冻结的 `independence_key`。同一 thesis episode 或 60 个交易日结果窗口重叠的决定只算一个独立聚类；只有出现新的官方事件/新 thesis 版本且前一暴露结束后，才允许新独立 key。
- 服务强制 `(study_id, independence_key, arm_id)` 唯一；统计按 independence key 成对，不把同一事件的多个复核当成多个样本。
- 新臂、新 Skill、新权重或排除规则只能创建新 study，不能追溯加入已经看见结果的旧研究。

## 6. 不可变执行与结果观察

新增 `ShadowExecutionObservation`，只接受可重算的结构化执行事实：

- study/arm/assignment/decision/protocol/independence key；
- symbol、market、signal/entry/exit/valuation 时间；
- 动作、股数、入场/退出价格、公司行为现金流、毛盈亏；
- commission/tax/transfer/slippage、净盈亏、成交额、换手和 NAV 前后值；
- MFE、MAE、持有交易日、流动性、成交参与率；
- replay quality、行情 observation/manifest 哈希、成本和成交版本；
- `ambiguous_intrabar_path`、保守结果和乐观敏感性结果；主结果永远使用保守值；
- 所有决策输入的 PIT 状态、可得时间、source snapshot 和排除原因。

服务重新计算金额恒等式、费用、收益率和 NAV，拒绝直接提交一个无法回到成交/估值事实的“收益率”。入场不得早于 `earliest_executable_time`；观察和退出不得早于入场；60 日结果未成熟时只能是 `PENDING_MATURITY`。

正式样本要求：决定及其输入在 signal time 前已冻结，结果行情在 signal time 后观测，关键输入均为 `CERTIFIED` 或 `DOCUMENT_RECONSTRUCTED`，或是有实时 fetch 时间证明的前向不可变快照。`APPROXIMATED` 单列敏感性结果；`NOT_PIT_SAFE` 只进入探索报告，绝不能取得 Phase 8 准入。

## 7. 确定性市场状态

新增 `MarketRegimeSnapshot`，至少保存指数 60 分钟/日线趋势、市场宽度、新高新低、成交额、行业扩散、波动率分位、回撤、风格相对表现和模拟策略表现；“多数策略亏损”只能是一个特征，不能单独决定状态。

首版 `market-regime-v1` 使用固定优先级：

1. `PANIC`：指数回撤不高于 -12%，且波动率分位至少 85% 或市场宽度不高于 20%。
2. `HIGH_VOL_BULL`：日线趋势分数至少 0.20、60 分钟趋势非负、宽度至少 55%、波动率分位至少 70%。
3. `TREND_BULL`：日线趋势分数至少 0.20、60 分钟趋势非负、宽度至少 55%。
4. `TREND_BEAR`：日线趋势分数不高于 -0.20、60 分钟趋势非正、宽度不高于 45%。
5. `RANGE`：核心字段齐全但不满足以上条件。
6. `UNCLASSIFIED`：核心字段缺失、PIT 不安全或时间线冲突。

特征和状态都必须引用当时可得的冻结快照。规则版本改变只产生新状态版本，不覆盖旧标签。

## 8. Walk-forward 和样本外窗口

固定权重不需要训练，因此不设置可调训练权重。所有正式前向决定从 study 生效起即为样本外；评测按 decision time 排序，禁止随机打乱。

- 每个拟评价/调权 Skill 至少 100 个已成熟、相互独立、可成对比较的决定。
- 至少 5 个连续 walk-forward 样本外折，每折至少 20 个独立决定。
- 折边界按时间和 independence key 切分；跨边界且结果窗口重叠的 episode 整体归入较晚折，形成最长 60 交易日 purge，避免结果泄漏。
- 至少覆盖 3 个非 `UNCLASSIFIED` 市场状态，每个至少 30 个独立决定。
- 观察跨度少于 6 个月为 `COLLECTING`；6～12 个月为 `PROVISIONAL`；Phase 8 准入至少 12 个月。
- 5/20 日结果只作早期诊断；正式专家增量和准入只使用已成熟 60 日结果和完整退出结果。
- 同一 independence key 在基准和对照臂缺任一成熟结果时，从成对效应中整体排除并报告原因，不能只保留表现较好的单边。

## 9. 指标、置信区间和多重比较

每个臂、折、市场状态和 Skill 至少报告：

- 扣费后收益、超额收益、NAV、最大回撤；
- MFE、MAE、盈亏比、胜率、持有周期、换手和流动性；
- 总费用、未成交/部分成交、路径不确定比例和保守/乐观差异；
- 1 分钟与 5 分钟差异（仅在合格 1 分钟局部数据真实存在时，不要求默认抓取 1 分钟）；
- PIT 覆盖率及四类状态分布、退市覆盖、历史成分覆盖、被排除样本和原因；
- 决策数量、独立聚类数、成熟数、各状态数和各折数。

专家增量使用同一 independence key 的成对净收益差。`shadow-statistics-v1` 使用固定 hash seed、2,000 次、长度 5 的移动块 bootstrap 计算 95% 百分位置信区间；输入排序和抽样索引完全确定，同一输入哈希必须得到相同结果。胜率同时报告 Wilson 区间。多个 Skill 同时申请升级时，以家族显著性 5% 做 Holm 校正，不能从许多候选中只挑偶然赢家。

所有指标保留精确分母、排除数和 Decimal 序列；报告不能只给一个平均收益数字。

## 10. Phase 8 准入硬门槛

新增不可变 `Phase8AdmissionReport`。只有同时满足以下条件，状态才可为 `ELIGIBLE_RULE_STATE_MACHINE_RESEARCH`：

1. 至少 12 个月、100 个独立成熟决定、5 折且每折至少 20 个、至少 3 个市场状态且每状态至少 30 个。
2. 正式决定输入 PIT 安全率 100%，`NOT_PIT_SAFE=0`；退市/历史成分/公司行为关键覆盖无未解释缺口。
3. 可回放率 100%，`UNREPLAYABLE=0`；双源验证或等价官方重建覆盖至少 90%；路径不确定比例不高于 5%，主结果使用保守路径。
4. 相对 BaseCase-only 的扣费后成对净收益差，Holm 校正后的 95% 置信区间下界大于 0。
5. 至少 4/5 walk-forward 折的增量点估计为正；三个必需市场状态中没有一个呈现置信区间完全低于 0 的明确伤害。
6. 增量臂最大回撤不高于 25%，且不比 BaseCase-only 恶化超过 2 个百分点。
7. 单个决定贡献不超过总正收益的 20%，单一市场状态贡献不超过 70%，避免结果由一个偶然事件或单一行情支配。
8. Phase 6 的所有硬风险门仍成立，权重/候选/协议/成本/成交版本无事后改变，审计为 `PASS`。

未满足样本门返回 `NOT_ELIGIBLE_INSUFFICIENT_SAMPLE`；数据/PIT/比较完整性失败返回 `NOT_ELIGIBLE_INTEGRITY`；样本足够但没有稳定增量返回 `NOT_ELIGIBLE_NO_INCREMENT`。这些都是正常研究结果，不允许人工改成通过。准入只允许进入 Phase 8 的“规则状态机研究”，不允许直接进入赌博机、RL 或真实交易。

## 11. 存储、恢复和唯一事实源

新增 migration `0028_shadow_evaluation.sql`，计划包含：

- `shadow_policy_index`
- `shadow_study_index`
- `shadow_arm_index`
- `shadow_assignment_index`
- `market_regime_index`
- `shadow_observation_index`
- `shadow_evaluation_run_index`
- `shadow_report_index`
- `phase8_admission_index`

完整 manifest、权重、分配、成交观察、折明细、指标序列、bootstrap 结果和准入报告只进入 ObjectStore；SQLite 只保存 ID、版本、哈希、状态、时间、数量和安全统计，不保存私有 thesis、原文、完整成交路径或本地路径。

Parquet 保存可重建的结构化事实序列；DuckDB 只建视图。每个对象记录父哈希和配置哈希。对象已写而索引未写的中断可以补登记；索引存在但对象损坏、输入改变、规则版本改变或成对集合不完整时明确不可恢复，必须新建 run/study。

## 12. CLI 和 Repo Skill 路由

计划增加稳定命令：

- `shadow-schema`：输出 Schema/规则版本和边界。
- `shadow-study-plan`：只校验定义、公平性和预计样本需求，不持久化。
- `shadow-study-create`：创建冻结 study 和臂。
- `shadow-assign`：在结果可见前登记成对分配。
- `market-regime-classify`：从冻结特征生成确定性状态。
- `shadow-observation-record`：校验并登记不可变执行观察。
- `shadow-evaluate`：只读取冻结对象，生成折、指标、置信区间和报告。
- `shadow-status`：返回安全状态、样本/成熟/状态/折计数。
- `shadow-audit`：核对对象、索引、时间线、PIT、公平性、成对集合和计算哈希。
- `shadow-recover`：补齐可证明的中断索引，不重算研究输入。
- `phase8-admission`：生成或读取确定性准入报告；默认预期为不具备资格。

`$astock-research-orchestrator` 负责宽泛状态路由；`$candidate-scan`、`$company-deep-research` 只生成上游冻结研究；`$paper-trading-recovery` 仍只管理用户模拟账户，不得被影子评测借用来绕过隔离；Repo Skill 不直接写 SQLite。

## 13. 分段实现顺序

### P7.1 合同、配置和持久化

交付 Schema、`shadow_evaluation.yaml`、配置哈希、migration 0028、repository 和对象/索引身份；先证明当前真实状态是 `NOT_RUN`。

### P7.2 冻结 study、公平分配和市场状态

交付 study/arm/weight、同候选同协议约束、独立 key、结果前分配和 `market-regime-v1`；未批准 Skill、未来输入和事后加臂必须拒绝。

### P7.3 执行观察、成熟度和成对集合

交付结构化执行观察、金额/费用/NAV 复算、PIT/时间线/回放质量、保守路径、5/20/60 日成熟状态和成对排除报告。

### P7.4 Walk-forward、统计和准入

交付连续折、成对指标、确定性 block bootstrap、Wilson、Holm、多状态/稳定性/回撤门和 Phase8AdmissionReport。

### P7.5 CLI、审计、恢复、Repo Skill 和终验

交付全部 CLI、审计/恢复、状态探针、Repo Skill、合成性质测试、真实 migration 和空真实库终验。真实 observation 不足时必须保留 `NOT_RUN/INSUFFICIENT_SAMPLE`。

每个子阶段独立提交并推送；计划文件本身先于任何 Phase 7 代码提交。

## 14. 自动化测试与验收矩阵

1. 权重和为 1、版本/时区/Decimal/排序/唯一性、状态机和金额恒等式 Schema 测试。
2. 相同 study/输入/config 产生相同 ID、对象哈希、折、bootstrap 索引、指标和准入结果。
3. study 生效前决定、决定后才可得的输入、未来市场状态、未来修订和 `NOT_PIT_SAFE` 正式样本均被拦截。
4. 结果出现后添加臂、删除失败决定、改变权重/候选/协议/成本或只排除对照单边均被拒绝。
5. 同一 episode 多次复核只计一个独立聚类；跨折重叠结果窗口被整体 purge，不发生泄漏。
6. 六类市场状态固定优先级、边界值、缺字段和相同输入稳定性测试。
7. 5/20/60 日成熟边界、交易日而非自然日、T+1、停牌、涨跌停、100 股、部分成交、退市和公司行为测试。
8. 毛盈亏、费用、净盈亏、收益率、NAV、MFE/MAE、回撤、换手和路径敏感性由原始字段重算；直接伪造收益拒绝。
9. 同时触发止盈/止损时主结果取保守路径；合格局部 1m 不存在时不补造。
10. 成对集合缺一臂时整体排除且计数；BaseCase 与专家使用不同候选/协议时 `NOT_COMPARABLE`。
11. bootstrap、Wilson、Holm 用手算小样本、常量序列、全赢/全输、负收益和空样本验证；空/不足样本不产生伪置信区间。
12. 99、100 个独立决定，29/30 个状态样本，5.9/6/12 个月，4/5 折和回撤/路径阈值边界测试。
13. 有正均值但 CI 跨 0、单一事件/状态贡献过高、回撤恶化或一折严重亏损时不得准入。
14. 故障注入后 recover 只补安全索引；对象、索引、父哈希、计算结果或准入报告篡改被 audit 发现。
15. 影子服务不能接受用户主模拟账户 ID，不能连接券商，不能修改已有 paper account/order/fill/position/journal 数量。
16. 私有原料、正文、Cookie、Profile、runtime 和真实成交路径不进入 Git；CLI 错误不回显私密 payload。
17. 全仓 `uv run pytest`、`uv run ruff check .`、`uv run pyright` 通过；live/长观察测试继续由显式环境门控制。

## 15. 可回滚点

- migration 只新增表，不改 Phase 1～6 的既有语义；停用 Phase 7 代码不影响已有 Artifact、委员会或模拟账本。
- 配置和统计方法升级必须新建 policy/study 版本，旧报告和准入结论永久保留。
- 错误观察通过新版本 supersede，不物理删除；正式报告明确使用的版本集合。
- 某专家失败只回退/禁用对应研究版本，不删除 BaseCase、其他专家或历史样本。
- Phase 8 研究失败时回到本阶段固定权重基线；不回写历史权重或重选样本。

## 16. 预算、人工事项和阶段交付边界

本地确定性开发不产生外部 LLM/API 费用；日常测试使用合成完整 PIT/成交夹具，不联网。真实前向观察沿用已有 Provider、对象缓存和低频采集策略，所有外部 live smoke 由显式开关控制。

开发期预计 5～8 个开发日；真实证据观察期至少 6～12 个月，不能压缩成一次回测。Phase 7 的程序交付时会报告当前真实样本数、观察跨度和缺口。只有用户未来批准候选 Skill、处理上游平台访问限制或提供真实人工监控仓位语义时，才需要人工介入；这些事项统一在全部开发工作后汇总，不在本阶段中途打断其他可完成任务。
