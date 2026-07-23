"""Read-only SQLite connector used by QueryForge."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from queryforge.core.schemas.models import (
    ExecutionResult,
    ForeignKeyReference,
    TableColumn,
    TableSchema,
)


class SQLiteConnectorError(RuntimeError):
    """Raised when SQLite setup or an operation fails."""


class SQLiteConnector:
    """Open one existing SQLite database with read-only enforcement."""

    def __init__(self, database_path: str) -> None:
        self.database_path = Path(database_path).expanduser().resolve()
        if not self.database_path.is_file():
            raise SQLiteConnectorError(
                f"SQLite database does not exist: {self.database_path}"
            )

        try:
            uri = f"{self.database_path.as_uri()}?mode=ro"
            self._connection = sqlite3.connect(uri, uri=True)
            self._connection.execute("PRAGMA query_only = ON")
        except sqlite3.Error as exc:
            raise SQLiteConnectorError(
                f"Could not open SQLite database {self.database_path}: {exc}"
            ) from exc

    def list_tables(self) -> list[str]:
        try:
            rows = self._connection.execute(
                """
                SELECT name
                FROM sqlite_master
                WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
                ORDER BY name
                """
            ).fetchall()
        except sqlite3.Error as exc:
            raise SQLiteConnectorError(f"Could not list SQLite tables: {exc}") from exc
        return [str(row[0]) for row in rows]

    def describe_table(self, table_name: str) -> TableSchema:
        if table_name not in self.list_tables():
            raise SQLiteConnectorError(f"Unknown SQLite table: {table_name}")

        quoted_name = self._quote_identifier(table_name)
        try:
            rows = self._connection.execute(
                f"PRAGMA table_info({quoted_name})"
            ).fetchall()
        except sqlite3.Error as exc:
            raise SQLiteConnectorError(
                f"Could not describe SQLite table {table_name!r}: {exc}"
            ) from exc

        columns = [
            TableColumn(
                name=str(row[1]),
                data_type=str(row[2] or ""),
                nullable=not bool(row[3]) and not bool(row[5]),
                primary_key=bool(row[5]),
            )
            for row in rows
        ]
        try:
            foreign_key_rows = self._connection.execute(
                f"PRAGMA foreign_key_list({quoted_name})"
            ).fetchall()
        except sqlite3.Error as exc:
            raise SQLiteConnectorError(
                f"Could not inspect foreign keys for {table_name!r}: {exc}"
            ) from exc
        foreign_keys = [
            ForeignKeyReference(
                column=str(row[3]),
                referenced_table=str(row[2]),
                referenced_column=str(row[4]),
            )
            for row in foreign_key_rows
        ]
        return TableSchema(
            table_name=table_name,
            columns=columns,
            foreign_keys=foreign_keys,
        )

    def find_matching_values(
        self,
        table_name: str,
        column_name: str,
        keywords: list[str],
        limit: int = 3,
    ) -> list[str]:
        """Return distinct text values containing question keywords."""
        if limit <= 0 or not keywords:
            return []
        schema = self.describe_table(table_name)
        if column_name not in {column.name for column in schema.columns}:
            raise SQLiteConnectorError(
                f"Unknown column {column_name!r} in SQLite table {table_name!r}"
            )

        quoted_table = self._quote_identifier(table_name)
        quoted_column = self._quote_identifier(column_name)
        patterns = [f"%{self._escape_like(keyword.lower())}%" for keyword in keywords]
        predicates = " OR ".join(
            f"LOWER(CAST({quoted_column} AS TEXT)) LIKE ? ESCAPE '\\'"
            for _ in patterns
        )
        sql = f"""
            SELECT DISTINCT CAST({quoted_column} AS TEXT) AS value
            FROM {quoted_table}
            WHERE {quoted_column} IS NOT NULL AND ({predicates})
            ORDER BY LENGTH(value), value
            LIMIT ?
        """
        try:
            rows = self._connection.execute(sql, [*patterns, limit]).fetchall()
        except sqlite3.Error as exc:
            raise SQLiteConnectorError(
                f"Could not sample values from {table_name}.{column_name}: {exc}"
            ) from exc
        return [str(row[0]) for row in rows]

    def execute_sql(self, sql: str) -> ExecutionResult:
        try:
            cursor = self._connection.execute(sql)
            if cursor.description is None:
                raise SQLiteConnectorError("SQL did not produce a query result")
            columns = [str(column[0]) for column in cursor.description]
            rows = [
                [self._json_safe(value) for value in row]
                for row in cursor.fetchall()
            ]
        except SQLiteConnectorError:
            raise
        except sqlite3.Error as exc:
            raise SQLiteConnectorError(f"SQLite query failed: {exc}") from exc

        return ExecutionResult(columns=columns, rows=rows, row_count=len(rows))

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> "SQLiteConnector":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    @staticmethod
    def _quote_identifier(identifier: str) -> str:
        return '"' + identifier.replace('"', '""') + '"'

    @staticmethod
    def _escape_like(value: str) -> str:
        return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")

    @staticmethod
    def _json_safe(value: object) -> object:
        if isinstance(value, bytes):
            return value.hex()
        return value
