# 统计口径与发布决策

EvalAnchor 的正式结论是针对固定评分协议、指定任务分布和允许退化幅度的条件性判断。均分更高并不自动产生 `pass`。默认合成数据只验证测评软件和有限测试合同；其采样不支持总体推断，正常结果可以是 `inconclusive`。

## 先确认可比，再计算

`compare_runs` 首先调用 `contracts.assert_comparable`，其中通过 `audit_run` 对绑定的 `suite_definition` 校验哈希、case 身份，并用原始输出重新计算每条分数。仅修改保存的 score、保留原始输出不变，会被拒绝。两边必须完成且具有完全相同的 suite、protocol、case/repeat 网格、case 内容哈希、group 和 tags。缺失行、重复行、判分版本不一致、同 ID 不同输入都直接报 `ContractError`，不会通过交集连接丢掉失败样本。

每条运行记录保留原始输出。模型请求失败的记录 `status=error, score=0`，仍计入分母；其具体错误另外报告。这是显式的运行可用性计分政策，并不声称 API 超时等同于模型认知错误。解析失败也不能通过修补答案获得成功分数。

哈希证明两份内容一致或有变化，不能证明答案正确、抽样随机、判分器可靠或数据未被训练见过。上游导入后的统一本地判分分数是本项目的分数，不等同于上游原生指标。

## 估计目标与权重

令 `b_gir`、`c_gir` 为组 `g`、题 `i`、重复 `r` 的 baseline/candidate 分数，每个分数在 `[0,1]`。当前所有题使用相同预设重复次数 `R`：

```text
b_g = mean_over_cases(mean_over_repeats(b_gir))
c_g = mean_over_cases(mean_over_repeats(c_gir))
d_g = c_g - b_g
Delta_hat = mean_over_groups(d_g)
```

三个层次分别等权。一个题运行 100 次不会变成 100 个独立题；同一语义任务的 9 个变体也不会压过另一个任务的 1 个变体。`n_pairs`、`n_cases`、`n_groups` 分别展示，正式推断单位是 group。报告同时保留逐组均值，因此可从原始 pairs 重算。

切片只选择具有对应 tag 的题，再执行相同聚合；某个 group 中只有符合 tag 的题参与该切片。切片之间可以相关，Bonferroni 不要求切片独立。不同 group 之间的独立性仍是推断假设。分组、任务权重和标签应在看分数前确定，不能结果出来后改组扩大有效样本。

当前只实现组等权目标，不声称按生产流量加权；若需要生产权重，应新增显式协议、相应区间公式与测试。

## 正式区间：有界独立组的 Hoeffding 界

每个 `d_g` 在 `[-1,1]`。对预设的 `G` 个独立组，不要求组内重复或变体独立。对等权均值使用：

```text
alpha_each = alpha / K
radius = sqrt(2 * log(2 / alpha_each) / G)
lower = max(-1, Delta_hat - radius)
upper = min( 1, Delta_hat + radius)
```

这里 `K = 1 + 标签数`：总体比较加上报告的所有标签切片，包括要求但缺失的标签，均纳入同一 Bonferroni 家族。由独立有界变量的 Hoeffding 不等式和 union bound，可得到家族覆盖至少 `1-alpha`。截断只使用已知参数范围。结论是有限样本保守覆盖，不是从 bootstrap 模拟得到的经验保证。

如果任务组并非同分布，该界针对这些预先定义独立组的期望均值；要把它解释为生产分布性能差，还需额外论证抽样代表性。标签集合不得根据结果筛选。当前没有实现随时有效的置信序列、可选停止或多次版本迭代的累积错误率控制。

默认 `alpha=.05`、允许退化 `margin=.05`、`min_groups=10`。正式统计判断有两个前提：

1. policy 中 `assume_independent_groups=true`。
2. 两次 run 的 `provenance.sampling.independent_groups` 都为 `true`。

运行中的采样声明还必须与绑定的 suite 声明一致；只改单侧 run 的声明会被合同审计拒绝。这两项是用户提供的采样声明，**不是系统验证过独立性**。任一不存在或为 false，统计结论不产生正式 pass/fail。即使同时重写所有自报元数据，也不能把合成模板变为 IID 证据。

## 三态决策

对总体及 `required_tags` 中的每个切片：

| 条件 | 条件性结论 |
| --- | --- |
| 组数不足 `min_groups`，或 required tag 不存在 | inconclusive |
| 下界 `>= -margin` | pass：支持允许容忍界内的非劣性 |
| 上界 `< -margin` | fail：支持超出容忍界的退化 |
| 其余 | inconclusive |

只在上述采样前提成立、baseline 错误率未超过限值且所有必需比较通过时，整体 `pass`。任一必需比较形成正式统计 fail 则整体 `fail`；其余为 `inconclusive`。未被要求的普通切片用于诊断，单独恶化不会自动覆盖用户的预设门禁政策。

另有两种**有限测试集验收合同**可直接 fail，无需总体推断：

- 具有 `critical_tags`（默认 `critical`）的任一配对观测分数下降，保留 case/repeat/group 和两侧分数作为证据。此规则刻意保守；它报告本次有限检查发现的回归，不宣称总体失败概率。
- candidate 的运行错误比例大于预设 `max_error_rate`（默认 0）。

baseline 错误超过限值会阻止 pass，但不会单独判 candidate fail。`decision_basis` 区分 `finite_suite_acceptance_contract`、`conditional_population_inference` 和 `insufficient_evidence`。

## 为什么同时显示 bootstrap

描述性区间对 `d_g` 的组数组有放回抽样，每次完整抽取组，计算均值差，取预设百分位。随机种子和抽样次数写入报告。它展示样本内变化，不承担正式门禁。

例如 24 个观测组差值全为 0 时，普通 percentile bootstrap 必然得到 `[0,0]`，这并未证明真实总体无退化。正式 Hoeffding 区间保持正宽，因而可能诚实地给出 `inconclusive`。报告用 `descriptive_only=true` 和 `degenerate=true` 显示这一情况。

此组 bootstrap 不是嵌套 bootstrap；它不单独分解组内生成随机性，也不估计输出排名后验。需要这些分析时应使用专门工具，并保持其不同的统计含义。[SciPy bootstrap 文档](https://docs.scipy.org/doc/scipy/reference/generated/scipy.stats.bootstrap.html) 说明了配对抽样与退化数据问题；[promptstats](https://github.com/paperwave/promptstats) 已实现更丰富的成对比较和重复运行分析。

## 保守性的实际代价

单比较、`alpha=.05` 时，100 个组的半径约为 0.272；1000 组约为 0.086。若观测差为 0，要通过 0.05 的容忍界至少需要约 2952 个独立组；标签切片的多重比较使所需样本更多。报告给出 `zero_difference_groups_needed_for_margin`，用于解释为什么小样本不能“证明没退化”，并非自动增加样本直到通过的指令。

重复生成可改善组内均值估计，但不会减少本实现的 Hoeffding 半径，除非增加有依据的独立任务组。这是明确的保守设计取舍。后续可以考虑 [Maurer–Pontil empirical Bernstein bounds](https://arxiv.org/abs/0907.3740) 等方差自适应方法；本版本未实现，也不声称它们已验证。

## 资源与重复计算

任一记录的成本未知，整次 run 的 `total_usd` 为 null；同时报告已知部分金额与未知记录数，不把缺失费用当零。latency 是运行记录的描述性 mean/median/p95，受并发、缓存、重试与机器负载影响，不套用质量分数的有界公式。token 字段报告已知小计和覆盖记录数。

pairs 保留输出、得分、原因、错误、身份信息；groups 保留聚合。evidence 字段记录两次 run、policy、suite、protocol 的哈希，以便确认复算对象。政策哈希便于追溯，但单凭哈希不能证明政策真的预注册在观察结果之前。
