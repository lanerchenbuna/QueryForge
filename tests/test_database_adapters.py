"""Real engine contract tests; CI installs duckdb and rejects unexpected skips."""
import importlib.util
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from queryforge.infrastructure.db.adapters import open_database
from queryforge.infrastructure.tools.database_tool import DatabaseTool, UnsafeSQLError
from queryforge.orchestration.tools.budget import install_sql_deadline_handler


class DefaultAdapterTest(unittest.TestCase):
    def test_sqlite_does_not_import_optional_driver(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d)/"test.sqlite"
            sqlite3.connect(p).close()
            with patch.dict('sys.modules', {'duckdb': None}):
                with open_database(str(p)) as c:
                    self.assertEqual(c.execute_sql('SELECT 1').rows, [[1]])


@unittest.skipUnless(importlib.util.find_spec("duckdb"), "install .[duckdb] for real backend contract")
class DuckDBAdapterTest(unittest.TestCase):
    def setUp(self):
        import duckdb
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.paths = [Path(self.temp.name)/"test.sqlite", Path(self.temp.name)/"test.duckdb"]
        for driver,p in zip([sqlite3, duckdb],self.paths):
            c=driver.connect(str(p))
            c.execute('CREATE TABLE items (id INTEGER PRIMARY KEY, category VARCHAR, amount INTEGER)')
            c.executemany('INSERT INTO items VALUES (?,?,?)',[(1,'a',10),(2,'b',20),(3,'a',30)])
            c.commit();c.close()

    def test_portable_queries_preview_and_explain(self):
        queries=[
            'SELECT category,SUM(amount) FROM items GROUP BY category ORDER BY category',
            'WITH a AS (SELECT id,amount FROM items) SELECT id, SUM(amount) OVER(ORDER BY id) FROM a ORDER BY id',
            'SELECT CAST(SUM(amount) AS REAL)/NULLIF(COUNT(*),0) FROM items',
            'SELECT id FROM items WHERE id<0',
        ]
        with open_database(str(self.paths[0])) as a, open_database(str(self.paths[1])) as b:
            for q in queries:
                self.assertEqual(DatabaseTool(a).execute_sql(q).rows, DatabaseTool(b).execute_sql(q).rows)
            self.assertEqual(b.describe_table('items').columns[0].primary_key,True)
            self.assertEqual(b.find_matching_values('items','category',['a']),['a'])
            # Inner LIMIT must not suppress the outer preview limit.
            q='WITH a AS (SELECT * FROM items LIMIT 3) SELECT * FROM a'
            self.assertEqual(DatabaseTool(b).execute_sql_preview(q,1).row_count,1)
            self.assertTrue(b.explain('SELECT id FROM items').rows)

    def test_engine_and_ast_write_and_external_access_denied(self):
        with open_database(str(self.paths[1])) as c:
            for sql in ['DELETE FROM items','CREATE TABLE hacked(x INTEGER)',"ATTACH '/tmp/other.duckdb' AS other"]:
                with self.assertRaises(UnsafeSQLError): DatabaseTool(c).execute_sql(sql)
                with self.assertRaises(Exception): c.execute_sql(sql)
            for sql in ["SELECT * FROM read_csv('/etc/passwd')",'SELECT * FROM information_schema.tables',
                        'SELECT * FROM other.items']:
                with self.assertRaises(UnsafeSQLError): DatabaseTool(c).execute_sql(sql)
            with self.assertRaises(Exception): c.execute_sql("SELECT * FROM read_text('/etc/passwd')")
            self.assertEqual(c.execute_sql('SELECT COUNT(*) FROM items').rows,[[3]])

    def test_types_quoting_and_unsupported_values(self):
        with open_database(str(self.paths[1])) as c:
            r=c.execute_sql('SELECT CAST(123456789012.34 AS DECIMAL(18,2)) AS "select", NULL, '
                            "DATE '2024-02-29', TIMESTAMPTZ '2024-01-01 08:00:00+08'")
            self.assertEqual(r.rows, [['123456789012.34',None,'2024-02-29','2024-01-01T00:00:00+00:00']])
            with self.assertRaises(Exception): c.execute_sql("SELECT [1,2]")
            with self.assertRaises(Exception): c.execute_sql("SELECT strftime('%Y', '2024-01-01')")

    def test_deadline_actually_interrupts_engine_and_connection_recovers(self):
        with open_database(str(self.paths[1])) as c:
            start=time.monotonic()
            guard=install_sql_deadline_handler(c._connection, start+0.05)
            try:
                with self.assertRaisesRegex(Exception,'Interrupt'):
                    c.execute_sql('SELECT SUM(a.amount*b.amount) FROM items a CROSS JOIN range(10000000000) b(amount)')
            finally:
                guard.restore()
            self.assertLess(time.monotonic()-start,2)
            self.assertEqual(c.execute_sql('SELECT 1').rows,[[1]])

    def test_client_cancel_and_no_connection_state_shared(self):
        with open_database(str(self.paths[1])) as c:
            timer=threading.Timer(.05,c.cancel);timer.start()
            try:
                with self.assertRaisesRegex(Exception,'Interrupt'):
                    c.execute_sql('SELECT SUM(i) FROM range(10000000000) x(i)')
            finally:
                timer.cancel();timer.join()
        with open_database(str(self.paths[0])) as c:
            self.assertEqual(c.execute_sql('SELECT COUNT(*) FROM items').rows,[[3]])
        # A closed adapter never reuses another domain's connection.
        with self.assertRaises(Exception): c.execute_sql('SELECT 1')

    def test_planner_uses_real_duckdb_main_path(self):
        from queryforge.application.analysis_planner import AnalysisPlannerService
        from queryforge.core.config import Config
        from tests.test_analysis_planner import SINGULAR_SEMANTIC_MODEL
        p=Path(self.temp.name)/'semantic.yml';p.write_text(SINGULAR_SEMANTIC_MODEL)
        config=Config('offline',None,'fixture',None,str(self.paths[1]),semantic_model_path=str(p))
        payload=AnalysisPlannerService(config_loader=lambda:config).analyze('item_count')
        self.assertEqual(payload['status'],'succeeded',payload)
        self.assertEqual(payload['answer']['value'],3)
