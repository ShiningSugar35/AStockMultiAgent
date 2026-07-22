# OpenCode / DeepSeek V4 Flash 论证单元筛选任务

在仓库根目录执行。不要联网补事实；除下列输入包、各批次 schema 和指定输出路径外，不要读取
Cookie、浏览器 Profile、SQLite 或其他 runtime 文件。

系统提示与输出合同：`OPENCODE_DEEPSEEK_PROMPT.md`

依次处理以下三个私有批次；每一行输入都是完整 `ArgumentUnit`，不得拆成裸段落：

1. Mr. Dang
   - 输入：`runtime/knowledge_semantic_packets/0c4e3f45c0460d4a4929777ebbc3b4b7e30a01c15697922af354e92b51428c3e/packet.jsonl`
   - Schema：同目录 `result-schema.json`
   - 输出：同目录 `deepseek-results.jsonl`
   - 预期：43 行
2. 黄彦臻
   - 输入：`runtime/knowledge_semantic_packets/400316605a481a1d53b20adb197a032fd4e76bea7194a03137d8fdd0a47a8f51/packet.jsonl`
   - Schema：同目录 `result-schema.json`
   - 输出：同目录 `deepseek-results.jsonl`
   - 预期：136 行
3. 派大星皮皮
   - 输入：`runtime/knowledge_semantic_packets/b9729bf26612014a3b274e83faf057b308dc465b7fdfa9509dee9c42fe08268d/packet.jsonl`
   - Schema：同目录 `result-schema.json`
   - 输出：同目录 `deepseek-results.jsonl`
   - 预期：1,126 行

要求：

- 使用 `deepseek-v4-flash`，逐行输出 JSONL，不加 Markdown 围栏或解释。
- 每个 `argument_unit_id` 恰好一条结果，保留输入 `input_sha256`。
- 只有完整、可复用、可证伪的投资方法才 `KEEP`；叙事、背景、单纯观点和证据不足项用 `DROP` 或 `REVIEW`。
- 候选证据只能引用本 AU 的 `paragraph_id`；不得创造事实、引用、批准状态、收益判断或交易指令。
- 某批失败时不要写部分最终文件；保留临时文件并报告，待整批完整后再原子改名。

生成后不要直接编辑 SQLite。回到 Codex 依次执行：

```powershell
uv run astock knowledge-semantic-result-stage semantic-llm-batch:0c4e3f45c0460d4a4929777ebbc3b4b7e30a01c15697922af354e92b51428c3e runtime/knowledge_semantic_packets/0c4e3f45c0460d4a4929777ebbc3b4b7e30a01c15697922af354e92b51428c3e/deepseek-results.jsonl
uv run astock knowledge-semantic-result-import semantic-llm-batch:0c4e3f45c0460d4a4929777ebbc3b4b7e30a01c15697922af354e92b51428c3e

uv run astock knowledge-semantic-result-stage semantic-llm-batch:400316605a481a1d53b20adb197a032fd4e76bea7194a03137d8fdd0a47a8f51 runtime/knowledge_semantic_packets/400316605a481a1d53b20adb197a032fd4e76bea7194a03137d8fdd0a47a8f51/deepseek-results.jsonl
uv run astock knowledge-semantic-result-import semantic-llm-batch:400316605a481a1d53b20adb197a032fd4e76bea7194a03137d8fdd0a47a8f51

uv run astock knowledge-semantic-result-stage semantic-llm-batch:b9729bf26612014a3b274e83faf057b308dc465b7fdfa9509dee9c42fe08268d runtime/knowledge_semantic_packets/b9729bf26612014a3b274e83faf057b308dc465b7fdfa9509dee9c42fe08268d/deepseek-results.jsonl
uv run astock knowledge-semantic-result-import semantic-llm-batch:b9729bf26612014a3b274e83faf057b308dc465b7fdfa9509dee9c42fe08268d
```

导入器会整批校验未知/重复/遗漏 AU、prompt/schema/input 哈希、跨段引用和候选 provenance；任一错误整批零写入。
