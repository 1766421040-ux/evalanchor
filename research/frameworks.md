# 通用测评框架源码调研与集成选择

调研日期：2026-10-29。以下判断基于本轮读取的官方 GitHub README、许可证、文档、源文件和默认分支提交，不是 stars 排名。代码维护记录只是维护信号，不代表所有能力均已在本机验证。本机执行证据另见 `examples/integrations/verified/receipt.json`。

## 五个主要候选

| 项目 | 许可证 | 本次看到的默认分支提交 | 核心价值 | 本项目的取舍 |
|---|---|---|---|---|
| [lm-evaluation-harness](https://github.com/EleutherAI/lm-evaluation-harness) | MIT | `d6de81643928d653435c431bae19945d41d32520`，2026-09-14 | 标准学术能力基准、模型后端和任务扩展；完整逐样本日志 | 保留未来标准能力结果接入方向；v0.1 不虚称已集成 |
| [Inspect AI](https://github.com/UKGovernmentBEIS/inspect_ai) | MIT | `19a11770f1557246402df69cb7cac253f4036848`，2026-10-29 | Task/Solver/Scorer、模型提供者、多轮工具任务、完整日志 | 实现真实 Task 导出和原生日志导入 |
| [LightEval](https://github.com/huggingface/lighteval) | MIT | `6d0cfb7d993471ba5a84e659f6263e1337c7c562`，2026-10-29 | 大量任务、后端与 sample-level 结果 | 已官方整合 Inspect；不重复开发相同 runner |
| [OpenCompass](https://github.com/open-compass/opencompass) | Apache-2.0 | `0aa60698c0426971737d3b97989be9b808cb7896`，2026-10-28 | 中文生态、配置注册、任务划分、推理和汇总 | 参考分层架构及导入陷阱；v0.1 不支持其任意结果文件 |
| [HELM](https://github.com/stanford-crfm/helm) | Apache-2.0 | `63754d05db6f874e41a395880fb573890a13e791`，2026-06-05 | 场景、适配器、指标分离，多维度透明测评 | 借鉴设计；不作为新运行底座 |

许可证原文：[lm-eval](https://github.com/EleutherAI/lm-evaluation-harness/blob/main/LICENSE.md)、[Inspect](https://github.com/UKGovernmentBEIS/inspect_ai/blob/main/LICENSE)、[LightEval](https://github.com/huggingface/lighteval/blob/main/LICENSE)、[OpenCompass](https://github.com/open-compass/opencompass/blob/main/LICENSE)、[HELM](https://github.com/stanford-crfm/helm/blob/main/LICENSE)。这些是软件许可证；不能据此推断其全部外部数据集、模型或商标的许可条件。EvalAnchor 未复制这些项目的大段实现或数据集。

### lm-eval：适合扩展，已经不必 fork

[插件指南](https://github.com/EleutherAI/lm-evaluation-harness/blob/main/docs/plugins.md)列出模型、过滤器、指标、聚合器四类 entry point。任务使用 `include_path`，而非插件组。注册表是进程全局状态，内置组件优先。2026/09 的 README 已说明独立 package 插件机制。

[Python API](https://github.com/EleutherAI/lm-evaluation-harness/blob/main/docs/python-api.md)中的 `simple_evaluate()` 返回指标、任务配置、版本、few-shot 数、方向和样本结果。它已经有 [system instruction/chat template 哈希](https://github.com/EleutherAI/lm-evaluation-harness/blob/main/lm_eval/loggers/evaluation_tracker.py)。因此，把“保存 prompt 哈希”包装成原创贡献并不准确。

接口边界：没有 logprobs 的 API 无法直接运行所有 likelihood 任务；生成任务的抽取策略改变可能改变排名。未来导入应保留 task/version/filter/metric，而不能只读取一个 accuracy 字段。

### Inspect AI：采用公共扩展 API

[Scorer 源码](https://github.com/UKGovernmentBEIS/inspect_ai/blob/main/src/inspect_ai/scorer/_scorer.py)定义异步 `Scorer(TaskState, Target) -> Score | None`，[Sample 类型](https://github.com/UKGovernmentBEIS/inspect_ai/blob/main/src/inspect_ai/dataset/_dataset.py)区分输入、目标、ID、元数据。EvalAnchor 通过这些公共接口构造任务，输入只取 messages；expected 留在本地评分器。

[日志文档](https://inspect.aisi.org.uk/eval-logs.html)支持 JSON 与 `.eval`、逐样本记录、离线重评分和 score history。[评分文档](https://inspect.aisi.org.uk/scorers.html)已经覆盖精确匹配、模型判分及格式失败。EvalAnchor 的价值应是业务契约、严格配对和门禁工作流，而非宣称发明这些基本能力。

本轮最新提交涉及 sandbox command output 完整保全与构建版本门禁。该信号说明项目仍在维护，但 EvalAnchor v0.1 不执行用户代码或工具，因此不声称提供 Inspect 全部沙箱能力。

### LightEval：现有整合直接削弱一个候选方向

[README](https://github.com/huggingface/lighteval/blob/main/README.md)已把使用 Inspect AI 的 `lighteval eval` 列为 preferred 入口。[任务配置源码](https://github.com/huggingface/lighteval/blob/main/src/lighteval/tasks/lighteval_task.py)包含 `solver/scorer/sample_fields` 兼容字段，以及 dataset revision、few-shot split、generation 配置。故“融合 LightEval + Inspect”不是尚未实现的方向。

它适合批量基准运行；[自定义任务文档](https://huggingface.co/docs/lighteval/en/adding-a-custom-task)说明 `Doc`、`LightevalTaskConfig` 和 `TASKS_TABLE`。继续把它作为第三个依赖会增加安装/版本矩阵，v0.1 没有足够收益。

### OpenCompass：结果的分母与抽取语义不能省略

[扩展总览](https://github.com/open-compass/opencompass/blob/main/docs/en/extension/extension_overview.md)将扩展分为 Dataset、PromptTemplate/Retriever/Inferencer、Postprocessor/Evaluator、Model、Partitioner/Runner/Task 和 Summarizer。[注册表源码](https://github.com/open-compass/opencompass/blob/main/opencompass/registry.py)可核实具体接口。

[Evaluator 指南](https://github.com/open-compass/opencompass/blob/main/docs/en/extension/new_evaluator_and_summarizer.md)明确两类容易误读的情况：Dataset 和 Evaluator 两处的后处理会顺序执行；重复运行的平均值依赖每批返回完整非空 details，否则当前路径可能只返回最后一批结果。它说明“总分可读”不等于“测评语义可比”。本项目以完整 case/repeat 网格和原始输出作为导入条件。

### HELM：设计值得学，生命周期不适合做新主依赖

[维护政策](https://github.com/stanford-crfm/helm/blob/main/docs/maintenance_mode.md)明确 2026-06-01 进入 maintenance mode，由志愿者 best effort 维护；不再增加新框架功能和新的官方 leaderboard evaluations，外部 API 变化可能影响场景。[Scenario 源码](https://github.com/stanford-crfm/helm/blob/main/src/helm/benchmark/scenarios/scenario.py)展示 Input/Output/Reference 分层和数据集 split，仍适合学习。

## 另外三个会改变选题的近邻

1. [Unitxt](https://github.com/IBM/unitxt)，[Apache-2.0](https://github.com/IBM/unitxt/blob/main/LICENSE)：已经有可分享的 task/card/template/metric 组合。[模板与 few-shot 分组示例](https://github.com/IBM/unitxt/blob/main/examples/evaluate_different_templates_num_demos.py)、[robustness augmentor 示例](https://github.com/IBM/unitxt/blob/main/examples/robustness_testing_for_vision_text_models.py)表明“对 prompt 做扰动再分组”不新。本项目把变体归属原始场景组，避免将相关变体当作独立证据；这也是常规统计原则，不能叫新理论。
2. [Evalchemy](https://github.com/mlfoundations/evalchemy)：已经以 lm-eval 为基础组合聊天、指令、数学、代码任务。单纯组合 benchmarks 易与它重合。本轮只核验 README，未作安装验收，因此不评价其当前全部运行能力。
3. [AgentCompass](https://github.com/open-compass/AgentCompass)：README 记载 2026-09-30 首个 PyPI v1.0.0，Apache-2.0；已解耦 Model/Benchmark/Harness/Environment，包含并发、恢复、轨迹与 usage/latency。再做通用 Agent runner 同样缺少明确差异。本轮只核验 README，未执行其 benchmark。

## 为什么选 Inspect + Promptfoo 的严格互操作

[Promptfoo Python provider](https://www.promptfoo.dev/docs/providers/python/)可以把本地 Python 程序接入它的测试矩阵；[测试配置](https://www.promptfoo.dev/docs/configuration/test-cases/)支持变量、metadata、重复；[输出文档](https://www.promptfoo.dev/docs/configuration/outputs/)支持完整 JSON。它和 Inspect 分别代表常见的应用测评和 Python 研究测评工作流。

本项目不是简单把两个库放在 requirements 中：它导出真正可运行的任务/配置，执行原框架，再将 raw output 与原始输入、case hash、重复次数绑定，重算本地契约分数并保留上游分数。只有经过真实执行的版本才列为已验证。任意第三方日志并不自动符合此契约。

## 选题比较、反对意见和重开条件

| 候选机制 | 价值 | 最近邻与反对意见 | 决策 |
|---|---|---|---|
| 通用大模型排行榜 | 容易理解，基准覆盖面广 | lm-eval、OpenCompass、LightEval 已成熟；个人很难维护基准与模型广度 | 暂不做主线 |
| Agent 环境/执行评测 | 可定位多轮工具错误 | Inspect、AgentCompass 已有完整运行基础；环境隔离与数据维护成本高 | 保留为未来接入 |
| 业务版本升级的证据与回归门禁 | 直接回答平均分上升时能否升级 | Inspect + pandas 脚本也能做；便利包装不等于方法创新 | 本轮实施，必须有严格失败示例和真实双框架闭环 |

判别验证：同一合成任务套件中，候选总体得分上升却在 critical 场景退化；另外对日志注入错 ID、漏行、重复行、变化输入、变更 expected。一个只看平均值或盲目 merge 的流程会误判，EvalAnchor 应分别 fail 或拒绝比较。该验收证明工程逻辑，不证明真实模型效能或学术创新。

重开条件：若用户确实需要广泛的基础能力排行榜，则优先实现 lm-eval 数据适配；若主要业务依赖多步有状态工具任务，则增加真实执行状态/轨迹契约，而不把单轮 JSON 工具建议误称为 Agent 完成任务。若实际用户能用短脚本同样可靠完成全部需求，应收缩本项目的独立平台主张。
