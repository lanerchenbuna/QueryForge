"""Static SQL review for user-provided SQL without execution."""

from __future__ import annotations

import re
from typing import Any

import sqlglot
from sqlglot import expressions as exp

from queryforge.orchestration.agents.base import RoleAgent
from queryforge.orchestration.schemas import ArtifactRef, TaskState
from queryforge.domain.security import SQLPolicyViolation
from queryforge.domain.semantic import SemanticModelContext
from queryforge.infrastructure.tools.database_tool import DatabaseTool


class SQLReviewAgent(RoleAgent):
    agent_name = "SQLReviewAgent"
    artifact_type = "review_report"

    _FENCED_SQL = re.compile(r"```(?:sql)?\s*(.*?)```", re.IGNORECASE | re.DOTALL)
    _SQL_START = re.compile(
        r"\b(with|select)\b[\s\S]*?(?=$|\n\s*(?:error|报错|请|帮|why|原因)[:：])",
        re.IGNORECASE,
    )

    def run(
        self,
        state: TaskState,
        user_input: str,
        database_tool: DatabaseTool,
        semantic_model: SemanticModelContext | None = None,
    ) -> ArtifactRef:
        sql = self.extract_sql(user_input)
        findings: list[dict[str, Any]] = []
        syntax_valid = False
        formatted_sql: str | None = None
        parse_error: str | None = None
        tree: exp.Expression | None = None

        if not sql:
            findings.append(
                {
                    "rule": "missing_sql",
                    "severity": "error",
                    "reason": "No SELECT or WITH statement was found in the request.",
                }
            )
        else:
            try:
                tree = sqlglot.parse_one(sql, read="sqlite")
                syntax_valid = True
                formatted_sql = tree.sql(dialect="sqlite", pretty=True)
            except Exception as exc:  # sqlglot raises several parser errors
                parse_error = str(exc)
                findings.append(
                    {
                        "rule": "sql_parse",
                        "severity": "error",
                        "reason": parse_error,
                    }
                )

        policy_payload: dict[str, Any] = {
            "allowed": False,
            "decision": None,
            "reason": "SQL was not parsed.",
        }
        if sql and syntax_valid:
            try:
                decision = database_tool.policy_engine.evaluate(sql)
                policy_payload = {
                    "allowed": True,
                    "decision": decision.model_dump(mode="json"),
                    "reason": decision.reason,
                }
            except SQLPolicyViolation as exc:
                policy_payload = {
                    "allowed": False,
                    "decision": exc.decision.model_dump(mode="json"),
                    "reason": exc.decision.reason,
                }
                findings.append(
                    {
                        "rule": exc.decision.rule,
                        "severity": "error",
                        "reason": exc.decision.reason,
                    }
                )

        if tree is not None:
            findings.extend(self._static_findings(tree))
            semantic_payload, semantic_findings = self._semantic_findings(
                tree,
                semantic_model,
            )
            findings.extend(semantic_findings)
        else:
            semantic_payload = {
                "checked": False,
                "reason": "SQL could not be parsed for semantic review.",
            }

        hard_errors = [item for item in findings if item["severity"] == "error"]
        warnings = [item for item in findings if item["severity"] == "warning"]
        score = max(0, 100 - len(hard_errors) * 35 - len(warnings) * 10)
        return self.emit(
            state,
            {
                "sql": sql,
                "formatted_sql": formatted_sql,
                "syntax": {
                    "valid": syntax_valid,
                    "error": parse_error,
                },
                "security": policy_payload,
                "performance": {
                    "findings": [
                        item for item in findings
                        if item["rule"] in {"select_star", "unbounded_scan_risk", "high_cost_join"}
                    ]
                },
                "readability": {
                    "formatted": formatted_sql is not None,
                    "uses_aliases": bool(tree and list(tree.find_all(exp.Alias))),
                },
                "semantic": semantic_payload,
                "findings": findings,
                "score": score,
                "recommendations": self._recommendations(findings),
                "executed": False,
            },
            status="blocked" if hard_errors else "warning" if warnings else "valid",
        )

    @classmethod
    def extract_sql(cls, text: str) -> str:
        fenced = cls._FENCED_SQL.search(text)
        if fenced:
            return cls._strip_sql(fenced.group(1))
        match = cls._SQL_START.search(text)
        if not match:
            return ""
        return cls._strip_sql(match.group(0))

    @staticmethod
    def _strip_sql(sql: str) -> str:
        cleaned = sql.strip()
        prefixes = ("review sql:", "review this sql:", "check sql:", "审核 sql：", "审核sql：")
        lowered = cleaned.lower()
        for prefix in prefixes:
            if lowered.startswith(prefix):
                cleaned = cleaned[len(prefix):].strip()
                break
        return cleaned.rstrip(";") + ";" if cleaned and not cleaned.endswith(";") else cleaned

    @staticmethod
    def _static_findings(tree: exp.Expression) -> list[dict[str, str]]:
        findings: list[dict[str, str]] = []
        if any(isinstance(select, exp.Star) for select in tree.find_all(exp.Star)):
            findings.append(
                {
                    "rule": "select_star",
                    "severity": "warning",
                    "reason": "SELECT * makes result shape unstable; list required columns explicitly.",
                }
            )
        joins = list(tree.find_all(exp.Join))
        if len(joins) >= 3:
            findings.append(
                {
                    "rule": "high_cost_join",
                    "severity": "warning",
                    "reason": f"Query contains {len(joins)} joins.",
                }
            )
        if tree.find(exp.Where) is None and tree.find(exp.Limit) is None:
            sql_text = tree.sql(dialect="sqlite").lower()
            aggregate = any(
                f"{function_name}(" in sql_text
                for function_name in ("count", "sum", "avg", "min", "max")
            )
            if not aggregate:
                findings.append(
                    {
                        "rule": "unbounded_scan_risk",
                        "severity": "warning",
                        "reason": "Query has neither WHERE nor LIMIT.",
                    }
                )
        return findings

    @staticmethod
    def _semantic_findings(
        tree: exp.Expression,
        semantic_model: SemanticModelContext | None,
    ) -> tuple[dict[str, Any], list[dict[str, str]]]:
        """Validate explicitly referenced governed metric aliases when available."""
        if semantic_model is None or not semantic_model.model.metrics:
            return (
                {
                    "checked": False,
                    "reason": "No semantic model metrics are configured.",
                },
                [],
            )
        sql_text = tree.sql(dialect="sqlite").lower()
        aliases = {
            alias.alias.lower()
            for alias in tree.find_all(exp.Alias)
            if alias.alias
        }
        columns = {
            column.name.lower()
            for column in tree.find_all(exp.Column)
            if column.name
        }
        matched: list[str] = []
        findings: list[dict[str, str]] = []
        for metric in semantic_model.model.metrics:
            if metric.name.lower() not in aliases:
                continue
            required_columns = {
                column.lower()
                for column in re.findall(
                    r'\b[A-Za-z_][A-Za-z0-9_]*\.(?:"([^"]+)"|([A-Za-z_][A-Za-z0-9_]*))',
                    metric.expression,
                )
                for column in [column[0] or column[1]]
            }
            expected_token = {
                "count": "count(",
                "sum": "sum(",
                "ratio": "/",
            }[metric.aggregation]
            if expected_token in sql_text and required_columns.issubset(columns):
                matched.append(metric.name)
                continue
            findings.append(
                {
                    "rule": "semantic_metric_contract",
                    "severity": "warning",
                    "reason": (
                        f"SQL aliases {metric.name!r} but does not match its "
                        "governed aggregation expression."
                    ),
                }
            )
        return (
            {
                "checked": True,
                "matched_metrics": matched,
                "declared_metric_aliases": sorted(aliases),
                "reason": "Validated explicitly aliased governed metrics.",
            },
            findings,
        )

    @staticmethod
    def _recommendations(findings: list[dict[str, Any]]) -> list[str]:
        recommendations: list[str] = []
        rules = {item["rule"] for item in findings}
        if "missing_sql" in rules:
            recommendations.append("Provide a SELECT or WITH query for review.")
        if "sql_parse" in rules:
            recommendations.append("Fix SQL syntax before applying semantic or policy review.")
        if "select_star" in rules:
            recommendations.append("Replace SELECT * with explicit column names.")
        if "unbounded_scan_risk" in rules:
            recommendations.append("Add an appropriate WHERE or LIMIT clause when returning detail rows.")
        if not recommendations:
            recommendations.append("No blocking static review issues were found.")
        return recommendations
