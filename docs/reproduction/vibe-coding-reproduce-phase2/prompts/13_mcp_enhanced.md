# Prompt 13: MCP 增强——resources / prompts / sessions

复制下面整段给 Codex 使用。

```text
继续上一轮。Report Artifact 已经实现。

本轮目标：增强 MCP（Model Context Protocol）支持，从"只有 ask_sql 工具"升级为完整的 MCP server。

## 背景

当前 MCP server 只有基本的 ask_sql 工具。完整的 MCP 应该包含：
- Resources：可读取的数据源（表结构、指标、历史查询等）
- Prompts：可复用的 prompt 模板
- Tools：可执行的操作（已有 ask_sql，需要补充）
- Sessions：会话管理

## 本轮要做的事

### 1. MCP Resources

新增以下 resources：

| Resource URI | 描述 | 内容 |
|-------------|------|------|
| `queryforge://tables` | 所有表列表 | 表名 + 简要描述 |
| `queryforge://tables/{table_name}` | 单表详情 | 字段、类型、主键、外键、样例行 |
| `queryforge://metrics` | 所有指标列表 | 指标名、描述、所属实体 |
| `queryforge://metrics/{metric_name}` | 单指标详情 | 指标定义、表达式、允许维度 |
| `queryforge://history` | 历史查询列表 | 最近 N 条成功查询 |
| `queryforge://skills` | 可用 Skills 列表 | Skill 名、描述、作用范围 |
| `queryforge://subjects` | 主题列表 | Subject id、名称、描述 |

Resources 是只读的，供 MCP client 浏览和引用。

### 2. MCP Prompts

新增以下 prompts：

| Prompt Name | 描述 | 参数 |
|------------|------|------|
| `queryforge.analyze_data` | 数据分析模板 | question, subject |
| `queryforge.sql_review` | SQL 评审模板 | sql |
| `queryforge.troubleshoot` | SQL 排错模板 | sql, error_message |
| `queryforge.build_report` | 报告生成模板 | question, subject, metrics |

Prompts 是可复用的模板，MCP client 可以加载后填充参数。

### 3. MCP Tools 增强

已有工具：
- `ask_sql`：执行自然语言查询

新增工具：
- `list_tables`：列出所有表
- `describe_table`：查看表结构
- `list_metrics`：列出所有指标
- `preview_sql`：预览 SQL 结果（带 LIMIT）
- `review_sql`：评审 SQL
- `get_history`：获取历史查询
- `new_session`：创建新会话
- `reset_session`：重置当前会话

注意：所有工具都复用已有的能力，不重新实现。

### 4. Session 支持

MCP server 增加会话支持：
- 每个 MCP 连接维护一个 session
- 连续的查询自动使用同一会话
- 提供 new_session 和 reset_session 工具

Session 复用 Phase B 实现的 Conversation Memory。

### 5. MCP Server 重构

当前 MCP server 可能是在一个单独的文件中，需要重构以支持 resources 和 prompts。

重构要点：
- 使用 MCP SDK 的 resources 和 prompts API
- 所有工具/资源/prompt 都复用 QueryForge 的核心能力
- 保持向后兼容：ask_sql 工具的行为不变
- 错误处理统一

### 6. 配置

配置项：
- `mcp_resources_enabled: bool`，默认 true
- `mcp_prompts_enabled: bool`，默认 true
- `mcp_session_enabled: bool`，默认 true
- `mcp_history_limit: int`，默认 20

### 7. 测试策略

MCP 的测试比较特殊，需要 MCP SDK 的测试支持。

测试内容：
- 每个 resource 都能正确返回数据
- 每个 prompt 都能正确加载
- 每个工具都能正常调用
- session 能正确维护上下文
- 错误处理正确

## 限制

- 所有 MCP 能力都复用已有核心能力，不重新实现
- 不改变核心业务逻辑
- 向后兼容：旧的 ask_sql 工具行为不变
- 安全策略仍然生效：MCP 的工具调用也经过 Governance
- 不引入新的依赖（MCP SDK 已有）

## 验收标准

1. MCP server 提供至少 5 种 resources
2. MCP server 提供至少 3 种 prompts
3. MCP server 提供至少 5 种 tools
4. session 能正确维护上下文
5. 所有 MCP 能力都复用已有核心逻辑
6. 向后兼容：ask_sql 行为不变
7. 安全策略在 MCP 中也生效
8. 新增测试覆盖：resources、prompts、tools、session
9. ask_sql 全量测试通过

## 输出要求

1. 先读取现有代码，确认 MCP server 的当前实现
2. 给出 MCP 增强设计和资源/prompt/工具列表
3. 实现代码
4. 编写测试
5. 运行测试，确保全量通过
```
