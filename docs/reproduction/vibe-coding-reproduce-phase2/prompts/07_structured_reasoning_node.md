# Prompt 07: Structured Reasoning Node——可审计决策摘要

复制下面整段给 Codex 使用。

```text
继续上一轮。Tool Loop 已经实现。

本轮目标：实现 Structured Reasoning Node，把 SQL 生成的决策过程结构化、可审计、可校验。

## 背景

当前 SQL 生成是"黑盒"的：模型生成一段 SQL 和一段自然语言解释，但我们不知道模型是怎么想的——选了哪些表、为什么选这些 Join、用了什么指标、做了什么假设。

Structured Reasoning Node 让模型输出结构化的决策摘要，而不是自由文本解释。这样可以：
1. 审计：知道模型是怎么决策的
2. 校验：检查决策和最终 SQL 是否一致
3. 复用：决策摘要可以被其他节点（Reflect、Fix、Plan）复用

## 本轮要做的事

### 1. 定义 ReasoningResult 模型

在 queryforge/schemas/models.py 中新增：

```python
class ReasoningResult(BaseModel):
    goal: str                    # 分析目标
    grain: str                   # 分析粒度
    tables: list[str]            # 选择的表
    joins: list[ReasoningJoin]   # Join 决策
    metrics: list[ReasoningMetric]  # 指标
    dimensions: list[str]        # 维度/分组字段
    filters: list[ReasoningFilter]  # 过滤条件
    time_range: str | None       # 时间范围
    sorting: list[ReasoningSort]  # 排序
    limit: int | None            # 条数限制
    assumptions: list[str]       # 做出的假设
    risks: list[str]             # 识别到的风险
    confidence: float            # 置信度 0-1
    strategy: str                # 生成策略（直接生成 / 参考历史 / 参考指标）
```

class ReasoningJoin(BaseModel):
    left_table: str
    right_table: str
    join_type: str  # inner / left / right / full
    left_key: str
    right_key: str
    reason: str

class ReasoningMetric(BaseModel):
    name: str
    expression: str
    alias: str
    source: str  # metric_model / ad_hoc

class ReasoningFilter(BaseModel):
    column: str
    operator: str
    value: str
    logic: str  # AND / OR

class ReasoningSort(BaseModel):
    column: str
    direction: str  # ASC / DESC

### 2. GenSqlNode 改造：先生成 reasoning，再生成 SQL

GenSqlNode 的流程从：
```
Prompt → SQL + explanation
```
改成：
```
Prompt → ReasoningResult → SQL + explanation
```

两步走：
1. 让模型先输出结构化的推理摘要
2. 基于推理摘要生成 SQL

或者一步到位：让模型同时输出 reasoning 和 SQL。

建议：**一步到位**，因为两步会增加一次 LLM 调用，成本翻倍。可以让模型在一次 JSON 输出中同时包含 reasoning 和 sql。

GenSqlNode 的输出增加 `reasoning_result` 字段，存在 Context 中。

### 3. 校验：Reasoning vs SQL

生成 SQL 后，用规则校验 reasoning 和 SQL 是否一致：

检查项：
- SQL 中用到的表是否都在 reasoning.tables 中
- SQL 中的 JOIN 是否和 reasoning.joins 一致
- SQL 中的 GROUP BY 是否和 reasoning.dimensions 一致
- SQL 中的 WHERE 是否和 reasoning.filters 匹配
- SQL 中的 ORDER BY 是否和 reasoning.sorting 匹配

校验不通过怎么办？
- 记 warning，不阻塞
- 在 reflect 阶段可以用这个作为反思的输入
- 未来可以作为 Fix 的依据

### 4. Plan Mode 集成

Plan Mode 现在展示的是 SQL 和风险。可以增强为：
- 展示结构化推理摘要
- 展示决策依据和假设
- 让用户更容易判断要不要批准

### 5. ReflectNode 集成

ReflectNode 现在用自然语言评估 SQL 质量。可以把 reasoning_result 作为输入，让评估更有依据：
- 检查 reasoning 中的假设是否成立
- 检查风险点是否被正确处理
- 检查指标口径是否正确

### 6. Artifact 集成

SQLDeveloperAgent 的 sql_candidate artifact 中增加 reasoning 字段。

### 7. 输出集成

最终输出中增加 `reasoning` 字段（可选，默认关闭？）。

考虑到 token 和隐私问题，默认输出 reasoning 但只包含结构化字段，不包含模型的完整思考过程。

## 限制

- 不改变 SQL 生成的核心逻辑，只是增加结构化输出
- 校验是 warning 级别，不阻塞
- 不引入额外的 LLM 调用（在一次生成中同时输出 reasoning 和 SQL）
- 向后兼容：如果模型没有输出 reasoning 字段，用空值填充
- 不改变 WorkflowRunner 的整体流程

## 验收标准

1. GenSqlNode 输出 ReasoningResult 结构化摘要
2. ReasoningResult 包含 goal、tables、joins、metrics、dimensions、filters、assumptions、risks 等字段
3. 有规则校验 reasoning 和 SQL 的一致性
4. 校验不一致时记录 warning，不阻塞
5. Plan Mode 展示推理摘要
6. ReflectNode 可以利用推理摘要
7. 最终输出中包含 reasoning 字段
8. 向后兼容：没有 reasoning 时不报错
9. 不增加额外的 LLM 调用次数
10. 新增测试覆盖：reasoning 生成、一致性校验、缺失兼容

## 输出要求

1. 先读取现有代码，确认 GenSqlNode 和 ReflectNode 的实现
2. 给出 ReasoningResult 模型设计和集成方案
3. 实现代码
4. 编写测试
5. 运行测试，确保全量通过
```
