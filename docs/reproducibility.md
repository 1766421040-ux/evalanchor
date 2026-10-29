# 0.2 交付、恢复和离线复核

所有命令在 macOS / Linux、Python 3.12+ 环境中从项目根目录运行。实验审核不需要 API 密钥、模型权重、SQLite 账本或 `calls/` 缓存；完整请求、响应和工具执行结果已在 `experiment.json` 内。

Python 3.12 改变了浮点求和实现；较早版本重放已保存的轨迹总费用可能出现末位差异。因此 v0.2 的包安装、严格证据复核与 CI 统一要求 Python 3.12+。冻结执行器、原始记录和精确比较保持原样；v0.1 当时的兼容范围仅作为历史说明保留。

## 从 Git 仓库到可复核实验

```bash
python3 scripts/restore_evidence.py
PYTHONPATH=src python3 scripts/verify_release.py
PYTHONPATH=src python3 -m evalanchor arena serve --workspace experiments --port 8780
```

恢复器是 macOS/Linux 的标准库程序。只接受清单中的 `experiments/<id>/experiment.json`、`budget-receipt.json`、最终 campaign 预算和指定的 v2 HTML 报告，不接受任意目标路径。它核验压缩对象 SHA256/大小，流式解压，限制单文件及总大小，再核验原始 SHA256/大小并原子创建目的文件。遇到符号链接、目录穿越或内容不同的已存在文件时停止；不会替你删除、覆盖或修复证据。内容相同的文件直接核验后跳过。

原始大 JSON 的恢复路径和哈希见 [公开证据清单](../evidence/manifest.json)；这些明文路径在 Git 中忽略，因此使用恢复后的本地文件，不把它们当作 GitHub 网页链接。

`python scripts/restore_evidence.py --verify-only` 只检查已经恢复的明文文件，不创建或修改任何文件。SHA256 是清单一致性检查，不构成签名或生成过程真实性证明。

## 三层核验

1. `scripts/verify_release.py --v1-only` 保留历史框架日志哈希、严格输出重评分、两组演示对比、Qwen/Luna 静态结果核验。这个选项不表示整个 0.2 发布通过。
2. `scripts/verify_v2.py` 默认要求全部发布实验。每个实验先独立重放工具与业务状态，再调用分析器重新评分和汇总，逐字段核对内嵌 `analysis` 与独立 `analysis.json`。主测试与资源敏感性测试还必须匹配冻结的任务、配置、提示、执行器和协议。
3. 同一脚本从公开 `budget-receipt.json` 重建每个请求的状态链和整数纳美元总额，核对每一步请求/响应摘要、响应 ID、结算费用及整个 campaign 的历史前缀。计费未知和孤立的收费尝试继续保留，不能悄悄按零费用处理。

```bash
# 开发期间只核对一个现成实验。输出明确标为子集，release_verified=false。
python scripts/verify_v2.py --experiment pilot-v2

# 最终发布核验；缺少任何必需证据即失败。
python scripts/verify_release.py
```

预算使用逐请求向上取整的整数纳美元。其总和可能略大于把原始浮点估价直接求和的值；核验按每笔结算金额逐一检查，不能把正常舍入差异当作漏账，也不能用大容差掩盖真实差额。费用是公开费率下的用量估算，未核验服务商账单。

## 维护压缩证据

原始文件先完成实验和分析核验，再建立压缩传输清单：

```bash
python scripts/build_evidence_bundle.py

# 如果单文件工作台也很大，可以一并压缩。
python scripts/build_evidence_bundle.py --report artifacts/orderops-v2/index.html
```

该脚本仅读取白名单公开证据。压缩对象以自身 SHA256 命名，manifest 同时记录原始路径、原始/压缩字节数及各自 SHA256。新增结果后应重新生成清单；不要手工改清单以掩盖结果变化。明文实验及完整预算流水通过压缩对象进入 Git，`analysis.json`、小型成本汇总和冻结协议可直接浏览；冗余调用缓存和数据库不进入发布包。

## wheel、源码包与完整交付 ZIP

```bash
python -m pip install -e '.[test,build]'
python scripts/restore_evidence.py
python -m pytest -q
python scripts/verify_release.py
python -m build
python scripts/package_release.py
```

wheel 仅安装库、CLI 和两个 HTML 模板。源码发行包和完整 ZIP 包含文档、测试及公开明文证据；完整 ZIP 另提供 `RELEASE_MANIFEST.json` 的逐文件校验清单。源码 ZIP 不递归已有 `dist/`，也不包括嵌套 ZIP、压缩传输副本、`calls/`、SQLite、虚拟环境、模型权重或密钥。已有同名 ZIP 不会被覆盖。

本版本的输出名称分别为 `dist/evalanchor-0.2.0-py3-none-any.whl`、`dist/evalanchor-0.2.0.tar.gz` 和 `dist/evalanchor-0.2.0.zip`。wheel 是安装包；后两者是不同格式的源码交付，解压后均从 `evalanchor-0.2.0/` 根目录复核。

完整 ZIP 已包含明文证据和恢复清单，因此恢复命令可以直接检查已存在的正确文件，不需要重复保存压缩对象。源代码归档中的命令仍以归档根目录为起点。

CI 会先恢复证据，再进行单元测试与离线复核；构建工作在新的安装环境检查 wheel 和完整 ZIP 的可用性。离线核验和测试不发送模型请求。Inspect / Promptfoo 集成 CI 只运行本地合成 provider，结果不能当作模型表现。复现框架夹具所需的依赖下载属于安装步骤，与模型调用分开。

若要重新做真正的模型实验，请使用新目录、明确预算和共享账本；那是新实验，不是本节的离线复核。API 模型别名与服务端行为可能变化，不能保证新调用逐字复现历史生成。
