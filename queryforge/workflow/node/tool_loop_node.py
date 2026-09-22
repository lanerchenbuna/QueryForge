"""Bounded, read-only observation loop before SQL generation.

Step 09 keeps the original bounded-loop semantics (whitelist, round cap, wall
clock, repeat detection) and moves the action dispatch onto
:class:`~queryforge.orchestration.tools.registry.ToolRegistry`: every dispatched
action is a registered :class:`~queryforge.orchestration.tools.specs.ToolSpec`,
so parameters are validated, permission/mode boundaries apply, resources are
reserved before the call, and both the typed ``ToolCall`` and its
``ToolObservation`` are recorded on ``context.task_context["tool_calls"]``.
"""

from __future__ import annotations

import json
import time
from typing import Any

from queryforge.workflow.node.base import Node
from queryforge.infrastructure.models.base import BaseModelProvider, ModelResponseError
from queryforge.core.schemas.models import Context, NodeResult, SQLContext
from queryforge.infrastructure.tools.database_tool import DatabaseTool, UnsafeSQLError
from queryforge.orchestration.tools import (
    BudgetManager,
    ToolObservation,
    ToolRegistry,
    build_default_registry,
)


#: Whitelist of actions the loop may ask for. ``final_answer`` is local logic;
#: the four observation actions are dispatched to the registry tools below.
ALLOWED_ACTIONS = {
    "list_tables",
    "describe_table",
    "preview_distinct_values",
    "execute_sql_preview",
    "final_answer",
}

#: Compatibility mapping from the historical action vocabulary onto registry
#: tools (the loop prompt still speaks the old names).
ACTION_TOOLS: dict[str, str] = {
    "list_tables": "list_tables",
    "describe_table": "describe_table",
    "preview_distinct_values": "preview_distinct_values",
    "execute_sql_preview": "execute_sql_preview",
}

#: Actions handled by this node itself instead of a registered tool.
LOCAL_ACTIONS = frozenset({"final_answer"})

#: Registry modes; ``plan_only`` refuses execute-class (SQL) tools.
VALID_MODES = frozenset({"execute", "plan_only"})


class ToolLoopNode(Node):
    name = "tool_loop"
    description = "Collect bounded read-only observations before SQL generation"

    def __init__(
        self,
        llm: BaseModelProvider,
        database_tool: DatabaseTool,
        *,
        max_rounds: int = 5,
        timeout_seconds: float = 30,
        preview_limit: int = 20,
        budget_manager: BudgetManager | None = None,
        registry: ToolRegistry | None = None,
        mode: str = "execute",
    ) -> None:
        if max_rounds < 1:
            raise ValueError("tool loop max_rounds must be positive")
        if timeout_seconds <= 0:
            raise ValueError("tool loop timeout_seconds must be positive")
        if mode not in VALID_MODES:
            raise ValueError(f"tool loop mode must be one of {sorted(VALID_MODES)}")
        self.llm = llm
        self.database_tool = database_tool
        self.max_rounds = max_rounds
        self.timeout_seconds = timeout_seconds
        self.preview_limit = min(max(preview_limit, 1), 100)
        self.mode = mode
        self.budget_manager = budget_manager or BudgetManager()
        self.registry = registry or build_default_registry(
            self.database_tool, self.budget_manager
        )

    def execute(self, context: Context) -> NodeResult:
        started = time.monotonic()
        context.tool_loop_history = []
        context.tool_loop_status = "completed"
        context.tool_loop_exit_reason = "final_answer"
        observations: list[dict[str, Any]] = []
        seen_actions: set[str] = set()
        tool_calls: list[dict[str, Any]] = context.task_context.setdefault("tool_calls", [])

        for round_number in range(1, self.max_rounds + 1):
            elapsed = time.monotonic() - started
            if elapsed >= self.timeout_seconds:
                context.tool_loop_status = "timeout"
                context.tool_loop_exit_reason = "timeout"
                return self.success("Tool loop timeout reached")
            try:
                decision = self._ask(context, observations, round_number)
                action = decision.get("action")
                params = decision.get("params") or {}
                if action not in ALLOWED_ACTIONS:
                    observation = self._denied_observation(context, action, params)
                    tool_calls.append(observation)
                    context.tool_loop_status = "error"
                    context.tool_loop_exit_reason = "invalid_action"
                    self._record(
                        context,
                        round_number,
                        action,
                        params,
                        observation["observation_payload"],
                    )
                    return self.success("Tool loop stopped on invalid action")
                if action in LOCAL_ACTIONS:
                    sql = params.get("sql")
                    if sql:
                        clean_sql = DatabaseTool.validate_readonly_sql(sql)
                        context.sql_context = SQLContext(
                            sql=clean_sql,
                            explanation=str(
                                params.get("explanation")
                                or "SQL selected after bounded tool observations."
                            ),
                            tables_used=list(params.get("tables_used") or []),
                        )
                    context.tool_loop_exit_reason = "final_answer"
                    self._record(
                        context,
                        round_number,
                        action,
                        params,
                        {"status": "final_answer"},
                    )
                    tool_calls.append(
                        {
                            "tool": action,
                            "params": dict(params),
                            "status": "succeeded",
                            "local": True,
                        }
                    )
                    return self.success("Tool loop completed with final answer")

                key = json.dumps(
                    {"action": action, "params": params},
                    sort_keys=True,
                    ensure_ascii=False,
                )
                if key in seen_actions:
                    context.tool_loop_status = "completed"
                    context.tool_loop_exit_reason = "repeated_action"
                    self._record(
                        context,
                        round_number,
                        action,
                        params,
                        {"error": "Repeated action stopped to avoid an unproductive loop."},
                    )
                    tool_calls.append(
                        {
                            "tool": ACTION_TOOLS.get(action, str(action)),
                            "params": dict(params),
                            "status": "skipped",
                            "reason": "repeated_action",
                        }
                    )
                    return self.success("Tool loop stopped on repeated action")
                seen_actions.add(key)
                observation = self._execute_action(action, params, context)
                tool_calls.append(
                    {
                        "call": observation.call.to_payload() if observation.call else None,
                        "observation": observation.to_payload(),
                    }
                )
                if not observation.ok:
                    self._record(
                        context,
                        round_number,
                        action,
                        params,
                        observation.observation_payload(),
                    )
                    context.tool_loop_status = "error"
                    context.tool_loop_exit_reason = "tool_error"
                    return self.success("Tool loop stopped on tool error")
                payload = observation.observation_payload()
                observations.append(
                    {"round": round_number, "action": action, "observation": payload}
                )
                self._record(context, round_number, action, params, payload)
            except ModelResponseError as exc:
                context.tool_loop_status = "error"
                context.tool_loop_exit_reason = "model_error"
                return self.failure(f"Tool loop model response failed: {exc}")
            except Exception as exc:
                observation = {"error": str(exc)}
                self._record(
                    context,
                    round_number,
                    decision.get("action") if "decision" in locals() else None,
                    decision.get("params") if "decision" in locals() else {},
                    observation,
                )
                context.tool_loop_status = "error"
                context.tool_loop_exit_reason = "tool_error"
                return self.success("Tool loop stopped on tool error")

        context.tool_loop_status = "max_rounds"
        context.tool_loop_exit_reason = "max_rounds"
        return self.success("Tool loop maximum rounds reached")

    def _ask(
        self,
        context: Context,
        observations: list[dict[str, Any]],
        round_number: int,
    ) -> dict[str, Any]:
        prompt = f"""Choose one bounded read-only action for a SQLite question.
Return exactly one JSON object with thought, action, and params.
Allowed actions: list_tables, describe_table, preview_distinct_values,
execute_sql_preview, final_answer.
For final_answer, params may contain sql, explanation, and tables_used.
Never use an action outside the allowlist.

Question:
{context.task.question}

Round:
{round_number}/{self.max_rounds}

Observations:
{json.dumps(observations[-5:], ensure_ascii=False, indent=2)}
"""
        payload = self.llm.generate_json(prompt)
        if not isinstance(payload, dict):
            raise ModelResponseError("Tool loop response must be a JSON object", str(payload))
        return payload

    def _execute_action(
        self, action: str, params: dict[str, Any], context: Context
    ) -> ToolObservation:
        """Dispatch one whitelisted action to its registered tool."""

        tool_name = ACTION_TOOLS.get(action)
        if tool_name is None:
            raise UnsafeSQLError(f"Unsupported tool action: {action}")
        if action == "preview_distinct_values":
            params = {
                "table_name": params.get("table_name"),
                "column_name": params.get("column_name"),
                "limit": self._bounded_limit(params.get("limit")),
            }
        elif action == "execute_sql_preview":
            params = {
                "sql": params.get("sql"),
                "limit": self._bounded_limit(params.get("limit")),
            }
        return self.registry.execute(
            tool_name,
            params,
            context=context,
            mode=self.mode,
        )

    def _bounded_limit(self, value: Any) -> int:
        try:
            requested = int(value)
        except (TypeError, ValueError):
            requested = self.preview_limit
        return max(1, min(requested, self.preview_limit, 100))

    def _denied_observation(
        self, context: Context, action: Any, params: dict[str, Any]
    ) -> dict[str, Any]:
        """Record the refused call in the same shape as a registry denial."""

        observation = self.registry.execute(
            str(action), params, context=context, mode=self.mode
        )
        return {
            "call": observation.call.to_payload() if observation.call else None,
            "observation": observation.to_payload(),
            "observation_payload": {
                "error": f"Unknown tool action: {action!r}",
                "allowed_actions": sorted(ALLOWED_ACTIONS),
                "error_category": observation.error_category,
                "status": observation.status,
            },
        }

    @staticmethod
    def _record(
        context: Context,
        round_number: int,
        action: Any,
        params: dict[str, Any],
        observation: dict[str, Any],
    ) -> None:
        context.tool_loop_history.append(
            {
                "round": round_number,
                "action": action,
                "params": params,
                "observation": observation,
            }
        )
