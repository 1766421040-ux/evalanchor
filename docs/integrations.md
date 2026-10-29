# Inspect AI / Promptfoo 双向集成

本项目已真实执行两个上游框架，并回导它们实际产生的日志。执行提供者均为合成 fixture，不调用真实模型，不能用其中的 62.5% 评价任何模型。

| 路径 | 实际验证版本 | 验收 |
|---|---|---|
| EvalAnchor → Inspect Task → 原生 JSON → EvalAnchor | inspect-ai 0.3.286 | 64 case × 2 repetitions，逐条原始输出与本地重评分一致 |
| Inspect 官方序列化 `.eval` → EvalAnchor | inspect-ai 0.3.286 | 同一真实运行日志转为 `.eval` 后，128 条全部回读一致 |
| EvalAnchor → Promptfoo config + Python provider → 原生 JSON → EvalAnchor | promptfoo 0.125.1 | 64 case × 2 repetitions，逐条原始输出与本地重评分一致 |

版本、原生日志路径和 SHA-256 在 [receipt.json](../examples/integrations/verified/receipt.json)。`tests/test_adapters.py` 会回读这些真实证据并校验哈希；单元测试中另外构造的日志对象明确只是异常路径测试 doubles。安装允许范围不等于整个范围都验证过。

## 一次完整的本地验收

在项目根目录，以 Python 3.12 和 Node.js 环境运行。下面各代码块使用同一个已激活的终端；`EVALANCHOR_PROJECT_DIR` 与 `PROMPTFOO_BIN` 保存绝对路径，子目录运行不会改变后续命令的起点：

```sh
export EVALANCHOR_PROJECT_DIR="$PWD"
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[test]'
python -m pip install inspect-ai==0.3.286
npm install --prefix .cache/promptfoo promptfoo@0.125.1 --no-audit --no-fund
export PROMPTFOO_BIN="$EVALANCHOR_PROJECT_DIR/.cache/promptfoo/node_modules/.bin/promptfoo"
export PROMPTFOO_PYTHON="$EVALANCHOR_PROJECT_DIR/.venv/bin/python"
export PROMPTFOO_DISABLE_TELEMETRY=1
evalanchor demo --output runs/integration-demo
python scripts/verify_integrations.py --output .cache/integration-check --promptfoo-bin "$PROMPTFOO_BIN"
python -m pytest tests/test_adapters.py -q
```

`runs/integration-demo` 必须是新目录；若已存在，请在本页所有命令中一致替换为另一个新目录。依赖安装需要网络；安装后验证程序只使用本地 fixture。脚本不自动下载 npm 包，不调用模型 API。Promptfoo 遥测在验证进程中关闭。Inspect mock token usage 显式设为零，避免仅为 mock 下载 tokenizer；零是 fixture 标记，不能当成本或 token 测量。

Inspect 0.3.286 的 macOS 默认数据目录位于用户 Library；本地验收脚本只把 `appdirs` 的 data/cache 路径重定向到输出目录 `.runtime`，并关闭控制服务与实时日志。这是验证环境的文件存储适配，不修改上游执行器、模型接口或评分代码，收据也保存了该说明。

## 导出 Inspect 任务并回导

```sh
evalanchor export inspect --suite runs/integration-demo/suite.json --output .cache/inspect-export --repeats 2
```

上述套件文件由 `evalanchor demo` 生成。导出包含 `task.py`、`suite.json` 和运行说明。导出的 `task.py` 使用 `build_inspect_task()` 构造真正的 `inspect_ai.Task`，通过 `generate()` 生成，再调用 `@scorer` 包装的本地契约评分器。

从项目根目录启动子 shell，在导出目录运行后自动返回项目根目录：

```sh
(
  cd "$EVALANCHOR_PROJECT_DIR/.cache/inspect-export" || exit
  inspect eval task.py --model mockllm/model --log-dir logs --log-format json
)
```

这个默认命令只验证传输流程；mock 默认字符串通常不满足业务契约。要跑真实模型，换成自己已经配置的 Inspect model。`scripts/verify_integrations.py` 使用的是预先定义的 baseline fixture，因此另有确定的 62.5% 验收预期。

回导文件：

```sh
evalanchor import inspect .cache/inspect-export/logs/ACTUAL_LOG.json --suite runs/integration-demo/suite.json --id inspect-baseline --output .cache/inspect-baseline.json
```

把 `ACTUAL_LOG.json` 换成真正生成的文件名。`.eval` 同样支持，但需要安装 inspect-ai。纯 JSON 导入只依赖 EvalAnchor 核心。Inspect 的 epoch 是 1-based，导入转换为 0-based repeat；导出重复次数与日志网格必须一致。

## 导出 Promptfoo 配置并回导

```sh
evalanchor export promptfoo --suite runs/integration-demo/suite.json --output .cache/promptfoo-export --repeats 2
```

导出为标准 `promptfooconfig.json`，可直接交给 Promptfoo：

```sh
(
  cd "$EVALANCHOR_PROJECT_DIR/.cache/promptfoo-export" || exit
  "$PROMPTFOO_BIN" eval --config promptfooconfig.json --output results.json --no-cache
)
evalanchor import promptfoo .cache/promptfoo-export/results.json --suite runs/integration-demo/suite.json --id promptfoo-baseline --output .cache/promptfoo-baseline.json
```

子 shell 的 cwd 与已验证脚本一致，因此 `file://fixture_provider.py`、fixture文件和输出路径都相对于同一个导出目录。`PROMPTFOO_BIN` 是本地安装的绝对路径，无需全局安装 Promptfoo。默认 provider 是隔离的 Python fixture；它只根据 messages 的摘要查找预存输出，没有读取 suite expected。要运行真实模型，修改 config 的 `providers`，保留 `prompts`、`vars.messages_json` 和 `metadata.evalanchor`。

Promptfoo 的实际导出会把裸 64 位十六进制摘要误判为敏感值而替换为 `[REDACTED]`。本项目保留上游敏感值清洗，将非秘密 SHA-256 摘要显式表示为 `{algorithm: "sha256", bytes: [32 个整数]}`；导入严格校验算法、长度和字节范围，再恢复摘要。原始输出若被上游清洗改变，回导只会评估日志实际保留的值，不能恢复不存在的原始内容。

导出已显式展开 repeats，不能再给 Promptfoo 添加 `--repeat`。多个 provider 的日志必须提供 `--provider-id`，且所选 provider 必须有完整 case/repeat 网格。不是按行号、模糊 ID 或相似文本猜测匹配。

## 导入的强约束

1. 套件、case、原始输入、重复次数与本次导出完全一致。case hash 包含评分器与 expected，修改私有答案同样会被拒绝。
2. 一个 case/repeat 恰好一行。未知 ID、重复行、漏行、额外重复、含糊 provider 选择直接报错，不丢弃坏行后计算漂亮均值。
3. 只接收原始字符串输出；不从 Markdown 提取 JSON、不自动修补类型、不把对象重新序列化成“模型原文”。
4. 上游原分数保存在 `record.native`，有效分数由当前本地 scorer 重算。上游 assertion failure 不被误写成网络/provider error。
5. provider 错误保留一行并记零分；Inspect 未完成或 error 状态整次日志拒绝进入 complete 证据。
6. 导入结果带完整 `suite_definition` 和 sampling 声明，经 `audit_run` 重新核实；case 输入 hash 和原始日志文件 hash 都保留。

Inspect 还校验执行 transcript 的输入前缀，并只接受一次 assistant 生成；修改 Solver 添加隐含提示或多轮流程不能沿用原导出合同。Inspect 的 latency 取样本 `total_time`，包含任务工作；Promptfoo 的 latency 来自原生 `latencyMs`。两者不是统一的纯模型推理耗时，不能直接据此比较部署速度。缺失时的零值也不是可靠的延迟测量。

这保证的是受支持导出协议下的**记录一致性**。哈希不是数据真实性或模型身份认证，不防止一个人同时伪造所有字段。适配器核对上游记录的输入，不证明供应商内部没有再加工；模型 revision 未声明时仍为 unknown。测试只验证文本、单次生成任务；多模态内容、任意 Agent 轨迹、工具执行副作用和任意外部日志不在 v0.1 的适配范围。

## 兼容性与来源

公共接口与协议依据：[Inspect Sample](https://github.com/UKGovernmentBEIS/inspect_ai/blob/main/src/inspect_ai/dataset/_dataset.py)、[Scorer](https://github.com/UKGovernmentBEIS/inspect_ai/blob/main/src/inspect_ai/scorer/_scorer.py)、[日志 API](https://inspect.aisi.org.uk/eval-logs.html)、[Promptfoo Python provider](https://www.promptfoo.dev/docs/providers/python/)、[test metadata](https://www.promptfoo.dev/docs/configuration/test-cases/)、[JSON 导出](https://www.promptfoo.dev/docs/configuration/outputs/)。适配器通过这些接口互操作，没有 vendoring 或改名复制上游实现。

lm-eval、LightEval、OpenCompass、HELM 的源码调研与不接入原因见 [frameworks.md](../research/frameworks.md)。它们属于已调研候选，不属于已实现支持矩阵。
