# EvalAnchor 0.2

**用真实业务状态、原始工具轨迹和成本证据，判断一次客服 Agent 升级是否值得。**

Agent 说“退款完成”，不代表钱退到了正确订单；平均分提高，也可能掩盖越权尝试或重复提交。EvalAnchor 在本地 OrderOps 订单沙盒中运行多轮 Agent，真实执行工具，再从初始状态重放全部操作，复算任务结果和行为违规，比较模型、提示词与执行预算。

这是面向模型评测 / Agent 工程岗位的本地项目。重点是业务建模、执行可靠性、评测有效性、成本约束和可复核决策。状态评分、配对比较、bootstrap、Pareto 和发布门禁均有成熟近邻；本项目不声称提出新的评测算法。

[产品目标](docs/product-goal.md) · [选题与最近邻审查](research/v2-goal-review.md) · [订单数据卡](docs/orderops-data-card.md) · [实验协议](docs/v2-experiment-design.md) · [交付与复现](docs/reproducibility.md)

![OrderOps 完整测试工作台](docs/figures/orderops-overview.png)

## 先看已经保存的实验

macOS / Linux，Python 3.12+。以下命令全部离线，不需要 API 密钥：

```bash
# 在克隆后的项目根目录运行。
python3 scripts/restore_evidence.py
PYTHONPATH=src python3 scripts/verify_release.py
PYTHONPATH=src python3 -m evalanchor arena serve --workspace experiments --port 8780
```

打开 <http://127.0.0.1:8780>。工作台可切换实验，查看配置矩阵、成本—成功率图、任务族切片，搜索失败并逐步查看模型请求、工具参数、返回值和最终状态。人工审核保存在本地独立 SQLite 中，不修改原始评分，也不触发模型调用。

实验较大，Git 仓库用 [证据清单](evidence/manifest.json) 和 gzip 对象保存原始 `experiment.json` 及完整预算流水；小型 `analysis.json` 可直接浏览。恢复器校验压缩与解压后的大小、SHA256，拒绝路径穿越、符号链接及覆盖内容不同的文件。完整源码 ZIP 已含明文证据；运行同一恢复命令会校验并跳过已存在的正确文件。

v0.2 严格复核历史轨迹费用需要 Python 3.12+：3.12 的浮点求和实现与较早版本可能产生末位差异。本项目保留原始费用和冻结执行器，核验不会用放宽容差绕过这个兼容要求。

```bash
# 导出可分享的单文件只读工作台；审核写入入口会被关闭。
PYTHONPATH=src python3 -m evalanchor arena export \
  --workspace experiments --output runs/orderops-dashboard.html
```

## 评什么，如何判定

OrderOps 使用虚构订单、客户会话、政策、物流信息和用户事件。工具实际读写每个任务独立的本地业务状态；不会连接真实支付、物流或客户系统。

| 关注点 | 验收方式 |
|---|---|
| 任务是否完成 | 最终订单状态与确定性目标谓词、必要证据、最终回执一致 |
| 是否遵守授权 | 身份来自会话，确认来自可信用户事件；预览绑定动作、金额和状态版本 |
| 是否发生危险行为 | 分开记录违规尝试与实际违规；被工具挡住的请求仍可计为违规尝试 |
| 是否正确处理故障 | 检查重复请求、歧义提交、状态冲突、不可信工具文本等任务切片 |
| 结果是否可复核 | 从原始模型响应提取工具调用，在全新状态重放并复算所有分数 |
| 比较是否完整 | 固定开发/测试划分、情境组、配置矩阵与重复网格；失败和预算停止保留在分母 |
| 升级是否划算 | 组等权任务成功率、安全完成率、原始分子/分母、费用、用量和延迟分别展示 |

正确 Agent 可以走不同的工具路径；评分不要求机械复制唯一参考动作序列。最终状态检查之外保留行为和工具事件，避免错误操作再撤销后被掩盖。自然语言礼貌、开放文本解释质量和真实用户满意度不属于当前确定性 oracle 的能力。

## 真实实验与结论

<!-- V2_RESULTS_START -->
已完成 **896 条真实多轮轨迹、4,727 次 Responses 请求**：128 条开发/试跑，384 条主测试，384 条轮数敏感性测试。两个测试矩阵均为 48 案例 × 2 重复 × 4 配置，测试前冻结任务、提示与执行器。共享账本的保守用量估算为 **$1.321334688**；无未知收费、未结算请求或预算超支。

| 配置 | 8 步任务成功 | 12 步任务成功 | 8 步达到上限 | 8 步有限合同 |
|---|---:|---:|---:|---|
| Luna · direct | 93/96 · 96.9% | 93/96 · 96.9% | 0 | pass |
| Luna · checklist | 74/96 · 77.1% | 92/96 · 95.8% | 19 | fail |
| Terra · direct | 96/96 · 100% | 96/96 · 100% | 0 | pass |
| Terra · checklist | 77/96 · 80.2% | 94/96 · 97.9% | 16 | pass |

两个完整矩阵中均未观测到违规尝试或违规执行。**更长的检查清单并未自动改善结果**：8 步条件下，两个 checklist 配置在 injection 任务族均为 0/12，35 条轨迹到达步数上限；扩大到 12 步后，checklist 分别提高 18.75 和 17.71 个百分点，所有轨迹均正常结束，但仍有任务失败。总体合同没有预设逐任务族门槛，因此 Terra checklist 的总体 pass 不能掩盖整族失败。

在当前主测试的通过配置中，Luna direct 观测成本最低（每条约 $0.000262）；Terra direct 多完成 3/96 条，平均每条约 $0.002240。合理下一步是复核低成本方案的失败，再用新留出数据检验是否值得升级。12 步实验在时间上后运行，缓存命中结构也不同；实际费用更低**不能归因于增加步数**。这些是有限自建套件的描述性结果，不是模型通用能力排名或生产放行证明。

[完整结果与消融](docs/orderops-results.md) · [全部失败 CSV](artifacts/orderops-v2/failure-details.csv) · [冻结协议](artifacts/orderops-v2/frozen-protocol.json) · [轮数配对分析](artifacts/orderops-v2/horizon-comparison.json)

![任务族与执行预算的交互](docs/figures/orderops-results.png)
<!-- V2_RESULTS_END -->

`pass` 只表示通过这个版本的**有限测试套件接受合同**；不意味着生产安全、总体能力或统计显著优于另一模型。`fail` 保留具体任务和违规原因；开发、试跑或未完成证据不能得到正式通过。分组 bootstrap 仅作描述，成本—质量前沿也不表示显著优势。

## 执行与预算

运行器使用原生 Responses 工具调用：模型收到真实工具结果后继续决策。模型别名、请求参数、提示快照、环境/工具版本、原始响应和用量都随实验保存。恢复已保存的模型回执不会重新发送相同请求；未知网络结果不能被自动当作免费失败重试。

整个 campaign 共用持久预算账本。每次请求在发送前原子预留费用，已知费用结算，未知收费保留全部预留；线程、进程重启和新实验目录都不重置余额。本轮账本上限为 **4.75 美元**，用户总授权为 5 美元。它是以有效请求费用上界为前提的本地承诺限制，不能代替服务商账户账单硬上限。

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[test]'
python -m pytest -q

# 零模型调用：验证所有自建任务的参考策略可达性。
evalanchor arena oracle --output runs/oracle-check.json

# 默认只展示计划，不读取密钥、不发请求。
evalanchor arena run --split dev --output runs/my-development \
  --ledger runs/my-campaign.sqlite3 --budget 4.75
```

只有显式加入 `--execute` 才会执行付费请求；同一个 campaign 必须一直使用同一 `--ledger`。不要向已有发布实验目录写入新结果。自己的环境、模型或协议变化需要新实验身份与新的明确预算，而不是修改旧记录重新计分。[执行细节与边界](docs/v2-experiment-design.md)。

计划执行自己的新实验时，先在本机创建密钥配置，再编辑文件填入 `OPENAI_API_KEY`。已有 `.env.local` 时保留原文件；密钥不进入 Git 或公开证据。

```bash
# 仅在本地尚无配置时复制模板；这一步不会请求模型。
test -e .env.local || cp .env.example .env.local

# 读取自己已存在的共享账本，显示余额并导出公开流水；不请求模型。
evalanchor arena budget --ledger runs/my-campaign.sqlite3 \
  --output runs/my-campaign-budget.json

# 恢复发布证据后，离线重算两套完整测试矩阵的轮数敏感性。
evalanchor arena compare-horizons \
  experiments/test-v2/experiment.json experiments/test-long-v2/experiment.json \
  --output runs/my-horizon-comparison.json
```

发布材料不包含 SQLite 账本；查看本项目历史预算应恢复 `artifacts/orderops-v2/campaign-budget.json`，并运行发布核验。`arena budget` 用于你实际执行新实验时产生的本地账本。

## 沿用并保留 v0.1

v0.1 的固定输出合同与双框架往返证据保留在原目录：Inspect AI 0.3.286、Promptfoo 0.125.1 各执行 64 题 × 2 次的离线 fixture；Qwen 本机与 Luna API 各有 16 条真实静态合同结果。fixture 只证明互操作，旧静态分数不能当作 v2 Agent 成功率。

```bash
# 单独核验历史证据，不代表通过整个 v0.2 发布核验。
python scripts/verify_release.py --v1-only

# 重做旧版工程演示；不调用模型。
evalanchor demo --output runs/v1-demo
```

[v0.1 能力与原始结果](docs/v1-overview.md) · [双框架集成](docs/integrations.md) · [历史实测记录](docs/results.md)

Inspect 和 Promptfoo 仍提供真实任务/日志互操作；当前 v2 的 OrderOps 运行与重放是独立模块。没有经过相应适配器验收的多轮轨迹，不自动宣称兼容所有上游框架日志。τ-bench、Inspect 和 Promptfoo 已具备状态或轨迹评测能力；本项目的价值是完成一个可运行、可审计的业务升级案例。[近邻与许可](research/v2-goal-review.md)。

## 工程结构与交付

```text
src/evalanchor/       订单环境、Agent运行器、预算、分析、工作台与v1模块
tests/               状态/授权/故障、重放、并发预算、完整网格与HTTP审核测试
examples/orderops/   自建冻结任务与配置
experiments/         分实验的原始证据、分析和公开预算回执
evidence/            Git传输用压缩对象、原始/压缩双重校验清单
artifacts/           冻结协议、总预算回执、历史结果及验收记录
research/ docs/      选题反证、数据卡、实验协议、架构和复现说明
scripts/             恢复、离线核验、框架验收与可复现打包
```

核心运行库使用 Python 标准库；Inspect、Transformers 等是可选集成依赖。`requirements-tested.txt` 记录本机历史验证环境，不是所有平台必须安装的通用依赖锁。

`python scripts/verify_release.py` 默认同时核验 v1 与完整 v2 交付，缺失必需实验会失败，不会静默跳过。`python scripts/package_release.py` 生成带逐文件 SHA256 清单的源码与证据 ZIP，排除密钥、依赖、权重、数据库、冗余调用缓存和嵌套发布包。wheel 用于安装运行库，完整 ZIP 提供研究、测试与可离线重放的证据。

本地完整测试通过 **649 项测试与 13 项子测试**；72/72 个环境任务参考策略可达。CI 配置包括多 Python 版本单元测试、离线证据复核、双框架 fixture 往返及打包后安装验证。配置存在不等于远端运行成功，远端状态以 GitHub Actions 的实际记录为准。源代码和自建数据采用 MIT；上游和模型权利边界见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
