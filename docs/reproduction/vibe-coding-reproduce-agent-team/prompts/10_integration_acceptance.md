# Prompt 10: 多 Agent Team 集成验收

复制下面整段给 Codex 使用。

```text
继续上一轮。现在请对 QueryForge 的多 Agent 协作层做完整集成验收。

本轮目标：

不要新增大功能。请验证多 Agent 协作层、原 workflow、Skills 三层加载、state/artifacts/checkpoints、runtime、governance、hooks 能一起工作。重点确认：原复现项目仍是主体，多 Agent 层只是可选增强。

请先读取当前项目，再执行检查和最小修复。

验收范围：

1. 基础兼容
   - 不开启 `--agent-team` 时，旧 workflow 仍能跑通。
   - 开启 `--agent-team` 时，走 EntryRouter -> Orchestrator。
   - 开启 `--agent-team` 后，SQL 主链路仍复用原 WorkflowRunner / Node，而不是复制一套新逻辑。

2. 路由
   - ask_sql 能路由到 Orchestrator。
   - sql_review 能路由到 QA + Governance。
   - metadata_query 能路由到 KnowledgeAgent。
   - unknown 能给出澄清或 fallback。

3. Agent pipeline
   ask_sql 至少经过：
   - ProductAnalystAgent
   - KnowledgeAgent
   - SchemaArchitectAgent
   - SQLDeveloperAgent
   - GovernanceAgent
   - DataQAAgent
   - VisualizationAgent 可选
   - Delivery

4. Artifacts
   检查 `.queryforge/runs/<run_id>/artifacts/` 中至少包含：
   - analysis_request.json
   - knowledge_context.json
   - schema_plan.json
   - sql_candidate.json
   - governance_report.json
   - execution_result.json
   - qa_report.json
   - delivery_report.json

5. State
   - state.json 包含 current_phase。
   - completed_phases 正确推进。
   - artifacts 路径可读。
   - blocked 状态可表示。

6. Skills
   - `--show-skill-plan` 能展示 role/base/task/overlay/context。
   - SQLDeveloperAgent 使用 SQLite overlay。
   - artifact 记录 selected_skills。

7. Parallel
   - ProductAnalystAgent 和 KnowledgeAgent 可并行。
   - trace 能看到并行阶段。

8. Governance
   - SELECT 通过。
   - DROP/INSERT/UPDATE/DELETE 阻断。
   - high cost warning 产生 checkpoint。

9. Checkpoints
   - Plan Mode checkpoint 不确认不执行。
   - high cost checkpoint 可 approve 后继续。
   - reject 后 run 标记 blocked。

10. Hooks / recovery
   - trace.jsonl 存在。
   - summary.json 存在。
   - `--recover` 能列出未完成 run。
   - `--resume-run` 能继续。

11. Sample data
   使用已打包的：
   - `sample_data/anime_streaming/anime_streaming.sqlite`
   - 至少运行一个真实问题：
     `What is the phone number of the anime with the highest total watch time?`

允许修改：

- 修 bug。
- 补 smoke tests。
- 改 README。
- 改错误提示。

不允许修改：

- 不新增大功能。
- 不引入外部平台依赖。
- 不破坏非 agent-team 模式。
- 不用新实现替换原 GenSqlNode / ExecuteSqlNode / ReflectNode / FixNode / VisualizationNode。

输出要求：

- 先列验收计划。
- 执行验证。
- 修复发现的问题。
- 输出最终验收报告。
- 输出从零运行命令。

最终验收标准：

- agent-team 模式能端到端跑通 ask_sql。
- 旧模式仍能跑通。
- agent-team 模式复用原 workflow 核心能力。
- 多 Agent 产物完整。
- Governance gate 生效。
- state/trace/summary 可用于调试和恢复。
```
