# 可靠性路线调研与选题边界

核验日期：2026-10-29。来源为 GitHub 官方仓库元数据、README、源码及官方文档。此文件记录选题证据，而非对这些项目性能的统一 benchmark。未把 stars 作为选型分数，也未运行它们的收费模型评测。

## 核验项目

| 仓库 | 许可证与维护快照 | 已核实能力 | 对 EvalAnchor 的影响 |
| --- | --- | --- | --- |
| [AlpacaEval](https://github.com/tatsu-lab/alpaca_eval) | 代码 Apache-2.0；README 的数据标识为 CC BY-NC 4.0；main `cd543a1`，2025-08-09 | 成对偏好、随机换序、缓存、判分器分析、长度控制胜率 | 不能把随机换序或 judge 校准思想声称原创；不混入存在单独许可限制的原始数据 |
| [FastChat / MT-bench](https://github.com/lm-sys/FastChat/tree/main/fastchat/llm_judge) | Apache-2.0；main `587d5cf`，2025-06-02 | 单答案打分、成对判断、双向换序、不一致结果状态 | 双向 judge 诊断是已知方法；保留不一致，不能平均成“正确” |
| [garak](https://github.com/NVIDIA/garak) | Apache-2.0；main `4b7761c`，2026-10-27 | probe/generator/detector/harness 插件与 JSONL 尝试记录 | 安全命中率与一般正确率语义不同；同 prompt 的 generations 不能当独立任务 |
| [Evidently](https://github.com/evidentlyai/evidently) | Apache-2.0；main `34771fe`，2026-09-10 | Report、Test Suite、CI/CD 检查、JSON/HTML、本地监控 UI | “评测 + 可视化 + 发布门禁”本身不是差异 |
| [Giskard OSS](https://github.com/Giskard-AI/giskard-oss) | Apache-2.0；main `9a30fc3`，2026-10-29 | v3 Scenario/Check/Suite、不可变 Trace、多轮 Agent、质量与漏洞扫描 | 不按已停止积极维护的 v2 教程融合；通用场景测试已经成熟 |
| [Inspect Evals](https://github.com/UKGovernmentBEIS/inspect_evals) | MIT；main `d1adf47`，2026-10-29 | Inspect 任务库、eval-set、日志；Inspect scorer 已支持聚类标准误和整簇 bootstrap | 聚类统计并非新能力；优先轻量按需接入，不复制巨大评测集合 |
| [promptstats](https://github.com/paperwave/promptstats) | 本轮查阅 main README；未把其代码引入项目，许可证未在本子调研中独立审计 | 按输入配对比较、Holm、多种区间、二层 nested bootstrap、方差分解、实验性 mixed-effects | 最直接的统计近邻；配对 CI、重复运行处理和不确定性可视化均不能成为首创声明 |

`pushed_at` 可能包含其他分支活动，不等于默认分支提交。上述日期使用实际 `/commits?per_page=1` 返回的默认分支提交；仓库活跃度不代表每个 API 稳定。软件代码许可证不能替代其所下载 benchmark 数据的许可证审计。

## 查到的具体接口与风险

[FastChat common.py](https://github.com/lm-sys/FastChat/blob/587d5cfa1609a43d192cedb8441cac3c17db105d/fastchat/llm_judge/common.py) 的 `play_a_match_pair` 交换答案顺序运行两次，并保存两份原始判断；读取时不同赢家可成为 `inconsistent`。EvalAnchor 的 judge audit 属于对已知诊断的明确实现，不能解释为新去偏算法，也不能把 judge 一致性直接当事实正确率。

[AlpacaEval helpers.py](https://github.com/tatsu-lab/alpaca_eval/blob/cd543a149df89434d8a54582c0151c0b945c3d20/src/alpaca_eval/metrics/helpers.py) 的胜率计算会过滤非偏好值。本项目选择另一份明确的合同：运行错误与解析错误保留，显示完整分母及失败类别，避免导入后因丢行改变任务总体。这个选择不是断言上游实现错误，而是发布门禁需要不同的数据完整性边界。

[garak attempt.py](https://github.com/NVIDIA/garak/blob/4b7761c2108446043012549cbe8f7c942cabb26d/garak/attempt.py) 将一个源 prompt 与多个 generations 放入同一 Attempt；序列化含 uuid/status/probe/outputs/detector_results。未来若添加 garak adapter，必须只接入完整状态、保存检测器名称与原始分数方向，不能把安全指标盲目改名为 accuracy。**本版本未声称已实现 garak adapter。**

[Giskard checks](https://github.com/Giskard-AI/giskard-oss/blob/9a30fc3b8f78532fb74e2ef0f541fdc12b50e8e6/libs/giskard-checks/README.md) 的 Suite 在没有有效评估时 `pass_rate=None`，Scenario 第一个检查失败后剩余步骤跳过。未来导入必须保留 skipped，而不能当成功或无声丢弃。**本版本未声称已实现 Giskard adapter。**

[Inspect scorer 官方接口](https://inspect.aisi.org.uk/reference/inspect_ai.scorer.html) 已有 `stderr(cluster=...)` 及支持整簇重采样的 CI，因此新增 bootstrap 无法支撑方法创新。[promptstats README](https://github.com/paperwave/promptstats/blob/main/README.md) 进一步覆盖二层重复运行分析，其 `from_dataframe(..., repair=True)` 可修复/平均部分输入；EvalAnchor 选择对不完整或重复证据直接拒绝的工程合同。两者用途不同，不宣称 EvalAnchor 的统计效率优于 promptstats。

## 比较过的两条机制路线

**A. 可比性契约与发布证据（本次实施主线）。** 解决跨框架、跨版本得分无法可信比较的问题。先验证任务/重复/判分版本/原始内容身份，再输出固定容忍界下的三态结论，保存可重算原始证据。已知统计和已有框架负责各自擅长的部分。自己的工程工作在严格 adapter 边界、完整失败分母、组等权、假设显式化、可追溯决策与故障反例测试。

最强反对意见是它可能只是一层 ETL 加已有统计。检验方式是让“少了失败题”“100 次重复充当 100 个新题”“同 ID 改题”“改了 scorer”“某类关键任务退化但总分上升”等案例进入系统，检查是否给出明确拒绝/失败证据。仅有漂亮仪表盘不足以成立这个项目。

**B. 判分器校准工作台（保留挑战路线）。** 固定人工 gold 校准集，检查双向换序、长短扰动、相同答案、明显事实错误与输出中的指令干扰，形成误判复核队列。最近邻为 AlpacaEval、FastChat 和 Giskard。缺点是没有独立 gold 标签时容易成为“用模型证明模型”；修改长度也可能改变信息量，不能直接归因为偏差。当前 judge audit 只提供明确的诊断统计，不声称真实 judge 已通过校准。拥有用户领域人工标注集时可以重开该路线。

## 统计取舍与证据范围

主线采用已知 Hoeffding + Bonferroni 作为保守正式界，组 bootstrap 只作描述。这处理了“所有观察差值相同导致 bootstrap 零宽”的危险验收情形；代价是样本量要求明显更高。独立/代表性必须由任务设计提供，哈希和 case 数量无法证明。

合成 fixture 用于软件验证，不用于模型排行、宣称实际模型提升或外推业务收益。judge 偏好、规则正确性、运行可用性、费用与延迟分别解释；缺失成本保留 unknown。详见 [统计口径](../docs/statistics.md)。

本次没有建立论文级创新性结论，没有复制上述仓库的数据或核心源码，也没有把调研到的每个框架描述为已集成。实际集成范围以运行代码、adapter 测试和可复现验证记录为准。
