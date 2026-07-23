import unittest

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
