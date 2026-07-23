# Prompt 01: 路由落地——让分类真正改变执行路径

复制下面整段给 Codex 使用。

```text
继续上一阶段。当前 QueryForge 的 Agent Team 已经是默认编排主链，EntryRouterAgent 能分类出多种任务类型，但所有分类最终都走同一套 ask_sql 工作流。

本轮目标：让 EntryRouter 的分类真正改变执行路径，至少实现 3 条独立 pipeline：

1. sql_review：SQL 评审 pipeline
2. metadata_query：元数据查询 pipeline
3. explain_result：结果解释 pipeline（复用 ask_sql 的执行，但侧重解释）

## 现状

当前代码位置（请先读取确认）：
- queryforge/agent_team/agents/entry_router.py：EntryRouterAgent，返回 task_type 和 confidence
- queryforge/agent_team/orchestrator/pipeline_registry.py：PIPELINES 字典，目前所有类型都用同一套角色
- queryforge/agent_team/orchestrator/orchestrator.py：OrchestratorAgent.run()，目前统一调 workflow
- queryforge/agent_team/agents/：各角色实现

## 本轮要做的事

### 1. 扩展 pipeline_registry

为不同 task_type 定义不同的 pipeline 阶段。不是所有 pipeline 都要走完整的 SQL 生成-执行流程。

| task_type | pipeline | 说明 |
|-----------|----------|------|
| ask_sql | product_analyst → knowledge → schema_architect → sql_developer → governance → execute_sql → data_qa → visualization → ops → delivery | 现有默认流程 |
| sql_review | product_analyst → schema_architect → sql_developer → governance → review → ops → delivery | 不执行 SQL，只做静态评审 |
| metadata_query | schema_architect → knowledge → delivery | 只返回表结构/指标信息 |
| troubleshoot_sql | product_analyst → schema_architect → sql_developer → governance → execute_sql → data_qa → ops → delivery | 用户提供 SQL，定位并修复错误 |
| explain_result | product_analyst → schema_architect → sql_developer → governance → execute_sql → data_qa → explain → ops → delivery | 执行 SQL 后侧重解释 |
| build_report | product_analyst → knowledge → schema_architect → sql_developer → governance → execute_sql → data_qa → visualization → report → ops → delivery | 生成多图报告 |
| unknown | product_analyst → knowledge → schema_architect → sql_developer → governance → execute_sql → data_qa → visualization → ops → delivery | 回退到默认 ask_sql 行为 |

注意：pipeline 是声明式的阶段列表，实际每个阶段由哪个 Agent 执行、执行什么逻辑，在 orchestrator 中映射。

### 2. 在 OrchestratorAgent 中实现阶段到 Agent 的映射

增加一个 `_execute_phase(state, phase, context, database_tool)` 方法，根据当前 phase 调用对应角色或操作。

关键映射：
- `product_analyst` → ProductAnalystAgent.run()
- `knowledge` → KnowledgeAgent.run()
- `schema_architect` → SchemaArchitectAgent.run()
- `sql_developer` → SQLDeveloperAgent.run()
- `governance` → GovernanceAgent.run()
- `review` → 新增 SQLReviewAgent（或用 Governance + SchemaArchitect 组合）
- `execute_sql` → 这是 WorkflowRunner 的职责，orchestrator 不直接执行
- `data_qa` → DataQAAgent.run()
- `visualization` → VisualizationAgent.run()
- `explain` → 复用 ReflectNode 的解释能力，生成更详细的解释报告
- `report` → 留 stub，标记为 degraded，Phase D 再实现
- `ops` → OpsAgent.run()
- `delivery` → 生成 DeliveryReport

### 3. 实现 SQLReviewAgent（最小可用）

新建 queryforge/agent_team/agents/sql_review.py：

职责：对一段用户提供的 SQL 做静态评审，输出评审报告。

评审维度（复用已有能力，不重新实现）：
- 安全性：用 DatabaseTool.policy_engine 评估
- 语法正确性：用 sqlglot parse 检查
- 性能风险：SELECT *、无 LIMIT、多表 JOIN、全表扫描风险
- 语义风险：是否符合语义模型口径（如果有）
- 可读性：表别名、列命名、格式

输入：用户问题（包含 SQL）、context、database_tool
输出：review_report artifact，包含各维度评分和改进建议

### 4. 实现 metadata_query 的快速路径

metadata_query 不应该走完整的 GenSQL -> Execute 流程，因为用户只是想看表结构/指标信息。

实现方式：
- 在 orchestrator 中检测到 task_type == "metadata_query"
- 直接调用 SchemaArchitectAgent，由它汇总表结构、语义模型实体、指标列表
- KnowledgeAgent 补充相关的历史查询或文档
- 直接生成交付，不调用 WorkflowRunner
- 输出格式：{ "status": "success", "metadata": {...}, "agent_team": {...}, "delivery_report": {...} }

metadata 查询的内容包括：
- 表列表和简要说明
- 指定表的字段列表和类型
- 语义模型中的实体和指标（如果有）
- 常用查询示例（来自历史 SQL）

### 5. 让 troubleshoot_sql 接受用户提供的 SQL

当前 EntryRouter 分类出 troubleshoot_sql，但没有机制接收用户提供的 SQL。

实现方式：
- 在 AgentOptions 中增加可选字段 `provided_sql: str | None = None`
- 如果 provided_sql 不为空且 task_type 是 troubleshoot_sql，则把这段 SQL 作为初始候选
- WorkflowRunner 仍然负责执行和反射修复，但起点是用户的 SQL 而不是 LLM 生成
- 新增一个节点或在 GenSqlNode 中支持"使用提供的 SQL"模式

或者更简单的实现：在 ProductAnalystAgent 中检测问题中的 SQL 片段，提取出来存入 context，后续 SQLDeveloperAgent 直接使用。

请选择更优雅的方案并说明理由。

### 6. 更新 EntryRouter 的分类逻辑

- sql_review：匹配"review sql"、"审核 sql"、"检查 sql"、"sql review"等
- metadata_query：匹配"show tables"、"表结构"、"list tables"、"schema"、"metadata"、"指标列表"等
- troubleshoot_sql：匹配"sql error"、"sql 报错"、"debug sql"、"修复 sql"等，且问题中包含 SQL 片段
- explain_result：匹配"explain"、"解释"、"为什么"、"原因"等
- build_report：匹配"report"、"报告"、"dashboard"、"报表"等
- ask_sql：默认

保持现有基于正则的确定性分类优先，不引入 LLM 分类器。

## 限制

- 不要引入新的 LLM 调用，所有新增 Agent 的评审/解释逻辑优先用规则和已有工具
- SQLReviewAgent 不执行 SQL（只读 AST 和 Schema）
- metadata_query 不调用 LLM 生成 SQL
- 不破坏 ask_sql 的现有行为和测试
- 所有新增 pipeline 都要有对应的 artifact 写入
- 不要新增 agent_team 开关，所有入口都统一走 Orchestrator

## 验收标准

1. `ask_sql` 行为不变，全量测试通过
2. `sql_review` 分类正确，返回 review_report artifact，不执行 SQL
3. `metadata_query` 分类正确，返回表结构和指标信息，不走 GenSQL
4. `troubleshoot_sql` 能从问题中提取 SQL，以用户 SQL 为起点进行修复
5. `explain_result` 返回更详细的解释报告（比默认 explanation 更丰富）
6. `build_report` 返回 degraded 状态的 report artifact（留待 Phase D 实现）
7. 所有 pipeline 都有 state.json 和 delivery_report
8. 新增对应测试：每种 pipeline 至少 2 个测试用例

## 输出要求

1. 先读取现有代码，确认 entry_router、pipeline_registry、orchestrator 的当前实现
2. 给出实现方案和文件变更列表
3. 实现代码
4. 编写测试
5. 运行测试，确保 ask_sql 全绿，新增 pipeline 测试通过
```
