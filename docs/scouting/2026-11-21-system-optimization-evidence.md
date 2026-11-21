# 系统性优化：来源核查与当前树证据

> 状态：FROZEN RESEARCH SNAPSHOT
> 核查日期：2026-11-21（Asia/Shanghai）
> 对应计划：[开发计划.md](../../开发计划.md)
> 输入：[系统下一步优化意见.md](../../系统下一步优化意见.md)
> 工作方式：research-tech-scout；工程/数据/量化三线独立只读核查，主线整合与交叉复核
> 范围：规划证据，不是依赖安装、来源正式准入、市场有效性或性能验收

## 一、方法与适用边界

原意见共 **66 个唯一显式 URL**，按首次出现顺序编号 S01–S66。下表逐一登记；重复出现的同一 URL 只计算一次。r.jina.ai 是带目标 URL 的模板，不能假装已经抓取某个目标；腾讯文档权限表只加载 UI/标签，未读到权限单元格。其他公开文章、论文/仓库已实际检索原文，并按需要进一步读取许可证、官方 API 文档或上游 issue。未向用户账户发起 live/收费请求，未读取或输出 `.env`。

决策定义：ADAPT_PATTERN 为借局部方法并复用现有架构；SHADOW_EXPERIMENT 为可关闭、固定样本、不得自行晋级的试验；WATCH 为资料/备选；REJECT 为拒绝对应重复或不适配接法，不代表否定整个上游项目。表中列明已经观察到的关键许可/维护约束；未采用代码的参考项目不因此获得任何代码或数据使用授权。任何将来安装/复制必须固定 tag/commit/版本、核具体目录/模型/数据许可、依赖树、Python3.12/Windows兼容、撤回路径。动态 main 页面的状态仅是本日快照。

技术判断优先官方仓库、供应商文档和论文原文；README 的效果宣传、星数与检出率不能转成项目验证结果。以下事实与建议分列，建议属于本项目的适配判断。所有方案在当前 ObjectStore/Parquet/DuckDB/SQLite、provider router、发布与账本边界上落地；不恢复已退役社区采集或 Kronos，不引第二状态/事实系统。

## 二、66 个原始链接的逐项登记

| 编号与原始来源 | 核实内容、版本/许可或限制 | 裁决 | 适配与最小下一步 |
|---|---|---|---|
| S01 [Harness engineering](https://openai.com/index/harness-engineering/) | 文章：2026-02-11；约100行AGENTS作为地图、配合机械规则。没有全行业200行/15KB定律。 | `ADAPT_PATTERN` | 根指令地图、按需规范与机械约束；不复制案例性能。WP-69/70 |
| S02 [Evaluating AGENTS.md](https://arxiv.org/abs/2602.11988) | arXiv:2602.11988，已读2026-09-29 v3；编码评测中上下文文件常未提高成功率、平均成本增加逾20%。不是固定下降3%，人写也非必然有益。 | `ADAPT_PATTERN` | 按本项目固定任务实测上下文增减；不据此删除金融不变量。WP-68/69 |
| S03 [AGENTS开放约定](https://github.com/agentsmd/agents.md) | 公开规范说明文件约定；不是Codex默认32KiB的实现依据。 | `ADAPT_PATTERN` | 兼容开放格式，宿主加载规则另核。WP-69 |
| S04 [OpenAI Codex](https://github.com/openai/codex) | 公开仓库与官方指令文档可查；32KiB为默认聚合预算，当前用户有效配置未核。 | `ADAPT_PATTERN` | 只读诊断cwd/有效配置/实际载入链；不宣称本次已截断。WP-69 |
| S05 [Context engineering](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents) | 官方2025-09-29文章：按需检索、压缩、持久状态；未支撑原文所有80%数字。 | `ADAPT_PATTERN` | 相关片段/投影入上下文，原件和引用留现有存储。WP-69/71 |
| S06 [When to fold subagents](https://claude.com/resources/articles/building-agents-when-to-fold-subagents-into-your-main-agent-loop) | 2026-10-22文章；约2倍提速是一个客户案例，仍认可并行、独立验证和隔离。 | `SHADOW_EXPERIMENT` | 只折叠实测耦合节点；不以Skills数替代调用数。WP-68/69 |
| S07 [single-agent-loop-kit](https://github.com/anthropics/single-agent-loop-kit) | 官方README标明参考实现、不维护/不接受贡献；要求逐步折叠并比较评测。 | `ADAPT_PATTERN` | 借实验清单；REJECT生产运行依赖。WP-69 |
| S08 [Anthropic Skills](https://github.com/anthropics/skills) | 渐进式披露示例真实；目录许可证不同，部分文档技能为source-available，不能视作全库同一授权。 | `ADAPT_PATTERN` | metadata常驻、正文按需；具体代码复制前逐目录核许可。WP-69 |
| S09 [Effective harnesses](https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents) | 初始化、增量任务、持久进展与实际验证；JSON扩展名不提供防篡改能力。 | `ADAPT_PATTERN` | 现有WP/receipt记录输入hash、owner、输出与验收。WP-68/70 |
| S10 [Harness design for apps](https://www.anthropic.com/engineering/harness-design-long-running-apps) | 2026-03-24文章：生成/评估分工及结构化交接，评估器需校准实际失败。 | `ADAPT_PATTERN` | 开发验收/研究质量分别验证，不每步加通用LLM Gate。WP-68/70 |
| S11 [Spec Kit](https://github.com/github/spec-kit) | spec→plan→tasks可借；官方也区别小修无需完整流程。 | `ADAPT_PATTERN` | 使用现有工作包表达规格；REJECT全量流程迁移。WP-70 |
| S12 [Tach](https://github.com/gauge-sh/tach) | 原URL重定向tach-org/tach；支持Python模块边界，远端web图可能传配置。 | `WATCH` | 与import-linter择一dev-only，本地运行。WP-70 |
| S13 [import-linter](https://github.com/seddonym/import-linter) | 原URL重定向import-linter/import-linter；分层、禁止、独立性静态合同。 | `ADAPT_PATTERN` | 优先小范围边界契约；静态import不等于运行期写权限。WP-70 |
| S14 [Vulture](https://github.com/jendrikseipp/vulture) | 未用代码检测带置信度/allowlist；动态注册与反射可能误报。 | `ADAPT_PATTERN` | advisory+实际调用/注册核对，不自动删除。WP-70 |
| S15 [Ruff](https://github.com/astral-sh/ruff) | 现有项目已用E/F/I/UP/B，尚未启C901；工具支持复杂度规则。 | `ADAPT_PATTERN` | 仅渐进约束新增/修改函数，不以分数代替语义正确。WP-70 |
| S16 [pre-commit](https://github.com/pre-commit/pre-commit) | 本地hooks能被跳过，不是沙箱；CI仍需同源规则。 | `ADAPT_PATTERN` | 检查staged凭据/体积/真实临时文件，保留合法fixture/migration。WP-70 |
| S17 [Dexter](https://github.com/virattt/dexter) | 实际AGENTS写默认10轮、工具scratchpad、按需Skill，最终单独调用读取完整scratchpad；不是只读压缩摘要。 | `ADAPT_PATTERN` | 借bounded loop/typed event，避免末次重复消耗；不导入Bun/第二事实账本。WP-69/71 |
| S18 [OpenBB](https://github.com/OpenBB-finance/OpenBB) | 原URL重定向openbq-org/OpenBB；当前仓库声明Apache-2.0；TET已在v4开发文档核实，不能混称所有代际。 | `ADAPT_PATTERN` | 借query→raw→normalized窄合同；不安装整个平台或第二router。WP-72 |
| S19 [TradingAgents](https://github.com/TauricResearch/TradingAgents) | 当前代码说明包含并行、持久恢复与时序修复，不能仍概括为纯串行。 | `ADAPT_PATTERN` | 证据驱动独立多空/有限争议，检查来源独立性。WP-65 |
| S20 [ai-hedge-fund](https://github.com/virattt/ai-hedge-fund) | 当前已演进到aihf、paper ledger/审批/回测，固定19名人描述过时；不保证A股适配。 | `ADAPT_PATTERN` | 只借扇出/聚合与实验mandate；拒绝名人投票/第二账本。WP-69/65 |
| S21 [RD-Agent](https://github.com/microsoft/RD-Agent) | 离线假设、代码、实验反馈闭环；并非低延迟投研问答所需框架。 | `SHADOW_EXPERIMENT` | 在现有离线试验和预算内验证；禁止自行改生产规则。WP-63/74 |
| S22 [Multi-agent research system](https://www.anthropic.com/engineering/multi-agent-research-system) | 官方广度并行、上下文压缩和工具恢复实践；没有金融“仅四个硬阻断”定律。 | `ADAPT_PATTERN` | 失败成typed gap，依赖分支隔离，研究/执行资格分别判断。WP-71 |
| S23 [DBOS Python](https://github.com/dbos-inc/dbos-transact-py) | DBOS官方编程指南支持SQLite，README偏Postgres不代表不支持；持久步骤不能保证任意外部副作用exactly-once。 | `WATCH` | 借未知完成状态协调；现有SQLite DAG无需迁移。WP-71 |
| S24 [PydanticAI](https://github.com/pydantic/pydantic-ai) | 类型化AI运行与评测框架；已有Pydantic模型不构成引入新执行器的理由。 | `WATCH` | 只有明确执行器缺口才窄adapter实验；目前不新增。WP-71 |
| S25 [LangGraph](https://github.com/langchain-ai/langgraph) | checkpoint/线程状态/长期记忆各异；replay需处理副作用和幂等，不保存全部进程推理状态。 | `WATCH` | 借恢复合同，保留当前runtime/状态库。WP-71 |
| S26 [stamina](https://github.com/hynek/stamina) | 基于Tenacity的重试封装，带抖动/观测；与本项目已有重试重叠。 | `REJECT` | 不叠加；未来确需替换同层才重评。WP-71 |
| S27 [Tenacity](https://github.com/jd/tenacity) | 支持有界配置；裸retry默认并非有等待且有界。 | `REJECT` | 不再包一层，使用现有HTTP/任务共享预算。WP-71 |
| S28 [pybreaker](https://github.com/danielfm/pybreaker) | 断路器库；本项目已有source+capability持久状态与half-open单探针。 | `REJECT` | 保留现有熔断，不硬改冷却为60秒。WP-71 |
| S29 [Humanizer](https://github.com/blader/humanizer) | MIT；当前README的16/16结果原issue说明评委均为同一模型家族，非人类盲测，77/144试验执行。 | `ADAPT_PATTERN` | 借减少空洞修辞，事实优先；不以stars/偏好声称有效性。WP-77 |
| S30 [humanizer-zh-cn](https://github.com/holygeek00/humanizer-zh-cn) | 中文结构与事实保持建议，MIT衍生；未提供足以证明本项目研究质量的独立效果实验。 | `ADAPT_PATTERN` | 适配术语/条件/不确定性表达，避免机械禁词。WP-77 |
| S31 [Humanizer-zh](https://github.com/op7418/Humanizer-zh) | MIT；更新强调条件、否定与事实保持，有限示例不等于金融效果实证。 | `ADAPT_PATTERN` | 纳入中文语义负例与盲化可读性评价。WP-77 |
| S32 [Unslop](https://github.com/asavvin-pixel/unslop) | 英文结构风格工具，引用StoryScope；原研究为小说文本，不是中文投研报告。 | `ADAPT_PATTERN` | 借结构检查，效果只作迁移假设，不新增通用润色服务。WP-77 |
| S33 [Wikipedia AI写作迹象](https://en.wikipedia.org/wiki/Wikipedia:Signs_of_AI_writing) | 社区编辑规范，可说明自身写作原则；不是投资质量量表或可靠AI身份判别器。 | `ADAPT_PATTERN` | 删除空话而保留事实、不确定性和有用结构。WP-77 |
| S34 [SEC Plain English](https://www.sec.gov/pdf/handbook.pdf) | 1998官方手册，Buffett序言，非“百年指南”；简明金融说明不等于删除风险条件。 | `ADAPT_PATTERN` | 自然语言、主动表达和清晰数字对照。WP-77 |
| S35 [Jina Reader URL模板](https://r.jina.ai/) | 原文是https://r.jina.ai/<URL>，登记此根端点；按Reader官方说明核对用途，未代入用户URL执行抓取。 | `SHADOW_EXPERIMENT` | 公开页面兜底，保留原文；不传私密文件/内网站点。WP-73 |
| S36 [Tushare Pro](https://tushare.pro/) | 主页及官方权限/财务文档可读；数据服务权益与客户端许可分离，分钟等另有独立权限。 | `ADAPT_PATTERN` | 优先已购权益，按endpoint探测，不猜用户积分。WP-72 |
| S37 [Ralph原始实践](https://ghuntley.com/ralph/) | 编码循环依赖文件状态/反馈；不证明投研应无限运行或所有渠道必搜。 | `ADAPT_PATTERN` | 取进展/完成验证，拒绝无限循环和DONE字符串凭证。WP-71 |
| S38 [Claude Code](https://github.com/anthropics/claude-code) | 宿主插件/Stop hook可查；不代表Codex/ChatGPT有完全相同hook，Windows兼容需验证。 | `ADAPT_PATTERN` | Python closure为权威，宿主hook可选；不依赖.sh统一控制。WP-71 |
| S39 [open-ralph-wiggum](https://github.com/Th0rgal/open-ralph-wiggum) | 多CLI外层循环、有迭代限制；不是金融语义调度器。 | `ADAPT_PATTERN` | 借有界外层判断，拒绝第二owner/daemon/Bun loop。WP-71 |
| S40 [ralph-loop-agent](https://github.com/vercel-labs/ralph-loop-agent) | 实验性JS AI SDK项目，verifyCompletion与轮数/token/cost预算。 | `ADAPT_PATTERN` | 完成证据与预算分开；不引入Python系统依赖。WP-71 |
| S41 [awesome-ralph](https://github.com/snwfdhmp/awesome-ralph) | 社区资源列表，非效果实验或可靠性保证。 | `WATCH` | 仅资料发现，不纳入生产选型分数。WP-71 |
| S42 [Open Deep Research](https://github.com/langchain-ai/open_deep_research) | 研究拆分/搜索/压缩/综合流程，带LangGraph/server相关栈。 | `ADAPT_PATTERN` | 借查询计划/矛盾驱动追问，不迁移整个服务。WP-71/73 |
| S43 [DeerFlow](https://github.com/bytedance/deer-flow) | 当前2.0为重写的super-agent harness，原研究版本在1.x。 | `WATCH` | 只借task packet/隔离模式，试验必须指明代际与固定版本。WP-71 |
| S44 [Mootdx](https://github.com/mootdx/mootdx) | TDX客户端；MIT标识同时有学习/非商业用途说明，适用解释需核；无永不封IP或行情SLA。 | `SHADOW_EXPERIMENT` | quote/bars60m/bars5m/financial分别测，不直接主源。WP-72 |
| S45 [AData](https://github.com/1nchaos/adata) | Apache-2.0；公开网页多源封装，本身也面临访问限制。 | `ADAPT_PATTERN` | 借字段/端点映射；不整体叠加自动fallback。WP-72 |
| S46 [AKShare](https://github.com/akfamily/akshare) | MIT；官方研报/预测接口可查，现有hint不等于所有新能力已准入。 | `ADAPT_PATTERN` | 窄研报adapter先shadow，保留EASTMONEY同源组及服务数据权利。WP-73 |
| S47 [a-stock-data](https://github.com/simonlin1212/a-stock-data) | Apache-2.0；README记录2026-09起所用TDX行情命令失败、腾讯后备、量单位/ETF份额/分红/历史行业与退市修正。 | `ADAPT_PATTERN` | 把问题变为adapter反例，不照搬全套Skill/路由，不外推所有TDX永久失效。WP-72/63 |
| S48 [stoke](https://github.com/birdilsss-byte/stoke) | MIT；聚合TDX/腾讯/东财/巨潮的小工具，展示不等于稳定性证明。 | `WATCH` | 已有源重复，只有实际补缺端点才实验。WP-72 |
| S49 [SearXNG](https://github.com/searxng/searxng) | AGPL-3.0；自托管聚合搜索，官方limiter说明上游限流/机器人封禁。 | `WATCH` | 覆盖确有增量才试；不是零运维、无限可靠的默认Docker。WP-73 |
| S50 [Agent-Reach](https://github.com/Panniantong/Agent-Reach) | MIT；跨平台CLI、社交站点/会话复制和多种额外凭据；与已退役社区能力重叠。 | `REJECT` | 整套不接入，仅借capability-health思路。WP-73 |
| S51 [Crawl4AI](https://github.com/unclecode/crawl4ai) | Apache-2.0；当前README0.9.6，CVE公告有特定Docker API/regex适用范围，不能只说升级就安全。 | `SHADOW_EXPERIMENT` | 普通HTTP失败且动态页必要时的可选后备，与Scrapling择一。WP-70/73 |
| S52 [Scrapling](https://github.com/D4Vinci/Scrapling) | BSD-3-Clause、Python≥3.10；自适应selector/多fetcher，基础安装不含所有浏览器能力。 | `WATCH` | 作为Crawl4AI对照，注入清洗不作信任边界。WP-73 |
| S53 [Jina Reader仓库](https://github.com/jina-ai/reader) | Apache-2.0；托管转写与自托管风险不同，转写不是原始文件；CVE无足够修复版本证据。 | `SHADOW_EXPERIMENT` | 公开页兜底，保留原件，逐跳地址验证/出站限制。WP-73 |
| S54 [Playwright MCP](https://github.com/microsoft/playwright-mcp) | Apache-2.0；官方明确不是安全边界，浏览器/profile/下载由宿主约束。 | `SHADOW_EXPERIMENT` | 仅确切必要页面，owner进程/限时/限制出站；不整套默认常驻。WP-70/73 |
| S55 [MinerU](https://github.com/opendatalab/MinerU) | 4.x分级解析；当前LICENSE在Apache-2.0上附商业条件，模型另核；本机Windows/硬件未测试。 | `SHADOW_EXPERIMENT` | 复杂财报目标页挑战者，按单元格准确率/追溯/RAM/时间评估。WP-73 |
| S56 [Docling](https://github.com/docling-project/docling) | MIT代码；结构化布局/表格/OCR与MCP，模型依赖许可另核。 | `SHADOW_EXPERIMENT` | 同一解析adapter中挑战现有PyMuPDF/OCR，按目标页升级。WP-73 |
| S57 [Qlib](https://github.com/microsoft/qlib) | MIT；Alpha158/360、中国示例与PIT能力属实，非完整官方数据源；main含未发布安全变更提示，需锁版本。 | `ADAPT_PATTERN` | 先透明本地因子；Alpha158/LightGBM后置shadow，不导入第二存储/GPU栈。WP-74 |
| S58 [vectorbt](https://github.com/polakowo/vectorbt) | 实际LICENSE为Apache-2.0 with Commons Clause；有销售相关限制，不是无条件普通Apache。 | `SHADOW_EXPERIMENT` | 小型参数面/数值对照；保留A股执行/费用/账本，许可核对随版本。WP-63 |
| S59 [Machine Learning for Trading](https://github.com/stefan-jansen/machine-learning-for-trading) | 第三版工作流、代码MIT；书稿和数据权利独立。 | `ADAPT_PATTERN` | 借假设—数据—探索/确认—成本风险—退役流程，不导入教学整库。WP-63/74 |
| S60 [Alphalens](https://github.com/quantopian/alphalens) | Apache-2.0；IC/分组收益/换手诊断。未验证本项目Python3.12兼容，也不能凭年龄称已archived。 | `ADAPT_PATTERN` | 借诊断定义，直接安装WATCH；信号分析不等于可成交收益。WP-74 |
| S61 [skfolio](https://github.com/skfolio/skfolio) | BSD-3-Clause；HRP/CVaR/成本/组约束及pooling可查，EntropyPooling重权情景概率后还需优化器。 | `SHADOW_EXPERIMENT` | 单一可关闭allocator插件，现有数学/执行先校准；不保证回撤。WP-76 |
| S62 [Riskfolio-Lib](https://github.com/dcajasn/Riskfolio-Lib) | BSD-3-Clause；CVXPY/多风险优化、solver依赖较重，部分高级模型建议商业solver。 | `WATCH` | 与skfolio高度重叠，不同时入生产；必要时只做独立数值对照。WP-76 |
| S63 [ml4t-diagnostic](https://github.com/ml4t/diagnostic) | MIT；官方支持Python3.12–3.14/Windows，含CV/DSR/PBO；核心依赖不少，非无成本一行修复泄漏。 | `SHADOW_EXPERIMENT` | 先论文/本项目反例对照，和purgedcv择一，不另建日历/registry。WP-63 |
| S64 [purged-cross-validation](https://github.com/eslazarev/purged-cross-validation) | MIT；purge/embargo/CPCV/WalkForward/DSR等，明确试验数不是CPCV路径数。 | `ADAPT_PATTERN` | 真实label区间、跨证券日期分组，选作轻量拆分器对照。WP-63 |
| S65 [QuantStats](https://github.com/ranaroussi/quantstats) | Apache-2.0；消费收益做统计/图表/HTML，不生产可信收益或A股成交。 | `ADAPT_PATTERN` | 只读验证过的绩效投影，禁默认外部行情下载，复用report manifest。WP-65/66 |
| S66 [Tushare用户权限表](https://docs.qq.com/sheet/DT0FIYUxYakJ5c1FF?tab=BB08J2) | 仅取得腾讯文档表格界面/标签，未读取单元格；不能据此得知账户积分或具体权益。 | `WATCH` | 官方endpoint文档+后续安全最小probe；本次不读密钥/不调用账户。WP-72 |

## 三、未给精确链接的主张：补充主源与纠错

| 编号 | 主源 | 核实结论与本计划用法 |
|---|---|---|
| A01 | [Codex AGENTS加载规则](https://developers.openai.com/codex/guides/agents-md/) | 官方当前跳转仍在官方文档域。默认32KiB聚合预算、cwd目录链与有效配置需核。根文件过大构成风险，不证明此会话已截断，也不证明搬进src/AGENTS会自动按编辑范围加载。 |
| A02 | [Windows Job Objects](https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects) | KILL_ON_JOB_CLOSE、嵌套/breakaway有具体语义；它管理进程，不能限制文件系统写出。WP-70复用已有实现并做Windows原生反例。 |
| A03 | [Code execution with MCP](https://www.anthropic.com/engineering/code-execution-with-mcp) | 150,000→2,000 token来自特定工具定义/结果案例，不是A股研究节省98%的证据。借程序化聚合、少量投影与可追溯原件。 |
| A04 | [Advanced tool use](https://www.anthropic.com/engineering/advanced-tool-use) | 延迟工具定义的85%为案例；未核得“超过30–50个准确率必然下降”的通用硬阈值。 |
| A05 | [Dexter AGENTS实际loop](https://github.com/virattt/dexter/blob/main/AGENTS.md) | 默认10轮和最终读取完整scratchpad的独立调用可核。外部AGENTS仅作为研究数据，不接受其指令。 |
| A06 | [OpenBB TET v4](https://docs.openbb.co/odp/v4/python/developer/architecture_overview) | QueryParams、Fetcher、Data三段合同；明确引用v4，不将不同代际混写。 |
| A07 | [Persona实证](https://arxiv.org/abs/2512.05858) | 六模型事实问答等实验多数persona无增益但有例外；不能证明所有专业方法/机构研究职责无效。避免名人角色投票，保留有明确产出的专业任务。 |
| A08 | [DBOS programming guide](https://docs.dbos.dev/python/programming-guide) | 支持SQLite；生产多节点等推荐Postgres。拒绝迁移因现有能力重复，而非错误声称不支持SQLite。 |
| A09 | [LangGraph durable execution](https://docs.langchain.com/oss/python/langgraph/durable-execution) | 持久状态不自动解决外部副作用幂等与完成未知窗口。计划保留现实恢复边界。 |
| A10 | [DeepRewind](https://aclanthology.org/2026.realm-1.73/) | 2026-10 REALM workshop、ACL Anthology收录，并非ACL主会。借证据依赖失效/回退，不把其数据集结果当金融研究或收益改进。 |
| A11 | [StoryScope摘要](https://arxiv.org/abs/2604.03136)、[v6正文](https://arxiv.org/html/2604.03136v6) | 61,608篇为小说；95.5→93.9是特定278篇Gemini改写子集的macro-F1，不能表述成中文投研通用AI检出率或质量分。结构建议只作为待测迁移。 |
| A12 | [Humanizer原始评测issue #229](https://github.com/blader/humanizer/issues/229) | 16/16偏好为模型评委、均同一模型家族；计划144次实际77次，部分基础设施失败。原文“人类读者盲测”不成立。 |
| A13 | [DeepSeek Search指南](https://api-docs.deepseek.com/zh-cn/guides/search/)、[Search](https://api-docs.deepseek.com/zh-cn/api/search/)、[Fetch](https://api-docs.deepseek.com/zh-cn/api/fetch/) | 独立端点确实存在；query最多128 Unicode；Search/Fetch各按成功非空请求计费，中文页为5元/千次，need_content可省额外fetch。authority与rerank只是供应商字段。严格不使用模型API，返回的搜索摘要仍是检索数据。 |
| A14 | [Tushare权限基础](https://tushare.pro/document/1?doc_id=13)、[积分/频次/独立权限](https://tushare.pro/document/1?doc_id=290) | 单股/全市场VIP、分钟等权限不同，2000分不能泛指全部接口；实际账户未核。计划读取已有权益，不建议重复购买。 |
| A15 | [daily](https://tushare.pro/document/1?doc_id=27)、[daily_basic](https://tushare.pro/document/2?doc_id=32)、[adj_factor](https://tushare.pro/document/2?doc_id=28) | 以endpoint定义确定字段/权限/频率，价格、复权、现金股息和成交用途不可混淆。 |
| A16 | [income](https://tushare.pro/document/2?doc_id=33)、[balancesheet](https://tushare.pro/document/2?doc_id=36)、[fina_indicator](https://tushare.pro/document/2?doc_id=79) | income明确ann_date/f_ann_date/end_date/report_type/update_flag；母公司/合并、调整前后、单季有不同类型。字段名不是PIT证明，日期级信息不能伪成盘前精确时刻。 |
| A17 | [AKShare研报/预测官方文档](https://akshare.akfamily.xyz/data/stock/stock_research.html) | 个股研报接口可含机构、日期、年度预测与PDF；年份不能按样例固定编码，网页异常不保证接口SLA。direct reportapi与AKShare的东财数据同源。未核得东财长期稳定开放API合同。 |
| A18 | [CNINFO官网](https://www.cninfo.com.cn/) | 复用已有官方披露路径；互动回复、管理层预测与审计财报事实等级不同，不能仅按域名全部同权。 |
| A19 | [Tavily credits](https://docs.tavily.com/documentation/api-credits) | 月度1000 credits不等于任意1000次搜索；basic/advanced/extraction消耗不同。仅WATCH。 |
| A20 | [Brave pricing](https://api-dashboard.search.brave.com/documentation/pricing)、[帮助](https://api-dashboard.search.brave.com/documentation/resources/help-feedback) | 当前Search为5美元/千次并有月度credit，账户/付款条件另有要求；不能写成无需条件的永久免费套餐。仅WATCH，不替用户注册。 |
| A21 | [腾讯搜索API](https://cloud.tencent.com/document/api/1806/121811)、[活动/计费](https://cloud.tencent.com/document/product/1806/130616)、[MCP](https://cloud.tencent.com/developer/mcp/server/11764) | 服务需开通/鉴权，活动额度不表示用户拥有或永久不变；不能累加成稳定4000+免费高质量搜索保证。 |
| A22 | [SearXNG limiter](https://docs.searxng.org/admin/searx.limiter.html) | 本地无按次账单不等于无资源/运维成本或无上游封禁。 |
| A23 | [a-stock-data raw README](https://raw.githubusercontent.com/simonlin1212/a-stock-data/main/README.md) | 上游故障与量单位/ETF修正已复核；仅作者报告，不能推出所有TDX节点已永久失效。 |
| A24 | [vectorbt LICENSE](https://github.com/polakowo/vectorbt/blob/master/LICENSE.md) | Apache-2.0 with Commons Clause，相关售卖限制需随使用场景核对，不登记成普通Apache。 |
| A25 | [Entropy Pooling](https://skfolio.org/auto_examples/entropy_pooling/plot_1_entropy_pooling.html)、[Opinion Pooling](https://skfolio.org/auto_examples/entropy_pooling/plot_2_opinion_pooling.html)、[Black–Litterman](https://skfolio.org/auto_examples/1_mean_risk/plot_12_black_and_litterman.html) | 情景概率更新与权重优化分开；历史样本支持范围限制尾部情景。LLM自报信心未校准不能直接作经济概率。 |
| A26 | [skfolio回测/评价](https://skfolio.org/user_guide/backtesting_and_evaluation.html) | 权重漂移与成本口径要明确；库的成本惩罚不能替代现有no-trade band和A股执行转换。 |
| A27 | [DSR原论文](https://davidhbailey.com/dhbpapers/deflated-sharpe.pdf) | 跨试验Sharpe离散度、独立试验数及所选序列样本/偏度/峰度等是关键输入；本地DSR-style近似须修正或改名。 |
| A28 | [PBO原论文](https://davidhbailey.com/dhbpapers/backtest-prob.pdf) | 原定义使用CSCV；不能把逐块平均收益胜负比例写成完整PBO。 |
| A29 | [ml4t CV文档](https://github.com/ml4t/diagnostic/blob/main/docs/user-guide/cross-validation.md)、[统计检验文档](https://github.com/ml4t/diagnostic/blob/main/docs/user-guide/statistical-tests.md) | 方法需正确时间区间/完整试验记录，支持CPCV不消除全样本预处理、修订倒灌和幸存者偏差；与另一拆分器只择必要实现。 |

### 安全编号核查

这些编号已经从供应商公告或CVEProject原始记录核实，不因编号罕见就删除。记录仅用于候选接入风险分析，不表示本项目安装或遭到利用。正式采用时再次按锁定版本复核。

| 原文主张 | 主源与核实边界 | 计划处理 |
|---|---|---|
| Crawl4AI CVE-2026-115508 | [供应商GHSA](https://github.com/unclecode/crawl4ai/security/advisories/GHSA-j843-8p47-rhxp)列受影响≥0.9.0且≤0.9.4、0.9.5修复；主要涉及认证Docker API不可信regex/ReDoS，直接库调用取决于输入。 | 固定修复版本、禁止任意不可信regex、进程限额；不是只填最低版本就全部安全。 |
| Jina CVE-2026-82638 | [CVE原记录](https://raw.githubusercontent.com/CVEProject/cvelistV5/main/cves/2026/82xxx/CVE-2026-82638.json)，部署相关的私网地址验证。 | 托管/自托管分开，出站网络约束。 |
| Jina CVE-2026-85699 | [CVE原记录](https://raw.githubusercontent.com/CVEProject/cvelistV5/main/cves/2026/85xxx/CVE-2026-85699.json)，重定向未重新验证；未取得足以声称某最新版已修复的版本证据。 | 每跳DNS/IP/私网检查，禁止metadata/localhost，不能写“升级最新版即可”。 |
| Vibe-Trading CVE-2026-114107 | [CVE原记录](https://raw.githubusercontent.com/CVEProject/cvelistV5/main/cves/2026/114xxx/CVE-2026-114107.json)，0.1.0至<0.1.7间接提示注入到shell/任意URL。 | 不引整套服务；网页数据不得授权shell/读密钥/上传文件。 |
| Vibe-Trading CVE-2026-114108 | [CVE原记录](https://github.com/CVEProject/cvelistV5/blob/main/cves/2026/114xxx/CVE-2026-114108.json)，默认无认证API/代码执行面，记录称0.1.7修复。 | 不裸暴露服务；未接入项目不为此另造扫描平台。 |

原意见把“ann_date/固定税率/T+1/涨跌停”缩成一行不足以定义回测：计划要求复用既有生效日期规则、证券类型、交易日历、实际可得时间、价格用途及成交边界。本次不发布任何新的费率/交易法规结论；实现时以已有权威规则来源和当期适用条款逐项验证，禁止用单个现行数字覆盖历史。

## 四、当前工作树的代码/运行证据

这是2026-11-21读取当前工作树所得的“已有基础与缺口”快照，不是对全部代码完成验收。未修改下面任何业务模块。起始HEAD为33dadb4，原有大量已修改/删除/未跟踪内容已记录且保留。行号会随后续开发变化，以路径及函数名定位。

| 编号 | 本地证据位置 | 已确认内容与影响 |
|---|---|---|
| C01 | `AGENTS.md`、`README.md`、`docs/README.md`、`skills/README.md`、`docs/workflows/README.md` | 根AGENTS 53,880 UTF-8字节/176行；Skills20、Workflows23。Workflow一方面只用于跨能力，另一方面要求每Skill至少有一个Workflow，整理须同步机器合同。CURRENT用新信息与历史实验PIT不是天然冲突。 |
| C02 | `configs/full_research_recommendation.yaml`、投资请求架构与AGENTS | 本金100,000一致；目标年化根规范50%、配置/架构100%分叉。计划统一权威及语义，不替用户改目标，不回写历史。 |
| C03 | `research/team.py::_company_tasks` | intent后多类研究可独立，fundamental/valuation/quant按真实依赖；bull/bear独立context。不是11阶段全串行。已有估值争议Reviewer和原子预约。 |
| C04 | `research/sla_runtime.py`、`validation_cache.py` | 持久DAG、generation/lease/CAS、过期恢复和资源并发已经实现；校验缓存含artifact/object/validator/policy/identity/fact hash，失败不缓存。补实际绕开路径/同键并发，不另建缓存。 |
| C05 | `research/continuation.py::run_to_terminal`、`acquisition.py::_run_parallel` | 同请求自动续接与部分采集并行已有；pending external tasks仍有串行段。仅独立ready缺口并行，不给共享SQLite连接直接套gather。 |
| C06 | `configs/chat_invocation_policy_v1.yaml`、`investor_orchestration/host_continuation.py` | 2100/2400/2700秒是activation策略，overall_deadline_enforced=False、partial_delivery_required=False；不能称当前整体研究到点必须交差。 |
| C07 | `research/process_worker.py` | Windows Job KILL_ON_JOB_CLOSE/start gate/watchdog已有；约L211–218外部scratch先mkdir再回退项目内，是明确目录边界缺陷。Job不等于文件沙箱。 |
| C08 | `scripts/run_local_quality.py` | TEMP/TMP/TMPDIR/cache/pytest basetemp/-B和receipt已有；Popen生命周期、总输出上限、异常终态与骤死恢复未统一。全树fingerprint会把无关并行改动与测试读集混在一起。 |
| C09 | `providers/http_resilience.py`、`core/source_resilience.py`、`configs/source_resilience.yaml` | 有指数退避/jitter/总预算/幂等GET/HEAD和source+capability持久熔断。默认elapsed30s、threshold3、cooldown300s、half-open1、Retry-After cap1800s；原文统一60s无根据。HTTP Retry-After是否被外层完整消费仍需链路测试，不宣称已确认bug。 |
| C10 | `configs/web_discovery_lead.yaml`、`research/web_discovery.py` | 明确不联网、不自动接acquisition/recovery，主要query/URL/dedup/ranking；这是主动Web执行缺口，不是官方证据链完全不存在。 |
| C11 | `providers/supplemental.py`、`configs/provider_registry.yaml` | TushareHintProvider只有daily、DISCOVERY_ONLY、formal_capabilities空；literal源码搜索mootdx/deepseek/reportapi无命中。用户现有权益尚未充分接入。 |
| C12 | `documents/cninfo.py`、`official_web.py`、`pdf_parser.py` | CNINFO查询/分页/PDF、官方原件capture、原生文本+低文本页OCR/缓存已有。exact-item capture不允许穷尽证明。无需从头写CNINFO/PDF栈。 |
| C13 | `schemas/financial.py`、`financial_sources.py`、`institutional_research.py`、`financial_sources/service.py` | 期间/单位/合并、累计/单季/TTM、supersedes与公开/接收/入库/修订元数据已有；FinancialSourceObservation仅合并累计。新adapter必须填这些合同并传显式source_metadata，不能混入母公司/单季。 |
| C14 | `portfolio/analytics.py::portfolio_risk_statistics` | 当前cumprod后直接maximum.accumulate漏初始净值。纯函数实测r=[-0.1,-0.1]返回MDD约0.10，含NAV=[1,0.9,0.81]应0.19。零文件/网络写入最小反例，退出0。 |
| C15 | `portfolio/analytics.py::constrain_scores`、`portfolio/service.py`、`schemas/portfolio.py` | 分母按真score求和、单个零score在分子变1；A/B分数[1,0]、不同group、target=.9、单票/组上限1，函数实返A=.9/B=.9，总1.8且binding空。下游Schema拒绝>1，不能称已下单；还需target=.4等超目标但≤1负例。 |
| C16 | `portfolio/allocators.py`、`portfolio/service.py`、`portfolio/decision.py` | 四配置及no-trade band已在；ScoreBuilder(AlignedPortfolioData)→scores，service再constrain。新增约束优化须在同registry扩展context/result，不可把权重当分数再裁剪仍宣称原解最优。 |
| C17 | `candidates/discovery_features.py`、`seeds.py`、`promotion.py` | 已有TTM/单季/同比/现金转换/应收库存/同行分位，blind/breadth/value cohort及8/12/16深研预算；新Top20–30强制深研可能更贵。 |
| C18 | `research/institutional.py`、`fundamental_analytics.py` | DriverTree/预测/估值/隐含增长与FCFF scale、DCF敏感度已具；经营驱动逆向、预期差、明确假设与勾稽才是增量。当前EV−net_debt和net_debt默认0需查生产者边界，不据此宣称所有估值错。 |
| C19 | `experiments/validation.py`、`market_evaluator.py`、`selection_seal.py` | 日线冻结行情/成本/限制/基准/现金股息评价器与双身份原子封存已有；股息pay_date=ex_date等范围有限，不是通用完整A股回测。holdout hash包括release/benchmark/period，需加相同经济窗换hash/子集的使用追踪。 |
| C20 | `shadow/statistics.py` | 函数自称DSR-style，缺跨试验Sharpe离散度；逐时间块均值法不是论文CSCV-PBO。计划修定义/输入或准确改名。 |
| C21 | `research/outcome_feedback.py` | 5/20/60交易日价格参考、基准差和公司行动检查存在；合法MATURE代表该参考已到期/可比，不等于净收益。未复权税前参考明确net_return/actual_trade_profit为空。 |
| C22 | `research/presentation.py`、`public_polish.py`及现行public contract | 已有NarrativeBundle/InvestorPresentationModel/FactFingerprint/ResponseGateway；数字等集合检查不单独证明实体/条件/因果绑定，需正式输出路径负例，尚未证明整个Gateway可绕过。 |
| C23 | `src/astock/cli.py`、`research/production_cli.py`、`observability.py`、`production.py` | _services会建目录/日志/migrate，部分report命令写artifact/policy；_read_only_services存在。此次改用只读diagnostics，不按Skill示例执行会写状态的“查看”命令。 |
| C24 | 现役注册报告，只读diagnostics | AgentObservabilityReport最新2026-09-17、1项观测/无质量标签；8个research run、0 complete，非当前性能。SkillEfficiencyReport最新2026-09-03、8 skills但usage/token=0。对ResearchRequestPerformanceReport类型查询无记录，不推论所有trace都不存在。 |
| C25 | `进度验收.md`接管前快照 | WP62核心已在；WP63接MarketEvaluator；WP65接Reviewer；WP66接ScheduledResearch/Monitor。旧计划有相反陈述，本轮纠正而不删除/重做既有代码。WP55记录0/18 Q3正式登记为本地准入缺口，不是发行人未披露证明。 |
| C26 | `tests/unit/test_repo_skills.py`、`.agents/skills/knowledge-ingest/SKILL.md` | 本轮L0首批33 passed/1 failed；触发metadata测试仍要求allowlisted/history，现有Skill已转向本地文档/保留证据且退役社区采集。同一合同还需核CLI示例和Output残留作者覆盖语义。失败函数不读本轮4文档；不改测试凑绿，完整整改登记WP-69。 |

### 可复核的读入指纹与观测标识

| 输入/报告 | SHA-256 |
|---|---|
| AGENTS.md，接管读取 | `65c359e87897a71a1abf2be81f0b0b3482c3617735bc01f87251078ea1feb5` |
| 系统下一步优化意见.md | `e917c8a840005edd809037329667a2e658bc1392d779114b6491436bde981de9` |
| 开发计划.md，改写前 | `bcbf5fd3db1c5274d8c7c98bab666d059c5cd895e367e47a227eb3bbbb3f30de` |
| planning/work_packages_v1.yaml，改写前 | `780dcdeb774e19bf0d5084b8b7178b8ee40d5881109e0db42d84df78dbf19501` |
| 进度验收.md，接管前 | `c9f3afd078bdb25efdd4278fbe3c0a5d63224b9c03b8c5ab76a8b4bdf545c29a` |
| 历史AgentObservabilityReport内容hash | `76d6e085ab800a03fed6eacbe3219f47c07c39c6ccb0b2bc8a9910aa1aef3157` |
| 历史SkillEfficiencyReport内容hash | `391b0c1d6dc3e77323cb7fe6fab34be5cd1bd29c9e10c8063491b94491e161fe` |

不能将旧报告的单条21.773秒任务或usage=0说成现时延迟/零费用，也不能把零标签当通过。因本次是规划，只读检查和纯数值反例不构成任何runtime准入或投资发布。

## 五、原未提交验收的证据保全

以下引用接管前2026-11-19验收文本的实质内容，作为冻结证据保存。它不是本轮重新运行的结果，不是新的CURRENT架构，不与新的进度验收争夺权威。保留原失败记录和未发布边界，避免改写开发计划时丢失先前工作。

> # AStockMultiAgent 进度验收
>
> > 状态：LATEST VERIFIED SNAPSHOT / 本地增量修复，整包未完成
> > 任务：2026-11-19 现役知识能力收口、研究与策略实验代码增量、Web URL/简报发布边界 REVIEW
> > 机器索引：`planning/work_packages_v1.yaml`
>
> ## 本次结论
>
> 本轮实际源码修复：Web 入站 URL 在 HttpUrl 规范化之前拒绝凭据、控制字符、空白和反斜杠；纯规范化拒绝非法 HTTP URL，官方域匹配安全处理畸形 authority。简报本地展示核对调用证券、草稿证券、来源证券及正式建议候选集合，拒绝跨证券展示。新增负向回归，未赋予线索或草稿任何正式研究发布权限。
>
> ## 验证结果
>
> 已执行验证：研究质量/策略/CLI 专项 89 passed；Web/简报/campaign/租约/来源专项 63 passed；文档/工作流/验证规划专项 54 passed。上述批次存在重叠，不相加作为独立覆盖数。定向 Ruff 通过，定向 Pyright 0 errors / 0 warnings，git diff --check 退出0。17文件影响回归实际为 254 passed / 2 failed（269.62秒）；两项失败均为本轮更新验收文档时遗漏合同必需标题，补回后完整失败文件复跑 6 passed。保留原批次失败记录，不宣称单次全绿。
>
> 本轮对现役 `runtime/state.sqlite` 执行了一次 SQLite URI `mode=ro` 只读查询：观察名单18只，2026-09-30 季报登记记录0条，未满足正式财报发布前置条件18只。没有修改现役数据库、账户、注册证据、正式财报回执或账本；没有执行网络研究、真实订单或外部发布，没有 commit/push。最终测试临时目录与工具缓存已指定在项目 scratch 内；首次有测试误用系统临时目录，已如实记录。其他未提交修改保留。
>
> 现行知识模块仅保留已登记来源身份、批准的本地文档、不可变来源证据、通用蒸馏、离线图片定位与本地图片审核。历史数据库迁移和来源引用保持兼容；未改写现役数据库、对象存储或账本。
>
> 删除专项验收：知识配置/蒸馏/语义流程/离线图片定位/删除回归 12 passed；CLI/删除回归/来源配置兼容 29 passed，两批存在重叠，不累加为独立覆盖数。定向 Ruff 与 `git diff --check` 通过。首次 CLI 验证暴露文件哈希及聚合哈希漂移，更新当前开源适配清单后完整 CLI 批次通过；曾有测试选择器中文经 shell 管道编码损坏，改用稳定 source_id 后通过。首次测试误用系统临时目录，后续所有测试临时目录和缓存均显式指定到项目 scratch。未修改现役数据库、WAL、user_state、账户或其它既有源码修改，未 commit/push。
>
> WP-55 软件层原有 campaign/租约/续接与来源准入回归通过，但现役只读查询确认 Q3正式记录仍0/18，业务 BLOCKED、不得制造 receipt。WP-62 保持 IN_PROGRESS / RESEARCH_ONLY。WP-63 已接入基于冻结 MarketBar、市场限制、费用、股息、基准和封存选择的确定性日线评价器及 CLI；六文件影响回归98 passed，Ruff/Pyright通过，但缺失完整真实行情和公司行动等市场证据，`market_validation_claim=false`，保持 IN_PROGRESS。WP-65 已接 ResearchTeam 不同估值争议Reviewer与原子单次预约，仍缺更广义争议接线、实际host执行器及成熟结果，保持 IN_PROGRESS。WP-66 已接入原有 ScheduledResearch/ContinuousMonitor 的本地 DRAFT 简报复用，跨账户/标的绑定与无授权负向校验通过，未建立新定时器或推送。WP-65/66 回归 34+41+24通过（批次有重叠），Reviewer复测10通过，Ruff/Pyright 0错误。上述新代码未进行全仓完整回归、正式市场实盘数据验证或正式投资发布，不将各项未满足的业务门设为 DONE。
>

## 六、本轮独立审阅与规划裁决

工程、数据、量化三线已对正文与机器索引进行独立只读审阅。已纳入的修订：同URL缓存按身份/范围/freshness而非永不刷新；共享收费预算原子预留/结算和未知结果保守占用；DeepSeek固定origin/路径、鉴权不跨站转发；日期级公告不能伪造盘前时刻；骤死receipt由监督者/恢复核对；本地staged与CI merge-base检查区分；完成包删除时原子移除已满足的依赖边；WP65/76/77缩小全包依赖；合法MATURE参考与净绩效分开；现有ScoreBuilder扩展而不重复裁剪优化解；留出集经济时间窗追踪；估值权益桥缺项不默认为零。

本次只交付规划文档。新包状态PLANNED，原55/62/63/65/66状态继续保留，来源资格/真实市场有效性/自然样本未被升级。最终文档合同、覆盖检查、diff检查及提交结果以[进度验收.md](../../进度验收.md)的本轮记录为准。
