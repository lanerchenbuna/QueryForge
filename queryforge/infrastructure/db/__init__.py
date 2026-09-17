"""Database connectors: the SQLite default path plus the optional DuckDB backend.

Why the lazy exports: importing this package must never import the optional
duckdb driver, so a SQLite-only install keeps working unchanged (18-R1). The
frozen contract, its capability matrix and the value/type normalization rules are
imported eagerly because they only depend on core packages; concrete connectors —
and the driver-dispatching ``open_database`` helper — are resolved on first
attribute access.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING, Any

from queryforge.infrastructure.db.adapter import (
    CAPABILITY_REGISTRY,
    DATE_FUNCTION_VOCABULARY,
    DUCKDB_CAPABILITIES,
    LOGICAL_TYPES,
    MAX_BOUNDED_ROWS,
    PREVIEW_MAX_ROWS,
    SQLITE_CAPABILITIES,
    AdapterCancelledError,
    AdapterCapabilities,
    AdapterError,
    AdapterPolicyError,
    AdapterQueryError,
    AdapterTimeoutError,
    AdapterTypeError,
    AdapterUnavailableError,
    AdapterUnsupportedError,
    ConnectorAdapter,
    DatabaseAdapter,
    adapt_connector,
    capabilities_for_dialect,
    normalize_type,
    normalize_value,
)

if TYPE_CHECKING:  # pragma: no cover - typing only, never imported at runtime
    from queryforge.infrastructure.db.adapters import open_database
    from queryforge.infrastructure.db.duckdb_connector import (
        DuckDBConnector,
        DuckDBConnectorError,
        DuckDBUnavailableError,
    )
    from queryforge.infrastructure.db.sqlite_connector import (
        SQLiteConnector,
        SQLiteConnectorError,
    )

__all__ = [
    "AdapterCancelledError",
    "AdapterCapabilities",
    "AdapterError",
    "AdapterPolicyError",
    "AdapterQueryError",
    "AdapterTimeoutError",
    "AdapterTypeError",
    "AdapterUnavailableError",
    "AdapterUnsupportedError",
    "CAPABILITY_REGISTRY",
    "ConnectorAdapter",
    "DATE_FUNCTION_VOCABULARY",
    "DUCKDB_CAPABILITIES",
    "DatabaseAdapter",
    "DuckDBConnector",
    "DuckDBConnectorError",
    "DuckDBUnavailableError",
    "LOGICAL_TYPES",
    "MAX_BOUNDED_ROWS",
    "PREVIEW_MAX_ROWS",
    "SQLITE_CAPABILITIES",
    "SQLiteConnector",
    "SQLiteConnectorError",
    "adapt_connector",
    "capabilities_for_dialect",
    "normalize_type",
    "normalize_value",
    "open_database",
]

_LAZY_EXPORTS: dict[str, tuple[str, str]] = {
    "SQLiteConnector": (
        "queryforge.infrastructure.db.sqlite_connector",
        "SQLiteConnector",
    ),
    "SQLiteConnectorError": (
        "queryforge.infrastructure.db.sqlite_connector",
        "SQLiteConnectorError",
    ),
    "DuckDBConnector": (
        "queryforge.infrastructure.db.duckdb_connector",
        "DuckDBConnector",
    ),
    "DuckDBConnectorError": (
        "queryforge.infrastructure.db.duckdb_connector",
        "DuckDBConnectorError",
    ),
    "DuckDBUnavailableError": (
        "queryforge.infrastructure.db.duckdb_connector",
        "DuckDBUnavailableError",
    ),
    "open_database": (
        "queryforge.infrastructure.db.adapters",
        "open_database",
    ),
}


def __getattr__(name: str) -> Any:
    """Resolve connector exports on first use (PEP 562), never at import time."""
    target = _LAZY_EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(importlib.import_module(target[0]), target[1])
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(_LAZY_EXPORTS))
