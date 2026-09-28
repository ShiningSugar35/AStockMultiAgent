# Workflow: 十股三季报观察与组合跟踪

> 状态：CURRENT（执行工作流；各能力的真实启用/送达以运行回执为准）
> 更新：2026-09-28
> 当前实现：watch_campaign 元数据接口、既有研究/QQ传输；定时创建、专项回归和端到端送达状态见本次进度验收，不能从本文推断已全部验收。
> 研究范围：用户明确指定的十只A股；不宣称全市场选股；不下真实或模拟订单。

## When to use

用于用户明确指定一组A股，要求持续观察至指定财报全部披露，并在正式研究闭环后形成受限组合，同时保留盘前/盘后增量跟踪和可恢复的ChatGPT激活边界。此Workflow不用于全市场荐股，也不授权订单。

## Flow

### 身份、日程和恢复

canonical campaign=`ten-stock-q3-2026`，元数据保存在现有SQLite checkpoint，成员从ResearchSubjectRegistry中同一request_id投影。禁止把聊天里的旧研究数字当事实。运行入口（工作目录D:\AStockMultiAgent）：

```
.venv\Scripts\python.exe -B -m astock.investor_orchestration.watch_campaign status
.venv\Scripts\python.exe -B -m astock.investor_orchestration.watch_campaign begin --slot pm --bucket YYYY-MM-DD --owner 本次唯一激活ID
```

时间均为Asia/Shanghai。观察任务每日16:00；组合任务07:30、16:00。07:30是盘前，价格使用上一交易日正式收盘并明确日期，不存在当天开盘价；休市日仅刷新有变化的新闻/政策，价格明确注明最近交易日。同日16点观察与组合使用同一pm round/owner，未取得所有权的任务不重复研究或推送；先取得者输出一份合并日终报告。

每次激活先记录真实开始时刻与唯一owner。相同owner重入不能重置时钟；旧轮未完成即续接旧round_id，不因跨日或平台retry另起平行轮。一个slot只保留当前轮次和必要checkpoint，非累积流水。不同owner仅在旧owner释放/租期到期后接管。`COMPLETE`表示本轮覆盖完成，不等于已完成选股或已送达QQ。

### 每轮内容与证据

先恢复本地持仓投影、当前研究事实、待处理事件；未发生的交易不导入，不把用户的“我会买入”视为成交。执行适用canonical Skills：continuous-investment-monitor、company-deep-research、financial-integrity-audit、industry-value-chain、catalyst-event-research、governance-management-quality、macro-policy-regime、investment-red-team、portfolio-manager。增量只重算受影响模块，不能每轮全市场扫描或整库蒸馏。

十股观察每日覆盖：收盘价、涨跌幅、成交额/换手与量能、估值变化；公司正式公告与财报；行业、上下游与政策；增量对原研究逻辑的加强/削弱/不变；尚缺证据与下次检查条件。关键事实回到交易所/CNINFO/发行人/监管官方源，多源交叉。新闻采集失败写清“本轮未完整核验”，不能写“无新闻”。旧半年报数据必须重新核验，未披露的报告不编造。

深研按十股分片推进并保存已核验证据ID：最新PE/PB/现金流估值及其适用性、可获得区间内的历史估值分位（明确窗口、亏损/异常值处理）、公司指引与有来源一致预期修正、交易活跃度/机构覆盖等关注度代理。市场聚焦AI不自动证明低估；并表、授权收入、汇兑、低基数与一次性收益单独拆分；医药同品规同税率净价/销量分别验证。

### 三季报停止与组合转换

必须逐家公司核对**报告期截至2026-09-30的正式第三季度报告**及实际披露时间。预约披露日、业绩预告、快报、分析师预测均不等价。保留十家公司本轮已核验的报告source_snapshot_id / Evidence / 财务工件引用，来源冲突或缺报时继续观察，不能到10月31日机械宣布齐全。

十家正式财报齐全后，使用前三季度累计减半年报推导Q3单季（合并/单位/重述口径一致），完成各股财务质量、经营驱动、估值、独立多空/Reviewer/Red Team、Committee与Portfolio和正式RecommendationResearchReceipt/Publication Gate。仅从这十股中形成不超过5只的组合，允许少于5只乃至暂不配置，不强行凑数；说明权重、现金、行业集中度、相关风险、买入条件与失效/退出条件。预算/风险参数优先已核实用户记录；未知则明确规划假设，不伪造账户事实。

机器转换入口为 `watch_campaign readiness --receipt-id <RecommendationResearchReceipt>` 与 `watch_campaign finalize --receipt-id <RecommendationResearchReceipt>`。`finalize` 只在以下条件同时成立时把 campaign 从 `OBSERVING` 转为 `PORTFOLIO_MONITORING`：十个成员各自存在报告期 `2026-09-30`、`QUARTERLY`、`CERTIFIED` 的 v2 财务 release，且 lineage 必须来自 CNINFO 完整枚举或正式官网 exact-item admission（recorded fixture/legacy lineage 不算）；正式 RecommendationResearchReceipt 必须通过 canonical replay，候选全集精确等于本 campaign 十股，Publication 为正式 `PUBLISH` 且允许发布，最终正权重标的必须是观察池子集、去重且 1–5 只。readiness 只读，条件不满足时保持 `OBSERVING`；finalize 只保存 release/receipt 引用，不复制财务或组合事实，也不写账户/订单。真实季报尚未披露时，测试夹具只能证明门禁行为，不能把 campaign 提前转换。

最终组合报告实际形成并通过发布后，停止“十股财报观察”对应platform任务；盘前/盘后组合任务保留，目标从正式Portfolio工件读取，不另建持仓事实。还未选出组合时，组合任务只消费共享观察进度，不给虚构的组合盈亏。用户确认实际买入后才使用canonical external-account导入/快照入口保留买入时价格、估值、逻辑、原件引用与风险条件，不自动创建交易。

### 30分钟收口 / 35分钟激活上限

`configs/chat_invocation_policy_v1.yaml`为唯一阈值配置。CHATGPT_CHAT每次激活1800秒起停止启动新长任务；2100秒前保存本轮已验证部分及缺口，并在已有用户授权且通知通道可验证时提交当前结果，然后释放owner。进度达到2/3或99%仍不能延长。OTHER_AGENT不继承总体截断。

Scheduled automation运行时不动态创建另一条automation。本campaign使用预先绑定的每小时continuation watcher：新round自动继承该opaque task ID，watcher只续接同一未完成round，不重新抓已满足来源；周期最高每小时一次，不能承诺亚小时周期。交互式ChatGPT会话只有在平台明确允许时才可创建新的子Scheduled；预绑定/平台权限失败必须写入断点，不能把提示词或本地配置冒充已创建任务。云平台在收口前中断时无法保证本次推送，此时从已落地checkpoint恢复，不以提示词保证平台永不retry。

通过JSON输入保存checkpoint（最多64KiB；内容覆盖，不追加历史）：

```
.venv\Scripts\python.exe -B -m astock.investor_orchestration.watch_campaign checkpoint --slot pm --owner 本次唯一激活ID --input runtime/watch-campaign/checkpoint.json
```

字段：checkpoint（已完成证据/缺口/下一节点，最多12000字）、completed_units/total_units、child_task_id、report_file、submit_status、status（PENDING/PARTIAL/COMPLETE）。仅当前owner可写；真实证据决定完成单位。PARTIAL/COMPLETE释放owner。交互式会话临时创建的子Scheduled只在其逻辑请求完成后停自身；本campaign预绑定的每小时continuation watcher不是临时子任务，单个am/pm round完成时不得停用，只有campaign及相关日常跟踪明确结束时才停。

### QQ推送与有界留存

正文保存固定路径`runtime/watch-campaign/latest-am.md`或`latest-pm.md`，覆盖写入。原有命令：

```
.venv\Scripts\python.exe -B -m astock investor qqbot-stock-submit runtime/watch-campaign/latest-pm.md
```

只发送研究/观察结果，不代发订单。QQBot现有订阅负责目标群与最终送达，LLM不接管。SSH的SENT仅代表inbox接收；只有QQBot实际delivery回执才可说已推送到群。UNKNOWN不盲目重发；核对远端既有inbox/delivery再恢复。16点两任务共用round，并只由owner提交一次正文。

当前md覆盖维护；基本面/新闻摘要按当前有效证据增量替换，不把全文天天累加。高频重算中间态、成功pytest目录立即清理；过期失败/孤儿缓存由原retention白名单回收，包括注册worktree内部quality-runs/tmp。买入快照、账本、当前引用的官方原件/正式receipt不是垃圾；失效蒸馏/未引用衍生对象只能经原canonical引用审计与GC删除，不用rmtree删整个ObjectStore。旧原件需要维持精简的历史估值/财务比较所需数据，不复制整套原件为“每日备份”。

## Stop conditions

- 单个日常 round 在当轮增量、报告写入与必要 checkpoint 完成后停止。
- 未完成 round 仅通过同一 round 的受控续接恢复，不创建平行研究身份。
- 十股观察仅在十家公司正式三季度报告均核验、正式研究链和不超过5只组合报告均完成后停止。
- 盘前/盘后组合跟踪在观察阶段结束后继续，直到用户另行停止。
