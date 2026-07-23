"""Create a dependency-free Vega-Lite chart configuration from SQL rows."""

from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import date, datetime, timezone
from numbers import Number
from pathlib import Path
from typing import Any, Literal

from queryforge.workflow.node.base import Node
from queryforge.core.schemas.models import Context, NodeResult, VisualizationResult


PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_CHART_OUTPUT_DIR = PROJECT_ROOT / ".queryforge/charts"
VEGA_LITE_SCHEMA = "https://vega.github.io/schema/vega-lite/v5.json"
LOGGER = logging.getLogger("queryforge.visualization")


class VisualizationNode(Node):
    """Choose one small chart by deterministic column/value rules."""

    name = "visualization"
    description = "Choose and persist a simple Vega-Lite result chart"
    PIE_HINTS = (
        "share",
        "percentage",
        "percent",
        "proportion",
        "composition",
        "distribution",
        "breakdown",
        "占比",
        "比例",
        "百分比",
        "构成",
        "分布",
    )
    DATE_NAME_PATTERN = re.compile(
        r"(?:^|_)(?:date|datetime|time|timestamp|year|month|day|week|quarter)(?:$|_)",
        re.IGNORECASE,
    )

    def __init__(self, output_dir: str | Path = DEFAULT_CHART_OUTPUT_DIR) -> None:
        path = Path(output_dir).expanduser()
        if not path.is_absolute():
            path = Path.cwd() / path
        self.output_dir = path.resolve()

    def execute(self, context: Context) -> NodeResult:
        execution = context.execution_result
        sql_context = context.sql_context
        if execution is None or sql_context is None or context.final_output is None:
            return self.failure("Visualization requires completed SQL output")

        try:
            result = self.build_visualization(
                question=context.task.question,
                sql=sql_context.sql,
                columns=execution.columns,
                rows=execution.rows,
            )
        except Exception as exc:
            LOGGER.warning("visualization_analysis_failed error=%s", exc)
            result = self._fallback(
                execution.columns,
                execution.rows,
                f"Visualization analysis failed; returning a table: {exc}",
                error=str(exc),
            )

        if result.chart_type != "table":
            try:
                chart_path = self._write_chart(
                    result.chart_config,
                    context.task.question,
                    sql_context.sql,
                )
                result = result.model_copy(update={"chart_path": str(chart_path)})
            except Exception as exc:
                LOGGER.warning(
                    "visualization_write_failed output_dir=%s error=%s",
                    self.output_dir,
                    exc,
                )
                reason = f"{result.reason} Chart file was not written: {exc}"
                result = result.model_copy(update={"reason": reason, "error": str(exc)})

        context.visualization_result = result
        context.final_output["visualization"] = result.model_dump(mode="json")
        return self.success(
            f"Selected {result.chart_type} visualization"
            + (" without a chart file" if result.chart_path is None else "")
        )

    @classmethod
    def build_visualization(
        cls,
        *,
        question: str,
        sql: str,
        columns: list[str],
        rows: list[list[Any]],
    ) -> VisualizationResult:
        del sql  # Reserved for a future optional recommendation strategy.
        values = cls._row_objects(columns, rows)
        if not columns or not rows:
            return cls._fallback(
                columns, rows, "No result rows are available for a chart."
            )

        date_columns = [
            column
            for index, column in enumerate(columns)
            if cls._is_date_column(column, cls._column_values(rows, index))
        ]
        numeric_columns = [
            column
            for index, column in enumerate(columns)
            if column not in date_columns
            and cls._is_numeric_column(cls._column_values(rows, index))
        ]
        category_columns = [
            column
            for index, column in enumerate(columns)
            if column not in date_columns
            and column not in numeric_columns
            and cls._is_category_column(cls._column_values(rows, index))
        ]

        if date_columns and numeric_columns:
            x_field, y_field = date_columns[0], numeric_columns[0]
            return VisualizationResult(
                chart_type="line",
                chart_config=cls._vega_config(
                    "line", values, x_field, y_field, question
                ),
                reason=(
                    f"Selected line because {x_field!r} is date/time-like and "
                    f"{y_field!r} is numeric."
                ),
            )

        if category_columns and numeric_columns:
            category, metric = category_columns[0], numeric_columns[0]
            pie_requested = len(rows) <= 8 and any(
                hint in question.lower() for hint in cls.PIE_HINTS
            )
            chart_type: Literal["bar", "pie"] = "pie" if pie_requested else "bar"
            reason = (
                f"Selected pie because the question asks for a share/distribution and "
                f"the result has {len(rows)} categories."
                if pie_requested
                else f"Selected bar because {category!r} is categorical and {metric!r} is numeric."
            )
            return VisualizationResult(
                chart_type=chart_type,
                chart_config=cls._vega_config(
                    chart_type, values, category, metric, question
                ),
                reason=reason,
            )

        return cls._fallback(
            columns,
            rows,
            "The result does not contain a supported date/category and numeric pairing.",
        )

    @classmethod
    def _vega_config(
        cls,
        chart_type: Literal["bar", "line", "pie"],
        values: list[dict[str, Any]],
        dimension: str,
        metric: str,
        title: str,
    ) -> dict[str, Any]:
        base: dict[str, Any] = {
            "$schema": VEGA_LITE_SCHEMA,
            "description": "Generated locally by QueryForge's rule-based visualizer.",
            "title": title,
            "data": {"values": values},
        }
        if chart_type == "line":
            base.update(
                {
                    "mark": {"type": "line", "point": True},
                    "encoding": {
                        "x": {"field": dimension, "type": "temporal", "title": dimension},
                        "y": {"field": metric, "type": "quantitative", "title": metric},
                        "tooltip": [
                            {"field": dimension, "type": "temporal"},
                            {"field": metric, "type": "quantitative"},
                        ],
                    },
                }
            )
        elif chart_type == "bar":
            base.update(
                {
                    "mark": "bar",
                    "encoding": {
                        "x": {"field": dimension, "type": "nominal", "sort": "-y", "title": dimension},
                        "y": {"field": metric, "type": "quantitative", "title": metric},
                        "tooltip": [
                            {"field": dimension, "type": "nominal"},
                            {"field": metric, "type": "quantitative"},
                        ],
                    },
                }
            )
        else:
            base.update(
                {
                    "mark": {"type": "arc", "tooltip": True},
                    "encoding": {
                        "theta": {"field": metric, "type": "quantitative"},
                        "color": {"field": dimension, "type": "nominal", "title": dimension},
                        "tooltip": [
                            {"field": dimension, "type": "nominal"},
                            {"field": metric, "type": "quantitative"},
                        ],
                    },
                }
            )
        return base

    @staticmethod
    def _fallback(
        columns: list[str],
        rows: list[list[Any]],
        reason: str,
        error: str | None = None,
    ) -> VisualizationResult:
        return VisualizationResult(
            chart_type="table",
            chart_config={
                "format": "table",
                "columns": columns,
                "rows": rows,
            },
            reason=reason,
            error=error,
        )

    def _write_chart(self, config: dict[str, Any], question: str, sql: str) -> Path:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        digest = hashlib.sha256(f"{question}\n{sql}".encode("utf-8")).hexdigest()[:10]
        path = self.output_dir / f"queryforge_chart_{timestamp}_{digest}.vl.json"
        path.write_text(
            json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return path

    @staticmethod
    def _row_objects(columns: list[str], rows: list[list[Any]]) -> list[dict[str, Any]]:
        return [
            {column: row[index] if index < len(row) else None for index, column in enumerate(columns)}
            for row in rows
        ]

    @staticmethod
    def _column_values(rows: list[list[Any]], index: int) -> list[Any]:
        return [row[index] for row in rows if index < len(row) and row[index] is not None]

    @classmethod
    def _is_date_column(cls, name: str, values: list[Any]) -> bool:
        normalized = re.sub(r"[^a-z0-9_]+", "_", name.lower()).strip("_")
        name_hint = bool(cls.DATE_NAME_PATTERN.search(normalized)) or any(
            hint in name for hint in ("日期", "时间", "年份", "月份")
        )
        if not values:
            return False
        if name_hint and all(cls._date_like(value, allow_year=True) for value in values):
            return True
        return all(cls._date_like(value, allow_year=False) for value in values)

    @staticmethod
    def _date_like(value: Any, *, allow_year: bool) -> bool:
        if isinstance(value, (datetime, date)):
            return True
        if allow_year and isinstance(value, Number) and not isinstance(value, bool):
            return 1000 <= int(value) <= 9999 and float(value).is_integer()
        if not isinstance(value, str):
            return False
        text = value.strip()
        if allow_year and re.fullmatch(r"\d{4}", text):
            return True
        if not re.match(r"^\d{4}[-/]\d{1,2}(?:[-/]\d{1,2})?", text):
            return False
        try:
            datetime.fromisoformat(text.replace("/", "-").replace("Z", "+00:00"))
            return True
        except ValueError:
            return bool(re.fullmatch(r"\d{4}-\d{1,2}", text.replace("/", "-")))

    @staticmethod
    def _is_numeric_column(values: list[Any]) -> bool:
        if not values:
            return False
        for value in values:
            if isinstance(value, bool):
                return False
            if isinstance(value, Number):
                continue
            if isinstance(value, str):
                try:
                    float(value.strip())
                    continue
                except ValueError:
                    pass
            return False
        return True

    @staticmethod
    def _is_category_column(values: list[Any]) -> bool:
        return bool(values) and all(
            isinstance(value, (str, bool)) for value in values
        )
