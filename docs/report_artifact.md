# Static Report Artifact

Report Artifact 在 SQL 已执行、QA 和可视化完成后生成静态 HTML。它不会改变 SQL 生成、治理或
执行路径；生成失败会保留查询输出并将 `report_artifact` 标记为 `degraded`。

## 触发方式

- 使用 `build_report` 意图，例如 “Build report for sales by category”。
- CLI 显式传入 `--report`。
- REST `/ask` 的 `report: true`。

```bash
queryforge --report \
  --report-output-dir .queryforge/reports \
  --question "Show sales by category"
```

配置项：

- `REPORT_ENABLED=true`
- `REPORT_MAX_ROWS=50`
- `REPORT_MAX_CHARTS=3`
- `REPORT_OUTPUT_DIR=.queryforge/reports`

## 内容与文件

每次成功生成两个文件：

```text
.queryforge/reports/
  qf_<run_id>.html
  qf_<run_id>.manifest.json
```

HTML 包含内联 CSS、结果表、指标卡片、可折叠 SQL、规则生成的发现和 Vega-Lite 图表配置。图表
优先使用 `vega-embed` CDN；同时会生成内联 SVG fallback，因此浏览器无法加载 CDN 时仍能
离线查看基本柱状图或折线图、表格、指标、SQL 和发现。

`ReportGenerator` 依据结果列选择折线图、柱状图或指标卡片，并用确定性规则生成行数、最大值/
最小值、Top 维度等发现。最终输出的 `report` 和 `report_artifact` 都包含 HTML 与 manifest
的绝对路径。

REST 提供 `GET /report/{run_id}` 下载默认报告目录中的 HTML；`run_id` 经过路径安全校验。
