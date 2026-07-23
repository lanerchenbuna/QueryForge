"""Translate a data question into a SQL-free analysis request."""

from __future__ import annotations

import re

from queryforge.orchestration.agents.base import RoleAgent
from queryforge.orchestration.schemas import ArtifactRef, TaskState
from queryforge.orchestration.schemas.session import SessionMemory
from queryforge.core.schemas.models import Context


class ProductAnalystAgent(RoleAgent):
    agent_name = "ProductAnalystAgent"
    artifact_type = "analysis_request"

    _FILTER_PATTERN = re.compile(
        r"\b(?:where|for|with)\s+(.+?)(?=\b(?:by|order|sort|limit|top)\b|[?.]|$)",
        re.IGNORECASE,
    )
    _GROUP_PATTERN = re.compile(
        r"\b(?:by|per|grouped by|broken down by)\s+([\w ._-]+?)(?=\b(?:where|order|sort|limit|top)\b|[?.]|$)",
        re.IGNORECASE,
    )
    _LIMIT_PATTERN = re.compile(r"\b(?:top|limit)\s+(\d+)\b", re.IGNORECASE)
    _RANKING_PATTERN = re.compile(
        r"\b(?:top|rank|ranking|highest|lowest|largest|smallest)\b|排名|排行|最高|最低|最大|最小",
        re.IGNORECASE,
    )
    _TREND_PATTERN = re.compile(
        r"\b(?:trend|growth|increase|decrease|change|over time|monthly|yearly|yoy)\b|趋势|增长|下降|变化|同比|环比",
        re.IGNORECASE,
    )
    _USER_COUNT_PATTERN = re.compile(
        r"\b(?:user count|users|customer count|customers)\b|用户数|客户数",
        re.IGNORECASE,
    )
    _COMPARISON_PATTERN = re.compile(
        r"\b(?:growth|increase|decrease|compare|comparison|versus|vs|yoy|mom)\b|增长|下降|对比|比较|同比|环比",
        re.IGNORECASE,
    )
    _BREAKDOWN_FOLLOWUP = re.compile(
        r"^(?:by|per|break(?:\s+it)?\s+down\s+by|按|按照)\s+(.+?)(?:\s*(?:again|再|一下|呢))?$",
        re.IGNORECASE,
    )
    _FILTER_FOLLOWUP = re.compile(
        r"^(?:only(?:\s+(?:show|include|look at))?|filter(?:\s+to)?|只看|仅看|只保留)\s+(.+)$",
        re.IGNORECASE,
    )
    _ADD_FOLLOWUP = re.compile(
        r"^(?:also\s+(?:include|add)|add|再加上|加上)\s+(.+)$",
        re.IGNORECASE,
    )
    _REMOVE_FOLLOWUP = re.compile(
        r"^(?:remove|drop|去掉|移除)\s+(.+)$",
        re.IGNORECASE,
    )
    _TOP_FOLLOWUP = re.compile(
        r"^(?:top\s+(\d+)|前\s*(\d+)\s*(?:名|个)?)$",
        re.IGNORECASE,
    )
    _TIME_FOLLOWUP = re.compile(
        r"^(?:break(?:\s+it)?\s+down\s+over\s+time|by\s+time|按时间展开|按时间拆分)$",
        re.IGNORECASE,
    )
    _REFERENCE_FOLLOWUP = re.compile(
        r"\b(?:that result|previous result|same result|刚才那个|那个结果|上一个结果)\b",
        re.IGNORECASE,
    )

    @classmethod
    def rewrite_followup(
        cls,
        question: str,
        memory: SessionMemory,
    ) -> dict[str, str | bool | None]:
        """Rewrite an explicit follow-up into a standalone analytical request."""

        original = question.strip()
        previous = (memory.last_question or "").strip()
        if not original or not previous:
            return {
                "question": original,
                "is_followup": False,
                "reason": None,
            }
        if cls._TIME_FOLLOWUP.match(original):
            return cls._rewrite(
                original,
                previous,
                "Break down the result over time by month.",
                "add_time_dimension",
            )
        breakdown = cls._BREAKDOWN_FOLLOWUP.match(original)
        if breakdown:
            return cls._rewrite(
                original,
                previous,
                f"Break down the result by {breakdown.group(1).strip()}.",
                "add_dimension",
            )
        filtered = cls._FILTER_FOLLOWUP.match(original)
        if filtered:
            return cls._rewrite(
                original,
                previous,
                f"Only include {filtered.group(1).strip()}.",
                "add_filter",
            )
        added = cls._ADD_FOLLOWUP.match(original)
        if added:
            return cls._rewrite(
                original,
                previous,
                f"Also include {added.group(1).strip()} as an additional metric or field.",
                "add_metric",
            )
        removed = cls._REMOVE_FOLLOWUP.match(original)
        if removed:
            return cls._rewrite(
                original,
                previous,
                f"Remove {removed.group(1).strip()} from the grouping or requested metrics.",
                "remove_dimension_or_metric",
            )
        top = cls._TOP_FOLLOWUP.match(original)
        if top:
            limit = top.group(1) or top.group(2)
            return cls._rewrite(
                original,
                previous,
                f"Return the top {limit} results ordered from highest to lowest.",
                "set_ranking",
            )
        if cls._REFERENCE_FOLLOWUP.search(original):
            return cls._rewrite(
                original,
                previous,
                original,
                "resolve_reference",
            )
        return {
            "question": original,
            "is_followup": False,
            "reason": None,
        }

    @staticmethod
    def _rewrite(
        original: str,
        previous: str,
        instruction: str,
        reason: str,
    ) -> dict[str, str | bool]:
        return {
            "question": f"Prior analytical request: {previous} {instruction}",
            "is_followup": True,
            "reason": reason,
        }

    def run(self, state: TaskState, context: Context) -> ArtifactRef:
        try:
            return self._run_parsed(state, context)
        except Exception as exc:
            question = context.task.question.strip()
            return self.emit(
                state,
                {
                    "question": question,
                    "goal": question,
                    "objective": question,
                    "metrics": [],
                    "metric_mappings": [],
                    "dimensions": [],
                    "dimension_mappings": [],
                    "filters": [],
                    "sort_by": None,
                    "date_context": None,
                    "time_range": None,
                    "ordering": None,
                    "limit": None,
                    "grain": "query_result",
                    "target_grain": ["query_result"],
                    "clarification_needed": True,
                    "clarifications": [
                        {
                            "aspect": "analysis_parse_fallback",
                            "question": "Analysis parsing was unavailable; confirm the request intent.",
                            "severity": "medium",
                        }
                    ],
                    "clarification_reasons": [
                        f"Analysis parsing fallback was used: {exc}"
                    ],
                    "ambiguities": ["analysis_parse_fallback"],
                    "assumptions": ["Use the original question as the analysis goal."],
                    "status": "warning",
                    "is_followup": state.is_followup,
                    "rewritten_from": state.original_question if state.is_followup else None,
                    "followup_reason": None,
                },
                status="warning",
            )

    def _run_parsed(self, state: TaskState, context: Context) -> ArtifactRef:
        question = context.task.question.strip()
        semantic_metric_matches = self._semantic_metric_matches(context, question)
        metric_names = [match["metric"] for match in semantic_metric_matches]
        if not metric_names:
            metric_names = [match.metric.name for match in context.metric_matches]
        dimensions = list(context.metric_requested_dimensions)
        if not dimensions:
            dimensions = [
                match.group(1).strip()
                for match in self._GROUP_PATTERN.finditer(question)
            ]
        semantic_dimensions = self._semantic_dimension_matches(context)
        for dimension in semantic_dimensions:
            reference = dimension["reference"]
            if reference not in dimensions and self._dimension_is_grouped(question, dimension):
                dimensions.append(reference)
        filters = [
            match.group(1).strip() for match in self._FILTER_PATTERN.finditer(question)
        ]
        limit_match = self._LIMIT_PATTERN.search(question)
        order = self._order_requirement(question)
        clarifications = self._clarifications(
            question=question,
            metrics=semantic_metric_matches,
            dimensions=dimensions,
            date_context_present=bool(context.date_context and context.date_context.ranges),
        )
        clarification_reasons = [item["question"] for item in clarifications]
        ambiguities = [item["aspect"] for item in clarifications]
        assumptions = self._assumptions(
            semantic_metric_matches,
            dimensions,
            bool(context.date_context and context.date_context.ranges),
        )
        if len(re.findall(r"[\w\u4e00-\u9fff]", question)) < 2:
            clarifications.append(
                {
                    "aspect": "missing_objective",
                    "question": "Please provide a concrete data question or SQL task.",
                    "severity": "high",
                }
            )
            clarification_reasons.append("Please provide a concrete data question or SQL task.")
            ambiguities.append("missing_objective")
        if re.search(r"\b(?:same|that|those|it)\b", question, re.IGNORECASE):
            clarifications.append(
                {
                    "aspect": "missing_conversation_context",
                    "question": "The request references prior context that is not available in this run.",
                    "severity": "medium",
                }
            )
            clarification_reasons.append(
                "The request references prior context that is not available in this run."
            )
            ambiguities.append("missing_conversation_context")
        blocked = any(item["severity"] == "high" for item in clarifications)
        artifact_status = "blocked" if blocked else "warning" if clarifications else "valid"
        time_range = (
            context.date_context.model_dump(mode="json")
            if context.date_context and context.date_context.ranges
            else None
        )
        return self.emit(
            state,
            {
                "question": question,
                "goal": question,
                "objective": question,
                "metrics": metric_names,
                "metric_mappings": semantic_metric_matches,
                "dimensions": dimensions,
                "dimension_mappings": semantic_dimensions,
                "filters": filters,
                "sort_by": order,
                "date_context": (
                    context.date_context.model_dump(mode="json")
                    if context.date_context
                    else None
                ),
                "time_range": time_range,
                "ordering": order,
                "limit": int(limit_match.group(1)) if limit_match else None,
                "grain": ", ".join(dimensions) if dimensions else "query_result",
                "target_grain": dimensions or ["query_result"],
                "clarification_needed": bool(clarification_reasons),
                "clarifications": clarifications,
                "clarification_reasons": clarification_reasons,
                "ambiguities": ambiguities,
                "assumptions": assumptions,
                "status": artifact_status,
                "is_followup": state.is_followup,
                "rewritten_from": state.original_question if state.is_followup else None,
                "followup_reason": (
                    state.followup_reason if state.is_followup else None
                ),
            },
            status=artifact_status,
        )

    @staticmethod
    def _order_requirement(question: str) -> str | None:
        lowered = question.lower()
        if any(word in lowered for word in ("highest", "largest", "top", "最高", "最大")):
            return "descending"
        if any(word in lowered for word in ("lowest", "smallest", "最低", "最小")):
            return "ascending"
        return None

    @staticmethod
    def _semantic_metric_matches(context: Context, question: str) -> list[dict]:
        if context.semantic_model is None:
            return []
        lowered = question.lower()
        matches: list[dict] = []
        for metric in context.semantic_model.model.metrics:
            terms = [metric.name, *metric.synonyms]
            matched_terms = [
                term for term in terms if term and term.lower() in lowered
            ]
            if matched_terms:
                matches.append(
                    {
                        "metric": metric.name,
                        "matched_terms": matched_terms,
                        "entity": metric.entity,
                        "aggregation": metric.aggregation,
                        "expression": metric.expression,
                        "allowed_dimensions": list(metric.allowed_dimensions),
                        "time_field": metric.time_field,
                    }
                )
        return matches

    @staticmethod
    def _semantic_dimension_matches(context: Context) -> list[dict]:
        if context.semantic_model is None:
            return []
        table_to_entity = {
            entity.table: entity.name for entity in context.semantic_model.model.entities
        }
        dimensions: list[dict] = []
        for match in context.semantic_model.matches:
            if match.kind != "dimension":
                continue
            entity = table_to_entity.get(match.table)
            if not entity:
                continue
            dimensions.append(
                {
                    "term": match.term,
                    "entity": entity,
                    "dimension": match.semantic_name,
                    "reference": f"{entity}.{match.semantic_name}",
                    "table": match.table,
                    "column": match.column,
                }
            )
        return dimensions

    @classmethod
    def _dimension_is_grouped(cls, question: str, dimension: dict) -> bool:
        term = re.escape(str(dimension["term"]).lower())
        lowered = question.lower()
        return bool(
            re.search(rf"\b(?:by|per|grouped by|broken down by)\s+.*{term}", lowered)
            or re.search(rf"(按|根据|依照).{{0,12}}{term}", lowered)
        )

    @classmethod
    def _clarifications(
        cls,
        *,
        question: str,
        metrics: list[dict],
        dimensions: list[str],
        date_context_present: bool,
    ) -> list[dict[str, str]]:
        lowered = question.lower()
        clarifications: list[dict[str, str]] = []
        if len(metrics) > 1 and any(
            term in lowered for term in ("revenue", "sales", "收入", "销售额", "营收")
        ):
            clarifications.append(
                {
                    "aspect": "ambiguous_metric_definition",
                    "question": "Multiple revenue or sales metrics matched; confirm the intended business definition.",
                    "severity": "medium",
                }
            )
        if cls._RANKING_PATTERN.search(question) and not dimensions:
            clarifications.append(
                {
                    "aspect": "missing_ranking_dimension",
                    "question": "Ranking was requested but no grouping dimension was specified.",
                    "severity": "medium",
                }
            )
        if cls._TREND_PATTERN.search(question) and not date_context_present:
            clarifications.append(
                {
                    "aspect": "missing_time_range",
                    "question": "Trend or growth analysis usually needs an explicit time range.",
                    "severity": "medium",
                }
            )
        if cls._USER_COUNT_PATTERN.search(question) and not metrics:
            clarifications.append(
                {
                    "aspect": "ambiguous_count_grain",
                    "question": "Count grain is ambiguous; clarify whether this means distinct users/customers or event rows.",
                    "severity": "medium",
                }
            )
        if cls._COMPARISON_PATTERN.search(question) and not any(
            term in lowered for term in ("than", "vs", "versus", "yoy", "mom", "同比", "环比")
        ):
            clarifications.append(
                {
                    "aspect": "missing_comparison_baseline",
                    "question": "Comparison or growth was requested but the baseline is not explicit.",
                    "severity": "medium",
                }
            )
        return clarifications

    @staticmethod
    def _assumptions(
        metrics: list[dict],
        dimensions: list[str],
        date_context_present: bool,
    ) -> list[str]:
        assumptions: list[str] = []
        if metrics:
            assumptions.append(
                "Use the highest-confidence matched semantic metric unless the user specifies otherwise."
            )
        if dimensions:
            assumptions.append("Use requested dimensions as the target result grain.")
        if not date_context_present:
            assumptions.append("No explicit date range is applied unless required by a matched metric.")
        return assumptions
