# QueryForge 多 Agent 协作层强化 Prompts

这个文件夹是第三阶段复现交付物。它的主体仍然是前两个文件夹逐步复现出的 `QueryForge` 项目：

- `vibe-coding-reproduce/`：基础版，自然语言转 SQL 主链路。
- `vibe-coding-reproduce-upgrade/`：进阶版，多模型、Skills、反思、修复、知识库、可视化、多端入口。

本阶段不是创建一个新的“Agent Team 项目”，也不是替换原来的 SQL Agent workflow，而是在原复现项目之上**新增一个可选的多 Agent 协作层**。这个协作层参考 `dw_agent_team_pkg_副本/dw-agent-team` 的设计思想，用来组织、调度和增强原项目已有能力。

## 目标

目标是让原复现项目保持原有主链路可运行，同时在开启 `--agent-team` 或同等配置时，走多 Agent 协作编排：

```text
用户请求
  -> Entry Router
  -> Orchestrator
  -> 多角色协作层
  -> 复用原有 Workflow / Node / Tool / Knowledge / Visualization
  -> Skills 三层加载
  -> 结构化 Artifacts
  -> HITL Checkpoints
  -> 统一 Delivery
```

核心原则：

1. 原复现项目是主体。
2. `WorkflowRunner / Workflow / Node` 仍是 SQL 执行主链路。
3. 多 Agent 协作层只做路由、编排、分工、质检、治理和交付汇总。
4. 新增能力必须复用已有模块，例如 `GenSqlNode`、`FixNode`、`ReflectNode`、SQL history、LanceDB、Visualization。
5. 必须保留旧模式：不开启多 Agent 时，项目仍按原 workflow 跑通。

## 参考到的关键模式

来自 `dw_agent_team_pkg_副本/dw-agent-team` 的关键设计：

1. Entry Router 只做路由，不做业务决策。
2. Orchestrator 负责分类、编排、上下文管理、质量关卡、最终交付。
3. 专业 Agent 角色边界清晰，例如产品、架构、开发、QA、运维、知识库、治理。
4. Skill 与 Agent 分离：角色定义不等于工作流细节，工作流细节放到 Skill 中。
5. Skills 支持 base + task-specific + platform overlay 的三层加载。
6. 每个阶段输出结构化 artifact，靠 schema 约束交接。
7. Human-in-the-Loop checkpoint 不可绕过。
8. Runtime state 独立持久化，支持恢复、回退、审计。
9. Hook / guard / telemetry 用于安全、恢复和观测。
10. 多 Agent 并行用于知识检索、QA、方案对比，而不是所有任务都串行。

## 适配后的多 Agent 协作层

建议在原项目上新增这些协作角色：

| Agent | 职责 |
| --- | --- |
| EntryRouterAgent | 判断任务类型，路由到 orchestrator 或轻量直接回答 |
| OrchestratorAgent | 管理全局状态、选择流程、调度子 Agent、做交付汇总 |
| ProductAnalystAgent | 把自然语言问题转成结构化分析需求 |
| SchemaArchitectAgent | 判断相关表、字段、join path、指标口径 |
| SQLDeveloperAgent | 生成 SQL、修复 SQL、解释 SQL |
| DataQAAgent | 执行只读验证、检查 SQL 结果是否符合需求 |
| KnowledgeAgent | 查询历史 SQL、schema docs、skills、reference SQL、向量知识库 |
| VisualizationAgent | 判断结果是否适合可视化，生成图表配置 |
| GovernanceAgent | 做 SQL 安全、成本风险、全表扫描风险、敏感字段风险检查 |
| OpsAgent | 管理 API/MCP/Gateway 入口、运行配置、smoke tests |

## 使用顺序

1. `prompts/01_extract_dw_team_patterns.md`
2. `prompts/02_agent_team_architecture.md`
3. `prompts/03_entry_router_and_orchestrator.md`
4. `prompts/04_role_agents.md`
5. `prompts/05_skill_layering_contract.md`
6. `prompts/06_state_artifacts_checkpoints.md`
7. `prompts/07_multi_agent_runtime.md`
8. `prompts/08_governance_and_safety.md`
9. `prompts/09_hooks_observability_recovery.md`
10. `prompts/10_integration_acceptance.md`
11. `prompts/11_future_extensions.md`

辅助阅读：

- `reference/dw_agent_team_patterns.md`
- `reference/queryforge_agent_team_map.md`

## 最终会得到什么

完成本文件夹 prompts 后，原复现项目仍然是一个自然语言转 SQL 项目，只是多了一个可选协作层：

```text
CLI / API / MCP / Gateway
  -> EntryRouterAgent
  -> OrchestratorAgent
  -> KnowledgeAgent 并行检索
  -> ProductAnalystAgent 需求结构化
  -> SchemaArchitectAgent schema / join path
  -> SQLDeveloperAgent 生成 SQL
  -> DataQAAgent 验证结果
  -> GovernanceAgent 安全和成本 gate
  -> VisualizationAgent 可选图表
  -> OpsAgent smoke test / deployment readiness
  -> Delivery Report
```

关闭多 Agent 层时：

```text
CLI / API / MCP / Gateway
  -> WorkflowRunner
  -> 原有 workflow
```

开启多 Agent 层时：

```text
CLI / API / MCP / Gateway
  -> EntryRouterAgent
  -> OrchestratorAgent
  -> 调度多个协作 Agent
  -> 复用原有 WorkflowRunner / Node / Tool
```

它仍然不是完整 DW Agent Team，也不依赖 ByteDance 内部平台命令。它复现的是“在原 SQL Agent 项目上增加多 Agent 协作层”的架构思想：角色边界、Skills 分层、状态机、产物契约、检查点和恢复机制。
