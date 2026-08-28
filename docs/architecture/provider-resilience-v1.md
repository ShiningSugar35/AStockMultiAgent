# Provider Resilience v1 — 按需数据平面、代理隔离与 Universe Fallback

状态：RELEASED（软件实现提交 `ed0c970`；External Dependency Resilience 集成增强发布中）
日期：2026-08-25；现行集成校正：2026-08-27

## 1. 背景与事故复盘

2026-08-24/25 的正式全市场荐股运行中，`research-seeds --live` 对 XSHG / XSHE / BJSE 均返回 `market_seed_count=0`。进一步排查确认：

1. Candidate Seed CLI 直接构造 `EastMoneyReferenceProvider`，绕过 `ProviderFactory` 与 `transport_profiles.yaml`，因此 transport profile 不是事实上的单一入口；
2. `instrument.master` 只有 BaoStock → EastMoney 两级，两个来源同时失效时没有第三条独立全市场 Universe 路；
3. HTTPX 默认读取 `HTTP_PROXY / HTTPS_PROXY / ALL_PROXY`，当前机器存在本地代理；东方财富链出现 `RemoteProtocolError`，而不同域名对代理/直连的适配并不相同；
4. transport profile 已声明 `max_attempts/backoff_seconds`，但现有 HTTP client 并未实际执行该策略；Reference 层另有 retry，形成职责分裂；
5. BaoStock circuit 只基于最近 SourceSnapshot 判断，缺少通用 transport-lane / provider-route 维度的恢复语义；
6. 即使 fallback 返回少量记录，也缺少“每市场最低 Universe 记录数”硬门，理论上存在 partial universe 被当作可用输入的风险。

本轮目标不是“保证所有第三方永不失效”，而是：**单一代理链、单一 HTTP provider、单一 SDK provider 失效时，系统仍可在按需任务内自动寻找独立路径；只有完整性达到硬门才恢复正式荐股，否则继续 fail closed。**

## 2. 外部设计依据

- HTTPX Environment Variables：HTTPX 默认使用环境代理；`trust_env=False` 可显式绕开环境变量，`NO_PROXY` 可按域名绕过代理。https://www.python-httpx.org/environment_variables/
- HTTPX Transports / Routing：HTTPX 支持按 route/mount 选择不同 transport/proxy，适合实现 provider 级 transport lane，而不是修改全局代理。https://www.python-httpx.org/advanced/transports/
- Azure Retry Pattern：只对可恢复瞬态故障做有界重试，避免多层嵌套 retry；长期故障应转 fallback 或 fail closed。https://learn.microsoft.com/en-us/azure/architecture/patterns/retry
- Azure Circuit Breaker：persistent fault 应 fail fast，恢复后通过 half-open 探测重新放行，避免级联资源耗尽。https://learn.microsoft.com/en-us/azure/architecture/patterns/circuit-breaker
- AWS Exponential Backoff And Jitter：指数退避配合 jitter，避免多个 worker 同时重试造成同步冲击。https://aws.amazon.com/blogs/architecture/exponential-backoff-and-jitter/
- 上交所公开数据目录明确存在“股票列表”等公开基础数据；北交所也提供股票列表页面。官方页面作为 future authoritative fallback 方向保留，但本轮不在没有稳定、可测试 machine-readable contract 的情况下硬接网页抓取。https://www.sse.com.cn/market/publicdata/ ; https://www.bse.cn/nq/listedcompany.html

## 3. 约束

### 3.1 运行环境

当前典型机器：8 logical CPU / ~16 GiB RAM 的 HP ProBook 450 G8 级轻薄办公本。

- 不要求 GPU；
- 不要求后台常驻服务；
- 不依赖用户在问题前预同步市场；
- 用户唤醒 Chat/Codex 后允许最长约 2 小时自动恢复；
- Provider I/O 并发继续受 `LOW_RESOURCE` hardware budget 限制。

### 3.2 投研安全

- fallback 只能恢复数据，不得创造 BUY authority；
- Universe 不能证明完整时 `UNIVERSE_COVERAGE=false`；
- Web/news 不能替代 Market Universe；
- Seed/Candidate 仍只有 research priority；
- broker execution 永远保持 false。

## 4. 目标架构

```text
Chat/Codex 唤醒
    ↓
ProviderFactory
    ↓
Transport Lane Resilience
    ├─ ENV proxy lane
    └─ DIRECT lane
    ↓
Provider Route Resilience
    instrument.master:
      BaoStock → EastMoney → Sina Market Center

    market seed snapshot:
      EastMoney → Sina Market Center

    identity:
      BaoStock → EastMoney exact → Sina exact → EM paginated

    daily:
      BaoStock → EastMoney → Sina
    ↓
Universe Completeness Gate
    XSHG / XSHE / BJSE 各自 minimum row floor
    ↓
Blind Candidate Scan
    ↓
Research Team DAG / Recommendation Gate
```

## 5. Transport Lane 设计

### 5.1 版本化策略

`transport_profiles.yaml` 增加：

- `proxy_strategy`: `ENV_ONLY | DIRECT_ONLY | ENV_THEN_DIRECT | DIRECT_THEN_ENV`
- `max_attempts`
- `backoff_seconds`
- `jitter_seconds`
- `retry_status_codes`

默认公共 HTTP provider 使用 `ENV_THEN_DIRECT`：先尊重用户本机代理；仅遇到 timeout/network/配置的 5xx 时尝试 direct lane。绝不修改系统全局代理，也不永久改写 `NO_PROXY`。

### 5.2 重试原则

- transport 层成为 HTTP retry 的唯一事实入口；
- Reference 上层 `retry.max_attempts` 收敛为 1，避免 nested retry；
- GET 只对 timeout/network/502/503/504 做最多 2 次 transport attempt；
- 401/403/429/4xx 不做盲重试，交给 provider route fallback；
- retry 使用 exponential backoff + jitter。

## 6. Provider Route 设计

### 6.1 Sina 全市场 fallback

扩展现有 `SinaReferenceProvider`：

- `instrument.master`
- `instrument.bjse_coverage`
- `fetch_master(market)`
- `fetch_seed_snapshot(market)`

新浪 Market Center 已在本机验证可返回 `sh_a` / `sz_a`，`hs_a` 排序结果包含 `bj92xxxx`；因此：

- XSHG：`node=sh_a`
- XSHE：`node=sz_a`
- BJSE：`node=hs_a`，严格只接收 `bj` provider symbol

采用 100 条/page 的有界分页；所有原始 page response 先进入 ObjectStore，再生成带 page snapshot lineage 的 aggregate snapshot。

### 6.2 Candidate Seed Router

Candidate Seed 不再直接 new EastMoney adapter。必须使用 ProviderFactory 创建的 EastMoney/Sina adapter，通过 `ResearchSeedProviderRouter`：

1. EastMoney market snapshot；
2. 失败则 Sina market snapshot；
3. Industry taxonomy 仍优先 EastMoney，失败只影响 Expert Overlay，不影响 Blind Market tranche。

## 7. Universe Completeness Gate

`market_reference.yaml` 保留保守最低完整性 floor：

- XSHG >= 1500 stocks
- XSHE >= 2000 stocks
- BJSE >= 150 stocks

这些 floor 不是“当前精确上市数量”，只用于抓住明显残缺/截断响应。现行正式 FULL 门已由 `external-dependency-resilience-v1.md` 收紧为：XSHG / XSHE / BJSE **每个市场**均须有可审计 `coverage_ratio >= 99.5%`，且 release/manifest/object/snapshot identity 全链可验证。floor 单独通过不能证明 FULL。

任何 provider 返回低于 floor、coverage ratio 不足或 lineage 校验失败时：

- 不发布 COMPLETE Instrument Master；
- 继续下一个 capability-compatible、health-eligible provider 或经验证的本地 COMPLETE release；
- 所有路径均不足时返回 PARTIAL/NEEDS_INFO 并关闭 full-market Recommendation Gate，禁止把 Provider failure 解释成 0 candidates。

Seed snapshot 同样先做 market/prefix/code/name 边界与明显截断 floor，再继承正式 Universe coverage 状态；Web/Search 不能补足该完整性证明。

## 8. 可观测性与错误语义

当前实现将错误分成三层，而不是把所有失败压成一个 `NETWORK_ERROR`：

- transport response extension：`astock_transport_lane=ENV|DIRECT`、`astock_transport_attempt=N`；
- provider/reference reason code：`EASTMONEY_MASTER_FAILED`、`SINA_MASTER_FAILED`、`SINA_FALLBACK_USED`、`*_MASTER_BELOW_MINIMUM_COVERAGE`；
- seed degradation code：`MARKET_SEED_SNAPSHOT_UNAVAILABLE:*`、`SINA_ACTIVITY_PROXY_USED:*`、`SINA_ACTIVITY_PROXY_RESEARCH_SEED`。

401/403/429/456 等非配置的 retry status 不在 transport 内盲重试；它们进入 provider-route fallback。GET/HEAD 默认可 retry，只有已审计为只读查询的 CNINFO POST 在对应 transport profile 显式加入 retry allowlist。用户侧仍只看自然语言结论，内部 reason/extension 用于审计与故障定位。

## 9. 本轮不做

- 不引入 Tushare token/API 注册；
- 不引入 AKShare 作为新的重依赖（其底层也可能复用相同第三方源）；
- 不把搜索引擎/Web 新闻当 Universe；
- 不依赖后台定时同步；
- 不为了“多一个 fallback”直接解析交易所易变前端 JS endpoint。官方 exchange machine-readable provider 作为后续增强项，必须单独审计 endpoint contract。

## 10. 验收标准

### P0 Transport

- [x] HTTP provider 的 `max_attempts/backoff/jitter` 真正执行；
- [x] `ENV_THEN_DIRECT` 在 env lane 抛 Network/Timeout 时自动尝试 direct；
- [x] direct 成功时不修改进程/系统全局 proxy env；
- [x] 401/403/429/456 等非 retry status 不在 transport 内盲重试；
- [x] transport 总尝试次数严格受 profile 上限约束；
- [x] GET/HEAD 默认可 retry，POST 只有显式 allowlist 才可重放；
- [x] Reference 层 retry 收敛为 1，避免形成双层 HTTP retry 放大。

### P1 Provider Fallback

- [x] `sina-reference` 声明并实现 `instrument.master` / `instrument.bjse_coverage`；
- [x] XSHG / XSHE / BJSE Sina master 均可解析为严格 `InstrumentRecord`；
- [x] `instrument.master` route 为 BaoStock → EastMoney → Sina；
- [x] EastMoney 失败或 coverage 不足而 Sina 成功时 `sync-instruments` 可发布 COMPLETE release；
- [x] Candidate Seed 使用 ProviderFactory + EastMoney→Sina router，不再直接裸构造 EastMoney；
- [x] Expert taxonomy 失败不阻断 Blind tranche；
- [x] 4xx/限流后 Seed 可复用 15 分钟内、Manifest/ObjectStore/release identity 全部验证通过的 fresh COMPLETE Sina Master，过期/篡改缓存拒绝使用。

### P2 Completeness / Safety

- [x] 每市场 Instrument Master 低于 floor 时拒绝该 provider 结果并继续 fallback；
- [x] 每市场 Seed snapshot 只统计身份边界有效行，低于 floor 时拒绝该 provider 结果；
- [x] 三路 provider 均失败/不足时仍保持 `market_seed_count=0 / NEEDS_INFO`，正式荐股门继续关闭；
- [x] Sina 盘前全市场 activity 字段不可用时，仅在满足 `trade=0 / amount=0 / turnover=0 / settlement>0` 的全市场模式下启用市值代理，并显式标记 `SINA_ACTIVITY_PROXY_*`；
- [x] 盘中 activity 恢复后自动回到真实 amount/turnover 评分；
- [x] Seed/Universe fallback 不改变 recommendation authority；
- [x] broker execution remain false。

### Performance

- [x] LOW_RESOURCE 仍最多 2 个 market fetch workers；
- [x] fallback 分页串行/有界，不产生上千并发请求；
- [x] 同一 provider page response 进入 ObjectStore；
- [x] fresh snapshot reuse 降低短时 4xx 后重复抓取压力；
- [x] 本轮自动恢复不需要 daemon/GPU。

### Engineering / Release

- [x] provider/reference/candidate 最终定向回归：`53 passed`；
- [x] Ruff=`PASS`；
- [x] Pyright=`0 errors / 0 warnings / 0 informations`；
- [x] `git diff --check`=`PASS`（仅 CRLF→LF 提示，无 diff error）；
- [x] SQLite `state-integrity-audit`=`PASS / integrity_check=ok / read_only=true`；
- [x] 全仓 `pytest -q`=`942 passed / 18 skipped / 0 failed`，1025.91s；
- [x] live Universe：XSHG=`2314 COMPLETE`、XSHE=`2897 COMPLETE`、BJSE=`338 COMPLETE`，均验证 Sina fallback；
- [x] live `research-seeds --live` 最终=`20 market seeds / READY`；
- [x] 盘前 live smoke 验证 activity proxy；进入交易时段后的最终 smoke 验证真实 amount/turnover 自动恢复且 warning 清零；
- [x] 文档记录 review 发现、返工和最终证据；
- [x] 软件实现提交 `ed0c970` 已成功 push `origin/main`，发布状态迁移为 RELEASED。

### Code Review / 返工记录

1. Candidate Seed 绕过 `ProviderFactory`、直接构造 EastMoney → **打回**，改为统一 Factory + router；
2. transport profile 的 `max_attempts/backoff` 只声明未执行 → **打回**，实现 `ResilientHttpClient`；
3. 初版 wrapper 只覆盖 `.get()`，破坏 CNINFO `.request()` / `.headers` 合同 → **打回**，补齐 read-only client 表面并通过全仓 Pyright；
4. retry 初版可能重放任意 POST → **打回**，增加 `retry_methods` allowlist，CNINFO 只读 POST 单独显式允许；
5. Master 只判断“非空”可能接受残缺 Universe → **打回**，增加 per-market floor；
6. Seed floor 初版只数 raw rows → **打回**，改为只统计 market/prefix/code/name 边界有效行；
7. Sina 盘前 `trade/amount/turnover=0` 导致 Seed=0 → **打回**，增加 settlement 价格锚和严格全市场 activity-unavailable 模式；
8. Sina 连续分页后出现 HTTP 456 → **打回**，增加 fresh COMPLETE Master reuse；
9. cache 初版只相信 SQLite coverage 元数据 → **打回**，升级为 ObjectStore Manifest hash + release identity + scope/provider/coverage/snapshot 验证，tampered/stale cache 均拒绝。

## 11. 实施状态

| 阶段 | 状态 |
|---|---|
| Architecture / external research | COMPLETE |
| Transport resilience | COMPLETE |
| Sina master/seed fallback | COMPLETE |
| Completeness gate | COMPLETE |
| Premarket / fresh-cache degradation | COMPLETE |
| Code review & rework | PASS |
| Test | PASS |
| Release | RELEASED |

## 12. External Dependency Resilience 现行集成校正

- `ed0c970` 的 Provider Resilience v1 仍是已发布事实；本节记录 2026-08-27 候选树对其进行的兼容增强，不改写历史发布测试数字，也不把增强候选提前称为已发布。
- Provider 资格不再使用 provider-wide 单一健康值。health、breaker、Retry-After、HALF_OPEN claim 均以 `provider/source + capability` 为键；identity 的 403/网络失败不能污染 daily、5m 或其他独立 capability。
- HALF_OPEN single-probe claim 持久化并带 TTL；owner 崩溃后的 stale claim 可回收，同时仍禁止并发双 probe。
- Provider probe health 只接受 pointer → event → artifact → object → typed report 全链校验通过的结果；任一层损坏或身份矛盾均 fail closed。
- 真实 5m smoke 中 EastMoney 请求实际发出后为 `UNAVAILABLE / NETWORK`，Sina 为 `HEALTHY`。真实 `sync-5m` 由 Sina fallback 返回 48 bars，但因仅单源而 `canonical_updated=false`，保留旧的双源验证 canonical；这证明 resilience 正确，不证明 EastMoney 已连通。
- 本节增强随 `external-dependency-resilience-v1.md` 的独立远端 tag/GitHub Release 门发布；在该门完成前，External Dependency 集成状态保持 `IMPLEMENTATION_IN_PROGRESS`。
