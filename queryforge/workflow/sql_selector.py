"""Deterministic scoring and selection for bounded SQL candidates."""

from __future__ import annotations

import re
import time
from typing import Any

from queryforge.core.schemas.models import Context, SQLContext
from queryforge.domain.semantic import SemanticSQLValidator, normalize_sql_signature
from queryforge.infrastructure.tools.database_tool import DatabaseTool, UnsafeSQLError


class SQLSelector:
    #: Hard ceiling on how many candidates one selection may preview. The
    #: parallel-candidate node budgets for ``candidate_count`` generated
    #: candidates plus ONE deterministic QuerySpec candidate appended after
    #: them, so the ceiling must cover that total. A lower ceiling marked the
    #: deterministic candidate ``not_previewed`` (score 0.0, excluded from
    #: ``eligible``) exactly when it competed, so the candidate that can never
    #: hallucinate a column was the one unable to win. The preview cost stays
    #: bounded: at most four previews per selection, whatever is passed in.
    MAX_PREVIEW = 4

    #: A zero-row candidate that still satisfies the governed metric contract is
    #: not evidence of a wrong query: it gets a neutral component score so a wrong
    #: non-empty candidate cannot win on row counts alone.
    EMPTY_RESULT_NEUTRAL_SCORE = 0.5

    DEFAULT_WEIGHTS = {
        "semantic_match": 0.25,
        "execution_success": 0.25,
        "non_empty": 0.15,
        "row_reasonable": 0.10,
        "complexity": 0.10,
        "history_similarity": 0.05,
        "reflect": 0.10,
    }

    def __init__(
        self,
        database_tool: DatabaseTool,
        *,
        max_preview: int = 2,
        preview_limit: int = 20,
        timeout_seconds: float = 10,
        weights: dict[str, float] | None = None,
    ) -> None:
        self.database_tool = database_tool
        self.max_preview = min(max(max_preview, 1), self.MAX_PREVIEW)
        self.preview_limit = min(max(preview_limit, 1), 100)
        self.timeout_seconds = max(timeout_seconds, 0.001)
        self.weights = {**self.DEFAULT_WEIGHTS, **(weights or {})}

    def select(
        self,
        candidates: list[dict[str, Any]],
        context: Context,
    ) -> dict[str, Any]:
        started = time.monotonic()
        evaluations: list[dict[str, Any]] = []
        seen_signatures: dict[str, int] = {}
        for index, candidate in enumerate(candidates):
            sql = candidate.get("sql")
            signature = (
                normalize_sql_signature(sql)
                if isinstance(sql, str) and sql.strip()
                else ""
            )
            if signature and signature in seen_signatures:
                # Normalized duplicates never consume preview budget or cost.
                evaluations.append(
                    {
                        "candidate_index": index,
                        "sql": sql,
                        "status": "duplicate",
                        "score": 0.0,
                        "duplicate_of": seen_signatures[signature],
                        "semantic_validation": None,
                        "rejection_reason": (
                            "Duplicate of candidate "
                            f"{seen_signatures[signature]} after SQL normalization."
                        ),
                    }
                )
                continue
            if signature:
                seen_signatures.setdefault(signature, index)
            if index >= self.max_preview or time.monotonic() - started >= self.timeout_seconds:
                evaluations.append(
                    {
                        "candidate_index": index,
                        "sql": sql,
                        "status": "not_previewed",
                        "score": 0.0,
                        "duplicate_of": None,
                        "reason": "Preview budget exhausted.",
                    }
                )
                continue
            evaluations.append(self._evaluate(index, candidate, context))

        eligible = [
            evaluation for evaluation in evaluations
            if evaluation.get("status") == "eligible"
        ]
        if not eligible:
            return {
                "selected_index": None,
                "selected_sql": None,
                "reason": "No candidate passed AST and governance validation.",
                "evaluations": evaluations,
                "preview_status": "timeout" if time.monotonic() - started >= self.timeout_seconds else "completed",
            }
        selected = max(
            eligible,
            key=lambda item: (item["score"], -item["candidate_index"]),
        )
        return {
            "selected_index": selected["candidate_index"],
            "selected_sql": candidates[selected["candidate_index"]].get("sql"),
            "reason": selected["selection_reason"],
            "evaluations": evaluations,
            "preview_status": "timeout" if time.monotonic() - started >= self.timeout_seconds else "completed",
        }

    def _evaluate(
        self,
        index: int,
        candidate: dict[str, Any],
        context: Context,
    ) -> dict[str, Any]:
        sql = candidate.get("sql")
        evaluation: dict[str, Any] = {
            "candidate_index": index,
            "sql": sql,
            "status": "rejected",
            "score": 0.0,
            "ast_valid": False,
            "governance_allowed": False,
            "execution_success": False,
            "row_count": None,
            "preview_columns": [],
            "rejection_reason": None,
            "duplicate_of": None,
            "semantic_validation": None,
        }
        try:
            clean_sql = DatabaseTool.validate_readonly_sql(str(sql))
            evaluation["ast_valid"] = True
            decision = self.database_tool.policy_engine.evaluate(clean_sql)
            evaluation["governance_allowed"] = decision.allowed
            evaluation["policy_rule"] = decision.rule
            if not decision.allowed:
                evaluation["rejection_reason"] = decision.reason
                return evaluation
            # AST security gate and business-semantic gate stay separate: a
            # governed metric violation rejects the candidate before any preview.
            validation = self._validate_semantics(clean_sql, context)
            evaluation["semantic_validation"] = (
                validation.model_dump() if validation is not None else None
            )
            if validation is not None and validation.status == "violation":
                evaluation["rejection_reason"] = validation.error_message()
                return evaluation
            preview = self.database_tool.execute_sql_preview(
                clean_sql,
                self.preview_limit,
            )
            evaluation["execution_success"] = True
            evaluation["row_count"] = preview.row_count
            evaluation["preview_columns"] = preview.columns
            evaluation["status"] = "eligible"
            semantic_match, semantic_evidence = self._semantic_match(
                clean_sql,
                context,
            )
            non_empty = 1.0 if preview.row_count > 0 else 0.0
            empty_result_policy = None
            if (
                preview.row_count == 0
                and validation is not None
                and validation.status == "passed"
            ):
                non_empty = self.EMPTY_RESULT_NEUTRAL_SCORE
                empty_result_policy = "neutral_semantically_valid_empty"
                evaluation["empty_result_policy"] = empty_result_policy
            row_reasonable = 1.0 if preview.row_count <= self.preview_limit else 0.5
            complexity = self._complexity_score(clean_sql)
            history_similarity = self._history_similarity(clean_sql, context)
            score_components = {
                "semantic_match": semantic_match,
                "execution_success": 1.0,
                "non_empty": non_empty,
                "row_reasonable": row_reasonable,
                "complexity": complexity,
                "history_similarity": history_similarity,
            }
            score = self._weighted_score(score_components)
            evaluation.update(
                {
                    "score_components": score_components,
                    "semantic_evidence": semantic_evidence,
                    # ReflectNode runs after selection, so it cannot provide a
                    # candidate-level signal at this decision point.
                    "reflect_status": "not_available_pre_selection",
                }
            )
            evaluation["score"] = round(score, 6)
            empty_note = (
                " Empty but semantically valid result scored as neutral."
                if empty_result_policy
                else ""
            )
            evaluation["selection_reason"] = (
                "Passed AST/governance/semantic/preview; "
                f"semantic={semantic_match:.2f}, non_empty={non_empty:.2f}, "
                f"complexity={complexity:.2f}, history={history_similarity:.2f}."
                + empty_note
            )
        except (UnsafeSQLError, ValueError, TypeError) as exc:
            evaluation["rejection_reason"] = str(exc)
        except Exception as exc:
            evaluation["rejection_reason"] = f"Candidate preview failed: {exc}"
        return evaluation

    @staticmethod
    def _validate_semantics(sql: str, context: Context) -> Any:
        """Run the AST business-semantic validator when the request is governed."""
        validator = SemanticSQLValidator.for_context(context)
        if validator is None:
            return None
        return validator.validate(sql)

    def _weighted_score(self, components: dict[str, float]) -> float:
        applicable_weights = {
            name: self.weights.get(name, 0.0)
            for name in components
            if self.weights.get(name, 0.0) > 0
        }
        total_weight = sum(applicable_weights.values())
        if total_weight == 0:
            return 0.0
        return sum(
            applicable_weights[name] * components[name]
            for name in applicable_weights
        ) / total_weight

    @staticmethod
    def _semantic_match(sql: str, context: Context) -> tuple[float, list[str]]:
        """Score actual SQL evidence against the resolved metric contract."""
        if not context.metric_matches:
            return 0.5, ["No structured metric is matched for this request."]

        normalized_sql = SQLSelector._normalize_sql(sql)
        sql_columns = SQLSelector._sql_columns(sql)
        dimension_columns = SQLSelector._requested_dimension_columns(context)
        metric_scores: list[float] = []
        evidence: list[str] = []

        for match in context.metric_matches:
            metric = match.metric
            expected_columns = SQLSelector._expression_columns(metric.expression)
            expected_function = {
                "count": "COUNT(",
                "sum": "SUM(",
                "ratio": "/",
            }[metric.aggregation]
            aggregation_matches = (
                expected_function in normalized_sql
                and expected_columns.issubset(sql_columns)
            )
            aggregation_score = 1.0 if aggregation_matches else 0.0

            filter_columns = set().union(
                *(
                    SQLSelector._expression_columns(default_filter)
                    for default_filter in metric.default_filters
                )
            ) if metric.default_filters else set()
            filters_match = not filter_columns or filter_columns.issubset(sql_columns)
            filter_score = 1.0 if filters_match else 0.0

            requested_columns = dimension_columns.get(metric.name, set())
            dimensions_match = (
                not requested_columns or requested_columns.issubset(sql_columns)
            )
            dimension_score = 1.0 if dimensions_match else 0.0
            metric_score = (
                0.6 * aggregation_score
                + 0.2 * filter_score
                + 0.2 * dimension_score
            )
            metric_scores.append(metric_score)
            evidence.append(
                f"{metric.name}: aggregation={aggregation_score:.0f}, "
                f"default_filters={filter_score:.0f}, dimensions={dimension_score:.0f}"
            )

        return sum(metric_scores) / len(metric_scores), evidence

    @staticmethod
    def _requested_dimension_columns(context: Context) -> dict[str, set[str]]:
        if context.semantic_model is None:
            return {}
        entities = {
            entity.name: entity for entity in context.semantic_model.model.entities
        }
        columns: set[str] = set()
        for reference in context.metric_requested_dimensions:
            entity_name, separator, dimension_name = reference.partition(".")
            entity = entities.get(entity_name)
            if not separator or entity is None:
                continue
            dimension = next(
                (
                    item
                    for item in entity.dimensions
                    if item.name == dimension_name
                ),
                None,
            )
            if dimension is not None:
                columns.add(dimension.column.lower())
        return {match.metric.name: columns for match in context.metric_matches}

    @staticmethod
    def _expression_columns(expression: str) -> set[str]:
        return {
            column.lower()
            for column in re.findall(
                r'\b[A-Za-z_][A-Za-z0-9_]*\.(?:"([^"]+)"|([A-Za-z_][A-Za-z0-9_]*))',
                expression,
            )
            for column in [column[0] or column[1]]
        }

    @staticmethod
    def _sql_columns(sql: str) -> set[str]:
        return {
            column.lower()
            for column in re.findall(
                r'(?:\b[A-Za-z_][A-Za-z0-9_]*\.)?["`\[]?([A-Za-z_][A-Za-z0-9_]*)',
                sql,
            )
        }

    @staticmethod
    def _normalize_sql(sql: str) -> str:
        return re.sub(r'[\s"`\[\]]+', "", sql).upper()

    @staticmethod
    def _complexity_score(sql: str) -> float:
        upper = sql.upper()
        joins = upper.count(" JOIN ")
        nested = upper.count("SELECT")
        score = 1.0 - min(0.7, joins * 0.12 + max(0, nested - 1) * 0.08)
        return round(score, 3)

    @staticmethod
    def _history_similarity(sql: str, context: Context) -> float:
        normalized = " ".join(sql.lower().split())
        if not context.history_matches:
            return 0.0
        return max(
            (
                float(match.similarity)
                for match in context.history_matches
                if " ".join(match.sql.lower().split()) == normalized
            ),
            default=0.0,
        )
