# 实时投研提效：上游模式复核与产品取舍

> 日期：2026-09-17
> 状态：RESEARCHED / PLANNING_ONLY；未实施、未实测达到40分钟
> 实施与验收唯一入口：`../../开发计划.md`、`../../planning/work_packages_v1.yaml`
> 本文是来源与取舍记录，不是另一套运行规范。

## 1. 所有者最新裁决

本次需求将产品明确限定为实时/增量投资研究，不承担历史时点回测。后续开发应删除活动PIT/防未来函数及其相关验证、门禁；彻底删除没有实际券商执行能力却遍布全链的真实券商禁止字段、验证与规则。删除不能仅改成默认关闭、改名保留或设为True。删除这些门禁不等于开发真实交易接口。

此前 `2026-09-17-recommendation-40min-sla-gate-audit.md` 中“PIT硬保留”“broker_execution_allowed=false硬保留”等建议已被本次用户裁决替代。其热点定位仍可参考，但它不是逐一验证所有门禁的完整证明。已有语义盘点中“候选控制点数”不等于独立业务门禁数。

仍保留普通研究需要的证券身份、财务报告期/币种/单位、真实来源、数字复算、重大事实冲突、当前数据更新状况和账户数据隔离。这些检查不判断历史某一时刻能否看见数据，不重新包装为PIT门。公告中未来生效日期、预计投产日期及模型预测期本来就可以在未来。

## 2. 2026-09-17 一手来源与适配结论

以下页面均在本次通过Web读取。引用为当日分支/文档视图，不宣称取得未显示的commit SHA；真正复制上游代码前必须固定具体commit与许可证。没有使用社区收益宣传作为本地效果证据。

| 来源 | 本次能核验的内容 | 本地取舍与最小落点 |
|---|---|---|
| FinRobot官方仓库与开源finrobot_equity | 开源模块将财务获取、指标/预测处理、同业比较、LLM文字和报告分层；明确列出financial_data_processor、valuation_engine、agent_manager等模块 | ADAPT_PATTERN：沿现有financial_sources/research/institutional实现统一标准化与计算接口；不引入整套服务、网页、数据库或另一本数值账本 |
| FinRobot主README的版本说明 | V0/V1为开源路径；V2在线版源码尚未开源，V3在开发。Desktop/线上产品说明不等于可取得相同源码 | 不把闭源产品描述算作已经审阅的实现，也不为此新增产品依赖 |
| TradingAgents官方README | 结构化决策、可选checkpoint恢复、持续决策记录、按任务区分轻/重模型 | ADAPT_PATTERN：复用本地任务游标、注册工件及观察池；不照搬收益回顾/回测评估或PIT过滤 |
| TradingAgents default_config.py | deep_think/quick_think、max_debate_rounds、max_risk_discuss_rounds、llm_max_retries、max_tokens、可配置数据供应链均有代码定义 | 采用任务级模型路由、总预算内重试与有限辩论；不硬编码上游模型ID，不照搬其用户home目录缓存路径 |
| TradingAgents graph/setup.py | 当前代码明确把分析师节点顺序连接，之后进入多空/风控链 | REJECT直接复制整张图。多Agent并不自动等于并行；本地改为满足依赖即启动、独立分支并发、局部失败恢复 |
| LangGraph Graph API | 节点可为普通代码或LLM；支持输入缓存键与TTL | ADAPT_PATTERN：只借鉴输入指纹缓存与节点接口，不新增必需LangGraph依赖；缓存键必须包含本地模型、数据及规则版本 |
| LangGraph use-graph-api | fan-out/fan-in、并行状态reducer、并发限制；有checkpoint时成功分支可保留、失败分支单独重试；不等长分支汇合有重复执行/等待风险 | ADAPT_PATTERN：任务完成通知、唯一归并、确定性合并顺序、成功分支不重跑。不给所有节点设置全局阶段屏障 |
| Anthropic Building Effective Agents | 对已知流程优先简单可组合工作流，独立任务并行，复杂自主任务才由Agent接管；强调延迟/成本权衡 | 将标准化、计算、登记交给代码；保留开放检索、假设解释、异常接管给LLM，不把程序当作永不变化的封闭系统 |

### 来源目录

1. FinRobot官方仓库：https://github.com/AI4Finance-Foundation/FinRobot
2. 开源Equity模块：https://github.com/AI4Finance-Foundation/FinRobot/blob/master/finrobot_equity/README.md
3. TradingAgents官方仓库：https://github.com/TauricResearch/TradingAgents
4. TradingAgents配置源码：https://raw.githubusercontent.com/TauricResearch/TradingAgents/main/tradingagents/default_config.py
5. TradingAgents流程源码：https://github.com/TauricResearch/TradingAgents/blob/main/tradingagents/graph/setup.py
6. LangGraph节点/缓存：https://docs.langchain.com/oss/python/langgraph/graph-api
7. LangGraph并行与故障处理：https://docs.langchain.com/oss/python/langgraph/use-graph-api
8. LangGraph持久化：https://docs.langchain.com/oss/python/langgraph/persistence
9. Anthropic工作流：https://www.anthropic.com/engineering/building-effective-agents

FinRobot和TradingAgents官方仓库当前显示Apache-2.0许可。此次仅提出设计模式，不下载/复制整库，不增加第三方依赖；若实施时复用代码，要保留许可/NOTICE并检查具体文件许可和传递依赖。美国FMP/SEC/Yahoo等数据覆盖不直接移植成A股数据事实或来源资格。

## 3. 对上一版方案的必要修正

1. 40分钟是交付预算上限，不是必须耗满的时间，也不是把40分钟切成完全串行的阶段。后端取数/解析/计算时，主LLM处理已就绪的其它任务；出现空闲资源时按剩余时间和增量价值安排下一候选。
2. 初始深研候选可以较少，但不永久锁死为3或4家，不按“容易读到数据”挑赢家；保留市场盲筛、广度、长期价值三路发现，记录未完成/被跳过的标的及原因。
3. “研究完成后没有合格买入标的”与“数据或时间不足以判断”必须分开。后者不能包装成全市场无机会或正式100%现金结论，也不能清空用户既有持仓。
4. 缓存不能只按artifact_id、宽泛时间桶或版本号字符串判断。不可变字节校验可复用；事实冲突状态、依赖版本、公司/市场身份、账户权限、适用规则变化时须失效。取消PIT后，缓存证明也不引入cutoff/PIT条件。
5. 多空使用不同字符串ID不能证明上下文独立；需要实际独立输入/调用记录，或者如实标记单模型多视角，不伪造独立审查。
6. 原示例中的4小时13分钟是跨中断的durable日历跨度；93个任务是后端命令数量，不能说成93次LLM调用，亦不能据此计算纯计算时间或宣称某类门禁消耗了确定百分比。先补request级计时与等待归因。
7. 本次工作树已有未提交的财务解析、推荐装配、行情因子和文档修改；本轮只写计划/调研标注。后续实施要接手并复核现状，不回滚这些工作。

## 4. 当前仓库复用点

- `financial_sources/certification.py`、`configs/financial_field_mappings.yaml`、`configs/provider_dialects.yaml`：字段、报表和来源标准化基础。
- `research/institutional.py`、`research/fundamental_analytics.py`：已有确定性财务/估值计算，扩展而不复制。
- `research/team.py`、`research/continuation.py`：任务、登记、恢复基础，改为一张请求图。
- `investor_orchestration/subjects.py`：已有append/add_watchlist/enroll_from_analysis/current_watchlist；扩展潜在目标状态，而非新建第二观察仓。
- `research/lifecycle.py`、`research/lifecycle_repository.py`、现有Continuous Monitor：复用增量任务与条件触发；仅在有可用执行器时消费LLM任务。
- `investor_orchestration/output_validation.py`、`domain_contracts.py`、`full_research.py`、`full_research_assembly.py`：统一校验与删除重复包装的主要位置。

本文没有证明本地并发速度、数据源成功率、投资判断质量或40分钟达标；这些只能由开发计划中的分层实测和验收记录证明。
