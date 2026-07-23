# DW Agent Team 可借鉴模式

本文总结 `dw_agent_team_pkg_副本/dw-agent-team` 中值得迁移到 QueryForge 复现项目的模式。

## 1. Entry Router

`agents/_entry.md` 的设计要点：

- 入口只做路由，不做业务。
- 读取上下文和分类信号。
- 决定下一跳 agent。
- 失败时退回 orchestrator。

迁移到 QueryForge：

```text
EntryRouterAgent
  -> metadata/query 问题：KnowledgeAgent
  -> SQL 分析问题：OrchestratorAgent
  -> SQL review：DataQAAgent + GovernanceAgent
  -> visualization：VisualizationAgent
  -> API/MCP/Gateway 运维：OpsAgent
  -> 简单解释：直接回答
```

## 2. Orchestrator

`agents/orchestrator.md` 的设计要点：

- 分类任务。
- 选择流程。
- 调度专业 Agent。
- 管理上下文和质量关卡。
- 处理回退和澄清。
- 汇总最终交付。

迁移到 QueryForge：

```text
OrchestratorAgent
  classify request
  choose pipeline
  create task_state
  dispatch agents
  validate artifacts
  checkpoint if needed
  produce delivery report
```

## 3. Role Agents

DW Agent Team 有明确角色边界：

- Product Manager：需求结构化。
- Architect：表设计和建模。
- Developer：实现代码。
- QA：测试和质量验证。
- Ops：发布上线和排障。
- Knowledge Base：知识检索。
- Governance：治理和安全。

迁移到 QueryForge：

- ProductAnalystAgent：把用户问题转成分析需求。
- SchemaArchitectAgent：选择表、字段、join path、指标口径。
- SQLDeveloperAgent：生成 SQL、修复 SQL。
- DataQAAgent：验证 SQL 和结果。
- KnowledgeAgent：检索历史 SQL / schema docs / reference SQL。
- GovernanceAgent：只读安全、成本、敏感字段、全表扫描 gate。
- OpsAgent：多端入口、配置、smoke test、运行状态。

## 4. Skills 三层加载

DW Agent Team 的 Developer Agent 使用：

```text
base skill
  + task-specific skill
  + platform overlay skill
```

迁移到 QueryForge：

```text
base SQL agent skill
  + task skill: nl2sql / sql_review / troubleshooting / visualization
  + datasource overlay: sqlite / duckdb / postgres
```

规则：

- base 负责通用约束。
- task skill 负责具体工作流。
- datasource overlay 只覆盖方言、连接器、限制和测试命令。

## 5. Artifacts Schema

DW Agent Team 用 JSON schema 约束阶段产物。

迁移到 QueryForge 的最小产物：

- `analysis_request.json`
- `schema_plan.json`
- `sql_candidate.json`
- `qa_report.json`
- `governance_report.json`
- `visualization_artifact.json`
- `delivery_report.json`

每个 Agent 只消费上游产物中自己需要的字段。

## 6. State 与 Checkpoint

DW Agent Team 的 `task-state.schema.json` 包含：

- task_id
- classification
- current_phase
- completed_phases
- artifacts
- retry_counts
- checkpoints
- blocked reason

迁移到 QueryForge：

```text
.queryforge/runs/<run_id>/state.json
.queryforge/runs/<run_id>/artifacts/*.json
.queryforge/runs/<run_id>/logs/*.log
```

Plan Mode、SQL 执行、危险 SQL、外部 API、Gateway 回复都可以作为 checkpoint。

## 7. Hooks / Guard / Telemetry

DW Agent Team 的 hooks 做：

- session recovery
- pre-tool guard
- telemetry
- stop reconciliation

迁移到 QueryForge：

- 启动时恢复未完成 run。
- SQL 执行前做只读 guard。
- 每个 Agent 产物写入 trace。
- 结束时写 delivery summary。

不需要照搬平台 hooks，可在 Python runtime 内实现。
