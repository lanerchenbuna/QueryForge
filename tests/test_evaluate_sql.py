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

from queryforge.infrastructure.models.base import BaseModelProvider


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

    # ------------------------------------------------- environment vs model (D-5)

    def test_account_failure_is_not_counted_as_a_model_failure(self):
        """A depleted balance returned HTTP 402 and was recorded as a model
        failure, dragging sql_execution_success_rate from 1.00 to 0.75. It must
        be excluded from the accuracy denominators instead."""

        class OutOfBalanceService:
            def ask(self, question, options):
                raise RuntimeError(
                    "Error code: 402 - {'error': {'message': 'Insufficient Balance'}}"
                )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "numbers.sqlite"
            with sqlite3.connect(database) as connection:
                connection.execute("CREATE TABLE numbers (value INTEGER)")
                connection.execute("INSERT INTO numbers VALUES (1)")
            case = self._query_case(str(database))
            report = self._run(
                OutOfBalanceService(), [case], database, root / "assets"
            )
        metrics = report["metrics"]
        self.assertEqual(metrics["environment_error_cases"], 1)
        self.assertEqual(metrics["scored_case_count"], 0)
        # No scored cases left, so the rate is unmeasurable rather than 0.0.
        self.assertIsNone(metrics["sql_execution_success_rate"])
        self.assertIsNone(metrics["semantic_correctness_rate"])
        self.assertTrue(report["results"][0]["environment_error"])
        self.assertEqual(
            report["results"][0]["environment_error_reason"], "insufficient balance"
        )

    def test_environment_error_classifier_covers_transport_and_account_failures(self):
        for message in (
            "Error code: 402 - {'message': 'Insufficient Balance'}",
            "Error code: 429 - rate limit reached",
            "Error code: 401 - unauthorized",
            "No API key is configured for provider 'deepseek'",
            "ConnectTimeout: connection error",
            "503 service unavailable",
        ):
            with self.subTest(message=message):
                self.assertTrue(evaluate_sql.is_environment_error(RuntimeError(message)))
        # A genuine model failure must NOT be excused.
        for message in (
            "Model response is not valid JSON: Expecting value",
            "node=reflect: Human review required",
            "no such column: foo",
        ):
            with self.subTest(message=message):
                self.assertFalse(evaluate_sql.is_environment_error(RuntimeError(message)))

    def test_measured_usage_prefers_provider_counts_over_the_heuristic(self):
        """A real provider reports estimated=False; a char-count guess must not
        be presented as measured usage."""
        measured = {"input_tokens": 4820, "output_tokens": 96, "estimated": False}
        self.assertEqual(
            evaluate_sql._measured_usage({"usage": measured}, None),
            {"input_tokens": 4820, "output_tokens": 96},
        )
        # Estimated values are refused.
        estimated = {"input_tokens": 12, "output_tokens": 30, "estimated": True}
        self.assertIsNone(evaluate_sql._measured_usage({"usage": estimated}, None))
        # Nothing recorded at all.
        self.assertIsNone(evaluate_sql._measured_usage({}, None))
        # Falls back to the run-level log when the per-case record is absent.
        self.assertEqual(
            evaluate_sql._measured_usage({}, [measured]),
            {"input_tokens": 4820, "output_tokens": 96},
        )

    def test_token_source_is_declared_in_the_report(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "numbers.sqlite"
            with sqlite3.connect(database) as connection:
                connection.execute("CREATE TABLE numbers (value INTEGER)")
                connection.execute("INSERT INTO numbers VALUES (1)")
            report = self._run(
                FakeService(), [self._query_case(str(database))], database, root / "assets"
            )
        # FakeService reports no usage, so the report must say so rather than
        # implying the heuristic figures are billing data.
        self.assertEqual(report["metrics"]["token_source"], "estimated")
        self.assertEqual(report["metrics"]["token_source_measured_cases"], 0)

    # ------------------------------------------------- multi-candidate ablation

    def test_parallel_candidates_override_forces_the_count_for_every_case(self):
        """The per-case flag correlates with category, so an ablation needs to be
        able to override it over identical inputs."""

        seen: list[int] = []

        class CountingService:
            def ask(self, question, options):
                seen.append(int(options.parallel_candidates))
                return {
                    "status": "success",
                    "rows": [[1]],
                    "columns": ["value"],
                    "sql": "SELECT 1",
                    "model_provider": "fake",
                    "model": "fake-model",
                }

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "numbers.sqlite"
            with sqlite3.connect(database) as connection:
                connection.execute("CREATE TABLE numbers (value INTEGER)")
                connection.execute("INSERT INTO numbers VALUES (1)")
            # One case that would normally ask for 2, one that would not.
            cases = [
                self._query_case(str(database), candidate_selection=True),
                {**self._query_case(str(database)), "id": "query_2"},
            ]
            for forced, expected in ((1, 1), (2, 2), (3, 3)):
                seen.clear()
                report = evaluate_sql.evaluate_cases(
                    cases,
                    service=CountingService(),
                    environment=evaluate_sql.EvaluationEnvironment(root / f"assets{forced}"),
                    default_database=str(database),
                    parallel_candidates_override=forced,
                )
                with self.subTest(forced=forced):
                    self.assertEqual(seen, [expected, expected])
                    self.assertEqual(
                        report["metrics"]["parallel_candidates_override"], forced
                    )

    def test_without_override_the_per_case_flag_is_honoured(self):
        seen: list[int] = []

        class CountingService:
            def ask(self, question, options):
                seen.append(int(options.parallel_candidates))
                return {
                    "status": "success",
                    "rows": [[1]],
                    "columns": ["value"],
                    "sql": "SELECT 1",
                }

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "numbers.sqlite"
            with sqlite3.connect(database) as connection:
                connection.execute("CREATE TABLE numbers (value INTEGER)")
                connection.execute("INSERT INTO numbers VALUES (1)")
            cases = [
                self._query_case(str(database), candidate_selection=True),
                {**self._query_case(str(database)), "id": "query_2"},
            ]
            report = self._run(CountingService(), cases, database, root / "assets")
        # candidate_selection=True -> 2, absent -> 1
        self.assertEqual(seen, [2, 1])
        self.assertIsNone(report["metrics"]["parallel_candidates_override"])
        # The report must warn that this mode is confounded.
        self.assertIn("confounded", report["multi_candidate_method"])

    # ------------------------------------------------- governance coverage (feat-007)

    def test_governance_coverage_reports_cases_the_semantic_layer_cannot_see(self):
        """A question matching no governed metric skips SemanticSQLValidator entirely.

        Measured on this repository: 11 of 12 context-dependent cases and 8 of 12
        checked-in multi-turn cases match no governed metric, because metric
        matching is term-based and a follow-up like "Break that down by region."
        names no metric. The report must state that boundary instead of leaving it
        to be discovered by hand.
        """
        cases = [
            # Names no metric -> ungoverned, and needs context.
            {
                "id": "ungoverned_ctx",
                "expected_outcome": "query",
                "question": "Break that down by region.",
                "follow_up_context": ["Show total watch hours."],
                "requires_context": True,
                "semantic_model": "sample_data/anime_streaming/semantic_model.yml",
                "database": "sample_data/anime_streaming/anime_streaming.sqlite",
            },
            # Names a governed metric -> counted as checked, not ungoverned.
            {
                "id": "governed_1",
                "expected_outcome": "query",
                "question": "What are the watch hours by playback region?",
                "semantic_model": "sample_data/anime_streaming/semantic_model.yml",
                "database": "sample_data/anime_streaming/anime_streaming.sqlite",
            },
            # Probes are excluded from the coverage count.
            {
                "id": "probe_1",
                "expected_outcome": "policy_rejection",
                "question": "Drop the table",
            },
        ]
        coverage = evaluate_sql._governance_coverage(
            cases,
            "sample_data/anime_streaming/semantic_model.yml",
            "sample_data/anime_streaming/anime_streaming.sqlite",
        )
        self.assertEqual(coverage["cases_checked"], 2)
        self.assertEqual(coverage["ungoverned_count"], 1)
        self.assertEqual(coverage["ungoverned_cases"], ["ungoverned_ctx"])
        # The intersection that matters: needs context AND invisible to governance.
        self.assertEqual(
            coverage["ungoverned_requiring_context"], ["ungoverned_ctx"]
        )
        self.assertIn("grain", coverage["note"])

    def test_governance_coverage_is_resilient_to_bad_paths(self):
        """A coverage probe must never fail the run."""
        coverage = evaluate_sql._governance_coverage(
            [
                {
                    "id": "bad",
                    "expected_outcome": "query",
                    "question": "anything",
                    "semantic_model": "/nonexistent/model.yml",
                    "database": "/nonexistent/db.sqlite",
                }
            ],
            None,
            None,
        )
        self.assertEqual(coverage["cases_checked"], 0)
        self.assertEqual(coverage["ungoverned_cases"], [])

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

    # ------------------------------------------------- projection tolerance (D-1)
    #
    # The evaluator compares the columns the two projections share by name, so an
    # answer that is correct but projects a different set of columns is no longer
    # scored as semantically wrong. The difference is reported instead, as
    # extra_columns / missing_columns and as projection_difference_rate.
    #
    # Regression origin: 8 of the 40 cases in the Step 0 baseline asked only for
    # anime titles while the gold reference SQL additionally projected
    # studio_name and studio_tier, so a correct answer was scored wrong and the
    # reported accuracy understated the model by ~19 percentage points.

    def test_narrower_projection_than_reference_is_not_a_semantic_error(self):
        """Real shape: question asks for titles, reference SQL also projects the
        filter columns. The answer is correct and must not be scored wrong."""

        class NarrowProjectionService:
            def ask(self, question, options):
                return {
                    "status": "success",
                    "rows": [["Neon Genesis"], ["Akira"]],
                    "columns": ["title"],
                    "sql": "SELECT title FROM anime WHERE tier = 'Major'",
                    "model_provider": "fake",
                    "model": "fake-model",
                }

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "anime.sqlite"
            with sqlite3.connect(database) as connection:
                connection.execute(
                    "CREATE TABLE anime (title TEXT, studio_name TEXT, tier TEXT)"
                )
                connection.execute(
                    "INSERT INTO anime VALUES "
                    "('Neon Genesis', 'Gainax', 'Major'), ('Akira', 'TMS', 'Major')"
                )
            case = self._query_case(str(database))
            case["expected_sql"] = (
                "SELECT title, studio_name, tier FROM anime WHERE tier = 'Major'"
            )
            report = self._run(
                NarrowProjectionService(), [case], database, root / "assets"
            )
        metrics = report["metrics"]
        self.assertEqual(metrics["semantic_correctness_rate"], 1.0)
        self.assertEqual(metrics["projection_difference_cases"], 1)
        self.assertEqual(metrics["projection_difference_rate"], 1.0)
        result = report["results"][0]
        self.assertEqual(result["extra_columns"], [])
        self.assertEqual(result["missing_columns"], ["studio_name", "tier"])
        self.assertTrue(result["columns_compared"])

    def test_wider_projection_than_reference_is_not_a_semantic_error(self):
        """Real shape (tier-3 reg_3): the reference projects one aggregate, the
        answer projects that aggregate plus extra descriptive aggregates."""

        class WideProjectionService:
            def ask(self, question, options):
                return {
                    "status": "success",
                    # n matches the oracle; the other two columns are extra.
                    "rows": [[2, 2374, 165041.07]],
                    "columns": ["n", "quantity", "amount"],
                    "sql": "SELECT COUNT(*), SUM(q), SUM(a) FROM items",
                    "model_provider": "fake",
                    "model": "fake-model",
                }

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "items.sqlite"
            with sqlite3.connect(database) as connection:
                connection.execute("CREATE TABLE items (q INTEGER, a REAL)")
                connection.execute("INSERT INTO items VALUES (1, 10.0), (2, 20.0)")
            case = self._query_case(str(database))
            case["expected_sql"] = "SELECT COUNT(*) AS n FROM items"
            report = self._run(
                WideProjectionService(), [case], database, root / "assets"
            )
        metrics = report["metrics"]
        self.assertEqual(metrics["semantic_correctness_rate"], 1.0)
        result = report["results"][0]
        self.assertEqual(result["extra_columns"], ["quantity", "amount"])
        self.assertEqual(result["missing_columns"], [])

    def test_projection_with_no_shared_column_name_is_a_wrong_answer(self):
        """Different width AND no shared column name = a different query shape.

        Projection tolerance must not excuse an answer that describes a
        different quantity.
        """

        class DifferentShapeService:
            def ask(self, question, options):
                return {
                    "status": "success",
                    "rows": [[1]],
                    "columns": ["left_id"],
                    "sql": "SELECT left_id FROM pairs",
                    "model_provider": "fake",
                    "model": "fake-model",
                }

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "pairs.sqlite"
            with sqlite3.connect(database) as connection:
                connection.execute("CREATE TABLE pairs (a INTEGER, b INTEGER)")
                connection.execute("INSERT INTO pairs VALUES (1, 2)")
            case = self._query_case(str(database))
            case["expected_sql"] = "SELECT a, b FROM pairs"
            report = self._run(
                DifferentShapeService(), [case], database, root / "assets"
            )
        self.assertEqual(report["metrics"]["semantic_correctness_rate"], 0.0)

    def test_renamed_column_of_equal_width_keeps_positional_fallback(self):
        """Equal width, disjoint names: a renamed column with the same values is
        not newly penalised. This preserves the pre-fix behaviour."""

        class RenamedColumnService:
            def ask(self, question, options):
                return {
                    "status": "success",
                    "rows": [[1]],
                    "columns": ["action_anime_count"],
                    "sql": "SELECT COUNT(*) FROM pairs",
                    "model_provider": "fake",
                    "model": "fake-model",
                }

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "pairs.sqlite"
            with sqlite3.connect(database) as connection:
                connection.execute("CREATE TABLE pairs (a INTEGER, b INTEGER)")
                connection.execute("INSERT INTO pairs VALUES (1, 2)")
            case = self._query_case(str(database))
            case["expected_sql"] = "SELECT COUNT(*) AS anime_count FROM pairs"
            report = self._run(
                RenamedColumnService(), [case], database, root / "assets"
            )
        self.assertEqual(report["metrics"]["semantic_correctness_rate"], 1.0)

    def test_wrong_values_under_a_shared_projection_are_still_a_semantic_error(self):
        """Shared column names must not rescue values that genuinely differ."""

        class WrongValueService:
            def ask(self, question, options):
                return {
                    "status": "success",
                    "rows": [["Card", 120]],  # expected: a single overall count
                    "columns": ["payment_method", "n"],
                    "sql": "SELECT payment_method, COUNT(*) FROM orders GROUP BY 1",
                    "model_provider": "fake",
                    "model": "fake-model",
                }

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "orders.sqlite"
            with sqlite3.connect(database) as connection:
                connection.execute(
                    "CREATE TABLE orders (payment_method TEXT, order_id INTEGER)"
                )
                connection.execute(
                    "INSERT INTO orders VALUES ('Card', 1), ('Cash', 2)"
                )
            case = self._query_case(str(database))
            case["expected_sql"] = (
                "SELECT COUNT(DISTINCT payment_method) AS n FROM orders"
            )
            report = self._run(
                WrongValueService(), [case], database, root / "assets"
            )
        # Shared column "n": 120 != 2, so this stays a genuine mismatch.
        self.assertEqual(report["metrics"]["semantic_correctness_rate"], 0.0)

    def test_projection_difference_rate_is_none_without_column_names(self):
        """Positional fallback (no column names) cannot report a projection diff."""

        class NoColumnsService:
            def ask(self, question, options):
                return {
                    "status": "success",
                    "rows": [[1]],
                    "sql": "SELECT 1",
                    "model_provider": "fake",
                    "model": "fake-model",
                }

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "numbers.sqlite"
            with sqlite3.connect(database) as connection:
                connection.execute("CREATE TABLE numbers (value INTEGER)")
                connection.execute("INSERT INTO numbers VALUES (1)")
            case = self._query_case(str(database))
            report = self._run(NoColumnsService(), [case], database, root / "assets")
        metrics = report["metrics"]
        self.assertEqual(metrics["semantic_correctness_rate"], 1.0)
        self.assertIsNone(metrics["projection_difference_rate"])
        self.assertFalse(report["results"][0]["columns_compared"])

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

    def test_duplicate_rows_are_preserved_in_semantic_comparison(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "numbers.sqlite"
            with sqlite3.connect(database) as connection:
                connection.execute("CREATE TABLE numbers (value INTEGER)")
                connection.execute("INSERT INTO numbers VALUES (1), (1)")
            case = self._query_case(str(database))
            case["expected_sql"] = "SELECT value FROM numbers"  # -> [[1],[1]]

            class OneRowService:
                def ask(self, question, options):
                    return {
                        "status": "success",
                        "rows": [[1]],
                        "columns": ["value"],
                        "sql": "SELECT value FROM numbers LIMIT 1",
                    }

            report = self._run(OneRowService(), [case], database, root / "a")
            # A single row is NOT equivalent to the duplicated expected rows.
            self.assertEqual(report["metrics"]["semantic_correctness_rate"], 0.0)

            class TwoRowService:
                def ask(self, question, options):
                    return {
                        "status": "success",
                        "rows": [[1], [1]],
                        "columns": ["value"],
                        "sql": "SELECT value FROM numbers",
                    }

            report = self._run(TwoRowService(), [case], database, root / "b")
            self.assertEqual(report["metrics"]["semantic_correctness_rate"], 1.0)

    def test_float_tolerance_absorbs_float_noise(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "numbers.sqlite"
            with sqlite3.connect(database) as connection:
                connection.execute("CREATE TABLE numbers (value REAL)")
            case = self._query_case(str(database))
            case["expected_sql"] = "SELECT 0.1 + 0.2"  # 0.30000000000000004

            class FloatService:
                def ask(self, question, options):
                    return {
                        "status": "success",
                        "rows": [[0.3]],
                        "columns": ["0.1 + 0.2"],
                        "sql": "SELECT 0.3",
                    }

            report = self._run(FloatService(), [case], database, root / "assets")
            self.assertEqual(report["metrics"]["semantic_correctness_rate"], 1.0)
            self.assertEqual(evaluate_sql._canonical_value(float("inf")), "Infinity")
            self.assertEqual(evaluate_sql._canonical_value(float("nan")), "NaN")

    def test_case_fingerprint_drives_unique_case_count(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "numbers.sqlite"
            with sqlite3.connect(database) as connection:
                connection.execute("CREATE TABLE numbers (value INTEGER)")
            duplicated = self._query_case(str(database))
            sibling = dict(duplicated)
            sibling["id"] = "query_2"  # same question + expected_sql, new id
            report = self._run(
                FakeService(), [duplicated, sibling], database, root / "assets"
            )
        self.assertEqual(report["case_count"], 2)
        self.assertEqual(report["unique_case_count"], 1)
        self.assertEqual(
            report["results"][0]["case_fingerprint"],
            report["results"][1]["case_fingerprint"],
        )

    def test_isolated_config_redirects_all_state_paths(self):
        from queryforge.core.config import Config

        config = Config(
            llm_provider="openai",
            llm_api_key=None,
            llm_model="offline",
            llm_base_url=None,
            database_path="items.sqlite",
            history_db_path=".queryforge/history.db",
            orchestration_state_root=".queryforge/runs",
            vector_kb_path=".queryforge/lancedb",
        )
        with tempfile.TemporaryDirectory() as directory:
            isolated = evaluate_sql.isolated_config(config, directory)
        root = Path(directory).resolve() / "isolated"
        self.assertEqual(isolated.history_db_path, str(root / "history.db"))
        self.assertEqual(isolated.orchestration_state_root, str(root / "runs"))
        self.assertEqual(isolated.vector_kb_path, str(root / "lancedb"))
        # The production config must stay untouched.
        self.assertEqual(config.history_db_path, ".queryforge/history.db")

    def test_oracle_latency_is_recorded_separately(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "numbers.sqlite"
            with sqlite3.connect(database) as connection:
                connection.execute("CREATE TABLE numbers (value INTEGER)")
                connection.execute("INSERT INTO numbers VALUES (1)")
            case = self._query_case(str(database))
            report = self._run(FakeService(), [case], database, root / "assets")
        result = report["results"][0]
        self.assertIsNotNone(result["oracle_latency_ms"])
        self.assertGreaterEqual(result["oracle_latency_ms"], 0)
        self.assertIsNotNone(report["metrics"]["average_oracle_latency_ms"])
        self.assertIsNotNone(report["metrics"]["average_service_latency_ms"])


if __name__ == "__main__":
    unittest.main()


class RecordingModelFactoryTest(unittest.TestCase):
    """The evaluator's provider wrapper must match the provider contract.

    Regression origin: ``_recording_model_factory`` wrapped the real adapter with a
    ``generate_with_messages`` that still had the pre-feat-012 two-argument
    signature. Once the model budget started passing ``timeout`` through, every
    evaluated case failed at ``gen_sql`` with an unexpected keyword argument — and
    the existing tests did not notice, because they inject an ``AgentService``
    double and never build this wrapper against a real adapter.

    A run that fails in 60 ms with zero measured tokens is the signature of this
    class of bug, so the test asserts the wrapper actually completes a call.
    """

    def test_the_recording_wrapper_accepts_the_provider_contract(self):
        import inspect

        contract = inspect.signature(BaseModelProvider.generate_with_messages)
        self.assertIn("timeout", contract.parameters)

        class Adapter(BaseModelProvider):
            provider = "test"
            model = "test-1"

            def generate_with_messages(
                self, messages, json_mode=False, timeout=None
            ) -> str:
                self.last_usage = None
                return '{"sql": "SELECT 1", "explanation": "e", "tables_used": []}'

        usage_log: list[dict] = []
        wrapped = evaluate_sql.wrap_provider_for_usage(Adapter(), usage_log)

        # The deadline is ambient; the wrapper must forward it rather than choke.
        from queryforge.core.observability import model_deadline

        with model_deadline(30.0):
            raw = wrapped.generate_with_messages(
                [{"role": "user", "content": "hi"}], timeout=30.0
            )
        self.assertIn("SELECT 1", raw)

    def test_the_wrapper_is_used_by_a_real_agent_service(self):
        """Guard the path the doubles never touch: a real service, real workflow."""
        import sqlite3
        import tempfile
        from pathlib import Path

        from queryforge.application import AgentService, AgentOptions
        from queryforge.core.config import Config

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "w.sqlite"
            with sqlite3.connect(database) as connection:
                connection.execute("CREATE TABLE items (name TEXT)")
                connection.execute("INSERT INTO items VALUES ('a')")
            config = Config(
                llm_provider="openai",
                llm_api_key=None,
                llm_model="offline",
                llm_base_url=None,
                database_path=str(database),
                history_db_path=str(root / "history.db"),
                orchestration_state_root=str(root / "runs"),
            )

            import json as _json

            class Adapter(BaseModelProvider):
                provider = "test"
                model = "test-1"

                def generate_with_messages(
                    self, messages, json_mode=False, timeout=None
                ) -> str:
                    self.last_usage = None
                    prompt = _json.dumps(messages, ensure_ascii=False)
                    if "Select local QueryForge skills" in prompt:
                        return _json.dumps({"skills": [], "reason": "none"})
                    if "Evaluate whether the SQL and result" in prompt:
                        return _json.dumps(
                            {"success": True, "strategy": "SUCCESS", "reason": "ok"}
                        )
                    return _json.dumps(
                        {"sql": "SELECT 1", "explanation": "e", "tables_used": []}
                    )

            usage_log: list[dict] = []

            def factory(_config):
                return evaluate_sql.wrap_provider_for_usage(Adapter(), usage_log)

            service = AgentService(
                config_loader=lambda **_: config, llm_factory=factory
            )
            output = service.ask(
                "How many items are there?",
                AgentOptions(
                    database=str(database),
                    skills=None,
                    run_id="wrapper_smoke",
                    orchestration_state_root=str(root / "runs"),
                ),
            )
        self.assertEqual(output.get("status"), "success")
