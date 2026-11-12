# Workflow：A股近七日社区舆情发现与证据复核

> 状态：CURRENT DEVELOPMENT（WP-58 / WP-59；真实采集启用须有本地 Chrome 扩展实际回执）
> 适用：选股荐股、单股研究、行业/治理研究、观察池、真实/模拟持仓、组合与风险分析
> 数据预算：零付费 API；使用当前 Chrome 登录会话和本机 127.0.0.1 采集桥
> 记录：Canonical ObjectStore、SourceSnapshot、SocialSearchResult 和 Continuous Monitor Event/Task

## When to use

面向已完成证券身份解析的个股、行业、候选与持仓研究，按统一证据契约补充近七日舆情；不独立驱动交易。

## Flow

### 1. 一次性安装与运行入口

在当前已经登录平台的 Chrome 用户配置中打开 `chrome://extensions`，打开开发者模式并选择“加载已解压的扩展程序”，选取项目内的 `browser_extension/social_capture/`。扩展只使用既有用户会话中打开的搜索页面，不复制浏览器 Profile 或建立第二份凭据库。打开扩展选项，在本机命令获取一次连接密钥并填写：

```powershell
.venv\Scripts\python.exe -B -m astock social-capture-token
.venv\Scripts\python.exe -B -m astock social-source-status
```

密钥仅进入本地 `runtime/local/social_capture.yaml` 与扩展私有配置，不进入 Git、投资报告、截图或诊断日志。后台任务桥只在查询期间绑定 `127.0.0.1:8766`，按任务租约分配并回收，不新建常驻抓取守护进程。

### 2. 同一证券统一查询

每个已解析到证券身份的对象，在源预算内并行或复用最新的市场/公告查询，增加两平台的社区搜索。输入**证券代码+名称**优先；补充已确认的行业、产业链和当前管理层关键词。带行业词的结果归入行业情境，不当作公司直接事实。

```powershell
.venv\Scripts\python.exe -B -m astock social-search --symbol 002475 --name 立讯精密 --industry AI铜连接 --management 董事长
.venv\Scripts\python.exe -B -m astock social-search --symbol 600521 --name 华海药业 --industry 仿制药 --management 董事长
```

雪球使用近七日原发内容，知乎使用近七日非视频内容。抓取后按原始发布时间、类型、实体相关性和去重键二次过滤。平台显示列表、正文片段、查询时间窗、结果截断、空结果和登录状态分别记录，不用搜索失败证明“没有舆情”。

CLI 返回 `result_artifact_id`、`coverage`、`analysis`、`watch_ingestion` 和代表性 URL。Agent 后续复核时可复用已有工件，不重复访问：

```powershell
.venv\Scripts\python.exe -B -m astock social-search-show SocialSearchResult:<artifact-hash>
```

也可通过 `research-supplemental-acquire` 的 `news.community.lead` 具名能力进入现行 Provider Registry / SourceRouter；新来源与 GDELT、CNINFO 共享证据存储，但保留不同来源身份。

### 3. 投研与监控闭环

- **荐股 / 全市场筛选**：先执行现有确定性 Universe、blind tranche 和候选研究，舆情只补充已入围候选的预期分歧、经营疑问、事件线索，不拿讨论热度替代基本面或伪造正式可买标的。
- **单股 / 行业 / 治理**：汇总多空论点、与行业上下游的联系、管理层相关讨论、原文引用及未查实事项；公司事实回到财报、公告、交易所、监管或发行人披露核验。
- **长期观察 / 实际或模拟持仓**：发现新社区线索后，在 Watch Universe 中稳定去重记录 `COMMUNITY/NEWS_LEAD`。普通讨论仅更新观察事件；重大候选事件生成 Evidence/Base Case/Committee 增量任务，由 Research Agent 后续核实。
- **组合 / 风险分析**：将已验证重大变化映射为组合暴露与风险假设，再由原组合能力复核；未经证实的社区观点只用于情景分析，不直接生成订单或改变账本。
- **时间与覆盖**：保留 `published_at` 与 `captured_at` 两种时间；七天窗口以请求时刻计算；`complete=false` 时列出失败渠道/未穷尽查询，不以空结果代替完整覆盖。
- **投资者表达**：已完成正式能力验证的 `SocialSearchResult` 进入现有 `VerifiedAnswerProjector` 的确定性答案投影，在普通投资者正文中增加最多三条近七日代表性原文、平台、日期、观点线索和来源链接，并标注“尚未作为公司事实核实”。部分覆盖明确列缺口，完整且为空不推断不存在负面消息；不改变原投委会/组合结论，也不输出原始 HTML。

投研计划中的 `SOCIAL_SENTIMENT` 为条件型、只读的补充节点。浏览器已配对时，普通投资请求将按已解析证券代码有界自动检索：单轮最多两只、总预算90秒、单只最多45秒，其余候选留待持续观察任务补采；全市场荐股先确定候选证券，再逐股补充，不在未定候选池上无界搜索。持续监控在完成原有行情、公告和模拟盘处理后，每轮最多对一只到期观察标的执行社区增量检查。冻结研究工件重放只使用已登记来源，不触发新的外部采集。此节点不替代 `EVENT_RESEARCH`、正式财务、投委会或 Publication Gate；无新数据时明确渠道未覆盖并继续正式研究，不无限重试，也不延迟已经完成的核心投资结论。

## Stop conditions

当七日覆盖窗口验证完毕、获取超时、平台会话不可用或本轮预算耗尽时，停止该来源调用并明确记录完整度；后续以既有 checkpoint 继续。不得因舆情未覆盖直接修改正式结论或账户状态。

### 4. 故障及验收

执行 recorded fixture、时间边界、原发/视频筛选、无关推荐卡片、空结果、分页未穷尽、同一帖子重新抓取、登录态丢失、桥接密钥校验、不同证券身份、崩溃恢复、已知持仓事件去重测试。低频 live smoke 以立讯精密、华海药业为目标核实返回 URL、文章时间及实际内容；不能把没有登录扩展的模拟结果当 live。

出现浏览器环境不可用时，在既有覆盖回执明确 `UNAVAILABLE/PARTIAL`，继续现有官方与结构化来源；关闭 `configs/social_search.yaml.enabled` 即可回滚新采集路径，原研究/模拟账本保持不变。
