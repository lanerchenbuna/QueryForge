# Prompt 07: LanceDB 向量知识库

复制下面整段给 Codex 使用。

```text
继续上一轮。现在请在 SQL history cache 的基础上增加可选 LanceDB 向量知识库。

本轮目标：

复现 Datus-Agent 中向量检索的核心思想，但保持范围可控：只把历史 SQL 和 schema 文档向量化，用于辅助 GenSqlNode，不做完整 7 层 RAG。

请先读取当前项目文件，再做增量修改。

实现范围：

1. VectorStore 抽象
   - 定义一个简单接口：
     - add_documents
     - search
     - rebuild
     - stats
   - 先支持 LanceDB。
   - 如果 lancedb 未安装，系统要降级到 SQLite history 检索。

2. LanceDB 后端
   - 存储路径默认 `.queryforge/lancedb`。
   - 建议表：
     - sql_history_vectors
     - schema_doc_vectors
   - 文档字段：
     - id
     - text
     - metadata
     - source_type
     - created_at

3. Embedding
   - 优先使用轻量方式。
   - 可以支持 OpenAI embedding。
   - 如果没有 embedding key，提供清晰提示并禁用向量检索。
   - 不要强依赖大型本地模型。

4. 数据来源
   - 历史 SQL：question + sql + explanation + tables_used。
   - Schema doc：表名 + 字段名 + 字段类型。
   - 动漫平台 reference SQL：`sample_data/anime_streaming/reference_sql/*.sql`。
   - 动漫平台 reference template：`sample_data/anime_streaming/reference_template/*.j2`。
   - 原项目 success story：`sample_data/anime_streaming/success_story.csv`。

5. 检索集成
   - SchemaLinkingNode 可以读取 schema vector matches。
   - GenSqlNode 可以读取 sql history vector matches。
   - prompt 中分开标注：
     - current schema
     - similar historical SQL
     - vector retrieved context

6. CLI
   - `--enable-vector-kb`
   - `--rebuild-vector-kb`
   - `--vector-top-k`
   - `--kb-stats`
   - `--kb-source sample_data/anime_streaming/reference_sql`
   - `--kb-source sample_data/anime_streaming/reference_template`

限制：

- 不实现完整 RAGScope。
- 不实现 Subject Tree。
- 不实现 hybrid rerank。
- 不要求 LanceDB 成为必需依赖。
- LanceDB 失败时不能影响基础 SQL 查询功能。

输出要求：

- 直接修改文件。
- 说明新增依赖。
- 说明降级策略。
- 给出 rebuild 和 search 的验证命令。
- 给出没有 lancedb / 没有 embedding key 时的行为。

验收标准：

- 不启用 vector kb 时，原流程不变。
- 启用后能把历史 SQL 或 schema docs 写入 LanceDB。
- 启用后能把打包的 `reference_sql/`、`reference_template/`、`success_story.csv` 写入 LanceDB。
- 类似问题能从 LanceDB 检索到上下文。
- LanceDB 不可用时自动降级并提示。
```
