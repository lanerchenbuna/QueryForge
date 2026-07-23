# Prompt 10: Phase B+C 集成验收

复制下面整段给 Codex 使用。

```text
Phase B（增强自主性）和 Phase C（质量提升）的 prompts 已经完成。本轮做集成验收。

## 验收范围

1. Conversation Memory：会话上下文与追问重写
2. Bounded Tool Loop：有界工具循环
3. Structured Reasoning：结构化推理摘要
4. Parallel + Selection：多候选生成与选择
5. Subject Tree：主题范围约束
6. 回归验证：ask_sql 主链路不退化

## 验收清单

### 1. Conversation Memory 验收

- [ ] 不传 session_id 时行为不变
- [ ] 传 session_id 时，追问能正确识别和重写
- [ ] 支持至少 5 种追问模式：增加维度、增加过滤、增加指标、减少维度、修改排序
- [ ] 会话状态持久化
- [ ] 会话有轮数限制
- [ ] 不保存结果行
- [ ] CLI/API/MCP 都支持 session_id

### 2. Tool Loop 验收

- [ ] 默认关闭时行为不变
- [ ] 开启后能正常调用 list_tables / describe_table / preview_distinct_values / execute_sql_preview
- [ ] 轮数限制生效
- [ ] 行数限制生效
- [ ] 超时限制生效
- [ ] 工具调用都经过安全策略检查
- [ ] final_answer 能正确退出循环
- [ ] 有完整的调用历史记录

### 3. Structured Reasoning 验收

- [ ] GenSqlNode 输出 ReasoningResult 结构化摘要
- [ ] ReasoningResult 包含完整字段
- [ ] 有 reasoning 和 SQL 的一致性校验
- [ ] 校验不一致时记录 warning，不阻塞
- [ ] Plan Mode 展示推理摘要
- [ ] ReflectNode 能利用推理摘要
- [ ] 最终输出包含 reasoning 字段
- [ ] 向后兼容：没有 reasoning 时不报错
- [ ] 不增加额外的 LLM 调用

### 4. Parallel + Selection 验收

- [ ] parallel_candidates=1 时行为不变
- [ ] 能生成多个候选
- [ ] 选择器能正确评分和选择
- [ ] AST 校验不通过的候选被淘汰
- [ ] Governance 拒绝的候选被淘汰
- [ ] 执行成功的候选得分更高
- [ ] 语义匹配度高的候选得分更高
- [ ] 选择过程可追溯
- [ ] 预览执行有行数和时间限制

### 5. Subject Tree 验收

- [ ] 默认关闭时行为不变
- [ ] 能用 YAML 定义多个主题
- [ ] 主题选择正确
- [ ] Schema 裁剪生效
- [ ] Skills 加载生效
- [ ] 历史 SQL 检索生效
- [ ] 选错主题时有 fallback 机制
- [ ] 可以手动指定主题

### 6. 回归验收

- [ ] 全量单元测试通过（180+ 项）
- [ ] ask_sql 输出向后兼容
- [ ] 所有入口（CLI/API/MCP/Gateway）都能正常工作
- [ ] 默认配置下性能没有明显退化
- [ ] 文档更新

## 要做的事

### 1. 编写集成验收测试

新建 tests/test_phasebc_integration.py，包含：
- 会话上下文端到端测试
- Tool Loop 端到端测试
- 结构化推理测试
- 多候选选择测试
- 主题树端到端测试
- 组合场景测试（比如会话 + Tool Loop + 多候选）

### 2. 更新文档

- 更新 README.md
- 更新 docs/agent_team_architecture.md
- 更新 docs/future_extensions.md，标记已完成项
- 新增 docs/subject_tree.md

### 3. 性能测试

度量（简单对比）：
- 默认配置 vs 关闭所有高级功能的耗时
- Tool Loop 开启后的额外耗时
- 多候选开启后的额外耗时
- 确保默认配置下性能不退化

### 4. 配置默认值确认

确认所有高级功能的默认值都是"关闭"或"最小化"：
- conversation_memory: 不传 session_id 就不启用
- tool_loop: 默认关闭
- parallel_candidates: 默认 1
- subject_tree: 默认关闭
- structured_reasoning: 总是输出但不影响流程

### 5. Bug 修复和清理

- 修复验收中发现的问题
- 清理冗余代码
- 统一命名和代码风格

## 验收标准

1. 全量测试通过（至少 180+ 项）
2. ask_sql 输出向后兼容
3. 默认配置下性能不退化
4. 所有高级功能都能正常开启和关闭
5. 文档与代码一致
6. 代码整洁，没有明显的冗余

## 输出

1. 集成验收报告
2. 更新后的文档
3. 修复后的代码
4. Phase B+C 完成标记
```
