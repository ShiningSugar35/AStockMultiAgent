# Phase 6 投资决策闭环状态

生成日期：`2026-07-27`

当前状态：`PROGRAM_COMPLETE / RECORDED_ACCEPTANCE_PASS`

## 已闭合链路

`ResearchRequest → EvidenceCollectionTask → EvidenceCollectionRun → EvidencePack →`
`FrozenEvidencePack → BaseCase → SpecialistRoute → Serenity Delta → 知乎专家 Delta →`
`ResearchMemo → Investment Committee → TradeProtocol → PaperExecutionRequest →`
`独立签名人工确认 → Paper Ledger`

委员会的四个必需角色是 `BASE_CASE`、`SERENITY_DELTA`、`ZHIHU_EXPERT_DELTA` 和
`FINANCIAL_INTEGRITY`。成员只能绑定已注册的冻结工件及其精确对象哈希；委员会运行时
`network/browser/MCP/broker` 均为关闭状态。

`TradeProtocol` 的公共结果仅为：

- `WATCH`
- `REJECT`
- `NEEDS_INFO`
- `APPROVE_SIMULATION`

只有 `APPROVE_SIMULATION` 可以准备模拟执行请求。方向、数量和限价必须由调用者显式给出；
准备请求不写账本。只有独立签名且与请求哈希完全绑定的人工确认通过后，才允许创建一张模拟订单。
确认对象和验签公钥均进入不可变 ObjectStore，后续审计可重新验证签名。系统没有实盘或券商执行入口。

## 300750 recorded 验收

| 项目 | 冻结身份 / 结果 |
|---|---|
| run | `dd9c81fd959bab5bd93ea7b784a8d2cf32702291cf96a329ec9a65d70d8d2377` |
| ResearchMemo | `research-memo:fb807504c03e9c4166c126df94f502f9def26d102fd3b4f0e6df5c2d24a708f4` |
| CommitteeDecision | `decision:73785b71129ff05b2fbece892721e7c3a0c3a3a8126f3bdb118cde4eee28f0ae` |
| TradeProtocol | `trade-protocol:138aa138879ec1b197c969b054418ad234bc70feaec2fedecc943f1af4162f6a` |
| 公共结果 | `APPROVE_SIMULATION` |
| PaperExecutionRequest | `1b61d5460c78528f0a7670c04cbab1b9a9937a0105e0f80fcfb75389a9d902b5` |
| 人工确认 | `cf8b46906dfdd0feb810b7b32bcf9c3ea5bccbae4850dcfb8ad049e8ed165ca9` |
| Paper order | `7246216b7e53454e96ecf0d95e053b82` |
| Phase 6 / execution audit | `PASS / PASS` |
| SQLite integrity | `ok` |

重复分析、重复准备和重复确认均复用同一确定性结果，只创建一张模拟订单。验收还模拟了
“订单已经写入、外层 checkpoint 尚未完成”的中断；恢复命令依据不可变操作记录将请求对齐为
`COMPLETE`，没有生成第二张订单。

完整命令与身份日志见
[`docs/evidence/phase6_recorded_300750.log`](docs/evidence/phase6_recorded_300750.log)。

## 验证状态

- Phase 6 端到端 acceptance：`1 passed in 19.44s`
- Phase 6、committee、paper、migration、open-source audit 定向回归：`63 passed`
- `uv run ruff check .`、`uv run pyright` 和全量 `uv run pytest`：最终提交前重跑并回填

## 明确保留的边界

- 本次 300750 是受控 recorded 软件验收数据，不是当前公司研究、投资建议或实盘授权。
- 生产研究入口已有通用冻结合同，但当前/实时 300750 仍取决于正式公告、财报和 PIT 数据可得性。
- recorded 验收使用明确标记的知乎专家测试 Delta；知乎图片证据完成冻结前，现有纯文本包仍是
  `PROVISIONAL_TEXT_VIEW`，不得冒充最终生产 Skill。
- 通用 live paper reference 的精确板块、风险警示、停牌和涨跌停分类工件仍须补齐；缺失时继续
  fail closed。
- 不实现自动 BUY/SELL、实盘、券商接口、强化学习或删除人工确认。
