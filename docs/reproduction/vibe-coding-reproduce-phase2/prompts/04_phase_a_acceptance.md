# Prompt 04: Phase A 集成验收

复制下面整段给 Codex 使用。

```text
Phase A（填平补齐）的三个 prompt 已经完成。本轮做集成验收，确保所有改动稳定、一致、可维护。

## 验收范围

1. 路由系统：sql_review / metadata_query / troubleshoot_sql / explain_result / build_report 各 pipeline 是否正常工作
2. 角色升级：ProductAnalyst / SchemaArchitect / KnowledgeAgent 的决策能力
3. 质量门禁：artifact 三级状态、阶段推进、blocked/warning 处理
4. 回归验证：ask_sql 主链路不退化

## 验收清单

### 1. 路由系统验收

- [ ] EntryRouter 分类正确，所有 7 种 task_type 都能识别
- [ ] ask_sql 行为与 Phase A 之前完全一致
- [ ] sql_review 返回 review_report，不执行 SQL
- [ ] metadata_query 返回表结构和指标，不走 GenSQL
- [ ] troubleshoot_sql 能从问题提取 SQL 并修复
- [ ] explain_result 返回详细解释报告
- [ ] build_report 返回 degraded report artifact
- [ ] unknown 回退到 ask_sql 行为
- [ ] 每种 pipeline 都有 state.json 和 delivery_report

### 2. 角色升级验收

- [ ] ProductAnalystAgent 能检测至少 3 类模糊点
- [ ] analysis_request 有 valid/warning/blocked 三级
- [ ] blocked 状态会终止流程
- [ ] warning 状态传递到最终结果
- [ ] SchemaArchitectAgent 能推荐 Join Path 并评估风险
- [ ] schema_plan 有表选择、Join 路径、字段建议、风险
- [ ] KnowledgeAgent 对历史 SQL 排序和去重
- [ ] 所有角色都有降级策略

### 3. 质量门禁验收

- [ ] 每个 artifact 都有 status 字段
- [ ] analysis 阶段有门禁
- [ ] schema 阶段有门禁
- [ ] sql_candidate 阶段有门禁
- [ ] execution 阶段有门禁
- [ ] qa 阶段有门禁
- [ ] TaskState 完整记录阶段推进
- [ ] Artifact schema 校验

### 4. 回归验收

- [ ] 全量单元测试通过
- [ ] ask_sql 输出结构不变（新增字段不算破坏）
- [ ] 所有入口（CLI/API/MCP/Gateway）都能正常工作
- [ ] 性能没有明显退化（简单查询不增加额外 LLM 调用）
- [ ] 文档更新：README、架构文档、配置说明

## 要做的事

### 1. 编写集成验收测试

新建 tests/test_phasea_integration.py，包含：

- 路由集成测试：每种 pipeline 至少 2 个用例
- 角色升级测试：模糊点检测、Join Path 推荐、blocked/warning 处理
- 质量门禁测试：各阶段门禁、阶段推进、artifact 校验
- 端到端测试：从入口到输出的完整链路

### 2. 更新文档

- 更新 README.md：说明 Phase A 新增的能力
- 更新 docs/agent_team_architecture.md：更新架构图和角色职责
- 更新 docs/future_extensions.md：标记 Phase A 完成的项

### 3. 性能基线

简单度量（不用精确）：
- 开启角色升级前后的简单查询耗时对比
- artifact 写入的开销
- 确认没有引入额外的 LLM 调用

### 4. Bug 修复和清理

- 修复验收中发现的问题
- 清理冗余代码
- 统一命名和代码风格

## 验收标准

1. 全量测试通过（至少 160+ 项，原 156 项 + 新增）
2. ask_sql 输出向后兼容
3. 所有 pipeline 都能端到端跑通
4. 文档与代码一致
5. 没有新增 LLM 调用（Phase A 全是规则）
6. 代码整洁，没有明显的冗余或 TODO

## 输出

1. 集成验收报告（测试结果、性能对比、问题清单）
2. 更新后的文档
3. 修复后的代码
4. Phase A 完成标记
```
