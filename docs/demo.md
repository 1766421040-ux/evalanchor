# 五分钟演示

1. 运行 `PYTHONPATH=src python -m evalanchor demo --output runs/demo-new`，打开生成的index.html。
2. 指出candidate分数从62.5%升到87.5%，结论却是FAIL。
3. 进入“逐条证据”，选择“仅退化”。16条记录其实来自8个案例、2个情境组，每题2次；避免将重复次数说成独立样本。
4. 打开`conflict_authorization`：比较冲突证据下应拒答与候选擅自回答的raw。再打开`transfer_preview`：检查工具参数中的预览要求。这里只验证JSON合同，不真的转账或执行工具。
5. 切换baseline→fixed：显示100%仍为INCONCLUSIVE，解释有限手写套件与总体推断的区别。
6. 打开“协议与来源”，查看采样声明、评分版本、成本未知、judge无效输出和位置分歧。
7. 展示`examples/integrations/verified/receipt.json`：Inspect与Promptfoo都实际运行并被严格回导；这证明集成，不是模型质量。

## 三条最有用的命令

```bash
# 验证保存的raw与所有评分一致
PYTHONPATH=src python -m evalanchor validate runs/demo-new/candidate/run.json

# 三态退出码便于CI接入；此例预期退出1
PYTHONPATH=src python -m evalanchor compare runs/demo-new/baseline/run.json runs/demo-new/candidate/run.json \
  --output runs/demo-new/recheck.json

# 导出真正可被上游运行的任务
PYTHONPATH=src python -m evalanchor export inspect --suite runs/demo-new/suite.json --output runs/inspect-export
```

## 面试时怎么讲

开场：项目解决“模型升级证据是否可信”，场景是平均分改善但关键业务合同退化。选择现有执行器，再在结果之间建立严格可比性和可追溯判断。

技术重点：数据契约与标签隔离；原始输出重评分；SQLite事务恢复与单写者；framework adapter的输入/ID/grid校验；组等权与统计假设；未知费用及错误分母；HTML输出防注入。

一个真实调试案例：Promptfoo默认清洗把hex摘要当疑似secret改写，导致回导失败。修复采用带算法标识的摘要字节数组，并保留原有清洗；随后真实执行128条核验往返。另一个是审查发现修改score可绕过只检查范围的validator，改为绑定套件并强制复算。

边界：没有证明新算法、通用模型优越性或生产多租户能力；严格引用字段不等于语义蕴含；合成案例数不等于独立用户数。将已完成测试、真实框架集成、真实模型推理分别陈述。
