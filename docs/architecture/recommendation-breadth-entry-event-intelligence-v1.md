# 推荐广度、入场质量与企业事件情报架构 v1

> 状态：CURRENT
> 适用：`FULL_RESEARCH_RECOMMENDATION` 当前研究链
> 权威边界：本层只补发现广度、入场上下文与事件证据；公共荐股权威仍只有 `RecommendationResearchReceipt + Publication Gate`。

## 1. 目标

本层解决三个彼此关联的问题：

1. 全市场 blind top-N 可能因成交额、流通市值、换手同时集中于少数热门行业而把有限深研预算集中到同一经济链；
2. 公司质量与估值研究较深，但缺少统一、可复算的“当前价格位置/趋势是否适合开始建仓”的上下文；
3. 事件研究对普通公告/政策覆盖较强，但关键人事、工商、信用、司法、上下游关键人变化缺少统一 typed contract 与跨源冲突解析。

该层不新增第二事实库、第二投委会、第二估值账本或独立交易系统。

## 2. Blind discovery 与 breadth challenger

### 2.1 Pure blind 永远先成立

`ResearchSeedService` 的 blind market tranche 只由当前全市场行情的 market score 选择。当前 market score 仍是成交额、流通市值、换手的确定性相对排序；Existing Candidate、Knowledge Skill、Serenity Delta 都不能改变 reserved blind 成员。

为避免未来参数调整导致 overlay priority 间接改变 blind 子集，最终 blind selection 也显式按 `market_liquidity_score` 重排，而不是按合并后的 research priority。

### 2.2 Breadth challenger 是额外研究预算，不是行业配额

breadth 的默认行业来源复用三交易所官方 Instrument Master：上交所的 `CSRC_CODE/CSRC_CODE_DESC`、深交所名录中的“所属行业”、北交所 master 的行业字段先穿透到当前 market snapshot，再折叠到宽口径 economic domains（金融、医疗、消费、商贸分销、硬科技、汽车、能源公用、资源采掘、材料、工业、农业、运输、地产、军工、新能源）。具体行业描述优先于一级字母代码；例如“C39 计算机/通信/电子设备制造”可识别为硬科技，而只有“C 制造业”时才保守归为工业；“F 批发和零售”保守归为商贸分销，不把半导体分销商等企业仅因交易所顶层 F 类误标成消费。该 taxonomy 只服务内部研究预算，不拥有 certified portfolio taxonomy 权威。

breadth challenger 只允许：

- 来自 blind cutoff 之外；
- 仍满足流动性/规模最低门；
- market score 不低于 blind cutoff 的策略比例；
- 属于 blind tranche 尚未覆盖的 broad domain；
- 每个缺失 domain 只取最高 market-score 候选，且总数受策略预算限制。

它不得替代 blind top-N，不得因某行业缺失而放松公司质量，不得使用 Serenity/Knowledge Skill 数量作为行业公平性打分。其 `BREADTH_CHALLENGER` origin 只表示“获得额外研究机会”，仍需 Promotion → Candidate → Company Research → Committee → Portfolio → Publication Gate。公共行业板块 taxonomy/constituent 只用于更细粒度增强与 Expert overlay；live 路由成功后会把已验证 Snapshot 与规范化 payload 指针冻结到本地 checkpoint，30 日内端点故障时可复用不可变缓存。即使该板块接口和缓存都不可用，breadth 仍优先使用交易所官方 master 的一级/细分行业字段；只有官方 master 也缺行业映射时 breadth 才显式降级，而 pure blind 始终继续运行。

### 2.3 行业中性发现，不等于 Skills 数量平权

当前私人/博主 Skills 的来源在部分资源、有色、科技链更密集，因此禁止用“每个行业补齐相同数量 Skills”来制造表面均衡。正确分工是：

- **发现层行业中性**：blind 与 breadth 不把私人 Skills 数量当作行业先验；
- **研究层能力诚实**：通用研究、行业专项和正式 Evidence 才构成 core readiness；当前 Research Team policy 要求 universal ≥ 90%、industry ≥ 80%、evidence ≥ 90%；
- **私人 Skills 只做 edge**：`private_skill_coverage` 可以提高某些行业的研究深度和差异化视角，但 `private_skill_gates_recommendation=false`；Skills 稀疏不能自动淘汰一个本来可由官方证据和行业 archetype 研究清楚的公司，Skills 丰富也不能挽救 core coverage 不达标的公司；
- **不降低行业门槛**：若银行、医药、消费等行业的行业 archetype、关键 KPI、竞争格局或证据覆盖不足，则候选降级为观察/继续补证据，不因 breadth 预算而强行进入正式推荐；
- **按缺口补能力，而不是按行业配额补博主**：后续 Skill scouting 由实际 `ResearchCoverageReport`、缺失行业任务和投研复盘驱动，优先补稳定、可验证、能复用的方法论；没有高质量来源时宁可保持 Skills 稀疏。
- **公共 core methodology 补齐行业方法，不注册成 Specialist**：`configs/industry_methodologies.yaml` 将 CFA/ACCA 行业框架、CFA Industry & Competitive Analysis、Damodaran 行业/估值方法以及 FDIC/FDA/IEA/USDA/WGC/Nareit 等监管或行业标准抽象成 question/KPI/valuation/red-flag 方法包，覆盖现有 22 个 internal archetype。它由 `industry-value-chain` 核心任务解析，不进入 `ResearchSkillService` specialist route，因此不会因为新增方法包缺少 `specialist_delta_draft` 而制造 `NEEDS_INFO`。
- **方法论来源不是公司事实来源**：所有 public-core methodology 均固定 `fact_authority_allowed=false`、`recommendation_allowed=false`；FDIC/FDA/USDA 等境外来源只提供“应该看什么/如何分析”的方法，A 股公司的当前事实仍必须来自本轮交易所/CNINFO/发行人/监管/合格行业证据。当前已知 22 个 archetype 全部具备专项包；未来新增 archetype 若专项包尚未同步，则自动 `ARCHETYPE_FALLBACK → GENERIC_COMPETITIVE_CORE`，完全未分类查询则 `GENERIC_FALLBACK`，方法包缺失本身不得终止研究。

因此 breadth 的目标是减少“研究机会被热点行业垄断”，公共方法论负责让非热门行业也具备稳定的 core research depth；私人 Skills 则继续提供可能有 alpha 的差异化 edge，而不是 readiness 前置门。

## 3. EntryQuality：好股票是否处在可接受的入场位置

### 3.1 不是技术面买卖系统

EntryQuality 只回答“正式研究已经认可的公司，现在的价格位置是否适合一次性/分批开始建仓”。它不能：

- 把 `REJECT / WATCH / NEEDS_INFO` 变成 BUY；
- 单独产生目标价、权重或订单；
- 用 RSI/MACD 单指标、K 线形态或“距离 52 周高点很远”直接宣称低位；
- 替代基本面、估值、Bull/Bear、Committee 或 Portfolio 风险预算。

### 3.2 确定性输入

`EntryQualitySnapshot` 从 canonical D1 历史和本轮 `MarketPriceAnchor` 计算：

- 20/60/120/250 日价格区间位置；
- 20/60/120/250 日收益；
- 距窗口高点 drawdown；
- 20/50/100/200 日均线距离与趋势排列；
- 实现波动；
- 5 日/20 日量额活跃度；
- benchmark 可用时的 60 日相对强弱。

盘中推荐允许用最新 `MarketPriceAnchor` 覆盖历史序列最后收盘来计算当前区间位置；历史底座和当前报价 lineage 分别保留。日线 release 只按注册的 `XSHG/XSHE/BJSE:{company_id}` scope 解析，必须唯一命中，不按证券代码前缀猜交易所。

120 日筑底状态要求实际 120 个观测；依赖 250 日位置的 `ATTRACTIVE_DISLOCATION / EXTENDED` 要求实际 250 个观测。历史不足时仍可识别短周期趋势/快速下跌风险，但不得把短历史包装成长期低位，且综合评分受 policy cap 限制。

当前 canonical D1 是未复权可执行价格。若检测到极端价格不连续，EntryQuality 必须降级并暴露 `CORPORATE_ACTION_DISCONTINUITY_RISK`，不得把除权/送转跳变误判为低位。未来可替换为研究专用复权价格视图，但不得改变上层 EntryQuality 合同或真实成交价格语义。

### 3.3 状态与组合用法

当前状态包括：

- `ATTRACTIVE_DISLOCATION`：较低价格位置且下跌已出现稳定迹象；
- `BASE_BUILDING`：中低位横向筑底；
- `TREND_CONFIRMED`：趋势健康，但不等于绝对低位；
- `FALLING_KNIFE_RISK`：价格低但趋势仍快速恶化；
- `EXTENDED`：趋势强但相对中期均线/区间已经过度延伸；
- `NEUTRAL`；
- `INSUFFICIENT_HISTORY`。

Full Research 排序在公司预期收益、质量、估值之后才看 timing risk / EntryQuality，避免技术面凌驾于公司质量。Portfolio 仅用 policy-driven multiplier 缩放已 eligible 的初始仓位并保留更多现金；无 EntryQuality 的历史工件按兼容默认值继续运行。

## 4. 企业/人事情报

### 4.1 Typed event scope

事件层除传统业绩、订单、政策、诉讼外，必须覆盖：

- 董事/高管任免、辞任、换届；
- 实控人、控股股东、法定代表人和工商登记变化；
- 关键人员外部政府/公共职务任命；
- 股份/股权质押、冻结、司法冻结；
- 行政处罚、重大诉讼、执行、信用/债务异常；
- 重要子公司、合作方、上下游关键实体变化；
- 关键合作方/上下游关键人物变化。

`NewsEvent` 附带 category、人物、相关实体和 relation scope，避免一条事件只有自然语言摘要而无法判断它影响公司、控制人、合作方还是公共职位。

### 4.2 来源层级与 MCP-first

生产基线仍按事实权威性排序：

1. 交易所 / CNINFO / 发行人 / 监管；
2. 政府正式任命、国家企业信用信息公示、信用中国、法院/执行等具名官方域；
3. 通过外部能力资格审计的企业数据库。

对 `enterprise.*` 的**外部商业能力**，SourceAccessRouter 使用 capability-scoped transport preference：`MCP > API > Browser/Search > Manual`。这不改变 officiality/formal eligibility 的大权重，因此一个商业 MCP 不会因为“传输方式优先”压过交易所或政府的 PRIMARY_OFFICIAL 事实。

QCC MCP、QCC API、Tianyancha API 当前只登记为 `SHADOW` optional capabilities，最高只能在真实 M-06 qualification、授权/数据权利、健康度和退出门通过后成为 `PRODUCTION_BACKUP`。无凭据或商业服务故障时，主研究继续沿官方 Web/现有 Provider 自动恢复。

## 5. Cross-source resolution

`EnterpriseIntelligenceObservation` 必须绑定：证券/公司、事件类型、关系范围、人物/实体、事件日期、标准化 fact key/value、SourceSnapshot、Evidence 以及对象 hash。

`EnterpriseIntelligenceService` 对同一 company + fact key 的多个 observation 做确定性解析：

- 归一值一致 → `CONFIRMED`；只有出现两个及以上不同 `source_independence_group` 时才标记 `cross_source_confirmed=true`。同一厂商的 MCP/API/SDK 属于同一独立组，不得冒充多源交叉验证；
- 值冲突 → `CONFLICTED` + `investigation_required=true`，不选 preferred value；
- 冲突 resolution 不能投影为正式 `NewsEvent`，必须返回 Evidence Investigation；
- 来源一致时优先 PRIMARY_OFFICIAL observation，商业 SECONDARY_STRUCTURED 只能作为交叉证据；
- `project_news_event` 必须反查已注册 resolution artifact，并要求 observation 集合与 resolution 完全一致，不能用调用方临时替换后的对象投影正式事件；
- 未显式传解析时间时，以输入 observation 的最新可用时间作为确定性 resolution 时间，保证同一实时输入幂等。

官方 Web 域名匹配采用“最具体后缀优先”，因此 `gsxt.gov.cn`、`creditchina.gov.cn`、`zxgk.court.gov.cn` 不会被通用 `gov.cn` 抢匹配。通用 `*.gov.cn` 仍只提供 `web.authoritative_fact`，不会被粗放赋予 `enterprise.personnel` 等专门 capability；地方政府任命公告需以精确页面冻结为正式 Web Evidence。商业能力未授权或不可用也不会让仍可公开取得的官方事实直接变成用户补资料。

这保证“网上冷门但重要的人事/工商线索”可以被捕捉，同时不会因为某一个商业数据库的延迟、错误或同名匹配而静默污染正式结论。

### 5.1 Canonical Evidence 权威上限与旧记录兼容

企业事实 observation 只接收既有 Evidence 库中的 DIRECT + PRIMARY_OFFICIAL/SECONDARY 事实。INFERRED、UNVERIFIED、CONFLICTED、COMMUNITY_LEAD 和 PRIVATE_PRIMARY 保留在原 Evidence 调查路径，不能因 observation 标签而变为已确认公共企业事实。

`observation.source_class` 是输入元数据，不是提升权威的授权：正式事件分类和优先 observation 的选择都受 canonical EvidenceGrade 约束。二手证据误标官方会自动降回二手，而不是因可选标签未对齐终止有效研究。多源确认必须同时存在两个不同 source_id 和两个独立组；同一源的两个自填 group 不能制造交叉验证。

解析身份包含 `canonical-direct-evidence-v1` authority policy，修复后的重算生成新解析而不改写旧不可变记录。读取旧解析时，投影仍重新核验 Evidence 权威和来源独立性，不能凭旧 cross_source_confirmed 字段恢复高置信度。

## 6. 永久安全边界

- breadth challenger / EntryQuality / Enterprise Intelligence 全部 `recommendation_allowed=false`；
- Existing Candidate、Skill、热点新闻、企业数据库都不能绕过 Universe/Candidate/Financial/Governance/Committee/Portfolio；
- 商业源不能被提升为官方 source class；
- Web/Search 未命中不能证明事件不存在；
- 本层只服务 CURRENT 实时研究与实时模拟交易，不新增回测/历史重放能力或相关额外门禁；
- `broker_execution_allowed=false` 永久不变。
