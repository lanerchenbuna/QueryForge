# Prompt 02: 原项目上的多 Agent 协作层架构设计

复制下面整段给 Codex 使用。

```text
继续上一轮。现在请为 QueryForge 设计“多 Agent 协作层”。本轮仍不写代码，只输出可实施设计。

关键定位：

QueryForge 原项目是主体。多 Agent 协作层必须作为原项目的增量 wrapper / orchestration layer 存在。不要新建另一个项目，不要替换原有 WorkflowRunner / Workflow / Node，不要复制已有 SQL 生成、执行、反思、修复、可视化逻辑。

现有能力假设：

- CLI/API/MCP/Gateway 多入口
- WorkflowRunner / Workflow / Node
- 多 LLM provider
- Skills 系统
- DateParserNode / Plan Mode
- ReflectNode / FixNode retry
- SQL history cache / LanceDB
- Visualization
- Logging

目标：

在现有 workflow 之上增加可选多 Agent 编排层，而不是替换现有节点。开启多 Agent 时，由协作层调度和增强；关闭多 Agent 时，原 workflow 完全照旧运行。

请设计：

1. 新目录结构
   至少包含：
   - agents/
   - skills/
   - orchestrator/
   - runtime/
   - artifacts/
   - schemas/
   - hooks/

2. Agent 列表与职责
   - EntryRouterAgent
   - OrchestratorAgent
   - ProductAnalystAgent
   - SchemaArchitectAgent
   - SQLDeveloperAgent
   - DataQAAgent
   - KnowledgeAgent
   - GovernanceAgent
   - VisualizationAgent
   - OpsAgent

3. Agent 与现有 Node 的关系
   必须说明哪些 Agent 复用已有 Node / Store / Tool：
   - SQLDeveloperAgent 复用 GenSqlNode / FixNode
   - DataQAAgent 复用 ExecuteSqlNode / ReflectNode 的部分逻辑，不重新实现数据库执行器
   - KnowledgeAgent 复用 SQL history / LanceDB
   - VisualizationAgent 复用 VisualizationNode
   - GovernanceAgent 复用已有 SQL guard / DatabaseTool 只读检查
   - OrchestratorAgent 最终仍调用已有 WorkflowRunner 或已有节点能力

4. Pipeline 类型
   至少设计：
   - ask_sql
   - sql_review
   - troubleshoot_sql
   - explain_result
   - build_report

5. Orchestrator 调度策略
   - 串行阶段
   - 可并行阶段
   - 失败回退
   - retry budget
   - HITL checkpoint

6. Artifact 契约
   设计这些 JSON artifact：
   - analysis_request.json
   - schema_plan.json
   - sql_candidate.json
   - execution_result.json
   - qa_report.json
   - governance_report.json
   - visualization_artifact.json
   - delivery_report.json

7. Runtime state
   设计 `.queryforge/runs/<run_id>/state.json` 的核心字段。

限制：

- 不要实现代码。
- 不要做复杂分布式调度。
- 不要把每个 Agent 做成独立进程，先允许同进程类调用。
- 不要复制已有 workflow 逻辑。
- 不要把多 Agent 协作层设计成新的主系统。
- 不要破坏不开启 `--agent-team` 时的旧运行路径。

输出要求：

- 架构设计文档。
- ASCII workflow 图。
- 目录结构图。
- Agent/Skill/Artifact 映射表。
- 下一轮实现 EntryRouter 和 Orchestrator 的任务清单。

验收标准：

- 设计能在现有 QueryForge 上增量实现，且原 workflow 仍是核心执行主链路。
- 多 Agent 和原 workflow 关系清楚。
- 每个 Agent 职责边界清楚，不互相抢职责。
```
