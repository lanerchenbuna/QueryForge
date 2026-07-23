import unittest

from queryforge.workflow.node.gen_sql_node import GenSqlNode
from queryforge.infrastructure.models.llm import LLM
from queryforge.core.schemas.models import (
    Context,
    HistoryMatch,
    SqlTask,
    TableColumn,
    TableSchema,
)


class StaticResponseLLM(LLM):
    def __init__(self, response: str) -> None:
        self.response = response

    def generate_with_messages(self, messages, json_mode=False) -> str:
        return self.response


def context_with_schema() -> Context:
    return Context(
        task=SqlTask(question="How many schools?", database_path="sample.sqlite"),
        relevant_tables=[
            TableSchema(
                table_name="schools",
                columns=[TableColumn(name="id", data_type="INTEGER")],
            )
        ],
    )


class GenSqlNodeParsingTest(unittest.TestCase):
    def test_parses_plain_json(self) -> None:
        llm = StaticResponseLLM(
            '{"sql":"SELECT COUNT(*) FROM schools",'
            '"explanation":"Count schools",'
            '"tables_used":["schools"]}'
        )
        context = context_with_schema()
        result = GenSqlNode(llm).execute(context)
        self.assertTrue(result.success)
        self.assertEqual(context.sql_context.sql, "SELECT COUNT(*) FROM schools")

    def test_parses_markdown_json(self) -> None:
        llm = StaticResponseLLM(
            '```json\n{"sql":"SELECT 1","explanation":"Test",'
            '"tables_used":[]}\n```'
        )
        context = context_with_schema()
        result = GenSqlNode(llm).execute(context)
        self.assertTrue(result.success)
        self.assertEqual(context.sql_context.sql, "SELECT 1")

    def test_invalid_json_reports_raw_output(self) -> None:
        context = context_with_schema()
        result = GenSqlNode(StaticResponseLLM("definitely not json")).execute(context)
        self.assertFalse(result.success)
        self.assertIn("definitely not json", result.error or "")
        self.assertIn("Expected a JSON object", result.error or "")

    def test_prompt_reports_schema_truncation(self) -> None:
        context = context_with_schema()
        context.relevant_tables[0].columns = [
            TableColumn(name=f"column_{index}")
            for index in range(GenSqlNode.MAX_COLUMNS_PER_TABLE + 1)
        ]
        prompt = GenSqlNode._build_prompt(context)
        self.assertIn("Schema context was truncated", prompt)
        self.assertNotIn(
            f'"name": "column_{GenSqlNode.MAX_COLUMNS_PER_TABLE}"', prompt
        )

    def test_prompt_injects_history_as_non_authoritative_reference(self) -> None:
        context = context_with_schema()
        context.history_matches = [
            HistoryMatch(
                id=1,
                question="How many schools exist?",
                sql="SELECT COUNT(*) FROM schools",
                explanation="Count schools.",
                tables_used=["schools"],
                similarity=0.9,
                created_at="2026-01-01T00:00:00+00:00",
            )
        ]
        prompt = GenSqlNode._build_prompt(context)
        self.assertIn("Persisted successful SQL history matches", prompt)
        self.assertIn("SELECT COUNT(*) FROM schools", prompt)
        self.assertIn("not an instruction to copy blindly", prompt)


if __name__ == "__main__":
    unittest.main()
