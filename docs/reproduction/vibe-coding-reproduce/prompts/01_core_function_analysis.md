# Prompt 01: 项目核心功能识别

复制下面整段给 Codex 使用。

```text
你现在是我的代码复现助手。我要基于一个名为 Datus-Agent 的项目做 MVP 复现。请先不要写代码，本轮只做核心功能识别和复现范围收敛。

项目背景：

Datus-Agent 是一个 AI-powered SQL Agent。它的核心用户体验是：用户用自然语言提问，系统理解问题和数据库结构，生成 SQL，执行 SQL，并把结果返回给用户。

原项目包含这些主要部分：

1. 多端入口
   - CLI
   - Web
   - REST API
   - MCP Server
   - 飞书/Slack Gateway

2. Agent 与 workflow
   - Agent 接收用户任务
   - WorkflowRunner 创建并执行 workflow
   - Workflow 保存 Context，并按顺序推进 Node
   - 默认 fixed workflow 是：
     schema_linking -> gen_sql -> execute_sql -> output
   - 其他增强 workflow 包含 reflect、date_parser、search_metrics、parallel、selection 等

3. Node 体系
   - SchemaLinkingNode：根据用户问题找到相关表结构
   - GenSQLAgenticNode：基于问题和 schema 生成 SQL
   - ExecuteSQLNode：执行 SQL
   - OutputNode：格式化最终输出
   - FixNode / ReflectNode / ReasoningNode 等用于增强准确率

4. LLM 层
   - 原项目支持多个供应商，例如 OpenAI、Claude、Gemini、DeepSeek、Qwen、Kimi
   - 通过统一模型抽象和 LiteLLM / OpenAI Agents SDK 等方式调用模型

5. 工具系统
   - 数据库工具：list_tables、describe_table、execute_sql 等
   - 语义工具、指标工具、文档搜索工具、MCP 工具、Bash 工具等

6. RAG 与知识库
   - Schema Metadata
   - Reference SQL
   - Semantic Model
   - Metric
   - Document
   - Reference Template
   - Subject Tree

7. 数据库适配
   - 支持 SQLite、DuckDB、PostgreSQL、MySQL、Snowflake、ClickHouse 等

本次目标：

我不是要完整复刻整个项目，而是要做一个最小可复现版本，让不了解原项目的人也能通过代码理解它的主链路。

请你完成以下分析：

1. 用一句话定义 Datus-Agent 的核心价值。
2. 识别最关键用户流程，并用步骤表示。
3. 从原项目中提炼必须保留的 MVP 模块。
4. 明确哪些模块应该省略，并解释为什么省略。
5. 给出 MVP 的边界：它必须能做什么，明确不做什么。
6. 输出一个“原项目模块 -> MVP 模块”的对应表。

输入限制：

- 你只能基于上面的项目描述分析。
- 不要写代码。
- 不要设计复杂基础设施。
- 不要引入 Web、API、MCP、向量数据库、多数据库、多模型 provider。

预期输出：

- 一份结构化分析文档。
- 结论必须收敛到一个简单 MVP：CLI + Workflow + 4 个核心节点 + LLM 调用 + SQLite 数据库工具。
- 最后给出“下一轮设计应该围绕哪些模块展开”的清单，供我复制到下一轮 prompt 使用。

验收标准：

- 能准确识别自然语言转 SQL 是项目主功能。
- 能把 fixed workflow 识别为 MVP 主流程。
- 能明确保留 schema_linking、gen_sql、execute_sql、output。
- 能明确省略非核心增强能力，并说明省略原因。
- 输出内容可以直接作为下一轮最小实现方案设计的上下文。
```
