"""Frozen adapter contract for read-only analytics databases.

Why this module exists
----------------------
QueryForge grew up on SQLite. The SQL policy engine, the semantic compiler, the
result renderer and the budget/deadline code must not learn a new dialect every
time a backend is added, so this module freezes the *contract* those callers can
rely on: catalog/schema listing, engine-enforced read-only execution, bounded
preview, cancellation, plan inspection, explicit capability declarations and
backend-independent value/type normalization.

Design rules
------------
* The contract is small on purpose. Everything a backend cannot do is declared
  in :class:`AdapterCapabilities` instead of being guessed from a dialect name.
* A backend that does not declare a capability must *refuse* SQL that needs it
  (:class:`AdapterUnsupportedError`) rather than emit a possibly wrong query.
* Normalization (:func:`normalize_value` / :func:`normalize_type`) is the single
  place where driver types become JSON-safe scalars and frozen logical types, so
  result rendering and downstream comparisons stay backend independent.
* Adding a backend must not add a dependency for the default install: this
  module imports no driver, and importing a concrete connector never imports its
  driver eagerly.

What the contract does NOT promise (honest boundaries)
-----------------------------------------------------
* **Write prevention is layered, not absolute.** ``execute_readonly`` refuses
  write/admin SQL through the shared AST policy engine *and* relies on the
  engine's read-only role (``sqlite3`` ``mode=ro`` + ``PRAGMA query_only``,
  DuckDB ``read_only=True``). The contract does not promise that a backend can
  stop every statement when the policy layer is bypassed: on SQLite the engine
  still accepts ``ATTACH`` of another file (writes *into* an attached database
  are refused by ``query_only``), which is exactly why the AST layer is
  load-bearing. :meth:`DatabaseAdapter.execute_sql` is a trusted primitive: a
  caller that invokes it directly has already waived the policy layer.
* **Credentials are not handled here.** Adapters open local files with the OS
  user's permissions. There is no credential vault, no DSN passthrough, no
  network authentication and no per-domain authorisation in this layer.
* **No pooling.** One adapter owns exactly one connection; connections are never
  shared between domains or reused across runs. Callers must ``close()`` them.
* **No cost model.** ``explain`` returns an engine plan, not a calibrated cost or
  wall-time estimate; ``capabilities.cost_estimates`` says whether a backend
  declares one at all.
* **No full type fidelity.** Unknown driver types are rejected instead of being
  silently stringified, and DECIMAL exactness survives only as text (see
  :func:`normalize_value`); a backend whose driver returns floats for
  decimal-typed columns cannot recover the declared scale.
* **Cancellation is best effort.** ``cancel()`` interrupts in-flight engine work
  using the driver's interrupt primitive; it cannot cancel a network model call
  and it cannot undo an engine's partial side effect.
"""

from __future__ import annotations

import logging
import math
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date, datetime, time, timezone
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Final, Mapping

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError
from sqlglot.optimizer.scope import traverse_scope

from queryforge.core.schemas.models import (
    ExecutionResult,
    SqlPolicyDecision,
    TableSchema,
)

if TYPE_CHECKING:  # pragma: no cover - typing only, keeps this module driver-free
    from queryforge.infrastructure.tools.database_tool import DatabaseTool

LOGGER = logging.getLogger("queryforge.infrastructure.db")

#: Hard cap for a single bounded read; protects callers from their own limits.
MAX_BOUNDED_ROWS: Final = 10_000
#: Display-oriented preview cap, kept aligned with DatabaseTool.execute_sql_preview.
PREVIEW_MAX_ROWS: Final = 100


class AdapterError(RuntimeError):
    """Base class for every failure raised by the adapter contract."""


class AdapterUnavailableError(AdapterError):
    """The backend cannot be used here: missing driver, file or connection."""


class AdapterQueryError(AdapterError):
    """The engine refused or failed the statement (unknown column, syntax...)."""


class AdapterPolicyError(AdapterError):
    """The shared AST policy refused the SQL before the engine saw it."""

    def __init__(self, message: str, decision: SqlPolicyDecision | None = None) -> None:
        self.decision = decision
        super().__init__(message)


class AdapterCancelledError(AdapterQueryError):
    """In-flight work was interrupted (client cancel or an expired deadline)."""


class AdapterTimeoutError(AdapterCancelledError):
    """In-flight work was interrupted because the caller's deadline expired."""


class AdapterUnsupportedError(AdapterQueryError):
    """The SQL needs a feature this backend declares unsupported."""


class AdapterTypeError(AdapterError):
    """A driver value has no honest, backend-independent normalization."""


# --------------------------------------------------------------------------- #
# Capability + dialect declaration
# --------------------------------------------------------------------------- #

#: Portability vocabulary for date/time functions. Only these names are policed
#: by :meth:`DatabaseAdapter.check_capabilities`; unknown functions are left to
#: the engine because the contract does not pretend to know every dialect.
DATE_FUNCTION_VOCABULARY: Final[frozenset[str]] = frozenset(
    {
        "date",
        "date_add",
        "date_bin",
        "date_diff",
        "date_part",
        "date_sub",
        "date_trunc",
        "datetime",
        "epoch_ms",
        "extract",
        "julianday",
        "strftime",
        "time",
        "timediff",
        "to_timestamp",
        "unixepoch",
    }
)


@dataclass(frozen=True)
class AdapterCapabilities:
    """Truthful, backend-declared feature set used to refuse unportable SQL.

    Why explicit: SQL generation must branch on declared capabilities instead of
    guessing from a dialect string, and a silently mistranslated query is a
    correctness bug rather than a rendering difference.
    """

    dialect: str
    window_functions: bool = True
    cte: bool = True
    ilike: bool = False
    qualify: bool = False
    #: How the backend expresses row bounds: "limit" today; kept as a string so a
    #: backend using FETCH FIRST can declare it without a new contract version.
    limit_style: str = "limit"
    #: Date/time functions that are declared *supported*; must be a subset of
    #: :data:`DATE_FUNCTION_VOCABULARY` and must actually run on the engine.
    date_functions: frozenset[str] = field(default_factory=frozenset)
    #: Documented integer-division behaviour, e.g. truncating (SQLite) vs
    #: fractional (DuckDB); callers must cast explicitly for portable ratios.
    integer_division: str = "backend specific"
    #: Prefix that turns a SELECT into an engine plan request.
    explain_prefix: str = "EXPLAIN"
    readonly_enforced_by_engine: bool = True
    cancellation: bool = True
    explain: bool = True
    cost_estimates: bool = False

    def as_dict(self) -> dict[str, Any]:
        """Render the declaration for documentation or telemetry."""
        return {
            "dialect": self.dialect,
            "window_functions": self.window_functions,
            "cte": self.cte,
            "ilike": self.ilike,
            "qualify": self.qualify,
            "limit_style": self.limit_style,
            "date_functions": sorted(self.date_functions),
            "integer_division": self.integer_division,
            "readonly_enforced_by_engine": self.readonly_enforced_by_engine,
            "cancellation": self.cancellation,
            "explain": self.explain,
            "cost_estimates": self.cost_estimates,
        }


SQLITE_CAPABILITIES: Final = AdapterCapabilities(
    dialect="sqlite",
    window_functions=True,
    cte=True,
    ilike=False,
    qualify=False,
    limit_style="limit",
    date_functions=frozenset(
        {"date", "datetime", "julianday", "strftime", "time", "unixepoch"}
    ),
    integer_division="truncates; cast the numerator explicitly for ratios",
    explain_prefix="EXPLAIN QUERY PLAN",
    readonly_enforced_by_engine=True,
    cancellation=True,
    explain=True,
    cost_estimates=False,
)

DUCKDB_CAPABILITIES: Final = AdapterCapabilities(
    dialect="duckdb",
    window_functions=True,
    cte=True,
    ilike=True,
    qualify=True,
    limit_style="limit",
    date_functions=frozenset(
        {
            "date",
            "date_add",
            "date_diff",
            "date_part",
            "date_sub",
            "date_trunc",
            "epoch_ms",
            "extract",
            "strftime",
            "to_timestamp",
        }
    ),
    integer_division="fractional; cast explicitly for portability",
    explain_prefix="EXPLAIN",
    readonly_enforced_by_engine=True,
    cancellation=True,
    explain=True,
    cost_estimates=False,
)

POSTGRES_CAPABILITIES: Final = AdapterCapabilities(
    dialect="postgres",
    window_functions=True,
    cte=True,
    ilike=True,
    # PostgreSQL has no QUALIFY clause. sqlglot can rewrite QUALIFY into a derived
    # table, but that rewriting belongs to SQL generation, not to an adapter that is
    # required to refuse rather than silently mistranslate.
    qualify=False,
    limit_style="limit",
    # Declared functions are the ones PostgreSQL itself provides and the new
    # backend's conformance suite probes one by one. Note that when sqlglot reads the
    # ``postgres`` dialect it normalizes some of these names (``date_trunc`` becomes
    # ``timestamp_trunc``, ``to_timestamp`` becomes ``unix_to_time``), so only names
    # sqlglot leaves alone are actually policed by ``check_capabilities``; unknown
    # names are left to the engine by contract.
    date_functions=frozenset(
        {"date_bin", "date_part", "date_trunc", "extract", "to_timestamp"}
    ),
    integer_division="truncates; cast the numerator explicitly for ratios",
    explain_prefix="EXPLAIN",
    readonly_enforced_by_engine=True,
    cancellation=True,
    explain=True,
    # ``EXPLAIN`` text does carry planner ``cost=`` estimates, but those are the
    # planner's own arbitrary units, not the calibrated cost model this flag
    # promises; declaring False keeps the escalation path open (and matches DuckDB,
    # whose plan output also prints estimates).
    cost_estimates=False,
)

#: Single declaration point for the frozen capability matrix.
CAPABILITY_REGISTRY: Final[Mapping[str, AdapterCapabilities]] = {
    SQLITE_CAPABILITIES.dialect: SQLITE_CAPABILITIES,
    DUCKDB_CAPABILITIES.dialect: DUCKDB_CAPABILITIES,
    POSTGRES_CAPABILITIES.dialect: POSTGRES_CAPABILITIES,
}


def capabilities_for_dialect(dialect: str) -> AdapterCapabilities:
    """Return the declared capabilities for a dialect name.

    Unknown dialects get a conservative declaration (nothing but the features
    every SQL engine has), never optimistic defaults.
    """
    declared = CAPABILITY_REGISTRY.get(dialect)
    if declared is not None:
        return declared
    return AdapterCapabilities(dialect=dialect, ilike=False, qualify=False)


# --------------------------------------------------------------------------- #
# Value and type normalization
# --------------------------------------------------------------------------- #

#: Frozen logical type vocabulary returned by :func:`normalize_type`.
LOGICAL_TYPES: Final[frozenset[str]] = frozenset(
    {
        "integer",
        "float",
        "decimal",
        "boolean",
        "text",
        "binary",
        "date",
        "time",
        "timestamp",
        "json",
        "unknown",
    }
)

_TYPE_ALIASES: Final[Mapping[str, str]] = {
    "int": "integer",
    "int2": "integer",
    "int4": "integer",
    "int8": "integer",
    "smallint": "integer",
    "integer": "integer",
    "bigint": "integer",
    "hugeint": "integer",
    "tinyint": "integer",
    "utinyint": "integer",
    "usmallint": "integer",
    "uinteger": "integer",
    "ubigint": "integer",
    "serial": "integer",
    "real": "float",
    "float": "float",
    "float4": "float",
    "float8": "float",
    "double": "float",
    "double precision": "float",
    "numeric": "decimal",
    "decimal": "decimal",
    "bool": "boolean",
    "boolean": "boolean",
    "logical": "boolean",
    "char": "text",
    "character": "text",
    "character varying": "text",
    "varchar": "text",
    "text": "text",
    "string": "text",
    "uuid": "text",
    "enum": "text",
    "blob": "binary",
    "bytea": "binary",
    "binary": "binary",
    "varbinary": "binary",
    "date": "date",
    "time": "time",
    "time with time zone": "time",
    "timestamp": "timestamp",
    "timestamp with time zone": "timestamp",
    "timestamp without time zone": "timestamp",
    "timestamptz": "timestamp",
    "timestamp_s": "timestamp",
    "timestamp_ms": "timestamp",
    "timestamp_ns": "timestamp",
    "datetime": "timestamp",
    "json": "json",
    "jsonb": "json",
}


def normalize_type(data_type: str | None) -> str:
    """Map a declared or engine-reported type name to a frozen logical type.

    The mapping ignores parameters (``DECIMAL(12,2)`` -> ``decimal``) and
    whitespace, so the same DDL yields the same logical type on every backend,
    including SQLite, whose declared type names are free-form.
    """
    if not data_type or not str(data_type).strip():
        return "unknown"
    name = str(data_type).strip().casefold()
    if "(" in name:
        name = name.split("(", 1)[0].strip()
    name = " ".join(name.split())
    if name.endswith("[]"):
        return "unknown"  # arrays/structs are not rendered by this contract
    if name.startswith("decimal") or name.startswith("numeric"):
        return "decimal"
    if name.startswith("timestamp") or name.startswith("datetime"):
        return "timestamp"
    return _TYPE_ALIASES.get(name, "unknown")


def _render_value(value: Any) -> Any:
    """Return the single normalized rendering of a driver value.

    Rules (frozen; both backends must return the same shape for the same value):

    ===========================  ===============================================
    driver value                 normalized value
    ===========================  ===============================================
    ``None``                     ``None`` (SQL NULL stays JSON null)
    ``bool``                     ``bool`` (checked before ``int``)
    ``int``                      ``int``
    ``float`` (finite)           ``float``; non-finite raises AdapterTypeError
    ``Decimal``                  exact decimal text, e.g. ``"12.50"``
    ``date``                     ``"YYYY-MM-DD"``
    ``datetime`` (aware)         UTC ISO-8601, e.g. ``"2024-01-01T00:00:00+00:00"``
    ``datetime`` (naive)         ISO-8601 without offset, unchanged wall clock
    ``time``                     ``"HH:MM:SS[.ffffff]"``
    ``bytes``/``bytearray``      lowercase hex text
    ``str``                      ``str``
    anything else                raises :class:`AdapterTypeError`
    ===========================  ===============================================

    Why text for DECIMAL: JSON has no exact decimal, and ``float`` would silently
    lose precision. Callers that need a number can parse it; callers that need
    portability get identical text from every backend that keeps decimal scale.
    """
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise AdapterTypeError(
                "non-finite float is not JSON-representable; CAST it or filter it"
            )
        return value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, bytes):
        return value.hex()
    if isinstance(value, (bytearray, memoryview)):
        return bytes(value).hex()
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            value = value.astimezone(timezone.utc)
        return value.isoformat()
    if isinstance(value, (date, time)):
        return value.isoformat()
    raise AdapterTypeError(
        f"unsupported result type {type(value).__name__!r}; CAST it to a scalar "
        "in SQL instead of relying on driver-specific objects"
    )


def normalize_value(value: Any) -> Any:
    """Public form of the frozen value normalization (see :func:`_render_value`)."""
    return _render_value(value)


def _function_names(tree: exp.Expression) -> set[str]:
    """Collect lowercase function names from a parsed statement."""
    names: set[str] = set()
    for function in tree.find_all(exp.Func):
        if isinstance(function, exp.Anonymous):
            name = function.name
        else:
            name = function.sql_name()
        if name:
            names.add(str(name).casefold())
    return names


def _looks_interrupted(exc: BaseException) -> bool:
    """Detect an engine interrupt without importing a driver-specific class."""
    current: BaseException | None = exc
    while current is not None:
        if "interrupt" in type(current).__name__.casefold():
            return True
        if "interrupt" in str(current).casefold():
            return True
        current = current.__cause__ or current.__context__
    return False


class _InterruptWatchdog:
    """Best-effort deadline: interrupt the engine when the caller's budget ends."""

    def __init__(self, adapter: "DatabaseAdapter", timeout: float | None) -> None:
        self._adapter = adapter
        self._timeout = timeout
        self._timer: threading.Timer | None = None
        self.fired = False

    def __enter__(self) -> "_InterruptWatchdog":
        if self._timeout is not None:
            self._timer = threading.Timer(self._timeout, self._fire)
            self._timer.daemon = True
            self._timer.start()
        return self

    def _fire(self) -> None:
        self.fired = True
        try:
            self._adapter.cancel()
        except Exception:  # noqa: BLE001 - a failed interrupt must not kill the run
            LOGGER.warning("adapter interrupt on deadline failed", exc_info=True)

    def __exit__(self, *_: object) -> None:
        # Cancel *and* join so a late interrupt can never hit the next statement.
        if self._timer is not None:
            self._timer.cancel()
            self._timer.join()


# --------------------------------------------------------------------------- #
# The contract
# --------------------------------------------------------------------------- #


class DatabaseAdapter(ABC):
    """Read-only analytics database adapter.

    Implementations provide the primitives (:meth:`list_tables`,
    :meth:`describe_table`, :meth:`execute_sql`, :meth:`cancel`, :meth:`close`)
    plus a truthful :attr:`dialect`/:attr:`capabilities` declaration. Everything
    derived from them — capability checks, policy enforcement, bounded reads,
    previews and normalization — is implemented once, here, so backends cannot
    drift apart.
    """

    dialect: str = "unknown"
    capabilities: AdapterCapabilities = AdapterCapabilities(dialect="unknown")
    #: Cached DatabaseTool built on demand by :meth:`_policy_tool`.
    _policy_tool_cache: "DatabaseTool | None" = None

    # ---- lifecycle ------------------------------------------------------- #

    def connect(self) -> "DatabaseAdapter":
        """Return a connected read-only handle.

        Connectors in this repository connect in their constructor, so the
        default is idempotent and returns ``self``; a backend may override it to
        connect lazily but must never open a writable connection.
        """
        return self

    @abstractmethod
    def close(self) -> None:
        """Release the connection and any driver resources."""

    def __enter__(self) -> "DatabaseAdapter":
        return self.connect()

    def __exit__(self, *_: object) -> None:
        self.close()

    # ---- catalog and schema ---------------------------------------------- #

    @abstractmethod
    def list_tables(self) -> list[str]:
        """Return the readable base tables of the current catalog/schema."""

    @abstractmethod
    def describe_table(self, table_name: str) -> TableSchema:
        """Return columns (name, raw ``data_type``, ``nullable``) and keys."""

    def describe_logical_table(self, table_name: str) -> list[tuple[str, str, bool]]:
        """Return ``(column, frozen logical type, nullable)`` without a dialect.

        Why: SQL generation and semantic validation want a backend-independent
        view of the schema; ``describe_table`` keeps the raw engine type so the
        original DDL stays auditable.
        """
        return [
            (column.name, normalize_type(column.data_type), column.nullable)
            for column in self.describe_table(table_name).columns
        ]

    # ---- execution primitives -------------------------------------------- #

    @abstractmethod
    def execute_sql(self, sql: str) -> ExecutionResult:
        """Run SQL on the engine and return normalized rows.

        Trusted primitive: it performs **no** policy check and **no** row bound,
        because the existing tool layer already guards it. New callers should use
        :meth:`execute_readonly`.
        """

    @abstractmethod
    def cancel(self) -> None:
        """Interrupt in-flight engine work using the driver's interrupt call."""

    # ---- derived contract operations ------------------------------------- #

    def check_capabilities(self, sql: str) -> None:
        """Refuse SQL that needs a feature this backend declares unsupported.

        Why: a silently mistranslated query is worse than a refusal. Only the
        frozen portability vocabulary is checked; unknown functions are left to
        the engine, and the contract does not claim to validate every dialect.
        """
        tree = self._parse(sql)

        capabilities = self.capabilities
        missing: list[str] = []
        if not capabilities.window_functions and tree.find(exp.Window) is not None:
            missing.append("window functions")
        if not capabilities.cte and tree.find(exp.With) is not None:
            missing.append("common table expressions")
        if not capabilities.ilike and tree.find(exp.ILike) is not None:
            missing.append("ILIKE")
        if not capabilities.qualify and tree.find(exp.Qualify) is not None:
            missing.append("QUALIFY")
        unsupported_dates = sorted(
            (_function_names(tree) & DATE_FUNCTION_VOCABULARY)
            - capabilities.date_functions
        )
        if unsupported_dates:
            missing.append("date function(s) " + ", ".join(unsupported_dates))
        if missing:
            raise AdapterUnsupportedError(
                f"{self.dialect} backend does not declare support for: "
                + "; ".join(missing)
            )

    def check_objects(self, sql: str) -> None:
        """Refuse SQL that names a table this adapter cannot read.

        Why: SQLite forwards unknown tables to the engine while the DuckDB policy
        layer rejects them during AST scoping, so without this check the same
        mistake would surface as two different error classes and one of them would
        echo driver text into user replies. CTE names are not tables (they resolve
        to scopes), so portable queries are unaffected.
        """
        tree = self._parse(sql)
        known = {name.casefold() for name in self.list_tables()}
        unknown = sorted(
            {
                source.name
                for scope in traverse_scope(tree)
                for source in scope.sources.values()
                if isinstance(source, exp.Table) and source.name.casefold() not in known
            }
        )
        if unknown:
            raise AdapterQueryError(
                f"{self.dialect} adapter has no readable table(s): "
                + ", ".join(unknown)
            )

    def _parse(self, sql: str) -> exp.Expression:
        try:
            tree = sqlglot.parse_one(sql, read=self.dialect)
        except ParseError as exc:
            raise AdapterQueryError(
                f"{self.dialect} could not parse the query: {exc}"
            ) from exc
        if tree is None:
            raise AdapterQueryError(f"{self.dialect} could not parse the query")
        return tree

    def execute_readonly(
        self,
        sql: str,
        *,
        limit: int | None = None,
        timeout: float | None = None,
    ) -> ExecutionResult:
        """Run one governed read: capability guard, object check, AST policy, engine role, bound.

        Layers are independent on purpose — a backend must not be able to bypass
        the policy layer just because its engine is read-only:

        1. :meth:`check_capabilities` refuses unportable SQL;
        2. :meth:`check_objects` refuses unknown tables before the engine sees them;
        3. the shared AST policy engine refuses writes/admin statements;
        4. the engine's own read-only mode refuses writes that reach it;
        5. an optional ``limit`` is pushed into the engine and enforced again on
           the returned rows, and an optional ``timeout`` interrupts the engine.
        """
        self._validate_budget(limit, timeout)
        self.check_capabilities(sql)
        self.check_objects(sql)
        effective_limit = None if limit is None else min(limit, MAX_BOUNDED_ROWS)

        tool = self._policy_tool()
        watchdog = _InterruptWatchdog(self, timeout)
        try:
            with watchdog:
                bounded = (
                    sql if effective_limit is None else self.bound_sql(sql, effective_limit)
                )
                # Same policy engine the tool layer uses; the bounded fetch below
                # lets a streaming backend stop pulling rows earlier.
                tool.policy_engine.evaluate(bounded)
                result = self._fetch_bounded(bounded, effective_limit)
        except AdapterPolicyError:
            raise
        except _policy_violation_types() as exc:
            raise AdapterPolicyError(str(exc), getattr(exc, "decision", None)) from exc
        except Exception as exc:
            # One taxonomy for both backends: driver-specific error classes are
            # preserved on the ``__cause__`` chain, never leaked to callers.
            raise self._translate_engine_error(
                exc, deadline_expired=watchdog.fired, timeout=timeout
            ) from exc
        return self._enforce_row_bound(result, effective_limit)

    def _fetch_bounded(self, sql: str, limit: int | None) -> ExecutionResult:
        """Run already-policy-checked SQL.

        Override point: a backend with a streaming cursor should fetch at most
        ``limit`` rows so a bounded request never materializes more (DuckDB does).
        The default engine bound (see :meth:`bound_sql`) already limits the result
        set, so the pre-contract SQLite connector needs no override.
        """
        return self.execute_sql(sql)

    def preview(self, sql: str, limit: int = 20) -> ExecutionResult:
        """Bounded, policy-checked preview for display.

        The cap matches ``DatabaseTool.execute_sql_preview`` so the contract and
        the tool layer cannot disagree about what a preview is.
        """
        if not isinstance(limit, int) or limit < 1:
            raise ValueError("preview limit must be a positive integer")
        return self.execute_readonly(sql, limit=min(limit, PREVIEW_MAX_ROWS))

    def explain(self, sql: str) -> ExecutionResult:
        """Return the engine plan for a read-only query.

        A plan is evidence about access paths, not a calibrated cost estimate.
        """
        if not self.capabilities.explain:
            raise AdapterUnsupportedError(f"{self.dialect} declares no EXPLAIN")
        self.check_capabilities(sql)
        self.check_objects(sql)
        tool = self._policy_tool()
        try:
            tool.policy_engine.evaluate(sql)
        except _policy_violation_types() as exc:
            raise AdapterPolicyError(str(exc), getattr(exc, "decision", None)) from exc
        try:
            return self.execute_sql(f"{self.capabilities.explain_prefix} {sql}")
        except Exception as exc:
            raise self._translate_engine_error(exc) from exc

    def bound_sql(self, sql: str, limit: int) -> str:
        """Push a row bound into the engine so it stops fetching early.

        Why: bounding only after ``fetchall`` would still materialize a huge
        result. An existing smaller literal LIMIT is preserved.
        """
        bounded = min(limit, MAX_BOUNDED_ROWS)
        tree = self._parse(sql)
        if tree.find(exp.Select) is None:
            raise AdapterQueryError("only SELECT queries can be bounded")
        existing = tree.args.get("limit")
        if existing is None:
            tree = tree.limit(bounded)
        elif isinstance(existing, exp.Limit) and existing.expression is not None:
            literal = existing.expression
            current = int(literal.this) if literal.is_int else bounded
            existing.set("expression", exp.Literal.number(min(current, bounded)))
        else:
            tree = tree.limit(bounded)
        return tree.sql(dialect=self.dialect)

    # ---- normalization --------------------------------------------------- #

    def normalize_value(self, value: Any) -> Any:
        """Normalize one driver value (see the module-level rules)."""
        return _render_value(value)

    def normalize_type(self, data_type: str | None) -> str:
        """Map a declared or engine-reported type to a frozen logical type."""
        return normalize_type(data_type)

    # ---- internals ------------------------------------------------------- #

    def _validate_budget(self, limit: int | None, timeout: float | None) -> None:
        if limit is not None and (not isinstance(limit, int) or limit < 1):
            raise ValueError("limit must be a positive integer or None")
        if timeout is not None and (
            isinstance(timeout, bool)
            or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout)
            or timeout <= 0
        ):
            raise ValueError("timeout must be a finite positive number of seconds")

    def _policy_tool(self) -> "DatabaseTool":
        """Reuse the tool layer's policy engine (single source of SQL policy).

        Imported lazily because ``DatabaseTool`` itself depends on the adapter
        package; caching keeps the per-call schema lookup out of hot paths.
        """
        cached = self._policy_tool_cache
        if cached is None:
            from queryforge.infrastructure.tools.database_tool import DatabaseTool

            cached = DatabaseTool(self)
            self._policy_tool_cache = cached
        return cached

    def _translate_engine_error(
        self,
        exc: BaseException,
        *,
        deadline_expired: bool = False,
        timeout: float | None = None,
    ) -> AdapterError:
        """Map a backend-native failure onto the frozen error taxonomy."""
        if deadline_expired:
            return AdapterTimeoutError(
                f"{self.dialect} query exceeded the {timeout}s deadline and was "
                "interrupted; no full result was produced"
            )
        if _looks_interrupted(exc):
            return AdapterCancelledError(
                f"{self.dialect} query was interrupted (client cancel); no full "
                "result was produced"
            )
        return AdapterQueryError(f"{self.dialect} query failed: {exc}")

    @staticmethod
    def _enforce_row_bound(
        result: ExecutionResult, limit: int | None
    ) -> ExecutionResult:
        """Re-apply the bound after fetch: the engine bound is not the contract."""
        if limit is None or len(result.rows) <= limit:
            return result
        rows = result.rows[:limit]
        return ExecutionResult(
            columns=list(result.columns), rows=rows, row_count=len(rows)
        )


def _policy_violation_types() -> tuple[type[BaseException], ...]:
    """Policy refusal types raised by the AST layer and the tool facade."""
    from queryforge.domain.security import SQLPolicyViolation
    from queryforge.infrastructure.tools.database_tool import UnsafeSQLError

    return (UnsafeSQLError, SQLPolicyViolation)


class ConnectorAdapter(DatabaseAdapter):
    """Contract view over a connector that predates this contract.

    Why: ``SQLiteConnector`` is the default lightweight path and must keep its
    behaviour (and its lack of optional dependencies) byte-for-byte. Wrapping it
    publishes the frozen surface — capability declaration, bounded reads,
    uniform errors, normalization — without touching the connector itself, so
    existing callers keep the exact semantics they had.
    """

    def __init__(
        self,
        connector: Any,
        capabilities: AdapterCapabilities | None = None,
    ) -> None:
        self._connector = connector
        dialect = str(getattr(connector, "dialect", "unknown") or "unknown")
        self.dialect = dialect
        self.capabilities = capabilities or capabilities_for_dialect(dialect)

    @property
    def connector(self) -> Any:
        """The wrapped connector, for callers that still need the raw object."""
        return self._connector

    def list_tables(self) -> list[str]:
        return list(self._connector.list_tables())

    def describe_table(self, table_name: str) -> TableSchema:
        return self._connector.describe_table(table_name)

    def execute_sql(self, sql: str) -> ExecutionResult:
        return self._connector.execute_sql(sql)

    def cancel(self) -> None:
        cancel = getattr(self._connector, "cancel", None)
        if cancel is None:
            raise AdapterUnsupportedError(
                f"{self.dialect} connector exposes no cancellation primitive"
            )
        cancel()

    def __getattr__(self, name: str) -> Any:
        """Delegate everything the contract does not define to the connector.

        The wrapper must stay a faithful *view*: callers reach for connector
        specifics the contract deliberately does not cover — the raw ``sqlite3``
        connection (cancellation installs a progress handler on it), value hints,
        the last policy decision, ``find_matching_values`` — and losing them
        silently disabled SQL interruption while every contract test still passed.
        """
        connector = self.__dict__.get("_connector")
        if connector is None:  # pragma: no cover - attribute set in __init__
            raise AttributeError(name)
        try:
            return getattr(connector, name)
        except AttributeError as exc:
            raise AttributeError(
                f"{type(connector).__name__!r} (viewed through ConnectorAdapter) "
                f"has no attribute {name!r}"
            ) from exc

    def close(self) -> None:
        self._connector.close()


def adapt_connector(
    connector: Any, capabilities: AdapterCapabilities | None = None
) -> DatabaseAdapter:
    """Expose any pre-contract connector through the frozen contract."""
    if isinstance(connector, DatabaseAdapter):
        return connector
    return ConnectorAdapter(connector, capabilities)
