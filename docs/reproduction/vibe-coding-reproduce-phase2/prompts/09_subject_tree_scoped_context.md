# Prompt 09: Subject Tree——主题范围约束与 Context 裁剪

复制下面整段给 Codex 使用。

```text
继续上一轮。Parallel + Selection 已经实现。

本轮目标：实现 Subject Tree（主题树），对查询进行范围约束，减少 token 消耗，降低跨域误用风险。

## 背景

当前 QueryForge 把所有 Schema 都塞进 Prompt 里。数据库表少的时候还好，表多了之后：
1. Token 消耗大
2. 模型容易混淆不同业务域的表
3. 跨域误用（比如把销售表和人力表乱 Join）

Subject Tree 通过定义"主题"来约束查询范围：每个主题包含一组相关的表、指标、Skills 和知识源。查询时先选主题，再在主题范围内工作。

## 本轮要做的事

### 1. Subject 定义

新增 Subject 模型（放在 queryforge/semantic/subject.py 或 queryforge/agent_team/schemas/）：

```python
class Subject(BaseModel):
    id: str                           # 主题 ID
    name: str                         # 显示名称
    description: str                  # 描述
    synonyms: list[str] = []          # 同义词
    tables: list[str] = []            # 包含的表
    entities: list[str] = []          # 包含的语义模型实体
    metrics: list[str] = []           # 包含的指标
    skills: list[str] = []            # 相关的 Skills
    knowledge_sources: list[str] = [] # 相关的知识源（reference_sql、documents 等）
    default_time_field: str | None = None  # 默认时间字段
    default_grain: str | None = None  # 默认粒度
    priority: int = 0                 # 优先级（用于歧义时选择）
```

### 2. Subject Tree 定义

```python
class SubjectTree(BaseModel):
    version: str = "1.0"
    subjects: list[Subject]
    default_subject: str | None = None
```

### 3. 配置文件

Subject 用 YAML 文件定义，放在语义模型目录下或单独的 subject 目录：

```yaml
# subjects.yml
version: "1.0"
default_subject: sales
subjects:
  - id: sales
    name: 销售分析
    description: 订单、客户、产品销售数据分析
    synonyms: ["销售额", "订单", "营收"]
    tables: ["fact_watch_session", "dim_user", "dim_anime", "dim_date"]
    entities: ["订单", "客户", "产品", "日期"]
    metrics: ["销售额", "订单量", "客单价"]
    skills: ["sales_best_practices"]
    knowledge_sources: ["sales_reference_sql"]

  - id: inventory
    name: 库存分析
    description: 库存、仓储、供应链分析
    synonyms: ["库存", "仓库", "存货"]
    tables: ["fact_inventory", "dim_anime", "dim_warehouse"]
    entities: ["库存", "产品", "仓库"]
    metrics: ["库存量", "库存周转"]
    skills: ["inventory_best_practices"]
```

### 4. Subject 选择

在 ProductAnalyst 阶段（或 SchemaArchitect 之前）自动选择主题。

选择策略（规则实现，不用 LLM）：
1. 关键词匹配：问题中的关键词和主题的 name/synonyms 匹配
2. 指标/维度匹配：问题中提到的指标属于哪个主题
3. 默认主题：如果都不匹配，使用 default_subject
4. 无默认主题：使用全部 Schema（降级）

选择结果存到 Context 和 artifact 中。

### 5. Context 裁剪

选择主题后，对 Context 中的 Schema 信息进行裁剪：
- 只保留主题内的表
- 只保留主题内的语义模型实体和指标
- 只加载主题相关的 Skills
- 只检索主题相关的历史 SQL 和知识库

裁剪发生在 SchemaLinkingNode 之前还是之后？

建议：**在 SchemaLinkingNode 之前选择主题，SchemaLinkingNode 只加载主题内的表**。这样可以减少 Schema 加载和 token 消耗。

但要注意：如果主题选择错了，后续就找不到正确的表了。所以需要有 fallback 机制：
- 如果在主题内找不到相关表，自动扩大到全部 Schema
- 或者在 Reflect 阶段检测到"表不存在"时，自动切换主题重试

### 6. 主题切换与重试

ReflectNode 检测到以下情况时，可以触发主题切换重试：
- 引用了不存在的表
- 表之间无法 Join（可能选错了主题）
- 结果完全不符合预期

切换策略：
- 第一次失败后，尝试第二匹配的主题
- 如果都失败了，使用全部 Schema
- 最多切换 1-2 次主题，避免无限循环

### 7. Agent Team 集成

在 Agent Team 架构中，Subject Tree 的位置：

```
EntryRouter
  → ProductAnalystAgent（识别主题关键词）
  → SubjectSelector（选择主题，新增角色/阶段）
  → SchemaArchitectAgent（在主题范围内规划）
  → KnowledgeAgent（只检索主题相关知识）
  → GenSqlNode（只看到主题内的 Schema）
  → execution
  → Reflect（检测是否需要切换主题）
  → completion
```

SubjectSelector 可以作为 SchemaArchitectAgent 的一部分，也可以独立。建议独立，职责更清晰。

### 8. 配置和入口

配置项：
- `subject_tree_enabled: bool`，默认 false（数据库表少的时候不需要）
- `subject_tree_path: str`，subjects.yml 的路径
- `default_subject: str`，默认主题

CLI：
- 新增 --subject 参数，手动指定主题
- 新增 --list-subjects 参数，列出所有主题

API：
- AskRequest 增加 subject 字段

## 限制

- 默认关闭，不影响现有行为
- 主题选择是启发式的，可能选错
- 选错时有 fallback 机制（扩大范围重试）
- 不改变 SQL 生成和执行逻辑
- 不引入 LLM 调用（主题选择用规则实现）

## 验收标准

1. subject_tree_enabled=false 时，行为和以前完全一样
2. 可以用 YAML 定义多个主题
3. 主题选择能正确匹配关键词和指标
4. Schema 裁剪生效：只加载主题内的表
5. Skills 加载生效：只加载主题相关的 Skills
6. 历史 SQL 检索生效：只检索主题相关的历史
7. 选错主题时有 fallback 机制
8. 可以手动指定主题
9. 新增测试覆盖：主题选择、Schema 裁剪、fallback 重试、手动指定
10. ask_sql 全量测试通过

## 输出要求

1. 先读取现有代码，确认 SchemaLinkingNode 和 ProductAnalystAgent 的实现
2. 给出 Subject Tree 设计和集成方案
3. 实现代码
4. 编写测试
5. 运行测试，确保全量通过
```
