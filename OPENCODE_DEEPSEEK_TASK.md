# OpenCode / DeepSeek V4 Flash 论证单元筛选任务

## 当前状态

`BOOK_VISUAL_PACKET_READY / ZHIHU_BLOCKED_IMAGE_EVIDENCE_NOT_FROZEN`

《价值投资功法》已有一个可执行的本地批次：

| 字段 | 冻结值 |
| --- | --- |
| visual run | `book-visual-run:e94590494783714a9f1743451ea91cbe778289ddd043c4db232c1f866bb66bf9` |
| semantic run | `semantic-run:e0605aa01c8ddc0dffe69a81d88389d546e52b39aa89d19fc8a2012835e06ea5` |
| embedding manifest | `semantic-embedding:7d9a87f5b6ce8757ddaf1a3147c122b3ca9ce238e06d319ff59dd9d48b6a52fe` |
| batch ID | `semantic-llm-batch:af5d59de3bd985232b0c8c7da0a9e5fd34337bd510a4eeb5ef36690a0464911b` |
| 目录 | `runtime/knowledge_semantic_packets/af5d59de3bd985232b0c8c7da0a9e5fd34337bd510a4eeb5ef36690a0464911b/` |
| READY / 结构留置 | 11 / 300 |
| packet hash | `809dd8929987667dcbd2ac5355e2c48f93af6eb013b490c385f58b1b8e51ac24` |
| canonical result-schema hash | `182625a72660175e1d4ca84c28e1a63cb3c37cfa24de34e5d762c1b58efea99d` |
| prompt hash | `da84a0fb5b286a9452e5ca66c5b6b30a65ff5d338b97e7d7b7902234fb90c572` |

该批次只含 11 个结构闭合 AU。300 个留置 AU 不在 `packet.jsonl`，不得从数据库、其他 runtime 文件
或原 PDF 手工补入。图片/OCR/PDF locator 的完整链已由本地 visual audit 验证；packet 只暴露任务
所需的 OCR 文本、Paragraph locator、visual evidence/chart IDs、质量状态和关系。当前 result schema
只输出 `evidence_paragraph_ids`，导入器由视觉 Paragraph 解析 evidence IDs；不得增加 schema 未声明
的字段。没有自动外发。

`result_schema_sha256` 是解析 JSON 后按项目 canonical JSON 规则序列化所得 hash，不是包含导出文件
末尾换行的原始文件 hash；`packet_object_sha256` 和 `prompt_sha256` 分别绑定原始 packet 与提示词
字节。

三位作者的 `PARAGRAPH_AUX_ARGUMENT_FINAL_V3` 纯文本本地运行和 packet export 已完成，但其中的
知乎 `[图片]` 仍只是占位符，尚无图片快照、DOM 定位、OCR 和受影响 AU 重建。因此这些包只能标记
为 `PROVISIONAL_TEXT_VIEW`，不得启动最终 OpenCode/DeepSeek 筛选：

| 作者 | semantic run | 文本 packet 数 | 当前视觉状态 |
| --- | --- | ---: | --- |
| Mr. Dang | `knowledge-semantic-run:bfc6c8981f5293c0417b895d43f4ae25db9a3cef5b63495c8a4072153e8b10d7` | 43 | `PROVISIONAL_TEXT_VIEW` |
| 黄彦臻 | `knowledge-semantic-run:5da9ae360128b7346797fc217aed2029b475688fed62e65ccb5cb094b86c68d9` | 136 | `PROVISIONAL_TEXT_VIEW` |
| 派大星皮皮 | `knowledge-semantic-run:a93acd607ffaf01f0d9c7678ee1902971a8d4d7002b8102345b4b04a8898f434` | 1,126 | `PROVISIONAL_TEXT_VIEW` |

知乎须由后续 Codex Spark 或 OpenCode 本地任务先完成不可变图片证据链和受影响 AU 重建。证据
冻结前不得猜测未来 batch ID、目录、图片数或 READY 计数。

## K5-D5：知乎图片证据链本地工作包

如果当前 OpenCode 会话承担代码与本地处理，先完成本节，再为三位作者生成新的视觉增强批次；
如果当前会话只承担 DeepSeek JSONL 筛选，则跳过本节，继续保持知乎批次阻断。不得让语言模型
直接读取浏览器 Cookie、Profile 或登录凭据，也不得把 `[图片]` 占位符、远程 URL 或 OCR 猜测当作
图片证据。

范围仅限三位白名单作者已冻结的回答、文章、专栏正文和想法中的正文图片。处理顺序和状态固定为：

`PROVISIONAL_TEXT_VIEW → IMAGE_URL_INVENTORIED → ACCESS_POLICY_VERIFIED →
IMAGE_SNAPSHOT_FROZEN → DOM_LOCATED → OCR_ATTEMPTED → VISUAL_CLASSIFIED →
CONTEXT_ASSEMBLED → AFFECTED_AU_REBUILT → PACKET_READY`

逐张图片必须完成以下可审计步骤：

1. 从已冻结 SourceItem 快照和正文 DOM 中枚举图片，绑定真实 `source_snapshot_id`、内容类型、
   作者、正文 ordinal、原 URL hash 和稳定 DOM locator；不从当前页面列表反推历史内容。
2. 优先复用 AStock 本地采集插件已保存的图片对象；仅当本地链缺失时，才在用户已登录的 Chrome
   页面中按正常可见路径获取。先写 ObjectStore，再登记 `image_snapshot_hash`；不得绕过登录、
   验证码、反爬或访问控制，不得把 Cookie、Profile、签名 URL 或私有路径写入工件。
3. 对冻结图片对象运行本地 OCR，并登记 OCR input hash、text object hash、引擎和模型版本、
   参数、置信度及确定性状态。原图和 OCR 文本不可覆盖；重跑产生新 attempt。
4. 将图片分类为 `CHART / TABLE / DIAGRAM / TEXT_IMAGE / DECORATIVE / UNKNOWN`。非装饰图片
   生成带 locator 和 evidence ID 的视觉 Paragraph；该 Paragraph 永远
   `standalone_distillable=false`。
5. 按原始 DOM 顺序装配前后 Paragraph。图表夹在论点和结论之间时固定
   `MERGE_WITH_BOTH`，形成包含“前文论点 + OCR/图表证据 + 后文结论”的完整
   ArgumentUnit；只重建受影响 AU，不覆盖纯文本 run。
6. 生成新的 immutable semantic run、三视图 Embedding manifest、coverage report 和 packet。
   Paragraph current/local 仍只作辅助，最终相关性和方法完整度只在完整 AU 上判断。

以下任一情况必须 fail closed，不得进入最终 packet：

- 无法证明正常访问策略：`BLOCKED_ACCESS_POLICY`；
- 图片对象未先冻结或 hash 不符：`SNAPSHOT_FAILED`；
- DOM locator 与冻结正文无法重放：`LOCATOR_STALE`；
- OCR 失败、低置信、疑似信息图但无文字、分类 `UNKNOWN`、缺前后文或跨内容项：
  `REVIEW_REQUIRED`。

完成后必须报告逐作者的 SourceItem、图片引用、唯一图片对象、OCR 状态、分类、受影响 AU、
`READY / REVIEW / BLOCKED` 数，并写入真实 run、coverage、manifest、batch 和对象 hash。对象缺失、
待消费信封、SQLite/Parquet 错配、跨作者引用和图片孤立候选均必须为 0。未达到这些门禁时，
不得把三位作者状态改为 `PACKET_READY`，也不得提前调用 DeepSeek。

## 激活条件

每个可执行批次必须同时满足：

- `manifest.json` 的 `schema_version` 为 `semantic-deepseek-input-manifest-v3`；
- `embedding_contract_version=PARAGRAPH_AUX_ARGUMENT_FINAL_V3`；
- `packet_contract_version=COMPLETE_ARGUMENT_UNIT_V2`；
- 本地书批次已经过 visual audit，packet 中每个视觉段都有 PDF locator、OCR text hash、
  `paragraph_id`、`argument_unit_id`、`visual_evidence_id` 和 `visual_chart_unit_id`；
- 未来知乎视觉包中的每张非装饰图片必须自带
  `source_snapshot_id`、`image_snapshot_hash`、DOM locator、OCR input/text hashes、
  `paragraph_id`、`argument_unit_id` 和 `visual_evidence_id`；
- 图片 Paragraph 均为 `standalone_distillable=false`；位于论点与结论之间的图表均为
  `MERGE_WITH_BOTH`；
- OCR 失败、低置信、疑似有信息但无文本、`UNKNOWN` 或上下文不完整的 AU 均显式
  `REVIEW_REQUIRED`；
- `transport_policy=LOCAL_ONLY_MANUAL_NO_AUTO_SEND`；
- `packet.jsonl`、`result-schema.json` 与 manifest 中的哈希、AU 清单和行数全部一致；
- batch 状态为 `PACKET_READY`，且没有自动外发。

满足后，在仓库根目录按 `OPENCODE_DEEPSEEK_PROMPT.md` 逐批处理。只允许读取该批次目录内的
`packet.jsonl`、`result-schema.json` 和 `manifest.json`，输出同目录
`deepseek-results.jsonl`。某批失败时不得留下部分最终文件。

每一行的 `argument_text` 是完整 ArgumentUnit；内部 `paragraphs` 只用于定位、角色、关系和证据
引用，不得拆成独立判断输入。每个 `argument_unit_id` 恰好输出一条 JSONL 并原样保留
`input_sha256`。候选证据只能引用本 AU 的 Paragraph；不得创造事实、批准状态、收益判断或交易
指令。

未来知乎可执行任务必须写入实际 batch ID、目录、packet/schema/prompt hashes，以及逐作者 AU、图片、
`READY/REVIEW/BLOCKED` 计数。只允许读取冻结批次内明确列出的 packet/schema，唯一输出为同目录
原子生成的 `deepseek-results.jsonl`；缺行、重复 AU、哈希不符或越界引用时整批零导入。

《价值投资功法》的上述 11 行批次可以开始；知乎任务继续停在图片证据准备边界。
