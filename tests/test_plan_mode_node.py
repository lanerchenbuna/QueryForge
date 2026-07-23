import io
import unittest
from contextlib import redirect_stderr
from unittest.mock import patch

import main as cli
from queryforge.workflow.node.plan_mode_node import PlanModeNode
from queryforge.core.schemas.models import (
    Context,
    DateContext,
    DateRange,
    SQLContext,
    SqlTask,
)


def planned_context(sql: str = "SELECT id FROM orders") -> Context:
    return Context(
        task=SqlTask(question="Show this month's orders", database_path="orders.sqlite"),
        date_context=DateContext(
            reference_date="2025-05-15",
            source="rule",
            ranges=[
                DateRange(
                    expression="this month",
                    start_date="2025-05-01",
                    end_date="2025-05-15",
                )
            ],
        ),
        sql_context=SQLContext(
            sql=sql,
            explanation="Show matching orders.",
            tables_used=["orders"],
        ),
    )


class PlanModeNodeTest(unittest.TestCase):
    def test_auto_approve_builds_plan_without_executing_sql(self) -> None:
        state = planned_context()
        presented = []
        result = PlanModeNode(
            auto_approve=True, presenter=presented.append
        ).execute(state)
        self.assertTrue(result.success)
        self.assertTrue(state.plan_approved)
        self.assertIsNotNone(state.execution_plan)
        self.assertEqual(state.execution_plan.tables, ["orders"])
        self.assertIsNone(state.execution_result)
        self.assertEqual(presented, [state.execution_plan])
        self.assertTrue(any("Date ranges" in risk for risk in state.execution_plan.risks))

    def test_rejected_plan_stops_before_execution(self) -> None:
        state = planned_context()
        result = PlanModeNode(approver=lambda plan: False).execute(state)
        self.assertFalse(result.success)
        self.assertFalse(state.plan_approved)
        self.assertIsNone(state.execution_result)
        self.assertIn("cancelled", result.error or "")

    def test_interactive_approver_requires_full_yes(self) -> None:
        node_state = planned_context()
        PlanModeNode(auto_approve=True).execute(node_state)
        execution_plan = node_state.execution_plan
        for response, expected in (("yes", True), ("YES", True), ("y", False), ("", False), ("no", False)):
            with self.subTest(response=response), patch("builtins.input", return_value=response):
                stderr = io.StringIO()
                with redirect_stderr(stderr):
                    actual = cli._interactive_plan_approver(execution_plan)
                self.assertEqual(actual, expected)
                self.assertIn("Type yes", stderr.getvalue())

    def test_cli_plan_presenter_marks_sql_as_not_executed(self) -> None:
        state = planned_context()
        PlanModeNode(auto_approve=True).execute(state)
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            cli._display_execution_plan(state.execution_plan)
        self.assertIn("SQL has not been executed", stderr.getvalue())
        self.assertIn('"sql": "SELECT id FROM orders"', stderr.getvalue())

    def test_auto_approve_flag_requires_plan_mode(self) -> None:
        stderr = io.StringIO()
        argv = [
            "main.py",
            "--auto-approve-plan",
            "--question",
            "How many schools are there?",
        ]
        with patch("sys.argv", argv), redirect_stderr(stderr):
            exit_code = cli.main()
        self.assertEqual(exit_code, 2)
        self.assertIn("requires --plan-mode", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
