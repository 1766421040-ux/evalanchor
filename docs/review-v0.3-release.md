# v0.3 发行与复现审查

这次审查先复核现有发布，再用可失败的测试检查发行路径。原有 v0.2 的 17 份恢复文件校验通过，完整离线核验通过；发布、恢复、账本核验和模型来源相关的 72 项测试通过。原始实验、冻结执行器和历史评分没有改写。

## 发现与修复

**源码发行包与完整 ZIP 的隐私过滤不一致。** `MANIFEST.in` 原先递归收录 JSON、Python 和 Markdown，但没有完整覆盖 ZIP 的凭据文件名规则。在只含虚构标记的临时工程中，标准 `python -m build --sdist` 实际收录了 `artifacts/access_token.json`、`artifacts/api_keys/customer.json`、`scripts/private_key.py`、`docs/private-key.md` 和 `artifacts/keys/session.json`。这证明本地再次构建时存在误发布风险；本次没有发现或宣称历史公开包含真实密钥。

v0.3 将文件名和目录规则集中到 `release_policy.py`。完整 ZIP、setuptools 源码清单及 wheel 模块/模板选择使用同一规则，排除凭据目录、私钥、令牌、运行数据库、模型权重和缓存。发行构建拒绝需要跟随符号链接的公开源文件；复用的 `build/` 若残留被排除文件，构建失败并要求使用干净目录，不静默带入 wheel。该规则检查路径，不宣称识别藏在任意普通文件名中的密钥，发布前仍须审查要公开的源代码和证据。

**发行版本分散写死。** ZIP 前缀、CI wheel 路径和版本断言原来分别写死 `0.2.0`。现在 ZIP 与 CI 读取 `pyproject.toml` 的项目版本，安装验证同时核对包内版本和发行元数据。旧证据清单中的 `release_version: 0.2.0` 保留，用来标识历史证据，不改为新软件版本。

**CI 缺少源码发行包的独立复核。** 除原有 wheel 安装和完整 ZIP 核验，新流程还解压 sdist，验证其原始证据，并在 sdist 内重新运行真实构建回归测试。默认 `python -m build` 从生成的 sdist 构建 wheel，因此也验证自定义构建规则随源码发行包完整交付。

## 验证方式

```bash
python -m pip install -e '.[test,build]'
python -m pytest tests/test_package_release.py tests/test_evidence_bundle.py tests/test_verify_v2.py -q
python scripts/restore_evidence.py --verify-only
python scripts/verify_release.py
python -m build
python scripts/package_release.py
```

新增回归测试在临时工程中放入合成凭据标记，实际构建 sdist 和由该 sdist 生成的 wheel，检查二者均不含标记；同时验证公开模块的符号链接会被拒绝、旧构建目录中的凭据模块不会混入 wheel。测试只处理虚构标记，不读取真实密钥、不调用模型。

发布时还需针对最终提交重新运行完整测试、远端 CI 和干净克隆恢复；本文件记录修复机制，不替代最终发布回执。
