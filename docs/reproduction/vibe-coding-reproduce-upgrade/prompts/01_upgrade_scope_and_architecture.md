# Prompt 01: 进阶升级范围与架构重构

复制下面整段给 Codex 使用。

```text
你现在要基于已有的 QueryForge 项目做进阶升级。请先不要写代码，本轮只做升级范围确认和架构方案。

前提：

基础版 QueryForge 已经能完成：

CLI -> WorkflowRunner -> Workflow -> SchemaLinkingNode -> GenSqlNode -> ExecuteSqlNode -> OutputNode

现在我希望在这个基础上逐步接近 Datus-Agent 原项目中的高级能力，包括：

- Skills 系统
- 多 LLM 供应商：OpenAI、Claude、Gemini、DeepSeek、Qwen、GLM
- ReflectNode 自动评估结果
- DateParserNode 自然语言日期解析
- Plan Mode 先审阅再执行
- 历史 SQL 知识库缓存
- 结果可视化
- 完善日志系统
- SQL 修复重试机制
- LanceDB 向量存储
- MCP / API / Gateway 多端入口

本轮目标：

1. 分析当前 QueryForge 架构还缺哪些扩展点。
2. 设计一个“可逐步升级”的模块架构，不要推倒重来。
3. 给出升级路线图和模块依赖顺序。
4. 明确哪些能力先做简化版，哪些能力可以后续再增强。

请输出：

1. 当前基础版架构评估。
2. 新增模块清单和职责：
   - config
   - model provider factory
   - skill registry / skill manager
   - date parser node
   - plan mode
   - reflect node
   - fix node
   - SQL history store
   - vector store
   - visualization
   - logging
   - API / MCP / Gateway adapters
3. 新的目录结构建议。
4. 新的 workflow 图。
5. Context 需要新增的字段。
6. 每个升级阶段的顺序和验收标准。

限制：

- 不要写代码。
- 不要一次性实现全部能力。
- 不要引入生产级权限、认证、多租户。
- 不要把 API / MCP / Gateway 做成独立业务逻辑，它们必须复用同一个 WorkflowRunner。
- 不要为了“像原项目”而引入过度复杂抽象。

验收标准：

- 输出能作为后续 10+ 轮增量开发的总蓝图。
- 能清晰说明为什么先做多模型和 Skills，再做反思、修复、知识库、多端入口。
- 保持 QueryForge 可运行，不要求推倒重构。
```
