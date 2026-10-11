# 可运行示例

这些示例使用仓库自带数据和临时工作区，展示数据接入、受治理查询、业务分析与恢复。
全部采用确定性脚本模型，不需要 API key，也不调用外部模型。

## 运行

在仓库根目录安装依赖后执行：

```bash
pip install -e ".[all,duckdb]"
make demo
```

也可以单独运行：

```bash
.venv/bin/python docs/demo/run_demo_a.py
```

| 脚本 | 使用场景 |
| --- | --- |
| `run_demo_a.py` | CSV 上传、质量拒绝、修正发布与查询 |
| `run_demo_b.py` | 识别 SQL 的业务口径错误，检查正确结果和证据 |
| `run_demo_c.py` | 澄清、期间趋势、下钻与贡献分析 |
| `run_demo_d.py` | CLI/REST/MCP 一致性、访问限制、恢复与取消 |
| `run_demo_e.py` | HTTP 上传发布、SQL 修复循环、数据驱动的变化诊断 |

脚本打印 SQL、结果与证据；退出码 `0` 表示运行成功，非零退出会指出失败步骤。
这些示例帮助理解工程链路，真实模型评测见[评测指南](../nl2sql_evaluation.md)。

## 与 Studio 联调

```bash
.venv/bin/python docs/demo/serve_offline_api.py
```

在另一个终端启动 Studio，详见[Studio 使用指南](../studio.md)。
