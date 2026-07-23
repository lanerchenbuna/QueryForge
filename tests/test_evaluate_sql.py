import importlib.util
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
MODULE_SPEC = importlib.util.spec_from_file_location(
    "evaluate_sql", PROJECT_ROOT / "scripts" / "evaluate_sql.py"
)
assert MODULE_SPEC and MODULE_SPEC.loader
evaluate_sql = importlib.util.module_from_spec(MODULE_SPEC)
MODULE_SPEC.loader.exec_module(evaluate_sql)


class FakeService:
    def ask(self, question, options):
        if question == "warmup":
            return {"status": "success", "rows": [[1]], "sql": "SELECT 1"}
        return {
            "status": "success",
            "rows": [[1]],
            "row_count": 1,
            "sql": "SELECT 1",
            "model_provider": "fake",
            "model": "fake-model",
            "candidate_selection": {
                "selected_index": 1,
                "candidates": [{"sql": "SELECT 2"}, {"sql": "SELECT 1"}],
            },
        }


class EvaluateSqlTest(unittest.TestCase):
    def test_checked_in_gold_set_has_three_domains_and_required_coverage(self):
        cases = evaluate_sql.load_cases(
            PROJECT_ROOT / "evaluation" / "gold" / "nl2sql_multidomain.jsonl"
        )
        self.assertEqual(len(cases), 120)
        self.assertEqual(
            {case["domain"] for case in cases},
            {"anime_content", "viewer_engagement", "platform_monetization"},
        )
        categories = {case["category"] for case in cases}
        self.assertTrue(
            {
                "single_table",
                "multi_table",
                "time",
                "metric",
                "follow_up",
                "policy_rejection",
            }.issubset(categories)
        )

    def test_metrics_include_semantics_policy_latency_cost_and_candidate_uplift(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "evaluation.sqlite"
            with sqlite3.connect(database) as connection:
                connection.execute("CREATE TABLE numbers (value INTEGER)")
                connection.execute("INSERT INTO numbers VALUES (1)")
            cases = [
                {
                    "id": "query_1",
                    "domain": "test",
                    "category": "metric",
                    "expected_outcome": "query",
                    "question": "return one",
                    "expected_sql": "SELECT 1",
                    "follow_up_context": ["warmup"],
                    "candidate_selection": True,
                },
                {
                    "id": "reject_1",
                    "domain": "test",
                    "category": "policy_rejection",
                    "expected_outcome": "policy_rejection",
                    "question": "delete",
                    "policy_probe_sql": "DELETE FROM numbers",
                },
            ]
            report = evaluate_sql.evaluate_cases(
                cases,
                service=FakeService(),
                environment=evaluate_sql.EvaluationEnvironment(
                    Path(directory) / "assets"
                ),
                default_database=str(database),
                input_cost_per_million=1.0,
                output_cost_per_million=2.0,
            )
        metrics = report["metrics"]
        self.assertEqual(metrics["sql_execution_success_rate"], 1.0)
        self.assertEqual(metrics["semantic_correctness_rate"], 1.0)
        self.assertEqual(metrics["policy_rejection_precision"], 1.0)
        self.assertEqual(metrics["policy_rejection_recall"], 1.0)
        self.assertEqual(metrics["candidate_selection_uplift"], 1.0)
        self.assertGreater(metrics["average_estimated_cost_usd"], 0)
        self.assertIsNotNone(metrics["p50_latency_ms"])


if __name__ == "__main__":
    unittest.main()
