"""Rule-based generator for static, portable analytical HTML reports."""

from __future__ import annotations

import html
import json
from datetime import datetime, timezone
from numbers import Number
from pathlib import Path
from typing import Any

from queryforge.workflow.node.visualization_node import VisualizationNode
from queryforge.core.schemas.models import Context
from queryforge.core.schemas.report import ReportArtifact, ReportSection


DEFAULT_REPORT_OUTPUT_DIR = ".queryforge/reports"
VEGA_EMBED_CDN = "https://cdn.jsdelivr.net/npm/vega-embed@6"


class ReportGenerator:
    def __init__(
        self,
        output_dir: str | Path = DEFAULT_REPORT_OUTPUT_DIR,
        *,
        max_rows: int = 50,
        max_charts: int = 3,
    ) -> None:
        if max_rows < 1 or max_charts < 1:
            raise ValueError("Report limits must be positive")
        self.output_dir = Path(output_dir).expanduser().resolve()
        self.max_rows = max_rows
        self.max_charts = max_charts

    def generate(self, context: Context) -> ReportArtifact:
        if context.sql_context is None or context.execution_result is None:
            raise ValueError("Report generation requires SQL and execution results")
        execution = context.execution_result
        sql_context = context.sql_context
        metrics, dimensions = self._classify_columns(execution.columns, execution.rows)
        findings = self._key_findings(execution.columns, execution.rows, metrics, dimensions)
        summary = (
            f"Query returned {execution.row_count} row(s) across "
            f"{len(execution.columns)} column(s)."
        )
        charts = self._charts(context, self.max_charts)
        sections = [
            ReportSection(
                id="summary",
                title="Summary",
                type="text",
                content={"text": summary},
            ),
            ReportSection(
                id="metrics",
                title="Key Metrics",
                type="metrics",
                content={"items": self._metric_cards(execution.columns, execution.rows, metrics)},
            ),
            ReportSection(
                id="results",
                title="Result Table",
                type="table",
                content={
                    "columns": execution.columns,
                    "rows": execution.rows[: self.max_rows],
                    "truncated": execution.row_count > self.max_rows,
                    "total_rows": execution.row_count,
                },
            ),
            *charts,
            ReportSection(
                id="methodology",
                title="SQL & Methodology",
                type="sql",
                content={
                    "sql": sql_context.sql,
                    "explanation": sql_context.explanation,
                    "reasoning": (
                        context.reasoning_result.model_dump(mode="json")
                        if context.reasoning_result
                        else None
                    ),
                },
            ),
            ReportSection(
                id="findings",
                title="Key Findings",
                type="text",
                content={"items": findings},
            ),
        ]
        self.output_dir.mkdir(parents=True, exist_ok=True)
        report_path = self.output_dir / f"{context.run_id}.html"
        manifest_path = self.output_dir / f"{context.run_id}.manifest.json"
        artifact = ReportArtifact(
            title=context.task.question,
            summary=summary,
            sections=sections,
            data_source=context.task.database_path,
            sql=sql_context.sql,
            metrics=metrics,
            dimensions=dimensions,
            key_findings=findings,
            file_path=str(report_path),
            manifest_path=str(manifest_path),
        )
        report_path.write_text(self._render_html(artifact), encoding="utf-8")
        manifest_path.write_text(
            json.dumps(artifact.model_dump(mode="json"), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return artifact

    @staticmethod
    def _classify_columns(
        columns: list[str],
        rows: list[list[Any]],
    ) -> tuple[list[str], list[str]]:
        metrics: list[str] = []
        dimensions: list[str] = []
        for index, column in enumerate(columns):
            values = [row[index] for row in rows if index < len(row) and row[index] is not None]
            if values and all(isinstance(value, Number) and not isinstance(value, bool) for value in values):
                metrics.append(column)
            else:
                dimensions.append(column)
        return metrics, dimensions

    @staticmethod
    def _metric_cards(
        columns: list[str], rows: list[list[Any]], metrics: list[str]
    ) -> list[dict[str, Any]]:
        cards = []
        for metric in metrics[:4]:
            index = columns.index(metric)
            values = [
                row[index]
                for row in rows
                if index < len(row) and isinstance(row[index], Number)
            ]
            if values:
                cards.append({"label": metric, "value": max(values), "aggregation": "max"})
        return cards

    @classmethod
    def _key_findings(
        cls,
        columns: list[str],
        rows: list[list[Any]],
        metrics: list[str],
        dimensions: list[str],
    ) -> list[str]:
        findings = [f"Returned {len(rows)} row(s) with {len(columns)} column(s)."]
        for metric in metrics[:2]:
            index = columns.index(metric)
            values = [
                row[index]
                for row in rows
                if index < len(row) and isinstance(row[index], Number)
            ]
            if not values:
                continue
            maximum, minimum = max(values), min(values)
            findings.append(f"{metric}: maximum {maximum}, minimum {minimum}.")
            if dimensions:
                dimension_index = columns.index(dimensions[0])
                best_row = max(
                    (row for row in rows if index < len(row) and isinstance(row[index], Number)),
                    key=lambda row: row[index],
                    default=None,
                )
                if best_row and dimension_index < len(best_row):
                    findings.append(
                        f"Top {dimensions[0]} by {metric}: {best_row[dimension_index]} ({best_row[index]})."
                    )
        if not metrics and rows:
            findings.append("The result is descriptive; no numeric metric column was detected.")
        return findings[:5]

    @staticmethod
    def _charts(context: Context, max_charts: int) -> list[ReportSection]:
        execution = context.execution_result
        sql_context = context.sql_context
        assert execution is not None and sql_context is not None
        visualization = VisualizationNode.build_visualization(
            question=context.task.question,
            sql=sql_context.sql,
            columns=execution.columns,
            rows=execution.rows,
        )
        if visualization.chart_type == "table" or max_charts == 0:
            return []
        return [
            ReportSection(
                id="chart_1",
                title="Chart",
                type="chart",
                content={
                    "chart_type": visualization.chart_type,
                    "spec": visualization.chart_config,
                    "reason": visualization.reason,
                },
            )
        ]

    @staticmethod
    def _render_html(report: ReportArtifact) -> str:
        section_html = "\n".join(
            ReportGenerator._render_section(section) for section in report.sections
        )
        return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(report.title)}</title>
<style>
body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;line-height:1.5;margin:0;background:#f7f8fa;color:#1f2937}}
main{{max-width:1100px;margin:32px auto;background:#fff;padding:36px;border-radius:12px;box-shadow:0 2px 12px #0001}}
h1,h2{{color:#111827}} .meta,.footer{{color:#6b7280;font-size:.9rem}} .cards{{display:flex;gap:12px;flex-wrap:wrap}}
.card{{background:#eff6ff;padding:14px;border-radius:8px;min-width:140px}} table{{width:100%;border-collapse:collapse;font-size:.9rem}}
th,td{{border:1px solid #e5e7eb;padding:8px;text-align:left}} th{{background:#f3f4f6}} pre{{overflow:auto;background:#111827;color:#e5e7eb;padding:16px;border-radius:8px}}
.notice{{color:#92400e;background:#fffbeb;padding:10px;border-radius:6px}} .chart{{min-height:280px}}
</style><script src="{VEGA_EMBED_CDN}"></script></head>
<body><main><header><h1>{html.escape(report.title)}</h1><p>{html.escape(report.summary)}</p>
<p class="meta">Generated {html.escape(report.generated_at)} · Data source {html.escape(report.data_source)}</p></header>
{section_html}<footer class="footer">Generated by QueryForge. Validate analytical results before making decisions.</footer>
</main></body></html>"""

    @staticmethod
    def _render_section(section: ReportSection) -> str:
        title = html.escape(section.title)
        content = section.content
        if section.type == "metrics":
            cards = "".join(
                f'<div class="card"><strong>{html.escape(str(card["label"]))}</strong><br>{html.escape(str(card["value"]))}</div>'
                for card in content.get("items", [])
            ) or "<p>No numeric metrics detected.</p>"
            return f"<section><h2>{title}</h2><div class=\"cards\">{cards}</div></section>"
        if section.type == "table":
            headers = "".join(f"<th>{html.escape(str(column))}</th>" for column in content["columns"])
            rows = "".join(
                "<tr>" + "".join(f"<td>{html.escape(str(value))}</td>" for value in row) + "</tr>"
                for row in content["rows"]
            )
            notice = (
                f'<p class="notice">Showing {len(content["rows"])} of {content["total_rows"]} rows.</p>'
                if content.get("truncated") else ""
            )
            return f"<section><h2>{title}</h2>{notice}<table><thead><tr>{headers}</tr></thead><tbody>{rows}</tbody></table></section>"
        if section.type == "chart":
            spec = json.dumps(content["spec"], ensure_ascii=False)
            fallback = ReportGenerator._chart_fallback_svg(content["spec"])
            return (
                f'<section><h2>{title}</h2><p>{html.escape(content["reason"])}</p>'
                f'<div id="{section.id}" class="chart"><div class="chart-fallback">'
                f"{fallback}</div></div><script>vegaEmbed(\"#{section.id}\", {spec})"
                f'.then(function(){{document.querySelector("#{section.id} .chart-fallback")'
                '.style.display="none";}).catch(function(){});</script></section>'
            )
        if section.type == "sql":
            explanation = html.escape(str(content.get("explanation") or ""))
            return f"<section><h2>{title}</h2><p>{explanation}</p><details><summary>Show SQL</summary><pre>{html.escape(str(content['sql']))}</pre></details></section>"
        items = content.get("items")
        body = (
            "<ul>" + "".join(f"<li>{html.escape(str(item))}</li>" for item in items) + "</ul>"
            if items else f"<p>{html.escape(str(content.get('text') or ''))}</p>"
        )
        return f"<section><h2>{title}</h2>{body}</section>"

    @staticmethod
    def _chart_fallback_svg(spec: dict[str, Any]) -> str:
        """Render a compact SVG fallback so report charts remain useful offline."""
        values = list(spec.get("data", {}).get("values", []))[:20]
        encoding = spec.get("encoding", {})
        mark = spec.get("mark")
        mark_type = mark.get("type") if isinstance(mark, dict) else mark
        x_field = encoding.get("x", {}).get("field")
        y_field = encoding.get("y", {}).get("field")
        if not values or not x_field or not y_field:
            return "<p>Chart data is available in the report manifest.</p>"
        numeric_values = [
            value.get(y_field)
            for value in values
            if isinstance(value.get(y_field), Number)
        ]
        if not numeric_values:
            return "<p>Chart data is available in the report manifest.</p>"
        width, height, padding = 720, 280, 38
        maximum = max(numeric_values) or 1
        chart_width = width - padding * 2
        chart_height = height - padding * 2
        labels: list[str] = []
        shapes: list[str] = []
        if mark_type == "line":
            points = []
            for index, value in enumerate(values):
                metric = value.get(y_field)
                if not isinstance(metric, Number):
                    continue
                x = padding + (chart_width * index / max(len(values) - 1, 1))
                y = padding + chart_height * (1 - float(metric) / maximum)
                points.append(f"{x:.1f},{y:.1f}")
                labels.append(
                    f'<text x="{x:.1f}" y="{height - 12}" text-anchor="middle">'
                    f"{html.escape(str(value.get(x_field, ''))[:12])}</text>"
                )
            shapes.append(
                '<polyline fill="none" stroke="#2563eb" stroke-width="3" points="'
                + " ".join(points)
                + '"/>'
            )
        else:
            bar_width = chart_width / max(len(values), 1) * 0.7
            for index, value in enumerate(values):
                metric = value.get(y_field)
                if not isinstance(metric, Number):
                    continue
                x = padding + chart_width * index / max(len(values), 1) + bar_width * 0.15
                bar_height = chart_height * float(metric) / maximum
                y = padding + chart_height - bar_height
                shapes.append(
                    f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar_width:.1f}" '
                    f'height="{bar_height:.1f}" fill="#2563eb"/>'
                )
                labels.append(
                    f'<text x="{x + bar_width / 2:.1f}" y="{height - 12}" '
                    f'text-anchor="middle">{html.escape(str(value.get(x_field, ""))[:12])}</text>'
                )
        return (
            f'<svg role="img" aria-label="Offline {html.escape(str(mark_type or "chart"))} chart" '
            f'viewBox="0 0 {width} {height}" width="100%" height="{height}">'
            f'<line x1="{padding}" y1="{padding + chart_height}" x2="{width - padding}" '
            f'y2="{padding + chart_height}" stroke="#9ca3af"/>'
            + "".join(shapes)
            + f'<g font-size="10" fill="#4b5563">{"".join(labels)}</g></svg>'
        )
