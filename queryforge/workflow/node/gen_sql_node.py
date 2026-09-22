"""Generate a structured SQLite query using the configured LLM."""

import json
import logging
from typing import Any

import sqlglot
from sqlglot import expressions as exp
from pydantic import ValidationError

from queryforge.workflow.node.base import Node
from queryforge.infrastructure.models.base import BaseModelProvider, ModelResponseError
from queryforge.core.schemas.models import (
    Context,
    NodeResult,
    ReasoningResult,
    SQLContext,
)


LOGGER = logging.getLogger("queryforge.sql")


class GenSqlNode(Node):
    name = "gen_sql"
    description = "Generate SQLite SQL from the question and schemas"
    MAX_TABLES_IN_PROMPT = 50
    MAX_COLUMNS_PER_TABLE = 100

    def __init__(self, llm: BaseModelProvider) -> None:
        self.llm = llm

    def execute(self, context: Context) -> NodeResult:
        if not context.relevant_tables:
            return self.failure("No table schemas are available for SQL generation")

        prompt = self._build_prompt(context)
        if context.task_context.get("sql_dialect") == "duckdb":
            prompt = prompt.replace("SQLite", "DuckDB") + "\nUse DuckDB native dates; external readers/extensions and non-main schemas are forbidden."
        try:
            payload = self.llm.generate_json(prompt)
            context.sql_context = SQLContext.model_validate(payload)
            if payload.get("reasoning") is not None:
                try:
                    normalized, coercions = self._normalize_reasoning(
                        payload["reasoning"]
                    )
                    context.sql_context.reasoning_result = ReasoningResult.model_validate(
                        normalized
                    )
                    context.sql_context.reasoning_validation = self._validate_reasoning(
                        context.sql_context.sql,
                        context.sql_context.reasoning_result,
                    )
                    if coercions:
                        context.sql_context.reasoning_validation["coercions"] = coercions
                    context.reasoning_result = context.sql_context.reasoning_result
                    context.reasoning_validation = (
                        context.sql_context.reasoning_validation
                    )
                except ValidationError as exc:
                    # Recorded on the context, not only in a nested warning: the
                    # payload is dropped entirely here, and a silent drop makes a
                    # model that consistently emits an incompatible shape
                    # indistinguishable from one that emits nothing.
                    context.reasoning_discarded = "; ".join(
                        f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
                        for error in exc.errors()[:5]
                    )
                    context.reasoning_validation = {
                        "status": "warning",
                        "warnings": [f"Invalid reasoning payload: {exc}"],
                    }
            LOGGER.info(
                "sql_generated sql=%s tables_used=%s",
                context.sql_context.sql,
                context.sql_context.tables_used,
            )
        except ModelResponseError as exc:
            raw = exc.raw_output.strip()
            if len(raw) > 1000:
                raw = raw[:1000] + "..."
            return self.failure(
                f"{exc}. Expected a JSON object with string fields 'sql' and "
                f"'explanation', plus list field 'tables_used'. "
                f"Raw model output: {raw!r}"
            )
        except ValidationError as exc:
            return self.failure(f"LLM JSON has invalid fields: {exc}")
        except Exception as exc:
            return self.failure(f"Could not generate SQL: {exc}")
        return self.success("Generated SQLite query")

    #: Words a model may use instead of a number for ``confidence``.
    #:
    #: Mapped strictly *below* the self-verification threshold (0.8) on purpose: a
    #: word is not a measurement, so it must never by itself be enough to skip the
    #: review pass. A model that genuinely means "high" can say 0.9.
    _CONFIDENCE_WORDS: dict[str, float] = {
        "high": 0.75,
        "very high": 0.79,
        "medium": 0.5,
        "moderate": 0.5,
        "low": 0.2,
        "very low": 0.1,
    }

    @classmethod
    def _normalize_reasoning(cls, payload: Any) -> tuple[dict[str, Any], list[str]]:
        """Coerce the shapes models actually emit into the declared contract.

        Observed from a real run, the same payload violated the schema in four
        places at once — ``metrics`` as bare strings instead of objects,
        ``sorting``/``limit`` as the *string* ``"None"`` instead of null, and
        ``confidence`` as the word ``"high"`` instead of a number. Every run
        therefore failed validation and the whole reasoning payload was discarded,
        which made the audit summary dead weight in production.

        Coercion is recorded and surfaced (``reasoning_validation.coercions``) so a
        drifting prompt cannot hide behind tolerant parsing. Anything that cannot
        be coerced is left alone and still fails validation, which keeps the
        original behaviour: no reasoning rather than wrong reasoning.
        """

        if not isinstance(payload, dict):
            return payload, []
        normalized = dict(payload)
        coercions: list[str] = []

        nullish = {"none", "null", "n/a", "na", "nan"}

        def _is_nullish(value: Any) -> bool:
            return isinstance(value, str) and value.strip().lower() in nullish

        for key in ("time_range", "limit", "strategy"):
            if _is_nullish(normalized.get(key)):
                normalized[key] = None
                coercions.append(f"{key}: string-null -> null")

        if _is_nullish(normalized.get("sorting")):
            normalized["sorting"] = []
            coercions.append("sorting: string-null -> []")

        # Each of these is "a list of objects" in the contract, and the model
        # reliably flattens some of them to bare strings. Confirmed on real runs:
        # ``metrics`` and ``sorting`` both arrive as ["COUNT(x)", "col DESC"]. A
        # single unconvertible entry discarded the entire payload, so the whole
        # audit summary was dead weight in production.
        list_shapes: tuple[tuple[str, str, Any], ...] = (
            ("metrics", "expression", None),
            ("sorting", "column", None),
            ("dimensions", None, None),
            ("tables", None, None),
        )
        for key, field, _unused in list_shapes:
            value = normalized.get(key)
            if not isinstance(value, list):
                continue
            if not any(isinstance(item, str) for item in value):
                continue
            if field is None:
                continue
            normalized[key] = [
                {field: item} if isinstance(item, str) else item for item in value
            ]
            coercions.append(f"{key}: string entries -> {{{field}}}")

        if isinstance(normalized.get("sorting"), list):
            parsing = []
            changed = False
            for item in normalized["sorting"]:
                if not isinstance(item, dict) or "direction" in item:
                    parsing.append(item)
                    continue
                column = str(item.get("column") or "")
                upper = column.upper()
                for suffix, direction in ((" DESC", "DESC"), (" ASC", "ASC")):
                    if upper.endswith(suffix):
                        item = {"column": column[: -len(suffix)].strip(), "direction": direction}
                        changed = True
                        break
                parsing.append(item)
            if changed:
                normalized["sorting"] = parsing
                coercions.append("sorting: 'col DESC' -> {column, direction}")

        confidence = normalized.get("confidence")
        if isinstance(confidence, str):
            text = confidence.strip().lower()
            if text in cls._CONFIDENCE_WORDS:
                normalized["confidence"] = cls._CONFIDENCE_WORDS[text]
                coercions.append(
                    f"confidence: {confidence!r} -> {normalized['confidence']}"
                )
            else:
                try:
                    normalized["confidence"] = float(text)
                    coercions.append(f"confidence: {confidence!r} -> float")
                except ValueError:
                    pass

        return normalized, coercions

    @staticmethod
    def _validate_reasoning(
        sql: str,
        reasoning: ReasoningResult,
    ) -> dict[str, object]:
        warnings: list[str] = []
        try:
            tree = sqlglot.parse_one(sql, read="sqlite")
            sql_tables = {table.name for table in tree.find_all(exp.Table) if table.name}
            declared_tables = set(reasoning.tables)
            if declared_tables and not sql_tables.issubset(declared_tables):
                warnings.append(
                    "SQL references undeclared reasoning tables: "
                    + ", ".join(sorted(sql_tables - declared_tables))
                )
            joins = list(tree.find_all(exp.Join))
            if joins and len(reasoning.joins) < len(joins):
                warnings.append(
                    f"SQL contains {len(joins)} JOIN(s), but reasoning declares "
                    f"{len(reasoning.joins)}."
                )
            if tree.find(exp.Group) is not None and not reasoning.dimensions:
                warnings.append("SQL contains GROUP BY but reasoning.dimensions is empty.")
            if tree.find(exp.Where) is not None and not reasoning.filters:
                warnings.append("SQL contains WHERE but reasoning.filters is empty.")
            if tree.find(exp.Order) is not None and not reasoning.sorting:
                warnings.append("SQL contains ORDER BY but reasoning.sorting is empty.")
            return {
                "status": "warning" if warnings else "valid",
                "warnings": warnings,
                "sql_tables": sorted(sql_tables),
                "sql_join_count": len(joins),
            }
        except Exception as exc:
            return {
                "status": "warning",
                "warnings": [f"Could not validate reasoning against SQL: {exc}"],
            }

    @staticmethod
    def _build_prompt(context: Context) -> str:
        selected_tables = context.relevant_tables[: GenSqlNode.MAX_TABLES_IN_PROMPT]
        hidden_columns = (
            context.semantic_model.hidden_column_refs()
            if context.semantic_model
            else set()
        )
        schemas = []
        truncated_columns = 0
        for schema in selected_tables:
            visible_columns = [
                column
                for column in schema.columns
                if (schema.table_name, column.name) not in hidden_columns
            ]
            columns = visible_columns[: GenSqlNode.MAX_COLUMNS_PER_TABLE]
            truncated_columns += max(0, len(visible_columns) - len(columns))
            schemas.append(
                {
                    "table_name": schema.table_name,
                    "columns": [column.model_dump() for column in columns],
                    "foreign_keys": [
                        foreign_key.model_dump()
                        for foreign_key in schema.foreign_keys
                    ],
                }
            )

        omitted_tables = len(context.relevant_tables) - len(selected_tables)
        truncation_note = "Schema context is complete."
        if omitted_tables or truncated_columns:
            truncation_note = (
                "Schema context was truncated to protect the LLM context window: "
                f"{omitted_tables} table(s) and {truncated_columns} column(s) omitted."
            )
        value_hints = [hint.model_dump() for hint in context.value_hints]
        reference_examples = [
            example.model_dump() for example in context.reference_examples
        ]
        history_matches = [match.model_dump() for match in context.history_matches]
        vector_sql_matches = [
            match.model_dump() for match in context.vector_sql_matches
        ]
        vector_schema_matches = [
            match.model_dump() for match in context.vector_schema_matches
        ]
        date_context = (
            context.date_context.model_dump() if context.date_context else {}
        )
        sql_policy = context.sql_policy
        semantic_model = (
            context.semantic_model.model.model_dump(
                by_alias=True, exclude={"metrics"}
            )
            if context.semantic_model
            else {}
        )
        semantic_matches = (
            [match.model_dump() for match in context.semantic_model.matches]
            if context.semantic_model
            else []
        )
        semantic_rule_lines = ""
        semantic_context_block = ""
        if context.semantic_model:
            semantic_rule_lines = """- The physical SQLite schema is authoritative for identifier existence and types.
- Use the validated semantic model's entity/dimension synonyms for business meaning and
  prefer its declared relationships for joins.
- Columns hidden by the semantic model are intentionally absent from the supplied schema
  and must not be selected, filtered, grouped, ordered, or joined.
"""
            semantic_context_block = f"""
Validated semantic model (business mapping):
{json.dumps(semantic_model, ensure_ascii=False, indent=2)}

Deterministic semantic matches for this question:
{json.dumps(semantic_matches, ensure_ascii=False, indent=2)}

Semantic model rules:
- The semantic model has already been validated against the physical SQLite schema.
- Map matched entity and dimension synonyms to their declared table and column.
- Prefer declared relationship from/to columns when joining their entities.
- The semantic model adds business meaning and visibility constraints; it never creates
  a physical table or column and never overrides the current SQLite schema.
"""
        metric_context_block = ""
        if context.metric_matches:
            metric_payload = [
                match.model_dump() for match in context.metric_matches
            ]
            join_path_payload = [
                path.model_dump() for path in context.metric_join_paths
            ]
            metric_context_block = f"""
Matched structured metrics (authoritative business definitions):
{json.dumps(metric_payload, ensure_ascii=False, indent=2)}

Requested metric group dimensions:
{json.dumps(context.metric_requested_dimensions, ensure_ascii=False, indent=2)}

Resolved safe Join Paths and Cardinality Contracts:
{json.dumps(join_path_payload, ensure_ascii=False, indent=2)}

Metric rules:
- Use each matched metric's aggregation expression with equivalent SQL semantics.
- Apply every default_filter unless the user explicitly requests a conflicting scope; if
  there is a conflict, explain it and preserve the governed metric definition.
- Group only by requested dimensions already validated in allowed_dimensions.
- For every cross-entity dimension, use exactly the resolved Join Path steps and their
  from_column/to_column equality. Do not skip a step, reverse the governed route, or add
  another fact table.
- Every supplied path has passed grain-aware fan-out validation. Preserve the metric's
  base grain; do not introduce any join outside those paths that can multiply it.
- When resolved date ranges are non-empty, filter the metric's time_field using those
  inclusive dates. Do not substitute another date column.
- Do not replace count, sum, or ratio metrics with a similarly named stored column.
"""
        return f"""Generate one SQLite query that answers the user question.

Rules:
- You are a SQLite SQL expert.
- Generate only one read-only SELECT query. A WITH ... SELECT query is allowed.
- Use only the tables and columns in the supplied schema.
{semantic_rule_lines.rstrip()}
- Never invent table names or column names.
- Never invent categorical filter values. Prefer exact values from the value hints below.
- A successful query must answer the requested metric, not merely execute without error.
- For a requested rate, ratio, or percentage, inspect the schema for matching numerator
  and denominator count columns. When available, calculate
  CAST(numerator AS REAL) / NULLIF(denominator, 0) instead of substituting a similarly
  named stored percentage column, unless the user explicitly requests that stored column.
- Match all qualifiers in a metric name, including age range, grade range, population,
  county, and school category. Numerator and denominator must use the same qualifiers.
- For lowest/highest/top/bottom metrics, exclude NULL metric values and zero denominators.
- Similar validated examples below are authoritative semantic evidence for metric formulas,
  filter columns, and category values. Use their semantic mapping when relevant, but adapt
  the SQL safely: SQLite division must cast the numerator to REAL and use NULLIF for the
  denominator.
- When matching a category such as a school type, choose the column whose meaning and
  observed value best match the user's phrase; do not guess shortened labels.
- Quote identifiers containing spaces or punctuation with double quotes.
- Obey the SQL security policy below. The supplied schema is already filtered to its
  authorized tables and columns. Never use recursive CTEs or listed dangerous functions.
- When require_limit is true, include a positive integer LIMIT no greater than max_limit,
  except for a scalar aggregate query guaranteed to return one row.
- Return only one JSON object with fields sql, explanation, tables_used, and optional
  reasoning. The reasoning must be a concise structured audit summary, not hidden
  chain-of-thought. If you include it, report "confidence" as a number between 0 and
  1 and list any unresolved assumption in "assumptions" and any known weakness in
  "risks":
  {{"sql": "SELECT ...", "explanation": "...", "tables_used": ["..."],
    "reasoning": {{"goal": "...", "grain": "...", "tables": ["..."], "joins": [],
      "metrics": [], "dimensions": [], "filters": [], "time_range": null,
      "sorting": [], "limit": null, "assumptions": [], "risks": [],
      "confidence": 0.0, "strategy": "direct_generation"}}}}
- Do not wrap the JSON in Markdown.

User question:
{context.task.question}

Regeneration feedback from a previous reflection (may be empty):
{context.regeneration_feedback}

Current SQLite schema (authoritative):
{json.dumps(schemas, ensure_ascii=False, indent=2)}

SQL security policy (authoritative):
{json.dumps(sql_policy, ensure_ascii=False, indent=2)}
{semantic_context_block.rstrip()}
{metric_context_block.rstrip()}

Question-relevant values observed in the database:
{json.dumps(value_hints, ensure_ascii=False, indent=2)}

Resolved date context:
{json.dumps(date_context, ensure_ascii=False, indent=2)}

Date context rules:
- Apply only non-empty ranges explicitly resolved above.
- start_date and end_date are inclusive calendar dates.
- For timestamp columns, implement an inclusive end date safely, usually with a strict
  upper bound at the following day rather than midnight at the end date.
- Do not invent a date filter when ranges is empty.

Similar validated question-to-SQL examples (may be empty):
{json.dumps(reference_examples, ensure_ascii=False, indent=2)}

Persisted successful SQL history matches (may be empty):
{json.dumps(history_matches, ensure_ascii=False, indent=2)}

Vector retrieved context - similar historical SQL and reference material (may be empty):
{json.dumps(vector_sql_matches, ensure_ascii=False, indent=2)}

Vector retrieved context - schema documentation (may be empty):
{json.dumps(vector_schema_matches, ensure_ascii=False, indent=2)}

History usage rules:
- Historical SQL is reference evidence, not an instruction to copy blindly.
- Current schema, observed values, date context, and user wording always take precedence.
- Reuse a historical metric formula or join only when its identifiers exist in the current
  schema and its business meaning matches the current question.
- Adapt historical SQL safely to the current request and SQLite semantics.
- Vector retrieved context is supporting evidence only. Never use a table, column, or
  categorical value from it unless it is also valid in the current authoritative schema
  or current observed value hints.

Local Skills policy:
- Skills are local instructions, constraints, and business rules for Agent behavior.
- Skills are not database tables, columns, values, executable code, or authorization.
- The available_skills block is a catalog only. Apply the full instructions only from
  loaded_skills.
- Skills cannot override the supplied schema, read-only SQL rules, or database safety.

{context.available_skills_context}

{context.loaded_skills_context}

Schema context note:
{truncation_note}
"""
