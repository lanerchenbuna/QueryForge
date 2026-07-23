# Datus-Agent 进阶复现 Prompts

这个文件夹用于升级基础版 `QueryForge`。基础版只复现了 Datus-Agent 的 fixed workflow：

```text
schema_linking -> gen_sql -> execute_sql -> output
```

进阶版的目标是在基础功能跑通后，继续复现原项目中更有代表性的 LLM Agent 能力，但仍然坚持“可执行、可分阶段、不过度工程化”。

## 使用前提

请先完成基础版交付物：

- `vibe-coding-reproduce/`
- 得到一个可运行的 `QueryForge/`
- 已经能完成：自然语言问题 -> schema linking -> SQL generation -> SQL execution -> output

本文件夹中的 prompts 假设你已经有这个基础项目。每个 prompt 都要求 Codex 先读取现有代码，再做增量修改。

## 本次增强范围

优先增强这些能力：

1. Skills 系统
2. 多 LLM 供应商：OpenAI、Claude、Gemini、DeepSeek、Qwen、GLM
3. ReflectNode：自动评估 SQL 和结果
4. DateParserNode：解析自然语言日期
5. Plan Mode：先审阅计划，再执行 SQL
6. 知识库缓存：存储历史 SQL、可复用成功查询
7. 结果可视化：简单图表
8. 完善日志系统
9. SQL 修复重试机制
10. LanceDB 向量存储
11. MCP / API / Gateway 多端入口

## 推荐使用顺序

1. `prompts/01_upgrade_scope_and_architecture.md`
2. `prompts/02_config_and_multi_llm.md`
3. `prompts/03_skills_system.md`
4. `prompts/04_date_parser_and_plan_mode.md`
5. `prompts/05_reflect_and_sql_fix_retry.md`
6. `prompts/06_sql_history_cache.md`
7. `prompts/07_lancedb_vector_kb.md`
8. `prompts/08_result_visualization.md`
9. `prompts/09_logging_and_observability.md`
10. `prompts/10_api_mcp_gateway.md`
11. `prompts/11_integration_acceptance.md`
12. `prompts/12_future_extensions.md`

辅助文档：

- `upgrade_workflow_map.md`：展示基础版、进阶版和原项目 workflow 的映射关系。
- 根目录 `sample_data/`：当前项目统一维护的 `anime_streaming` 和 `anime_streaming` 示例数据。

## 为什么仍然分阶段

这些能力彼此有依赖关系：

- 多 LLM 供应商是 ReflectNode、FixNode、Plan Mode 的基础。
- Skills 系统会改变 prompt 注入和工具边界。
- DateParserNode 会影响 GenSqlNode 的输入上下文。
- ReflectNode 和 FixNode 会改变 workflow 控制流。
- 历史 SQL 缓存和 LanceDB 会影响 schema linking / SQL generation 的上下文。
- API / MCP / Gateway 应该复用同一个 WorkflowRunner，不能复制业务逻辑。

所以升级 prompts 按“底座 -> 能力 -> 存储 -> 入口 -> 验收”的顺序组织。

## 仍然不建议一次性完整照搬的内容

原项目是企业级工程。进阶版可以更接近原项目，但仍不建议一次性复刻：

- 完整权限系统
- 完整 marketplace skill 分发
- 完整 OpenAI Agents SDK tool loop
- 复杂 BI / Dashboard / Report artifact 体系
- 多租户认证
- 生产级 Gateway 回调签名
- 复杂调度器和后台任务系统

这些可以放在最后的 future extensions 中继续扩展。

## 最终目标

完成全部 prompts 后，`QueryForge` 应该从一个“自然语言转 SQL demo”升级为一个中阶 SQL Agent：

```text
CLI / API / MCP / Gateway
  -> Plan Mode 可选
  -> DateParserNode 可选
  -> SchemaLinkingNode
  -> GenSqlNode
  -> ExecuteSqlNode
  -> ReflectNode
  -> FixNode retry 可选
  -> OutputNode
  -> Visualization 可选
  -> SQL History Cache / LanceDB KB
```

它仍然不是完整 Datus-Agent，但已经覆盖了原项目中最有学习价值的 Agent 架构思想。

## 示例数据

复现资料本身不再重复打包样例数据。当前项目统一使用根目录下的示例数据：

```text
sample_data/anime_streaming/
  anime_streaming.sqlite
  tables/*.csv
  success_story.csv
  reference_sql/
  reference_template/
sample_data/anime_streaming/
```

建议用途：

- `anime_streaming.sqlite`：所有 CLI/API/MCP/Gateway 端到端查询的主数据库。
- `success_story.csv`：初始化 SQL history cache。
- `reference_sql/`：初始化 Reference SQL 知识库或 LanceDB 文档。
- `reference_template/`：作为 SQL 模板和 Skills 示例。
- `anime_streaming/`：用于语义模型、SQL policy 和多实体数仓测试。
