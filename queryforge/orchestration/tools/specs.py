"""Typed tool protocol: specs, calls, observations, and typed tool errors.

Step 09 replaces the ad-hoc action dispatch with an explicit contract:

* :class:`ToolSpec`  - what a tool is, in which modes it may run, which
  permissions it needs, and which budget category it charges.
* :class:`ToolCall`  - one attempted invocation bound to run/task/domain/version.
* :class:`ToolObservation` - the typed result of one invocation, including an
  explicit truncation marker so a partial result is never passed off as full.

The module only depends on the workflow error taxonomy; it knows nothing about
transports, agents, or the SQL kernel.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError, create_model

from queryforge.workflow.errors import WorkflowErrorCategory

ToolMode = Literal["read", "execute", "plan_only"]
ToolCallStatus = Literal["pending", "running", "succeeded", "failed", "timeout", "denied"]

#: Modes in which a tool may be invoked.  ``execute`` is the normal analysis
#: mode, ``plan_only`` is the static-metadata mode that must never run generated
#: SQL, and ``read`` marks a tool as read-only data access.  A tool that lists
#: ``execute`` (or ``plan_only``) declares which environment may call it.
DEFAULT_MODES: tuple[ToolMode, ...] = ("read", "execute")


class ToolBudgetError(ValueError):
    """A reservation was refused because a budget bound would be exceeded."""

    def __init__(self, message: str, *, limit: str | None = None) -> None:
        self.limit = limit
        super().__init__(message)


class ToolUnavailable(ValueError):
    """The tool is unknown, undeclared, or declared without an implementation."""

    def __init__(self, message: str, *, tool: str = "", reason: str = "not_implemented") -> None:
        self.tool = tool
        self.reason = reason
        super().__init__(message)


class ToolDenied(ValueError):
    """The call was refused before execution (mode, permission, or parameter)."""

    def __init__(self, message: str, *, tool: str = "", reason: str = "denied") -> None:
        self.tool = tool
        self.reason = reason
        super().__init__(message)


def utc_now_iso() -> str:
    """Return the current UTC instant in ISO-8601 form (second resolution)."""

    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


# --------------------------------------------------------------------- params


_TYPE_ANNOTATIONS: dict[str, Any] = {
    "string": str,
    "integer": int,
    "number": float,
    "boolean": bool,
    "object": dict,
    "array": list,
    "null": type(None),
}

_PARAM_MODEL_CACHE: dict[tuple[str, str], type[BaseModel]] = {}


def annotation_for(schema: dict[str, Any] | None) -> Any:
    """Map a JSON-Schema-style fragment onto a python annotation.

    Only the subset QueryForge actually uses is modelled: scalars, arrays,
    objects.  Anything unrecognised is accepted as ``Any`` so an unsupported
    keyword cannot silently reject a legal value.
    """

    if not isinstance(schema, dict):
        return Any
    declared = schema.get("type")
    if isinstance(declared, list):
        annotations = [annotation_for({**schema, "type": item}) for item in declared]
        unique: list[Any] = []
        for item in annotations:
            if item not in unique:
                unique.append(item)
        if len(unique) == 1:
            return unique[0]
        return Any
    if isinstance(declared, str) and declared in _TYPE_ANNOTATIONS:
        return _TYPE_ANNOTATIONS[declared]
    if "enum" in schema or "oneOf" in schema or "anyOf" in schema:
        return Any
    return Any


def build_param_model(tool_name: str, schema: dict[str, Any] | None) -> type[BaseModel]:
    """Build (and cache) a strict pydantic model for one parameter schema."""

    schema = schema if isinstance(schema, dict) else {}
    cache_key = (tool_name, json.dumps(schema, sort_keys=True, default=str))
    cached = _PARAM_MODEL_CACHE.get(cache_key)
    if cached is not None:
        return cached
    properties = schema.get("properties")
    properties = properties if isinstance(properties, dict) else {}
    required = {str(item) for item in (schema.get("required") or [])}
    fields: dict[str, Any] = {}
    for key, fragment in properties.items():
        annotation = annotation_for(fragment)
        if key in required:
            fields[key] = (annotation, ...)
        else:
            default = fragment.get("default") if isinstance(fragment, dict) else None
            fields[key] = (annotation, default)
    model = create_model(
        f"{_model_name(tool_name)}Params",
        __config__=ConfigDict(extra="forbid"),
        **fields,
    )
    _PARAM_MODEL_CACHE[cache_key] = model
    return model


def _model_name(tool_name: str) -> str:
    cleaned = "".join(
        part.capitalize() for part in str(tool_name or "tool").replace("-", "_").split("_")
    )
    return cleaned or "Tool"


def validate_params(
    tool_name: str,
    schema: dict[str, Any] | None,
    params: dict[str, Any] | None,
) -> dict[str, Any]:
    """Validate ``params`` against a JSON-Schema-style schema.

    Raises :class:`ToolDenied` (a ``ValueError``) before any handler runs, so an
    invalid call can never reach an arbitrary function.
    """

    if params is None:
        params = {}
    if not isinstance(params, dict):
        raise ToolDenied(
            f"invalid_tool_params: {tool_name!r} params must be a JSON object, "
            f"got {type(params).__name__}",
            tool=tool_name,
            reason="invalid_params",
        )
    model = build_param_model(tool_name, schema)
    try:
        validated = model.model_validate(params)
    except ValidationError as exc:
        raise ToolDenied(
            f"invalid_tool_params: {tool_name!r} {exc.errors()}",
            tool=tool_name,
            reason="invalid_params",
        ) from exc
    data = validated.model_dump()
    for key, allowed in _enum_fields(schema).items():
        value = data.get(key)
        if value is None:
            continue
        if value not in allowed:
            raise ToolDenied(
                f"invalid_tool_params: {tool_name!r} parameter {key!r} must be one "
                f"of {allowed}, got {value!r}",
                tool=tool_name,
                reason="invalid_params",
            )
    return data


def _enum_fields(schema: dict[str, Any] | None) -> dict[str, list[Any]]:
    if not isinstance(schema, dict):
        return {}
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        return {}
    return {
        key: list(fragment["enum"])
        for key, fragment in properties.items()
        if isinstance(fragment, dict) and isinstance(fragment.get("enum"), list)
    }


# ---------------------------------------------------------------------- specs


class ToolSpec(BaseModel):
    """Declarative description of one governed tool."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    description: str = ""
    parameter_schema: dict[str, Any] = Field(default_factory=dict)
    output_schema: dict[str, Any] = Field(default_factory=dict)
    permissions: list[str] = Field(default_factory=list)
    modes: list[ToolMode] = Field(default_factory=lambda: list(DEFAULT_MODES))
    idempotent: bool = True
    budget_category: str = "read"

    def permits(self, mode: str) -> bool:
        """True when this tool may be invoked in ``mode``."""

        return mode in self.modes

    def to_payload(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


class ToolCall(BaseModel):
    """One attempted invocation, bound to run/task/domain/version."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(default_factory=lambda: f"tc_{uuid4().hex[:12]}")
    run_id: str | None = None
    task_id: str | None = None
    domain_id: str | None = None
    data_version: str | None = None
    tool: str = Field(min_length=1)
    params: dict[str, Any] = Field(default_factory=dict)
    status: ToolCallStatus = "pending"
    started_at: str | None = None
    finished_at: str | None = None
    error_category: str | None = None
    error: str | None = None
    observation_ref: str | None = None
    duration_ms: float = 0.0
    estimated_tokens: int = 0
    output_rows: int = 0
    truncated: bool = False

    @property
    def succeeded(self) -> bool:
        return self.status == "succeeded"

    def to_payload(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


class ToolObservation(BaseModel):
    """Typed result of one invocation (never a silently partial result)."""

    model_config = ConfigDict(extra="forbid")

    tool: str = Field(min_length=1)
    params: dict[str, Any] = Field(default_factory=dict)
    truncated: bool = False
    result: Any = None
    error_category: str | None = None
    evidence_ids: list[str] = Field(default_factory=list)
    duration_ms: float = 0.0
    estimated_tokens: int = 0
    #: Traceability additions: the id of the recorded :class:`ToolCall` and the
    #: call record itself, so a caller can journal both without a second lookup.
    call_id: str | None = None
    call: ToolCall | None = None
    status: ToolCallStatus = "succeeded"
    truncation: dict[str, Any] | None = None

    @property
    def ok(self) -> bool:
        return self.error_category is None

    def observation_payload(self) -> dict[str, Any]:
        """Payload stored in workflow traces (result, or a typed error)."""

        if self.error_category is not None:
            payload: dict[str, Any] = {
                "error": self.call.error if self.call and self.call.error else f"{self.tool} failed",
                "error_category": self.error_category,
                "status": self.status,
            }
        else:
            payload = self.result if isinstance(self.result, dict) else {"result": self.result}
            payload = dict(payload)
        if self.truncated:
            payload["truncated"] = True
            if self.truncation:
                payload["truncation"] = dict(self.truncation)
        payload["tool"] = self.tool
        payload["status"] = self.status
        return payload

    def to_payload(self) -> dict[str, Any]:
        payload = self.model_dump(mode="json")
        payload["ok"] = self.ok
        return payload


# -------------------------------------------------------------------- context


@dataclass
class ToolContext:
    """Environment handed to a tool handler (data scope, versions, resources).

    A handler receives this instead of the workflow ``Context`` so tools cannot
    reach into unrelated workflow state; :meth:`coerce` adapts the workflow
    ``Context`` (or a plain mapping) when the tool loop or the executor calls in.
    """

    run_id: str | None = None
    task_id: str | None = None
    domain_id: str | None = None
    data_version: str | None = None
    question: str | None = None
    database_tool: Any = None
    semantic_model: Any = None
    granted_permissions: frozenset[str] | None = None
    allowed_domains: frozenset[str] | None = None
    evidence_prefix: str | None = None
    #: Resolved date semantics for this step (step 04/06): a time-scoped question
    #: must reach the compiler, or the answer silently covers all time.
    date_context: Any = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def coerce(cls, context: Any, **overrides: Any) -> "ToolContext":
        """Build a :class:`ToolContext` from any workflow-ish object."""

        if isinstance(context, ToolContext):
            resolved = ToolContext(**{**context.__dict__, **overrides})
            return resolved
        task_context = getattr(context, "task_context", None)
        task_context = task_context if isinstance(task_context, dict) else {}
        task = getattr(context, "task", None)
        values: dict[str, Any] = {
            "run_id": _text(getattr(context, "run_id", None)) or _text(task_context.get("run_id")),
            "task_id": _text(
                getattr(context, "task_id", None)
                or task_context.get("task_id")
                or getattr(task, "task_id", None)
            ),
            "domain_id": _text(
                getattr(context, "domain_id", None) or task_context.get("domain_id")
            ),
            "data_version": _text(
                getattr(context, "data_version", None) or task_context.get("data_version")
            ),
            "question": _text(
                getattr(context, "question", None)
                or getattr(task, "question", None)
                or task_context.get("question")
            ),
            "database_tool": getattr(context, "database_tool", None),
            "semantic_model": getattr(context, "semantic_model", None),
            "granted_permissions": _permission_set(
                getattr(context, "permissions", None) or task_context.get("permissions")
            ),
            "allowed_domains": _permission_set(
                getattr(context, "allowed_domains", None) or task_context.get("allowed_domains")
            ),
            "evidence_prefix": _text(
                getattr(context, "evidence_prefix", None)
                or task_context.get("evidence_prefix")
            ),
            "metadata": dict(task_context),
        }
        values.update({key: value for key, value in overrides.items() if value is not None})
        return cls(**values)

    def evidence_id(self, kind: str, index: int = 0) -> str:
        prefix = self.evidence_prefix or self.run_id or "run"
        return f"ev:{prefix}:{kind}:{index}"


def _permission_set(value: Any) -> frozenset[str] | None:
    if value is None:
        return None
    if isinstance(value, str):
        return frozenset({value})
    if isinstance(value, (list, tuple, set, frozenset)):
        return frozenset(str(item) for item in value)
    return None


def _text(value: Any) -> str | None:
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    return None


# ------------------------------------------------------------------ estimates


def estimate_tokens(payload: Any) -> int:
    """Rough token estimate for a payload (chars/4), never negative."""

    try:
        text = json.dumps(payload, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        text = str(payload)
    return max(1, len(text) // 4)


JsonSchemaLike = dict[str, Any]


__all__ = [
    "DEFAULT_MODES",
    "JsonSchemaLike",
    "ToolBudgetError",
    "ToolCall",
    "ToolCallStatus",
    "ToolContext",
    "ToolDenied",
    "ToolMode",
    "ToolObservation",
    "ToolSpec",
    "ToolUnavailable",
    "WorkflowErrorCategory",
    "annotation_for",
    "build_param_model",
    "estimate_tokens",
    "utc_now_iso",
    "validate_params",
]
