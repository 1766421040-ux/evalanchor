# v0.1 历史结果与证据边界

日期：2026-10-29。软件测试、框架互操作、真实模型推理分别记录，不混为一个“模型准确率”。

## 真实上游框架执行

Inspect AI 0.3.286 与 Promptfoo 0.125.1 分别执行128条（64条case × 2）。输入为自建套件，输出为确定性baseline夹具。原始输出回导后均为80/128通过，与40/64同一比例一致。两份原生日志、Inspect二进制`.eval`及导入run都保留；SHA256见[收据](../examples/integrations/verified/receipt.json)。

这个结果证明上游真实执行、日志解析、ID/输入匹配与重评分可工作。由于模型是mock / fixture，不能将62.5%当模型表现。最初Promptfoo清洗改写hex摘要的失败促成编码修复；不是关闭清洗后忽略差异。

## 原生执行与回归反例

64条合成案例、16组、2次重复。baseline 62.5%，candidate 87.5%，fixed 100%。candidate 有8个关键案例、2组、16次配对观察退化，固定合同门禁FAIL；fixed没有这些退化，但当前采样不支持总体推断，INCONCLUSIVE。

保存的[候选比较](../artifacts/demo/comparison-candidate.json)、[修复比较](../artifacts/demo/comparison-fixed.json)均可从raw重新计算。`scripts/verify_release.py`必须逐字段复算一致，而不只检查某个摘要数字。

## 真实本机模型推理

对已存在的 **Qwen2.5-0.5B-Instruct** safetensors权重做一次16条、8中8英、greedy推理。Apple Metal / bfloat16，max_new_tokens=192，0重试，模型只看messages，没有微调、输出修复或规则兜底。

| 项目 | 实测 |
|---|---:|
| 完成 / 调用失败 | 16 / 0 |
| 严格合同通过 | 2/16（12.5%） |
| 生成token总数 | 636 |
| 记录的逐条推理时间合计 | 约8.85秒 |

时间只包含逐条`generate`与计分，不包含首次模型加载、权重哈希和依赖安装；不得用作完整冷启动吞吐或生产延迟结论。真实权重、tokenizer、配置manifest与原始token IDs在[run.json](../artifacts/local-qwen-final/run.json)中，权重文件不随项目发布。

多条输出使用Markdown代码围栏，因此严格JSON合同判0；另有时区、价格缺失、引用ID及退款单位错误。分数低是真实保留的证据，不提取/修复JSON以美化指标。该16条有限合同集不是通用能力排名，不能将它单独解释为通用能力。与OpenAI的同题对照及配置差异见下文。

沙箱环境的首次MPS访问失败发生在生成之前；随后在本机授权Metal环境中完成上述16条。失败状态保留于开发运行材料，不计为模型能力结果。

## OpenAI API真实实测

在用户授权的小预算内，对同一份16条、8中8英手写套件执行 **GPT-6 Luna / Responses API**。16次请求，0重试，单条max_output_tokens=256、reasoning=none、store=false；未发送参考答案或标签。首次旧凭据认证失败，更新后认证通过，随后才开始生成。

| 项目 | 实测 |
|---|---:|
| 完成 / 调用失败 / 输出截断 | 16 / 0 / 0 |
| 严格合同通过 | 15/16（93.75%） |
| 输入 / 输出 / 总token | 1350 / 394 / 1744 |
| 依据返回用量与已核验单价估算 | $0.000332 |
| 记录的逐条请求耗时合计 | 约54.47秒 |

费用依据response中的default服务层、零cached/cache-write token及gpt-6-luna模型标识计算，**不是读取到账户账单**。执行前保守估计$0.01187625，实际生成较短；预算计划、原始Responses响应及[执行摘要](../artifacts/openai-luna-smoke/smoke-summary.json)均保留。价格来源见[预算说明](openai-budget.md)。

唯一失败为 `refund_preview-2`：模型生成正确JSON后，又追加无关字符和第二段JSON，严格解析返回Extra data。原始输出保留，未截取第一段来提升分数，也未重试该题。

与Qwen比较的题目、分组、参考答案、评分规则和重复网格完全一致；[逐条报告](../artifacts/openai-luna-smoke/index.html)显示12.5%→93.75%、13条改善、3条持平、0条退化，整体仍为 **INCONCLUSIVE**。这是两套目标配置的有限样本对照：本地模型greedy、上限192 token，远端模型reasoning=none、上限256 token；不是仅改变模型权重的控制实验。CPU/GPU、网络与推理实现均不同，不将两者耗时作为公平速度排名。16个手写情境未声明独立随机抽样，高分不能证明通用能力或生产可靠性。

## 如何独立复核

```bash
PYTHONPATH=src python scripts/verify_release.py --v1-only
python -m pytest -q
```

这些命令重算保存证据并测软件，不会重新调用付费API或重新生成模型输出。GitHub Actions配置已提供；只有实际远端run完成后才能称远端CI通过。

## 软件发布验收

本地Python 3.12.14：280项测试与13项子测试通过，7组保存证据复算通过。wheel在全新虚拟环境中以 `--no-index --no-deps` 安装，并从源码目录之外运行demo，观察到预期FAIL / INCONCLUSIVE。报告筛选、失败原文与JSON导出已在浏览器核对。详见[发布记录](../artifacts/release-verification.json)。远端GitHub CI尚未运行。
