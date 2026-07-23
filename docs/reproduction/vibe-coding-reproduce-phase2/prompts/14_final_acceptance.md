# Prompt 14: 最终集体验收与文档收尾

复制下面整段给 Codex 使用。

```text
Phase D 的 prompts 已经完成。本轮做最终的全链路集体验收和文档收尾。

## 验收范围

整个第二阶段（Agent Team 深度进化）的全部能力：

### Phase A：填平补齐
- 路由系统：sql_review / metadata_query / troubleshoot_sql / explain_result / build_report
- 角色升级：ProductAnalyst / SchemaArchitect / KnowledgeAgent 决策化
- 质量门禁：artifact 三级状态、阶段推进

### Phase B：增强自主性
- Conversation Memory：会话上下文与追问重写
- Bounded Tool Loop：有界工具循环
- Structured Reasoning：结构化推理摘要

### Phase C：质量提升
- Parallel + Selection：多候选生成与选择
- Subject Tree：主题范围约束

### Phase D：形态扩展
- Streaming Output：节点级事件流
- Report Artifact：静态 HTML 分析报告
- MCP 增强：resources / prompts / sessions

### 回归验证
- ask_sql 主链路不退化
- 所有入口正常工作
- 性能基线

## 验收清单

### 1. 功能完整性

- [ ] 7 种 task_type 路由正确
- [ ] ProductAnalyst 决策化完成
- [ ] SchemaArchitect 决策化完成
- [ ] Artifact 质量门禁生效
- [ ] Conversation Memory 正常工作
- [ ] Tool Loop 正常工作（4 个工具）
- [ ] Structured Reasoning 正常输出
- [ ] Parallel + Selection 正常工作
- [ ] Subject Tree 正常工作
- [ ] Streaming Output 正常工作
- [ ] Report Artifact 正常生成
- [ ] MCP resources/prompts/sessions 正常

### 2. 架构一致性

- [ ] 所有入口都经过 EntryRouter → Orchestrator → WorkflowRunner
- [ ] 没有绕过 Governance 的路径
- [ ] 所有高级功能默认关闭或最小化
- [ ] 模块职责清晰，没有越界
- [ ] 代码风格一致

### 3. 性能基线

- [ ] 默认配置下，简单查询耗时不退化超过 10%
- [ ] 开启 Tool Loop 后，额外耗时可接受
- [ ] 开启多候选后，额外耗时可接受
- [ ] 报告生成耗时可接受

### 4. 测试覆盖率

- [ ] 全量单元测试通过（200+ 项）
- [ ] 每个新功能都有对应测试
- [ ] 集成测试覆盖主要场景
- [ ] 错误场景有测试覆盖

### 5. 文档完整性

- [ ] README.md 更新
- [ ] 架构文档更新
- [ ] 配置文档更新
- [ ] API 文档更新
- [ ] MCP 文档更新

## 要做的事

### 1. 全量回归测试

运行所有测试，确保全绿。

### 2. 编写端到端验收测试

新建 tests/test_phase2_final_acceptance.py，包含：
- 完整 ask_sql 端到端测试
- sql_review 端到端测试
- metadata_query 端到端测试
- 会话上下文 + 追问测试
- Tool Loop + 多候选组合测试
- Subject Tree + 主题切换测试
- 报告生成测试
- Streaming 测试（如果可行）

### 3. 文档更新

- 更新 README.md：全面介绍 Phase 2 新增能力
- 更新 docs/agent_team_architecture.md：更新架构图和完整角色列表
- 更新 docs/future_extensions.md：标记已完成项，新增后续方向
- 更新 docs/configuration.md：新增配置项说明
- 更新 docs/mcp_server.md：MCP 增强说明
- 更新 docs/api_reference.md：API 端点更新

### 4. 代码清理

- 删除废弃的代码和 TODO
- 统一命名
- 补充 docstring
- 修复 lint 警告

### 5. 示例数据和 demo

- 准备 demo 脚本，展示各能力
- 准备示例 subject 配置
- 准备示例 SQL 评审用例

### 6. 最终验收报告

生成最终验收报告，包含：
- 功能清单和完成状态
- 测试结果
- 性能对比
- 已知限制和后续方向

## 验收标准

1. 全量测试通过（200+ 项）
2. 所有新功能都能端到端跑通
3. 默认配置下性能不退化超过 10%
4. 文档完整、准确、与代码一致
5. 代码整洁，没有明显的冗余或 TODO
6. 所有入口（CLI/API/MCP/Gateway）都能正常工作
7. 安全边界完整，没有绕过 Governance 的路径

## 输出

1. 最终验收报告
2. 更新后的所有文档
3. 清理后的代码
4. Phase 2 完成标记和后续方向
```
