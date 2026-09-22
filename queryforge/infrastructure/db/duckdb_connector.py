"""Restricted local DuckDB backend. SQL policy is additionally enforced by DatabaseTool."""
import math
import threading
from pathlib import Path

from queryforge.core.schemas.models import ExecutionResult, TableColumn, TableSchema, ForeignKeyReference
from .adapter import (
    AdapterError,
    AdapterUnavailableError,
    DUCKDB_CAPABILITIES,
    DatabaseAdapter,
    normalize_value,
)


class DuckDBConnectorError(AdapterError):
    """Any DuckDB backend failure (unavailable driver, connection, query)."""


class DuckDBUnavailableError(DuckDBConnectorError, AdapterUnavailableError):
    """The optional duckdb driver is not installed in this environment (18-R1)."""


class DuckDBConnector(DatabaseAdapter):
    dialect = "duckdb"
    capabilities = DUCKDB_CAPABILITIES

    def __init__(self, database_path: str, *, timeout_seconds: float = 30):
        if not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 300:
            raise ValueError("SQL timeout must be finite and between 0 and 300 seconds")
        self.timeout_seconds = timeout_seconds
        try:
            import duckdb
        except ImportError as exc:
            # Only *using* the backend requires the driver: importing this module
            # must stay safe for the SQLite-only default install.
            raise DuckDBUnavailableError(
                "DuckDB requires the optional extra: pip install 'queryforge[duckdb]'"
            ) from exc
        self.database_path = Path(database_path).expanduser().resolve()
        if not self.database_path.is_file():
            raise DuckDBConnectorError("DuckDB database does not exist")
        try:
            self._connection = duckdb.connect(str(self.database_path), read_only=True, config={
                "enable_external_access": False, "autoload_known_extensions": False,
                "autoinstall_known_extensions": False, "threads": 2, "memory_limit": "512MB",
            })
            self._connection.execute("SET lock_configuration = true")
        except Exception as exc:
            if hasattr(self, "_connection"):
                self._connection.close()
            raise DuckDBConnectorError("Cannot open read-only DuckDB database") from exc

    def list_tables(self) -> list[str]:
        return [r[0] for r in self._connection.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema='main' "
            "AND table_catalog=current_database() AND table_type='BASE TABLE' ORDER BY table_name"
        ).fetchall()]

    def describe_table(self, table_name: str) -> TableSchema:
        if table_name not in self.list_tables():
            raise DuckDBConnectorError(f"Unknown DuckDB table: {table_name}")
        rows = self._connection.execute(
            "SELECT column_name,data_type,is_nullable FROM information_schema.columns "
            "WHERE table_catalog=current_database() AND table_schema='main' AND table_name=? ORDER BY ordinal_position",
            [table_name],
        ).fetchall()
        primary = {c for r in self._connection.execute(
            "SELECT constraint_column_names FROM duckdb_constraints() "
            "WHERE database_name=current_database() AND schema_name='main' AND table_name=? AND constraint_type='PRIMARY KEY'",
            [table_name]).fetchall() for c in r[0]}
        foreign_keys = [ForeignKeyReference(column=column,referenced_table=row[1],referenced_column=referenced)
                        for row in self._connection.execute(
                            "SELECT constraint_column_names,referenced_table,referenced_column_names FROM duckdb_constraints() "
                            "WHERE database_name=current_database() AND schema_name='main' AND table_name=? AND constraint_type='FOREIGN KEY'",
                            [table_name]).fetchall() for column,referenced in zip(row[0],row[2])]
        return TableSchema(table_name=table_name, foreign_keys=foreign_keys, columns=[TableColumn(
            name=r[0], data_type=r[1], nullable=r[2] == 'YES', primary_key=r[0] in primary
        ) for r in rows])

    def find_matching_values(self, table_name, column_name, keywords, limit=3):
        if limit <= 0 or not keywords:
            return []
        if column_name not in {c.name for c in self.describe_table(table_name).columns}:
            raise DuckDBConnectorError("Unknown column")
        table, col = self._quote(table_name), self._quote(column_name)
        predicate = " OR ".join(f"contains(lower(CAST({col} AS VARCHAR)), ?)" for _ in keywords)
        rows = self._connection.execute(
            f"SELECT DISTINCT CAST({col} AS VARCHAR) AS value FROM {table} WHERE {predicate} "
            "ORDER BY length(value), value LIMIT ?", [*[k.lower() for k in keywords], min(limit, 100)]).fetchall()
        return [r[0] for r in rows]

    def execute_sql(self, sql: str) -> ExecutionResult:
        return self._run(sql, max_rows=None)

    def _fetch_bounded(self, sql: str, limit):
        """Fetch at most ``limit`` rows: DuckDB streams, so stop pulling early."""
        return self._run(sql, max_rows=limit)

    def _run(self, sql: str, max_rows: int | None) -> ExecutionResult:
        deadline = threading.Timer(self.timeout_seconds, self.cancel)
        deadline.daemon = True
        deadline.start()
        try:
            cursor = self._connection.execute(sql)
            columns = [c[0] for c in cursor.description]
            source = cursor.fetchall() if max_rows is None else cursor.fetchmany(max_rows)
            rows = [[self._json_safe(v) for v in r] for r in source]
        except AdapterError:
            # Normalization failures carry their own actionable message.
            raise
        except Exception as exc:
            # Engine messages may echo external paths; retain diagnostics by
            # category only, but keep the driver exception type name so callers
            # can still recognize an interrupted query.
            raise DuckDBConnectorError(f"DuckDB query failed ({type(exc).__name__})") from exc
        finally:
            deadline.cancel()
            deadline.join()
        return ExecutionResult(columns=columns, rows=rows, row_count=len(rows))

    def cancel(self):
        self._connection.interrupt()

    # ``explain`` is inherited from the contract: it capability-checks, applies the
    # same AST policy engine and uses ``capabilities.explain_prefix`` ("EXPLAIN"),
    # so a denied plan request raises the one contract error class on every backend.

    def close(self):
        self._connection.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    @staticmethod
    def _quote(value):
        return '"' + value.replace('"', '""') + '"'

    @staticmethod
    def _json_safe(value):
        """Delegate to the frozen contract normalization (one set of rules)."""
        return normalize_value(value)
