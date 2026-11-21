# AStock 技术分析与研究质量增量层

> 状态：CURRENT（仅限本文件已标注的软件能力）
> 是否已实现：技术面计算、只读研究 CLI、实验计划/描述统计、争议复核输入、正式建议参考复盘、简报草稿均已存在；**真实策略收益生产者准入、自动 Agent 调度**。
> 对应工作包：WP-62、WP-63、WP-65、WP-66；未验收开发状态以 `planning/work_packages_v1.yaml` 与 `开发计划.md` 为准。
> 机器合同：`src/astock/schemas/technical.py`、`configs/technical_analysis.yaml`、`configs/strategy_validation.yaml`、`configs/research_quality.yaml`。
> 相关入口：`src/astock/research/continuation_cli.py` 内既有扩展注册。
> 验证入口：`tests/unit/test_technical_analysis.py`、`tests/integration/test_technical_registered.py`、`tests/unit/test_strategy_validation.py`、`tests/unit/test_selection_seal.py`、`tests/integration/test_strategy_validation_cli.py`、`tests/unit/test_debate_evaluation.py`、`tests/unit/test_outcome_feedback.py`、`tests/unit/test_session_brief.py`、`tests/integration/test_research_quality_cli.py`、`tests/unit/test_quality_review_regressions.py`。
> 更新日期：2026-11-13

## 软件分层与单一事实源

```text
已有 Canonical Market Reference/Parquet、SourceSnapshot、ObjectStore、StateStore
  ├── TECHNICAL (只读计算、既有发行版本定位与一次验证)
  ├── STRATEGY EXPERIMENT (冻结参数/窗口/训练选择的纯研究协调；未取得合法收益生产者时只能诊断)
  └── RESEARCH QUALITY (独立多空争议材料、已发布正式建议的行情参考复盘、DRAFT中文简报)
       → 仅由现有 ResearchTeam / Committee / RecommendationResearchReceipt /
         Publication Gate / ResponseGateway 接入正式投资请求与发布
```

这里没有第二套行情事实、交易账本、组合持仓或额外调度器。所有新功能默认研究用途。生成结果不能赋予任何原本不存在的正式荐股、仓位或交易权限。

### WP-62 技术面

`TechnicalAnalysisService` 对已收盘未复权且证券身份一致的 `DailyBarObservation` 做单次遍历。EMA/MACD、Wilder RSI/ATR、布林带、量价确认和前期高低点是确定性研究特征。日线读取走 `CanonicalRegimeReferenceViews` 的现有核验路径；Parquet 物理/语义完整性在既有边界验证，尾部数据只保留策略预算允许的数量。**读取父发布的核验与全记录解码仍有 O(N) 成本**，所以技术面使用配置 `max_release_rows: 4096` 在读取前拒绝过大父发布；该预算并未让核验本身成为 O(W)，后续只有验证后可随机访问的尾部读取方可改变此结论。不得绕过核验或重新抓取行情。原始行情必须具有来源和官方交易日日历语义；缺失时保留警告而不是伪装成有效突破。公司行动覆盖不明时只能显示警告，不能从未复权跳空推断可建仓。

CLI：`research-technical-schema`、`research-technical-analyze`。计算不联网、无 GPU、无机器学习依赖。现行 `MarketContextPack` 已具备**可选**注册技术快照 artifact/hash 对：在 `ResearchTeamService` 原有 `company-market-context` 注册边界一次性核对同证券、原日线发布 hash、时点与工件内容；`QuantFactorResearchPack` 若引用技术快照，沿用现有 source_artifact_ids/object_hashes 并校验因子证券与时点，仍由原有 `QUANT_FACTOR` Typed Gate 决定研究充分性。没有引用该快照的旧公司研究照常运行；此处不会自动采集或补造技术源，也不增加新的研究任务、估值因子打分或下单权限。自动 Agent 消费仍须在实际角色执行方提供同源注册引用。正式买卖意见仍需公司基本面、财务完整性、独立多空及唯一 Publication Gate。

### WP-63 策略实验的明确权限边界

`make_plan` 按有界的训练、purge/embargo、测试与 final holdout 建立不可变计划；`Coordinator` 要求在测试读取前封存全部候选选择。`RegisteredSelectionSeal`（`src/astock/experiments/selection_seal.py`）已实现跨会话原子封存：复用现有 ObjectStore 与 `StateStore.transaction()` / Artifact Registry，在同一 SQLite `BEGIN IMMEDIATE` 事务内唯一认领 `study_id+run_id` 及最终留出集，任何重复认领均拒绝且整体回滚；无需新表、独立事实源或额外服务。它只证明选择被封存，**不证明收益数据可信**。算法限制候选数、折次数、样本量、成本扰动、参数大小及 CPU 耗时；已注册证据按版本化 `max_cached_evidence=8` 做有界 LRU 解码复用（允许设为 0），防止长试验将全部证据留驻内存。复用 `shadow.statistics` 的回撤与统计，不建立第二个回测执行器。

**尚不可用：** 当前 `ReplayExecutionReport` 并不包含经可信来源绑定的收益序列，`RegisteredEvidenceEvaluator` 只允许 `DIAGNOSTIC`，故 `strategy-validation-plan`/ `strategy-validation-diagnose` 不能声称真正的样本外成交验证、可交易收益或生产资格。此功能要正式准入，须在原 Host / ModelRisk 执行边界提供可信收益生产者：绑定证券、窗口、执行日期、有效源、成交/费用和可复现输入，再经独立对照/前向样本门审查。缺桥接时保持 `RESEARCH_ONLY`；不能由包装 JSON 的 `approved=true` 越权。

### WP-65 争议与结果反馈

`compile_debate` 仅从已冻结的独立 Bull/Bear 和当前已注册证据形成有界质询材料；内容等价的主题去重，重大问题优先，超出预算的争议保留；最多一轮交叉回应，未回复的冲突不自动变为已解决。无争议时额外模型调用为零，且没有新增投委会或独立事实源。

`evaluate_outcome` 只评价正式已发布、非条件入场、方向符合研究口径的建议；使用决策完成**之后**的交易日开盘、独立基准及交易日历，未成熟或缺数据不伪造成功率。输出属于**未复权、未扣交易成本、未包含分红的建议参考价格表现**，绝非已成交盈亏；不修改真实或模拟持仓。公司行动覆盖若未知或窗口内有拆分、除息等价格不可比事件，仍可显示原始价格轨迹，但 `direction_correct=null`，状态为 `PRICE_ONLY_UNQUALIFIED`，不可进入后验正确率统计；只有有可信已核验的“无行动”覆盖才可给出方向标签。当前 CLI 已尝试复用既有官方公司行动 release，但仅接受本身标记 COMPLETE、覆盖整个评价期间且每条实际事件具备 TERMS_VERIFIED 的注册来源；`MarketReferenceService.sync_corporate_actions()` 当前永远发布 `complete=False`，因此现行来源**不能证明无公司行动**，常规输出只能作描述性参考。不能为了获得 MATURE 统计而把 PARTIAL 伪装成 COMPLETE；后续须在既有官方证据链实现真正完整覆盖，再启用合格方向评价。

CLI：`research-debate-evaluate`、`research-outcome-feedback`。争议任务的 Agent 自然语言执行仍依赖现有 ResearchTeam/Reviewer 的显式编排，CLI 不是自动模型循环。

### WP-66 投资者简报

`build_session_brief` 只投影经现有 Publication Gate 确认、账户/证券/时间边界匹配的正式研究结论。技术观察要求实际最后收盘交易日与官方日历一致；未核实的事件只统计线索，不复制外部文本形成新买卖指令。对前后条件/操作方向生成语义变化摘要，未发生实质变化时不重复制造新研究工作。

`research-session-brief` 保留严格的 `DRAFT` / `public_approved=false` 语义；新增按需 `research-session-brief-public` 入口只在读取同一注册 `RecommendationResearchReceipt` 并确认真正的 Publication Gate 准入、同证券/账户/时点和 draft 事实指纹后，经现有 `ResponseGateway.render` 的完整公开审计生成 `LOCAL_ONLY` 正式展示正文。任何缺收据、伪造已审核标志、错误证券或仅有草稿的请求一律不得展示投资结论。这个本地呈现不发送外部消息、不开新定时器、不改账本；现有 Scheduled/报告宿主可显式消费通过核验的展示正文，但本模块不谎称已有自动发送链。

## 资源、质量与回滚

- LOW_RESOURCE 默认保留最多512根日线；技术面数值计算 O(W) 时间、O(W) 空间；完整 canonical release 父发布的读取/验证按其实际记录数 O(N)，技术入口用同一配置中的 `max_release_rows=4096` 在读前限定 N（必要时拒绝输出，而不是降级绕过验证）。不并行下载或重跑未失败资料。
- 研究仅调用明确请求的模块。无争议不额外多轮LLM；研究结果成熟时才进行参考归因；不按每个K线tick启动 Agent。
- 新增计算的原始派生结果沿用现有 artifact registry，对同一来源一次核验，不复制 Parquet/SQLite。
- 每个工作包使用对应风险分级的专项、合同、录制集成、负向及独立 defect-first Review；在真实证据不足时不宣称端到端正式投研或完整回测 PASS。
- 各能力可独立关闭或撤回自身扩展点；既有 SourceRouter、ResearchTeam、Committee、MarketReference、PaperLedger 和 ResponseGateway 保留，回滚不删账本或正式证据。
