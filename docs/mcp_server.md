# Enhanced MCP Server

QueryForge 的 MCP server 是可选传输层，所有 resources、prompts 和 tools 都复用
`AgentService`、Semantic Model、SessionStore 与 `DatabaseTool`。它不拥有独立 SQL 执行或
安全逻辑。

```bash
python -m pip install -r requirements-mcp.txt
python -m queryforge.interfaces.mcp.server --transport stdio
```

## Resources

只读 resources：

| URI | 内容 |
| --- | --- |
| `queryforge://tables` | 已授权表列表 |
| `queryforge://tables/{table_name}` | 表结构和最多 5 行样例 |
| `queryforge://metrics` | 语义模型指标 |
| `queryforge://metrics/{metric_name}` | 单指标定义 |
| `queryforge://history` | 紧凑的成功 SQL 历史 |
| `queryforge://skills` | 本地 Skills |
| `queryforge://subjects` | Subject Tree 主题 |

没有配置 semantic model 或 subject tree 时，相应资源返回空列表。资源不会返回 API Key 或
模型 Prompt；表样例和 SQL preview 都通过共享 SQL policy。

## Prompts

- `queryforge.analyze_data(question, subject)`
- `queryforge.sql_review(sql)`
- `queryforge.troubleshoot(sql, error_message)`
- `queryforge.build_report(question, subject, metrics)`

这些是可复用的客户端模板，不会调用模型或数据库。

## Tools

`ask_sql` 保持兼容，另提供 `list_tables`、`describe_table`、`list_metrics`、`preview_sql`、
`review_sql`、`get_history`、`new_session`、`reset_session`。其中 `preview_sql` 强制经过 AST
policy 并限制 100 行；`review_sql` 调用既有 `sql_review` pipeline。

当 `MCP_SESSION_ENABLED=true` 时，连接内未显式传入 `session_id` 的 `ask_sql` 自动复用
`new_session` 创建的 session。会话数据仍只保存结构化摘要和结果 schema，不保存结果行。

## Configuration

```dotenv
MCP_RESOURCES_ENABLED=true
MCP_PROMPTS_ENABLED=true
MCP_SESSION_ENABLED=true
MCP_HISTORY_LIMIT=20
```

MCP server 默认仅应在受控环境中运行。它没有内置认证、租户隔离或速率限制，禁止直接暴露
到公网。
