"""Factory for read-only database adapters.

This module used to declare a *second*, smaller ``DatabaseAdapter`` Protocol next
to the frozen contract in :mod:`queryforge.infrastructure.db.adapter`, which left
two competing abstractions for the same responsibility (a reviewer flagged it).
The factory now returns the contract type and the duplicate Protocol is gone:
callers that need the richer surface (capability declarations, bounded reads,
normalisation, the error taxonomy) import ``DatabaseAdapter`` from ``adapter``.

Optional drivers are still imported only when the matching backend is requested,
so importing this module never imports ``duckdb`` or ``psycopg``.

Three backends are routable:

* ``*.sqlite`` (or anything unrecognized) -> the SQLite default path;
* ``*.duckdb`` -> the embedded DuckDB backend;
* a PostgreSQL DSN (``postgres://`` / ``postgresql://``) or a ``*.pg``/``*.pgsql``/
  ``*.postgres`` marker file -> the server backend. A marker file is a small text
  file whose first non-empty, non-``#`` line is the DSN, which keeps credentials out
  of command lines, config files and this repository; it is read by
  :func:`resolve_postgres_dsn` and never echoed into an error message.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

from queryforge.infrastructure.db.adapter import (
    AdapterCapabilities,
    AdapterError,
    AdapterUnavailableError,
    DatabaseAdapter,
    adapt_connector,
)

__all__ = [
    "AdapterCapabilities",
    "AdapterError",
    "DatabaseAdapter",
    "adapt_connector",
    "is_postgres_target",
    "open_database",
    "open_postgres",
    "resolve_postgres_dsn",
]

#: DSN schemes that route to the server backend.
POSTGRES_DSN_SCHEMES: Final[tuple[str, ...]] = ("postgres://", "postgresql://")
#: Suffixes that mark a local file holding the DSN on its first usable line.
POSTGRES_MARKER_SUFFIXES: Final[tuple[str, ...]] = (".pg", ".pgsql", ".postgres")


def is_postgres_target(target: str) -> bool:
    """Return whether ``target`` addresses a PostgreSQL server.

    Detection is explicit and side-effect free: a DSN scheme, or a marker-file
    suffix. Nothing about a file's contents is inspected here, so routing can be
    decided (and asserted) without a driver and without touching the filesystem.
    """
    text = str(target or "").strip()
    if not text:
        return False
    return text.casefold().startswith(POSTGRES_DSN_SCHEMES) or Path(
        text
    ).suffix.casefold() in POSTGRES_MARKER_SUFFIXES


def resolve_postgres_dsn(target: str) -> str:
    """Return the DSN a PostgreSQL ``target`` stands for.

    A DSN is returned verbatim. A marker file is read and must contain at least one
    non-empty, non-comment line. Errors name the file, never its contents: the DSN
    usually carries a password, and adapter errors are logged and shown to users.
    """
    text = str(target or "").strip()
    if text.casefold().startswith(POSTGRES_DSN_SCHEMES):
        return text
    marker = Path(text).expanduser()
    if not marker.is_file():
        raise AdapterUnavailableError(
            f"PostgreSQL marker file does not exist: {marker.name!r}"
        )
    try:
        lines = marker.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise AdapterUnavailableError(
            f"PostgreSQL marker file is unreadable: {marker.name!r}"
        ) from exc
    for line in lines:
        candidate = line.strip()
        if candidate and not candidate.startswith("#"):
            return candidate
    raise AdapterUnavailableError(
        f"PostgreSQL marker file {marker.name!r} contains no DSN line"
    )


def open_postgres(
    dsn: str,
    *,
    schema: str | None = None,
    require_readonly_role: bool = True,
) -> DatabaseAdapter:
    """Open the read-only PostgreSQL backend explicitly.

    ``require_readonly_role`` (default) refuses a superuser session: see
    :func:`queryforge.infrastructure.db.postgres_connector.readonly_role_problem`.
    """
    from .postgres_connector import PostgresConnector

    return PostgresConnector(
        dsn, schema=schema, require_readonly_role=require_readonly_role
    )


def open_database(database_path: str) -> DatabaseAdapter:
    """Open a read-only adapter for the backend implied by the target string."""
    if is_postgres_target(database_path):
        return open_postgres(resolve_postgres_dsn(database_path))
    if Path(database_path).suffix.casefold() == ".duckdb":
        from .duckdb_connector import DuckDBConnector

        return DuckDBConnector(database_path)
    from .sqlite_connector import SQLiteConnector

    return adapt_connector(SQLiteConnector(database_path))
