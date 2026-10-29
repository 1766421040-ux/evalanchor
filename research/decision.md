# 选题决定：版本决策的证据可信度

检索日期：2026-10-29。目标由用户确认：求职作品集，突出工程完整度与技术深度；允许节省使用已有OpenAI API额度。本次在本机落实工程，采用具体反例检验设计。

## 比较的四类方向

| 机制族 | 解决什么 | 最接近的项目 | 本次判断 |
|---|---|---|---|
| 通用能力排行榜 | 统一跑知识/数学/代码benchmark | lm-eval、LightEval、OpenCompass、HELM、Evalchemy | 基础设施成熟；本机资源不适合承诺大规模榜单。作为未来输入源，当前不重建 |
| RAG/Agent语义评分平台 | 检索、工具链、轨迹、judge与观测 | Ragas、DeepEval、Phoenix、TruLens、τ-bench、BFCL | 与已有Agent/训练项目重合，真实数据和标注门槛较高。选确定的接口做深 |
| 可比性与发布证据审计 | 阻止漏样本、错协议、改分及均分掩盖的回归 | Inspect、Promptfoo、Evidently、Giskard、promptstats | **主线**：明确场景，能用真实上游执行和反例形成完整验收 |
| Judge校准与偏差实验 | 检查评审器自身是否可靠 | AlpacaEval、FastChat/MT-bench、Giskard | **保留挑战路线**：当前缺独立人工标注，先做位置交换/覆盖诊断模块 |

框架细节与具体源码链接分为 [通用框架](frameworks.md)、[应用评测](application-evals.md)、[可靠性](reliability.md)。根代理另实时核验 [promptstats README](https://github.com/paperwave/promptstats/blob/main/README.md)：其已包含配对差、多重比较、重复采样及统计图，因此这些均不能成为原创性宣称。

## 检索改变了哪些决定

1. LightEval已将Inspect后端列为推荐路径，因此“把两者融合”并非独特成果；本次连接执行机制差异更大的Inspect和Promptfoo，并要求实际往返。
2. HELM已进入维护模式，当前不把它作为新项目运行核心。
3. Phoenix的ELv2许可与MIT/Apache不同，未复制或嵌入其服务。
4. 旧τ-bench任务已被官方标记过时，新版又改变协议，不能把两个版本的分数直接混比。
5. Inspect已有cluster bootstrap，Evidently/Giskard已有CI门禁，promptstats已有统计比较；将差异收窄为严格跨框架证据流程与错误比较拒绝。
6. 普通bootstrap在恒定差值上退化，不能独自承担非劣放行；正式门禁采用简单有界集中界，公开其保守代价。

## 最强反对意见

一个有经验的团队可用Inspect、Promptfoo和若干Python脚本完成相似工作。EvalAnchor的价值应通过完整、可维护、可复核的实现衡量，而不能用“全新评测算法”包装。

因此验收面向可检验行为：原始输出不变而分数被改时必须报错；相同ID下题目变化必须拒绝；缺失失败样本不能提高分数；重复100次不能把独立组数变100倍；平均分提升而关键合同退化必须阻断；非IID合成套件不能声称总体通过；真实框架运行日志必须往返无损重评分。

## 自己的设计与融合边界

自己实现的核心包括：suite/run evidence schema、messages投影、可重评分审计、完整重复网格、SQLite恢复合同、组等权配对计算与正式/描述区间分离、关键回归规则、可携带原始证据报告。上游仍负责自己的执行协议和日志，未复制其源码再改名。

当前业务套件不是权威综合benchmark，评的是确定字段合同。真实模型实验使用同一有限套件、不改输出，结果即使差也保留；演示夹具只验工程。

## 下一步与重开条件

- 有真实标注集后：冻结train/dev/test与独立组，加入盲测judge校准；才讨论更广泛质量结论。
- 有标准benchmark需求后：按lm-eval逐样本协议新增adapter，先验证输入/metric/filter语义，再纳入比较；不直接导入其总分。
- 有更大真实抽样集后：比较Hoeffding与方差自适应界、预注册所需样本量；不得为通过而事后改margin。
- 有多人长期使用需求后：设计身份认证、对象存储、队列、签名证据与审计权限；不把单机SQLite描述成生产多租户平台。

这些是后续扩展条件，不是本次已完成的功能或实验结论。
