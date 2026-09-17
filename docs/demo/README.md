# QueryForge 端到端验收 Demo（步骤 17）

这五个脚本把「系统真的能做什么」写成**可复现、可断言**的演示：每一步都打印真实证据，
并且在断言不成立时**以非零退出**——所以它们既是给用户看的文档，也是 CI 里的验收测试
（`tests/test_demo_scripts.py`）。

全部脚本**离线、确定性、零模型调用**：只用仓库自带的样例数据与自己生成的临时工作区，
不会写进仓库，不需要 API key，不访问网络。

## 运行方式

```bash
# 全部（约 30 秒）
make demo            # 等价于 python docs/demo/run_all.py

# 单个
.venv/bin/python docs/demo/run_demo_a.py        # 上传 → 质量拦截 → 修正 → 发布 → 查询
.venv/bin/python docs/demo/run_demo_b.py        # 合法但业务错误的 SQL → 语义校验定位 → 正确结果与证据
.venv/bin/python docs/demo/run_demo_c.py        # 澄清 → 质量 → 趋势 → 下钻 → 贡献 → 证据化回答
.venv/bin/python docs/demo/run_demo_d.py        # 跨传输一致 / 部署契约拒绝 / 崩溃恢复与取消
.venv/bin/python docs/demo/run_demo_e.py        # 真实 HTTP 上传闭环 / 真实修复循环 / 数据驱动的归因
```

退出码：`0` = 所有断言的声明都成立；`1` = 有声明不成立（脚本会指出是哪一条）。

## 每个 Demo 证明什么

| Demo | 演示的故事 | 关键断言（节选） | 对应验收项 |
|---|---|---|---|
| A | 上传的 CSV 决定结果；坏文件被拦下并留下可诊断证据；坏批次不污染已发布版本 | 坏批次退出 1 且**不发布任何行**、watermark 不前进、被拒行在回滚后仍可查；修正后 3 行发布，答案 = 1.5 小时；再加一行答案变 2 小时；再次失败后旧版本仍答 2 小时 | 17-E2E1 |
| B | 一条「能跑但答え错了」的 SQL 被语义校验抓住；受治理编译给出正确 SQL 并与独立 SQLite 口径逐桶一致；答案的每条结论都锚定真实证据 | 手写 SQL 是受治理值的 **3600 倍**（秒 vs 小时）；校验器给出 `metric_expression` 违规；编译 SQL 带 `/3600.0` 且与独立口径完全一致；每条 finding 的 `evidence_ids` 都存在于本次运行 | 16-T1、17-I1 |
| C | 歧义先澄清、质量检查作为计划步骤、期间比较按时间排序、下钻可对账、空窗口不装成 0 | `needs_clarification` 且不跑 SQL；`ORDER BY dim_date.month_number`（不是按月份名排序）；12 个月数值逐个等于独立 SQL；下钻 `buckets + others == 合计`；Q1 1990 → `partial` 且不产出答案 | 13-B1、16-N1 |
| D | 同一问题在 CLI / REST / MCP 得到同一个受治理答案；部署契约该拒就拒；崩溃可恢复、取消不可复活 | CLI 与 REST 的合计**完全相等**；MCP 暴露 `ask_sql` 等同一套工具；被策略屏蔽的列被拒（`column_scope`）；allowlist 外的库在 `/ask` 与 `/analyze` 都返回 400；崩溃后 resume **复用全部已提交步骤、工具调用 0 次**；流式取消的运行不可 resume | 17-I1、17-S1、15-N1/C2 |
| E | 经**真实 HTTP 处理器**的上传→质量拒绝→发布→查询；可执行但错误的 SQL 被**真实 fix 节点**修回受治理定义；归因**随数据变化** | 上传 3 行→计数 3；被拒上传后旧版本仍可查（17-E2E1）；错误语句返回 99（`valid=0`）→ 修复后返回 30（`valid=1`）且 `run_summary` 含 `fix` 节点；`paid=20/40/60` → 总变化 −20/0/+20、方向 decrease/flat/increase、残差 0；已发布域不可被调用方路径改写、已吊销域被拒 | M0、M1、M2–M3 |

## 离线确定性与 live 模式

- 本目录的脚本**全部是离线确定性**模式：不配置任何模型凭证，不产生模型调用与费用；
  Demo D 的 MCP 检查使用内存中的 FastMCP 替身（与 `tests/test_service_api_gateway_mcp.py` 同法），
  验证的是「同一服务被注册为工具」，不是真实模型行为。
- **live 模式**（真实模型）不在本目录内脚本化，因为它不可离线复现、且每次成本不同：
  见 `docs/nl2sql_evaluation.md` 与 `.github/workflows/model-eval.yml`（tier 3，需凭证，结果单独报告）。
- 因此：**本目录的 Demo 证明的是工程链路（治理、执行、证据、恢复），不是模型的 NL2SQL 准确率。**

## Demo 过程中发现并已修复的产品缺陷

写 Demo 的价值就在于此——下面每一条都是「所有单测都是绿的，但真实使用时会出错」：

1. **跨实体分组不编译 JOIN，改用原始预览冒充指标答案**（Demo C 的前身问题）：规划器给编译器传空
   join path，于是「watch hours by anime format」降级为 `SELECT * FROM fact_watch_session LIMIT 5`
   并算作 `metric_value` 证据。已修复：解析受治理 join path，无法表达则显式失败。
2. **时间维度接到了错误的列**：`watch_hours` 的时间字段是观看日期，但编译器走「分集上线日期」的
   join path，于是「watch hours by month」回答的是**分集上线月**的量。已修复（按指标自身
   `time_field` 直连日历表）。
3. **时间序列按月份名排序**：`ORDER BY month_name` 让「上月对比」选到字母序最后两个月；
   已修复（有 `month_number` 时按其排序）。
4. **未声明的分组字段被静默丢弃**：问「by brand_name」原本返回全体总计并报成功；已修复为澄清。
5. **空窗口返回 NULL 仍报成功**：Q1 1990 现在按 `data_absent` 收为 `partial`。
6. **坏批次回滚会丢掉被拒行的证据**：现在回滚后重放 quarantine 记录，运维能看到是哪几行被拒。

## 已知局限（Demo 里也如实展示）

- **贡献分解**：受治理编译器目前无法表达「按类别做两期贡献分解」，`contribution` 模板会诚实地
  收敛为期间比较（Demo C 的 C5 断言「多维度时不得产出假的期间比较」）。
- **语义校验器不检测手写 fan-out**：`ON 1=1` 这类乘法不会被 `SemanticSQLValidator` 单独发现
  （它检测的是粒度与请求维度不一致）；真正的防线是**编译期拒绝不安全的 join path**
  （Demo B 的 B5 三条断言把这点讲清楚）。
- **月度聚合跨年**：没有指定年份的「month over month」会把所有年份的同月加总，这是当前实现的
  确定行为，Demo C 因此显式用「in 2024」限定窗口。

## 只有一份 Demo 实现

仓库里曾经有两份 step-17 Demo：本目录的脚本，以及 `scripts/demo_data_agent.py`（由
`tests/test_step17_demos.py` 覆盖）。现已整合：那份脚本的三个场景（真实 HTTP 上传闭环、
真实工作流修复循环、数据驱动的增长归因）连同它的第四条安全断言（已发布域不可被改写）
一起搬进本目录，成为 **Demo E**：

- 场景代码：`docs/demo/scenarios_api_and_repair.py`（原 `scripts/demo_data_agent.py`）
- 叙事与断言：`docs/demo/run_demo_e.py`
- 离线起一个真实 API（脚本模型）供 Studio 联调：`docs/demo/serve_offline_api.py`
- `scripts/demo_data_agent.py` 与 `tests/test_step17_demos.py` 已删除，tier-2 门禁清单同步改为
  `tests.test_demo_scripts`。

因此 `make demo` 现在是 step-17 Demo 的唯一入口。
