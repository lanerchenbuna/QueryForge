import logging
import sqlite3
import tempfile
import unittest
from io import StringIO
from pathlib import Path

from queryforge.infrastructure.db.sqlite_connector import SQLiteConnector
from queryforge.core.observability import run_logging_context
from queryforge.domain.security import SQLSecurityPolicy
from queryforge.workflow.workflow import WorkflowError
from queryforge.core.config import Config
from queryforge.application import AgentOptions, AgentService
from queryforge.infrastructure.tools.database_tool import DatabaseTool, UnsafeSQLError


class SQLSecurityPolicyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.database = Path(self.temporary.name) / "policy.sqlite"
        connection = sqlite3.connect(self.database)
        connection.executescript(
            """
            CREATE TABLE items (id INTEGER PRIMARY KEY, name TEXT, secret TEXT);
            CREATE TABLE audit_log (id INTEGER PRIMARY KEY, payload TEXT);
            INSERT INTO items VALUES (1, 'alpha', 'hidden');
            INSERT INTO audit_log VALUES (1, 'private');
            """
        )
        connection.commit()
        connection.close()
        self.policy = SQLSecurityPolicy(
            name="test_analyst",
            allowed_tables=["items"],
            allowed_columns={"items": ["id", "name"]},
            dangerous_functions=["randomblob"],
            require_limit=True,
            max_limit=10,
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def tool(self, connector: SQLiteConnector) -> DatabaseTool:
        return DatabaseTool(connector, self.policy)

    def test_allowed_query_runs_and_schema_is_policy_filtered(self) -> None:
        with SQLiteConnector(str(self.database)) as connector:
            tool = self.tool(connector)
            self.assertEqual(tool.list_tables(), ["items"])
            self.assertEqual(
                [column.name for column in tool.describe_table("items").columns],
                ["id", "name"],
            )
            result = tool.execute_sql("SELECT name FROM items ORDER BY name LIMIT 10")
        self.assertEqual(result.rows, [["alpha"]])
        self.assertTrue(tool.last_policy_decision.allowed)
        self.assertEqual(tool.last_policy_decision.rule, "allow")

    def test_table_column_and_star_scope_are_rejected(self) -> None:
        invalid = {
            "SELECT payload FROM audit_log LIMIT 1": "table_scope",
            "SELECT secret FROM items LIMIT 1": "column_scope",
            "SELECT * FROM items LIMIT 1": "column_scope_star",
        }
        with SQLiteConnector(str(self.database)) as connector:
            tool = self.tool(connector)
            for sql, rule in invalid.items():
                with self.subTest(rule=rule):
                    with self.assertRaisesRegex(UnsafeSQLError, f"rule={rule}"):
                        tool.execute_sql(sql)

    def test_functions_recursive_cte_writes_and_cost_are_rejected(self) -> None:
        invalid = {
            "SELECT load_extension('x') LIMIT 1": "dangerous_function",
            "SELECT randomblob(8) LIMIT 1": "dangerous_function",
            (
                "WITH RECURSIVE x(n) AS (SELECT 1 UNION ALL "
                "SELECT n + 1 FROM x) SELECT n FROM x LIMIT 5"
            ): "recursive_cte",
            "DELETE FROM items": "read_only_ast",
            "SELECT name FROM items": "unbounded_result",
            "SELECT name FROM items LIMIT 11": "max_limit",
            "SELECT name FROM items LIMIT ?": "limit_literal",
        }
        with SQLiteConnector(str(self.database)) as connector:
            tool = self.tool(connector)
            for sql, rule in invalid.items():
                with self.subTest(rule=rule):
                    with self.assertRaisesRegex(UnsafeSQLError, f"rule={rule}"):
                        tool.execute_sql(sql)

    def test_shape_budgets_reject_cross_join_and_excessive_tables(self) -> None:
        policy = SQLSecurityPolicy(
            allowed_tables=["items", "audit_log"],
            max_joins=0,
            max_tables=1,
            allow_cross_join=False,
        )
        with SQLiteConnector(str(self.database)) as connector:
            tool = DatabaseTool(connector, policy)
            with self.assertRaisesRegex(UnsafeSQLError, "rule=max_tables"):
                tool.execute_sql(
                    "SELECT items.name FROM items JOIN audit_log ON 1 = 1"
                )
            with self.assertRaisesRegex(UnsafeSQLError, "rule=cross_join"):
                DatabaseTool(
                    connector,
                    SQLSecurityPolicy(
                        allowed_tables=["items", "audit_log"],
                        allow_cross_join=False,
                    ),
                ).execute_sql("SELECT items.name FROM items CROSS JOIN audit_log")

    def test_scalar_aggregate_is_intrinsically_bounded(self) -> None:
        with SQLiteConnector(str(self.database)) as connector:
            result = self.tool(connector).execute_sql(
                "SELECT COUNT(*) AS item_count FROM items"
            )
        self.assertEqual(result.rows, [[1]])

    def test_rejection_contains_run_id_rule_and_audit_log(self) -> None:
        stream = StringIO()
        handler = logging.StreamHandler(stream)
        logger = logging.getLogger("queryforge.domain.security")
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        try:
            with SQLiteConnector(str(self.database)) as connector:
                tool = self.tool(connector)
                with run_logging_context("qf_security_test"):
                    with self.assertRaises(UnsafeSQLError) as captured:
                        tool.execute_sql("SELECT secret FROM items LIMIT 1")
            message = str(captured.exception)
            self.assertIn("run_id=qf_security_test", message)
            self.assertIn("rule=column_scope", message)
            self.assertIn("sql_policy_decision allowed=False", stream.getvalue())
        finally:
            logger.removeHandler(handler)

    def test_workflow_security_rejection_is_terminal_and_keeps_run_id(self) -> None:
        policy_path = Path(self.temporary.name) / "strict.yml"
        policy_path.write_text(
            """version: 1
name: workflow_strict
allowed_tables: [items]
allowed_columns:
  items: [id, name]
require_limit: true
max_limit: 10
""",
            encoding="utf-8",
        )

        class UnsafeColumnLLM:
            def generate_json(self, prompt):
                if "Select local QueryForge skills" in prompt:
                    return {"skills": [], "reason": "none"}
                return {
                    "sql": "SELECT secret FROM items LIMIT 1",
                    "explanation": "Attempt a hidden column.",
                    "tables_used": ["items"],
                }

        config = Config(
            llm_provider="openai",
            llm_api_key=None,
            llm_model="unsafe-test",
            llm_base_url=None,
            database_path=str(self.database),
            history_db_path=str(Path(self.temporary.name) / "history.sqlite"),
        )
        service = AgentService(
            config_loader=lambda **_: config,
            llm_factory=lambda _: UnsafeColumnLLM(),
        )
        with self.assertRaises(WorkflowError) as captured:
            service.ask(
                "Show secrets",
                AgentOptions(
                    database=str(self.database),
                    sql_policy_path=str(policy_path),
                    skills=[],
                    max_retries=3,
                    run_id="qf_terminal_security",
                ),
            )
        message = str(captured.exception)
        self.assertIn("run_id=qf_terminal_security", message)
        self.assertIn("rule=column_scope", message)
        self.assertEqual(captured.exception.context.retry_count, 0)
        self.assertIsNone(captured.exception.context.execution_result)


if __name__ == "__main__":
    unittest.main()
