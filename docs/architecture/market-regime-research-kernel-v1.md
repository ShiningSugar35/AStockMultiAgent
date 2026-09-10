# 市场状态研究内核与风险上限接缝 v1

> 状态：CURRENT / COMPONENT IMPLEMENTED / SHADOW_ONLY
> 更新：2026-09-07
> 范围：原 WP-06/07 的特征构建、训练/过滤/评估工具和绝对风险上限；不替代 `market-regime-control-v1.md`、原工作包或全局验收。
> 本文件不宣称真实 A 股历史效果、完整 Universe 数据覆盖、68 业务 E2E、自然前向样本或生产启用已经完成。

## 1. 唯一事实源与代码接口

`regime_features.py` 的 `RegimeFeatureBuilder` 消费 canonical `StateStore.artifact_registry` 与 `ObjectStore` 中的既有 typed observations/reports，不复制原始事实表。数值列、证券/序列身份、单位、可得时点、观察期间、变换窗口和标准化方法由 `RegimeFeaturePolicy/RegimeSeriesSpec` 绑定。原始价格使用未复权 `DailyBarObservation`，且核对关联 SourceSnapshot 的状态、原始对象和实际系统可得时间；宏观观察保留原始对象哈希。

派生结果为 `RegimeFeatureBuildReport`，包含 canonical `MarketRegimeFeatureSnapshot`、每个序列的来源 ID/hash、状态、原始变换值和标准化值。注册时同时保存不可变特征政策，并从源工件重算结果；`load_registered` 再核验报告、政策、来源及确定性重算的一致性。不是读取调用者声称的 coverage 就予以认证。

七个族的 coverage 分母固定；缺失关键数值不能用 coverage=1 掩盖，省略族键也不能使剩余一个族变成整体100%。`MISSING`、`STALE` 与 `INSUFFICIENT_HISTORY` 分开，均不填零。

## 2. 时间与 vintage

只使用 `observed_at <= as_of` 且 canonical availability 不晚于 `as_of` 的观察。可得日期不能被重新绑定为网页发布日期。宏观按观察期间组织，按当时已可得的版本选择 `FIRST_OBSERVED` 或 `LATEST_AS_OF`；FIRST_OBSERVED 仅代表本地首次观察，**不等于经过认证的官方首次发布**。迟到的旧期间修订不会被当作最新期间，同一序列不能混合年、季、月、日频率。

支持水平、变化、收益率、波动率和回撤的单边尾窗变换。分位标准化只使用当前点之前的历史窗，不读取整段测试区间或未来极值。追加未来观察不改变过去截断点的特征与前向过滤结果。低频 carry-forward 受显式过期阈值约束，不把未来月度发布填回日频过去。

CORE_LONG_HISTORY 与 ENRICHED_HISTORY 是明确的模型/特征合同。缺少正式的广度分母、退市历史、rights、财务 vintage 等，不会因为存在这些计算工具而被补成合格真实样本；模型对缺失维度要求独立冻结的历史层模型，禁止零填充。

## 3. 真实拟合的 challenger

`regime_challenger.py` 提供对角高斯 HMM 的 EM 拟合、前向过滤、注册与滚动样本外评估。`configs/regime_challenger_v1.yaml` 冻结六个经济状态的特征空间原型、使用维度、窗口上限和数值参数。TRANSITION/UNCLASSIFIED 是信息与分歧门，不是拿来训练的潜在经济状态。

训练内部 forward-backward 只访问训练窗口。训练得到 emission means/variances、transition probabilities 和初始概率；它不是把手写打分重新命名为训练模型。通过训练特征到预注册原型的指派固定状态语义，不按 holdout 收益事后命名牛熊。测试与在线研究投影只使用前向过滤，不使用未来观测平滑当期状态；跨越过长缺口时重置先验，而非默认为连续观察。

参数哈希绑定配置、输入与全部数值结果，运行完成的墙钟时间独立记录，避免同一训练得到不同数值身份。`fit_registered` 要求完整的注册特征报告/政策/原始对象链；直接传内存合成样本产生的模型标为 `UNREGISTERED_DIAGNOSTIC`。两者都保持 **SHADOW_ONLY**，不自动进入已有市场状态或风险订单权限。

`MarketRegimeService.compare_registered_challenger(model_artifact_id=..., feature_artifact_ids=...)` 是只读对照入口：重新验证注册报告/模型，返回透明规则快照和真正拟合后的 filtered predictions，并统计分歧。不会改写 active state、账户或订单，不自动发放启用资格。原 `infer` 的 configured Markov prior 明确标记 `UNCALIBRATED_CONFIGURED_MARKOV_PRIOR`；不得用该旧先验替代新拟合模型证据。

## 4. 风险上限

`regime_risk.py` 读取真实 `PortfolioIntentProfile.max_total_exposure/max_single_position/minimum_cash_weight/maximum_turnover_weight` 等字段。兼容旧别名时必须相等；冲突、bool、非数值、NaN/Inf、越界值一律拒绝。显式零上限保持零，缺少关键上限才使用版本化配置中的保守默认。

有效总风险不超过 `min(用户总敞口上限, 1-现金底线)`；单票上限同时受用户单票和有效总上限约束；新仓预算还受换手上限约束。牛市系数不能突破任何用户绝对边界。PANIC→BEAR→RISK_OFF→NEUTRAL→HEALTHY_BULL 的有效上限保持单调。持久化 overlay 必须绑定数据库中内容完全相同的 regime snapshot，不能保留旧 ID 后修改状态；纯 `persist=False` what-if 运算不构成正式证据。

这些上限不是目标仓位或自动交易：组合、财务、治理、估值、流动性、确认和成交仍走原能力合同。研究概率/标签不单独触发买卖，`broker_execution_allowed=false` 不变。

## 5. 验证与证据边界

回归文件：`test_regime_risk_binding.py`、`test_regime_feature_pipeline.py`、`test_regime_challenger.py`、`test_regime_registered_pipeline.py`。包括源工件重建、首次/最新 vintage、未来追加不变性、EM 参数非固定值、穷举隐路径对照 forward/expected transition counts、时间有序切分、模型修改拒绝、注册到只读对照和经济表零变化。

数值测试用确定性合成数据检验算法，不计入真实 controlled-live 或 prospective 样本。walk-forward 输出每折范围/样本数/密度/熵/切换率/OOD；**状态熵或概率和为1不等于概率校准**。缺少事前登记、到期且有实际来源的 observable outcome 时，校准字段为 `NOT_EVALUABLE_WITHOUT_OBSERVABLE_OUTCOME`，不生成伪 Brier/log loss。真实 A 股历史分层评估、成本后静态基线对照、前向观察和 owner approval 仍按原上线门完成；代码能训练不能替代这些证据。

复杂度：EM 每轮约 O(T K² + T K D)，内存 O(TK+KD)；政策限制 T/K/D/迭代数。单份特征报告采用有界尾窗；同一调用复用已验证的 payload，来源始终由不可变仓库提供。重算校验发生在显式研究/注册边界，不增加每次投资 preflight 的 HMM 训练开销。

## 6. 方法依据与取舍

- hmmlearn 官方说明：HMM 训练采用 Baum–Welch/EM，forward-backward 与 Viterbi 解决不同问题。采用有限状态、对角 Gaussian、数值稳定 log-space 推断；未增加新的训练框架依赖，复用锁定的 NumPy/SciPy。
  https://hmmlearn.readthedocs.io/en/latest/tutorial.html
- scikit-learn 官方泄漏防范：先分训练/测试，所有学习步骤只拟合训练。采用单边特征、训练期语义映射与按时间滚动评估；不使用全样本 scaler、测试收益挑状态或居中窗。
  https://scikit-learn.org/stable/common_pitfalls.html
  https://scikit-learn.org/stable/modules/generated/sklearn.model_selection.TimeSeriesSplit.html
- SciPy 官方数值原语：采用 `logsumexp` 稳定计算归一化与序列 likelihood，采用 `linear_sum_assignment` 匹配预登记的特征原型；不手写指数归一化溢出回退或按收益重命名。
  https://docs.scipy.org/doc/scipy/reference/generated/scipy.special.logsumexp.html
  https://docs.scipy.org/doc/scipy/reference/generated/scipy.optimize.linear_sum_assignment.html
