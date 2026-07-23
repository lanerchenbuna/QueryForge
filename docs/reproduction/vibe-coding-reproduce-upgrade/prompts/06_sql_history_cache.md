# Prompt 06: 历史 SQL 知识库缓存

复制下面整段给 Codex 使用。

```text
继续上一轮。现在请为 QueryForge 增加历史 SQL 知识库缓存。

本轮目标：

复现 Datus-Agent 中 Reference SQL 的核心思想：把成功执行过的自然语言问题、SQL、解释、表、结果摘要存下来，后续生成 SQL 时可以参考相似历史案例。

请先读取当前项目文件，再做增量修改。

实现范围：

1. SQLHistoryStore
   - 使用 SQLite 存储，不要一开始就用 LanceDB。
   - 建议独立文件：`storage/sql_history_store.py`。
   - 存储字段：
     - id
     - question
     - sql
     - explanation
     - tables_used
     - success
     - error
     - row_count
     - created_at
     - provider
     - model

2. 写入时机
   - OutputNode 成功输出后写入历史。
   - SQL 执行失败也可以写入，但 success=false。
   - 不要写入敏感数据全量结果，只保存摘要。

3. 初始化原项目 success story
   - 支持从 `sample_data/anime_streaming/success_story.csv` 导入历史 SQL。
   - CSV 至少包含 question 和 sql 字段。
   - 如果 CSV 中还有 evidence、expected_table、expected_knowledge 等字段，可以保存到 metadata。
   - 增加 CLI：
     - `--import-success-stories sample_data/anime_streaming/success_story.csv`
   - 导入时去重，避免重复写入同一个 question + sql。

4. 导入 reference SQL
   - 支持读取 `sample_data/anime_streaming/reference_sql/*.sql`。
   - SQL 文件中注释行可以作为 question / evidence。
   - SQL 语句作为 reference_sql 保存。
   - 这个导入功能可以先做简单解析，不要求完美。

5. 检索方式
   - MVP 先实现关键词检索和简单相似度。
   - 输入当前 question，返回 top_k 历史 SQL。
   - 支持按 tables_used 过滤。

6. GenSqlNode 集成
   - 在生成 SQL prompt 中注入历史 SQL 示例。
   - 明确告诉模型：历史 SQL 是参考，不可盲目照抄。
   - 如果 schema 不匹配，必须以当前 schema 为准。

7. CLI
   - `--history-top-k`
   - `--show-history`
   - `--clear-history` 可选，需要二次确认或明确参数。
   - `--import-success-stories`
   - `--import-reference-sql`

8. 配置
   - HISTORY_DB_PATH 默认 `.queryforge/history.db`。

限制：

- 不保存完整查询结果。
- 不引入向量库，本轮只做 SQLite 缓存。
- 不做多用户权限。
- 不做复杂相似度算法。

输出要求：

- 直接修改文件。
- 给出历史表结构说明。
- 给出检索策略说明。
- 给出 prompt 注入示例。
- 给出验证命令。

验收标准：

- 成功查询后会写入历史 SQL。
- 可以从 `sample_data/anime_streaming/success_story.csv` 导入动漫平台成功案例。
- 可以从 `sample_data/anime_streaming/reference_sql/` 导入参考 SQL。
- 后续类似问题能检索到历史 SQL。
- GenSqlNode 能把历史案例纳入 prompt。
- 用户可以查看历史记录。
- 历史库不存在时自动创建。
```
