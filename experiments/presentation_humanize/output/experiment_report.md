# E-01 受约束中文 Humanize A/B 实验报告

- 实验：`E-01-presentation-humanize-20260902`
- 报告 ID：`7a9e029d983ee4febb637a672c3728083570522a090d067a736257bdd06f0a81`
- 网络请求：0
- 外部模型调用：0
- 外部 Humanizer Skill 实跑：否（仅风险画像样本）
- 真实人工盲评：未完成
- 生产提案：`NO_CHANGE`
- 生产默认开关：关闭

## 自动门结果

| 实验臂 | 事实漂移 | 敏感泄漏 | 自动门 | 状态 |
|---|---:|---:|---|---|
| RULE_BASELINE | 0/8 | 0 | 通过 | REFERENCE_BASELINE |
| PROJECT_CONSTRAINED | 0/8 | 0 | 通过 | BLOCKED_HUMAN_REVIEW_PENDING |
| HUMANIZER_ZH_RISK_PROFILE | 0/8 | 0 | 未通过 | REJECTED_RISK_PROFILE |

## 裁决

本实验没有改动生产 Response Gateway。项目受约束臂即使通过自动事实门，
在真实人工盲评完成前仍不得形成生产备用提案。Humanizer-zh 相关样本仅用于
复现其公开规则可能引入的虚构经历、具体细节或金融事实漂移风险，未被下载、
执行或授予任何来源/生产资格。当前裁决为保持确定性规则基线不变。
