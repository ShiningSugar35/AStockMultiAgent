# 2026-09-14 自然投研表达与定时跟踪技术侦察

> 状态：CURRENT SCOUTING NOTE
> 工作包：WP-24

## 结论

本次不引入第三方运行时依赖。外部 Humanizer 项目只作为规则设计参考，采用“小型本地 policy + 事实等价审计 + 有界重写”的模式；事实、数字、引用、风险、条件和投资结论继续由现有 ResponseGateway 与机器合同保护。

| candidate | source / maturity | project fit | decision | smallest next action |
|---|---|---|---|---|
| ai-zixun/humanizer-zh | GitHub；2026-09-14 检索时约 150 stars / 16 forks，v1.3.0，MIT；专门处理中文翻译腔、模型拼接稿、营销通稿与段落节奏 | 当前候选中可见采用度最高，且与“中文母语式投研表达”最接近 | ADAPT_PATTERN | 采用“删除空泛大词、拆机械对照、恢复中文句法节奏、保留事实”的规则思想；不安装运行时依赖 |
| jiji262/humanizer-chinese | GitHub；36 类中文模式，MIT；由英文 humanizer 本土化，并补充 CCL 2023、HC3 与中文社区经验，明确反对编造第一人称、假口语和故意错字 | 研究依据和防过度纠偏边界较完整 | ADAPT_PATTERN | 用于负向边界与审稿 rubric：自然化不能编故事、删限定条件或改投资结论 |
| lian-xiao/zh-humanizer | GitHub；2026-09-14 检索时 1 star / 0 forks，MIT；强调不增事实、保持语域和控制篇幅 | 规则清楚，但采用度不足，不能称为高认可度项目 | REFERENCE_ONLY | 仅参考“不新增事实、保持学术语域”的边界，不作为依赖或主要依据 |
| z123-cloud/humanizer | GitHub；0.x 开发版，规则/API 仍变动 | 可参考，但稳定性不足 | WATCH | 不依赖其 API/版本 |
| ChatGPT Scheduled Tasks | OpenAI 官方帮助中心；平台权威能力说明 | 可承担定时语义复核/通知；支持的计划频率、账户/应用权限属于平台能力 | ADAPT_PATTERN | 保留本地 monitor 为事实平面；平台任务只做批量语义复核，不写死套餐容量或把“创建成功”当成“已成功运行” |

## 采用边界

1. “去 AI 味”不是规避检测器。目标是自然、克制、可读，且事实不漂移。
2. 不做简单同义词替换；优先删空话、拆机械对照、恢复明确主语和因果。
3. 引用、URL、证券代码、数值、日期、金融公式与真实不确定性不进入机械改写。
4. 高阶术语仅在确有必要时保留，并首次出现时给一句简释；常见术语不做教科书式注释。
5. ChatGPT 定时任务能力按运行时 capability 处理。OpenAI 官方帮助中心 2026-09-14 的现行说明是：符合条件的付费计划可将周期任务做到最高每小时一次并支持精确时间；任务是否可用仍取决于账户、应用版本及连接器权限。因此业务 Schema 不写死某个模型、套餐容量或永久上限。
6. 官方同时明确：在 Project 中创建的 Scheduled Task 不能直接读取该 Project 的上传文件；任务可使用账户当前支持且已授权的连接应用。对本项目而言，平台任务必须每次重新取得允许的连接能力，不能把“任务已创建”当成“已读取本机项目”或“已完成持仓复核”。本地 Continuous Monitor、canonical 账户/研究状态和下次会话增量恢复继续承担事实平面与 fallback。

## License / dependency decision

本次只抽象设计模式和自写少量规则，不复制第三方源文件，也不新增 package dependency。若未来需要直接 vendoring 某个规则文件，必须先单独核验对应仓库 LICENSE、版本 pin 和 attribution。
