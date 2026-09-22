"""Step 18 conformance suite: one adapter contract, two real engines.

Why this module exists
----------------------
The optimization plan requires an explicit Adapter Contract *before* a second
backend, verified against real engines. Nothing here is mocked: the SQLite half
uses ``sqlite3``, the DuckDB half uses the optional ``duckdb`` driver, and every
assertion compares real query output, real engine refusals and real interrupt
behaviour.

Evidence produced
-----------------
* 18-N1  the same logical fixture (fact + dimension + date column, 360 rows) is
         built in both backends and answers the same count/sum/group-by/join/
         window/CTE/date-range/empty queries identically after normalization.
* 18-S1  write and admin SQL is refused by the AST policy layer *and* by the
         engine's read-only role; the fixture is proven unchanged afterwards.
* 18-R1  the SQLite default path keeps working while the duckdb driver is made
         unimportable (monkeypatched ``sys.modules``/``find_spec``) and the
         adapter package never imports that driver eagerly.
* capabilities are exercised in both directions: declared-supported features must
         actually run, declared-unsupported features must be refused instead of
         being silently mistranslated.
* a deadline and a client cancel both interrupt in-flight engine work quickly.
* EXPLAIN returns a plan for both backends; an unknown table/column produces one
         typed contract error class from both backends.
* type conversion keeps dates, decimals, booleans, bytes and NULL comparable.

Runtime: a few seconds; temp directories only, no network, no repository writes.
"""

import importlib.util
import json
import os
import re
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from queryforge.infrastructure.db import (
    CAPABILITY_REGISTRY,
    DATE_FUNCTION_VOCABULARY,
    AdapterCancelledError,
    AdapterCapabilities,
    AdapterPolicyError,
    AdapterQueryError,
    AdapterTimeoutError,
    AdapterTypeError,
    AdapterUnavailableError,
    AdapterUnsupportedError,
    DatabaseAdapter,
    DuckDBConnector,
    SQLiteConnector,
    adapt_connector,
    capabilities_for_dialect,
    normalize_type,
    normalize_value,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DUCKDB_AVAILABLE = importlib.util.find_spec("duckdb") is not None
DUCKDB_SKIP_REASON = (
    "optional DuckDB backend: install the 'duckdb' extra and run this module in "
    "the tier-2 integration job"
)

# --------------------------------------------------------------------------- #
# Deterministic fixture (same logical dataset for both engines)
# --------------------------------------------------------------------------- #

CATEGORIES = (
    (1, "electronics", "north"),
    (2, "books", "north"),
    (3, "toys", "south"),
    (4, "garden", "west"),
    (5, "music", "east"),
    (6, "sports", "west"),
)
FACT_ROWS = 360
STRESS_ROWS = 3000
DISCOUNT_STEPS = (Decimal("0.00"), Decimal("0.25"), Decimal("0.50"), Decimal("0.75"), Decimal("1.00"))

DDL = (
    "CREATE TABLE dim_category ("
    "category_id INTEGER PRIMARY KEY, "
    "category_name TEXT NOT NULL, "
    "region TEXT NOT NULL)",
    "CREATE TABLE fact_orders ("
    "order_id INTEGER PRIMARY KEY, "
    "category_id INTEGER NOT NULL, "
    "order_date DATE NOT NULL, "
    "amount INTEGER NOT NULL, "
    "discount DECIMAL(12,2) NOT NULL, "
    "is_returned BOOLEAN NOT NULL, "
    "payload BLOB, "
    "note TEXT)",
    "CREATE TABLE stress_rows (id INTEGER NOT NULL, value INTEGER NOT NULL)",
)

#: Probe row used by the type-conversion assertions (kept explicit so the
#: fixture generator itself is covered by a hardcoded oracle).
PROBE_ORDER_ID = 11
PROBE_ROW = {
    "order_id": 11,
    "category_id": 6,
    "order_date": "2024-03-18",
    "amount": 417,
    "discount": Decimal("0.75"),
    "is_returned": True,
    "payload_hex": "0b21",
    "note": "note-11",
}


def category_rows() -> list[tuple]:
    return list(CATEGORIES)


def fact_rows() -> list[tuple]:
    """Build the fact table rows from a closed-form, seed-free formula."""
    rows: list[tuple] = []
    for index in range(1, FACT_ROWS + 1):
        order_date = date(2024, 1, 1) + timedelta(days=(index * 7) % 300)
        rows.append(
            (
                index,
                1 + (index % 6),
                order_date.isoformat(),
                10 + (index * 37) % 500,
                DISCOUNT_STEPS[(index * 13) % 5],
                index % 11 == 0,
                bytes(((index % 251), (index * 3) % 251)),
                None if index % 7 == 0 else f"note-{index}",
            )
        )
    return rows


def stress_rows() -> list[tuple]:
    return [(index, (index * 7) % 1000) for index in range(1, STRESS_ROWS + 1)]


def build_sqlite_fixture(database: Path) -> None:
    """Create the fixture with the same DDL as DuckDB, using SQLite bindings.

    SQLite has no BOOLEAN/DECIMAL storage classes: booleans are stored as 0/1 and
    decimals as REAL. The contract normalization has to reconcile exactly that,
    so the fixture keeps the difference instead of hiding it with casts.
    """
    connection = sqlite3.connect(database)
    try:
        for statement in DDL:
            connection.execute(statement)
        connection.executemany(
            "INSERT INTO dim_category VALUES (?, ?, ?)", category_rows()
        )
        connection.executemany(
            "INSERT INTO fact_orders VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    order_id,
                    category_id,
                    order_date,
                    amount,
                    float(discount),
                    int(returned),
                    payload,
                    note,
                )
                for (
                    order_id,
                    category_id,
                    order_date,
                    amount,
                    discount,
                    returned,
                    payload,
                    note,
                ) in fact_rows()
            ],
        )
        connection.executemany(
            "INSERT INTO stress_rows VALUES (?, ?)", stress_rows()
        )
        connection.commit()
    finally:
        connection.close()


def build_duckdb_fixture(database: Path) -> None:
    """Create the same fixture in DuckDB with native types (DATE/DECIMAL/BOOLEAN)."""
    import duckdb  # local import: this module must import without the driver

    connection = duckdb.connect(str(database))
    try:
        for statement in DDL:
            connection.execute(statement)
        connection.executemany("INSERT INTO dim_category VALUES (?, ?, ?)", category_rows())
        connection.executemany(
            "INSERT INTO fact_orders VALUES (?, ?, ?, ?, ?, ?, ?, ?)", fact_rows()
        )
        connection.executemany(
            "INSERT INTO stress_rows VALUES (?, ?)", stress_rows()
        )
    finally:
        connection.close()


def sqlite_adapter(database: Path) -> DatabaseAdapter:
    """SQLite default path: the pre-contract connector, seen through the contract."""
    return adapt_connector(SQLiteConnector(str(database)))


def duckdb_adapter(database: Path) -> DatabaseAdapter:
    return DuckDBConnector(str(database))


# --------------------------------------------------------------------------- #
# Shared comparison helpers
# --------------------------------------------------------------------------- #

_DECIMAL_TEXT = re.compile(r"^-?\d+(\.\d+)?$")


def comparable_value(value: object) -> object:
    """Comparison form for a normalized value.

    DuckDB renders a DECIMAL column as exact text (``"0.75"``) while SQLite's
    driver returns a REAL (``0.75``); both describe the same logical value, so the
    cross-backend comparator compares them numerically instead of by Python type.
    Text columns are unaffected because only digit-only text is treated as a number.
    """
    if isinstance(value, str) and _DECIMAL_TEXT.fullmatch(value):
        return Decimal(value)
    if isinstance(value, float):
        return Decimal(str(value))
    return value


class ResultComparisonMixin:
    def assertResultsEquivalent(self, left, right) -> None:  # noqa: N802 - unittest style
        self.assertEqual(left.columns, right.columns)
        self.assertEqual(left.row_count, right.row_count)
        self.assertEqual(len(left.rows), len(right.rows))
        for left_row, right_row in zip(left.rows, right.rows):
            self.assertEqual(len(left_row), len(right_row))
            self.assertEqual(
                [comparable_value(value) for value in left_row],
                [comparable_value(value) for value in right_row],
            )


# --------------------------------------------------------------------------- #
# Parameterised conformance suite (runs once per backend)
# --------------------------------------------------------------------------- #


class AdapterConformanceMixin(ResultComparisonMixin):
    """One contract, one suite, executed against every backend.

    Deliberately a mixin instead of a ``TestCase``: an abstract base class would
    itself be collected and run against whichever backend its defaults name. The
    concrete classes below pair it with ``unittest.TestCase``.
    """

    DIALECT = ""
    build_fixture = staticmethod(build_sqlite_fixture)
    open_adapter = staticmethod(sqlite_adapter)
    #: An extra admin statement only this backend's engine refuses by itself.
    ENGINE_REFUSED_EXTRA: tuple[str, ...] = ()
    #: A date function this backend declares supported, with a working probe.
    DATE_FUNCTION_PROBE = ""

    WRITE_STATEMENTS = (
        "INSERT INTO fact_orders VALUES (9999, 1, '2024-01-01', 1, 1.00, 0, NULL, NULL)",
        "UPDATE fact_orders SET amount = 0 WHERE order_id = 1",
        "DELETE FROM fact_orders WHERE order_id = 1",
        "DROP TABLE fact_orders",
        "CREATE TABLE hacked (x INTEGER)",
        "ATTACH '../attached-probe.db' AS other",
        "PRAGMA table_info('fact_orders')",
    )
    #: The four classic writes every engine read-only role must refuse.
    ENGINE_REFUSED_WRITES = WRITE_STATEMENTS[:5]
    SLOW_SQL = (
        "SELECT SUM(x.value * y.value + z.value) AS total FROM stress_rows x "
        "JOIN stress_rows y ON x.id < y.id JOIN stress_rows z ON y.id < z.id"
    )

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.database = Path(self.temp.name) / f"fixture.{self.DIALECT}"
        self.build_fixture(self.database)
        self.adapter = self.open_adapter(self.database)
        self.addCleanup(self.adapter.close)

    # ---- surface ---------------------------------------------------------

    def test_contract_surface_is_implemented(self):
        self.assertIsInstance(self.adapter, DatabaseAdapter)
        self.assertEqual(self.adapter.dialect, self.DIALECT)
        self.assertIsInstance(self.adapter.capabilities, AdapterCapabilities)
        self.assertEqual(self.adapter.capabilities.dialect, self.DIALECT)
        self.assertEqual(self.adapter.capabilities.limit_style, "limit")
        self.assertTrue(self.adapter.capabilities.readonly_enforced_by_engine)
        self.assertLessEqual(
            self.adapter.capabilities.date_functions, DATE_FUNCTION_VOCABULARY
        )
        for operation in (
            "connect",
            "list_tables",
            "describe_table",
            "describe_logical_table",
            "execute_sql",
            "execute_readonly",
            "preview",
            "cancel",
            "explain",
            "normalize_value",
            "normalize_type",
            "close",
        ):
            with self.subTest(operation=operation):
                self.assertTrue(callable(getattr(self.adapter, operation)))
        self.assertIs(self.adapter.connect(), self.adapter)

    # ---- catalog / schema ------------------------------------------------

    def test_catalog_and_logical_schema(self):
        self.assertEqual(
            self.adapter.list_tables(), ["dim_category", "fact_orders", "stress_rows"]
        )
        self.assertEqual(
            self.adapter.describe_logical_table("fact_orders"),
            [
                ("order_id", "integer", False),
                ("category_id", "integer", False),
                ("order_date", "date", False),
                ("amount", "integer", False),
                ("discount", "decimal", False),
                ("is_returned", "boolean", False),
                ("payload", "binary", True),
                ("note", "text", True),
            ],
        )
        schema = self.adapter.describe_table("dim_category")
        self.assertTrue(schema.columns[0].primary_key)
        self.assertFalse(schema.columns[0].nullable)
        self.assertEqual(
            [column.data_type for column in schema.columns][:1],
            ["INTEGER"],
        )

    # ---- bounded execution ----------------------------------------------

    def test_execute_readonly_bounds_rows_and_preview_keeps_inner_limit(self):
        bounded = self.adapter.execute_readonly(
            "SELECT order_id FROM fact_orders ORDER BY order_id", limit=5
        )
        self.assertEqual(bounded.columns, ["order_id"])
        self.assertEqual(bounded.row_count, 5)
        self.assertEqual(bounded.rows, [[1], [2], [3], [4], [5]])

        # A larger limit must not be silently clamped by the preview cap.
        wide = self.adapter.execute_readonly(
            "SELECT order_id FROM fact_orders", limit=250
        )
        self.assertEqual(wide.row_count, 250)

        # An inner LIMIT inside a CTE must not cap the outer preview bound.
        nested = self.adapter.preview(
            "WITH head AS (SELECT * FROM fact_orders LIMIT 3) SELECT * FROM head", 1
        )
        self.assertEqual(nested.row_count, 1)

        unbounded = self.adapter.execute_readonly("SELECT COUNT(*) AS n FROM fact_orders")
        self.assertEqual(unbounded.rows, [[FACT_ROWS]])
        with self.assertRaises(ValueError):
            self.adapter.execute_readonly("SELECT 1 AS one", limit=0)
        with self.assertRaises(ValueError):
            self.adapter.execute_readonly("SELECT 1 AS one", timeout=0)
        with self.assertRaises(ValueError):
            self.adapter.preview("SELECT 1 AS one", 0)

    # ---- 18-S1 ----------------------------------------------------------

    def test_18_s1_write_and_admin_sql_is_refused_before_execution(self):
        before = self.adapter.execute_readonly("SELECT COUNT(*) AS n FROM fact_orders")
        for statement in self.WRITE_STATEMENTS:
            with self.subTest(statement=statement.split()[0]):
                with self.assertRaises(AdapterPolicyError) as caught:
                    self.adapter.execute_readonly(statement)
                self.assertIsNotNone(caught.exception.decision)
                self.assertEqual(caught.exception.decision.rule, "read_only_ast")
                self.assertFalse(caught.exception.decision.allowed)
        # Nothing ran: the fixture is untouched.
        self.assertEqual(
            self.adapter.execute_readonly("SELECT COUNT(*) AS n FROM fact_orders"),
            before,
        )
        self.assertNotIn("hacked", self.adapter.list_tables())

    def test_18_s1_engine_read_only_role_refuses_writes(self):
        before = self.adapter.execute_readonly("SELECT COUNT(*) AS n FROM fact_orders")
        for statement in self.ENGINE_REFUSED_WRITES + self.ENGINE_REFUSED_EXTRA:
            with self.subTest(statement=statement.split()[0]):
                with self.assertRaises(Exception) as caught:
                    self.adapter.execute_sql(statement)
                self.assertNotIsInstance(caught.exception, AdapterPolicyError)
        self.assertEqual(
            self.adapter.execute_readonly("SELECT COUNT(*) AS n FROM fact_orders"),
            before,
        )
        self.assertEqual(
            sorted(self.adapter.list_tables()),
            ["dim_category", "fact_orders", "stress_rows"],
        )

    # ---- type conversion -------------------------------------------------

    def test_type_conversion_normalizes_driver_values(self):
        result = self.adapter.execute_readonly(
            "SELECT order_date, discount, is_returned, payload, note, amount "
            f"FROM fact_orders WHERE order_id = {PROBE_ORDER_ID}"
        )
        self.assertEqual(result.row_count, 1)
        order_date, discount, returned, payload, note, amount = result.rows[0]

        # Dates always arrive as ISO text, whichever driver produced them.
        self.assertIsInstance(order_date, str)
        self.assertEqual(order_date, PROBE_ROW["order_date"])
        # Decimals keep their value; DuckDB keeps the declared scale as text while
        # SQLite reports a double (no DECIMAL storage class).
        self.assertEqual(Decimal(str(discount)), PROBE_ROW["discount"])
        self.assertIn(type(discount), (str, float))
        # Booleans are booleans wherever the engine has the type, 0/1 on SQLite.
        self.assertIn(type(returned), (bool, int))
        self.assertEqual(bool(returned), PROBE_ROW["is_returned"])
        # Binary becomes lowercase hex text, never a raw driver object.
        self.assertEqual(payload, PROBE_ROW["payload_hex"])
        self.assertEqual(note, PROBE_ROW["note"])
        self.assertEqual(amount, PROBE_ROW["amount"])
        # NULL survives as JSON null for a nullable column.
        self.assertIsNone(
            self.adapter.execute_readonly(
                "SELECT note FROM fact_orders WHERE order_id = 7"
            ).rows[0][0]
        )
        # Every normalized value is JSON-serializable without a custom encoder.
        json.dumps(result.rows)

        self.assertEqual(self.adapter.normalize_value(Decimal("12.50")), "12.50")
        self.assertEqual(self.adapter.normalize_value(b"ab"), "6162")
        self.assertEqual(self.adapter.normalize_value(None), None)
        self.assertEqual(self.adapter.normalize_value(True), True)
        self.assertEqual(
            self.adapter.normalize_value(
                datetime(2024, 1, 1, 8, tzinfo=timezone(timedelta(hours=8)))
            ),
            "2024-01-01T00:00:00+00:00",
        )
        with self.assertRaises(AdapterTypeError):
            self.adapter.normalize_value(float("nan"))
        with self.assertRaises(AdapterTypeError):
            self.adapter.normalize_value([1, 2])

    # ---- capabilities ----------------------------------------------------

    def test_connector_declaration_is_the_frozen_matrix_entry(self):
        """A connector must not re-derive its own capability declaration.

        ``SQLiteConnector`` used to build ``AdapterCapabilities("sqlite")`` from
        dataclass defaults, which silently disagreed with ``SQLITE_CAPABILITIES``
        on ``explain_prefix`` (``"EXPLAIN"`` vs ``"EXPLAIN QUERY PLAN"``),
        ``date_functions`` and ``integer_division``. Anyone reading the connector
        got a different capability set from the one the adapter enforced (E-23).
        """
        self.assertIs(self.adapter.capabilities, CAPABILITY_REGISTRY[self.DIALECT])
        self.assertIs(
            self.CONNECTOR_CLASS.capabilities, CAPABILITY_REGISTRY[self.DIALECT]
        )
        self.assertIs(
            self.CONNECTOR_CLASS.capabilities, capabilities_for_dialect(self.DIALECT)
        )

    def test_declared_capabilities_are_truthful(self):
        capabilities = self.adapter.capabilities
        probes = (
            (
                capabilities.window_functions,
                "window functions",
                "SELECT SUM(amount) OVER (PARTITION BY category_id ORDER BY order_id) "
                "AS running FROM fact_orders ORDER BY order_id",
            ),
            (
                capabilities.cte,
                "common table expressions",
                "WITH totals AS (SELECT category_id, SUM(amount) AS total "
                "FROM fact_orders GROUP BY category_id) "
                "SELECT total FROM totals ORDER BY total",
            ),
            (
                capabilities.ilike,
                "ILIKE",
                "SELECT category_name FROM dim_category "
                "WHERE category_name ILIKE 'B%' ORDER BY category_name",
            ),
            (
                capabilities.qualify,
                "QUALIFY",
                "SELECT category_name FROM dim_category "
                "QUALIFY ROW_NUMBER() OVER (ORDER BY category_id) = 1",
            ),
            (
                "date_trunc" in capabilities.date_functions,
                "date function(s) date_trunc",
                "SELECT date_trunc('month', order_date) AS bucket "
                "FROM fact_orders ORDER BY order_id",
            ),
        )
        for declared, label, sql in probes:
            with self.subTest(capability=label):
                if declared:
                    result = self.adapter.execute_readonly(sql, limit=4)
                    self.assertTrue(result.rows, f"{label} declared but produced no rows")
                else:
                    with self.assertRaises(AdapterUnsupportedError) as caught:
                        self.adapter.execute_readonly(sql, limit=4)
                    self.assertIn("does not declare support", str(caught.exception))

        # A date function declared supported must really run on this engine.
        self.assertTrue(self.DATE_FUNCTION_PROBE)
        date_probe = self.adapter.execute_readonly(self.DATE_FUNCTION_PROBE, limit=1)
        self.assertTrue(date_probe.rows)
        self.assertEqual(len(date_probe.rows[0]), 1)

    # ---- errors ----------------------------------------------------------

    def test_unsupported_object_is_a_typed_query_error(self):
        with self.assertRaises(AdapterQueryError) as unknown_table:
            self.adapter.execute_readonly("SELECT * FROM missing_table")
        self.assertIs(type(unknown_table.exception), AdapterQueryError)
        with self.assertRaises(AdapterQueryError) as unknown_column:
            self.adapter.execute_readonly("SELECT nope FROM fact_orders")
        self.assertIs(type(unknown_column.exception), AdapterQueryError)

    # ---- plan ------------------------------------------------------------

    def test_explain_returns_a_plan_and_refuses_writes(self):
        result = self.adapter.explain(
            "SELECT d.category_name FROM fact_orders f "
            "JOIN dim_category d ON d.category_id = f.category_id"
        )
        self.assertTrue(result.rows)
        self.assertTrue(str(result.rows[0]).strip())
        with self.assertRaises(AdapterPolicyError):
            self.adapter.explain("DROP TABLE fact_orders")

    # ---- cancellation ----------------------------------------------------

    def test_deadline_interrupts_in_flight_work(self):
        started = time.monotonic()
        with self.assertRaises(AdapterTimeoutError) as caught:
            self.adapter.execute_readonly(self.SLOW_SQL, timeout=0.25)
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 5.0, "deadline did not interrupt the engine")
        self.assertIn("deadline", str(caught.exception))
        # An interrupted query must not poison the connection.
        self.assertEqual(
            self.adapter.execute_readonly("SELECT COUNT(*) AS n FROM stress_rows").rows,
            [[STRESS_ROWS]],
        )

    def test_client_cancel_interrupts_in_flight_work(self):
        timer = threading.Timer(0.25, self.adapter.cancel)
        timer.daemon = True
        timer.start()
        started = time.monotonic()
        try:
            with self.assertRaises(AdapterCancelledError) as caught:
                self.adapter.execute_readonly(self.SLOW_SQL)
        finally:
            timer.cancel()
            timer.join()
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 5.0, "cancel did not interrupt the engine")
        self.assertIn("interrupted", str(caught.exception))
        self.assertEqual(self.adapter.execute_readonly("SELECT 1 AS one").rows, [[1]])


class SQLiteAdapterConformanceTest(AdapterConformanceMixin, unittest.TestCase):
    DIALECT = "sqlite"
    build_fixture = staticmethod(build_sqlite_fixture)
    open_adapter = staticmethod(sqlite_adapter)
    CONNECTOR_CLASS = SQLiteConnector
    DATE_FUNCTION_PROBE = (
        "SELECT strftime('%Y-%m', order_date) AS bucket FROM fact_orders"
    )


@unittest.skipUnless(DUCKDB_AVAILABLE, DUCKDB_SKIP_REASON)
class DuckDBAdapterConformanceTest(AdapterConformanceMixin, unittest.TestCase):
    DIALECT = "duckdb"
    build_fixture = staticmethod(build_duckdb_fixture)
    open_adapter = staticmethod(duckdb_adapter)
    CONNECTOR_CLASS = DuckDBConnector
    # DuckDB's read-only role additionally refuses ATTACH and config PRAGMA.
    ENGINE_REFUSED_EXTRA = ("ATTACH 'probe.db' AS other",)
    DATE_FUNCTION_PROBE = (
        "SELECT date_trunc('month', order_date) AS bucket FROM fact_orders"
    )

    def test_bounded_fetch_stops_at_the_limit(self):
        """DuckDB streams, so the bounded fetch override pulls only ``limit`` rows."""
        result = self.adapter._fetch_bounded(
            "SELECT i FROM range(200000) AS generated(i)", 4
        )
        self.assertEqual(result.columns, ["i"])
        self.assertEqual(result.rows, [[0], [1], [2], [3]])
        self.assertEqual(result.row_count, 4)


# --------------------------------------------------------------------------- #
# 18-N1: cross-backend business equivalence
# --------------------------------------------------------------------------- #

EQUIVALENCE_QUERIES = (
    (
        "count",
        "SELECT COUNT(*) AS order_count FROM fact_orders",
    ),
    (
        "sum",
        "SELECT SUM(amount) AS amount_total FROM fact_orders",
    ),
    (
        "group_by_join",
        "SELECT d.category_name AS category_name, SUM(f.amount) AS amount_total "
        "FROM fact_orders f JOIN dim_category d ON d.category_id = f.category_id "
        "GROUP BY d.category_name ORDER BY d.category_name",
    ),
    (
        "window",
        "SELECT order_id, SUM(amount) OVER (PARTITION BY category_id "
        "ORDER BY order_id) AS running_total FROM fact_orders ORDER BY order_id",
    ),
    (
        "cte",
        "WITH per_category AS (SELECT category_id, SUM(amount) AS amount_total "
        "FROM fact_orders GROUP BY category_id) "
        "SELECT d.category_name AS category_name, p.amount_total AS amount_total "
        "FROM per_category p JOIN dim_category d ON d.category_id = p.category_id "
        "ORDER BY d.category_name",
    ),
    (
        "date_range",
        "SELECT COUNT(*) AS order_count, SUM(amount) AS amount_total "
        "FROM fact_orders WHERE order_date >= '2024-03-01' "
        "AND order_date < '2024-06-01'",
    ),
    (
        "empty",
        "SELECT category_name FROM dim_category WHERE category_id < 0",
    ),
)


@unittest.skipUnless(DUCKDB_AVAILABLE, DUCKDB_SKIP_REASON)
class PortableEquivalenceTest(ResultComparisonMixin, unittest.TestCase):
    """18-N1: the same fixture in both engines answers the same questions."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.temp = tempfile.TemporaryDirectory()
        cls.sqlite_path = Path(cls.temp.name) / "fixture.sqlite"
        cls.duckdb_path = Path(cls.temp.name) / "fixture.duckdb"
        build_sqlite_fixture(cls.sqlite_path)
        build_duckdb_fixture(cls.duckdb_path)
        cls.sqlite = sqlite_adapter(cls.sqlite_path)
        cls.duckdb = duckdb_adapter(cls.duckdb_path)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.sqlite.close()
        cls.duckdb.close()
        cls.temp.cleanup()

    def test_18_n1_same_queries_return_identical_results(self):
        for label, sql in EQUIVALENCE_QUERIES:
            with self.subTest(query=label):
                left = self.sqlite.execute_readonly(sql)
                right = self.duckdb.execute_readonly(sql)
                # Exact equality: these queries project integers, text and dates
                # only, so no numeric tolerance is needed or wanted.
                self.assertEqual(left.columns, right.columns)
                self.assertEqual(left.row_count, right.row_count)
                self.assertEqual(left.rows, right.rows)
                self.assertEqual(label != "empty", bool(left.rows))

    def test_18_n1_schema_metadata_parity(self):
        for table in ("dim_category", "fact_orders", "stress_rows"):
            with self.subTest(table=table):
                self.assertEqual(
                    self.sqlite.describe_logical_table(table),
                    self.duckdb.describe_logical_table(table),
                )
                self.assertEqual(
                    [column.primary_key for column in self.sqlite.describe_table(table).columns],
                    [column.primary_key for column in self.duckdb.describe_table(table).columns],
                )

    def test_18_n1_type_conversion_parity(self):
        sql = (
            "SELECT order_date, discount, is_returned, payload, note, amount "
            f"FROM fact_orders WHERE order_id = {PROBE_ORDER_ID}"
        )
        self.assertResultsEquivalent(
            self.sqlite.execute_readonly(sql), self.duckdb.execute_readonly(sql)
        )

    def test_unknown_object_uses_one_typed_error_class(self):
        for sql in ("SELECT * FROM missing_table", "SELECT nope FROM fact_orders"):
            with self.subTest(sql=sql):
                with self.assertRaises(AdapterQueryError) as left:
                    self.sqlite.execute_readonly(sql)
                with self.assertRaises(AdapterQueryError) as right:
                    self.duckdb.execute_readonly(sql)
                self.assertIs(type(left.exception), type(right.exception))
                self.assertIs(type(left.exception), AdapterQueryError)

    def test_declared_capability_difference_is_refused_not_guessed(self):
        duck_only = (
            "SELECT category_name FROM dim_category "
            "WHERE category_name ILIKE 'B%' ORDER BY category_name",
            "SELECT category_name FROM dim_category "
            "QUALIFY ROW_NUMBER() OVER (ORDER BY category_id) = 1",
            "SELECT date_trunc('month', order_date) AS bucket "
            "FROM fact_orders ORDER BY order_id",
        )
        for sql in duck_only:
            with self.subTest(sql=sql[:48]):
                self.assertTrue(self.duckdb.execute_readonly(sql, limit=2).rows)
                with self.assertRaises(AdapterUnsupportedError):
                    self.sqlite.execute_readonly(sql, limit=2)


# --------------------------------------------------------------------------- #
# 18-R1: the default install stays SQLite-only
# --------------------------------------------------------------------------- #


class _DuckDBHidden:
    """Make the optional driver unimportable without uninstalling it.

    Why monkeypatching: the regression the plan asks for is "the SQLite path still
    works when the new backend's dependency is missing". Hiding the module keeps
    the check honest (no dependency on a second virtualenv) and reversible.
    """

    def __enter__(self) -> "_DuckDBHidden":
        real_find_spec = importlib.util.find_spec

        def guarded_find_spec(name: str, *args: object, **kwargs: object):
            if name.split(".")[0] == "duckdb":
                return None
            return real_find_spec(name, *args, **kwargs)

        self._modules = patch.dict(sys.modules, {"duckdb": None})
        self._find_spec = patch("importlib.util.find_spec", guarded_find_spec)
        self._modules.start()
        self._find_spec.start()
        return self

    def __exit__(self, *_: object) -> None:
        self._find_spec.stop()
        self._modules.stop()


class DefaultLightweightPathTest(unittest.TestCase):
    """18-R1: SQLite remains the dependency-free default path."""

    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.database = Path(self.temp.name) / "lightweight.sqlite"
        build_sqlite_fixture(self.database)

    def test_18_r1_sqlite_contract_path_without_the_driver(self):
        with _DuckDBHidden():
            self.assertIsNone(importlib.util.find_spec("duckdb"))
            adapter = adapt_connector(SQLiteConnector(str(self.database)))
            try:
                self.assertEqual(
                    adapter.execute_readonly(
                        "SELECT COUNT(*) AS n FROM fact_orders"
                    ).rows,
                    [[FACT_ROWS]],
                )
                self.assertEqual(len(adapter.describe_logical_table("fact_orders")), 8)
                self.assertEqual(
                    adapter.preview("SELECT order_id FROM fact_orders", 3).row_count, 3
                )
                self.assertTrue(
                    adapter.explain("SELECT order_id FROM fact_orders").rows
                )
                with self.assertRaises(AdapterPolicyError):
                    adapter.execute_readonly("DELETE FROM fact_orders")
            finally:
                adapter.close()

    def test_18_r1_duckdb_backend_fails_loudly_only_when_used(self):
        with _DuckDBHidden():
            # Importing the module and the lazy package export stays safe...
            from queryforge.infrastructure.db import DuckDBConnector as hidden_connector

            self.assertTrue(callable(hidden_connector))
            # ...and only *using* the backend reports the missing dependency.
            with self.assertRaises(AdapterUnavailableError) as caught:
                hidden_connector(str(self.database.with_suffix(".duckdb")))
            self.assertIn("queryforge[duckdb]", str(caught.exception))

    def test_18_r1_adapter_package_never_imports_the_driver_eagerly(self):
        script = (
            "import sys;"
            "import queryforge.infrastructure.db as db;"
            "print('duckdb_imported=' + str('duckdb' in sys.modules));"
            "print('contract=' + db.DatabaseAdapter.__name__);"
            "print('duckdb_backend=' + db.DuckDBConnector.__name__);"
            "print('sqlite_backend=' + db.SQLiteConnector.__name__)"
        )
        environment = dict(os.environ)
        environment["PYTHONPATH"] = os.pathsep.join(
            [str(PROJECT_ROOT), environment.get("PYTHONPATH", "")]
        ).rstrip(os.pathsep)
        completed = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            cwd=str(PROJECT_ROOT),
            env=environment,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(
            completed.stdout.split(),
            [
                "duckdb_imported=False",
                "contract=DatabaseAdapter",
                "duckdb_backend=DuckDBConnector",
                "sqlite_backend=SQLiteConnector",
            ],
        )

    def test_18_s1_sqlite_role_boundary_is_documented(self):
        """ATTACH is the AST policy layer's job; the role still blocks its writes.

        Honest limitation of the SQLite backend: ``mode=ro`` protects the main
        database only, so the engine accepts ATTACH of another file. Writes into
        that attached database are still refused by ``PRAGMA query_only``, and the
        contract refuses ATTACH through the policy layer before execution.
        """
        attached = Path(self.temp.name) / "attached.sqlite"
        connection = sqlite3.connect(f"{self.database.as_uri()}?mode=ro", uri=True)
        try:
            connection.execute("PRAGMA query_only = ON")
            connection.execute(f"ATTACH DATABASE '{attached}' AS other")
            with self.assertRaises(sqlite3.OperationalError):
                connection.execute("CREATE TABLE other.probe (x INTEGER)")
        finally:
            connection.close()

        adapter = adapt_connector(SQLiteConnector(str(self.database)))
        try:
            with self.assertRaises(AdapterPolicyError):
                adapter.execute_readonly(f"ATTACH DATABASE '{attached}' AS other")
        finally:
            adapter.close()


# --------------------------------------------------------------------------- #
# Normalization and capability vocabulary (backend independent)
# --------------------------------------------------------------------------- #


class NormalizationRuleTest(unittest.TestCase):
    def test_value_rules_are_frozen(self):
        self.assertIsNone(normalize_value(None))
        self.assertIs(normalize_value(True), True)
        self.assertEqual(normalize_value(7), 7)
        self.assertEqual(normalize_value(1.5), 1.5)
        self.assertEqual(normalize_value(Decimal("12.50")), "12.50")
        self.assertEqual(normalize_value(date(2024, 2, 29)), "2024-02-29")
        self.assertEqual(
            normalize_value(datetime(2024, 1, 1, 8, 30)), "2024-01-01T08:30:00"
        )
        self.assertEqual(
            normalize_value(
                datetime(2024, 1, 1, 8, 30, tzinfo=timezone(timedelta(hours=8)))
            ),
            "2024-01-01T00:30:00+00:00",
        )
        self.assertEqual(normalize_value(b"\x00\xff"), "00ff")
        self.assertEqual(normalize_value(memoryview(b"ab")), "6162")
        self.assertEqual(normalize_value("text"), "text")
        for value in (float("inf"), float("nan"), [1], {"a": 1}, object()):
            with self.subTest(value=type(value).__name__):
                with self.assertRaises(AdapterTypeError):
                    normalize_value(value)

    def test_type_vocabulary_is_shared_by_both_dialects(self):
        # SQLite declares free-form names, DuckDB reports engine names; the same
        # DDL column must map to the same logical type.
        self.assertEqual(normalize_type("TEXT"), "text")
        self.assertEqual(normalize_type("VARCHAR"), "text")
        self.assertEqual(normalize_type("character varying(20)"), "text")
        self.assertEqual(normalize_type("DECIMAL(12,2)"), "decimal")
        self.assertEqual(normalize_type("NUMERIC"), "decimal")
        self.assertEqual(normalize_type("INTEGER"), "integer")
        self.assertEqual(normalize_type("HUGEINT"), "integer")
        self.assertEqual(normalize_type("REAL"), "float")
        self.assertEqual(normalize_type("DOUBLE PRECISION"), "float")
        self.assertEqual(normalize_type("BOOLEAN"), "boolean")
        self.assertEqual(normalize_type("BLOB"), "binary")
        self.assertEqual(normalize_type("BYTEA"), "binary")
        self.assertEqual(normalize_type("DATE"), "date")
        self.assertEqual(normalize_type("TIMESTAMPTZ"), "timestamp")
        self.assertEqual(normalize_type("timestamp with time zone"), "timestamp")
        self.assertEqual(normalize_type("JSON"), "json")
        self.assertEqual(normalize_type(""), "unknown")
        self.assertEqual(normalize_type(None), "unknown")
        self.assertEqual(normalize_type("STRUCT(a INTEGER)"), "unknown")
        self.assertEqual(normalize_type("INTEGER[]"), "unknown")


if __name__ == "__main__":
    unittest.main()
