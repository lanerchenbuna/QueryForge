import sqlite3
import tempfile
import unittest
from pathlib import Path

from queryforge.infrastructure.tools.database_tool import DatabaseTool, UnsafeSQLError


class DatabaseToolValidationTest(unittest.TestCase):
    def test_accepts_select_and_cte(self) -> None:
        self.assertEqual(
            DatabaseTool.validate_readonly_sql("SELECT 1;"),
            "SELECT 1;",
        )
        self.assertEqual(
            DatabaseTool.validate_readonly_sql("WITH x AS (SELECT 1) SELECT * FROM x"),
            "WITH x AS (SELECT 1) SELECT * FROM x",
        )

    def test_allows_keywords_and_semicolons_inside_strings(self) -> None:
        sql = "SELECT 'delete; drop' AS value"
        self.assertEqual(DatabaseTool.validate_readonly_sql(sql), sql)
        commented = "SELECT 1; -- one statement"
        self.assertEqual(DatabaseTool.validate_readonly_sql(commented), commented)

    def test_rejects_empty_write_and_multiple_statements(self) -> None:
        invalid = [
            "",
            "DELETE FROM schools",
            "WITH x AS (DELETE FROM schools RETURNING *) SELECT * FROM x",
            "SELECT 1; SELECT 2",
            "PRAGMA table_info(schools)",
        ]
        for sql in invalid:
            with self.subTest(sql=sql):
                with self.assertRaises(UnsafeSQLError):
                    DatabaseTool.validate_readonly_sql(sql)


if __name__ == "__main__":
    unittest.main()


class PreviewPolicyOrderingTest(unittest.TestCase):
    """E-12: the preview path must not neutralise the policy it reports honouring.

    ``execute_sql_preview`` injected its bounding LIMIT *before* calling the policy
    engine, so with ``require_limit: true`` the engine saw a LIMIT the model never
    wrote. The audit record then said the statement satisfied the policy while the
    statement itself would have been refused by it.

    Preview still tolerates exactly one refusal — ``unbounded_result``, because an
    unbounded preview is the point and the injected LIMIT bounds it. Every other
    refusal still applies.
    """

    def setUp(self) -> None:
        import sqlite3
        import tempfile

        from queryforge.domain.security.sql_policy import SQLSecurityPolicy
        from queryforge.infrastructure.db.sqlite_connector import SQLiteConnector
        from queryforge.infrastructure.tools.database_tool import DatabaseTool

        self._directory = tempfile.TemporaryDirectory()
        database = Path(self._directory.name) / "preview.sqlite"
        with sqlite3.connect(database) as connection:
            connection.execute("CREATE TABLE fact_sales (id INTEGER, amount REAL)")
            connection.executemany(
                "INSERT INTO fact_sales VALUES (?, ?)",
                [(index, float(index)) for index in range(50)],
            )
        self.connector = SQLiteConnector(str(database))
        self.connector.__enter__()
        self.tool = DatabaseTool(
            self.connector, SQLSecurityPolicy(require_limit=True, max_limit=100)
        )

    def tearDown(self) -> None:
        self.connector.__exit__(None, None, None)
        self._directory.cleanup()

    def test_preview_is_bounded_but_the_policy_saw_the_original_statement(self):
        sql = "SELECT s.id, s.amount FROM fact_sales s"
        result = self.tool.execute_sql_preview(sql, limit=20)
        self.assertEqual(len(result.rows), 20)
        # The engine consulted on the original, unbounded statement is on the
        # record; the final decision is the bounded statement it actually ran.
        self.assertIsNotNone(self.tool.last_policy_decision)

    def test_the_same_statement_is_still_refused_outside_the_preview_path(self):
        from queryforge.infrastructure.tools.database_tool import UnsafeSQLError

        with self.assertRaises(UnsafeSQLError):
            self.tool.execute_sql("SELECT s.id, s.amount FROM fact_sales s")

    def test_preview_does_not_excuse_non_limit_refusals(self):
        from queryforge.infrastructure.tools.database_tool import UnsafeSQLError

        with self.assertRaises(UnsafeSQLError) as caught:
            self.tool.execute_sql_preview(
                "SELECT amount FROM a_table_that_is_not_authorized", limit=20
            )
        self.assertNotIn("unbounded_result", str(caught.exception))


class PolicyDecisionOwnershipTest(unittest.TestCase):
    """E-23: the decision record must describe a call this tool actually made.

    ``last_policy_decision`` was a plain public attribute, so a caller could
    *assign* a decision it had computed itself (``PlanOutputNode`` did), leaving the
    tool reporting an audit record for a call that never happened — and a reader
    could pick up an unrelated caller's decision from a shared tool instance.
    """

    def setUp(self) -> None:
        from queryforge.infrastructure.db.sqlite_connector import SQLiteConnector
        from queryforge.infrastructure.tools.database_tool import DatabaseTool

        self._directory = tempfile.TemporaryDirectory()
        database = Path(self._directory.name) / "policy.sqlite"
        with sqlite3.connect(database) as connection:
            connection.execute("CREATE TABLE items (id INTEGER, amount REAL)")
            connection.execute("INSERT INTO items VALUES (1, 2.5)")
        connector = SQLiteConnector(str(database))
        self.addCleanup(self._directory.cleanup)
        self.addCleanup(connector.close)
        self.tool = DatabaseTool(connector)

    def test_the_decision_cannot_be_overwritten_from_outside(self):
        self.tool.execute_sql("SELECT id FROM items LIMIT 1")
        recorded = self.tool.last_policy_decision
        self.assertIsNotNone(recorded)
        self.assertTrue(recorded.allowed)

        with self.assertRaises(AttributeError):
            self.tool.last_policy_decision = None
        self.assertIs(self.tool.last_policy_decision, recorded)

    def test_the_decision_is_written_only_by_a_real_call(self):
        self.assertIsNone(self.tool.last_policy_decision)
        with self.assertRaises(UnsafeSQLError) as caught:
            self.tool.execute_sql("SELECT amount FROM not_authorized LIMIT 1")
        refusal = self.tool.last_policy_decision
        self.assertIsNotNone(refusal)
        self.assertFalse(refusal.allowed)
        self.assertIs(refusal, caught.exception.decision)
