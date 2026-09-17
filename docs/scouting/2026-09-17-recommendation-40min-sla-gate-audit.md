# 荐股 / 投资组合 40 分钟 SLA：门禁、冗余与提效审计（2026-09-17）

> 状态：SUPERSEDED_PROPOSAL（2026-09-17 所有者修订）。后续实施以 `../../开发计划.md` 的 WP-30～WP-37 与机器索引为准，调研复核见 `2026-09-17-current-research-sla-patterns.md`。
> 本文“PIT硬保留”“真实券商禁止门硬保留”的建议已明确撤销，计划要求删除这些活动字段、校验与门禁，不只是默认关闭。固定串行40分钟阶段、固定深研3～4家、粗cutoff缓存键也已被新方案修订。
> 本文是热点与改造建议，不是所有控制点逐项验收的证明。4小时13分钟为跨中断的durable日历跨度，93项为后端命令数；不能当成纯计算耗时或LLM调用数。保留下文仅用于解释方案演变，不作为现行开发约束。

## 0. 结论先行

当前系统的问题不宜概括为“研究太深”，更准确地说，是四类开销叠加：

1. **数据规范化仍有一部分落在 Agent / 临时脚本层**。Provider 原始字段、行业特有财务口径、旧/新 schema、RoleOutput / Result 组装没有完全收口到稳定的确定性函数。2026-09-17 实战中，Sina / Tencent 的 PE/PB 映射、旧 CompanyResearchIntent / PolicyRegimeProfile 字段、artifact identity / readiness identity 对齐都造成了重复返工。
2. **同一研究事实被多层门禁重复证明**。Research Team readiness、Capability RegisteredOutputVerifier、DomainContractAudit、Full Research Mandatory DAG、Publication Gate、Closure、ResponseGateway 之间有明显的“同一份 typed artifact / hash / PIT / completion 再查一次”现象。
3. **深研 fan-out 与重试预算对 40 分钟 SLA 不友好**。当前 LOW_RESOURCE 仍允许最多 8 个 deep candidates；Current Research 单家公司自动补证预算上限 1800 秒。广义荐股如果 2～4 家同时遇到数据补证或 schema drift，尾延迟会非常高。
4. **慢变量复用不足，SLA 可观测性不足**。当前 observability report 没有真实覆盖这类跨 Agent、跨工具、跨 gate 的端到端 wall time；research-efficiency-report 对当前 Skills 仍大量显示 usage_count=0。系统因此既难确认“时间花在哪”，也难做基于材料变化的增量复用。

2026-09-17 最近一次正式投资组合实战，从 durable run 创建到完成约 **4 小时 13 分钟**，累计 **93 个执行任务**。这不是正常生产态应该接受的时延。目标应明确设为：

- **P50 ≤ 20 分钟**；
- **P90 ≤ 35 分钟**；
- **P95 / hard deadline ≤ 40 分钟**；
- 40 分钟不是降低事实正确性或 PIT 边界，而是要求到期后停止新增可选研究；若硬门仍未闭合，则输出“本轮不形成新的正式买入/组合结论”，而不是继续无限补证。

建议把当前多层门禁重构为三层：

1. **Artifact Admission（工件准入）**：typed schema、hash、PIT、entity、lineage 在工件生成/注册时只验证一次，输出可缓存的 AdmissionProof；
2. **Recommendation Readiness（推荐就绪）**：仅聚合“真正影响投资结论”的 hard/conditional gates，一次性形成 canonical readiness snapshot；
3. **Publication Guard（发布护栏）**：只检查最终结论是否具备发布资格、隐私/输出边界、是否错误携带即时交易参数，不再递归重跑整棵研究树。

---

## 1. 当前热路径门禁总表与裁决

### 1.1 请求、来源与冻结边界

| 门禁 | 当前作用 | 裁决 | 40 分钟方案 |
|---|---|---|---|
| Investment Expectation 解析 | 本金/目标年化从本轮→历史→默认解析 | **保留，提速** | 改为 O(1) 的 indexed latest expectation，不再扫描最多 1024 个 registry / 16 个历史对象作为常态路径 |
| Request identity / idempotency | 同 request_id 不得绑定不同语义 | **硬保留** | 注册时一次完成；重试只比对已冻结 fingerprint |
| Current Decision Freeze | 固定 evidence cutoff 与输入 artifact | **硬保留** | 保留；由编排器自动生成，不允许 Agent 手工拼 input bindings |
| PIT / no-look-ahead | 禁止未来信息泄漏 | **硬保留** | 历史研究严格；CURRENT 只检查 `available_to_system_at <= cutoff`，不要套历史 revision completeness 的额外要求 |
| SourcePolicyGate | 限制来源权威性、域名、能力范围 | **硬保留** | Provider / official-web 层一次判断并缓存 source qualification，不在每个 Agent 重问 |
| broker_execution_allowed=false | 研究结果不得直接真实下单 | **硬保留** | 完全不动；与 40 分钟 SLA 无冲突 |
| paper_ledger_write_allowed=false | 研究阶段不得偷偷写模拟账本 | **硬保留** | 完全不动；模拟交易另走 execution lane |

### 1.2 Current Research 数据获取门

| 门禁 / capability | 当前作用 | 裁决 | 40 分钟方案 |
|---|---|---|---|
| INSTRUMENT_IDENTITY | 证券身份 / 上市状态 | **硬保留** | 本地 instrument master 优先，只有 revision 过期/冲突才访问外网 |
| DAILY_MARKET | 当前及近期日线 | **硬保留** | 同一 run 只取一次；多个后续节点共享 canonical daily snapshot |
| FINANCIAL_ANNUAL | 最新年度财务 | **硬保留** | `FinancialFactStandardizer` 程序化处理，Provider secondary 失败时直接定位官方年报，不让 LLM 做字段清洗 |
| FINANCIAL_LATEST_INTERIM | 最新中报/季报 | **硬保留** | 与 annual 共用同一 financial adapter / canonical metric pack |
| CORPORATE_ACTIONS | 公司行动 | **条件保留** | 仅当存在分红、增发、并购、拆并股、停复牌等 materially affects valuation/execution 的变化才进入热路径 |
| automatic resolution 1800s | 单公司自动补证预算 | **必须收紧** | 建议常态每公司 360～480s，上限 600s；全请求 hard deadline 40min；fallback 用 provider race / hedged request，避免串行三轮重试 |
| max retry rounds=3 | Provider / acquisition 重试 | **宽松化为 1+fallback** | 每 provider 最多 1 次 retry；同 capability 的健康备源可并发/延迟竞速；非 retryable 立即换源 |

### 1.3 候选发现门

| 门禁 | 当前作用 | 裁决 | 方案 |
|---|---|---|---|
| FULL Universe official denominator | 证明上交所/深交所/北交所 denominator 完整 | **条件硬门** | 只有用户明确要求“全市场最优/覆盖全部 A 股”时严格 blocking；普通“推荐股票/组合”允许 `BROAD_MARKET` 声明清晰覆盖范围，不为一个边缘 exchange denominator 卡死整轮 |
| Blind Candidate Scan | 防止手工挑股 / 历史偏见污染 | **硬保留** | 继续 deterministic，成本很低 |
| Candidate QUALITY_GATE | 数据质量 PASS/PARTIAL/FAIL | **保留** | FAIL 仍 hard；PARTIAL 可进入研究但带质量折扣，不应等同 fail |
| Candidate LIQUIDITY_GATE | 最少交易日、中位成交额、非零成交比率 | **分层** | 对“研究资格”改 soft/conditional；对“可执行仓位”仍 hard。长期价值候选不能仅因流动性较低被禁止研究 |
| Candidate TRADABILITY_GATE | 可交易状态 | **分层** | 研究可以继续；形成“当前可执行仓位”时 hard。停牌公司仍可做研究但不能给即时交易参数 |
| Candidate signal strength | MODERATE/STRONG 才 RESEARCH_READY | **保留，前移为 cheap screen** | 强度由程序计算，不让 LLM 解释后再决定是否入围 |
| universal/industry/evidence coverage floors | 90/80/90 | **保留但避免多重阻断** | coverage 只在 shortlist promotion 一次计算；后续引用该 proof，不重复重算 |

---

## 2. Research Team recommendation gate：逐项裁决

### 2.1 Full-market required checks（当前 19 项）

| Check | 裁决 | 说明 / 修改方式 |
|---|---|---|
| TEAM_DAG_COMPLETE | **删除“独立门禁”身份** | DAG 是否完成是 readiness 的结构事实，不需要再作为一个业务 check；由聚合器直接验证 required task terminal states |
| UNIVERSE_COVERAGE | **条件硬门** | 只有 `FULL_MARKET` claim 必须 100% 形式化；普通 broad-market 推荐允许声明 coverage scope |
| MACRO_REGIME | **保留，共享一次** | 一次生成 `SharedRegimePack`，所有公司复用，不按公司重复跑 Agent |
| SECTOR_COMPARISON | **与 INDUSTRY_PROFILE 合并** | 一个 `IndustryContextPack` 同时承担 sector ranking + 深度行业语义，shortlist 行业共享 |
| BLIND_CANDIDATE_SCAN | **硬保留** | 成本低、能防止人工挑股偏差 |
| INDUSTRY_PROFILE | **保留但合并** | 不再与 sector comparison 两套工件/两层 gate |
| COMPANY_ECONOMICS | **硬保留** | 公司业务与利润驱动是核心投资事实；数据层 deterministic，LLM 只负责无法公式化的驱动解释 |
| FINANCIAL_INTEGRITY | **硬保留但改“重大性”口径** | 核心三表、审计、关键 identity/PIT、重大勾稽冲突仍 hard；非重大字段缺失允许 `COMPLETE_WITH_LIMITATIONS`，不能因一个边缘字段反复补证 |
| DRIVER_TREE | **不再单独 hard gate** | 合并进 `CompanyEconomics/ForecastPack`；只有 forecast 引用的 driver 必须有 evidence/provenance |
| FORECAST_BULL_BASE_BEAR | **硬保留** | 但尽量由 deterministic scenario engine + 少量 LLM assumptions 完成 |
| VALUATION | **硬保留** | 买入/卖出/组合推荐必须有 current-price-relative valuation |
| QUANT_FACTOR | **降为条件门/soft** | 3～12 个月基本面荐股不应因十因子不全而禁止正式结论；只有 factor/timing/optimizer 明确需要时 hard |
| MARKET_PRICE_ANCHOR | **硬保留** | 正式“当前是否值得买”必须有价格锚；但 120s quote freshness 只用于即时执行参数，不用于中期研究结论 |
| CATALYST_RISK | **保留但 bounded** | 做 material event delta；不要求每个公司每次都重新扫 7/30/90 天所有类别；已有 coverage 未发生 material delta 时复用 |
| BULL_CASE | **保留，和 Bear 并行** | 结构化短输出；不做长报告 |
| BEAR_CASE | **保留，和 Bull 并行** | 同上 |
| INDEPENDENT_REVIEW | **改条件触发** | 默认先用 deterministic contradiction / consistency check；只有 Bull/Bear spread、治理/会计红旗、估值边界冲突时调用独立 Reviewer LLM |
| COMMITTEE | **保留，但 rules-first** | 明确可判定的 PASS/WATCH/REJECT 由确定性委员会规则直接出结果；只有边界冲突再调用 LLM 解释/仲裁 |
| PORTFOLIO_CONSTRUCTION | **硬保留** | 确定性优化；若无 eligible candidate，直接合法全现金，不需要额外 Agent |

### 2.2 Company required checks（当前 18 项）

| Check | 裁决 | 说明 / 修改方式 |
|---|---|---|
| TEAM_DAG_COMPLETE | **取消独立业务 gate** | 同上 |
| MACRO_REGIME | **共享** | 不为每家公司重复生成 |
| POLICY_REGIME | **条件化 / 与 SharedRegimePack 合并** | 政策敏感行业/重大政策事件 hard；一般公司作为 shared context/soft signal |
| INDUSTRY_PROFILE | **共享行业包** | 同行业候选复用 |
| COMPANY_ECONOMICS | **硬保留** | 与 deterministic company facts 合并生产 |
| FINANCIAL_INTEGRITY | **硬保留 + materiality-aware** | 见上 |
| DRIVER_TREE | **合并** | 不单独 blocking |
| FORECAST_BULL_BASE_BEAR | **硬保留** | deterministic scenario engine |
| GOVERNANCE_QUALITY | **关键否决 hard，其余 soft** | 实控人/资金占用/违规担保/重大调查等 critical veto 继续 hard；普通人员变动、一般治理评分不能因为资料不完整卡整轮 |
| VALUATION | **硬保留** | 核心 |
| QUANT_FACTOR | **条件/soft** | 不再普遍 hard |
| MARKET_PRICE_ANCHOR | **硬保留** | current recommendation 必须 |
| CATALYST_RISK | **增量化** | 复用已有 coverage，仅查询上次 cutoff 之后新事件 |
| BULL_CASE | **保留** | 并行结构化 |
| BEAR_CASE | **保留** | 并行结构化 |
| INDEPENDENT_REVIEW | **条件触发** | 只在冲突/边界场景启用 |
| MODEL_RISK_VALIDATION | **合并/条件化** | 标准 DCF/NAV/倍数法只做 deterministic sensitivity / assumption bounds；新 ML/自定义模型才启用独立 model-risk gate |
| COMMITTEE | **硬保留** | 规则优先、LLM 仅边界仲裁 |

---

## 3. Full Research Mandatory DAG（当前 20 节点）：逐项裁决

当前 `FullResearchInputReadinessReport` 之后又存在第二套 20-node Mandatory Research DAG。多数节点不是新增研究，而是对前面 capability 的再次投影。

| Mandatory node | 当前来源 | 裁决 |
|---|---|---|
| REQUEST_CONTRACT | FULL_MARKET / request | **并入请求冻结，不作为第二次门禁** |
| POINT_IN_TIME_SNAPSHOT | FULL_MARKET / sources | **硬保留，但消费 ArtifactAdmissionProof** |
| MACRO | FULL_MARKET | **引用 SharedRegimePack proof** |
| MARKET_REGIME | FULL_MARKET | **与 MACRO / liquidity 合成 SharedRegimePack，不重复研究** |
| INDUSTRY | INDUSTRY | **引用 IndustryContext proof** |
| UNIVERSE_SCREENING | FULL_MARKET | **按 claim scope 条件硬门** |
| COMPANY_FUNDAMENTAL | COMPANY_RESEARCH | **硬保留 proof** |
| FINANCIAL_QUALITY_AUDIT | FINANCIAL_INTEGRITY | **硬保留 proof** |
| VALUATION | FORECAST_VALUATION | **硬保留 proof** |
| NEWS_EVENT | EVENT_RESEARCH | **增量化 proof** |
| GOVERNANCE | GOVERNANCE | **critical-veto hard，其余 soft** |
| QUANT_FACTOR | PORTFOLIO | **条件/soft，移出 universal mandatory set** |
| BEAR_CASE_CHALLENGER | RED_TEAM | **与 conditional IndependentReview 合并** |
| CANDIDATE_RANKING | COMMITTEE | **保留 deterministic projection，不应触发新 Agent** |
| PORTFOLIO_CONSTRUCTION | PORTFOLIO | **硬保留 deterministic** |
| EXECUTION_PLANNING | CURRENT_MARKET | **从普遍 hard 改为“有持仓建议且输出即时交易参数时才 hard”**；全现金/观察结论无需执行计划 |
| RISK_AUDIT | PORTFOLIO | **硬保留 deterministic**；空组合可直接 PASS |
| EVIDENCE_AUDIT | FULL_MARKET | **合并到 Artifact Admission，不再 final 递归重审全部对象** |
| PUBLICATION_GATE | FULL_MARKET | **硬保留，作为最终唯一发布权威** |
| TRACKING_REEVALUATION | SUBJECT_REGISTRY | **移出发布 mandatory DAG**；推荐发布后异步/尽力 enrollment，监控注册失败不能推翻已经合法的研究结论 |

**核心建议**：Mandatory DAG 保留为一张“最终审计视图”，但它不再运行第二遍语义/lineage 检查，只读取前面已经冻结的 proof。这样仍然有完整 20-node 可审计视图，但 hot path 不再重复做 20 次实际验证。

---

## 4. 结构性门禁：保留规则，但改为“一次验证、多次引用”

### 4.1 ResearchRoleOutput / ResearchRoleResult

当前会验证：

- output_contract 与 task 对齐；
- readiness_check_results 完整；
- canonical completed checkpoint；
- evidence union；
- member artifact input-hash binding；
- dependency current hashes；
- Bull / Bear independent_context_id。

这些**正确性要求都应该保留**。问题在于下游 `RegisteredOutputVerifier`、`DomainContractAudit` 和 final receipt 又会继续递归打开对象做相近验证。

改法：

`register_role_result()` 成功时生成 `RoleAdmissionProof`：

```text
RoleAdmissionProof
- plan_id / task_id
- output_artifact_id / object_hash
- verified_member_hashes
- verified_evidence_ids
- dependency_result_hashes
- pit_cutoff
- contract_version
- independence_result (when applicable)
- proof_hash
```

后续 capability / readiness / publication 只验 proof_hash 与目标 cutoff/policy 是否兼容，不再递归重读整棵 dependency graph。

### 4.2 RegisteredOutputVerifier / DomainContractAudit

当前每次 capability admission 会再次检查：type、schema、decision freeze binding、time、linked hashes、request/account/entity、completion status、broker flag，再进入 domain-specific status/evidence 检查。

这些规则仍保留，但改成 `ArtifactAdmissionService.verify_once(artifact_id, cutoff, policy_hash)`，结果缓存键：

`(artifact_id, object_hash, cutoff_bucket, policy_hash)`。

对象内容是不可变的，只要 hash、policy 与 cutoff 兼容，同一请求内第二次检查无需重新解析 JSON、递归 `ObjectStore.get_bytes()`、重复 Evidence lookup。

### 4.3 Publication / receipt replay

当前 seal 时立即 `verify_receipt()`，注册后 `DomainContractAudit` 还会 `verify_receipt()`，后续 load/replay 又会验证。建议：

- seal 前做一次完整 replay；
- 成功后注册 `ReceiptVerificationProof(receipt_hash, policy_hash, status=PASS)`；
- 同进程/同版本发布只验证 proof；
- **离线审计、跨版本读取、用户显式 replay** 仍跑完整 `verify_receipt()`。

不会削弱审计能力，只是把“审计可重放”与“每次请求都重放”分开。

---

## 5. 目前最明显的冗余项

### 5.1 双层 Research Team：Full-market + Company plan 重叠

当前 Full-market plan 已有：fundamental / financial / catalyst / market / valuation / quant / bull / bear / reviewer / committee；Company plan 又有 macro / policy / industry / governance / financial / fundamental / catalyst / market / valuation / quant / bull / bear / reviewer / model-risk / committee。

建议采用：

- **一个 RequestGraph**；
- shared nodes：macro/policy/liquidity/universe/industry；
- per-company nodes：financial/fundamental/governance/event/valuation；
- per-company parallel views：bull/bear；
- conditional reviewer；
- one committee + one portfolio。

不再先跑一套 full-market team，再为候选各建一棵完整 company team 后再把结果包装回 full-market team。

### 5.2 Sector comparison / Industry profile 重叠

合并为 `IndustryContextPack`：

- broad sector score/rank；
- value-chain / cycle / peer set；
- industry methodology；
- candidate-company applicability。

同一行业多家公司共享；只有 company-specific differentiation 由 company agent 补充。

### 5.3 Company economics / driver tree / forecast 输入清洗重叠

合并为 deterministic `CompanyModelInputPack`：

- standardized financial facts；
- unit-normalized history；
- industry-archetype KPIs；
- driver candidates；
- balance-sheet / share-count / corporate-action adjustments；
- evidence provenance。

LLM 只需要选择/解释“哪些 driver material”，不再从 PDF / provider row 自己拼数字。

### 5.4 Bull + Bear + Reviewer + Red Team + Model Risk 层级过厚

目标保留观点独立性，但减少固定调用：

- Bull 与 Bear 两个独立 structured calls，并行；
- deterministic conflict engine 检查假设差、估值差、事实冲突、critical veto；
- **只有触发阈值时**调用一个 `IndependentChallenge`；
- model-risk 对标准估值并入 sensitivity validator，对新模型才单独调用。

### 5.5 全量 news windows / categories 每次重扫

政策目前配置 7/30/90 天 + 大量事件类别。改成：

- 第一次公司正式研究：完整 baseline；
- 后续请求：`last_verified_cutoff -> now` 增量窗口；
- material event fingerprint 未变则直接复用 baseline；
- headline/news 可作线索，真正改变结论的事实只回到官方公告/监管/发行人来源。

### 5.6 投资预期历史扫描

当前 policy 仍有 `history_limit=16 / registry_scan_limit=1024`。应增加 canonical `LatestInvestmentExpectation` projection，更新时 O(1) 写入；每轮直接读 projection，不再以 artifact scan 作为常态。

---

## 6. 数据标准化：把“LLM 清洗数据”彻底赶出生产主链

用户提出的方向正确，而且项目已经有 `SchemaRepairPolicy` / `provider_dialects` / `financial_field_mappings` 的基础，但目前 schema repair 更像“Agent 提案 → 样本验证 → 人工 admission”的治理能力，还没有变成荐股 hot path 的**稳定 canonicalization API**。

建议新增以下确定性组件：

### 6.1 ProviderCanonicalizer

```python
normalize_provider_snapshot(
    provider_id,
    capability,
    raw_snapshot_id,
) -> CanonicalSnapshot
```

负责：

- 字段别名映射；
- 数值 / 日期 / 币种 / 单位统一；
- symbol/market identity；
- PE/PB/market cap/turnover 等 quote factor 映射；
- provider schema version；
- missing-field diagnostics。

任何 Provider 新字段变化先在 adapter 层解决，不允许上层 Agent 临时写 Python 去“猜字段”。

### 6.2 FinancialFactStandardizer

```python
build_financial_fact_pack(company_id, annual, interim, archetype)
```

输出固定 canonical metric keys，支持 industry adapters：

- `GENERAL_CORPORATE`
- `BANK`
- `INSURANCE`
- `SECURITIES`
- `RESOURCE_MINING`
- `REAL_ESTATE`
- 其他行业先 generic + methodology pack。

这样 000001 一类银行不会在入选后才发现“普通企业财务链不适用”。在 shortlist promotion 前就知道 adapter capability：**supported → deep research；unsupported → observation with explicit reason**，避免白跑。

### 6.3 ResearchArtifactFactory

```python
register_role_completion(plan_id, task_id, member_ids, evidence_ids)
```

统一完成：

- schema typed validation；
- evidence union；
- member hashes；
- dependency hashes；
- RoleOutput；
- RoleResult；
- checkpoint；
- AdmissionProof。

禁止实战时再出现 `build_xxx.py / repair_schema_chain.py` 一类临时脚本去手工对齐 schema。

### 6.4 CompatibilityAdapterRegistry

对**已存在的旧工件**做显式版本迁移：

`v1 artifact -> derived v2 artifact`，保留旧对象不可变并记录 `source_artifact_id`。

不允许 LLM 在运行中看到 ValidationError 后临时猜新字段。

### 6.5 GateCompiler

```python
compile_recommendation_readiness(request, admitted_artifacts, claim_scope)
```

一次输出：hard pass / conditional / soft warning / blocker，作为后面 Committee、Portfolio、Publication 的唯一 gate truth。

---

## 7. 缓存与增量研究：40 分钟 SLA 的最大杠杆

### 7.1 Material Delta Signature

每家公司维护：

```text
financial_revision
latest_interim_revision
governance_revision
official_event_revision
industry_revision
macro_regime_revision
price_bucket / valuation_anchor
corporate_action_revision
```

若财报、治理、重大公告、行业没有 material change：

- 复用 CompanyEconomics / FinancialIntegrity / Governance / Industry；
- 只刷新 current price；
- 重算 expected return / margin of safety；
- 增量查 event；
- 必要时重跑 Bull/Bear/Committee/Portfolio。

典型 follow-up 不应重新做一遍年报解析和所有角色研究。

### 7.2 Validation Proof Cache

immutable artifact + same policy hash 的验证结果可以复用。缓存的不是“结论”，而是：

- schema valid；
- object exists/hash valid；
- lineage valid；
- Evidence exists/verified；
- dependency witness valid。

只要依赖 hash 改变 proof 自动失效。

### 7.3 Shared Run Cache

一轮组合推荐中：

- macro/policy/liquidity 只生成一次；
- 同行业 profile 只生成一次；
- 同一 official document 只下载/解析一次；
- 同一 market snapshot 只取一次；
- Web official capture 按 URL + as_of + content hash 去重。

---

## 8. 40 分钟目标流水线

### 0～2 分钟：请求与用户态

- O(1) expectation projection；
- portfolio / monitor snapshot；
- request freeze；
- 解析 claim scope：`FULL_MARKET` / `BROAD_MARKET` / `NAMED_SECURITIES`。

### 2～7 分钟：市场与候选 cheap path

并行：

- instrument/universe；
- SharedRegimePack；
- live quote snapshot；
- candidate seed generation。

deterministic quick screen 80 seeds → 约 8～12 research candidates → **最终 deep shortlist 3～4 家**（不是 8～16 家）。

### 7～23 分钟：公司深研并行

3～4 家并行：

- canonical data + financial integrity；
- company economics / industry context reuse；
- deterministic forecast/valuation；
- governance critical screen；
- incremental event delta。

Agent 只做无法稳定编码的 qualitative synthesis。

### 23～30 分钟：独立观点与委员会

- Bull/Bear parallel；
- deterministic conflict check；
- 只有触发条件才调用 IndependentChallenge；
- rules-first Committee。

### 30～34 分钟：组合与风险

- deterministic ranking；
- portfolio construction；
- risk audit；
- all-cash 是合法结果。

### 34～37 分钟：单一 Recommendation Readiness

- 一次聚合 AdmissionProof；
- 不再逐层递归重审所有 artifact；
- 生成 receipt。

### 37～40 分钟：发布

- Publication Guard；
- 必要的最后官方 spot check（只针对本轮 material delta）；
- 一次中文自然语言 synthesis；
- deterministic privacy/no-internal-fields lint。

### deadline 规则

- T+25：停止新增低价值 specialist；
- T+32：停止广谱 Web 搜索，只允许 decision-critical 官方补证；
- T+36：不再启动新的 LLM Reviewer，除非其是唯一未闭合 hard gate；
- T+38：进入 finalization；
- T+40：若硬 gate 仍缺，输出“本轮不形成新的正式买入/组合结论 + 已完成的观察结论”，不得继续后台拖延。

---

## 9. 并发与重试策略

当前 LOW_RESOURCE 对 remote Agent workers 也按本地资源保守限制，这对 I/O-bound LLM 调用不一定合理。

建议拆开：

- `provider_io_workers`: 3～4，带 per-domain rate-limit；
- `llm_io_workers`: 4～6；
- `local_cpu_workers`: 2；
- `duckdb_threads`: 2～4；
- `max_parallel_deep_companies`: 3（默认）/4（高资源）。

Provider 不再“primary 失败 → retry → fallback1 → retry → fallback2”的纯串行链；采用 bounded hedge：

1. primary 先发；
2. 达到短延迟阈值仍无有效响应，启动一个已健康 fallback；
3. first valid wins；
4. 取消/忽略其余；
5. official source 只在 material fact 验真阶段使用，不与 quote provider 混为同一权威层。

---

## 10. 外部高认可度项目 / 官方 Agent 工程的可采纳模式

### FinRobot — ADAPT_PATTERN

官方项目：`https://github.com/AI4Finance-Foundation/FinRobot`

其 equity research pipeline 明确先：

1. fetch financial data；
2. process financial metrics；
3. generate forecasts / DCF / peer comparison；
4. 再运行 AI text / specialized agents；
5. 报告层加载标准分析输出。

**可采纳**：数据计算和估值先确定性标准化，再让 Agent 做 thesis/risk/synthesis。与本次“不要让 LLM 清洗数据”的要求完全一致。

### TradingAgents — ADAPT_PATTERN

官方/主项目：`https://github.com/TauricResearch/TradingAgents`

2026 changelog 的工程重点包括：

- verified data-access contract / provider registry；
- structured-output agents；
- checkpoint resume；
- graph-shape-aware checkpoint identity；
- configurable LLM retry budget；
- PIT / look-ahead fixes。

**可采纳**：统一 data contract、结构化输出、resume identity 和 bounded retry；不引入其整套 Agent swarm。

### Anthropic Agent engineering — ADAPT_PATTERN

官方：`https://www.anthropic.com/engineering/building-effective-agents`

核心原则：用最简单可组合模式；agentic system 会用 latency/cost 换性能；可拆分的独立子任务使用 parallelization。

**可采纳**：把可预测流程做 workflow，把真正需要开放推理的地方留给 Agent；并行 Bull/Bear、并行公司 research，而不是用 Agent 自由决定每一步数据加工。

### OpenAI Agents SDK Guardrails — ADAPT_PATTERN

官方：`https://openai.github.io/openai-agents-python/guardrails/`

Input guardrail 在首 Agent，Output guardrail 在最终 Agent；输入 guardrail 默认可与 Agent 并行，只有需要避免副作用/成本时 blocking。

**可采纳**：把强 guardrail 放在 workflow 边界；工具/工件级安全在对应函数边界验证，不要让每个 Agent 再重复跑一套全局 guardrail。

### Microsoft Qlib / RD-Agent — ADAPT_PATTERN

官方：

- `https://github.com/microsoft/qlib`
- `https://github.com/microsoft/RD-Agent`

Qlib 强调 loose coupling，portfolio strategy 可作为独立模块；RD-Agent(Q) 把 data/factor/model R&D 作为可组合过程。

**可采纳**：数据、模型、策略、组合各自 typed/standalone；Agent 负责研究假设与迭代，不能成为数据清洗 glue code。

---

## 11. 推荐的工作包

### WP-SLA-01：端到端可观测性（P0）

新增每次荐股 request 的：

- total wall time；
- 每 stage wall time；
- provider calls / fallback / retries；
- LLM calls / tokens / model；
- artifact validation count / cache hit；
- ObjectStore bytes reread；
- Web/official fetch time；
- per-company deep research time；
- gate blocker dwell time。

当前 `agent-observability-report` / `research-efficiency-report` 不能代表 2026-09-17 这类真实 4h+ run，必须先修测量。

### WP-SLA-02：Canonical Data Pipeline（P0）

- ProviderCanonicalizer；
- FinancialFactStandardizer；
- industry/archetype adapters；
- schema drift golden snapshots + contract tests；
- quote factors / PE / PB / market cap 等统一 adapter。

### WP-SLA-03：ArtifactFactory + AdmissionProof（P0）

- 统一 RoleOutput/RoleResult/checkpoint；
- ArtifactAdmissionProof；
- validation cache；
- compatibility adapter registry。

### WP-SLA-04：Single Recommendation Readiness（P0）

- 合并 Team readiness + Mandatory DAG 的实际验证；
- Mandatory DAG 退化为 final audit projection；
- closure 使用 readiness outcome，不维护第二套“是否完成”判断；
- Publication Guard 保留唯一正式发布权。

### WP-SLA-05：Gate policy v4（P1）

按本文裁决：

- QUANT_FACTOR conditional；
- DRIVER_TREE merged；
- INDEPENDENT_REVIEW conditional；
- MODEL_RISK conditional；
- GOVERNANCE critical-veto hard / details soft；
- EXECUTION_PLANNING only-if-position；
- TRACKING_REEVALUATION post-publication；
- UNIVERSE_COVERAGE claim-scoped。

### WP-SLA-06：Deadline-aware planner（P1）

- hard deadline 2400s；
- stage budgets；
- 360～600s per-company recovery cap；
- provider hedge；
- stop-new-optional-work thresholds；
- terminal `NO_FORMAL_RECOMMENDATION_WITHIN_SLA`，不再拖过 40min。

### WP-SLA-07：Material Delta / Research Cache（P1）

- slow-changing research reuse；
- event delta；
- price/valuation cheap refresh；
- shared macro/industry pack。

### WP-SLA-08：Fan-out / model routing（P2）

- deep shortlist 默认 3；最多 4；
- quick model 做 extraction/classification；
- strongest model 只做 fundamental synthesis、Bull/Bear、borderline reviewer；
- rules-first committee。

---

## 12. 验收标准

不以“单次跑得快”验收，建议冻结 30 个真实任务：

- 10 个 named-company buy/hold/sell；
- 10 个 broad-market / portfolio；
- 5 个有重大事件 / 重组 / 治理红旗；
- 5 个 provider failure / schema drift / bank-specialized cases。

硬门：

- P50 ≤ 20min；P90 ≤ 35min；P95 ≤ 40min；
- 100% 请求在 40min 内进入合法终态；
- PIT leakage = 0；
- critical accounting/governance veto leak = 0；
- broker execution = 0；
- immutable lineage mismatch = 0；
- 与现行正式链对拍：对共同可判样本的最终 BUY/WATCH/REJECT / all-cash disposition 不得出现未解释的系统性漂移；
- 每个速度优化必须有 failure-injection：provider timeout、schema drift、artifact collision、stale quote、missing non-material field。

## 最终建议

**不要以“直接删除安全门”作为主要提速手段。** 真正该删的是“同一事实的重复门禁身份”，该放宽的是与投资结论不必然相关的 universal hard checks；该保留的是 PIT、来源/哈希、重大财务完整性、估值、critical governance、组合风险和最终发布权。

如果按优先级只做第一批，建议先落地：

1. Canonical Data Pipeline；
2. ArtifactFactory + AdmissionProof；
3. Single Recommendation Readiness；
4. 40min Deadline-aware planner；
5. Material Delta Cache。

这五项比单纯提高并发或换更快模型更有可能把当前“小时级”任务稳定压到“分钟级”，同时不牺牲系统最值得保留的审计与风险边界。
