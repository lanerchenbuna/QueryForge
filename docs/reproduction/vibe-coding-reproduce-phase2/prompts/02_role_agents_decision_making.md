# Prompt 02: 角色升级——ProductAnalyst / SchemaArchitect 决策化

复制下面整段给 Codex 使用。

```text
继续上一轮。当前 Agent Team 的角色中，ProductAnalystAgent 和 SchemaArchitectAgent 基本上是把 Context 里已有的信息重新打包成 artifact，没有独立的决策能力。

本轮目标：让这两个核心角色从"信息打包"升级为"有判断能力"，真正为后续 SQL 生成提供价值。

## 现状（请先读取确认）

- queryforge/agent_team/agents/product_analyst.py：当前实现
- queryforge/agent_team/agents/schema_architect.py：当前实现
- queryforge/schemas/models.py：Context 模型，看看有哪些字段
- queryforge/agent/node/metric_search_node.py：指标搜索逻辑
- queryforge/semantic/model.py：语义模型和匹配逻辑

## 本轮要做的事

### 1. ProductAnalystAgent 升级

当前 ProductAnalystAgent 只是用正则提取一些关键词。升级后应该做真正的需求分析：

#### 1.1 需求澄清检测

检测问题中的模糊点和缺失信息，输出 clarification_needed 列表：

```python
{
  "artifact_type": "analysis_request",
  "status": "valid" | "warning" | "blocked",
  "goal": "用户的分析目标",
  "metrics": ["识别到的指标列表"],
  "dimensions": ["识别到的维度/分组字段"],
  "filters": ["识别到的过滤条件"],
  "sort_by": ["排序字段"],
  "limit": 数量或 null,
  "grain": "分析粒度",
  "time_range": "时间范围或 null",
  "clarification_needed": [
    {
      "aspect": "指标口径不明确",
      "question": "用户说'销售额'，是指订单金额还是实收金额？",
      "severity": "low" | "medium" | "high"
    }
  ],
  "ambiguities": ["其他歧义点"],
  "assumptions": ["做出的假设"]
}
```

检测规则（不用 LLM，用规则实现）：
- 指标口径不明确：问题中提到"销售额"、"收入"但语义模型中有多个相关指标
- 维度缺失：提到了"排名"但没说按什么维度排
- 时间范围缺失：涉及趋势、增长但没有时间范围
- 粒度模糊：说"用户数"但没说明是去重用户还是订单用户
- 比较基准缺失：提到"增长"、"同比"但没说和什么比

#### 1.2 指标和维度映射

如果有语义模型，ProductAnalystAgent 应该：
- 主动把用户提到的业务术语映射到语义模型的指标/维度
- 检测用户提到的指标是否在 allowed_dimensions 内
- 推荐最合适的指标，而不是等 MetricSearchNode 去匹配

注意：ProductAnalystAgent 不替代 MetricSearchNode，而是提前做业务层面的分析和建议。MetricSearchNode 仍然做精确的结构化匹配。

#### 1.3 artifact 状态三级制

analysis_request 的 status 字段：
- `valid`: 需求清晰，可以直接生成 SQL
- `warning`: 有一些模糊点，但做了合理假设，可以继续
- `blocked`: 关键信息缺失，无法继续（比如完全不知道用户要查什么）

blocked 状态会终止流程（由 orchestrator 判断）。

### 2. SchemaArchitectAgent 升级

当前 SchemaArchitectAgent 只是汇总已有信息。升级后应该做真正的 Schema 规划：

#### 2.1 表选择和优先级排序

不只是把所有相关表列出来，还要：
- 按相关性排序
- 标注每张表的角色（事实表、维度表、参考表）
- 标注哪些表是必须的，哪些是可选的
- 检测是否遗漏了关键表（比如用户提到"销售额"但没选到订单表）

#### 2.2 Join Path 推荐和风险评估

如果有语义模型：
- 推荐最优 Join Path
- 评估 fan-out 风险
- 说明为什么选这条路径而不是另一条
- 如果有多条路径，列出各自的 trade-off

如果没有语义模型：
- 基于外键信息推荐 Join
- 标注"无外键约束，Join 可能不准确"的风险

#### 2.3 字段建议

为常见分析任务推荐字段：
- 时间维度字段
- 维度/分组字段
- 度量/聚合字段
- 过滤字段

推荐依据：
- 语义模型中的维度定义
- 字段名和类型
- 历史 SQL 中常用的字段
- 值提示（value hints）

#### 2.4 schema_plan artifact 结构升级

```python
{
  "artifact_type": "schema_plan",
  "status": "valid" | "warning" | "blocked",
  "primary_tables": [
    {
      "table_name": "fact_watch_session",
      "role": "fact",
      "relevance_score": 0.95,
      "reason": "包含订单金额和数量，是销售额指标的主表",
      "key_metrics": ["order_amount", "quantity"],
      "key_dimensions": ["order_date", "customer_id"]
    }
  ],
  "join_paths": [
    {
      "path_name": "orders_to_customers",
      "tables": ["fact_watch_session", "dim_user"],
      "join_keys": [{"left": "customer_id", "right": "customer_id"}],
      "cardinality": "many_to_one",
      "fan_out_risk": false,
      "recommended": true
    }
  ],
  "recommended_fields": {
    "time_dimensions": ["order_date"],
    "group_by": ["customer_segment", "region"],
    "aggregations": ["SUM(order_amount)", "COUNT(DISTINCT customer_id)"],
    "filters": ["order_date >= '2024-01-01'"]
  },
  "risks": [
    {
      "type": "missing_join",
      "severity": "medium",
      "description": "用户提到地区，但没有选择区域维度表"
    }
  ],
  "assumptions": []
}
```

### 3. KnowledgeAgent 小幅升级

KnowledgeAgent 也顺便升级一下，但改动不用太大：
- 对历史 SQL 匹配结果按相关性和质量排序
- 去重相似的历史 SQL
- 标注哪些历史 SQL 最值得参考（比如执行成功、有反射验证通过的）
- 如果历史 SQL 太少，标记为 warning

### 4. Orchestrator 集成

OrchestratorAgent 需要：
- 根据 analysis_request 的 status 判断是否继续
- 如果 status 是 blocked，终止并返回澄清建议
- 如果 status 是 warning，在 delivery_report 中标注
- schema_plan 的 warning 也要传递到最终结果

注意：不要因为 warning 就终止流程。warning 只是提示，不阻塞。

## 限制

- 不引入 LLM 调用，所有分析都用规则实现
- 不改变 WorkflowRunner 和节点的内部逻辑
- 不改变 Context 的字段定义（如果需要传递信息，用 artifact）
- 不破坏现有测试
- ProductAnalystAgent 和 SchemaArchitectAgent 的输入输出接口不变（artifact 结构扩展但向后兼容）

## 验收标准

1. ask_sql 全量测试通过
2. ProductAnalystAgent 能检测至少 3 类模糊点
3. analysis_request 有 valid/warning/blocked 三级状态
4. SchemaArchitectAgent 能推荐 Join Path 并评估风险
5. schema_plan 有表选择、Join 路径、字段建议、风险评估
6. blocked 状态会终止流程并返回澄清建议
7. warning 状态不阻塞，信息传递到最终结果
8. 新增测试覆盖：模糊点检测、Join Path 推荐、blocked 终止、warning 传递

## 输出要求

1. 先读取现有代码，确认各角色的当前实现
2. 给出升级方案和 artifact 结构定义
3. 实现代码
4. 编写测试
5. 运行测试，确保全量通过
```
