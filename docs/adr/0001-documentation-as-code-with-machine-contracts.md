# ADR-0001: 文档即代码，但机器合同拥有执行权

> 状态：ACCEPTED
> 日期：2026-09-07
> 决策者：项目所有者 / 架构审查
> Supersedes：根目录多个 Markdown 共同、隐式承担全部项目真相的旧做法

## Context

项目已经拥有大量 Pydantic Schema、versioned config、SQLite/Parquet/ObjectStore、CLI、Skill、Workflow 和自动测试，但根目录 README、总体方案、开发计划和进度验收仍重复维护能力描述、阶段状态与验收结论。多 Agent 并行时，这会造成：

- Agent 读取到不同版本的“当前事实”；
- 已完成历史长期占据开发计划；
- README 过长，入口信息被命令和历史淹没；
- 规划文字被误当成已实现能力；
- 同一架构取舍反复讨论，缺少替代链；
- 机器行为无法仅凭 Markdown 自动验收。

## Decision

1. 继续采用 Markdown 作为 docs-as-code 的解释层，而不是弃用 Markdown。
2. 将项目真相拆成明确层级：
   - `AGENTS.md`：全局硬规则；
   - Schema/config/state machine/CLI：可执行合同；
   - SQLite/Parquet/ObjectStore/manifest：运行事实；
   - tests/audit：验收证据；
   - ADR/architecture/workflow/skill：决策、边界与方法；
   - `开发计划.md`：仅当前未完成工作；
   - `进度验收.md`：仅最近验证快照；
   - Git/tag/release/冻结文档：历史。
3. 新增 `docs/README.md` 作为文档治理和导航入口。
4. README 收敛为最小产品入口，不再复制全部命令和阶段历史。
5. 重要架构取舍使用 ADR；旧决策不删除，只能被新 ADR 显式替代。
6. 文档中的“已实现/可用”必须能够定位到机器合同和测试；否则只能标记 `PROPOSED`、`SHADOW` 或 `HISTORICAL`。
7. 使用 `planning/work_packages_v1.yaml` 作为当前工作包 ID、依赖、状态、优先级和唯一写入 lane 的机器索引；`开发计划.md` 只承担可读目标、验收与回滚说明，合同测试防止二者漂移。
8. `.ai-bridge/` 只用于单次执行的瞬时交接，不取得 canonical authority；旧 handoff 必须被当前 plan/run 显式替代，不能覆盖 Git、机器索引、运行事实或最近验收。

## Consequences

### Positive

- 多 Agent 获得统一的读取顺序和写入职责；
- 当前计划、当前事实和历史不再互相污染；
- 架构取舍可追踪，减少重复讨论；
- 文档可以进入 CI 做链接、状态、命令和合同检查；
- 工作包可被 Agent 调度器读取并在派工前检查依赖与写入冲突；
- README 更适合新成员快速启动。

### Costs

- 每次架构变更需要同步 ADR、架构文档和机器合同；
- 旧长文档需要标记状态而不是继续追加；
- 需要新增文档 lint/链接/状态检查，避免治理规则再次退化。
- 机器索引与可读计划仍需同步维护，因此必须依赖一致性测试，后续可再生成 Markdown 视图。

### Risks

- 只重写文档而不补机器合同，会产生“看起来更专业”的假完成；
- 过度使用 ADR 会增加噪声，因此只记录跨模块、长期或不可逆的重要决策。

## Alternatives considered

### 完全弃用 Markdown，全部迁入项目管理平台

不采用。外部平台不一定可离线、可版本化、可被本地 Agent 稳定读取，也不能替代 Schema、测试和运行存储。

### 保留现状，只压缩 README

不采用。根因是事实职责不清，不只是字数过多。

### 用一个超大“项目真相.md”取代现有文件

不采用。单文件会再次混合安全、架构、计划、状态和历史，且无法建立细粒度所有权。

## Compliance and verification

- `docs/README.md` 必须存在并列出事实层级；
- README 不再包含完整命令清单和阶段历史；
- `开发计划.md` 不保留已完成阶段流水；
- `进度验收.md` 只保留最近验证快照；
- 架构文档必须声明状态与实现边界；
- 后续增加文档合同测试，检查链接、状态头和 ADR 索引；
- 工作包机器索引与 `开发计划.md` 的 ID、标题、依赖、状态和 owner lane 必须一致；
- `.ai-bridge/` 交接不得继续引用已替代的 canonical plan 或 durable run。

## References

- AWS ADR process: https://docs.aws.amazon.com/prescriptive-guidance/latest/architectural-decision-records/adr-process.html
- AWS ADR best practices: https://docs.aws.amazon.com/prescriptive-guidance/latest/architectural-decision-records/best-practices.html
