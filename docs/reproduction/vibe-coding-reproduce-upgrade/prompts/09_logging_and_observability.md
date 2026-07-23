# Prompt 09: 日志系统与轻量可观测性

复制下面整段给 Codex 使用。

```text
继续上一轮。现在请为 QueryForge 增加完善但轻量的日志系统。

本轮目标：

让每次 Agent 运行都有可追踪的 run_id、节点耗时、模型调用摘要、SQL 尝试历史和错误信息。不要做生产级 tracing，但要足够调试。

请先读取当前项目文件，再做增量修改。

实现范围：

1. logging 配置
   - 使用 Python logging 标准库。
   - 支持 console + file。
   - 支持 LOG_LEVEL。
   - 日志文件默认 `.queryforge/logs/queryforge.log`。

2. run_id
   - 每次 CLI/API/MCP/Gateway 调用生成 run_id。
   - run_id 要进入 Context。
   - 所有日志包含 run_id。

3. 节点日志
   每个 Node 执行时记录：
   - node name
   - start time
   - end time
   - duration
   - success/failure
   - error message

4. 模型调用日志
   - 记录 provider、model、prompt 字符数、返回字符数。
   - 不默认记录完整 prompt，避免泄露。
   - 可通过 `--debug-prompts` 显式保存 prompt 到 `.queryforge/traces/`。

5. SQL 日志
   - 记录生成 SQL。
   - 记录执行耗时。
   - 记录 row_count。
   - 记录 fix attempts。

6. 运行摘要
   - 每次运行结束后输出 run summary。
   - 包含：
     - run_id
     - question
     - workflow nodes
     - selected model
     - retries
     - history/vector matches count
     - output status

7. CLI
   - `--log-level`
   - `--debug-prompts`
   - `--show-run-summary`

限制：

- 不引入 OpenTelemetry。
- 不引入外部日志服务。
- 不默认保存敏感完整 prompt。
- 不把日志系统做成复杂框架。

输出要求：

- 直接修改文件。
- 说明日志字段。
- 给出日志文件路径。
- 给出验证命令。

验收标准：

- 每次运行都有 run_id。
- 节点执行耗时可见。
- 错误可定位到具体节点。
- debug-prompts 关闭时不保存完整 prompt。
- debug-prompts 开启时能保存 prompt trace。
```
