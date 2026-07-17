# Phase 3 M3.2 验收报告

验收日期：2026-07-17
环境：Windows 10 / Python 3.12.10
结论：通过

## 1. 本次实际交付

M3.2 已把 M3.1 的单期勾稽扩展为可审计的跨期、完整评分和同行比较能力：

1. 财务事实增加 `INSTANT`、`STANDALONE_PERIOD`、`YEAR_TO_DATE`、`REPORTED_PERIOD` 四种显式期间语义。
2. 支持明确请求的 TTM、同比、环比和每股值；累计季报先差分为独立季度，期间语义不明确时拒绝推导。
3. 完整实现 Beneish 8 变量 M-score、Piotroski 9 信号 F-score、Altman 5 因子 Z-score、Sloan accrual ratio 和三因子 DuPont。
4. 每个评分输出公式版本、输入期间、输入事实、Evidence、组件值、阈值和行业适用性。
5. 增加同行 cohort 合同：行业、指标、公式版本、报告期、as_of、公司唯一性、最小样本、SourceSnapshot、PIT 和 Evidence 全部门禁通过后，才计算稳定中秩百分位。
6. 新增 migration 0011 保存同行 cohort 元数据和 ObjectStore 对象身份；重复审计保持同一请求与工件身份。

## 2. 关键安全边界

- 高级评分不是默认强制规则，只有请求后才执行；旧 M3.1 请求不会因为没有两年完整高级字段而被无故阻断。
- 银行和保险继续排除传统工业企业评分；房地产、早期生物科技、证券和 `OTHER` 按配置只计算、不套用通用报警阈值。
- 同行样本不足、公式版本不一致、期间不一致、来源/PIT/Evidence 失败时不输出分位。
- 季度累计值不被直接当作单季度值；缺少前一累计期时显式形成缺口。
- 输出仍为 advisory-only，不创建交易硬阻断，不写模拟账本，不断言“造假”。

## 3. 主要代码与配置

| 范围 | 文件 |
|---|---|
| Schema | `src/astock/schemas/financial.py` |
| 纯 Decimal 高级公式 | `src/astock/financial_integrity/advanced_calculations.py` |
| 规则、跨期和同行引擎 | `src/astock/financial_integrity/service.py` |
| cohort 恢复元数据 | `src/astock/financial_integrity/repository.py` |
| 规则和行业版本 | `configs/financial_rules.yaml`、`configs/financial_industry_profiles.yaml` |
| migration | `migrations/0011_financial_m3_2.sql` |
| 计划 | `docs/Phase3-M3.2-M3.3开发计划.md` |

规则注册表版本为 `financial-rules-m3.2-v1`，兼容引擎为 `financial-deterministic-m3.2.0`。

## 4. 自动化验收

| 检查 | 结果 |
|---|---|
| `uv run pytest` | 132 collected；124 passed / 8 skipped |
| `uv run ruff check .` | All checks passed |
| `uv run pyright` | 0 errors / 0 warnings / 0 informations |
| `uv run astock probe` | Python 3.12.10；state integrity `ok` |
| SQLite migrations | 0001～0011 |
| SQLite 外键 | 0 条违规 |

新增测试覆盖：

- 五类公式 golden 与评分组件；
- 比率/Altman 单位缩放不变性、同行分位输入顺序不变性；
- 两期完整高级审计；
- YTD 季报差分、TTM、环比、每股值；
- 未声明季度语义时 `NEEDS_INFO`；
- 房地产只计算、不使用通用报警阈值；
- 同行来源/PIT/Evidence 门禁、最小样本和 cohort 持久化；
- M3.1 原有勾稽、行业排除、冲突、未来数据、幂等和恢复全部继续通过。

8 个默认跳过项仍是 7 个显式 live probe 和 1 个 30 文档真实 OCR 基准，不属于 M3.2 的纯本地确定性公式验收。

## 5. 未完成项

M3.3 尚未完成：稳健 Z-score、Isolation Forest、PyOD、冻结数据集/模型工件、误报解释和 Phase 3 完整验收仍需继续开发。因此本报告只标记 M3.2 通过，不标记整个 Phase 3 完成。

## 6. 人工事项

M3.2 没有需要用户操作、注册或购买的事项。
