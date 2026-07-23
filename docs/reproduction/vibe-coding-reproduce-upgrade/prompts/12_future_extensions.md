# Prompt 12: 后续扩展路线

复制下面整段给 Codex 使用。

```text
继续上一轮。现在请基于 QueryForge 进阶版，整理后续扩展路线。

本轮目标：

不要写代码。请从原 Datus-Agent 项目中继续提炼值得学习的 LLM Agent 功能，形成下一阶段路线图。

已完成能力假设：

- 多 LLM provider
- Skills
- DateParserNode
- Plan Mode
- ReflectNode
- FixNode retry
- SQL history cache
- LanceDB vector KB
- Visualization
- Logging
- API / MCP / Gateway

请分析还能继续补充哪些功能，并按优先级排序。

候选方向：

1. Agentic tool loop
   - 让模型可以多轮调用 list_tables、describe_table、execute_sql preview。
   - 对比现在的一次性 prompt 生成 SQL。

2. Parallel + Selection
   - 并行生成多个 SQL 候选。
   - 执行或评估后选择最优。

3. ReasoningNode
   - 输出推理过程。
   - 与 GenSqlNode 分离。

4. MetricToSQL
   - 支持业务指标定义。
   - search_metrics -> date_parser -> gen_sql。

5. Semantic Model
   - 业务实体、字段别名、指标口径。
   - 用于减少字段误用。

6. Subject Tree / scoped context
   - 按主题管理知识。
   - 限制检索范围。

7. Subagent
   - 面向某个领域的定制 Agent。
   - 包含固定 skills、schema scope、model、workflow。

8. Conversation memory
   - 多轮追问。
   - “刚才那个结果按月份拆一下”。

9. Streaming output
   - 节点级进度。
   - LLM token streaming。

10. 权限与安全
   - 表级白名单。
   - SQL AST 检查。
   - 审计日志。

11. 更完整 MCP
   - resources
   - prompts
   - tools
   - session state

12. Dashboard / Report artifact
   - 从单次图表升级为报告和 dashboard。

输出要求：

1. 给出下一阶段路线图。
2. 每个功能说明：
   - 为什么值得做。
   - 依赖哪些已有能力。
   - MVP 版本怎么做。
   - 不建议一开始做哪些复杂部分。
   - 验收标准。
3. 给出推荐顺序。
4. 给出风险清单。
5. 给出可以继续拆成 prompts 的文件清单。

限制：

- 不要写代码。
- 不要把所有功能都标成最高优先级。
- 不要脱离 QueryForge 当前架构。
- 不要为了追求完整而牺牲可运行性。

验收标准：

- 输出能作为下一批 prompt 文件夹的规划。
- 能清晰区分“高学习价值”和“工程复杂但非必要”。
- 能继续沿用增量升级思路。
```
