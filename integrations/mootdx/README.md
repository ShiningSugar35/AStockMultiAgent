# Mootdx 行情后备

## 当前可用范围

完整 Mootdx 0.11.7 与 tdxpy 0.2.7 安装在唯一的项目隔离环境
`runtime/tool-environments/mootdx`。主应用通过已有 KillableResearchWorker
运行固定 SDK 子进程，主环境的 httpx 与其他依赖不受该 SDK 的旧版本约束影响。

当前默认路线是 **Mootdx + MAC compatibility**，补丁版本
`astock-mootdx-mac-v1`。Mootdx 创建、握手并关闭真实 TDX TCP session；
补丁仅替换两个行情 command 的构造和解析，保持固定 source ID
`mootdx-fallback`，并在 raw envelope 标明 SDK / transport / patch 版本。
这是对原 Mootdx 标准行情命令失效的明确兼容修复，不能称为“原标准协议已经恢复”。

2026-11-21 在本项目 Windows 环境实际验证：

- 上海 600519：121.36.248.138:7709，quote 1 条、raw 60m 20 根、raw 5m 240 根。
- 深圳 000001：123.60.47.136:7709，同样三项成功。
- 两次 bars 均覆盖 2026-11-16 至 2026-11-20，现役 validate_batch 为 PASS。
- 原 STANDARD 协议四个明确候选的空响应/解析失败也保留在 runtime/provider-probes/mootdx；
  TCP 能连通不能证明行情命令可用。
- 只声明沪深股票 quote / 原价 60m / 原价 5m。北交所、ETF、财务、逐笔与复权尚未接入。

配置在 `configs/mootdx_fallback.yaml`。两个候选逐一、一次尝试；没有全 IP 扫描、
heartbeat、自动 SDK 重试或行情推理。既有同步循环根据 fallback_only 在需要时才调用，
来源健康、熔断、canonical 与双源质量处理仍由现役数据链负责。

## 重建隔离环境

在项目根目录执行：

```powershell
.venv\Scripts\python.exe -B integrations\mootdx\setup.py
```

安装以 `requirements.lock` 精确版本与分发文件 SHA-256 为准；不向主 .venv 安装 Mootdx。
安装器的缓存和临时文件也在项目内。环境属于受保护的可重建工具环境，
不得当作普通临时目录自动清理。依赖缺失或导入失败分类为 CAPABILITY_UNAVAILABLE，
其余行情源继续按现役链处理。

只有维护依赖版本时才使用 setup.py 的 --refresh-lock；变更后重新审阅锁、
运行本目录记录的合同与真实验证。不会在普通行情请求中自动联网安装依赖。

## 可复现验证

```powershell
.venv\Scripts\python.exe -B integrations\mootdx\smoke.py
.venv\Scripts\python.exe -B integrations\mootdx\smoke.py --symbol 000001 --market XSHE --server 123.60.47.136:7709
.venv\Scripts\python.exe -B integrations\mootdx\smoke.py --days 28 --limit 1600
```

每次报告与验证用 SQLite / ObjectStore 在独立 runtime/provider-probes/mootdx/<UTC 时间>/，
不写入 canonical 行情存储。报告保留 adapter、worker、补丁、锁文件和配置的输入 SHA。
--server 最多允许两个显式公开地址；--protocol STANDARD 可用于复现原协议故障。
一次调用的时间、页数和输出大小都受配置限制；外层 owned worker 到期终止整个子进程树。

成功报告可按固定规则生成录制 fixture：

```powershell
.venv\Scripts\python.exe -B integrations\mootdx\record_fixture.py <项目内成功报告的绝对路径>
```

此命令要求 quote / 60m / 5m 三项真实 PASS，验证原始对象哈希后，把完整二进制响应帧、
来源快照 ID 与输入版本写入 tests/fixtures/providers/mootdx-fallback-success.json。

SDK worker 的类型验证使用质量 runner 声明的隔离环境，不向主环境安装 SDK：

```powershell
.venv\Scripts\python.exe -B scripts/run_local_quality.py --type-environment mootdx pyright integrations/mootdx/worker.py integrations/mootdx/transport_compat.py
```

## 数据语义与边界

- MAC bars 的 volume 是股；MAC quote 位图 volume 是手（100 股）。两者分别标注，
  不沿用旧标准 bars 的手。录制茅台 2026-11-20：4 根 60m 与 48 根 5m
  各合计 6,722,900 股，quote 为 67,229 手。
- MAC OHLC 是 float32。仅沪深股票的主合同价格按人民币 0.01 tick 归一化，
  原始浮点值与二进制帧完整保留。amount 保留源值和 CNY 单位，不用价格乘量估算；
  float32 导致各周期汇总和 quote 金额可有微小舍入差异。
- bars 按 Asia/Shanghai 的 bar end 解释；频率、显式市场、证券代码、请求页偏移与
  回包身份均校验。只返回请求时间窗内原价数据。
- MAC quote 提供源更新日期/时间；作为 SOURCE_UPDATE_TIME 保存。未验证当前交易时段
  的 freshness 时保持 false，尤其不能把周末收到的上一交易日报价当作当前盘中价。
  STANDARD quote 没有日期时保存 None，绝不补“今天”。
- MAC 每页上限 700。帧头、压缩解压长度、回包 count、页偏移与记录长度均须匹配；
  股票元数据尾块只允许本机录制确认的 0 或 120 字节。重复页报 PAGINATION_CYCLE。
- 缺失、空结果、截断、未知字段位图和非有限数字不会形成成功数据。源历史不足或
  页预算耗尽保留 PARTIAL，不保证任意历史窗口完整。
- SDK 在导入前仅收到子进程专用 USERPROFILE，位于项目拥有的任务 scratch；
  父进程与系统 HOME/USERPROFILE 均不修改，任务结束由 owned worker 清理。
- 测试入口：tests/unit/test_mootdx_fallback.py。覆盖原始追溯、精确身份、股/手、
  时间与原价、截断/压缩炸弹、wrong offset、循环页、SDK 缺失、关闭失败和真实录制帧。

补丁的公开源码依据、固定提交与许可证归属见 THIRD_PARTY_NOTICES.md。
