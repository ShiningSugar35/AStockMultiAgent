# Serenity 方法层架构 v1

> 状态：CURRENT
> 是否已经实现：是
> 更新日期：2026-09-10
> 活动注册表：`configs/research_skills.yaml` / `research-skills-v4`

## 1. 定位

Serenity 是 AStockMultiAgent 的证据约束型 Specialist 方法层，不是独立交易系统、第二套估值系统或额外投委会。项目不 vendoring、也不在正常运行时执行两个上游仓库源码；只把冻结并审计过的方法映射成本地 typed contracts，再由现有 Research Runtime、Evidence/PIT、Committee 与 Portfolio 链消费。

当前上游为 `muxuuu/serenity-skill` 与 `haskaomni/serenity-skill`。活动上游审计为 v5，二者共享 `third_party/audits/serenity/local-adaptation-release-v1.json` 证明本地适配文件身份；历史 v4 manifest 保留用于 provenance，不作为当前活动注册表。

## 2. 当前机器合同

- `configs/research_skills.yaml`：活动 Skill registry、`source_family`、`selection_group`、资源预算与 family limit。
- `src/astock/schemas/serenity_v2.py`：历史兼容的 typed method contracts；新代码不再继续向该单体文件堆叠。
- `src/astock/schemas/serenity/`：Serenity 新领域 facade 与 compiler schema。
- `src/astock/research/serenity/`：确定性输入编译、Evidence/PIT policy 与 CLI。
- `src/astock/research/runtime.py` / `runtime_inputs.py`：多 Serenity Delta frozen recovery 与旧单值兼容。
- `src/astock/research/open_source_audit.py`：上游 audit 与共享 Local Adaptation Release 防漂移验证。

## 3. 多方法协同

同一 BaseCase 可以在既有 Specialist 资源预算内选择多个互补 Serenity Delta。路由同时执行三层约束：已有 `incompatible_skills`、`selection_group` 去重，以及 `source_family_limits`。当前 `SERENITY` family 上限为 2，避免增长、估值、趋势等相关方法机械堆叠；显式请求违反这些约束时 fail closed。

历史 `serenity_delta_artifact_id` 单值 frozen input 继续兼容；新的 canonical 字段为 `serenity_delta_artifact_ids`。多个 Serenity Delta 可以共同进入 Research Memo，但不新增 Committee 投票权，也不改变交易分类或 Portfolio 权限。

## 4. 确定性输入编译

`SerenityInputCompiler` 只自动生成能够从既有 canonical facts 机械复算的字段：

- 20/50/100/200 日均线由同一 canonical D1 数据集直接计算，绑定 market/symbol/adjustment mode、quality report、content hash、as-of 与 frozen evidence；少于 200 根、质量失败、未来数据、hash/identity 漂移或 PIT 不合格均拒绝生成。
- `FundamentalModelBundle` / `ValuationPack` 只建立 artifact/hash binding，不复制 Forecast/Valuation 数值，不形成 Serenity 自己的平行估值账本。

产业必要性、事件因果、增长假设、反证、迁移信号等语义研究仍必须由研究链显式形成并绑定证据，compiler 不推断或补写这些事实。

## 5. 开源治理

活动 v5 upstream manifest 负责冻结 repository、commit、MIT license、reviewed files、local mappings 与 adaptation decision；共享 `LocalAdaptationRelease` 单独冻结当前本地实现文件 hash。这样一次本地安全重构只需更新一个共享 release，而不会在两个 upstream manifest 中重复维护同一套本地 hash。

`open-source-audit-status` 是当前离线审计入口。正常 runtime 保持 `normal_runtime_network_required=false`、`source_vendored=false`；上游 commit 变化只能触发开发审计/升级决策，不能自动下载、替换 contract、改变权重或取得生产权限。

## 6. 永久边界

Serenity/scorecard/Juglar 输出只能是 evidence-bound Delta 或 report-only metric，不能直接产生目标价、仓位、交易权重或订单。所有 method-node evidence 继续受 frozen Evidence Pack、Evidence Grade、PIT、validity window 和 conflict gate 约束。

Forecast/Valuation 的唯一数值账本仍是 Phase 9 Python deterministic artifacts；Committee、Portfolio、TradingClassification、paper confirmation 与 `broker_execution_allowed=false` 不因 Serenity 改变。任何 compiler、审计或多 Delta 恢复失败都只能降级到显式研究/NEEDS_INFO，不能放松这些硬门。

## 7. 性能与回滚

路由集合规模很小，family/group 冲突检查为小常数 O(n²)，不值得引入额外图基础设施。日均线 compiler 单证券扫描 O(n)，只读取已有 canonical bars，不复制全市场事实；正常路径不增加上游网络 I/O。

回滚时可关闭 compiler/autoresolve 并继续接受旧单 Serenity frozen input；v5 活动审计可切回仍保留的 v4 历史 manifest。回滚不得删除历史 artifact、原始 Evidence 或 ObjectStore 内容。

## 8. 验收入口

核心回归覆盖 `tests/unit/test_open_source_audits.py`、`tests/integration/test_research_core.py`、`tests/integration/test_research_diagnostics.py`、`tests/integration/test_research_runtime_complete.py`。发布级验收仍服从 `AGENTS.md` 的 L3 工作流：专项测试之后必须在最终冻结树执行 Ruff、Pyright、文档合同、全仓 pytest、`git diff --check` 与 defect-first Review。
