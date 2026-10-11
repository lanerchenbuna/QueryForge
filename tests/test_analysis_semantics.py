"""Phase 2: public analysis results checked against independent populations."""
from datetime import date
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import sqlite3
import unittest
from unittest.mock import patch

import yaml
from pydantic import ValidationError

from tests.test_analysis_planner import Fixture
from queryforge.core.schemas.models import DateContext, DateRange
from queryforge.domain.semantic import SemanticContractValidator, SemanticModelLoader
from queryforge.domain.semantic.schemas import MetricMatch, SemanticMetric, SemanticModelContext
from queryforge.domain.semantic.sql_validator import QuerySpecCompiler, SemanticSQLValidator
from queryforge.domain.semantic.periods import period_bounds, period_label
from queryforge.domain.semantic.query_constraints import require_filter_predicates, UnsupportedConstraint
from queryforge.infrastructure.db.sqlite_connector import SQLiteConnector


class AnalysisSemanticsTest(unittest.TestCase):
    def setUp(self):
        self.fixture = Fixture()

    def tearDown(self):
        self.fixture.cleanup()

    def model(self):
        return yaml.safe_load(self.fixture.semantic_model.read_text())

    def save(self, model):
        self.fixture.semantic_model.write_text(yaml.safe_dump(model))

    def load(self):
        with SQLiteConnector(str(self.fixture.database)) as db:
            schemas = [db.describe_table(t) for t in db.list_tables()]
        return SemanticModelLoader.load_and_validate(self.fixture.semantic_model, schemas, "")

    @staticmethod
    def dataset(payload):
        return next(s["outputs"] for s in payload["steps"] if s["action"] == "query_metric")

    def cross_fact(self):
        with sqlite3.connect(self.fixture.database) as db:
            db.executemany("INSERT INTO brands VALUES (?, ?)", [(1, "a"), (2, "b")])
            db.execute("CREATE TABLE charges (id INTEGER PRIMARY KEY, brand_id INTEGER, amount REAL)")
            db.executemany("INSERT INTO charges VALUES (?, ?, ?)", [(1, 1, 5), (2, 1, 7), (3, 2, 3)])
        model = self.model()
        model["entities"].append({"name": "charges", "table": "charges", "entity_type": "fact", "primary_key": ["id"]})
        model["relationships"].append({"name": "charges_to_brands", "from": "charges.brand_id", "to": "brands.brand_id", "relationship_type": "many_to_one"})
        model["metrics"][1]["allowed_dimensions"].append("brands.brand_name")
        model["metrics"][1]["analysis"] = {"unit": "USD", "additivity": "additive"}
        model["metrics"].append({"name": "charge_total", "description": "Independent charges.", "entity": "charges", "aggregation": "sum", "expression": "SUM(charges.amount)", "synonyms": ["charge total"], "allowed_dimensions": ["brands.brand_name"], "analysis": {"unit": "USD", "additivity": "additive"}})
        self.save(model)

    def timed(self, days, *, timezone="UTC", fiscal=1, complete=None, start="2024-01-01", strict=False):
        with sqlite3.connect(self.fixture.database) as db:
            db.execute("ALTER TABLE items ADD COLUMN happened TEXT")
            db.execute("ALTER TABLE items ADD COLUMN shipped TEXT")
            db.executemany("UPDATE items SET happened=?, shipped='2030-01-01' WHERE item_id=?", [(day, i) for i, day in enumerate(days, 1)])
        model = self.model()
        model["entities"][0]["dimensions"].extend([{"name": "event_date", "column": "happened"}, {"name": "ship_date", "column": "shipped"}])
        metric = model["metrics"][1]
        metric["time_field"] = "items.happened"
        metric["allowed_dimensions"].extend(["items.event_date", "items.ship_date"])
        temporal = {"role": "order_created", "timezone": timezone, "fiscal_year_start_month": fiscal, "require_complete_periods": strict}
        if complete:
            temporal.update(coverage_start=start, complete_through=complete)
        metric["analysis"] = {"unit": "USD", "additivity": "additive", "time": temporal}
        self.save(model)

    def ratio(self):
        model = self.model()
        model["metrics"].append({"name": "mean_amount", "description": "Amount per item.", "entity": "items", "aggregation": "ratio", "ratio": {"numerator": "SUM(items.amount)", "denominator": "COUNT(items.item_id)"}, "allowed_dimensions": ["items.category"], "synonyms": ["mean amount"], "analysis": {"unit": "USD/item", "additivity": "non_additive"}})
        self.save(model)

    def test_cross_fact_scalar_has_no_raw_fact_join(self):
        self.cross_fact()
        result = self.fixture.analyze("Show item amount and charge total")
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["answer"]["values"], {"item_amount": 60.0, "charge_total": 15.0})
        self.assertEqual(len(self.dataset(result)["query_spec"]["metric_queries"]), 2)

    def test_cross_fact_shared_grain_matches_independent_totals(self):
        self.cross_fact()
        result = self.fixture.analyze("Show item amount and charge total by brand_name")
        self.assertEqual(result["status"], "succeeded")
        dataset = self.dataset(result)
        indexes = [0, dataset["columns"].index("item_amount"), dataset["columns"].index("charge_total")]
        self.assertEqual([[row[i] for i in indexes] for row in dataset["rows"]], [["a", 40.0, 12.0], ["b", 20.0, 3.0]])
        self.assertTrue(result["answer"]["evidence_ids"])

    def test_shared_filter_governs_every_fact_branch(self):
        self.cross_fact()
        result = self.fixture.analyze("Show item amount and charge total where brands.brand_name = 'a'")
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["answer"]["values"], {"item_amount": 40.0, "charge_total": 12.0})
        dataset = self.dataset(result)
        require_filter_predicates(dataset["sql"], dataset["filters"], metric_ctes=dataset["query_spec"]["metric_ctes"])
        broken = dataset["sql"].replace("WHERE (brands.brand_name = 'a')", "", 1)
        with self.assertRaises(UnsupportedConstraint):
            require_filter_predicates(broken, dataset["filters"], metric_ctes=dataset["query_spec"]["metric_ctes"])

    def test_cross_fact_local_filter_is_clarified(self):
        self.cross_fact()
        result = self.fixture.analyze("Show item amount and charge total where amount > 10")
        self.assertEqual(result["status"], "needs_clarification")
        self.assertEqual(result["steps"], [])

    def test_aggregate_join_validator_rejects_changed_wrapper(self):
        self.cross_fact()
        context = self.load()
        matches = [MetricMatch(metric=m, matched_term=m.name) for m in context.model.metrics if m.name in {"item_amount", "charge_total"}]
        spec = QuerySpecCompiler.compile(semantic_model=context, metric_matches=matches, requested_dimensions=["brands.brand_name"])
        validator = SemanticSQLValidator(context, matches, requested_dimensions=["brands.brand_name"])
        self.assertEqual(validator.validate(spec.sql).status, "passed")
        cases = [spec.sql.replace(" IS ", " != ", 1),
                 spec.sql.replace("UNION SELECT", "UNION ALL SELECT"),
                 spec.sql.replace("SUM(charges.amount)", "COUNT(charges.amount)"),
                 spec.sql.replace("items.brand_id = brands.brand_id", "items.item_id = brands.brand_id"),
                 spec.sql.replace("GROUP BY brands.brand_name", "GROUP BY brands.brand_id", 1)]
        for sql in cases:
            with self.subTest(sql=sql):
                self.assertNotEqual(validator.validate(sql).status, "passed")

    def test_aggregate_join_validator_requires_every_default_filter(self):
        self.cross_fact()
        model = self.model()
        model["metrics"][-1]["default_filters"] = ["charges.amount > 3"]
        self.save(model)
        context = self.load()
        matches = [MetricMatch(metric=m, matched_term=m.name) for m in context.model.metrics if m.name in {"item_amount", "charge_total"}]
        spec = QuerySpecCompiler.compile(semantic_model=context, metric_matches=matches)
        validator = SemanticSQLValidator(context, matches)
        self.assertEqual(validator.validate(spec.sql).status, "passed")
        self.assertNotEqual(validator.validate(spec.sql.replace("WHERE (charges.amount > 3)", "")).status, "passed")

    def test_aggregate_join_validator_rejects_optional_time_window(self):
        import sqlglot
        from sqlglot import exp
        self.timed(["2024-02-01", "2025-01-01", "2025-02-01"])
        model = self.model()
        model["metrics"][0]["time_field"] = "items.happened"
        self.save(model)
        context = self.load()
        matches = [MetricMatch(metric=m, matched_term=m.name) for m in context.model.metrics]
        dates = DateContext(reference_date="2025-03-01", source="rule", ranges=[DateRange(expression="February 2025", start_date="2025-02-01", end_date="2025-02-28")])
        spec = QuerySpecCompiler.compile(semantic_model=context, metric_matches=matches, date_context=dates)
        validator = SemanticSQLValidator(context, matches, date_context=dates)
        self.assertEqual(validator.validate(spec.sql).status, "passed")
        tree = sqlglot.parse_one(spec.sql, read="sqlite")
        branch = next(tree.find_all(exp.CTE)).this
        branch.set("where", exp.Where(this=exp.or_(branch.args["where"].this, exp.EQ(this=exp.Literal.number(1), expression=exp.Literal.number(1)))))
        self.assertNotEqual(validator.validate(tree.sql(dialect="sqlite")).status, "passed")

    def test_missing_fact_group_is_not_invented_zero(self):
        self.cross_fact()
        with sqlite3.connect(self.fixture.database) as db:
            db.execute("DELETE FROM charges WHERE brand_id = 2")
        result = self.fixture.analyze("Show item amount and charge total by brand_name", max_replans=0)
        self.assertNotEqual(result["status"], "succeeded")
        self.assertIsNone(result["answer"])

    def test_duplicate_dimension_keys_fail_runtime_grain_check(self):
        self.cross_fact()
        with sqlite3.connect(self.fixture.database) as db:
            db.execute("ALTER TABLE brands RENAME TO old_brands")
            db.execute("CREATE TABLE brands (brand_id INTEGER, brand_name TEXT)")
            db.execute("INSERT INTO brands SELECT * FROM old_brands")
            db.execute("INSERT INTO brands VALUES (1, 'duplicate')")
        result = self.fixture.analyze("Show item amount and charge total by brand_name", max_replans=0)
        self.assertNotEqual(result["status"], "succeeded")
        self.assertTrue(any("unverified_join_grain" in (s.get("error") or "") for s in result["steps"]))

    def test_many_to_many_dimension_contract_is_rejected(self):
        self.cross_fact()
        model = self.model()
        model["relationships"][1]["relationship_type"] = "many_to_many"
        self.save(model)
        with self.assertRaisesRegex(ValueError, "fan-out"):
            self.load()

    def test_forbidden_dimension_does_not_return_a_total(self):
        self.cross_fact()
        result = self.fixture.analyze("Show item amount and charge total by category", max_replans=0)
        self.assertNotEqual(result["status"], "succeeded")
        self.assertIsNone(result["answer"])

    def test_weighted_ratio_recomputes_components_at_each_grain(self):
        self.ratio()
        with sqlite3.connect(self.fixture.database) as db:
            db.execute("UPDATE items SET amount=100 WHERE item_id=2")
        scalar = self.fixture.analyze("Show mean amount")
        grouped = self.fixture.analyze("Show mean amount by category")
        self.assertEqual(scalar["status"], "succeeded")
        self.assertAlmostEqual(scalar["answer"]["value"], 140 / 3)
        self.assertEqual(grouped["status"], "succeeded")
        self.assertEqual(self.dataset(grouped)["rows"], [["a", 20.0, 40.0, 2], ["b", 100.0, 100.0, 1]])
        self.assertNotAlmostEqual(scalar["answer"]["value"], (20 + 100) / 2)
        self.assertFalse(any(s["action"] == "drill_down" for s in grouped["steps"]))
        self.assertEqual(scalar["answer"]["metric_contracts"]["mean_amount"]["unit"], "USD/item")

    def test_zero_denominator_cannot_be_successful(self):
        self.ratio()
        model = self.model()
        model["metrics"][-1]["ratio"]["denominator"] = "SUM(items.amount)"
        self.save(model)
        with sqlite3.connect(self.fixture.database) as db:
            db.execute("UPDATE items SET amount=0")
        result = self.fixture.analyze("Show mean amount", max_replans=0)
        self.assertEqual(result["status"], "partial")
        self.assertIsNone(result["answer"])

    def test_ratio_sql_gate_rejects_wrong_denominator_and_scale(self):
        self.ratio()
        model = self.model()
        model["metrics"][-1]["ratio"]["denominator"] = "COUNT(*)"
        self.save(model)
        context = self.load()
        metric = context.model.metrics[-1]
        validator = SemanticSQLValidator(context, [MetricMatch(metric=metric, matched_term=metric.name)])
        correct = "SELECT CAST(SUM(i.amount) AS REAL) / NULLIF(COUNT(*), 0) AS mean_amount FROM items i"
        self.assertEqual(validator.validate(correct).status, "passed")
        self.assertEqual(validator.validate("SELECT ROUND(CAST(SUM(i.amount) AS REAL) / NULLIF(COUNT(*), 0), 6) AS mean_amount FROM items i").status, "passed")
        for expression in ("CAST(SUM(i.amount) AS REAL) / NULLIF(COUNT(i.amount), 0)",
                           "CAST(COUNT(*) AS REAL) / NULLIF(SUM(i.amount), 0)",
                           "CAST(SUM(i.amount) AS REAL) * 100 / NULLIF(COUNT(*), 0)",
                           "ROUND(CAST(SUM(i.amount) AS REAL), 0) / NULLIF(COUNT(*), 0)"):
            with self.subTest(expression=expression):
                self.assertNotEqual(validator.validate(f"SELECT {expression} AS mean_amount FROM items i").status, "passed")

    def test_avg_equivalence_requires_same_null_population_and_scale(self):
        self.ratio()
        model = self.model()
        model["metrics"][-1]["ratio"].update(denominator="COUNT(*)", scale=100.0)
        self.save(model)
        context = self.load()
        metric = context.model.metrics[-1]
        matches = [MetricMatch(metric=metric, matched_term=metric.name)]
        sql = "SELECT AVG(i.amount) * 100 AS mean_amount FROM items i"
        self.assertNotEqual(SemanticSQLValidator(context, matches).validate(sql).status, "passed")
        proven = SemanticSQLValidator(context, matches, non_null_columns={("items", "amount")})
        self.assertEqual(proven.validate(sql).status, "passed")
        self.assertEqual(proven.validate("SELECT ROUND(AVG(i.amount) * 100, 4) AS mean_amount FROM items i").status, "passed")
        self.assertNotEqual(proven.validate("SELECT AVG(i.amount) AS mean_amount FROM items i").status, "passed")

    def test_nullable_avg_with_count_column_and_derived_aggregates(self):
        self.ratio()
        model = self.model()
        model["metrics"][-1]["ratio"]["denominator"] = "COUNT(items.amount)"
        self.save(model)
        context = self.load()
        metric = context.model.metrics[-1]
        validator = SemanticSQLValidator(context, [MetricMatch(metric=metric, matched_term=metric.name)])
        self.assertEqual(validator.validate("SELECT AVG(i.amount) AS mean_amount FROM items i").status, "passed")
        wrong = "WITH x AS (SELECT SUM(items.amount) AS amount FROM items GROUP BY category) SELECT AVG(x.amount) AS mean_amount FROM x"
        self.assertNotEqual(validator.validate(wrong).status, "passed")

    def test_distinct_count_groups_are_never_summed(self):
        model = self.model()
        model["metrics"][0]["expression"] = "COUNT(DISTINCT items.brand_id)"
        self.save(model)
        result = self.fixture.analyze("Show item count by category")
        self.assertEqual(result["status"], "succeeded")
        self.assertFalse(any(s["action"] == "drill_down" for s in result["steps"]))
        self.assertEqual(result["answer"]["metric_contracts"]["item_count"]["additivity"], "non_additive")

    def test_unique_row_count_can_be_added_across_disjoint_groups(self):
        model = self.model()
        model["metrics"][0]["expression"] = "COUNT(DISTINCT items.item_id)"
        self.save(model)
        result = self.fixture.analyze("Show item count by category")
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["answer"]["rows"], [["a", 2], ["b", 1]])
        drill = next(s["outputs"] for s in result["steps"] if s["action"] == "drill_down")
        self.assertEqual(drill["total_value"], 3)
        self.assertEqual(result["answer"]["metric_contracts"]["item_count"]["additivity_proof"], "unique_row_identity")

    def test_identity_additivity_requires_verified_fact_grain(self):
        from queryforge.infrastructure.tools.data_quality_tool import QualityCheckResult
        model = self.model()
        model["metrics"][0]["expression"] = "COUNT(DISTINCT items.item_id)"
        self.save(model)
        with patch("queryforge.infrastructure.tools.data_quality_tool.DataQualityTool._grain_check", return_value=QualityCheckResult(
            table="items", check="grain_unique", status="warning", reason="bounded_scan_incomplete")):
            result = self.fixture.analyze("Show item count by category", max_replans=0)
        self.assertNotEqual(result["status"], "succeeded")
        self.assertTrue(any("unverified_additivity" in (s.get("error") or "") for s in result["steps"]))

    def test_requested_year_includes_yoy_baseline_automatically(self):
        self.timed(["2024-02-01", "2025-01-01", "2025-02-01"])
        result = self.fixture.analyze("Show item amount monthly trend for February 2025 year over year")
        self.assertEqual(result["status"], "succeeded")
        comparison = next(s["outputs"] for s in result["steps"] if s["action"] == "compare_periods")
        self.assertEqual((comparison["current"], comparison["baseline"]), (30.0, 10.0))
        self.assertEqual(comparison["percent_change"], 200.0)
        self.assertEqual(len(self.dataset(result)["query_spec"]["query_ranges"]), 2)

    def test_missing_requested_current_never_uses_baseline_as_current(self):
        self.timed(["2024-02-01", "2024-03-01", "2025-01-01"])
        result = self.fixture.analyze("Show item amount monthly trend for February 2025 year over year")
        self.assertNotEqual(result["status"], "succeeded")

    def test_each_requested_metric_gets_its_own_period_comparison(self):
        self.timed(["2024-02-01", "2025-01-01", "2025-02-01"])
        model = self.model()
        model["metrics"][0].update(time_field="items.happened", analysis={"unit": "items", "additivity": "additive", "time": {"role": "order_created"}})
        model["metrics"][0]["allowed_dimensions"].append("items.event_date")
        self.save(model)
        result = self.fixture.analyze("Show item count and item amount monthly trend for February 2025 year over year")
        self.assertEqual(result["status"], "succeeded")
        comparisons = result["answer"]["comparisons"]
        self.assertEqual(set(comparisons), {"item_count", "item_amount"})
        self.assertEqual(comparisons["item_count"]["delta"], 0)
        self.assertEqual(comparisons["item_amount"]["delta"], 20.0)
        self.assertEqual(result["final_answer"]["status"], "success")
        self.assertEqual(next(s["outputs"] for s in result["steps"] if s["action"] == "compose_answer")["validation_problems"], [])

    def test_cross_fact_periods_use_each_metrics_event_role(self):
        self.cross_fact()
        self.timed(["2024-02-01", "2025-01-01", "2025-02-01"])
        with sqlite3.connect(self.fixture.database) as db:
            db.execute("ALTER TABLE charges ADD COLUMN charged TEXT")
            db.executemany("UPDATE charges SET charged=? WHERE id=?", [(d, i) for i, d in enumerate(["2024-02-01", "2025-01-01", "2025-02-01"], 1)])
            db.execute("CREATE TABLE calendar (day TEXT PRIMARY KEY, month TEXT)")
            db.executemany("INSERT INTO calendar VALUES (?, ?)", [(d, d[:7]) for d in ["2024-02-01", "2025-01-01", "2025-02-01"]])
        model = self.model()
        model["entities"].append({"name": "calendar", "table": "calendar", "primary_key": ["day"], "dimensions": [{"name": "month", "column": "month"}]})
        for entity, field in (("items", "happened"), ("charges", "charged")):
            model["relationships"].append({"name": entity + "_calendar", "from": entity + "." + field, "to": "calendar.day", "relationship_type": "many_to_one"})
        model["metrics"][1]["allowed_dimensions"].append("calendar.month")
        model["metrics"][-1].update(time_field="charges.charged", analysis={"unit": "USD", "additivity": "additive", "time": {"role": "charge_posted"}})
        model["metrics"][-1]["allowed_dimensions"].append("calendar.month")
        self.save(model)
        result = self.fixture.analyze("Show item amount and charge total monthly trend for February 2025 year over year")
        self.assertEqual(result["status"], "succeeded", result["steps"])
        self.assertEqual(result["answer"]["comparisons"]["item_amount"]["delta"], 20.0)
        self.assertEqual(result["answer"]["comparisons"]["charge_total"]["delta"], -2.0)
        self.assertEqual(result["answer"]["metric_contracts"]["charge_total"]["time"]["role"], "charge_posted")

    def test_bundled_cross_fact_comparison_matches_independent_sql(self):
        from queryforge.application.analysis_planner import AnalysisPlannerService
        sample = Path(__file__).resolve().parents[1] / "sample_data" / "anime_streaming"
        result = AnalysisPlannerService().analyze(
            "Show watch hours and average rating monthly trend for February 2025 year over year",
            database=str(sample / "anime_streaming.sqlite"), semantic_model_path=str(sample / "semantic_model.yml"),
            sql_policy_path=str(sample / "sql_policy.yml"))
        self.assertEqual(result["status"], "succeeded", result["steps"])
        with sqlite3.connect(f"file:{sample / 'anime_streaming.sqlite'}?mode=ro", uri=True) as db:
            hours = dict(db.execute("SELECT watch_date_key / 100, SUM(watch_seconds)/3600.0 FROM fact_watch_session WHERE watch_date_key / 100 IN (202402,202502) GROUP BY watch_date_key / 100"))
            scores = dict(db.execute("SELECT rating_date_key / 100, SUM(score)*1.0/COUNT(*) FROM fact_rating WHERE rating_date_key / 100 IN (202402,202502) GROUP BY rating_date_key / 100"))
        for name, oracle in (("watch_hours", hours), ("average_rating", scores)):
            comparison = result["answer"]["comparisons"][name]
            self.assertAlmostEqual(comparison["baseline"], oracle[202402], places=9)
            self.assertAlmostEqual(comparison["current"], oracle[202502], places=9)
            self.assertAlmostEqual(comparison["delta"], oracle[202502] - oracle[202402], places=9)
        self.assertEqual(result["answer"]["comparisons"]["watch_hours"]["unit"], "hours")
        self.assertEqual(result["answer"]["period_completeness"], "complete")

    def test_event_role_and_timestamp_end_day_are_preserved(self):
        self.timed(["2025-02-28 23:59:59", "2025-03-01", "2024-02-01"])
        result = self.fixture.analyze("Show item amount for February 2025")
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["answer"]["value"], 10.0)
        self.assertEqual(result["answer"]["metric_contracts"]["item_amount"]["time"]["role"], "order_created")

    def test_fixed_timezone_moves_utc_timestamp_into_local_period(self):
        self.timed(["2025-01-31 20:00:00", "2025-02-28 20:00:00", "2024-02-01"], timezone="+08:00")
        result = self.fixture.analyze("Show item amount for February 2025")
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["answer"]["value"], 10.0)

    def test_weekly_monday_buckets_cross_year_correctly(self):
        self.timed(["2024-12-23", "2024-12-30", "2025-01-01"])
        result = self.fixture.analyze("Show item amount weekly trend vs previous period")
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(self.dataset(result)["rows"], [["2024-12-23", 10.0], ["2024-12-30", 50.0]])
        comparison = next(s["outputs"] for s in result["steps"] if s["action"] == "compare_periods")
        self.assertEqual((comparison["baseline"], comparison["current"]), (10.0, 50.0))

    def test_fiscal_quarter_starts_and_month_end_are_correct(self):
        self.timed(["2025-01-31", "2025-03-31", "2025-04-30"], fiscal=4)
        result = self.fixture.analyze("Show item amount quarterly trend qoq")
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(self.dataset(result)["rows"], [["2025-01-01", 30.0], ["2025-04-01", 30.0]])

    def test_fiscal_year_recomputes_before_comparison(self):
        self.timed(["2024-03-31", "2024-04-01", "2025-04-01"], fiscal=4)
        result = self.fixture.analyze("Show item amount yearly trend year over year")
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(self.dataset(result)["rows"], [["2023-04-01", 10.0], ["2024-04-01", 20.0], ["2025-04-01", 30.0]])

    def test_complete_period_contract_allows_full_pair(self):
        self.timed(["2024-02-01", "2025-01-01", "2025-02-01"], complete="2025-02-28", strict=True)
        result = self.fixture.analyze("Show item amount monthly trend for February 2025 year over year")
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["answer"]["period_completeness"], "complete")

    def test_incomplete_current_period_is_blocked(self):
        self.timed(["2024-02-01", "2025-01-01", "2025-02-01"], complete="2025-02-15", strict=True)
        result = self.fixture.analyze("Show item amount monthly trend year over year")
        self.assertNotEqual(result["status"], "succeeded")
        self.assertTrue(any("incomplete_period" in (s.get("error") or "") for s in result["steps"]))

    def test_scalar_window_crossing_coverage_boundary_is_blocked(self):
        self.timed(["2024-02-01", "2025-01-01", "2025-02-01"], complete="2025-02-15", strict=True)
        result = self.fixture.analyze("Show item amount for February 2025", max_replans=0)
        self.assertNotEqual(result["status"], "succeeded")
        self.assertIsNone(result["answer"])

    def test_semi_additive_time_rollup_is_unsupported(self):
        self.timed(["2025-01-01", "2025-01-02", "2025-02-01"])
        model = self.model()
        model["metrics"][1]["analysis"].update(additivity="semi_additive", non_additive_dimensions=["items.event_date"])
        self.save(model)
        result = self.fixture.analyze("Show item amount monthly trend", max_replans=0)
        self.assertNotEqual(result["status"], "succeeded")

    def test_incomplete_baseline_period_is_blocked(self):
        self.timed(["2024-02-01", "2025-01-01", "2025-02-01"], complete="2025-02-28", start="2024-02-15", strict=True)
        result = self.fixture.analyze("Show item amount monthly trend year over year")
        self.assertNotEqual(result["status"], "succeeded")

    def test_missing_period_inside_strict_window_is_blocked(self):
        self.timed(["2025-01-01", "2025-03-01", "2025-03-15"], complete="2025-12-31", strict=True)
        result = self.fixture.analyze("Show item amount monthly trend for Q1 2025")
        self.assertNotEqual(result["status"], "succeeded")
        self.assertTrue(any("missing 2025-02" in (s.get("error") or "") for s in result["steps"]))

    def test_leap_day_yoy_has_no_silent_february_28_substitution(self):
        self.timed(["2023-02-28", "2024-02-28", "2024-02-29"])
        result = self.fixture.analyze("Show item amount daily trend year over year")
        self.assertNotEqual(result["status"], "succeeded")
        self.assertTrue(any("leap day" in (s.get("error") or "") for s in result["steps"]))

    def test_unverified_periods_are_explicit_in_answer(self):
        self.timed(["2024-02-01", "2025-01-01", "2025-02-01"])
        result = self.fixture.analyze("Show item amount monthly trend year over year")
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["answer"]["period_completeness"], "unverified")
        self.assertTrue(any("completeness" in limitation for limitation in result["answer"]["limitations"]))

    def test_time_contract_publication_rejects_invalid_event_values(self):
        self.timed(["bad-date", "2025-01-01", "2025-02-01"])
        report = SemanticContractValidator.validate(self.load().model, self.fixture.database)
        self.assertFalse(report.passed)
        self.assertEqual(report.blocking_failures[0].rule, "time_values")

    def test_invalid_calendar_day_blocks_publication_and_analysis(self):
        self.timed(["2025-02-30", "2025-01-01", "2025-02-01"])
        report = SemanticContractValidator.validate(self.load().model, self.fixture.database)
        self.assertFalse(report.passed)
        result = self.fixture.analyze("Show item amount for February 2025", max_replans=0)
        self.assertNotEqual(result["status"], "succeeded")
        self.assertTrue(any("unverified_time_values" in (s.get("error") or "") for s in result["steps"]))

    def test_legacy_models_load_without_new_contracts(self):
        loaded = self.load()
        self.assertIsNone(loaded.model.metrics[0].analysis)
        self.assertNotIn("analysis", loaded.model.metrics[0].model_dump(exclude_none=True))

    def test_invalid_contracts_fail_authoring(self):
        base = self.model()["metrics"][1]
        cases = [
            {"unit": "USD", "additivity": "semi_additive"},
            {"unit": "USD", "additivity": "additive", "time": {"role": "created", "timezone": "America/New_York"}},
            {"unit": "USD", "additivity": "additive", "time": {"role": "created", "require_complete_periods": True}},
            {"unit": "USD", "additivity": "additive", "time": {"role": "created", "coverage_start": "2025-01-01", "complete_through": "2024-01-01"}},
        ]
        for contract in cases:
            with self.subTest(contract=contract), self.assertRaises(ValidationError):
                SemanticMetric.model_validate({**base, "analysis": contract})

    def test_bad_ratio_population_and_wrong_expression_fail_authoring(self):
        for numerator in ("SUM(items.amount) / COUNT(items.item_id)", "AVG(items.amount)", "(SELECT SUM(items.amount) FROM items)", "SUM(amount)", "SUM(items.amount); SELECT 1", "SUM(items.amount) -- ignored"):
            with self.subTest(numerator=numerator), self.assertRaises(ValidationError):
                SemanticMetric.model_validate({"name": "x", "description": "x", "entity": "items", "aggregation": "ratio", "ratio": {"numerator": numerator, "denominator": "COUNT(*)"}})
        with self.assertRaises(ValidationError):
            SemanticMetric.model_validate({"name": "x", "description": "x", "entity": "items", "aggregation": "ratio", "expression": "SUM(items.amount) / 2", "ratio": {"numerator": "SUM(items.amount)", "denominator": "COUNT(*)"}})

    def test_calendar_bounds_include_leap_month_and_fiscal_year(self):
        self.assertEqual(period_bounds(date(2024, 2, 15), "monthly"), (date(2024, 2, 1), date(2024, 2, 29)))
        self.timed(["2025-01-01", "2025-01-02", "2025-01-03"], fiscal=4)
        temporal = self.load().model.metrics[1].analysis.time
        self.assertEqual(period_bounds(date(2025, 1, 31), "yearly", temporal), (date(2024, 4, 1), date(2025, 3, 31)))

    def test_changed_time_and_ratio_contracts_are_detected_as_drift(self):
        from scripts.check_semantic_drift import semantic_snapshot, compare_snapshots
        before, _ = semantic_snapshot(self.fixture.database, self.fixture.semantic_model)
        self.ratio()
        after, _ = semantic_snapshot(self.fixture.database, self.fixture.semantic_model)
        report = compare_snapshots(before, after)
        self.assertNotEqual(before["fingerprint"], after["fingerprint"])
        self.assertEqual(report[0]["section"], "metrics")
        self.assertEqual(report[0]["added"], ["mean_amount"])

    def test_changed_units_and_time_roles_are_detected_as_drift(self):
        from scripts.check_semantic_drift import semantic_snapshot, compare_snapshots
        self.timed(["2025-01-01", "2025-01-02", "2025-01-03"])
        before, _ = semantic_snapshot(self.fixture.database, self.fixture.semantic_model)
        model = self.model()
        model["metrics"][1]["analysis"]["unit"] = "CNY"
        model["metrics"][1]["analysis"]["time"]["role"] = "payment_settled"
        self.save(model)
        after, _ = semantic_snapshot(self.fixture.database, self.fixture.semantic_model)
        self.assertEqual(compare_snapshots(before, after), [{"section": "metrics", "added": [], "removed": [], "changed": ["item_amount"]}])

    def test_resume_recomputes_when_ratio_contract_changes_at_same_version(self):
        self.ratio()
        result = self.fixture.analyze("Show mean amount", run_id="ratio-contract")
        self.assertEqual(result["answer"]["value"], 20.0)
        journal = self.fixture.root / ".queryforge" / "runs" / "ratio-contract" / "execution.json"
        state = json.loads(journal.read_text())
        state.update(status="running", terminal_outcome=None, terminal_at=None)
        journal.write_text(json.dumps(state))
        model = self.model()
        model["metrics"][-1]["ratio"]["scale"] = 100.0
        model["metrics"][-1]["analysis"]["unit"] = "cents/item"
        self.save(model)
        resumed = self.fixture.analyze("Show mean amount", run_id="ratio-contract", resume=True)
        self.assertEqual(resumed["status"], "succeeded")
        self.assertEqual(resumed["answer"]["value"], 2000.0)
        self.assertEqual(resumed["answer"]["metric_contracts"]["mean_amount"]["unit"], "cents/item")
        self.assertIn("query_metric", resumed["recomputed_steps"])
        self.assertEqual(resumed["reused_steps"], [])

    def test_resume_recomputes_when_relative_window_resolves_differently(self):
        self.timed(["2024-02-01", "2025-01-01", "2025-02-01"])
        january = DateRange(expression="last month", start_date="2025-01-01", end_date="2025-01-31")
        february = DateRange(expression="last month", start_date="2025-02-01", end_date="2025-02-28")
        with patch("queryforge.workflow.node.date_parser_node.DateParserNode.parse_rules", return_value=[january]):
            first = self.fixture.analyze("Show item amount last month", run_id="relative-contract")
        self.assertEqual(first["answer"]["value"], 20.0)
        journal = self.fixture.root / ".queryforge" / "runs" / "relative-contract" / "execution.json"
        state = json.loads(journal.read_text())
        state.update(status="running", terminal_outcome=None, terminal_at=None)
        journal.write_text(json.dumps(state))
        with patch("queryforge.workflow.node.date_parser_node.DateParserNode.parse_rules", return_value=[february]):
            resumed = self.fixture.analyze("Show item amount last month", run_id="relative-contract", resume=True)
        self.assertEqual(resumed["answer"]["value"], 30.0)
        self.assertEqual(resumed["reused_steps"], [])

    def test_cli_ratio_retains_components_and_unit(self):
        self.ratio()
        from queryforge.cli import main
        with patch("queryforge.application.analysis_planner.AnalysisPlannerService", return_value=self.fixture.service()), patch("sys.argv", ["queryforge", "--analyze", "--question", "Show mean amount",
             "--database", str(self.fixture.database), "--semantic-model", str(self.fixture.semantic_model)]):
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = main()
        self.assertEqual(code, 0)
        answer = json.loads(output.getvalue())["answer"]
        self.assertEqual(answer["value"], 20.0)
        self.assertIn("mean_amount", answer["ratio_components"])
        self.assertEqual(answer["metric_contracts"]["mean_amount"]["unit"], "USD/item")

    @unittest.skipUnless(importlib.util.find_spec("fastapi") and importlib.util.find_spec("httpx"), "API extras absent")
    def test_rest_cross_fact_delivery_contains_all_governed_metrics(self):
        self.cross_fact()
        from fastapi.testclient import TestClient
        from queryforge.application import AgentService
        from queryforge.interfaces.api.app import create_app
        client = TestClient(create_app(AgentService(config_loader=lambda **_: self.fixture.config())))
        response = client.post("/analyze", json={"question": "Show item amount and charge total by brand_name"})
        self.assertEqual(response.status_code, 200)
        answer = response.json()["answer"]
        self.assertEqual(set(answer["metrics"]), {"item_amount", "charge_total"})
        self.assertEqual(answer["metric_contracts"]["charge_total"]["unit"], "USD")
        self.assertTrue(answer["evidence_ids"])


if __name__ == "__main__":
    unittest.main()
