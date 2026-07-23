# Prompt 04: 专业 Role Agents

复制下面整段给 Codex 使用。

```text
继续上一轮。现在请为 QueryForge 的多 Agent 协作层实现专业 Role Agents 的最小可用版本。

本轮目标：

在保留原 ask_sql workflow 主链路的前提下，让多个角色 Agent 围绕它协作并产出 artifacts。实现要轻量，不要一次做成大型框架。

重要约束：

- Role Agents 是原项目的协作层，不是替代原 workflow 的新实现。
- 能复用已有 Node / Store / Tool 的地方必须复用。
- SQL 的最终生成、修复、执行、反思、可视化优先走已有模块。

请先读取当前项目，再做增量修改。

需要实现的 Agent：

1. ProductAnalystAgent
   输入：用户问题、date_context。
   输出：analysis_request.json。
   职责：
   - 提炼用户想要的指标、维度、过滤条件、排序、limit。
   - 不写 SQL。
   - 信息不足时输出 clarification_needed。

2. KnowledgeAgent
   输入：用户问题、analysis_request。
   输出：knowledge_context.json。
   职责：
   - 查询 SQL history。
   - 查询 reference SQL。
   - 查询 LanceDB。
   - 查询本地 Skills 摘要。
   - 可与 ProductAnalystAgent 并行。

3. SchemaArchitectAgent
   输入：analysis_request、knowledge_context、当前数据库 schema。
   输出：schema_plan.json。
   职责：
   - 选择相关表和字段。
   - 推断 join path。
   - 标注可能的指标计算口径。
   - 不生成最终 SQL。

4. SQLDeveloperAgent
   输入：schema_plan、analysis_request、skills_context、knowledge_context。
   输出：sql_candidate.json。
   职责：
   - 优先调用现有 GenSqlNode 逻辑或模型层。
   - 生成 SQL、解释、tables_used。
   - 需要修复时复用 FixNode。

5. GovernanceAgent
   输入：sql_candidate、schema_plan。
   输出：governance_report.json。
   职责：
   - 只读检查。
   - 多语句检查。
   - 全表扫描风险。
   - LIMIT 缺失风险。
   - 敏感字段风险。
   - 高成本 join 风险。

6. DataQAAgent
   输入：sql_candidate、execution_result、analysis_request。
   输出：qa_report.json。
   职责：
   - 校验 row_count。
   - 检查结果列是否回答问题。
   - 检查空结果是否合理。
   - 可以调用 ReflectNode 的模型评估。

7. VisualizationAgent
   输入：execution_result、analysis_request。
   输出：visualization_artifact.json。
   职责：
   - 复用已有 VisualizationNode 或 visualization 规则。
   - 不适合图表时输出 table fallback。

8. OpsAgent
   输入：run state。
   输出：ops_report.json。
   职责：
   - smoke test。
   - API/MCP/Gateway readiness。
   - 配置检查。

限制：

- 每个 Agent 先实现为 Python class，不要独立进程。
- 每个 Agent 必须写 artifact。
- 不要让 ProductAnalyst 直接写 SQL。
- 不要让 SQLDeveloper 跳过 Governance。
- 不要让 QA 修改 SQL；QA 只能建议 retry/fix。
- 不要重写 SQLiteConnector、DatabaseTool、GenSqlNode、ExecuteSqlNode。
- 不要破坏不开启 `--agent-team` 的旧路径。

输出要求：

- 直接修改文件。
- 给出每个 Agent 的输入/输出 artifact。
- 给出 ask_sql pipeline 的新流程。
- 给出验证命令。

验收标准：

- `--agent-team` 下 ask_sql 产生多个 artifact。
- SQL 执行前有 governance_report。
- SQL 执行后有 qa_report。
- 最终 delivery_report 汇总所有 Agent 结果。
```
