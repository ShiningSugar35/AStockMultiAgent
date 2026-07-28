# AStock Research

这是一个面向 A 股研究、证据审计和模拟交易的本地模块化单体。主要交互入口是 Codex，确定性工作由 Python 3.12 的 `astock` CLI 完成。

长期架构、未完成工作和当前验收事实分别维护在根目录的
[低成本A股多Agent投研系统方案.md](低成本A股多Agent投研系统方案.md)、
[开发计划.md](开发计划.md) 和 [验收报告.md](验收报告.md)。Phase 7 的运行计数由
[phase7_status.md](phase7_status.md) 持续生成；Phase 6 recorded 决策闭环见
[phase6_status.md](phase6_status.md)。三者不再在 `docs/` 保留重复副本。

当前阶段矩阵：

| 阶段 | 当前状态 | 边界 |
|---|---|---|
| Phase 0～4 | PROGRAM COMPLETE | 原生入口、可恢复状态、行情/账本、证据/PIT、财务可信度和通用投资内核已实现 |
| Phase 5 | PARTIAL / PROVISIONAL | 三位作者纯文本 v3 包已生成但等待知乎图片证据；《价值投资功法》74/74 图片已 OCR、定位并进入视觉 AU，11 个 READY AU 已冻结为本地包，44 个保留 REVIEW |
| Phase 6 | PROGRAM COMPLETE / RECORDED ACCEPTANCE PASS | 300750 recorded 链已覆盖研究、四成员冻结委员会、公共 TradeProtocol、签名人工确认和单一模拟订单；不代表当前公司研究 |
| Phase 7 | PROGRAM COMPLETE / COLLECTING (0/100) | 真实前向闭环已实现；本地 runtime 因并行 0044 校验和漂移只读，三类评估均保持 `COLLECTING` |
| Phase 8 | P8.0 ONLY | 默认禁用状态壳已实现；P8.1～P8.4 未进入 |

Phase 5 后续底座矩阵：

| 能力 | 当前状态 | 边界 |
|---|---|---|
| Serenity v2 | ACCEPTED | 两个固定开源 commit 映射到 6 个 v2 方法合同；用户候选路由尚未接通 |
| Provider Registry | ACCEPTED | 七个免费 Provider；recorded 默认、live 显式开启 |
| Reference foundation | ACCEPTED | 主数据、日历、未复权日线和公司行动线索；不写模拟账本 |
| 财务事实源 | ACCEPTED | 东财/Sina 辅助定位；只有精确官方 PDF 证据可认证，当前真实 release 为 0 |
| 候选注册表 | ACCEPTED | typed/PIT/覆盖证明和审计闭合；候选仅表示值得研究，不含交易指令 |

Phase 5 正文类型矩阵：

| 类型 | 程序能力 | 线上覆盖 |
|---|---|---|
| 回答 | AVAILABLE | PARTIAL |
| 想法 | AVAILABLE | PARTIAL |
| 文章 | AVAILABLE | PARTIAL |
| 专栏 | CONTRACT_GATE | B1b0 已验收；B1b1 等待真实枚举快照，`BLOCKED_EXTERNAL_OBSERVATION` |

线上覆盖审计统一使用冻结截点，默认保留 30 秒静默窗口；仅在调用方确认停写时使用 `--quiescence-lag-seconds 0`。

```powershell
uv sync --all-groups
uv sync --extra semantic
uv run astock init
uv run astock probe
uv run astock knowledge-semantic-model-status
uv run astock knowledge-semantic-plan <author-source-id>
uv run astock knowledge-semantic-run <author-source-id>
uv run astock knowledge-semantic-embedding-run <semantic-run-id>
uv run astock knowledge-semantic-packet-export <semantic-run-id>
# 按 OPENCODE_DEEPSEEK_TASK.md 在 OpenCode 生成结果后：
uv run astock knowledge-semantic-result-stage <batch-id> <result.jsonl>
uv run astock knowledge-semantic-result-import <batch-id>
uv run astock provider-list
uv run astock provider-probe baostock-reference
uv run astock provider-status baostock-reference
uv run astock sync-instruments
uv run astock sync-calendar --exchange XSHG --start 2026-07-01 --end 2026-07-31
uv run astock sync-daily 600519 --market XSHG --start 2026-07-01 --end 2026-07-31
uv run astock reference-audit
uv run astock sync-financial 000001 --market XSHE --period-end 2025-12-31
uv run astock financial-source-status 000001 --period-end 2025-12-31
uv run astock financial-source-audit
uv run astock candidate-scan <request.json>
uv run astock candidate-status --scan-id <scan-id>
uv run astock candidate-audit <scan-id>
uv run astock sync-5m 600519 --market XSHG --start 2026-07-01 --end 2026-07-13
uv run astock quality-report 600519 --market XSHG
uv run astock private-pdf-ingest <private.pdf> --source-id <id> --title <title> --author-source-id <author-id> --file-version <version> --full
uv run astock private-docx-ingest <private.docx> --source-id <id> --title <title> --author-source-id <author-id> --file-version <version>
uv run astock paper-replay 600519 --market XSHG --cursor 2026-07-13T15:00:00+08:00
# Phase 6 recorded 验收：分析只生成冻结协议，不会创建订单
uv run astock analyze 300750
uv run astock phase6-status 300750
# Phase 7：先冻结研究与决定，未来行情实际可得后再冻结观察证据
uv run astock shadow-study-create <study-request.json>
uv run astock shadow-assign <assignment-request.json>
uv run astock shadow-forward-market-freeze <assignment-id> --symbol 600519 --market XSHG --valuation-time <iso-time>
uv run astock shadow-observation-record <observation.json>
uv run astock shadow-evaluate <study-id> --as-of <iso-time>
uv run astock phase7-status-update
uv run pytest
uv run ruff check .
uv run pyright
```

运行数据默认写入 `runtime/`，不会进入 Git；私有 PDF、DOC 和 DOCX 也被显式忽略。项目不提供自动实盘下单接口，也不要求配置外部大模型 API Key。

`paper-replay` 使用 `configs/fee_rules.yaml`。印花税和过户费按公开规则留有来源与生效日期；示例券商佣金必须在正式影子账户前按自己的协议确认。北交所当前 5m 实测为东财单源等级，且默认费用档案尚不覆盖北交所，因此不会被误当成已验证回放。

Phase 7 只接受在研究信号后真实采集并冻结的未来行情。价格、成交量、MFE/MAE 必须与不可变 5m
OHLCV 明细一致，同一 Memo 或 Decision 只能计为一个独立事件。历史回放可以用于探索，但永远不
增加 100 个正式独立研究事件的计数；系统不做强化学习、动态调权或自动修改 Skill。
