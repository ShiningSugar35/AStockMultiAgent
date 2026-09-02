# Operations SLO 与存储生命周期架构 v1

## 目标

R-03 为现有运行时补充有界存储治理和业务 SLO 视图，不建立第二套对象存储、监控数据库或调度器。事实对象、SourceSnapshot、Evidence、报告 manifest、用户状态和账本继续由既有 StateStore/ObjectStore/Artifact Registry 管理。

## 生命周期流程

`storage-lifecycle-plan` 只读取现有索引和受控目录，输出每个候选的类别、字节、引用状态和原因，并把 exact plan 持久化到现有 SQLite。`plan_id` 由政策、候选内容和扫描状态计算，不包含生成时间，因此相同状态可稳定恢复。

`storage-lifecycle-audit` 从 SQLite 读取同一个 plan 并重新校验内容哈希。路径越界或“已引用但标记删除”属于阻断缺陷；达到 scan limit 只记录 `SCAN_TRUNCATED`，允许后续以有界批次继续推进，避免积压越大越无法清理。

`storage-lifecycle-run --confirm` 才执行删除。执行前再次读取实时引用，并检查路径、文件大小和 mtime：对象后来被引用、报告后来被发布、staging 后来恢复为活动状态、文件被改写、文件已消失或 Windows 锁定都会跳过而不是强删。每次执行写入审计回执。

## 被治理的受控存储

- ObjectStore：仅超过保留期且无任何数据库 hash 引用的对象候选。
- runtime/tmp：按小时 TTL、有界扫描。
- report staging：超过 TTL 且没有活动 report checkpoint 的文件候选。
- runtime report output：超过保留期且不再被 report manifest 引用的输出候选。
- operational log backup：超过保留期且不是活动日志的文件候选。

用户选择的桌面/自定义报告目录不由该自动生命周期删除；这里只治理项目可证明拥有的 runtime 报告目录。

## 安全与有界性

- 默认只 plan/audit，不删除；删除必须显式确认。
- 每类目录有独立 scan limit，数据库 hash 扫描也有上限。
- 正式 SourceSnapshot/Evidence、Artifact、报告 manifest、账本和 user state 只作为引用事实读取，不属于文件删除候选。
- scan truncation 支持批次进展；候选路径或引用冲突则 fail closed。
- Windows 文件锁、mtime/size 漂移和并发引用变更安全跳过。
- 日志自身继续由 logging policy 负责轮转；R-03 只清理过期备份并汇总水位。

## Operations SLO

`operations-slo-report` 从现有表和有界目录汇总：

- 正式 Universe coverage proof 数量；
- Provider degraded/open circuit；
- 最近成功 SourceSnapshot 的可得时间与证据新鲜度；
- 正式 research route 数量；
- 报告总量、成功率和 recovered 数量；
- Continuous Monitor backlog、failed/partial、平均运行时间与恢复时间；
- Skill token cost 与单位 research route token；
- runtime/ObjectStore/tmp/report bytes 及可选基线增长量。

报告只给 PASS/WARN 和稳定 finding codes，不改变正式研究、推荐或模拟交易权限。

## 回滚

关闭生命周期 `run` 不影响 plan/audit/SLO；删除 `storage_lifecycle.yaml` 的调用入口或撤回 CLI 注册即可恢复到只使用既有 logging/report/ObjectStore 行为。0065 仅增加生命周期 plan/audit/SLO 回执表，不迁移或重写既有事实。
