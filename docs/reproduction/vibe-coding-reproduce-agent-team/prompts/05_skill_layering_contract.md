# Prompt 05: Skills 三层加载契约

复制下面整段给 Codex 使用。

```text
继续上一轮。现在请把 QueryForge 原有 Skills 系统增强为类似 DW Agent Team 的三层加载契约。

本轮目标：

在不推翻原 Skills 系统的前提下，让多 Agent 协作层中的每个 Role Agent 都能按固定顺序加载：

1. role definition
2. base skill
3. task-specific skill
4. datasource overlay skill
5. runtime context

请先读取当前项目，再做增量修改。

实现要求：

1. Skills 目录结构
   建议：

   skills/
     base/
       sql_agent/SKILL.md
       safety/SKILL.md
     product_analyst/
       analyze_question/SKILL.md
     schema_architect/
       plan_schema/SKILL.md
     sql_developer/
       nl2sql/SKILL.md
       fix_sql/SKILL.md
     data_qa/
       validate_result/SKILL.md
       review_sql/SKILL.md
     governance/
       sql_safety_gate/SKILL.md
       cost_risk_gate/SKILL.md
     knowledge/
       retrieve_context/SKILL.md
     visualization/
       chart_selection/SKILL.md
     datasource/
       sqlite/SKILL.md
       duckdb/SKILL.md

2. skill.yml metadata
   增加字段：
   - name
   - layer: base | task | overlay
   - agent_roles
   - task_types
   - datasource_types
   - priority
   - enabled

3. SkillLoadingPlan
   对每次 Agent 调用生成加载计划：
   - role file
   - base skills
   - task skills
   - overlay skills
   - runtime context files/artifacts

4. 加载顺序
   必须固定：
   role -> base -> task -> overlay -> runtime context

5. 覆盖语义
   - overlay 可以覆盖 task skill 中与 SQL 方言、连接器、测试命令相关的要求。
   - task skill 可以覆盖 base skill 的一般流程。
   - safety base skill 不可被覆盖，只能更严格。

6. CLI
   - `--show-skill-plan`
   - `--skills`
   - `--disable-skill`

7. Artifact
   每个 Agent artifact 中记录：
   - selected_skills
   - skill_selection_reason
   - skill_plan_hash

限制：

- Skill 仍然是 prompt/context，不执行任意代码。
- 不实现远程 marketplace。
- 不实现权限系统，但要预留 enabled/disabled。
- 不要把所有技能一次加载到所有 Agent。
- 不要删除或重写前一阶段已经实现的 SkillRegistry / SkillManager；应在其基础上扩展。
- 不开启多 Agent 协作层时，原 Skills 注入方式仍可工作。

输出要求：

- 直接修改文件。
- 创建示例 Skills。
- 给出 SQLDeveloperAgent 的 skill loading 示例。
- 给出验证命令。

验收标准：

- 每个 Agent 能生成 skill loading plan。
- `--show-skill-plan` 可见加载顺序。
- SQLite overlay 能影响 SQL 方言要求。
- artifact 记录 selected_skills。
```
