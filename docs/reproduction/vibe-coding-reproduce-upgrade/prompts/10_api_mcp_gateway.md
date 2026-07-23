# Prompt 10: API / MCP / Gateway 多端入口

复制下面整段给 Codex 使用。

```text
继续上一轮。现在请为 QueryForge 增加多端入口：API、MCP、Gateway。

本轮目标：

复现 Datus-Agent 的多入口思想：不同入口共享同一个 WorkflowRunner，不复制核心业务逻辑。

请先读取当前项目文件，再做增量修改。

实现顺序：

1. 服务层
   先抽出统一服务：
   - AgentService.ask(question, options)
   - AgentService.plan(question, options)
   - AgentService.list_models()
   - AgentService.list_skills()
   - AgentService.health()

   CLI、API、MCP、Gateway 都调用 AgentService。

2. REST API
   - 使用 FastAPI。
   - 路由：
     - GET /health
     - GET /models
     - GET /skills
     - POST /ask
     - POST /plan
   - /ask 输入：
     - question
     - database
     - model_provider 可选
     - model 可选
     - skills 可选
     - plan_mode 可选
     - auto_approve_plan 可选
     - visualize 可选
   - 输出复用 CLI final_output。

3. MCP
   - 如果可用，使用 mcp Python SDK。
   - 暴露工具：
     - ask_sql
     - list_models
     - list_skills
     - get_history
   - 如果 mcp SDK 未安装，提供清晰降级提示。
   - 不要求完整生产级 MCP Server，只要能展示工具映射思想。

4. Gateway
   - 实现一个简化 HTTP webhook adapter。
   - 路由：
     - POST /gateway/webhook
   - 输入可模拟 Slack/Feishu：
     - user_id
     - channel
     - text
   - 输出：
     - text response
     - sql
     - rows preview
   - 不实现真实签名校验和平台 SDK。

5. CLI
   - 增加启动 API 的命令或脚本说明。
   - CLI 仍然保留，不被 API 替代。

限制：

- 不要把 API/Gateway 写成另一套 SQL 生成逻辑。
- 不做认证。
- 不做真实飞书/Slack SDK。
- 不做 SSE streaming，除非非常简单。
- MCP 不可用时不能影响 CLI/API。

输出要求：

- 直接修改文件。
- 给出新目录结构。
- 给出 API 请求示例。
- 给出 MCP 工具说明。
- 给出 Gateway webhook 示例。
- 给出验证命令。

验收标准：

- CLI 仍可运行。
- API /ask 能返回 SQL 和 rows。
- API /health 能返回 ok。
- Gateway webhook 能调用同一套 AgentService。
- MCP 入口可用或有清晰降级提示。
```
