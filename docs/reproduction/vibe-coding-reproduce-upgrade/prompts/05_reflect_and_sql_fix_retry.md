# Prompt 05: ReflectNode 与 SQL 修复重试

复制下面整段给 Codex 使用。

```text
继续上一轮。现在请为 QueryForge 增加 ReflectNode 和 SQL 修复重试机制。

本轮目标：

让系统不只是“生成一次 SQL 然后执行”，而是具备基础自我评估和修复能力。

请先读取当前项目文件，再做增量修改。

新增节点：

1. ReflectNode
   位置：execute_sql 之后，output 之前。
   输入：
   - 用户问题
   - 生成的 SQL
   - SQL 解释
   - 执行结果摘要
   - 表结构
   - 错误信息，如果有
   输出：
   - success
   - strategy
   - reason
   - suggested_fix 可选

2. FixNode
   位置：当 SQL 执行失败或 ReflectNode 要求修复时触发。
   输入：
   - 用户问题
   - 原 SQL
   - 执行错误
   - 表结构
   - date_context
   - skills_context
   输出：
   - fixed_sql
   - explanation

Reflect strategy 最小集合：

- SUCCESS：结果可以接受，进入 output。
- FIX_SQL：SQL 有错误或结果明显不对，进入 FixNode。
- REGENERATE：当前 SQL 偏离问题，回到 GenSqlNode。
- NEED_USER_REVIEW：模型无法判断，停止并输出人工审阅提示。

Workflow 控制流：

- 增加 max_retries，默认 2。
- execute_sql 失败时优先进入 FixNode。
- FixNode 生成新 SQL 后重新执行。
- execute_sql 成功后进入 ReflectNode。
- ReflectNode 返回 FIX_SQL 或 REGENERATE 时，未超过 max_retries 才重试。
- 超过 max_retries 后停止，并输出最后错误和尝试历史。

Context 新增字段：

- reflection_result
- fix_attempts
- retry_count
- execution_errors
- sql_attempt_history

LLM prompt 要求：

- ReflectNode prompt 必须让模型只输出 JSON。
- FixNode prompt 必须要求只生成 SQLite SELECT 或 WITH 查询。
- 修复 SQL 不允许 DDL/DML。

限制：

- 不实现复杂动态 DAG。
- 不实现并行 selection。
- 不做无限循环。
- 不把错误直接隐藏，必须保留尝试历史。

输出要求：

- 直接修改文件。
- 说明 workflow 控制流。
- 给出 ReflectNode 和 FixNode 的 prompt 结构。
- 给出验证命令。
- 给出一个故意错误 SQL 的修复测试方式。

验收标准：

- SQL 执行失败时会尝试自动修复。
- ReflectNode 能判断成功并进入 output。
- 超过最大重试次数后有清晰错误。
- 所有尝试会记录在 context 或输出摘要中。
```
