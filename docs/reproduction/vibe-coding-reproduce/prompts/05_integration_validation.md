# Prompt 05: 基础联调与运行验证

复制下面整段给 Codex 使用。使用前请确保上一轮已经实现核心功能。

```text
继续上一轮。现在请对 QueryForge 做基础联调和运行验证。

本轮目标：

不要新增大功能。请验证现有 MVP 是否真的完成了主链路：

自然语言问题 -> schema linking -> SQL generation -> SQL execution -> output

请先读取当前项目文件，然后按下面顺序检查、运行、修复。

验证任务：

1. 静态检查
   - 检查目录结构是否符合 MVP 设计。
   - 检查是否存在不必要的复杂功能，例如 Web、API、MCP、向量库、多数据库管理。
   - 检查所有 import 是否正确。
   - 检查数据模型是否存在可变默认值问题。

2. sample database 验证
   - 运行 sample database 检查命令。
   - 确认 `sample_data/anime_streaming/anime_streaming.sqlite` 存在。
   - 用 sqlite3 或 Python 验证至少有 `dim_anime`、`fact_subscription`、`fact_rating` 三张表。
   - 验证每张表都有示例数据。
   - 验证 `sample_data/anime_streaming/reference_sql/` 和 `success_story.csv` 存在。

3. 数据库工具验证
   - 验证 list_tables 能返回表名。
   - 验证 describe_table 能返回字段结构。
   - 验证 execute_sql 能执行 SELECT。
   - 验证 execute_sql 会拒绝 INSERT、UPDATE、DELETE、DROP、CREATE、多语句 SQL。

4. workflow 验证
   - 运行 `--show-workflow`，确认节点顺序是：
     schema_linking -> gen_sql -> execute_sql -> output
   - 检查每个节点是否只读写自己负责的 Context 字段。
   - 检查任一节点失败时 workflow 是否停止，并输出清晰错误。

5. LLM 输出解析验证
   - 检查 GenSqlNode 能解析纯 JSON。
   - 检查 GenSqlNode 能解析包在 markdown code fence 中的 JSON。
   - 检查解析失败时错误信息包含原始模型输出摘要。

6. 端到端验证
   在设置 OPENAI_API_KEY 后，至少尝试这些问题：
   - What is the highest eligible free rate for K-12 students in the dim_anime in Alameda County?
   - Please list the lowest three eligible free rates for students aged 5-17 in continuation dim_anime.
   - What is the phone number of the anime with the highest total watch time?

   每次输出都应包含：
   - question
   - relevant tables
   - sql
   - explanation
   - rows
   - row_count

7. 无 API key 降级验证
   - 未设置 OPENAI_API_KEY 时，程序应给出清晰提示。
   - 不应出现难懂堆栈。

允许修改：

- 可以修复 bug。
- 可以补充最小测试脚本或简单 pytest。
- 可以改善错误信息。
- 可以调整 README 的运行说明。

不允许修改：

- 不要新增 Web/API/MCP。
- 不要引入向量数据库。
- 不要加入自动修复或反思节点。
- 不要把 sample database 的表名硬编码进 SQL 生成逻辑。
- 不要大规模重构项目结构。

输出要求：

1. 先给出验证计划。
2. 执行验证命令。
3. 如果失败，定位原因并做最小修复。
4. 最后输出验证报告，包含：
   - 运行过的命令
   - 通过项
   - 修复项
   - 仍存在的限制
   - 下一轮收尾建议

验收标准：

- sample database 能成功检查并用于查询。
- sample database 使用原项目 `anime_streaming` 数据，而不是临时 mock 数据。
- 数据库工具能独立工作。
- workflow 顺序正确。
- 主链路能在有 API key 时跑通。
- 没有 API key 时错误提示友好。
- 危险 SQL 被拒绝。
- 输出能让用户看懂 SQL、解释和结果。
```
