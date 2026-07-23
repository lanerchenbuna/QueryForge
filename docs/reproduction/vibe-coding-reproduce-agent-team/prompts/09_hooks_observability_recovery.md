# Prompt 09: Hooks、观测与恢复机制

复制下面整段给 Codex 使用。

```text
继续上一轮。现在请为 QueryForge 的多 Agent 协作层增加轻量 hooks、观测和恢复机制。

本轮目标：

参考 DW Agent Team 的 SessionStart、PreToolUse、Stop、Telemetry、Recovery 思想，但用本地 Python runtime 实现，不依赖外部平台 hooks。hooks 只包裹协作层和原 workflow 调用，不改变原业务结果。

请先读取当前项目，再做增量修改。

实现要求：

1. HookManager
   支持这些生命周期事件：
   - session_start
   - before_agent
   - after_agent
   - before_sql_execute
   - after_sql_execute
   - on_checkpoint
   - on_error
   - run_stop

2. 内置 hooks
   - recovery_hook：启动时发现未完成 run。
   - sql_guard_hook：执行 SQL 前调用 Governance gate。
   - telemetry_hook：记录 agent 运行、耗时、token/字符数摘要。
   - artifact_hook：每个阶段结束后校验 artifact 存在。
   - stop_hook：运行结束时写 run summary。

3. Recovery
   - `--recover` 列出 interrupted/blocked runs。
   - `--resume-run <run_id>` 从 state.current_phase 继续。
   - `--archive-run <run_id>` 归档旧 run。

4. Observability
   - `.queryforge/runs/<run_id>/trace.jsonl`
   - `.queryforge/runs/<run_id>/summary.json`
   - 每条 trace 包含：
     - timestamp
     - event
     - agent
     - phase
     - duration_ms
     - status
     - artifact_path
     - error

5. CLI
   - `--show-agent-trace`
   - `--recover`
   - `--resume-run`
   - `--archive-run`

6. 配置
   - hooks.yml 控制启用/禁用。
   - 默认启用 recovery、sql_guard、artifact、stop。
   - telemetry 可关闭。

限制：

- 不接外部 telemetry endpoint。
- 不实现 IDE 平台 hook 安装。
- 不记录完整敏感 prompt，除非 debug 开启。
- 不让 hooks 修改业务结果，除 sql_guard 可以阻断。
- 不开启 `--agent-team` 时，不强制启用协作层 hooks；原日志系统仍可独立工作。

输出要求：

- 直接修改文件。
- 给出 hook lifecycle 图。
- 给出 trace.jsonl 示例。
- 给出恢复流程。
- 给出验证命令。

验收标准：

- 每次 agent-team run 有 trace.jsonl。
- SQL 执行前触发 sql_guard_hook。
- run_stop 会写 summary。
- 可列出未完成 run。
- 可 resume 一个 blocked/interrupted run。
```
