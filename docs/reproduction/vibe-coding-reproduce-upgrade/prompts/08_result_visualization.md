# Prompt 08: 结果可视化

复制下面整段给 Codex 使用。

```text
继续上一轮。现在请为 QueryForge 增加简单结果可视化能力。

本轮目标：

当 SQL 查询结果适合可视化时，自动生成一个简单图表配置或本地 HTML 图表，让用户不只看到 rows，还能看到趋势、排名或分布。

请先读取当前项目文件，再做增量修改。

实现范围：

1. VisualizationNode
   - 放在 OutputNode 之后或集成到 OutputNode 末尾。
   - 输入：
     - question
     - sql
     - columns
     - rows
   - 输出：
     - chart_type
     - chart_config
     - chart_path 可选
     - reason

2. 图表类型
   只支持最小集合：
   - bar：分类 + 数值
   - line：日期/时间 + 数值
   - pie：分类占比，行数较少时
   - table：不适合图表时 fallback

3. 判断规则
   - 如果有日期列 + 数值列，优先 line。
   - 如果有分类列 + 数值列，优先 bar。
   - 如果行数 <= 8 且有分类列 + 数值列，可选 pie。
   - 其他情况返回 table。

4. 输出方式
   选择一种简单方式：
   - 生成 Vega-Lite JSON 配置；或
   - 生成本地 HTML，使用轻量 CDN 图表库；或
   - 使用 matplotlib 输出 PNG。

   请优先选择最容易运行、依赖最少的方案，并说明原因。

5. CLI
   - `--visualize`
   - `--chart-output-dir`
   - 如果未开启 visualize，只输出普通结果。

6. LLM 可选
   - 本轮优先用规则判断图表类型。
   - 可以预留 LLM 推荐图表类型接口，但不要默认依赖 LLM。

限制：

- 不做复杂 dashboard。
- 不做交互式 BI 编辑器。
- 不做多图组合报告。
- 不把可视化作为 SQL 查询成功的必要条件。

输出要求：

- 直接修改文件。
- 给出图表判断规则。
- 给出输出文件路径策略。
- 给出验证命令。

验收标准：

- 查询结果为分类 + 数值时能生成 bar chart。
- 查询结果为日期 + 数值时能生成 line chart。
- 不适合图表时优雅 fallback 到 table。
- 可视化失败不影响 SQL 输出。
```
