import importlib.util
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path("scripts/check_semantic_drift.py").resolve()
SPEC = importlib.util.spec_from_file_location("check_semantic_drift", SCRIPT)
assert SPEC and SPEC.loader
drift = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(drift)


class SemanticDriftTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.database = self.root / "events.sqlite"
        self.model = self.root / "events.semantic.yml"
        self.baseline = self.root / "semantic_baseline.json"
        self.report = self.root / "semantic_report.json"
        with sqlite3.connect(self.database) as connection:
            connection.executescript(
                """
                CREATE TABLE fact_event (
                    event_id INTEGER PRIMARY KEY,
                    duration_seconds INTEGER NOT NULL
                );
                INSERT INTO fact_event VALUES (1, 120), (2, 240);
                """
            )
        self.model.write_text(
            """version: 1
name: events
entities:
  - name: event
    table: fact_event
    entity_type: fact
    grain: [event_id]
    expected_columns: [event_id, duration_seconds]
    allow_additive_columns: false
    dimensions:
      - {name: duration, column: duration_seconds}
metrics:
  - name: event_count
    description: Count of events.
    entity: event
    aggregation: count
    expression: COUNT(DISTINCT fact_event.event_id)
""",
            encoding="utf-8",
        )

    def tearDown(self):
        self.directory.cleanup()

    def update_baseline(self):
        return drift.run_check(
            self.database,
            self.model,
            self.baseline,
            self.report,
            update_baseline=True,
        )

    def test_reviewed_baseline_has_no_drift(self):
        self.assertEqual(self.update_baseline()["status"], "baseline_updated")
        result = drift.run_check(
            self.database,
            self.model,
            self.baseline,
            self.report,
        )
        self.assertEqual(result["status"], "no_change")
        self.assertTrue(result["contract"]["passed"])

    def test_schema_drift_is_reported_by_table(self):
        self.update_baseline()
        with sqlite3.connect(self.database) as connection:
            connection.execute("ALTER TABLE fact_event ADD COLUMN device TEXT")
        result = drift.run_check(
            self.database,
            self.model,
            self.baseline,
            self.report,
        )
        self.assertEqual(result["status"], "contract_failed")
        self.assertEqual(result["changes"][0]["section"], "schema")
        self.assertEqual(result["changes"][0]["changed"], ["fact_event"])

    def test_metric_change_is_detected(self):
        self.update_baseline()
        text = self.model.read_text(encoding="utf-8").replace(
            "Count of events.",
            "Canonical count of uploaded events.",
        )
        self.model.write_text(text, encoding="utf-8")
        result = drift.run_check(
            self.database,
            self.model,
            self.baseline,
            self.report,
        )
        self.assertEqual(result["status"], "drift_detected")
        self.assertEqual(result["changes"][0]["section"], "metrics")
        self.assertEqual(result["changes"][0]["changed"], ["event_count"])

    def test_quality_failure_blocks_baseline_update(self):
        with sqlite3.connect(self.database) as connection:
            connection.execute(
                "CREATE TABLE bad_event (event_id INTEGER, duration_seconds INTEGER)"
            )
            connection.execute("INSERT INTO bad_event VALUES (1, 1), (1, 2)")
        self.model.write_text(
            self.model.read_text(encoding="utf-8").replace(
                "fact_event",
                "bad_event",
            ),
            encoding="utf-8",
        )
        with self.assertRaisesRegex(ValueError, "quality contracts fail"):
            drift.run_check(
                self.database,
                self.model,
                self.baseline,
                self.report,
                update_baseline=True,
            )


if __name__ == "__main__":
    unittest.main()
