"""Perform a pre-execution policy gate and non-blocking query risk review."""

from __future__ import annotations

import sqlglot
from sqlglot import expressions as exp

from queryforge.orchestration.agents.base import RoleAgent
from queryforge.orchestration.schemas import ArtifactRef, TaskState
from queryforge.core.schemas.models import Context
from queryforge.domain.security import SQLPolicyViolation
from queryforge.infrastructure.tools.database_tool import DatabaseTool, UnsafeSQLError


class GovernanceAgent(RoleAgent):
    agent_name = "GovernanceAgent"
    artifact_type = "governance_report"
    _SENSITIVE_NAMES = {"email", "phone", "ssn", "password", "token", "secret"}

    def run(
        self, state: TaskState, context: Context, database_tool: DatabaseTool
    ) -> ArtifactRef:
        if context.sql_context is None:
            raise ValueError("Governance requires an SQL candidate")
        sql = context.sql_context.sql
        try:
            decision = database_tool.policy_engine.evaluate(sql)
        except SQLPolicyViolation as exc:
            self.emit(
                state,
                {
                    "allowed": False,
                    "policy": database_tool.policy_summary,
                    "decision": exc.decision.model_dump(mode="json"),
                    "risks": [],
                    "blocking_rule": exc.decision.rule,
                    "reason": exc.decision.reason,
                    "checked_before_execution": True,
                },
                status="blocked",
            )
            raise UnsafeSQLError(str(exc), exc.decision) from exc
        except Exception as exc:
            self.emit(
                state,
                {
                    "allowed": False,
                    "policy": database_tool.policy_summary,
                    "decision": None,
                    "risks": [],
                    "blocking_rule": "policy_engine_unavailable",
                    "reason": f"SQL policy engine failed: {exc}",
                    "checked_before_execution": True,
                },
                status="blocked",
            )
            raise

        risks = self._risk_findings(sql, decision.columns)
        return self.emit(
            state,
            {
                "allowed": True,
                "policy": database_tool.policy_summary,
                "decision": decision.model_dump(mode="json"),
                "checks": {
                    "read_only": True,
                    "single_statement": True,
                    "table_column_scope": True,
                    "dangerous_functions": True,
                },
                "risks": risks,
                "blocking_rule": None,
                "reason": decision.reason,
                "checked_before_execution": True,
            },
        )

    @classmethod
    def _risk_findings(cls, sql: str, columns: list[str]) -> list[dict[str, str]]:
        tree = sqlglot.parse_one(sql, read="sqlite")
        findings: list[dict[str, str]] = []
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
            findings.append(
                {
                    "rule": "unbounded_scan_risk",
                    "severity": "warning",
                    "reason": "Query has neither WHERE nor LIMIT; policy may still allow aggregate use.",
                }
            )
        sensitive = sorted(
            column for column in columns
            if column.rsplit(".", 1)[-1].lower() in cls._SENSITIVE_NAMES
        )
        if sensitive:
            findings.append(
                {
                    "rule": "sensitive_column",
                    "severity": "warning",
                    "reason": "Query references sensitive-looking column(s): " + ", ".join(sensitive),
                }
            )
        return findings
