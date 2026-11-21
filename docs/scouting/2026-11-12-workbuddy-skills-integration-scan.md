# WorkBuddy 项目空间 21 个技能集成扫描（2026-11-12）

> 状态：CURRENT｜性质：外部能力侦察记录，**不直接取得生产资格**
> 依据：`docs/workflows/workflow-research-tech-scout.md`、`.agents/skills/research-tech-scout/SKILL.md`、`AGENTS.md` §门禁最小化 / §外部能力、`docs/architecture/external-dependency-resilience-v1.md`
> 结果落地：`wp61-web-discovery-lead`，架构见 `docs/architecture/web-discovery-lead-v1.md`

## 0. 基线与裁决口径

先盘本地再盘外部。基线事实：项目已有 20 个 Repo Skill、`configs/provider_registry.yaml` 20 个 provider（8 个 PRIMARY_OFFICIAL）、`SourceAccessRouter` / `SourcePolicyGate` / `ObjectStore` / `ExternalCapabilityService` 完整闭环、14 个外部能力全部 `default_stage: SHADOW`、SERENITY 方法论已通过 `configs/research_skills.yaml` 与 `third_party/audits/serenity/` 正式集成。

裁决类别沿用 `research-tech-scout`：

- **ADAPT_PATTERN** — 只吸收真实缺口对应的最小确定性模式，本地实现，不引入依赖
- **REJECT_DUPLICATE** — 本地已有等价或更强保证（`workflow-research-tech-scout.md` Stop condition 1）
- **REJECT_GATE** — 账号/密钥/私有授权、来源不可审计、或违反唯一事实源/门禁规则
- **WATCH_ASSET** — 主体拒绝，但某个静态资产值得在对应 owner lane 交接后评估


外部能力默认 `SHADOW`；升 `PRODUCTION_BACKUP` 只认 canonical M-06 资格报告（endpoint-specific 数据权利、recorded + controlled-live、SBOM、安全、退出门），包能装、许可证、README、fixture、治理 Skill 都不构成准入证据。`docs/scouting/` 本身不授予生产资格。

**唯一真实缺口**：代码层没有统一 Web 检索抽象。`core/source_router.py` 只排序不采集，`core/source_policy_gate.py` 只做提案准入，`AccessTransport.SEARCH` 仅是评分枚举；权威 Web 检索、去重、交叉验证全部是 SKILL.md 自然语言，合同测试只做字符串断言。

## 1. 21 项逐条裁决

| # | 注册名 | 裁决 | 决定性理由 |
|---|---|---|---|
| 1 | `multi-search-engine` | **ADAPT_PATTERN** | 唯一对上真实缺口：纯标准库、零密钥、链接模式可离线、URL 规范化去重、`SEARCH` 结果仅作线索须回原文核实。与 `DISCOVERY_ONLY` 原则天然对齐（本实现只版本化 8 个可直连/常用引擎入口，不复制其 16 引擎全表与反爬策略） |
| 2 | `westockdata` | REJECT_GATE | 专属 APIKey 扫码（5 分钟窗口）+ Go CLI；厂商官方非交易所一手；SKILL.md 禁止同类能力替代，与多源交叉验证冲突；无 M-06 资格报告 |
| 3 | `ad-api` | REJECT_GATE | 营业部账密、私有 wheel、硬编码 IP、单点登录、500GB HDF5；违反「优先无需账号/密钥」与低成本 |
| 4 | `mx-finance-data` | REJECT_GATE | `EM_API_KEY` OAuth2；输出 xlsx/md 不含来源 URL 与取数时点，与可审计冲突 |
| 5 | `ifind-finance-data` | REJECT_GATE | iFinD 密钥 + 付费权益分级并发；自然语言解析零命中风险；无资格报告 |
| 6 | `market-query` | REJECT_GATE / WATCH_ASSET | `PINGAN_SKILL_APIKEY` + 日配额；行情能力已有 20 个 provider。内嵌 100+ 申万→券商板块编码表可 WATCH，但属专有编码，通用性不足 |
| 7 | `news-search` | REJECT_GATE | 共用券商 APIKey；`metadata.path` 是内部知识库路径而非公开 URL，无法达到可审计的正式来源资格 |
| 8 | `cn-financial-scraper` | REJECT_GATE / WATCH_ASSET | 脚本和反爬措施复杂，与唯一事实源冲突；其分析算法与现有 `shadow/statistics.py`、`financial_integrity/anomaly.py` 有等价能力，不另行引入 |
| 9 | `macro-monitor` | REJECT_DUPLICATE / WATCH_ASSET | 纯 prompt 工作流、无随包脚本；宏观 current 由 `OfficialMacroCaptureService` + `macro_current_releases_v1.yaml` 承担。`indicators.md` 阈值表若代码不消费会形成双轨配置，WATCH |
| 10 | `neodata-financial-search` | REJECT_GATE | 凭证绑定宿主 `connect_cloud_service` tempToken（项目不可控）；SKILL.md 强制「禁止混合多数据源、禁止其他 skill 获取同类数据」，实质自封事实源，与 `source-access-policy` 冲突 |
| 11 | `business-search` | REJECT_GATE | 依赖未随包的 `baidu-search`；且明文要求产出报告「不列出数据来源」，与可审计直接冲突 |
| 12 | `上市公司护城河分析` | REJECT_DUPLICATE | 纯 LLM 定性流程，依赖 neodata 与宿主 artifact 工具；`company-deep-research` + `governance-management-quality` + `financial-integrity-audit` 已覆盖 |
| 13 | `industry-chain-analysis` | REJECT_DUPLICATE | 纯 prompt 评分矩阵，依赖不在包内的 `wb-finance-skill` / `westock-tool`；`industry-value-chain` + `industry_methodologies.yaml` + 已集成 SERENITY 覆盖 |
| 14 | `serenity-skill` | REJECT_DUPLICATE | **已正式集成**：`configs/research_skills.yaml` 含 `source_family_limits: SERENITY: 2`、`third_party/audits/serenity/*`、`local-adaptation-release-v2.json`。重复引入会形成第二套事实源 |
| 15 | `stock-research-engine` | REJECT_DUPLICATE | 8 条铁律与 `investment-red-team` / `model-risk-backtest-validation` / 一级来源规则重复，且靠 LLM 自检无代码强制 |
| 16 | `a-stock-analysis` | REJECT_DUPLICATE | 四板块与 `company-deep-research` 重复；全靠网页抓取 + ECharts CDN；项目已有 `report_policy.yaml` 与 `report-visual-qa` |
| 17 | `a-share-daily-review` | REJECT_GATE | 明文要求「报告中绝不暴露具体数据源名称、抓取路径」，与可审计/唯一事实源**直接冲突**；且 `westock-tool` / `wb-finance-skill` 缺失 |
| 18 | `daily-financial-briefing` | REJECT_GATE / WATCH_ASSET | 依赖外部插件、网盘和固定目录；文本自检不能证明数据正式准入，部分披露核验规则需要先核对现有 `financial_integrity` 实现是否重复 |
| 19 | `Agentic Coding` | REJECT_DUPLICATE | 纯 prompt 编码纪律，非金融数据域；`AGENTS.md` §强制开发工作流已有更严格的 5 门与证据留痕 |
| 20 | `agent-memory` | REJECT_DUPLICATE | 包内 SDK / HTTP API / `memory_store.py` 均不存在（仅文档声明）；项目已有 ObjectStore + SQLite + append-only 事件 + provenance |
| 21 | `data-query` | REJECT_DUPLICATE | 项目已按 `AGENTS.md` §唯一事实源用 DuckDB 建 Parquet 视图，`q.py` 会形成第二套查询入口 |

统计：**1 ADAPT_PATTERN / 20 REJECT**，其中部分条目仅保留静态资产观察资格。

## 2. 采纳项的集成边界

`multi-search-engine` 只取**确定性模式**，不复制整包、不引入依赖：

- 引擎 URL 模板、跟踪参数、中转参数、预算与权重 → 版本化到 `configs/web_discovery_lead.yaml`
- 链接构造、URL 规范化、跨引擎按独立组去重、官方域最长后缀优先 → 本地纯确定性实现
- 官方域不复制，运行时读唯一 `configs/authority_domains.yaml`
- 每个候选 URL 映射为既有 `AgentSourceProposal` 并过既有 `SourcePolicyGate`
- 产出恒 `DISCOVERY_ONLY`，不抓取、不改 Router、不写账本、不升外部能力资格、不接自动路由

完整机器合同、命令、验收与回滚见 `docs/architecture/web-discovery-lead-v1.md`。

## 3. 未采纳项的可复用线索（不在本轮落地）

| 线索 | 归属 | 前置条件 |
|---|---|---|
| `market-query` 申万→板块编码表 | 行业方法论配置 | 专有编码，需评估通用性与版本化成本 |
| `macro-monitor/references/indicators.md` 阈值表 | 宏观配置 | 必须有代码消费，否则不新增配置 |

## 4. 停止条件

依 `workflow-research-tech-scout.md` §Stop conditions：本地已有等价或更强保证时停止（20 项 REJECT 的主因）；许可/数据权利/可复现性无法建立时停止（密钥与私有授权类）；不得创建平行状态存储、研究账本、Skill registry 或 portfolio engine。本轮未引入任何第三方运行时依赖，未修改现役 Skill / Workflow 文本，未越界写入其他活动工作包的 lane。
