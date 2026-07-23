# Prompt 04: 核心功能开发

复制下面整段给 Codex 使用。使用前请确保上一轮已经创建 `QueryForge/` 项目骨架。

```text
继续上一轮。现在请在已有 QueryForge 项目中实现核心功能。

本轮目标：

把占位 workflow 变成可运行的自然语言转 SQL 主链路。实现范围严格限制为：

CLI 输入自然语言问题
  -> WorkflowRunner 创建固定 workflow
  -> SchemaLinkingNode 获取数据库表结构
  -> GenSqlNode 调用 LLM 生成 SQLite SELECT SQL
  -> ExecuteSqlNode 执行 SQL
  -> OutputNode 返回 SQL、解释、结果

请先读取当前项目文件，再修改。不要重新生成整个项目。

实现要求：

1. SQLiteConnector
   文件：`db/sqlite_connector.py`
   职责：
   - 连接 SQLite 文件。
   - 判断数据库文件是否存在。
   - 列出用户表，排除 sqlite 内部表。
   - 获取指定表的字段名、类型、是否主键、是否可空。
   - 执行 SQL 并返回 columns + rows。
   - 对执行异常返回清晰错误，不要吞掉异常。

2. DatabaseTool
   文件：`tools/database_tool.py`
   职责：
   - 包装 SQLiteConnector。
   - 提供 list_tables、describe_table、execute_sql。
   - execute_sql 必须只允许只读查询。
   - 对空 SQL、非 SELECT、包含多语句或明显 DDL/DML 的 SQL 给出错误。
   - 只读防护可以简单直接，不需要做完整 SQL parser。

3. 原项目 sample database
   文件：`sample/prepare_sample_data.py`
   职责：
   - 使用随 prompts 交付物打包的 `sample_data/anime_streaming/anime_streaming.sqlite`。
   - 不要再创建 users/anime/watch sessions 临时 mock 数据作为主样例。
   - 检查 `sample_data/anime_streaming/anime_streaming.sqlite` 是否存在。
   - 检查数据库中至少包含 `dim_anime`、`fact_subscription`、`fact_rating` 三张表。
   - 检查 reference SQL 和 success story 文件是否存在。
   - 如果样例数据缺失，输出清晰提示：从项目根目录 `sample_data/anime_streaming/` 检查或恢复。
   - 数据要能支持几个原项目测试问题，例如：
     - What is the highest eligible free rate for K-12 students in the dim_anime in Alameda County?
     - Please list the lowest three eligible free rates for students aged 5-17 in continuation dim_anime.
     - What is the phone number of the anime with the highest total watch time?

4. LLM 层
   文件：`models/llm.py`
   职责：
   - 封装 OpenAI-compatible chat completion。
   - 从 config 读取 api key、model。
   - 提供一个 generate_json(prompt) 或等价方法。
   - 要求模型输出 JSON，但仍要处理模型返回 markdown code fence、前后多余文本等情况。
   - 如果没有 OPENAI_API_KEY，要返回清晰错误，不能抛出难懂堆栈。

5. SchemaLinkingNode
   文件：`agent/node/schema_linking_node.py`
   职责：
   - 调用 DatabaseTool 获取表列表和表结构。
   - MVP 不做向量检索，先把所有表结构放入 Context。
   - 可以做一个非常简单的关键词过滤，但必须保证找不到关键词时 fallback 到所有表。
   - 输出写入 context.relevant_tables。

6. GenSqlNode
   文件：`agent/node/gen_sql_node.py`
   职责：
   - 读取 context.sql_task.question 和 context.relevant_tables。
   - 构造 LLM prompt。
   - prompt 必须说明：
     - 你是 SQLite SQL 专家。
     - 只能生成 SELECT 查询。
     - 只能使用给定表和字段。
     - 不要编造表名和字段名。
     - 输出 JSON，字段为 sql、explanation、tables_used。
   - 解析 LLM 输出为 SQLContext。
   - 如果解析失败，要返回 NodeResult failure，并提示原始模型输出。

7. ExecuteSqlNode
   文件：`agent/node/execute_sql_node.py`
   职责：
   - 读取最后一个 SQLContext。
   - 调用 DatabaseTool.execute_sql。
   - 写入 context.execution_result。
   - 执行失败时返回失败结果，让 Workflow 停止。

8. OutputNode
   文件：`agent/node/output_node.py`
   职责：
   - 整理最终输出。
   - 至少包含 question、sql、explanation、tables_used、columns、rows、row_count。
   - 控制输出格式，保证 CLI 易读。

9. Workflow / WorkflowRunner
   文件：
   - `agent/workflow.py`
   - `agent/workflow_runner.py`
   职责：
   - 确保四个节点按固定顺序执行。
   - 每个节点执行前后可以打印简短阶段信息。
   - 任一节点失败时停止，并输出失败节点、错误原因、已生成的上下文摘要。

10. CLI
   文件：`main.py`
   职责：
   - 支持 `--question`、`--database`、`--prepare-sample-data`、`--show-workflow`。
   - `--prepare-sample-data` 会检查 bundled sample data 是否可用后退出。
   - `--database` 默认指向 `sample_data/anime_streaming/anime_streaming.sqlite`。
   - 正常运行时校验数据库路径存在。
   - 打印最终结果。

限制条件：

- 不实现 Web、API、MCP。
- 不实现 RAG、embedding、LanceDB。
- 不实现多数据库。
- 不实现多模型 provider。
- 不实现自动修复、反思、并行、selection。
- 不引入复杂依赖。
- 不把 prompt 写死成只适配 sample database；它应该能读取任意 SQLite 表结构。

输出要求：

- 直接修改项目文件。
- 不要只输出设计。
- 修改完成后，列出变更文件和每个文件的作用。
- 给出运行验证命令。
- 如果你发现上一轮骨架有不合理之处，可以小范围调整，但必须说明调整原因。

验收标准：

1. 可以初始化示例库：
   `python main.py --prepare-sample-data`

2. 可以查看 workflow：
   `python main.py --show-workflow --question "What is the phone number of the anime with the highest total watch time?"`

3. 设置 OPENAI_API_KEY 后可以运行：
   `python main.py --database sample_data/anime_streaming/anime_streaming.sqlite --question "What is the phone number of the anime with the highest total watch time?"`

4. 输出中必须包含：
   - 生成 SQL
   - SQL 解释
   - 查询结果 rows
   - row_count

5. 对危险 SQL 或执行错误有清晰错误提示。
```
