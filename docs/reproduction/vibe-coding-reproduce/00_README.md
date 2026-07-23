# Datus-Agent MVP 复现 Prompts 交付物

这个文件夹是一套面向 vibe coding 的复现指引。目标不是完整重建 Datus-Agent，而是把它最核心、最有代表性的主功能提炼成一个低门槛 MVP：用户输入自然语言问题，程序识别相关数据表，调用 LLM 生成 SQL，执行 SQL，并返回 SQL、解释和结果。

## 如何使用

按顺序把 `prompts/` 下的文件逐个复制给 Codex。每一轮只推进一个阶段，不要跳步。每轮结束后，把 Codex 的输出保留在当前对话里，下一轮 prompt 会显式要求 Codex 基于上一轮结果继续。

建议顺序：

1. `prompts/01_core_function_analysis.md`
2. `prompts/02_minimal_design.md`
3. `prompts/03_init_project.md`
4. `prompts/04_core_development.md`
5. `prompts/05_integration_validation.md`
6. `prompts/06_fix_and_wrap_up.md`

辅助阅读：

- `workflow_overview.md`：用图说明原项目 workflow 和 MVP workflow 的对应关系。
- 根目录 `sample_data/anime_streaming/README.md`：说明当前项目统一维护的示例数据、表结构和推荐测试问题。

## 为什么这样拆分

Datus-Agent 原项目很大，包含 CLI、Web、API、MCP、Gateway、多模型适配、RAG 知识库、多数据库连接器、工作流编排、节点系统、工具系统、权限、观测和插件等能力。直接让 Codex “复刻整个项目”会导致范围失控、实现路线发散、依赖过多。

这套 prompt 按软件实现路径拆成六步：

1. 先让 Codex 识别项目核心，避免把非核心功能带进 MVP。
2. 再确定最小方案，把复杂架构压缩成可运行主链路。
3. 初始化目录和配置，让后续开发有稳定落点。
4. 开发核心模块，只实现 workflow、node、LLM、DB tool、SQLite connector。
5. 做端到端联调，确保自然语言到 SQL 的完整闭环能跑通。
6. 处理常见问题，收尾成一个可交付、可演示、可继续扩展的最小版本。

## 被认定为项目主要功能的内容

主要功能是“自然语言转 SQL 的智能数据库代理”。对应原项目中的核心链路是：

```text
用户问题
  -> WorkflowRunner 创建 Workflow
  -> Workflow 按顺序执行 Node
  -> SchemaLinkingNode 找到相关表结构
  -> GenSQLAgenticNode / GenSqlNode 调用 LLM 生成 SQL
  -> ExecuteSQLNode 执行 SQL
  -> OutputNode 整理输出
```

MVP 必须保留的模块：

- CLI 入口：接收用户问题和数据库路径。
- WorkflowRunner：负责创建并启动 workflow。
- Workflow：保存上下文，按顺序推进节点。
- Node 体系：统一节点接口，至少包含 schema linking、SQL generation、SQL execution、output。
- LLM 层：封装一次文本生成调用。
- 数据库工具层：提供 list tables、describe table、execute SQL。
- SQLite connector：用最少依赖完成真实数据库查询。
- Context 数据模型：承接节点之间的数据流。

## 被刻意省略的内容

这些内容在原项目中重要，但不是最小复现主功能所必需：

| 省略内容 | 省略原因 |
| --- | --- |
| Web / REST API / MCP / 飞书 Slack Gateway | 多端入口会增加大量框架代码，CLI 足够验证主链路 |
| LanceDB 和 7 层 RAG 知识库 | 需要向量库、embedding、索引构建，MVP 可用全表 schema fallback 代替 |
| 多数据库适配器 | SQLite 内置、零服务依赖，足够演示 SQL 生成和执行 |
| 多模型 provider 和 LiteLLM 适配 | MVP 只保留一个 OpenAI-compatible 调用入口 |
| OpenAI Agents SDK tool loop | 原项目核心但实现复杂，MVP 用“schema 入 prompt + 生成 JSON SQL”复现主要效果 |
| 并行、反思、自动修复、selection | 属于准确率增强功能，先保证主链路可运行 |
| 权限、审计、观测、trace、checkpoint | 工程化能力，不影响最小主功能复现 |
| Subject Tree、RAGScope、Subagent | 领域化和上下文隔离能力，MVP 阶段不引入 |

## 最终 MVP 预期效果

使用者跟随 prompts 与 Codex 连续交互后，应得到一个 `QueryForge` 项目。它能完成：

```bash
python main.py \
  --database sample_data/anime_streaming/anime_streaming.sqlite \
  --question "What is the phone number of the anime with the highest total watch time?"
```

预期输出包含：

- 用户问题
- 识别到的表结构摘要
- LLM 生成的 SQL
- SQL 解释
- 查询结果表格或 JSON
- 出错时的清晰错误信息

## 示例数据

复现资料本身不再重复打包样例数据。当前项目统一使用根目录下的 `anime_streaming` 示例数据：

```text
sample_data/anime_streaming/
  anime_streaming.sqlite
  tables/*.csv
  success_story.csv
  reference_sql/
  reference_template/
```

基础版复现项目应优先使用 `anime_streaming.sqlite` 做试运行，而不是只创建临时的 users/anime/watch sessions mock 数据。这样可以更接近原项目的真实 SQL 生成场景，包括多表 join、带空格的字段名、指标口径和参考 SQL。

## 与原项目的关键对应关系

| 原项目模块 | MVP 对应模块 | 保留的思想 |
| --- | --- | --- |
| `datus/cli/main.py` | `main.py` | CLI 作为最小入口 |
| `datus/agent/workflow_runner.py` | `agent/workflow_runner.py` | 负责 workflow 生命周期 |
| `datus/agent/workflow.py` | `agent/workflow.py` | 保存 context 并推进节点 |
| `datus/agent/node/*` | `agent/node/*` | 节点化执行单元 |
| `datus/tools/func_tool/database.py` | `tools/database_tool.py` | 数据库工具供生成与执行使用 |
| `datus/models/*` | `models/llm.py` | 封装模型调用 |
| `datus/storage/schema_metadata/*` | schema fallback | 为 SQL 生成提供表结构上下文 |

这套交付物强调可执行性和最简单复现路径。prompt 本身不直接贴实现代码，而是提供足够具体的结构、接口、流程、限制和验收标准，让 Codex 在每一轮中生成或修改代码。
