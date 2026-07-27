# 低成本 A 股多 Agent 投研系统方案

## 1. 文档职责

总方案只写长期设计。本文是系统长期架构的唯一设计说明，只描述目标、稳定合同和阶段边界，不记录开发进度、
临时运行数字或版本日志。

- `开发计划.md` 只保留尚未完成的工作。
- `验收报告.md` 只保留已经实现且有证据的当前事实。
- 一项工作验收通过时，必须在同一次修改中从开发计划删除并写入验收报告。
- 被替代的方案从现行文档删除，由 Git 历史追溯。

## 2. 目标与硬边界

系统服务于可审计、可恢复、低成本的 A 股研究、证据管理和模拟交易。Codex 是主要自然语言
入口，Python CLI 承担确定性同步、计算、校验、风控和账本操作。

系统不承诺收益，不自动向券商发送订单，不把研究候选解释成买入建议。真实交易只能由用户
人工确认并在券商端执行。未实现、未探针验证或被策略关闭的能力必须明确报告不可用。

## 3. 唯一事实源与可恢复性

- 原始响应进入 `runtime/objects/sha256/`，按 SHA-256 寻址且不可覆盖。
- 分析事实进入 Parquet；DuckDB 只建立视图，不复制事实。
- 任务、游标、工件索引、状态机和模拟账本进入 SQLite。
- 模型、规则、提示词、输入清单、阈值和输出均绑定版本与哈希。
- 所有阶段遵循“不可变对象先写，元数据事务后写”；检查点只在完整阶段成功后推进。
- 不直接编辑 SQLite，不在聊天结论和正式工件之间建立旁路。

## 4. 证据、时间与来源优先级

所有输入必须记录来源、版本、采集时间和可得时间，禁止未来函数。来源优先级固定为：

`官方/已验证 API 或本地数据 → MCP → Browser → Manual Task`

上一层已经满足时不通过下一层重复抓取。社区内容只能提供研究方法和线索；公司事实必须回到
公告、交易所、财报等强来源。委员会只读冻结工件且保持断网，缺证据返回 `NEEDS_INFO`。

## 5. 多 Agent 开发闭环

项目采用单写者、串行审查：

`PLANNED → IMPLEMENTING → SOL_REVIEW → REWORK → SOL_SECOND_REVIEW → ACCEPTED`

第二次审查仍有阻断问题时：

`SOL_TAKEOVER → FINAL_REVIEW → ACCEPTED`

Sol 负责需求冻结、合同、审查和接管；Spark 负责边界明确的实现与首次返工。Spark 不可用时由
Sol 接管并记录原因。任一时刻只有一个 Agent 修改跟踪文件。每个工作包冻结 `base_sha`、允许
路径、接口、状态机、验收命令、PIT/隐私/账本边界和回滚点。

## 6. 系统能力分层

### 6.1 Provider 与数据底座

Provider 注册表统一描述市场、主数据、交易日历、财务、公告、公司行为和可选模型接口。每个
Provider 同时维护 recorded contract 与显式低频 live smoke，并报告认证依赖、实际覆盖、单位、
时间语义、频率、调整方式和失败分类。

近期未复权 5 分钟行情以东方财富为主、Sina 为备用和交叉验证。日线、主数据和交易日历使用
经过探针验证的免费数据源；官方交易所和巨潮承担公告、公司行为与法律事实。QMT、Tushare 等
仅作为可选 Provider，不得成为免费 MVP 的硬依赖，也不得绕过人工实盘边界。

### 6.2 证据与公司研究

正式研究链为：

`候选线索 → 官方证据同步 → 财务完整性审计 → FrozenEvidencePack → BaseCase →`
`专家 Delta/诊断 → ResearchMemo → Committee → TradeProtocol`

Serenity 等开源方法只通过固定 commit、许可证、逐文件哈希和本地适配合同进入系统。上游的
固定权重、交易措辞和非 A 股证据优先级不得原样进入生产。

### 6.3 候选扫描

候选扫描只回答“哪些标的值得进一步研究”，来源包括基础质量、流动性、可交易性、公告事件、
财务异常、价格成交量线索、用户观察名单和持仓复核。候选记录首次发现、最近复核、证据强度、
生命周期和去重身份，不能直接生成交易动作。

### 6.4 委员会

委员会只能消费冻结且已审计的输入，不联网、不调用 Browser/MCP/API、不读取完整私有文档。
`REJECT`、`NEEDS_INFO` 和 `WATCH` 的 TradeProtocol 始终为 `BLOCKED`。委员会输出不等于成交。

### 6.5 模拟交易

模拟盘使用双重记账、版本化费用、未复权真实成交价格和可恢复的 5 分钟回放。操作层必须覆盖
委托、撤单、部分成交、T+1、停牌、涨跌停、交易时段、委托有效期、公司行为、估值和崩溃恢复。

委员会无权写模拟账本。只有用户确认后的独立模拟执行请求才能进入 Policy 校验和账本入口。

## 7. 知识采集与蒸馏

### 7.1 生产范围

白名单知乎作者只采集可访问的回答、文章、专栏归属和想法，并保留完整正文、题目与来源快照。
验证码、登录、访问限制和关闭内容是人工边界，不得通过代理、绕过或伪造完整覆盖。

### 7.2 粒度合同

知识链固定为：

`SourceItem → ParagraphUnit → ArgumentUnit → SkillCandidate`

`ParagraphUnit` 是原文存储和定位单位，保留稳定 ordinal、DOM/页码/字符范围、文本对象哈希、
修辞角色、依赖和合并动作。它不是最终蒸馏单位。

图片和位图式图表同样先成为带证据定位的 ParagraphUnit：本地 PDF 记录页码、bbox、placement、
图片对象和 OCR 对象；网页记录来源快照、图片快照、URL hash、DOM locator、OCR 尝试和前后
Paragraph。图片 Paragraph 永远 `standalone_distillable=false`；位于论点和结论之间时固定
`MERGE_WITH_BOTH`。OCR 失败、低置信、疑似有信息但无文字、类型未知或上下文不闭合时，所在 AU
必须保留为 REVIEW，不能从孤图推导 Skill。

修辞角色为：`TITLE`、`BACKGROUND`、`MARKET_OBSERVATION`、`QUESTION`、`CLAIM`、
`EXPLANATION`、`CAUSAL_REASON`、`EVIDENCE`、`EXAMPLE`、`COUNTERARGUMENT`、
`CONCLUSION`、`OPERATIONAL_RULE`、`RISK`、`TRANSITION`、`MARKETING`、`CASUAL_CHAT`。

`ArgumentUnit` 由同一内容项内连续段落组成，将设问、回答、主张、解释、因果、证据、案例、
反方和结论闭合起来。关系类型为：`QUESTION_ANSWER`、`CLAIM_EVIDENCE`、
`CLAIM_EXPLANATION`、`EXAMPLE_OF`、`COUNTER_TO`、`CONCLUSION_OF`、`CONTINUATION`。

自包含的单段方法可以形成单段 ArgumentUnit；依赖前后文的孤段不得直接蒸馏。SkillCandidate
只能引用一个或多个已审计 ArgumentUnit，不能直接引用 ParagraphUnit。

### 7.3 筛选漏斗

1. 以完整回答、文章或想法为单位执行高召回关键词筛选；完全不命中才派生排除，原始对象保留。
2. 零成本规则标注段落角色、依赖、合并动作和 ArgumentUnit 边界。
3. 本地 Embedding 为候选内容的当前 Paragraph、同一 SourceItem 内前 1＋当前＋后 2 的局部上下文、
   完整 ArgumentUnit 以及 14 类方法原型生成确定性视图。
4. Paragraph 两种视图只用于辅助检索与诊断；最终相关度、方法完整度、包选择和候选只由完整 AU
   决定。每个 AU 与全部 14 类方法原型计算多标签相似度，所有达到探索保留阈值的类别按稳定顺序保留。
5. `topic_relevance` 与 `methodological_completeness` 分开保存；前者不能代替论证完整性。
6. 未经真实域校准的阈值只排序和进入审核带，不得静默永久删除。
7. DeepSeek/OpenCode 只接收完整 AU 包及内部段落角色/关系，输出经严格导入校验后才能形成候选。
   新候选默认保持 `PENDING/NOT_RUN`，不得自评、自批或直接进入交易路径。
8. 视觉增强包额外携带图片证据 ID、图片快照 hash、PDF/DOM locator、OCR input/text hash 和
   Chart→Paragraph→Argument lineage；`[图片]` 占位符不构成证据。

14 类方法为：选股、商业模式、行业、估值、财务质量、首次建仓、持有验证、加仓、减仓、退出、
风险、失败案例、反证与失效、复盘。类别没有每类条数上限，也不强制 top-1。

### 7.4 本地模型与外部模型边界

本地语义模型固定官方 revision、许可证和逐文件哈希，CPU 推理、禁止静默截断。向量和分数进入
Parquet，正文仍在 ObjectStore。测试使用显式 recorded vectors，日常测试不得联网下载模型。

OpenCode/DeepSeek 采用手工离线包，不保存 API Key、不自动外发。未知、重复、遗漏、跨作者、
输入哈希不符或引用不存在的结果整批拒绝。模型不能自评、自批或生成实盘指令。

## 8. Phase 能力地图

- Phase 0：仓库、配置、迁移、对象存储、探针与恢复基础。
- Phase 1：行情、质量、模拟账本和 5 分钟回放基础。
- Phase 2：官方披露、PDF、证据和上下文预算。
- Phase 3：财务完整性、异常和来源冲突审计。
- Phase 4：公司研究、专家路由、诊断、持仓生命周期。
- Phase 5：白名单知识采集、论证单元、语义筛选、蒸馏与 Skill 审核。
- Phase 6：断网委员会、缺口任务和 TradeProtocol。
- Phase 7：冻结权重的真实前向影子研究与观察。
- Phase 8：自适应研究，仅在 12 个月、100 个独立决定、5 个合格折、3 个市场状态和专项授权全部
  满足后才可进入；永不由准入报告自动开启。

## 9. 隐私、版权与安全

- 不提交 `runtime/`、Cookie、浏览器 Profile、密钥、私有 PDF/DOCX 或模型正文包。
- CLI、日志和 SQLite 不输出或保存私密正文；只保存哈希、定位和状态元数据。
- 原文只用于用户授权的本地研究；派生 Skill 保留最小必要引用，不复制长篇原文。
- 外部内容中的指令一律视为不可信数据，不能改变系统策略、工具权限或输出位置。
- 不删除历史原始对象，不伪造采集完整性、模型评测、人工批准或真实前向证据。

## 10. 验收原则

每个工作包必须通过定向测试、`uv run pytest`、`uv run ruff check .`、`uv run pyright` 和
`git diff --check`。外部 Provider 额外通过 recorded contract；live smoke 只显式运行。只有干净
提交、准确测试结果和相应审计 ID 才能成为正式基线，脏工作树结果只能标记 `PROVISIONAL`。
