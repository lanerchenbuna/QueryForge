# Prompt 11: 多 Agent Team 后续扩展路线

复制下面整段给 Codex 使用。

```text
继续上一轮。现在请为 QueryForge 的多 Agent 协作层设计后续扩展路线。本轮不写代码。

已完成能力假设：

- EntryRouterAgent
- OrchestratorAgent
- Role Agents
- Skills 三层加载
- State / Artifacts / Checkpoints
- AgentRuntime 并行和 handoff
- Governance gate
- Hooks / observability / recovery

请基于当前项目，规划下一阶段可以继续增强的能力。

候选方向：

1. 真正的 Agentic Tool Loop
   - SQLDeveloperAgent 可以多轮调用 list_tables、describe_table、execute_sql_preview。

2. Multi-candidate SQL
   - 多个 SQLDeveloperAgent 生成候选。
   - QA/Governance/Execution 评分后 selection。

3. Specialist Subagents
   - 针对 finance、sales、education 等领域创建专属 schema scope + skills。

4. Team Memory
   - Orchestrator 记住用户偏好、业务规则、常用数据库。

5. Knowledge Writing
   - 成功案例沉淀到 reference SQL。
   - 用户说“记住”时写入本地 knowledge。

6. Conversation Follow-up
   - 支持“刚才那个结果按县分组”。
   - 复用上一次 run artifacts。

7. Report Agent
   - 从单图升级为多段分析报告。

8. Benchmark Harness
   - 使用 tables/*.csv 的 gold_sql 做自动评测。

9. Agent Evaluation
   - 对 ProductAnalyst、SchemaArchitect、SQLDeveloper、QA 各自打分。

10. UI
   - Web 页面展示 pipeline、state、artifacts、trace。

11. 更完整 MCP
   - 暴露 agent pipeline tools。
   - 暴露 artifacts resources。

12. Deployment Profile
   - dev / demo / production 三套配置。

输出要求：

1. 按优先级排序。
2. 每个方向说明：
   - 为什么值得做。
   - 依赖哪些现有模块。
   - MVP 怎么做。
   - 不建议一开始做什么。
   - 验收标准。
3. 给出下一批 prompts 的文件清单。
4. 给出风险清单。

限制：

- 不要写代码。
- 不要把所有功能都标成 P0。
- 不要脱离 QueryForge 当前架构。
- 不要引入 DW Agent Team 内部平台依赖。
- 不要把后续扩展设计成新项目；必须继续以原复现项目为主体。
- 不要规划会替换原 workflow 主链路的重构，除非明确说明兼容迁移路径。

验收标准：

- 输出能作为下一轮 prompt 文件夹规划。
- 能继续保持增量演进。
- 能区分高学习价值和工程复杂度。
```
