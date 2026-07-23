"""Create a schema and join plan without generating SQL."""

from __future__ import annotations

import re

from queryforge.orchestration.agents.base import RoleAgent
from queryforge.orchestration.schemas import ArtifactRef, TaskState
from queryforge.core.schemas.models import Context


class SchemaArchitectAgent(RoleAgent):
    agent_name = "SchemaArchitectAgent"
    artifact_type = "schema_plan"

    def run(self, state: TaskState, context: Context) -> ArtifactRef:
        try:
            return self._run_planned(state, context)
        except Exception as exc:
            return self.emit(
                state,
                {
                    "status": "blocked",
                    "primary_tables": [],
                    "tables": [],
                    "selected_tables": [],
                    "selected_columns": {},
                    "join_paths": [],
                    "recommended_fields": {
                        "time_dimensions": [],
                        "group_by": [],
                        "aggregations": [],
                        "filters": [],
                    },
                    "metric_contracts": [],
                    "target_grain": [],
                    "fanout_risks": [],
                    "semantic_model": None,
                    "risks": [
                        {
                            "type": "schema_planning_error",
                            "severity": "high",
                            "description": f"Schema planning failed: {exc}",
                        }
                    ],
                    "assumptions": [],
                    "note": "Schema planning fallback; no SQL execution is authorized.",
                },
                status="blocked",
            )

    def _run_planned(self, state: TaskState, context: Context) -> ArtifactRef:
        used = set(context.sql_context.tables_used if context.sql_context else [])
        semantic_tables = {
            match.table
            for match in context.semantic_model.matches
        } if context.semantic_model else set()
        selected = used | semantic_tables
        schemas = [
            schema for schema in context.relevant_tables
            if not selected or schema.table_name in selected
        ]
        primary_tables = self._primary_tables(context, schemas)
        join_paths = self._join_paths(context, schemas)
        recommended_fields = self._recommended_fields(context, schemas)
        risks = self._risks(context, schemas, join_paths)
        assumptions = self._assumptions(context, schemas)
        status = "blocked" if not schemas else "warning" if risks else "valid"
        metric_contracts = [
            {
                "name": match.metric.name,
                "entity": match.metric.entity,
                "aggregation": match.metric.aggregation,
                "expression": match.metric.expression,
                "default_filters": match.metric.default_filters,
                "allowed_dimensions": match.metric.allowed_dimensions,
            }
            for match in context.metric_matches
        ]
        return self.emit(
            state,
            {
                "status": status,
                "primary_tables": primary_tables,
                "tables": [
                    {
                        "name": schema.table_name,
                        "columns": [
                            column.model_dump(mode="json") for column in schema.columns
                        ],
                        "foreign_keys": [
                            foreign_key.model_dump(mode="json")
                            for foreign_key in schema.foreign_keys
                        ],
                    }
                    for schema in schemas
                ],
                "selected_tables": [schema.table_name for schema in schemas],
                "selected_columns": {
                    schema.table_name: [column.name for column in schema.columns]
                    for schema in schemas
                },
                "join_paths": join_paths,
                "recommended_fields": recommended_fields,
                "metric_contracts": metric_contracts,
                "target_grain": list(context.metric_requested_dimensions),
                "fanout_risks": [
                    risk
                    for path in context.metric_join_paths
                    for risk in path.fanout_steps
                ],
                "semantic_model": (
                    context.semantic_model.model.name if context.semantic_model else None
                ),
                "risks": risks,
                "assumptions": assumptions,
                "note": "This plan describes physical choices and does not authorize execution.",
            },
            status=status,
        )

    def _primary_tables(self, context: Context, schemas: list) -> list[dict]:
        question = context.task.question.lower()
        semantic_tables = {
            match.table for match in context.semantic_model.matches
        } if context.semantic_model else set()
        metric_tables: set[str] = set()
        if context.semantic_model:
            entities = {
                entity.name: entity
                for entity in context.semantic_model.model.entities
            }
            for match in context.metric_matches:
                entity = entities.get(match.metric.entity)
                if entity:
                    metric_tables.add(entity.table)
        plans: list[dict] = []
        for schema in schemas:
            role = self._table_role(context, schema.table_name)
            column_names = [column.name for column in schema.columns]
            lexical_hits = [
                column for column in column_names
                if column.lower() in question
            ]
            score = 0.2
            if schema.table_name in metric_tables:
                score += 0.45
            if schema.table_name in semantic_tables:
                score += 0.25
            if lexical_hits:
                score += min(0.2, len(lexical_hits) * 0.05)
            key_metrics = self._metric_columns(schema.table_name, context)
            key_dimensions = self._dimension_columns(schema.table_name, context)
            reason_parts = []
            if schema.table_name in metric_tables:
                reason_parts.append("hosts a matched metric")
            if schema.table_name in semantic_tables:
                reason_parts.append("matched semantic vocabulary")
            if lexical_hits:
                reason_parts.append("question references columns: " + ", ".join(lexical_hits[:5]))
            if not reason_parts:
                reason_parts.append("available in scoped schema")
            plans.append(
                {
                    "table_name": schema.table_name,
                    "role": role,
                    "required": schema.table_name in metric_tables or schema.table_name in semantic_tables,
                    "relevance_score": round(min(score, 1.0), 3),
                    "reason": "; ".join(reason_parts),
                    "key_metrics": key_metrics,
                    "key_dimensions": key_dimensions,
                }
            )
        return sorted(plans, key=lambda item: (-item["relevance_score"], item["table_name"]))

    def _join_paths(self, context: Context, schemas: list) -> list[dict]:
        paths = []
        for path in context.metric_join_paths:
            join_keys = [
                {
                    "left": f"{step.from_table}.{step.from_column}",
                    "right": f"{step.to_table}.{step.to_column}",
                }
                for step in path.steps
            ]
            paths.append(
                {
                    "path_name": path.name,
                    "tables": path.tables,
                    "join_keys": join_keys,
                    "cardinality": " -> ".join(step.cardinality for step in path.steps),
                    "fan_out_risk": not path.safe,
                    "recommended": path.safe,
                    "reason": "Declared semantic join path" if path.explicit else "Resolved semantic relationship path",
                    "trade_off": "safe for aggregation" if path.safe else "; ".join(path.fanout_steps),
                }
            )
        if paths:
            return paths

        schema_by_name = {schema.table_name: schema for schema in schemas}
        for schema in schemas:
            for foreign_key in schema.foreign_keys:
                if foreign_key.referenced_table not in schema_by_name:
                    continue
                paths.append(
                    {
                        "path_name": f"{schema.table_name}_to_{foreign_key.referenced_table}",
                        "tables": [schema.table_name, foreign_key.referenced_table],
                        "join_keys": [
                            {
                                "left": f"{schema.table_name}.{foreign_key.column}",
                                "right": f"{foreign_key.referenced_table}.{foreign_key.referenced_column}",
                            }
                        ],
                        "cardinality": "many_to_one",
                        "fan_out_risk": False,
                        "recommended": True,
                        "reason": "SQLite foreign key relationship",
                        "trade_off": "No semantic model relationship was used.",
                    }
                )
        return paths

    def _recommended_fields(self, context: Context, schemas: list) -> dict:
        time_dimensions: list[str] = []
        group_by: list[str] = list(context.metric_requested_dimensions)
        aggregations = [match.metric.expression for match in context.metric_matches]
        filters = [
            filter_expression
            for match in context.metric_matches
            for filter_expression in match.metric.default_filters
        ]
        for match in context.metric_matches:
            if match.metric.time_field and match.metric.time_field not in time_dimensions:
                time_dimensions.append(match.metric.time_field)
        for schema in schemas:
            for column in schema.columns:
                reference = f"{schema.table_name}.{column.name}"
                lowered = column.name.lower()
                if (
                    reference not in time_dimensions
                    and ("date" in lowered or "time" in lowered or lowered.endswith("_dt"))
                ):
                    time_dimensions.append(reference)
                if reference not in group_by and any(
                    token in lowered
                    for token in ("region", "segment", "category", "county", "district", "status", "channel")
                ):
                    group_by.append(reference)
        if context.date_context and context.date_context.ranges:
            for date_range in context.date_context.ranges:
                filters.append(
                    f"Apply inclusive date range {date_range.start_date} to {date_range.end_date}"
                )
        return {
            "time_dimensions": time_dimensions[:8],
            "group_by": group_by[:12],
            "aggregations": aggregations[:8],
            "filters": filters[:12],
        }

    def _risks(self, context: Context, schemas: list, join_paths: list[dict]) -> list[dict]:
        risks: list[dict] = []
        if not schemas:
            risks.append(
                {
                    "type": "missing_schema",
                    "severity": "high",
                    "description": "No relevant tables were available for schema planning.",
                }
            )
        if len(schemas) > 1 and not join_paths:
            risks.append(
                {
                    "type": "missing_join",
                    "severity": "medium",
                    "description": "Multiple tables are in scope but no semantic path or physical foreign key was found.",
                }
            )
        for path in join_paths:
            if path.get("fan_out_risk"):
                risks.append(
                    {
                        "type": "fan_out",
                        "severity": "high",
                        "description": path.get("trade_off") or "Join path may expand grain.",
                    }
                )
        if context.semantic_model is None and len(schemas) > 1 and not join_paths:
            risks.append(
                {
                    "type": "ungoverned_join",
                    "severity": "medium",
                    "description": "No semantic model is active; joins rely only on physical metadata.",
                }
            )
        return risks

    @staticmethod
    def _assumptions(context: Context, schemas: list) -> list[str]:
        assumptions = []
        if context.metric_matches:
            assumptions.append("Matched semantic metric contracts are authoritative for aggregation.")
        if context.semantic_model is None:
            assumptions.append("Schema planning uses physical table metadata because no semantic model is active.")
        if schemas:
            assumptions.append("Only policy-visible columns are recommended.")
        return assumptions

    @staticmethod
    def _table_role(context: Context, table_name: str) -> str:
        if context.semantic_model is not None:
            for entity in context.semantic_model.model.entities:
                if entity.table == table_name:
                    return entity.entity_type
        if table_name.startswith("fact_"):
            return "fact"
        if table_name.startswith("dim_"):
            return "dimension"
        return "reference"

    @staticmethod
    def _metric_columns(table_name: str, context: Context) -> list[str]:
        columns: list[str] = []
        for match in context.metric_matches:
            for reference in re.findall(
                r'\b([A-Za-z_][A-Za-z0-9_]*)\.(?:"([^"]+)"|([A-Za-z_][A-Za-z0-9_]*))',
                match.metric.expression,
            ):
                table, quoted, bare = reference
                if table == table_name:
                    column = quoted or bare
                    if column not in columns:
                        columns.append(column)
        return columns

    @staticmethod
    def _dimension_columns(table_name: str, context: Context) -> list[str]:
        if context.semantic_model is None:
            return []
        columns: list[str] = []
        for entity in context.semantic_model.model.entities:
            if entity.table != table_name:
                continue
            for dimension in entity.dimensions:
                if dimension.column not in columns:
                    columns.append(dimension.column)
        return columns[:12]
