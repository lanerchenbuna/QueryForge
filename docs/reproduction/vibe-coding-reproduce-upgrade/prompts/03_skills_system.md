# Prompt 03: Skills 系统

复制下面整段给 Codex 使用。

```text
继续上一轮。现在请为 QueryForge 增加 Skills 系统。

本轮目标：

复现 Datus-Agent 中 Skills 的核心思想：技能不是普通代码插件，而是一组可被模型读取的本地说明，用来影响 Agent 的行为、SQL 风格、业务规则和工具使用方式。

请先读取当前项目文件，再做增量修改。

实现范围：

1. 本地 skills 目录
   建议结构：

   skills/
     sql_best_practices/
       SKILL.md
       skill.yml
     business_rules/
       SKILL.md
       skill.yml

2. skill.yml metadata
   至少支持：
   - name
   - description
   - allowed_nodes
   - enabled
   - priority

3. SkillRegistry
   - 扫描 skills 目录。
   - 读取 metadata。
   - 校验 SKILL.md 是否存在。
   - 返回可用技能列表。

4. SkillManager
   - 根据 node name 过滤技能。
   - 加载技能内容。
   - 拼接成 `<available_skills>` 和 `<loaded_skills>` 文本块。
   - 提供给 GenSqlNode、ReflectNode、FixNode 使用。

5. CLI
   - 增加 `--list-skills`。
   - 增加 `--skills skill_a,skill_b` 用于手动启用技能。
   - 如果未指定，则加载 enabled=true 且适用于当前节点的技能。

6. GenSqlNode 集成
   - 在 prompt 中注入可用 Skills 摘要。
   - 注入已加载 Skills 正文。
   - 明确告诉模型：Skills 是约束和业务规则，不是数据表。

7. 示例 Skills
   - SQL 最佳实践：只读查询、避免 SELECT *、加 LIMIT、使用清晰别名。
   - 业务规则示例：订单金额字段含义、取消订单是否排除等。

限制：

- 不实现远程 marketplace。
- 不实现权限系统。
- 不允许 Skill 直接执行任意代码。
- Skill 本轮只作为 prompt context，不作为 Python plugin。
- 不要过度设计复杂包管理。

输出要求：

- 直接修改或新增文件。
- 给出 Skills 目录结构。
- 给出两个示例 Skill 的内容。
- 给出如何在 SQL 生成 prompt 中注入 Skills 的说明。
- 给出验证命令。

验收标准：

- `--list-skills` 能列出本地 Skills。
- GenSqlNode prompt 中能包含已加载 Skill 内容。
- 未指定 Skills 时能自动加载 enabled 且适用于 gen_sql 的 Skills。
- 禁用 Skill 后不会注入。
- 原有 SQL 生成流程不被破坏。
```
