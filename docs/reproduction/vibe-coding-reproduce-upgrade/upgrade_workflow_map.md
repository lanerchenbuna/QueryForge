# 进阶 Workflow 映射图

## 基础版

```text
User
  -> CLI
  -> WorkflowRunner
  -> Workflow
  -> SchemaLinkingNode
  -> GenSqlNode
  -> ExecuteSqlNode
  -> OutputNode
```

## 进阶版目标

```text
User
  -> CLI / API / MCP / Gateway
  -> WorkflowRunner
  -> optional PlanModeNode
  -> optional DateParserNode
  -> SchemaLinkingNode
  -> GenSqlNode
       | uses:
       | - selected LLM provider
       | - available skills context
       | - schema context
       | - SQL history cache
       | - optional LanceDB retrieval
  -> ExecuteSqlNode
  -> ReflectNode
       | strategy:
       | - SUCCESS -> OutputNode
       | - FIX_SQL -> FixNode -> ExecuteSqlNode
       | - REGENERATE -> GenSqlNode
       | - NEED_USER_REVIEW -> stop with message
  -> OutputNode
  -> optional VisualizationNode
  -> SQLHistoryStore
```

## 原项目能力对应

| 原项目能力 | 进阶复现对应 | 复现深度 |
| --- | --- | --- |
| `LLMBaseModel` + 多 provider | `models/` 下 provider adapter + factory | 中等 |
| `SkillManager` / `SkillRegistry` | 本地 `skills/` 目录 + metadata + prompt 注入 | 中等 |
| `ReflectNode` | 执行后 LLM 评估 + strategy | 中等 |
| `DateParserNode` | 规则优先 + LLM fallback 的日期上下文 | 中等 |
| `FixNode` | SQL 执行失败后自动修复重试 | 中等 |
| `Plan Mode` | 先输出执行计划，用户确认后运行 | 简化 |
| `Reference SQL RAG` | SQLite 历史 SQL 缓存 | 简化 |
| `LanceDB` | 可选向量检索历史 SQL / schema docs | 简化到中等 |
| `API` | FastAPI chat endpoint | 简化 |
| `MCP` | 暴露 ask_sql/list_tools 等工具 | 简化 |
| `Gateway` | HTTP webhook adapter | 简化 |

## 控制流重点

基础版 workflow 是线性的。进阶版开始出现两类控制流：

1. 前置增强节点
   - PlanModeNode
   - DateParserNode

2. 后置反馈节点
   - ReflectNode
   - FixNode

建议先让 workflow 支持“插入节点”和“重试循环”，但不要实现复杂 DAG。最小可用策略：

```text
max_retries = 2
run nodes in order
if execute_sql fails:
  run FixNode
  retry ExecuteSqlNode
if execute_sql succeeds:
  run ReflectNode
  if strategy == SUCCESS:
    continue
  if strategy == FIX_SQL and retry_count < max_retries:
    run FixNode
    retry ExecuteSqlNode
  if strategy == REGENERATE and retry_count < max_retries:
    run GenSqlNode
    retry ExecuteSqlNode
  else:
    stop with clear error
```

## 数据流新增字段

进阶版 Context 建议新增：

```text
plan
plan_approved
date_context
skills_context
selected_model
history_matches
vector_matches
reflection_result
fix_attempts
visualization
run_logs
```

这些字段足以承载进阶能力，不需要照搬原项目全部 schema。
