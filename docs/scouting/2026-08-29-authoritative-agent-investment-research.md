# 2026-08-29 权威 Agent 投研与模型风险调研

## 1. 范围与证据门

本记录只采用监管机构、标准机构、交易所/行业协会、原始论文以及一手工程文档。检索日期为 2026-08-29。它服务于 current company research、full-market research team、Provider resilience 与投研 Skills 的架构决策，不是投资建议。

硬边界：

- Web/Search 只能发现和补齐可冻结的正式证据，不能充当全市场 Universe、negative proof 或最终投资结论。
- LLM 只负责开放式研究、归纳和提出候选判断；完整性、PIT、lineage、预算、安全与最终 readiness gate 必须由代码执行。
- 自动化不得改变 `broker_execution_allowed=false`，不得绕过模拟下单的人工确认。

## 2. 一手来源

| 主题 | 来源 | 检索时有效结论 |
| --- | --- | --- |
| 模型风险 | Federal Reserve, SR 26-2, *Revised Guidance on Model Risk Management*, 2026-04-17, https://www.federalreserve.gov/supervisionreg/srletters/SR2602.htm | SR 26-2 已取代 SR 11-7 与 SR 21-8；强调与模型风险画像、机构规模和复杂度相适配的风险导向治理。因此本项目不再把 SR 11-7 写成现行依据。 |
| AI 风险 | NIST, *AI Risk Management Framework*, https://www.nist.gov/itl/ai-risk-management-framework | AI RMF 1.0 仍是公开基线，但 NIST 明示其正在修订；2024 年 GenAI Profile 可用于识别生成式 AI 特有风险。实现应版本化引用，不能把当前草案状态写成永久规则。 |
| Agent 设计 | Anthropic, *Building effective agents*, 2024-12-19, https://www.anthropic.com/engineering/building-effective-agents | 优先简单、可组合工作流；固定任务用 chaining/routing/parallelization，开放式搜索才用 orchestrator-workers；evaluator-optimizer 必须有清晰评价标准与停止条件。 |
| 多 Agent 研究 | Anthropic, *How we built our multi-agent research system*, 2025-06-13, https://www.anthropic.com/engineering/multi-agent-research-system | 多 Agent 适合高价值、可高度并行、信息量超出单上下文的研究；不适合高度依赖共享上下文的链。多 Agent 成本显著更高，必须按任务价值和可并行性启用。 |
| 编排 | OpenAI Agents SDK, *Agent orchestration*, https://openai.github.io/openai-agents-python/multi_agent/ | LLM 编排与代码编排可以混合；代码编排在速度、成本和可预测性上更确定。manager 模式适合由一个 Agent 统一最终答案并聚合专家结果。 |
| Guardrails | OpenAI Agents SDK, *Guardrails*, https://openai.github.io/openai-agents-python/guardrails/ | 首 Agent 的 input guardrail 与末 Agent 的 output guardrail不能覆盖所有委派步骤；每次自定义工具调用应使用 tool guardrail。具有副作用的边界优先 blocking guardrail。 |
| 可观测性 | OpenAI Agents SDK, *OpenAI Agents SDK*, https://openai.github.io/openai-agents-python/ | sessions/persistent session backends、human-in-the-loop 与 tracing 是一手工程能力。项目继续使用自身的 StateStore、checkpoint、Artifact Registry 与 typed lineage 提供 durable recovery，而非把 SDK session 等同于项目事实源。 |
| 估值流程 | CFA Institute, *Equity Valuation: Applications and Processes*, 2026 Curriculum, https://www.cfainstitute.org/insights/professional-learning/refresher-readings/2026/equity-valuation-applications-and-processes | 估值链应依次覆盖理解业务、预测经营、选择模型、把预测转成价值、形成结论；行业前景、竞争位置、战略与盈利质量属于必要前置。 |
| 估值模型 | CFA Institute, *Equity Valuation: Concepts and Basic Tools*, 2026 Curriculum, https://www.cfainstitute.org/insights/professional-learning/refresher-readings/2026/equity-valuation-concepts-basic-tools | 模型选择受数据质量和适用性约束；应遵守简约原则，并常用多个模型交叉验证。单一复杂模型不等于更准确。 |
| 依赖韧性 | AWS Builders' Library, *Timeouts, retries and backoff with jitter*, https://aws.amazon.com/builders-library/timeouts-retries-and-backoff-with-jitter/ | 所有远程调用应有超时；重试只放在一个明确层级，必须有上限、退避与抖动；有副作用的操作必须幂等，否则重试会放大故障。 |
| 回测过拟合 | Bailey, Borwein, López de Prado, Zhu, *The Probability of Backtest Overfitting*, SSRN 2326253 / Journal of Computational Finance | 策略配置搜索越多，选中样本内赢家越可能过拟合；模型验证需记录试验族并估计 PBO，而不是只报告最佳回测。 |
| 数据窥探 | White, *A Reality Check for Data Snooping*, Econometrica 68(5), 2000, DOI 10.1111/1468-0262.00152 | 重复使用同一历史样本做模型选择会使偶然结果看似有效；应使用 Reality Check 或等价的多重比较控制。 |
| 预测能力 | Hansen, *A Test for Superior Predictive Ability*, JBES 23, 2005, SSRN 264569 | SPA 相比 Reality Check 对劣质备选模型更有统计功效，可作为大量候选策略对基准比较的验证工具。 |
| 多重检验 | Harvey, Liu, Zhu, *…and the Cross-Section of Expected Returns*, RFS 29(1), 2016, DOI 10.1093/rfs/hhv059 | 金融研究中的大量因子挖掘使传统显著性门过低；新发现需要更高门槛与多重检验校正，不能以普通 `p<0.05` 作为上线证据。 |

## 3. 采用决策

### 3.1 编排与续跑

1. **代码拥有确定性主流程**：acquisition → external evidence continuation → company research DAG → independent review/model-risk validation → committee → recommendation gate。
2. **manager 保持用户会话所有权**：专家作为受限 worker/工具返回 typed artifact，不直接接管最终投资者答复。
3. **只并行真正独立的工作**：公司意图冻结后，宏观、政策、行业与治理可并行；催化剂必须等待这四类上游工件，估值依赖财务与经营预测，投委会依赖 Bull/Bear、独立复核和模型风险验证，不能为了速度越级。
4. **外部研究使用 evaluator-optimizer**：每轮补证后由确定性 gap evaluator 判断是否仍缺正式证据；达到 gate、预算耗尽、来源不可用或需要私有材料时停止。
5. **同一请求内 durable continuation**：所有阶段写 StateStore checkpoint 和 Artifact Registry；崩溃后从 checkpoint 续跑，不把 `NEEDS_EXTERNAL_RESEARCH` 直接暴露为最终投资者答案。
6. **工具级 guardrail**：URL/域名、来源等级、artifact 类型、PIT、对象哈希、公司绑定、重复证据和预算均在每次 capture/bind 前后校验。

### 3.2 投研链

1. 采用 CFA 的五步估值流程，并把业务理解拆为宏观/政策、行业价值链、公司经济性、治理和财务质量。
2. 同时保留绝对估值、相对估值和在适用时的资产/分部估值；强制说明模型适用性、输入质量和敏感性。
3. Bull 与 Bear 使用不同 context，Reviewer 不复用其结论上下文；Committee 只能消费已冻结工件。
4. 新增治理与管理质量、催化剂/事件、投资红队、模型风险与回测验证能力；这些能力不替代现有财务完整性和 evidence investigation。
5. 模型风险验证记录模型目的、输入 lineage、假设、局限、独立复核、回测试验族、PBO/多重检验状态和上线限制。

### 3.3 韧性与性能

1. 远程重试只在 Provider HTTP transport 一层执行；业务层只做来源回退，不再叠加相同请求重试。
2. 所有 capture/bind/resume 命令使用稳定幂等键；同一 object hash 与 capability 绑定不重复落库。
3. 并发上限继续遵守本机合同：远程采集不超过 2，CPU worker 不超过 4。
4. 只缓存不可变配置、已冻结工件和请求内服务对象；不缓存未绑定 PIT 的市场事实。
5. 低价值、强依赖共享上下文的任务不启用多 Agent；简单摘要和格式转换保持单 Agent/确定性代码。

## 4. 明确不采用

- 不采用“所有问题都启动完整多 Agent 团队”的高成本模式。
- 不让 LLM 自由决定 readiness、安全、完整性或交易权限。
- 不把 specialist handoff 作为最终投资者答复所有者；manager 必须统一综合和承担 gate。
- 不在 Provider、业务服务、Agent 三层同时重试同一远程调用。
- 不用 Search/Web 未命中证明公司、公告、风险或全市场标的不存在。
- 不以单一 DCF、单一倍数、单一情景或黑箱复杂模型形成目标价。
- 不以样本内最佳 Sharpe、普通 `p<0.05` 或未记录试验次数的回测作为策略上线依据。
- 不因自动补证失败而提前请求用户提供公开材料；只有预算和正式渠道真实耗尽，或材料属于用户私有域，才生成一次性人工清单。

## 5. 对代码的直接要求

- 扩展现有 `ResearchTeamService` 支持 `COMPANY` scope，而不是新增 team/router。
- 新增 durable `CurrentResearchContinuation` 工件并复用 acquisition report、OfficialWebDocumentCapture、ResearchTeamPlan/Status、checkpoint 与 recommendation gate。
- 公司 DAG 增加 governance、red-team、model-risk 节点和 scope-specific readiness checks。
- 自动补证状态必须区分 `AUTO_RESOLUTION_REQUIRED`、`TEAM_RESEARCH_REQUIRED`、`READY_FOR_INVESTOR_VIEW`、`OBSERVATION_ONLY_FOR_INVESTOR_VIEW`、`NEEDS_USER_INPUT` 与 `FAILED`。
- recommendation gate 只允许产生 `READY_FOR_INVESTOR_VIEW` 或显式 `OBSERVATION_ONLY_FOR_INVESTOR_VIEW` 终态；只有前者允许正式推荐，任何中间 `PARTIAL`/缺口状态都不得升级为投资者结论。
- 新增 Skills 必须提供输入、命令、输出工件、证据等级、停止条件和禁止事项，并由 repo contract test 锁定。
