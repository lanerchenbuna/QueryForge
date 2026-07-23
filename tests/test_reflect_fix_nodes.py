import unittest

from queryforge.workflow.node.fix_node import FixNode
from queryforge.workflow.node.reflect_node import ReflectNode
from queryforge.core.schemas.models import (
    Context,
    ExecutionResult,
    SQLContext,
    SqlTask,
    TableColumn,
    TableSchema,
)
from queryforge.domain.skills import SkillManager


def evaluated_context() -> Context:
    return Context(
        task=SqlTask(question="List item names", database_path="items.sqlite"),
        relevant_tables=[
            TableSchema(
                table_name="items",
                columns=[TableColumn(name="name", data_type="TEXT")],
            )
        ],
        loaded_skill_names=["sql_best_practices"],
        sql_context=SQLContext(
            sql="SELECT missing_name FROM items",
            explanation="Attempt to list names.",
            tables_used=["items"],
        ),
        execution_result=ExecutionResult(
            columns=["name"], rows=[["example"]], row_count=1
        ),
    )


class PayloadLLM:
    def __init__(self, payload) -> None:
        self.payload = payload

    def generate_json(self, prompt):
        self.prompt = prompt
        return self.payload


class ReflectAndFixNodeTest(unittest.TestCase):
    def test_reflect_success_uses_structured_strategy(self) -> None:
        llm = PayloadLLM(
            {
                "success": True,
                "strategy": "success",
                "reason": "The result contains the requested item names.",
                "suggested_fix": None,
            }
        )
        state = evaluated_context()
        result = ReflectNode(llm, SkillManager()).execute(state)
        self.assertTrue(result.success)
        self.assertEqual(state.reflection_result.strategy, "SUCCESS")
        self.assertIn("Execution result summary", llm.prompt)
        self.assertIn("Loaded reflection skills", llm.prompt)

    def test_reflect_rejects_inconsistent_success_flag(self) -> None:
        llm = PayloadLLM(
            {
                "success": True,
                "strategy": "FIX_SQL",
                "reason": "A fix is needed.",
            }
        )
        result = ReflectNode(llm, SkillManager()).execute(evaluated_context())
        self.assertFalse(result.success)
        self.assertIn("success", result.error or "")

    def test_fix_replaces_sql_and_records_attempt(self) -> None:
        llm = PayloadLLM(
            {
                "fixed_sql": "SELECT name FROM items",
                "explanation": "Use the existing name column.",
                "tables_used": ["items"],
            }
        )
        state = evaluated_context()
        state.execution_result = None
        state.retry_count = 1
        state.last_execution_error = "no such column: missing_name"
        result = FixNode(llm, SkillManager()).execute(state)
        self.assertTrue(result.success)
        self.assertEqual(state.sql_context.sql, "SELECT name FROM items")
        self.assertEqual(len(state.fix_attempts), 1)
        self.assertIn("no such column", state.fix_attempts[0].trigger)
        self.assertIn("Resolved date context", llm.prompt)
        self.assertIn("Loaded fix skills", llm.prompt)

    def test_fix_rejects_write_sql_before_retry(self) -> None:
        llm = PayloadLLM(
            {
                "fixed_sql": "DELETE FROM items",
                "explanation": "Unsafe",
                "tables_used": ["items"],
            }
        )
        state = evaluated_context()
        state.retry_count = 1
        result = FixNode(llm, SkillManager()).execute(state)
        self.assertFalse(result.success)
        self.assertIn("read-only", result.error or "")


if __name__ == "__main__":
    unittest.main()
