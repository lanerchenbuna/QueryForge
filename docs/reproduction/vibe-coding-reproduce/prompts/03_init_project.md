# Prompt 03: 技术栈与目录结构初始化

复制下面整段给 Codex 使用。使用前请确保上一轮已经产出 MVP 技术设计。

```text
继续上一轮。现在请开始初始化 QueryForge 项目。

本轮目标：

只创建项目骨架、基础配置、数据模型、CLI 入口和可运行的空 workflow。不要实现 LLM 生成 SQL 和真实数据库逻辑，这些放到下一轮。

项目目标回顾：

- 复现 Datus-Agent 的最小主链路。
- 主流程是：CLI -> WorkflowRunner -> Workflow -> schema_linking -> gen_sql -> execute_sql -> output。
- 本轮只搭骨架，确保项目可以启动，并能打印“workflow 尚未实现或 mock 结果”之类的占位输出。

请创建一个目录 `QueryForge/`，建议结构如下：

```text
QueryForge/
  README.md
  requirements.txt
  .env.example
  main.py
  config.py
  schemas/
    __init__.py
    models.py
  agent/
    __init__.py
    workflow.py
    workflow_runner.py
    node/
      __init__.py
      base.py
      schema_linking_node.py
      gen_sql_node.py
      execute_sql_node.py
      output_node.py
  models/
    __init__.py
    llm.py
  tools/
    __init__.py
    database_tool.py
  db/
    __init__.py
    sqlite_connector.py
  sample_data/
    anime_streaming/
      README.md
      anime_streaming.sqlite
      tables/*.csv
      success_story.csv
      reference_sql/
      reference_template/
  sample/
    prepare_sample_data.py
```

你可以根据上一轮设计做少量调整，但必须解释原因。

本轮需要完成的内容：

1. `requirements.txt`
   - 只列必要依赖。
   - 必须包含 openai、python-dotenv、pydantic。
   - 如果使用 rich，也说明它只是显示增强，不是核心依赖。

2. `.env.example`
   - 包含 OPENAI_API_KEY。
   - 包含 OPENAI_MODEL，给出默认模型名。
   - 包含 DATABASE_PATH，默认指向 sample_data/anime_streaming/anime_streaming.sqlite。
   - 不要创建真实 `.env`，避免写入敏感信息。

3. `config.py`
   - 读取环境变量。
   - 提供一个简单配置对象或字典。
   - 对缺失 API key 不要在导入阶段报错，应在运行阶段给出清晰提示。

4. `schemas/models.py`
   - 定义 SqlTask、Context、TableSchema、SQLContext、NodeResult。
   - 字段要足够支撑后续四个节点的数据传递。
   - 注意避免 Python 可变默认值问题。

5. `agent/node/base.py`
   - 定义 Node 基类。
   - 定义统一 execute(context) 接口。
   - 定义 node name / description / status 等最小元信息。

6. 四个节点文件
   - 先写成可运行的占位实现。
   - 每个节点都接收 Context，返回 NodeResult。
   - 每个节点都明确自己将来要读写 Context 的哪些字段。

7. `agent/workflow.py`
   - 支持按固定顺序执行节点。
   - 执行失败时停止并返回错误。
   - 成功时返回 Context 中的 final_output。

8. `agent/workflow_runner.py`
   - 接收 config。
   - 创建 SqlTask 对应的 Workflow。
   - 按固定顺序装配四个节点。

9. `main.py`
   - 使用 argparse。
   - 支持参数：
     - `--question`
     - `--database`
     - `--show-workflow`
   - 能启动 WorkflowRunner。

10. `sample_data/anime_streaming/`
    - 从本 prompt 文件夹的 `sample_data/anime_streaming/` 复制到 `QueryForge/sample_data/anime_streaming/`。
    - 这套数据来自QueryForge 专门生成的动漫平台场景，包含 `anime_streaming.sqlite`、CSV、参考 SQL、参考模板和 success story。
    - 本轮只要求复制并在 README 中说明，不需要重新生成数据库。

11. `sample/prepare_sample_data.py`
    - 提供一个轻量脚本，检查 bundled sample data 是否存在。
    - 如果目标路径没有 `anime_streaming.sqlite`，提示用户从 prompt 交付物复制。
    - 不要再默认创建 users/anime/watch sessions 这种临时 mock 数据。

输出要求：

- 请直接创建或修改文件。
- 不要只给建议。
- 不要实现复杂业务逻辑。
- 完成后说明你创建了哪些文件，每个文件现在的职责是什么。
- 给出本轮验证命令。

限制条件：

- 不实现真实 LLM 调用。
- 不执行真实 SQL。
- 不引入 Web/API/MCP。
- 不引入向量库。
- 不写过度抽象的插件系统或注册系统。

验收标准：

- `python main.py --show-workflow --question "What is the phone number of the anime with the highest total watch time?"` 可以运行。
- 程序能展示固定 workflow 顺序。
- 程序能返回一个明确的占位 final output 或未实现提示。
- 项目中包含 `sample_data/anime_streaming/anime_streaming.sqlite` 或清晰说明如何从交付物复制。
- 项目结构清晰，下一轮可以直接补数据库、LLM 和节点逻辑。

下一轮衔接：

完成后，请在回复最后列出“核心功能开发待办清单”，至少包含 SQLite connector、DatabaseTool、SchemaLinkingNode、GenSqlNode、ExecuteSqlNode、OutputNode。
```
