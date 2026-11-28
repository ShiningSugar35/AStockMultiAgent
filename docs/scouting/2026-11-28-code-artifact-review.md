# code_artifact.md 需求与开发评审

> 状态：REVIEWED_PLAN_ONLY
> 日期：2026-11-28（Asia/Shanghai）
> 范围：审阅原稿第0～5节、WP-79～WP-87全部建议及验收要求；仅形成评审与计划。
> 开始时基线：main / e09328b；pyproject.toml声明版本0.4.1。
> 原稿SHA-256：24b96bd144a50d9430bd29260bcb8508cc067a7673328f1981a788c5aba234b1
> 执行计划以[开发计划](../../开发计划.md)和[机器索引](../../planning/work_packages_v1.yaml)为唯一入口。
> 原稿与《供外部架构师参考.md》是用户提供的未跟踪输入，保留原文；本文不赋予任何策略、来源、模型或交易生产资格。

## 一、总体裁决与证据边界

这份建议包含值得吸收的工程思想，但不能直接作为可编码规格书：部分“缺失能力”在现役代码中已经存在；部分论文只支持经济机制，未支持所给A股权重、阈值、收益和性能承诺；部分实现会形成第二套状态源，或把一个趋势策略的经验推广成全部投资研究的强制规则。

本轮保留七个经重写的工作包：WP-87、WP-82、WP-81、WP-84、WP-83、WP-79、WP-80。WP-85和WP-86保留为WATCH，不进入当前必须完成的开发清单；WP-85的红利可持续性定性检查吸收到WP-83。原WP-69、WP-78保持状态与原边界。

判决标签沿用research-tech-scout：ADAPT_PATTERN为适配现有能力的小范围工程改进；SHADOW_EXPERIMENT为有期限、有基线的诊断实验；WATCH为暂不开发、等待明确问题和输入；REJECT为拒绝原设计，不代表拒绝其中所有研究动机。没有实验收益时也必须能结束工作包，不能把外部来源资格或长期样本等待改成永久开发任务。

本轮核对了仓库规范、README、现役架构/配置/Schema/相关实现、原计划和最近验收，并对公开原始论文、官方文档、交易所规则和开源实现进行交叉核查。没有运行新策略回测、模型推理或真实数据采集，也没有测量新算法在目标机器上的性能。原稿称来自三份知乎资料，但未给出能重放所述阈值、收益比例和样本的完整出处；这部分只按研究假设处理。软件存在、配置启用、实测效果与正式准入分别说明，不相互替代。

## 二、项目现状校正

| 已核实事实 | 仓库依据 | 规划含义 |
|---|---|---|
| Agent负责研究判断，Python负责证据、计算、状态、账本；CURRENT与历史研究时间语义不同 | AGENTS.md第5～29行；README.md；docs/README.md | 不重建Agent运行时，不恢复CURRENT历史截断门 |
| 默认CHAT_ORCHESTRATED；DeepSeek现役适配器只提供Search/Fetch | configs/research_team.yaml；src/astock/providers/deepseek_tools.py；docs/architecture/full-market-research-team-v1.md | 原稿指定的investor_orchestration/llm_gateway.py不存在；缓存优化先面向宿主无关材料 |
| 市场状态已有概率、滞回、风险覆盖；HMM研究挑战者已有前向过滤且仍为SHADOW_ONLY | investor_orchestration/regime.py；configs/market_regime_v2.yaml；docs/architecture/market-regime-research-kernel-v1.md | 不能称“只有基础状态划分”，不另建Regime服务或Skill |
| Candidate层已有20日成交额中位数和非零占比；Seed粗排仍含快照成交额权重 | candidates/service.py第1180～1201行；candidates/seeds.py第2345～2397行 | 真正增量是Seed输入与跨层口径对齐，不是重建全部漏斗 |
| 已有pure blind、质量价值、行业广度、命题发现、EntryQuality | docs/architecture/skill-driven-candidate-discovery-v1.md；configs/entry_quality.yaml | 趋势/ETF路线只能作为受限研究通道 |
| 已有交易增量整手、目标带、现金/成本/T+1实施复核；股票规则仍共用统一lot配置 | portfolio/decision.py第1843～1916行；portfolio/implementation.py；configs/portfolio_decision.yaml | 先补不同板块最小申报量与递增单位，后比较MILP |
| 已有完整PIT血缘、时态依赖审计、试验登记、holdout封存、DSR/CSCV-PBO/CPCV | schemas/pit.py；pit/temporal.py；experiments/cross_validation.py；experiments/selection_seal.py；shadow/statistics.py；shadow/governance.py | WP-84是接线审计与补缺，不能另建统计与试验账本 |
| 已有raw-first、来源熔断、增量回执和next_watermark | core/source_resilience.py；investor_orchestration/models.py；scheduled.py | WP-87扩展数据集合同，不建平行sync_status真相源 |
| 已有宏观传导、行业定价权/周期方法、财务现金流和应收异常检查 | 现役Macro/Industry Skills；configs/industry_methodologies.yaml；configs/financial_rules.yaml | 补结构化证据与适用性，不再造政策/行业/财务引擎 |
| AKShare已登记为SHADOW，并对东方财富包装接口记录同源组 | configs/external_capabilities.yaml；configs/provider_registry.yaml；外部依赖架构第424～428行 | 免费安装不等于来源正式资格，也不等于独立第二源 |

以上为所列路径的阅读证据，不宣称对全仓所有运行入口完成了端到端测试。完整工作区保留原有未提交源码和.env.example删除状态；本轮只管理四个评审/计划文档路径。

## 三、全局约束与来源扩展评审（原稿0、1、5节）

| 编号／原建议 | 判决 | 理由及确定的改法 |
|---|---|---|
| G01：以v1.0.0作为代码基线 | REJECT版本假定 | 当前包元数据为0.4.1，开始HEAD为e09328b。用户说明稿的v1.0.0不能替代代码/发布事实，后续实施冻结真实commit、配置和输入。 |
| G02：i5-1135G7/MX450、16GB、全部线程≤2、单模块<2GB | ADAPT_PATTERN资源预算 | 本轮未探测硬件；CPU优先和有界内存可作目标，硬件型号不是已核实事实。沿现有LOW_RESOURCE、owned worker和BLAS预算；区分任务并发、网络并发、解析线程与ONNX内部线程，不统一硬改为2。2GB是待冻结的批任务预算，需测整个归属进程树，超过即分块/结束并给回执。 |
| G03：零新增成本、现有2120积分和Key | ADAPT_PATTERN费用边界 | 不新增付费订阅/来源、不擅自扩大预算；已有Key不代表请求免费。2120积分在既有验收中是用户报告，不能冒称本轮查实；限额按现役端点策略和真实错误处理，不回显凭据。 |
| G04：A股含ETF/可转债/GC001 | REJECT自动扩执行范围 | 现役系统无真实券商执行；ETF模拟执行默认关闭。可研究一个资产类别不等于已有交易规则/回放资格。可转债、逆回购不在本轮自动新增执行范围。真实账户事件与paper账本分别履行自己的语义，不要求每条外部成交先经paper回放。 |
| G05：新增AKShare所有列举端点 | WATCH来源；复用已登记能力 | 只针对实际缺口选择端点，审单位、时间、覆盖、上游权利和同源性。两融、期指基差、ETF份额、基金季报不是可互换数据；现有SHADOW资格不因本评审升级。来源资格缺口保持运行边界，不挂永久backlog。[S05] |
| G06：FRED DGS10/DGS3MO/THREEFYTP10 | ADAPT_PATTERN窄适配；效果待验证 | 按确切series注册单位、观察频率、发布日期和版本。API使用用户自己的Key；历史分析使用可证明的vintage/ALFRED语义，不能拿今天修订后的历史序列冒充当时可见。THREEFYTP10是Kim–Wright模型估计，与ACM分开。[S01][S02] |
| G07：NYFed ACM周频官方序列 | ADAPT_PATTERN并纠错 | ACM是模型估计，需保留模型/版本。区分日频/月末观测、发布更新频率和本地抓取频率；当前NYFed导航列Updated: Daily，不能沿用旧周度更新描述当观测频率。[S03] |
| G08：BGE本地模型为免费数据源 | WATCH | 模型是检索组件，不是经济事实来源。下载、许可、版本、量化方式和CPU预算均需独立登记；没有漏召回问题和标注基线前不新增依赖。[S23][S24] |
| G09：维持分层、SQLite/Parquet/CAS、不引外部数据库 | ADAPT_PATTERN，沿用现行不变量 | 不引入第二Router、Evidence、账本、状态源或常驻向量服务；配置字符串不能成为第二公式计算器。只把真实缺口登记为开发项。 |
| G10：每次修改都跑import-linter和全部pytest | REJECT机械全仓门 | 本轮L0只跑相关文档/计划合同和diff检查。后续按实际影响选L1～L3门；改变分层/依赖才跑相关架构验证。不能为每项模板改动重复执行全仓。 |
| G11：原稿三批落地顺序 | REJECT原顺序 | 把数据/语义、宿主上下文、交易单位正确性和现有验证链补全提前；新信号只在验证方法可用后做影子。WP-85/86观察项不阻塞本轮。 |
| G12：每包以新增某些文件视为完成 | REJECT文件数量验收 | 能在现有入口补足就不建新包。验收以可复现结果、负例、成本和权限不变为准，不能用“文件已创建”证明有效。 |

## 四、WP-79：宏观流动性与市场状态

**结论：保留ID，改为“既有市场状态内核的流动性特征影子评估”，P1。**

| 原条目 | 判决 | 需求与开发评审 |
|---|---|---|
| 79.1a：Amihud非流动性 | SHADOW_EXPERIMENT | 机制合理，但须定义研究收益与原始成交额口径，区分停牌/缺数/零额、稳健处理微小成交额与极值。不能先把异常数据当风险警报。 |
| 79.1b：20日流动性共震 | SHADOW_EXPERIMENT | 采用留一市场聚合或独立基准，防止个股包含在市场均值中的机械相关；检验短窗噪声、最小有效样本与缺失覆盖。 |
| 79.1c：两融和IF/IC/IM基差 | SHADOW_EXPERIMENT | 两融变动受价格与融资资格范围影响；基差需合约期限、换月和持有成本。原稿“贴水收缩度反向合成”符号不明确，先冻结指标定义，不由LLM临时猜方向。 |
| 79.1d：横截面负偏度 | SHADOW_EXPERIMENT | 偏度不等于整体下跌程度；分组规模、极端值和小盘权重会影响结果。与现有tail/breadth特征比较，避免同一风险重复计权。 |
| 79.1e：252日Z分、0.35/0.25/0.25/0.15、Sigmoid | SHADOW_EXPERIMENT | Brunnermeier–Pedersen支持流动性相互强化，未提供这套A股权重和阈值。固定权重先作为透明基线；标准化只用预测时已知数据，避免全样本拟合。0～100是分数，未校准前不是危机概率。[S04] |
| 79.1f：60/75/85三级动作、仓位减半和转入红利ETF/GC001 | REJECT生产规则 | 60～75区间、恢复条件和滞回缺失；75/85及减半无独立验证。初版只输出诊断，不能自动卖出、覆盖用户风险约束或启用其他工具执行。 |
| 79.2：成交额2/2.5万亿定市况 | REJECT永久硬门；改实验 | 跨时期市场规模不同。比较成交额历史分位、自由流通换手、大小盘结构和行业集中度；绝对金额可展示，不能单独推出“趋势主升”。 |
| 79.3a：Lasso/逻辑回归四象限概率 | ADAPT解释视图；SHADOW模型 | 已有概率状态与HMM。普通Lasso不保证概率单纯形；监督模型先定义标签、训练窗与校准。缺标签时用增长/通胀轴得分和不确定性，不伪造置信度。 |
| 79.3b：四象限驱动ERC大类配置 | WATCH配置用途 | 逆波动率一般不等于ERC；相关性和资产执行资格须另审。四象限不能替代现有信用/流动性/市场状态层，初版不直接配权。 |
| 79.4a：股息率减中美10Y利率 | ADAPT口径；SHADOW预测 | 本币对比和美元外部冲击分开，币种、期限、税与权益风险不相同。Greenwood–Vayanos研究美国国债供给/期限结构及收益，不能直接证明A股红利择时。FRED Kim–Wright与NYFed ACM分别登记，不能混称一个模型或两个独立观察事实。[S01][S03][S30] |
| 79.4b：LPR-Shibor及60日Oracle | SHADOW_EXPERIMENT | 先明确LPR与Shibor期限、月频阶梯、发布时点；异动只能是候选解释，尚无“高召回”证据，不直接降杠杆。[S29] |
| 79.A：2024年1～2月必触发≥75 | REJECT单窗口过关 | 该期可作已知案例，不作为调参后验收。预先冻结多年代时间窗、危机与平静期，比较召回、误报、领先时间、稳定性和成本；没有独立有效增量就保持SHADOW或结束实验。 |

**最小集成面：**既有RegimeFeatureBuilder/RegimeSeriesSpec、宏观来源模型与regime comparison；不新增research/regime平行服务、状态库或market-regime-control Skill。可用数据不足时明确NOT_EVALUABLE，不制造历史值。

## 五、WP-80：全市场候选发现

**结论：保留ID，改为“Seed持久流动性与策略分流对照”，P1。**

| 原条目 | 判决 | 需求与开发评审 |
|---|---|---|
| 80.1a：ST/B股/252日/零量洁净池 | ADAPT身份；REJECT全局排除 | 正式证券身份决定市场范围；零成交不等于停牌。ST/新股的研究、风险监控与买入资格分开。252日要求只用于需要该窗口的策略，不能阻断其他基本面研究。 |
| 80.1b：100亿市值、ADV20≥5000万 | ADAPT持久流动性；SHADOW阈值 | Candidate层已有20日中位数/非零占比，应复用。针对Seed快照排序比较ADV、中位数和截尾均值；市值/成交额底线须按容量、市场和策略冻结，不全局硬改。 |
| 80.2a：20只行业ETF、覆盖率77.5%/84.3%/86.1% | SHADOW_EXPERIMENT | 原稿缺ETF名单、日期、分母和去重口径。正式Universe为分母，ETF成分/行业映射按时点版本化；该覆盖率不写死为事实。 |
| 80.2b：每行业2～3龙头＋活跃领涨并集 | SHADOW_EXPERIMENT | 可能增强大盘与趋势偏差，不能声称必然消除哑铃。新增通道只使用有限研究预算，保留pure blind、质量价值、行业广度、命题发现。 |
| 80.3a：全局Close>MA200 | REJECT全局硬门 | 可作趋势策略条件，不能否决价值/事件/反转/持仓复核。复用EntryQuality，不新增有独立推荐权的Gate。 |
| 80.3b：12-1动量必须>0 | SHADOW趋势特征 | 跳过最近一月有成熟动量定义，但French样本及规则不能证明A股全策略适用。明确总收益/复权、交易日偏移与缺历史状态，不把特征缺失说成公司不合格。[S08] |
| 80.3c：MA200乖离≤30%、GRACEFUL_SKIP | ADAPT“不凑仓”；SHADOW30% | 保留无法满足约束时不强行填满组合的原则；30%及60日尾险恶化未附可复验结果，只列试验候选。 |
| 80.4a：禁用Calmar精排 | REJECT全系统禁令 | 排序目标与风格暴露应区分；Calmar与动量回答的问题不同。先测实际偏移，再限定具体策略用途，不凭推断删除通用分析。 |
| 80.4b：取消行业最多1只，35%单票/40%行业 | REJECT未经证实的缺陷和默认覆盖 | 未在所查现役路径证实通用“行业1只”规则。软惩罚与硬上限必须分开，用户约束和已有风险配置优先；35%/40%不是本轮批准值。 |
| 80.A：DuckDB全市场<2s、gate_audit | ADAPT审计；性能待测 | 使用现有分析数据层，不能为实现示例路径引入未有必要的依赖。区分首次获取、I/O、冷/热缓存和纯计算；输出候选原因和不可评价原因，勿用运行速度替代研究质量。 |

**最小集成面：**candidates/seeds.py、现有CandidateInputRelease与CandidateService、发现路由配置、EntryQuality。对相同Universe和研究预算比较命题召回、广度、重复率、正式研究转化与资源；未经研究准入的候选不能进入正式组合。

## 六、WP-81：交易单位与离散实施

**结论：保留ID，改为“证券交易单位正确性与有界离散实施优化”，P0先正确性。**

| 原条目 | 判决 | 需求与开发评审 |
|---|---|---|
| 81.1a：SciPy/HiGHS的L1 MILP | ADAPT_PATTERN | 适合整数线性目标，作为既有实施层的可选求解器。目标权重按总NAV还是扣现金预算必须统一；同时纳入现有持仓、冻结/占用资金、可卖数量、交易成本/最低佣金、行业/单票/换手和现金约束。[S06] |
| 81.1b：每手100股整数模型 | REJECT全市场假定；优先修正 | 当前_stock_rule确实读取统一lot。科创板最小200股且超过部分1股递增；北交所最小100股、可按1股变化。区分minimum_order_quantity、quantity_step、方向、订单类型、上限、零股和生效期；现有持仓不得强行变成整手。[S07] |
| 81.1c：1.5倍单手成本自动顺延 | REJECT硬门 | 1.5倍不是数学可行边界。由真实规则与约束求解；不可行则保留现金或选择已经通过正式研究的候选，不能自动给下一名荐股资格。 |
| 81.1d：100标的<20ms、扩二次目标 | REJECT承诺/错误求解器假定 | 实测冷暖P50/P95、内存、候选数及约束密度；记录optimal/feasible/time_limit/infeasible、gap和确定性重验。Clarabel不支持MIP，CVXPY的HiGHS整数能力不等于通用MIQP。第一版保持L1线性即可。[S06] |
| 81.2：20日逆波动、35%封顶、10%现金 | ADAPT现有分配器；SHADOW参数 | INVERSE_VOLATILITY已存在。比较波动窗口/方差下限和缺数处理；不覆盖用户风险上限与现有现金约束，不重复扣现金或为凑满配额放宽上限。 |
| 81.3a：买入Top N／持有Top M | SHADOW_EXPERIMENT | 是可研究的滞回机制；先与已有no-trade目标带比较。排名缓冲不能保护失去研究资格或出现重大风险的持仓。 |
| 81.3b：季末前7日、降低42%～54%换手 | REJECT经验当事实；后置实验 | 原稿未给样本、费用和对照。比较事件驱动＋目标带、固定月/季频和双线策略，不预写节省比例。 |
| 81.4a：季度内跌破MA200只冻结加仓 | REJECT全局政策 | 财务治理异常、投资逻辑失效、明确用户风控等仍触发及时复核。不能用“会反抽”覆盖现有退出理由，也不承诺卖出一定成交。 |
| 81.4b：年化波动>35%启用MA50止盈 | SHADOW_EXPERIMENT | 阈值、窗口及退出价需要选择过程和留出证据；研究价格与执行价格区分，避免分红引发假触发。 |
| 81.4c：ETF的MA200三日 | WATCH/SHADOW | ETF信号可只读研究；涉及可成交回放时才要求对应证券的有效执行规则，不打开默认关闭的模拟执行。指数与个股退出不同的动机可保留，参数不能直接成为硬规则。 |
| 81.A：新exit_fsm和单元测试即验收 | REJECT平行状态机；扩验收 | 沿PositionLifecycle/HoldingReview/TargetBand增量实现。先测主板、科创板、北交所、零股、T+1、涨跌停、停牌、费用、订单占用；MILP和现有投影对照只改变实现质量，不能改变研究批准集合。 |

**两阶段交付：**A阶段修正交易单位和实施一致性；B阶段在同一已准入候选与用户约束下比较现有投影和MILP。无可行解不产生非法订单，超时只可使用重验通过的可行候选；未改善实施误差、费用或稳定性时不切默认。退出Alpha实验不作为A/B关闭条件。

## 七、WP-82：Agent上下文、返修与结论核查

**结论：保留ID，改为“宿主无关的证据上下文与最小返修”，P0。**

| 原条目 | 判决 | 需求与开发评审 |
|---|---|---|
| 82.1a：所有Schema/CAS30-2026置顶冻结 | ADAPT稳定前缀；REJECT全量注入 | 仅装载当前角色适用的方法/Schema/准则版本。按发行人实际采用而非全局CAS30-2026；动态证据和时间放后部。DeepSeek缓存是best effort，前缀相同不保证立即命中。[S09] |
| 82.1b：会话模型模式不变，共享Bull/Bear缓存 | ADAPT可重复性；REJECT质量服从缓存 | 可以共享无立场的冻结证据，不能共享一方结论、合并独立上下文或为了缓存牺牲模型质量/可用性。缓存键包含宿主、模型、模式、方法版本和权限边界。 |
| 82.2a：恒等式/日期/合规下沉Hooks并修复 | ADAPT最小返修 | 这些约束已在确定性内核。归并重复验证，沿现有ResearchValidationCache绑定artifact/object/validator/policy/identity_scope/fact_state_hash缓存成功结果；自动修复仅限无损格式/展示精度，不能修改资产/负债/利润使等式通过，不能放宽容差。Hooks为适配，不是第二财务引擎。词表也不能代替现有事实/隐私/权限判断。 |
| 82.2b：20个Skill零重叠 | REJECT互斥；ADAPT主责清楚 | 多域请求本来需要协作。明确主角色、必要依赖、禁止越权；用单域/多域/增量任务验证覆盖，不以少调用Skill作为唯一成功标准。[S10] |
| 82.2c：Quiet Truncation、只传hash | REJECT静默与hash-only | 返回相关摘录/统计、证据定位/hash、覆盖与省略标记、按需展开入口。Hash证明身份但不能提供语义；长表尾部异常和反证不得静默丢失。原件仍由CAS保存。[S10] |
| 82.2d：/handoff包 | ADAPT复用checkpoint | 在原request/generation/lease/checkpoint上投影已完成节点、证据、未决分歧、预算、权限、未知状态和下一步，不建第二状态副本。WP-69的原生Stop未观测不阻塞纯软件接缝。 |
| 82.3a：Yes/No加权消除漂移 | ADAPT有锚点rubric；REJECT强制二值 | 使用YES/NO/UNKNOWN/NOT_APPLICABLE/CONFLICTED及证据/理由；必要程度维度用有锚点等级。确定性加权仅使结果可复算，不能自动证明预测效度，未知不等于零分，不授予Agent投票配权。 |
| 82.3b：轻量Prompt逐句对原文 | ADAPT已有发布链内核查 | 区分事实支持、数值复算、推断前提、预测条件与意见标识。合理推断不要求原文出现同一句话；核查器可能误判，结果只是原有Claim/Evidence/Publication链的评审输入。[S11] |
| 82.4：匿名化和封闭世界彻底防泄漏 | ADAPT防护；REJECT证明标准 | 引擎选择当时可得版本并限制评测工具，匿名化只作实名对照和污染探针，无法从技术上保证删除预训练记忆。模型视图可隐藏实体，核验层保留真实映射；前向冻结观察是更强证据。CURRENT继续最新可信事实规则。 |
| 82.A：新llm_gateway.py记录cache字段 | REJECT新网关；ADAPT现有观测 | 复用ResearchResourceUsage/AgentTaskObservation，实际宿主提供usage才记缓存token/费用，缺失为UNKNOWN，不以字节数估算。DeepSeek专属推理优化等未来确有受控推理路径再做。 |

**最小集成面：**EvidenceLocator/Claim模型、现有output_validation/validation_cache、host_continuation、任务材料和agent_observability。质量对照覆盖格式错误与真实事实错误、错证券/期间/单位/符号、不支持引用、合理推断、隐藏反证和中断续接。比较首轮有效率、关键风险召回、错误放行/误阻断、返修次数、端到端P50/P95及可观测真实usage；不能以压缩更多或token下降单独判定改善。

## 八、WP-83：政策兑现、行业传导与财务异常证据

**结论：保留ID，改为“政策兑现与行业传导证据增强”，P1。**

| 原条目 | 判决 | 需求与开发评审 |
|---|---|---|
| 83.1a：按部门判钱袋子/权袋子、HARD/SOFT | REJECT部门硬分类；ADAPT兑现过程 | 行政约束无新增财政支出也可能有效，资金宣布不代表已拨付。记录法律/行政效力、目标、预算来源与规模、执行主体、细则、拨付/项目/订单、失效条件，依证据判断阶段。 |
| 83.1b：签发/公开时滞与会议时间轴 | ADAPT_PATTERN | 扩现有PolicyRegimeProfile，区分签发、公开、生效和观察到执行；未知留空，时滞不自动代表利多强度。 |
| 83.2a：石油40/80/130价格机制 | ADAPT适用范围清楚的规则 | 原文确有分段；这是汽柴油定价机制，不能直接算炼厂利润或股价方向。核原料/产品结构、库存、采购/定价周期、税费、风险准备金及临时调控；阻止省略传导环节的断言，不硬编码90美元否决。[S12] |
| 83.2b：价格相对现金成本与出清 | ADAPT方法；SHADOW分类 | 有明确成本、利用率、资本开支和退出证据才有意义；行业亏损并不充分证明底部，现金成本必须同口径。 |
| 83.2c：CR5/壁垒导致弱周期化 | ADAPT结构证据；REJECT自动跃迁 | 保存市场边界、分母、时间及实际定价行为；集中度高不代表需求不周期，也不代表竞争有纪律。弱周期化保持可反驳假设。 |
| 83.2d：中游4～5倍/全链10倍牛鞭 | REJECT常数 | 无可复核样本。记录订单、终端销量、库存、交付期；仅对限定行业估计弹性区间并给不确定性。 |
| 83.2e：四阶段必经且复苏等于双击 | ADAPT描述；REJECT硬状态机 | 允许未知、混合、跳跃和子行业异步；复苏不保证估值倍数上涨。 |
| 83.3a：1～60天固定传导链 | REJECT默认常数；ADAPT证据区间 | 价格传导、订单交付、结算和会计确认不同。复用行业方法表，记录来源、样本、适用期、区间和置信度，不另建无证据供给链参数库。 |
| 83.3b：物理替代vs场景共存 | ADAPT扩展问题清单 | 加入性能、成本、成熟度、转换成本、产能、目标场景及速度；同一技术在不同场景可以同时替代与互补。 |
| 83.4a：剥离卖方评级/目标价 | ADAPT权威隔离；REJECT删除信息 | 评级不能替代独立结论，但预测/目标价/修订保留为“某机构某时点的观点”，供预期差和校准。不能破坏现有机构预测估值链。 |
| 83.4b：DSO跳升与CFO背离 | ADAPT增量 | 已有CFO/净利、应收/收入、Beneish。新增DSO用平均应收、对应收入/天数，考虑合同资产、票据、季节性、并购、合并口径与不适用行业；先解释异常，不凭新阈值授予财务否决。 |
| 83.4c：Veto Player卡点 | ADAPT依赖图 | 保存关键供应/渠道/审批节点、替代难度和证据，不另造同名否决权限；正式CriticalVeto由原合同管理。 |
| 83.A：新taxonomy/policy引擎和自动触发 | REJECT平行引擎；改质量验收 | 复用PolicyRegimeProfile、行业methodologies/archetypes、FinancialIntegrity和Macro/Industry Skills。检查混合政策、宣布未执行、无需财政但有效约束、未知时滞和业务异质性，不能以触发固定规则证明研究正确。 |

**吸收WP-85的低成本增量：**分红覆盖、经营现金流、资本开支、债务、一次性盈利、政策稳定性与指数换样等红利可持续性问题，进入现有财务/行业证据清单；不先建VAR择时。新CAS30特殊字段仍由WP-78负责，未实现字段不能以指标字典自授正式数值。

## 九、WP-84：量化验证链补缺

**结论：保留ID，改为“量化试验接线审计与验证口径补全”，P0先于新信号准入。**

| 原条目 | 判决 | 需求与开发评审 |
|---|---|---|
| 84.1：新增event_time/knowledge_time彻底防未来函数 | ADAPT调用审计；REJECT平行模型/充分性 | 现有PointInTimeMetadata已有period/published/effective/ingested/available/revised/supersedes。核消费者使用版本、横截面成分/退市、特征依赖和复权；CURRENT不恢复历史截断。 |
| 84.2a：全面改Triple Barrier | SHADOW可选标签；REJECT全面替换 | 标签与任务匹配。入场从下一实际可执行时点，处理T+1、停牌/涨跌停、跳空、双碰线先后未知、费用、可卖数量；触线不等于成交。保留固定期限净收益对照。[S15] |
| 84.2b：Meta-labeling决定仓位 | SHADOW_EXPERIMENT | 主策略输出必须OOF/前向，不能全样本生成后训练元模型。概率先校准和验证期望净效用，不能直接等于资金权重。 |
| 84.3a：浅GBDT/多窗口纯CPU毫秒级 | SHADOW，先简单基线 | 常数/规则/逻辑回归先行；深度2～3、窗口1/5/10/20/60为预登记候选，所有归一化/特征选择仅在训练折。CPU训练、推理和I/O分别实测，有增益才引依赖。[S16] |
| 84.3b：估值/流动性单调约束 | REJECT通用先验 | 高估值与低收益不是跨行业无条件单调关系。仅对明确适用域和可解释机制试验，并对比无约束模型。库支持功能不等于先验有效。[S16] |
| 84.4a：新experiment_trials表 | REJECT平行账本；ADAPT完整登记 | 复用shadow trial registry、TrialRecorder、不可变run/attempt和economic-family selection seal。失败/中止/放弃试验也保留；历史未知数量标未知，不补造完整N。 |
| 84.4b：DSR与sqrt(2lnN)、统一0.95门 | ADAPT适用性；REJECT简化与单门准入 | 已有DSR依赖试验Sharpe分布、有效独立试验数、偏峰度与共同频率。现役分别保留NOT_APPLICABLE与INSUFFICIENT_INPUT；评审汇总层若用NOT_EVALUABLE须说明映射与原因，不替换底层枚举；0.95不是充分生产证据。[S13] |
| 84.4c：强制每次CPCV、CPCV_PBO | REJECT混称；ADAPT分开报告 | 现有CPCV是固定策略响应的分组诊断，不能冒充训练内重拟合或部署OOS。PBO原始对应CSCV；保留各自诊断、purging/embargo、walk-forward与未触碰holdout，禁止重建dsr_cpcv.py。[S14][S15] |
| 84.5a：MWU适应非平稳与遗憾保证 | WATCH/SHADOW，非首期 | 固定专家外部遗憾界不等于追踪动态最优或保证盈利。必须有成熟、同口径、扣成本、有界损失，明确延迟、相关性、学习率和裁剪；不得偷用未成熟未来回报。[S17] |
| 84.5b：按近期收益削弱Agent发言权 | REJECT | 财务、治理和反证角色不等于同质预测器。短期价格表现不能剥夺关键风险检查或改变Committee权限，违反现有量化仅影响优先级边界。 |
| 84.A：所有报告固定N/DSR/CPCV_PBO | ADAPT真实报告；REJECT伪造数值 | 分别输出trial coverage、DSR、CSCV-PBO、CPCV诊断、walk-forward/holdout、适用状态和理由。缺数据不填0，不把统计通过升级成正式策略权威。 |

**最小交付：**先做现有入口接线矩阵及适用状态、失败试验留痕、泄漏负例；仅有合格输入时做一组有界标签/简单模型比较。无样本时形成NOT_EVALUABLE及缺口证据即可结束当前工程范围，不强行训练。

量化验收必须保留：时间有序切分、同日横截面分组、标签重叠清除、训练折内预处理、未触碰最终留出、选择家族而非改名重试、真实交易限制和成本。Qlib的Signal/SigAna/PortAna记录模式可用于核对“预测质量与成本后组合表现分开记录”，不引进整套Qlib运行时。[S25]

## 十、WP-85：红利、动量崩溃与公募赎回

**结论：整体WATCH，不新增本轮活动工作包；定性分红质量进入WP-83。**

| 原条目 | 判决 | 理由与未来前提 |
|---|---|---|
| 85.1a：Campbell–Shiller VAR及2.5～3.5年周期 | WATCH；纠正引用 | 原链接实际是Knox/Vissing-Jorgensen 2022，且提出不同于Campbell–Shiller的方法；Chinco链接当前为Ben-David/Chinco 2026版。A股周期长度无可复验依据。先检查现金分红、回购/增发、预测误差与识别条件。[S18][S19] |
| 85.1b：红利短久期、成长长久期轮动 | WATCH | Gormsen研究股息索取权期限结构，不能直接等同股票风格。510880是上证红利ETF，并非中证红利。要立项先定义可观测久期与适用资产，冻结时点数据和基线。[S20][S21] |
| 85.2a：熊市24月/126日方差预警 | SHADOW后置 | Daniel–Moskowitz主要多空动量中输家反弹的机制，不能直接套无杠杆A股多头。窗口和状态先预登记，比较现有风险覆盖的增量。[S22] |
| 85.2b：目标波动/预测波动等于原论文动态策略 | REJECT同名；WATCH简化候选 | 原论文动态权重含预期收益/方差，可负权和杠杆，与恒波动缩放分开。零下限、无杠杆版本可另列项目自有候选，不能承诺将所有崩盘变成可控回撤。[S22] |
| 85.3a：免费季报直接算真实PRESSURE | WATCH数据可得性 | 原论文使用极端流量基金的实际持仓变动，并按个股成交股数等论文明确规定的分母归一化，具体口径依规格而定；Own≥10有论文背景，-5%/-15%并非通用主规格。前十大重仓/份额变化不等于完整强制卖出，基金类别、分红/份额变化和上榜截断需处理。[S26] |
| 85.3b：t-2～t+12三阶段硬规则 | REJECT可交易时间叙事 | 原稿未明确时间单位，事后可见赎回不能倒灌事件前决策。若未来试验，只从实际披露后观测，输出拥挤/赎回暴露线索，不冒称实时被迫卖出事实。 |
| 85.A：直接接risk_overlay | REJECT当前接管 | 2026-05-01起基金报告规范已更新，需锁实际模板与数据覆盖。以后只有真实漏报风险、充分数据、冻结净效用实验和独立准入齐备，才讨论窄范围风险提示；当前不等待数据而悬挂开发任务。[S27] |

## 十一、WP-86：中文语义检索与“逻辑发酵”

**结论：WATCH，当前不下载模型、不安装依赖、不训练Alpha。**

| 原条目 | 判决 | 理由与未来前提 |
|---|---|---|
| 86.1：BGE-small/ONNX、每季单向量 | WATCH检索对照 | 中文small是512维；FP32约94.9MB、INT8约23.9MB，均非运行内存。按实际披露更新、业务段落分块，保留tokenizer/model/hash/原件可见时间，防止整份MD&A静默截断。[S23][S24] |
| 86.2a：余弦相似度×情绪×衰减 | SHADOW仅未来启动 | 分开业务相关、受益方向和事件时间。相似度不是收益预测或可直接相乘的经济量纲；情绪不能仅凭语义相近推定。 |
| 86.2b：远离公告质心等于信息增量 | REJECT充分条件 | 也可能是错实体、谣言、无关内容或OOD。必须先做事件去重和官方核验。 |
| 86.2c：当日波动≈0证明未定价，T+1～T+5必发酵 | REJECT证明与窗口事实 | 可能提前定价、不重要、停牌或不流动。事件当日特征完成后最早下一可执行时点入样；窗口本身只是待检验假设。 |
| 86.A：500×1500全流程<15秒/<600MB | 性能目标待测 | 分离编码、检索矩阵和I/O，固定文本长度、批次、冷暖、机器和线程。ONNX默认内部线程可能用多核，须接预算。未来先与现有关键词检索比较，并按需加入BM25这一低成本基线，评价Recall/NDCG、人工复核量和P50/P95，有改进才引依赖。[S23][S24] |

**重新立项触发：**现有检索在真实研究中出现可定位漏召回/重复阅读问题，并有冻结标注集与可复用来源。先证明检索质量，再提出Alpha假设；无收益立即结束，不要求以交易收益证明基础检索组件，也不自动连接组合权威。

## 十二、WP-87：数据完整性、新鲜度和语义指标

**结论：保留ID，改为“数据集新鲜度、完整性与指标口径补强”，P0。**

| 原条目 | 判决 | 需求与开发评审 |
|---|---|---|
| 87.1a：全部raw JSON入CAS | ADAPT覆盖审计 | 已有raw-first。核受影响来源是否遗漏；原始HTTP字节和SDK标准化输出分别标明，不把SDK表声称为原始响应。保留解析器、schema、时间、来源及不可变hash。 |
| 87.1b：新sync_status、3天空即全局熔断 | REJECT双状态与固定门；ADAPT数据集合同 | Breaker处理source/capability故障，数据集合同处理预期发布、覆盖区间和适用新鲜度。日价陈旧一天可能已危险，季报三天空则正常；区分休市、停牌、合法空事件、失败、部分、陈旧非空。只影响依赖分支。 |
| 87.2a：停牌/权息/IPO硬清洗 | ADAPT语义规范化；REJECT删事件 | 原始价供执行，复权/总收益供适用分析；保留停牌、公司行动、特殊涨跌幅和缺失。不得ffill成可成交价，不得抹平真实事件或用零量一刀切判断停牌。 |
| 87.2b：移除北向硬依赖、用其他指标替代 | ADAPT来源变更；REJECT等价填补 | 定向搜索未发现现役硬依赖，不虚构已存在缺陷。官方2024-08-19起改季度披露；ETF份额、两融、期指基差是不同变量，应另注册指标，不能填回北向字段。[S05] |
| 87.3：指标单一字典 | ADAPT薄注册表 | 定义ID/版本、单位/币种、公式入口、窗口、市场/证券、价格和复权、合并/累计/单季/准则适用域、缺失与零值、时间/血缘。指向唯一领域计算器，拒绝YAML再执行一套公式。CAS30新指标受WP-78实际采用与字段覆盖限制。 |
| 87.A：三天空Preflight全局拒绝 | REJECT验收；改故障矩阵 | 覆盖合法空、节假日、季度等待、停牌、陈旧非空、部分分页、schema漂移、重复发布/重述、失败恢复和水位线不能越过未完成缺口。核真实受影响分支而非整个系统停工。 |

**最小集成面：**ObjectStore/SourceSnapshot、SourceAccessRouter与source_resilience、既有同步/排程回执和next_watermark、领域Schema与计算函数。新指标注册表只做索引和一致性约束。无实际北向依赖时只补新增来源回归场景，不创建无调用方删除任务。

## 十三、纳入计划的边界和顺序

| 阶段 | 活动工作包 | 形成的结果 |
|---|---|---|
| 第一阶段：正确性和可观测性 | WP-87、WP-82；WP-81A可独立推进；WP-84接在数据合同后 | 数据缺失有正确语义、指标口径一致；证据上下文可恢复；交易单位合法；既有验证链可查证 |
| 第二阶段：研究质量和有限实验 | WP-83、WP-81B、WP-79、WP-80 | 政策/行业证据更完整；同约束下改进实施；市场/候选新增特征有基线对照 |
| 观察项，不是完成阻断 | 原WP-85、原WP-86；MWU、GBDT及退出择时中无合格输入的部分 | 记录重启条件与可观测缺口，当前不新增依赖或生产规则 |

原WP-69原生Stop证据和WP-78剩余语义/盲测不被本轮伪造关闭；没有任何新包默认等待它们全部完成。准则新增字段只有在确实消费时才依赖WP-78；WP-82可以完善软件交接，原生宿主验证仍由WP-69独立拥有。

新软件包均为PLANNED，不代表开始正式开发。工程正确性可以通过适用软件验证关闭；策略有效性必须预登记、时间有序/盲测和成本后检验，结果不支持就不启用。任何来源/模型资格不足保持现有受限状态，不为通过计划将其伪造成合格。

## 十四、来源与适用范围

以下为2026-11-28核查的主要来源；只引用它们实际支持的定义、机制、接口和规则。正文中的阈值拒绝/架构取舍属于结合本仓库做出的评审判断。网页/PDF读取失败而仅搜索可见的条目已注明，不将摘要当成完整实证复制。

| 编号 | 一手来源 | 本轮支撑及边界 |
|---|---|---|
| S01 | [FRED THREEFYTP10](https://fred.stlouisfed.org/series/THREEFYTP10) | Kim–Wright、日频、可能修订；不是ACM |
| S02 | [FRED实时版本](https://fred.stlouisfed.org/docs/api/fred/realtime_period.html)、[API Key](https://fred.stlouisfed.org/docs/api/api_key.html) | 默认今天所知历史、ALFRED历史可见信息、用户Key |
| S03 | [NYFed ACM](https://www.newyorkfed.org/research/data_indicators/term-premia-tabs)、[官方更新导航](https://www.newyorkfed.org/research/data_indicators) | 期限溢价模型；当前更新导航为Daily，数据页部分动态正文通过官方搜索检索 |
| S04 | [Brunnermeier–Pedersen作者页](https://markus.scholar.princeton.edu/publications/market-liquidity-and-funding-liquidity) | 市场/融资流动性反馈；不支持原稿LSWI数值 |
| S05 | [深交所披露机制通知](https://www.szse.cn/szhk/hkbussiness/news/t20240726_608353.html)、[AKShare更新日志](https://akshare.akfamily.xyz/changelog.html#v1-20-54) | 官方通知搜索正文可读、直接抓取超时；北向持仓改季度披露。相关库变更在1.20.54，1.20.70页面标题不等于该变更版本 |
| S06 | [SciPy milp](https://docs.scipy.org/doc/scipy/reference/generated/scipy.optimize.milp.html)、[CVXPY求解器](https://www.cvxpy.org/tutorial/solvers/index.html) | MILP、状态/限制/gap、求解器边界；未承诺20ms |
| S07 | [上交所2026规则](https://www.sse.com.cn/lawandrules/sselawsrules2025/stocks/exchange/c/c_20260424_10816482.shtml)、[科创板投教](https://edu.sse.com.cn/tib/)、[北交所2026规则](https://www.bse.cn/jygl_list/200028217.html)、[北交所申报说明](https://www.bse.cn/uploads/6/file/public/202109/20210909101532_hebppbkoi0.pdf) | 交易单位差异；北交所直读重定向失败，官方搜索正文可核实最低量/1股变化 |
| S08 | [Kenneth French动量定义](https://mba.tuck.dartmouth.edu/pages/Faculty/ken.french/Data_Library/det_6_port_form_sz_pr_12_2_daily.html) | 跳过最近一月的研究定义；不是A股全部策略硬门依据 |
| S09 | [DeepSeek Context Caching](https://api-docs.deepseek.com/guides/kv_cache/) | 持久前缀单元、best effort、实际usage字段；不证明本项目有推理网关 |
| S10 | [Anthropic上下文工程](https://www.anthropic.com/research/effective-context-engineering-for-ai-agents) | 按需材料、可展开引用和压缩取舍；不等于允许静默丢反证 |
| S11 | [CoVe原论文](https://aclanthology.org/2024.findings-acl.212/) | 独立核查可降低所测任务幻觉；未证明金融判真或无幻觉 |
| S12 | [发改委石油价格管理办法原件](https://www.ndrc.gov.cn/xxgk/zcfb/tz/201601/W020190905506573420251.pdf) | 第六/七条分段及临时调控；不直接推出企业利润 |
| S13 | [Bailey/López de Prado DSR原论文](https://www.davidhbailey.com/dhbpapers/deflated-sharpe.pdf) | 选择偏差、非正态与试验信息，不以一个N充分替代 |
| S14 | [PBO原论文](https://www.davidhbailey.com/dhbpapers/backtest-prob.pdf) | CSCV与PBO定义；不是CPCV同义词 |
| S15 | [López de Prado研究索引](https://www.quantresearch.org/Innovations.htm) | Triple Barrier/Meta-labeling/CPCV的研究来源，不等于A股可直接交易 |
| S16 | [LightGBM官方参数](https://lightgbm.readthedocs.io/en/stable/Parameters.html) | CPU/正则/单调约束可实现；不证明特定先验或性能 |
| S17 | [Arora/Hazan/Kale MWU综述](https://theoryofcomputing.org/articles/v008a006/v008a006.pdf) | 固定专家比较、有界代价与遗憾界假设 |
| S18 | [Knox/Vissing-Jorgensen 2022原件](https://www.federalreserve.gov/econres/feds/files/2022014pap.pdf) | 原稿误链；明确提出不同于Campbell–Shiller的分解 |
| S19 | [Ben-David/Chinco原件](https://www.alexchinco.com/crimes-against-campbell-shiller.pdf) | 取回版本为2026-07-03；说明应用假设与限制 |
| S20 | [Gormsen 2021](https://onlinelibrary.wiley.com/doi/10.1111/jofi.13020) | 股息期限结构研究；不直接等同红利/成长风格轮动 |
| S21 | [上交所510880信息](https://www.sse.com.cn/assortment/fund/list/etfinfo/basic/index.shtml?FUNDID=510880) | 上证红利ETF的正式身份 |
| S22 | [Daniel/Moskowitz 2016](https://www.kentdaniel.net/papers/published/jfe_16.pdf) | 动量崩溃及动态策略与恒波动缩放的区别 |
| S23 | [BAAI BGE-small中文model card](https://huggingface.co/BAAI/bge-small-zh-v1.5) | 512维及检索定位；相似度须按任务校准 |
| S24 | [Xenova ONNX文件](https://huggingface.co/Xenova/bge-small-zh-v1.5/tree/main/onnx)、[ORT线程管理](https://onnxruntime.ai/docs/performance/tune-performance/threading.html) | 权重大小及线程机制，不等于全流程内存/耗时 |
| S25 | [Microsoft Qlib Recorder](https://qlib.readthedocs.io/en/stable/component/recorder.html)、[官方实现](https://github.com/microsoft/qlib/blob/main/qlib/workflow/record_temp.py) | 分开信号评估、组合评估及成本结果的模式；不引入平行平台 |
| S26 | [Coval/Stafford 2007](https://www.hbs.edu/ris/Publication%20Files/Asset%20Fire%20Sales%20in%20Equity%20Markets_24af6e8c-8417-48a3-a384-053b90473e78.pdf) | 原PRESSURE、样本与持有者条件，不支持全部A股阈值 |
| S27 | [证监会2026年第5号公告](https://www.csrc.gov.cn/csrc/c101954/c7619929/content.shtml) | 基金定期报告新规范自2026-05-01实施，未来数据接入需核实际模板 |
| S28 | [Campbell/Shiller原研究NBER条目](https://www.nber.org/papers/w2100) | 原论文元数据通过搜索核对，PDF/页面直读受限；本评审不声称完成该原论文实证复制 |
| S29 | [中国货币网LPR](https://www.chinamoney.org.cn/chinese/bklpr/) | 期限与发布日期由官方指标定义；LPR-Shibor预测效度未验证 |
| S30 | [Greenwood/Vayanos作者版](https://personal.lse.ac.uk/vayanos/Papers/BSEBR_RFS14.pdf) | 美国国债供给、久期风险与国债收益；不是A股红利股息率利差的可交易性证明 |

原稿指向[smallake的PDF](https://www.smallake.kr/wp-content/uploads/2018/07/SSRN-id3104816.pdf)实际为“The 10 Reasons Most Machine Learning Funds Fail”，本轮主评审与子评审均读取原件核实标题；正式方法依据使用S13～S15原始来源。源社区与开源仓库用于发现实现模式、复核接口及失败边界，没有把热度、帖子结论或单一收益图升级成投资证据。

## 十五、本轮完成标准

本轮完成指：逐项判决与依据已写入本评审，七个PLANNED包及两个原有包在开发计划和机器索引中一致，独立Review和相关L0验证有真实回执。它不意味着任何新优化已经开发、达到回测效果、获得来源资格或改变正式投资/交易权限。最近验证结果由进度验收.md记录，后续实现只从开发计划接续。
