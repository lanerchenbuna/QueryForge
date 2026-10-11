"""Independent SQLite source coverage for opt-in evaluation contracts.

Source coverage is a necessary check, not proof that joins, filters or business
meaning are correct. Only physical sources in scopes used by the final SELECT
are counted. An unused CTE, a table alias and a coincidentally equal result
cannot establish a required source.
"""

from __future__ import annotations

from dataclasses import dataclass

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError
from sqlglot.optimizer.normalize_identifiers import normalize_identifiers
from sqlglot.optimizer.scope import Scope, build_scope


@dataclass(frozen=True)
class SourceCoverage:
    verified: bool
    active_tables: tuple[str, ...] = ()
    missing_tables: tuple[str, ...] = ()
    reason: str | None = None

    @property
    def passed(self) -> bool:
        return self.verified and not self.missing_tables


class _UnsupportedSource(ValueError):
    pass


def check_sql_sources(sql: object, required_tables: list[str]) -> SourceCoverage:
    """Verify physical source names without executing SQL or consulting runtime.

    This contract supports the isolated SQLite fixtures: ASCII identifiers,
    unqualified names and the ``main`` namespace, SELECTs, non-recursive CTEs
    and set operations.
    Other namespaces, recursive CTEs, source functions and unsupported scopes
    return unverified. Values, aliases and the model's own verdict are ignored.
    """

    if not isinstance(required_tables, list) or any(
        not isinstance(name, str) or not name.strip().isascii() or not name.strip().isidentifier()
        for name in required_tables
    ):
        return SourceCoverage(False, reason="unsupported_required_table")
    required = {name.strip().casefold() for name in required_tables}
    if not isinstance(sql, str) or not sql.strip():
        return SourceCoverage(False, reason="missing_sql")
    try:
        statements = sqlglot.parse(sql, read="sqlite")
        if len(statements) != 1 or not isinstance(statements[0], (exp.Select, exp.SetOperation)):
            return SourceCoverage(False, reason="unsupported_statement")
        # SQLGlot's Unicode normalization must not equate distinct SQLite names.
        if any(not node.name.isascii() for node in statements[0].find_all(exp.Identifier)):
            return SourceCoverage(False, reason="unsupported_identifier")
        tree = normalize_identifiers(statements[0], dialect="sqlite")
        if any(node.args.get("recursive") for node in tree.find_all(exp.With)):
            return SourceCoverage(False, reason="recursive_cte")
        root = build_scope(tree)
        if root is None:
            return SourceCoverage(False, reason="unsupported_scope")
        active: set[str] = set()
        visited: set[int] = set()

        def visit(scope: Scope) -> None:
            if id(scope) in visited:
                return
            visited.add(id(scope))
            if not isinstance(scope.expression, (exp.Select, exp.SetOperation)):
                raise _UnsupportedSource("unsupported_scope")
            for _, source in scope.selected_sources.values():
                if isinstance(source, Scope):
                    visit(source)
                elif isinstance(source, exp.Table) and isinstance(source.this, exp.Identifier):
                    if not source.name:
                        raise _UnsupportedSource("unsupported_source")
                    if source.catalog or (source.db and source.db.casefold() != "main"):
                        raise _UnsupportedSource("unsupported_namespace")
                    active.add(source.name.casefold())
                else:
                    raise _UnsupportedSource("unsupported_source")
            for child in (*scope.subquery_scopes, *scope.union_scopes):
                visit(child)

        visit(root)
        missing = required - active
        return SourceCoverage(True, tuple(sorted(active)), tuple(sorted(missing)),
                              "missing_required_tables" if missing else None)
    except _UnsupportedSource as exc:
        return SourceCoverage(False, reason=str(exc))
    except (SqlglotError, ValueError, TypeError, RecursionError):
        return SourceCoverage(False, reason="invalid_or_unsupported_sql")
