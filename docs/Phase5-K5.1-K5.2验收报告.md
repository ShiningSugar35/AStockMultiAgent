# Phase 5 K5.1～K5.2 验收报告

验收日期：2026-07-17
验收结论：通过。该结论只覆盖采集合同、确定性 Python 采集器、恢复与覆盖基础；不代表三位作者的知乎全集已经采集完成。

## 1. 交付范围

- 新增白名单作者、采集范围、身份、列表页、内容版本、缺口和覆盖报告 Schema。
- 新增 migration 0015，保存作者身份、列表页清单、内容版本和覆盖报告元数据；正文不进入 SQLite。
- 原始 HTTP 响应先进入不可变 ObjectStore，再解析、登记 SourceSnapshot、写内容版本和 Parquet 索引，最后推进检查点。
- 新增 `knowledge-source-list`、`zhihu-author-probe`、`zhihu-author-sync`、`zhihu-coverage` CLI；CLI 只输出状态、计数和哈希引用，不输出正文。
- 支持崩溃后从最后边界恢复、重复内容幂等、更新内容保留前序版本、失败游标形成开放缺口、成功补回后关闭对应历史缺口。
- 仅允许 `https://www.zhihu.com/api/`。知乎返回的同域 HTTP API 游标可升级为 HTTPS；跨域、用户信息、异常端口、fragment 和非 API 路径继续拒绝。

## 2. 真实身份核验

| 作者 | url token | 核验结果 |
|---|---|---|
| MR Dang | `mr-dang-77` | 主页 API 返回 token 与显示名一致 |
| 黄彦臻 | `huang-wei-yan-30` | 公开索引唯一候选与主页 API 的 token、显示名精确一致 |
| 派大星皮皮 | `xiao-peng-61-47` | 公开索引唯一候选与主页 API 的 token、显示名精确一致 |
| 寒武纪的鳄鱼 | 本地完整导出 | 用户确认 DOCX 已含全部语料，不发起线上请求 |

黄彦臻、派大星皮皮原有身份人工任务已改为 `RESOLVED`。平台用户 ID 和主页响应只保存在 runtime 状态与不可变快照中，不写入 Git。

## 3. 低频 live smoke

| 作者 | 回答 | 文章 | 想法 |
|---|---|---|---|
| MR Dang | `ACCESS_RESTRICTED / AUTH_REQUIRED` | `ACCESS_RESTRICTED / AUTH_REQUIRED` | `COMPLETE / CONFIRMED_EMPTY`，0 条 |
| 黄彦臻 | `ACCESS_RESTRICTED / AUTH_REQUIRED` | `ACCESS_RESTRICTED / AUTH_REQUIRED` | `COMPLETE / PAGINATION_COMPLETE`，1 条 |
| 派大星皮皮 | `ACCESS_RESTRICTED` | `ACCESS_RESTRICTED / AUTH_REQUIRED` | 已保存 615 条；第 125 个请求边界返回访问限制，保持开放缺口 |

这些结果证明错误语义正确：401/403 没有被算成“0 条”或“已经到底”。派大星皮皮已成功的 124 个列表页及 615 个稳定内容 ID 均已落盘；受限后的内容数量未知，因此不能宣称完整。

Chrome 中已能看到登录状态和 MR Dang 主页，但当前 Chrome 扩展在接管/读取该页时多次超时；项目未读取 Cookie、Profile 或本地会话存储，也没有绕过安全验证。回答、文章和派大星皮皮后续想法继续保留为 K5.3～K5.5 的访问缺口。

## 4. 自动化验收

- `uv run pytest -q`：143 passed / 8 skipped。
- `uv run ruff check .`：通过。
- `uv run pyright`：0 errors / 0 warnings。
- 真实状态库：migration 0001～0015 已应用，`PRAGMA integrity_check=ok`，外键违规 0。
- recorded fixture 只含人工构造文本；真实作者正文只存在于被 Git 忽略的 runtime/ObjectStore。
- Git 隐私扫描确认 PDF、DOCX 和 runtime 采集对象均未被跟踪；`runtime/.gitkeep` 是唯一允许的占位文件。

## 5. 完成边界

K5.1 和 K5.2 可以标记完成。Phase 5 仍未完成，原因是：

1. 三位作者的回答和文章尚未通过登录通道连续枚举；
2. 派大星皮皮的想法还有平台访问缺口；
3. 评论分页、嵌套回复和作者参与链尚未实现并验收；
4. 寒武纪本地覆盖报告、三位线上作者最终覆盖审计和蒸馏仍待后续步骤。
