# Prompt 03: Artifact 质量门禁与阶段推进

复制下面整段给 Codex 使用。

```text
继续上一轮。当前 Agent Team 的各角色已经产生 artifact，但 artifact 只是"写了就过"，没有质量门禁，也没有统一的状态推进机制。

本轮目标：建立 artifact 质量门禁体系和统一的阶段推进机制，让 Orchestrator 真正成为"编排者"而不是"调用器"。

## 现状（请先读取确认）

- queryforge/agent_team/orchestrator/orchestrator.py：OrchestratorAgent 当前实现
- queryforge/agent_team/agents/base.py：RoleAgent 基类
- queryforge/agent_team/schemas/__init__.py：TaskState、ArtifactRef 等模型
- queryforge/agent_team/runtime/state_store.py：状态存储

## 本轮要做的事

### 1. 统一 Artifact Status 体系

所有 artifact 都必须有明确的 status 字段，取值为：

- `valid`: 完全通过，可以进入下一阶段
- `warning`: 有一些问题，但不阻塞继续（默认行为）
- `blocked`: 严重问题，必须终止流程
- `degraded`: 功能降级（比如某个 Agent 不可用，用 fallback 代替）

在 RoleAgent 基类中增加 status 校验和统一 emit 方法。

### 2. 质量门禁（Quality Gates）

在每个关键阶段后增加质量门禁检查：

| 阶段 | 门禁检查 | 触发 blocked 的条件 |
|-----|---------|-------------------|
| analysis | 需求清晰度检查 | 完全无法识别分析目标、关键指标缺失 |
| schema | Schema 完整性检查 | 没有找到任何相关表 |
| sql_candidate | SQL 候选质量检查 | Governance 拒绝、SQL 为空或非法 |
| execution | 执行结果检查 | 执行失败且无法修复 |
| qa | 数据质量检查 | 结果严重不符合预期（由 Reflect 判断） |

门禁逻辑放在 OrchestratorAgent 中，每个阶段完成后调用 `_check_gate(state, phase)` 方法。

### 3. 阶段推进状态机

将当前的"一次性调用所有角色"改为"逐阶段推进"，每个阶段：
1. 执行该阶段的角色
2. 检查门禁
3. 如果 blocked，终止并记录
4. 如果 warning，记录但继续
5. 如果 valid，进入下一阶段

TaskState 增加字段：
- `current_phase`: 当前阶段
- `completed_phases`: 已完成阶段列表
- `pending_phases`: 待执行阶段列表
- `blocked_phase`: 阻塞在哪个阶段（如果 blocked）
- `blocked_reason`: 阻塞原因
- `warnings`: 所有 warning 列表

这些字段现在部分已有，但需要完善和统一使用。

### 4. Artifact Schema 校验

为每个 artifact 类型定义 Pydantic schema，在 emit 时校验：
- analysis_request
- knowledge_context
- schema_plan
- sql_candidate
- governance_report
- qa_report
- visualization_artifact
- ops_report
- delivery_report
- review_report（新增）

校验失败的 artifact 标记为 `degraded` 并记录错误，但不阻塞流程（除非是关键阶段）。

### 5. 失败恢复与降级策略

定义每个角色的降级策略：

| 角色 | 降级策略 |
|-----|---------|
| ProductAnalyst | 如果正则解析失败，用问题本身作为 goal，其他字段为空，status=warning |
| Knowledge | 如果历史/向量检索失败，返回空上下文，status=degraded |
| SchemaArchitect | 如果 Schema 读取失败，返回空 plan，status=blocked |
| SQLDeveloper | 必须有 SQL，否则 status=blocked |
| Governance | 如果策略引擎不可用，默认放行但 status=warning（慎用，安全第一） |
| DataQA | 如果检查失败，标记 warning 但继续 |
| Visualization | 如果失败，标记 degraded 但继续 |
| Ops | 永远不阻塞 |

注意：Governance 的降级策略要谨慎。默认策略是：如果策略引擎不可用，应该 blocked 而不是放行，因为安全是底线。可以留一个配置项控制。

### 6. Orchestrator 重构

将 OrchestratorAgent 的 run 方法从"三个钩子"重构为"阶段状态机"：

```python
def run(self, *, run_id, decision, workflow, plan_only=False):
    # 初始化状态
    state = self._initialize_state(run_id, decision, plan_only)
    
    try:
        # Phase 1: Analysis
        self._run_phase(state, "analysis", context=None)
        if state.status == "blocked":
            return self._blocked_output(state)
        
        # Phase 2: SQL Generation & Execution (via WorkflowRunner)
        result = self._run_execution_phase(state, workflow)
        
        # Phase 3: Completion
        self._run_phase(state, "completion", context=context_from_result)
        
        return self._success_output(state, result)
    except Exception as exc:
        return self._failed_output(state, exc)
```

等等，但是 WorkflowRunner 内部是一个完整的循环，怎么把阶段推进和它的执行结合起来？

答案：保持三个生命周期钩子的设计，但在每个钩子中推进阶段状态。Orchestrator 的阶段推进通过钩子触发：
- analysis_hook → 推进 analysis 阶段
- candidate_hook → 推进 sql_candidate 阶段（每次生成 SQL 都推进）
- completion_hook → 推进 completion 阶段

WorkflowRunner 内部的执行循环不变，Orchestrator 通过钩子感知进度。

### 7. 重试机制

某些阶段失败后可以重试：
- analysis 失败：不重试（输入就是问题本身）
- sql_candidate 失败：由 WorkflowRunner 的 Reflect/Fix 循环处理，Orchestrator 不单独重试
- governance 拒绝：不重试（安全问题重试没用）
- execution 失败：由 WorkflowRunner 处理

Orchestrator 层面的重试主要是为未来的 Tool Loop 和并行候选做准备，本轮先建立机制但不启用。

## 限制

- 不改变 WorkflowRunner 的内部逻辑
- 不引入 LLM 调用
- 不破坏现有 artifact 结构（向后兼容）
- 所有门禁规则都用确定性规则实现
- 不新增 agent_team 或其他开关

## 验收标准

1. 所有 artifact 都有 valid/warning/blocked/degraded 状态
2. 关键阶段有质量门禁检查
3. blocked 状态会终止流程并返回原因
4. warning 状态会记录但不阻塞
5. TaskState 完整记录阶段推进过程
6. ask_sql 全量测试通过
7. 新增测试覆盖：blocked 终止、warning 传递、阶段状态推进、artifact schema 校验
8. 新增测试覆盖：各角色的降级策略

## 输出要求

1. 先读取现有代码，确认 Orchestrator 和各角色的当前实现
2. 给出质量门禁设计和阶段状态机设计
3. 实现代码
4. 编写测试
5. 运行测试，确保全量通过
```
