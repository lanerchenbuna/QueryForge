# Prompt 08: GovernanceAgent 与安全门禁

复制下面整段给 Codex 使用。

```text
继续上一轮。现在请强化 QueryForge 多 Agent 协作层的 GovernanceAgent 和安全门禁。

本轮目标：

参考 DW Agent Team 中 Governance 的思想，但迁移为本地 SQL Agent 可实现的安全、成本和质量 gate。GovernanceAgent 必须复用原项目已有 SQL guard、DatabaseTool 只读检查或相关能力，不能另写一套执行入口。

请先读取当前项目，再做增量修改。

GovernanceAgent 要求：

1. SQL 安全 gate
   - 只允许 SELECT / WITH。
   - 拒绝 DDL/DML。
   - 拒绝多语句。
   - 拒绝危险函数或可疑注入片段。

2. 成本风险 gate
   - 无 LIMIT 的大结果风险。
   - 无 WHERE 的大表扫描风险。
   - 多表 join 无 join key 风险。
   - 聚合无 group by 合理性检查。

3. Schema 风险 gate
   - SQL 使用不存在的表或字段。
   - 字段名需要反引号但未处理。
   - join path 与 schema_plan 不一致。

4. 敏感字段 gate
   - 通过配置维护 sensitive column patterns。
   - 例如 email、phone、address、id_card。
   - 命中时输出 warning 或 checkpoint。

5. Checkpoint 触发
   - dangerous_sql_blocked：直接阻断。
   - high_cost_warning：要求用户确认或 `--auto-approve-risk`。
   - sensitive_field_warning：根据配置决定阻断或提示。

6. Artifact
   governance_report.json 包含：
   - decision: pass | warn | block
   - checks
   - risks
   - required_checkpoints
   - suggested_changes

7. 与 SQLDeveloperAgent / FixNode 集成
   - 如果 block 原因可修复，交给 FixNode。
   - 如果是危险 SQL，不允许自动修复后绕过，必须重新过 gate。

限制：

- 不需要完整 SQL AST parser；如已有 sqlglot 可以使用，否则用保守规则。
- 不做权限系统。
- 不做真实脱敏。
- 不允许 GovernanceAgent 执行 SQL。
- 不要绕过原 DatabaseTool / ExecuteSqlNode 的只读防护；GovernanceAgent 是额外 gate，不是替代。

输出要求：

- 直接修改文件。
- 给出 gate 列表。
- 给出 governance_report 示例。
- 给出危险 SQL 测试命令。
- 给出 high cost warning 测试命令。

验收标准：

- DROP/INSERT/UPDATE/DELETE 被阻断。
- 多语句被阻断。
- 高成本查询能产生 checkpoint。
- governance_report 写入 artifacts。
- SQL 执行前必须通过 GovernanceAgent。
```
