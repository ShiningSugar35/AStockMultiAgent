# 技能驱动候选发现架构 v1

> 状态：CURRENT
> 是否已经实现：是；当前机器合同、运行入口与 WP-42 受控验收一致。
> 更新日期：2026-09-21
> 机器合同：`src/astock/candidates/discovery_*.py`、`src/astock/schemas/discovery_runtime.py`、`src/astock/candidates/seeds.py`、`src/astock/candidates/promotion.py`。
> 配置：`configs/research_skills.yaml`、`configs/candidate_scan.yaml`。
> Workflow / Skill：`docs/workflows/workflow-candidate-discovery.md`、`.agents/skills/candidate-scan/SKILL.md`。

## 1. 当前能力边界

当前候选发现保留市场盲选、质量价值、行业广度与既有候选复用，同时增加一条**证据约束的技能命题发现通道**。该通道只负责提出“值得进一步研究”的公司—命题，不产生 BUY/SELL、仓位、目标价、Committee 决策或模拟交易权限。

历史视觉/关键词 Expert overlay 已退出生产 Seed 准入。新技能发现只能经当前 audited/semantic registry、复核后的方法合同、公司事实与不可变评估工件生成 `VerifiedDiscoverySeedRelease`，并以精确 `VERIFIED_DISCOVERY_THESIS` 标记进入既有 Seed 预算。泛化的 `EXPERT_SKILL`、作者热度、行业关键词或旧视觉成员均不能取得同等权限。

## 2. 方法与事实层

### 2.1 方法库存

`knowledge/discovery_catalog.py` 与 `knowledge/discovery_review.py` 将现役审计技能整理为可执行发现目录；退役成员不会因旧 release、作者名或同义文本重新启用。语义条件保留适用范围、前置条件、条件族、反证与来源 lineage；同义方法和同源事实不会按“技能数量”重复计票。

当前语义命题运行时实现三条主通道：

- `BOTTLENECK`：必要性、供应稀缺、替代难度、公司暴露、利润捕获、反证；
- `EVENT`：已发生事件、需求传导、公司暴露、财务传导、验证窗口、反证；
- `CYCLE`：需求、价格、库存、产能、资本开支、周期阶段、公司经营变化、反证。

质量/价值、现金分红及行业适配继续通过共享基本面特征与既有 Seed 路径承担，不把全部投资逻辑塞进一张万能语义评分表。

### 2.2 条件状态

发现条件使用四态语义：`SATISFIED / NOT_SATISFIED / UNKNOWN / NOT_APPLICABLE`。只有已经复核并有登记 Evidence 的正/负条件可以写入 canonical Claim；未知和不适用不能伪造成 0 分或通过。

`DiscoveryRuntimeService.admit_condition_claim` 是唯一条件准入边界。以下字段从复核方法合同确定性派生，扩展 metadata 不能覆盖：

- `condition_state`
- `thesis_family_id`
- `condition_family`

事件发生必须是事实型 Claim；反证复核必须是显式 inference，并用布尔 `material_refutation_found` 表示是否发现实质反证。

## 3. 证据与发布 lineage

运行时按以下链路发布：

```text
registered SourceSnapshot / Evidence
  → reviewed discovery Claim
  → DiscoveryThesisEvaluation
  → VerifiedDiscoverySeedRelease
  → ResearchSeedReport
  → Promotion
  → Candidate
```

每个 `DiscoveryThesisEvaluation` 绑定：

- 当前 audited/semantic registry object hash；
- 当前方法合同 hash；
- 公司、行业与业务模型；
- 条件状态、Evidence 引用与 dependency fingerprint。

`VerifiedDiscoverySeedService.publish` 不接受“任意已登记来源工件”代替命题评估。发布时必须为每个声明的 `DiscoveryThesisResult` 找到**精确匹配且已注册的** `DiscoveryThesisEvaluation`；对象不存在、hash 不可验证、registry/方法合同不匹配或 result 内容不一致均拒绝发布。

Release 本身只允许 discovery-only `EXPERT_SKILL` Seed，且必须同时携带精确 verified marker、命题身份、发现通道和复核方法 identity。它仍明确声明 recommendation/candidate-write/paper-ledger 权限为 false。

## 4. 缺口恢复

`discovery_cli refresh` 对缺少必要事实的条件返回恢复计划，而不是直接把公开数据缺失写成终局 UNKNOWN。顺序固定为：

1. 本地已登记证据；
2. 当前研究采集能力；
3. 同能力 Provider fallback；
4. 权威 Web/Search 与官方原文冻结。

恢复后的公开事实先进入不可变快照/Evidence，再通过 Claim 准入重跑原命题。只有有界公共渠道真实耗尽，才允许受影响条件保持未知；无关命题不连带失败。

## 5. Seed 预算与旧入口退役

全局预算继续使用既有 80/60/8–12–16 约束：

- Seed 总数最多 80；
- Promotion 最多 60；
- 市场盲选最多 40；
- 质量价值最多 16；
- 行业广度最多 12；
- verified discovery 最多 8；
- 既有 Candidate 复用最多 4；
- 深研仍按 LOW/STANDARD/HIGH = 8/12/16。

`merge_discovery_seed_tranche` 保留盲选市场成员在前，同时把已验证发现放在其后的优先席位，避免证据充分的新命题被结构性挤出 Promotion@60。泛化 `EXPERT_SKILL` 不会因此升级；历史 Expert-domain 逻辑只保留只读审计兼容，不再生成新 Seed。

## 6. Candidate 贯通

verified discovery Seed 可与同公司其它合法 origin 合并，但公司只占一个 Seed/Promotion/Candidate 身份。命题 marker、方法引用、Evidence snapshot 和 release lineage 随 Seed 报告进入审计；Promotion 与 Candidate 仍执行自身身份、数据质量、流动性与研究充分性边界。发现命题只提高研究优先级，不能绕过深研、Red Team、Committee、组合与 Publication Gate。

## 7. 增量与性能

方法编译绑定现役 registry；静态方法未变化时不重复全库编译。公司事实和命题评估按对象/hash 复用，同一公司多通道命中不重复创建公司身份。运行时不执行“证券数 × 全技能数”的自由模型扫描。

2026-09-21 WP-42 当前冻结树采用 **clean v3** 受控验收。早先 v2 工件因物化脚本在构造 B 臂前读取已揭盲标签，存在标签泄漏，已明确失效，不再作为质量结论或性能验收依据。clean v3 严格执行“参数冻结 → A/B 物化并冻结 hash → 独立盲审揭盲 → 统一评分”：

- 60 个预注册公司—命题样本，5 个命题族各 12；20 个 holdout；v3 同时排除此前已揭盲 v1/v2 证券；
- GeminiAgentBridge 独立 Flash reviewer 在 A/B hash 冻结后才评审 60/60 样本，holdout 标签分布为 0=12、1=2、2=6；
- 原路径 A 对 holdout label-2 的 Promotion@60 召回 0/6；新路径 B 召回 1/6，提升 16.67 个百分点；
- B 臂已验证命题精确率 1/1=100%，新增错误命题 0；质量门 PASS；
- 10 次冷路径 P90 8.021 秒，10 次热路径 P90 0.847 秒，热规则合并 P90 0.000059 秒；局部性能门 PASS；
- 当前稳定树 30 场景受控总请求 30/30 终态、22/22 正常场景有用交付、8/8 故障合同通过；P50 9.429 秒、P75 11.723 秒、P90 14.192 秒、最大 26.310 秒；总请求门 PASS。

上述性能是本地已登记/录制域与现有失败验收的受控墙钟；**首次官方原始网络抓取成本单独记录，不包含在局部冷/热 P90，也不把该结果宣传成公网冷启动延迟或投资收益证明。**

## 8. 回滚与限制

发现质量退化、错误命题、退役技能冒用或预算突破时，关闭 verified discovery tranche 即回到同 Universe 的既有发现路径；不得恢复旧视觉/关键词 Expert 准入。

本能力不证明未来收益，不使用未来收益标签，不新增回测/PIT/券商权限门，也不替代正式公司深研。权威数据、证据身份、单位/会计一致性、隐私和账本不变量继续由既有机器合同负责。
