# Architecture Decision Records

> 状态：CURRENT
> 更新日期：2026-09-07

## 目的

ADR 用于记录会影响多个模块、多个 Agent 或长期维护成本的重要决策。它回答“为什么这样设计”，而不是复制代码或写任务流水。

## 状态

- `PROPOSED`：待批准或待实现；
- `ACCEPTED`：已接受，后续变更必须显式替代；
- `SUPERSEDED`：被具名 ADR 替代；
- `REJECTED`：评审后不采用；
- `DEPRECATED`：仍有历史引用，但不再用于新实现。

## 编号与模板

文件名：`NNNN-short-kebab-title.md`。编号只增不复用。

```markdown
# ADR-NNNN: 标题

> 状态：PROPOSED | ACCEPTED | SUPERSEDED | REJECTED | DEPRECATED
> 日期：YYYY-MM-DD
> 决策者：...
> Supersedes / Superseded by：...

## Context
## Decision
## Consequences
## Alternatives considered
## Compliance and verification
```

旧 ADR 不删除；新决策通过 `Superseded by` 建立可追溯替代链。

## 索引

| ADR | 状态 | 决策 |
|---|---|---|
| [0001](0001-documentation-as-code-with-machine-contracts.md) | ACCEPTED | Markdown 保留为解释与决策层，机器合同和运行存储拥有执行权 |
| [0002](0002-market-regime-as-risk-overlay.md) | ACCEPTED | 市场状态只调整风险预算和研究供给，不替代个股证据与估值 |
