# Phase 5 K5.6c～K5.6d 验收报告

验收日期：2026-07-17

验收结论：确定性自动生成与真实材料运行通过。该结论只表示私有摘录草稿、候选 Skill 的来源回链、存储边界和审批门禁可靠，不表示任何作者观点已经人工综合，也不表示任何 Skill 已通过评测或获准生产使用。

## 1. 已交付

- migration 0020：私有观点草稿、候选 Skill、安全引用关系和作者生成报告；SQLite 只保存 ID、哈希、类别、版本、计数和状态。
- migration 0021：把唯一键扩展为“蒸馏 run + 生成规则版本”，允许未来 v2 规则在不覆盖 v1 的前提下重新生成；真实迁移前后 401 条观点、40 个候选、4 份报告和两类 401 条引用全部守恒。
- 版本化生成器 `private-excerpt-draft-v1`：只处理 canonical、`KEEP_CANDIDATE` 且方法类别明确的单元。
- 每个方法类别最多选择 12 个代表单元；排序只使用规则分数、文本长度距离、来源顺序和稳定 ID，相同输入重复运行结果不变。
- 自动观点明确标为 `SOURCE_EXCERPT_NOT_SYNTHESIZED`。命题暂时只是原文规范化摘录；适用范围、反证和失效条件保持空集合，并分别记录未推导质量缺口。
- 每个有材料的方法类别生成一个候选 Skill；`formal_rule` 固定为空，必须经过人工核对上下文、改写为可测试规则、补适用范围/反证/失效条件后才能评测。
- Candidate Selection 覆盖选股、商业模式、行业、估值和财务质量；Position Lifecycle 覆盖建仓、持仓、加仓、减仓、退出、风险、失败案例、反证失效和复盘。
- 候选 Skill 强制携带“不自动交易、公司事实回到官方证据、PIT 校验、样本外评测后才可批准”等安全门禁。
- CLI：`knowledge-draft-generate/status/audit` 只输出报告、计数和状态，不输出原文、文件名或本地路径。

## 2. 私有内容边界

- 观点摘录只存在于 `runtime/objects/sha256/` 的不可覆盖对象中；SQLite 中的 `draft_json` 不包含命题正文。
- Skill 载荷只存在于 ObjectStore；SQLite 中的 `candidate_json` 不包含原文或正式规则。
- 每条观点草稿回链一个蒸馏单元、规范化文本 SHA-256 和统一来源定位；PDF 使用页定位，DOCX 使用块定位，知乎使用内容/评论版本定位。
- 候选 Skill 通过外键引用观点草稿和蒸馏单元，审计同时核对 JSON 引用、关系表顺序和对象载荷。
- PDF、DOCX、runtime、Cookie 和浏览器 Profile 均未进入 Git；两份用户材料继续由 `.gitignore` 排除。

## 3. 四位作者真实结果

### MR Dang

- 1,042 个 canonical 方法候选单元全部具备方法分类。
- 生成 148 条代表性摘录草稿，覆盖 14 个方法类别；每类最多 12 条。
- 生成 14 个候选 Skill：Candidate Selection 5 个、Position Lifecycle 9 个。
- 全部观点 `PENDING`，全部 Skill `NOT_RUN/PENDING`。

### 寒武纪的鳄鱼

- 只读取已完整解析的本地 DOCX，不访问知乎。
- 417 个 canonical 方法候选单元全部具备方法分类。
- 生成 128 条代表性摘录草稿，覆盖 12 个方法类别。
- 生成 12 个候选 Skill：Candidate Selection 5 个、Position Lifecycle 7 个。
- 全部观点 `PENDING`，全部 Skill `NOT_RUN/PENDING`。

### 黄彦臻

- 当前唯一已保存内容是 `UNCLASSIFIED`，不满足“保留且方法明确”的生成门槛。
- 正确产出 0 条观点草稿和 0 个候选 Skill，没有从单条不明确材料推测作者方法。
- 作者报告仍为 `PENDING`，回答/文章的线上缺口继续保留。

### 派大星皮皮

- 260 个 canonical 方法候选单元全部具备方法分类。
- 生成 125 条代表性摘录草稿，覆盖 14 个方法类别。
- 生成 14 个候选 Skill：Candidate Selection 5 个、Position Lifecycle 9 个。
- 全部观点 `PENDING`，全部 Skill `NOT_RUN/PENDING`。

## 4. 汇总、幂等与审计

- 合计 401 条私有摘录草稿、40 个候选 Skill、4 份作者生成报告。
- 401 条观点全部 `PENDING`；40 个 Skill 全部 `evaluation_status=NOT_RUN`、`approval_status=PENDING`。
- 四位作者重复真实运行后报告 ID 均保持不变，草稿和候选计数不增加。
- 四位 `knowledge-draft-audit` 均为 `PASS`：载荷对象缺失 0、载荷无效 0、源引用错配 0、候选引用错配 0、审批门禁错配 0、报告计数错配 0。
- 真实状态库 migration 0001～0021，`integrity_check=ok`，外键违规 0。

## 5. 自动化验收

- 跨 PDF/DOCX/知乎合成测试验证：对象先写、重复生成幂等、原文只在对象载荷、SQLite 元数据不含测试原文/私有路径、正式规则为空、引用和状态审计通过。
- Schema 测试验证：自动观点不能自我批准，自动 Skill 不能声称已评测或已批准。
- `uv run ruff check .`：通过。
- `uv run pyright`：0 errors / 0 warnings。
- `uv run pytest`：165 passed / 8 skipped。

## 6. 未完成边界

- 401 条摘录还不是作者观点卡；需要人工阅读上下文后合并、改写或拒绝。
- 40 个候选还不是生产 Skill；需要形成正式规则、补证据门槛和失效条件，并完成历史/前向、PIT 和样本外评测。
- 三位线上作者仍有 7 个采集 gap 和 616 个未完成根评论范围，因此 Phase 5 总体保持 `PARTIAL`。
- 黄彦臻现有材料不足以形成方法候选；系统保持空结果，不以常识或其他作者材料填补。
