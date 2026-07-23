import unittest
from datetime import date

from queryforge.workflow.node.date_parser_node import DateParserNode
from queryforge.workflow.node.gen_sql_node import GenSqlNode
from queryforge.core.schemas.models import Context, SqlTask, TableColumn, TableSchema


TODAY = date(2025, 5, 15)


def context(question: str) -> Context:
    return Context(task=SqlTask(question=question, database_path="sample.sqlite"))


class DateParserNodeTest(unittest.TestCase):
    def assert_range(self, expression: str, start: str, end: str) -> None:
        ranges = DateParserNode.parse_rules(expression, TODAY)
        self.assertEqual(len(ranges), 1)
        self.assertEqual((ranges[0].start_date, ranges[0].end_date), (start, end))

    def test_english_relative_date_rules(self) -> None:
        cases = {
            "today": ("2025-05-15", "2025-05-15"),
            "yesterday": ("2025-05-14", "2025-05-14"),
            "last 30 days": ("2025-04-16", "2025-05-15"),
            "this month": ("2025-05-01", "2025-05-15"),
            "last month": ("2025-04-01", "2025-04-30"),
            "this quarter": ("2025-04-01", "2025-05-15"),
            "last quarter": ("2025-01-01", "2025-03-31"),
            "this year": ("2025-01-01", "2025-05-15"),
            "last year": ("2024-01-01", "2024-12-31"),
            "2024-02-29": ("2024-02-29", "2024-02-29"),
            "in 2024": ("2024-01-01", "2024-12-31"),
        }
        for expression, expected in cases.items():
            with self.subTest(expression=expression):
                self.assert_range(expression, *expected)

    def test_chinese_relative_date_rules(self) -> None:
        cases = {
            "今天": ("2025-05-15", "2025-05-15"),
            "昨天": ("2025-05-14", "2025-05-14"),
            "最近 7 天": ("2025-05-09", "2025-05-15"),
            "本月": ("2025-05-01", "2025-05-15"),
            "上月": ("2025-04-01", "2025-04-30"),
            "本季度": ("2025-04-01", "2025-05-15"),
            "上季度": ("2025-01-01", "2025-03-31"),
            "今年": ("2025-01-01", "2025-05-15"),
            "去年": ("2024-01-01", "2024-12-31"),
            "2024年": ("2024-01-01", "2024-12-31"),
        }
        for expression, expected in cases.items():
            with self.subTest(expression=expression):
                self.assert_range(expression, *expected)

    def test_invalid_explicit_date_fails_clearly(self) -> None:
        result = DateParserNode(today_provider=lambda: TODAY).execute(
            context("Show rows on 2025-02-30")
        )
        self.assertFalse(result.success)
        self.assertIn("Invalid explicit date", result.error or "")

    def test_no_date_does_not_call_disabled_fallback(self) -> None:
        class MustNotRun:
            def generate_json(self, prompt):
                raise AssertionError("fallback must be disabled")

        state = context("How many schools are there?")
        result = DateParserNode(
            llm=MustNotRun(),
            enable_llm_fallback=False,
            today_provider=lambda: TODAY,
        ).execute(state)
        self.assertTrue(result.success)
        self.assertEqual(state.date_context.source, "none")
        self.assertEqual(state.date_context.ranges, [])

    def test_optional_llm_fallback_returns_structured_range(self) -> None:
        class FiscalYearLLM:
            def generate_json(self, prompt):
                return {
                    "ranges": [
                        {
                            "expression": "previous fiscal year",
                            "start_date": "2024-04-01",
                            "end_date": "2025-03-31",
                        }
                    ]
                }

        state = context("Show revenue for the previous fiscal year")
        result = DateParserNode(
            llm=FiscalYearLLM(),
            enable_llm_fallback=True,
            today_provider=lambda: TODAY,
        ).execute(state)
        self.assertTrue(result.success)
        self.assertEqual(state.date_context.source, "llm")
        self.assertEqual(state.date_context.ranges[0].start_date, "2024-04-01")

    def test_gen_sql_prompt_includes_resolved_date_context(self) -> None:
        state = context("Show orders from last 30 days")
        state.relevant_tables = [
            TableSchema(
                table_name="orders",
                columns=[TableColumn(name="created_at", data_type="TEXT")],
            )
        ]
        DateParserNode(today_provider=lambda: TODAY).execute(state)
        prompt = GenSqlNode._build_prompt(state)
        self.assertIn('"start_date": "2025-04-16"', prompt)
        self.assertIn('"end_date": "2025-05-15"', prompt)
        self.assertIn("strict", prompt)
        self.assertIn("upper bound at the following day", prompt)


if __name__ == "__main__":
    unittest.main()
