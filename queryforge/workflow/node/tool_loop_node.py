"""Bounded, read-only observation loop before SQL generation."""

from __future__ import annotations

import json
import time
from typing import Any

from queryforge.workflow.node.base import Node
from queryforge.infrastructure.models.base import BaseModelProvider, ModelResponseError
from queryforge.core.schemas.models import Context, NodeResult, SQLContext
from queryforge.infrastructure.tools.database_tool import DatabaseTool, UnsafeSQLError


ALLOWED_ACTIONS = {
    "list_tables",
    "describe_table",
    "preview_distinct_values",
    "execute_sql_preview",
    "final_answer",
}


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
    ) -> None:
        if max_rounds < 1:
            raise ValueError("tool loop max_rounds must be positive")
        if timeout_seconds <= 0:
            raise ValueError("tool loop timeout_seconds must be positive")
        self.llm = llm
        self.database_tool = database_tool
        self.max_rounds = max_rounds
        self.timeout_seconds = timeout_seconds
        self.preview_limit = min(max(preview_limit, 1), 100)

    def execute(self, context: Context) -> NodeResult:
        started = time.monotonic()
        context.tool_loop_history = []
        context.tool_loop_status = "completed"
        context.tool_loop_exit_reason = "final_answer"
        observations: list[dict[str, Any]] = []
        seen_actions: set[str] = set()

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
                    observation = {
                        "error": f"Unknown tool action: {action!r}",
                        "allowed_actions": sorted(ALLOWED_ACTIONS),
                    }
                    context.tool_loop_status = "error"
                    context.tool_loop_exit_reason = "invalid_action"
                    self._record(context, round_number, action, params, observation)
                    return self.success("Tool loop stopped on invalid action")
                if action == "final_answer":
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
                    return self.success("Tool loop stopped on repeated action")
                seen_actions.add(key)
                observation = self._execute_action(action, params)
                observations.append(
                    {"round": round_number, "action": action, "observation": observation}
                )
                self._record(context, round_number, action, params, observation)
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

    def _execute_action(self, action: str, params: dict[str, Any]) -> dict[str, Any]:
        if action == "list_tables":
            return {"tables": self.database_tool.list_tables()}
        if action == "describe_table":
            schema = self.database_tool.describe_table(str(params["table_name"]))
            return schema.model_dump(mode="json")
        if action == "preview_distinct_values":
            values = self.database_tool.preview_distinct_values(
                str(params["table_name"]),
                str(params["column_name"]),
                int(params.get("limit", self.preview_limit)),
            )
            return {"values": values[: self.preview_limit]}
        if action == "execute_sql_preview":
            result = self.database_tool.execute_sql_preview(
                str(params["sql"]),
                int(params.get("limit", self.preview_limit)),
            )
            return {
                "columns": result.columns,
                "rows": result.rows[: self.preview_limit],
                "row_count": min(result.row_count, self.preview_limit),
            }
        raise UnsafeSQLError(f"Unsupported tool action: {action}")

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
