# AStockMultiAgent

AStockMultiAgent 是本地优先的 A 股研究、证据与模拟交易系统。Agent 负责意图、专业判断与协作，Python 3.12 的 `astock` CLI 负责数据、计算、校验、状态和账本。任务通过现有 Skill、Workflow 和服务续接，使用同一份可追溯事实。

## 研究边界

- 当前问题使用有界获取后的最新可信事实，不以提问时刻截断；历史回测/OOS/前向验证单独遵守真实 point-in-time 可见性。
- 关键事实回到交易所、公告、正式财报和权威政策；搜索、社区和二级结构化数据提供线索，不能冒充正式证据。
- 缺口先复用本地证据、同能力备用源和权威 Web；真实耗尽后明确尚不能核实的事实。公开数据不可得与需要用户私人资料分开处理。
- 候选、正式研究、组合建议、模拟订单和成交是不同状态。只有模拟成交改变模拟持仓；外部真实账户事件与模拟账本分开。
- 不承诺收益。系统没有真实券商执行功能；ETF 模拟执行默认关闭，启用需要独立有效规则与既有确认/回放。

## 能力入口

| 领域 | 入口与边界 |
|---|---|
| 公司研究 | 当前获取/续接、机构基本面、财务完整性、行业/治理/催化、独立多空与投委会；正式结论须通过同请求发布门 |
| 全市场与组合 | Universe/候选、blind cohort、研究优先级、组合构建/迁移/风险；初筛和量化排序不自动取得推荐权威 |
| 数据补证 | 宿主 Web 能力优先，已有正文复用；明确能力缺口才走配置备用，搜索/Markdown 不升级为正式 PDF |
| 结构化补充 | Tushare 的基础、行情、财报、宏观入口受统一配置与限额约束，尚不替换正式 financial_sources 回源合同 |
| 分钟行情 | Mootdx 使用隔离工具环境与有界 worker，为 raw 60m/5m 提供配置化备用；不能冒充财报或复权价格源 |
| 账户与持续观察 | append-only 外部账户、模拟订单/成交/T+1、持久监控任务、增量复核；排队任务不等于已完成分析 |
| 知识 | 批准的本地 PDF/DOCX 与存量证据审核；线上社区作者采集已退役，运行只消费 audited active registry |
| 验证与准入 | 历史研究、shadow/prospective、来源资格与自然场景样本分别记录；软件存在不等于生产资格已获准 |

字段、路由、限额和当前可用状态以 Schema、配置、CLI、测试与运行事实为准；验证范围见最近的[进度验收](进度验收.md)。

## 开始使用

```powershell
uv sync --all-groups
uv run astock init
uv run astock --help
```

研究任务按需加载 `.agents/skills/astock-research-orchestrator/SKILL.md`，由总控路由专业 Skill。独立诊断才使用 `probe`，无需每次投资提问都重跑。

```powershell
# 当前公司数据获取；完整研究由同请求 continuation / workflow 驱动
uv run astock research-acquire-current 600519 --market XSHG
# 仅查看已有状态
uv run astock local-portfolio-status
uv run astock continuous-monitor-status
# 按端点读取结构化来源合同，不加载全部接口文档
uv run astock tushare-schema --endpoint income
```

可选凭据从项目 `.env` 或进程环境读取，环境变量优先；复制 `.env.example` 后按需填写，再用 `provider-credentials-status` 核验是否识别，不回显密钥。`.env`、用户持仓和私有原文不进 Git。

## 单一事实源

| 内容 | 权威位置 |
|---|---|
| 原始响应/证据对象 | `runtime/objects/sha256/`，按引用生命周期保护 |
| 分析事实 | Parquet；DuckDB 只建视图 |
| 任务/游标/工件与账户/模拟账本 | SQLite；只经正式服务写入 |
| 字段、策略与执行规则 | `src/astock/schemas/`、versioned config、migration、CLI |
| 用户可读投影 | Git-ignore 的 `user_state/`，不能反向制造事实 |

## 开发与验证

先确认改动影响，再运行项目内 runner；所有临时、日志、缓存与工具环境留在项目目录。长任务具有进程树归属、取消/超时、输出预算和终态回执。heavy gate 全项目最多两组，避免子孙进程与 BLAS 叠加。

```powershell
.venv\Scripts\python.exe -B scripts/plan_validation.py src/astock/portfolio/analytics.py --output .ai-bridge/validation-plan.json
.venv\Scripts\python.exe -B scripts/run_local_quality.py pytest tests/unit/test_portfolio_numeric_invariants.py -q
```

示例仅演示调用方式，实际执行选择器给出的相关测试并补充任务负例；选出测试不等于覆盖证明。输入树变化、超时或漏片不能记 PASS。日常回归使用 recorded fixture，live 证据单独记录。

## 文档入口

- [AGENTS.md](AGENTS.md)：不超过 8 KiB 的仓库级硬规则与按需路由。
- [docs/README.md](docs/README.md)：文档治理、机器合同与现役架构导航。
- [开发计划.md](开发计划.md) / [机器索引](planning/work_packages_v1.yaml)：尚未完成的工作包。
- [进度验收.md](进度验收.md)：最近任务的真实验证结果与限制。
- [Workflow 索引](docs/workflows/README.md) / [Skill 目录](skills/README.md)：按当前问题选择专业流程。
- [业务问题能力矩阵](docs/acceptance/business-question-capability-matrix-v1.md)：业务链的可执行验收边界。
