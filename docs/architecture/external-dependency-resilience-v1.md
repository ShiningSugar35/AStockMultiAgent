# External Dependency Resilience v1 — 能力路由、权威证据与低 API 依赖数据平面

状态：RELEASED（v0.1.0 历史基线；v0.3.0 扩展已正式发布）
日期：2026-09-02
发布证据：v0.3.0 release baseline 为 `5567cab46195a5073a9bf5a4fc16acfe1ff35066`；annotated tag `v0.3.0` 与非草稿、非预发布 GitHub Release 均已创建并指向该 baseline。wheel SHA-256=`19ddb987ef5d73058b204f329525d5628d1a04c934ae6d4ec9f396f76bf1abd8`，sdist SHA-256=`3a0a3efd2b84cecfa67114dabef1a2ad29f51f92d02770641b0f4ee52fa25109`。历史 v0.1.0 release baseline `c764e842d3eb1922bc206b7f3cffdd9759c8f1cc` 与其 tag 保持不变；发布后的 docs-only closeout 只推进 `main`，不移动任何已发布 tag。

## 1. 审核来源与优先级

本文件是外部依赖去脆弱化的现行架构合同，事实源按 `AGENTS.md → docs/architecture/ → 开发计划.md → 进度验收.md → 唯一 durable run → Git/测试/运行工件` 恢复。《进度验收》只保留最近一次任务；历史验收流水由 Git/Release 历史承担。纳入并统一以下输入：

1. 用户提供的《AStockMultiAgent 外部依赖鲁棒性架构与验收规范 v1.0》；
2. 当前唯一 durable run `lr_mtb4gekw_ff5540ff05d2`；
3. 已发布子架构 `provider-resilience-v1.md`；
4. 已发布业务架构 `full-market-research-team-v1.md` 与 `continuous-investment-research-v1.md`。

此前同目标的临时/旧 long-run 只保留历史审计意义，均已被当前唯一 durable run 取代，不再作为可继续写入的平行事实源。`provider-resilience-v1.md` 继续作为 transport lane、Sina Universe fallback、明显截断 floor 与 fresh COMPLETE Master reuse 的已发布子模块；本文件在其上增加 capability-scoped health/breaker、严格 coverage ratio、官方来源与 Web/Search 准入、typed lineage 和 formal gate，不另建第二套路由或证据架构。

## 2. 架构目标

把系统从“业务流程绑定具体 API / 平台”收敛为：

`Research Need -> Capability Contract -> Source Route Plan -> Acquisition -> Validation -> Evidence Admission -> Formal Gate`

核心原则：

- 本地满足 freshness 时优先；
- 正式事实优先官方来源，而不是优先某种 transport；
- 业务层声明 capability，不声明“必须 EastMoney / Sina / BaoStock / CNINFO”；
- Agent / LLM 可以动态决定搜什么、去哪找、换哪个来源；
- Python deterministic core 决定 capability 是否匹配、来源权威性、PIT、完整性、provenance、冲突和 formal eligibility；
- 单 Provider / 单 endpoint / 单代理链故障不得扩散成整个投研任务故障；
- Search/Web 不能替代全市场 Universe、连续 OHLCV 或公告 negative proof 的完整性证明；
- `broker_execution_allowed=false` 永久不变。

## 3. 架构师关键裁决

### 3.1 不新建第二套 Source Router

用户规范建议 `src/astock/sources/`。项目已经存在：

- `core/source_router.py`
- `provider_registry.yaml`
- `source_access_policy.yaml`
- `transport_profiles.yaml`
- Adaptive Edge / ProviderRecovery / SchemaRepair

因此本轮**升级现有事实源，不平行再造一套路由框架**。任何新增 schema/policy 必须接入既有 Router / Registry / Artifact / ObjectStore，不建立第二套来源状态机。

### 3.2 `source_id` 从硬绑定降为兼容 hint

现状 `SourceAccessRequest.source_id` 必填，Router 只在同一 source_id 内选 transport。这不是 capability routing。

目标：

- `requested_capability` 是主键；
- `source_id` 仅作为兼容/显式限定 hint，可为空；
- 当 source_id 为空时，在所有匹配 capability 的候选 source 中排序；
- 结果同时记录 selected source 与 fallback source chain；
- 旧 Zhihu transport audit 调用保持兼容。

### 3.3 Transport 不是证据质量

`API / BROWSER / MCP / Search` 只是传输方式。`source_access_policy.yaml` 现有 `API > MCP > BROWSER` 奖励必须降为弱 tie-breaker。

主排序因素改为：

1. formal eligibility / authority；
2. completeness semantics；
3. local availability；
4. freshness；
5. health / breaker state；
6. independence；
7. cost / latency / auth friction；
8. transport 仅做弱偏好。

对明确要求强官方证据的 capability，只要存在可用 `PRIMARY_OFFICIAL_WEB / PRIMARY_OFFICIAL`，secondary structured 不得反超。

### 3.4 PARTIAL_UNIVERSE 的统一语义

允许在“数据不完整但仍有研究价值”时返回 observation / research candidates；但：

- 不得标 FULL；
- 不得声称完成全市场扫描；
- 不得通过 full-market Recommendation Gate；
- `0 candidates` 与 `universe unavailable/partial` 必须区分。

因此 fail-soft 只作用于研究发现，formal recommendation 仍 fail closed。

### 3.5 `CERTIFIED + FETCH_OBSERVED` 的 PIT 语义

允许当前 live 采集使用 `FETCH_OBSERVED` 证明“最晚在本次成功抓取时已可获得”，前提是：

- 必须绑定不可变 SourceSnapshot；
- `available_to_system_at` 不得早于实际抓取；
- historical `as_of` 早于该时间时必须拒绝；
- 不得把 FETCH_OBSERVED 解释为已知官方发布时间。

这不会放宽未来函数门，但消费者必须保留 availability basis。

## 4. Source Class

统一逻辑分类：

- `LOCAL_IMMUTABLE`：本地不可变 release / snapshot / 冻结配置；
- `PRIMARY_OFFICIAL_WEB`：交易所、CNINFO、证监会、政府、公司 IR 官方网页/文件；
- `SECONDARY_STRUCTURED`：EastMoney、Sina、BaoStock 等批量结构化源；
- `REPUTABLE_WEB_SEARCH`：主流财经媒体、行业协会、可靠研究资料；
- `MANUAL`：自动化路径耗尽后的最后手段。

Source Class 与 Transport 分离。官方网页通过 Browser/Search/HTTP 取得仍属于 `PRIMARY_OFFICIAL_WEB`；财经 API 即使结构化也仍属于 `SECONDARY_STRUCTURED`。

## 5. Capability Contract

正式 capability vocabulary 至少覆盖：

- `instrument.identity`
- `instrument.master`
- `market.calendar`
- `market.daily_unadjusted`
- `market.raw_5m`
- `disclosure.discover`
- `disclosure.enumerate`
- `disclosure.document`
- `corporate_action.negative_proof`
- `financial.official_document`
- `financial.statement_values`
- `news.discover`
- `web.authoritative_fact`
- `embedding.semantic`
- `retrieval.lexical`
- `llm.route_plan`
- `llm.research_synthesis`
- `llm.formal_decision`

Provider Registry / Source Catalog 对每个 source 声明：

- source class；
- capabilities；
- authority / officiality；
- completeness semantics；
- formal eligibility；
- market / scope；
- transport / transport profile；
- independence group；
- cost class；
- local cache/freshness policy；
- retry semantics。

## 6. Local-first / Stale-While-Revalidate

### 6.1 交易日历

- `official_trading_calendar.yaml` 是当前年度正式本地基线；
- 由 SSE / SZSE / BSE 官方休市通知核验后冻结；
- 已覆盖年份 `sync-calendar --live` 不先调用 BaoStock/API；
- 配置缺失年份由 Agent 自动 Search 官方年度休市公告，经 deterministic official-domain admission 后更新版本化配置；
- structured provider 仅作 fallback / shadow cross-check。

### 6.2 Instrument Master / Identity

- 现有 versioned Instrument Master release 是唯一正式本地 release，不建立第二套表；
- 单票 identity 在 freshness 内优先复用本地；
- 全市场 scan 优先最近有效 COMPLETE release，再做增量刷新；
- refresh 失败不得转换为“0 candidates”；
- stale/partial 可服务观察性研究，但 formal full-market gate 关闭。

### 6.3 日线 OHLCV

- 现有 canonical local market store 第一优先；
- 增量更新在注册的 structured providers 间按 capability/health 路由；
- 单票 current research 可使用一条完整日线链，并对最新关键交易日做第二独立源/官方核验；
- 冲突进入 `CONFLICTED`，禁止 silent overwrite。

### 6.4 5m / Paper

- 5m 不作为普通日线投研硬前置；
- 只服务 paper fill / 路径歧义高保真回放；
- raw intraday sync 与其它外部采集一致，breaker 以 `(provider, capability)` 隔离；5m 使用 `market.raw_5m`，不得再回写 provider-wide probe health；
- 单源 fallback 只保留 `SINGLE_SOURCE_5M` 降级语义；已有 canonical 且本轮任一 provider 失败时保持原 canonical，不用单源结果 silent overwrite；
- provider 全挂时研究继续，paper fill 保持等待数据，不伪造成交。

## 7. 公告与 negative proof

拆开两个 capability：

### `disclosure.discover / disclosure.document`

已知公告发现与下载允许通过 CNINFO、SSE/SZSE 官方页、官方域名 Search 等路径恢复，只要最终冻结官方文档与 lineage。

### `disclosure.enumerate / corporate_action.negative_proof`

必须同时证明：

- date range；
- security identity；
- complete pagination；
- terminal proof / has_more=false；
- 无重复/跳页；
- snapshot lineage。

普通 Search 的“未搜到”永远不能证明“没有公告”。CNINFO/交易所完整枚举都不可用时返回 `ENUMERATION_INCOMPLETE / NEEDS_INFO`。

## 8. 财务

- 官方年报/中报/季报 PDF/HTML 是正式真值锚；
- EastMoney/Sina 是快速构表、多期 series、异常扫描与交叉校验工具，不是 deep research 唯一前置；
- secondary structured 全挂时，命名单票允许 Agent 找到官方报告并冻结有限关键字段；CNINFO `ProviderError` 后本次恢复链不重复撞击同一失败 capability，而是转入已准入的精确官方报告；
- deterministic parser/recalculation 负责表名、合并口径、期间、单位、公式与 provenance；正式 release 必须持久化 typed `official_lineage_kind / official_lineage_snapshot_ids / official_exhaustive_proof_allowed`，并逐一验证 snapshot/object；
- `CNINFO_EXHAUSTIVE_ENUMERATION` 可以承载全分页 proof；`OFFICIAL_WEB_EXACT_ITEM_ADMISSION` 只证明已知精确文件准入，`official_exhaustive_proof_allowed=false`，不得冒充公告全集或 negative proof；
- Candidate discovery 可接受满足严格 research-safe policy 的低风险 PARTIAL 财务包，但该状态不得自动升级为 COMPLETE。只有 ObjectStore 可验证且 `status=SUCCEEDED / coverage_status=COMPLETE` 的 typed `FinancialIntegrityEvidencePack` 才能使 `FINANCIAL_INTEGRITY=true`；
- PARTIAL/冲突/过期 current 数据不得开启 `VALUATION=true` 的精确估值，也不得开启正式推荐；observation-only 研究可继续，但必须保留 `NEEDS_INFO / PARTIAL` 与缺口。

## 9. 新闻 / 行业 / 知识 / Embedding / LLM

### 新闻/政策/行业

- GDELT 降为 optional discovery accelerator；
- Web/Search 是 Agent 正式可用的 discovery capability；
- SourcePolicyGate 对官方/监管、可靠媒体、行业权威、普通媒体、社媒分层；
- 重大事实优先一项官方 Primary，缺官方时至少两项独立可靠来源且只能形成相应置信度的研究事实。

### Zhihu / 社区知识

- 已冻结本地知识优先；
- API/HTML/browser 只是 transport；
- 新采集失败只影响 freshness，不阻断公司研究。

### Embedding

- 固定本地模型版本/hash 后日常不依赖 HuggingFace 在线；
- 模型不可用时保留 lexical/BM25 fallback；
- lexical fallback 不改变 active Skill registry 的证据等级。

### LLM

- 路由/查询生成/分类/摘要可动态选择可用模型；
- 正式研究综合与 formal decision 必须满足 approved-quality capability；
- 弱模型/本地小模型不得冒充正式投资判断；
- LLM 只提交 source/query proposal，不能直接改变 formal admission。

## 10. Retry 与 Circuit Breaker

权威工程依据采用 AWS Well-Architected / Builders' Library 与 Azure Retry/Circuit Breaker：

- retry 只在 acquisition boundary 一层实现；
- GET/HEAD 默认最多 2 次；
- POST 仅显式标记为幂等只读查询才可重放；
- 400/401/403/404 等确定性错误不重试；
- 429 不立即轰击，尊重 `Retry-After`，并进入 provider+capability health/breaker 语义；
- 502/503/504/timeout 使用指数退避 + jitter；
- capability request 有总 elapsed-time budget；
- breaker 粒度为 `source/provider + capability`，不是整个平台一刀切；
- CLOSED / OPEN / HALF_OPEN；阈值、窗口、cooldown、half-open probe 数量全部进入 versioned policy，不写死在业务 Service；
- HALF_OPEN 使用持久化 single-probe claim；进程崩溃或 owner 消失后的 stale claim 必须按版本化 TTL 回收，不能永久占槽，也不能同时放行两个 probe；
- OPEN 或有效 HALF_OPEN claim 存在时，当前任务直接切 fallback source，禁止重复撞击同一失败源；同 Provider 的其他健康 capability 不受污染。

### 10.1 Provider Probe 完整 lineage

- 每次 probe 的结构化 report 先进入 ObjectStore，再登记 Artifact 与 append-only event，latest pointer 只指向该 event/artifact，不保存第二份可漂移状态；
- 读取 health 时必须验证 pointer → event → artifact → object → typed report 的完整链，以及 source/provider、capability、artifact id、object hash 和 schema identity 一致；
- pointer、event、artifact、object 或 report 任一缺失、损坏、类型不符或身份矛盾均 fail closed，不得回退为“最近看起来健康”；
- probe 必须分别记录“请求是否实际发出”“Provider 真实成功/失败类别”和“系统 resilience 是否正确处理”，外部网络失败不能被描述成 Provider 连通成功。

## 11. Web/Search First-class Proposal Gate

Agent 只提交：

- query；
- candidate domain/source；
- expected fact；
- requested capability；
- preferred source class；
- reason；
- formal-use intent。

Deterministic `SourcePolicyGate` 检查：

- domain/source 是否属于已知 authority class；
- capability 是否允许 Search/Web；
- 是否需要 exhaustive proof；
- 是否要求 official Primary；
- PIT/freshness；
- independence；
- snapshot/evidence lineage；
- formal admission 是否允许。

Search 结果在未冻结/验源前不能直接成为正式数据库事实。

## 12. P0 / P1 / P2

### P0 — Release blocker

1. 交易日历 local-first 且 policy 真正生效；
2. SourceAccessRouter 从 source-bound 改为 capability-first；
3. Source catalog/provider registry 增加 source class / completeness / formal eligibility / independence / cache semantics；
4. Candidate Scan 不把 provider failure 伪装成 0 candidates；
5. 财务 API 不再是 deep-research / candidate-research 不必要的硬前置；
6. CNINFO discover 与 exhaustive enumeration 语义分开并防重复页/截断；
7. GDELT/社区/embedding/5m failure 不扩散到日线研究主链；
8. single-layer retry；
9. provider+capability breaker / Retry-After；
10. formal evidence admission policy。

### P1 — 本轮至少完成 Web/Search capability + official-domain admission

1. Web/Search capability contract；
2. Authority domain registry；
3. Agent source proposal schema；
4. deterministic SourcePolicyGate；
5. official-domain Search 模板与冻结/admission 合同。

### P2 — 非 release blocker，可在不扩大风险时完成

1. Instrument Master refresh/freshness policy；
2. local OHLCV rolling-cache policy 统一；
3. 官方财报有限字段 extraction recovery；
4. BM25/lexical fallback；
5. optional local LLM route/classification；
6. source-health trend telemetry。

若现有系统已经等价实现，使用审计证据验收，不重复造轮子。

## 13. Failure / Fault Injection Matrix

必须覆盖：

- EastMoney down -> approved alternative/local path；
- Sina down -> approved alternative/local path；
- BaoStock TCP down -> calendar/identity/candidate 不因低频 API 单点失败；
- CNINFO down -> known disclosure 可通过官方网页恢复；negative proof 无 exhaustive route 时 fail closed；
- GDELT down -> Web/Search discovery 继续；
- timeout / 502 / 503 / 504 -> bounded retry + fallback；
- 429 -> Retry-After / breaker，不 retry storm；
- invalid JSON / schema drift -> raw snapshot 保留、拒绝正式结构化写入、切 fallback；
- truncated / duplicate pagination -> enumeration fail closed；
- conflicting OHLCV / financial value -> `CONFLICTED`；
- corrupted local snapshot -> hash verification fail closed；
- total offline -> historical/local portfolio/paper/knowledge 可读，current 明确 stale/unavailable。

## 14. 完整性与 Formal Gate

- Full-market scan：XSHG / XSHE / BJSE **每个市场**均需有可审计 `coverage_ratio >= 99.5%` 才标 FULL；旧 row-count floor 仅作截断防护，不能单独证明 FULL。不足时可返回 PARTIAL research observations，但 formal recommendation gate=false；
- 技术窗口：要求交易日 100% 覆盖、无重复、OHLC 合法；
- disclosure negative proof：完整分页 + terminal proof + lineage；
- precise valuation：关键财务字段无未解决冲突；
- current 结论：不能只依赖 stale 数据；
- formal recommendation：研究/Committee/TradingClassification 等既有硬门继续成立；
- paper fill：数据不足不得伪造成交。

## 15. 本机资源约束

目标环境：约 8 logical CPU / 16 GiB RAM Windows 轻薄本。

- remote acquisition 并发 <= 2；
- CPU 密集解析 <= 4；
- 无 GPU 要求；
- 无常驻 daemon 要求；
- breaker OPEN 后当前任务不重复打同一失败 source；
- fallback 有总预算，不能出现递归/指数请求放大。

## 16. Code Review Gate

逐项检查：

1. 业务 Service 是否仍按具体 provider 名写死流程；
2. Search 是否被误作 completeness / negative proof；
3. 是否存在两层以上 retry；
4. 单 provider failure 是否能扩散成 global failure；
5. LLM 是否可绕过 evidence admission；
6. fresh local snapshot 是否被无意义重复联网覆盖；
7. conflict 是否 silent overwrite；
8. stale/current 是否混淆；
9. Candidate 是否把数据源失败误判为无候选；
10. 正式数值是否有 provenance；
11. 配置字段是否真正被代码消费，禁止“看似可配、实际硬编码”的双轨事实源；
12. broker/paper/PIT/immutable evidence 原有硬边界是否被放松。

任一失败 -> 打回开发返工。

## 17. Professional Test / Release Gate

发布前必须：

- P0 全部完成；
- P1 至少 Web/Search capability + official-domain admission 完成；
- targeted unit/contract/integration/fault injection 全绿；
- `uv run pytest` 0 failed；
- `uv run ruff check .` PASS；
- `uv run pyright` 0 errors / 0 warnings；
- `git diff --check` PASS；
- SQLite read-only `state-integrity-audit` PASS；
- 至少两类独立 market provider + official disclosure live smoke，或由用户明确授权为 manual/live skip；
- single-provider kill matrix 有自动化/recorded fault-injection 证据；
- 文档迁移完成；
- code review PASS；
- 仅用显式路径暂存，staged diff、secret/private/runtime 审计通过；
- commit / push 成功，远端分支 commit 与本地一致；
- 正式 tag 与 GitHub Release 创建并远端验证成功；
- 架构状态迁移为 `RELEASED` 的提交也已推送；
- worktree clean，且唯一 durable run 最终 review/complete 成功。

## 18. 当前实现与正式发布 Code Review 结论

本轮未建立第二套 `src/astock/sources/`。既有 `SourceAccessRouter / ProviderFactory / provider_registry / Adaptive Edge / ObjectStore / Evidence` 继续作为单一事实源，P0/P1 已按本文件合同落地并正式发布；以下为冻结发布树及其远端 release 已验证事实：

1. `SourceAccessRequest.source_id` 已降为可空兼容 hint；Router 以 capability 为主键，按 formal eligibility、官方性、完整性、本地可用性、health、freshness、independence 等排序，transport 只保留弱 tie-breaker。
2. `provider_registry-v5` 已扩展为 capability-aware Source Catalog，声明 source class、formal capabilities、completeness semantics、independence group、cache TTL、transport profile；业务链通过 Factory/Router 消费，不再以具体 Provider 名作为研究资格。
3. `source_resilience.yaml` 的 30 秒 acquisition elapsed budget 已真正接入 HTTP transport；GET/HEAD 默认最多 2 次，CNINFO 只读 POST 由专用 profile 显式允许，Reference 上层 retry 固定 1，避免嵌套放大。
4. `source/provider + capability` breaker 已持久化到 SQLite，支持 CLOSED/OPEN/HALF_OPEN、Retry-After、单 probe claim 和 failover；live probe 的 ProviderError/raw-capture schema drift 均被收口为结构化 health 状态，不再击穿探针框架。
5. `official_trading_calendar.yaml` 已成为 2026 年 local-official-first 基线；已覆盖年份的日历不先调用 BaoStock/API。EastMoney/Sina 日线 fallback 只有在实际 K 线日期集合与该官方日历的开放交易日集合完全一致时才可标 COMPLETE；缺日、多日或年份未配置继续 fail closed，因此 BaoStock 不再是日线完整性的隐性认证单点。
6. Instrument Master / Seed 路由会验证本地 release/object/snapshot identity；generic Provider Catalog 不再用“该 Provider 最近有任意快照”伪造 capability-specific local cache 命中。
7. Full-market FULL 不再由 XSHG/XSHE/BJSE 的最低行数 floor 单独证明；**每个市场均必须有可审计 `coverage_ratio >= 99.5%`**。floor 仅用于明显截断防护；PARTIAL 可做观察研究但 `formal_full_market_coverage_allowed=false`。
8. “完整 Universe 下 0 个候选”与“Universe unavailable/partial”已拆成不同状态，Provider failure 不再伪装成 0 candidates。
9. CNINFO `search` 与 `search_all`/enumeration 已分离；重复页、截断、`hasMore` 异常 fail closed。命名单票官方财报恢复必须穷举分页后才能形成 negative proof，首页 miss 不再等价于“官方报告不存在”。
10. 财务 secondary providers 全挂时可从精确官方报告恢复有限关键字段；strict research-safe PARTIAL pack 只能继续候选研究，不能升级 COMPLETE、精确估值或正式推荐。
11. live 日线主链在最新关键交易日做独立源 shadow validation；规范化 OHLCV 值冲突进入 `CONFLICTED`，不发布 canonical release、不 silent overwrite。第二源不可用只形成验证缺口，不反向否定已取得的主链事实。
12. Search/Web proposal 必须经 `SourcePolicyGate`；普通 Search 永远不能证明 Universe、连续 OHLCV 或公告 negative proof 的 completeness。PIT、不可变 SourceSnapshot/provenance、paper ledger、`broker_execution_allowed=false` 均未放宽。
13. Provider probe health 只接受 pointer → event → artifact → object → typed report 全链校验通过的结果；任一层损坏或身份矛盾均 fail closed。HALF_OPEN stale claim 可恢复，且 health/breaker 按 capability 隔离。
14. 正式官方财务 release 持久化 typed lineage；CNINFO 全分页与 Official Web exact-item 的 authority 明确分离。CNINFO `ProviderError` 后不会在同一恢复链重复撞击，已冻结 exact-item 只能恢复有限字段。
15. `ResearchSeedReport` 与 `FinancialIntegrityEvidencePack` 已成为 Universe/Financial readiness 的 typed member artifact；Role 文本或布尔值不能自证 FULL/COMPLETE。财务 PARTIAL 强制 `VALUATION=false`，正式推荐保持 observation-only。
16. 真实 live 没有掩盖第三方状态：EastMoney 5m 为 `UNAVAILABLE / NETWORK`，Sina 5m 为 `HEALTHY`；真实 fallback 由 Sina 返回 48 bars，但未覆盖旧 canonical。CNINFO 本次真实连通并冻结 3 条公告索引与 1 份官方 PDF；2026 年 XSHG 日历直接命中本地官方配置。
17. s6 定向 resilience 矩阵为 `206 passed / 0 failed`；s7 迁移与最终独立 Review 又发现并修复 migration 0060 缺少显式 `0059 → 0060`/checksum 回归，以及 raw 5m/60m 同步仍写 provider-wide health、未消费 capability breaker 的双轨语义。修复后的最终冻结树真实门为：Ruff=`PASS`；Pyright=`0 errors / 0 warnings / 0 informations`；`git diff --check`=`exit 0`（仅 CRLF→LF 提示，无 whitespace error）；`uv run pytest` 收集 `1072` 项，结果 **`1054 passed / 18 skipped / 0 failed`，1162.11s**；只读 `state-integrity-audit`=`PASS / integrity_check=ok / read_only=true`。18 个 skip 均为既有显式 opt-in 的 OCR/private/live 项，本轮真实 live 证据已另行执行并记录。
18. s8 发布门已完成：91 个显式 staged 路径经最终 diff Review、secret/private/runtime、binary、private absolute path 与 broker authority 复扫均无阻断；实现提交 `c764e842d3eb1922bc206b7f3cffdd9759c8f1cc` 已推送并与远端 `main` 一致；`v0.1.0` annotated tag 与 GitHub Release 已创建，Release 为正式非草稿、非预发布状态，target 精确绑定该实现提交。
19. v0.3.0 M-06 已在既有 Router/Provider 架构上增加 `ExternalCapabilityRegistry` 与 append-only qualification/revocation 索引。`PRODUCTION_BACKUP` 不由 Provider 配置自证：Provider 必须绑定 external capability，资格报告需在 ObjectStore lineage 中有效、未到期/未撤销、authority/completeness ceiling 与 Provider 一致，且标准路线当前不可用时才可参与路由。GDELT 已纳入 Provider Registry/capability breaker/SourceSnapshot，固定为 `news.discovery.lead + DISCOVERY_ONLY`、无 formal capability；执行型券商 MCP 在资格 schema 层永久拒绝。SQLite 资格索引不是新的事实源，任何索引字段与不可变资格报告漂移均 fail closed。
20. v0.3.0 R-01 已把上交所/深交所官方证券 denominator freeze 接入既有 `instrument.master` 证明链；新的官方 freeze 只从真实 `available_to_system_at` 起生效，禁止回填历史。国家统计局、人民银行、财政部、发改委宏观 authority 当前保持 recorded-first、`live_supported=false`；北交所财报/公司行动只准 exact-item official capture，不声称 exhaustive enumeration 或 negative proof。
21. v0.3.0 E-02 已对 Arelle、Docling、Playwright MCP、AKShare、Crawl4AI、changedetection.io 与三个 Repo Skills 固定版本并执行 M-06 资格裁决。当前九项候选**全部保持 `SHADOW`**：外部候选缺 endpoint-specific rights / recorded-live / SBOM 等资格证据；`source-qualification-auditor` 虽通过 recorded contract、observability、revocation 与 uninstall 回归，但没有冻结真实 controlled-live Skill 的任务输入、命令/输出 trace、不可变结果与延迟证据，因此也不能获得 `PRODUCTION_BACKUP`。治理 Skill 的结果始终不能提升底层数据源的官方性、完整性或推荐资格。
22. AKShare 的包级许可证/可安装性不能替代 endpoint-specific 上游数据权利、PIT、provenance、recorded/live、SBOM 与退出证据；缺任一 M-06 门时不得为了满足数量目标硬升生产级。资格证据的 ObjectStore freeze 对 validated model 做 canonical JSON（`exclude_unset=true`），因此 CRLF/LF checkout 差异和未显式提供的嵌套默认时间不会改变 report identity。
23. `SHADOW` 是外部资格证据不足时的安全稳定终态，不等于“软件还没开发完”。M-06 权利/授权或 controlled-live 证据只有在用户明确重新开启相应资格任务并具备合法输入时才继续收集；在此之前不得把 AKShare 或其他候选的 `PRODUCTION_BACKUP` 资格长期挂在《开发计划》作为发布/完工 blocker。资格缺口只保持 Router fail-closed，不得用包 License、安装成功、fixture 或治理 Skill 替代上游 endpoint 权利。

Code Review 实际发生多轮打回返工，包括：CNINFO 财报首页误作 negative proof、elapsed budget 只解析未消费、完整扫描 0 candidates 与 Universe unavailable 混淆、OHLCV 多源冲突 silent-first-win、generic local cache 误把不相关快照算作 capability cache、EastMoney/Sina 日线 fallback 永久 `complete=false` 造成 BaoStock 隐性单点、Provider 排序把 EastMoney bulk fallback 提前到 Sina exact 之前、live probe 对 ProviderError/raw schema drift 异常穿透、官方 Web 稳定 source id 未进入财务白名单、财务 PARTIAL 可自报精确估值、migration 0060 缺少升级/checksum 专项门，以及 legacy intraday sync 继续写 provider-wide health。上述问题均已修复并增加回归/故障注入测试；最终独立 Review 对 capability 隔离、完整性、lineage、PIT/ObjectStore/Evidence/Paper/broker 边界、migration 与文档职责逐项复核后通过。

## 19. 不采用的极端方案

- 全部改用 Web Search；
- 单纯再堆大量财经 API；
- 让 LLM 直接把网页结果当正式数据库；
- 为离线强行把正式投资判断降级到本地小模型；
- 再造第二套 Source Router / Evidence Store / Paper Ledger。

## 20. 权威依据

- AWS Well-Architected — Control and limit retry calls / exponential backoff with jitter / one retry layer；
- Microsoft Azure Architecture Center — Retry Pattern / Circuit Breaker Pattern；
- OpenAI Agents SDK — LLM orchestration 与 code orchestration 可混合；
- SSE / SZSE / BSE 官方年度休市安排；
- CNINFO / 交易所 / 发行人 IR 作为 A 股正式披露事实的高权威来源。
