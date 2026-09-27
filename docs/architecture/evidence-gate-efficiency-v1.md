# Evidence gate efficiency and native document recovery v1

> 状态：CURRENT
> 是否已实现：是；机器合同、迁移、解析/准入/恢复实现与回归测试已落地
> 对应工作包：WP-49～WP-53（已验收并从活动计划移除）
> 更新日期：2026-09-27

## 1. 范围与不变量

本设计收敛当前研究链路中重复、与真实证据无关或粒度过粗的门禁，同时保留真正影响投资结论可靠性的硬约束。它不建立第二套证据库、Provider Registry、事实账本或发布通道，也不放宽公司/证券身份、报告期、合并/母公司口径、币种/单位、来源权威性、原始对象 hash、完整性证明、publication/committee/execution 等正式边界。

Search/发现结果不能证明“没有公告”，单个 exact-item admission 不能冒充 exhaustive enumeration；摘要不能冒充全文，非完整财务子集不能冒充完整财务或估值输入。任何复用证据若对象缺失、绑定矛盾、身份冲突或版本失配，仍然 fail closed。

权威机器合同仍由 src/astock/schemas、StateStore、ObjectStore、versioned config、迁移和测试承担；本文只描述当前边界。

## 2. 捕获身份：内容 canonical 与抓取观察分离

migrations/0079_source_snapshot_observations.sql 为已有 snapshot 增加两层事实。source_snapshot_alias 把调用者请求的 snapshot id 解析到由 source_id + object_hash 决定的 canonical snapshot；source_snapshot_observation 以 append-only 观察保存每次请求身份、时间、availability、URL、MIME、大小、header hash、抓取状态和 rights 状态。

StateStore.register_snapshot() 在一个事务内完成 canonical 注册、alias 校验和 observation 记录，并返回 canonical SourceSnapshot。相同源、相同内容的重复提交不会再因为第二个逻辑 ID 引用不存在的父 snapshot 触发 FK 错误，也不会用后一次观察覆盖先前 URL/时间。alias 碰撞、大小不一致和 canonical detail 缺失仍显式失败。

所有下游 repository 在建立 FK 前必须消费 canonical snapshot；不得关闭外键、吞 IntegrityError、用 REPLACE 删除父对象或重写历史 snapshot/hash。

## 3. 官方文档：业务类型、报告身份和准入角色分开

OfficialWebDocumentCapture 继续把 document_type 作为业务类别，而不是把文件扩展名塞进业务类型；同时记录 media_type、parser/version、period_end、document_completeness、revision_status。official-web-admission-v2 把原文对象与 admission 对象明确分角色，并绑定 document id、canonical document snapshot、原文 hash、报告期和完整性。

报告身份匹配采用“归一化后匹配 + 非主报告前缀拒绝”。允许空白/标点、常见年度/半年度/季度别名和修订/更正版本，也识别 Q1/Q3 英文别名；但“关于延期披露…报告”“独立董事关于…报告”“取消/撤回/审议/问询/回复/决议/提示”等包装公告不得被当作主报告。正文/可信元数据的公司身份、期间和 FULL/SUMMARY 仍分别验证。

v1 admission 保持只读兼容；v2 消费者额外核对原文 hash、期间、完整性与修订状态。兼容读取不是降低新记录的准入要求。

## 4. 原生多格式解析与可定位证据

官方文档按内容识别实际格式，而不是只信 URL/扩展名。当前正式接纳面包括 PDF、DOCX、TXT，以及在受限解析器真正可用时的 legacy DOC。

PDF 保留 page locator、原生文本/OCR 状态和 parser version；DOCX 使用受限 ZIP/XML 解析，拒绝宏启用格式和资源越界，生成 native blocks；TXT 只有在字节确为文本且不是 HTML/XML/RTF 片段时接纳，并保留行/块定位。legacy DOC 解析能力缺失时返回该格式解析不可用，不得把 DOC 伪装成 DOCX/PDF 或调用 Office 宏。

reflow_parser 为 DOCX/TXT 生成 immutable block text objects；财务认证在兼容页文本中定位数值后会映射回原生 block/page locator，再创建正式 evidence。解析缓存按原始对象、parser/version 与范围复用，并在返回缓存前验证缓存引用对象仍存在。缓存可丢弃重建，不是正式来源事实。

## 5. 官方报告检索索引与重复下载减少

migrations/0080_document_text_cache_and_lookup.sql 增加 source_snapshot_index(object_hash) 索引，并增加 official_document_lookup，按 company/period/document_type/published_at 定位已准入官方文档，避免每次恢复都遍历所有 capture/artifact。

索引只加速定位，不授予证据权威性。返回文档时仍重新检查 snapshot/admission/hash/身份/完整性合同；索引损坏或缺失不能把非法文档变成合法证据。

## 6. Current Research 的能力级增量复用

configs/current_research_policy.yaml 为每个 capability 声明 reuse_freshness_seconds、reuse_across_shanghai_date、reuse_requires_same_lookback。

复用判断从“整个 plan/hash 任意变化就全失效”收窄到 capability 的实际依赖：capability/stage/core/dependencies/provider candidates/preferred authorities/reuse policy 共同组成 step reuse contract。成功 attempt 必须有 verified_at，依赖 step 也必须可复用，关联 snapshot 仍需通过完整性与时效检查。

因此，和某 capability 无关的 planner/其他 step 变化不会迫使整个研究计划重新下载；真正改变该 capability 输入、依赖、lookback、跨日规则或 freshness 的变化仍会局部失效。价格数据保持短 TTL 且默认不过上海自然日；身份/正式财报允许更长且可跨日复用。

## 7. 失败分类、充分性与有界恢复

恢复链路区分可重试的网络/超时 transient failure、公共来源穷尽、格式/解析能力不可用、数据冲突/身份矛盾、已绑定证据不被接受，以及真正需要用户私有材料或授权。

BOUND_EVIDENCE_NOT_ACCEPTED:* 在绑定未变化时是确定性失败，不进入空重试；获取到新/不同绑定后才由正常 continuation 重新判断。公共来源穷尽仍保持 public-data unresolved，而不是伪装成私人输入请求。

财务充分性按事实用途边界判断：可认证子集只能支持对应事实，不能因为存在若干数字就取得完整财务/估值资格。Bull/Bear/Reviewer/Committee 可以共享同一个冻结事实层，但职责、独立判断和 publication gate 不合并。

完整推荐默认模型目标年化收益在 configs/full_research_recommendation.yaml 统一为 0.50。显式用户目标或可验证的用户历史合同仍优先；该默认值不是收益承诺，也不是 BUY 门槛。

## 8. 仍保留的硬门

即使增加运行成本，也保留 identity/period/caliber/unit/source lineage、raw/admission/derived object 存在性与 hash、exhaustive negative-proof 的完整分页/终止证明、当前 quote freshness、formal publication coverage、financial certification 的 statement/period/unit/evidence locator，以及 Committee、risk、portfolio 和 execution 的独立权限边界。

“减少死门禁”不等于把上述负向用例改成通过。任何优化若只能靠放弃这些不变量获得绿色结果，应视为回归。

## 9. 验证与已知限制

WP-49～WP-53 收口使用 FULL 影响面选择，并对 storage/migration/document/research/financial 边界做专项回归。最终修复后的聚焦质量 run 为 206/206 通过。独立 defect-first Review 曾发现 canonical alias FK、主报告包装标题误接纳和 deterministic failure 空重试三项 HIGH，均在收口前修复并有回归。

全仓分片仍有少量已在未修改 main 基线复现的既有失败，以及并发压力下的 timing-sensitive worker 波动；这些没有被伪报为本次 PASS，也未通过弱化测试消除。历史 002557/600521 材料只读回演用于验证真实报告身份与补证消费，不修改生产 evidence。

性能证据只声明已实测范围：重复 snapshot 注册从旧实现的同样本 20/20 FK 失败变为修复后的 20/20 成功，并记录本地缓存/DB 路径耗时。公网网络、LLM 调用/token 成本未在本轮稳定环境实测，保持“未测”，不推算加速倍数。

当前已知非阻断成本：reflow parser 为确认 media/parser identity 仍可能先读取 raw object；official_document_lookup 的兼容存在性检查会产生极小 SQLite 元数据查询。它们属于后续可测优化点，不是证据正确性缺陷。

## 10. 相关当前流程与 Skill

- docs/workflows/workflow-evidence-recovery.md
- docs/workflows/workflow-current-company-research.md
- .agents/skills/evidence-investigation/SKILL.md
- docs/architecture/adaptive-recovery-and-validation-v1.md

本设计不改变 paper trading 或 broker execution 权限；开发/验证过程不产生真实或模拟订单。
