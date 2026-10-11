"""Mutation regressions for independent physical SQL source coverage."""
import sqlite3
import unittest

from queryforge.evaluation.sources import check_sql_sources


class SourceCoverageTest(unittest.TestCase):
    def test_coincidentally_equal_values_cannot_hide_wrong_source(self):
        with sqlite3.connect(":memory:") as db:
            db.executescript("CREATE TABLE fact_order (date_key INT);"
                             "CREATE TABLE dim_date (date_key INT);"
                             "INSERT INTO fact_order VALUES (1),(10);"
                             "INSERT INTO dim_date VALUES (1),(5),(10);")
            right = "SELECT MIN(date_key), MAX(date_key) FROM fact_order"
            wrong = "SELECT MIN(date_key), MAX(date_key) FROM dim_date"
            self.assertEqual(db.execute(right).fetchall(), db.execute(wrong).fetchall())
            self.assertTrue(check_sql_sources(right, ["fact_order"]).passed)
            result = check_sql_sources(wrong, ["fact_order"])
            self.assertTrue(result.verified)
            self.assertFalse(result.passed)
            self.assertEqual(result.missing_tables, ("fact_order",))
            db.execute("INSERT INTO dim_date VALUES (20)")
            self.assertNotEqual(db.execute(right).fetchall(), db.execute(wrong).fetchall())

    def test_unused_cte_does_not_establish_a_source(self):
        sql = "WITH proof AS (SELECT * FROM fact_order) SELECT * FROM dim_date"
        result = check_sql_sources(sql, ["fact_order"])
        self.assertFalse(result.passed)
        self.assertEqual(result.active_tables, ("dim_date",))

    def test_alias_cannot_impersonate_a_physical_source(self):
        sql = "SELECT * FROM dim_date AS fact_order"
        self.assertFalse(check_sql_sources(sql, ["fact_order"]).passed)

    def test_cte_shadowing_uses_physical_source_with_sqlite_case_rules(self):
        sql = 'WITH "fact_order" AS (SELECT * FROM dim_date) SELECT * FROM "FACT_ORDER"'
        result = check_sql_sources(sql, ["fact_order"])
        self.assertFalse(result.passed)
        self.assertEqual(result.active_tables, ("dim_date",))

    def test_active_cte_and_derived_table_are_followed(self):
        sql = "WITH F AS (SELECT * FROM FACT_ORDER) SELECT * FROM (SELECT * FROM f) t"
        self.assertTrue(check_sql_sources(sql, ["fact_order"]).passed)

    def test_scalar_subqueries_cover_both_independent_counts(self):
        sql = "SELECT (SELECT COUNT(*) FROM fact_order), (SELECT COUNT(*) FROM fact_order_line)"
        self.assertTrue(check_sql_sources(sql, ["fact_order", "fact_order_line"]).passed)

    def test_all_set_operation_branches_are_followed(self):
        sql = "SELECT date_key FROM fact_order UNION SELECT date_key FROM dim_date"
        self.assertTrue(check_sql_sources(sql, ["fact_order", "dim_date"]).passed)

    def test_main_namespace_and_case_are_supported(self):
        self.assertTrue(check_sql_sources('SELECT * FROM main."FACT_ORDER"', ["fact_order"]).passed)

    def test_unsupported_namespaces_are_unverified(self):
        for sql in ("SELECT * FROM other.fact_order", "SELECT * FROM temp.fact_order"):
            with self.subTest(sql=sql):
                result = check_sql_sources(sql, ["fact_order"])
                self.assertFalse(result.verified)
                self.assertFalse(result.passed)

    def test_missing_and_invalid_sql_are_unverified(self):
        for sql in (None, False, "", "SELECT FROM", "SELECT * FROM fact_order; SELECT 1"):
            with self.subTest(sql=sql):
                result = check_sql_sources(sql, ["fact_order"])
                self.assertFalse(result.verified)
                self.assertFalse(result.passed)

    def test_nonquery_and_unsupported_sources_are_unverified(self):
        for sql in ("DELETE FROM fact_order", "SELECT * FROM json_each('[]')",
                    "SELECT * FROM (VALUES (1)) AS t"):
            with self.subTest(sql=sql):
                result = check_sql_sources(sql, ["fact_order"])
                self.assertFalse(result.verified)
                self.assertFalse(result.passed)

    def test_recursive_cte_is_unverified(self):
        sql = "WITH RECURSIVE f AS (SELECT * FROM fact_order UNION ALL SELECT * FROM f) SELECT * FROM f"
        self.assertFalse(check_sql_sources(sql, ["fact_order"]).verified)

    def test_unicode_case_folding_cannot_impersonate_a_fixture_table(self):
        with sqlite3.connect(":memory:") as db:
            db.executescript('CREATE TABLE "K" (n INT); CREATE TABLE k (n INT);'
                             'INSERT INTO "K" VALUES (1); INSERT INTO k VALUES (2);')
            self.assertNotEqual(db.execute('SELECT n FROM "K"').fetchall(),
                                db.execute('SELECT n FROM k').fetchall())
        self.assertFalse(check_sql_sources('SELECT n FROM "K"', ['k']).verified)
        self.assertFalse(check_sql_sources('SELECT n FROM k', ['K']).verified)


if __name__ == "__main__":
    unittest.main()
