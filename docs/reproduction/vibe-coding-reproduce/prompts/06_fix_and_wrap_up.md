# Prompt 06: 常见问题修复与收尾

复制下面整段给 Codex 使用。使用前请确保上一轮已经完成基础联调。

```text
继续上一轮。现在请做 QueryForge 的常见问题修复和交付收尾。

本轮目标：

把 MVP 整理成一个别人可以克隆、配置、运行、理解的最小项目。不要新增非核心功能。

请先读取上一轮验证报告和当前代码，然后完成以下工作。

常见问题修复清单：

1. LLM 输出不稳定
   - 如果模型返回 markdown、解释文本、单引号 JSON 或多余前后缀，确保解析逻辑尽量稳健。
   - 如果仍无法解析，错误信息必须告诉用户模型返回了什么摘要，以及期望格式是什么。

2. SQL 安全边界
   - 确保只允许单条 SELECT 或 WITH 查询。
   - 拒绝分号拼接多语句。
   - 拒绝常见写操作关键词。
   - 在错误信息中说明 MVP 是只读模式。

3. Schema 上下文过长
   - 如果数据库表很多，请加入简单限制，例如最多展示前 N 张表或每张表最多 N 列。
   - 但不要实现向量检索。
   - 超出限制时在 prompt 中提示 schema 被截断。

4. CLI 体验
   - 错误信息要清晰。
   - `--help` 要能说明所有参数。
   - `--show-workflow` 要输出节点顺序和每个节点职责。

5. README
   - 更新 QueryForge 的 README。
   - 必须包含：
     - 项目是什么
     - 它复现了 Datus-Agent 的哪些核心思想
     - 安装步骤
     - 配置 `.env`
     - 使用已打包的 `anime_streaming` sample database
     - 运行示例
     - 常见错误
     - 明确说明省略了哪些原项目功能

6. 最小测试
   - 如果项目里已有测试框架，补充最小测试。
   - 如果没有测试框架，可以提供一个 `python` 命令或 `scripts/smoke_test.py` 做 smoke test。
   - 至少覆盖：
     - sample database 存在性检查
     - `dim_anime`、`fact_subscription`、`fact_rating` 三张表可读取
     - list_tables
     - describe_table
     - SELECT 执行
     - 非只读 SQL 被拒绝
     - workflow 在无 API key 时给出友好错误

7. 最终项目边界说明
   - 在 README 或回复中明确：
     - 这是 MVP，不是生产级 SQL Agent。
     - 它没有 RAG、MCP、多端入口、多数据库、多模型 provider。
     - 它保留的是原项目最核心的 workflow + node + context + tool 思想。

限制条件：

- 不要新增 Web、API、MCP。
- 不要新增向量库。
- 不要新增复杂配置。
- 不要引入 Docker，除非项目已经有明确需要。
- 不要为了测试引入重型依赖。
- 不要把代码重写成大型框架。

输出要求：

1. 先列出你发现的问题。
2. 做最小必要修改。
3. 给出最终文件结构。
4. 给出从零运行命令。
5. 给出最终验收清单。
6. 说明这个 MVP 与原 Datus-Agent 的对应关系。

最终验收标准：

- 新用户能按 README 在 10 分钟内跑起来。
- 已打包的 `anime_streaming` sample database 可用于试运行。
- 至少一个自然语言问题可以端到端返回 SQL 和结果。
- 无 API key、危险 SQL、数据库缺失等常见问题都有清晰提示。
- 项目结构仍然简单。
- README 明确说明复现范围和省略范围。
```
