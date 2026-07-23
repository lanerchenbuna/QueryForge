# Prompt 11: Streaming Output——节点级事件流

复制下面整段给 Codex 使用。

```text
Phase B+C 已完成。现在进入 Phase D：形态扩展。

本轮目标：实现 Streaming Output，让用户实时看到进度，而不是等完整结果。

## 背景

当前 QueryForge 是同步阻塞的：发出请求后，等所有步骤完成才返回结果。Tool Loop、多候选这些能力会让等待时间变长，用户不知道进度。

Streaming Output 让每个节点/阶段的完成都立即推送一个事件，用户可以实时看到：
- 正在分析问题...
- 正在检索 Schema...
- 正在生成 SQL...
- 正在执行 SQL...
- 正在反思修正...
- 完成！

## 本轮要做的事

### 1. 事件协议（Event Protocol）

定义统一的事件格式：

```python
class WorkflowEvent(BaseModel):
    event_id: str
    event_type: str          # node_started / node_completed / node_failed / phase_started / phase_completed / artifact_created / final_result
    timestamp: str
    run_id: str
    node_name: str | None = None
    phase_name: str | None = None
    artifact_type: str | None = None
    status: str | None = None  # success / failed / skipped / warning
    message: str | None = None
    data: dict | None = None   # 额外数据（不包含结果正文，避免泄露）
```

事件类型：
- `node_started`: 节点开始执行
- `node_completed`: 节点执行完成
- `node_failed`: 节点执行失败
- `phase_started`: 阶段开始（Agent Team 的阶段）
- `phase_completed`: 阶段完成
- `artifact_created`: 新 artifact 生成
- `retrying`: 重试（反射修复、重新生成）
- `final_result`: 最终结果

### 2. EventBus / EventEmitter

新增 EventEmitter 类（放在 queryforge/agent/event_emitter.py）：

职责：
- 收集事件
- 推送给订阅者
- 支持多种输出方式（callback、SSE、队列）

接口：
```python
class EventEmitter:
    def emit(self, event: WorkflowEvent) -> None: ...
    def on_event(self, callback: Callable[[WorkflowEvent], None]) -> None: ...
    def get_events(self) -> list[WorkflowEvent]: ...
```

### 3. Workflow Runner 集成

在 ReflectiveWorkflow 和 WorkflowRunner 中埋点：
- 每个节点开始前 emit node_started
- 每个节点完成后 emit node_completed
- 节点失败时 emit node_failed
- 重试时 emit retrying
- 最终结果时 emit final_result

事件流由 EventEmitter 管理，通过 Context 传递。

### 4. Agent Team 集成

在 OrchestratorAgent 中埋点：
- 每个阶段开始时 emit phase_started
- 每个阶段完成时 emit phase_completed
- 每个 artifact 生成时 emit artifact_created
- blocked 时 emit node_failed

### 5. API SSE 支持

FastAPI 增加 SSE 端点：
- `POST /ask/stream`：流式返回事件
- 使用 Server-Sent Events 协议
- 每个事件是一条 JSON

或者：在现有 `/ask` 端点中增加 `Accept: text/event-stream` 头的支持。

建议：新增独立的 `/ask/stream` 端点，逻辑更清晰。

### 6. CLI 进度显示

CLI 增加 `--stream` 参数：
- 开启后，在 stderr 显示进度
- 最终结果仍然在 stdout 输出（JSON 格式，便于管道处理）
- 进度显示用简单的文本或 emoji

示例：
```
🔍 分析问题...
📊 检索 Schema...
🧠 生成 SQL...
✅ SQL 生成完成
⚙️ 执行 SQL...
🔄 第 1 次修复...
✅ 执行完成
📈 生成可视化...
🎉 完成！
```

### 7. MCP 支持

MCP 工具增加流式支持：
- ask_sql_stream 工具
- 或者在 ask_sql 结果中包含事件历史

MCP 的流式支持比较复杂，本轮可以先做"返回事件历史"的方式，后续再做真正的流式。

### 8. 配置

配置项：
- `streaming_enabled: bool`，默认 true（不影响 API，只是支持）
- `streaming_event_buffer_size: int`，默认 100

### 9. 测试策略

- 测试 EventEmitter 的事件收集
- 测试 Workflow Runner 的事件埋点
- 测试 API SSE 端点（用 TestClient）
- 测试 CLI 的 --stream 参数

## 限制

- 流式输出不改变业务逻辑
- 最终 JSON 结果保持不变
- 不流式传输结果数据（只传进度事件，不传行数据），避免数据安全问题
- 不增加 LLM token 流式输出（那个是 Provider 层面的，不是工作流层面的）
- 向后兼容：不开启 streaming 时行为不变

## 验收标准

1. EventEmitter 能正确收集和分发事件
2. Workflow Runner 的每个节点都有开始/完成事件
3. Agent Team 的每个阶段都有开始/完成事件
4. 重试事件正确触发
5. API SSE 端点能正常工作
6. CLI --stream 参数能显示进度
7. 最终结果和非流式完全一致
8. 不流式传输敏感数据
9. 新增测试覆盖：事件收集、SSE 端点、CLI 进度
10. ask_sql 全量测试通过

## 输出要求

1. 先读取现有代码，确认 Workflow Runner 和 API 的实现
2. 给出事件协议和 EventEmitter 设计
3. 实现代码
4. 编写测试
5. 运行测试，确保全量通过
```
