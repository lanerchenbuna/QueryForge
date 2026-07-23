# Prompt 07: 多 Agent Runtime、并行与 Handoff

复制下面整段给 Codex 使用。

```text
继续上一轮。现在请实现 QueryForge 多 Agent 协作层的轻量 runtime。

本轮目标：

让 Orchestrator 能以统一方式调度 Role Agents，支持串行、并行、handoff 和失败回退。这个 runtime 只服务多 Agent 协作层，不替代原 WorkflowRunner。

请先读取当前项目，再做增量修改。

实现要求：

1. AgentRuntime
   提供统一接口：
   - run_agent(agent_name, input_context)
   - run_agents_parallel(agent_specs)
   - collect_artifacts()
   - handle_failure()

2. AgentSpec
   字段：
   - agent_name
   - phase
   - input_artifacts
   - output_artifact
   - required
   - can_run_parallel
   - retry_limit

3. 并行执行
   ask_sql pipeline 中允许并行：
   - ProductAnalystAgent
   - KnowledgeAgent

   sql_review pipeline 中允许并行：
   - DataQAAgent
   - GovernanceAgent
   - KnowledgeAgent

4. Handoff contract
   每个 Agent 返回：
   - status: success | blocked | failed
   - artifact_path
   - summary
   - risks
   - next_suggestions

5. 失败策略
   - required agent failed -> pipeline blocked。
   - optional agent failed -> 记录 warning，继续。
   - retry_limit 用尽 -> blocked。
   - blocked 必须写 block_reason。

6. Orchestrator 集成
   - pipeline 不再手写逐个调用。
   - 从 pipeline config 构建 AgentSpec。
   - Runtime 负责执行和收集。

7. 调试输出
   - `--show-agent-trace`
   - trace 中展示每个 Agent 的开始、结束、耗时、artifact。

限制：

- 可以用 Python asyncio 或线程池，但不要引入 Celery/RQ。
- 不要求真正跨进程 subagent。
- 不要并行执行会写同一个 artifact 的 Agent。
- 不要让并行破坏 checkpoint 顺序。
- 不要把原 WorkflowRunner 改造成 AgentRuntime；AgentRuntime 应该调用或包裹原 WorkflowRunner。
- 不开启 `--agent-team` 时，AgentRuntime 不参与运行。

输出要求：

- 直接修改文件。
- 给出 runtime 设计说明。
- 给出 ask_sql 和 sql_review 的 pipeline config。
- 给出验证命令。

验收标准：

- ProductAnalystAgent 和 KnowledgeAgent 可并行执行。
- trace 能展示 Agent 执行顺序和耗时。
- required/optional 失败行为不同。
- Orchestrator 不再负责底层执行细节。
```
