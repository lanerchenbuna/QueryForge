"""Registry of typed, budgeted, permission-checked tools.

Every tool call in step 09 goes through :meth:`ToolRegistry.execute`, which:

1. resolves the :class:`ToolSpec` (unknown or unimplemented tools raise
   :class:`ToolUnavailable`),
2. refuses calls that the current mode does not allow (``plan_only`` never runs
   generated SQL, previews, or quality scans),
3. validates parameters against the spec's JSON-Schema-style contract *before*
   any handler runs,
4. checks the declared permissions and the domain scope,
5. reserves and settles shared budget around the handler,
6. maps handler exceptions through the workflow error taxonomy onto the
   recorded :class:`ToolCall`, and
7. truncates oversized results explicitly (rows/bytes) instead of pretending a
   partial result is complete.

The default catalog is intentionally thin: metadata, metric, SQL, and data
quality tools are implemented through the existing governed
:class:`~queryforge.infrastructure.tools.database_tool.DatabaseTool` (never a
parallel unguarded path), and the five step 11 analysis actions run the
deterministic computations of
:mod:`queryforge.domain.analysis.analysis_tools` over inputs that were already
produced by a governed SQL step.  Those five tools never touch the database
themselves, so a chart can be rendered without any data access and a comparison
cannot smuggle in an ungoverned query.
"""

from __future__ import annotations

import json
import time
from typing import Any, Callable, Iterable, Mapping

from queryforge.domain.analysis.analysis_tools import (
    ANOMALY_METHODS,
    CHART_TYPES,
    COMPARISON_METHODS,
    FLOAT_TOLERANCE,
    METRIC_KINDS,
    MISSING_POLICIES,
    SEASONALITY_KINDS,
    build_chart,
    compare_periods,
    contribution_breakdown,
    detect_anomaly,
    drill_down,
    require_consistent_versions,
)
from queryforge.infrastructure.tools.data_quality_tool import (
    SUPPORTED_CHECKS,
    DataQualityBudget,
    DataQualityTool,
)
from queryforge.infrastructure.tools.database_tool import DatabaseTool, UnsafeSQLError
from queryforge.workflow.errors import WorkflowErrorCategory, categorize_error

from queryforge.orchestration.tools.budget import BudgetManager, connection_for
from queryforge.orchestration.tools.specs import (
    ToolBudgetError,
    ToolCall,
    ToolContext,
    ToolDenied,
    ToolObservation,
    ToolSpec,
    ToolUnavailable,
    estimate_tokens,
    utc_now_iso,
    validate_params,
)

ToolHandler = Callable[[dict[str, Any], ToolContext], Any]

#: Tools that were declared in step 09 and are now implemented by step 11.
#: The constant stays exported (empty) so existing imports keep working; the
#: analysis tools are part of the default catalog rather than placeholders.
PLACEHOLDER_TOOLS: tuple[str, ...] = ()

_EMPTY_OBJECT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {},
    "additionalProperties": False,
}

#: Budget categories whose tools never touch the governed database: they compute
#: or render from parameters that an already-authorised SQL step produced.
_NON_DATABASE_BUDGET_CATEGORIES: frozenset[str] = frozenset({"compute", "render"})


class ToolRegistry:
    """Register, describe, and execute governed tools."""

    def __init__(
        self,
        budget_manager: BudgetManager | None = None,
        *,
        database_tool_factory: Any = None,
        semantic_model: Any = None,
        data_quality_budget: DataQualityBudget | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.budget_manager = budget_manager or BudgetManager()
        self.database_tool_factory = database_tool_factory
        self.semantic_model = semantic_model
        self.data_quality_budget = data_quality_budget
        self._clock = clock
        self._specs: dict[str, ToolSpec] = {}
        self._handlers: dict[str, ToolHandler] = {}
        self._journal: list[ToolCall] = []

    # ------------------------------------------------------------- catalogue

    def register(self, spec: ToolSpec, handler: ToolHandler | None = None) -> ToolSpec:
        """Register a spec, optionally with an implementation."""

        if spec.name in self._specs:
            raise ValueError(f"tool {spec.name!r} is already registered")
        self._specs[spec.name] = spec
        if handler is not None:
            self._handlers[spec.name] = handler
        return spec

    def resolve(self, name: str) -> ToolSpec:
        """Return the spec for ``name`` or raise :class:`ToolUnavailable`."""

        spec = self._specs.get(name)
        if spec is None:
            raise ToolUnavailable(
                f"unknown tool {name!r}",
                tool=str(name),
                reason="unknown_tool",
            )
        return spec

    def handler_for(self, name: str) -> ToolHandler:
        spec = self.resolve(name)
        handler = self._handlers.get(spec.name)
        if handler is None:
            raise ToolUnavailable(
                f"tool {name!r} is declared but not implemented in this phase",
                tool=name,
                reason="not_implemented",
            )
        return handler

    def has(self, name: str) -> bool:
        return name in self._specs

    def is_available(self, name: str) -> bool:
        return name in self._handlers

    def names(self) -> list[str]:
        return sorted(self._specs)

    def allows(self, name: str, mode: str = "execute") -> bool:
        """True when ``name`` may run in ``mode`` (no exception for unknown names)."""

        spec = self._specs.get(name)
        return bool(spec and spec.permits(mode))

    def list_for(self, mode: str = "execute") -> list[ToolSpec]:
        """Specs usable in ``mode``, sorted by name."""

        return [self._specs[name] for name in self.names() if self._specs[name].permits(mode)]

    def validate_params(self, name: str, params: Mapping[str, Any] | None) -> dict[str, Any]:
        """Validate ``params`` against the registered spec's parameter schema."""

        spec = self.resolve(name)
        return validate_params(spec.name, spec.parameter_schema, dict(params or {}))

    # --------------------------------------------------------------- journal

    @property
    def journal(self) -> list[ToolCall]:
        """Recorded calls in execution order (a copy)."""

        return list(self._journal)

    def call(self, call_id: str) -> ToolCall | None:
        for recorded in self._journal:
            if recorded.id == call_id:
                return recorded
        return None

    def clear_journal(self) -> None:
        self._journal.clear()

    # --------------------------------------------------------------- execute

    def execute(
        self,
        name: str,
        params: Mapping[str, Any] | None = None,
        *,
        context: Any = None,
        mode: str = "execute",
        evidence_ids: Iterable[str] | None = None,
    ) -> ToolObservation:
        """Execute one tool call and return its typed observation."""

        resolved_params = dict(params or {})
        tool_context = ToolContext.coerce(
            context,
            database_tool=self._resolve_database_tool(context),
            semantic_model=self.semantic_model,
        )
        started = self._clock()
        try:
            spec = self.resolve(name)
        except ToolUnavailable as exc:
            return self._finalize(
                ToolCall(
                    tool=str(name),
                    params=resolved_params,
                    run_id=tool_context.run_id,
                    task_id=tool_context.task_id,
                    domain_id=tool_context.domain_id,
                    data_version=tool_context.data_version,
                    started_at=utc_now_iso(),
                ),
                None,
                error=exc,
                error_category=WorkflowErrorCategory.unsupported.value
                if exc.reason == "not_implemented"
                else WorkflowErrorCategory.unknown.value,
                status="denied",
                started=started,
                evidence_ids=evidence_ids,
            )

        call = ToolCall(
            tool=spec.name,
            params=resolved_params,
            run_id=tool_context.run_id,
            task_id=tool_context.task_id,
            domain_id=tool_context.domain_id,
            data_version=tool_context.data_version,
            started_at=utc_now_iso(),
            status="pending",
        )

        # 1. mode gate (plan_only must never run execute-class tools).
        if not spec.permits(mode):
            return self._finalize(
                call,
                None,
                error=ToolDenied(
                    f"tool {spec.name!r} is not available in mode {mode!r} "
                    f"(declared modes: {', '.join(spec.modes)})",
                    tool=spec.name,
                    reason="mode_not_allowed",
                ),
                error_category=WorkflowErrorCategory.permission.value,
                status="denied",
                started=started,
                evidence_ids=evidence_ids,
            )

        # 2. parameter contract, validated before any handler runs.
        try:
            call.params = validate_params(spec.name, spec.parameter_schema, resolved_params)
        except ValueError as exc:
            return self._finalize(
                call,
                None,
                error=exc,
                error_category=WorkflowErrorCategory.unknown.value,
                status="denied",
                started=started,
                evidence_ids=evidence_ids,
            )

        # 3. permissions and domain scope.
        try:
            self._check_permissions(spec, call.params, tool_context)
        except ToolDenied as exc:
            return self._finalize(
                call,
                None,
                error=exc,
                error_category=WorkflowErrorCategory.permission.value,
                status="denied",
                started=started,
                evidence_ids=evidence_ids,
            )

        # 4. declared-but-unimplemented tools fail loudly.
        handler = self._handlers.get(spec.name)
        if handler is None:
            return self._finalize(
                call,
                None,
                error=ToolUnavailable(
                    f"tool {spec.name!r} is declared but not implemented in this phase",
                    tool=spec.name,
                    reason="not_implemented",
                ),
                error_category=WorkflowErrorCategory.unsupported.value,
                status="denied",
                started=started,
                evidence_ids=evidence_ids,
            )

        # 5. reserve shared budget before the expensive part starts.
        deadline = self.budget_manager.sql_deadline_at()
        try:
            reservation = self.budget_manager.reserve(
                category=spec.budget_category,
                calls=1,
                # A single call's SQL time is bounded by the SQLite deadline
                # installed below; the global cap charges the *actual* duration
                # at settle time, so it is only required to have headroom.
                require_remaining=("max_sql_duration_ms",)
                if spec.budget_category == "sql"
                else (),
                call_id=call.id,
                tool=spec.name,
            )
        except ToolBudgetError as exc:
            return self._finalize(
                call,
                None,
                error=exc,
                error_category=WorkflowErrorCategory.budget.value,
                status="denied",
                started=started,
                evidence_ids=evidence_ids,
            )

        call.status = "running"
        guard = None
        result: Any = None
        error: Exception | None = None
        rows = 0
        try:
            # Tools that compute over already-assembled inputs (the step 11
            # analysis tools: budget category ``compute``/``render``) must stay
            # usable without any governed connection - a chart or a comparison
            # never queries the database, so requiring one would make a report
            # fail for no reason. Every other category still resolves (and, when
            # missing, refuses) the governed tool exactly as before.
            database_tool = (
                None
                if spec.budget_category in _NON_DATABASE_BUDGET_CATEGORIES
                else self._database_tool(tool_context)
            )
            if spec.budget_category == "sql":
                guard = self.budget_manager.install_sql_deadline_handler(
                    connection_for(database_tool), deadline
                )
            result = handler(call.params, tool_context)
        except Exception as exc:  # typed below; never leaked as a raw traceback
            error = exc
        finally:
            if guard is not None:
                guard.restore()
            duration_ms = max(0.0, (self._clock() - started) * 1000.0)
            reservation.settle(
                max_sql_duration_ms=duration_ms if spec.budget_category == "sql" else 0,
                max_estimated_tokens=estimate_tokens(result) if error is None else 0,
                max_output_rows=rows,
            )
        duration_ms = max(0.0, (self._clock() - started) * 1000.0)

        if error is not None:
            category = categorize_error(error)
            timed_out = _is_timeout(error, guard, self._clock, deadline)
            return self._finalize(
                call,
                None,
                error=error,
                error_category=(
                    WorkflowErrorCategory.budget.value
                    if timed_out
                    else category.value
                ),
                status="timeout" if timed_out else "failed",
                started=started,
                evidence_ids=evidence_ids,
            )

        normalized, truncation, rows = self._truncate(result, spec)
        tokens = estimate_tokens(normalized)
        return self._finalize(
            call,
            normalized,
            error=None,
            error_category=None,
            status="succeeded",
            started=started,
            evidence_ids=evidence_ids,
            truncation=truncation,
            duration_ms=duration_ms,
            estimated_tokens=tokens,
            output_rows=rows,
        )

    # -------------------------------------------------------------- internals

    def _resolve_database_tool(self, context: Any) -> Any:
        """Resolve the governed DatabaseTool for one call.

        A caller-provided tool always wins; otherwise the registry factory is
        used. Callable factories are invoked **per call** (so per-thread or
        per-request connections work), while non-callable factories are treated
        as already-bound tools. Without this, a callable factory would be
        mistaken for a bound tool and the real connection would be dropped.
        """
        existing = getattr(context, "database_tool", None)
        if existing is not None:
            return existing
        factory = self.database_tool_factory
        if factory is None or isinstance(factory, DatabaseTool):
            return factory
        if callable(factory):
            probe = ToolContext.coerce(context)
            try:
                return factory(probe)
            except TypeError:
                try:
                    return factory()
                except TypeError:
                    return factory
        return factory

    def _database_tool(self, tool_context: ToolContext) -> DatabaseTool:
        if tool_context.database_tool is not None:
            return tool_context.database_tool  # type: ignore[return-value]
        raise ToolUnavailable(
            "no governed database tool is bound to this registry",
            tool="database",
            reason="database_tool_unavailable",
        )

    def _semantic_model(self, tool_context: ToolContext) -> Any:
        model = tool_context.semantic_model or self.semantic_model
        if model is None:
            raise ToolUnavailable(
                "no semantic model is bound to this registry",
                tool="semantic_model",
                reason="semantic_model_unavailable",
            )
        return model

    @staticmethod
    def _check_permissions(
        spec: ToolSpec, params: Mapping[str, Any], tool_context: ToolContext
    ) -> None:
        granted = tool_context.granted_permissions
        if spec.permissions and granted is not None:
            missing = sorted(permission for permission in spec.permissions if permission not in granted)
            if missing:
                raise ToolDenied(
                    f"tool {spec.name!r} requires permission(s) {missing}",
                    tool=spec.name,
                    reason="missing_permission",
                )
        if (
            spec.budget_category == "sql"
            and granted
            and "*" not in granted
            and "sql:execute" not in granted
        ):
            # A caller that declares its permissions must also declare the SQL
            # execution permission; an empty/no declaration means "not scoped".
            raise ToolDenied(
                f"tool {spec.name!r} requires permission 'sql:execute'",
                tool=spec.name,
                reason="missing_permission",
            )
        declared_domain = params.get("domain_id") if isinstance(params, Mapping) else None
        if declared_domain is not None:
            declared = str(declared_domain)
            if tool_context.domain_id and declared != str(tool_context.domain_id):
                raise ToolDenied(
                    f"tool {spec.name!r} domain {declared!r} does not match the "
                    f"authorised domain {tool_context.domain_id!r}",
                    tool=spec.name,
                    reason="domain_mismatch",
                )
            if not tool_context.domain_id:
                allowed = tool_context.allowed_domains
                if not allowed or declared not in allowed:
                    raise ToolDenied(
                        f"tool {spec.name!r} domain {declared!r} is not authorised "
                        "in this context",
                        tool=spec.name,
                        reason="domain_not_authorised",
                    )

    def _truncate(
        self, result: Any, spec: ToolSpec
    ) -> tuple[Any, dict[str, Any] | None, int]:
        """Cap result rows/bytes, marking every truncation explicitly."""

        payload = _normalize_result(result)
        max_rows = int(self.budget_manager.per_call.max_output_rows)
        max_bytes = int(self.budget_manager.per_call.max_output_bytes)
        truncation: dict[str, Any] | None = None
        rows = 0
        candidate_rows = payload.get("rows")
        if isinstance(candidate_rows, list):
            rows = len(candidate_rows)
            if rows > max_rows:
                payload = dict(payload)
                payload["rows"] = candidate_rows[:max_rows]
                if isinstance(payload.get("row_count"), int):
                    payload["row_count_returned"] = min(int(payload["row_count"]), max_rows)
                truncation = {
                    "reason": "max_output_rows",
                    "limit": max_rows,
                    "returned_rows": max_rows,
                    "total_rows": rows,
                }
                rows = max_rows
        size = _byte_size(payload)
        if size > max_bytes:
            payload, byte_note = _cap_bytes(payload, max_bytes)
            note = dict(truncation or {})
            note.update(byte_note)
            truncation = note
            trimmed_rows = payload.get("rows")
            if isinstance(trimmed_rows, list):
                rows = len(trimmed_rows)
        if truncation is not None:
            truncation.setdefault("tool", spec.name)
        return payload, truncation, rows

    def _finalize(
        self,
        call: ToolCall,
        result: Any,
        *,
        error: Exception | None,
        error_category: str | None,
        status: str,
        started: float,
        evidence_ids: Iterable[str] | None,
        truncation: dict[str, Any] | None = None,
        duration_ms: float | None = None,
        estimated_tokens: int | None = None,
        output_rows: int = 0,
    ) -> ToolObservation:
        call.finished_at = utc_now_iso()
        call.status = status  # type: ignore[assignment]
        call.duration_ms = (
            round(duration_ms, 3)
            if duration_ms is not None
            else round(max(0.0, (self._clock() - started) * 1000.0), 3)
        )
        call.error = str(error) if error is not None else None
        call.error_category = error_category
        call.truncated = truncation is not None
        call.output_rows = output_rows
        observation = ToolObservation(
            tool=call.tool,
            params=dict(call.params),
            truncated=truncation is not None,
            result=result,
            error_category=error_category,
            evidence_ids=list(evidence_ids or []),
            duration_ms=call.duration_ms,
            estimated_tokens=(
                estimated_tokens
                if estimated_tokens is not None
                else estimate_tokens(result) if result is not None else 0
            ),
            call_id=call.id,
            call=call,
            status=status,  # type: ignore[arg-type]
            truncation=truncation,
        )
        call.observation_ref = f"obs:{call.id}"
        self._journal.append(call)
        return observation


def _is_timeout(
    error: Exception, guard: Any, clock: Callable[[], float], deadline: float
) -> bool:
    """Decide whether a failed SQL call actually hit its SQLite deadline."""

    if getattr(guard, "installed", False) and clock() >= deadline:
        return True
    message = str(error).casefold()
    return "interrupted" in message or "sql deadline exceeded" in message


def _normalize_result(result: Any) -> dict[str, Any]:
    if result is None:
        return {}
    if isinstance(result, dict):
        return dict(result)
    dump = getattr(result, "model_dump", None)
    if callable(dump):
        return dict(dump(mode="json"))
    if isinstance(result, (list, tuple)):
        return {"items": list(result)}
    return {"result": result}


def _byte_size(payload: Any) -> int:
    try:
        return len(json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8"))
    except (TypeError, ValueError):
        return len(str(payload).encode("utf-8"))


def _cap_bytes(payload: dict[str, Any], max_bytes: int) -> tuple[dict[str, Any], dict[str, Any]]:
    """Shrink an oversized payload until it fits, recording what was dropped."""

    original = _byte_size(payload)
    trimmed = dict(payload)
    rows = trimmed.get("rows")
    if isinstance(rows, list) and rows:
        kept = list(rows)
        while kept and _byte_size({**trimmed, "rows": kept}) > max_bytes:
            kept = kept[: max(0, len(kept) // 2)]
        trimmed["rows"] = kept
        if _byte_size(trimmed) <= max_bytes:
            return trimmed, {
                "reason": "max_output_bytes",
                "limit": max_bytes,
                "original_bytes": original,
                "returned_rows": len(kept),
                "total_rows": len(rows),
            }
    text = json.dumps(trimmed, ensure_ascii=False, default=str)
    keep = max(0, max_bytes - 256)
    dropped = max(0, len(text.encode("utf-8")) - keep)
    truncated = {
        "reason": "max_output_bytes",
        "limit": max_bytes,
        "original_bytes": original,
        "dropped_bytes": dropped,
    }
    return (
        {
            "truncated_json_prefix": text[:keep],
            "byte_truncation": truncated,
        },
        truncated,
    )


# ----------------------------------------------------------------- catalogues


def build_default_registry(
    database_tool_factory: Any = None,
    budget_manager: BudgetManager | None = None,
    *,
    semantic_model: Any = None,
    data_quality_budget: DataQualityBudget | None = None,
    include_placeholders: bool = True,
) -> ToolRegistry:
    """Build the default governed catalog.

    ``database_tool_factory`` may be a :class:`DatabaseTool` or a callable that
    receives the :class:`ToolContext` and returns one; either way every SQL tool
    runs through the same policy engine as model generated SQL.

    ``include_placeholders`` is kept for callers written against step 09.  There
    are no unimplemented placeholders left, so it now controls the step 11
    analysis tools (:data:`_ANALYSIS_CATALOG`), which are part of the default
    catalog.
    """

    registry = ToolRegistry(
        budget_manager or BudgetManager(),
        database_tool_factory=database_tool_factory,
        semantic_model=semantic_model,
        data_quality_budget=data_quality_budget,
    )
    for spec, handler in _DEFAULT_CATALOG:
        registry.register(spec, handler)
    if include_placeholders:
        for spec, handler in _ANALYSIS_CATALOG:
            registry.register(spec, handler)
    return bind_catalog(registry)


def _spec(
    name: str,
    description: str,
    parameter_schema: dict[str, Any],
    output_schema: dict[str, Any],
    *,
    modes: Iterable[str] = ("read", "execute"),
    permissions: Iterable[str] = (),
    idempotent: bool = True,
    budget_category: str = "read",
) -> ToolSpec:
    return ToolSpec(
        name=name,
        description=description,
        parameter_schema=parameter_schema,
        output_schema=output_schema,
        permissions=list(permissions),
        modes=list(modes),  # type: ignore[arg-type]
        idempotent=idempotent,
        budget_category=budget_category,
    )


_TABLE_NAME: dict[str, Any] = {"type": "string", "minLength": 1}
_LIMIT: dict[str, Any] = {"type": "integer", "minimum": 1, "maximum": 100}


def _bind(
    handler: Callable[[ToolRegistry, dict[str, Any], ToolContext], Any],
) -> ToolHandler:
    """Adapt a handler that needs the registry (for the bound DatabaseTool)."""

    registry_ref: dict[str, ToolRegistry] = {}

    def bound(params: dict[str, Any], context: ToolContext) -> Any:
        return handler(registry_ref["registry"], params, context)

    bound.__tool_bind__ = registry_ref  # type: ignore[attr-defined]
    return bound


def _list_tables_impl(registry: ToolRegistry, params: dict[str, Any], context: ToolContext) -> dict:
    tool = registry._database_tool(context)
    return {"tables": tool.list_tables(), "policy": tool.policy_summary}


def _describe_table_impl(
    registry: ToolRegistry, params: dict[str, Any], context: ToolContext
) -> dict:
    tool = registry._database_tool(context)
    table_name = str(params["table_name"])
    try:
        schema = tool.describe_table(table_name)
    except UnsafeSQLError:
        # A policy denial keeps its own decision-based error category.
        raise
    except Exception as exc:
        if "unknown sqlite table" in str(exc).casefold():
            # Normalise the connector wording so the shared taxonomy classifies
            # "table does not exist" as an identifier error, like SQLite does.
            raise ValueError(f"no such table: {table_name}") from exc
        raise
    return {"table": schema.model_dump(mode="json")}


def _list_metrics_impl(
    registry: ToolRegistry, params: dict[str, Any], context: ToolContext
) -> dict:
    model = registry._semantic_model(context)
    return {
        "metrics": [metric.model_dump(mode="json") for metric in model.model.metrics],
        "semantic_version": model.model.version,
        "source_path": model.source_path,
    }


def _get_metric_impl(
    registry: ToolRegistry, params: dict[str, Any], context: ToolContext
) -> dict:
    model = registry._semantic_model(context)
    name = str(params["metric_name"])
    metric = next((item for item in model.model.metrics if item.name == name), None)
    if metric is None:
        raise ToolUnavailable(
            f"unknown metric {name!r} in the governed semantic model",
            tool="get_metric",
            reason="unknown_metric",
        )
    return {
        "metric": metric.model_dump(mode="json"),
        "semantic_version": model.model.version,
    }


def _preview_sql_impl(
    registry: ToolRegistry, params: dict[str, Any], context: ToolContext
) -> dict:
    tool = registry._database_tool(context)
    limit = int(params.get("limit") or 20)
    result = tool.execute_sql_preview(str(params["sql"]), limit)
    decision = tool.last_policy_decision
    return {
        "columns": result.columns,
        "rows": result.rows,
        "row_count": result.row_count,
        "policy_decision": decision.model_dump(mode="json") if decision else None,
    }


def _execute_sql_impl(
    registry: ToolRegistry, params: dict[str, Any], context: ToolContext
) -> dict:
    tool = registry._database_tool(context)
    result = tool.execute_sql(str(params["sql"]))
    decision = tool.last_policy_decision
    return {
        "columns": result.columns,
        "rows": result.rows,
        "row_count": result.row_count,
        "policy_decision": decision.model_dump(mode="json") if decision else None,
    }


def _preview_distinct_values_impl(
    registry: ToolRegistry, params: dict[str, Any], context: ToolContext
) -> dict:
    tool = registry._database_tool(context)
    limit = int(params.get("limit") or 20)
    values = tool.preview_distinct_values(
        str(params["table_name"]), str(params["column_name"]), limit
    )
    return {"values": values, "value_count": len(values)}


def _check_data_quality_impl(
    registry: ToolRegistry, params: dict[str, Any], context: ToolContext
) -> dict:
    tool = registry._database_tool(context)
    quality = DataQualityTool(tool, registry.data_quality_budget or DataQualityBudget())
    checks = [str(item) for item in (params.get("checks") or [])]
    options = params.get("options") or {}
    if not isinstance(options, dict):
        raise ToolDenied(
            "check_data_quality 'options' must be an object",
            tool="check_data_quality",
            reason="invalid_params",
        )
    unsupported = sorted(set(options) - _QUALITY_OPTION_KEYS)
    if unsupported:
        raise ToolDenied(
            f"check_data_quality does not support option(s) {unsupported}",
            tool="check_data_quality",
            reason="invalid_params",
        )
    report = quality.check(str(params["table_name"]), checks, **options)
    payload = report.to_payload()
    payload["table"] = report.table
    payload["requested_checks"] = checks
    return payload


_QUALITY_OPTION_KEYS = frozenset(
    {
        "time_field",
        "window",
        "grain_columns",
        "expected_max_date",
        "referenced",
        "columns",
        "max_null_rate",
        "tolerance_days",
        "min_observed_ratio",
    }
)

_DEFAULT_CATALOG: tuple[tuple[ToolSpec, ToolHandler], ...] = (
    (
        _spec(
            "list_tables",
            "List policy-visible tables of the current database.",
            _EMPTY_OBJECT_SCHEMA,
            {"type": "object", "properties": {"tables": {"type": "array"}}},
            modes=("read", "execute", "plan_only"),
        ),
        _bind(_list_tables_impl),
    ),
    (
        _spec(
            "describe_table",
            "Describe one policy-visible table (columns, keys, foreign keys).",
            {
                "type": "object",
                "properties": {"table_name": _TABLE_NAME},
                "required": ["table_name"],
                "additionalProperties": False,
            },
            {"type": "object", "properties": {"table": {"type": "object"}}},
            modes=("read", "execute", "plan_only"),
        ),
        _bind(_describe_table_impl),
    ),
    (
        _spec(
            "list_metrics",
            "List governed metrics declared by the semantic model.",
            _EMPTY_OBJECT_SCHEMA,
            {"type": "object", "properties": {"metrics": {"type": "array"}}},
            modes=("read", "execute", "plan_only"),
        ),
        _bind(_list_metrics_impl),
    ),
    (
        _spec(
            "get_metric",
            "Return one governed metric definition by name.",
            {
                "type": "object",
                "properties": {"metric_name": _TABLE_NAME},
                "required": ["metric_name"],
                "additionalProperties": False,
            },
            {"type": "object", "properties": {"metric": {"type": "object"}}},
            modes=("read", "execute", "plan_only"),
        ),
        _bind(_get_metric_impl),
    ),
    (
        _spec(
            "preview_sql",
            "Run a bounded read-only preview through the governed SQL policy engine.",
            {
                "type": "object",
                "properties": {"sql": {"type": "string", "minLength": 1}, "limit": _LIMIT},
                "required": ["sql"],
                "additionalProperties": False,
            },
            {"type": "object", "properties": {"columns": {"type": "array"}, "rows": {"type": "array"}}},
            modes=("read", "execute"),
            budget_category="sql",
        ),
        _bind(_preview_sql_impl),
    ),
    (
        _spec(
            "execute_sql",
            "Execute one read-only SELECT through the governed SQL policy engine.",
            {
                "type": "object",
                "properties": {"sql": {"type": "string", "minLength": 1}},
                "required": ["sql"],
                "additionalProperties": False,
            },
            {"type": "object", "properties": {"columns": {"type": "array"}, "rows": {"type": "array"}}},
            modes=("read", "execute"),
            budget_category="sql",
        ),
        _bind(_execute_sql_impl),
    ),
    (
        _spec(
            "execute_sql_preview",
            "Compatibility alias of preview_sql used by the bounded tool loop.",
            {
                "type": "object",
                "properties": {"sql": {"type": "string", "minLength": 1}, "limit": _LIMIT},
                "required": ["sql"],
                "additionalProperties": False,
            },
            {"type": "object", "properties": {"columns": {"type": "array"}, "rows": {"type": "array"}}},
            modes=("read", "execute"),
            budget_category="sql",
        ),
        _bind(_preview_sql_impl),
    ),
    (
        _spec(
            "preview_distinct_values",
            "Sample distinct visible values of one column (bounded, read-only).",
            {
                "type": "object",
                "properties": {
                    "table_name": _TABLE_NAME,
                    "column_name": _TABLE_NAME,
                    "limit": _LIMIT,
                },
                "required": ["table_name", "column_name"],
                "additionalProperties": False,
            },
            {"type": "object", "properties": {"values": {"type": "array"}}},
            modes=("read", "execute"),
            budget_category="sql",
        ),
        _bind(_preview_distinct_values_impl),
    ),
    (
        _spec(
            "check_data_quality",
            "Collect runtime data-quality evidence for one table (step 08 tool).",
            {
                "type": "object",
                "properties": {
                    "table_name": _TABLE_NAME,
                    "checks": {"type": "array", "items": {"type": "string", "enum": list(SUPPORTED_CHECKS)}},
                    "options": {"type": "object"},
                },
                "required": ["table_name", "checks"],
                "additionalProperties": False,
            },
            {"type": "object", "properties": {"status": {"type": "string"}, "checks": {"type": "array"}}},
            modes=("read", "execute"),
            budget_category="sql",
        ),
        _bind(_check_data_quality_impl),
    ),
)


# ------------------------------------------------------- step 11 analysis tools
#
# ``planner.plan.ACTION_TOOL_MAP`` routes the five analysis actions
# (compare_periods / drill_down / calculate_contribution / detect_anomaly /
# render_chart) to these tools.  They compute over inputs that a governed SQL
# step already produced (see
# ``infrastructure.tools.analysis_tool.AnalysisInputAssembler``), return the
# declared method/parameters/limitations of
# ``domain.analysis.analysis_tools``, and never open a database themselves.

_NUMBER_OR_NULL: dict[str, Any] = {"type": ["number", "null"]}
_STRING_OR_NULL: dict[str, Any] = {"type": ["string", "null"]}
_CATEGORY_OR_VALUE: dict[str, Any] = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {"category": {"type": "string"}, "value": _NUMBER_OR_NULL},
        "additionalProperties": False,
    },
}
_COMPARISON_BUCKET: dict[str, Any] = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {
            "category": {"type": "string"},
            "current": _NUMBER_OR_NULL,
            "baseline": _NUMBER_OR_NULL,
        },
        "additionalProperties": False,
    },
}
_SERIES_POINT: dict[str, Any] = {
    "type": "array",
    "items": {
        "type": "object",
        "properties": {"period": {"type": "string"}, "value": _NUMBER_OR_NULL},
        "additionalProperties": False,
    },
}
_SCOPE_PROPERTIES: dict[str, Any] = {
    "unit": _STRING_OR_NULL,
    "version": _STRING_OR_NULL,
}


def _analysis_input_required(tool: str, params: Mapping[str, Any], inputs: tuple[str, ...]) -> None:
    """Refuse a call that supplies none of the tool's analysis inputs.

    Integration convention for the five step-11 tools: every parameter is
    optional (``additionalProperties: false``, no ``required``), because the
    planner validates a step's ``inputs`` against this very schema *before* run
    time while the actual numbers (values/buckets/series/rows) are assembled by
    the executor from governed SQL results and passed straight to the tool.  The
    handler is therefore the only place that can notice missing input, and it
    answers with a :class:`ToolUnavailable` - a ``ValueError`` carrying
    ``reason="missing_analysis_input"`` - instead of defaulting to zero, an empty
    series, or an invented trend.

    Because parameter validation fills optional fields with their defaults before
    the handler runs, "the caller supplied nothing" is checked explicitly over
    ``inputs`` rather than inferred from missing keys.  A caller that means "both
    windows had no rows" still gets the explicit ``missing_value``/
    ``empty_buckets`` state as long as it supplies at least one of its inputs;
    note the limit of the rule: an omitted key and an explicit ``null`` are
    indistinguishable after validation, so an all-null call is refused too.
    """

    if any(params.get(key) is not None for key in inputs):
        return
    raise ToolUnavailable(
        f"missing_analysis_input: {tool} needs {', '.join(inputs)} but none of them "
        "was supplied, so there is nothing to compute (the planning-stage call "
        "carries no values by design; the executor passes the assembled inputs at "
        "run time). Refused instead of defaulting to zero or an empty series.",
        tool=tool,
        reason="missing_analysis_input",
    )


def _option(params: Mapping[str, Any], key: str, default: Any) -> Any:
    """Return one declared option, applying its documented default.

    ``ToolRegistry.execute`` validates params with
    :func:`~queryforge.orchestration.tools.specs.validate_params`, which builds a
    pydantic model from the spec's schema and dumps it again.  Optional fields
    therefore always arrive - as ``None`` when the caller omitted them - so a
    handler must never forward ``params.get(key)`` straight into a domain
    function: ``detect_anomaly(method=None)`` would raise
    ``unsupported anomaly method None``.  The schemas declare the same defaults
    for documentation and for callers that inspect them; this helper is the
    second half of that guarantee, and it also covers a schema that declares an
    optional field without a default.
    """

    value = params.get(key)
    return default if value is None else value


def _scope_payload(
    payload: dict[str, Any], params: Mapping[str, Any], context: ToolContext
) -> dict[str, Any]:
    """Attach unit/version provenance and refuse a declared-version mismatch."""

    payload["unit"] = params.get("unit")
    payload["data_version"] = context.data_version
    payload["version"] = require_consistent_versions(
        {params.get("version"), context.data_version}
    )
    if context.domain_id:
        payload["domain_id"] = context.domain_id
    return payload


def _compare_periods_impl(params: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    _analysis_input_required("compare_periods", params, ("current", "baseline"))
    result = compare_periods(
        params.get("current"),
        params.get("baseline"),
        label=_option(params, "label", None),
        method=str(_option(params, "method", "absolute_relative")),
    )
    payload = result.model_dump(mode="json")
    payload["evidence_kind"] = "period_comparison"
    return _scope_payload(payload, params, context)


def _drill_down_impl(params: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    _analysis_input_required("drill_down", params, ("buckets", "total", "min_sample", "dimension"))
    result = drill_down(
        params.get("buckets") or [],
        total=_option(params, "total", None),
        max_categories=int(_option(params, "max_categories", 10)),
        min_sample=_option(params, "min_sample", None),
        dimension=_option(params, "dimension", None),
    )
    payload = result.model_dump(mode="json")
    payload["evidence_kind"] = "drill_down"
    return _scope_payload(payload, params, context)


def _calculate_contribution_impl(params: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    _analysis_input_required("calculate_contribution", params, ("buckets", "expected_total_delta"))
    result = contribution_breakdown(
        params.get("buckets") or [],
        expected_total_delta=_option(params, "expected_total_delta", None),
        tolerance=float(_option(params, "tolerance", FLOAT_TOLERANCE)),
        additive=bool(_option(params, "additive", True)),
        metric_kind=str(_option(params, "metric_kind", "additive")),
    )
    payload = result.model_dump(mode="json")
    payload["evidence_kind"] = "contribution_breakdown"
    return _scope_payload(payload, params, context)


def _detect_anomaly_impl(params: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    _analysis_input_required("detect_anomaly", params, ("series",))
    result = detect_anomaly(
        params.get("series") or [],
        method=str(_option(params, "method", "baseline_deviation")),
        min_points=int(_option(params, "min_points", 6)),
        seasonality=str(_option(params, "seasonality", "none")),
        missing=str(_option(params, "missing", "skip")),
        threshold=float(_option(params, "threshold", 2.0)),
    )
    payload = result.model_dump(mode="json")
    payload["evidence_kind"] = "anomaly_scan"
    return _scope_payload(payload, params, context)


def _render_chart_impl(params: dict[str, Any], context: ToolContext) -> dict[str, Any]:
    _analysis_input_required("render_chart", params, ("rows", "columns"))
    result = build_chart(
        params.get("rows") or [],
        params.get("columns") or [],
        metric_kind=str(_option(params, "metric_kind", "additive")),
        grain=_option(params, "grain", None),
        chart_type=_option(params, "chart_type", None),
    )
    payload = result.model_dump(mode="json")
    payload["evidence_kind"] = "chart_spec"
    return _scope_payload(payload, params, context)


def _analysis_spec(
    name: str,
    description: str,
    parameter_schema: dict[str, Any],
    output_schema: dict[str, Any],
    *,
    budget_category: str,
) -> ToolSpec:
    """Spec for one step 11 analysis tool.

    ``modes=("execute",)`` only: these tools produce the analysis facts a plan is
    validated against, so they are never offered in ``plan_only``.  They need no
    permission of their own because they read no data - their inputs arrive as
    parameters produced by an already-authorised governed SQL step, which is also
    why ``budget_category`` is ``compute``/``render`` rather than ``sql``.

    Every parameter is optional on purpose (``additionalProperties: false`` and no
    ``required``): the planner validates a step's ``inputs`` against this schema
    while those inputs are still empty (``{}``), and the executor then calls
    :meth:`ToolRegistry.execute` with the values it assembled from governed SQL at
    run time.  A ``required`` field would therefore reject every planning-stage
    step, and the analysis contracts treat an absent/NULL input as a *data fact*
    (``missing_value``, ``insufficient_data``) that the handler answers for with a
    typed ``missing_analysis_input`` error instead of a schema denial.

    Option knobs carry explicit ``default`` values, and the string knobs also
    accept an explicit ``null`` ("unspecified"), so parameter validation can never
    hand a handler ``method=None``: :func:`_option` resolves both an omitted and a
    nulled knob to its declared default.  Numeric/boolean knobs must be omitted
    rather than nulled, so a non-numeric value can never be silently coerced into
    a number.
    """

    return _spec(
        name,
        description,
        parameter_schema,
        output_schema,
        modes=("execute",),
        permissions=(),
        idempotent=True,
        budget_category=budget_category,
    )


_ANALYSIS_CATALOG: tuple[tuple[ToolSpec, ToolHandler], ...] = (
    (
        _analysis_spec(
            "compare_periods",
            "Compare a current and a baseline window (absolute, relative and "
            "percent change) with explicit zero-baseline/missing states.",
            {
                "type": "object",
                "properties": {
                    "current": _NUMBER_OR_NULL,
                    "baseline": _NUMBER_OR_NULL,
                    "label": _STRING_OR_NULL,
                    "method": {
                        "type": ["string", "null"],
                        "enum": list(COMPARISON_METHODS),
                        "default": "absolute_relative",
                    },
                    **_SCOPE_PROPERTIES,
                },
                "additionalProperties": False,
            },
            {
                "type": "object",
                "properties": {
                    "state": {"type": "string"},
                    "delta": _NUMBER_OR_NULL,
                    "relative_change": _NUMBER_OR_NULL,
                    "percent_change": _NUMBER_OR_NULL,
                    "method": {"type": "string"},
                },
            },
            budget_category="compute",
        ),
        _compare_periods_impl,
    ),
    (
        _analysis_spec(
            "drill_down",
            "Rank dimension buckets, keep the top N, aggregate the tail into an "
            "explicit 'others' bucket and report coverage.",
            {
                "type": "object",
                "properties": {
                    "buckets": _CATEGORY_OR_VALUE,
                    "total": _NUMBER_OR_NULL,
                    "max_categories": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 1_000,
                        "default": 10,
                    },
                    "min_sample": _NUMBER_OR_NULL,
                    "dimension": _STRING_OR_NULL,
                    **_SCOPE_PROPERTIES,
                },
                "additionalProperties": False,
            },
            {
                "type": "object",
                "properties": {
                    "buckets": {"type": "array"},
                    "others": {"type": "object"},
                    "coverage": _NUMBER_OR_NULL,
                    "truncated": {"type": "boolean"},
                },
            },
            budget_category="compute",
        ),
        _drill_down_impl,
    ),
    (
        _analysis_spec(
            "calculate_contribution",
            "Decompose a total change into mutually exclusive additive groups and "
            "report the residual; refuses ratio/distinct inputs.",
            {
                "type": "object",
                "properties": {
                    "buckets": _COMPARISON_BUCKET,
                    "expected_total_delta": _NUMBER_OR_NULL,
                    "tolerance": {"type": "number", "minimum": 0, "default": FLOAT_TOLERANCE},
                    "additive": {"type": "boolean", "default": True},
                    "metric_kind": {
                        "type": ["string", "null"],
                        "enum": list(METRIC_KINDS),
                        "default": "additive",
                    },
                    **_SCOPE_PROPERTIES,
                },
                "additionalProperties": False,
            },
            {
                "type": "object",
                "properties": {
                    "contributions": {"type": "array"},
                    "total_delta": _NUMBER_OR_NULL,
                    "residual": _NUMBER_OR_NULL,
                    "residual_explained": {"type": "boolean"},
                },
            },
            budget_category="compute",
        ),
        _calculate_contribution_impl,
    ),
    (
        _analysis_spec(
            "detect_anomaly",
            "Scan a period series against a declared baseline method and report "
            "per-point scores, expected values and limitations.",
            {
                "type": "object",
                "properties": {
                    "series": _SERIES_POINT,
                    "method": {
                        "type": ["string", "null"],
                        "enum": list(ANOMALY_METHODS),
                        "default": "baseline_deviation",
                    },
                    "min_points": {"type": "integer", "minimum": 2, "default": 6},
                    "seasonality": {
                        "type": ["string", "null"],
                        "enum": list(SEASONALITY_KINDS),
                        "default": "none",
                    },
                    "missing": {
                        "type": ["string", "null"],
                        "enum": list(MISSING_POLICIES),
                        "default": "skip",
                    },
                    "threshold": {"type": "number", "minimum": 0, "default": 2.0},
                    **_SCOPE_PROPERTIES,
                },
                "additionalProperties": False,
            },
            {
                "type": "object",
                "properties": {
                    "state": {"type": "string"},
                    "points": {"type": "array"},
                    "anomalies": {"type": "array"},
                    "method_description": {"type": "string"},
                },
            },
            budget_category="compute",
        ),
        _detect_anomaly_impl,
    ),
    (
        _analysis_spec(
            "render_chart",
            "Choose a chart from the metric kind and grain, or fall back to an "
            "explicit table/metric card when a chart is not justified (no data "
            "access needed).",
            {
                "type": "object",
                "properties": {
                    "rows": {"type": "array", "items": {"type": "array"}},
                    "columns": {"type": "array", "items": {"type": "string"}},
                    "metric_kind": {
                        "type": ["string", "null"],
                        "enum": list(METRIC_KINDS),
                        "default": "additive",
                    },
                    "grain": _STRING_OR_NULL,
                    "chart_type": {"type": ["string", "null"], "enum": list(CHART_TYPES)},
                    **_SCOPE_PROPERTIES,
                },
                "additionalProperties": False,
            },
            {
                "type": "object",
                "properties": {
                    "chart_type": {"type": "string"},
                    "reason": {"type": "string"},
                    "vega_lite_spec": {"type": ["object", "null"]},
                },
            },
            budget_category="render",
        ),
        _render_chart_impl,
    ),
)


def bind_catalog(registry: ToolRegistry) -> ToolRegistry:
    """Bind the registry reference into handlers created by :func:`_bind`."""

    for handler in registry._handlers.values():
        reference = getattr(handler, "__tool_bind__", None)
        if isinstance(reference, dict):
            reference["registry"] = registry
    return registry


__all__ = [
    "PLACEHOLDER_TOOLS",
    "ToolHandler",
    "ToolRegistry",
    "bind_catalog",
    "build_default_registry",
]
