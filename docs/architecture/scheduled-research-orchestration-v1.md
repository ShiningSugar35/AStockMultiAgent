# ChatGPT / Codex 定时投研适配架构 v1

> 状态：PROPOSED
> 是否已实现：否
> 版本：scheduled-research-orchestration-v1
> 更新日期：2026-09-07
> 适用范围：AStockMultiAgent 的持续跟踪、定时语义研究与用户通知；不授权真实交易或无确认模拟交易

## 1. 结论

ChatGPT/Codex 的定时任务可以补足“一问一答结束后没有语义研究 Agent 持续在线”的缺口，但它**不能替代**本地 Continuous Monitor，也不能成为新的行情、账户、研究事实或任务事实源。

目标分工固定为：

```text
本地 Continuous Monitor（确定性事实/任务平面，常驻）
  ├─ 交易日历、游标、节流、provider fallback
  ├─ 行情/公告/新闻线索/催化剂的低成本增量采集
  ├─ 事件去重、重大性初筛、持久任务、开放模拟订单回放
  └─ SQLite / Parquet / ObjectStore 机器事实
                       ↓ 只消费增量
ChatGPT / Work / Codex Scheduled Task（语义研究/通知平面，按时唤醒）
  ├─ 读取统一 preflight 与待研究任务
  ├─ 对重大增量调用适用 Skills/Workflows 做定向复核
  ├─ 形成 typed delta、动作建议与 MaterialChangeDigest
  └─ 有重大变化才通知；无变化只写 NO_MATERIAL_CHANGE receipt
```

定时 Agent 不得每小时重新扫描全市场、重建全账本或重跑所有已完成研究；不得直接追加外部真实账户事件、创建/确认模拟订单、修改持仓、调低正式推荐门，或在无法读取本地事实时假装已完成持仓复核。

## 2. 已核实的平台边界

依据 OpenAI 2026-09-07 可见的官方说明：

- ChatGPT Scheduled 支持一次性、周期性、变化监控和受支持的事件触发任务；符合条件的付费计划可配置**最高每小时一次**的周期和精确执行时间。本项目不假设存在亚小时定时能力：亚小时轮询继续交给本地 Continuous Monitor。
- 活动任务数量、可用模型、额度和事件触发资格随计划与工作区设置变化；这些属于创建预览时的动态 capability probe，不写死进业务 Schema。
- 在普通 ChatGPT Project 中创建的 Scheduled Task 不能访问该 Project 上传或保存的文件，因此不能据此假定任务能读取 `D:\AStockMultiAgent`。
- ChatGPT Desktop 中获准的本地 Scheduled Task 可以在项目目录或隔离 worktree 运行；依赖本地文件时，电脑必须开机、桌面应用必须运行、项目路径必须仍可访问。
- Web/Cloud Scheduled 可以使用该任务实际可见的上传上下文、连接工具、Skills 或 Plugins，但不能直接访问电脑本地目录，也不得把聊天记忆当作本地账本。
- 独立定时任务每轮从保存的提示词启动；建在既有聊天中的定时任务返回同一聊天并复用其上下文。Codex CLI/IDE 不提供 Scheduled 管理界面；应在 ChatGPT Web/Desktop 创建和管理，并先在普通聊天中测试提示词。
- 定时任务默认使用最窄沙箱和权限；扩大网络或文件写入权限必须有具名理由。涉及外部动作且平台要求审批时，任务会暂停等待用户批准。
- 本次核实到的官方说明描述的是 ChatGPT Web/Desktop 内的原生创建与管理流程；本项目不把一个稳定、可由仓库代码直接调用的 Scheduled Task 公共 API 当作既有能力。适配器首先支持“用户原生创建 → 返回/录入 opaque task id → 本地绑定与校验”；未来只有在重新核实官方 API、权限和版本合同后才增加 direct-create driver。
- 受支持的事件触发目前不是任意本地事件 webhook。AStock 本地 Monitor 仍以 durable queue/watermark 保存事件，再由时间型 Scheduled Task 有界消费；不得假设本地事件可以直接唤醒云端任务。

官方依据：

- [Scheduled tasks in ChatGPT](https://help.openai.com/en/articles/10291617-tasks-in-chatgpt)
- [ChatGPT Work and Codex](https://help.openai.com/en/articles/20001275-chatgpt-work-and-codex)
- [Scheduled tasks / Automations](https://developers.openai.com/codex/automations)

平台能力会更新，实施时必须重新核验官方文档、当前账户计划、工作区权限和桌面应用版本；本文不把当前任务上限、模型名称或界面名称硬编码进业务 Schema。

## 3. 为什么采用双层而非“让 GPT 每小时全做一遍”

### 3.1 本地 Monitor 擅长的工作

- 高频、低成本、可重复的轮询和增量抓取；
- 交易日历、available-time/PIT、原始响应先落 ObjectStore；
- provider 熔断、退避、游标、幂等、任务 lease；
- 账户/订单的确定性状态转换与审计；
- 即使没有可用 GPT 会话，也能保留事件和待办，之后补消费。

### 3.2 定时语义 Agent 擅长的工作

- 将已经发生的重大变化与原 thesis、估值、财务、治理、行业、宏观和组合上下文串联；
- 对事件做权威来源复核、反证、影响路径和动作建议；
- 根据用户持仓、研究标的和风险偏好生成可读摘要；
- 在同一上下文中持续跟踪一个有明确结束条件的临时事项。

### 3.3 禁止的重复建设

- 不新建 ChatGPT 私有“持仓记忆”替代 external-account/paper SQLite；
- 不让定时提示词维护第二份标的列表，目标集合来自 `ResearchSubjectRegistry`、实际/模拟持仓和开放订单；
- 不让 ChatGPT 自己记住上次游标，watermark/lease/run receipt 必须本地持久化；
- 不以 Scheduled 页面历史代替项目运行审计；
- 不让一个无本地目录权限的 Web 任务降级为凭聊天记忆报告账户或市场事实。

## 4. 建议的产品形态

### 4.1 用户友好的创建预览

系统不直接创建任务，先显示一张自然语言预览：

| 项 | 必须展示的内容 |
|---|---|
| 任务名 | 不含账户号、持仓明细等敏感信息的通用名称 |
| 目的 | 例如“检查已持有/已研究标的是否出现重大变化” |
| 执行面 | Desktop 本地项目 / Web 云任务 / 既有聊天定时任务 |
| 时间 | 明确时区、交易日条件、频率和首次运行时间 |
| 创建方式 | 用户原生创建并绑定 / 经重新核实的官方 API；不得显示尚不存在的“一键自动创建” |
| 数据范围 | actual、paper、open order、研究 registry、宏观/公告/行情增量中的哪些 lane |
| 披露级别 | `MINIMUM` / `ACTIONABLE` / `EXPLICIT_FULL`；默认 `MINIMUM`，明确哪些字段会进入任务上下文 |
| 权限 | 默认只读；是否允许写研究 metadata/任务 receipt；经济写入永远不允许 |
| 通知 | 哪些重大性等级通知；无变化如何处理；失败是否通知 |
| 资源 | 占用一个活动任务槽；每次运行会消耗相应计划额度，频率可降级 |
| 停止条件 | 固定到期、事件解决、连续无变化次数、用户手工暂停/删除 |
| 回滚 | 暂停/删除 Scheduled binding，不删除本地事实和历史 receipt |

只有用户确认预览后，才进入平台侧创建；任务标题和提示词不嵌入账户号码、成本、持仓数量或其他敏感金融详情，而是在运行时从获准的本地事实源读取。

### 4.2 数据最小化与云端上下文边界

本地项目可被桌面任务访问，不等于整个处理过程天然“只在本机”。任务消息、提示词、工具返回的选定内容和会话上下文可能进入平台侧处理/保存范围，因此必须把数据最小化做成机器 policy，而不是只把任务标题写得模糊。

- 定时 Agent 只调用专用的只读导出入口，获取已脱敏的 preflight/delta；禁止直接把整个 SQLite、完整交易历史、私有研究原文或凭据灌入上下文。
- 默认 `MINIMUM`：仅提供匿名 lane、证券身份、组合暴露比例、重大事件和必要 thesis delta；不提供账户号、完整成交流水、精确现金余额或本地绝对路径。
- `ACTIONABLE` 只在形成具名持仓动作确有必要时增加成本区间、数量区间和账户别名；`EXPLICIT_FULL` 必须逐任务显式同意，且凭据、令牌和未相关私有文件仍永不披露。
- 本地 receipt 保存原始 lineage；平台 binding 只保存 opaque id、policy hash、披露级别和最小摘要。披露 policy、用户同意、执行面和任务版本必须可审计、可撤销。
- 用户预览必须明确说明：哪些数据离开本地工具边界、由哪个执行面处理、如何暂停/删除 binding；不能用“本地项目”暗示零云端处理。

### 4.3 推荐任务模板

#### A. 交易时段每小时重大变化复核

- 日程：仅 A 股交易日，交易时段内最多每小时一次；交易时区使用 `Asia/Shanghai`，通知可显示用户所在时区。
- 输入：自上次 watermark 后的 `HIGH/CRITICAL` 事件、实际/模拟持仓、开放订单、明确研究中的标的。
- 行为：先做统一 preflight；没有新重大事件则 `NO_MATERIAL_CHANGE`；有事件只研究受影响标的及其组合传导。
- 不做：全市场荐股、重复完整公司研究、创建订单、根据新闻线索直接改仓。

#### B. 每个交易日收盘后组合与事件摘要

- 日程：收盘且当日行情/公告 watermark 完成后运行，不仅依赖固定钟点。
- 输入：当日持仓变化、成交/开放订单、重大公告、价格/波动异常、市场状态变化。
- 输出：组合重大变化、需执行/需复核事项、失效 thesis、次日观察条件；actual/paper 分 lane。

#### C. 每周研究清单与风险复盘

- 日程：每周一次。
- 输入：`RESEARCHED/RECOMMENDED/HELD` registry、未解决事件、研究陈旧度、宏观/市场状态周变化。
- 输出：需要刷新、降级、移出、继续观察的标的；只对真正变化做深度研究。

#### D. 有限期催化剂/订单跟踪

- 形态：优先使用聊天内定时任务，复用正在处理的上下文；每次提示词包含明确检查项和结束条件。
- 结束：公告落地、订单终结、催化剂窗口结束、达到最大运行次数或需要用户裁决。

默认不建议创建多个按公司拆分的小时任务；应按“组合/研究集合 + 重大增量”聚合，以节省活动任务槽、模型额度和通知噪声。

## 5. 执行位置选择

| 场景 | 推荐位置 | 理由 |
|---|---|---|
| 读取 `D:\AStockMultiAgent` 的 runtime/SQLite/配置 | Desktop Work/Codex 的本地项目 | Web 任务不能直接访问本机目录 |
| 只查公开网页、不依赖本地账户 | Web/Cloud Work Scheduled | 云端执行不依赖本机常开，但不能声称完成本地持仓、订单或账本复核 |
| 连续跟踪当前聊天里的临时事项 | 聊天内 Scheduled | 复用上下文并具备结束条件 |
| 修改源代码或修复缺陷 | 独立 worktree | 隔离未完成代码；不与运行时投研任务混用 |
| 日常投资监控与本地 runtime 状态 | 主项目只读/受限 workspace-write | ignored runtime DB 默认不随 worktree 复制；必须显式绑定共享 runtime 才能用 worktree |

投资监控任务默认不修改源码。若平台只允许 `workspace-write`，应通过 Rules/allowlist 只开放经过审计的只读或 metadata-only CLI；禁止 full access 作为默认设置。

## 6. 机器合同

下一步实现下列版本化对象；平台任务本身只保存 binding，不保存业务事实：

### 6.1 `ScheduledResearchPolicy`

- `policy_id/version/hash`
- 允许的 task template、最低/最高频率、时区和交易日条件
- 可读 lane、可写 metadata 类型、禁止的经济副作用
- materiality 阈值、每次最大标的/事件/外部调用预算
- retry/backoff、catch-up、max lag、stop conditions
- notification policy 与 no-change policy
- disclosure level、field allowlist/redaction、cloud-context acknowledgement 与 consent hash

### 6.2 `ScheduledTaskBinding`

- 本地 `binding_id` 与平台侧 opaque task id
- creation mode：`NATIVE_UI_BOUND/OFFICIAL_API_VERIFIED`，以及创建能力探测证据；未核实 API 时不得出现 direct-created 状态
- execution surface：`DESKTOP_LOCAL/WEB_CLOUD/EXISTING_CHAT`
- project/worktree/runtime-root binding
- schedule/时区、active/paused 状态、policy hash
- created/confirmed/paused timestamps；不得保存平台凭据或敏感提示词快照

### 6.3 `ScheduledResearchRunRequest/Receipt`

- 幂等键：`binding_id + schedule_bucket + source_revision_set + policy_hash`
- claimed watermark、统一 preflight receipt、capability plan/coverage receipt
- 输入事件/任务 id、输出 artifact id、耗时/usage/fallback
- 结果：`MATERIAL_CHANGE/NO_MATERIAL_CHANGE/DEGRADED/BLOCKED/FAILED`
- next watermark、需要用户动作、通知决策与原因

### 6.4 `MaterialChangeDigest`

- 事件/来源/available time；
- 受影响的 actual/paper/研究标的和组合暴露；
- 原 thesis/预期与实际 delta；
- 动作：继续持有、复核、降低风险、等待、移出候选等，以及触发条件；
- 置信度、缺口、何时复查；
- 不含内部 CLI 流水、Schema、provider 故障细节。

## 7. 幂等、并发与恢复

1. 同一 `binding_id + schedule_bucket` 只能存在一个 accepted run；平台重复唤醒返回既有 receipt。
2. worker 使用有 TTL 的 lease；异常退出后可接管，但不得重复提交 metadata、通知或研究任务。
3. watermark 只在 receipt 和全部输出持久化后前移；失败保留旧 watermark。
4. 电脑/应用未运行造成漏跑时，下次只从 watermark 做**有界补采**，不无上限追赶；超过 max lag 进入 `DEGRADED` 并要求人工复核。
5. 手工问答与定时任务共享 subject/event claim，避免同一事件重复深研；手工高优先级可抢占，但双方都写清 disposition。
6. actual、paper、open order、研究 metadata 分 lane；任何 scheduled run 的经济写入均为 0，经济副作用计数必须为 0。
7. `NO_MATERIAL_CHANGE` 仍写轻量 receipt 以证明任务执行过；若平台无法真正静默通知，提示词只返回一行无变化摘要，并通过降低频率/合并模板控制噪声，禁止假称平台支持不可验证的“零通知”。

## 8. 失败与降级语义

| 故障 | 正确行为 |
|---|---|
| 本地电脑/应用关闭或项目路径不可达 | 不运行或标记 missed；恢复后按 watermark 有界 catch-up |
| Web 任务无本地文件权限 | 只能做公共信息研究；禁止报告已核对本地持仓/订单 |
| local Monitor heartbeat/staleness 不合格 | `BLOCKED/DEGRADED`，提示先恢复事实平面，不用 Web 结果冒充完整监控 |
| preflight/账本审计失败 | fail closed，不把失败解释为空持仓 |
| provider、网页、模型或 Skill 失败 | 保存 coverage gap；能做局部结论时明确降级，否则不通知投资动作 |
| 任务槽/使用额度不足 | 保留本地待研究队列；合并或降低语义任务频率，不降低本地确定性采集 |
| 多个任务重叠 | lease/coalescing 合并增量；不同时研究同一 event/subject |
| 平台任务暂停/删除 | binding 状态同步为 PAUSED/DETACHED；不删除本地事件、研究和运行收据 |

## 9. 验收标准

### 9.1 平台适配合同

- 创建前完成普通聊天手工 dry-run，提示词、Skills、权限、输出均可审查；
- 原生创建与 opaque binding 路径可验证；没有重新核实的官方 API 时 direct-create 调用数为 0；
- 默认 `MINIMUM` 披露，专用只读导出字段全部在 allowlist 中；整库 SQLite、完整交易历史、凭据和无关私有原文进入任务上下文的次数为 0；
- 执行面、披露级别、云端上下文提示和用户同意均绑定 policy hash，撤销后后续运行被阻断；
- local/web/existing-chat 三种 execution surface 的能力边界有反例测试；
- 计划/工作区不支持的频率或活动任务数会在预览时阻断，而非创建后静默降级；
- 时区、交易日、停止条件、通知策略和敏感信息检查全部显式。

### 9.2 业务安全

- scheduled run 的 external-account 事件、paper order/fill、broker execution 写入均为 0；
- actual/paper/open order 不混淆；空持仓用户不出现“当前无持仓”噪声；
- 本地事实不可读时，声称“已检查持仓/订单”的次数为 0；
- 未通过正式准入的股票不会因为定时任务或健康牛市被升级为正式 BUY。

### 9.3 持续运行

- 同一 bucket 重复触发 100 次只产生一个 accepted receipt/一份通知；
- 中断发生在 claim、研究、持久化、通知前后的故障注入均可恢复且不丢 watermark；
- missed-run 有界 catch-up、超期降级、暂停/恢复/删除 binding 全部可审计；
- 无重大变化时不进行全市场研究或重复抓取，外部调用和模型使用满足 policy budget；
- 手工会话与 scheduled run 同时命中同一事件时只有一个语义研究 owner。

### 9.4 用户体验

- 小时任务只报重大变化，日终任务给可执行摘要，周任务给研究清单治理，职责不重复；
- 每项重大变化都有“影响什么、现在做什么、什么条件改变判断”；
- actual/paper 分列，但空 lane 静默；
- 失败消息说明缺失的能力和下一次处理方式，不倾倒内部命令或日志。

## 10. 启用顺序与回滚

启用顺序：

1. run receipt/idempotency/lease 与只读 dry-run；
2. Desktop local 手工 `Run now`，只消费 fixture；
3. recorded 每小时模板，通知关闭；
4. controlled-live 单一日终模板，人工审阅前几次结果；
5. 小时重大变化模板；
6. 周度 registry 治理；
7. 用户显式批准后才设为长期 active。

回滚只需暂停/删除平台 Scheduled Task 并将 binding 标记为 `PAUSED/DETACHED`；本地 Continuous Monitor、账户/模拟账本、ObjectStore、研究 registry 和历史 run receipt 均保持不变。任何平台适配失败都不得迫使系统关闭确定性 Monitor。
