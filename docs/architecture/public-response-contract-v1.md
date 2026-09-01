# Public Response Contract v1

## 1. 目的与边界

本合同定义 AStockMultiAgent 的唯一公共输出边界。它只投影既有冻结研究、组合、持仓、监控和模拟账户事实，不创建第二套事实源、Provider Router、Evidence、研究状态或交易账本。

- 普通投资问题默认 `INVESTOR`。
- 只有用户明确肯定地要求查看、检查、排查或调试内部状态时才进入 `DEVELOPER`。
- 系统自身出现错误、文本中被动包含 `provider / traceback / 数据库 / 接口错误`，以及“不要看日志”“不需要调试”等否定表达，均不得触发 Developer Mode。
- 两种模式都不得暴露 Secret、Cookie、Token、密码、请求体或私人绝对路径。
- `broker_execution_allowed=false` 为固定边界，展示层不得改变研究、推荐、模拟账本或真实交易权限。

## 2. 唯一规范字段

`src/astock/schemas/presentation.py` 是公共输出合同的唯一 Schema 源。`presentation.py` 和所有调用方只能使用该文件中的模型，不得在服务或测试中复制平行字段。

| 语义 | 唯一规范字段 | 兼容读取 | 规范序列化 |
|---|---|---|---|
| 方向事实 | `direction_terms` | `directions` | 只输出 `direction_terms` |
| 投资结论 | `headline` | `conclusion` | 只输出 `headline` |
| 用户期望详略 | `requested_detail` | 无 | `SHORT / STANDARD / DETAILED` |
| 主体 | `subject` | 无 | 必填 |
| 结论强度 | `conclusion_strength` | 无 | `UNSPECIFIED / NOT_CERTIFIED / LOW / MODERATE / HIGH` |
| 估值或赔率 | `valuation_or_odds` | 无 | 受限列表 |
| 报告引用 | `report_reference` | 内部可输入 `report_path` | 只输出安全文件名，不输出绝对路径 |

旧字段别名只用于反序列化兼容。调用方同时提交冲突的新旧字段时必须拒绝，而不是任选其一。

当前版本：

- `response-context-v1`
- `fact-fingerprint-v2`
- `research-narrative-bundle-v1`
- `investor-presentation-v1`
- `developer-diagnostics-v1`
- `presentation-audit-v1`
- `rendered-response-v1`

## 3. 公共投影的强制内容

`InvestorPresentationModel` 使用 allowlist，只允许：

1. 主体或公司名称；
2. 结论；
3. 结论强度；
4. 估值或赔率；
5. 最多若干条非关键理由；
6. 最大风险；
7. 改变判断的条件；
8. 数据截至时间；
9. 必要引用；
10. 安全化后的正式报告文件名。

只要源叙事提供了风险、变化条件、数据时点、估值/赔率、引用或报告引用，这些内容即属于强制字段。长度控制不得删除它们。

## 4. 事实等价门

事实保护由两个方向共同构成：

1. **禁止新增**：输出事实必须属于源叙事允许的事实集合；
2. **禁止删除或替换强制事实**：所有 required fingerprint 必须完整出现在最终输出。

锁定维度包括：

- 主体/公司名称等已知实体；
- 六位证券代码；
- 数值、百分比、金额、倍数；
- 日期和时点；
- 买入、卖出、加仓、减仓、持有、观望等方向；
- 结论强度；
- 引用；
- 风险、估值/赔率、变化条件、数据时点等规范化锁定短语。

任一维度被新增、删除或改变时：

- `fact_equivalence_status=FAIL`
- `fact_drift_detected=true`
- `safe_to_send=false`
- 网关不得发送原稿，只能进入安全降级。

## 5. 长度治理

允许的压缩顺序：

1. 去除精确或高相似重复理由；
2. 按任务预算减少非关键理由数量；
3. 保留所有强制字段和必要引用；
4. 若强制内容本身仍超过预算，返回固定安全摘要。

禁止通过删除风险、改变判断的条件、数据时间、估值/赔率、引用、主体、结论或结论强度来满足字符预算。

`PresentationAudit.budget_status` 明确区分：

- `WITHIN_BUDGET`
- `EXCEEDED`
- `SAFE_FALLBACK`

## 6. 模式识别

Developer Mode 只接受肯定式用户动作，例如：

- “请查看日志”
- “帮我排查接口错误”
- “检查数据库状态”
- “调试这段流程”

以下情况保持 Investor Mode：

- “不要看日志”
- “不需要调试”
- “别进入开发者模式”
- “为什么这只股票下跌”
- 系统错误文本自动进入提示，例如 `provider timeout`、`Traceback` 或“数据库连接失败”

显式 `explicit_mode` 仍可由受控调用方指定；系统错误本身不具备切换权限。

## 7. 安全降级

任何内部词、Secret、私人路径、事实漂移、重复/样式门或强制内容超预算失败，都不得回显原稿。

安全降级只保留：

- 经独立审计安全的主体；主体本身异常时固定为“研究对象”；
- 固定降级文本；
- `NOT_CERTIFIED` 结论强度。

绝对报告路径只用于内部审计允许列表；公共模型仅保留文件名。

## 8. CLI 兼容合同

原机器命令维持 `investor-research-view-v1` JSON：

- `research-investor-view`
- `research-acquisition-investor-view`

新增公共展示入口：

- `research-public-view`
- `research-acquisition-public-view`

新增入口统一经过 `ResponseGateway`。不得把旧机器命令的 JSON 偷换成新的公共投影。后续其它模块应采用同样原则：机器诊断/状态命令保持稳定，公开展示使用独立命令或显式 `--public`。

## 9. 审计输出

`PresentationAudit` 必须显式给出：

- 事实等价状态；
- 是否发生事实漂移；
- 强制内容是否完整；
- Secret 是否暴露；
- 私人路径是否暴露；
- 内部实现是否暴露；
- 字符预算及预算状态；
- 是否可发送；
- 是否回显原稿。

## 10. 验证门

本合同的参数化样例不少于 150 个，覆盖：

- 普通投资者问题；
- 肯定式和否定式诊断意图；
- 普通“为什么”；
- 系统错误文本；
- 套话、中英混排、符号与内部词；
- 主体、代码、数值、日期、时点、方向、引用、结论强度漂移；
- 估值、风险、变化条件、数据时点删除；
- Secret 和私人路径；
- 安全降级不回显；
- 长度压缩只删除非关键理由；
- 旧机器 CLI 合同与新增公共命令。

任何公共输出合同修改都必须同时通过参数化合同测试、相关产品化回归、Ruff、Pyright 和 `git diff --check`。
