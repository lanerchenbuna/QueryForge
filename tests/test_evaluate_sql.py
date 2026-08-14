import importlib.util
import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch


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


class BlockedService:
    """Query case whose generated SQL was (wrongly) rejected by the policy engine."""

    def ask(self, question, options):
        return {
            "status": "blocked",
            "rows": [],
            "sql": "SELECT secret FROM numbers",
            "sql_security": {
                "decisions": [
                    {
                        "allowed": False,
                        "run_id": "qf_blocked",
                        "policy_name": "strict",
                        "rule": "column_scope",
                        "reason": "outside the allowed column scope",
                    }
                ]
            },
        }


class ReorderedService:
    """Model returns correct rows but with a different column order."""

    def ask(self, question, options):
        return {
            "status": "success",
            "rows": [[2, 1]],
            "columns": ["b", "a"],
            "sql": "SELECT b, a FROM pairs",
            "model_provider": "fake",
            "model": "fake-model",
        }


class WarmupService:
    def ask(self, question, options):
        if question == "warmup":
            time.sleep(0.25)
            return {"status": "success", "rows": [[1]], "sql": "SELECT 1"}
        time.sleep(0.02)
        return {"status": "success", "rows": [[1]], "sql": "SELECT 1"}


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
        self.assertEqual(report["query_count"], 1)
        self.assertEqual(report["probe_count"], 1)
        self.assertEqual(metrics["policy_true_positives"], 1)
        self.assertEqual(metrics["policy_false_positives"], 0)
        self.assertEqual(metrics["policy_false_negatives"], 0)

    def _query_case(self, database: str, **extra) -> dict:
        case = {
            "id": "query_1",
            "domain": "test",
            "category": "metric",
            "expected_outcome": "query",
            "question": "return one",
            "expected_sql": "SELECT 1",
        }
        case["database"] = database
        case.update(extra)
        return case

    def _probe_case(self, database: str, probe: str, sql_policy: str | None = None) -> dict:
        case = {
            "id": "reject_1",
            "domain": "test",
            "category": "policy_rejection",
            "expected_outcome": "policy_rejection",
            "question": "delete",
            "database": database,
            "policy_probe_sql": probe,
        }
        if sql_policy:
            case["sql_policy"] = sql_policy
        return case

    def _run(
        self, service, cases, database: Path, environment_root: Path
    ) -> dict:
        return evaluate_sql.evaluate_cases(
            cases,
            service=service,
            environment=evaluate_sql.EvaluationEnvironment(environment_root),
            default_database=str(database),
        )

    def test_probes_run_through_the_real_policy_engine_with_case_policy(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "numbers.sqlite"
            with sqlite3.connect(database) as connection:
                connection.execute("CREATE TABLE numbers (value INTEGER)")
                connection.execute("INSERT INTO numbers VALUES (1)")
            scope_policy = root / "scope.yml"
            scope_policy.write_text(
                "version: 1\nname: scope\nallowed_tables: []\n",
                encoding="utf-8",
            )
            open_policy = root / "open.yml"
            open_policy.write_text(
                "version: 1\nname: open\nallowed_tables: [numbers]\n",
                encoding="utf-8",
            )
            cases = [
                # Table-scope probe must be rejected by the real engine.
                self._probe_case(
                    str(database),
                    "SELECT value FROM numbers",
                    sql_policy=str(scope_policy),
                ),
                # A probe that the policy allows is a bypass -> false negative.
                self._probe_case(
                    str(database),
                    "SELECT 1",
                    sql_policy=str(open_policy),
                ),
            ]
            report = self._run(FakeService(), cases, database, root / "assets")
        metrics = report["metrics"]
        self.assertEqual(metrics["policy_true_positives"], 1)
        self.assertEqual(metrics["policy_false_negatives"], 1)
        self.assertEqual(metrics["policy_rejection_recall"], 0.5)
        probe = report["results"][0]
        self.assertEqual(probe["policy_rule"], "table_scope")
        self.assertEqual(probe["policy_name"], "scope")

    def test_query_case_policy_rejection_counts_as_false_positive(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "numbers.sqlite"
            with sqlite3.connect(database) as connection:
                connection.execute("CREATE TABLE numbers (value INTEGER)")
                connection.execute("INSERT INTO numbers VALUES (1)")
            cases = [
                self._query_case(str(database)),
                self._probe_case(str(database), "DELETE FROM numbers"),
            ]
            report = self._run(
                BlockedService(), cases, database, root / "assets"
            )
        metrics = report["metrics"]
        # TP=1 (probe), FP=1 (legitimate query wrongly rejected) -> precision 0.5.
        self.assertEqual(metrics["policy_false_positives"], 1)
        self.assertEqual(metrics["policy_rejection_precision"], 0.5)
        self.assertEqual(metrics["policy_rejection_recall"], 1.0)

    def test_semantic_equivalence_is_tolerant_to_column_order(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "pairs.sqlite"
            with sqlite3.connect(database) as connection:
                connection.execute("CREATE TABLE pairs (a INTEGER, b INTEGER)")
                connection.execute("INSERT INTO pairs VALUES (1, 2)")
            case = self._query_case(str(database))
            case["expected_sql"] = "SELECT a, b FROM pairs"
            report = self._run(
                ReorderedService(), [case], database, root / "assets"
            )
        self.assertEqual(report["metrics"]["semantic_correctness_rate"], 1.0)

    def test_followup_warmup_turns_are_excluded_from_latency(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "numbers.sqlite"
            with sqlite3.connect(database) as connection:
                connection.execute("CREATE TABLE numbers (value INTEGER)")
                connection.execute("INSERT INTO numbers VALUES (1)")
            case = self._query_case(str(database))
            case["follow_up_context"] = ["warmup"]
            report = self._run(
                WarmupService(), [case], database, root / "assets"
            )
        latency = report["results"][0]["latency_ms"]
        self.assertLess(latency, 200, "latency must exclude the 250ms warmup turn")

    def test_gate_failures_only_apply_to_measurable_cases(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "numbers.sqlite"
            with sqlite3.connect(database) as connection:
                connection.execute("CREATE TABLE numbers (value INTEGER)")
            # A probe the default policy allows -> recall 0.0 (bypass).
            cases = [self._probe_case(str(database), "SELECT 1")]
            report = self._run(FakeService(), cases, database, root / "assets")
        self.assertIsNone(report["metrics"]["sql_execution_success_rate"])
        self.assertEqual(report["metrics"]["policy_rejection_recall"], 0.0)
        # Probe-only runs must not trip the execution-success gate.
        with patch("sys.argv", ["evaluate_sql.py", "--cases", "x.jsonl"]):
            args = evaluate_sql.parse_args()
        self.assertEqual(evaluate_sql._gate_failures(report, args), [])
        args.min_policy_recall = 1.0
        self.assertEqual(
            evaluate_sql._gate_failures(report, args),
            ["policy_rejection_recall=0.0 below --min-policy-recall 1.0"],
        )


if __name__ == "__main__":
    unittest.main()
