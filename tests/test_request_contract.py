"""Production request gates, including counterexamples against real SQLite."""
import unittest
import importlib.util
from contextlib import redirect_stdout, redirect_stderr
from io import StringIO
from unittest.mock import patch

from queryforge.application import AgentService
from queryforge.core.config import Config
from scripts.benchmark_runners import ScriptedModel

from queryforge.core.schemas.models import SQLContext
from queryforge.domain.semantic import SemanticModelLoader, SemanticSQLValidator
from queryforge.domain.semantic.request_contract import output_contract, validate_output_contract
from queryforge.workflow.node.execute_sql_node import ExecuteSqlNode
from queryforge.workflow.sql_selector import SQLSelector
from tests.test_semantic_sql_validator import FixtureMixin


class RequestContractTest(FixtureMixin, unittest.TestCase):
    def test_explicit_labels_and_chinese_followup(self):
        for question, columns in (
            ("Count receipts. Return orders and lines, in that order.", ("orders", "lines")),
            ("Return a single column named n.", ("n",)),
            ("Return only breach_id.", ("breach_id",)),
            ("Return region and amount, sorted by region.", ("region", "amount")),
            ("仍只返回order_count列。", ("order_count",)),
            ("Return net revenue by month", ()),
        ):
            with self.subTest(question=question):
                self.assertEqual(output_contract(question), columns)

    def test_wrong_column_order_and_star_are_rejected(self):
        question = "Count receipts. Return orders and lines, in that order."
        for sql in ("SELECT 1 AS lines, 2 AS orders", "SELECT * FROM fact_sales", "SELECT 1 AS orders"):
            with self.subTest(sql=sql):
                self.assertIsNotNone(validate_output_contract(question, sql))
        self.assertIsNone(validate_output_contract(question, "SELECT 1 AS orders, 2 AS lines"))

    def test_output_label_cannot_create_metric_obligation(self):
        semantic = self.load_model("Count rows. Return net_sales.")
        self.assertEqual(SemanticModelLoader.match_metrics(semantic.model, "Count rows. Return net_sales."), [])

    def test_unused_cte_aggregate_cannot_satisfy_metric(self):
        sql = "WITH unused AS (SELECT SUM(amount) AS n FROM fact_sales WHERE is_valid=1) SELECT COUNT(*) FROM fact_sales"
        self.assertEqual(self.validate(sql).status, "violation")

    def test_active_cte_unselected_aggregate_cannot_satisfy_metric(self):
        sql = "WITH a AS (SELECT SUM(amount) AS value, COUNT(*) AS n FROM fact_sales WHERE is_valid=1) SELECT n FROM a"
        self.assertEqual(self.validate(sql).status, "violation")

    def test_selected_derived_and_scalar_aggregates_remain_valid(self):
        for sql in (
            "WITH a AS (SELECT SUM(amount) AS value FROM fact_sales WHERE is_valid=1) SELECT value FROM a",
            "SELECT (SELECT SUM(amount) FROM fact_sales WHERE is_valid=1) AS value",
        ):
            with self.subTest(sql=sql):
                self.assertEqual(self.validate(sql).status, "passed")
                self.assertEqual(self.rows(sql), [[60.0]])

    def test_distinct_wrong_column_is_rejected(self):
        result = self.validate("SELECT COUNT(DISTINCT item_id) FROM fact_sales WHERE is_valid=1", question="valid orders")
        self.assertEqual(result.status, "violation")

    def test_count_star_requires_physical_single_nonnull_primary_key(self):
        context = self.context("valid orders")
        context.relevant_tables = self.schemas
        sql = "SELECT COUNT(*) FROM fact_sales WHERE is_valid=1"
        self.assertEqual(SemanticSQLValidator.for_context(context).validate(sql).status, "passed")
        self.assertEqual(self.rows(sql), [[3]])
        context.relevant_tables = []
        self.assertEqual(SemanticSQLValidator.for_context(context).validate(sql).status, "violation")

    def test_count_star_self_join_cannot_use_uniqueness_proof(self):
        context = self.context("valid orders")
        context.relevant_tables = self.schemas
        sql = "SELECT COUNT(*) FROM fact_sales a JOIN fact_sales b ON a.item_id=b.item_id WHERE a.is_valid=1"
        self.assertEqual(SemanticSQLValidator.for_context(context).validate(sql).status, "violation")

    def test_projection_violation_prevents_execution_and_candidate_preview(self):
        context = self.context("What are net sales? Return only amount.")
        sql = "SELECT SUM(amount) AS wrong FROM fact_sales WHERE is_valid=1"
        context.sql_context = SQLContext(sql=sql, explanation="fixture")
        tool = self.tool()
        self.assertFalse(ExecuteSqlNode(tool).execute(context).success)
        self.assertIsNone(context.execution_result)
        selection = SQLSelector(tool).select([{"sql": sql}], context)
        self.assertIsNone(selection["selected_sql"])
        self.assertFalse(selection["evaluations"][0]["execution_success"])

    def test_requested_null_predicate_cannot_be_omitted_or_weakened(self):
        context = self.context("How many valid orders have NULL item_id? Return only n.")
        context.relevant_tables = self.schemas
        validator = SemanticSQLValidator.for_context(context)
        prefix = "SELECT COUNT(DISTINCT sale_id) AS n FROM fact_sales WHERE is_valid=1"
        correct = prefix + " AND item_id IS NULL"
        self.assertEqual(validator.validate(correct).status, "passed")
        self.assertEqual(self.rows(correct), [[0]])
        for wrong in (prefix, prefix + " AND item_id IS NOT NULL", prefix + " AND (item_id IS NULL OR 1=1)"):
            with self.subTest(sql=wrong):
                self.assertEqual(validator.validate(wrong).status, "violation")

    def test_detail_identifiers_cannot_be_an_aggregate_alias(self):
        question = "List sale identifiers. Return only sale_id."
        self.assertIsNone(validate_output_contract(question, "SELECT sale_id FROM fact_sales"))
        for sql in ("SELECT COUNT(*) AS sale_id FROM fact_sales",
                    "WITH x AS (SELECT MAX(sale_id) AS sale_id FROM fact_sales) SELECT sale_id FROM x",
                    "WITH x AS (SELECT 1 AS sale_id) SELECT sale_id FROM x",
                    "WITH x AS (SELECT sale_id + 1 AS sale_id FROM fact_sales) SELECT sale_id FROM x",
                    "SELECT 1 AS sale_id"):
            with self.subTest(sql=sql):
                self.assertIsNotNone(validate_output_contract(question, sql))

    def test_longer_metric_phrase_does_not_hide_separately_requested_metric(self):
        model = self.load_model("valid orders").model.model_copy(deep=True)
        model.metrics[0].synonyms = ["sales"]
        model.metrics[1].synonyms = ["valid sales"]
        matches = SemanticModelLoader.match_metrics(model, "Show valid sales and sales.")
        self.assertEqual({item.metric.name for item in matches}, {"net_sales", "valid_order_count"})

    def test_unused_grouped_cte_cannot_satisfy_requested_grain(self):
        context = self.context("net sales by product category", requested=["item.category"])
        sql = "WITH unused AS (SELECT category FROM dim_item GROUP BY category) SELECT SUM(amount) FROM fact_sales WHERE is_valid=1"
        self.assertEqual(SemanticSQLValidator.for_context(context).validate(sql).status, "violation")

    def service(self, sql):
        config = Config("scripted", None, "fixture", None, str(self.database),
                        semantic_model_path=str(self.model_path),
                        history_db_path=str(self.root / "history.db"),
                        orchestration_state_root=str(self.root / "runs"),
                        allowed_database_paths=(str(self.root),))
        return AgentService(config_loader=lambda **_: config,
                            llm_factory=lambda _: ScriptedModel(sql))

    @unittest.skipUnless(importlib.util.find_spec("fastapi"), "FastAPI not installed")
    def test_rest_projection_gate_rejects_before_sql_execution(self):
        from fastapi.testclient import TestClient
        from queryforge.interfaces.api.app import create_app
        service = self.service("SELECT SUM(amount) AS wrong FROM fact_sales WHERE is_valid=1")
        with patch("queryforge.infrastructure.tools.database_tool.DatabaseTool.execute_sql", side_effect=AssertionError("Output violation reached SQL boundary")) as execute:
            response = TestClient(create_app(service)).post("/ask", json={
                "question": "What are net sales? Return only amount.", "complexity_mode": "simple"})
        self.assertEqual(response.status_code, 422)
        self.assertIn("Output contract", response.json()["detail"])
        execute.assert_not_called()

    def test_cli_projection_gate_rejects_before_sql_execution(self):
        import main as cli
        service = self.service("SELECT SUM(amount) AS wrong FROM fact_sales WHERE is_valid=1")
        error = StringIO()
        with patch.object(cli, "AgentService", return_value=service), patch.object(cli, "load_config", service.config_loader), \
             patch("sys.argv", ["queryforge", "--question", "What are net sales? Return only amount.",
                                 "--database", str(self.database), "--semantic-model", str(self.model_path), "--complexity-mode", "simple"]), \
             patch("queryforge.infrastructure.tools.database_tool.DatabaseTool.execute_sql", side_effect=AssertionError("Output violation reached SQL boundary")) as execute, \
             redirect_stdout(StringIO()), redirect_stderr(error):
            code = cli.main()
        self.assertEqual(code, 1)
        self.assertIn("Output contract", error.getvalue())
        execute.assert_not_called()
