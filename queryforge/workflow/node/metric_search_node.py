"""Resolve structured business metrics before SQL generation."""

from __future__ import annotations

import logging
import re

from queryforge.workflow.node.base import Node
from queryforge.core.schemas.models import Context, NodeResult
from queryforge.domain.semantic import SemanticModelLoader


LOGGER = logging.getLogger("queryforge.metrics")

_REFERENCE_PATTERN = re.compile(
    r'\b([A-Za-z_][A-Za-z0-9_]*)\.(?:"([^"]+)"|([A-Za-z_][A-Za-z0-9_]*))'
)


def _extract_references(expression: str) -> list[tuple[str, str]]:
    return [
        (match.group(1), match.group(2) or match.group(3))
        for match in _REFERENCE_PATTERN.finditer(expression)
    ]


class MetricSearchNode(Node):
    name = "metric_search"
    description = "Match structured metrics and validate requested group dimensions"

    def execute(self, context: Context) -> NodeResult:
        if context.semantic_model is None:
            return self.success("Metric search disabled: no semantic model")
        try:
            return self._match_metrics(context)
        finally:
            # Keep the step-05 linking evidence in sync with the authoritative
            # resolved metric requirements, on success and failure alike.
            self._record_link_requirements(context)

    def _match_metrics(self, context: Context) -> NodeResult:
        assert context.semantic_model is not None
        model = context.semantic_model.model
        context.metric_matches = SemanticModelLoader.match_metrics(
            model, context.task.question
        )
        if not context.metric_matches:
            return self.success("No structured metric matched; treating as a field query")

        requested_dimensions = self._requested_group_dimensions(context)
        context.metric_requested_dimensions = requested_dimensions
        context.metric_join_paths = []
        invalid: list[str] = []
        for match in context.metric_matches:
            allowed = set(match.metric.allowed_dimensions)
            for dimension in requested_dimensions:
                dimension_entity = dimension.split(".", 1)[0]
                if dimension not in allowed:
                    diagnostic = SemanticModelLoader.resolve_join_path(
                        model,
                        match.metric.entity,
                        dimension_entity,
                        include_undeclared=True,
                    )
                    if diagnostic is not None and not diagnostic.safe:
                        return self.failure(
                            f"Fan-out risk for metric {match.metric.name!r} at "
                            f"base grain {match.metric.entity!r} with dimension "
                            f"{dimension!r}: "
                            + "; ".join(diagnostic.fanout_steps)
                        )
                    invalid.append(
                        f"{dimension} for metric {match.metric.name}"
                    )
                    continue
                if dimension_entity == match.metric.entity:
                    continue
                resolved = SemanticModelLoader.resolve_join_path(
                    model, match.metric.entity, dimension_entity
                )
                if resolved is None:
                    return self.failure(
                        f"No governed join path for metric {match.metric.name!r} "
                        f"and dimension {dimension!r}"
                    )
                if not resolved.safe:
                    return self.failure(
                        f"Fan-out risk for metric {match.metric.name!r} with "
                        f"dimension {dimension!r}: "
                        + "; ".join(resolved.fanout_steps)
                    )
                if all(
                    existing.name != resolved.name
                    for existing in context.metric_join_paths
                ):
                    context.metric_join_paths.append(resolved)
            if context.date_context and context.date_context.ranges:
                if not match.metric.time_field:
                    return self.failure(
                        f"Metric {match.metric.name!r} has no time_field but the "
                        "question requests a date range"
                    )
        if invalid:
            return self.failure(
                "Unsupported metric dimension combination: "
                + ", ".join(invalid)
            )
        return self.success(
            "Matched metric(s): "
            + ", ".join(match.metric.name for match in context.metric_matches)
            + (
                "; safe join path(s): "
                + ", ".join(path.name for path in context.metric_join_paths)
                if context.metric_join_paths
                else ""
            )
        )

    @staticmethod
    def _record_link_requirements(context: Context) -> None:
        """Record which tables/columns the resolved metrics actually require.

        Step 05 evidence must stay verifiable: when a required table is missing
        from the retrieved schema selection the gap is reported instead of being
        silently generated against.
        """
        evidence = context.task_context.get("schema_retrieval")
        if not isinstance(evidence, dict) or context.semantic_model is None:
            return
        entities = {
            entity.name: entity
            for entity in context.semantic_model.model.entities
        }
        tables: list[str] = []
        columns: list[str] = []
        for match in context.metric_matches:
            entity = entities.get(match.metric.entity)
            if entity is None:
                continue
            if entity.table not in tables:
                tables.append(entity.table)
            for reference in (
                [match.metric.expression, *match.metric.default_filters]
                + ([match.metric.time_field] if match.metric.time_field else [])
            ):
                for table, column in _extract_references(reference):
                    reference_text = f"{table}.{column}"
                    if table == entity.table and reference_text not in columns:
                        columns.append(reference_text)
        join_paths: list[dict] = []
        for path in context.metric_join_paths:
            for table in path.tables:
                if table not in tables:
                    tables.append(table)
            join_keys: list[str] = []
            for step in path.steps:
                for reference_text in (
                    f"{step.from_table}.{step.from_column}",
                    f"{step.to_table}.{step.to_column}",
                ):
                    if reference_text not in columns:
                        columns.append(reference_text)
                    if reference_text not in join_keys:
                        join_keys.append(reference_text)
            join_paths.append(
                {
                    "name": path.name,
                    "tables": list(path.tables),
                    "join_keys": join_keys,
                    "safe": path.safe,
                }
            )
        loaded = {
            str(table) for table in (evidence.get("selected_table_names") or [])
        }
        missing = [table for table in tables if loaded and table not in loaded]
        evidence["metric_requirements"] = {
            "matched": bool(context.metric_matches),
            "metrics": [match.metric.name for match in context.metric_matches],
            "tables": tables,
            "columns": columns,
            "requested_dimensions": list(context.metric_requested_dimensions),
            "join_paths": join_paths,
            "missing_tables": missing,
        }
        if missing:
            degradation = evidence.setdefault("degradation", [])
            note = "required_metric_tables_missing:" + ",".join(missing)
            if note not in degradation:
                degradation.append(note)
            LOGGER.warning("metric_requirement_gap tables=%s", ",".join(missing))

    @classmethod
    def _requested_group_dimensions(cls, context: Context) -> list[str]:
        assert context.semantic_model is not None
        question = SemanticModelLoader._normalize(context.task.question)
        table_to_entity = {
            entity.table: entity.name
            for entity in context.semantic_model.model.entities
        }
        base_entities = {
            match.metric.entity for match in context.metric_matches
        }
        allowed_dimensions = {
            dimension
            for match in context.metric_matches
            for dimension in match.metric.allowed_dimensions
        }
        requested: list[str] = []
        grouped_candidates: dict[str, list[str]] = {}
        for match in context.semantic_model.matches:
            if match.kind != "dimension":
                continue
            term = SemanticModelLoader._normalize(match.term)
            english_pattern = re.compile(
                rf"\b(?:by|per|grouped by|broken down by)\s+(?:the\s+)?"
                rf"(?:[a-z0-9_]+\s+){{0,2}}{re.escape(term)}\b"
            )
            chinese_grouping = any(
                re.search(re.escape(marker) + r".{0,12}" + re.escape(term), question)
                for marker in ("按", "依照", "根据")
            )
            if not english_pattern.search(question) and not chinese_grouping:
                continue
            entity_name = table_to_entity.get(match.table)
            if entity_name:
                reference = f"{entity_name}.{match.semantic_name}"
                grouped_candidates.setdefault(term, []).append(reference)
        selected_terms = [
            term
            for term in grouped_candidates
            if not any(
                term != other
                and f" {term} " in f" {other} "
                for other in grouped_candidates
            )
        ]
        for term in selected_terms:
            candidates = grouped_candidates[term]
            preferred = [
                reference
                for reference in candidates
                if reference.split(".", 1)[0] in base_entities
            ]
            if not preferred:
                preferred = [
                    reference
                    for reference in candidates
                    if reference in allowed_dimensions
                ]
            for reference in preferred or candidates[:1]:
                if reference not in requested:
                    requested.append(reference)
        return requested
