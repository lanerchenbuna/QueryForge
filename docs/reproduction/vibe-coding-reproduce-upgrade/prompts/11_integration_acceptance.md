# Prompt 11: 进阶版集成验收

复制下面整段给 Codex 使用。

```text
继续上一轮。现在请对 QueryForge 进阶版做完整集成验收。

本轮目标：

不要新增大功能。请系统性验证前面所有升级能力是否能一起工作。

请先读取当前项目文件和 README，再执行检查。

验收范围：

1. 基础主链路
   - CLI 输入自然语言问题。
   - 生成 SQL。
   - 执行 SQL。
   - 输出 rows。
   - 使用已打包的 `sample_data/anime_streaming/anime_streaming.sqlite` 作为主测试数据库。

2. 多模型
   - list models 正常。
   - 至少一个 OpenAI-compatible provider 可运行。
   - 缺少 key 有友好提示。

3. Skills
   - list skills 正常。
   - 默认 enabled skill 会注入 GenSqlNode prompt。
   - 指定 skill 生效。
   - 禁用 skill 不注入。

4. DateParserNode
   - 最近 30 天 / last 30 days。
   - 本月 / this month。
   - 去年 / last year。
   - date_context 出现在 GenSqlNode prompt。

5. Plan Mode
   - 不确认不执行。
   - auto approve 可用于测试。

6. Reflect + Fix Retry
   - SQL 执行失败能尝试修复。
   - Reflect SUCCESS 能进入 output。
   - 超过 max_retries 能清晰失败。

7. SQL History Cache
   - 成功查询写入历史。
   - 能从 `sample_data/anime_streaming/success_story.csv` 导入动漫平台成功案例。
   - 能从 `sample_data/anime_streaming/reference_sql/` 导入参考 SQL。
   - 类似问题能检索历史。
   - history prompt 注入可见。

8. LanceDB Vector KB
   - 未启用时不影响运行。
   - 启用但依赖缺失时能降级。
   - 启用且配置完整时能 rebuild / search。
   - 能索引已打包的 `reference_sql/` 和 `reference_template/`。

9. Visualization
   - 排名问题生成 bar。
   - 时间序列生成 line。
   - 不适合图表时 fallback table。

10. Logging
   - 每次运行有 run_id。
   - 节点耗时可见。
   - 错误能定位节点。

11. API / MCP / Gateway
   - API health。
   - API ask。
   - Gateway webhook。
   - MCP 可用或降级清晰。

请执行：

1. 静态检查。
2. 依赖检查。
3. bundled sample data 检查。
4. smoke tests。
5. 端到端手工命令。
6. README 检查。

允许修改：

- 修复集成 bug。
- 补充最小测试。
- 修正 README。
- 调整错误信息。

不允许修改：

- 不新增大功能。
- 不大规模重构。
- 不引入生产级认证/权限。

输出要求：

- 给出验收报告。
- 列出运行过的命令。
- 列出通过项和失败项。
- 对失败项做最小修复。
- 最后给出“进阶版使用说明摘要”。

最终验收标准：

- 基础链路不回退。
- 每个增强点都有至少一个可验证入口。
- 缺少可选依赖时有降级策略。
- README 能指导新用户运行进阶版。
```
