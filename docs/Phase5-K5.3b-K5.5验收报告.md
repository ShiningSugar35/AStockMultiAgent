# Phase 5 K5.3b～K5.5 验收报告

验收日期：2026-07-17

验收结论：评论采集的确定性内核与覆盖审计通过；三位线上作者的登录内容连续枚举未完成，Phase 5 整体继续标记 `PARTIAL`。

## 1. 评论链内核

- migration 0017 保存评论页清单、评论版本和作者参与链；正文只在私有 ObjectStore，SQLite 与 Parquet 只保留定位和哈希元数据。
- 根评论与每个子回复父节点使用不同 durable checkpoint，分页失败不会推进游标，重复页与重复节点幂等。
- 评论节点保留 root、parent、reply-to、作者身份、发布时间、正文对象哈希和原始 SourceSnapshot；目标作者参与链只派生必要分支，但可回链完整私有原始树。
- Chrome 页面实际观察到回答根评论路径 `/api/v4/comment_v5/answers/{content_id}/root_comment`，因此只有该模板被标为 `VERIFIED`。
- 子回复请求没有被实际观察到，模板保持 `PENDING_OBSERVATION`。导入器要求响应路径精确匹配已批准模板；未知或猜测路径在写入 ObjectStore 前拒绝。
- 登录页面中观察到的根评论请求经低频 Python 复核返回访问限制，已如实保留为受限边界，没有把 403 当成空评论。

## 2. 本地完整导出核验

`astock knowledge-local-coverage zhihu:hanwujideeyu` 的真实结果：

- 文件 SHA-256 与白名单期望值一致；不可变源对象和 SourceSnapshot 均可验证。
- DOCX 解析报告为 `COMPLETE`，报告对象和 block-set 对象均可验证。
- 期望块数 2032，实际块数 2032，块 ID 顺序和集合哈希一致。
- 正文对象 2032/2032、元数据对象 2032/2032，缺失对象 0。
- 最终状态为 `USER_CONFIRMED_COMPLETE_EXPORT`。它只表示“用户确认导出完整，系统确认本地文件与解析结果完整”，不表示系统自行遍历了知乎全部分页。

该作者按用户要求不再发起任何线上抓取。

## 3. 聚合覆盖审计实测

`astock knowledge-coverage-audit` 对四位作者的真实运行结果：

- 已保存线上正文版本 616 条：黄彦臻想法 1 条、派大星皮皮想法 615 条；正文对象缺失 0。
- SQLite 与 Parquet 内容版本数量及正文哈希一致，错配 0；评论元数据当前也无错配。
- 待消费导入信封 0，长时间停留在 RUNNING 的知乎任务 0。
- 开放采集 gap 共 7 个：三位作者回答/文章共 6 个，派大星皮皮后续想法 1 个。
- 尚未完成根评论范围 616 个：黄彦臻 1 个、派大星皮皮 615 个。
- MR Dang、黄彦臻、派大星皮皮均为 `PARTIAL`；寒武纪的鳄鱼单独为 `USER_CONFIRMED_COMPLETE_EXPORT`；聚合状态为 `PARTIAL`。

## 4. 自动化与运行库验收

- `uv run pytest -q`：162 passed / 8 skipped。
- `uv run ruff check .`：通过。
- `uv run pyright`：0 errors / 0 warnings。
- 真实状态库 migration 0001～0018 齐全，`integrity_check=ok`，外键违规 0。
- 集成测试覆盖评论分页、节点版本、作者参与链、根/子回复检查点隔离、重复回放、受限响应 gap、未知端点拒绝、本地覆盖成功/损坏降级和聚合审计。
- Git 跟踪内容不包含 PDF、DOCX、runtime 响应、Cookie、浏览器 Profile 或私有解析正文；fixture 为人工合成内容。

## 5. 未完成边界

K5.4 仍需在平台允许且登录响应可稳定导出的窗口，补采三位作者的回答、文章、派大星皮皮后续想法，以及每条已收内容所需的评论分页和嵌套回复。验证码、安全验证或未知子回复端点都必须继续暂停相应来源，不阻塞本地语料的结构化清洗与蒸馏，但会阻止对应线上作者 Skill 获批。
