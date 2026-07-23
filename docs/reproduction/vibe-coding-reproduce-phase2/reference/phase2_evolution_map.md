# QueryForge 第二阶段演进路径图

## 前三阶段回顾

### Phase 0: 基础版（vibe-coding-reproduce）
```
CLI -> WorkflowRunner -> SchemaLinking -> GenSQL -> ExecuteSQL -> Output
```
核心能力：自然语言转 SQL 的基础链路。

### Phase 1: 进阶版（vibe-coding-reproduce-upgrade）
```
CLI / API / MCP / Gateway
  -> DateParser
  -> SchemaLinking + Skills + History + VectorStore
  -> GenSQL
  -> Plan Mode (可选)
  -> ExecuteSQL
  -> Reflect -> Fix/Regenerate (循环)
  -> Output
  -> Visualization (可选)
  -> SQL History Cache
```
核心能力：多模型、Skills、反思修复、知识库、可视化、多端入口。

### Phase 2: Agent Team 层（vibe-coding-reproduce-agent-team）
```
CLI / API / MCP / Gateway
  -> EntryRouterAgent (路由分类)
  -> OrchestratorAgent (编排协调)
  -> Analysis Hook: ProductAnalyst + Knowledge + SchemaArchitect
  -> WorkflowRunner (SQL 生成/执行/反射/修复)
  -> Candidate Hook: SQLDeveloper + Governance
  -> Completion Hook: DataQA + Visualization + Ops
  -> DeliveryReport
```
核心能力：路由编排、角色协作、状态持久化、artifact 追踪。

---

## 本阶段（Phase 3: 深度进化）目标

从"框架层面的编排"进化为"真正能提升准确率和体验的协作系统"。

### Phase 3-A: 填平补齐

```
EntryRouter
  ├── ask_sql ────────┐
  ├── sql_review ────┤
  ├── metadata_query ─┤→ 各走各的 pipeline
  ├── troubleshoot ───┤
  ├── explain ────────┤
  ├── build_report ───┘
  └── unknown ────────→ 回退到 ask_sql

每个 pipeline 的角色都是有决策能力的，不是信息打包：
  ProductAnalyst: 需求澄清检测、歧义识别、指标映射
  SchemaArchitect: 表选择排序、Join Path 推荐、风险评估
```

新增：质量门禁
```
analysis gate → schema gate → sql_candidate gate → execution gate → qa gate
  (blocked 终止，warning 记录但继续)
```

### Phase 3-B: 增强自主性

```
Conversation Memory:
  Session → ProductAnalyst 追问重写 → 完整问题 → 正常流程

Bounded Tool Loop:
  SchemaArchitect → Tool Loop (list/describe/preview) → GenSQL
  (最多 5 轮，严格只读，预算可控)

Structured Reasoning:
  GenSQL 同时输出 SQL + 结构化推理摘要
  推理摘要可审计、可校验、可复用
```

### Phase 3-C: 质量提升

```
Parallel + Selection:
  GenSQL → 2-3 个候选 → Selector (规则+执行信号评分) → 最优 SQL → 执行
  (默认 1 个，可配置)

Subject Tree:
  ProductAnalyst → Subject 选择 → 裁剪 Schema/Skills/History → GenSQL
  (选错有 fallback，自动扩大范围重试)
```

### Phase 3-D: 形态扩展

```
Streaming Output:
  EventBus → SSE / CLI 进度 / MCP 事件流
  (只传进度事件，不传结果数据)

Report Artifact:
  Result → DataQA → Visualization → ReportGenerator → HTML 报告
  (多图、表格、口径说明、关键发现)

MCP 增强:
  Resources: tables / metrics / history / skills / subjects
  Prompts: analyze / review / troubleshoot / build_report
  Tools: ask_sql / list_tables / describe / preview / review / sessions
  Sessions: 会话上下文持久化
```

---

## 最终形态

```
CLI / API / MCP / Gateway
  ├─ [Streaming EventBus] （可选，进度推送）
  │
  → EntryRouterAgent (任务分类)
  → OrchestratorAgent (编排协调 + 质量门禁)
  │
  ├─ [Session Memory] （可选，会话上下文）
  ├─ [Subject Tree] （可选，主题范围约束）
  │
  → Analysis Phase:
  │   ├─ ProductAnalystAgent (需求分析 + 歧义检测 + 追问重写)
  │   ├─ KnowledgeAgent (知识检索 + 排序去重)
  │   └─ SchemaArchitectAgent (Schema 规划 + Join 推荐 + 风险)
  │
  → [Tool Loop Phase] （可选，观察-行动循环）
  │   ├─ list_tables
  │   ├─ describe_table
  │   ├─ preview_distinct_values
  │   └─ execute_sql_preview
  │
  → SQL Candidate Phase:
  │   ├─ [Parallel Candidates] （可选，多候选生成）
  │   ├─ SQLDeveloperAgent (候选注册 + 结构化推理)
  │   └─ GovernanceAgent (安全治理 + 策略验证)
  │
  → Execution Phase:
  │   └─ WorkflowRunner (Plan Mode → Execute → Reflect → Fix/Regenerate)
  │
  → Completion Phase:
  │   ├─ DataQAAgent (质量报告 + 一致性检查)
  │   ├─ VisualizationAgent (图表生成)
  │   ├─ [Report Agent] （可选，完整 HTML 报告）
  │   └─ OpsAgent (运维状态 + 健康检查)
  │
  → DeliveryReport
  │
  └─ [Artifacts] （持久化：state.json + 各角色 artifact）
```

---

## 能力演进阶梯

| 层级 | 能力 | 关键词 |
|-----|------|--------|
| L0 | 基础 NL2SQL | 单轮、单 SQL、无反思 |
| L1 | 反思修复 | Reflect + Fix + Regenerate |
| L2 | 知识增强 | Skills + History + VectorStore |
| L3 | Agent 编排 | EntryRouter + Orchestrator + Roles |
| L4 | 路由分化 | 多 pipeline + 角色决策化 |
| L5 | 对话能力 | Session Memory + 追问重写 |
| L6 | 自主观察 | Tool Loop + 结构化推理 |
| L7 | 质量提升 | 多候选选择 + 主题约束 |
| L8 | 形态扩展 | Streaming + Report + 完整 MCP |

本阶段完成后，QueryForge 达到 L7-L8 水平。
