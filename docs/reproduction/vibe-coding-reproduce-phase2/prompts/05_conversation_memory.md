# Prompt 05: Conversation Memory——会话上下文与追问重写

复制下面整段给 Codex 使用。

```text
Phase A 已完成。现在进入 Phase B：增强自主性。

本轮目标：实现 Conversation Memory，支持自然的分析追问。

## 背景

当前 QueryForge 是单轮的：每个问题都是独立的，不知道上下文。真实的分析场景是追问式的：

用户："上个月各地区的销售额是多少？"
用户："按产品类别再拆一下呢？"
用户："只看华东区"

第二个和第三个问题如果没有上下文，根本无法理解。

## 本轮要做的事

### 1. Session 模型

定义会话模型，保存结构化的上下文信息。

新增 queryforge/agent_team/schemas/session.py（或在现有 schemas 中扩展）：

```python
class SessionMemory(BaseModel):
    session_id: str
    created_at: str
    updated_at: str
    turn_count: int
    last_question: str
    last_sql: str
    last_result_schema: list[str]  # 列名，不保存数据行
    last_metrics: list[str]       # 上轮用到的指标
    last_dimensions: list[str]    # 上轮的分组维度
    last_filters: list[dict]      # 上轮的过滤条件
    last_time_range: dict | None  # 上轮的时间范围
    history: list[SessionTurn]    # 最近 N 轮的摘要
```

class SessionTurn(BaseModel):
    turn_number: int
    question: str
    sql: str
    metrics: list[str]
    dimensions: list[str]
    filters: list[dict]
    result_schema: list[str]
    status: str  # success / failed

存储位置：.queryforge/sessions/<session_id>.json

### 2. ProductAnalystAgent 扩展：追问重写

ProductAnalystAgent 增加追问识别和重写能力：

检测追问模式（规则实现，不用 LLM）：
- "按 X 再拆一下" → 在上轮基础上增加维度 X
- "只看 X" → 在上轮基础上增加过滤 X
- "按时间展开" → 在上轮基础上增加时间维度
- "那个结果"、"刚才那个" → 指代上轮结果
- "再加上 X" → 在上轮基础上增加指标 X
- "去掉 X" → 在上轮基础上移除维度/指标 X
- "TOP N" → 在上轮基础上增加排序和 LIMIT

重写逻辑：
1. 检测是否为追问
2. 如果是追问，基于上轮上下文和本轮增量，重写为独立问题
3. 重写后的问题交给后续流程正常处理
4. 在 analysis_request 中标记 `is_followup: true` 和 `rewritten_from: "..."`

注意：追问重写是在 ProductAnalyst 阶段做的，不改变后续节点的逻辑。后续节点看到的是重写后的完整问题。

### 3. AgentOptions 增加 session_id

- session_id: str | None = None
- 如果提供 session_id，加载对应会话
- 如果不提供，创建新会话
- 新增 reset_session: bool = False 选项

### 4. Orchestrator 集成

OrchestratorAgent 增加会话管理：
- run 方法增加 session_id 参数
- 执行前：加载/创建会话
- ProductAnalyst 阶段：传入会话上下文，做追问重写
- 执行后：更新会话（保存本轮结果摘要）
- 最终输出中增加 session 信息

会话更新策略：
- 只保存最近 N 轮（默认 10 轮）
- 不保存完整结果行，只保存 schema 和摘要
- 每轮的 SQL、指标、维度、过滤条件都保存
- 失败的轮次也保存，但标记为 failed

### 5. 入口集成

CLI：
- 新增 --session-id 参数
- 新增 --new-session 参数（忽略旧会话）
- 新增 --reset-session 参数

API：
- AskRequest 增加 session_id 字段
- 返回结果中包含 session_id

MCP：
- ask_sql 工具增加 session_id 参数

Gateway：
- 每个 user_id + channel 组合自动维护一个 session

### 6. 会话存储

新增 SessionStore（类似 StateStore 的设计）：
- load(session_id) -> SessionMemory | None
- save(session_id, session) -> None
- create() -> str（生成新 session_id）
- reset(session_id) -> None

存储格式：JSON 文件，原子写入。

## 限制

- 不用 LLM 做追问识别和重写，全部用规则实现
- 不保存完整结果行，只保存 schema 和摘要
- 会话有轮数上限（默认 10 轮），防止无限增长
- 不破坏单轮模式：不传 session_id 时行为和以前完全一样
- 不改变 WorkflowRunner 和节点内部逻辑

## 验收标准

1. 不传 session_id 时，行为和以前完全一样
2. 传 session_id 时，追问能被正确识别和重写
3. 至少支持 5 种追问模式：增加维度、增加过滤、增加指标、减少维度、修改排序
4. 会话状态持久化，重启后仍可用
5. 会话有轮数限制，不会无限增长
6. 不保存结果行，保护数据隐私
7. 新增测试覆盖：追问识别、重写正确性、会话持久化、轮数限制、重置
8. ask_sql 全量测试通过

## 输出要求

1. 先读取现有代码，确认 ProductAnalystAgent 和 OrchestratorAgent 的当前实现
2. 给出会话模型设计和追问重写规则
3. 实现代码
4. 编写测试
5. 运行测试，确保全量通过
```
