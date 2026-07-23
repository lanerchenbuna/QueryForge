# Prompt 12: Report Artifact——静态 HTML 分析报告

复制下面整段给 Codex 使用。

```text
继续上一轮。Streaming Output 已经实现。

本轮目标：实现 Report Artifact，从单张图升级为完整的静态 HTML 分析报告。

## 背景

当前 VisualizationAgent 只能生成一张 Vega-Lite 图表配置。对于复杂的分析任务，用户需要的是一份完整的报告：
- 问题和分析目标
- SQL 和口径说明
- 结果表格
- 多张图表（趋势、对比、分布等）
- 关键发现

Report Artifact 把这些整合为一份静态 HTML 报告，可以直接在浏览器打开，也可以分享给别人。

## 本轮要做的事

### 1. Report 模型

新增 Report 模型（放在 queryforge/agent_team/schemas/report.py）：

```python
class ReportArtifact(BaseModel):
    artifact_type: str = "report"
    title: str
    summary: str
    sections: list[ReportSection]
    generated_at: str
    data_source: str
    sql: str
    metrics: list[str]
    dimensions: list[str]
    key_findings: list[str]
    file_path: str  # HTML 文件路径
```

class ReportSection(BaseModel):
    id: str
    title: str
    type: str  # text / table / chart / metrics
    content: dict  # 根据 type 有不同结构

### 2. ReportGenerator

新增 ReportGenerator 类（放在 queryforge/agent/report_generator.py）：

职责：
- 根据查询结果生成 HTML 报告
- 支持多种 section 类型
- 输出自包含的静态 HTML（CSS 内联）

Section 类型：
- `text`: 文本段落
- `table`: 数据表格
- `chart`: Vega-Lite 图表（用 vega-embed 渲染）
- `metrics`: 指标卡片
- `sql`: SQL 代码块（可折叠）

### 3. 报告内容结构

一份标准报告包含：

1. **Header**: 标题、生成时间、数据源
2. **Summary**: 一句话总结 + 关键指标卡片
3. **Result Table**: 结果表格（最多展示 50 行，超过的话提示下载）
4. **Charts**: 自动生成 1-3 张图表（根据数据特征选择）
5. **SQL & Methodology**: SQL 代码 + 口径说明 + 假设
6. **Key Findings**: 3-5 条关键发现（基于结果和反射）
7. **Footer**: 生成工具、版本、免责声明

### 4. 图表自动选择

根据结果 schema 和数据特征自动选择图表类型：

| 数据特征 | 推荐图表 |
|---------|---------|
| 1 个时间维度 + 1 个度量 | 折线图 |
| 1 个类别维度 + 1 个度量 | 柱状图 |
| 2 个类别维度 + 1 个度量 | 分组柱状图 |
| 1 个类别维度 + 多个度量 | 分组柱状图或折线图 |
| 只有度量，没有维度 | 指标卡片 |
| 占比数据 | 饼图（慎用） |
| 2 个度量 + 类别 | 散点图 |

图表选择逻辑用规则实现，不用 LLM。

### 5. 关键发现生成

基于结果数据自动生成 3-5 条关键发现：

发现类型：
- 最大值/最小值
- 增长率/下降率
- Top N
- 异常值（突增突降）
- 分布特征

用规则实现，不用 LLM（或者用 LLM 但作为可选增强）。

### 6. Agent Team 集成

Report 是 completion 阶段的一部分，在 DataQA 和 Visualization 之后：

```
execution
  → DataQA
  → Visualization
  → ReportGenerator（新增）
  → Ops
  → Delivery
```

ReportAgent（新增角色）负责：
- 判断是否需要生成报告
- 选择图表类型
- 生成关键发现
- 生成 HTML 文件
- 返回 report artifact

触发条件：
- task_type 是 build_report
- 或者用户问题中提到"报告"、"report"
- 或者结果行数超过阈值且有多个维度

### 7. 输出和下载

报告保存位置：
- `.queryforge/reports/<run_id>.html`

CLI：
- 新增 --report 参数，强制生成报告
- 生成后输出报告路径

API：
- 新增 `GET /report/{run_id}` 端点，下载 HTML 报告
- 或者在结果中包含 report_url

### 8. 配置

配置项：
- `report_enabled: bool`，默认 true（支持但不强制）
- `report_max_rows: int`，默认 50（表格展示行数）
- `report_max_charts: int`，默认 3
- `report_output_dir: str`，默认 `.queryforge/reports`

## 限制

- 报告是静态 HTML，不依赖后端
- 图表用 Vega-Lite + vega-embed CDN，不需要额外安装
- 不做交互式报告（过滤、钻取等），那是 BI 工具的事
- 不改变 SQL 生成和执行逻辑
- 报告生成不阻塞主流程（失败了就跳过，标记 degraded）

## 验收标准

1. 能生成自包含的 HTML 报告
2. 报告包含：标题、摘要、表格、图表、SQL、关键发现
3. 能根据数据特征自动选择图表类型
4. 至少支持 3 种图表类型：折线图、柱状图、指标卡片
5. 关键发现用规则生成，至少 3 种类型
6. 报告不依赖后端，可以直接在浏览器打开
7. 报告生成失败不阻塞主流程
8. CLI 和 API 都能生成和获取报告
9. 新增测试覆盖：报告生成、图表选择、关键发现、HTML 渲染
10. ask_sql 全量测试通过

## 输出要求

1. 先读取现有代码，确认 VisualizationAgent 和 OutputNode 的实现
2. 给出 Report 模型和生成器设计
3. 实现代码
4. 编写测试
5. 运行测试，确保全量通过
```
