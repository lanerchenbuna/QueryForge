"""Diagnosis outcomes checked against independent SQLite populations."""
import contextlib
import io
import json
import sqlite3
import unittest
from unittest.mock import patch

import yaml

from tests.test_analysis_planner import Fixture
from queryforge.domain.analysis.diagnostics import reconcile_buckets
from queryforge.orchestration.planner import AnalysisPlan


QUESTION = "Explain item amount monthly year over year by category"


class AnalysisDiagnosticsTest(unittest.TestCase):
    def setUp(self):
        self.fixture = Fixture()
        with sqlite3.connect(self.fixture.database) as db:
            db.execute("ALTER TABLE items ADD COLUMN happened TEXT")
            db.execute("DELETE FROM items")
            db.executemany("INSERT INTO items VALUES (?,?,?,?,?,?)", [
                (1, "baseline a", "a", 30, 1, "2024-02-05"),
                (2, "baseline b", "b", 20, 2, "2024-02-10"),
                (3, "current a", "a", 10, 1, "2025-02-05"),
                (4, "current b", "b", 50, 2, "2025-02-10"),
                (5, "history", "a", 10, 1, "2024-01-01"),
                (6, "history", "a", 15, 1, "2025-01-01"),
            ])
        model = yaml.safe_load(self.fixture.semantic_model.read_text())
        model["entities"][0]["dimensions"].append({"name": "event_date", "column": "happened"})
        for metric in model["metrics"]:
            metric.update(time_field="items.happened", analysis={
                "unit": "USD" if metric["aggregation"] == "sum" else "rows", "additivity": "additive",
                "time": {"role": "sale", "coverage_start": "2024-01-01", "complete_through": "2025-12-31", "require_complete_periods": True},
            })
            metric["allowed_dimensions"].append("items.event_date")
        self.save(model)

    def tearDown(self):
        self.fixture.cleanup()

    def save(self, model):
        self.fixture.semantic_model.write_text(yaml.safe_dump(model))

    def analyze(self, question=QUESTION, **kwargs):
        return self.fixture.analyze(question, max_replans=0, **kwargs)

    @staticmethod
    def output(payload, step_id):
        return next(s["outputs"] for s in payload["steps"] if s["step_id"] == step_id)

    def oracle(self):
        with sqlite3.connect(f"file:{self.fixture.database}?mode=ro", uri=True) as db:
            return db.execute("SELECT substr(happened,1,7), category, SUM(amount) FROM items "
                              "WHERE substr(happened,1,7) IN ('2024-02','2025-02') GROUP BY 1,2 ORDER BY 1,2").fetchall()

    def test_independent_totals_and_offsetting_contributions_reconcile(self):
        result = self.analyze()
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(self.oracle(), [("2024-02", "a", 30.0), ("2024-02", "b", 20.0), ("2025-02", "a", 10.0), ("2025-02", "b", 50.0)])
        diagnosis = result["answer"]["diagnosis"]
        part = diagnosis["contributions"]["item_amount"]
        self.assertEqual((part["independent_current"], part["independent_baseline"], part["total_delta"]), (60, 50, 10))
        by_category = {x["category"]: x for x in part["contributions"]}
        self.assertEqual((by_category['["a"]']["delta"], by_category['["b"]']["delta"]), (-20, 30))
        self.assertEqual((by_category['["a"]']["share_pct"], by_category['["b"]']["share_pct"]), (-200, 300))
        self.assertEqual((part["residual_current"], part["residual_baseline"], part["residual"]), (0, 0, 0))
        self.assertTrue(part["residual_explained"])
        self.assertEqual(part["unit"], "USD")
        self.assertEqual(diagnosis["quality"]["status"], "ok")
        self.assertTrue(diagnosis["quality"]["time_checks"])
        self.assertEqual(diagnosis["period_completeness"], "complete")
        self.assertIn("not_causal", diagnosis["interpretation"])
        known = {e.get("evidence_id", e.get("id")) for e in result["evidence"]}
        self.assertEqual(len(diagnosis["evidence_ids"]), 6)
        self.assertTrue(set(diagnosis["evidence_ids"]).issubset(known))
        self.assertEqual(result["final_answer"]["status"], "success")

    def test_every_requested_metric_has_independent_diagnosis(self):
        result = self.analyze("Explain item count and item amount monthly year over year by category")
        self.assertEqual(result["status"], "succeeded")
        parts = result["answer"]["diagnosis"]["contributions"]
        self.assertEqual(set(parts), {"item_count", "item_amount"})
        self.assertEqual(parts["item_count"]["total_delta"], 0)
        self.assertEqual(parts["item_amount"]["total_delta"], 10)
        self.assertEqual(set(result["answer"]["diagnosis"]["anomalies"]), set(parts))
        self.assertEqual(result["final_answer"]["status"], "success")

    def test_missing_category_is_zero_only_with_complete_coverage(self):
        with sqlite3.connect(self.fixture.database) as db:
            db.execute("UPDATE items SET category='new' WHERE item_id=4")
        result = self.analyze()
        self.assertEqual(result["status"], "succeeded")
        part = result["answer"]["diagnosis"]["contributions"]["item_amount"]
        self.assertEqual(len(part["empty_buckets"]), 2)
        self.assertEqual(part["total_delta"], 10)
        model = yaml.safe_load(self.fixture.semantic_model.read_text())
        model["metrics"][1]["analysis"]["time"]["require_complete_periods"] = False
        self.save(model)
        blocked = self.analyze()
        self.assertNotEqual(blocked["status"], "succeeded")
        self.assertIsNone(blocked["answer"])
        self.assertTrue(any("unverified_empty_bucket" in (s.get("error") or "") for s in blocked["steps"]))

    def test_null_category_remains_a_distinct_auditable_bucket(self):
        with sqlite3.connect(self.fixture.database) as db:
            db.execute("UPDATE items SET category=NULL WHERE category='a'")
        result = self.analyze()
        self.assertEqual(result["status"], "succeeded")
        part = result["answer"]["diagnosis"]["contributions"]["item_amount"]
        self.assertTrue(any(x["category"] == "[null]" for x in part["contributions"]))
        self.assertEqual(part["total_delta"], 10)

    def test_orphan_dimension_rows_cannot_hide_behind_zero_delta_residual(self):
        with sqlite3.connect(self.fixture.database) as db:
            db.execute("INSERT INTO brands VALUES (1,'a')")
            db.execute("UPDATE items SET amount=20 WHERE item_id=4")
        model = yaml.safe_load(self.fixture.semantic_model.read_text())
        model["metrics"][1]["allowed_dimensions"].append("brands.brand_name")
        self.save(model)
        result = self.analyze("Explain item amount monthly year over year by brand_name")
        self.assertNotEqual(result["status"], "succeeded")
        self.assertIsNone(result["answer"])
        self.assertTrue(any("independent period totals" in (s.get("error") or "") for s in result["steps"]))

    def test_zero_total_change_keeps_opposing_drivers_without_percentage(self):
        with sqlite3.connect(self.fixture.database) as db:
            db.execute("UPDATE items SET amount=40 WHERE item_id=4")
        result = self.analyze()
        self.assertEqual(result["status"], "succeeded")
        part = result["answer"]["diagnosis"]["contributions"]["item_amount"]
        self.assertEqual(part["total_delta"], 0)
        self.assertEqual(sorted(c["delta"] for c in part["contributions"]), [-20, 20])
        self.assertTrue(all(c["share_pct"] is None for c in part["contributions"]))

    def test_dimension_specific_non_additivity_refuses_contribution(self):
        model = yaml.safe_load(self.fixture.semantic_model.read_text())
        model["metrics"][1]["analysis"]["non_additive_dimensions"] = ["items.category"]
        self.save(model)
        result = self.analyze()
        self.assertNotEqual(result["status"], "succeeded")
        self.assertIsNone(result["answer"])

    def test_non_additive_metric_is_not_summed_into_drivers(self):
        model = yaml.safe_load(self.fixture.semantic_model.read_text())
        model["metrics"][1].update(aggregation="ratio", expression="CAST(SUM(items.amount) AS REAL) / COUNT(*)")
        model["metrics"][1]["analysis"]["additivity"] = "non_additive"
        self.save(model)
        result = self.analyze()
        self.assertNotEqual(result["status"], "succeeded")
        self.assertIsNone(result["answer"])

    def test_query_cap_cannot_produce_complete_contribution(self):
        plan = AnalysisPlan.model_validate(self.analyze()["plan"])
        plan.step("query_metric").inputs["limit"] = 2
        result = self.analyze(plan=plan)
        self.assertNotEqual(result["status"], "succeeded")
        self.assertIsNone(result["answer"])

    def test_insufficient_anomaly_history_is_an_explicit_limitation(self):
        result = self.analyze("Explain item amount monthly year over year by category for February 2025")
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["answer"]["diagnosis"]["anomalies"]["item_amount"]["state"], "insufficient_data")
        self.assertTrue(any("insufficient_data" in x for x in result["answer"]["limitations"]))

    def test_resume_preserves_distinct_total_and_bucket_evidence(self):
        first = self.analyze(run_id="diagnostic-resume")
        # Simulate a crash after steps were recorded, before terminal commit.
        journal = self.fixture.root / ".queryforge" / "runs" / "diagnostic-resume" / "execution.json"
        record = json.loads(journal.read_text())
        record.update(status="running", terminal_outcome=None, terminal_at=None)
        journal.write_text(json.dumps(record))
        resumed = self.analyze(run_id="diagnostic-resume", resume=True)
        self.assertEqual(first["status"], resumed["status"])
        self.assertEqual(first["answer"]["diagnosis"], resumed["answer"]["diagnosis"])
        self.assertIn("query_totals", resumed["reused_steps"])
        self.assertIn("query_metric", resumed["reused_steps"])

    def test_custom_plan_cannot_omit_requested_diagnosis(self):
        preview = self.analyze()
        plan = AnalysisPlan.model_validate(preview["plan"])
        plan.steps = [s for s in plan.steps if s.action not in {"calculate_contribution", "detect_anomaly"}]
        compose = plan.steps[-1]
        compose.depends_on = ["query_metric", "compare_periods"]
        compose.validation["require_evidence"] = ["metric_resolution", "metric_value", "period_comparison", "data_quality", "metric_totals"]
        result = self.analyze(plan=plan)
        self.assertNotEqual(result["status"], "succeeded")
        self.assertTrue(any("request_incomplete: diagnosis" in (s.get("error") or "") for s in result["steps"]))

    def test_cli_delivers_diagnosis_and_anchored_evidence(self):
        from queryforge.cli import main
        with patch("queryforge.application.analysis_planner.AnalysisPlannerService", return_value=self.fixture.service()), patch("sys.argv", [
            "queryforge", "--analyze", "--question", QUESTION, "--database", str(self.fixture.database),
            "--semantic-model", str(self.fixture.semantic_model),
        ]):
            stream = io.StringIO()
            with contextlib.redirect_stdout(stream):
                code = main()
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(stream.getvalue())["answer"]["diagnosis"]["contributions"]["item_amount"]["total_delta"], 10)

    def test_reconciliation_rejects_duplicate_and_mismatched_populations(self):
        result = self.analyze()
        buckets, totals = self.output(result, "query_metric"), self.output(result, "query_totals")
        with self.assertRaisesRegex(ValueError, "different populations"):
            reconcile_buckets({**buckets, "filters": ["items.amount>0"]}, totals, "item_amount", "2025-02", "2024-02")
        with self.assertRaisesRegex(ValueError, "duplicate category"):
            reconcile_buckets({**buckets, "rows": buckets["rows"] + [buckets["rows"][-1]]}, totals, "item_amount", "2025-02", "2024-02")

    def test_rest_delivers_the_same_reconciled_diagnosis(self):
        from fastapi.testclient import TestClient
        from queryforge.application import AgentService
        from queryforge.interfaces.api.app import create_app
        config = self.fixture.config()
        with TestClient(create_app(AgentService(config_loader=lambda **_: config))) as client:
            response = client.post("/analyze", json={"question": QUESTION, "database": str(self.fixture.database),
                                                   "semantic_model_path": str(self.fixture.semantic_model)})
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["status"], "succeeded")
        self.assertEqual(payload["final_answer"]["status"], "success")
        self.assertEqual(payload["answer"]["diagnosis"]["contributions"]["item_amount"]["total_delta"], 10)


class DiagnosticGoldTest(unittest.TestCase):
    def test_frozen_gold_matches_independent_sql_and_all_schemas_execute(self):
        import tempfile
        from pathlib import Path
        from queryforge.evaluation import load_spec_splits
        from queryforge.evaluation.isolation import audit_splits, audit_dataset_schemas
        from scripts import benchmark_agent as harness
        from scripts.freeze_diagnostic_tasks import build_tasks
        root = Path(__file__).resolve().parents[1]
        specs = load_spec_splits(root / "evaluation/diagnostics")
        regenerated = build_tasks()
        for spec in specs:
            self.assertEqual(spec.model_dump(exclude_unset=True), regenerated[spec.split])
        audit_splits(specs)
        self.assertEqual(len(set(audit_dataset_schemas(specs, harness.load_datasets(), root).values())), 3)
        with tempfile.TemporaryDirectory() as directory:
            outcomes, traces = harness.run_suite(specs, harness.BenchmarkContext(harness.load_datasets(), Path(directory)))
        self.assertEqual(len(outcomes), 3)
        self.assertTrue(all(o.passed for o in outcomes), [(o.task_id, o.checks) for o in outcomes if not o.passed])
        for trace in traces:
            diagnosis = trace["payload"]["answer"]["diagnosis"]
            self.assertTrue(all(a["state"] == "insufficient_data" for a in diagnosis["anomalies"].values()))


if __name__ == "__main__":
    unittest.main()
