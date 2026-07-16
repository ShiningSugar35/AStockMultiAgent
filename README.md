# AStock Research

这是一个面向 A 股研究、证据审计和模拟交易的本地模块化单体。主要交互入口是 Codex，确定性工作由 Python 3.12 的 `astock` CLI 完成。

Phase 0、M1 和 Phase 2 已完成：除项目原生入口、可恢复状态、双源 5 分钟行情和双重记账模拟账户外，现已包含官方文档快照、PDF/OCR、页/块 Evidence、PIT，以及私有 PDF/DOCX 的内容寻址摄入。知识清洗、Skill 蒸馏和知乎线上历史采集仍属于后续阶段。

```powershell
uv sync --all-groups
uv run astock init
uv run astock probe
uv run astock sync-5m 600519 --market XSHG --start 2026-07-01 --end 2026-07-13
uv run astock quality-report 600519 --market XSHG
uv run astock private-pdf-ingest <private.pdf> --source-id <id> --title <title> --author-source-id <author-id> --file-version <version> --full
uv run astock private-docx-ingest <private.docx> --source-id <id> --title <title> --author-source-id <author-id> --file-version <version>
uv run astock paper-replay 600519 --market XSHG --cursor 2026-07-13T15:00:00+08:00
uv run pytest
uv run ruff check .
uv run pyright
```

运行数据默认写入 `runtime/`，不会进入 Git；私有 PDF、DOC 和 DOCX 也被显式忽略。项目不提供自动实盘下单接口，也不要求配置外部大模型 API Key。

`paper-replay` 使用 `configs/fee_rules.yaml`。印花税和过户费按公开规则留有来源与生效日期；示例券商佣金必须在正式影子账户前按自己的协议确认。北交所当前 5m 实测为东财单源等级，且默认费用档案尚不覆盖北交所，因此不会被误当成已验证回放。
