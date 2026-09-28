# Operations SLO 与存储生命周期架构 v1

## 目标

R-03 为现有运行时补充有界存储治理和业务 SLO 视图，不建立第二套对象存储、监控数据库或调度器。事实对象、SourceSnapshot、Evidence、报告 manifest、用户状态和账本继续由既有 StateStore/ObjectStore/Artifact Registry 管理。2026-09-24 起，生命周期同时治理测试/Agent 执行现场，解决 `.ai-bridge/quality-runs`、`runtime/longrun`、`runtime/worktrees` 等目录无界增长的问题。

## 两条生命周期通道

### 正式事实通道

`storage-lifecycle-plan` 读取现有索引和受控目录，输出候选类别、字节、引用状态和原因，并把 exact plan 持久化到现有 SQLite。`plan_id` 由政策、候选内容和扫描状态计算，不包含生成时间，因此相同状态可稳定恢复。

`storage-lifecycle-audit` 从 SQLite 读取同一个 plan 并重新校验内容哈希。路径越界或“已引用但标记删除”属于阻断缺陷；达到 scan limit 只记录 `SCAN_TRUNCATED`，允许后续以有界批次继续推进。

`storage-lifecycle-run --confirm` 才删除 ObjectStore orphan、runtime/tmp、report staging/output、日志备份等文件级候选。执行前再次读取实时引用，并检查路径、文件大小和 mtime；对象后来被引用、报告后来被发布、staging 恢复为活动状态、文件被改写、文件已消失或 Windows 锁定都会跳过。

### Ephemeral 自动通道

`storage-lifecycle-auto` 只生成明确列入 `_AUTO_TREE_CATEGORIES` 的整树候选；默认 dry-run，`--apply` 才执行。它永远不把 ObjectStore、正式报告、SQLite state、Artifact/SourceSnapshot/Evidence 或用户状态放进自动删除集合。

当前自动类别：

- `.ai-bridge/quality-runs/<run>/tmp`：覆盖主工作树和已注册 `runtime/worktrees/*` 内相同精确目录；同级已有 `result.json` 时按 2 小时 TTL 删除，没有 `result.json` 的运行先保护 24 小时，超过 orphan grace 且整树不再变化时才可回收。候选扫描在测量前即拒绝 `.ai-bridge`、`quality-runs`、run root 或 `tmp` 任一层的 symlink/junction，删除前仍重复执行路径边界与整树快照校验；`result.json`、`junit.xml`、`output.log` 等小体积回执继续保留。
- `runtime/longrun` 内的 `pytest*`、`cache-*`、`process-tmp`、`tmp*`：24 小时 TTL，按 owned tree 删除；authority receipt、checkpoint、正式输出等不按目录整体删除。
- `runtime/worktrees/*`：48 小时 TTL，并且必须不在 Git 自身 `.git/worktrees/*/gitdir` 注册集合中。计划后若重新注册，执行时再次保护。
- runtime 根级 `pytest-*`、`uv-cache-*`：24 小时 TTL。
- `.ai-bridge/tmp*`：24 小时 TTL。

大规模临时数据不再逐文件制造数十万候选；计划记录整树的字节数、文件数、目录数和最新 mtime，执行前重新测量。任一指标变化即 `CHANGED_SINCE_PLAN`，整树跳过。Windows 打开句柄导致删除失败时同样跳过，不强制破坏活动任务。

## 自动调度与预算

自动清理由已有单实例 `continuous-monitor-daemon` 低频触发，不新增第二个 daemon。默认每 6 小时检查一次；单次自动删除预算为 20 GiB。cleanup 异常属于 housekeeping 故障，不允许拖垮 Continuous Monitor；下一周期从新 plan 重新审计后再尝试。

`storage-lifecycle-auto --apply` 仍可人工立即执行，适合磁盘水位告警后的受控收口。

## 保留与备份原则

仓库不再把“完整测试文件系统”当作长期备份。长期可追溯性依赖 Git、SQLite append-only 状态、ObjectStore 内容寻址对象、Artifact/报告 manifest，以及小体积测试/Agent 回执；pytest basetemp、cache、临时 worktree 和重复 runtime 副本属于可再生执行现场。

`runtime/backups` 不是当前主要占用源；备份治理重点是阻止同一测试输入在多个临时 runtime/worktree 中长期复制。ObjectStore 已按 SHA-256 内容寻址去重，不允许为了“多一份保险”复制成平行对象库。

用户选择的桌面/自定义报告目录不由生命周期自动删除。

## 水位

`configs/storage_lifecycle.yaml` 同时维护逻辑类别与卷级水位：

- runtime warning / critical：16 GiB / 32 GiB；
- ephemeral warning / critical：4 GiB / 8 GiB；
- D: 可用空间 warning / critical：40 GiB / 20 GiB；
- ObjectStore、report、runtime/tmp 保留独立 warning。

`operations-slo-report` 使用完整 tree snapshot 统计 runtime/ObjectStore/tmp/report，而不是最多 10 万文件的截断统计；水位只形成 PASS/WARN 和 finding code，不改变正式研究、推荐或模拟交易权限。

## 安全不变量

- 正式 SourceSnapshot/Evidence、Artifact、报告 manifest、账本和 user state 不属于自动删除候选。
- tree candidate 必须落在配置允许的 owned root 且名称匹配对应 pattern。
- active Git worktree、未终态 quality run 和 plan 后发生变化的树必须跳过。
- 自动通道只接受 TREE 类型的 ephemeral category；正式类别被伪造成 TREE、或 ephemeral 类别被伪造成 FILE 时 audit fail closed。
- 手工正式事实通道继续保留 plan → audit → explicit confirm，不因自动清理而放宽。
- AUDIT/EXECUTION 回执使用确定性语义 `run_id`，同一身份若字段漂移则 fail closed。

## 回滚

关闭 `automation.enabled` 即可停止 daemon 自动清理而保留 dry-run/audit；撤回 `storage-lifecycle-auto` 和 tree retention 实现后，原有 ObjectStore/report/log 生命周期仍可独立工作。0065 继续只承载生命周期 plan/audit/SLO 回执，不迁移或重写正式事实。
