# Phase 5 K5.3～K5.5 开发计划

计划日期：2026-07-17

状态：已落盘，按本文顺序实施

## 1. 已知起点

- 三位线上作者身份已确认；寒武纪的鳄鱼固定为本地完整导出，不再访问知乎。
- Python 公开接口已完成可审计实测：MR Dang 想法 0 条、黄彦臻想法 1 条均到真实终点；派大星皮皮已保存 615 条后被限制。
- 三位作者的回答/文章都需要登录通道。当前 Chrome 已登录，但扩展控制在接管/读取标签页时不稳定；不得复制 Cookie、读取 Profile 或把 401/403 误写成空集合。
- K5.1～K5.2 已有不可变响应、内容版本、Parquet 索引、列表检查点、缺口和覆盖报告基础。

## 2. K5.3：登录响应导入和评论树

### 2.1 无凭据响应导入

新增一个只接受 runtime 本地文件的 `ZhihuBrowserResponseEnvelope`。信封只允许包含作者 source_id、内容类型/内容 ID、请求 URL、HTTP 状态、响应 MIME、原始响应体和采集运输类型；禁止 Cookie、Authorization、请求头全集、Profile 路径和浏览历史。

CLI 分两步：

1. `astock zhihu-response-import <envelope>`：校验白名单、同源 HTTPS API URL、正文大小与 JSON 结构，先存 ObjectStore/SourceSnapshot，再登记“待消费响应”；
2. `astock zhihu-import-replay <source-id>`：按原请求游标把已导入响应送入同一适配器、版本仓储和检查点逻辑，浏览器与 Python 不形成旁路。

导入文件位于被 Git 忽略的 runtime；命令输出只给 envelope_id、snapshot_id、分类和计数。重复导入同一响应幂等。

### 2.2 评论与嵌套回复合同

新增并版本化：

- `ZhihuCommentNode`：comment_id、content_id、author_id、parent_id、reply_to_comment_id、root_comment_id、正文对象哈希、创建/更新时间、点赞数、作者是否为目标作者、原始快照 ID；
- `ZhihuCommentPage`：父内容或根评论、分页/游标、节点 ID、is_end、下一游标、原始对象哈希和运输来源；
- `ZhihuAuthorParticipationChain`：目标作者评论、完整祖先链、作者直接回复对象、理解连续对话所需的中间节点和作者后续回复；
- 评论覆盖报告：根评论页和每个需要展开的 child page 分开计数、设终点和缺口。

原始评论树完整保存在私有 runtime。派生链只选择目标作者参与的分支，但每个节点继续回链原始评论版本和 SourceSnapshot。

### 2.3 端点发现边界

知乎没有项目可依赖的稳定公开评论 API 合同，因此不凭记忆硬编码。先从已登录页面实际观察到的请求 URL 建立 endpoint template，冻结一页真实私有响应，再生成不含真实正文的人工 fixture。只允许 `www.zhihu.com/api/` 且路径必须匹配已批准 template；结构变化返回 `INVALID_RESPONSE`，不推进评论检查点。

## 3. K5.4：三位作者连续枚举

执行顺序固定为：

1. 公开 Python 接口尚可访问的游标；
2. 已导入的登录浏览器响应；
3. 若 Chrome 扩展恢复，逐页观察并导入，低频串行；
4. 若出现验证码、安全验证或扩展持续不可用，为对应作者/类型生成单独人工任务，其他来源继续。

已成功的公开快照不重复从浏览器抓取。浏览器只补开放缺口，并从 SQLite 检查点指定的精确 URL 开始。

## 4. K5.5：覆盖审计

新增聚合命令 `astock knowledge-coverage-audit`，逐作者、逐内容类型检查：

- 身份已确认；
- 列表终点、成功/失败/受限/重复/更新/缺失计数；
- 每个内容版本的正文对象存在且哈希可验证；
- 需要评论的内容是否有根评论终点；
- 需要展开的作者参与根评论是否有 child reply 终点；
- 开放 gap、长期 RUNNING job/attempt 和未消费导入信封；
- Parquet 索引与 SQLite 元数据是否一一对应。

寒武纪的鳄鱼另由 `knowledge-local-coverage` 校验 DOCX 文件哈希、不可变源对象、2,032 个解析块及解析报告，输出 `USER_CONFIRMED_COMPLETE_EXPORT`；该状态不得伪装成知乎分页独立核验。

## 5. 测试与提交顺序

1. K5.3a：导入信封 Schema、migration 0016、仓储、路径/URL/敏感字段拒绝和幂等测试。
2. K5.3b：评论页/节点/参与链 Schema、migration、人工 fixture、分页与崩溃恢复测试。
3. K5.4：登录响应 live smoke；按作者/类型恢复，限制发生即暂停该来源。
4. K5.5：覆盖审计、本地 DOCX 覆盖、对象/Parquet/SQLite 对账和隐私扫描。
5. 每个小步分别运行 `pytest`、`ruff`、`pyright`，通过后形成独立提交；最终再跑全仓验收。

## 6. 完成门

只有三位线上作者的每种列表均为 `PAGINATION_COMPLETE` 或结构化 `CONFIRMED_EMPTY`、所有必需评论分页有终点、`missing_count=0` 且开放 gap 为 0，K5.3～K5.5 才能整体完成。平台限制或浏览器不可用时继续标记 `ACCESS_RESTRICTED/PARTIAL`，不阻塞本地材料蒸馏和 Phase 4/6 的确定性内核开发，但会阻止对应作者知识 Skill 获批。
