# 数据消费者与原文续跑合同 v1

本文是 WP-72、WP-73、WP-55 的现役实现说明；使用既有 SQLite、ObjectStore、Parquet、provider registry 与 continuation，不建立第二套事实库、费用账本或任务调度器。版本日期：2026-11-22。

## 1. Tushare 结构化数据进入现有消费者

| 能力 | 实现与消费者 | 语义和边界 |
| --- | --- | --- |
| 股票身份 | `TushareReferenceProvider.fetch_instruments` → 既有 MarketReferenceService 精确身份路线 | 显式市场、六位代码、CNY、上市状态校验；保留实际抓取可用时间。当前列表不能证明历史某日上市状态。 |
| 股票名录 | 同一 provider → 既有 master 路线 | 可供补充，缺少权威分母时保留 `TUSHARE_MASTER_COVERAGE_UNPROVEN`，不因返回若干条而宣称完整市场。 |
| 原价日线 | `fetch_daily_records` → 既有 canonical reference Parquet / DatasetRelease | 原始 vol（手）乘 100 为股；amount（千元）乘 1000 为 CNY。校验身份、重复交易日、OHLC、负值，使用既有官方日历判断覆盖。 |
| 周线 / 月线 | `tushare-query` → 既有 `TushareStructuredProvider` 原始捕获 | 目前为直接查询和冻结 raw 的入口；尚未进入独立 canonical 周/月自动消费者，不宣称具有该额外消费链。 |
| 财务三表 | 既有 Tushare 三表适配器 → `FinancialSourceService` → 官方原件认证 | live 采集在现路由判定可用的来源内落实配置首选；供应商成功仍不能代替官方字段认证。 |
| 估值与股本指标 | `valuation_metrics` → 注册 `TushareMetricCapture` → `discovery features --supplemental-file` 实际消费 | total/float/free shares 与 mv 从万单位转换；PE/PB/PS 为倍数、换手率/股息率为百分数。亏损等空值保留空值。指标是二级供应商观察，不转成官方财务事实。 |
| GDP / CPI | `macro_metrics` → 同类型观察 → `OfficialMacroCaptureService.compare_tushare` | GDP 总量为年初累计、单位亿元转 CNY；gdp_yoy 为当季同比，不能与累计同比混用。CPI 同比/环比使用百分数。与已有 NBS 同系列、同期间、同单位观察比较，保留两方原文 hash；不覆盖官方版本。 |

正式 reference 路线继续使用现役 provider qualification、health、breaker 与数据质量检查。有 token 的 live 身份/日线优先使用 Tushare；录制模式或没有凭证时跳过该路线。配置登记能力不等于本机 live 资格晋升。

财务三表在既有路由完成健康、凭证、熔断与能力筛选后，稳定提升配置的首选 provider；当前首选为 Tushare。其余 fallback 保留原排名，不会重新加入已排除来源。这样可避免健康评分较高的二级源先成功后，让配置首选始终没有实际尝试。官方认证仍走同一原件与字段检查，不改变来源权威。

查询共用 `TushareStructuredProvider` 已有 endpoint 白名单、积分/频率限制、缓存和原始响应。绑定请求预算时，一次物理请求只记一次 attempt；预算耗尽在发送前退出。不会向日志输出 token，也不会将带 token 的 MCP 地址存入来源。

### 公共读取接口

```python
load_metric_capture(state, objects, artifact_id, *, subject_id, as_of)
# subject_id: XSHG:600519 / XSHE:000001 / BJSE:... / CN
# artifact_id: TushareMetricCapture:<ObjectStore hash>
```

producer 的 artifact_id 属性直接使用 model_dump JSON 的完整 ObjectStore SHA；CLI 不用会排除易变时间字段的 semantic content_hash 代替注册 ID。读取时核对注册类型、对象 hash、subject、实际可用时间、SourceSnapshot、底层 TushareStructuredCapture，并从原始 fields/items 重算对应 row_hash 和数值。只给一个自行拼装的字典不构成可信输入。宏观输出没有可验证的历史修订档案，始终标明 `historical_vintage_verified=false`。

```python
service = FinancialSourceService(state, objects,
    FinancialSourceParquetStore(paths.parquet / "financial_sources"), paths.root)
release = service.read_certified_release(company_id, period_end, period_type, as_of=as_of)
```

该只读方法返回 `VerifiedFinancialRelease(manifest, facts, observations)` 或 None，复用完整 manifest、Parquet 和原始血缘校验。只接受 v2 CERTIFIED 且官方完整枚举或官方 exact-item admission；legacy、录制索引 fixture、未来可用版本不能作为预测实际值。预测成熟度和量化特征共同使用此入口。

认证事实的 `source_snapshot_id` 指向官方 PDF；原始供应商 observations 指向供应商快照，两者不能靠 snapshot ID 直接等同。合并/母公司范围须从明确 statement、period、duration、field 绑定核验。金额统一调用 `financial_amount_cny(value, unit)`，拒绝 SHARES、RATIO、PERCENT，避免维护第三套换算表。

命令：`tushare-valuation-capture`、`tushare-macro-capture`、`tushare-macro-compare`。比较所需官方 GDP/CPI 同系列观察未存储时明确返回不可比较，不凭厂商值生成 NBS 事实。

`python -m astock.candidates.discovery_cli features` 保留原参数，并通过一个可选 `--supplemental-file` 读取 `SupplementalFeatureRequest`；同模块 `schema` 展示该请求合同。请求只引用本地已注册工件，证券、市场、截止时间、观察日期、单位和 raw 均经公共读取入口验证。PE/PB 非正值不参与价值分位，股息率零与缺失分开；同行描述只在同交易日、同显式研究组且至少 10 家有效不同公司时计算中秩分位。分组明确标为调用方研究假设，来源仍为二级供应商，不能当作独立推荐票数。

财务认证新增可选 `CASH_DIVIDENDS_PAID`，原 18 个必需映射不变。它没有供应商混合科目别名，只接受合并现金表中明确人民币、目标累计期和当前列顺序的完整单行“向本公司普通股股东支付的现金股利”。每股、预案、含利息、少数股东、跨行借数及歧义行不形成该事实；缺失不补零。候选以同周期 TTM 经营现金流除以已支付普通股现金股息计算覆盖，零分母、金融行业或期间不足保持不可比；这不是自由现金流覆盖，也不能把股息收益率充当现金支付金额。

## 2. 券商预测样本

`EastMoneyForecastProvider` 使用 AKShare 官方报告接口适配器公开的同一上游。直接限量访问 report/list 是为了避免 SDK 默认抓取全历史；来源组统一为 EASTMONEY，不能把 AKShare 包装层和东方财富重复计成独立证据。

一次只取一个公司、最多 366 天窗口、默认 2 页 × 50 条，上限 3 页 × 100 条，每页最大 2 MiB，总采集窗口 20 秒并受外层请求预算约束。不自动下载报告 PDF，也不调用语言模型。

每个样本明确记录公司/市场、EPS、目标财年、CNY_PER_SHARE、机构、报告标识、报告标题、可空作者原文、机构原评级、供应商归一评级、报告发布时间及精度、实际捕获可用时间、raw snapshot/hash、row_hash 和来源组。两种评级分字段，不把东方财富归一评级冒称券商原文；PDF 目前仅 URL，明确标记 URL_ONLY_NOT_CAPTURED，PDF hash 和页号保持 None。目标年份取响应显式 currentYear 及偏移，不用运行机器年份补全。响应未提供年份时拒绝。当前上游版本不提供完整历史预测修订档案，不能用于回填过去已知信息。

`aggregate_forecasts(...)` 验证注册 capture、所有原始页的来源与可见时间，并要求 capture 时间等于原始快照最晚可见时间后再聚合；把 wrapper 时间倒填不会纳入未来预测：

- 同一报告的重复抓取作为版本，取截止时间之前的最新捕获，不增加机构票数。
- 每家机构取最新报告；相同最新时间的冲突值排除并列出机构 ID。
- 同一报告、同一捕获时点的冲突版本也排除，不能用 hash 排序选一个假定正确值。
- 不同财年、公司、单位不混合；缺失预测不补零。
- 输出样本数、机构数、原始样本数、均值、中位数、样本标准差、范围、冲突列表、来源组和输入版本。

结果注册类型为 `SampledForecastAggregate`，ID 为 `sampled-forecast-aggregate:<hash>`；消费者只可称“有界券商预测样本”，不得标成全市场一致预期。机构研究上下文仅在公司、截止时间、显式 EPS 财年及 CNY_PER_SHARE 一致时比较预期差。FCFF 或收入没有 EPS 绑定时返回不可比。

命令：`forecast-sample-capture`、`forecast-sample-aggregate`。只读消费者使用同一 `load_forecast_capture` 与 `select_latest_forecasts`；`aggregate_forecasts(..., persist=False)` 不登记新对象。候选消费当前和此前实际冻结的 capture，按同公司、同机构、同财年配对计算已观察修订，新增/退出机构不伪装成盈利修订。下一年度 EPS 增长还须两财年的最新版本均无冲突，且来自同一报告、同一 raw 行；任一年度冲突或非正基数均不参与。输出逐对来源、机构数量、冲突和缺失覆盖，不倒填历史版本。

官方业绩预告是另一类输入：`load_official_web_capture` 共享原 acquisition 的完整 PDF、admission、注册身份、时点和来源政策核验。候选再验证同一原件的原生页 hash、证券及明确目标期次/本期类别，只读取明确的预告方向；年度财报追述、历史类别、未勾选选项和含糊正文不替代当期指引。预测方向不是已实现财务事实，数值区间不从标题或类别推算。

## 3. 宿主 Web 结果进入原文和同一 continuation

宿主本次 invocation 的 Web 能力观察仍由既有 WebToolPolicy 路由。可用且订阅内包含的 search/fetch 优先；只有既有策略允许的失败或不可用情形才回退 standalone DeepSeek search/fetch，费用使用已有 WebToolOperationRepository。原文解析不需要第二次模型总结。

`WebOfficialDocumentSelection` 必须选择同一成功 operation 返回的 URL，并绑定当前 continuation/task、公司、报告类型、期间和来源政策。链路为：

1. 现役宿主搜索/抓取结果冻结。
2. `resolve_official_result` 检查同一 operation；用现役 ownership 锁防并发重复抓取。
3. 新下载显式绑定同一 continuation 的请求预算，由 downloader 每个物理跳转计一次 attempt；耗尽时连 DNS 都不启动。下载原文时逐跳执行来源政策和公网检查；只允许 HTTPS 443。DNS 解析后的所有地址均须为公网，连接固定解析 IP，TLS 验证原域名。
4. 限重定向、字节、页面和时间；PDF magic 不符、private/mixed DNS、循环跳转、非官方跳转、超限都拒绝。原始 bytes 和抓取时间独立冻结，成功重试可复用。
5. 交由现有 OfficialWebDocumentCaptureService 生成 admission，并冻结 `CurrentResearchAutomaticResolution`。
6. 现有 coordinator 验证 operation identity、绑定 capture、恢复同一 continuation；财务原文仍由 FinancialSourceService 认证字段/期间/合并范围，URL 或摘要本身不能形成 CERTIFIED。

现役 `research-current-continuation-start` 与 `research-current-continuation-resume` 默认调用同一 `advance_current_web` 应用入口。start 可用 `--host-capabilities` 声明本次真实工具；resume 用 `--web-batch` 接收有界 `CurrentResearchWebBatch`，其中包含本次能力观察、真实 HostWebResult 和来自原件的 metadata/selection。空输入先准备既有 scheduler 的真实代际，返回原 continuation 顶层字段及 `web_operations`；缺失、未知或过期的宿主能力只返回待观察请求，不默认付费。宿主内工具仍必须由当前 Agent 真正调用，Python 不读取或伪造订阅私有接口。

回填先核整个批次的请求与代际，再冻结结果；已具备合法原件 selection 的独立任务由生产 factory 创建 KillableResearchWorker，交现役 SLA 的显式任务子集并行执行。worker 只冻结原件和 resolution，唯一 coordinator 负责 apply 与同请求 resume。它不再要求把一个任务手工拆成多次独立 CLI；旧 `web-original-resolve <selection_file>` 也委托此同一入口。当前软件接线不等于 Antigravity 宿主原生会话已实测。

wave selection 的 `dispatch_id`、Web request 的 `continuation_dispatch_id` 和当前 task 的 scheduler 代际必须一致；Web operation 自身的 dispatch/callback 代际另行核验。实际等待只根据已登记的真实 operation，不把尚未取得宿主能力的 prepared 任务当作外部在途。A 已消费而 B 仍等回填时，B 保持原身份；独立 C 的 acquisition 只排除 B/UNKNOWN 对应能力的网络，原全局 `allow_network=False` 始终最高优先。请求键使用稳定 task、scope 和 started_at，不随兄弟任务更新或当前时钟生成第二个收费键。重复已应用回传只确认原记录；过时代际、跨请求和冲突仍拒绝。

新原件下载和明确允许的付费 fallback 使用现有 owned worker，沿用原 request deadline/attempt 预算和费用账本；取消会等待子进程树回收。已冻结 raw 在预算结束后可以用 `allow_network=False` 完成本地 admission，worker 仅获得既有 policy timeout 范围内的 CPU 回收时间，transport 预算不续期。原件缺失、hash 损坏或解析失败保留该分支原因，其他独立合法原件仍可继续；不新增 host、paid 或 SDK 请求。日线缺口继续走 typed 行情消费者，不把 PDF admission 当作行情。财务字段认证与投资发布门保持不变。

### 动态页面适配的本轮裁决

本轮没有出现必须使用浏览器才能取得、且现役官方 HTTP/PDF 路径无法满足的合格目标页，因此不选用 Crawl4AI/Scrapling，也不安装第二套浏览器采集栈。这个按实际缺口作出的不接入决定不等于性能比较通过；未来若同一请求出现明确的动态页面缺口，再按已有出站、预算和 owned worker 合同选择单个适配器。Agent-Reach 与退役社区采集不恢复。

## 4. 复杂 PDF challenger 评估决定

当前采用现役 PyMuPDF 原文解析和既有有界 OCR。本轮 native 验证覆盖官方三表的 18 个必需字段、表名/期间/单位/科目/数值页证据、编号表头、期初期末列顺序，以及非人民币、单位歧义、母公司表、缺目标期的拒绝；新增纯现金股息也用真实生成的 PDF 走原件准入和认证正负例。这些是当前样本的原生解析与金融口径验证，**不代表已覆盖银行/保险、附注、横页、多层跨页表头和扫描预测表的全部复杂表型**。

当前没有取得经人工标注且确认为“原生结构解析不足、但可由新 parser 恢复”的关键目标页。已有负例主要是期间、币种、合并范围和科目歧义，模型不能替代该来源口径；Q3 增量运行的缺口则是未取得合格中文原件，不是已取得原件后发现复杂表结构失败。因此原计划“仅结构不足关键目标页试 challenger”的条件目前未触发。**本轮未进行 MinerU/Docling 质量比较，也未声称完成全套复杂 PDF 真值 benchmark。**

2026-11-21 对本机项目 Python 的只读依赖检查结果：pymupdf 可用；docling、magic_pdf、paddleocr 均不可导入。这个依赖事实只补充资源条件；拒绝接入的主要依据是当前没有已证实的可恢复需求样本、固定本地模型和相同真值下的质量/时延/资源收益。本轮没有下载模型或上传文档，Commons Clause 未作为选型阻断。

决定为 **本轮拒绝启用新的复杂 PDF challenger**。Docling 官方说明首次使用会自动下载模型；关闭 remote services 只约束上传数据，不能阻止下载模型。因此只写 `enable_remote_services=False` 不满足可控资源与可复现要求。当前没有预置且固定 hash 的模型，也没有与现役表格认证比较的质量/时延/峰值资源证据，不应把可选组件接入默认请求。

未来有真实复杂 PDF 样本且确认本地模型资产时，复用 owned worker 与原文对象，固定模型版本、禁用隐式下载/远程上传、限制页数/字节/期限，仅对失败页作 challenger。准入必须同时报告字段准确率、跨页表格错误、单位/负号错误和资源成本。此评估结论完成本轮可选项取舍，不代表装了依赖或已证明性能更好。

## 5. WP-55 原观察任务增量补证

`WatchCampaignService.recovery_plan()` 是当前 ResearchSubjectRegistry 成员和缺少 Q3 正式 release 的投影，不创建第二份名单。稳定请求身份含 campaign、期间、instrument。

`recover_missing_q3` 必须持有现役 slot 的 owner、round_id、lease_generation；同轮补证复用 ownership 串行化。原来的“每轮最多3家且始终选前三家”会让持续失败的标的饿死剩余名单，现默认一次最多覆盖18家、600秒（调用方显式上限50家/1800秒），物理请求预约随上限相应扩展并继续受真实Tushare源级频率、并发和owner租约限制。每轮仅登记**真正尝试**的成员，在同round下跨lease_generation保留已尝试集合；优先尚未尝试者，遍历一轮后才重试失败者。预算/租约到期只结束本代网络操作，已获取正式原件继续本地认证，不伪报全部来源已经耗尽。只调用既有FinancialSourceService.sync和持久checkpoint，不增加调度器或推荐权限。

最终就绪检查现在读取完整认证 manifest 和原始血缘，修复只从 SQL release 行取 official_lineage_kind 导致误判的问题。失效 owner 的显式 generation checkpoint 也被拒绝。

2026-11-22 对原 campaign `ten-stock-q3-2026` 全18名成员实测：修复首选Tushare实际排序、CNINFO季度公告检索词过窄，以及沪市报告“人民币审计类型”紧连和利润表标题/金额单位跨页的版式误判后，**18个Tushare结构化来源均取得18项观察，18家均取得CNINFO官方PDF和`CERTIFIED`财务release**。`readiness`严格核验全部原件与字段血缘，返回`q3_ready=true, missing_q3=[]`；其中002001已认证9项字段，其余17家各16项，字段缺口和600641的供应商/官方现金流数值冲突保留供财务质量审计。既有PM轮generation12已应用`COMPLETE`取件checkpoint（18/18），原long-term watcher未被停止、原成员/绑定不变。**`FORMAL_PORTFOLIO_REQUIRED`仍成立**：财报就绪不等于独立多空、委员会、正式RecommendationResearchReceipt和组合/公开发表已经完成；不能虚构推荐或调用交易。具体回执见[最近验收](../../进度验收.md)。

## 6. 验证与来源

针对性测试覆盖单位、身份、截止时间、raw hash 污染、宏观期间、重复/冲突预测、预算截断、私网 DNS/跳转、同 operation single-flight、同 continuation 绑定、真实认证链只读、Q3 缺量和陈旧租约。测试结果统一进入主验收记录，不在此维护重复计数。

Mootdx 沿用已跑通的隔离 SDK + MAC compatibility fallback。当前公开能力、2026-11-21 沪深真实 quote / 60m / 5m 记录及原 STANDARD 协议失败证据见 [Mootdx 现役说明](../../integrations/mootdx/README.md)；本轮消费者扩展不重复创建 provider 资格，不能将 fallback 提升为稳定主源。

官方依据：

- [Tushare 股票列表](https://tushare.pro/document/2?doc_id=25)、[日线](https://tushare.pro/document/2?doc_id=27)、[每日指标](https://tushare.pro/document/2?doc_id=32)。
- [Tushare GDP](https://tushare.pro/document/2?doc_id=227)、[CPI](https://tushare.pro/document/2?doc_id=228)、[积分与频次](https://tushare.pro/document/1?doc_id=290)。
- [AKShare 股票研究文档](https://akshare.akfamily.xyz/data/stock/stock_research.html)及[官方适配器源码](https://github.com/akfamily/akshare/blob/main/akshare/stock/research/stock_research_report_em.py)。
- [Docling 离线模型、remote services 与资源约束](https://docling-project.github.io/docling/usage/advanced_options/)。
