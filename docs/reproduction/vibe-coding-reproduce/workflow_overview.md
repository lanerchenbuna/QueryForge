# Workflow 图解：从 Datus-Agent 到 MVP

本文用于帮助使用者理解原项目 workflow，并把它映射到 vibe coding 复现版本。

## 原项目核心链路

原项目有多个入口，但最终都会进入 Agent / Workflow 层。

```text
CLI / Web / API / MCP / Gateway
        |
        v
Agent
        |
        v
WorkflowRunner
        |
        v
generate_workflow(plan_type)
        |
        v
Workflow(context + node_order + nodes)
        |
        v
+------------------+      +-----------------+      +------------------+      +-------------+
| SchemaLinkingNode| ---> | GenSQLAgenticNode| ---> | ExecuteSQLNode   | ---> | OutputNode  |
+------------------+      +-----------------+      +------------------+      +-------------+
        |                         |                         |                       |
        v                         v                         v                       v
 table_schemas              SQLContext                  execution_result        final_output
 table_values               sql / explanation           rows / columns          answer package
```

原项目 fixed workflow：

```text
schema_linking -> gen_sql -> execute_sql -> output
```

原项目增强 workflow 示例：

```text
reflection:
schema_linking -> gen_sql -> execute_sql -> reflect -> output

metric_to_sql:
schema_linking -> search_metrics -> date_parser -> gen_sql -> execute_sql -> output

chat_agentic:
chat -> execute_sql -> output
```

## MVP 复现链路

MVP 只复现 fixed workflow，不复现增强链路。

```text
User
 |
 | natural language question
 v
+-------------------+
| main.py CLI       |
| parse args        |
+-------------------+
 |
 v
+-------------------+
| WorkflowRunner    |
| build SqlTask     |
| create connector  |
| assemble nodes    |
+-------------------+
 |
 v
+-------------------+
| Workflow          |
| owns Context      |
| runs nodes        |
+-------------------+
 |
 v
+----------------------+        context.relevant_tables
| SchemaLinkingNode    | --------------------------------+
| list all tables      |                                 |
| describe columns     |                                 |
+----------------------+                                 |
 |                                                      |
 v                                                      |
+----------------------+        context.sql_contexts     |
| GenSqlNode           | <-------------------------------+
| build LLM prompt     |
| parse JSON SQL       |
+----------------------+
 |
 v
+----------------------+        context.execution_result
| ExecuteSqlNode       |
| run read-only SQL    |
+----------------------+
 |
 v
+----------------------+        context.final_output
| OutputNode           |
| format result        |
+----------------------+
 |
 v
CLI output
```

## Context 数据流

```text
初始 Context
  sql_task = { question, database_path }

SchemaLinkingNode 后
  relevant_tables = [
    { table_name, columns: [{ name, type, nullable, primary_key }] }
  ]

GenSqlNode 后
  sql_contexts = [
    { sql, explanation, tables_used }
  ]

ExecuteSqlNode 后
  execution_result = {
    columns,
    rows,
    row_count
  }

OutputNode 后
  final_output = {
    question,
    relevant_tables,
    sql,
    explanation,
    tables_used,
    columns,
    rows,
    row_count
  }
```

## 原项目能力与 MVP 取舍

| 原项目能力 | MVP 处理方式 | 原因 |
| --- | --- | --- |
| 多端入口 | 只保留 CLI | 最短路径验证主功能 |
| WorkflowRunner / Workflow | 保留简化版 | 这是项目核心结构 |
| Node 工厂与大量节点 | 保留 4 个核心节点 | fixed workflow 足够复现主链路 |
| Schema Metadata RAG | 直接读取 SQLite schema | 避免向量库和索引构建 |
| GenSQLAgenticNode tool loop | 简化为一次 LLM JSON SQL 生成 | 降低实现难度 |
| DBFuncTool | 简化为 DatabaseTool | 保留数据库工具思想 |
| 多数据库 connector | 只保留 SQLiteConnector | 零服务依赖 |
| Reflect / Fix / Reasoning | 省略 | 准确率增强，不是主链路必需 |
| MCP / Gateway / API | 省略 | 入口增强，不影响核心算法 |

## 给 Codex 的实现重点

实现时不要追求企业级完整度，应优先保证下面四件事：

1. Context 字段在节点之间清晰传递。
2. LLM prompt 包含足够 schema 信息，且要求 JSON 输出。
3. SQL 执行层默认只读。
4. CLI 能端到端跑通 sample database。

一旦这四件事成立，就已经复现了 Datus-Agent 最有代表性的主功能骨架。
