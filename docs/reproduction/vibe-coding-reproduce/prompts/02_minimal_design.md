# Prompt 02: 最小实现方案设计

复制下面整段给 Codex 使用。使用前请确保上一轮已经得到“核心功能识别”的结论。

```text
继续上一轮。我们已经确认 MVP 目标是复现 Datus-Agent 的主功能：通过 CLI 输入自然语言问题，程序按 workflow 执行 schema linking、SQL generation、SQL execution、output，最终返回 SQL 和查询结果。

本轮请只做最小实现方案设计，不要写代码。

上一轮结论摘要：

- 核心价值：自然语言转 SQL，并执行查询。
- MVP 主流程：用户问题 -> schema_linking -> gen_sql -> execute_sql -> output。
- 保留模块：CLI、WorkflowRunner、Workflow、Node 基类、4 个核心节点、Context 数据模型、LLM 调用层、数据库工具、SQLite connector。
- 省略模块：Web、REST API、MCP、Gateway、向量数据库、RAG 七层知识库、多模型 provider、多数据库适配、权限、审计、trace、parallel、selection、reflect、fix、subagent。

请设计一个名为 QueryForge 的 MVP 项目。

设计要求：

1. 技术栈
   - Python 3.11 或更高版本
   - SQLite，使用标准库 sqlite3
   - OpenAI-compatible chat completion API
   - python-dotenv 用于读取环境变量
   - rich 可选，用于更好地打印表格；如果你认为会增加复杂度，可以不用
   - 尽量少依赖

2. 目录结构
   请给出清晰目录结构，并解释每个文件职责。
   目录必须至少覆盖：
   - CLI 入口
   - config
   - schemas / models
   - agent/workflow
   - agent/workflow_runner
   - agent/node
   - models/llm
   - tools/database_tool
   - db/sqlite_connector
   - sample database 初始化或说明

3. 数据模型
   请描述这些模型的字段和用途，不要写代码：
   - SqlTask：表示一次用户查询任务
   - Context：节点间共享状态
   - TableSchema：表结构摘要
   - SQLContext：SQL 生成结果
   - NodeResult：节点执行结果

4. 模块接口
   请描述关键类和方法的职责，不要写代码：
   - WorkflowRunner.run(task)
   - Workflow.run()
   - Node.execute(context)
   - SchemaLinkingNode.execute(context)
   - GenSqlNode.execute(context)
   - ExecuteSqlNode.execute(context)
   - OutputNode.execute(context)
   - LLM.generate(messages or prompt)
   - SQLiteConnector.list_tables()
   - SQLiteConnector.describe_table(table)
   - SQLiteConnector.execute_sql(sql)
   - DatabaseTool 的只读 SQL 防护

5. Workflow 设计
   请用 ASCII 图画出 MVP workflow。
   同时说明 Context 在每个节点后的状态变化。

6. Prompt 设计
   请描述 GenSqlNode 给 LLM 的 prompt 应包含哪些信息：
   - 角色
   - 用户问题
   - 可用表结构
   - SQL 方言为 SQLite
   - 只能输出 SELECT 查询
   - 输出格式必须是 JSON，包含 sql、explanation、tables_used

7. 错误处理
   请设计最小错误处理策略：
   - 没有 API key
   - 数据库文件不存在
   - 没有表
   - LLM 输出不是 JSON
   - SQL 不是 SELECT
   - SQL 执行失败

限制条件：

- 不写代码。
- 不设计自动修复、反思、多轮 tool calling。
- 不加入向量数据库或 embedding。
- 不做多 datasource 管理。
- 不做复杂配置系统。

预期输出：

- 一份 MVP 技术设计文档。
- 包含目录结构、模块职责、数据模型、接口说明、workflow 图、错误处理策略。
- 最后输出“下一轮初始化项目时需要创建的文件清单”。

验收标准：

- 设计足够简单，Codex 下一轮可以直接按它初始化项目。
- 每个模块都有明确职责，且不重叠。
- workflow 和 Context 数据流讲清楚。
- 没有把原项目的非核心工程化能力带入 MVP。
```
