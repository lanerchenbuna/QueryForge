# QueryForge 第二阶段：Agent Team 深度进化 Prompts

这个文件夹是第四阶段复现交付物。前三阶段分别是：

- `vibe-coding-reproduce/`：基础版，NL2SQL 主链路。
- `vibe-coding-reproduce-upgrade/`：进阶版，多模型、Skills、反思、修复、知识库、可视化、多端入口。
- `vibe-coding-reproduce-agent-team/`：Agent Team 层，路由、编排、角色、状态、artifact。

本阶段的起点是：**Agent Team 已经成为默认编排主链**，不再是可选包装层。所有入口（CLI/API/MCP/Gateway）都经过 EntryRouter → Orchestrator → WorkflowRunner。

## 本阶段目标

让 Agent Team 从"框架层面的编排"进化为"真正能提升准确率和体验的协作系统"。重点是：

1. **填平补齐**：让现有角色和路由真正发挥作用，而不是信息打包。
2. **增强自主性**：从单轮生成走向观察-行动循环。
3. **提升质量**：多候选选择、结构化推理、主题范围控制。
4. **丰富形态**：流式输出、报告交付、更完整的 MCP。

## 原则

1. **不推翻现有架构**：Agent Team 编排 + WorkflowRunner 执行的双层结构保持不变。
2. **安全边界只增不减**：任何新能力都不能绕过 DatabaseTool 和 GovernanceAgent。
3. **每步可验收**：每个 prompt 都有明确的验收标准和测试用例。
4. **可配置可降级**：高级功能默认关闭或有预算限制，简单查询不增加额外成本。

## 阶段划分

### Phase A：填平补齐（4 个 prompt）

让现有的 Agent Team 框架真正立得住。

| # | Prompt | 核心内容 |
|---|--------|---------|
| 01 | 路由落地：sql_review / metadata_query / explain 独立 pipeline | 让 EntryRouter 的分类真正改变执行路径 |
| 02 | 角色升级：ProductAnalyst / SchemaArchitect 决策化 | 从"信息打包"变成"有判断能力" |
| 03 | Artifact 质量门禁与阶段推进 | 每个角色 artifact 有 valid/warning/blocked 三级 |
| 04 | Phase A 集成验收 | 验证填平补齐后的稳定性和一致性 |

### Phase B：增强自主性（3 个 prompt）

从"被动生成"走向"主动观察-行动"。

| # | Prompt | 核心内容 |
|---|--------|---------|
| 05 | Conversation Memory：会话上下文与追问重写 | session_id、结构化记忆、指代消解 |
| 06 | 有界 Tool Loop：观察-行动-再规划 | 工具白名单、预算控制、多轮观察 |
| 07 | Structured Reasoning Node：可审计决策摘要 | 结构化推理、可校验、可复用 |

### Phase C：质量提升（3 个 prompt）

提升准确率和稳定性。

| # | Prompt | 核心内容 |
|---|--------|---------|
| 08 | Parallel + Selection：多候选生成与选择 | 2-3 个候选、规则+执行信号评分 |
| 09 | Subject Tree / Scoped Context：主题范围约束 | 减少 token、降低跨域误用 |
| 10 | Phase B+C 集成验收 | 自主性 + 质量提升的端到端验证 |

### Phase D：形态扩展（3 个 prompt）

丰富交互和交付形态。

| # | Prompt | 核心内容 |
|---|--------|---------|
| 11 | Streaming Output：节点级事件流 | SSE、CLI 进度、事件协议 |
| 12 | Report Artifact：静态 HTML 分析报告 | 多图、表格、口径说明 |
| 13 | MCP 增强：resources / prompts / sessions | 更完整的 MCP 协议支持 |
| 14 | 最终集体验收 | 全链路回归、性能基线、文档收尾 |

## 使用顺序

按编号顺序使用，每个 prompt 完成后再进入下一个。Phase 之间的集成验收 prompt 不要跳过。

## 最终会得到什么

完成全部 prompts 后，QueryForge 从"有 Agent Team 外壳的 NL2SQL 工具"升级为"真正多 Agent 协作的分析系统"：

```text
CLI / API / MCP / Gateway
  -> EntryRouterAgent (任务分类)
  -> OrchestratorAgent (编排协调)
  -> Conversation Memory (会话上下文)
  -> ProductAnalystAgent (需求分析 + 歧义检测)
  -> [Subject Tree 主题范围]
  -> KnowledgeAgent (知识检索)
  -> SchemaArchitectAgent (Schema 规划 + Join 推荐)
  -> [Bounded Tool Loop] (可选，观察-行动循环)
  -> [Parallel Candidates] (可选，多候选生成)
  -> SQLDeveloperAgent (候选注册)
  -> GovernanceAgent (安全治理)
  -> WorkflowRunner (SQL 生成 / 执行 / 反射 / 修复)
  -> DataQAAgent (质量报告)
  -> VisualizationAgent (可视化)
  -> OpsAgent (运维状态)
  -> [Report Artifact] (可选，报告交付)
  -> DeliveryReport
  -> [Streaming] (可选，事件流)
```

它仍然不是企业级数据平台，但已经覆盖了从"单轮 SQL 生成"到"多 Agent 协作分析系统"的关键演进路径。
