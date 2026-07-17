# Phase 5 K5.6a～K5.6b 验收报告

验收日期：2026-07-17

验收结论：通过。该结论覆盖现有原料的确定性单元化、重复识别、初步分类、覆盖报告和人工审核队列，不代表观点或 Skill 已获人工批准。

## 1. 已交付

- migration 0019：蒸馏 run、单元安全元数据、作者报告、审核队列、本地清洗报告和方法覆盖报告。
- 统一定位：PDF 使用 `PAGE_TEXT` 页码和字符区间；DOCX 使用 `BLOCK_TEXT` 块序号；知乎使用内容/评论版本与 SourceSnapshot，不为 DOCX 伪造页码。
- 规范化：固定 Unicode NFKC、零宽字符和空白规则；知乎 HTML/JSON 正文使用固定文本投影；规范化正文只进入 ObjectStore。
- 重复链：同作者、同规则版本内按规范化文本 SHA-256 识别精确重复，首个单元为 canonical，后续单元只记录 `duplicate_of_unit_id`。
- 分类：20 个版本化通用内容类别，允许多标签；输出 `KEEP_CANDIDATE`、`DOWNWEIGHT_CANDIDATE` 或 `UNCLASSIFIED` 和理由码，不物理删除内容。
- 恢复：稳定 run/unit ID、批次提交、共享 checkpoint、重复运行幂等；Parquet 已存在时必须与确定性单元顺序一致。
- 安全 CLI：计划、运行、状态、对象/索引审计和审核队列命令均不输出正文、文件名或路径。

## 2. 四位作者真实结果

### MR Dang

- 输入：私有 PDF 249/249 页，线上想法确认 0 条。
- 产出：5,942 单元；5,771 canonical，171 精确重复。
- 决策：1,042 方法候选、809 降权候选、4,091 待分类。
- 代表性自动命中分布：行业 251、财务质量 228、估值 225、风险 149、商业模式 70、退出 70、建仓 66。
- 线上回答/文章仍有 2 个 gap，因此作者覆盖为 `PARTIAL`；审核为 `PENDING`。

### 寒武纪的鳄鱼

- 输入：本地 DOCX 2,032 块；不发起任何知乎抓取。
- 176 个空块，1,856 个非空块全部进入蒸馏；1,816 canonical，40 精确重复。
- 决策：417 方法候选、81 降权候选、1,358 待分类。
- 代表性自动命中分布：行业 208、建仓 62、失败案例 37、风险 35、估值 29、选股 27、财务质量 25。
- 本地完整导出与对象覆盖均已验证，来源覆盖为 `COMPLETE`；审核仍为 `PENDING`。

### 黄彦臻

- 输入：当前已保存想法 1 条，完整进入审核队列。
- 通用规则没有可靠命中，保留为 1 个 `UNCLASSIFIED` 单元，没有强行推断方法。
- 回答/文章仍有 2 个 gap，作者覆盖为 `PARTIAL`；审核为 `PENDING`。

### 派大星皮皮

- 输入：当前已保存想法 615 条，全部形成 canonical 单元。
- 决策：260 方法候选、15 降权候选、340 待分类。
- 代表性自动命中分布：持仓验证 85、建仓 72、风险 44、行业 36、退出 30、加仓 26、减仓 13、估值 12、复盘 11。
- 回答、文章和后续想法共有 3 个 gap，作者覆盖为 `PARTIAL`；审核为 `PENDING`。

## 3. 汇总与完整性

- 总蒸馏单元 8,414；canonical 8,203，精确重复 211。
- 方法候选 1,719，降权候选 905，待分类 5,790；三类之和与单元总数一致。
- 4 个作者报告、4 个私有审核队列，队列候选合计 8,203，全部 `PENDING`。
- 两份本地材料各生成一个清洗报告和一个方法覆盖报告；无人审前零命中只能是 `INSUFFICIENT_SOURCE`，不能是 `AUTHOR_SILENT`。
- 逐作者 `knowledge-distill-audit` 均为 `PASS`：规范化对象缺失 0、源对象缺失 0、Parquet 缺行 0、孤儿行 0、哈希错配 0、报告计数错配 0。

## 4. 自动化与运行库验收

- `uv run pytest -q`：163 passed / 8 skipped。
- `uv run ruff check .`：通过。
- `uv run pyright`：0 errors / 0 warnings。
- 真实状态库 migration 0001～0019，`integrity_check=ok`，外键违规 0。
- 真实运行库：4 runs、8,414 units、4 author reports、4 pending review queues、2 cleaning reports、2 method coverage reports。
- Git 变更和未跟踪清单不包含 PDF、DOCX、runtime、Cookie 或浏览器 Profile；SQLite/Parquet/CLI 不保存或输出私有正文与本地路径。

## 5. 未完成边界

K5.6c 尚未完成：需要从已分类且可定位的单元生成私有观点草稿和两类 Skill 候选，并建立人工审核与评测门禁。当前所有结果只是“待复核的结构候选”，不能用于声称作者明确表达了某项规则，更不能自动成为生产 Skill。
