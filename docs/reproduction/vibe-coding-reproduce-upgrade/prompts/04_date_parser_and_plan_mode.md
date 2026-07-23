# Prompt 04: DateParserNode 与 Plan Mode

复制下面整段给 Codex 使用。

```text
继续上一轮。现在请为 QueryForge 增加 DateParserNode 和 Plan Mode。

本轮目标：

1. DateParserNode：把“最近 30 天”“本月”“上季度”“2024 年”等自然语言日期解析成明确日期上下文。
2. Plan Mode：SQL 执行前先展示计划，用户确认后再执行。

请先读取当前项目文件，再做增量修改。

DateParserNode 要求：

1. 放在 schema_linking 或 gen_sql 之前。
2. 输入：SqlTask.question、当前日期。
3. 输出：context.date_context。
4. 支持规则优先：
   - today
   - yesterday
   - last N days
   - this month
   - last month
   - this quarter
   - last quarter
   - this year
   - last year
   - YYYY-MM-DD
5. 对中文表达也做基础支持：
   - 今天
   - 昨天
   - 最近 N 天
   - 本月
   - 上月
   - 今年
   - 去年
6. 可选 LLM fallback：
   - 如果规则解析不到，允许调用当前 LLM 输出结构化日期。
   - fallback 必须可关闭。
7. GenSqlNode prompt 要注入 date_context。

Plan Mode 要求：

1. 增加 `--plan-mode`。
2. 在执行 SQL 前生成执行计划。
3. 计划内容至少包含：
   - 用户问题
   - 识别的表
   - 日期上下文
   - 即将执行的 SQL
   - 风险提示
4. CLI 交互：
   - 用户输入 yes 才执行。
   - 输入 no 或回车默认不执行。
5. 非交互模式：
   - 增加 `--auto-approve-plan`，用于测试或 API 调用。

Workflow 调整：

建议进阶 workflow：

date_parser -> schema_linking -> gen_sql -> plan_mode_check -> execute_sql -> output

也可以把 plan mode 实现在 WorkflowRunner 中，但必须说明原因。

限制：

- 不要做复杂自然语言时间库。
- 不要引入大型依赖。
- 不要在 plan mode 中执行 SQL。
- 不要让 plan mode 依赖人工输入导致测试无法运行，必须提供 auto approve。

输出要求：

- 直接修改文件。
- 说明 workflow 顺序变化。
- 给出日期解析示例。
- 给出 plan mode 交互示例。
- 给出验证命令。

验收标准：

- 包含日期的问题能在 prompt 中注入明确日期范围。
- `--plan-mode` 会先展示计划，不确认不执行 SQL。
- `--auto-approve-plan` 可以让测试自动通过。
- 原有无日期问题仍正常运行。
```
