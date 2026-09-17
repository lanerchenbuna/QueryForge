"""Raw runner traces: the material the evaluator grades (step 16).

A trace is deliberately *raw*: the JSON payload the agent returned, the tool
calls the runner observed, the wall time and the token/cost accounting.  Nothing
in a trace is a verdict -- in particular a payload's own ``validation_problems``,
``review_required`` or ``special_cases`` are never read as a result; the
evaluator re-derives traceability from the payload itself.

Every accessor here is defensive because the payload can come from two different
layers (the analysis planner and the workflow output node) and from runners that
record optional fields or add their own:

* a missing key yields the documented neutral value instead of raising;
* a wrongly typed value is coerced when the intent is unambiguous (a numeric
  string is a number) and ignored otherwise;
* unknown keys are preserved by the pydantic model, so a runner can attach its
  own bookkeeping without breaking scoring.
"""

from __future__ import annotations

import math
from typing import Any, Iterable, Mapping, Sequence

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

#: Tool-call statuses that mean "this call did not succeed".
FAILED_CALL_STATUSES: frozenset[str] = frozenset(
    {"failed", "error", "timeout", "timed_out", "blocked", "denied", "rejected"}
)

#: Keys inside one tool-call record that carry a failure verdict, in priority
#: order: an explicit boolean wins over a status word, which wins over an error
#: message (a runner that records only the error still produces a failed call).
_OK_KEYS: tuple[str, ...] = ("ok", "success", "succeeded")

_TRUE_WORDS: frozenset[str] = frozenset({"true", "1", "yes", "ok", "success", "succeeded"})
_FALSE_WORDS: frozenset[str] = frozenset({"false", "0", "no", "failed", "error", "timeout"})


def as_mapping(value: Any) -> dict[str, Any]:
    """Return ``value`` as a plain dict, or an empty dict when it is not one."""

    if isinstance(value, Mapping):
        return {str(key): item for key, item in value.items()}
    return {}


def as_sequence(value: Any) -> list[Any]:
    """Return ``value`` as a list; a scalar becomes a one-item list, ``None`` empty."""

    if value is None:
        return []
    if isinstance(value, (list, tuple, set, frozenset)):
        return list(value)
    return [value]


def as_text(value: Any) -> str:
    """Return a stripped string, or ``""`` when there is nothing to read."""

    if value is None or isinstance(value, bool):
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float)):
        return str(value)
    return ""


def as_number(value: Any) -> float | None:
    """Return a finite float, or ``None`` (``bool`` is not a number here)."""

    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        try:
            number = float(value.strip())
        except ValueError:
            return None
    else:
        return None
    return number if math.isfinite(number) else None


def as_int(value: Any) -> int | None:
    """Return an int when ``value`` is integral, else ``None``."""

    number = as_number(value)
    if number is None:
        return None
    rounded = int(round(number))
    return rounded if math.isclose(number, rounded, rel_tol=0.0, abs_tol=1e-9) else None


def as_bool(value: Any) -> bool | None:
    """Return a real boolean; the words ``true``/``false`` are accepted."""

    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        word = value.strip().casefold()
        if word in _TRUE_WORDS:
            return True
        if word in _FALSE_WORDS:
            return False
    return None


def payload_path(payload: Any, path: str) -> tuple[bool, Any]:
    """Resolve a dotted ``path`` inside ``payload``.

    Returns ``(found, value)`` so a caller can tell "absent" from "present but
    ``None``" -- the difference matters when a gold value is compared.
    """

    node: Any = payload
    for part in [item for item in str(path).split(".") if item]:
        if isinstance(node, Mapping) and part in node:
            node = node[part]
            continue
        if isinstance(node, (list, tuple)):
            try:
                node = node[int(part)]
            except (ValueError, IndexError):
                return False, None
            continue
        return False, None
    return True, node


def payload_get(payload: Any, path: str, default: Any = None) -> Any:
    """Convenience wrapper around :func:`payload_path`."""

    found, value = payload_path(payload, path)
    return value if found else default


def tool_name(call: Any) -> str:
    """Name of one recorded tool call (``tool``, else ``name``/``action``)."""

    record = as_mapping(call)
    for key in ("tool", "tool_name", "name", "action"):
        text = as_text(record.get(key))
        if text:
            return text
    return ""


def tool_action(call: Any) -> str:
    """Planner action of one recorded call, when the runner recorded it.

    The runner may record either vocabulary (the governed tool name or the plan
    action); keeping both lets the allowance check accept the gold's spelling
    without the evaluator having to guess which one it used.
    """

    return as_text(as_mapping(call).get("action"))


def tool_ok(call: Any) -> bool:
    """Whether one recorded call succeeded.

    A record with no verdict at all counts as successful: the runner simply did
    not say otherwise, and inventing a failure from silence would report defects
    the trace does not support.
    """

    record = as_mapping(call)
    for key in _OK_KEYS:
        verdict = as_bool(record.get(key))
        if verdict is not None:
            return verdict
    status = as_text(record.get("status")).casefold()
    if status:
        return status not in FAILED_CALL_STATUSES
    for key in ("error", "error_category", "exception", "failure"):
        if as_text(record.get(key)):
            return False
    return True


def tool_error_category(call: Any) -> str:
    """Error category of one recorded call (``""`` when absent)."""

    record = as_mapping(call)
    for key in ("error_category", "category", "error_type"):
        text = as_text(record.get(key))
        if text:
            return text
    return ""


def tool_error(call: Any) -> str:
    """Error text of one recorded call (``""`` when absent)."""

    record = as_mapping(call)
    for key in ("error", "message", "detail"):
        text = as_text(record.get(key))
        if text:
            return text
    return ""


def tool_duration_ms(call: Any) -> float | None:
    """Duration of one recorded call in milliseconds, when recorded."""

    record = as_mapping(call)
    for key in ("duration_ms", "wall_ms", "elapsed_ms"):
        number = as_number(record.get(key))
        if number is not None:
            return number
    return None


class TaskTrace(BaseModel):
    """One recorded run of one gold task (contract 3.1)."""

    model_config = ConfigDict(extra="allow")

    task_id: str
    payload: dict[str, Any] = Field(default_factory=dict)
    wall_ms: float = 0.0
    tool_calls: list[dict[str, Any]] = Field(default_factory=list)
    usage: dict[str, Any] | None = None
    cost_usd: float | None = None
    provider: str | None = None
    model: str | None = None
    error: str | None = None

    @field_validator("task_id")
    @classmethod
    def _require_task_id(cls, value: str) -> str:
        text = str(value).strip()
        if not text:
            raise ValueError("a trace must name its task_id")
        return text

    @field_validator("payload", mode="before")
    @classmethod
    def _coerce_payload(cls, value: Any) -> dict[str, Any]:
        return as_mapping(value)

    @field_validator("wall_ms", mode="before")
    @classmethod
    def _coerce_wall_ms(cls, value: Any) -> float:
        number = as_number(value)
        if number is None or number < 0.0:
            return 0.0
        return round(number, 3)

    @field_validator("tool_calls", mode="before")
    @classmethod
    def _coerce_tool_calls(cls, value: Any) -> list[dict[str, Any]]:
        calls: list[dict[str, Any]] = []
        for item in as_sequence(value):
            if isinstance(item, Mapping):
                calls.append(as_mapping(item))
            elif as_text(item):
                # A runner that recorded only tool names still yields a call.
                calls.append({"tool": as_text(item)})
        return calls

    @field_validator("usage", mode="before")
    @classmethod
    def _coerce_usage(cls, value: Any) -> dict[str, Any] | None:
        if value is None:
            return None
        if isinstance(value, Mapping):
            return as_mapping(value)
        dump = getattr(value, "to_dict", None)
        if callable(dump):
            return as_mapping(dump())
        return None

    @field_validator("cost_usd", "provider", "model", "error", mode="before")
    @classmethod
    def _coerce_optional_text(cls, value: Any) -> Any:
        return None if value is None or value == "" else value

    @model_validator(mode="after")
    def _normalize_cost(self) -> "TaskTrace":
        if self.cost_usd is not None:
            self.cost_usd = as_number(self.cost_usd)
        if self.provider is not None:
            self.provider = as_text(self.provider) or None
        if self.model is not None:
            self.model = as_text(self.model) or None
        if self.error is not None:
            self.error = as_text(self.error) or None
        return self

    # -- convenience ------------------------------------------------------

    @property
    def tool_call_count(self) -> int:
        return len(self.tool_calls)

    @property
    def failed_tool_calls(self) -> list[dict[str, Any]]:
        return [call for call in self.tool_calls if not tool_ok(call)]

    @property
    def tool_names(self) -> list[str]:
        return [name for name in (tool_name(call) for call in self.tool_calls) if name]

    @classmethod
    def from_mapping(cls, record: Mapping[str, Any]) -> "TaskTrace":
        """Build a trace from a runner record (unknown keys are preserved)."""

        return cls.model_validate(as_mapping(record))


def recorded_tool_counts(tool_calls: Sequence[Any]) -> dict[str, int]:
    """Histogram of recorded tool names (stable ordering by name)."""

    counts: dict[str, int] = {}
    for call in tool_calls:
        name = tool_name(call)
        key = name or "<unnamed>"
        counts[key] = counts.get(key, 0) + 1
    return {key: counts[key] for key in sorted(counts)}


def iter_mappings(values: Iterable[Any]) -> Iterable[dict[str, Any]]:
    """Yield only the mapping entries of ``values`` (defensive list walking)."""

    for value in values:
        if isinstance(value, Mapping):
            yield as_mapping(value)


__all__ = [
    "FAILED_CALL_STATUSES",
    "TaskTrace",
    "as_bool",
    "as_int",
    "as_mapping",
    "as_number",
    "as_sequence",
    "as_text",
    "iter_mappings",
    "payload_get",
    "payload_path",
    "recorded_tool_counts",
    "tool_action",
    "tool_duration_ms",
    "tool_error",
    "tool_error_category",
    "tool_name",
    "tool_ok",
]
