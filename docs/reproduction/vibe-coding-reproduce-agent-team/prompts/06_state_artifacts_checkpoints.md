# Prompt 06: State、Artifacts 与 HITL Checkpoints

复制下面整段给 Codex 使用。

```text
继续上一轮。现在请为 QueryForge 的多 Agent 协作层实现结构化 state、artifacts 和 HITL checkpoints。

本轮目标：

复现 DW Agent Team 的“状态机 + 阶段产物 + 人工确认点”思想，让多 Agent 协作层可恢复、可审计、可回退。原 Workflow 的 Context 仍然保留，state/artifacts 是协作层的外部运行记录，不替代 Context。

请先读取当前项目，再做增量修改。

实现要求：

1. task_state
   文件位置：
   `.queryforge/runs/<run_id>/state.json`

   字段至少包含：
   - run_id
   - user_question
   - task_type
   - current_phase
   - completed_phases
   - artifacts
   - retry_counts
   - checkpoints
   - blocked
   - block_reason
   - created_at
   - updated_at

2. phase enum
   至少包含：
   - init
   - classify
   - product_analysis
   - knowledge_retrieval
   - schema_planning
   - sql_development
   - governance
   - execution
   - qa
   - visualization
   - delivery
   - completed
   - blocked

3. artifact schemas
   在 `schemas/artifacts/` 下定义轻量 JSON schema 或 Pydantic model：
   - analysis_request
   - knowledge_context
   - schema_plan
   - sql_candidate
   - governance_report
   - execution_result
   - qa_report
   - visualization_artifact
   - delivery_report

4. checkpoint 类型
   - clarification_required
   - plan_approval
   - dangerous_sql_blocked
   - high_cost_warning
   - qa_failed
   - delivery_review

5. CLI 交互
   - `--resume-run <run_id>`
   - `--list-runs`
   - `--show-run <run_id>`
   - `--approve-checkpoint <run_id>:<checkpoint_id>`
   - `--reject-checkpoint <run_id>:<checkpoint_id>`

6. Plan Mode 整合
   - Plan Mode 应写 checkpoint。
   - 用户 approve 后才能继续 execution。

7. 恢复策略
   - 如果 run interrupted，从 state.json 恢复 current_phase。
   - 已完成 artifact 不重复生成，除非 `--force-phase`。

限制：

- 不实现数据库型 workflow engine。
- 不实现复杂 UI。
- 不绕过 checkpoint。
- 不把 runtime artifacts 写进源码目录根部，必须写入 `.queryforge/runs/`。
- 不要把原 Context 迁移成 state.json；state.json 只记录协作层运行状态和 artifact 路径。

输出要求：

- 直接修改文件。
- 给出 state.json 示例。
- 给出 artifact 文件示例。
- 给出 checkpoint 生命周期。
- 给出验证命令。

验收标准：

- 每次 agent-team run 都产生 state.json。
- 每个阶段 artifact 路径写入 state。
- Plan Mode 会产生可审批 checkpoint。
- run 可以 list/show/resume。
```
