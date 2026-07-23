# 新旧工作流代码对比

本文对比 QueryForge 最初 MVP 与当前受治理分析工作流。“旧工作流”指
`docs/reproduction/vibe-coding-reproduce/` 所描述的固定 MVP，不是上游
Datus-Agent 的完整实现。

## 工作流形态

### 旧 MVP

```text
CLI
-> WorkflowRunner
-> SchemaLinkingNode
-> GenSqlNode
-> ExecuteSqlNode
-> OutputNode
```

所有非空问题都作为同一种 NL2SQL 请求处理：生成一条 SQLite `SELECT`，执行并返回结果。
任意节点失败即终止。

### 当前工作流

```text
CLI / REST-SSE / MCP / Gateway
-> AgentService
-> EntryRouterAgent
-> OrchestratorAgent
-> analysis -> candidate -> execution -> completion -> delivery
-> WorkflowRunner / ReflectiveWorkflow
```

SQL 内核仍是节点化流程：

```text
范围确定 -> 日期/Schema/Skill/指标上下文
-> 可选 Tool Loop 或并发候选
-> SQL 生成
-> Governance
-> DatabaseTool 执行
-> 反思 / 有界修复
-> 输出
```

五阶段是编排状态，不等于额外五次模型调用。默认简单请求仍为一次 SQL 生成加一次反思。

## 代码映射

| 关注点 | 旧 MVP | 当前实现 | 行为变化 |
| --- | --- | --- | --- |
| 入口 | `main.py` 直接创建 `WorkflowRunner` | `application/agent_service.py` | 所有传输层复用同一服务行为。 |
| 请求类型 | 只有隐式 NL2SQL | `orchestration/agents/entry_router.py` | 元数据与 SQL 审查无需执行模型 SQL。 |
| 编排 | `WorkflowRunner` 固定节点顺序 | `orchestration/orchestrator/` | 持久化阶段、artifact、门禁与交付。 |
| SQL 循环 | 单次生成、执行、输出 | `workflow/workflow.py:ReflectiveWorkflow` | 支持反思、修复、重生成、Tool Loop、候选选择。 |
| Schema 上下文 | SQLite 物理 Schema | `SchemaLinkingNode` + 语义层 + Subject Tree | 支持业务指标、Join Path、粒度和 fan-out 约束。 |
| 执行 | 只读校验后调用 connector | `ExecuteSqlNode -> DatabaseTool -> SQLiteConnector` | 语义 Join Guard、策略决策记录、执行时复核。 |
| 输出 | SQL、解释、结果行 | `OutputNode`、artifact、报告/SSE/MCP | 稳定响应之外可按需提供审计和交付。 |

## Router 的具体变化

### 旧实现

没有 Router 模块。CLI 的每个问题都会进入固定工作流，因此不存在任务分类、复杂度评估、
扩展路由或按任务类型跳过 SQL 执行。

### 当前实现

[`EntryRouterAgent`](../queryforge/orchestration/agents/entry_router.py) 是不依赖模型、
数据库、`WorkflowRunner` 或 SQL 工具的确定性组件，因此它本身不能生成或执行 SQL。

它完成三项工作：

1. 按 marker 分类：
   `ask_sql`、`sql_review`、`troubleshoot_sql`、`metadata_query`、
   `explain_result`、`build_report`、`unknown`。
2. 按跨表、指标、趋势比较、排名、多维分组、多条件过滤和问题长度计算
   `simple` / `complex` 复杂度。
3. 通过 `TaskRoute` 与 `register_pipeline()` 支持部署侧注册新任务类型。

`AgentService._run()` 根据复杂度配置执行策略：

```text
simple  -> 单候选，不启用 Tool Loop
complex -> 有界 Tool Loop + 至少两个并发候选
```

路由结果会写入 `routing_decision.json`，并出现在 `agent_team` 响应元数据中。当前对外
流程统一为：

| 任务类型 | 阶段流程 |
| --- | --- |
| `ask_sql`、`troubleshoot_sql`、`explain_result`、`build_report`、`unknown` | `analysis -> candidate -> execution -> completion -> delivery` |
| `sql_review` | `analysis -> candidate -> review -> completion -> delivery` |
| `metadata_query` | `analysis -> delivery` |

Router 不是 LLM Planner，而是低成本、可测试、可解释的准入与执行配置决策。

## Governance 的具体变化

### 旧实现

旧 MVP 的 `DatabaseTool` 在执行前做基础只读检查：空 SQL、非 `SELECT`、多语句和明显
DDL/DML 被拒绝。没有独立 Governance artifact，也没有表列白名单、危险函数、递归 CTE、
查询形状预算或候选阶段门禁。

### 当前实现

当前治理刻意分为两层：

```text
candidate 阶段
-> GovernanceAgent
-> SQLPolicyEngine.evaluate(sql)
-> governance_report artifact

execution 边界
-> ExecuteSqlNode
-> DatabaseTool.execute_sql(sql)
-> SQLPolicyEngine.evaluate(sql) 再次校验
-> SQLiteConnector(query_only)
```

[`GovernanceAgent`](../queryforge/orchestration/agents/governance.py) 在正式执行前：

- 要求存在 SQL candidate；
- 调用共享 SQLGlot 策略引擎；
- 写入完整 `governance_report`；
- 在策略拒绝或引擎不可用时阻断候选阶段；
- 对高 Join 数、无 WHERE/LIMIT、敏感列名产生非阻断风险告警。

[`SQLPolicyEngine`](../queryforge/domain/security/sql_policy.py) 当前强制：

- 单一、可解析的 SQLite Query AST；
- 只读根节点和禁止 AST 节点；
- 禁止递归 CTE；
- 危险函数黑名单；
- 表和列访问范围；
- 必须/最大 `LIMIT`；
- 最大表数、最大 Join 数与 CROSS JOIN 控制。

[`DatabaseTool`](../queryforge/infrastructure/tools/database_tool.py) 是不可绕过的正式执行
边界。`execute_sql()` 会再次执行同一策略，连接器还设置 `PRAGMA query_only = ON`。

双重校验的目的不同：候选阶段检查提供可审计的编排决策；执行阶段复核确保 preview、
资源接口或未来调用方即使错误绕过编排，也不能绕过策略。

## 状态与失败处理

| 方面 | 旧 MVP | 当前工作流 |
| --- | --- | --- |
| 状态 | 内存 `Context` | `Context` + 持久化 `TaskState` + artifact |
| 节点失败 | 直接停止 | 可在预算内修复；否则输出 `blocked` / `degraded` 证据 |
| SQL 错误 | 执行失败即结束 | 反思选择 `SUCCESS`、`FIX_SQL`、`REGENERATE` 或人工审查 |
| 策略失败 | 基础校验错误 | 命名 AST 规则、策略决策、Governance artifact、禁止执行 |
| 会话 | 无 | 显式 opt-in 结构化记忆，不保存结果行 |
| 交付 | CLI JSON | 共享 JSON、报告、SSE 进度、MCP 资源与工具 |

## 未改变的核心约束

- SQLite 仍是唯一正式执行后端。
- `WorkflowRunner`、节点契约和 `Context` 仍是 SQL 执行内核。
- 任何正式 SQL 仍必须经过 `DatabaseTool`。
- 默认简单路径仍不会启用 Tool Loop 和多候选开销。
- 旧 Python import 路径仍通过兼容 shim 保留。

## 推荐阅读顺序

1. `application/agent_service.py`：统一请求装配。
2. `orchestration/agents/entry_router.py`：路由与复杂度。
3. `orchestration/orchestrator/orchestrator.py`：五阶段生命周期与 artifact。
4. `workflow/workflow.py`：反思式 SQL 循环。
5. `orchestration/agents/governance.py`：执行前治理 artifact。
6. `domain/security/sql_policy.py`：AST 安全规则。
7. `infrastructure/tools/database_tool.py`：最终执行边界。
