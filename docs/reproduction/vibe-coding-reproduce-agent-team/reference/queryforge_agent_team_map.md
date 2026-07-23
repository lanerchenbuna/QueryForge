# QueryForge 多 Agent 协作层映射

## 原复现项目是主体

进阶版 QueryForge 当前已有主链路：

```text
WorkflowRunner
  -> DateParserNode
  -> SchemaLinkingNode
  -> GenSqlNode
  -> ExecuteSqlNode
  -> ReflectNode / FixNode
  -> OutputNode / VisualizationNode
```

第三阶段不是替换这条链路，而是在它外面增加一个可选协作层：

```text
EntryRouterAgent
  -> OrchestratorAgent
       -> KnowledgeAgent
       -> ProductAnalystAgent
       -> SchemaArchitectAgent
       -> SQLDeveloperAgent
       -> DataQAAgent
       -> GovernanceAgent
       -> VisualizationAgent
       -> OpsAgent
       -> existing WorkflowRunner / Nodes
```

原则：

- 原 `WorkflowRunner / Workflow / Node` 继续承担 SQL 生成、执行、反思、修复、输出等核心运行逻辑。
- 新增 Agent 主要负责结构化需求、知识检索、schema 规划、治理 gate、QA 和交付汇总。
- SQLDeveloperAgent 应优先调用已有 `GenSqlNode / FixNode`，而不是重写 SQL 生成。
- VisualizationAgent 应优先调用已有 VisualizationNode。
- KnowledgeAgent 应优先调用已有 SQL history / LanceDB。

## Pipeline 示例

### ask_sql：协作层包裹原 workflow

```text
EntryRouterAgent
  -> OrchestratorAgent
  -> parallel:
       KnowledgeAgent: history/reference/schema docs
       ProductAnalystAgent: structured analysis request
  -> SchemaArchitectAgent: tables, columns, join path
  -> SQLDeveloperAgent: SQL candidate
  -> GovernanceAgent: read-only / cost gate
  -> existing ExecuteSqlNode / WorkflowRunner 执行 SQL
  -> DataQAAgent: result validation
  -> existing VisualizationNode 或 VisualizationAgent: optional chart
  -> OrchestratorAgent: delivery report
```

### sql_review

```text
EntryRouterAgent
  -> DataQAAgent: correctness and result risk
  -> GovernanceAgent: safety and cost risk
  -> KnowledgeAgent: best practices
  -> OrchestratorAgent: consolidated review
```

### troubleshoot

```text
EntryRouterAgent
  -> OpsAgent: classify failure
  -> KnowledgeAgent: similar failures
  -> SQLDeveloperAgent: repair proposal
  -> DataQAAgent: verify repair
  -> OrchestratorAgent: report
```

## Agent Artifact Contract

```text
ProductAnalystAgent
  output: analysis_request.json

SchemaArchitectAgent
  input: analysis_request.json + knowledge_context
  output: schema_plan.json

SQLDeveloperAgent
  input: schema_plan.json + skills_context + date_context
  output: sql_candidate.json

GovernanceAgent
  input: sql_candidate.json + schema_plan.json
  output: governance_report.json

DataQAAgent
  input: sql_candidate.json + execution_result
  output: qa_report.json

VisualizationAgent
  input: execution_result + analysis_request
  output: visualization_artifact.json

OrchestratorAgent
  input: all artifacts
  output: delivery_report.json
```

## Skills Loading Contract

```text
Agent Role Definition
  + Base Skill
  + Task Skill
  + Datasource Overlay
  + Runtime Context
```

示例：

```text
SQLDeveloperAgent
  role: agents/sql_developer.md
  base: skills/base/sql_agent/SKILL.md
  task: skills/sql_developer/nl2sql/SKILL.md
  overlay: skills/datasource/sqlite/SKILL.md
  context: schema_plan + history_matches + date_context
```

## HITL Checkpoints

建议保留这些检查点：

- `requirement_unclear`：用户问题不清楚。
- `plan_approval`：Plan Mode 下执行前确认。
- `dangerous_sql_blocked`：SQL guard 拦截。
- `high_cost_warning`：可能全表扫描或结果过大。
- `qa_failed`：QA 判断结果不满足需求。
- `delivery_review`：最终交付前确认。

## 不迁移的内容

不迁移 DW Agent Team 中强绑定内部平台的能力：

- bytedcli Dorado / Oceanus / Meego 具体命令。
- 飞书知识库真实拉取。
- 影子任务 promote。
- 内部 telemetry endpoint。
- 平台 shim 安装脚本。

只迁移架构思想和可在本地复现的最小实现；实现主体仍然是前两个复现文件夹产出的 QueryForge 项目。
