# GitHub 应用评测项目调研与选择

核验日期：**2026-10-29**。通过 GitHub API 与仓库源码实时读取下列项目的 README、接口、提交和许可证。本文中的维护日期表示当时的默认分支提交或版本记录，不代表未来维护承诺。star 数未用于决定选题。

## 候选与边界

| 项目 | 适合复用的能力 | 核验状态 | 选择判断 |
|---|---|---|---|
| [Promptfoo](https://github.com/promptfoo/promptfoo) | 配置驱动的模型/prompt/断言矩阵，Python provider、assertion、导出结果 | MIT；默认分支 `1b4dff170a589a31bbd57d4878e1abc59ef80655`（10-29）；release 0.125.1（10-25） | 优先真实适配，减少重复实现模型生态 |
| [DeepEval](https://github.com/confident-ai/deepeval) | 用例与指标协议，RAG、工具、轨迹指标，pytest式工作流 | Apache-2.0；`90e230cbe916c73240a48312c494065a39bf52d1`（10-29） | 适合未来可选指标插件，不宣称本项目已支持全部指标 |
| [Ragas](https://github.com/vibrantlabsai/ragas) | RAG样本协议、claim/NLI评分、上下文ID | Apache-2.0；`298b68274234c060deacab3cf5fb52aa3a20e885`（02-24）；release v0.4.3（01-13） | 可选语义评测来源；已有确定性合同评分不等同于Ragas |
| [Phoenix](https://github.com/Arize-ai/phoenix) | OTel/OpenInference轨迹、dataset/experiment、自托管服务 | 根及phoenix-evals LICENSE为ELv2；`9089efc2bdbd7d71286cd27a3b731b132f41fb79`（10-29） | 可作为外部观测系统；不整体复制进MIT工程 |
| [TruLens](https://github.com/truera/trulens) | OTel装饰器、span选择器、Metric、Run API | MIT；`2437c3a3d4956a97520ffa94f839a94fda20eafe`（10-28）；2.16.0（10-19） | 重要最近邻；trace、版本比较和judge验证已有实现 |
| [旧τ-bench](https://github.com/sierra-research/tau-bench) | 交互式客户支持环境及历史轨迹 | MIT；README明确任务已过时并指向tau2-bench | 不采用其旧任务作为新的正式基准 |
| [当前τ-bench仓库](https://github.com/sierra-research/tau2-bench) | 多语言/语音交互、冻结任务、独立task reward与post-hoc judges | MIT；`7aad81c37a3a78b4ed3e4866769f851400c64415`（10-29）；README为2.0 | 高质量可选外部结果源；成本较高且版本分数不可混比 |
| [BFCL](https://github.com/ShishirPatil/gorilla/tree/main/berkeley-function-call-leaderboard) | AST、执行/状态检验，生成与评分分离 | Apache-2.0；默认分支 `6ea57973c7a6097fd7c5915698c54c17c5b1b6c8`（03-23） | 工具能力基准较成熟；单纯重建与已有ToolShift过于相近 |

Ragas原链接 `explodinggradients/ragas` 已重定向到 `vibrantlabsai/ragas`。DeepEval最近GitHub release条目是 `setup-v0.1.0`，它不是DeepEval库本体的可靠版本号；实际集成需读取已安装package metadata。BFCL仓库的 `pushed_at` 晚于默认分支最新提交日期，两者不能混称“最近主线更新”。

## 可接入接口与明确的限制

### Promptfoo

[Python provider源码文档](https://github.com/promptfoo/promptfoo/blob/main/site/docs/providers/python.md)规定 `call_api(prompt, options, context)` 返回字典；[Python assertion文档](https://github.com/promptfoo/promptfoo/blob/main/site/docs/configuration/expected-outputs/python.md)支持 `get_assert(output, context)`。当前[ProviderResponse](https://github.com/promptfoo/promptfoo/blob/main/src/contracts/providers.ts)包含raw、output、tokenUsage、latencyMs、cost、cached等字段。

可将固定suite导出为Promptfoo配置，经过真实执行后导入原始输出并重新评分。必须限制已测过的schema、保留case哈希和原始结果，不能仅凭JSON看起来相似就宣称互通。缓存成本与本次实际成本需区分；不得把离线replay耗时当在线推理速度。

### Ragas

[SingleTurnSample/MultiTurnSample](https://github.com/vibrantlabsai/ragas/blob/main/src/ragas/dataset_schema.py)定义user_input、retrieved_contexts、reference、response和上下文ID等。当前[Faithfulness源码](https://github.com/vibrantlabsai/ragas/blob/main/src/ragas/metrics/_faithfulness.py)先生成声明，再判定可由上下文推出的比例；无声明时返回NaN。

未来导入这类指标必须保留invalid/unavailable及coverage，不能靠忽略NaN得到更高均分。FaithfulnesswithHHEM仍依赖LLM分解声明，源码还使用模型下载与远端代码加载；不能将它称为零模型依赖的客观规则裁判。本项目的grounded_answer仅为已知值和引用ID合同，不冒用其语义指标名称。

### DeepEval

[LLMTestCase](https://github.com/confident-ai/deepeval/blob/main/deepeval/test_case/llm_test_case.py)包含input、actual_output、expected_output、retrieval_context、工具调用等；`additional_metadata`已弃用，应使用metadata。[BaseMetric](https://github.com/confident-ai/deepeval/blob/main/deepeval/metrics/base_metric.py)保留score、reason、error、成本、token和skipped状态。

可在扩展中映射合同，但接入某个TestCase不代表已验证全部指标。对LLM裁判结果应另外保存模型/模板版本和无效输出，不能与确定性合同分数无标识混合。

### Phoenix与TruLens

Phoenix的[根许可证](https://github.com/Arize-ai/phoenix/blob/main/LICENSE)及[evals许可证](https://github.com/Arize-ai/phoenix/blob/main/packages/phoenix-evals/LICENSE)均为Elastic License 2.0，限制以托管/管理服务提供其大量功能。选择可选互操作而非直接整体融合，保留第三方权利边界。

TruLens [README](https://github.com/truera/trulens/blob/main/README.md)示范OTel `@instrument`、`Metric`、`Selector`与`RunConfig`。它已经覆盖版本、成本质量比较及对人工标注的judge验证，所以这些一般功能本身不构成EvalAnchor原创性证明。

### τ-bench与BFCL

当前[τ-bench README](https://github.com/sierra-research/tau2-bench/blob/main/README.md)区分冻结任务版本，明确1.x与2.0输入不同、分数不可直接比较，也区分后置judge与task reward。[旧仓库README](https://github.com/sierra-research/tau-bench/blob/main/README.md)直接告警过时任务。这是必须将数据、输入、评分协议纳入比较指纹的具体证据。

BFCL的[子项目README](https://github.com/ShishirPatil/gorilla/blob/main/berkeley-function-call-leaderboard/README.md)分开生成和评分，并支持已有模型endpoint；PyPI包名是`bfcl-eval`，不是无关的`bfcl`。其AST及状态执行判定可做外部结果来源，本项目不复制该benchmark或宣称执行过这些任务。

## 收敛理由与挑战路线

选定的问题是：**候选模型/提示版本的总分更高时，比较是否完整可复核，关键失败是否被掩盖？** EvalAnchor自己的工作是严格的样本/协议合同、可追溯导入、同样本配对分析、分组与缺失处理、关键退化阻断，以及独立的裁判审计。上游负责其已有执行能力；不把多个框架拷进同一目录当成创新。

最强反对意见是现成框架已经能做执行、trace、比较、门禁，新的封装可能更弱。验收因此必须展示具体故障：丢失样本制造的虚假提升、重复样本、协议漂移、错误类型等价、关键子群退化和位置偏好。若无法在真实适配器输入上可靠识别这些故障，应缩小兼容范围，不能扩大宣传。

保留的挑战路线为RAG语义裁判校准与τ-bench真实轨迹回归。前者需要可信人工标注和实际judge调用预算；后者需要固定benchmark版本与多轮调用预算。未具备这些证据时，先保留扩展接口，不把合成验收数字包装成真实模型结论。

与已有作品的分工：ToolShift处理工具变化适应，DeltaWeave处理更新传播，EvidenceGate约束训练与证据生成。EvalAnchor评价输出与版本比较的证据完整性。它是工程整合和测量严谨性的作品；本轮调研与有限实现不足以声称新的科研算法或行业首创。
