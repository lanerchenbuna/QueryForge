"""Step 18 follow-up conformance suite: the server-type (PostgreSQL) backend.

Why this module exists
----------------------
Step 18 froze the adapter contract and verified it against two *embedded* engines
(SQLite, DuckDB), so "supports a second database" was only true for engines that
need no server. This module adds the third backend:
``queryforge/infrastructure/db/postgres_connector.py``, driven through the same
contract and the same error taxonomy over a real PostgreSQL server.

Skip discipline (chosen explicitly, and why)
--------------------------------------------
The suite has two halves.

**The always-run half** (``PostgresContractWiringTest``, ``PostgresDialectLevelTest``)
needs neither a server nor the ``psycopg`` driver, and it is *not* skipped:

* the connector module and the factory import without the driver, and a subprocess
  proves the driver is never imported eagerly (18-R1 for the new extra);
* the capability declaration is registered in ``CAPABILITY_REGISTRY`` and checked
  field by field against the frozen vocabulary;
* the factory routes ``postgres://``/``postgresql://`` DSNs and ``*.pg`` marker files
  to the new backend without touching the SQLite/DuckDB paths, and a DSN read from a
  marker file never appears in an error message;
* the engine-independent contract behaviour of the declaration is executed for real
  (``check_capabilities`` refusals, ``bound_sql``/preview rewriting round-trips,
  catalog type-name reconstruction, the superuser refusal rule).

**The server-backed half** (``PostgresAdapterConformanceTest``,
``PostgresSqliteEquivalenceTest``) runs the same parameterised conformance coverage
the DuckDB backend gets -- 18-N1 equivalence against SQLite, 18-S1 write refusal by
the AST policy *and* by the server's read-only session, capability truthfulness,
typed errors for unknown objects, cancellation and deadline, ``explain`` -- against a
server reached through ``QUERYFORGE_TEST_POSTGRES_DSN``.

That half is **skipped, not faked**, when the variable is unset, because the
repository's default gate (``python -m unittest discover -s tests``) must stay green
on a machine with no database server. The absence is kept *visible*:

* the skip reason names the variable, the command and the section of
  ``docs/database_adapters.md`` that documents the boundary;
* the server-backed classes say in their own docstrings that the engine half is
  **unverified in this environment** and why;
* setting ``QUERYFORGE_REQUIRE_POSTGRES=1`` turns a missing DSN into a **failure**
  (not a skip), so a job that is supposed to run the server half cannot pass by
  skipping it. That is the switch to use in a tier-2 integration job, where
  "必需接口测试因为缺依赖而跳过，却将里程碑标记完成" is exactly the failure mode to
  avoid: with the switch on, this module is red until a server answers.

Running the server half (one command)
-------------------------------------
.. code:: bash

    pip install -e '.[postgres]'
    QUERYFORGE_TEST_POSTGRES_DSN=postgresql://qf_reader:secret@127.0.0.1:5432/qf_test \\
        python -m unittest tests.test_postgres_adapter_contract -v

The DSN is used by the *adapter* and must therefore be a read-only (non-superuser)
role: the adapter refuses a superuser session by default. The fixture tables live in
a dedicated schema (``queryforge_step18_conformance`` / ``..._equivalence`` by
default, override with ``QUERYFORGE_TEST_POSTGRES_SCHEMA``), which this module creates
and drops; point the DSN at a disposable test database. If that role is genuinely
read-only it cannot create the fixture, so an optional *setup* DSN may be supplied:

* ``QUERYFORGE_TEST_POSTGRES_SETUP_DSN`` -- used only to create/drop the fixture
  schema (a write-capable role). Defaults to the main DSN.
* ``QUERYFORGE_TEST_POSTGRES_ALLOW_SUPERUSER=1`` -- opens the adapter with
  ``require_readonly_role=False`` for throwaway containers where only a superuser
  exists. The engine-side boundary is then the session flag alone, and
  ``test_readonly_boundary_layers_are_reported`` reports exactly that instead of
  claiming the role layer.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from queryforge.infrastructure.db import (
    CAPABILITY_REGISTRY,
    DATE_FUNCTION_VOCABULARY,
    AdapterPolicyError,
    AdapterQueryError,
    AdapterUnavailableError,
    AdapterUnsupportedError,
    DatabaseAdapter,
    capabilities_for_dialect,
    normalize_type,
)
from queryforge.infrastructure.db.adapters import (
    is_postgres_target,
    open_database,
    open_postgres,
    resolve_postgres_dsn,
)
from queryforge.infrastructure.db.postgres_connector import (
    PostgresConnector,
    PostgresUnavailableError,
    declared_type_name,
    readonly_role_problem,
    sqlstate_of,
)
from tests.test_db_adapter_contract import (
    EQUIVALENCE_QUERIES,
    FACT_ROWS,
    PROBE_ORDER_ID,
    PROBE_ROW,
    AdapterConformanceMixin,
    ResultComparisonMixin,
    build_sqlite_fixture,
    category_rows,
    fact_rows,
    sqlite_adapter,
    stress_rows,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]

SERVER_DSN = os.environ.get("QUERYFORGE_TEST_POSTGRES_DSN", "").strip()
SETUP_DSN = os.environ.get("QUERYFORGE_TEST_POSTGRES_SETUP_DSN", "").strip() or SERVER_DSN
SCHEMA_PREFIX = os.environ.get("QUERYFORGE_TEST_POSTGRES_SCHEMA", "").strip() or (
    "queryforge_step18"
)
#: Opt-in switch that turns the missing DSN into a failure instead of a skip.
REQUIRE_POSTGRES = os.environ.get(
    "QUERYFORGE_REQUIRE_POSTGRES", ""
).strip().casefold() not in {"", "0", "false", "no"}
#: Opt-out from the superuser refusal (throwaway containers only; see the docstring).
ALLOW_SUPERUSER = os.environ.get(
    "QUERYFORGE_TEST_POSTGRES_ALLOW_SUPERUSER", ""
).strip().casefold() not in {"", "0", "false", "no"}

SERVER_SKIP_REASON = (
    "server-backed PostgreSQL conformance is UNVERIFIED without a server: set "
    "QUERYFORGE_TEST_POSTGRES_DSN=postgresql://user:pass@host:5432/db and run "
    "'python -m unittest tests.test_postgres_adapter_contract -v' "
    "(docs/database_adapters.md, 'Running the server-backed suite'); set "
    "QUERYFORGE_REQUIRE_POSTGRES=1 to make this a failure instead of a skip"
)
MISSING_DSN_FAILURE = (
    "QUERYFORGE_REQUIRE_POSTGRES is set but QUERYFORGE_TEST_POSTGRES_DSN is empty: "
    "the server-backed half of the PostgreSQL contract cannot be skipped in this mode. "
    "Run 'QUERYFORGE_TEST_POSTGRES_DSN=postgresql://... python -m unittest "
    "tests.test_postgres_adapter_contract -v' against a real server."
)

# --------------------------------------------------------------------------- #
# Fixture (same logical dataset as the SQLite/DuckDB halves, PostgreSQL types)
# --------------------------------------------------------------------------- #

#: The shared DDL with PostgreSQL spellings: SQLite's BLOB is BYTEA here (PostgreSQL
#: has no BLOB type); the other columns keep the same names and logical types.
POSTGRES_DDL = (
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
    "payload BYTEA, "
    "note TEXT)",
    "CREATE TABLE stress_rows (id INTEGER NOT NULL, value INTEGER NOT NULL)",
)

#: Support tables are read back through ``information_schema``; this backend reports
#: PostgreSQL's canonical (lowercase) type names, with the numeric modifier rebuilt.
POSTGRES_RAW_TYPES = (
    "integer",
    "integer",
    "date",
    "integer",
    "numeric(12,2)",
    "boolean",
    "bytea",
    "text",
)


#: One engine probe per declared date function. The always-run half asserts that this
#: map and ``POSTGRES_CAPABILITIES.date_functions`` are the *same* set, so a function
#: cannot be declared for this dialect without a probe that the server-backed half
#: runs -- the declaration cannot silently become aspirational.
PROBED_DATE_FUNCTIONS = {
    "date_bin": (
        "SELECT date_bin(interval '1 day', timestamp '2024-01-01 05:00:00', "
        "timestamp '2024-01-01') AS bucket"
    ),
    "date_part": (
        "SELECT date_part('month', timestamp '2024-03-18 00:00:00') AS bucket"
    ),
    "date_trunc": (
        "SELECT date_trunc('month', timestamp '2024-03-18 00:00:00') AS bucket"
    ),
    "extract": "SELECT EXTRACT(MONTH FROM timestamp '2024-03-18 00:00:00') AS bucket",
    "to_timestamp": "SELECT to_timestamp(0) AS bucket",
}


def postgres_driver_available() -> bool:
    return importlib.util.find_spec("psycopg") is not None

def schema_for(label: str) -> str:
    return f"{SCHEMA_PREFIX}_{label}"


def create_postgres_fixture(dsn: str, schema: str) -> None:
    """(Re)create the fixture schema and its tables on the test server.

    The *harness* connection is deliberately not the adapter: the adapter refuses a
    superuser session and may be handed a role that cannot create tables, so fixture
    creation uses ``QUERYFORGE_TEST_POSTGRES_SETUP_DSN`` (or the same DSN) instead.
    """
    import psycopg  # local import: this module imports without the driver

    with psycopg.connect(dsn, autocommit=True) as connection:
        with connection.cursor() as cursor:
            cursor.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
            cursor.execute(f'CREATE SCHEMA "{schema}"')
            cursor.execute(f'SET search_path TO "{schema}"')
            for statement in POSTGRES_DDL:
                cursor.execute(statement)
            cursor.executemany(
                "INSERT INTO dim_category VALUES (%s, %s, %s)", category_rows()
            )
            cursor.executemany(
                "INSERT INTO fact_orders VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                [
                    (
                        order_id,
                        category_id,
                        date.fromisoformat(order_date),
                        amount,
                        discount,
                        bool(returned),
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
            cursor.executemany(
                "INSERT INTO stress_rows VALUES (%s, %s)", stress_rows()
            )


def drop_postgres_fixture(dsn: str, schema: str) -> None:
    """Drop the fixture schema; failures are reported, never hidden."""
    import psycopg

    with psycopg.connect(dsn, autocommit=True) as connection:
        with connection.cursor() as cursor:
            cursor.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')


def postgres_adapter(schema: str) -> DatabaseAdapter:
    """Open the fixture schema through the factory (the operator's own entry point)."""
    if is_postgres_target(SERVER_DSN):
        resolved = resolve_postgres_dsn(SERVER_DSN)
    else:
        resolved = SERVER_DSN
    return open_postgres(
        resolved, schema=schema, require_readonly_role=not ALLOW_SUPERUSER
    )


def requires_server(cls: type) -> type:
    """Gate a server-backed test class on the DSN, loudly in both directions."""
    if SERVER_DSN:
        return cls

    def _fail(*_args: object, **_kwargs: object) -> None:
        raise AssertionError(MISSING_DSN_FAILURE)

    if REQUIRE_POSTGRES:
        cls.setUp = _fail  # type: ignore[method-assign]
        cls.setUpClass = classmethod(lambda cls, *_a, **_k: _fail())  # type: ignore[assignment]
        return cls
    return unittest.skip(SERVER_SKIP_REASON)(cls)


# --------------------------------------------------------------------------- #
# Always-run half: wiring and dialect-level behaviour (no server, no driver)
# --------------------------------------------------------------------------- #


class _PsycopgHidden:
    """Make the optional driver unimportable without uninstalling it.

    Same technique as the DuckDB half of the step-18 suite: the regression the plan
    asks for is "the default path still works when the new backend's dependency is
    missing", and hiding the module keeps that check honest and reversible.
    """

    def __enter__(self) -> "_PsycopgHidden":
        real_find_spec = importlib.util.find_spec

        def guarded_find_spec(name: str, *args: object, **kwargs: object):
            if name.split(".")[0] == "psycopg":
                return None
            return real_find_spec(name, *args, **kwargs)

        self._modules = patch.dict(sys.modules, {"psycopg": None})
        self._find_spec = patch("importlib.util.find_spec", guarded_find_spec)
        self._modules.start()
        self._find_spec.start()
        return self

    def __exit__(self, *_: object) -> None:
        self._find_spec.stop()
        self._modules.stop()


class PostgresContractWiringTest(unittest.TestCase):
    """The contract wiring that is verifiable without a server or the driver."""

    def test_capabilities_are_declared_and_registered(self):
        self.assertEqual(PostgresConnector.dialect, "postgres")
        self.assertIs(PostgresConnector.capabilities, CAPABILITY_REGISTRY["postgres"])
        self.assertIs(
            capabilities_for_dialect("postgres"), CAPABILITY_REGISTRY["postgres"]
        )
        capabilities = PostgresConnector.capabilities
        self.assertEqual(capabilities.dialect, "postgres")
        self.assertEqual(capabilities.limit_style, "limit")
        self.assertEqual(capabilities.explain_prefix, "EXPLAIN")
        self.assertTrue(capabilities.window_functions)
        self.assertTrue(capabilities.cte)
        self.assertTrue(capabilities.ilike)
        # PostgreSQL has no QUALIFY clause: the declaration must refuse, not guess.
        self.assertFalse(capabilities.qualify)
        self.assertTrue(capabilities.readonly_enforced_by_engine)
        self.assertTrue(capabilities.cancellation)
        self.assertTrue(capabilities.explain)
        # EXPLAIN prints planner cost units, which are not the calibrated cost model
        # this flag promises; the declaration stays conservative.
        self.assertFalse(capabilities.cost_estimates)
        self.assertLessEqual(capabilities.date_functions, DATE_FUNCTION_VOCABULARY)
        self.assertTrue(capabilities.date_functions)
        self.assertIn("truncate", capabilities.integer_division)
        self.assertEqual(
            capabilities.as_dict()["date_functions"], sorted(capabilities.date_functions)
        )
        self.assertLessEqual(
            capabilities.date_functions,
            {"date_bin", "date_part", "date_trunc", "extract", "to_timestamp"},
        )
        # Every declared capability is exercised by the server-backed half; nothing in
        # the declaration may be aspirational without a probe.
        self.assertTrue(set(PROBED_DATE_FUNCTIONS) == capabilities.date_functions)

    def test_factory_routes_dsn_and_marker_files_without_the_driver(self):
        self.assertTrue(is_postgres_target("postgres://host/db"))
        self.assertTrue(is_postgres_target("postgresql://user:pw@host:5432/db"))
        self.assertTrue(is_postgres_target("/tmp/deploy.pg"))
        self.assertTrue(is_postgres_target("deploy.pgsql"))
        self.assertFalse(is_postgres_target("/tmp/analytics.sqlite"))
        self.assertFalse(is_postgres_target("/tmp/analytics.duckdb"))
        self.assertFalse(is_postgres_target(""))

        dsn = "postgresql://reader:secret@127.0.0.1:1/qf_test"
        self.assertEqual(resolve_postgres_dsn(dsn), dsn)
        with self.assertRaises(AdapterUnavailableError) as missing:
            resolve_postgres_dsn("/tmp/queryforge-definitely-missing.pg")
        self.assertIn("marker file does not exist", str(missing.exception))

        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "deploy.pg"
            marker.write_text(
                "# QueryForge PostgreSQL target\n\n" + dsn + "\n", encoding="utf-8"
            )
            self.assertEqual(resolve_postgres_dsn(str(marker)), dsn)
            blank = Path(directory) / "blank.pg"
            blank.write_text("# only a comment\n", encoding="utf-8")
            with self.assertRaises(AdapterUnavailableError) as empty:
                resolve_postgres_dsn(str(blank))
            self.assertIn("contains no DSN", str(empty.exception))

            with _PsycopgHidden():
                # Routing happens (the target is not mistaken for a SQLite file) and
                # the missing extra is reported by name...
                with self.assertRaises(AdapterUnavailableError) as caught:
                    open_database(dsn)
                self.assertIn("queryforge[postgres]", str(caught.exception))
                with self.assertRaises(AdapterUnavailableError) as from_marker:
                    open_database(str(marker))
                self.assertIn("queryforge[postgres]", str(from_marker.exception))
                # ...and the DSN read from the marker file is never echoed: it holds a
                # password, and adapter errors are logged and shown to users.
                self.assertNotIn("secret", str(from_marker.exception))

    def test_sqlite_and_duckdb_paths_stay_driver_free(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "lightweight.sqlite"
            build_sqlite_fixture(database)
            with _PsycopgHidden():
                self.assertIsNone(importlib.util.find_spec("psycopg"))
                adapter = open_database(str(database))
                try:
                    self.assertIsInstance(adapter, DatabaseAdapter)
                    self.assertEqual(adapter.dialect, "sqlite")
                    self.assertEqual(
                        adapter.execute_readonly("SELECT COUNT(*) AS n FROM fact_orders").rows,
                        [[FACT_ROWS]],
                    )
                    self.assertEqual(len(adapter.describe_logical_table("fact_orders")), 8)
                    self.assertEqual(
                        adapter.preview("SELECT order_id FROM fact_orders", 3).row_count, 3
                    )
                    self.assertTrue(adapter.explain("SELECT order_id FROM fact_orders").rows)
                    with self.assertRaises(AdapterPolicyError):
                        adapter.execute_readonly("DELETE FROM fact_orders")
                finally:
                    adapter.close()
                # The new backend fails loudly only when it is *used*.
                with self.assertRaises(AdapterUnavailableError) as caught:
                    PostgresConnector("postgresql://reader@127.0.0.1:1/qf_test")
                self.assertIn("queryforge[postgres]", str(caught.exception))
                self.assertIsInstance(caught.exception, PostgresUnavailableError)

    def test_connector_module_never_imports_the_driver_eagerly(self):
        script = (
            "import sys;"
            "import queryforge.infrastructure.db as db;"
            "import queryforge.infrastructure.db.adapters as ad;"
            "import queryforge.infrastructure.db.postgres_connector as pg;"
            "print('psycopg_imported=' + str('psycopg' in sys.modules));"
            "print('dialect=' + pg.PostgresConnector.dialect);"
            "print('capabilities=' + pg.PostgresConnector.capabilities.dialect);"
            "print('routing=' + str(ad.is_postgres_target('postgresql://h/db')));"
            "print('contract=' + db.DatabaseAdapter.__name__)"
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
                "psycopg_imported=False",
                "dialect=postgres",
                "capabilities=postgres",
                "routing=True",
                "contract=DatabaseAdapter",
            ],
        )


class PostgresDialectLevelTest(unittest.TestCase):
    """Engine-independent contract behaviour of the real declaration.

    Uses a driver-free instance (``object.__new__`` without ``__init__``): the shared
    ``check_capabilities``/``bound_sql`` implementations only need ``dialect`` and
    ``capabilities``, so these are the *real* frozen code paths against the real
    declaration -- no fake adapter and no mock -- but they say nothing about a server,
    which is why the server-backed classes above exist and are gated separately.
    """

    def setUp(self) -> None:
        self.offline = object.__new__(PostgresConnector)

    def test_declared_capabilities_refuse_unportable_sql(self):
        with self.assertRaises(AdapterUnsupportedError) as caught:
            self.offline.check_capabilities(
                "SELECT category_name FROM dim_category "
                "QUALIFY ROW_NUMBER() OVER (ORDER BY category_id) = 1"
            )
        self.assertIn("QUALIFY", str(caught.exception))
        # A SQLite-only date function is not declared by this dialect, so the shared
        # vocabulary guard refuses it instead of letting the server guess.
        with self.assertRaises(AdapterUnsupportedError) as date_caught:
            self.offline.check_capabilities(
                "SELECT strftime('%Y', order_date) AS bucket FROM fact_orders"
            )
        self.assertIn("strftime", str(date_caught.exception))
        # Declared features and unknown (unpoliced) names pass the guard.
        for sql in (
            "SELECT order_id, SUM(amount) OVER (PARTITION BY category_id "
            "ORDER BY order_id) AS running FROM fact_orders",
            "WITH totals AS (SELECT category_id FROM fact_orders) "
            "SELECT category_id FROM totals",
            "SELECT category_name FROM dim_category WHERE category_name ILIKE 'B%'",
            "SELECT DATE_TRUNC('month', order_date) AS bucket FROM fact_orders",
            "SELECT DATE_BIN(INTERVAL '1 day', order_date, DATE '2024-01-01') AS bucket "
            "FROM fact_orders",
        ):
            with self.subTest(sql=sql[:48]):
                self.offline.check_capabilities(sql)

    def test_bounded_sql_round_trips_through_the_postgres_dialect(self):
        bounded = self.offline.bound_sql(
            "SELECT order_id FROM fact_orders ORDER BY order_id", 5
        )
        self.assertIn("LIMIT 5", bounded)
        # An existing, smaller LIMIT in the same query is preserved, not widened.
        preserved = self.offline.bound_sql("SELECT order_id FROM fact_orders LIMIT 2", 50)
        self.assertIn("LIMIT 2", preserved)
        self.assertNotIn("LIMIT 50", preserved)
        # An inner LIMIT inside a CTE is untouched while the outer bound is added: this
        # is what keeps a preview from being capped by a nested head query.
        nested = self.offline.bound_sql(
            "WITH head AS (SELECT * FROM fact_orders LIMIT 3) SELECT * FROM head", 50
        )
        self.assertIn("LIMIT 3)", nested)
        self.assertTrue(nested.rstrip().endswith("LIMIT 50"), nested)
        # A render round-trip keeps the dialect-specific predicate intact.
        self.assertIn(
            "ILIKE",
            self.offline.bound_sql(
                "SELECT category_name FROM dim_category "
                "WHERE category_name ILIKE 'B%' ORDER BY category_name",
                4,
            ),
        )
        with self.assertRaises(AdapterQueryError):
            self.offline.bound_sql("DELETE FROM fact_orders", 5)

    def test_catalog_type_names_are_rebuilt_and_normalized(self):
        self.assertEqual(declared_type_name("integer"), "integer")
        self.assertEqual(
            declared_type_name("numeric", numeric_precision=12, numeric_scale=2),
            "numeric(12,2)",
        )
        self.assertEqual(
            declared_type_name("numeric", numeric_precision=12), "numeric(12)"
        )
        self.assertEqual(
            declared_type_name(
                "character varying", character_maximum_length=20
            ),
            "character varying(20)",
        )
        self.assertEqual(declared_type_name("bytea"), "bytea")
        self.assertEqual(declared_type_name(""), "unknown")
        self.assertEqual(normalize_type("numeric(12,2)"), "decimal")
        self.assertEqual(normalize_type("bytea"), "binary")

    def test_superuser_sessions_are_refused_by_rule(self):
        self.assertIsNone(readonly_role_problem("qf_reader", is_superuser=False))
        problem = readonly_role_problem("postgres", is_superuser=True)
        self.assertIsNotNone(problem)
        self.assertIn("superuser", str(problem))
        self.assertIn("require_readonly_role=False", str(problem))

    def test_sqlstate_lookup_walks_the_cause_chain(self):
        class _DriverError(Exception):
            sqlstate = "25006"

        wrapper = PostgresUnavailableError("wrapped")
        wrapper.__cause__ = _DriverError("inner")
        self.assertEqual(sqlstate_of(wrapper), "25006")
        self.assertIsNone(sqlstate_of(RuntimeError("no sqlstate anywhere")))


# --------------------------------------------------------------------------- #
# Server-backed half: the parameterised conformance suite (needs a real server)
# --------------------------------------------------------------------------- #


@requires_server
class PostgresAdapterConformanceTest(AdapterConformanceMixin, unittest.TestCase):
    """The shared conformance suite, executed against a real PostgreSQL server.

    **UNVERIFIED without a server.** This class is skipped when
    ``QUERYFORGE_TEST_POSTGRES_DSN`` is unset -- the state of the repository's default
    gate -- and the skip reason says so rather than reporting a green suite. One
    command runs it (see the module docstring):

    .. code:: bash

        QUERYFORGE_TEST_POSTGRES_DSN=postgresql://... \\
            python -m unittest tests.test_postgres_adapter_contract -v

    What it covers here, beyond the shared mixin:

    * 18-S1 in three layers: the shared AST policy refuses 14 write/admin statements
      (including ``SET default_transaction_read_only = off``, ``COPY``, ``VACUUM`` and
      ``CREATE ROLE``) before the server sees them; the server's read-only session
      refuses the classic writes on its own, with SQLSTATE 25006 (read-only
      transaction) or 42501 (insufficient privilege); and ``SELECT ... INTO`` -- which
      the AST layer parses as a plain ``SELECT`` and therefore allows -- is refused by
      the server, which is exactly why the engine-side layer is load-bearing here.
    * the read-only boundary is *reported*, not assumed: ``SHOW
      default_transaction_read_only`` is read back from the server, and the role
      identity/verification flags are asserted to be consistent with the configuration
      this run was asked for (with ``QUERYFORGE_TEST_POSTGRES_ALLOW_SUPERUSER`` the
      role layer is explicitly *not* claimed).
    * every declared date function is probed on the engine, so the declaration is
      verified rather than aspirational.
    * the bounded read really streams: a named server cursor fetches exactly ``limit``
      rows out of 200 000 and leaves no cursor behind (``pg_cursors``).
    """

    DIALECT = "postgres"
    #: The mixin's ``setUp`` is replaced below (the fixture lives in a schema on the
    #: server), but the hooks stay declared so the class is self-describing.
    build_fixture = staticmethod(create_postgres_fixture)
    open_adapter = staticmethod(postgres_adapter)

    #: PostgreSQL spellings of the shared write/admin statements. ``ATTACH`` is
    #: deliberately absent: it is not PostgreSQL syntax, so sqlglot cannot parse it in
    #: this dialect and the refusal would come from the parse layer rather than from a
    #: policy decision (asserted separately by
    #: ``test_unparseable_statement_is_refused_before_execution``). The first five are
    #: the classic writes the *engine* must also refuse (``ENGINE_REFUSED_WRITES``).
    WRITE_STATEMENTS = (
        "INSERT INTO fact_orders VALUES (9999, 1, DATE '2024-01-01', 1, 1.00, FALSE, NULL, NULL)",
        "UPDATE fact_orders SET amount = 0 WHERE order_id = 1",
        "DELETE FROM fact_orders WHERE order_id = 1",
        "DROP TABLE fact_orders",
        "CREATE TABLE hacked (x INTEGER)",
        "PRAGMA table_info('fact_orders')",
        "GRANT SELECT ON fact_orders TO PUBLIC",
        "ALTER TABLE fact_orders ADD COLUMN extra INTEGER",
        "TRUNCATE fact_orders",
        "COPY fact_orders FROM '/etc/hostname'",
        "SET default_transaction_read_only = off",
        "VACUUM fact_orders",
        "ANALYZE fact_orders",
        "CREATE ROLE queryforge_probe LOGIN",
    )
    ENGINE_REFUSED_EXTRA = (
        "TRUNCATE fact_orders",
        "ALTER TABLE fact_orders ADD COLUMN extra INTEGER",
        "GRANT SELECT ON fact_orders TO PUBLIC",
        # The AST policy parses this as a SELECT and allows it; the server refuses it.
        "SELECT * INTO hacked FROM fact_orders",
    )
    #: The mixin binds ``ENGINE_REFUSED_WRITES`` to *its own* ``WRITE_STATEMENTS`` at
    #: class-definition time, so overriding ``WRITE_STATEMENTS`` alone would leave the
    #: engine half running SQLite-flavoured SQL (``0`` into a BOOLEAN column is a
    #: type error on PostgreSQL, not a refusal of the write). The five classic writes
    #: are therefore restated in PostgreSQL spelling.
    ENGINE_REFUSED_WRITES = (
        "INSERT INTO fact_orders VALUES (9999, 1, DATE '2024-01-01', 1, 1.00, FALSE, NULL, NULL)",
        "UPDATE fact_orders SET amount = 0 WHERE order_id = 1",
        "DELETE FROM fact_orders WHERE order_id = 1",
        "DROP TABLE fact_orders",
        "CREATE TABLE hacked (x INTEGER)",
    )
    DATE_FUNCTION_PROBE = (
        "SELECT date_trunc('month', order_date) AS bucket FROM fact_orders"
    )
    #: One probe per declared date function (the declaration must be verified).
    DATE_FUNCTION_PROBES = PROBED_DATE_FUNCTIONS

    @classmethod
    def setUpClass(cls) -> None:
        cls.schema = schema_for("conformance")
        create_postgres_fixture(SETUP_DSN, cls.schema)

    @classmethod
    def tearDownClass(cls) -> None:
        drop_postgres_fixture(SETUP_DSN, cls.schema)

    def setUp(self) -> None:
        # The fixture is per class: nothing in the contract suite mutates data (writes
        # are refused by both layers), so one fixture serves every test.
        self.adapter = postgres_adapter(self.schema)
        self.addCleanup(self.adapter.close)

    # ---- shared mixin assertion, adjusted for PostgreSQL's raw type spelling ----

    def test_catalog_and_logical_schema(self):
        """The mixin's assertions, with this backend's raw type names.

        The shared mixin asserts the literal raw type ``INTEGER`` because DuckDB
        reports uppercase and SQLite echoes the DDL text. PostgreSQL's
        ``information_schema`` reports the canonical lowercase ``integer`` for the same
        column, so the raw name is asserted in that spelling *and* mapped through the
        shared ``normalize_type``; the logical schema, primary key and nullability
        assertions are the mixin's, unchanged, plus the re-attached numeric modifier.
        """
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
        raw_first = schema.columns[0].data_type
        self.assertEqual(raw_first.casefold(), "integer")
        self.assertEqual(self.adapter.normalize_type(raw_first), "integer")
        self.assertEqual(
            [
                column.data_type
                for column in self.adapter.describe_table("fact_orders").columns
            ],
            list(POSTGRES_RAW_TYPES),
        )

    # ---- PostgreSQL-specific conformance -------------------------------------- #

    def test_bounded_fetch_streams_and_releases_the_server_cursor(self):
        """A named cursor transmits only ``limit`` rows, promptly, and leaves nothing.

        The elapsed-time bound is what makes this a *streaming* assertion rather than a
        row-count assertion: with a client cursor the same query would have to
        materialize 20 000 000 digests in the client before ``fetchmany`` could stop,
        which cannot finish in the budget below. ``pg_cursors`` then proves the portal
        was closed instead of being left allocated for the session (a leaked named
        cursor holds server memory and locks).
        """
        started = time.monotonic()
        result = self.adapter._fetch_bounded(
            "SELECT i, md5(i::text) AS digest FROM generate_series(1, 20000000) AS s(i)",
            4,
        )
        elapsed = time.monotonic() - started
        self.assertEqual(result.columns, ["i", "digest"])
        self.assertEqual([row[0] for row in result.rows], [1, 2, 3, 4])
        self.assertEqual(result.row_count, 4)
        self.assertLess(elapsed, 5.0, "the bounded read did not stream from the server")
        self.assertEqual(
            self.adapter.execute_sql("SELECT COUNT(*) FROM pg_cursors").rows, [[0]]
        )
        # The cursor name is a constant, so a second bounded read on the same session
        # must reuse it safely instead of colliding with the first.
        again = self.adapter._fetch_bounded(
            "SELECT i FROM generate_series(1, 10) AS s(i)", 2
        )
        self.assertEqual(again.rows, [[1], [2]])
        self.assertEqual(
            self.adapter.execute_sql("SELECT COUNT(*) FROM pg_cursors").rows, [[0]]
        )

    def test_readonly_boundary_layers_are_reported(self):
        """The session flag and the role facts are read back and must be consistent.

        This asserts the *layered* claim rather than one configuration: the server must
        report a read-only session, the reported role identity must match the server,
        and ``readonly_role_verified`` must be exactly "this session is not a
        superuser". A genuinely read-only role (the recommended setup) additionally
        fails its own privileges, which the SQLSTATE test below exercises; with
        ``QUERYFORGE_TEST_POSTGRES_ALLOW_SUPERUSER=1`` the role layer is explicitly
        *not* claimed instead of being silently assumed.
        """
        self.assertEqual(
            self.adapter.execute_sql("SHOW default_transaction_read_only").rows,
            [["on"]],
        )
        self.assertEqual(
            self.adapter.current_role,
            self.adapter.execute_sql("SELECT current_user").rows[0][0],
        )
        session_superuser = self.adapter.execute_sql(
            "SELECT current_setting('is_superuser')"
        ).rows[0][0]
        self.assertIn(session_superuser, {"on", "off"})
        self.assertEqual(
            session_superuser == "on", self.adapter.role_is_superuser
        )
        self.assertEqual(
            self.adapter.readonly_role_verified, not self.adapter.role_is_superuser
        )
        if self.adapter.role_is_superuser:
            # Only reachable with the explicit opt-out: the adapter must say so.
            self.assertTrue(ALLOW_SUPERUSER)
            self.assertFalse(self.adapter.readonly_role_verified)
        else:
            # Without the opt-out the strict default had to accept the role, i.e. the
            # constructor's superuser refusal did not fire on this DSN.
            self.assertTrue(self.adapter.readonly_role_verified)
            self.assertIsNone(
                readonly_role_problem(
                    self.adapter.current_role, is_superuser=self.adapter.role_is_superuser
                )
            )

    def test_18_s1_server_refusal_is_read_only_or_privilege_denied(self):
        """The *engine* refuses writes with a locale-independent SQLSTATE.

        Accepted codes: 25006 (read-only SQL transaction) and 42501 (insufficient
        privilege -- what a genuinely read-only role reports for a statement it is not
        allowed to run). Both prove the server refused the statement on its own, which
        is what the second layer claims; matching message *text* would break on a
        localized server.
        """
        before = self.adapter.execute_readonly("SELECT COUNT(*) AS n FROM fact_orders")
        for statement in self.ENGINE_REFUSED_WRITES + self.ENGINE_REFUSED_EXTRA:
            with self.subTest(statement=statement.split()[0]):
                with self.assertRaises(Exception) as caught:
                    self.adapter.execute_sql(statement)
                self.assertNotIsInstance(caught.exception, AdapterPolicyError)
                self.assertIn(
                    sqlstate_of(caught.exception),
                    {"25006", "42501"},
                    f"unexpected refusal of {statement!r}: {caught.exception}",
                )
        self.assertEqual(
            self.adapter.execute_readonly("SELECT COUNT(*) AS n FROM fact_orders"), before
        )
        self.assertNotIn("hacked", self.adapter.list_tables())

    def test_18_s1_ast_policy_misses_select_into_and_the_server_catches_it(self):
        """``SELECT ... INTO`` documents why the engine layer is load-bearing.

        The shared AST policy classifies ``SELECT * INTO hacked FROM fact_orders`` as a
        read (it is a ``SELECT`` node), so on this backend only the server's read-only
        transaction stops it from creating a table.
        """
        with self.assertRaises(Exception) as caught:
            self.adapter.execute_readonly("SELECT * INTO hacked FROM fact_orders")
        self.assertNotIsInstance(caught.exception, AdapterPolicyError)
        self.assertIn(sqlstate_of(caught.exception), {"25006", "42501"})
        self.assertNotIn("hacked", self.adapter.list_tables())

    def test_unparseable_statement_is_refused_before_execution(self):
        """A statement this dialect cannot parse never reaches the server.

        ``ATTACH`` is SQLite syntax; sqlglot cannot parse it as PostgreSQL, so the
        refusal is a typed parse error instead of a policy decision. It is asserted
        here so its absence from ``WRITE_STATEMENTS`` is a documented choice, not a
        silently dropped case.
        """
        with self.assertRaises(AdapterQueryError) as caught:
            self.adapter.execute_readonly("ATTACH '../attached-probe.db' AS other")
        self.assertIn("could not parse", str(caught.exception))

    def test_declared_date_functions_all_run_on_the_server(self):
        """Every declared date function is probed, so the declaration is verified."""
        declared = set(self.adapter.capabilities.date_functions)
        self.assertEqual(declared, set(self.DATE_FUNCTION_PROBES))
        for name, sql in sorted(self.DATE_FUNCTION_PROBES.items()):
            with self.subTest(function=name):
                result = self.adapter.execute_readonly(sql, limit=1)
                self.assertEqual(result.row_count, 1)
                self.assertIsNotNone(result.rows[0][0])

    def test_factory_marker_file_opens_the_server_backend(self):
        """The documented operator path -- a ``*.pg`` marker file -- works end to end."""
        if ALLOW_SUPERUSER:
            self.skipTest(
                "the factory always applies the strict role check; this run allows a "
                "superuser through the direct entry point only"
            )
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "qf_test.pg"
            marker.write_text(f"# QueryForge PostgreSQL target\n{SERVER_DSN}\n", encoding="utf-8")
            adapter = open_database(str(marker))
            try:
                self.assertIsInstance(adapter, DatabaseAdapter)
                self.assertEqual(adapter.dialect, "postgres")
                self.assertTrue(adapter.readonly_role_verified)
                self.assertEqual(adapter.execute_sql("SELECT 1 AS one").rows, [[1]])
            finally:
                adapter.close()

    def test_value_sampling_is_parameterized_and_case_insensitive(self):
        """``find_matching_values`` mirrors the DuckDB helper for the tool layer."""
        self.assertEqual(
            self.adapter.find_matching_values("dim_category", "category_name", ["BO"]),
            ["books"],
        )
        self.assertEqual(
            self.adapter.find_matching_values("dim_category", "region", ["NORTH"]),
            ["north"],
        )
        self.assertEqual(
            self.adapter.find_matching_values("dim_category", "category_name", ["north"]),
            [],
        )
        # No keywords / a non-positive limit are cheap no-ops, not queries.
        self.assertEqual(self.adapter.find_matching_values("dim_category", "region", []), [])
        self.assertEqual(
            self.adapter.find_matching_values("dim_category", "region", ["north"], 0), []
        )
        # A column that does not exist is refused before any SQL is built.
        with self.assertRaises(Exception):
            self.adapter.find_matching_values("dim_category", "nope", ["a"])


# --------------------------------------------------------------------------- #
# 18-N1 across an embedded engine and a server engine
# --------------------------------------------------------------------------- #


@requires_server
class PostgresSqliteEquivalenceTest(ResultComparisonMixin, unittest.TestCase):
    """18-N1: SQLite and PostgreSQL answer the same questions identically.

    **UNVERIFIED without a server** (same gate and same command as the conformance
    class above). Comparison is on *logical* results: the seven portable queries
    project integers, text and dates only, so their normalized rows must be exactly
    equal, while the schema check compares the frozen logical types (PostgreSQL
    reports ``integer``, SQLite echoes ``INTEGER``) and primary-key flags.
    """

    @classmethod
    def setUpClass(cls) -> None:
        cls.temp = tempfile.TemporaryDirectory()
        cls.sqlite_path = Path(cls.temp.name) / "fixture.sqlite"
        build_sqlite_fixture(cls.sqlite_path)
        cls.schema = schema_for("equivalence")
        create_postgres_fixture(SETUP_DSN, cls.schema)
        cls.sqlite = sqlite_adapter(cls.sqlite_path)
        cls.postgres = postgres_adapter(cls.schema)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.sqlite.close()
        cls.postgres.close()
        drop_postgres_fixture(SETUP_DSN, cls.schema)
        cls.temp.cleanup()

    def test_18_n1_same_queries_return_identical_results(self):
        for label, sql in EQUIVALENCE_QUERIES:
            with self.subTest(query=label):
                left = self.sqlite.execute_readonly(sql)
                right = self.postgres.execute_readonly(sql)
                self.assertEqual(left.columns, right.columns)
                self.assertEqual(left.row_count, right.row_count)
                self.assertEqual(left.rows, right.rows)
                self.assertEqual(label != "empty", bool(left.rows))

    def test_18_n1_schema_metadata_parity(self):
        for table in ("dim_category", "fact_orders", "stress_rows"):
            with self.subTest(table=table):
                self.assertEqual(
                    self.sqlite.describe_logical_table(table),
                    self.postgres.describe_logical_table(table),
                )
                self.assertEqual(
                    [
                        column.primary_key
                        for column in self.sqlite.describe_table(table).columns
                    ],
                    [
                        column.primary_key
                        for column in self.postgres.describe_table(table).columns
                    ],
                )
                self.assertEqual(
                    [
                        column.nullable
                        for column in self.sqlite.describe_table(table).columns
                    ],
                    [
                        column.nullable
                        for column in self.postgres.describe_table(table).columns
                    ],
                )

    def test_18_n1_type_conversion_parity(self):
        sql = (
            "SELECT order_date, discount, is_returned, payload, note, amount "
            f"FROM fact_orders WHERE order_id = {PROBE_ORDER_ID}"
        )
        self.assertResultsEquivalent(
            self.sqlite.execute_readonly(sql), self.postgres.execute_readonly(sql)
        )
        # The server keeps DECIMAL scale as exact text, like DuckDB does.
        self.assertEqual(
            self.postgres.execute_readonly(sql).rows[0][1], str(PROBE_ROW["discount"])
        )

    def test_unknown_object_uses_one_typed_error_class(self):
        for sql in ("SELECT * FROM missing_table", "SELECT nope FROM fact_orders"):
            with self.subTest(sql=sql):
                with self.assertRaises(AdapterQueryError) as left:
                    self.sqlite.execute_readonly(sql)
                with self.assertRaises(AdapterQueryError) as right:
                    self.postgres.execute_readonly(sql)
                self.assertIs(type(left.exception), type(right.exception))
                self.assertIs(type(left.exception), AdapterQueryError)

    def test_declared_capability_difference_is_refused_not_guessed(self):
        postgres_only = (
            "SELECT category_name FROM dim_category "
            "WHERE category_name ILIKE 'B%' ORDER BY category_name",
            # sqlglot normalizes DATE_TRUNC to an unpoliced name when reading the
            # postgres dialect, so this runs; SQLite refuses it by declaration.
            "SELECT date_trunc('month', order_date) AS bucket "
            "FROM fact_orders ORDER BY order_id",
        )
        for sql in postgres_only:
            with self.subTest(sql=sql[:48]):
                self.assertTrue(self.postgres.execute_readonly(sql, limit=2).rows)
                with self.assertRaises(AdapterUnsupportedError):
                    self.sqlite.execute_readonly(sql, limit=2)


class PostgresTestCountSanityTest(unittest.TestCase):
    """Guards the two halves of this module against disappearing silently.

    Why: the server half is skipped without a DSN, so a refactor that deleted it -- or
    a decorator that accidentally skipped the always-run half too -- would still leave
    a green suite. This test counts the collected cases on the module object itself.
    """

    def test_both_halves_are_present_in_this_module(self):
        always_run = {
            "test_capabilities_are_declared_and_registered",
            "test_factory_routes_dsn_and_marker_files_without_the_driver",
            "test_sqlite_and_duckdb_paths_stay_driver_free",
            "test_connector_module_never_imports_the_driver_eagerly",
        }
        self.assertLessEqual(
            always_run, set(dir(PostgresContractWiringTest))
        )
        for name in (
            "test_catalog_and_logical_schema",
            "test_18_s1_write_and_admin_sql_is_refused_before_execution",
            "test_18_s1_engine_read_only_role_refuses_writes",
            "test_deadline_interrupts_in_flight_work",
            "test_client_cancel_interrupts_in_flight_work",
            "test_bounded_fetch_streams_and_releases_the_server_cursor",
        ):
            with self.subTest(test=name):
                self.assertTrue(hasattr(PostgresAdapterConformanceTest, name))
        self.assertTrue(hasattr(PostgresSqliteEquivalenceTest, "test_18_n1_same_queries_return_identical_results"))
        self.assertTrue(SERVER_DSN or SERVER_SKIP_REASON)
        if SERVER_DSN:
            # A DSN without the driver would surface as 30-odd confusing failures; fail
            # here first with the one line that fixes it.
            self.assertTrue(
                postgres_driver_available(),
                "install the extra first: pip install -e '.[postgres]'",
            )
        if not SERVER_DSN:
            self.assertTrue(
                getattr(PostgresAdapterConformanceTest, "__unittest_skip__", False)
                or REQUIRE_POSTGRES,
                "the server half must be either skipped loudly or forced to fail",
            )


if __name__ == "__main__":
    unittest.main()
