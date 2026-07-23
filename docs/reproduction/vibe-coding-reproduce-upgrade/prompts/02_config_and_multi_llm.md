# Prompt 02: 配置系统与多 LLM 供应商

复制下面整段给 Codex 使用。

```text
继续上一轮。现在请在 QueryForge 中实现轻量级多 LLM 供应商系统。

本轮目标：

把基础版单一 OpenAI 调用升级为 provider factory，支持：

- OpenAI
- Claude
- Gemini
- DeepSeek
- Qwen
- GLM

这是后续 ReflectNode、FixNode、Plan Mode、Skills prompt 注入的基础。

请先读取当前项目文件，再做增量修改。

设计要求：

1. 配置结构
   - 支持 `.env` 环境变量。
   - 支持一个简单 `config.yml` 或 `models.yml`。
   - 用户可以选择 active provider 和 model。
   - 每个 provider 至少包含：
     - type
     - model
     - api_key env name
     - base_url 可选
   - 不要实现原项目复杂的全局/项目级覆盖系统，只做轻量版本。

2. 模型抽象
   - 定义统一接口：
     - generate_text
     - generate_json
     - generate_with_messages
   - 所有 provider adapter 输出统一格式。
   - 失败时返回清晰错误。

3. Provider adapter
   - OpenAI、DeepSeek、Qwen、GLM 可以优先走 OpenAI-compatible HTTP/chat completions。
   - Claude、Gemini 可以实现简化 adapter；如果没有 SDK，先通过清晰接口和 TODO fallback 保持可扩展。
   - 不要把所有 provider 的逻辑堆在一个函数里。

4. CLI
   - 增加 `--model-provider` 和 `--model`。
   - 增加 `--list-models`。
   - 运行时打印当前使用的 provider/model。

5. GenSqlNode
   - 改为通过 ModelFactory 获取模型。
   - 不直接依赖 OpenAI 类。

6. 测试/验证
   - 不要求所有 provider 都真实调用成功，因为用户可能没有所有 key。
   - 至少要验证：
     - list models 正常。
     - 缺少 key 时提示友好。
     - OpenAI-compatible provider 能按配置构造请求。

限制：

- 不引入 LiteLLM，除非你能说明它显著简化实现。
- 不做 OAuth。
- 不做模型缓存 LRU，最多保留一个简单实例缓存。
- 不做流式输出。

输出要求：

- 直接修改文件。
- 给出新增/修改文件清单。
- 给出配置示例。
- 给出验证命令。
- 说明每个 provider 的支持等级：已实现、OpenAI-compatible、占位扩展。

验收标准：

- 现有 SQL 生成流程仍能运行。
- GenSqlNode 不再绑定单一 OpenAI 类。
- CLI 能选择 provider/model。
- 缺少 API key 时有清晰错误。
```
