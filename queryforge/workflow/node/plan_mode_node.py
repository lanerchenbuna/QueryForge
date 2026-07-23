"""Build a reviewable execution plan and require approval before SQL runs."""

from __future__ import annotations

import re
from typing import Callable

from queryforge.workflow.node.base import Node
from queryforge.core.schemas.models import Context, ExecutionPlan, NodeResult
from queryforge.infrastructure.tools.database_tool import DatabaseTool, UnsafeSQLError


PlanApprover = Callable[[ExecutionPlan], bool]
PlanPresenter = Callable[[ExecutionPlan], None]


class PlanModeNode(Node):
    name = "plan_mode_check"
    description = "Show generated SQL and require explicit approval before execution"

    def __init__(
        self,
        auto_approve: bool = False,
        approver: PlanApprover | None = None,
        presenter: PlanPresenter | None = None,
        approval_required: bool = True,
    ) -> None:
        self.auto_approve = auto_approve
        self.approver = approver
        self.presenter = presenter
        self.approval_required = approval_required

    def execute(self, context: Context) -> NodeResult:
        if context.sql_context is None:
            return self.failure("Cannot build a plan before SQL generation")

        plan = ExecutionPlan(
            question=context.task.question,
            tables=context.sql_context.tables_used,
            date_context=context.date_context,
            sql=context.sql_context.sql,
            risks=self._identify_risks(context),
        )
        context.plan_mode = True
        context.execution_plan = plan
        if self.presenter is not None:
            self.presenter(plan)

        if not self.approval_required:
            context.plan_approved = None
            return self.success("Execution plan built without executing SQL")

        if self.auto_approve:
            approved = True
        elif self.approver is not None:
            approved = bool(self.approver(plan))
        else:
            approved = False
        context.plan_approved = approved
        if not approved:
            return self.failure(
                "Plan was not approved; SQL execution was cancelled"
            )
        return self.success("Execution plan approved")

    @staticmethod
    def _identify_risks(context: Context) -> list[str]:
        assert context.sql_context is not None
        sql = context.sql_context.sql
        risks = [
            "The SQL is model-generated; verify metric semantics and join keys before execution."
        ]
        try:
            DatabaseTool.validate_readonly_sql(sql)
        except UnsafeSQLError as exc:
            risks.append(f"Read-only validation will reject this SQL: {exc}")
        if re.search(r"\bselect\s+\*", sql, re.IGNORECASE):
            risks.append("SELECT * may expose unnecessary columns and increase data volume.")
        if re.search(r"\bjoin\b", sql, re.IGNORECASE):
            risks.append("Joins may multiply rows if the selected keys are not unique.")
        if context.date_context and context.date_context.ranges:
            risks.append(
                "Date ranges are inclusive calendar dates; verify the chosen database "
                "date column and timestamp boundary handling."
            )
        if not re.search(r"\blimit\s+\d+", sql, re.IGNORECASE) and not re.search(
            r"\b(count|sum|avg|min|max)\s*\(", sql, re.IGNORECASE
        ):
            risks.append("The query has no LIMIT and may return many rows.")
        return risks
