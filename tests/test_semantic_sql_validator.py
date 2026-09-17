"""Offline tests for steps 06/07: AST semantic validation, QuerySpec, repair loop.

Every test builds a small deterministic SQLite fixture plus a semantic model YAML
in a temporary directory and exercises the real validator/compiler/selector.
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

import yaml

from queryforge.core.schemas.models import (
    Context,
    DateContext,
    DateRange,
    NodeResult,
    SQLContext,
    SqlAttempt,
    SqlPolicyDecision,
    SqlTask,
)
from queryforge.domain.semantic import (
    QuerySpecCompiler,
    SemanticModelLoader,
    SemanticSQLValidator,
    normalize_sql_signature,
)
from queryforge.domain.skills import SkillManager
from queryforge.infrastructure.db.sqlite_connector import SQLiteConnector
from queryforge.infrastructure.tools.database_tool import DatabaseTool, UnsafeSQLError
from queryforge.workflow.errors import (
    WorkflowErrorCategory,
    categorize_error,
    guidance_for,
    record_error_category,
)
from queryforge.workflow.node.base import Node
from queryforge.workflow.node.execute_sql_node import ExecuteSqlNode
from queryforge.workflow.node.fix_node import FixNode
from queryforge.workflow.sql_selector import SQLSelector
from queryforge.workflow.workflow import ReflectiveWorkflow, WorkflowError


SCHEMA = """
CREATE TABLE dim_item (
    item_id INTEGER PRIMARY KEY,
    category TEXT NOT NULL,
    is_valid INTEGER NOT NULL
);
CREATE TABLE fact_sales (
    sale_id INTEGER PRIMARY KEY,
    item_id INTEGER NOT NULL,
    amount REAL NOT NULL,
    is_valid INTEGER NOT NULL,
    sale_date_key INTEGER NOT NULL,
    FOREIGN KEY (item_id) REFERENCES dim_item(item_id)
);
CREATE TABLE fact_returns (
    return_id INTEGER PRIMARY KEY,
    sale_id INTEGER NOT NULL,
    amount REAL NOT NULL,
    FOREIGN KEY (sale_id) REFERENCES fact_sales(sale_id)
);
"""

ITEMS = [(1, "a", 1), (2, "b", 1), (3, "a", 0)]
SALES = [
    (1, 1, 10.0, 1, 20250101),
    (2, 2, 20.0, 1, 20250102),
    (3, 1, 30.0, 1, 20250601),
    (4, 3, 40.0, 0, 20250103),
    (5, 3, 5.0, 0, 20250103),
]
RETURNS = [(1, 1, 2.5), (2, 4, 1.5)]

SEMANTIC_MODEL = {
    "version": 1,
    "name": "shop_fixture",
    "description": "Deterministic fixture semantic model for validator tests.",
    "entities": [
        {
            "name": "item",
            "table": "dim_item",
            "description": "Product dimension.",
            "entity_type": "dimension",
            "primary_key": ["item_id"],
            "grain": ["item_id"],
            "expected_columns": ["item_id", "category", "is_valid"],
            "dimensions": [
                {"name": "category", "column": "category", "synonyms": ["product category"]}
            ],
        },
        {
            "name": "sale",
            "table": "fact_sales",
            "description": "Sales fact at one row per sale.",
            "entity_type": "fact",
            "primary_key": ["sale_id"],
            "grain": ["sale_id"],
            "expected_columns": [
                "sale_id",
                "item_id",
                "amount",
                "is_valid",
                "sale_date_key",
            ],
            "dimensions": [
                {"name": "item_id", "column": "item_id", "synonyms": ["sold item"]}
            ],
        },
        {
            "name": "sale_return",
            "table": "fact_returns",
            "description": "Return fact at one row per returned sale.",
            "entity_type": "fact",
            "primary_key": ["return_id"],
            "grain": ["return_id"],
            "expected_columns": ["return_id", "sale_id", "amount"],
            "dimensions": [{"name": "amount", "column": "amount"}],
        },
    ],
    "relationships": [
        {
            "name": "sales_to_item",
            "from": "fact_sales.item_id",
            "to": "dim_item.item_id",
            "relationship_type": "many_to_one",
        },
        {
            "name": "returns_to_sale",
            "from": "fact_returns.sale_id",
            "to": "fact_sales.sale_id",
            "relationship_type": "many_to_one",
        },
    ],
    "join_paths": [
        {
            "name": "sales_to_item_path",
            "from_entity": "sale",
            "to_entity": "item",
            "relationships": ["sales_to_item"],
            "description": "Safe many-to-one path from sales to the product dimension.",
        }
    ],
    "metrics": [
        {
            "name": "net_sales",
            "description": "Valid sales amount in US dollars.",
            "entity": "sale",
            "aggregation": "sum",
            "expression": "SUM(fact_sales.amount)",
            "synonyms": ["net sales", "revenue"],
            "default_filters": ["fact_sales.is_valid = 1"],
            "allowed_dimensions": ["item.category"],
            "time_field": "fact_sales.sale_date_key",
        },
        {
            "name": "valid_order_count",
            "description": "Distinct valid sales.",
            "entity": "sale",
            "aggregation": "count",
            "expression": "COUNT(DISTINCT fact_sales.sale_id)",
            "synonyms": ["valid orders"],
            "default_filters": ["fact_sales.is_valid = 1"],
            "allowed_dimensions": ["item.category"],
        },
        {
            "name": "valid_ratio",
            "description": "Valid sales divided by all sales.",
            "entity": "sale",
            "aggregation": "ratio",
            "expression": (
                "CAST(SUM(fact_sales.is_valid) AS REAL) / NULLIF(COUNT(*), 0)"
            ),
            "synonyms": ["valid ratio"],
            "default_filters": [],
            "allowed_dimensions": ["item.category"],
        },
    ],
}


class FixtureMixin:
    """Small governed database + semantic model reused by every test case."""

    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.root = Path(self._directory.name)
        self.database = self.root / "shop.sqlite"
        connection = sqlite3.connect(self.database)
        connection.executescript(SCHEMA)
        connection.executemany("INSERT INTO dim_item VALUES (?, ?, ?)", ITEMS)
        connection.executemany("INSERT INTO fact_sales VALUES (?, ?, ?, ?, ?)", SALES)
        connection.executemany("INSERT INTO fact_returns VALUES (?, ?, ?)", RETURNS)
        connection.commit()
        connection.close()
        self.model_path = self.root / "semantic_model.yml"
        self.model_path.write_text(
            yaml.safe_dump(SEMANTIC_MODEL, sort_keys=False), encoding="utf-8"
        )
        with SQLiteConnector(str(self.database)) as connector:
            tool = DatabaseTool(connector)
            self.schemas = [tool.describe_table(table) for table in tool.list_tables()]

    def tearDown(self) -> None:
        self._directory.cleanup()

    # ------------------------------------------------------------- factories
    def load_model(self, question: str):
        return SemanticModelLoader.load_and_validate(
            self.model_path, self.schemas, question
        )

    def tool(self) -> DatabaseTool:
        return DatabaseTool(SQLiteConnector(str(self.database)))

    def rows(self, sql: str) -> list[list]:
        """Execute reference SQL through an independent read-only connection."""
        with SQLiteConnector(str(self.database)) as connector:
            return DatabaseTool(connector).execute_sql(sql).rows

    def context(
        self,
        question: str = "What are net sales?",
        *,
        requested: list[str] | None = None,
        date_context: DateContext | None = None,
    ) -> Context:
        """Governed context mirroring what SchemaLinking/MetricSearch produce."""
        semantic = self.load_model(question)
        matches = SemanticModelLoader.match_metrics(semantic.model, question)
        paths = []
        for reference in requested or []:
            entity_name = reference.split(".", 1)[0]
            for match in matches:
                path = SemanticModelLoader.resolve_join_path(
                    semantic.model, match.metric.entity, entity_name
                )
                if path is not None and all(
                    existing.name != path.name for existing in paths
                ):
                    paths.append(path)
        return Context(
            task=SqlTask(question=question, database_path=str(self.database)),
            semantic_model=semantic,
            metric_matches=matches,
            metric_requested_dimensions=list(requested or []),
            metric_join_paths=paths,
            date_context=date_context,
        )

    def validate(self, sql: str, **kwargs) -> object:
        context = self.context(**kwargs)
        validator = SemanticSQLValidator.for_context(context)
        self.assertIsNotNone(validator)
        return validator.validate(sql)


class SemanticValidatorViolationTest(FixtureMixin, unittest.TestCase):
    """Counter-examples that MUST be reported as violations."""

    def test_default_filter_wrong_value_is_a_violation(self) -> None:
        result = self.validate(
            "SELECT SUM(s.amount) AS net_sales FROM fact_sales s WHERE s.is_valid = 0"
        )
        self.assertEqual(result.status, "violation")
        self.assertIn("default_filter", result.rule_names)
        self.assertIn("is_valid", result.error_message())

    def test_default_filter_changed_from_one_to_zero_via_cte_is_a_violation(self) -> None:
        result = self.validate(
            "WITH raw AS (SELECT amount, is_valid FROM fact_sales) "
            "SELECT SUM(r.amount) AS net_sales FROM raw r WHERE r.is_valid = 0"
        )
        self.assertEqual(result.status, "violation")
        self.assertIn("default_filter", result.rule_names)

    def test_missing_default_filter_is_a_violation(self) -> None:
        result = self.validate("SELECT SUM(s.amount) AS net_sales FROM fact_sales s")
        self.assertEqual(result.status, "violation")
        self.assertIn("default_filter", result.rule_names)
        self.assertIn("missing", result.error_message())

    def test_filter_in_unrelated_cte_is_a_violation(self) -> None:
        result = self.validate(
            "WITH stale AS (SELECT * FROM fact_sales WHERE is_valid = 1) "
            "SELECT SUM(s.amount) AS net_sales FROM fact_sales s"
        )
        self.assertEqual(result.status, "violation")
        self.assertIn("default_filter", result.rule_names)

    def test_wrong_join_key_is_a_violation(self) -> None:
        result = self.validate(
            "SELECT i.category, SUM(s.amount) AS net_sales FROM fact_sales s "
            "JOIN dim_item i ON s.sale_id = i.item_id "
            "WHERE s.is_valid = 1 GROUP BY i.category",
            requested=["item.category"],
        )
        self.assertEqual(result.status, "violation")
        self.assertIn("join_key", result.rule_names)
        self.assertIn("fact_sales.item_id", result.error_message())
        self.assertIn("dim_item.item_id", result.error_message())

    def test_missing_governed_table_is_a_join_key_violation(self) -> None:
        result = self.validate(
            "SELECT i.category, SUM(s.amount) AS net_sales FROM fact_sales s "
            "WHERE s.is_valid = 1 GROUP BY i.category",
            requested=["item.category"],
        )
        self.assertEqual(result.status, "violation")
        self.assertIn("join_key", result.rule_names)
        self.assertIn("omitted required table", result.error_message())

    def test_direct_fact_to_fact_join_is_a_fanout_violation(self) -> None:
        result = self.validate(
            "SELECT SUM(s.amount) AS net_sales FROM fact_sales s "
            "JOIN fact_returns r ON s.sale_id = r.sale_id WHERE s.is_valid = 1"
        )
        self.assertEqual(result.status, "violation")
        self.assertIn("fanout", result.rule_names)
        self.assertIn("Fan-out execution guard", result.error_message())
        self.assertIn("one_to_many", result.error_message())

    def test_missing_group_by_dimension_is_a_violation(self) -> None:
        result = self.validate(
            "SELECT SUM(s.amount) AS net_sales FROM fact_sales s "
            "JOIN dim_item i ON s.item_id = i.item_id WHERE s.is_valid = 1",
            requested=["item.category"],
        )
        self.assertEqual(result.status, "violation")
        self.assertIn("grain", result.rule_names)
        self.assertIn("item.category", result.error_message())

    def test_wrong_aggregation_shape_is_a_violation(self) -> None:
        result = self.validate("SELECT COUNT(*) AS net_sales FROM fact_sales s")
        self.assertEqual(result.status, "violation")
        self.assertIn("metric_expression", result.rule_names)

    def test_missing_time_filter_is_a_violation(self) -> None:
        result = self.validate(
            "SELECT SUM(s.amount) AS net_sales FROM fact_sales s WHERE s.is_valid = 1",
            date_context=DateContext(
                reference_date="2025-02-01",
                source="rule",
                ranges=[
                    DateRange(
                        expression="last month",
                        start_date="2025-01-01",
                        end_date="2025-01-31",
                    )
                ],
            ),
        )
        self.assertEqual(result.status, "violation")
        self.assertIn("time_filter", result.rule_names)

    def test_unknown_table_and_column_are_violations(self) -> None:
        result = self.validate(
            "SELECT SUM(s.amount) AS net_sales FROM fact_sales s "
            "JOIN dim_unknown u ON s.item_id = u.item_id WHERE s.is_valid = 1"
        )
        self.assertEqual(result.status, "violation")
        self.assertIn("unknown_table_or_column", result.rule_names)

        result = self.validate(
            "SELECT SUM(s.amount) AS net_sales FROM fact_sales s "
            "WHERE s.is_valid = 1 AND s.missing_column = 1"
        )
        self.assertEqual(result.status, "violation")
        self.assertIn("unknown_table_or_column", result.rule_names)

    def test_unsupported_shapes_are_never_passed(self) -> None:
        unsupported = self.validate(
            "SELECT SUM(s.amount) AS net_sales FROM fact_sales s WHERE s.is_valid = 1 "
            "UNION SELECT SUM(r.amount) AS net_sales FROM fact_returns r"
        )
        self.assertEqual(unsupported.status, "unsupported")
        self.assertTrue(unsupported.unsupported_reason)
        self.assertEqual(unsupported.violations, [])

        nested = self.validate(
            "SELECT SUM(MAX(s.amount)) AS net_sales FROM fact_sales s "
            "WHERE s.is_valid = 1"
        )
        self.assertEqual(nested.status, "unsupported")

        recursive = self.validate(
            "WITH RECURSIVE nums(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM nums "
            "WHERE n < 3) SELECT SUM(s.amount) AS net_sales FROM fact_sales s "
            "WHERE s.is_valid = 1"
        )
        self.assertEqual(recursive.status, "unsupported")
        self.assertIn("recursive", recursive.unsupported_reason)

        multiple = self.validate(
            "SELECT SUM(s.amount) AS net_sales FROM fact_sales s "
            "WHERE s.is_valid = 1; DROP TABLE dim_item"
        )
        self.assertEqual(multiple.status, "unsupported")
        self.assertIn("exactly one SQLite statement", multiple.unsupported_reason)


class SemanticValidatorPositiveTest(FixtureMixin, unittest.TestCase):
    """Equivalent, alias, and CTE shapes must not be wrongly rejected."""

    def test_correct_alias_query_passes_with_evidence(self) -> None:
        result = self.validate(
            "SELECT SUM(s.amount) AS net_sales FROM fact_sales AS s "
            "WHERE s.is_valid = 1"
        )
        self.assertEqual(result.status, "passed")
        self.assertEqual(result.violations, [])
        self.assertEqual(result.evidence["metrics_checked"], ["net_sales"])
        self.assertEqual(result.evidence["default_filters"][0]["status"], "value_checked")
        self.assertIn("fact_sales.amount", result.evidence["columns_found"])

    def test_non_recursive_cte_wrapping_correct_query_passes(self) -> None:
        result = self.validate(
            "WITH valid_sales AS (SELECT s.item_id, s.amount FROM fact_sales s "
            "WHERE s.is_valid = 1) "
            "SELECT i.category, SUM(v.amount) AS net_sales FROM valid_sales v "
            "JOIN dim_item i ON v.item_id = i.item_id GROUP BY i.category",
            requested=["item.category"],
        )
        self.assertEqual(result.status, "passed")
        self.assertIn("value_checked", [
            item["status"] for item in result.evidence["default_filters"]
        ])
        self.assertIn(
            "fact_sales.item_id = dim_item.item_id",
            result.evidence["join_keys_verified"],
        )
        self.assertEqual(result.evidence["group_by"], ["dim_item.category"])

    def test_ratio_metric_accepts_equivalent_average_shape(self) -> None:
        result = self.validate(
            "SELECT AVG(s.is_valid) AS valid_ratio FROM fact_sales s",
            question="What is the valid ratio?",
        )
        self.assertEqual(result.status, "passed")

    def test_count_distinct_metric_passes(self) -> None:
        result = self.validate(
            "SELECT COUNT(DISTINCT s.sale_id) AS valid_order_count "
            "FROM fact_sales s WHERE s.is_valid = 1",
            question="How many valid orders?",
        )
        self.assertEqual(result.status, "passed")

    def test_case_style_default_filter_passes(self) -> None:
        result = self.validate(
            "SELECT SUM(CASE WHEN s.is_valid = 1 THEN s.amount ELSE 0 END) AS net_sales "
            "FROM fact_sales s"
        )
        self.assertEqual(result.status, "passed")


class QuerySpecCompilerTest(FixtureMixin, unittest.TestCase):
    def spec(self, question: str, **kwargs):
        context = self.context(question, **kwargs)
        spec = QuerySpecCompiler.for_context(context, limit=100)
        self.assertIsNotNone(spec)
        return spec

    def test_sum_metric_matches_hand_executed_sql(self) -> None:
        spec = self.spec("What are net sales?")
        self.assertIn("SUM(fact_sales.amount)", spec.sql)
        self.assertIn("fact_sales.is_valid = 1", spec.sql)
        compiled = self.rows(spec.sql)[0][0]
        reference = self.rows(
            "SELECT SUM(amount) FROM fact_sales WHERE is_valid = 1"
        )[0][0]
        self.assertEqual(compiled, 60.0)
        self.assertEqual(compiled, reference)

    def test_count_metric_matches_hand_executed_sql(self) -> None:
        spec = self.spec("How many valid orders?")
        self.assertIn("COUNT(DISTINCT fact_sales.sale_id)", spec.sql)
        compiled = self.rows(spec.sql)[0][0]
        reference = self.rows(
            "SELECT COUNT(DISTINCT sale_id) FROM fact_sales WHERE is_valid = 1"
        )[0][0]
        self.assertEqual(compiled, 3)
        self.assertEqual(compiled, reference)

    def test_ratio_metric_matches_hand_executed_sql(self) -> None:
        spec = self.spec("What is the valid ratio?")
        compiled = self.rows(spec.sql)[0][0]
        reference = self.rows(
            "SELECT CAST(SUM(is_valid) AS REAL) / NULLIF(COUNT(*), 0) FROM fact_sales"
        )[0][0]
        self.assertAlmostEqual(compiled, 0.6, places=12)
        self.assertAlmostEqual(compiled, reference, places=12)

    def test_grouped_metric_uses_resolved_join_path_and_gold_rows(self) -> None:
        spec = self.spec("What are net sales by product category?", requested=["item.category"])
        self.assertIn(
            "JOIN dim_item ON fact_sales.item_id = dim_item.item_id", spec.sql
        )
        self.assertIn("GROUP BY dim_item.category", spec.sql)
        rows = self.rows(spec.sql)
        self.assertEqual([list(row) for row in rows], [["a", 40.0], ["b", 20.0]])

    def test_date_range_filter_matches_gold_and_passes_validator(self) -> None:
        date_context = DateContext(
            reference_date="2025-02-01",
            source="rule",
            ranges=[
                DateRange(
                    expression="last month",
                    start_date="2025-01-01",
                    end_date="2025-01-31",
                )
            ],
        )
        context = self.context("What are net sales?", date_context=date_context)
        spec = QuerySpecCompiler.for_context(context, limit=100)
        self.assertIsNotNone(spec)
        self.assertIn(
            "fact_sales.sale_date_key BETWEEN 20250101 AND 20250131", spec.sql
        )
        rows = self.rows(spec.sql)
        reference = self.rows(
            "SELECT SUM(amount) FROM fact_sales WHERE is_valid = 1 "
            "AND sale_date_key BETWEEN 20250101 AND 20250131"
        )
        self.assertEqual([list(row) for row in rows], [[30.0]])
        self.assertEqual([list(row) for row in reference], [[30.0]])
        validation = SemanticSQLValidator.for_context(context).validate(spec.sql)
        self.assertEqual(validation.status, "passed")

    def test_compiler_returns_none_without_metric_matches(self) -> None:
        context = Context(
            task=SqlTask(question="list rows", database_path=str(self.database))
        )
        self.assertIsNone(QuerySpecCompiler.for_context(context))


class SelectorHardeningTest(FixtureMixin, unittest.TestCase):
    def test_correct_empty_candidate_beats_wrong_non_empty_candidate(self) -> None:
        context = self.context(requested=["item.category"])
        correct_empty = (
            "SELECT i.category, SUM(s.amount) AS net_sales FROM fact_sales s "
            "JOIN dim_item i ON s.item_id = i.item_id "
            "WHERE s.is_valid = 1 AND s.sale_date_key >= 99990101 GROUP BY i.category"
        )
        wrong_non_empty = (
            "SELECT i.category, SUM(s.amount) AS net_sales FROM fact_sales s "
            "JOIN dim_item i ON s.item_id = i.item_id "
            "WHERE s.is_valid = 0 GROUP BY i.category"
        )
        self.assertGreater(self.rows(wrong_non_empty)[0][1], 0)
        selection = SQLSelector(self.tool(), max_preview=2, preview_limit=20).select(
            [{"sql": correct_empty}, {"sql": wrong_non_empty}], context
        )
        self.assertEqual(selection["selected_index"], 0)
        self.assertEqual(selection["evaluations"][0]["status"], "eligible")
        self.assertEqual(selection["evaluations"][0]["row_count"], 0)
        self.assertEqual(
            selection["evaluations"][0]["empty_result_policy"],
            "neutral_semantically_valid_empty",
        )
        self.assertEqual(
            selection["evaluations"][0]["score_components"]["non_empty"], 0.5
        )
        rejected = selection["evaluations"][1]
        self.assertEqual(rejected["status"], "rejected")
        self.assertIn("default_filter", rejected["rejection_reason"])
        self.assertIsNone(rejected["row_count"])
        self.assertEqual(
            rejected["semantic_validation"]["status"], "violation"
        )

    def test_duplicate_candidates_are_previewed_once(self) -> None:
        context = self.context()
        selection = SQLSelector(self.tool(), max_preview=3).select(
            [
                {"sql": "SELECT SUM(s.amount) AS net_sales FROM fact_sales s "
                        "WHERE s.is_valid = 1"},
                {"sql": "select  sum(s.amount) as net_sales from fact_sales s "
                        "where s.is_valid = 1"},
            ],
            context,
        )
        self.assertEqual(selection["selected_index"], 0)
        duplicate = selection["evaluations"][1]
        self.assertEqual(duplicate["status"], "duplicate")
        self.assertEqual(duplicate["duplicate_of"], 0)
        self.assertIsNone(duplicate.get("row_count"))
        self.assertTrue(selection["evaluations"][0]["execution_success"])

    def test_semantic_validation_is_recorded_on_passing_candidate(self) -> None:
        context = self.context()
        selection = SQLSelector(self.tool(), max_preview=1).select(
            [
                {
                    "sql": "SELECT SUM(s.amount) AS net_sales FROM fact_sales s "
                    "WHERE s.is_valid = 1"
                }
            ],
            context,
        )
        evaluation = selection["evaluations"][0]
        self.assertEqual(evaluation["status"], "eligible")
        self.assertEqual(evaluation["semantic_validation"]["status"], "passed")


class FakeCandidateLLM:
    """Deterministic provider returning the same (wrong) candidate twice."""

    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.prompts: list[str] = []

    def generate_json(self, prompt: str) -> dict:
        self.prompts.append(prompt)
        return self.payload


class ParallelCandidatesQuerySpecTest(FixtureMixin, unittest.TestCase):
    def test_query_spec_candidate_is_appended_and_wins(self) -> None:
        from queryforge.workflow.node.parallel_candidates_node import (
            ParallelCandidatesNode,
        )

        context = self.context()
        llm = FakeCandidateLLM(
            {
                "sql": "SELECT SUM(s.amount) AS net_sales FROM fact_sales s "
                "WHERE s.is_valid = 0",
                "explanation": "Wrong filter value.",
                "tables_used": ["fact_sales"],
            }
        )
        result = ParallelCandidatesNode(llm, self.tool(), candidate_count=2).execute(
            context
        )
        self.assertTrue(result.success, result.error)
        selection = context.candidate_selection
        self.assertTrue(selection["query_spec_candidate"])
        self.assertEqual(len(selection["candidates"]), 3)
        self.assertEqual(selection["candidates"][-1]["generated_by"], "query_spec")
        self.assertEqual(selection["candidates"][-1]["candidate_index"], 2)
        self.assertEqual(selection["selected_index"], 2)
        self.assertIn("fact_sales.is_valid = 1", context.sql_context.sql)
        rejected = selection["evaluations"][0]
        self.assertEqual(rejected["status"], "rejected")
        self.assertIn("default_filter", rejected["rejection_reason"])
        self.assertIn("duplicate", selection["evaluations"][1]["status"])

    def test_no_query_spec_candidate_without_metrics(self) -> None:
        from queryforge.workflow.node.parallel_candidates_node import (
            ParallelCandidatesNode,
        )

        context = Context(
            task=SqlTask(question="List rows", database_path=str(self.database))
        )
        llm = FakeCandidateLLM(
            {
                "sql": "SELECT item_id FROM dim_item",
                "explanation": "List rows.",
                "tables_used": ["dim_item"],
            }
        )
        result = ParallelCandidatesNode(llm, self.tool(), candidate_count=2).execute(
            context
        )
        self.assertTrue(result.success, result.error)
        self.assertFalse(context.candidate_selection["query_spec_candidate"])
        self.assertEqual(len(context.candidate_selection["candidates"]), 2)
        self.assertTrue(
            all(
                candidate.get("generated_by") is None
                for candidate in context.candidate_selection["candidates"]
            )
        )


class ExecuteSqlNodeIntegrationTest(FixtureMixin, unittest.TestCase):
    def test_violation_fails_execution_before_running_sql(self) -> None:
        context = self.context()
        context.sql_context = SQLContext(
            sql="SELECT SUM(s.amount) AS net_sales FROM fact_sales s WHERE s.is_valid = 0",
            explanation="Wrong filter value.",
            tables_used=["fact_sales"],
        )
        result = ExecuteSqlNode(self.tool()).execute(context)
        self.assertFalse(result.success)
        self.assertIn("default_filter", result.error or "")
        self.assertIsNone(context.execution_result)
        self.assertEqual(
            context.task_context["semantic_validation"]["status"], "violation"
        )
        self.assertIn("semantic", context.task_context["error_categories"])

    def test_correct_sql_executes_and_records_passed_validation(self) -> None:
        context = self.context()
        context.sql_context = SQLContext(
            sql="SELECT SUM(s.amount) AS net_sales FROM fact_sales s WHERE s.is_valid = 1",
            explanation="Governed metric.",
            tables_used=["fact_sales"],
        )
        result = ExecuteSqlNode(self.tool()).execute(context)
        self.assertTrue(result.success, result.error)
        self.assertEqual(context.execution_result.rows, [[60.0]])
        self.assertEqual(
            context.task_context["semantic_validation"]["status"], "passed"
        )

    def test_unsupported_shape_records_evidence_and_still_executes(self) -> None:
        context = self.context()
        context.sql_context = SQLContext(
            sql="SELECT 1 AS net_sales UNION SELECT 2",
            explanation="Unsupported shape.",
            tables_used=[],
        )
        result = ExecuteSqlNode(self.tool()).execute(context)
        self.assertTrue(result.success, result.error)
        validation = context.task_context["semantic_validation"]
        self.assertEqual(validation["status"], "unsupported")
        self.assertTrue(validation["unsupported_reason"])


class ErrorTaxonomyTest(FixtureMixin, unittest.TestCase):
    def test_categorize_error_mapping_table(self) -> None:
        cases = [
            (UnsafeSQLError("ast_parse", SqlPolicyDecision(
                allowed=False, run_id="run", policy_name="p", rule="ast_parse",
                reason="unparsable")), WorkflowErrorCategory.syntax),
            (UnsafeSQLError("table_scope", SqlPolicyDecision(
                allowed=False, run_id="run", policy_name="p", rule="table_scope",
                reason="out of scope")), WorkflowErrorCategory.identifier),
            (UnsafeSQLError("column_scope", SqlPolicyDecision(
                allowed=False, run_id="run", policy_name="p", rule="column_scope",
                reason="out of scope")), WorkflowErrorCategory.identifier),
            (UnsafeSQLError("ambiguous", SqlPolicyDecision(
                allowed=False, run_id="run", policy_name="p",
                rule="ambiguous_column_scope", reason="ambiguous")),
             WorkflowErrorCategory.identifier),
            (UnsafeSQLError("dangerous", SqlPolicyDecision(
                allowed=False, run_id="run", policy_name="p",
                rule="dangerous_function", reason="load_extension")),
             WorkflowErrorCategory.permission),
            (UnsafeSQLError("read only", SqlPolicyDecision(
                allowed=False, run_id="run", policy_name="p",
                rule="read_only_ast", reason="DDL")), WorkflowErrorCategory.permission),
            (UnsafeSQLError("recursive", SqlPolicyDecision(
                allowed=False, run_id="run", policy_name="p",
                rule="recursive_cte", reason="recursive")),
             WorkflowErrorCategory.permission),
            (UnsafeSQLError("limit", SqlPolicyDecision(
                allowed=False, run_id="run", policy_name="p",
                rule="max_limit", reason="too large")), WorkflowErrorCategory.budget),
            ("SQLite query failed: no such table: ghost",
             WorkflowErrorCategory.identifier),
            ("SQLite query failed: no such column: ghost",
             WorkflowErrorCategory.identifier),
            ("SQLite query failed: syntax error near FROM",
             WorkflowErrorCategory.syntax),
            ("Semantic SQL validation failed (default_filter): metric 'net_sales'",
             WorkflowErrorCategory.semantic),
            ("Maximum SQL retries (2) exhausted after execution failure",
             WorkflowErrorCategory.budget),
            ("Repeated SQL cycle detected: attempt 3 reuses the normalized SQL",
             WorkflowErrorCategory.budget),
            ("shape is outside supported coverage", WorkflowErrorCategory.unsupported),
            ("quality rule null_rate failed for dim_user.country",
             WorkflowErrorCategory.data_quality),
            ("Something entirely unexpected", WorkflowErrorCategory.unknown),
        ]
        for error, expected in cases:
            with self.subTest(error=str(error)):
                self.assertEqual(categorize_error(error), expected)
        self.assertEqual(categorize_error(None), WorkflowErrorCategory.unknown)

    def test_record_error_category_appends_to_task_context(self) -> None:
        context = self.context()
        category = record_error_category(context, "SQLite query failed: no such column: x")
        self.assertEqual(category, WorkflowErrorCategory.identifier)
        self.assertEqual(context.task_context["error_categories"], ["identifier"])

    def test_typed_workflow_error_keeps_workflow_error_semantics(self) -> None:
        from queryforge.workflow.errors import TypedWorkflowError

        context = self.context()
        error = TypedWorkflowError(
            "fix", "budget exhausted", context, WorkflowErrorCategory.budget
        )
        self.assertIsInstance(error, WorkflowError)
        self.assertEqual(error.category, WorkflowErrorCategory.budget)
        self.assertEqual(error.node_name, "fix")

    def test_guidance_distinguishes_permission_and_budget(self) -> None:
        permission = guidance_for(WorkflowErrorCategory.permission)
        budget = guidance_for(WorkflowErrorCategory.budget)
        self.assertIn("wider access", permission)
        self.assertIn("Do not", budget)
        self.assertNotEqual(permission, budget)


class FakeFixLLM:
    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.prompts: list[str] = []

    def generate_json(self, prompt: str) -> dict:
        self.prompts.append(prompt)
        return self.payload


class FixLoopHardeningTest(FixtureMixin, unittest.TestCase):
    def fix_context(self, *, error: str, history: list[str]) -> Context:
        context = self.context()
        context.sql_context = SQLContext(
            sql="SELECT SUM(fact_sales.amount) FROM fact_sales",
            explanation="Original attempt.",
            tables_used=["fact_sales"],
        )
        context.sql_attempt_history = [
            SqlAttempt(
                attempt_number=index + 1,
                sql=sql,
                status="failed",
                error="no such column",
            )
            for index, sql in enumerate(history)
        ]
        context.last_execution_error = error
        return context

    def test_fix_rejects_sql_already_attempted_after_normalization(self) -> None:
        llm = FakeFixLLM(
            {
                "fixed_sql": "select   sum(amount)  from fact_sales",
                "explanation": "Same query with different spacing.",
                "tables_used": ["fact_sales"],
            }
        )
        context = self.fix_context(
            error="no such column: missing",
            history=["SELECT SUM(amount) FROM fact_sales"],
        )
        result = FixNode(llm, SkillManager()).execute(context)
        self.assertFalse(result.success)
        self.assertIn("repeated a previous SQL attempt", result.error or "")
        self.assertEqual(
            context.task_context["error_categories"], ["identifier", "budget"]
        )

    def test_fix_accepts_materially_different_sql(self) -> None:
        llm = FakeFixLLM(
            {
                "fixed_sql": "SELECT SUM(amount) AS net_sales FROM fact_sales "
                "WHERE is_valid = 1",
                "explanation": "Restore the governed filter.",
                "tables_used": ["fact_sales"],
            }
        )
        context = self.fix_context(
            error="no such column: missing",
            history=["SELECT SUM(amount) FROM fact_sales"],
        )
        result = FixNode(llm, SkillManager()).execute(context)
        self.assertTrue(result.success, result.error)
        self.assertEqual(len(context.fix_attempts), 1)

    def test_typed_semantic_category_reaches_repair_prompt(self) -> None:
        llm = FakeFixLLM(
            {
                "fixed_sql": "SELECT SUM(amount) AS net_sales FROM fact_sales "
                "WHERE is_valid = 1",
                "explanation": "Restore the exact default filter value.",
                "tables_used": ["fact_sales"],
            }
        )
        context = self.fix_context(
            error=(
                "Semantic SQL validation failed (default_filter): metric 'net_sales' "
                "default filter 'fact_sales.is_valid = 1' is violated"
            ),
            history=["SELECT SUM(amount) AS net_sales FROM fact_sales WHERE is_valid = 0"],
        )
        result = FixNode(llm, SkillManager()).execute(context)
        self.assertTrue(result.success, result.error)
        prompt = llm.prompts[0]
        self.assertIn("Typed error category", prompt)
        self.assertIn("semantic", prompt)
        self.assertIn("Restore the metric expression", prompt)
        self.assertEqual(context.task_context["error_categories"], ["semantic"])

    def test_permission_category_forbids_widening_access(self) -> None:
        llm = FakeFixLLM(
            {
                "fixed_sql": "SELECT SUM(amount) FROM fact_sales",
                "explanation": "Different query.",
                "tables_used": ["fact_sales"],
            }
        )
        context = self.fix_context(
            error=(
                "SQL_SECURITY_ERROR run_id=run rule=read_only_ast: only read-only "
                "SELECT statements are allowed"
            ),
            history=["SELECT SUM(amount) FROM fact_sales LIMIT 1"],
        )
        result = FixNode(llm, SkillManager()).execute(context)
        self.assertTrue(result.success, result.error)
        self.assertIn("permission", llm.prompts[0])
        self.assertIn("Do not ask for wider access", llm.prompts[0])


class _StubNode(Node):
    def __init__(self, name: str, action) -> None:
        self.name = name
        self.action = action

    def execute(self, context: Context) -> NodeResult:
        return self.action(context)


class RepeatedSqlCycleWorkflowTest(FixtureMixin, unittest.TestCase):
    def test_a_to_b_to_a_repair_cycle_stops_with_typed_budget_error(self) -> None:
        context = self.context()
        first = "SELECT missing_a FROM fact_sales"
        second = "SELECT missing_b FROM fact_sales"
        context.sql_context = SQLContext(
            sql=first, explanation="First attempt.", tables_used=["fact_sales"]
        )
        calls = {"count": 0}

        def fix_action(state: Context) -> NodeResult:
            calls["count"] += 1
            state.sql_context = SQLContext(
                sql=second if calls["count"] == 1 else first,
                explanation="Alternating repair.",
                tables_used=["fact_sales"],
            )
            state.last_execution_error = None
            return NodeResult(
                node_name="fix", success=True, status="success", message="fixed"
            )

        def noop(state: Context) -> NodeResult:
            return NodeResult(
                node_name="stub", success=True, status="success", message="stub"
            )

        workflow = ReflectiveWorkflow(
            context,
            setup_nodes=[],
            gen_sql_node=_StubNode("gen_sql", noop),
            execute_sql_node=ExecuteSqlNode(self.tool()),
            reflect_node=_StubNode("reflect", noop),
            fix_node=_StubNode("fix", fix_action),
            output_node=_StubNode("output", noop),
            max_retries=5,
        )
        with self.assertRaises(WorkflowError) as captured:
            workflow.run()
        error = captured.exception
        self.assertEqual(error.node_name, "retry_limit")
        self.assertIn("Repeated SQL cycle", str(error))
        self.assertEqual(calls["count"], 2)
        self.assertEqual(
            error.context.task_context["attempt_signatures"],
            [normalize_sql_signature(first), normalize_sql_signature(second)],
        )
        self.assertIn("budget", error.context.task_context["error_categories"])


if __name__ == "__main__":
    unittest.main()
