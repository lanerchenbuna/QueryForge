# Prompt 03: EntryRouterAgent 与 OrchestratorAgent

复制下面整段给 Codex 使用。

```text
继续上一轮。现在请在 QueryForge 中实现 EntryRouterAgent 和 OrchestratorAgent。

本轮目标：

在原 QueryForge 项目上新增一个可选的多 Agent 协作入口和总协调层。只实现路由、状态、调度骨架，不实现所有专业 Agent 的完整能力。

重要约束：

- 原 `WorkflowRunner / Workflow / Node` 是主体，不能删除、替换或绕开。
- EntryRouterAgent 和 OrchestratorAgent 是 wrapper / orchestration layer。
- `--agent-team` 未开启时，旧路径必须完全不变。
- `--agent-team` 开启后，ask_sql 仍应能调用已有 WorkflowRunner 完成 SQL 主链路。

请先读取当前项目，再做增量修改。

实现要求：

1. EntryRouterAgent
   职责：
   - 接收用户输入和入口来源（CLI/API/MCP/Gateway）。
   - 分类任务类型。
   - 简单任务可直接返回。
   - 复杂任务交给 OrchestratorAgent。
   - 只做路由，不生成 SQL、不执行 SQL、不做业务判断。

   任务类型至少包括：
   - ask_sql
   - sql_review
   - troubleshoot_sql
   - explain_result
   - build_report
   - metadata_query
   - unknown

2. OrchestratorAgent
   职责：
   - 创建 run_id。
   - 初始化 task_state。
   - 根据 task_type 选择 pipeline。
   - 调度后续 agent。
   - 管理 artifacts。
   - 管理 checkpoints。
   - 汇总 delivery_report。
   - 在专业 Agent 尚未完整实现时，调用已有 WorkflowRunner 作为核心执行 fallback。

3. Pipeline 定义
   先实现配置化 pipeline，不要写死在大 if else 中。
   例如：

   ask_sql:
     - product_analyst
     - knowledge
     - schema_architect
     - sql_developer
     - governance
     - data_qa
     - visualization
     - delivery

4. Runtime State
   写入：
   - `.queryforge/runs/<run_id>/state.json`
   - `.queryforge/runs/<run_id>/artifacts/`
   - `.queryforge/runs/<run_id>/logs/`

5. CLI 集成
   - 增加 `--agent-team` 开关。
   - 未开启时保留旧 workflow。
   - 开启后走 EntryRouterAgent -> OrchestratorAgent。
   - 增加 `--show-agent-plan`。

6. 降级策略
   - 如果某个专业 Agent 还未实现，Orchestrator 应输出明确 TODO artifact，而不是崩溃。
   - 保证 ask_sql 可以 fallback 到已有 WorkflowRunner。
   - fallback 是正式设计的一部分，不是临时错误处理；它确保原复现项目始终可运行。

限制：

- 不要实现完整专业 Agent。
- 不要删除旧 CLI / WorkflowRunner。
- 不要引入异步队列或多进程。
- 不要绕过 Plan Mode / SQL guard。
- 不要为了多 Agent 层重写 GenSqlNode、ExecuteSqlNode、ReflectNode、FixNode。

输出要求：

- 直接修改文件。
- 给出新增目录和文件。
- 给出路由规则。
- 给出 state.json 示例。
- 给出验证命令。

验收标准：

- `--agent-team --show-agent-plan` 能展示将要执行的 Agent pipeline。
- ask_sql 可以通过 Orchestrator fallback 到旧 workflow 跑通。
- 每次运行都有 run_id 和 state.json。
- EntryRouter 不直接执行 SQL。
- 不开启 `--agent-team` 时，原 CLI 行为不变。
```
