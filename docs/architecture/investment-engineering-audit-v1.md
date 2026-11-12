# 投研与工程质量审查：WP-60

> 状态：CURRENT（缺陷矩阵和实际运行证据；发布状态以Git和本轮验收为准）
> 日期：2026-11-12
> 工程：AStockMultiAgent；验证与发布均以实际回执为准
> 现行主线：WP-58、WP-59、WP-60；durable run `lr_mwacvu2v_2ce80cbe8f7b`

## 审查方法与范围

先从代码、版本化配置、正式工作包、录制样本和真实运行回执定位缺陷，再按安全影响、用户体验、平均耗时、失败恢复成本和回滚成本排序。完整性不能以测试夹具、回答文本或单一 provider 成功推断。对本轮无法取得同环境前后基线的项目，明确标为“待量化”，不夸大性能收益。保留原 ResearchSubjectRegistry、StateStore/ObjectStore、Publication、真实/模拟账户边界和唯一 writer。

## 经代码及运行结果确认的问题

| 严重度 | 问题与定位 | 影响 | 处理与验收边界 |
|---|---|---|---|
| P0 | `monitoring/news.py` 的 ArticleList 上限与 `monitoring/service.py::_process_news` 原游标推进未绑定截断回执 | 高密度新闻窗口可能漏检 | 已实现有界时间窗二分、完成度回执与未完成不前移 watermark；专项 recorded regression 待最终冻结树确认 |
| P1 | 社区页面相同内容重复注册时，`source_snapshot_index` 存在 `UNIQUE(source_id, object_hash)` | 第二次快照原始 ID 漂移产生 SQLite 外键错误 | 社区快照统一复用 `source_id + content_sha`；原始请求哈希和后续工件均可追溯，录制持久化专项已通过 |
| P1 | 新闻重新抓取时快照 ID 改变可能改变 `MonitorEvent` 去重身份 | 重复事件/Agent 研究任务与占用增大 | 仅对 `NEWS_LEAD` 采用来源与文章稳定标识做幂等键，仍保留独立 payload hash 审计；不得改变其他事件合同 |
| P1 | 独立 worktree 共用根 `.venv` 的 editable install | 测试执行可能误加载另一代码树、得到虚假的失败/通过 | `scripts/run_local_quality.py` 在子进程显式前置当前 `src/` 到 `PYTHONPATH`；保留源树前后指纹与受控临时目录 |
| P1 | 原浏览器扩展按单任务串行处理两个平台，每平台多个关键词，同时 bridge 只有 45 秒 | 热路径经常超时，部分来源未能形成可用回执 | 两平台最多两个独立后台标签并行执行；120 秒有界任务预算，扩展缺席检查可提前返回；单个平台失败不得取消另一平台有效结果 |
| P1 | Provider Registry 新成员及新增 Workflow 未与机器验收表同步 | 影响其他组件的确定性回归 | 原有顺序和兼容请求合同保留；索引、录制 fixture、标准章节和专项测试对齐 |
| P1 | 已登录知乎搜索页仍可能展示无关推荐卡，通用 `.ContentItem` 选择器容易把个性化推荐当作搜索结果 | 查询无关内容形成假完整覆盖、误导舆情结论 | MV3 内容提取增加股票/行业查询语境匹配，单独的 Node 行为负例 5 passed；真实页面仍需复核 |
| P1 | 社区 Provider 构造时自动创建桥接密钥，并在扩展未配对时等待轮询 | 常规投研可能无意义增加约 35 秒外部等待 | 密钥只由显式配对命令创建；未配对直接返回源不可用，不为缺失扩展启用超时采集 |
| P1 | 投研能力图虽然接入 `SOCIAL_SENTIMENT`，但 `VerifiedAnswerProjector` 初始没有消费该类型，投资者正文看不到收集到的观点 | 形成“后台采集、前台遗漏”且难以复核的断层 | 在既有冻结来源、正式 output verification 之后增量投影最多三条讨论标题、平台日期和原文链接，并显式保留未经官方核实及部分覆盖风险；不更改正式买卖建议和真实/模拟执行 |
| P1 | 定时研究录制测试使用 UTC 日期构造 CNINFO 查询区间，而业务要求覆盖上海交易日 | UTC 16:00 后运行时产生虚假的 `DISCLOSURES INVALID` | 修正测试按 `Asia/Shanghai` 构造公告日期；原失败三用例复测 3 passed，生产时间覆盖门未改 |
| P1 | `monitoring/source_cursor` 在 0059 迁移中固定来源枚举，新增 `COMMUNITY` 后触发 SQLite `CHECK` 错误 | 社区监控无法确认已检查水位 | 新增不可变 `0079_continuous_monitor_community_cursor.sql`，原位复制历史游标、扩大来源白名单，保留外键和旧水位；新增迁移保真/非法来源负例，通过本次专项测试 |
| P1 | 投资能力图虽有 `SOCIAL_SENTIMENT`，但正常 `InvestorOrchestrationService.execute` 未安装对应处理器，常驻 Monitor 也未执行 `COMMUNITY` | 注册能力但不会实际采集，持仓风险复核遗漏渠道 | 增量注册已配对会话的可选只读处理器，仅普通请求运行、冻结工件重放不重抓；常驻监控在原行情/公告/模拟盘后处理最多1个到期标的，失败隔离。新增处理器与调度测试通过 |
| P1 | 真实知乎搜索卡使用 `.SearchItem-time`、雪球卡使用 `.date-and-source`，旧选择器未取得时间；仅月日的上海日期在 UTC 投影可能显示前一日 | 有效舆情被误判未定，报告日期偏差一天 | 复用不可变DOM原件修复日期解析；增加 DAY/RELATIVE 精度和完整自然日边界判断；投资者层改为北京时间；录制正反例与真实样例核验 |\n| P1 | `DomainContractAudit` 原来只核对已登记的社区快照/哈希，没有把 SocialLead 证券代码与本次 InvestorRequest 的 entity_ids 匹配 | 被错误复用的另一证券真实社交工件可能混入当前报告 | 在唯一域准入层复用原 `_same_identity` 核验每条线索；不同证券/无证券身份负例均拒绝。2026-11-12修复树84项专项通过，不增独立阻断门 |\n| P2 | 2026-11-10 原 Antigravity 旧父进程退出后仍留两项语言服务 | 无用工作集约 187 MiB | 已按 PID/路径/父进程/启动时刻核验后结束 20028、20904；当前 GUI 与活动进程继续运行，事后 PID 消失已核实 |

## 运行义务与未消除的风险

- **真实平台接线**：2026-11-11已配对本机Chrome扩展。2026-11-12两公司真实后台采集均获得已登记SourceSnapshot和SocialSearchResult：立讯精密XUEQIU原始1/接纳0、ZHIHU原始169/接纳4；华海药业XUEQIU原始2/接纳0、ZHIHU原始72/接纳0。两公司覆盖均为PARTIAL，故真实链路可用不等于近七日完整搜尽；不输出“无负面舆情”。
- **WP-54**：历史 QQBot inbox/部分群送达有正面回执，但最近同 ID 只读 SSH 查询遭连接重置。现时生产投递状态未独立复核；不得盲目重发。
- **WP-55**：动态观察成员 18，机器正式 Q3 release 登记仍见 0/18、最终组合 receipt 缺失。不能依据投资报告自然语言跳过正式财报认证。
- **运行磁盘**：历史抽样 `.scratchpad` 约 460 MiB、`.ai-bridge/quality-runs` 约 1012 MiB、`runtime/longrun` 约 700 MiB。存在旧质量执行现场，但活动运行、来源引用、陌生 owner、正式账本和 worktree 必须先审计后才可列入可释放额度。
- **投资算法审计**：继续复核风险预算、持仓相关性、风格/行业集中、估值时点和单位、财报重述、费用/滑点和模拟执行路径；现有 PIT `available_to_system_at` 与正式 `Publication` 检查属于应保留的不变量，不能为了速度删掉。
- **采集与编排效率待量化**：本轮两个真实后台检索的墙钟约50.1秒与51.0秒；此前两次在不同代码/网络状态下约75.4秒与75.1秒，只记录实测，不因N=2且环境不同宣称因果提速。长期P50/P95、请求总数、内存、缓存命中和语义研究增量命中仍需自然生产流量监测；全链保持有界预算、缓存和单一事实源。
- **舆情模型误差**：标题/正文关键词初筛只为发现线索，不能代表事实可信度、独立观点数或预期收益。相似标题传播、跨平台转载、负面反证和实体歧义仍须在证据调查层复核；相关质量须用盲化样本记录准确率/漏检率。

## 验收和回退

生产交付按 `Schema → Provider Registry/State → Service → CLI/Research Workflow → recorded/live tests` 顺序。以版本化代码、SourceSnapshot/Result 回执、真实重放和确定性边界形成终局 Review：Pytest 风险影响面、Ruff、Pyright、Git diff check、外部源小样本真实浏览器验证、原投资硬门负向样本都需要实际 PASS。完成前不可把本页状态改为 RELEASED。回滚通过关闭新 provider/capability 路由并回退该工作包 owned commit；保留原账本、正式证据和其它 Agent 工作树。
