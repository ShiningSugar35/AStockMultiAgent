# Phase 5 K5.6 知识清洗与蒸馏开发计划

计划日期：2026-07-17

状态：K5.6a～K5.6d 的确定性自动链与真实运行已实现并验收；人工综合、审核和样本外评测待后续执行，Phase 5 总体仍为 `PARTIAL`

## 1. 目标与真实输入

本步把已获准且已保存的私有/社区原料变成可审计的结构化候选知识，不把自动分类结果直接冒充已批准投资规则。

当前输入固定为：

- MR Dang《价值投资功法》：私有 PDF，全量解析 249/249 页，解析文本 151,014 字符；
- 寒武纪的鳄鱼：用户确认完整的私有 DOCX，全量解析 2,032 块，解析文本 93,323 字符；不再访问知乎；
- 黄彦臻：当前已保存想法 1 条；
- 派大星皮皮：当前已保存想法 615 条；
- MR Dang 线上想法确认 0 条；三位线上作者的回答、文章和评论缺口继续保留，不阻塞现有材料的处理，也不得据此宣布作者全集蒸馏完成。

## 2. 不可突破的边界

1. PDF、DOCX、知乎原文、清洗后全文和浏览器会话只进入 `runtime/objects/sha256/` 或其他被 Git 忽略的 runtime 工件。
2. SQLite 和 Parquet 只保存 ID、来源定位、分类、计数、版本和对象哈希，不保存私有正文、标题、观点原句或文件路径。
3. 自动清洗只生成“保留/降权/待复核”候选，永久保留原始对象与回链，不执行物理删除。
4. 社区内容只能形成研究方法候选或线索；涉及公司事实的规则必须要求回到公告、交易所和财报验证。
5. 未经人工审核与评测，观点卡、规则和 Skill 一律保持 `PENDING/NOT_RUN`，不得批准。
6. 不模仿作者人格和文风，只抽取可复核的方法、适用条件、反证和失效条件。

## 3. 先修正的工件合同

现有 `BookViewpointCard` 和 `BookSkillCandidate` 只接受 PDF 页码，不能合法引用 DOCX 块或知乎内容；明文 proposition/rule 若直接进入 SQLite 也会破坏私有内容边界。因此先新增统一引用与私有载荷合同：

- `DistillationSourceLocator`：`PAGE_TEXT`、`BLOCK_TEXT`、`ZHIHU_CONTENT` 或 `ZHIHU_COMMENT`，保存 source_snapshot_id、源单元 ID、页码/块序号/content_id、字符区间、parser/version 和对象哈希；
- `DistillationUnit`：稳定 unit_id、作者/来源、规范化文本对象哈希、原始定位、重复关系、内容分类、方法分类、规则版本、理由码和置信度；
- `PrivateViewpointDraft`：命题、反证和失效条件只保存在 ObjectStore 载荷中，SQLite 只登记 draft_id、对象哈希、证据引用数量和审核状态；
- `PrivateSkillCandidate`：规则载荷同样只在 ObjectStore，必须引用证据定位，默认 `NOT_RUN/PENDING`；
- `AuthorDistillationReport`：按作者汇总输入覆盖、分类、重复、方法类别、缺口和审核队列，不含正文。

原有 PDF 专用 Schema 保留兼容，但 K5.6 新链统一走上述跨 PDF/DOCX/知乎的合同。

## 4. K5.6a：确定性单元目录与清洗分类

### 4.1 单元化

- PDF：按已解析页文本中的非空段落切分，记录页码与字符区间；
- DOCX：每个非空稳定块作为一个单元，引用 `BLOCK_TEXT`；过长块只在对象层切片，保留父块定位；
- 知乎：对已保存正文做 HTML/空白规范化后按段落切分，引用内容版本和原始 SourceSnapshot；评论只处理已取得的目标作者参与链；
- 全部规范化采用固定 Unicode NFKC、换行/空白规则和版本号；同一输入重复运行必须产生相同 unit_id 与文本哈希。

### 4.2 重复与噪声

- 先做同作者范围内的规范化文本精确重复；首次出现为 canonical，后续单元只记录 duplicate_of；
- 短文本、纯链接/目录/页码、无正文 HTML 等使用明确理由码降权；
- “营销、情绪、故事、过时价格、无新增复述”等只能由版本化通用规则标成候选，不能自动删除；规则未命中则进入 `UNCLASSIFIED`，不得强行归类。

### 4.3 方法分类

版本化词典和组合规则覆盖：选股、商业模式、行业、估值、财务质量、建仓、持仓验证、加仓、减仓、退出、风险、失败案例、反证/失效和复盘。允许多标签，保存命中理由码和分数；自动结果统一标为待人工复核。

## 5. K5.6b：覆盖报告与人工审核队列

每个来源生成：

- `BookCleaningReport` 或等价跨来源清洗报告；
- `BookMethodCoverageReport`：八个核心阶段分别给出段落/证据计数；自动阶段零命中只能写 `INSUFFICIENT_SOURCE`，不能写 `AUTHOR_SILENT`；
- `AuthorDistillationReport`：列出来源覆盖状态，线上缺口与本地完整导出语义分开；
- 私有 review queue：包含待审核单元对象引用和上下文对象引用，只写 runtime，不在 CLI 输出正文。

审核前 `human_review_status=PENDING`。人工可以批准、拒绝或要求修改分类；决定以 `HumanReviewDecision` 追加保存，不覆盖自动结果。

## 6. K5.6c：观点与 Skill 候选

仅从“保留且方法分类明确”的单元生成私有草稿：

1. 观点候选必须包含命题、适用范围、证据引用、反证和失效条件；
2. Candidate Selection 候选覆盖候选发现、公司/行业分析、估值和证据门槛；
3. Position Lifecycle 候选覆盖建仓、持仓验证、加减仓、退出、风险和复盘；
4. 没有明确原文依据时不补写作者观点；
5. 每条候选默认 `evaluation_status=NOT_RUN`、`approval_status=PENDING`；
6. 后续评测必须验证可执行字段、未来函数、证据门槛、作者差分和通用内核冲突。

本步只交付候选与审核包，不在没有用户审核的情况下批准生产 Skill。

## 7. K5.6d：真实运行顺序

1. MR Dang PDF：全量单元化、分类、清洗/方法覆盖报告和审核队列；
2. 寒武纪 DOCX：全量 2,032 块处理，生成块级引用与独立作者报告；
3. 黄彦臻 1 条想法：处理现有版本并标记线上历史缺口；
4. 派大星皮皮 615 条想法：批量处理、断点提交并保留后续游标 gap；
5. MR Dang 线上想法 0 条只记录已确认空集合，不从 PDF 结果反推线上内容；
6. 汇总跨作者类别分布、未分类比例和审核工作量，不合并不同作者的观点身份。

## 8. Migration、CLI 与恢复

- migration 0019：distillation run、unit metadata、分类版本、重复关系、作者报告和初步审核队列；
- migration 0020：私有摘录草稿、候选 Skill、安全引用关系和作者生成报告；
- migration 0021：同一蒸馏输入按生成规则版本并存，升级规则不得覆盖旧草稿；
- Parquet：只保存安全元数据索引，正文/命题/规则载荷只存 ObjectStore；
- CLI：`knowledge-distill-plan/run/status/audit`、`knowledge-review-queue` 和 `knowledge-draft-generate/status/audit`，输出仅含 ID、哈希、计数和状态；
- 检查点按 run、source、source_unit 递进；ObjectStore 已写而 SQLite 未提交时可幂等恢复；
- 相同源快照与规则版本重复运行不重复生成单元和候选。

## 9. 测试与验收门

自动化测试至少覆盖：

- PDF 页、DOCX 块、知乎内容三种定位；
- 中文 Unicode/空白规范化、稳定 ID、精确重复链和多标签分类；
- 分类计数守恒，所有非空输入单元恰好进入 canonical 或 duplicate；
- 对象先写、元数据后提交、崩溃恢复、重复运行幂等；
- DOCX 结果不伪造页码；零命中在未审核前不得成为 `AUTHOR_SILENT`；
- 未审核候选不能变为 `APPROVED`；
- SQLite/Parquet/CLI/Git 不泄露正文、文件路径、Cookie 或浏览器 Profile。

真实验收必须证明：

- PDF 249 页、DOCX 2,032 块和线上 616 条现有内容全部纳入或给出结构化缺口；
- 输入对象和派生对象哈希全部可验证，SQLite/Parquet 对账为 0；
- 每位作者独立生成分类和方法覆盖报告；
- 所有观点/Skill 仍为待人工审核和待评测，不提前批准；
- `pytest`、`ruff`、`pyright`、SQLite integrity/foreign key 和 Git 隐私扫描通过。

## 10. 停止条件

只有遇到无法从源对象、现有 Schema 或明确项目规则判断的语义选择，才暂停对应候选并进入人工审核队列；单个作者或单个类别不确定时继续处理其他来源。平台访问限制不阻止本地材料和已保存线上内容的确定性蒸馏。

## 11. 实施进度

- [x] K5.6a：统一定位、规范化单元、精确重复链、版本化多标签分类、SQLite/Parquet/ObjectStore 和断点恢复。
- [x] K5.6b：作者清洗/方法覆盖报告、私有审核队列、四位作者真实运行与逐作者对象/索引审计。
- [x] K5.6c 自动链：私有原文摘录草稿、空规则 Skill 候选、来源回链、人工审核与评测门禁；未经审核不批准。
- [ ] K5.6c 人工链：把摘录综合为可测试观点、补适用范围/反证/失效条件，执行评测并作批准或拒绝决定。
- [x] K5.6d：四位作者真实自动运行、幂等复跑、对象/引用/审批门禁和运行库终验。

K5.6c～K5.6d 自动链真实结果为 401 条 `PENDING` 摘录草稿和 40 个 `NOT_RUN/PENDING` Skill 候选；黄彦臻现有唯一材料未达到生成门槛，结果保持 0/0。验收依据见 `docs/Phase5-K5.6c-K5.6d验收报告.md`。

K5.6a～K5.6b 验收依据见 `docs/Phase5-K5.6a-K5.6b验收报告.md`。
