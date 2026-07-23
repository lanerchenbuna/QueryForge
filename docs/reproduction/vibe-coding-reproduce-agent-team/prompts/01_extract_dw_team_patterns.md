# Prompt 01: 提炼 DW Agent Team 多 Agent 模式

复制下面整段给 Codex 使用。

```text
你现在要继续强化已有 QueryForge 项目。前两阶段已经实现了自然语言转 SQL、Skills、多模型、反思、修复、历史 SQL、LanceDB、可视化、日志和多端入口。

重要定位：

本阶段必须以已有 QueryForge 为主体。不要创建新项目，不要重命名项目，不要替换原来的 WorkflowRunner / Workflow / Node 主链路。我们只是借鉴 DW Agent Team 的多 Agent 协作思想，给原复现项目增加一个可选的多 Agent 协作层。

本阶段参考一个新的项目：DW Agent Team。请先不要写代码，本轮只做架构模式提炼。

DW Agent Team 的关键特征：

1. Entry Router 只做路由，不做业务执行。
2. Orchestrator 负责分类、编排、上下文管理、质量关卡和最终交付。
3. 多个专业 Agent 分工：
   - Product Manager
   - Architect
   - Developer
   - QA
   - Ops
   - Knowledge Base
   - Governance
4. Agent 与 Skill 分离。
5. Skills 使用三层加载：
   - base skill
   - task-specific skill
   - platform/datasource overlay skill
6. 每个阶段产出结构化 artifact，并通过 schema 约束。
7. 有 task_state，记录 current_phase、completed_phases、artifacts、retry_counts、checkpoints、blocked reason。
8. 有 Human-in-the-Loop checkpoints，不能绕过关键确认。
9. 有 hooks / guard / telemetry / recovery 机制。
10. 支持并行调用知识库、QA、治理等子 Agent。

请基于以上信息，完成以下分析：

1. 哪些模式适合迁移到 QueryForge。
2. 哪些模式不适合迁移，原因是什么。
3. QueryForge 应该新增哪些 Agent 角色。
4. QueryForge 应该新增哪些 Skills 分层。
5. QueryForge 在保留原 Context 的前提下，应该新增哪些 artifact schema。
6. QueryForge 在保留原 run/context 逻辑的前提下，task_state 应该如何设计。
7. 哪些场景需要 HITL checkpoint。
8. 多 Agent 架构与现有 Workflow/Node 架构如何共存。

限制：

- 不要写代码。
- 不要照搬 DW Agent Team 的内部平台命令。
- 不要引入 bytedcli、Dorado、Meego、飞书真实集成。
- 不要推倒现有 QueryForge 架构。
- 不要让多 Agent 层重写已有 GenSqlNode、ExecuteSqlNode、ReflectNode、FixNode、VisualizationNode。
- 不开启多 Agent 层时，原项目必须仍然能按旧 workflow 跑通。

输出要求：

- 一份结构化架构分析。
- 最后给出下一轮“在原项目上新增多 Agent 协作层”的输入摘要。

验收标准：

- 能清晰解释 DW Agent Team 的核心架构价值。
- 能把它迁移成适合 SQL Agent 的本地可复现方案。
- 能区分“架构思想”与“内部平台绑定实现”。
```
