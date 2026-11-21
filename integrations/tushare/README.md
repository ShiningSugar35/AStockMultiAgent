# Tushare 受控查询

## 已实现的入口

现有主程序提供以下薄 CLI，直接复用 canonical ObjectStore 和 StateStore。无需安装第二套 Tushare SDK 或账户。

- `uv run --frozen astock tushare-schema --endpoint income`
- `uv run --frozen astock tushare-query --endpoint income --params-json '{"ts_code":"600519.SH","period":"20260930"}'`
- `uv run --frozen astock tushare-query --endpoint daily --params-json '{"ts_code":"600519.SH","start_date":"20261101","end_date":"20261120"}'`
- `uv run --frozen astock tushare-query --endpoint cn_cpi --params-json '{"m":"202610"}'`
- `uv run --frozen astock tushare-mcp-probe`

宿主通过结构化进程参数传递 JSON；不同 Windows shell 的引号处理可能不同。凭据只由项目 credential loader 在运行时读取，不能把 token 放入命令、参数 JSON、日志或输出文件。

Registry 的 `tushare-structured` 只声明 `tushare.structured.query`，不会冒充现有 financial statement、instrument master 或 intraday bar 接口。十个已审阅 API 的具体参数、权限门槛、缓存 TTL 和原始文档链接由 `configs/tushare_access.yaml` 与 `tushare-schema` 给出。

现有 `supplemental.TushareHintProvider` 已委托同一 query adapter。`market.reference.hint` 的 Tushare 优先级为 10，因此这一路的按需日行情查询确实优先使用 Tushare。`configs/financial_sources.yaml` 的原有财务审计管线仍保持原合同；本轮没有把所有财务审计、全市场列表或基准历史管线改成自动使用 Tushare。宿主数据采集 skill 应先查询 schema，按需求调用 typed query，再使用正式公告和现有财务审计合同验证。

## 范围与限额

允许的十个 API 是 `stock_basic`、`daily`、`weekly`、`monthly`、`daily_basic`、`income`、`balancesheet`、`cashflow`、`cn_gdp`、`cn_cpi`。分钟、VIP 财报、`share_float` 和任意 API 名称均不在自动调用范围。

2120 积分是用户报告的信息，不是每个端点的权限证明。权限观察来自真实 API 响应，并按凭据的不可逆内部 scope 分开记录。缓存指纹也绑定该 scope，切换凭据不会复用先前凭据的缓存或拒权冷却。scope 和 token 均不进入用户结果。

限额在同一个 StateStore 中通过短 SQLite transaction 原子预占；复用现有 `cursor_state` 和 `lease_lock`，没有新数据库或另一套路由：

- 全部受控 Tushare 调用合计最多 200 次/滚动分钟。
- `stock_basic` 最多 50 次/滚动分钟。
- 三个财报端点合计最多 80 次/滚动分钟。这是用户要求的保守上限，未冒称官方单独明示 80。
- 每端点每日最多 100000 次，日界使用 Asia/Shanghai。
- 最多两个在途请求，等价请求去重；失败调用也消耗预占额度。
- 同一凭据的认证、权限和限流失败进入有界冷却，不做重试风暴。
- HTTP 固定使用 HTTPS 地址，不跟随重定向；流式读取最多 4 MiB。
- 查询窗口、字段数量和返回行数受限。返回空表、字段变化或超过预算均不会证明完整覆盖。

这些限额只能约束本项目该 StateStore 内的调用。其他项目、宿主直接连接的外部 MCP 或用户另外运行的客户端不受本地计数器控制。

## 数值、单位与时点合同

原始响应独立存入 ObjectStore，结果指向原始快照和不可变 capture artifact；缺失值不转为 0。没有声明全市场完整性，返回数据不直接成为财务认证证据。

| 数据 | 当前合同 |
| --- | --- |
| daily | 原价 CNY；成交量为手，成交额为千元；pct_chg 为百分数；pre_close 为供应商除权昨收参考，不能串成原价前收盘收益 |
| weekly | 原价；pre_close 为上一周收盘；官方 doc144 明示 pct_chg 未乘 100。vol/amount 保留 SOURCE_NATIVE_UNVERIFIED，不自动转换 |
| monthly | 原价；pre_close 为上月收盘。vol/amount 保留 SOURCE_NATIVE_UNVERIFIED；pct_chg 只说明当前录制样本为小数收益 |
| daily_basic | 股本字段为万股，市值字段为万元；PE 缺失保持缺失 |
| balancesheet | total_share 为股，不能套用 daily_basic 的万股倍率 |
| 财报三表 | 保存所有返回的 report_type、comp_type、update_flag 和公告日，不按 end_date 合并重述；end_date 是报告期，绝不是公告时刻 |
| 宏观 | GDP 累计值为亿元，增长率为百分数；CPI 指数水平与百分数变化分开说明；统计期不是发布时间 |

财报 `f_ann_date`/`ann_date` 只有日期精度，不能据此证明同日盘中已经公开；抓取时刻也不能冒充原始发布时间。宏观返回未证明历史 vintage。当前股票列表是当前快照，不能当成无幸存者偏差的历史股票池。所有 query 结果默认 `formal_use_allowed=false`，后续必须经过现有正式证据和时点合同。

### 周月单位不能从日线复制

本机 2026-11-21 的真实响应显示，当前周/月返回量纲与日线不同。`reconcile_periods.py` 使用 600519.SH 和 000001.SZ，从 2026-09-01 到 2026-11-20，比较每只证券 11 个完整周和两个月，共 26 个完整期间：

- 周/月原始成交量匹配日线“手数合计 × 100”。
- 周/月原始成交额匹配日线“千元合计 × 1000”。
- 这批样本的 pct_chg 匹配小数收益。

官方现行周/月参数表没有明确 vol/amount 单位，旧示例不能证明所有历史年份一致。因此查询结果仍保留原始值并标记单位未全历史验证；录制样本只证明其自身范围，不自动授权通用转换。

首次十端点烟测报告中的周/月单位标签曾错误复用 daily 标签，原始字节未改变。该语义标签已更正，缓存合同已从 `tushare-query-v1` 提升为 `tushare-query-v2`，旧缓存不能命中新合同。首次报告可用于证明当时端点可访问与原始数据，不能作为周/月单位认证证据。

## 真实验证与重放

- 十个端点均返回非空有效数据：`runtime/provider-probes/tushare/20261121T071759959814Z/report.json`。
- 两交易所、26 个期间的单位对账及 MCP 认证状态：`runtime/provider-probes/tushare/20261121T072444607891Z/report.json`。
- 基础资料录制：`tests/fixtures/providers/tushare-structured-success.json`。
- 单位对账录制含六个原始响应及来源哈希：`tests/fixtures/providers/tushare-period-units.json`。
- 负面合同测试：`tests/unit/test_tushare_structured.py`，并回归 `tests/unit/test_provider_supplemental.py`。

可复用验证命令：

`uv run --frozen python -B integrations/tushare/smoke.py --mcp`

`uv run --frozen python -B integrations/tushare/reconcile_periods.py --start 2026-09-01 --end 2026-11-20 --mcp`

只有明确加 `--record-fixture` 才更新受版本控制的录制文件。验证窗口冻结期间不要使用该参数。脚本正常使用项目 runtime 的共享限额，没有单独的测试账户配额库。

## MCP 的实际状态

官方 MCP 地址需要在 URL 路径中带凭据；本项目只在函数内生成该地址，禁止回显、持久化或跟随跳转。能力探针只发送 Streamable HTTP 的 initialize、initialized、tools/list，支持有界 JSON 和 SSE 响应，没有执行数据查询，也没有创建第二个数据传输路由。

2026-11-21 的本机官方路径 initialize 返回 **HTTP 401**，没有 Location。当前分类为 `AUTH_REQUIRED`；不能据此断言 HTTP 数据 API 没有权限，因为同一项目凭据下十个 HTTP API 已成功。301/302/307/308 等跳转分类为策略拒绝，并只保留状态码、是否同源及是否仅尾斜杠的布尔元数据，不将跳转误称为账户拒权。

目前 MCP 数据查询没有启用。宿主不能绕过本项目 quota 直接向远程 MCP 取数后宣称已经共享额度。正式查询入口继续使用已验证的受控 HTTPS adapter。

## 官方依据

- HTTP 规范：https://tushare.pro/document/1?doc_id=40
- 权限与频次：https://tushare.pro/document/1?doc_id=108 ，https://tushare.pro/document/1?doc_id=290
- 股票列表：https://tushare.pro/document/2?doc_id=25
- 日线/周线/月线：https://tushare.pro/document/2?doc_id=27 ，https://tushare.pro/document/2?doc_id=144 ，https://tushare.pro/document/2?doc_id=145
- 每日指标：https://tushare.pro/document/2?doc_id=32
- 三张财报：https://tushare.pro/document/2?doc_id=33 ，https://tushare.pro/document/2?doc_id=36 ，https://tushare.pro/document/2?doc_id=44
- GDP/CPI：https://tushare.pro/document/2?doc_id=227 ，https://tushare.pro/document/2?doc_id=228
- MCP：https://tushare.pro/document/1?doc_id=463

官方现金流表还记录了 `is_calc` 等参数，本轮窄接口没有自动启用这些额外模式。需要扩展时先审阅字段、计算语义、权限和负面用例，不依据积分自行扩大范围。
