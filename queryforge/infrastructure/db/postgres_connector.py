"""Read-only PostgreSQL backend: the server-type database behind the frozen contract.

Why this module exists
----------------------
Step 18 froze the adapter contract (:mod:`queryforge.infrastructure.db.adapter`)
and verified it against two *embedded* engines (SQLite, DuckDB), so "supports a
second database" was only true for engines that need no server. This module is the
server-type backend: a real PostgreSQL server reached over a DSN, with the same
contract surface (catalog/schema listing, engine-enforced read-only execution,
bounded preview, cancellation, plan inspection, one error taxonomy, one set of
value/type normalization rules).

Why psycopg 3 and not asyncpg
-----------------------------
The frozen contract is synchronous -- ``execute_sql``/``cancel``/``close`` are plain
methods and ``_InterruptWatchdog`` interrupts from a ``threading.Timer`` -- while
``asyncpg`` has no synchronous API: driving it from a synchronous contract would
mean owning a private event loop in a background thread and marshalling every call
(and every interrupt) onto it. ``psycopg`` 3 speaks the same protocol with a
synchronous API, ships wheels (``psycopg[binary]``: no compiler needed), and gives
this backend the two primitives the contract needs from a *server* engine:

* a real cancel request (``Connection.cancel_safe()`` / ``Connection.cancel()``,
  i.e. the ``PQcancel``/``pg_cancel_backend`` path) that another thread may send
  while a statement is in flight;
* server-side cursors (``DECLARE`` / ``FETCH FORWARD n``), which is what makes the
  bounded fetch *stream* instead of materializing the whole result on the client.

The driver is imported lazily inside :meth:`PostgresConnector.__init__` and never at
module import time, so a SQLite-only install can import this module, the package and
the factory without the driver (18-R1).

Read-only layering (what each layer is worth)
---------------------------------------------
1. the shared AST policy, applied by ``DatabaseAdapter.execute_readonly`` before the
   server sees the SQL;
2. the session GUC ``default_transaction_read_only``: pinned through the DSN
   ``options`` so the session starts read-only, and re-verified with ``SHOW`` after
   connecting (a server that reports ``off`` is refused);
3. the role expectation: by default the constructor **refuses a superuser session**
   (:func:`readonly_role_problem`), because ``default_transaction_read_only`` is a
   user-settable GUC -- the role itself can switch it back off -- and a superuser is
   not subject to ordinary privilege checks, so a superuser session has no
   engine-enforced boundary. Pass ``require_readonly_role=False`` to accept layers
   1-2 only; the adapter then records ``readonly_role_verified = False``.

Known boundary, stated rather than hidden: PostgreSQL's read-only transactions still
allow writes to *temporary* tables, and ``SELECT ... INTO`` parses as a plain SELECT
for the shared AST policy, so on this backend the server's read-only transaction --
not the AST layer -- is what refuses ``SELECT INTO``. Both are asserted by
``tests/test_postgres_adapter_contract.py`` (the engine half of 18-S1).

Honest boundaries of *this file*
--------------------------------
* The code is written against the psycopg 3 public API and the PostgreSQL catalog;
  the engine-dependent half is verified only by
  ``tests/test_postgres_adapter_contract.py``, which needs a reachable server and the
  ``QUERYFORGE_TEST_POSTGRES_DSN`` environment variable. Without that variable the
  engine half is **unverified** (see the module docstring of that test file and the
  "Running the server-backed suite" section of ``docs/database_adapters.md``).
* Credentials travel in the DSN given by the caller. This layer stores no secret and
  never echoes the DSN: connection failures are reported by driver error *category*
  only, and SQL text never appears in an adapter error message.
* No pooling: one adapter owns exactly one connection (contract rule).
* The tool layer's SQLite progress-handler deadline cannot interrupt a PostgreSQL
  statement (``install_sql_deadline_handler`` needs ``set_progress_handler`` or
  ``interrupt``, neither of which a psycopg connection has). For a real deadline use
  ``DatabaseAdapter.execute_readonly(sql, timeout=...)``, whose watchdog sends a
  cancel request, or set ``statement_timeout`` for the role/server.
"""

from __future__ import annotations

import logging
import math
import re
from typing import Any, Final

from queryforge.core.schemas.models import (
    ExecutionResult,
    ForeignKeyReference,
    TableColumn,
    TableSchema,
)

from .adapter import (
    POSTGRES_CAPABILITIES,
    AdapterCancelledError,
    AdapterError,
    AdapterTimeoutError,
    AdapterUnavailableError,
    DatabaseAdapter,
    normalize_value,
)

LOGGER = logging.getLogger("queryforge.infrastructure.db")

#: Fixed server-side cursor name: a leaked named cursor holds server memory and
#: locks until the session ends, so the name is a constant the suite can assert on.
BOUNDED_CURSOR_NAME: Final = "queryforge_bounded"

#: Pattern for the one identifier this module interpolates into SQL (a schema name,
#: which PostgreSQL does not accept as a bind parameter). Everything else is bound.
_IDENTIFIER: Final = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*")

#: SQLSTATE raised by PostgreSQL when a statement is cancelled (client cancel or
#: ``statement_timeout``); used instead of matching localized message text.
SQLSTATE_QUERY_CANCELED: Final = "57014"


class PostgresConnectorError(AdapterError):
    """Any PostgreSQL backend failure (connection, catalog, query)."""


class PostgresUnavailableError(PostgresConnectorError, AdapterUnavailableError):
    """The optional psycopg driver is missing, or no usable read-only session exists."""


def sqlstate_of(exc: BaseException) -> str | None:
    """Return the SQLSTATE of a failure, walking the ``__cause__`` chain.

    Why: the contract translates the *wrapper* thrown by this backend, while the
    SQLSTATE lives on the psycopg exception underneath it. SQLSTATE is also
    locale-independent, unlike the server's message text.
    """
    current: BaseException | None = exc
    while current is not None:
        sqlstate = getattr(current, "sqlstate", None)
        if sqlstate:
            return str(sqlstate)
        current = current.__cause__ or current.__context__
    return None


def declared_type_name(
    data_type: str,
    *,
    numeric_precision: Any = None,
    numeric_scale: Any = None,
    character_maximum_length: Any = None,
) -> str:
    """Rebuild an auditable type spelling from ``information_schema`` values.

    Why: PostgreSQL reports ``numeric`` for a ``DECIMAL(12,2)`` column and drops the
    modifier, while the contract keeps the raw engine type precisely so the DDL stays
    auditable (DuckDB reports ``DECIMAL(12,2)``). The modifier is re-attached here;
    :func:`~queryforge.infrastructure.db.adapter.normalize_type` ignores modifiers, so
    the frozen logical type is unaffected.
    """
    name = str(data_type or "").strip() or "unknown"
    base = name.split("(", 1)[0].strip().casefold()
    if numeric_precision is not None and base in {"numeric", "decimal"}:
        if numeric_scale is None:
            return f"{name}({int(numeric_precision)})"
        return f"{name}({int(numeric_precision)},{int(numeric_scale)})"
    if character_maximum_length is not None and base in {
        "character",
        "character varying",
        "varchar",
        "char",
        "bit",
        "bit varying",
    }:
        return f"{name}({int(character_maximum_length)})"
    return name


def readonly_role_problem(role: str, *, is_superuser: bool) -> str | None:
    """Return why a session role is not a read-only boundary, or ``None``.

    A superuser is refused because ``default_transaction_read_only`` is a
    user-settable session setting (the role can turn it off again) and a superuser is
    not subject to ordinary privilege checks, so such a session has no
    engine-enforced read-only boundary.
    """
    if is_superuser:
        return (
            f"the PostgreSQL role {role!r} is a superuser, so the session is not a "
            "read-only boundary (the read-only flag is a user-settable session GUC). "
            "Connect as a read-only role, or pass require_readonly_role=False to "
            "accept that only the session flag and the shared AST policy apply "
            "(see docs/database_adapters.md, 'Running the server-backed suite')."
        )
    return None


class PostgresConnector(DatabaseAdapter):
    """PostgreSQL backend implementing the frozen adapter contract."""

    dialect = "postgres"
    capabilities = POSTGRES_CAPABILITIES

    def __init__(
        self,
        dsn: str,
        *,
        schema: str | None = None,
        connect_timeout_seconds: float = 10.0,
        require_readonly_role: bool = True,
    ) -> None:
        if not isinstance(dsn, str) or not dsn.strip():
            raise PostgresConnectorError("a PostgreSQL DSN is required")
        if (
            isinstance(connect_timeout_seconds, bool)
            or not isinstance(connect_timeout_seconds, (int, float))
            or not math.isfinite(connect_timeout_seconds)
            or connect_timeout_seconds <= 0
        ):
            raise ValueError("connect timeout must be a finite positive number of seconds")
        if schema is not None and not _IDENTIFIER.fullmatch(str(schema)):
            raise PostgresConnectorError("invalid PostgreSQL schema name")
        self.connect_timeout_seconds = float(connect_timeout_seconds)
        self.require_readonly_role = bool(require_readonly_role)
        #: Set by the sequence below; ``close()``/``cancel()`` must tolerate the
        #: half-constructed state, so the attribute exists before it is usable.
        self._connection: Any = None
        self._psycopg: Any = None
        self.schema: str = str(schema) if schema is not None else ""
        self.current_role: str = ""
        self.role_is_superuser = False
        self.role_bypasses_rls = False
        self.readonly_role_verified = False
        try:
            import psycopg
        except ImportError as exc:
            # Only *using* the backend requires the driver: importing this module
            # must stay safe for the SQLite-only default install (18-R1).
            raise PostgresUnavailableError(
                "PostgreSQL requires the optional extra: pip install 'queryforge[postgres]'"
            ) from exc
        self._psycopg = psycopg
        try:
            connection = psycopg.connect(
                dsn,
                # Autocommit keeps every statement self-contained: a failed or
                # cancelled read never leaves the session "idle in transaction",
                # which is what keeps an interrupt from poisoning the connection.
                autocommit=True,
                connect_timeout=connect_timeout_seconds,
                application_name="queryforge",
                # Pin the read-only default from the very first statement; the
                # explicit SET below is the belt to this braces.
                options="-c default_transaction_read_only=on",
            )
        except Exception as exc:
            raise PostgresUnavailableError(
                f"cannot connect to the PostgreSQL server ({type(exc).__name__})"
            ) from exc
        self._connection = connection
        try:
            self._verify_readonly_session()
            self.schema = self._resolve_schema(schema)
        except Exception:
            self.close()
            raise

    # ---- lifecycle ------------------------------------------------------- #

    def close(self) -> None:
        """Release the connection (idempotent, never raises)."""
        connection = self._connection
        self._connection = None
        if connection is None:
            return
        try:
            connection.close()
        except Exception:  # noqa: BLE001 - closing must not mask a caller error
            LOGGER.warning("closing the PostgreSQL connection failed", exc_info=True)

    def cancel(self) -> None:
        """Send a PostgreSQL cancel request for the statement in flight.

        ``cancel_safe()`` (psycopg 3.2+, non-blocking on libpq 17) is preferred over
        the legacy ``cancel()``; both send the same ``pg_cancel_backend``-style
        request without touching the connection's own protocol state, which is what
        makes them usable from the contract's watchdog thread. A cancel that cannot
        be sent is logged, not raised: the interrupt is best effort by contract.
        """
        connection = self._connection
        if connection is None or connection.closed:
            return
        try:
            cancel_safe = getattr(connection, "cancel_safe", None)
            if cancel_safe is not None:
                cancel_safe()
            else:  # pragma: no cover - psycopg < 3.2 fallback
                connection.cancel()
        except Exception:  # noqa: BLE001 - best effort, mirrors _InterruptWatchdog
            LOGGER.warning("postgres cancel request failed", exc_info=True)

    # ---- catalog and schema ---------------------------------------------- #

    def list_tables(self) -> list[str]:
        """Return the readable base tables of the adapter's schema."""
        rows = self._fetch(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = %s AND table_type = 'BASE TABLE' "
            "ORDER BY table_name",
            (self.schema,),
        )
        return [str(row[0]) for row in rows]

    def describe_table(self, table_name: str) -> TableSchema:
        """Return columns (raw engine type, nullability), keys and foreign keys."""
        if table_name not in self.list_tables():
            raise PostgresConnectorError(
                f"unknown PostgreSQL table in schema {self.schema!r}: {table_name!r}"
            )
        rows = self._fetch(
            "SELECT column_name, data_type, is_nullable, numeric_precision, "
            "numeric_scale, character_maximum_length "
            "FROM information_schema.columns "
            "WHERE table_schema = %s AND table_name = %s ORDER BY ordinal_position",
            (self.schema, table_name),
        )
        primary = self._primary_key_columns(table_name)
        return TableSchema(
            table_name=table_name,
            foreign_keys=self._foreign_keys(table_name),
            columns=[
                TableColumn(
                    name=str(row[0]),
                    data_type=declared_type_name(
                        row[1],
                        numeric_precision=row[3],
                        numeric_scale=row[4],
                        character_maximum_length=row[5],
                    ),
                    nullable=str(row[2]).upper() == "YES",
                    primary_key=str(row[0]) in primary,
                )
                for row in rows
            ],
        )

    def find_matching_values(
        self,
        table_name: str,
        column_name: str,
        keywords: list[str],
        limit: int = 3,
    ) -> list[str]:
        """Mirror of ``DuckDBConnector.find_matching_values`` for the tool layer.

        Pre-contract helper: ``DatabaseTool`` authorises the table/column before
        calling it, keywords and limit are bound as parameters, and the statement
        still runs under the read-only session.
        """
        if limit <= 0 or not keywords:
            return []
        if column_name not in {
            column.name for column in self.describe_table(table_name).columns
        }:
            raise PostgresConnectorError("unknown PostgreSQL column for value sampling")
        table, column = self._quote(table_name), self._quote(column_name)
        predicate = " OR ".join(
            f"position(lower(%s) in lower(CAST({column} AS TEXT))) > 0"
            for _ in keywords
        )
        # PostgreSQL allows neither an output-column alias inside an ORDER BY expression
        # nor an ORDER BY expression that is missing from a SELECT DISTINCT list, so the
        # short-first ordering is applied to a derived table over the distinct values.
        rows = self._fetch(
            f"SELECT value FROM (SELECT DISTINCT CAST({column} AS TEXT) AS value "
            f"FROM {table} WHERE {predicate}) AS sampled "
            "ORDER BY length(value), value LIMIT %s",
            [*[str(keyword).lower() for keyword in keywords], min(limit, 100)],
        )
        return [str(row[0]) for row in rows]

    # ---- execution primitives -------------------------------------------- #

    def execute_sql(self, sql: str) -> ExecutionResult:
        """Trusted primitive: no policy check, no row bound (see the contract)."""
        return self._run(sql, max_rows=None)

    def _fetch_bounded(self, sql: str, limit: int | None) -> ExecutionResult:
        """Fetch at most ``limit`` rows through a server-side cursor.

        Why a named cursor: a client cursor materializes the whole result before
        ``fetchmany`` can stop, so the bound would only save Python objects rather
        than server work and network traffic. ``DECLARE`` + ``FETCH FORWARD n``
        transmits exactly ``n`` rows and then releases the portal. ``DECLARE`` is only
        allowed inside a transaction block, so the bounded read runs in its own
        explicit transaction -- psycopg starts one even on an autocommit connection --
        which is committed on success and rolled back on failure; that is also what
        keeps a cancelled read from poisoning the session.
        """
        return self._run(sql, max_rows=limit)

    # ---- internals ------------------------------------------------------- #

    def _run(self, sql: str, max_rows: int | None) -> ExecutionResult:
        try:
            if max_rows is None:
                columns, source = self._fetch_all(sql)
            else:
                columns, source = self._fetch_bounded_rows(sql, max_rows)
            rows = [[normalize_value(value) for value in row] for row in source]
        except AdapterError:
            # Normalization failures carry their own actionable message.
            raise
        except Exception as exc:
            self._recover_after_error()
            # Engine messages may echo SQL text and external paths; keep the
            # diagnostics by category and driver type only.
            raise PostgresConnectorError(
                f"PostgreSQL query failed ({type(exc).__name__})"
            ) from exc
        return ExecutionResult(columns=columns, rows=rows, row_count=len(rows))

    def _fetch_all(self, sql: str) -> tuple[list[str], list[Any]]:
        connection = self._require_connection()
        with connection.cursor() as cursor:
            cursor.execute(sql)
            return self._columns(cursor), self._rows(cursor)

    def _fetch_bounded_rows(self, sql: str, max_rows: int) -> tuple[list[str], list[Any]]:
        connection = self._require_connection()
        with connection.transaction():
            with connection.cursor(name=BOUNDED_CURSOR_NAME) as cursor:
                cursor.execute(sql)
                rows = (
                    []
                    if getattr(cursor, "description", None) is None
                    else list(cursor.fetchmany(max_rows))
                )
                return self._columns(cursor), rows

    @staticmethod
    def _columns(cursor: Any) -> list[str]:
        """Column names of an executed cursor, normalized to ``str``."""
        description = getattr(cursor, "description", None) or ()
        return [str(getattr(column, "name", column[0])) for column in description]

    @staticmethod
    def _rows(cursor: Any) -> list[Any]:
        """Rows of an executed cursor; ``[]`` when it produced no result set.

        Why the guard: a statement such as ``SET``/``SHOW``-less control command has no
        result set, and psycopg raises ``ProgrammingError`` if ``fetchall()`` is called
        on it ("the last operation didn't produce records").
        """
        if getattr(cursor, "description", None) is None:
            return []
        return list(cursor.fetchall())

    def _translate_engine_error(
        self,
        exc: BaseException,
        *,
        deadline_expired: bool = False,
        timeout: float | None = None,
    ) -> AdapterError:
        """Map psycopg failures onto the frozen error taxonomy.

        The contract's ``_looks_interrupted`` recognizes an interrupt from the driver
        class name or message. psycopg reports a cancelled statement as
        ``errors.QueryCanceled`` ("QueryCanceled", SQLSTATE 57014, "canceling
        statement due to user request"), which contains neither "interrupt" nor the
        contract's wording, so without this override every client cancel would be
        reported as a plain query error instead of ``AdapterCancelledError``.
        """
        if not deadline_expired and sqlstate_of(exc) == SQLSTATE_QUERY_CANCELED:
            if "statement timeout" in str(exc).casefold():
                return AdapterTimeoutError(
                    f"{self.dialect} query was ended by the server's statement "
                    "timeout; no full result was produced"
                )
            return AdapterCancelledError(
                f"{self.dialect} query was interrupted (client cancel); no full "
                "result was produced"
            )
        return super()._translate_engine_error(
            exc, deadline_expired=deadline_expired, timeout=timeout
        )

    def _verify_readonly_session(self) -> None:
        """Pin and verify the read-only session, then record the role identity."""
        status = str(self._scalar("SHOW default_transaction_read_only") or "")
        if status.casefold() not in {"on", "true", "1"}:
            self._fetch("SET default_transaction_read_only = on")
            status = str(self._scalar("SHOW default_transaction_read_only") or "")
        if status.casefold() not in {"on", "true", "1"}:
            raise PostgresUnavailableError(
                "the PostgreSQL session is not read-only: 'default_transaction_read_only' "
                "did not take effect, so the engine would accept writes"
            )
        role = str(self._scalar("SELECT current_user") or "")
        facts = self._row(
            "SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user"
        )
        self.current_role = role
        self.role_is_superuser = bool(facts[0]) if facts else False
        self.role_bypasses_rls = bool(facts[1]) if facts else False
        # BYPASSRLS is recorded but is not a write privilege, so it does not by itself
        # disqualify the role; a superuser does.
        self.readonly_role_verified = not self.role_is_superuser
        problem = readonly_role_problem(role, is_superuser=self.role_is_superuser)
        if problem and self.require_readonly_role:
            raise PostgresUnavailableError(problem)
        if problem:  # pragma: no cover - only with require_readonly_role=False
            LOGGER.warning(
                "PostgreSQL adapter connected with a superuser role %r: the engine-side "
                "read-only boundary is the session flag alone",
                role,
            )

    def _resolve_schema(self, schema: str | None) -> str:
        """Return the schema this adapter reads, pinning ``search_path`` if given."""
        if schema is None:
            current = self._scalar("SELECT current_schema()")
            return str(current) if current else "public"
        if self._scalar("SELECT to_regnamespace(%s) IS NOT NULL", (str(schema),)) is not True:
            raise PostgresUnavailableError(f"PostgreSQL schema {str(schema)!r} does not exist")
        # A schema name cannot be bound as a parameter; it was validated above.
        self._fetch(f'SET search_path TO "{schema}"')
        return str(schema)

    def _primary_key_columns(self, table_name: str) -> set[str]:
        """Read declared primary keys from ``pg_constraint``.

        Not ``information_schema``: those views hide constraints from a role that does
        not own the table, so a read-only role would see zero primary keys here while
        the same catalog lookup sees them for everyone. Keys therefore come from
        ``pg_catalog``, exactly like the foreign keys below.
        """
        rows = self._fetch(
            "SELECT a.attname FROM pg_catalog.pg_constraint AS c "
            "JOIN pg_catalog.pg_class AS src ON src.oid = c.conrelid "
            "JOIN pg_catalog.pg_namespace AS nsp ON nsp.oid = src.relnamespace "
            "JOIN pg_catalog.pg_attribute AS a "
            "ON a.attrelid = src.oid AND a.attnum = ANY(c.conkey) "
            "WHERE c.contype = 'p' AND nsp.nspname = %s AND src.relname = %s",
            (self.schema, table_name),
        )
        return {str(row[0]) for row in rows}

    def _foreign_keys(self, table_name: str) -> list[ForeignKeyReference]:
        """Read declared foreign keys from ``pg_constraint``.

        Column pairs are unnested positionally (``conkey``/``confkey``) so composite
        keys pair correctly, which the ``information_schema`` views make awkward.
        """
        rows = self._fetch(
            "SELECT a.attname, ref.relname, refa.attname "
            "FROM pg_catalog.pg_constraint AS c "
            "JOIN pg_catalog.pg_class AS src ON src.oid = c.conrelid "
            "JOIN pg_catalog.pg_namespace AS nsp ON nsp.oid = src.relnamespace "
            "JOIN pg_catalog.pg_class AS ref ON ref.oid = c.confrelid "
            "CROSS JOIN LATERAL unnest(c.conkey, c.confkey) AS k(con, conf) "
            "JOIN pg_catalog.pg_attribute AS a "
            "ON a.attrelid = src.oid AND a.attnum = k.con "
            "JOIN pg_catalog.pg_attribute AS refa "
            "ON refa.attrelid = ref.oid AND refa.attnum = k.conf "
            "WHERE c.contype = 'f' AND nsp.nspname = %s AND src.relname = %s "
            "ORDER BY a.attname",
            (self.schema, table_name),
        )
        return [
            ForeignKeyReference(
                column=str(column),
                referenced_table=str(referenced_table),
                referenced_column=str(referenced_column),
            )
            for column, referenced_table, referenced_column in rows
        ]

    def _recover_after_error(self) -> None:
        """Return the session to a usable state without hiding the original error.

        The contract promises the connection stays usable after an interrupt; a
        cancelled statement can leave an explicit transaction aborted.
        """
        connection = self._connection
        if connection is None or connection.closed:
            return
        try:
            status = connection.info.transaction_status
            aborted = (
                self._psycopg.pq.TransactionStatus.INTRANS,
                self._psycopg.pq.TransactionStatus.INERROR,
            )
            if status in aborted:
                connection.rollback()
        except Exception:  # noqa: BLE001 - recovery must never replace the real error
            LOGGER.warning("PostgreSQL session recovery failed", exc_info=True)

    def _require_connection(self) -> Any:
        connection = self._connection
        if connection is None or connection.closed:
            raise PostgresConnectorError("the PostgreSQL adapter is closed")
        return connection

    def _fetch(self, sql: str, params: Any = None) -> list[Any]:
        """Run an internal catalog/control statement on the read-only session.

        Internal helper: no policy check (the statements are this module's own) and no
        row bound (catalog reads are small). Failures are reported by category only.
        """
        connection = self._require_connection()
        try:
            with connection.cursor() as cursor:
                cursor.execute(sql, params)
                return self._rows(cursor)
        except Exception as exc:
            self._recover_after_error()
            raise PostgresConnectorError(
                f"PostgreSQL statement failed ({type(exc).__name__})"
            ) from exc

    def _scalar(self, sql: str, params: Any = None) -> Any:
        rows = self._fetch(sql, params)
        return rows[0][0] if rows else None

    def _row(self, sql: str, params: Any = None) -> Any:
        rows = self._fetch(sql, params)
        return rows[0] if rows else None

    @staticmethod
    def _quote(value: str) -> str:
        return '"' + str(value).replace('"', '""') + '"'
