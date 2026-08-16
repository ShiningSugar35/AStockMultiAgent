# 低成本 A 股多 Agent 投研系统方案

> 说明：本文件根据此前完整讨论、已确认的业务目标、数据边界和专家体系重建。
> 它用于描述“系统要做什么、为什么这样做、最终应具备哪些能力”，**不是当前 GitHub 仓库的开发进度或实施计划**。
> 文档职责固定为：总方案只写长期设计；当前实现、未完成项和验收事实分别以《开发计划》和《验收报告》为准。
> 当前代码进度、提交、测试和阶段状态，应以仓库中的 `开发计划.md`、`验收报告.md`、PM 状态提交和阶段验收报告为准。
> 核心安全边界：系统不自动向券商发送订单；真实交易只由用户在券商端人工执行。
> 来源访问优先级固定为：官方/已验证 API 或本地数据 → MCP → Browser → Manual Task。
> 知识蒸馏主链固定为：SourceItem → ParagraphUnit → ArgumentUnit → SkillCandidate；`ParagraphUnit` 是原文存储和定位单位，完整 `ArgumentUnit` 才能产生最终语义判断。

---

# 一、项目定位

## 1.1 项目目标

建设一套：

> **低成本、证据可追溯、A 股为主、兼顾波段与中长期价值投资的半自动多 Agent 投研、持仓监控、模拟交易和持续复盘系统。**

系统主要帮助用户完成：

1. 从全市场或指定范围发现值得研究的候选标的；
2. 对用户指定公司或标的进行全面投研；
3. 对 A 股公司财务可信度、治理和监管风险进行红旗审计；
4. 结合产业链、供需、估值、趋势、政策和事件进行综合判断；
5. 主动识别自动化无法获得、但人工低成本可补充的关键证据；
6. 形成有来源、有反方、有失效条件的研究结论；
7. 建立模拟交易协议并持续验证判断；
8. 对持仓进行增量监控、加减仓和退出复核；
9. 记录各 Agent、Skill 和策略的真实历史表现；
10. 在积累足够样本后，研究市场状态识别、策略权重调整和有限强化学习。

系统不允许大模型绕过风险规则，也不保留默认自动连接券商下单接口。

---

## 1.2 运行方式

```text
用户进入 D:\AStockMultiAgent
        ↓
启动 Codex & openCode & openClaw & 网页端AI（ 如 ChatGPT & Gemini ）
        ↓
输入自然语言任务
        ↓
AI 按需调用本地 Repo Skills 和 Python CLI，网页端则直接访问同步更新的Github仓库
        ↓
本地程序或网页端完成数据、计算、证据、账本和风险门槛
        ↓
AI读取压缩后的冻结工件，生成结构化分析
        ↓
Schema、引用和 Policy 校验
        ↓
输出报告、观察名单、模拟交易协议或 NEEDS_INFO
```

典型指令：

```text
请给出最近值得优先研究的 A 股标的。
```

```text
分析宁德时代目前是否值得进入模拟仓位。
```

```text
检查我的全部模拟持仓（系统自动更新模拟账户，并检查哪些投资逻辑变强、变弱或已经失效，或基本面发生变化？以及上次关闭后是否触发止盈、止损或论文失效条件）
```

---

# 二、现实约束与设计原则

## 2.1 硬件和预算约束

成本约束：

- 优先使用免费公开数据；
- 不购买全市场高频数据作为 MVP 前提；
- 不让多个大模型 Agent 重复读取同一批资料；
- Codex&openCode 等用于交互式研究和开发；
- Python 负责可确定的计算；
- 可选第三方模型只处理少量边界样本；
- 系统在关闭大模型能力后仍可完成数据同步、审计、模拟盘恢复和硬风控。

---

## 2.2 核心原则

1. **Agent 是职责模块，不等于每个模块都运行一个大模型。**
2. **数字由代码计算，大模型只解释和综合。**
3. **事实、管理层说法、第三方估计和 Agent 推断必须分开。**
4. **每个重要结论必须回链原始来源。**
5. **委员会只能消费冻结工件，不能临时重新搜索。**
6. **关键证据不足时必须输出 `NEEDS_INFO`。**
7. **社区内容只能生成线索和方法，不直接充当事实真相。**
8. **财务低估之前先判断财务数据是否值得信任。**
9. **回测、模拟盘和实盘逻辑尽量共用同一策略代码。**
10. **先固定规则和权重，后研究动态权重和强化学习。**
11. **所有原始资料不可覆盖，派生结果必须可重建。**
12. **不因某个数据源失效导致整个系统瘫痪。**

---

# 三、总体架构

```text
数据采集与质量控制
        ↓
原始对象存储 + Parquet 事实层 + PIT 元数据
        ↓
统一证据库 Claim—Evidence
        ↓
确定性市场扫描与专家候选生成
        ↓
候选注册、去重和硬性初筛
        ↓
通用投资内核 BaseCase
        ↓
专家 Skill 路由
        ├── Serenity 专家
        ├── 作者蒸馏专家
        ├── 财务可信度审计
        ├── 行业/政策/事件专家
        └── 波段与趋势专家
        ↓
信息补全与人工调查任务
        ↓
反方与证据审查
        ↓
冻结工件委员会
        ↓
程序化风险管理
        ↓
模拟交易 / 真实持仓只读监控
        ↓
绩效归因、Skill 评测和市场状态研究
```

---

# 四、数据体系

## 4.1 多时间尺度行情

系统不以日线替代全部波段研究，也不依赖全市场长期分钟数据。

### 5 分钟

主要用于：

- 模拟成交；
- 系统关闭期间的路径回放；
- 止盈、止损和移动止损；
- 最大有利/不利变动；
- 持仓价格条件监控；
- 近期波段结构。

默认来源：

- 东方财富 5 分钟；
- 新浪 5 分钟作为备用和交叉验证。

模拟成交必须使用未复权原始价格。

### 60 分钟

主要用于：

- 波段趋势；
- 行业相对强弱；
- 突破与回撤；
- 波动率；
- 市场宽度；
- 市场状态识别。

长期小时线优先由本地持续积累的 5 分钟数据聚合生成，避免不同供应商切线口径不一致。

### 日线和周线

用于：

- 中长期趋势；
- 估值分位；
- 行业周期；
- 基本面验证；
- 长线持有逻辑；
- 市场状态和组合风险。

### 财务周期

季度、半年和年度数据用于：

- 收入、利润和现金流；
- 资本回报；
- 资产负债质量；
- 经营验证；
- 财务可信度；
- 估值和长期论文。

---

## 4.2 行情数据原则

1. 保存原始未复权行情；
2. 单独保存公司行为；
3. 本地生成版本化复权研究序列；
4. 复权价格不能用于模拟成交；
5. 成交量统一为股、成交额统一为元；
6. 原始单位必须保留；
7. 双源冲突不能简单平均；
8. 请求失败不能覆盖上次有效 canonical 数据；
9. 缺柱不能静默插值；
10. 每批次记录请求范围、实际范围、时间戳语义、缺失、重复和质量等级。

建议回放等级：

```text
DUAL_SOURCE_5M_VERIFIED
SINGLE_SOURCE_5M
PROVIDER_1H_APPROX
DAILY_OPEN_MODEL
DAILY_CLOSE_MODEL
DAILY_CONSERVATIVE
UNREPLAYABLE
```

---

## 4.3 官方事实层

A 股核心事实源包括：

- 巨潮资讯；
- 上交所；
- 深交所；
- 北交所；
- 证监会；
- 证券期货市场失信记录；
- 公司投资者关系页面；
- 上证 e 互动；
- 深交所互动易；
- 上证路演中心；
- 国家企业信用信息公示系统；
- 中国执行信息公开网；
- 中国裁判文书网；
- 信用中国；
- 中国结算质押信息；
- 港交所互联互通持股；
- 融资融券数据；
- 国家统计局；
- 人民银行；
- 财政部；
- 国家发改委；
- 国家能源局；
- 国内期货交易所；
- 行业监管部门。

官方原文优先于聚合数据库。

---

## 4.4 行业和外部交叉证据

针对不同公司，补充：

- 客户和供应商公告；
- 招投标和政府采购；
- 环评、能评和项目备案；
- 排污许可；
- 土地、矿权和采矿许可；
- 产品认证；
- 专利和标准；
- 临床试验和药品注册；
- ICP 和 App 备案；
- 产品召回和质量抽检；
- 海关和 UN Comtrade；
- 商品价格和期货结构；
- 竞争对手财报与电话会；
- 行业产能、库存和资本开支；
- 招聘和门店等低等级观察数据。

这些数据用于验证公司自己的说法，不能都只听公司财报。

---

# 五、证据与防幻觉

## 5.1 来源等级

### 强证据

- 法定公告；
- 交易所文件；
- 监管文件；
- 审计报告；
- 官方合同；
- 招标和中标；
- 项目审批；
- 环评、能评和许可；
- 专利、标准和认证；
- 官方客户或供应商披露。

### 中等证据

- 可信财经媒体；
- 行业媒体；
- 行业协会；
- 专业研究；
- 公司产品和官网；
- 多家公司公开信息的交叉印证。

### 弱证据

- KOL；
- 知乎；
- 社交媒体；
- 论坛；
- 截图；
- 未署名渠道调研；
- 单纯价格和成交量异动。

弱证据只能作为线索，不能单独支持高置信结论。

---

## 5.2 Claim—Evidence

每个关键判断拆成 Claim：

```json
{
  "claim_id": "C-001",
  "claim": "新增产线计划在未来一年释放产能",
  "claim_type": "management_guidance",
  "evidence_ids": ["E-001"],
  "confidence": 0.72,
  "analyst_inference": false
}
```

Evidence 至少记录：

```json
{
  "evidence_id": "E-001",
  "source_type": "exchange_filing",
  "document_title": "...",
  "published_at": "...",
  "source_url": "...",
  "page": 43,
  "section": "在建工程",
  "excerpt_hash": "...",
  "strength": "strong"
}
```

所有重要数字必须能回到对象哈希、页面、区块或网页定位。

---

## 5.3 Point-in-Time

数据必须标记：

```text
CERTIFIED
DOCUMENT_RECONSTRUCTED
APPROXIMATED
NOT_PIT_SAFE
```

以及：

```text
ORIGINAL_DOCUMENT_TIMESTAMP
ARCHIVED_SNAPSHOT
PROVIDER_METADATA
CURRENT_PROVIDER_VALUE
MANUAL_ASSUMPTION
```

规则：

- `NOT_PIT_SAFE` 不进入正式历史 Agent 评测；
- 系统启用后持续快照，逐步建立真实 PIT 库；
- 处罚、问询结果可以作为未来标签，不能泄漏到历史输入；
- 回测必须披露 PIT 覆盖率；
- 必须考虑退市股和历史成分，避免幸存者偏差。

---

# 六、财务可信度与红旗审计 Agent

正式名称：

> **Financial Integrity & Red-Flag Agent
> 财务可信度与红旗审计 Agent**

它不能证明“公司一定造假”，只能识别：

- 已验证事实；
- 重算结果；
- 红旗；
- 跨期异常；
- 同行异常；
- 文档冲突；
- 治理风险；
- 可能的正常解释；
- 数据质量解释；
- 证据缺口；
- 风险等级和硬阻断。

---

## 6.1 方法来源

推荐组合：

```text
AuditAgent 的审计方法论
+
leafpaper/claude-company-analysis 的可复用确定性规则
+
FinRobot 的编排和 provenance 思想
+
PyOD/PyGOD 的异常发现
```

### AuditAgent

借鉴：

- 会计科目风险先验；
- 稀疏检索和语义检索；
- 单份财报专家；
- 跨年度专家；
- 跨科目专家；
- 全局证据链聚合。

不把处罚结果泄漏到更早分析时点。

### leafpaper

拆用：

- Beneish；
- Piotroski；
- Altman；
- Sloan；
- DuPont；
- 现金流质量；
- PDF 关键段落；
- 红旗审核；
- 缺口闭环。

不整套复制多写手长报告流水线，不直接采用其总评分。

### FinRobot

仅借鉴：

- Lead/Worker 职责分离；
- 数值计算与文本解释分离；
- provenance；
- Bull/Bear/Judge；
- 确定性工具接口。

不整套安装为运行时依赖。

---

## 6.2 审计内部结构

```text
确定性报表校验
├── 三表勾稽
├── 资产负债表恒等式
├── TTM、同比和环比重算
└── 每股数据与股本重算

盈余质量规则
├── Beneish M
├── Sloan Accrual
├── CFO/净利润
├── 应收/收入
├── 存货/成本
├── 研发资本化
└── 非经常损益

跨期异常
├── 3—10年趋势
├── 会计政策变化
├── 科目重分类
├── 年末突击变化
└── 历史更正

同行异常
├── 行业分位
├── 稳健 Z-score
├── Isolation Forest
└── PyOD

文本审计
├── 收入确认
├── 关联方
├── 商誉和减值
├── 或有事项
├── 关键审计事项
└── 管理层解释

治理审计
├── 审计意见
├── 审计机构变化
├── 财务负责人变化
├── 资金占用
├── 质押和担保
└── 处罚、问询和诉讼

解释竞争
├── 风险解释
├── 正常经营解释
├── 数据质量解释
└── 最低成本补证路径
```

---

## 6.3 行业条件化

传统公式不能机械应用于所有行业。

至少区分：

- 一般制造；
- 软件和互联网；
- 消费；
- 医药；
- 银行；
- 保险；
- 证券；
- 房地产；
- 早期生物科技；
- 资源和周期；
- 重资产项目公司。

不适用时输出 `NOT_APPLICABLE`，不能强行得分。

---

## 6.4 输出工件

```text
FinancialIntegrityEvidencePack
├── company_id
├── reporting_periods
├── raw_source_ids
├── verified_numbers
├── recalculated_metrics
├── red_flags
├── time_series_anomalies
├── peer_anomalies
├── document_conflicts
├── governance_findings
├── alternative_explanations
├── evidence_gaps
├── requested_followups
├── risk_level
├── hard_blocks
└── model_and_rule_versions
```

严重审计意见、监管立案、三表重大勾稽失败可硬阻断；单一 Beneish 超阈值只能作为线索。

## 6.5 机构级基本面模型层

新当前公司研究在进入专家 Delta 和投资委员会前，先构建一套共享、确定性、可审计的机构级基本面模型。它不是新增一组人格 Agent，而是把所有 Agent 共用的经济驱动、预测和估值主账本固定下来：

```text
FrozenEvidencePack
→ EvidenceSufficiencyReport
→ IndustryProfile / CompanyEconomicsProfile
→ DriverTree
→ Bull / Base / Bear ForecastPack
→ ValuationPack / Market-Implied Expectations
→ FundamentalModelBundle
→ InstitutionalDecisionContext
→ BaseCase / Specialist Delta / ResearchMemo
→ Investment Committee
```

设计约束：

- Evidence Sufficiency 以 material claim 为单位区分事实、管理层主张、行业估计、解释、因果主张、预测和估值假设；authority、directness、independence、freshness、scope、extraction 与 PIT 分开记录，不压成一个伪精确总分。重复转载属于同一 independence group，不能增加独立证据数。
- IndustryProfile 与 CompanyEconomicsProfile 是 canonical artifacts；没有认证行业 taxonomy 时只能标 `PROVISIONAL_TAXONOMY`。
- DriverTree 是可复算 DAG。Bull/Base/Bear assumptions 绑定 claim/evidence 或明确 provenance，最终 Forecast 数值必须由 Python 计算，不由 LLM 直接填写。
- Valuation 与 Forecast 共用同一 driver assumptions。FCFF DCF、reverse DCF、mid-cycle normalized、archetype-specific 方法和 sensitivity 均为确定性计算；Serenity Growth/Valuation 只能提供增量研究视角，不能维护第二套平行数值账本。
- `MarketPriceAnchor` 将 current price 与已注册 source artifact/object hash、observed_at、available_to_system_at 一起冻结。没有 PIT price anchor 时仍可输出内在价值区间，但不计算 expected return 或市场隐含预期。
- `FundamentalModelBundle` 绑定六个组件 exact artifact/hash；`InstitutionalDecisionContext` 只压缩 decision horizon、investment thesis、variant perception、3–5 key drivers、competing hypotheses 和 portfolio context，避免把完整 EvidencePack 再塞给委员会。
- Bundle/DecisionContext 是委员会共享 PRIMARY 输入，不是新投票成员。委员会 conviction/risk cap 不能直接等于最终组合权重；实际组合配置仍由 Portfolio layer 在相关性、风险贡献、流动性和总敞口约束下决定。

---

# 七、专家投研体系

## 7.1 统一投资内核

所有标的只构建一次 BaseCase：

```text
BaseCasePack
├── 商业模式
├── 收入和利润驱动
├── 现金流和资本回报
├── 再投资能力
├── 管理层和治理
├── 竞争优势
├── 行业供需
├── 估值和隐含预期
├── 日线趋势
├── 小时波段上下文
├── 已知风险
├── 证据缺口
├── 专家标签
└── 基础置信度
```

后续专家只能输出增量 Delta，不能重写整份公司报告。

---

## 7.2 Serenity 专家体系

### `muxuuu/serenity-skill`

定位：

> 产业链瓶颈研究总流程。

核心逻辑：

```text
市场叙事
→ 系统变化
→ 必要部件
→ 产业链层级
→ 稀缺约束
→ 公司候选
→ 证据
→ 市场可能忽视的地方
→ 证伪条件
```

适合：

- AI 基建；
- 半导体；
- CPO；
- 先进封装；
- 电力设备；
- 机器人；
- 材料；
- 测试设备；
- 产业链供需错配。

应保留：

- 产业链层级优先；
- 强中弱证据阶梯；
- 反方和证伪；
- 公司候选排序；
- 来源路径。

固定评分权重只作参考，不作为本地系统的自动权重。scarcity、substitution、value-capture、产业链层级等可审计维度可以进入 `SpecialistDelta`；aggregate score 仍保持 report-only。

---

### `haskaomni/serenity-skill`

拆为六个互补方法 Skill，外加一个本地冻结引用的 ResearchMemo Composer：

#### Serenity Alpha

```text
新闻
→ 已发生的需求变化
→ 财务传导
→ 小公司弹性
→ 未来1—4季度验证
```

#### Bayesian Intrinsic Growth

- 将未来增长拆成多个概率假设；
- 根据新证据做概率更新；
- 区分内在增长和 FOMO；
- 比较内在增长与市场隐含增长。

#### GF-DMA

- 基本面速度；
- 20/50/100/200 日均线；
- ATR；
- 价格与均线偏离；
- 趋势平行度；
- 预期上修。

A 股缺乏一致预期时必须降级，不能编造。

#### TAM-Adj-PEG

- 增长速度；
- 增长持续时间；
- TAM；
- 商业质量；
- 资本开支；
- 稀释；
- 周期性。

#### Juglar Cycle Stock Stage

- 先判断行业固定资产投资周期，再判断公司自身经营阶段；
- 需求、ASP、利润率、Capex、库存、产能释放、客户行为、资本市场反应八维评分；
- 同时输出复苏 / 扩张 / 过热 / 衰退 / 出清五阶段概率；
- 行业周期、公司经营阶段、股票定价阶段必须分开；
- 必须保留反证和可观察迁移信号，不能用股价、PE 或叙事直接判周期。

在本项目中它编译为 `JuglarCycleStageSkill / juglar-cycle-stage-v1` typed contract，只返回 `SpecialistDelta`；除资本市场反应可使用 secondary evidence 外，其余核心维度、反证和迁移信号优先要求官方强证据。

#### Buy-Side Memo / ResearchMemo Composer

- 投资观点；
- Bull/Base/Bear；
- 估值；
- 催化剂；
- 反方；
- 监控指标；
- 来源列表。

---

## 7.3 作者专家知识

来源：

- MR Dang；
- 黄彦臻；
- 派大星皮皮；
- 寒武纪的鳄鱼；
- 《价值投资功法》；
- 已整理 DOCX；
- 知乎回答、文章、想法、专栏和评论链。

作者不是完整人格 Agent，而是知识来源和差分方法。

系统应抽取：

```text
共享分析原语
共享证据规则
共享持仓规则
作者差分
作者冲突
作者静默领域
```

---

## 7.4 专家路由

路由不能只看申万行业，还要看：

- 收入来源；
- 资产类型；
- 当前投资命题；
- 价值驱动；
- 证据缺口；
- 风险暴露；
- 持有周期。

第一版采用：

```text
规则和标签
→ 关键词
→ Embedding
→ 只有边界案例才让模型判断
```

每次默认最多加载 1—3 个必要专家。

---

## 7.5 SpecialistDelta

```json
{
  "skill": "energy_supply_constraint",
  "incremental_findings": [],
  "base_case_corrections": [],
  "industry_specific_metrics": [],
  "additional_evidence_requests": [],
  "failure_modes": [],
  "confidence_delta": -0.08,
  "valuation_adjustments": [],
  "risk_adjustments": []
}
```

---

# 八、知识采集与蒸馏

## 8.1 白名单作者

至少包括：

- MR Dang；
- 黄彦臻；
- 派大星皮皮；
- 寒武纪的鳄鱼。

采集范围：

```text
回答全集
想法全集
文章全集
专栏
专栏文章
必要问题上下文
正文图片
图片文字
图片式表格
```

只做用户授权白名单，并保留后续增量更新的作者接口

---

## 8.2 历史全集要求

每种内容：

1. 从第一页或起始游标开始；
2. 连续遍历至明确终止；
3. 不能只抓热门、近期或搜索结果；
4. 每页、每篇和每个评论分页提交检查点；
5. 未抓到不等于不存在；
6. 失败、受限和未知游标进入缺口；
7. 完成全量快照后转为增量同步。

统计：

```text
discovered
scheduled
success
failed
restricted
duplicate
updated
missing
last_cursor
terminal_condition
```

---

## 8.3 评论链

原始评论节点：

```text
comment_id
content_id
author_id
parent_id
reply_to_comment_id
root_comment_id
content
created_at
updated_at
cursor
fetch_status
source_snapshot_id
```

知识层保留：

- 根评论到目标作者回复的祖先路径；
- 作者直接回复对象；
- 后续追问和作者再次回复的完整分支；
- 理解作者观点所需上下文。

与作者从未互动的分支不进入默认知识库。

---

## 8.4 图片和表格

### 普通图片

先分类：

```text
DECORATIVE
PHOTO
TEXT_SCREENSHOT
TABLE
CHART
DIAGRAM
FORMULA
UNKNOWN
```

- 文字截图使用本地 OCR；
- 表格使用结构识别；
- 图表保留原图、图注和轴标签；
- 示意图不能自动臆测关系；
- 低置信结果进入人工复核。

### PDF 图片式表格

《价值投资功法》中图片表格应输出：

```text
原始图片
JSON
CSV
Markdown 或 HTML
质量报告
```

简单表格转 Markdown，复杂合并单元格优先保留 HTML。

不确定值明确标记，不能猜测。

---

## 8.5 DOCX 和 Markdown

《寒武纪的鳄鱼 知乎文章》DOCX 应转为：

- 原始转换 Markdown；
- 图片资源；
- 表格资源；
- 清洗版；
- 方法和案例索引；
- 来源和缺口报告。

每篇内容 Markdown 使用 frontmatter 保存：

```yaml
source_id:
author_id:
content_id:
content_type:
title:
question_title:
column_id:
source_url:
published_at:
updated_at:
source_snapshot_id:
content_hash:
rights_status:
collection_status:
cleaning_status:
methodology_labels:
lifecycle_stages:
```

---

# 九、知识清洗

## 9.1 不能直接按段落蒸馏

必须区分：

```text
ParagraphUnit
→ ArgumentUnit
→ MethodRule / SkillCandidate
```

自然段只是来源定位单元。

例如第一段提出问题、后面多段解释时，必须合并成完整论证单元。

修辞角色包括：

```text
TITLE
BACKGROUND
MARKET_OBSERVATION
SETUP_QUESTION
CLAIM
EXPLANATION
CAUSAL_REASON
EVIDENCE
EXAMPLE
COUNTERARGUMENT
CONCLUSION
ACTION_RULE
RISK_WARNING
TRANSITION
MARKETING
DAILY_CHATTER
```

同时区分：

```text
topic_relevance
methodological_completeness
context_value
standalone_distillable
```

只有设问的段落不能单独蒸馏，也不能直接删除。

---

## 9.2 低成本清洗漏斗

### Level 0：结构恢复和去重

- HTML/Markdown 恢复；
- 模板和签名去除；
- Unicode 规范；
- 精确重复；
- SimHash/MinHash 近重复；
- 同一文章在不同入口的关系；
- 版本差异。

### Level 1：规则多维评分

评分：

- 投资相关；
- 方法密度；
- 证据密度；
- 持仓相关；
- 公司/行业特异性；
- 故事噪声；
- 营销噪声；
- 日常闲聊。

规则只分流，不直接永久删除。

### Level 2：本地轻量 Embedding

使用小型中文向量模型，对完整 ArgumentUnit 和局部窗口编码。

原型至少包括：

- 商业模式；
- 财务质量；
- 估值；
- 行业周期；
- 产业链；
- 选股；
- 建仓；
- 持仓验证；
- 加仓；
- 减仓；
- 退出；
- 风险；
- 反证；
- 组合；
- 复盘；
- 非投资生活；
- 营销互动。

### Level 3：少量人工标签 + 线性分类器

在 Embedding 上训练：

- Logistic Regression；
- Linear SVC。

目标是高召回保留价值内容，不追求激进删除。

### Level 4：便宜模型审核包

只将边界样本导出，由用户手动交给便宜模型。

### Level 5：人工终审

只有人工批准内容可以进入生产 Skill。

---

## 9.3 蒸馏内容

蒸馏的不是文风，而是：

- 决策问题；
- 适用条件；
- 推理步骤；
- 必要证据；
- 正向信号；
- 负向信号；
- 失效条件；
- 常见失败模式；
- 适用行业；
- 持有周期；
- 风险边界。

每条规则必须绑定原始来源。

---

# 十、持仓生命周期

系统不能只会选股。

## 10.1 建仓

- 首次建仓条件；
- 初始仓位；
- 分批建仓；
- 价格和估值门槛；
- 证据门槛；
- 不追高条件。

## 10.2 持仓验证

- 需要验证哪些经营指标；
- 订单、收入、利润率、现金流；
- 客户和产能；
- 时间窗口；
- 暂未验证与证伪的区别。

## 10.3 加仓

- 新证据；
- 基本面验证；
- 回调与突破；
- 最大仓位；
- 禁止摊低的条件。

## 10.4 减仓

- 估值过热；
- 价格领先基本面；
- 催化兑现；
- 周期反转；
- 集中度；
- 风险收益恶化。

## 10.5 退出

- 论文失效；
- 事实被推翻；
- 财务可信度恶化；
- 治理风险；
- 价格止损；
- 时间止损；
- 移动止损；
- 机会成本；
- 最大持有期。

## 10.6 复盘

- 原始依据；
- 后续事实；
- 正确和错误判断；
- 盈亏归因；
- 市场 Beta；
- Skill 增量价值；
- 是否修改规则。

---

## 10.7 持仓工件

```text
PositionMonitoringPlan
HoldingEvidenceUpdate
HoldingReviewPack
PositionActionProposal
ExitReviewPack
```

每次持仓复核先比较上次冻结时点与当前时点，只分析增量变化。

---

# 十一、信息补全 Agent

它生成用户可执行的人工调查任务。

每次最多提出 3—7 项、一小时内可完成、可能改变投资结论的任务。

任务卡：

```text
任务名称
待验证主张
影响的投资结论
优先级
预计耗时
免费信息源
复制搜索词
操作步骤
需要保存
支持信号
反驳信号
信息源局限
完成条件
替代证据
```

原则：

- 优先官方和免费；
- 未搜索到不等于不存在；
- 核对公司全称、子公司和历史名称；
- 达到足够证据后停止，不无限研究；
- 不自行认定舞弊。

---

# 十二、候选股票池

## 12.1 候选生成

来源：

- 纯 Python 因子；
- 公告事件；
- 财务异常；
- 行业和商品代理变量；
- Serenity 产业瓶颈；
- 作者专家 Skill；
- 用户指定标的。

## 12.2 低成本 Research Seeds

为避免每次“推荐几只股”都对数千只 A 股逐一执行公告、财务、证据和多 Agent 深研，候选系统之前增加一层**无推荐权的 Research Seeds**。它只负责把全市场压缩成值得进一步花成本的有限集合，不生成 CandidateRecord、BUY、目标价或仓位。

Research Seeds 合并三类来源：

1. **Existing Candidate Seeds**：复用当前可见、已审计的 `RESEARCH_READY` Candidate；
2. **Market Seeds**：使用当前公开市场快照，仅按流动性、成交规模、流通市值和换手做研究优先级排序，不把涨跌幅方向当推荐信号；
3. **Expert Skill Seeds**：从当前已发布的几位作者 Skills 动态生成 `ExpertDomainProfile`，再与当前公开行业板块及其成分股交叉。

Expert Skill Seeds 不硬编码“某作者擅长什么行业”。每次运行都从已发布 Skill 的名称、决策问题、核心原则、适用条件、所需证据及正负信号重新统计对当前行业 taxonomy 的支持密度；只有达到版本化最小 Skill 数和占比的领域才进入作者画像。相同 Skill 集合重复命中的父/子行业板块去重，避免一个大类占满 Top-N。没有达到行业密度门的作者保持“无行业型 Expert Seed”，但其通用方法 Skill 仍可在单股 Deep Research 中使用。

ExpertDomainProfile 和 Market Seeds 使用的公开市场/板块响应都先冻结为 `SourceSnapshot`；`ResearchSeedReport` 绑定当前 composite Knowledge registry release/hash，并可独立 audit。行业板块只用于研究范围发现，不允许充当公司正式事实证据。

```text
Existing RESEARCH_READY Candidate
        +
Market Seeds
        +
Expert Skill Seeds
        ↓
ResearchSeedReport（无推荐权）
        ↓
Seed Promotion
        ↓
CandidateInstrumentUniverseProof + 官方公告 / 财务 / PIT / 质量 / 公司行动证据
        ↓
自动 CandidateInputRelease → Candidate Scan → Deep Research → Committee
```

Promotion 只自动化取证、组装、校验与扫描，不自动降低 Candidate 门槛。普通 `CandidateInputRelease` 的 Instrument Master 仍要求与候选全集 exact match；只有 Promotion 可生成 `CandidateInstrumentUniverseProof`，把 `ResearchSeedReport → parent Instrument Master → exact bounded seed subset` 的 object hash/版本/快照完整冻结。单只 Seed 缺公告、财务、公司行动或 reference 时只返回结构化 task 并隔离该公司，不拖死整批，也不把缺口解释为“无风险”。

## 12.3 中央候选注册表

负责：

- 合并去重；
- 股票代码统一；
- 来源；
- 推荐理由；
- 生命周期；
- 流动性；
- 可交易性；
- 风格和行业暴露；
- 数据完整度；
- 证据完整度；
- 组合已有敞口。

删除“昂贵的股票池 LLM Agent”，保留候选系统。

## 12.4 组合评估与构建

候选系统与组合系统必须分离：候选只负责发现“值得研究”的标的；只有完成单股正式研究、投委会和 TradingClassification，并获得当前 `APPROVE_SIMULATION` 的 `ClassifiedTradeProtocol`，才有资格进入组合构建。

组合层分成两类任务：

```text
Portfolio Review
当前持仓 → PIT 日线矩阵 → 风险/集中度/相关性/尾部风险/风险贡献

Portfolio Construction
已准入单股 → 同一 as_of 对齐 → 多模型提案 → 统一硬约束 → 保留现金
```

组合评估至少输出：

- 年化波动和下行波动；
- Beta 和 Tracking Error；
- 最大回撤；
- 历史 VaR / CVaR / CDaR；
- HHI 与有效持仓数；
- 两两相关性；
- 边际风险贡献；
- 总敞口、现金、行业/风险组暴露；
- 数据质量、PIT 和公司行动风险提示。

组合构建不把单一均值—方差优化器视为“标准答案”，固定比较四类方案：

1. **受约束等权**：默认稳健基准，减少预期收益估计误差；
2. **逆波动率**：只使用波动信息分配风险；
3. **层次风险分配（HRP-style）**：按相关簇和簇内风险递归分配；
4. **收缩协方差最小方差**：使用 Ledoit-Wolf covariance shrinkage，再做 long-only minimum-variance。

统一约束优先于优化结果：禁止杠杆，执行单股、总敞口、组暴露和其他版本化风险上限；无法安全分配的部分保留现金，不强行满仓。默认方法保持受约束等权，除非真实 Phase 7 样本外证据证明其他方案在相同候选集、费用、PIT 和约束下具有稳定增量价值。

当前项目没有已经验收的正式申万/中证行业 taxonomy release，因此组合构建中的 `risk_group` 只能作为显式来源的辅助组约束；系统必须标记其 provenance，不得把调用者或模型自行判断的组标签冒充官方行业分类。

实现参考吸收的是成熟开源组合库和专业组合管理中的共同原则：风险模型、目标函数和约束分离；协方差收缩；HRP/风险预算；CVaR/CDaR 等尾部风险；walk-forward / 样本外检验；以及简单等权基准。项目不直接复制第三方库的“最优组合”结论，所有正式权重仍由本地确定性代码、冻结输入和本项目风险规则生成。

---

# 十三、委员会与风险

委员会读取：

```text
BaseCasePack
SpecialistDelta
FinancialIntegrityEvidencePack
Manual Evidence
CounterCasePack
Portfolio Risk
Market Regime
```

委员会不重新搜索。

决策顺序：

```text
硬阻断
→ 数据和证据覆盖
→ 预期收益/下行
→ 专家增量
→ 反方
→ 组合风险
→ 最终结果
```

结果：

```text
REJECT
NEEDS_INFO
WATCH
APPROVE_SIMULATION
```

风险规则：

- 禁止杠杆；
- 总仓位；
- 单股仓位；
- 行业集中；
- 流动性；
- 组合相关性；
- 最大回撤；
- 财务红旗；
- 重大公告冻结；
- 数据异常冻结；
- 手动紧急停止；
- 真实交易人工确认。

---

# 十四、模拟交易

## 14.1 本地账户与用户态镜像

不依赖第三方免费模拟账户作为核心，也不要求为波段/价值投资常驻一个后台 Agent。确定性账户仍保留，Agent 只在用户发起投资会话时按需恢复离线区间。

自建：

- 双重记账；
- 现金；
- 冻结资金；
- 费用；
- 订单；
- 成交；
- 持仓；
- NAV；
- 公司行为；
- 事件日志；
- 幂等 ID；
- replay cursor；
- 崩溃恢复。

同时维护 `user_state/portfolio.md`、`orders.md`、`trades.md` 三份 Git-ignore 的用户态 Markdown 镜像，供网页/桌面 Agent 每次启动快速读取。SQLite 账本仍是订单、成交、现金和持仓的确定性事实源；Markdown 不能反向制造成交。

用户明确要求模拟买入/卖出时，用户意图覆盖模型观点但不覆盖账户机械规则；AI 主动模拟下单则必须先通过研究/交易准入并满足当前 entry rule。两类订单都必须经过订单状态与成交回放，不能把“已提交订单”直接写成“已持仓”。

---

## 14.2 交易协议

每个建议必须同时生成：

```text
entry_rule
position_size
stop_loss_rule
take_profit_rule
trailing_stop_rule
max_holding_period
thesis_review_events
invalidation_conditions
price_data_resolution
```

没有交易协议，就不能公平评测 Agent。

---

## 14.3 会话式恢复与成交模拟

1. 用户发起任一投资类会话；
2. 校验账本并读取开放订单、持仓和本地 Markdown 镜像；
3. 读取最后回放游标，计算离线缺失区间；
4. 默认拉取未复权 60 分钟行情并做双源质量校验；
5. 顺序回放订单，恢复冻结资金、成交、T+1 与持仓；
6. 小时 OHLC 只能证明“该小时触及限价”，不能证明盘口队列与小时内先后路径，因此小时级回放明确标记近似，并使用更保守的成交假设；
7. 只有当小时级路径会实质改变成交/止盈/止损判断时，再按需拉取 5 分钟行情复核；
8. 处理公司行为、结算和 NAV；
9. 将确定性账户状态重新投影到 `user_state/*.md`；
10. 在本次投资任务之外，对已有持仓做增量事实复核并写入新的 review boundary。

5 分钟回放因此继续保留，但从“默认全程依赖”降级为“成交路径歧义时的高精度 fallback”。系统不需要在两次聊天之间持续运行。

---

# 十五、市场状态与策略自适应

## 15.1 市场状态

不能只凭“多数策略亏钱”判断熊市。

输入：

- 指数小时和日线趋势；
- 市场宽度；
- 新高新低；
- 成交额；
- 行业扩散；
- 波动率；
- 回撤；
- 大中小盘；
- 风格相对表现；
- 策略近期表现。

输出：

```text
趋势牛市
高波动牛市
震荡
趋势熊市
恐慌
```

---

## 15.2 策略健康

单独评估：

- 净收益；
- 超额收益；
- 最大回撤；
- 胜率；
- 盈亏比；
- 换手；
- 成本；
- 信号数量；
- 收益集中；
- 置信度校准。

区分：

```text
市场变了
策略坏了
数据坏了
成本吃掉收益
```

---

## 15.3 强化学习路线

第一阶段禁止端到端 RL 直接选股票。

顺序：

```text
固定规则
→ 市场状态机
→ 固定权重影子账户
→ 离线动态权重
→ 上下文多臂赌博机
→ 最后才研究 PPO/A2C
```

RL 只调整：

- Skill 权重；
- 策略权重；
- 总风险敞口；
- 现金比例；
- 行业和单股上限。

不能自由生成订单。

奖励考虑：

- 扣费收益；
- 超额收益；
- 回撤；
- 换手；
- 集中；
- 尾部亏损；
- 违反风险规则。

---

# 十六、评测体系

## 16.1 判断质量

- 5/20/60日方向；
- 目标区间；
- 置信度校准；
- 验证周期；
- 证伪反应；
- 引用覆盖；
- 缺失信息识别。

## 16.2 交易质量

- 扣费收益；
- 超额收益；
- 最大回撤；
- MAE/MFE；
- 盈亏比；
- 胜率；
- 持有时间；
- 换手。

## 16.3 研究质量

- 提前发现风险；
- 发现产业变化；
- 来源多样性；
- 热门追高；
- 事实错误；
- 历史观点篡改；
- 是否知道证据不足。

## 16.4 组合质量

- 行业集中；
- 风格暴露；
- Beta；
- 波动；
- 尾部风险；
- Agent相关性；
- 真正的多样化。

## 16.5 基准账户

至少包括：

- 沪深300或中证全指；
- 等权候选；
- 规则策略；
- BaseCase-only；
- BaseCase+单专家；
- 完整委员会。

作者和 Serenity 的增量价值必须在相同候选集、相同交易协议下比较。

---

# 十七、存储与技术栈

## 17.1 唯一事实源

```text
原始文档和网页：
SHA-256 内容寻址对象存储

结构化分析事实：
Parquet

分析查询：
DuckDB 读取 Parquet

状态和账本：
SQLite
```

SQLite 不复制行情和财务事实；DuckDB 不再保存第二份相同事实。

Knowledge 历史生产流水采用**热状态 / 冷历史分层**：当前 audited registry、现役 Direct/Visual provenance、SourceSnapshot/Evidence、原始 Zhihu version 与 Paper Ledger 留在热状态/不可变 ObjectStore；已被 KGA 固化且不再参与 Research Runtime 的 Semantic/Distillation/Reviewed/Book/Private 中间流水，可在计算所有存活 FK 父级闭包后写入按表 zstd Parquet 冷归档。冷档必须有 ObjectStore-backed manifest、文件 hash、逐表行数、FK 审计和可执行 restore；没有完成恢复演练不得从 SQLite 删除。大量单行 immutable metadata Parquet 必须按已有业务 partition 合并，避免小文件膨胀；历史 additive schema 演进以 nullable union 表示。SQLite 只在所有迁移审计通过后执行单次 VACUUM。

---

## 17.2 建议技术栈

- Python 3.12；
- uv；
- Pydantic v2；
- Polars；
- Pandas 兼容第三方；
- NumPy；
- PyArrow；
- Parquet；
- DuckDB；
- SQLite；
- scikit-learn；
- PyOD；
- hmmlearn；
- Stable-Baselines3（后期）；
- PyMuPDF；
- pdfplumber；
- RapidOCR；
- 可选 PaddleOCR 表格识别；
- Pandoc；
- FastAPI 薄层；
- Streamlit 可选；
- pytest；
- Hypothesis；
- ruff；
- pyright；
- Git。

避免：

- Kubernetes；
- Kafka；
- 微服务集群；
- 大型向量数据库；
- 云 GPU；
- 复杂分布式训练；
- 一开始引入完整通用 Agent 框架。

---

# 十八、成本和上下文控制

1. 数据 API 优先；
2. MCP 其次；
3. 浏览器最后；
4. 用户人工补证作为兜底；
5. 共性分析只做一次；
6. 专家只输出 Delta；
7. 同一原文不重复送给多个专家；
8. 再次研究只处理增量变化；
9. 委员会只读冻结工件；
10. 原文按 Evidence 精确打开；
11. 不维护无限增长长对话；
12. 每次运行生成 ContextBudgetReport；
13. 低难度清洗和转换由 Python 完成；
14. 便宜模型只审核边界样本；
15. 高难度方法抽象和正式 Skill 批准留给更强模型和人工。

---

# 十九、分阶段建设路线

## Phase 0：验证

- 本机性能；
- 免费行情；
- PDF/OCR；
- 知乎登录和访问；
- 开源仓库；
- SQLite账本；
- QMT能力；
- 模型接入能力。

## Phase 1：数据和模拟账户

- ObjectStore；
- Parquet；
- DuckDB；
- SQLite；
- 5分钟双源；
- 数据质量；
- 模拟账本；
- 回放；
- Codex入口。

## Phase 2：官方文档和证据库

- 巨潮和交易所；
- PDF/DOCX；
- OCR；
- 页码和区块；
- Claim—Evidence；
- PIT；
- 私有资料摄入。

## Phase 3：财务可信度

- FinancialFact；
- 三表；
- 盈余质量；
- 行业 Profile；
- 同行异常；
- PyOD；
- 红旗 EvidencePack。

## Phase 4：通用投资内核和 Serenity

- BaseCase；
- Industry Bottleneck；
- Event-to-Alpha；
- Growth Probability；
- TAM-Adj-PEG；
- GF-DMA；
- 小时波段；
- 专家 Delta；
- 持仓通用内核。

## Phase 5：知识工厂

- 知乎全集；
- 专栏；
- 评论链；
- 图片和表格；
- Markdown；
- ArgumentUnit；
- 去重和低成本清洗；
- 人工审核包；
- 观点卡；
- Candidate/Holding Skill 候选。

## Phase 6：委员会与风险

- 冻结工件；
- NEEDS_INFO；
- 反方；
- 决策；
- 风险；
- TradeProtocol。

## Phase 7：固定权重影子评测

- 多账户；
- 多市场状态；
- 样本外；
- Skill增量价值；
- PIT覆盖；
- 费用和回放质量。

## Phase 8：可选自适应

- 市场状态；
- 离线权重；
- 多臂赌博机；
- 最后才研究RL。

---

# 二十、近期业务收敛目标

## 20.1 知识线

先收敛到：

- 四位作者真实采集覆盖率；
- 回答、文章、想法、专栏可恢复；
- 原始 Markdown；
- 图片和表格；
- ArgumentUnit；
- 去重；
- 初步清洗；
- 人工审核包；
- 清洗版知识册。

Spark 等能力有限模型可负责机械处理；高质量方法抽象和正式 Skill 批准后置。

## 20.2 投资线

第一个可用 MVP：

用户输入：

```text
分析某家 A 股公司
```

系统返回：

- 官方证据；
- 财务可信度与红旗；
- BaseCase；
- 至少一个合适的 Serenity Delta；
- 估值与趋势；
- 反方；
- 信息缺口；
- WATCH / REJECT / PAPER_ELIGIBLE；
- PositionMonitoringPlan 草稿。

不能因作者 Skill 未完成而无限推迟投研 MVP。

---


# 二十一、最终用户体验

系统成熟后，用户可以：

```text
请扫描最近的 A 股候选，列出最值得优先研究的 10 家。
```

系统返回：

- 候选来源；
- 财务准入；
- 产业链位置；
- 市场可能忽视的地方；
- 证据强度；
- 主要风险；
- 需要人工补证；
- 当前研究优先级。

用户也可以：

```text
分析一下某公司。
```

系统形成：

```text
公司事实
→ 财务可信度
→ BaseCase
→ Serenity 和作者 Delta
→ 反方
→ 估值和趋势
→ 风险
→ 缺口
→ 模拟研究资格
```

用户还可以直接问：

```text
这只股票现在能不能买？什么位置考虑进入？预期卖出位和失效条件是什么？
```

系统必须先完成单股正式研究和投委会，再由 `TradePlanView` 把 `ClassifiedTradeProtocol`、DecisionPack 和 TradingClassification 投影成普通用户可读的交易计划。没有结构化价格证据时，系统只能给 entry/stop/take-profit/invalidation 规则及以冻结参考价换算的委员会情景区间；情景区间不是目标价，禁止由模型临场猜精确买卖点。

组合问题：

```text
评估我当前的投资组合。
```

系统返回组合集中度、相关性、Beta、波动/下行波动、最大回撤、VaR/CVaR/CDaR、风险贡献、现金和风险组暴露，并指出哪些持仓贡献了主要尾部风险。

```text
从当前研究候选中推荐几只股并组成一个组合。
```

系统必须先做 Candidate shortlist，再逐只完成正式单股研究和投委会；只有当前 `APPROVE_SIMULATION` 标的进入 Portfolio Construction。最终同时给出受约束等权默认方案与逆波动率、HRP-style、收缩最小方差三套对照，并解释为什么保留现金、哪些约束生效、哪些标的因单股研究不通过而被排除。

对于持仓：

```text
更新我的持仓。
```

系统：

- 恢复5分钟行情；
- 更新账本；
- 检查价格规则；
- 同步新增公告和事件；
- 比较论点变化；
- 输出 HOLD / ADD / TRIM / EXIT / REVIEW；
- 所有真实交易由用户人工确认。

---

# 二十二、方案与工程计划的关系

本文件定义：

```text
业务目标
系统边界
数据和专家体系
知识蒸馏方法
审计原则
模拟交易逻辑
评测和长期路线
```

它不用于声明：

```text
某模块已经完成
某个测试已经通过
某个作者已经爬完
某个 Skill 已经批准
当前分支和 commit
```

上述工程事实必须以 GitHub 仓库、PM 状态提交、`开发计划.md`、`验收报告.md` 和阶段报告为准。

---
