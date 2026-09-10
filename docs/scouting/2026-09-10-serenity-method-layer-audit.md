# Serenity 方法层结构审计（2026-09-10）

## 结论

本轮不升级、vendoring 或直接执行 Serenity 上游源码，而是继续采用“冻结上游方法 → 本地类型化适配 → Evidence/PIT/确定性核心校验”的模式。2026-09-10 现场 `git ls-remote ... HEAD` 证明两个上游 HEAD 与当前冻结提交一致：

- `https://github.com/muxuuu/serenity-skill.git` → `c2fe93deedfd0d1bd9fe7ef0601ea1b9c20ea24a`
- `https://github.com/haskaomni/serenity-skill.git` → `dedcf8f9ca8bd48f21239456ede50a9eb9f7ecb0`

两份活动 v4 audit manifest 已冻结 MIT license、精确 reviewed files、commit 和本地 mapping；正常 runtime 不依赖上游网络，也未 vendoring 上游源码。当前主要缺口来自本地扩展后的合同不一致与重复适配成本，而不是上游版本落后。

## 候选审计

| 候选 | 当前证据 | 项目适配判断 | 决策 | 最小落地面 |
|---|---|---|---|---|
| 多 Serenity 协同 | `ResearchSkillService` 支持多 Specialist；`ResearchRunFrozenInputs/runtime_inputs/runtime` 仍只保存并要求 1 个 Serenity Delta | 能直接提高互补方法覆盖，且不需要新增 Agent swarm | `ADAPT_PATTERN` | 多值 frozen input + 旧单值兼容 + family/overlap 去重 |
| canonical artifact → Serenity compiler | runtime 仍要求调用者提供完整 `method_contract`；Phase 9、canonical D1 已存在结构化事实 | 可减少重复抄写、token 和格式错误，但不能自动生成因果/反证等语义事实 | `ADAPT_PATTERN` | 只编译机械字段；语义节点继续显式研究 |
| shared Local Adaptation Release | 两份 v4 manifest 重复绑定同一组本地 adaptation file hash | 安全性不变但维护重复；适合拆成“上游审计 + 单一本地适配 release” | `ADAPT_PATTERN` | registry-level release；v4 历史保留、v5 active |
| canonical D1 确定性均线 | `DailyTrendHealthContractV2` 仍接受 caller-supplied 20/50/100/200DMA；`CanonicalMarketStore` 已能读取 canonical bars/manifest | 数学可复算，不应交给 LLM/调用者 | `ADAPT_PATTERN` | O(n) 单证券均线 compiler，绑定 manifest/hash/as_of |
| 全量搬迁 `serenity_v2.py` | 文件接近千行，但大量历史 import、Pydantic union 与测试依赖既有路径 | 直接物理拆分收益低、回归面大 | `REJECT`（本轮全量搬迁） | 建立领域 facade、新 compiler/policy 模块；旧入口兼容 |
| runtime 自动跟随 upstream | 两个上游当前 HEAD 未漂移；生产正常运行不需要 GitHub | 会破坏可复现与审计边界 | `REJECT` | 仅显式 developer status 报告 frozen/upstream 状态，不自动升级 |

## 不可改变边界

1. Serenity/scorecard/Juglar 只产生 evidence-bound Delta 或 report-only metric；不得直接生成目标价、仓位、交易权重或订单。
2. Forecast/Valuation 数值继续以 Phase 9 Python deterministic artifacts 为唯一账本；Serenity 只能引用或形成派生视图。
3. 新 compiler 只能从已注册 artifact、canonical dataset 和 frozen evidence 读取；缺 hash、as_of、Evidence/PIT 或足够 bars 时 fail closed。
4. Committee、Portfolio、TradingClassification、paper confirmation、`broker_execution_allowed=false` 不因本轮改变。
5. normal runtime 继续零 Serenity 上游网络依赖；任何 upstream commit 变化必须形成新 audit/release，经显式审查后才可进入活动 registry。

## 性能与维护判断

- 路由集合最大 8，family/overlap 去重即使使用 O(n²) 也只是常数级小集合，不值得引入复杂图优化。
- Daily MA compiler 对单证券最多扫描 canonical D1 序列一次，时间 O(n)、额外内存 O(1)～O(窗口数)，不复制全市场事实。
- shared Local Adaptation Release 把一次本地重构由“同时维护两个 upstream manifest 的重复 hash”降为“一处 release 更新 + upstream mapping 不变”，降低误改和审计噪声。
- 物理拆分旧 `serenity_v2.py` 暂缓，避免为了代码行数制造高 churn；新能力改走 `astock.schemas.serenity` / `astock.research.serenity` 模块，待下一 contract major version 再迁移旧类。
