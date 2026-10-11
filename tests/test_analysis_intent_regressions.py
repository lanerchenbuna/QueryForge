"""Review regressions: verify complete answers against independent fixture data."""

import contextlib
import importlib.util
import io
import json
import sqlite3
import unittest
from unittest.mock import patch

import yaml

from tests.test_analysis_planner import Fixture
from queryforge.domain.semantic.sql_validator import QuerySpecCompiler


class AnalysisIntentRegressionTest(unittest.TestCase):
    def setUp(self):
        self.fixture = Fixture()

    def tearDown(self):
        self.fixture.cleanup()

    def time_fixture(self, periods=("2024-02-01", "2025-01-01", "2025-02-01")):
        with sqlite3.connect(self.fixture.database) as db:
            db.execute("ALTER TABLE items ADD COLUMN month TEXT")
            db.executemany(
                "UPDATE items SET month = ? WHERE item_id = ?",
                [(period, index) for index, period in enumerate(periods, 1)],
            )
        model = yaml.safe_load(self.fixture.semantic_model.read_text())
        model["entities"][0]["dimensions"].append(
            {"name": "month", "column": "month", "description": "Event month"}
        )
        model["metrics"][1]["time_field"] = "items.month"
        model["metrics"][1]["allowed_dimensions"].append("items.month")
        self.fixture.semantic_model.write_text(yaml.safe_dump(model))

    @staticmethod
    def metric_result(payload):
        return next(s["outputs"] for s in payload["steps"] if s["action"] == "query_metric")

    def test_all_requested_metrics_are_returned_with_evidence(self):
        payload = self.fixture.analyze("Show item count and total amount")
        self.assertEqual(payload["status"], "succeeded")
        self.assertEqual(set(payload["answer"]["metrics"]), {"item_count", "item_amount"})
        self.assertEqual(payload["answer"]["values"], {"item_count": 3, "item_amount": 60.0})
        self.assertTrue(payload["answer"]["evidence_ids"])
        composed = next(s["outputs"] for s in payload["steps"] if s["action"] == "compose_answer")
        self.assertEqual(composed["validation_problems"], [])

    def test_multiple_metrics_by_category_match_fixture_rows(self):
        payload = self.fixture.analyze("Show item count and total amount by category")
        self.assertEqual(payload["status"], "succeeded")
        result = self.metric_result(payload)
        names = result["columns"]
        actual = {row[0]: (row[names.index("item_count")], row[names.index("item_amount")])
                  for row in result["rows"]}
        self.assertEqual(actual, {"a": (2, 40.0), "b": (1, 20.0)})

    def test_different_metric_scopes_are_aggregated_independently(self):
        model = yaml.safe_load(self.fixture.semantic_model.read_text())
        model["metrics"][1]["default_filters"] = ["items.amount > 10"]
        self.fixture.semantic_model.write_text(yaml.safe_dump(model))
        payload = self.fixture.analyze("Show item count and total amount")
        self.assertEqual(payload["status"], "succeeded")
        self.assertEqual(payload["answer"]["values"], {"item_count": 3, "item_amount": 50.0})
        self.assertEqual(self.metric_result(payload)["metric_contracts"]["item_amount"]["population_filters"], ["items.amount > 10"])

    def test_explicit_filter_returns_requested_slice_not_full_total(self):
        payload = self.fixture.analyze("Show total amount where item_id = 1")
        self.assertEqual(payload["status"], "succeeded")
        self.assertEqual(payload["answer"]["value"], 10.0)
        self.assertTrue(payload["analysis_request"]["filters"])
        self.assertIn("items.item_id = 1", self.metric_result(payload)["sql"])

    def test_filter_columns_are_not_inferred_as_grouping_dimensions(self):
        for clause in ("where category = 'a' AND amount >= 20", "for category = 'a' AND amount >= 20"):
            with self.subTest(clause=clause):
                payload = self.fixture.analyze("Show total amount " + clause)
                self.assertEqual(payload["status"], "succeeded")
                self.assertEqual(payload["answer"]["value"], 30.0)
                self.assertEqual(payload["analysis_request"]["dimensions"], [])

    def test_filter_and_grouping_are_both_preserved(self):
        payload = self.fixture.analyze("Show total amount where amount >= 20 by category")
        self.assertEqual(payload["status"], "succeeded")
        self.assertEqual(self.metric_result(payload)["rows"], [["a", 30.0], ["b", 20.0]])

    def test_literal_filter_operators_match_independent_fixture_values(self):
        cases = (("item_id IN (1, 3)", 40.0), ("amount BETWEEN 10 AND 20", 30.0),
                 ("(category = 'a' OR category = 'b') AND amount > 10", 50.0),
                 ("NOT (category = 'b')", 40.0), ("amount >= -1", 60.0))
        for clause, expected in cases:
            with self.subTest(clause=clause):
                payload = self.fixture.analyze("Show total amount where " + clause)
                self.assertEqual(payload["status"], "succeeded")
                self.assertEqual(payload["answer"]["value"], expected)

    def test_quoted_filter_values_do_not_change_time_or_grouping_intent(self):
        value = "by supplier 2024 monthly yoy"
        with sqlite3.connect(self.fixture.database) as db:
            db.execute("UPDATE items SET name = ? WHERE item_id = 1", (value,))
        payload = self.fixture.analyze(f"Show total amount where name = '{value}'")
        self.assertEqual(payload["status"], "succeeded")
        self.assertEqual(payload["answer"]["value"], 10.0)
        request = payload["analysis_request"]
        self.assertEqual(request["dimensions"], [])
        self.assertIsNone(request["time_range"])
        self.assertIsNone(request["time_grain"])
        self.assertIsNone(request["comparison_baseline"])

    def test_null_filter_and_empty_multi_metric_scope_are_explicit(self):
        with sqlite3.connect(self.fixture.database) as db:
            db.execute("UPDATE items SET category = NULL WHERE item_id = 1")
        payload = self.fixture.analyze("Show total amount where category IS NULL")
        self.assertEqual(payload["status"], "succeeded")
        self.assertEqual(payload["answer"]["value"], 10.0)
        empty = self.fixture.analyze("Show item count and total amount where item_id = 999", max_replans=0)
        self.assertEqual(empty["status"], "partial")
        self.assertIsNone(empty["answer"])

    def test_unknown_or_unsupported_filters_do_not_execute_sql(self):
        clauses = ("item_id = 1; DROP TABLE items", "supplier_code = 1",
                   "item_id IN (SELECT item_id FROM items)", "item_id = 1 -- ignore",
                   "item_id = ABS(-1)", "category is popular")
        for clause in clauses:
            with self.subTest(clause=clause):
                payload = self.fixture.analyze("Show total amount where " + clause)
                self.assertEqual(payload["status"], "needs_clarification")
                self.assertEqual(payload["steps"], [])
                self.assertIsNone(payload["answer"])
        with sqlite3.connect(self.fixture.database) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM items").fetchone()[0], 3)

    def test_informal_filter_clauses_are_clarified_instead_of_ignored(self):
        for clause in ("for category a", "with supplier_code 'x'", "for popular items"):
            with self.subTest(clause=clause):
                payload = self.fixture.analyze("Show total amount " + clause)
                self.assertEqual(payload["status"], "needs_clarification")
                self.assertEqual(payload["steps"], [])

    def test_for_a_calendar_year_remains_a_time_window(self):
        self.time_fixture()
        payload = self.fixture.analyze("Show item amount for 2024")
        self.assertEqual(payload["status"], "succeeded")
        self.assertEqual(payload["answer"]["value"], 10.0)
        self.assertEqual(payload["analysis_request"]["filters"], [])

    def test_time_window_without_governed_time_cannot_return_full_total(self):
        payload = self.fixture.analyze("Show total amount for 2024")
        self.assertEqual(payload["status"], "needs_clarification")
        self.assertEqual(payload["steps"], [])

    def test_explicit_sql_cannot_bypass_the_requested_time_window(self):
        from queryforge.orchestration.planner import AnalysisPlan
        self.time_fixture()
        planned = self.fixture.analyze("Show item amount")
        plan = AnalysisPlan.model_validate(planned["plan"])
        next(s for s in plan.steps if s.action == "query_metric").inputs["sql"] = (
            "SELECT SUM(items.amount) AS item_amount FROM items LIMIT 100"
        )
        payload = self.fixture.analyze("Show item amount for 2024", plan=plan)
        self.assertNotEqual(payload["status"], "succeeded")
        self.assertIsNone(payload["answer"])

    def test_yoy_uses_same_month_last_year_not_previous_month(self):
        self.time_fixture()
        payload = self.fixture.analyze("Show item amount monthly trend year over year")
        self.assertEqual(payload["status"], "succeeded")
        comparison = next(s["outputs"] for s in payload["steps"] if s["action"] == "compare_periods")
        self.assertEqual(comparison["current"], 30.0)
        self.assertEqual(comparison["baseline"], 10.0)
        self.assertEqual(comparison["percent_change"], 200.0)
        self.assertEqual(comparison["comparison_baseline"], "same_period_last_year")
        self.assertEqual(payload["final_answer"]["status"], "success")

    def test_missing_yoy_baseline_never_falls_back_to_previous_month(self):
        self.time_fixture(("2025-01-01", "2025-02-01", "2025-03-01"))
        payload = self.fixture.analyze("Show item amount monthly trend year over year")
        self.assertNotEqual(payload["status"], "succeeded")
        self.assertTrue(any("baseline" in (s.get("error") or "") for s in payload["steps"]))

    def test_missing_previous_calendar_month_never_uses_last_observed_month(self):
        self.time_fixture(("2024-02-01", "2025-01-01", "2025-03-01"))
        payload = self.fixture.analyze("Show item amount monthly trend mom")
        self.assertNotEqual(payload["status"], "succeeded")

    def test_monthly_grain_aggregates_days_before_comparison(self):
        self.time_fixture(("2025-01-01", "2025-01-15", "2025-02-01"))
        payload = self.fixture.analyze("Show item amount monthly trend mom")
        self.assertEqual(payload["status"], "succeeded")
        comparison = next(s["outputs"] for s in payload["steps"] if s["action"] == "compare_periods")
        self.assertEqual((comparison["baseline"], comparison["current"]), (30.0, 30.0))

    def test_quarterly_comparison_crosses_the_year_boundary(self):
        self.time_fixture(("2024-07-01", "2024-10-01", "2025-01-01"))
        payload = self.fixture.analyze("Show item amount quarterly trend qoq")
        self.assertEqual(payload["status"], "succeeded")
        comparison = next(s["outputs"] for s in payload["steps"] if s["action"] == "compare_periods")
        self.assertEqual(comparison["label"], "2024-Q4 -> 2025-Q1")
        self.assertEqual((comparison["baseline"], comparison["current"]), (20.0, 30.0))

    def test_daily_comparison_crosses_the_year_boundary(self):
        self.time_fixture(("2024-12-30", "2024-12-31", "2025-01-01"))
        payload = self.fixture.analyze("Show item amount daily trend vs previous day")
        self.assertEqual(payload["status"], "succeeded")
        comparison = next(s["outputs"] for s in payload["steps"] if s["action"] == "compare_periods")
        self.assertEqual(comparison["label"], "2024-12-31 -> 2025-01-01")
        self.assertEqual((comparison["baseline"], comparison["current"]), (20.0, 30.0))

    def test_time_grain_without_governed_time_is_clarified(self):
        payload = self.fixture.analyze("Show total amount monthly")
        self.assertEqual(payload["status"], "needs_clarification")
        self.assertEqual(payload["steps"], [])

    def test_comparison_without_a_calendar_grain_is_clarified(self):
        self.time_fixture()
        payload = self.fixture.analyze("Show item amount trend 环比")
        self.assertEqual(payload["status"], "needs_clarification")
        self.assertEqual(payload["steps"], [])

    def test_mom_abbreviation_has_an_explicit_monthly_grain(self):
        self.time_fixture(("2025-01-01", "2025-01-15", "2025-02-01"))
        payload = self.fixture.analyze("Show item amount mom")
        self.assertEqual(payload["status"], "succeeded")
        self.assertEqual(payload["analysis_request"]["time_grain"], "monthly")
        comparison = next(s["outputs"] for s in payload["steps"] if s["action"] == "compare_periods")
        self.assertEqual((comparison["baseline"], comparison["current"]), (30.0, 30.0))

    def test_bounded_time_series_cannot_claim_to_compare_the_latest_period(self):
        self.time_fixture()
        with sqlite3.connect(self.fixture.database) as db:
            db.executemany(
                "INSERT INTO items (item_id, name, category, amount, brand_id, month) VALUES (?, 'item', 'a', 1, 1, ?)",
                [(100 + i, f"{2000 + i // 12:04d}-{i % 12 + 1:02d}-01") for i in range(101)],
            )
        payload = self.fixture.analyze("Show item amount monthly trend year over year")
        self.assertNotEqual(payload["status"], "succeeded")
        self.assertTrue(any("bounded series" in (s.get("error") or "") for s in payload["steps"]))

    def test_result_missing_requested_metric_is_never_success(self):
        original = QuerySpecCompiler.compile
        def omit_metric(**kwargs):
            spec = original(**kwargs)
            return spec.model_copy(update={"sql": "SELECT SUM(items.amount) AS item_amount FROM items LIMIT 100"})
        with patch.object(QuerySpecCompiler, "compile", side_effect=omit_metric):
            payload = self.fixture.analyze("Show item count and total amount")
        self.assertNotEqual(payload["status"], "succeeded")

    def test_dropped_or_optional_filter_in_compiled_sql_is_refused(self):
        original = QuerySpecCompiler.compile
        for where in ("", " WHERE items.item_id = 1 OR items.amount > 0"):
            with self.subTest(where=where):
                def omit_filter(**kwargs):
                    spec = original(**kwargs)
                    return spec.model_copy(update={"sql": f"SELECT SUM(items.amount) AS item_amount FROM items{where} LIMIT 100"})
                with patch.object(QuerySpecCompiler, "compile", side_effect=omit_filter):
                    payload = self.fixture.analyze("Show total amount where item_id = 1")
                self.assertNotEqual(payload["status"], "succeeded")
                self.assertTrue(any("mandatory filter" in (s.get("error") or "") for s in payload["steps"]))

    def test_caller_plan_omitting_requested_filters_cannot_complete(self):
        from queryforge.orchestration.planner import AnalysisPlan
        planned = self.fixture.analyze("Show total amount")
        plan = AnalysisPlan.model_validate(planned["plan"])
        payload = self.fixture.analyze("Show total amount where item_id = 1", plan=plan)
        self.assertEqual(payload["status"], "blocked")
        self.assertIsNone(payload["answer"])
        self.assertTrue(any("request_incomplete: filters" in (s.get("error") or "") for s in payload["steps"]))

    def test_multi_metric_filtered_run_resumes_without_querying_or_spending_again(self):
        question = "Show item count and total amount where item_id = 1"
        first = self.fixture.analyze(question, run_id="intent-resume")
        self.assertEqual(first["status"], "succeeded")
        journal = self.fixture.root / ".queryforge/runs/intent-resume/execution.json"
        state = json.loads(journal.read_text())
        state.update(status="running", terminal_outcome=None, terminal_at=None)
        journal.write_text(json.dumps(state))
        resumed = self.fixture.analyze(question, run_id="intent-resume", resume=True)
        self.assertEqual(resumed["status"], "succeeded")
        self.assertEqual(resumed["answer"]["values"], {"item_count": 1, "item_amount": 10.0})
        self.assertEqual(set(resumed["reused_steps"]), {"resolve_metric", "check_data_quality", "query_metric", "compose_answer"})
        self.assertEqual(resumed["recomputed_steps"], [])
        self.assertEqual(resumed["budgets"]["usage"], first["budgets"]["usage"])

    def test_cli_filtered_analysis_returns_the_same_number(self):
        from queryforge.cli import main
        with patch("queryforge.application.analysis_planner.AnalysisPlannerService", return_value=self.fixture.service()):
            output = io.StringIO()
            with patch("sys.argv", ["queryforge", "--analyze", "--question", "Show total amount where item_id = 1",
                                    "--database", str(self.fixture.database),
                                    "--semantic-model", str(self.fixture.semantic_model)]):
                with contextlib.redirect_stdout(output):
                    code = main()
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue())["answer"]["value"], 10.0)

    @unittest.skipUnless(importlib.util.find_spec("fastapi") and importlib.util.find_spec("httpx"), "API extras absent")
    def test_rest_filtered_multi_metric_analysis_and_clarification(self):
        from fastapi.testclient import TestClient
        from queryforge.application import AgentService
        from queryforge.interfaces.api.app import create_app
        config = self.fixture.config()
        client = TestClient(create_app(AgentService(config_loader=lambda **_: config)))
        response = client.post("/analyze", json={"question": "Show item count and total amount where item_id = 1"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["answer"]["values"], {"item_count": 1, "item_amount": 10.0})
        rejected = client.post("/analyze", json={"question": "Show total amount where unknown_column = 1"})
        self.assertEqual(rejected.status_code, 400)
        self.assertEqual(rejected.json()["detail"]["status"], "needs_clarification")


if __name__ == "__main__":
    unittest.main()
