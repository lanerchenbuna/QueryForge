"""Step-08 tests: budgeted, read-only runtime data-quality checks."""

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from queryforge.core.schemas.models import (
    Context,
    DateContext,
    DateRange,
    ExecutionResult,
    SQLContext,
    SqlTask,
)
from queryforge.domain.security import SQLSecurityPolicy
from queryforge.domain.semantic import SemanticModelLoader
from queryforge.infrastructure.db.sqlite_connector import SQLiteConnector
from queryforge.infrastructure.tools.data_quality_tool import (
    DataQualityBudget,
    DataQualityTool,
)
from queryforge.infrastructure.tools.database_tool import DatabaseTool
from queryforge.orchestration.agents.data_qa import (
    DataQAAgent,
    open_policy_filtered_database_tool,
)
from queryforge.orchestration.runtime.state_store import AgentTeamStateStore
from queryforge.orchestration.schemas import RoutingDecision, TaskState


PROJECT_ROOT = Path(__file__).resolve().parents[1]

USERS = (
    "CREATE TABLE users (user_id INTEGER PRIMARY KEY, name TEXT, signup_date TEXT)"
)
EVENTS = (
    "CREATE TABLE events ("
    "event_id INTEGER NOT NULL, user_id INTEGER, amount REAL, "
    "event_date TEXT NOT NULL, secret TEXT)"
)
EVENT_ROWS = [
    (1, 1, 10.0, "2025-01-01", "s1"),
    (1, 1, None, "2025-01-02", "s2"),
    (2, 2, 20.0, "2025-01-02", "s3"),
    (2, 999, 30.0, "2025-01-05", "s4"),
    (3, 3, 40.0, "2025-01-05", "s5"),
]


class FakeClock:
    """Deterministic monotonic clock for timeout tests."""

    def __init__(self, values: list[float]) -> None:
        self._values = list(values)
        self._last = self._values[-1] if self._values else 0.0

    def __call__(self) -> float:
        if self._values:
            self._last = self._values.pop(0)
        return self._last


class DataQualityToolTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.database = self.root / "quality.sqlite"
        connection = sqlite3.connect(self.database)
        connection.execute(USERS)
        connection.execute(EVENTS)
        connection.execute("CREATE TABLE empty_table (id INTEGER PRIMARY KEY, value REAL)")
        connection.execute(
            "CREATE TABLE all_null (id INTEGER PRIMARY KEY, value REAL)"
        )
        connection.executemany("INSERT INTO users VALUES (?, ?, ?)", [
            (1, "ada", "2025-01-01"),
            (2, "bob", "2025-01-02"),
            (3, "cy", "2025-01-03"),
        ])
        connection.executemany("INSERT INTO events VALUES (?, ?, ?, ?, ?)", EVENT_ROWS)
        connection.execute("INSERT INTO all_null VALUES (1, NULL)")
        connection.commit()
        connection.close()

    def tearDown(self) -> None:
        self.directory.cleanup()

    def tool(self, **kwargs) -> DataQualityTool:
        connector = SQLiteConnector(str(self.database))
        self.addCleanup(connector.close)
        return DataQualityTool(DatabaseTool(connector), **kwargs)

    def test_grain_duplicates_are_detected(self) -> None:
        report = self.tool().check("events", ["grain_unique"], grain_columns=["event_id"])
        check = report.checks[0]
        self.assertEqual(check.status, "error")
        self.assertEqual(check.reason, "grain_not_unique")
        self.assertEqual(check.evidence["duplicate_groups"], 2)
        self.assertEqual(check.evidence["null_key_rows"], 0)
        self.assertFalse(check.evidence["bounded"])
        self.assertTrue(report.errors)
        self.assertTrue(report.to_payload()["blocking"])

        duplicates = self.tool().check(
            "events", ["duplicates"], grain_columns=["event_id"]
        ).checks[0]
        self.assertEqual(duplicates.status, "warning")
        self.assertEqual(duplicates.evidence["duplicate_groups"], 2)

    def test_grain_unique_passes_for_unique_primary_key(self) -> None:
        report = self.tool().check("users", ["grain_unique"])
        check = report.checks[0]
        self.assertEqual(check.status, "ok", check.reason)
        self.assertEqual(check.evidence["grain_columns"], ["user_id"])
        self.assertEqual(check.evidence["sampled_rows"], 3)

    def test_null_rate_evidence_matches_hand_counted_fixture(self) -> None:
        report = self.tool().check("events", ["null_rate"], columns=["amount", "user_id"])
        check = report.checks[0]
        # 5 rows sampled, one NULL amount -> 0.2 > default 0.05 threshold.
        self.assertEqual(check.status, "warning")
        self.assertEqual(check.evidence["columns"]["amount"], {
            "sampled": 5, "nulls": 1, "ratio": 0.2,
        })
        self.assertEqual(check.evidence["columns"]["user_id"], {
            "sampled": 5, "nulls": 0, "ratio": 0.0,
        })
        strict = self.tool().check(
            "events", ["null_rate"], columns=["amount"], max_null_rate=0.0
        ).checks[0]
        self.assertEqual(strict.status, "warning")

    def test_entirely_null_column_is_an_error_and_empty_table_is_unknown(self) -> None:
        nulls = self.tool().check("all_null", ["null_rate"], columns=["value"]).checks[0]
        self.assertEqual(nulls.status, "error")
        self.assertEqual(nulls.reason, "column_entirely_null")

        empty = self.tool().check("empty_table", ["grain_unique"]).checks[0]
        self.assertEqual(empty.status, "unknown")
        self.assertEqual(empty.reason, "empty_table")

    def test_freshness_requires_expected_date_and_compares_lag(self) -> None:
        missing = self.tool().check(
            "events", ["freshness"], time_field="event_date"
        ).checks[0]
        self.assertEqual(missing.status, "unknown")
        self.assertEqual(missing.reason, "missing_expected_max_date")

        current = self.tool().check(
            "events", ["freshness"], time_field="event_date",
            expected_max_date="2025-01-05",
        ).checks[0]
        self.assertEqual(current.status, "ok")
        self.assertEqual(current.evidence["lag_days"], 0)
        self.assertEqual(current.evidence["time_semantics"], "event_time")
        self.assertFalse(current.evidence["ingestion_time_available"])

        tolerance = self.tool().check(
            "events", ["freshness"], time_field="event_date",
            expected_max_date="2025-01-06",
        ).checks[0]
        self.assertEqual(tolerance.status, "warning")
        self.assertEqual(tolerance.evidence["lag_days"], 1)

        stale = self.tool().check(
            "events", ["freshness"], time_field="event_date",
            expected_max_date="2025-01-08",
        ).checks[0]
        self.assertEqual(stale.status, "error")
        self.assertEqual(stale.reason, "stale_event_data")

        no_column = self.tool().check(
            "events", ["freshness"], time_field="missing_column",
            expected_max_date="2025-01-05",
        ).checks[0]
        self.assertEqual(no_column.status, "unknown")
        self.assertIn("column_not_visible", no_column.reason)

    def test_coverage_counts_missing_days_in_window(self) -> None:
        report = self.tool().check(
            "events",
            ["coverage"],
            time_field="event_date",
            window=("2025-01-01", "2025-01-05"),
        )
        check = report.checks[0]
        self.assertEqual(check.status, "warning")
        self.assertEqual(check.reason, "missing_days_in_window")
        self.assertEqual(check.evidence["expected_days"], 5)
        self.assertEqual(check.evidence["observed_days"], 3)
        self.assertEqual(check.evidence["missing_days"], 2)

        empty_window = self.tool().check(
            "events",
            ["coverage"],
            time_field="event_date",
            window=("2025-02-01", "2025-02-05"),
        ).checks[0]
        self.assertEqual(empty_window.status, "error")
        self.assertEqual(empty_window.reason, "no_data_in_window")

        missing_window = self.tool().check(
            "events", ["coverage"], time_field="event_date"
        ).checks[0]
        self.assertEqual(missing_window.status, "unknown")
        self.assertEqual(missing_window.reason, "missing_window")

    def test_referential_orphans_are_counted(self) -> None:
        report = self.tool().check(
            "events", ["referential"], referenced=("users", "user_id")
        )
        check = report.checks[0]
        self.assertEqual(check.status, "error")
        self.assertEqual(check.reason, "orphan_foreign_keys")
        self.assertEqual(check.evidence["orphan_count"], 1)
        self.assertEqual(check.evidence["column"], "user_id")
        self.assertFalse(check.evidence["bounded"])

        clean = self.tool().check(
            "users", ["referential"], referenced=("users", "user_id")
        ).checks[0]
        self.assertEqual(clean.status, "ok")
        self.assertEqual(clean.evidence["orphan_count"], 0)

    def test_hidden_columns_are_not_accessible_through_the_policy(self) -> None:
        connector = SQLiteConnector(str(self.database))
        self.addCleanup(connector.close)
        policy = SQLSecurityPolicy(
            name="quality_test",
            allowed_tables=["events"],
            allowed_columns={
                "events": ["event_id", "user_id", "amount", "event_date"],
            },
        )
        tool = DataQualityTool(DatabaseTool(connector, policy))
        check = tool.check("events", ["null_rate"], columns=["secret"]).checks[0]
        self.assertEqual(check.status, "unknown")
        self.assertIn("column_not_visible", check.reason)
        # the requested column name is reported, but no hidden value ever leaks.
        rendered = json.dumps(check.evidence)
        for value in ("s1", "s2", "s3", "s4", "s5"):
            self.assertNotIn(value, rendered)

        mixed = tool.check(
            "events", ["null_rate"], columns=["secret", "amount"]
        ).checks[0]
        self.assertEqual(mixed.status, "warning")
        self.assertEqual(mixed.evidence["columns_skipped"], ["secret"])
        self.assertNotIn("secret", mixed.evidence["columns"])
        denied_table = tool.check("users", ["grain_unique"]).checks[0]
        self.assertEqual(denied_table.status, "unknown")
        self.assertIn("policy_denied", denied_table.reason)

    def test_timeout_path_reports_unknown_and_never_ok(self) -> None:
        tool = self.tool(
            budget=DataQualityBudget(timeout_seconds=1.0),
            clock=FakeClock([100.0, 102.0]),
        )
        report = tool.check(
            "events", ["grain_unique", "null_rate"], grain_columns=["event_id"]
        )
        self.assertEqual([check.status for check in report.checks], ["unknown", "unknown"])
        self.assertEqual({check.reason for check in report.checks}, {"timeout"})
        self.assertEqual(report.status, "unknown")
        self.assertFalse(report.to_payload()["blocking"])

    def test_checks_never_write_to_the_database(self) -> None:
        before_stat = self.database.stat()
        before_files = sorted(path.name for path in self.root.iterdir())
        before_bytes = self.database.read_bytes()
        connector = SQLiteConnector(str(self.database))
        self.addCleanup(connector.close)
        tool = DataQualityTool(DatabaseTool(connector))
        payload = tool.report(
            [
                ("events", ["grain_unique", "duplicates"], {"grain_columns": ["event_id"]}),
                ("events", ["null_rate", "coverage"], {
                    "columns": ["amount"],
                    "time_field": "event_date",
                    "window": ("2025-01-01", "2025-01-05"),
                }),
                ("events", ["referential"], {"referenced": ("users", "user_id")}),
                ("users", ["grain_unique", "freshness"], {
                    "time_field": "signup_date",
                    "expected_max_date": "2025-01-03",
                }),
            ]
        )
        self.assertGreaterEqual(len(payload["checks"]), 6)
        self.assertEqual(
            payload["counts"]["ok"] + payload["counts"]["warning"]
            + payload["counts"]["error"] + payload["counts"]["unknown"],
            len(payload["checks"]),
        )
        self.assertTrue(payload["blocking"])
        after_stat = self.database.stat()
        self.assertEqual(before_stat.st_mtime_ns, after_stat.st_mtime_ns)
        self.assertEqual(before_stat.st_size, after_stat.st_size)
        self.assertEqual(before_bytes, self.database.read_bytes())
        self.assertEqual(
            sorted(path.name for path in self.root.iterdir()), before_files
        )

    def test_report_summarizes_requests_and_is_deterministic(self) -> None:
        requests = [
            ("users", ["grain_unique", "null_rate"], {"columns": ["name"]}),
            ("events", ["duplicates"], {"grain_columns": ["event_id"]}),
        ]
        first = self.tool().report(requests)
        second = self.tool().report(requests)
        self.assertEqual(first, second)
        self.assertEqual(
            {entry["table"] for entry in first["checks"]}, {"users", "events"}
        )
        self.assertEqual(
            [(entry["table"], entry["check"]) for entry in first["checks"]],
            [("users", "grain_unique"), ("users", "null_rate"), ("events", "duplicates")],
        )
        self.assertEqual(first["status"], "warning")
        self.assertFalse(first["blocking"])
        for entry in first["checks"]:
            self.assertIn("status", entry)
            self.assertIn("evidence", entry)

    def test_unsupported_check_is_unknown_with_reason(self) -> None:
        report = self.tool().check("users", ["not_a_check"])
        self.assertEqual(report.checks[0].status, "unknown")
        self.assertIn("unsupported_check", report.checks[0].reason)


QUALITY_SEMANTIC_MODEL = """version: 1
name: quality_fixture
entities:
  - name: event
    table: events
    entity_type: fact
    primary_key: [event_id]
    grain: [event_id]
    dimensions:
      - name: date
        column: event_date
metrics:
  - name: event_amount
    description: Total event amount.
    entity: event
    aggregation: sum
    expression: SUM(events.amount)
    synonyms: [event amount, total amount]
    allowed_dimensions: []
    time_field: events.event_date
"""


class DataQAAgentQualityTest(unittest.TestCase):
    """Step 08: the QA artifact carries runtime quality evidence for the task."""

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.database = self.root / "quality.sqlite"
        connection = sqlite3.connect(self.database)
        connection.execute(EVENTS)
        connection.executemany("INSERT INTO events VALUES (?, ?, ?, ?, ?)", EVENT_ROWS)
        connection.commit()
        connection.close()
        self.model_path = self.root / "semantic.yml"
        self.model_path.write_text(QUALITY_SEMANTIC_MODEL, encoding="utf-8")
        self.state_store = AgentTeamStateStore(root=self.root / "runs")

    def tearDown(self) -> None:
        self.directory.cleanup()

    def build_context(self, question: str, with_window: bool) -> Context:
        with SQLiteConnector(str(self.database)) as connector:
            tool = DatabaseTool(connector)
            schemas = [tool.describe_table(name) for name in tool.list_tables()]
        context = Context(
            task=SqlTask(question=question, database_path=str(self.database))
        )
        context.semantic_model = SemanticModelLoader.load_and_validate(
            self.model_path, schemas, question
        )
        context.metric_matches = SemanticModelLoader.match_metrics(
            context.semantic_model.model, question
        )
        context.sql_context = SQLContext(sql="SELECT 1", explanation="fixture")
        context.execution_result = ExecutionResult(columns=["c"], rows=[[1]], row_count=1)
        if with_window:
            context.date_context = DateContext(
                reference_date="2025-01-05",
                source="rule",
                ranges=[
                    DateRange(
                        expression="last five days",
                        start_date="2025-01-01",
                        end_date="2025-01-05",
                    )
                ],
            )
        return context

    def run_agent(self, context: Context) -> dict:
        state = TaskState(
            run_id="quality_run",
            entrypoint="cli",
            classification=RoutingDecision(
                task_type="ask_sql",
                entrypoint="cli",
                confidence=0.9,
                reason="fixture",
                pipeline="standard",
            ),
            status="running",
            current_phase="completion",
            pending_phases=["completion"],
        )
        self.state_store.initialize(state)
        DataQAAgent(self.state_store).run(state, context)
        reference = next(
            artifact for artifact in state.artifacts
            if artifact.artifact_type == "qa_report"
        )
        document = json.loads(
            (self.state_store.run_dir(state.run_id) / reference.path).read_text(
                encoding="utf-8"
            )
        )
        return document["payload"]

    def test_quality_checks_are_merged_and_errors_block_the_report(self) -> None:
        context = self.build_context("How much event amount is there?", False)
        payload = self.run_agent(context)
        checks = payload["quality_checks"]
        self.assertTrue(checks)
        self.assertEqual(
            [(check["table"], check["check"]) for check in checks],
            [
                ("events", "grain_unique"),
                ("events", "duplicates"),
                ("events", "null_rate"),
            ],
        )
        grain = next(check for check in checks if check["check"] == "grain_unique")
        self.assertEqual(grain["status"], "error")
        self.assertEqual(grain["evidence"]["duplicate_groups"], 2)
        self.assertFalse(payload["passed"])
        self.assertEqual(payload["quality_status"], "error")
        blocking_rules = {
            issue["rule"] for issue in payload["issues"] if issue["severity"] == "error"
        }
        self.assertIn("data_quality_grain_unique", blocking_rules)
        stored = context.task_context["data_quality"]
        self.assertEqual(stored["counts"]["error"], 1)

    def test_quality_unavailable_degrades_instead_of_failing_the_run(self) -> None:
        context = self.build_context("How much event amount is there?", False)
        context.task.database_path = str(self.root / "missing.sqlite")
        payload = self.run_agent(context)
        self.assertEqual(payload["quality_checks"], [])
        # an unavailable quality tool is unknown, never a silent pass.
        self.assertEqual(payload["quality_status"], "unknown")
        self.assertIn("quality_tool_unavailable", payload["quality_reason"])
        self.assertFalse(
            [issue for issue in payload["issues"] if issue["severity"] == "error"]
        )

    def test_complete_window_reports_ok_and_time_checks_follow_the_window(self) -> None:
        connection = sqlite3.connect(self.database)
        connection.execute("DELETE FROM events")
        connection.executemany(
            "INSERT INTO events VALUES (?, ?, ?, ?, ?)",
            [
                (index, index, float(index) * 10, f"2025-01-0{index}", f"s{index}")
                for index in range(1, 6)
            ],
        )
        connection.commit()
        connection.close()
        context = self.build_context(
            "How much event amount is there last five days?", True
        )
        payload = self.run_agent(context)
        checks = {check["check"]: check for check in payload["quality_checks"]}
        self.assertEqual(
            set(checks),
            {"grain_unique", "duplicates", "null_rate", "freshness", "coverage"},
        )
        self.assertEqual(checks["grain_unique"]["status"], "ok")
        self.assertEqual(checks["freshness"]["status"], "ok")
        self.assertEqual(checks["coverage"]["status"], "ok")
        self.assertEqual(checks["coverage"]["evidence"]["missing_days"], 0)
        self.assertEqual(
            checks["freshness"]["evidence"]["expected_max_date"], "2025-01-05"
        )
        self.assertEqual(payload["quality_status"], "ok")
        self.assertFalse(
            [
                issue
                for issue in payload["issues"]
                if issue["rule"].startswith("data_quality_")
            ]
        )

    def test_partial_window_is_a_warning_not_a_silent_business_decline(self) -> None:
        context = self.build_context(
            "How much event amount is there last five days?", True
        )
        payload = self.run_agent(context)
        checks = {check["check"]: check for check in payload["quality_checks"]}
        coverage = checks["coverage"]
        self.assertEqual(coverage["evidence"]["expected_days"], 5)
        self.assertEqual(coverage["evidence"]["observed_days"], 3)
        self.assertEqual(coverage["evidence"]["missing_days"], 2)
        self.assertIn(coverage["status"], {"warning", "error"})
        self.assertEqual(payload["quality_counts"]["ok"] >= 1, True)


class PolicyFilteredQualityToolTest(unittest.TestCase):
    """Step 08-S1: quality checks cannot widen the run's column scope."""

    def test_agent_rebuilds_the_same_policy_scope_before_checking(self) -> None:
        database = PROJECT_ROOT / "sample_data/anime_streaming/anime_streaming.sqlite"
        policy_path = PROJECT_ROOT / "sample_data/anime_streaming/sql_policy.yml"
        self.assertTrue(database.is_file())
        self.assertTrue(policy_path.is_file())
        context = Context(task=SqlTask(question="viewer region", database_path=str(database)))
        context.sql_policy = {
            "status": "active",
            "name": "anime_streaming_analyst",
            "version": 1,
            "source_path": str(policy_path),
            "table_scope": None,
            "column_scope": {"dim_user": ["user_id", "region"]},
        }
        with open_policy_filtered_database_tool(context) as tool:
            self.assertEqual(tool.policy_summary["name"], "anime_streaming_analyst")
            visible = {column.name for column in tool.describe_table("dim_user").columns}
            self.assertNotIn("email", visible)
            self.assertIn("region", visible)
            report = DataQualityTool(tool).check(
                "dim_user", ["null_rate"], columns=["email"]
            )
            self.assertEqual(report.checks[0].status, "unknown")
            self.assertIn("column_not_visible", report.checks[0].reason)


if __name__ == "__main__":
    unittest.main()
