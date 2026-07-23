"""Resolve structured business metrics before SQL generation."""

from __future__ import annotations

import re

from queryforge.workflow.node.base import Node
from queryforge.core.schemas.models import Context, NodeResult
from queryforge.domain.semantic import SemanticModelLoader


class MetricSearchNode(Node):
    name = "metric_search"
    description = "Match structured metrics and validate requested group dimensions"

    def execute(self, context: Context) -> NodeResult:
        if context.semantic_model is None:
            return self.success("Metric search disabled: no semantic model")
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
