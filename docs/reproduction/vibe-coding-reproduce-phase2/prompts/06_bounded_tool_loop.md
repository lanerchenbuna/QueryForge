# Prompt 06: 有界 Tool Loop——观察-行动-再规划

复制下面整段给 Codex 使用。

```text
继续上一轮。Conversation Memory 已经实现。

本轮目标：实现有界 Tool Loop，让 Agent 可以主动观察 Schema 和数据，再生成 SQL。这是从"单轮生成"到"观察-行动循环"的关键一步。

## 背景

当前流程是：一次性生成 SQL → 执行 → 反射 → 修复。模型没有机会先了解数据再决定怎么查。

Tool Loop 让模型可以：
1. 先看有哪些表
2. 深入了解某张表的字段
3. 预览某个字段的枚举值
4. 基于观察结果生成更准确的 SQL

## 本轮要做的事

### 1. 定义工具集（Tool Set）

只开放 4 个只读工具，严格控制范围：

| 工具名 | 功能 | 参数 | 返回 |
|-------|------|------|------|
| `list_tables` | 列出所有表名 | 无 | 表名列表和简要描述 |
| `describe_table` | 查看表结构 | table_name | 字段列表、类型、主键、外键 |
| `preview_distinct_values` | 预览字段枚举值 | table_name, column_name, limit=20 | 去重后的值列表 |
| `execute_sql_preview` | 预览查询结果 | sql, limit=20 | 带 LIMIT 的查询结果 |

安全约束：
- 所有工具都是只读的
- `execute_sql_preview` 自动加 LIMIT，最多 100 行
- 所有工具调用都经过 SQL 安全策略检查
- 有轮数限制（默认 5 轮）
- 有总耗时限制（默认 30 秒）

### 2. Tool Loop 节点

新增 ToolLoopNode（放在 queryforge/agent/node/tool_loop_node.py）：

职责：
- 调用 LLM，让模型选择工具和参数
- 执行工具调用
- 把观察结果反馈给模型
- 循环直到模型说"我有足够信息了，可以生成 SQL 了"
- 或者达到轮数限制

输入：Context（包含 question、schema、history 等）
输出：更新后的 Context（包含 tool loop 的观察结果，以及可能更新的 sql_context）

等等，Tool Loop 是在 GenSQL 之前还是之后？

答案：**在 GenSQL 之前**，作为"信息收集"阶段。模型先通过工具收集信息，然后再生成 SQL。

但也可以设计为"生成 → 观察 → 修正"的循环。本轮先做"信息收集"模式，简单且安全。

### 3. LLM 工具调用协议

定义结构化的工具调用格式（用 JSON，不用 Function Calling）：

模型输出 JSON：
```json
{
  "thought": "我需要先了解订单表的结构",
  "action": "describe_table",
  "params": {
    "table_name": "fact_watch_session"
  }
}
```

或者：
```json
{
  "thought": "信息足够了，可以生成 SQL 了",
  "action": "final_answer",
  "params": {
    "sql": "SELECT ...",
    "explanation": "..."
  }
}
```

为什么不用 Function Calling？
- 不是所有 Provider 都支持 Function Calling
- JSON 模式更通用，测试更简单
- 后续可以再增加 Function Calling 支持

### 4. 预算控制

硬限制：
- 最大轮数：5 轮（可配置）
- 最大行数：预览结果最多 100 行
- 最大耗时：30 秒（可配置）
- 工具白名单：只有上面 4 个工具

软限制：
- 如果模型反复调用同一个工具且没有新信息，提前终止
- 如果模型调用 final_answer，直接退出循环

### 5. Agent Team 集成

Tool Loop 在 Agent Team 架构中的位置：

```
analysis lifecycle (ProductAnalyst, Knowledge, SchemaArchitect)
  → Tool Loop (可选，信息收集)
  → GenSqlNode
  → candidate lifecycle (SQLDeveloper, Governance)
  → execute_sql
  → ...
```

放在 SchemaArchitect 之后、GenSQL 之前。由 SchemaArchitectAgent 判断是否需要 Tool Loop（或者由配置控制）。

配置项：
- `tool_loop_enabled: bool`，默认 false
- `tool_loop_max_rounds: int`，默认 5
- `tool_loop_timeout_seconds: int`，默认 30

为什么默认关闭？
- 增加 LLM 调用次数，成本更高
- 简单查询不需要 Tool Loop
- 避免性能退化

### 6. 观测和审计

每次 Tool Loop 都要记录：
- 每轮的 action 和 params
- 每轮的观察结果（截断后）
- 总轮数
- 退出原因（final_answer / 轮数耗尽 / 超时 / 错误）

记录到 Context 的 tool_loop_history 字段和 artifact 中。

### 7. 测试策略

测试 Tool Loop 需要 LLM 调用，所以用 mock LLM：
- 模拟模型选择 list_tables → describe_table → final_answer 的流程
- 模拟模型超过最大轮数
- 模拟模型调用不存在的工具
- 模拟模型调用 final_answer 退出

## 限制

- 只开放 4 个只读工具
- 严格的轮数、行数、时间限制
- 所有工具调用都经过 SQL 安全策略检查
- 默认关闭，需要显式开启
- 不改变 GenSQL 之后的流程
- execute_sql_preview 不是正式执行，正式执行仍走 ExecuteSqlNode

## 验收标准

1. Tool Loop 可以正常工作：模型调用工具 → 观察 → 生成 SQL
2. 4 个工具都能正常调用
3. 轮数限制生效：超过最大轮数后终止
4. 行数限制生效：预览结果不超过限制
5. 超时限制生效
6. 工具调用都经过安全策略检查
7. 默认关闭时，行为和以前完全一样
8. 开启后，最终 SQL 仍然经过 Governance 和 ExecuteSqlNode 的双重校验
9. 有完整的工具调用历史记录
10. 新增测试覆盖：正常流程、超轮数、超行数、超时、非法工具、安全策略拦截

## 输出要求

1. 先读取现有代码，确认节点结构和 LLM 调用方式
2. 给出 Tool Loop 设计和工具协议
3. 实现代码
4. 编写测试
5. 运行测试，确保全量通过
```
