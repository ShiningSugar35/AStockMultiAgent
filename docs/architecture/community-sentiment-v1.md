# Community Sentiment Research — 增量接入架构 v1

> 状态：CURRENT（WP-58、WP-59 的代码结构已实现；第三方搜索完整性按逐次回执判定）
> 是否已经实现：已实现后台双平台采集、证据落库、投研投影、增量监控及GDELT补洞；不能保证第三方搜索全量完整
> 基线：现有 Provider Registry、ObjectStore、SourceSnapshot、Investor Capability DAG、Continuous Monitor
> 原则：保持单一事实源和研究/交易分层，不新增独立研究数据库、执行器或守护进程

## 目标

对已解析的A股证券身份增加雪球（原发、近7日）和知乎（非视频、近7日）的公司名、代码、行业链和管理层关键词检索，供候选研究、单股/行业/治理研究、持续跟踪、模拟盘、持仓和组合风险使用。

## 数据路径

```text
ResearchSubject / Watch / Request
    ↓ 证券身份、别名、行业与管理层关键词
SOCIAL_SENTIMENT (conditional supplemental input)
    ↓ Provider Registry [news.community.lead]
Chrome 原有登录配置（MV3 扩展）
    ↔ 127.0.0.1 临时 token 绑定任务桥
    ↓ 原始查询页结构化快照（content hash）
SourceSnapshot / ObjectStore
    ↓ 时间、类型、实体、相似标题及 URL 去重
SocialSearchReceipt + SocialSearchResult
    ├─ Investor 近7日多空/争议/行业观点（仅研究输入）
    ├─ Research Agent 核查官方公告、财报、监管
    └─ Watch Universe => COMMUNITY/NEWS_LEAD => existing MonitorTask
                           ↓
                       Incremental Evidence / BaseCase / Committee
```

## 组件所有权

- `social_sources/models.py`：严格的窗口、查询、帖子、覆盖、结果合同。
- `social_sources/config.py`：版本化配置与本地配对；令牌在 Git 忽略的 `runtime/local/`。
- `social_sources/bridge.py`：仅 localhost、来源校验、单次租约、有界 payload、确定性终态。
- `social_sources/provider.py`：站内查询构造、二次过滤、原始结果落对象库、规范化线索与覆盖回执。
- `social_sources/analysis.py`：研究者可审阅的观点分组和证据链接；关键词只作倾向提示，非事实或收益预测。
- `social_sources/monitor.py`：仅将合法工件的线索写入已有 MonitorEvent / MonitorResearchTask，不写经济事实。
- `social_sources/cli.py`：`social-source-status`、`social-capture-token`、`social-search` 和 `social-search-show`；由现行 root CLI 注册。
- `browser_extension/social_capture/`：单个 Chrome MV3 扩展，使用同一 Chrome 用户配置搜索原发/非视频帖子，不额外收集账号登录信息。
- `monitoring/news.py`：GDELT 上限饱和自动拆窗，预算不足返回不完整；`monitoring/service.py` 不推进不完整覆盖游标，消息重采有稳定事件 ID。
- `investor_orchestration/capabilities.py`：研究/荐股/监控/模拟请求增加只读 `SOCIAL_SENTIMENT` 条件输入；已配对浏览器时由 `social_sources/investor_handler.py` 在普通投资执行中受控自动采集，未配对时跳过；冻结工件重放不启动浏览器，不修改原有正式投资判定依赖或账户/交易权限。
- `providers/supplemental.py`：继续统一线索能力，区分社区来源的覆盖完整度；一次失败不替代权威来源。

## 重要实现细节

**窗口与单位：** 原始采集及证据存储使用 UTC 有时区时间，投资者展示使用北京时间。明确区分完整时间戳、相对时间与仅精确到日的日期；日级标签只有在整个上海自然日落在七日区间内时才准入，区间边界无法确定则保持未判定。严格核对Xueqiu原发、Zhihu非视频和证券相关性；缺失时间或来源结构绝不推断成无负面新闻。

**来源完整性：** 检索回执记录每平台查询数、已确认页数、截断、失败、原始数量、接纳数量和去重数量；`complete` 不按有无帖子推断。GDELT 拆分最小1秒间隔，不使用下一查询覆盖上次未完成窗口。

**幂等性：** URL 作为跨采集会话稳定帖子身份，SourceSnapshot 记录版本；研究事件按 target+source+lead_identity 去重。不同平台的同名标题保留不同来源，重复传播只标注标题簇，不当作独立证据。

**资源：** 限定每平台关键词数量、搜索滚动/条数、120秒有界桥接窗口、30秒扩展轮询与双平台并行任务、重试和租约；无持续额外 daemon。原本的 GDELT、CNINFO 和市场行情源与社区桥无强耦合；失败写覆盖缺口并让主研究继续。

**风险：** 把所有在线内容作为外部待核线索，Agent 在正式判断前独立核实来源。辅助情绪不跨越 Financial Integrity、Formal Publication、Paper confirmation 或真实券商权限。任何凭据、Cookie、浏览器 Profile 不纳入代码库或可见日志。

## 验证、发布与回滚

使用录制夹具验证七天、原发、视频、权限失效、DOM 漂移、空结果、分页未穷尽、证券相关性、URL 幂等、损坏快照、监控任务重复及跨场景编排。2026-11-12 已在登录Chrome中以后台标签执行立讯精密与华海药业各一次真实采集：立讯雪球1条原始/0条准入、知乎169条原始/4条准入；华海雪球2条原始/0条准入、知乎72条原始/0条准入。两次回执均保留PARTIAL，原因分别含截断分页、无法验证时间与页面结构不完整。该证据证明后台端到端可用，但不代表近七日全量覆盖或各平台所有页面均可解析。代码发布与上线完成仍以独立Review、回归及Git实际回执判断。

停用 `configs/social_search.yaml.enabled` 即让新增社会舆情不参与下轮研究；既有快照、正式证据和账本引用仍按原保留策略处理，不额外整体复制和清空。
