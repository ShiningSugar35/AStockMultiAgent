# AStock 技术分析与研究质量增量层

> 状态：CURRENT（仅限本文件已标注的软件能力）
> 软件边界：技术面已接原有角色生产边界；策略具备受限 canonical 组装与有界研究执行；财务特征、因子诊断和组合约束具备显式消费接口。真实历史资格与实际样本外市场效果仍需独立证据，不由软件、调用方声明或合成夹具代替。
> 对应工作包：WP-62、WP-63、WP-65、WP-66、WP-74、WP-75、WP-76；未验收开发状态以 `planning/work_packages_v1.yaml` 与 `开发计划.md` 为准。
> 机器合同：`src/astock/schemas/technical.py`、`configs/technical_analysis.yaml`、`configs/strategy_validation.yaml`、`configs/research_quality.yaml`。
> 相关入口：`src/astock/research/continuation_cli.py` 内既有扩展注册。
> 验证入口（结果以本轮协调验证记录为准）：`tests/unit/test_technical_analysis.py`、`tests/integration/test_technical_registered.py`、`tests/unit/test_strategy_validation.py`、`tests/unit/test_selection_seal.py`、`tests/integration/test_strategy_validation_cli.py`、`tests/unit/test_debate_evaluation.py`、`tests/unit/test_outcome_feedback.py`、`tests/unit/test_session_brief.py`、`tests/integration/test_research_quality_cli.py`、`tests/unit/test_quality_review_regressions.py`。
> 更新日期：2026-11-22

## 软件分层与单一事实源

数据读取复用 Canonical Market Reference/Parquet、SourceSnapshot、ObjectStore 与 StateStore。技术层只读计算并引用现有发布；实验层冻结参数、窗口与训练/选择状态；研究质量层处理独立多空争议、参考复盘和简报。正式请求仍通过原 ResearchTeam、Committee、RecommendationResearchReceipt、Publication Gate 与 ResponseGateway。


这里没有第二套行情事实、交易账本、组合持仓或额外调度器。所有新功能默认研究用途。生成结果不能赋予任何原本不存在的正式荐股、仓位或交易权限。

### WP-62 技术面

`TechnicalAnalysisService` 对已收盘未复权且证券身份一致的 `DailyBarObservation` 做单次遍历。EMA/MACD、Wilder RSI/ATR、布林带、量价确认和前期高低点是确定性研究特征。日线读取走 `CanonicalRegimeReferenceViews` 的现有核验路径；Parquet 物理/语义完整性在既有边界验证，尾部数据只保留策略预算允许的数量。**读取父发布的核验与全记录解码仍有 O(N) 成本**，所以技术面使用配置 `max_release_rows: 4096` 在读取前拒绝过大父发布；该预算并未让核验本身成为 O(W)，后续只有验证后可随机访问的尾部读取方可改变此结论。不得绕过核验或重新抓取行情。原始行情必须具有来源和官方交易日日历语义；缺失时保留警告而不是伪装成有效突破。公司行动覆盖不明时只能显示警告，不能从未复权跳空推断可建仓。

CLI：`research-technical-schema`、`research-technical-analyze`。计算不联网、无 GPU、无机器学习依赖。`ResearchTeamService.register_role_output` 的 `company-market-context` 边界现在调用 `attach_market_context`：日线发布、证券、最新收盘和时点全部一致时，注册可复用的技术快照并派生新的 `MarketContextPack`，不改写旧对象。缺少可计算的发布时保留原上下文和明确观察项；已有技术引用继续严格验证，错误证券或父发布不被降级放行。`QUANT_FACTOR` 仍通过原 Typed Gate，技术观察不能越过财务、独立多空和 Publication Gate 获得正式买卖权限。

### WP-63 策略实验与统计资格

`make_plan` 冻结训练、purge/embargo、测试及 final holdout；可显式提供每个样本的标签结束时点，训练标签跨入测试、测试标签跨入最终留出时必须剔除或拒绝。未提供标签区间时只按既有固定预测跨度处理，不宣称已经覆盖任意事件标签。候选在测试读取前封存。原 ObjectStore / Artifact Registry / SQLite 事务承担唯一认领，不增加新表或服务。

`RegisteredSelectionSeal` v2 除 study/run 与原留出身份外，还认领同一 A 股日频研究族的每个上海交易日期。因此修改 run 名称、基准、数据 release 或留出子集，都不能重复消费已使用的日期。历史 v1 登记不含可恢复日期；存在这类登记时要求可核实迁移，不能清空旧登记或假装它们没有消费过留出集。

#### 从已有 canonical 发布到受限研究执行

`strategy-market-assemble` 读取 `MarketAssemblyRequest`，消费原生 instrument master、官方交易日日历、未复权股票与独立指数日线、逐日交易分类、有效规则与费用来源。注册开关仅写现有 `StrategyCanonicalMarketRelease` 工件。它复用原 `FrozenMarketRelease` 和 restricted daily-close evaluator，没有新建行情 store、账本或通用回测引擎。

首个软件切片只接纳**普通、固定涨跌幅、非停牌、逐日来源可见，且具有完整无公司行动证据**的股票。板块代码、有效规则、已度过 IPO 不设涨跌幅阶段的交易日下界、前收盘连续性、精确价格上下限和逐日费用适用性都必须匹配。指数点位允许非分币精度；股票仍遵循现有交易金额精度。

公司行动的原 HTTP 响应不能证明实际查询窗。CNINFO 原搜索边界现在把真实 `DisclosureSearchBatch.request` 与对应 raw snapshot 冻结到既有 artifact registry；baseline 增加可选 query receipt 引用，旧对象继续兼容。组装器同时核验请求证券、交易所、ALL 分类、页码、日期、终页与 raw 总数，不能相信调用方的 `complete=true`。

组装器优先消费请求显式绑定的既有 COMPLETE 公司行动父发布：沿 `MarketReferencePaperVerifier` 验证券、可见 head、完整请求窗、原始快照和官方 TERMS_VERIFIED 事件。当前切片仅接纳评价窗内无事件的完整发布；原合同要求非空记录，窗口外历史事件可以保留，不要求公司从未披露。

缺少上述完整发布时，后备证明要求从上市日开始至当日的实际**空披露枚举**，不能从短窗零命中推导无行动。现行 `sync_corporate_actions` 仍是 PARTIAL，本轮没有升级它。窗口内公司行动、缺少历史来源或未知停牌/限制条件返回具体 `MISSING_PROOF`；分红支付、除权及复杂行动执行不在该受限切片内。原生发布录制夹具覆盖两条可走通路径以及 PARTIAL、raw 终页冲突、未来捕获等负例，不证明真实股票已经取得历史资格。

`strategy-validation-plan` / `strategy-validation-diagnose` 的外部 Replay 证据仍然只能诊断。`CanonicalDailyCloseEvaluator` 只能在上述冻结来源与已有执行约束下输出受限研究结果；来源资格、算术正确、样本外金融有效性是不同结论。当前捕获的 Tushare 数据不能回填为过去可见数据，`RESEARCH_ONLY` 不因包装 JSON、选择封存或合成通过而变成生产策略许可。

#### 全部实验尝试与只读恢复

`strategy-validation-run` 在构造评价器前，把完整 Plan、预声明假设、全部候选/窗口和未尝试项写入现有 Artifact Registry；每个实际查询先原子预约，再记录结果。负分、未选中、失败和未执行的候选均保留，不能只保存优胜结果。失败只保存阶段与异常类别，不记录可能含凭据的原始异常。假设未提供时明确 `UNSPECIFIED`；声明时间不构成从未接触历史数据的证明。

成功 run 的同身份重放只核验既有报告和全部尝试，不重新评价；并发同 run 只允许一个 owner。中断状态保持 `INCOMPLETE`。`--recover-recorded` 仅在全部所需查询均已成功冻结且原选择封存仍有效时，重新组合报告，不追加网络、评价或留出集消费；缺一个结果就不能假装恢复完成。原 SQLite/工件登记承担唯一状态，不增加实验数据库或调度器。

换手来自实际逐期成交额相对于期初 NAV 的比例，费用/滑点分别保留，不能将费用金额叫作换手。参数稳定性只比较同策略、同训练窗口中预先声明的相邻数值网格及容忍度；未声明网格或容忍度时为未知，不在样本外结果上重新选择“稳定”区间。验证入口为 `test_strategy_validation.py`、`test_selection_seal.py` 和真实 CLI 集成测试；通过数以最近验收回执为准。

#### 选择偏差的统计含义

`shadow.statistics` 的 DSR 使用所有候选的同频每观测 Sharpe 分布、显式有效独立试验数，以及收益偏度/峰度。缺少收益频率、候选 Sharpe 方差或独立性假设时保留空值/输入不足，不以候选数量虚构一个阈值；不把年化 Sharpe 直接代入每观测公式。

CSCV PBO 使用偶数个等长连续块的全部互补半块组合：训练半块选择 Sharpe 最优候选，测试半块以中秩计算该候选是否低于中位。平局中性，完全不可辨识返回空值；不丢弃尾部样本，不把旧 blocked-mean fold 代理指标叫作 CSCV。CSCV 是对给定候选矩阵的选择偏差诊断，不是按时间部署的 walk-forward。块数、候选数、组合数与运算预算都有限制。

`strategy-validation-cpcv` 直接调用 `experiments.cross_validation.build_grouped_cpcv_report`，消费明确冻结的候选响应矩阵。它按上海日期把全部证券放入同一组，以组内最早预测至最晚标签结束形成闭区间，进行 purge 与标签结束后的时间 embargo；每折只用训练日期选择候选，报告所有同分候选、被剔除的行及训练晚于测试的情况。重叠标签只取日截面均值再取均值，不复合成一条可交易净值。输入限10,000行、128个候选、256个组合，另有工作量、10秒CPU和2MiB输出限制。该入口没有模型拟合，来源、生产者防前视及历史资格仍是调用方声明；所有正式准入、账户写入和部署样本外权限均为false，不能代替顺序 walk-forward 与未触碰留出。

同一冻结输入已实际对照 purgedcv 1.0.0、ml4t-diagnostic 0.2.1 与 vectorbt 1.2.1。两种标签场景各6折：显式把闭区间终点转为半开终点后，purgedcv与本地CPCV全部一致；固定最大horizon的ml4t在变长标签中多剔除样本。本次数值对照显式使用MEAN作为选择/排名度量：无平局CSCV三者PBO均为2/3；对称平局例分别为本地中性2/3、purgedcv严格负logit的1/3、ml4t首次最大值/排序的1，差异保留。该对照不冒称默认Sharpe度量也已与外部实现逐值相等。DSR本地0.750449、purgedcv0.751257、ml4t统一矩条件后0.834369，审计具体记录自由度、矩估计与null-Sharpe方差/rescaling定义；不强凑同值。论文expected-max-Z（100试验）数值完全一致。vectorbt六组lookback×shares的合成零费用TRAIN逐日净值与实际canonical评价器完全相同；这不验证A股费用、停牌、涨跌停或T+1。最终维持本地可核验统计与执行权威，不引入重复runtime依赖。

研究依据：[Deflated Sharpe Ratio 原文](https://www.davidhbailey.com/dhbpapers/deflated-sharpe.pdf)、[Probability of Backtest Overfitting 原文](https://www.davidhbailey.com/dhbpapers/backtest-prob.pdf)。现有 LRU 证据缓存、工作量/CPU 上限及单一执行语义继续保留。

### WP-65 争议与结果反馈

`compile_debate` 只读已冻结的独立 Bull/Bear；`ResearchCasePosition` 区分事实、假设、计算与风格。只有影响决策的分歧产生质询，风格差异不追加模型调用。引用必须出现在原案例中；证据对象必须属于该案例的冻结输入，类型、证券和可见时间匹配。共享来源合并为同一个来源组，不能因多个角色复述而增加独立票数。材料身份包含完整问题、来源与策略，超出预算的争议保留为 deferred。

`research-team-reviewer-prepare` 在原 checkpoint 原子预约一次宿主执行，返回 input hash 与 operation ID；`research-team-reviewer-complete` 以相同身份登记有界回应。重试预约不会重复派发，完全相同的回传幂等，错身份或不同内容的重复回传拒绝；完成未知保持原预约，不能另开一次调用。回应交回原 Reviewer/CounterCase/Committee，不生成事实或发布权限。这是显式宿主适配合同，状态查询不派发模型、不占用执行预约；未观测的 Antigravity 原生执行不得写成已运行。

`evaluate_outcome` 评价正式已发布建议的后续价格参考表现；模拟策略、账户账本继续从各自既有事实读取。未触发条件、TRIM/EXIT 等不具备适用基准时保持 `NOT_EVALUABLE`，未到期为 `IMMATURE`。未复权、未扣成本、未含分红的轨迹不冒充实际成交净收益；公司行动未知时方向标签为空，状态为 `PRICE_ONLY_UNQUALIFIED`。现行 `sync_corporate_actions` 的 PARTIAL release 不能证明完整无行动覆盖，不能为了统计成熟率擅自升级。汇总按证券、入场日期和账户范围聚簇，去掉语义重复收据；同簇互相冲突的价格或方向直接报错，混合基准、期限或绩效类别不汇总。

`research-forecast-outcome` 读取冻结的 `ForecastPack` 与 `DriverTree`，再经 `read_certified_release` 验证官方 v2 财务 release：公司、年度、合并口径、原件、数值和可见时间一致后，按统一 CNY 计算收入、营业利润、净利润、毛利率或营业利润率的点误差及三情景范围覆盖。尚无成熟官方事实为 `IMMATURE`；事后形成的预测不能包装成事前预测。情景范围覆盖不等于校准概率，零样本不自动调权。

年度事件概率复盘复用同一预测血缘。`research-event-freeze` 用服务器执行时钟从已注册 `ForecastPack/DriverTree` 冻结年度财务阈值事件、概率、比较方向和到期口径；首个切片只支持该类型，不能把模型一句“看好”当事件。冻结必须早于对应财年结束，不能回填创建时间或在已结束财年上登记事前概率。相同身份保留第一次版本，重放可读取已失效父预测的原历史冻结；改变事件或概率必须使用新身份并重新检查当前父发布。

`research-event-outcomes` 只读已认证官方财务事实，核对公司、市场、年度、单位、合并口径、原件与可见时间；调用方不能提供“事件已发生”标签。缺成熟事实为 `IMMATURE`，不支持口径为 `NOT_EVALUABLE`，均不补零。报告按公司与期间聚簇、语义去重并保留最早有效冻结，排除同一经济事件最早冻结时点的冲突概率；同期间不同阈值事件可以分别为真或假。每个公司/期间簇合计权重为1，输出 Brier、可靠性分箱和分箱绝对校准误差。至少30个公司/期间簇仅是诊断充分性阈值，不假设簇间独立，也不证明概率已经校准，`calibrated_probability` 仍为 false，不自动改生产权重、推荐强度或组合权限。实际 CLI/官方 v2 财务录制链及负例见 `tests/integration/test_forecast_event_outcomes.py`。

`research-forecast-revision-impact` 只读输出受影响输入、驱动后代和指标；`research-forecast-invalidate` 记录有来源的前提失效并通过已有 input hashes 递归查后代。旧 forecast/valuation/decision 的当前入口停止复用，历史对象与审计仍保留；同一事务检查失效祖先并更新当前 head，防止并发写入重新激活旧依赖。未关联前提不触发整份重算。

CLI 继续复用 `research-debate-evaluate`、`research-outcome-feedback` 及上述增量入口，没有新调度器、绩效账本或自动生产权重。

### WP-66 投资者简报

`build_session_brief` 只投影账户、证券和时间一致的正式研究收据。原 scheduled research / continuous monitor 复用同一服务；技术面与事件从已注册快照按 as-of 读取，放入血缘与语义变化指纹，缺失或损坏的可选输入以 gap 保留，不使独立有效的简报失败。未核实事件只形成线索，不复制外部指令或改变买卖权限。没有实质变化时不重复制造研究任务。

`research-session-brief` 保持 `DRAFT/public_approved=false`。`research-session-brief-public` 验证 draft 全正文指纹、账户范围、同一注册 `RecommendationResearchReceipt` 和原 Publication Gate，再走 `ResponseGateway.render` 的事实/隐私审计生成 `LOCAL_ONLY` 正式正文。正文 hash 负责完整性，正式权限来自既有收据和发布合同，不把普通 hash 当签名或授权。

已有合法已完成 bar、盘前条件计划、盘中持仓/观察名单实质触发、盘后观点变化与到期复盘仍各走原路径；此层无新 timer、WebUI、消息传输或账本。语义事实绑定和中文表达见 `public-response-contract-v1.md`，未证明自动发送的环境不声称已发送。

### WP-74 透明财务特征与因子诊断

`python -m astock.candidates.discovery_cli features` 是只读消费入口。它通过 `FinancialSourceService.read_certified_release` 读取有限的当期、同期、前季和年度历史，复用已有官方 v2 CERTIFIED 来源核验。事实指向官方报表 snapshot，结构化 observations 指向供应商 snapshot；按科目、报表、期间与 duration 绑定合并口径，不能把这两类 snapshot 强行相等，也不能把母公司口径静默混入合并口径。

特征 manifest 输出公式版本、期间、单位、行业/可比组假设、状态、事实/证据 ID、依赖指纹和缺失原因。新增三年现金转化下界、使用期初期末平均总权益的三年总利润 ROE 波动范围，以及同周期经营现金流对已认证普通股现金股息的 TTM 覆盖；负权益、金融行业不适用、零分母或历史不足不会补成 0。可选 `--supplemental-file` 接入实际已注册的 daily_basic 估值、显式同行描述、同机构跨版本 EPS 修订/下一年增长及明确官方业绩预告方向；原始来源、两期可见性、冲突和样本构成均保留。详见 [来源消费合同](source-consumers-v1.md)，没有本地合格输入时保留未知，不在特征命令中联网补数。该命令使用只读状态，禁止迁移或登记衍生对象；每项 `independent_vote_weight=0`，用途仅为研究优先级，纯盲 tranche、深研席位预算和正式研究充分性不被改写。

`factor-freeze REQUEST.json` 在同一 discovery CLI 中调用真实 `build_registered_features`，以执行时钟冻结预先选定的因子、公式、总体、行业组、基准和持有期，登记 `DiscoveryFactorPanel`。它不接受调用方填入因子数值或回填 signal 时刻；同一 panel ID 重放保留首次版本，改变选择必须用新身份。原金融 release 的语义 hash 与 ObjectStore 字节 hash 分别核对，特征、原始对象和可选费用表都沿原工件登记保留血缘，无新事实表。

`factor-report REQUEST.json` 是只读生产消费入口：读取最多50个冻结 panel、10,000条成员，以共享 `ReferencePriceInputReader` 核验 canonical 股票/独立基准日线及官方公司行动完整覆盖。原 `research-outcome-feedback` 也复用该 reader 和 `evaluate_reference_price_path`；每份报告仅缓存8个已经完整核验的日线父发布，固定 stores/cutoff/预算，不建全局缓存。一个日频经济总体同日只能有一个 panel，避免多次冻结同一组证券虚增样本。缺 feature、未成熟、缺行情、公司行动未知/窗口内有行动分别保留，未合格价格不会进入 IC。

数值层复用 `factor_diagnostics.evaluate_factor_panel` 的同组 Spearman IC、保留同值的分位组、总 L1 换手和失败覆盖；报告中每个成员可回到冻结 panel 及后续价格/公司行动工件。未知成本保持净收益为空；显式提供预先冻结的 `ReplayFeeSchedule` 时，复用既有费用算术，按声明整手参考数量分别核算买入与卖出日费用，买入成本进入初始资金分母。它仍未模拟成交、滑点、分红或容量，不是账户净收益。行业组为声明分组，报告不授予历史PIT、独立样本、实验晋级或正式推荐资格；真实5/20/60交易日结果须自然成熟。

#### 技术补充与实际候选优先级

`SupplementalFeatureRequest.technical_daily_release_id` 只读消费同证券、同截止时点的已核验 canonical 日线，复用现有技术计算。`TECHNICAL_EMA_TREND` 为快慢 EMA 比值减1；`TECHNICAL_ATR_PRICE_RANGE` 为 Wilder ATR 与慢 EMA 的比值，用于价格振幅观察。后者不是年化收益波动率；未复权及公司行动资格警告继续保留。历史不足输出缺失，错误证券或未来数据拒绝，无自动采集或第二套指标库。

实际 `research-seeds --feature-overlay PATH` 读取有界配置，绑定冻结 panel、方向、假设、新鲜度与最多0.1的优先级增量（默认0.05）。消费服务重新核验 panel、真实原始字节及当前财务 head，只在已有非盲候选的同来源队列中以可比样本中秩调整优先级；不新增证券、不占用其他来源席位，不改变盲选 tranche、投票权重、深研席位或正式研究资格。少于2个有效样本、常数特征、零增量、过期或损坏的可选输入保留原排序及明确原因。

新鲜度同时核对冻结 signal、feature 截止时间和当前来源捕获时间，不能“今天包装旧输入”绕过；跨版本修订的历史基准不因年龄而被误删。技术指标额外按同一官方交易日日历检查最近行情，周末/休市不虚增交易日年龄；缺日历不接纳技术优先级。合法财务新版本使旧成员退出当前 overlay，按历史 as-of 审计仍可重放原结果。生成报告保留 panel/源 hash、基础 seed、可比成员、方向、分位和增量；实际 audit 重建并逐项核对排名算术与血缘，不能仅相信调用方的优先级数值。端到端入口与上述负例见 `test_feature_priority_overlay.py`、`test_registered_factor_diagnostics.py`。

### WP-76 组合上下文、约束与可选优化器

实际 `PortfolioResearchService` 已经经 `PortfolioAllocatorRegistry.allocate` 消费统一 `AllocationContext`。上下文固定证券顺序、对齐收益、收益口径、当前权重、买卖资格、重叠业务组上限、单票/现金/总 L1 换手和单边一次性成本。所有算法共用 `validate_allocation` 出口；已有等权、逆波动与层次风险基线兼容保留。当前层次方法准确标为平均连接树的递归分配，不能称为标准 HRP。

无成本、无既有持仓等简单情形沿用现有 score 约束；有锁定持仓、重叠组或成本时使用已有 SciPy 的有界线性投影。该投影并不最小化组合风险，状态明确为 `PROJECTED_HEURISTIC`。不可行、超时、不完整成本向量不能通过事后裁剪/归一化伪装解决。单边成本只从交易前 NAV 扣一次现金，不能重复扣到收益序列。返回目标暴露偏差、风险、费用、换手与约束状态；不写订单或持仓。

`PortfolioDecisionService.transition` 在原 no-trade band、整手数量和费用输入核验后，实际调用 `assess_transition_implementation → revalidate_implementation`。指定账户与 ACTUAL/PAPER lane 从现有 `CanonicalStateProjectionReader` 读取；持仓/NAV 漂移为 STALE_CONTEXT，可卖数量未知/不足、缺冻结约束或费用估计为 UNQUALIFIED，HOLD 保留权重导致组/现金/换手越界为 INFEASIBLE，不阻断原只读研究报告。

可行投影单列 RESEARCH_PROJECTED 权重、最小必要整手数量、一次性成本估计、风险与暴露偏差；原连续目标保留。重建账户/成本上下文后撤销旧 solver 最优性。往返基点费用折算为单边估计，不声称替代实际最低佣金、非对称税费或成交核算。所有状态都不授予执行或账本写入权限；ACTUAL reader 当前无可卖数量，涉及卖出时明确未资格。录制 reader 配合真实 transition 入口验证持仓漂移、T+1 不足和 HOLD 组上限等分支。上下文指纹同时覆盖实际算法读取的收益、协方差及相关矩阵。

**实际对照后的裁决：不接入 skfolio 生产优化器。** `scripts/compare_quant_research.py` 在唯一受控隔离环境安装 skfolio 1.14.4，对解析两资产及6资产正常、左尾、近共线、持仓/费用/重叠组限制场景，实际运行 HRP、MinVariance、CVaR 共15次 fit；连同现役4种基线共35组原权重都经过同一约束出口。4个无额外约束、相同暴露的最小方差目标最大差约6.83×10⁻¹³；当前透明实现已满足该数值范围。标准单连接HRP与本地平均连接递归不同，合成路径不证明谁更优。

CVaR 确实改善其自身合成经验尾损目标，保留这一结果；近共线场景的原 solver 状态是 `optimal_inaccurate`，警告和非最优性声明均保留。在受限场景，本地投影暴露0.737365与solver的0.8不同，不能把更低方差写成同预算胜出；CVaR原权重换手约0.3，通过0.5%数值权重网格后换手变为0.31，被现役共享出口判为 `INFEASIBLE`，原最优性随转换撤销。该网格是数值压力，不是真实账户整手/T+1执行。当前没有已合格真实输入与可行实施的增量证据，因此明确拒绝本轮生产接入，删除先前无生产消费者的 adapter 草稿及空注册支路。保留既有 allocator、统一上下文和实施后复核，不声称skfolio在真实OOS劣于本地。

唯一可复现结果见 [量化方法对照审计](../../third_party/audits/quant-method-comparison-v1.json)，含完整冻结输入、源文件/环境hash、全部尝试、solver状态/警告和实际运行凭据。最终完整比较35.64秒；外部进程峰值约202MiB（skfolio）、251MiB（统计）、320MiB（warm vectorbt），首次cold vectorbt约424MiB。线程/工作量/时间有界，RSS只是实测与复核阈值，未施加硬内存上限。隔离包与缓存结束后已删除，不增加主环境依赖或后台服务。

没有事件定义、校准概率/置信度或同源依赖边界的观点，不能启用 Black–Litterman / Entropy Pooling；本轮选择不引入观点引擎。Qlib、vectorbt、Riskfolio 等平行研究/回测框架没有进入 runtime；vectorbt仅完成上述实际隔离数值对照。当前边界继续复用现有来源、选择封存和执行器，不将未开展的Qlib/Riskfolio或观点实验写成比较通过。

未接入候选的接口研究依据：[skfolio MeanRisk](https://skfolio.org/generated/skfolio.optimization.MeanRisk.html)、[HRP](https://skfolio.org/generated/skfolio.optimization.HierarchicalRiskParity.html)、[EmpiricalPrior](https://skfolio.org/generated/skfolio.prior.EmpiricalPrior.html)、[LedoitWolf](https://skfolio.org/generated/skfolio.moments.LedoitWolf.html)、[Clarabel 资源设置](https://clarabel.org/stable/api_settings/)。

## 资源、质量与回滚

- LOW_RESOURCE 默认保留最多512根日线；技术面数值计算 O(W) 时间、O(W) 空间；完整 canonical release 父发布的读取/验证按其实际记录数 O(N)，技术入口用同一配置中的 `max_release_rows=4096` 在读前限定 N（必要时拒绝输出，而不是降级绕过验证）。不并行下载或重跑未失败资料。
- 研究仅调用明确请求的模块。无争议不额外多轮LLM；研究结果成熟时才进行参考归因；不按每个K线tick启动 Agent。
- 新增计算的原始派生结果沿用现有 artifact registry，对同一来源一次核验，不复制 Parquet/SQLite。
- 每个工作包使用对应风险分级的专项、合同、录制集成、负向及独立 defect-first Review；在真实证据不足时不宣称端到端正式投研或完整回测 PASS。
- 各能力可独立关闭或撤回自身扩展点；既有 SourceRouter、ResearchTeam、Committee、MarketReference、PaperLedger 和 ResponseGateway 保留，回滚不删账本或正式证据。
