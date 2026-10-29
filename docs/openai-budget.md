# OpenAI低预算实测

该入口用同一份 `local_smoke_suite()` 执行16个真实API请求；输入仍是手写合成情境，结果只说明这些情境上的行为。默认只打印预算，不读取密钥、不联网。真实执行需要显式 `--execute`。

## 官方参数核验

截至2026-10-29，[GPT-6 Luna官方模型页](https://developers.openai.com/api/docs/models/gpt-6-luna)支持Responses API，`reasoning.effort`含`none`。本入口固定使用 `gpt-6-luna`、`reasoning={"effort":"none"}`、无工具调用；`max_tokens`配置映射为Responses的`max_output_tokens=256`，可显式选择512。[官方推理说明](https://developers.openai.com/api/docs/guides/reasoning)明确该上限包括不可见推理/格式token，生成可能因上限而不完整。本项目保留这种原始响应，不修补或自动续写。

请求使用 `POST https://api.openai.com/v1/responses`，设置 `store=false`、`truncation=disabled`、`stream=false`、`service_tier=default`。不发送temperature、模型参考答案、标签或case ID；HTTP Authorization只在内存请求头中使用。参考[Responses API](https://developers.openai.com/api/reference/resources/responses/methods/create)及[文本输出结构](https://developers.openai.com/api/docs/guides/text)。响应可能有多个output项，解析器按原顺序聚合文本，完整native response一并留存。

## 密钥配置

在项目根目录的本地 `.env.local` 写入一个字面值配置：

```dotenv
OPENAI_API_KEY=你的实际密钥
```

不要把真实密钥写进示例JSON、命令参数或提交记录。脚本仅读取 `OPENAI_API_KEY`，优先使用已有环境变量；不会执行 `source`、shell、变量插值或命令替换，不会打印密钥。允许单引号/双引号包裹字面值；拒绝重复定义、空值和shell表达式。其他变量不加载。

## 运行

在项目根目录执行：

```bash
.venv/bin/python scripts/run_openai_smoke.py
.venv/bin/python scripts/run_openai_smoke.py --execute
```

如需检验重复生成，可新建输出目录并显式选择两次重复：

```bash
.venv/bin/python scripts/run_openai_smoke.py --execute --repeats 2 --max-output-tokens 512 --output runs/openai-luna-repeat2
```

每次运行固定16或32次计划调用，`workers=1`、`retries=0`。provider另有32次网络尝试上限。遇到HTTP、传输或不支持的响应错误，会停止后续网络请求；其余样本仍作为错误记录保留在评测分母中。已有目录不会被静默覆盖，也不会自动重跑以挑选更好输出。

## 成本预留与实际费用

[官方价格表](https://developers.openai.com/api/docs/pricing)在核验日列出Luna标准输入$0.10/百万token、输出$0.50/百万token，cache write价格随保留时长为$0.125或$0.25/百万token。预算没有假定缓存折扣：每次输入按UTF-8字节数加2048个包装余量估计token，全部按$0.25/百万预留，输出按配置上限预留。仅接受总估算不超过$0.05的实验。

**$0.05是本地保守预估门槛，不是服务商账户的账单硬上限。** 调用次数与输出token上限是明确限制；估算仍取决于token封装、服务商计费和未来价格。失败请求可能已消耗额度。本工具不会把未知收费算成零。

`cost_usd`仅在返回usage完整、明确无cache writes、返回模型标识仍为核验的gpt-6-luna且实际service tier为default时按该价格表估算；cache write保留时长不明、usage缺失、实际模型或tier改变时记为null。`known_cost_subtotal_usd`只是已知记录小计，必须同时看 `unknown_cost_records`，不能把它当最终账单。原始usage与response.model保留供审计。

## 结果与复现边界

成功执行后，输出目录包含 `run.json`、`suite.json`、SQLite journal、`budget-plan.json`、`smoke-summary.json`。请求与响应记录不包含Authorization头。脚本把本adapter源文件SHA256绑定到target revision；预算和真实响应同时留存。

官方模型页当前提供的是 `gpt-6-luna` 别名。记录远端返回的model字符串能够追踪观察到的服务标识，但不能证明服务器权重固定。两次调用也不是独立抽样的新业务情境。与本地Qwen对比时，应使用相同16条suite、评分协议和输入哈希，展示有限配对结果及证据不足状态，不能据此给出总体模型排名。

## 本次执行记录

已完成16次真实调用，15条严格合同通过；返回用量1350输入token、394输出token，价格估算$0.000332。完整收据在 `artifacts/openai-luna-smoke/`，详见[结果与边界](results.md)。后续复跑默认仍只做dry-run，不会因为已有密钥而自动消费。
