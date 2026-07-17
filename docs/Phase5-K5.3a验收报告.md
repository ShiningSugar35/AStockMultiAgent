# Phase 5 K5.3a 验收报告

验收日期：2026-07-17

验收结论：通过。该结论覆盖无凭据响应信封的导入、持久化和列表回放，不代表登录浏览器内容已采完。

## 1. 已交付

- `ZhihuBrowserResponseEnvelope`：只允许作者、响应种类、列表边界、请求 URL、HTTP/MIME、base64 原始响应体、运输类型和采集时间；额外字段一律拒绝。
- migration 0016：保存导入响应队列、快照引用、内容范围和消费状态，不保存文件路径或凭据。
- `zhihu-response-import`：只读 runtime 内文件，校验白名单与 HTTPS API 同源边界，原始字节先进入 ObjectStore，再登记 `PENDING` 信封。
- `zhihu-import-replay`：把单个列表响应送回既有适配器、版本仓储、Parquet、覆盖报告和检查点；处理成功后才改为 `CONSUMED`。
- 回放顺序门：列表页号和 request_cursor 必须等于当前 durable checkpoint；乱序页保持 `PENDING`，返回 `CONFLICT`。
- 崩溃补偿：若列表边界已提交但信封尚未来得及改状态，再次回放会识别已有 SourceSnapshot 并安全补记 `CONSUMED`。

## 2. 安全门

- 信封必须位于项目 runtime 目录；解析后的真实路径逃逸会在读取正文前拒绝。
- 只接受 `CHROME` 或 `MANUAL_IMPORT`；Python API 响应不能冒充浏览器信封。
- 只接受精确 `https://www.zhihu.com/api/`；HTTP、跨域、用户信息、异常端口和作者列表路径不匹配均拒绝。
- `cookie`、`authorization`、Profile 路径等额外顶层字段因 strict Schema 被拒绝。
- CLI 不输出响应正文、信封路径或任何会话信息，只输出 ID、状态、计数和对象哈希。

## 3. 自动化验收

- `uv run pytest -q`：152 passed / 8 skipped。
- `uv run ruff check .`：通过。
- `uv run pyright`：0 errors / 0 warnings。
- 真实状态库：migration 0001～0016，`integrity_check=ok`，外键违规 0。
- 集成测试覆盖：重复导入幂等、原始字节不变、runtime 路径门、跨域/HTTP/错作者拒绝、Cookie 字段拒绝、两页连续回放、乱序冲突、重复消费幂等和 CLI 正文脱敏。
- Git 隐私扫描：PDF、DOCX、runtime 响应与对象均未被跟踪。

## 4. 未完成边界

Chrome 扩展尚未稳定导出真实登录响应，评论 endpoint template、评论树、嵌套回复和作者参与链仍属于 K5.3b；三位作者回答/文章及派大星皮皮后续想法仍为开放访问缺口。
