# E-03 外部量化模式对拍报告

- 实验：`external-quant-patterns-e03`
- 报告 ID：`e0e8c222a529ad91cc33342f988d5cdd2f429c988acfc21cc219def2f739349e`
- 数据生成时点：`2026-09-02T00:00:00Z`
- fixture hash：`34152a3948d563ed34cf31b7f843e481f25f0715126e855271f85a9d7580227b`
- 生产模块改动：无
- 网络请求：0
- 外部模型调用：0
- 默认依赖接入：否

## 候选逐项裁决

| 候选 | 裁决 | 环境/退出 |
|---|---|---|
| qlib | ADAPT_PATTERN | No Qlib code imported; simulation is a minimal local model of Recorder file semantics. Removable by deleting this directory. |
| rdagent | SHADOW_EXPERIMENT | RD-Agent LLM loop not executed; simulation uses the deterministic fixture hypotheses and results only. |
| lean | REJECT_ORDERING | LEAN engine not installed; only the ordering model is simulated with deterministic events. |
| rqalpha | WATCH_PATTERN_ONLY | RQAlpha is not installed; the robustness model uses five deterministic local variants only. The repository license includes material commercial-use restrictions, so runtime/code adoption is rejected without authorization. |

## 基准数值（deterministic benchmark metrics）

| 候选 | 指标 | 数值 |
|---|---|---|
| qlib_recorder | qlib_entry_count | 21 |
| qlib_recorder | astock_entry_count | 3 |
| qlib_recorder | unique_param_count | 3 |
| qlib_recorder | unique_artifact_count | 6 |
| rdagent_hypothesis | rdagent_iterations | 3 |
| rdagent_hypothesis | astock_prospective_entries | 2 |
| rdagent_hypothesis | rdagent_convergence | 0.029 |
| rdagent_hypothesis | astock_formal_events | 0 |
| lean_event_ordering | lean_events | 6 |
| lean_event_ordering | astock_events | 6 |
| lean_event_ordering | lean_violates_astock | True |
| rqalpha_robustness | variant_count | 5 |
| rqalpha_robustness | sharpe_mean | 16.5649 |
| rqalpha_robustness | sharpe_std | 4.2629 |
| rqalpha_robustness | max_drawdown_worst | 0.0068 |

## 汇总

The fixed-fixture comparisons support only pattern-level decisions: Qlib-style experiment grouping is an ADAPT pattern; the modeled RD-Agent-style hypothesis loop remains SHADOW only; selected walk-forward/parameter-perturbation ideas remain WATCH items. The simplified event-order fixture does not justify replacing the project's classification/protocol/ledger chain, but it is not a benchmark of the full LEAN or RQAlpha engines. The modeled robustness grid adds no demonstrated marginal value beyond existing block-bootstrap/deflated-Sharpe governance.

## 边界

本实验没有改动生产 Provider、Evidence、Committee、组合权重或 paper ledger；未把 Qlib、RD-Agent、LEAN、RQAlpha 加入 pyproject 默认依赖或生产运行路径；所有结果来自固定本地 fixtures，可删除 `experiments/external_quant_patterns/` 完整退出。
