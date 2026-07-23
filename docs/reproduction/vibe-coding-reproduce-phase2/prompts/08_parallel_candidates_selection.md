# Prompt 08: Parallel + Selection——多候选生成与选择

复制下面整段给 Codex 使用。

```text
Phase B 已完成。现在进入 Phase C：质量提升。

本轮目标：实现多候选 SQL 生成与选择机制，通过多生成 + 多维度评分来提升准确率。

## 背景

当前 SQL 生成是单轮的：生成一个 SQL，执行，反射，修复。

Parallel + Selection 的思路是：
1. 一次生成 2-3 个 SQL 候选
2. 用规则和执行信号对每个候选评分
3. 选择最优的那个作为最终结果

这是一种"用算力换质量"的策略，适用于复杂查询或准确率要求高的场景。

## 本轮要做的事

### 1. 多候选生成

在 SQLDeveloper 阶段生成多个 SQL 候选。

生成策略：
- 候选 A：基于 Schema 和问题的直接生成（当前方式）
- 候选 B：参考历史 SQL 的生成
- 候选 C：基于语义模型指标的生成

或者更简单：让模型生成 N 个不同的 SQL，每个都有不同的推理路径。

建议：**让模型一次生成 N 个候选**，但要控制数量，避免成本过高。默认 2 个，最多 3 个。

候选数量配置：
- `parallel_candidates: int`，默认 1（即关闭并行）
- 可配置为 2 或 3

为什么默认 1？
- 成本：多生成一个 SQL 多花一次 LLM 调用
- 大多数简单查询不需要并行
- 复杂查询可以手动开启

### 2. 选择器（Selector）

新增 SQLSelector 类（放在 queryforge/agent/sql_selector.py）：

职责：
- 对每个候选评分
- 选择最优的
- 输出选择理由

评分维度（按权重从高到低）：

| 维度 | 权重 | 说明 |
|-----|------|------|
| AST 校验通过 | 基础项 | 不通过直接淘汰 |
| Governance 验证通过 | 基础项 | 不通过直接淘汰 |
| 语义模型口径匹配 | 高 | 指标/维度是否和语义模型一致 |
| 执行成功 | 高 | 是否能正常执行 |
| 结果非空 | 中 | 是否返回了数据 |
| 结果行数合理 | 中 | 行数在合理范围内 |
| Reflect 评分 | 中低 | ReflectNode 的评估结果 |
| 历史相似度 | 低 | 和历史成功 SQL 的相似程度 |
| 复杂度合理性 | 低 | 复杂度是否和问题匹配 |

评分算法：加权求和，基础项不通过直接淘汰。

### 3. 执行预览验证

对每个候选做带 LIMIT 的预览执行（比如 LIMIT 20），验证：
- SQL 是否能正常执行
- 返回的列是否合理
- 行数是否在合理范围内

注意：只做预览，不做正式执行。正式执行还是在 ExecuteSqlNode 中。

预览执行也有预算控制：
- 最多预览 N 个候选（默认 2 个）
- 每个预览最多 M 行（默认 20 行）
- 预览总时间不超过 T 秒（默认 10 秒）

### 4. Agent Team 集成

在 Agent Team 架构中，Parallel + Selection 的位置：

```
analysis lifecycle
  → [Tool Loop 可选]
  → SQLDeveloperAgent 生成多候选
  → GovernanceAgent 逐个验证
  → SQLSelector 选择最优
  → 执行最优 SQL
  → completion lifecycle
```

SQLDeveloperAgent 负责生成多候选，SQLSelector 是一个新的角色或工具。

建议：把 SQLSelector 作为 SQLDeveloperAgent 的一部分，而不是独立角色。因为选择和生成是紧密相关的。

### 5. 候选选择过程可追溯

把每个候选的评分和选择理由都记录下来：
- 保存到 sql_candidate artifact
- 保存到 Context
- ReflectNode 可以参考这些信息

### 6. 配置项

新增配置：
- `parallel_candidates: int = 1`（候选数量，1 表示关闭）
- `parallel_max_preview: int = 2`（最多预览多少个）
- `selector_weights: dict`（各维度权重）
- `parallel_auto_trigger_complexity: int`（复杂度超过多少自动开启并行，可选）

### 7. 测试策略

测试 SQLSelector 不需要 LLM，用 mock SQL 候选测试评分逻辑：
- 基础项淘汰测试
- 各维度评分测试
- 选择最优测试
- 平局处理测试

端到端测试需要 LLM，用 mock 模拟。

## 限制

- 默认关闭（parallel_candidates=1），不影响现有行为
- 候选数量限制为最多 3 个
- 预览执行有严格的行数和时间限制
- 正式执行仍然走 ExecuteSqlNode，不绕过任何安全检查
- 选择器的评分是启发式的，不保证最优
- 不引入新的 LLM Provider 依赖

## 验收标准

1. parallel_candidates=1 时，行为和以前完全一样
2. parallel_candidates=2 时，生成 2 个候选并选择最优
3. 选择器能正确淘汰 AST 校验不通过的候选
4. 选择器能正确淘汰 Governance 拒绝的候选
5. 执行成功的候选得分高于执行失败的
6. 语义模型匹配度高的候选得分更高
7. 选择过程可追溯，有完整的评分记录
8. 预览执行有行数和时间限制
9. 新增测试覆盖：选择器评分、候选淘汰、平局处理、预览限制
10. ask_sql 全量测试通过

## 输出要求

1. 先读取现有代码，确认 GenSqlNode 和 SQLDeveloperAgent 的实现
2. 给出多候选生成和选择器设计
3. 实现代码
4. 编写测试
5. 运行测试，确保全量通过
```
