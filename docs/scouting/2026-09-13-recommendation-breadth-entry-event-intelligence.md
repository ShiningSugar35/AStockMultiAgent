# WP-23 外部调研：荐股广度、入场质量与企业事件情报（2026-09-13）

## 结论先行

本轮不采用“为了行业均衡批量导入行业 Skill”的方案。现有内部行业 archetype 已覆盖金融、消费、医药、汽车、公用事业、煤炭、有色、化工、装备、军工、交通、农业等主要 A 股经济模型。复核源码后确认 reserved blind membership 已经在 Existing Candidate/Expert overlay 前按纯 market score 选定，历史 Candidate priority 不会把 blind 外成员挤入；真实缺口是**有限 top-N 研究预算只有单一的成交额/流通市值/换手 market rank，缺少不替代 pure blind 的跨行业 breadth challenger**。外部方法只补研究能力与数据路由，不得成为候选配额器或第二推荐权威。

最终取舍：

1. **Blind + breadth research budget：本地增强，ADAPT_PATTERN**。现有 pure blind membership 保持；新增独立的 breadth challenger 与 concentration diagnostics。Existing Candidate / Knowledge / Serenity 只能丰富研究，不能成为 breadth fairness 的打分来源。
2. **Entry Quality：本地确定性实现，ADAPT_PATTERN**。吸收移动均线、交易区间/52周位置、动量、波动/成交等经研究验证过的“测量思想”，不复制整套 TA 框架，不使用单指标硬规则。
3. **企业事件情报：官方源为生产基线；QCC MCP 为 SHADOW_EXPERIMENT，QCC/TYC API 为授权 fallback；OpenBB MCP 为 WATCH/基础设施参考**。商业数据库最高只到 `SECONDARY_STRUCTURED`，没有资格报告和凭据时不影响生产路径。

## 1. 行业研究 / Agent / MCP 生态

| 候选 | 观察 | 本项目决策 | 原因 |
|---|---|---|---|
| OpenBB ODP / OpenBB MCP | 活跃开源数据平台，可把 provider 暴露为 Python / REST / MCP；MCP server 为官方扩展 | WATCH / SHADOW_EXPERIMENT | “connect once, consume everywhere”适合作为 MCP-first 路由参考；但 A 股具体覆盖、数据权利、provider 质量需逐项资格审计，且引入整个平台会复制现有 Provider Registry / SourceAccessRouter |
| FinRobot | 多 Agent equity research、确定性估值、Bull/Bear/Judge | ADAPT_PATTERN only | 角色拆分和确定性模型理念已被现有 Research Team/Committee 覆盖；直接引入会形成第二 Agent swarm/第二事实链 |
| FinResearchAgent | Python 先采结构化数据，再让受限 Agent 做研究/交叉检查 | ADAPT_PATTERN only | “data first, LLM analysis second”与现有架构一致，可用于审查方法；不引入完整 swarm |
| agent-skill-industry-research / industry-research-skill | 通用行业研究 Skill，覆盖市场规模、竞争、生命周期、证据账本 | WATCH | 适合借鉴报告/证据检查表，但项目活跃度/采用度与 A 股专门性不足，不应为了行业数量平衡进入 active registry |
| 各类免费 stock MCP | 多数聚焦美股/SEC/FINRA/FRED/Yahoo | REJECT for A-share core | 数据权利和 A 股覆盖不足；不能替代上交所/深交所/CNINFO/本地 market reference |

参考：
- OpenBB ODP / MCP docs: https://docs.openbb.co/odp , https://docs.openbb.co/agents/workspace-mcp-quickstart
- OpenBB repository: https://github.com/OpenBB-finance/OpenBB
- FinRobot: https://github.com/AI4Finance-Foundation/FinRobot （若当前镜像/分叉不同，以官方组织仓为准，生产不得自动拉取）
- FinResearchAgent pattern candidate: https://github.com/Schadenfreunde/fin-research-agent
- Industry research skill examples: https://github.com/147356/agent-skill-industry-research , https://github.com/lu90/industry-research-skill

## 2. 技术分析 / “低位”研究

### 2.1 可采纳的证据

- Brock, Lakonishok & LeBaron, *Journal of Finance* (1992), “Simple Technical Trading Rules and the Stochastic Properties of Stock Returns”：移动均线与 trading-range-break 规则在长期样本中呈现非随机差异，说明技术状态可以作为条件信息，而不是证明一个固定规则永远有效。DOI: 10.1111/j.1540-6261.1992.tb04681.x
- Lo, Mamaysky & Wang, *Journal of Finance* (2000), “Foundations of Technical Analysis”：用系统化、自动化的模式识别替代主观画图，部分模式对条件收益分布有增量信息。NBER Working Paper 7613: https://www.nber.org/papers/w7613
- Sullivan, Timmermann & White, *Journal of Finance* (1999), “Data-Snooping, Technical Trading Rule Performance, and the Bootstrap”：大量技术规则筛选必须控制 data-snooping，不能看到历史表现后再挑阈值。
- George & Hwang, *Journal of Finance* (2004/2005), “The 52-Week High and Momentum Investing”：接近 52 周高点本身含动量信息，因此“距离高点越远=越低=越好”是错误定义。

### 2.2 工程决策

`TA-Lib` 等成熟库可以作为指标定义/对照参考，但本轮 **不引入为生产依赖**。本项目只需要少量确定性滚动统计，直接从 canonical D1 bars 计算更可审计、更少依赖，也避免 200+ 指标造成过拟合搜索空间。

新的 Entry Quality 只测量：
- 价格在 20/60/120/250 日区间的位置；
- 距 52 周/250 日高点的 drawdown；
- 20/50/100/200DMA 距离与均线结构；
- 20/60/120/250 日收益与短/中期趋势稳定；
- 实现波动、成交/换手上下文；
- 可选 benchmark relative strength；
- 与已有 valuation margin-of-safety 组合解释。

禁止：RSI<30 自动 BUY、MACD 金叉自动 BUY、52周低点自动 BUY、把 technical state 当公司质量或估值事实。

## 3. 关键人事 / 工商 / 风险数据源

### 3.1 Primary / authoritative baseline

上市公司内部关键人事、控制权和持股变化继续优先：

1. 上交所 / 深交所 / 北交所正式披露；
2. CNINFO 正式公告和财报；
3. 证监会/监管机构；
4. 对外政府任职或公共职位变化：对应中央/地方政府正式网站；
5. 工商/信用/司法：国家企业信用信息公示系统、信用中国、中国执行信息公开网/法院正式入口（只有完成域名与能力资格后才能正式 admission）。

上交所《股票上市规则（2026年4月修订）》明确覆盖董事、高级管理人员、控股股东/实际控制人等“关键少数”的披露与异常情况核实，这为 management/personnel event 的正式来源优先级提供监管依据。

### 3.2 企查查

官方开放平台当前产品导航明确含 `MCP / API / SDK`；企业户/开放平台覆盖董监高、实际控制人、历史信息、变更、股权冻结/质押、司法风险、行政处罚、新闻舆情等。示例接口：
- 企业变更记录：API 734，约 1 元/次，需企业实名与应用场景审核；
- 企业工商详情：API 735，约 2 元/次，含主要人员/股东/分支/变更/行业分类；
- MCP 产品入口存在，但当前公开页面未给出可无需授权直接调用的生产 endpoint。

决策：`qcc-enterprise-intelligence-mcp` 进入 external capability registry 的 **SHADOW** 候选，优先级高于同厂 API，但只有用户/部署方提供合法授权并通过 M-06 qualification 后才能成为 production backup；source ceiling=`SECONDARY_STRUCTURED`。

官方入口：https://openapi.qcc.com/

### 3.3 天眼查

官方开放平台提供企业基本信息（含主要人员）、历史主要人员、变更记录、司法风险、股权变更、人员商业角色等 API；需要 token 且按次收费。示例：
- 历史主要人员 API 1050（0.1 元/次）；
- 主要人员/工商详情 API 365（0.25 元/次）；
- 股权变更 API 998（0.15 元/次）；
- 司法风险组合接口等按能力收费。

决策：`tianyancha-enterprise-intelligence-api` 为 **SHADOW/WATCH fallback**，不因企查查 MCP 不可用就自动要求用户购买；无商业凭据时回落官方 Web。

官方入口：https://open.tianyancha.com/

## 4. Source routing 决策

现行 `source_access_policy.yaml` 的通用 transport score 为 `LOCAL 5 > API 3 > MCP 2.9 > BROWSER/SEARCH 2.8`。用户对**新外部平台能力**明确要求 MCP 优先，因此不宜全局粗改 API/MCP 顺序影响行情、财报等既有 route。

采用 capability-scoped transport preference：对 `enterprise.*` / 企业情报能力使用 `MCP > API > BROWSER/SEARCH > MANUAL`；正式上市披露仍由 officiality/formal eligibility 的大权重优先，不会因为商业 MCP 而压过交易所/CNINFO 的 PRIMARY_OFFICIAL 事实。

## 5. 行业广度最终方案

- 不增加“每行业至少一只”的强制配额；市场可能真实集中在少数行业，硬配额会把弱标的推入研究。
- reserved blind tranche 必须先按纯 market rank 冻结，再合并 existing/expert overlay。
- existing Candidate 作为 incumbent/reuse pool，不能重新定义 blind。
- Skill domain 只能影响 overlay research budget，不能修改 blind membership。
- 输出 `origin concentration` 和 sector concentration/breadth diagnostics；稳定基础映射已直接复用 SSE/SZSE/BJSE 官方 Instrument Master 的行业字段，只有官方 master 也缺行业标签时 diagnostics 才降级，pure blind 始终继续运行。
- 具体行业描述优先于顶层代码：例如 C39/C36/C27 可分别识别为硬科技/汽车/医药，只有粗粒度 `C 制造业` 才保守归为 INDUSTRIALS；`B 采矿业` 单独归 `RESOURCES`，细分为煤炭/石油时才归 ENERGY_UTILITIES。公共行业板块 taxonomy 只负责更细粒度增强与 Expert overlay，实时端点失败时可回退完整性校验通过且未过期的本地不可变缓存。
- bounded breadth-challenger slots 已落地：只从 blind cutoff 附近、仍满足流动性/规模门且属于 blind 未覆盖 broad domain 的候选中补研究广度；不能替代 top blind、不能形成 Candidate/BUY/仓位权威。2026-09-14 最后一次已记录 live smoke（research-seeds:a27daa4cbd026deac76006346ae0cff270548fbfd5bb5a7d3dc239f648e8810）在 20 个 pure blind 之外实际补出 6 个 challenger，覆盖 CONSUMER、ENERGY_UTILITIES、TRANSPORTATION、FINANCIALS、RESOURCES、COMMERCE_DISTRIBUTION；不把早期粗分类运行的行业名单当成最终名单。

这比批量引入 sector-specific Skill 更符合“高冗余、强鲁棒、低耦合”：发现公平性由确定性 core 保证，行业专家只负责提高入选公司的研究深度。
## 6. 2026-09-14 行业方法论补强：公共 Core Methodology，而不是批量博主 Skill

所有者新增约束：现有私人/博主 Skills 在有色、材料、科技链更密集；若 breadth 只扩大行业候选、却没有稳定的行业研究方法，非热门行业可能因研究模板稀疏而质量下降。复核当前 runtime 后，**不把新行业框架注册为自动 Specialist Skill**：`ResearchRuntime` 会要求每个被自动选中的 specialist 同时具备 `specialist_delta_draft`，否则进入 `SPECIALIST_DRAFT_MISSING → NEEDS_INFO`，这会制造新的合同耦合。

因此采用 `PUBLIC_CORE_METHODOLOGY` 设计：

- 核心通用框架：CFA Institute `Industry and Competitive Analysis`，覆盖行业边界、规模/增长/利润、市占率、Porter Five Forces、PESTLE 和竞争定位；
- 跨行业专项框架：ACCA + CFA Institute `Sector Analysis – A Framework for Investors`，提供 21 个行业的业务模型、关键绩效驱动和红旗，来源汇总了上市公司 CFO、资管、卖方/买方分析师和 IR 等行业参与者意见；
- 估值/基准：Aswath Damodaran / NYU Stern 的行业 Margin/ROIC 数据与 Financial Services Valuation，用于方法和同业基准设计，不作为 A 股事实；
- 银行：FDIC Quarterly Banking Profile 的 NIM、ROA、非息收入、净核销、不良/非流动资产、核心资本等标准观察维度；
- 医药/创新药：FDA Drug Development Process 的 preclinical → clinical phases → review → post-market 框架，用于管线阶段/证据不确定性设计；
- 新能源/电池：IEA Global EV Outlook 的装机、化学体系、价格、产能利用与供应链集中框架；
- 农业：USDA ERS Commodity Costs and Returns 的单位成本/区域/品类 cost-return 框架；
- 矿业：World Gold Council AISC/AIC 指引，强调标准化全维持成本且必须回勾 GAAP/IFRS；
- 地产资产运营：Nareit AFFO 作为资产型业务的补充口径，同时明确 AFFO 非统一定义，必须核对公司口径。

工程落地：新增 `configs/industry_methodologies.yaml` + typed `IndustryMethodologyRegistry`，用 1 个 generic fallback + 22 个 sector methodology packs 覆盖当前 22 个 internal archetype。每个方法包只包含 analysis questions / operating metrics / valuation lenses / red flags / source metadata，并永久固定 `fact_authority_allowed=false`、`recommendation_allowed=false`、`private_skill_required_for_analysis=false`、`specialist_draft_required=false`。未知行业回落 `GENERIC_COMPETITIVE_CORE`，方法论稀疏本身不得触发 `NEEDS_INFO`。

主要来源：
- ACCA/CFA Sector Analysis: https://www.accaglobal.com/hk/en/professional-insights/global-profession/ACCA-CFAI-sector-analysis.html
- CFA Industry and Competitive Analysis: https://www.cfainstitute.org/insights/professional-learning/refresher-readings/2026/industry-and-competitive-analysis
- Damodaran Margin/ROIC by Sector: https://pages.stern.nyu.edu/adamodar/New_Home_Page/datafile/mgnroc.html
- Damodaran Investment Valuation: https://pages.stern.nyu.edu/adamodar/New_Home_Page/Inv4ed.htm
- FDIC QBP Graph Book: https://www.fdic.gov/quarterly-banking-profile/fdic-qbp-graph-book-535
- FDA Drug Development Process: https://www.fda.gov/patients/learn-about-drug-and-device-approvals/drug-development-process
- IEA Global EV Outlook 2026 – EV Batteries: https://www.iea.org/reports/global-ev-outlook-2026/electric-vehicle-batteries
- USDA Commodity Costs and Returns: https://www.ers.usda.gov/data-products/commodity-costs-and-returns
- World Gold Council AISC/AIC: https://www.gold.org/gold-standards/non-gaap-metrics-guide
- Nareit AFFO: https://www.reit.com/glossary/adjusted-funds-operations-affo


## 7. 2026-09-14 收尾来源复核与适用性

再次核验了上列 ACCA/CFA 行业分析页面、CFA 2026 行业竞争分析公开摘要、NYU Investment Valuation 支持站、FDA 药物开发流程、IEA 2026 电池章节、USDA 成本收益页面、WGC AISC/AIC 指引及 Nareit AFFO 定义。ACCA 页面明确说明原报告覆盖 21 个行业；本地 22 个 archetype 是内部业务分类和独立改编，并非声称原报告有 22 个分类或官方认证了本地实现。补充交叉参考 FFIEC UBPR（https://www.ffiec.gov/data/ubpr/uniform-bank-performance-report），用于确认银行盈利、资本、流动性及资产负债分析的行业独特性；未新增运行时依赖。

这些来源的可信度支持分析问题和指标口径，不等于已验证的选股收益。复核只使用公开可访问材料及既有已审阅资产，不声称取得 CFA 会员全文或新的转载许可。方法清单按业务适用性裁剪，缺少专项包继续使用 generic fallback；不能为了满足数量或模板而捏造公司事实、跨行业机械套用 KPI，或修改正式研究质量门。
