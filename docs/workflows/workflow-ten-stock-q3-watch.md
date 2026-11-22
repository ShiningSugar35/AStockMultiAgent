# Workflow: 10+三季报观察与组合跟踪

> 状态：CURRENT（执行工作流；实际启用以本地运行回执为准）
> 更新：2026-10-08
> 当前实现：watch_campaign 元数据接口、代际 lease/CAS 续接、已有增量研究与本地报告投影；实际调度、专项回归以运行回执为准，不能从本文推断全部能力均已验收。
> 研究范围：原十股与后续新增的8只身份核验候选，目前18股，今后以ResearchSubjectRegistry动态投影为准；不宣称全市场选股；不下真实或模拟订单。

## When to use

用于用户明确指定一组A股，要求持续观察至指定财报全部披露，并在正式研究闭环后形成受限组合，同时保留盘前/盘后增量跟踪和可恢复的ChatGPT激活边界。此Workflow不用于全市场荐股，也不授权订单。

## Flow

### 身份、日程和恢复

canonical campaign=`ten-stock-q3-2026`，元数据保存在现有SQLite checkpoint，成员从ResearchSubjectRegistry中同一request_id投影。禁止把聊天里的旧研究数字当事实。运行入口（工作目录D:\AStockMultiAgent）：

```
.venv\Scripts\python.exe -B -m astock.investor_orchestration.watch_campaign status
.venv\Scripts\python.exe -B -m astock.investor_orchestration.watch_campaign begin --slot pm --bucket YYYY-MM-DD --owner 本次唯一激活ID
```

时间均为Asia/Shanghai。观察任务每日16:00；组合任务07:30、16:00。07:30是盘前，价格使用上一交易日正式收盘并明确日期，不存在当天开盘价；休市日仅刷新有变化的新闻/政策，价格明确注明最近交易日。同日16点观察与组合使用同一pm round/lease，未取得lease的任务不重复研究或生成报告；先取得者保存一份合并日终报告。

每次激活先记录真实开始时刻并执行`begin`。返回的`round_id + lease_generation`是后续checkpoint的CAS身份，owner仅作诊断及旧调用兼容；相同owner重入不能重置时钟，到期后返回`lease_acquired=false / EXPIRED_LEASE`；不得改名伪造新activation来绕过实际租约。空checkpoint只确认当前lease、不续租；到期的显式token和旧owner-only调用均返回`STALE_LEASE`。新激活遇到未过期live lease时，`begin`正常返回`lease_acquired=false / LIVE_OWNER`并立即静默结束，而不是把Scheduled任务打成失败；同一目标轮已`COMPLETE`时返回`ALREADY_COMPLETE`并同样不重复研究/生成报告。旧轮释放或租期到期后，续接保持原round_id和已有进度、递增lease_generation，并把活动状态恢复为`PENDING`，使本activation的中途checkpoint/提交前lease校验不会因继承旧`PARTIAL`状态而再次释放lease；不因跨日或平台retry另起平行轮。一个slot只保留当前轮次和必要checkpoint，非累积流水。`COMPLETE`表示本轮覆盖完成，不等于正式投资决策已完成。

### 每轮内容与证据

先恢复本地持仓投影、当前研究事实、待处理事件；未发生的交易不导入，不把用户的“我会买入”视为成交。执行适用canonical Skills：continuous-investment-monitor、company-deep-research、financial-integrity-audit、industry-value-chain、catalyst-event-research、governance-management-quality、macro-policy-regime、investment-red-team、portfolio-manager。增量只重算受影响模块，不能每轮全市场扫描或整库蒸馏。

10+观察每日覆盖：收盘价、涨跌幅、成交额/换手与量能、估值变化；公司正式公告与财报；行业、上下游与政策；增量对原研究逻辑的加强/削弱/不变；尚缺证据与下次检查条件。关键事实回到交易所/CNINFO/发行人/监管官方源，多源交叉。新闻采集失败写清“本轮未完整核验”，不能写“无新闻”。旧半年报数据必须重新核验，未披露的报告不编造。

行情正式数据口径（2026-10-09增量）：单股`sync-daily`生成的`DAILY_UNADJUSTED`当日可追溯release代表项目已登记该交易日时点行情，不能在缺少`OHLCV_SECONDARY_VALIDATED`时进一步声称交易所官方或独立双源确认；Sina等来源没有交易额时不以收盘价乘股数冒充真实成交额。原生整手（100股）来源与逐股来源比对时，按`configs/market_reference.yaml`具名路由的`daily_volume_quantum_shares`声明核验整数份额，保留完整OHLC/身份/日期/复权/有值成交额严格比较，容忍量纲舍入仅限小于原生半个量化单位并显式留`OHLCV_SECONDARY_VOLUME_QUANTIZED`。源熔断或未抓到有效双源时仍按单源登记并说明缺口，不伪造双源确认。按本轮有效证据更新本地`latest-am.md`，但报告文件不代替正式已登记的研究工件。

深研按当前成员分片推进并保存已核验证据ID：最新PE/PB/现金流估值及其适用性、可获得区间内的历史估值分位（明确窗口、亏损/异常值处理）、公司指引与有来源一致预期修正、交易活跃度/机构覆盖等关注度代理。市场聚焦AI不自动证明低估；并表、授权收入、汇兑、低基数与一次性收益单独拆分；医药同品规同税率净价/销量分别验证。

### 三季报停止与组合转换

必须逐家公司核对**报告期截至2026-09-30的正式第三季度报告**及实际披露时间。预约披露日、业绩预告、快报、分析师预测均不等价。保留所有已登记公司本轮已核验的报告source_snapshot_id / Evidence / 财务工件引用，来源冲突或缺报时继续观察，不能到10月31日机械宣布齐全。缺少原件时优先触发现有Tushare财务三表＋CNINFO官方季度报告枚举，正常取件和认证无须等待人工登录或财报“自然成熟”。`recover-q3`不设每轮公司数量上限，依据当前注册成员的真实缺口持续尝试；使用现役600秒默认时间预算、实际传输调用次数预算和租约，耗尽时保存最后尝试的证券游标供下一代恢复。已认证的不再补采，失败者不会永久占据队头；旧 `attempted_members` checkpoint 可向后兼容，新的定长游标不随成员数膨胀。实际取件与财报字段认证仍分开记录。2026-11-22当前18/18已经认证且PM取件轮完成，正式组合研究仍单独进行。

全部观察公司正式财报齐全后，使用前三季度累计减半年报推导Q3单季（合并/单位/重述口径一致），完成各股财务质量、经营驱动、估值、独立多空/Reviewer/Red Team、Committee与Portfolio和正式RecommendationResearchReceipt/Publication Gate。仅从当前全部观察成员中形成不超过5只的组合，允许少于5只乃至暂不配置，不强行凑数；说明权重、现金、行业集中度、相关风险、买入条件与失效/退出条件。预算/风险参数优先已核实用户记录；未知则明确规划假设，不伪造账户事实。

机器转换入口为 `watch_campaign readiness --receipt-id <RecommendationResearchReceipt>` 与 `watch_campaign finalize --receipt-id <RecommendationResearchReceipt>`。`finalize` 只在以下条件同时成立时把 campaign 从 `OBSERVING` 转为 `PORTFOLIO_MONITORING`：所有当前成员各自存在报告期 `2026-09-30`、`QUARTERLY`、`CERTIFIED` 的 v2 财务 release，且 lineage 必须来自 CNINFO 完整枚举或正式官网 exact-item admission（recorded fixture/legacy lineage 不算）；正式 RecommendationResearchReceipt 必须通过 canonical replay，候选全集精确等于本 campaign 当前全部成员，Publication 为正式 `PUBLISH` 且允许发布，最终正权重标的必须是观察池子集、去重且 1–5 只。readiness 只读，条件不满足时保持 `OBSERVING`；finalize 只保存 release/receipt 引用，不复制财务或组合事实，也不写账户/订单。真实季报尚未披露时，测试夹具只能证明门禁行为，不能把 campaign 提前转换。

最终组合报告实际形成并通过发布后，停止“10+三季报观察”对应platform任务；盘前/盘后组合任务保留，目标从正式Portfolio工件读取，不另建持仓事实。还未选出组合时，组合任务只消费共享观察进度，不给虚构的组合盈亏。用户确认实际买入后才使用canonical external-account导入/快照入口保留买入时价格、估值、逻辑、原件引用与风险条件，不自动创建交易。

### 实际宿主中断与同轮续接

`configs/chat_invocation_policy_v1.yaml`保留兼容路径，当前v2只配置交接/绑定预留及续接频次。仅实际观测到的平台期限、中断、用户取消或资源预算耗尽触发受控收口；不得因运行超过35/40/45分钟或完成比例不足而机械早停。预留量围绕真实宿主期限计算，不反推项目自设总时限。保存同一round已核验证据和checkpoint，核对真实续接绑定后按合同释放lease；OTHER_AGENT不继承Chat任务计划，实际中断仍需保存恢复点。未完成研究正文不冒充正式报告。

Scheduled automation运行时不动态创建另一条automation。本campaign使用预先绑定的每小时continuation watcher：新round自动继承该opaque task ID，watcher只续接同一未完成round，不重新抓已满足来源；周期最高每小时一次，不能承诺亚小时周期。交互式ChatGPT会话只有在平台明确允许时才可创建新的子Scheduled；预绑定/平台权限失败必须写入断点，不能把提示词或本地配置冒充已创建任务。云平台在收口前中断时，按已落地 checkpoint 恢复剩余研究和报告生成，不假定平台永不 retry。

通过JSON输入保存checkpoint（最多64KiB；内容覆盖，不追加历史）。新调用必须使用`begin`返回的完整lease token：

```
.venv\Scripts\python.exe -B -m astock.investor_orchestration.watch_campaign checkpoint --slot pm --round-id <begin返回round_id> --lease-generation <begin返回lease_generation> --input runtime/watch-campaign/checkpoint.json
```

字段：checkpoint（已完成证据/缺口/下一节点，最多12000字）、completed_units/total_units、child_task_id、report_file、status（PENDING/PARTIAL/COMPLETE）。`round_id`与`lease_generation`必须成对传入；二者匹配且lease仍活动时才应用写入。过期、已释放或已被新activation接管的旧lease返回`checkpoint_applied=false / STALE_LEASE`，不覆盖当前进度、不把调用打成异常，也不得继续覆盖报告。旧的owner-only调用暂保留兼容，但owner字符串不再是新流程的并发正确性凭据。真实证据决定完成单位；PARTIAL/COMPLETE释放lease。交互式会话临时创建的子Scheduled只在其逻辑请求完成后停自身；本campaign预绑定的每小时continuation watcher不是临时子任务，单个am/pm round完成时不得停用，只有campaign及相关日常跟踪明确结束时才停。

### 报告投影与有界留存

研究 checkpoint 与报告投影分别处理：保存证据、进度及 PARTIAL 释放不依赖历史报告文件存在；只有显式新增或重新登记 `report_file` 时才校验项目内的真实文件。文件生成或覆盖失败仅影响本轮报告投影，不清空已完成的研究，也不重跑已核验的来源。

`checkpoint_applied=true` 仅表示本轮元数据写入成功，并非跨文件操作的事务锁或报告发布许可。正文可以覆盖保存至 `runtime/watch-campaign/latest-am.md` 或 `latest-pm.md`；正式投资结论仍须由现有完整研究链与发布门认证。16点的观察和组合任务共享同一 pm round，并只保存一次合并报告。

当前md覆盖维护；基本面/新闻摘要按当前有效证据增量替换，不把全文天天累加。高频重算中间态、成功pytest目录立即清理；过期失败/孤儿缓存由原retention白名单回收，包括注册worktree内部quality-runs/tmp。买入快照、账本、当前引用的官方原件/正式receipt不是垃圾；失效蒸馏/未引用衍生对象只能经原canonical引用审计与GC删除，不用rmtree删整个ObjectStore。旧原件需要维持精简的历史估值/财务比较所需数据，不复制整套原件为“每日备份”。

## Stop conditions

- 单个日常 round 在当轮增量、报告写入与必要 checkpoint 完成后停止。
- 未完成 round 仅通过同一 round 的受控续接恢复，不创建平行研究身份。
- 10+观察仅在所有已登记公司正式三季度报告均核验、正式研究链和不超过5只组合报告均完成后停止。
- 盘前/盘后组合跟踪在观察阶段结束后继续，直到用户另行停止。
