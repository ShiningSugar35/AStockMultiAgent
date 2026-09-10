# Business Question Capability & Acceptance Matrix v1

> 状态：CURRENT ACCEPTANCE CONTRACT
> 执行边界：68 个原始业务 ID 已全部接入真实领域服务 E2E；成功路径、缺失/冲突/PIT、actual append-only、paper prepare/confirm/replay、禁止能力与公开回答均按本矩阵断言。最新稳定套件为 70 个 pytest 用例，其中额外用例属于辅助安全断言，不改变 68 个业务 ID 口径。
> 更新日期：2026-09-09
> 关联架构：`docs/architecture/investment-request-orchestration-v1.md`、`docs/architecture/market-regime-control-v1.md`

## 1. 目标

把用户给出的 68 个自然语言业务问题转换为可执行验收合同，检查：

1. 是否运行了所有与问题实质相关的能力；
2. 能力是否按依赖顺序串联，而不是孤立地产生互相矛盾的结论；
3. 是否正确恢复实际/模拟账户、持仓、订单和重大监控增量；
4. 是否发生了允许的写入，且没有发生禁止的写入；
5. 是否输出投资者真正需要的结论、仓位、价格/估值区间、风险和改变判断的条件；
6. 是否发现当前能力、数据或合同缺口并 fail closed；
7. 空持仓时是否彻底静默，不输出“当前没有持仓”等无效文字。

本矩阵不要求盲目运行所有命令；它要求 `REQUIRED` 能力 100% 覆盖、`CONDITIONAL` 能力有可审计触发理由、`PROHIBITED` 能力调用为 0。

## 2. 能力缩写

| 缩写 | 能力 |
|---|---|
| `U` | question time + identity + actual/paper/monitor preflight + latest-regime availability（非全局硬门）+ coverage receipt + response gateway |
| `CR` | 当前公司/证券 research acquisition、continuation 与正式深度研究 |
| `MQ` | 当前价格、K 线、交易状态、流动性和 market-price anchor |
| `MA` | 宏观、政策、信用、流动性与市场状态 |
| `IN` | 行业、产业链、盈利池、周期、供需与可比公司 |
| `FI` | 正式财报、现金流、会计质量、异常和审计证据 |
| `GV` | 控制权、质押、减持、激励、审计机构、管理层与资本配置 |
| `EV` | 公告、新闻、催化剂、事故、订单、解禁与事件时间线 |
| `FV` | driver tree、forecast、valuation、情景、隐含预期和目标/退出区间 |
| `RR` | 独立 bull/bear、red team、模型风险、反证与 decision fragility |
| `CO` | readiness/Committee/TradeProtocol，只读冻结工件 |
| `PO` | 组合风险、相关性、集中度、压力、构建、迁移、成本和 target band |
| `HR` | 持仓增量复核，形成 HOLD/ADD/TRIM/EXIT 与条件 |
| `FM` | 三市场 official Universe、候选漏斗、全市场 Research Team |
| `SR` | ResearchSubjectRegistry、已研究/推荐/持有状态与 Continuous Monitor |
| `EA` | external account append-only 交易/现金/转托管/更正/投影/审计 |
| `ET` | ETF/基金/指数 profile、成分、费率、规模、流动性、tracking error、溢折价和 hedge evaluation |
| `PT` | 模拟账户、订单准备/确认、回放、成交、T+1 与恢复 |

## 3. Side-effect 分类

- `READ`：不得写真实账户或模拟账本；允许生成不可变研究工件、覆盖回执和 subject registry 元数据。
- `META`：追加 `MENTIONED/RESEARCHED/RECOMMENDED/MONITOR_*` 等研究元数据，不是经济交易。
- `EA_WRITE`：只向 external account event lane 追加具名事件；不得写 paper ledger。
- `EA_PROVISIONAL`：只追加“持仓存在/成本估算”的 provisional 事实，不能伪装为精确成交。
- `PT_PREPARE`：准备模拟操作并请求精确确认；不得直接产生成交。
- `PT_CONFIRM`：只有独立确认和机械规则通过后才能创建订单；仍不等于持仓。
- `PT_REPLAY`：只对**已经确认且既存**的模拟订单按冻结交易规则做确定性回放；可以追加 fill/settlement/position 账本事件，但不得新建、改价或重复确认订单，重跑不得重复成交。
- `NONE`：拒绝或降级时不发生任何写入。

表中的 `READ+META` 表示只允许不可变研究工件与研究元数据的并集，经济账本写入仍为 0。side-effect class 必须在执行前冻结；一旦触发 `PT_REPLAY` 等经济状态转换，本次运行不得继续标记为纯 `READ`。

## 4. 全局验收断言

每一个问题都必须通过：

1. 存在合法 `InvestorSessionPreflightReceipt`；
2. 实际账户、模拟账户、open order、fill 和 position 不混淆；
3. required capability set 全部有 typed output 或合法复用；
4. conditional capability 有触发/不触发理由；
5. 当前数据带 `as_of`、freshness、source/PIT lineage；
6. 空持仓时用户回复中不得出现空持仓声明或空章节；
7. 有重大持仓变化时必须说明影响和动作；
8. read-only 问题不得写经济账本；
9. 写入问题必须幂等、可审计、可恢复；
10. Investor Mode 不输出 CLI、内部 Agent、Schema、artifact/hash、provider 故障流水；
11. 不把成本价当估值、不把跌幅当抄底理由、不把涨幅当追涨理由；
12. 不把初筛、seed、新闻热度或模型单票当正式推荐；
13. 市场状态只能调整风险预算，不能绕过公司/财务/治理/估值硬门；
14. 所有推荐/已研究证券正确追加 subject registry，并在需要时纳入 monitor；
15. 回答无法完成时说明决定性缺口，不伪造价格、公告、账户或牛熊状态。
16. 已发生交易记录、更正、现金划转、转托管与去重检查不得把 current market regime 设为提交前硬门；投资判断类问题则必须由 `MA` 或具名 regime 能力提供 current 状态或明确降级。
17. “今天/昨天”等相对日期必须记录 user timezone、解析后的 civil date 与 A 股市场时区映射；多账户仅在唯一 active account 或显式默认账户时自动选择，其他情况不得猜测或串账。

## 5. 场景矩阵

### 5.1 单只股票：买入、估值、深度研究

| ID | 场景 | Required capability chain | Side effect | 核心验收 |
|---:|---|---|---|---|
| 1 | 标的现在适合买入、什么位置合适 | `U → MQ + MA + IN + CR + FI + GV + EV → FV → RR → CO → PO(有组合时) → SR` | `READ+META` | 明确 BUY/WAIT/AVOID 或条件式结论、估值/入场区间、分批和失效条件；价格过期时不得给伪精确点位 |
| 2 | 当前价格低估/合理/高估、持有多久、何处卖 | `U → MQ + MA + IN + CR + FI + GV + EV → FV → RR → CO → PO(有组合时) → SR` | `READ+META` | 区分合理价值与市场价格；给基础/上行/下行情景、时间范围、卖出条件而非单一神奇目标价 |
| 3 | 涨很多能否追、跌很多能否抄底 | `U → MQ(price path/liquidity) + MA + CR + IN + FI + GV + EV → FV → RR → CO → PO → SR` | `READ+META` | 涨跌幅只作输入；必须判断估值、盈利变化、事件、拥挤、流动性和市场状态；禁止机械追涨/均值回归 |

### 5.2 已有持仓：加仓、减仓、退出

| ID | 场景 | Required capability chain | Side effect | 核心验收 |
|---:|---|---|---|---|
| 11 | 持有标的、跌破成本 20% | `U → EA/PT projection → MQ + MA + IN + CR + FI + GV + EV → FV → RR → PO → HR → SR` | `READ+META` | 自动计算实际损益和风险贡献；成本价不替代估值；输出 HOLD/ADD/TRIM/EXIT、目标带和改变判断条件 |
| 12 | 500 股、成本 25 元，未明确当前盈亏 | `U → projection/position assertion → MQ → MA + IN + CR + FI + GV + EV → FV → RR → PO → HR → SR` | `READ+META`；若无记录则条件式 `EA_PROVISIONAL` | 自行查当前价并计算；不重复询问可恢复字段；缺购买时点/账户时不得伪造 exact trade |
| 13 | 2026 年 5 月 30 元买 1000 股，如何处理 | `U → duplicate lookup → EA assertion → MQ + full holding research → PO → HR → SR` | exact fields 足够时 `EA_WRITE`，否则 `EA_PROVISIONAL` | “2026 年 5 月”不是精确成交时点；给定 30 元可作声明成本，缺失成本时 K 线只能生成估算区间，不能写成真实成交价 |
| 14 | 之前买过这些标的/组合，现在怎么办 | `U → local/external/paper projections → SR material deltas → per-position CR/FI/GV/EV/FV → MA → PO → HR` | `READ+META` | 默认读取本地记录；按重大性排序，不逐只重复全报告；实际与模拟分列，给组合级和个股级动作 |

### 5.3 跨会话账户和交易记录

| ID | 场景 | Required capability chain | Side effect | 核心验收 |
|---:|---|---|---|---|
| 22 | 昨天买入 300 股，12.35 元 | `U → entity/account/date precision → duplicate/economic conflict → EA append → projection/audit → SR` | `EA_WRITE` 或字段不足时 `NONE` | 相对日期解析为绝对日期；若只有日期无时刻，必须由 date-precision 合同保存，不能编造具体成交时刻；幂等 |
| 23 | 今天卖出 200 股，15.82 元 | `U → account/position availability → duplicate → EA append → projection/audit → SR` | `EA_WRITE` 或 `NONE` | 校验可售数量与账户；相对日期绝对化；不得改 paper ledger；写后数量/成本/P&L 投影正确 |
| 24 | 上次买入数量写错，改为 600 股 | `U → locate exact prior event → correction preview → reversal/replacement → projection/audit` | `EA_WRITE` | 不 UPDATE/DELETE；目标唯一、同账户、经济影响可复算；重复纠正幂等 |
| 25 | A、B 两证券账户分别记录 | `U → account identity/create → isolated projections/audit` | `EA_WRITE` | 账户 id 唯一；现金/证券/事件不串；无账户名歧义；聚合视图仍保留来源 |
| 26 | A 转托管 1000 股到 B，不是买卖 | `U → source position/cost basis → paired transfer plan → atomic append/audit` | `EA_WRITE` | 数量守恒、成本继承、无交易 P&L/现金；成对事件有 correlation id；失败整组回滚 |
| 27 | 向 B 转入 5 万元现金 | `U → account identity → cash deposit append → projection/audit` | `EA_WRITE` | B 账户现金增加 50,000；不影响证券成本，不写 paper ledger，重复输入不双计 |
| 28 | 从证券账户转出 2 万元现金 | `U → resolve account → known cash check → withdrawal append → projection/audit` | `EA_WRITE` 或 `NONE` | 多账户时不能猜；known cash 不足按政策阻断/警示；unknown cash 不伪造余额 |
| 29 | 交易可能重复，检查不要重复持仓 | `U → idempotency key + source hash + economic fingerprint + actual/paper conflict → return existing/append` | 通常 `NONE`，确认为新事实才 `EA_WRITE` | exact duplicate 0 新事件；相似但非同笔不得误删；给可理解的去重结果 |

### 5.4 组合分析与资产配置

| ID | 场景 | Required capability chain | Side effect | 核心验收 |
|---:|---|---|---|---|
| 31 | 评估整个股票组合风险 | `U → lane-separated positions → MQ/PIT total-return series → MA → PO(stress/CVaR/CDaR/beta/liquidity) → HR` | `READ+META` | 总风险、边际贡献、尾部、流动性和数据质量；unknown cash 不算 0；重大动作按优先级 |
| 32 | 是否过度集中某行业 | `U → certified industry taxonomy → PO concentration/stress → MA/IN → HR` | `READ+META` | 行业分类必须有正式 release；不能用调用方随手标签冒充；给当前与目标区间 |
| 33 | 哪些股票风险大、如何调整 | `U → per-position CR/FI/GV/EV/FV + PO marginal risk → MA → HR` | `READ+META` | 区分公司风险、估值风险和组合贡献；给 TRIM/EXIT/hold band 与替代方案 |
| 34 | 哪些相关性高、没有分散作用 | `U → PIT returns/total-return quality → correlation/clustering/tail correlation → PO transition` | `READ+META` | 不只报普通相关系数；检查压力期与共同因子；给调整后风险变化和成本 |
| 35 | 再买某标的，风险变好还是变差 | `U → full target research/readiness → current PO → counterfactual add → MA → transition` | `READ+META` | 比较前后集中度、波动、CVaR、流动性和因子暴露；未正式准入不能直接给权重 |
| 36 | 加投 10 万元，买哪些、各配多少 | `U → current PO/risk gaps → MA budget → FM/approved candidates → full research → construction/transition/cost` | `READ+META` | 10 万作为新增资本，不假定其他现金；给金额/股数/目标带/分批/剩余现金和推荐本金适用区间 |
| 37 | 5 只股票的目标仓位区间 | `U → per-stock readiness → PO allocator comparison → MA overlay → target bands/cost` | `READ+META` | 给区间而非点权重；约束单股/行业/流动性；当前落在 no-trade band 时保持 |
| 38 | 降低组合波动/风险 | `U → PO risk-gap/stress → approved stock/ETF complements → transition/hedge evaluation → MA` | `READ+META` | 区分 diversification/natural hedge/unproven；给风险改善、成本、税费/换手和缺点 |

### 5.5 组合构建与荐股

| ID | 场景 | Required capability chain | Side effect | 核心验收 |
|---:|---|---|---|---|
| 41 | 当前 A 股哪些股票值得买 | `U → FM official Universe → MA recommendation budget → candidate funnel → full research each → CO → PO → SR` | `READ+META` | 默认“现在可买”的完整研究；初筛不准发布；数量由合格标的与 regime cap 共同决定，可少于预期或为 0 |
| 42 | 全市场筛 10 只值得购买 | `U → FM → MA → 10+ deep-research funnel → CO → PO construction/transition → SR` | `READ+META` | 最终最多 10 只且每只正式准入；给权重、适用本金区间、买入/卖出条件、组合风险和候选不足说明 |
| 43 | 构建 5 只稳健型组合 | `U → risk intent → FM/full research → PO robust allocators/stress → MA → SR` | `READ+META` | 5 只是用户目标数量，不是用不合格标的凑数的授权；合格标的不足时必须返回较少标的、现金与缺口，强调现金流、财务质量、流动性和尾部风险 |
| 44 | 偏成长、控制单股风险 | `U → growth intent → FM/full research/FV → PO caps/diversification → MA → SR` | `READ+META` | 成长必须有盈利/现金流/估值支撑；单股/行业 caps；高波动牛市不放大集中度 |
| 45 | 尽量低波动组合 | `U → FM/approved candidates + optional ET → PO shrinkage/inverse-vol/HRP + stress/cost → MA` | `READ+META` | 不能用历史低波动直接保证未来；报告相关性、容量、收益牺牲与 regime 变化敏感性 |
| 46 | 持有 3 年以上的长期公司 | `U → FM → long-horizon CR/IN/FI/GV/capital allocation/FV → RR → CO → PO → SR` | `READ+META` | 强调长期竞争力、再投资、资本配置和估值；短期催化不主导；给长期监控 KPI |
| 47 | 排除银行和地产 | `U → FM with certified taxonomy exclusion → full research → PO → MA → SR` | `READ+META` | 排除是硬约束并写入 coverage receipt；上下游/类金融边界透明；不能筛后手工删出破坏组合 |
| 48 | 已确定买入这些标的，补 2–4 只改善结构 | `U → anchors full readiness → current+planned PO risk gap → FM complements → deep research → transition/hedge → MA → SR` | `READ+META` | 尊重 anchor 但展示其风险；补充标的必须改善具名结构，给前后指标、权重和成本；不把相关性低等同对冲 |

### 5.6 行业与产业链研究

| ID | 场景 | Required capability chain | Side effect | 核心验收 |
|---:|---|---|---|---|
| 51 | 行业处于什么阶段、是否适合投资 | `U → MA → IN cycle/supply-demand/profit pool → representative CR/FI/FV → RR → SR` | `READ+META` | 区分宏观周期与行业自身周期；给阶段证据、领先/滞后指标、估值与可投资条件 |
| 52 | 拆上中下游并找买点 | `U → MA → IN full chain/value capture/bottlenecks → candidate research → FV/CO → PO → SR` | `READ+META` | 产业链完整、利润传导明确；“买点”只对正式研究公司给出，行业热度不能直接荐股 |
| 53 | 真正赚钱最多的环节 | `U → IN profit pool/ROIC/cash conversion/cyclicality → FI representative firms → MA` | `READ+META` | 区分收入规模、利润额、利润率、ROIC 和周期位置；给数据口径/时点 |
| 54 | 行业关键跟踪指标 | `U → IN driver tree → MA/policy → company KPI mapping → SR monitor rules` | `READ+META` | 指标具备来源、频率、领先/滞后、阈值和传导；避免列一堆无动作指标 |
| 55 | 龙头核心竞争优势 | `U → IN structure → CR economics/moat → FI/GV/FV → RR` | `READ+META` | 优势必须连接到份额、价格、成本、现金流/ROIC及反证；不以“龙头”作循环论证 |
| 56 | 公司 A/B 商业模式区别 | `U → identity → shared IN framework → parallel CR/FI/GV/FV → RR` | `READ+META` | 同口径比较客户、收入、成本、资本强度、现金周期、风险和估值；不只比财务倍数 |
| 57 | 原材料/产品价涨 20% 谁受益/受损 | `U → IN quantity/price transmission → company driver trees → deterministic sensitivity/FI → FV → RR` | `READ+META` | 明确传导时滞、合同、套保、需求弹性与二阶效应；20% 是情景不是事实 |

### 5.7 宏观、政策与周期

| ID | 场景 | Required capability chain | Side effect | 核心验收 |
|---:|---|---|---|---|
| 61 | 当前宏观对 A 股偏利好/利空 | `U → official current macro releases/vintages → MA economic + market regime → earnings/valuation/liquidity transmission → RR` | `READ+META` | 给多维而非一句利好/利空；列当前时点、状态概率、主要驱动、反证和数据缺口 |
| 62 | 进一步降息哪些行业真正受益 | `U → policy scenario → MA rates/credit/FX transmission → IN balance-sheet/demand sensitivity → representative CR/FV → RR` | `READ+META` | 区分估值、融资成本、需求、息差和汇率渠道；名义受益不等于利润受益 |
| 63 | 政策变化如何影响行业 | `U → official policy text/effective date → MA transmission chain → IN supply/demand/cost/capacity → KPI/monitor` | `READ+META` | 逐环节、时间窗、受益/受损、执行不确定性；媒体解读不能替代政策原文 |
| 64 | 政策对公司是否实质利好 | `U → official policy → MA + IN → CR exposure/eligibility/economics → FI/FV → RR` | `READ+META` | 量化收入/成本/资本开支/竞争格局可能影响；区分一次性情绪与可持续现金流 |
| 65 | 某变量继续走弱的传导 | `U → define observable variable/scenario → MA → IN → CR/FI/FV → RR` | `READ+META` | 变量、幅度、期限和情景明确；给一阶/二阶效应、受益/受损及监控指标 |
| 66 | 当前宏观政策对持仓影响 | `U → holdings exposure → MA → per-position IN/CR sensitivity → PO stress → HR` | `READ+META` | 覆盖全部实际/模拟持仓但分 lane；按影响重大性给动作，不只列宏观观点 |
| 67 | 行业处于产业周期哪阶段、建议 | `U → MA → IN capacity/inventory/capex/profit cycle → representative FI/FV → PO/SR` | `READ+META` | 周期阶段证据、可能持续时间、领先指标、适合的公司类型/风险；不机械套“复苏—繁荣” |

### 5.8 财务质量、治理与风险排查

| ID | 场景 | Required capability chain | Side effect | 核心验收 |
|---:|---|---|---|---|
| 71 | 利润增长但现金流质量 | `U → official reports → FI earnings-to-cash/reconciliation/accruals → CR/IN → GV/FV → RR` | `READ+META` | 解释差异来源、持续性和红旗；利润增长不能单独判可靠 |
| 72 | 应收快于收入、存货大增 | `U → FI trend/common-size/turnover/aging/impairment → IN seasonality/channel → CR/GV/FV → RR` | `READ+META` | 与业务模式和行业比较；量化营运资金与减值敏感性；给正常/异常判据 |
| 73 | 财务异常或会计红旗 | `U → FI full integrity audit → official notes/auditor → GV/CR/FV → RR` | `READ+META` | 不用单指标定罪；列证据、严重性、替代解释、对估值/准入影响；缺证据时弃权 |
| 74 | 更换审计机构是否警惕 | `U → official appointment/resignation/reason → FI audit history/opinion/fees → GV → CR/FV → RR` | `READ+META` | 核对变更时间、原因、前后审计意见、监管/内控信号；非自动利空 |
| 75 | 大股东质押比例高 | `U → official pledge data/current ownership → GV control/margin-call/refinance → MQ/EV/FI → PO/HR` | `READ+META` | 比例、质押价格/补仓线可得性、流动性、控制权和强平传导；不可得字段保持未知 |
| 76 | 管理层频繁减持 | `U → official sell-down timeline/amount/reason → GV incentives/ownership → MQ/EV/CR/FV → HR` | `READ+META` | 区分预披露、执行、到期、被动/主动；按持股占比和基本面变化判断，不只看次数 |
| 77 | 过去资本配置质量 | `U → FI cash flow/capex/M&A/dividend/buyback/debt → GV incentives → CR ROIC/value creation → FV/RR` | `READ+META` | 资金去向、回报、减值、机会成本、股东回报和未来约束；给跨周期证据 |

### 5.9 公告、事件、催化剂与持续监控

| ID | 场景 | Required capability chain | Side effect | 核心验收 |
|---:|---|---|---|---|
| 81 | 今天最新公告及持仓影响 | `U → official disclosure search/sync → EV materiality → CR/FI/GV/FV delta → PO/HR → SR` | `READ+META` | “最新/今天”按绝对日期和交易所原文；影响 thesis、估值、仓位与行动；无持仓时静默跳过持仓段 |
| 82 | 业绩预告超预期/低预期 | `U → official preview → explicit expectation baseline → FI quality → CR/FV delta → MQ/EV → HR` | `READ+META` | 必须说明相对谁的预期、口径和一次性项目；无合法预期基准时不能宣称超预期 |
| 83 | 新建产能是真利好还是过度资本开支 | `U → official project terms → IN supply/demand/cycle → FI funding/leverage/capex → CR driver tree/FV → RR/HR` | `READ+META` | 产能、投产时点、利用率、价格、资金来源、回报率和供给冲击；给情景而非公告情绪 |
| 84 | 重大订单对利润影响 | `U → official contract/order → EV enforceability/timing → CR margin/revenue recognition → FI/FV sensitivity → RR/HR` | `READ+META` | 订单额不直接等于收入/利润；检查履约、毛利、周期、客户集中和重复公告 |
| 85 | 媒体报道重大事故 | `U → authoritative source confirmation → EV severity/operations/legal/ESG → CR/IN/FI/FV → PO/HR → SR` | `READ+META` | 先确认权威来源和时间；未经确认只能线索；给停产/赔偿/声誉/供应链情景和动作 |
| 86 | 减持计划是否提前调整 | `U → official plan → GV/EV size/window/holder → MQ/liquidity/valuation → PO/HR` | `READ+META` | 计划不等于执行；检查占流通股、原因、窗口、历史行为、基本面和组合风险 |
| 87 | 下月大规模解禁 | `U → official unlock schedule/share type/holder/cost → MQ liquidity/valuation → GV/EV → PO/HR` | `READ+META` | 解禁不等于减持；量化潜在供给、持有人动机、成交容量和已定价程度 |
| 88 | 原催化剂已落地，是否符合预期 | `U → SR original thesis/catalyst snapshot → official result → EV delta → CR/FI/FV → RR/HR` | `READ+META` | 必须对照当初冻结预期而非事后改口；给达成/部分/失败、估值和动作变化 |
| 89 | 所有持仓最近重要新事件 | `U → all actual/paper holdings → SR events/tasks → authority verification → per-position delta → PO/HR` | `READ+META` | 只报重大事件，按组合影响排序；每项必须给继续持有/复核/减仓等动作和条件 |
| 90 | 已研究未买入股票的重大变化 | `U → SR RESEARCHED minus HELD registry → recent events → authority verification → research/FV delta → recommendation status` | `READ+META` | 依赖完整跨会话 registry；不能只查当前 monitor 子集；给升级/维持/降级/移除和原因 |

### 5.10 ETF、模拟交易、异常与边界

| ID | 场景 | Required capability chain | Side effect | 核心验收 |
|---:|---|---|---|---|
| 91 | 用 ETF 降低组合波动 | `U → PO risk gap → ET official profiles/constituents/metrics → hedge evaluation/cost → MA → transition` | `READ+META` | ETF 需正式 profile；检查重叠、tracking error、流动性、费率和压力期效果；正确标记 diversification/hedge |
| 92 | 某 ETF 是否值得买 | `U → ET index methodology/constituents/fee/AUM/liquidity/tracking error/premium → MA → PO → RR` | `READ+META` | 覆盖用户指定五项；缺 NAV/iNAV 不算伪溢折价；ETF 与指数/基金事实不混淆 |
| 93 | ETF A/B 跟踪类似方向选哪个 | `U → parallel ET profiles/metrics/constituent overlap → cost/liquidity/tracking comparison → PO counterfactual → MA` | `READ+META` | 同一时点同口径比较；给不同本金/持有期/成交额下的选择条件，不只看管理费 |
| 94 | 加入 ETF 是否形成更好对冲 | `U → current PO → ET returns/constituents → hedge effectiveness normal+stress+cost → MA` | `READ+META` | 计算加入前后具名风险；低相关不自动叫 hedge；basis/model risk 和费用单列 |
| 95 | 模拟买入 1000 股 | `U → entity/instrument rule → explicit user command → optional current research opinion → PT prepare → confirmation → order status/SR` | `PT_PREPARE`，独立确认后 `PT_CONFIRM` | 用户命令可覆盖研究意见但不覆盖现金、lot/tick、价格限制、T+1和确认；未给价格/订单类型时不伪造；订单不等于持仓 |
| 96 | 查看之前的模拟限价单 | `U → PT open-order/status/audit → current trading calendar/data → deterministic replay if due → projection/monitor` | 仅查看为 `READ`；既存已确认订单到期需回放时为 `PT_REPLAY` | 返回订单/触发/成交/失效状态；不得新建或改价；重复查询不重复成交；只有 fill 更新 position；60m 近似与 5m fallback 语义正确 |

### 5.11 本轮现状基线：结构审查与只读实测

本节不是 68 个自然语言场景的端到端通过声明，而是下一阶段实施前的当前基线：

1. **静态场景覆盖为 68/68。** 用户给出的原始 ID 已逐项登记，文档合同测试验证每个 ID 恰好出现一次，并为其定义 required capability chain、side effect 与核心验收。
2. **按本矩阵新增的完整共同硬门，当前可认证完成数为 0/68。** `InvestorSessionPreflightReceipt`、`UnifiedPortfolioContext`、机器可读 `CapabilityCoverageReceipt`、`ResearchSubjectRegistry` 和所有 public entry point 的统一 gateway 强制证明尚未在 `src/` 中实现，因此当前不能证明“每次回答前均恢复实际/模拟账户，并完整运行全部相关能力”。这不表示现有领域能力为零，也不是对 68 份答案内容质量的评分；它是共同 P0 运行合同尚未闭合造成的结构性未认证。
3. **现有底座应复用而非重写。** 源码核验确认多账户 append-only 事件与投影、paper ledger、本地兼容投影、Continuous Monitor、组合与 ETF 服务、ResponseGateway、宏观 recorded provider 和 shadow regime 已存在。
4. **前置恢复目前分散且存在明确热路径缺陷。** 在全量测试并发负载下，单次只读观察为：`external-account-list` 13.403 秒、`local-portfolio-status` 7.770 秒、`continuous-monitor-status` 9.655 秒、`paper-status` 66.838 秒；这些数值不是正式 p95。源码已定位 `paper-status` 的稳定根因：CLI 先调用 `LedgerService.status()`，随后 `portfolio_nav()` 再次调用 `status()`，而每次 `status()` 都执行全库 `PRAGMA integrity_check`。
5. **宏观与市场状态仍有明确当前边界。** NBS/PBOC/MOF/NDRC 四个 provider 在 `live=True` 时均明确拒绝；现有 regime 分类位于 shadow service，核心只使用日/小时趋势、市场宽度、波动分位和指数回撤，尚无概率、置信度、滞回、完整宏观/盈利/估值特征或正式风险预算联动。
6. **公共出口尚未形成全局强制。** `ResponseGateway` 已实现，但当前源码只发现 research runtime CLI 的显式实例化；在所有公开入口完成绕过反例测试前，不能声称所有投资回答已统一收口。

因此，下一阶段首先实现共同 preflight、能力 DAG、typed output、side-effect receipt 和 gateway 硬门，再执行每题 positive/negative/error-injection recorded E2E 与代表性 controlled-live；不能先把答案写得更长，再倒推运行证据。

## 6. 当前审查发现的缺陷/不足清单

| ID | 严重度 | 缺陷/不足 | 影响场景 | 下一步 |
|---|---|---|---|---|
| GAP-01 | P0 | 没有代码级统一投资请求入口与强制 preflight receipt | 全部 68 | 实现 `InvestorRequestEnvelope`、`InvestorSessionPreflightService` 和网关硬门 |
| GAP-02 | P0 | 真实多账户、本地兼容投影、模拟账户尚无统一且分 lane 的请求级上下文 | 持仓、组合、ETF、模拟 | 实现 `UnifiedPortfolioContext` 和经济重复检测 |
| GAP-03 | P1 | `paper-status` 单次观察 66.838 秒；CLI 与 `portfolio_nav()` 重复构建 `status()`，每次又执行全库 `PRAGMA integrity_check` | 全部 | 单请求复用状态快照；完整 integrity check 移至显式/低频审计；增加 revision cache、coalescing 和冷暖性能门 |
| GAP-04 | P0 | 没有机器可读的 required/conditional/prohibited 能力策略和覆盖回执 | 全部 | YAML/Schema scenario policy + planner/executor + `CapabilityCoverageReceipt` |
| GAP-05 | P0 | 当前市场状态只在 shadow 链，维度与验证不足，未控制风险预算 | 单股、持仓、组合、荐股、行业、宏观、公告与 ETF 场景 | 实现 market regime v2 与 decision overlay，保持默认关闭 |
| GAP-06 | P0 | NBS/PBOC/MOF/NDRC 宏观 adapter 目前 live 未实现 | 61–67 及所有当前投资判断 | 先补 official current capture/PIT，再训练模型 |
| GAP-07 | P1 | 没有统一的所有历史提及/已研究/已推荐/已持有 registry | 88–90、持续监控 | append-only `ResearchSubjectRegistry` |
| GAP-08 | P1 | 用户只给月份/日期、持仓和估算成本时，现有 exact trade Schema 不适合安全落库 | 12、13、22、23 | 增加时间精度和 provisional position/cost lane；禁止 K 线伪造成交 |
| GAP-09 | P1 | 尚未证明所有投资者回复都必须通过统一 ResponseGateway/coverage audit | 全部 | 收敛所有 public entry point，增加绕过反例测试 |
| GAP-10 | P1 | 认证行业 taxonomy 仍是组合行业集中度正式结论的前置门 | 32、36、37、43–48 | 建立 effective-dated taxonomy release 与映射审计 |
| GAP-11 | P1 | “超预期”、订单利润、产能回报、解禁压力需要具名基准/模型合同，不能靠叙述 | 82–84、87、88 | 补 expectation baseline 与 deterministic sensitivity contracts |
| GAP-12 | P1 | 自然语言交易日期粒度、默认账户选择、成对转托管原子性需做 E2E 反例 | 22–29 | 扩展账户事件 Schema/Service/CLI 测试，不先假定已满足 |
| GAP-13 | P2 | 能力调用多不等于回答好，当前缺少覆盖、延迟、成本、复用与答案质量联合指标 | 全部 | 建立 scenario scorecard 和 observability |
| GAP-14 | P2 | 市场状态下推荐数量与研究预算没有版本化 policy | 41–48 | regime-aware recommendation budget，但不降低准入门 |
| GAP-15 | P1 | 本地 Monitor 已能确定性采集和排队，但 ChatGPT / Codex 定时语义研究、任务绑定、幂等通知和 missed-run 恢复尚未接入 | 81–90、96 及持续跟踪 | 实现 Scheduled Research Adapter；本地事实平面与平台唤醒/通知平面分离，经济写入恒为 0 |

## 7. 测试分层

### L0：Schema 与静态合同

- 68 个 scenario id 唯一且全部登记；
- capability id、side effect、依赖和输出 Schema 可解析；
- 每项至少有一个 positive 和一个 negative fixture；
- 文档矩阵与 machine manifest 一致；
- 相对日期、账户、证券身份和金额类型明确。

### L1：Planner/Router 单测

- 同一问题生成固定 required set；
- 条件触发可解释；
- 不相关能力不运行；
- 纯账户事实写入不依赖 current regime；买卖/持仓/组合/荐股判断缺 current regime 时正确降级；
- read-only 不获得经济写权限；
- 任何 required 能力失败都会改变 readiness/结论。

### L2：Service 合同测试

- preflight、外部账户、paper snapshot、subject registry、regime、portfolio、gateway；
- append-only、幂等、事务回滚、缓存 revision；
- empty-holdings silence；
- actual/paper conflict；
- provisional cost 不进入 exact P&L。

### L3：Recorded end-to-end

为 68 个问题构造冻结时点数据和期望工件链。每个场景验证：

- 实际调用轨迹与 required DAG 一致；
- typed output lineage 完整；
- side effect 精确；
- public answer 字段和禁词；
- 重跑幂等；
- 注入缺失/冲突时 fail closed。

### L4：Controlled live

按领域选代表场景，不用一次性对 68 个全部打 live：

- 当前公司/价格/公告；
- official macro release；
- full-market Universe；
- ETF profile；
- monitor event；
- provider failover/schema drift。

live 证据必须保存原始 snapshot、时间、来源、耗时和回退路径。测试 fixture 不能替代该门。

### L5：Shadow/prospective

对影响荐股数量、仓位和动作的 market regime / orchestration policy：

- 与现有基线并行，不改变用户账本和正式建议；
- 记录覆盖、延迟、成本、稳定性、风险和机会成本；
- 样本独立、policy 冻结；
- 只有通过预注册门并经显式批准才能激活。

## 8. 评分卡

每个场景至少报告：

- `required_capability_coverage`：必须 100%；
- `prohibited_call_count`：必须 0；
- `duplicate_external_fetch_count`；
- `artifact_reuse_ratio`；
- `preflight_warm/cold_latency`；
- `economic_side_effect_match`：必须精确；
- `idempotent_rerun`：必须通过；
- `PIT/freshness/source_coverage`；
- `empty_holding_silence`：必须通过；
- `investor_output_guardrail`：必须通过；
- `conclusion_readiness`；
- `critical_gap_discovery`；
- `answer_actionability`：结论、理由、风险、动作、改变判断条件是否齐全。

不允许用平均分掩盖 P0 硬门失败。任何账本错误、未来函数、伪造价格/持仓、真实/模拟混淆、空持仓噪声或 public gateway 绕过都使整包失败。

## 9. 执行策略

1. 先实现 machine manifest/schema 和 synthetic fixtures；
2. 从每个大类选择 1 个代表问题做 thin vertical slice；
3. 再覆盖全部 68 个 recorded E2E；
4. 修复场景发现的代码/数据/Skill/Workflow 缺陷；
5. 运行受控 live 代表集；
6. market regime 和风险预算只进入 shadow；
7. 完成 full suite、审计与回滚演练后，逐 feature flag 启用。

下一阶段不得从“先让 68 个回答看起来完整”开始；必须先让每个回答拥有可检查的 preflight、能力 DAG、typed outputs 和 side-effect receipt。
