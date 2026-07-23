"""Deterministic entry routing without SQL generation or database access."""

from __future__ import annotations

import re
from dataclasses import dataclass

from queryforge.orchestration.schemas import RoutingDecision, TaskType


@dataclass(frozen=True, slots=True)
class TaskRoute:
    """One deployment-configurable, deterministic task routing rule."""

    task_type: str
    markers: tuple[str, ...]
    confidence: float = 0.9
    reason: str = "Matched a configured route."
    priority: int = 0


class EntryRouterAgent:
    """Classify a request and select a configured pipeline.

    The router intentionally has no WorkflowRunner, model, connector, or tool
    dependency. It cannot generate or execute SQL.
    """

    _SQL_REVIEW_MARKERS = (
        "review sql",
        "sql review",
        "review this sql",
        "check sql",
        "audit sql",
        "审核sql",
        "审核 sql",
        "检查sql",
        "检查 sql",
    )
    _TROUBLESHOOT_MARKERS = (
        "troubleshoot",
        "debug sql",
        "sql error",
        "query error",
        "fix this sql",
        "sql报错",
        "sql 报错",
        "修复sql",
        "修复 sql",
        "排查sql",
        "排查 sql",
    )
    _EXPLAIN_MARKERS = (
        "explain result",
        "explain",
        "explain these rows",
        "explain the output",
        "why",
        "解释结果",
        "解读结果",
        "解释这些数据",
        "为什么",
        "原因",
    )
    _REPORT_MARKERS = (
        "build report",
        "create report",
        "generate report",
        "dashboard",
        "report",
        "生成报告",
        "制作报告",
        "构建报告",
        "生成看板",
        "报表",
    )
    _METADATA_MARKERS = (
        "list tables",
        "show tables",
        "database schema",
        "show schema",
        "metadata",
        "有哪些表",
        "列出表",
        "数据库结构",
        "表结构",
        "元数据",
        "指标列表",
    )

    def __init__(self, extra_routes: tuple[TaskRoute, ...] = ()) -> None:
        self.extra_routes = tuple(
            sorted(extra_routes, key=lambda route: route.priority, reverse=True)
        )

    def route(self, user_input: str, entrypoint: str = "service") -> RoutingDecision:
        normalized = " ".join(user_input.strip().lower().split())
        task_type, confidence, reason = self._classify(normalized)
        profile, score, reasons = self._complexity(normalized, task_type)
        return RoutingDecision(
            task_type=task_type,
            entrypoint=entrypoint,
            confidence=confidence,
            reason=reason,
            pipeline=task_type,
            requires_orchestrator=task_type != "unknown",
            complexity_profile=profile,
            complexity_score=score,
            complexity_reasons=reasons,
        )

    def _classify(self, text: str) -> tuple[TaskType, float, str]:
        if not text or not re.search(r"[\w\u4e00-\u9fff]", text):
            return "unknown", 1.0, "The request has no classifiable text."
        for route in self.extra_routes:
            if self._contains(text, route.markers):
                return route.task_type, route.confidence, route.reason
        if self._contains(text, self._TROUBLESHOOT_MARKERS):
            return "troubleshoot_sql", 0.98, "Matched SQL troubleshooting intent."
        if self._contains(text, self._SQL_REVIEW_MARKERS):
            return "sql_review", 0.98, "Matched SQL review intent."
        if self._contains(text, self._EXPLAIN_MARKERS):
            return "explain_result", 0.96, "Matched result explanation intent."
        if self._contains(text, self._REPORT_MARKERS):
            return "build_report", 0.96, "Matched report-building intent."
        if self._contains(text, self._METADATA_MARKERS):
            return "metadata_query", 0.95, "Matched database metadata intent."
        return "ask_sql", 0.75, "Defaulted a data question to the ask_sql pipeline."

    @staticmethod
    def _contains(text: str, markers: tuple[str, ...]) -> bool:
        return any(marker in text for marker in markers)

    @classmethod
    def _complexity(
        cls,
        text: str,
        task_type: TaskType,
    ) -> tuple[str, int, list[str]]:
        if task_type in {"metadata_query", "sql_review"}:
            return "simple", 0, ["metadata/review path does not need SQL exploration"]
        score = 0
        reasons: list[str] = []
        rules = (
            (r"\b(?:join|joined|across|between)\b|关联|连接|跨表", 2, "cross-table reasoning"),
            (r"\b(?:trend|growth|yoy|mom|compare|versus|vs)\b|趋势|增长|同比|环比|对比", 1, "time/comparison reasoning"),
            (r"\b(?:top|rank|ranking|highest|lowest)\b|排名|排行|最高|最低", 1, "ranking or ordering requirement"),
            (r"\b(?:revenue|sales|margin|rate|ratio|metric)\b|收入|销售额|利润率|指标|比率", 1, "business metric semantics"),
            (r"\b(?:by|per|grouped by)\b.*\b(?:and|then|also)\b|按.*(?:和|再|同时)", 1, "multiple grouping dimensions"),
            (r"\bwhere\b.*\b(?:and|or)\b|在.+(?:并且|同时|且)", 1, "multiple filter conditions"),
        )
        for pattern, weight, reason in rules:
            if re.search(pattern, text, re.IGNORECASE):
                score += weight
                reasons.append(reason)
        if len(text) > 120:
            score += 1
            reasons.append("long analytical request")
        if text.count("?") + text.count("？") > 1:
            score += 1
            reasons.append("multiple questions")
        profile = "complex" if score >= 2 else "simple"
        if not reasons:
            reasons.append("single-step lookup")
        return profile, score, reasons
