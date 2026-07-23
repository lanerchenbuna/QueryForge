# Subject Tree

Subject Tree 用 YAML 将 Schema、语义实体、指标、Skills 和检索上下文限制在一个业务主题内。
它默认关闭；不开启时 QueryForge 保持全量 Schema 的既有行为。

## 配置

可参考 [动漫流媒体样例](../sample_data/anime_streaming/subjects.yml)：

```yaml
version: "1.0"
default_subject: engagement
subjects:
  - id: engagement
    name: Viewer engagement analytics
    synonyms: [watch, viewer, completion, rating]
    tables: [fact_watch_session, dim_user, dim_episode, dim_anime]
    entities: [watch_session, user, episode, anime]
    metrics: [watch_hours, completion_rate, unique_viewers]
    skills: [business_rules]
    knowledge_sources: [viewer_engagement]
    default_time_field: fact_watch_session.watch_date_key
    default_grain: watch_session
    priority: 10
```

主题由问题中的名称、同义词、指标、实体和表名进行规则匹配。若没有匹配项，则使用
`default_subject`；若没有默认主题，或主题未包含当前数据库的任何表，则自动回退为全量
Schema，并在响应的 `subject` 中说明原因。

## 使用

```bash
queryforge --list-subjects \
  --subject-tree sample_data/anime_streaming/subjects.yml

queryforge \
  --database sample_data/anime_streaming/anime_streaming.sqlite \
  --enable-subject-tree \
  --subject-tree sample_data/anime_streaming/subjects.yml \
  --subject engagement \
  --question "Show monthly watch hours by subscription tier"
```

REST `/ask` 和 `/plan` 接受 `subject_tree_enabled`、`subject_tree_path`、`subject` 和
`default_subject`；MCP `ask_sql` 使用同名参数，另提供 `list_subjects`。

启用后，`SchemaLinkingNode` 仍先以全量物理 Schema 校验语义模型，再将可见 Schema、语义
实体与指标裁剪到主题范围。历史 SQL、参考 SQL 和主题声明的 Skills 也会被过滤；正式 SQL
执行仍由原有 SQL AST 策略和 Governance 控制。
