"""Fixed sequential workflow implementation."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from datetime import datetime, timezone

from queryforge.workflow.node.base import Node
from queryforge.workflow.event_emitter import emit_event
from queryforge.core.schemas.models import Context, NodeResult, SqlAttempt
from queryforge.core.observability import node_logging_context, run_logging_context


LOGGER = logging.getLogger("queryforge.workflow")


def _execute_observed_node(context: Context, node: Node) -> NodeResult:
    started_at = datetime.now(timezone.utc)
    started = time.perf_counter()
    emit_event(
        context.event_emitter,
        "node_started",
        context.run_id,
        node_name=node.name,
        status="running",
        message=f"Started {node.name}.",
    )
    with run_logging_context(context.run_id), node_logging_context(node.name):
        LOGGER.info("node_start node=%s started_at=%s", node.name, started_at.isoformat())
        try:
            result = node.execute(context)
        except Exception as exc:
            result = NodeResult(
                node_name=node.name,
                success=False,
                status="failed",
                error=f"Unexpected node error: {exc}",
            )
        ended_at = datetime.now(timezone.utc)
        duration_ms = round((time.perf_counter() - started) * 1000, 3)
        result = result.model_copy(
            update={
                "started_at": started_at.isoformat(),
                "ended_at": ended_at.isoformat(),
                "duration_ms": duration_ms,
            }
        )
        context.node_results.append(result)
        emit_event(
            context.event_emitter,
            "node_completed" if result.success else "node_failed",
            context.run_id,
            node_name=node.name,
            status=result.status,
            message=result.message if result.success else result.error,
            data={"duration_ms": duration_ms},
        )
        log = LOGGER.info if result.success else LOGGER.error
        log(
            "node_end node=%s ended_at=%s duration_ms=%s success=%s error=%s",
            node.name,
            ended_at.isoformat(),
            duration_ms,
            result.success,
            result.error,
        )
        return result


class WorkflowError(RuntimeError):
    """Raised when a workflow node reports failure."""

    def __init__(self, node_name: str, error: str, context: Context) -> None:
        completed = [result.node_name for result in context.node_results if result.success]
        summary = {
            "completed_nodes": completed,
            "tables_loaded": len(context.relevant_tables),
            "date_ranges": len(context.date_context.ranges) if context.date_context else 0,
            "sql_generated": context.sql_context is not None,
            "plan_approved": context.plan_approved,
            "query_executed": context.execution_result is not None,
            "retry_count": context.retry_count,
            "sql_attempts": len(context.sql_attempt_history),
            "fix_attempts": len(context.fix_attempts),
            "last_execution_error": context.last_execution_error,
        }
        super().__init__(f"node={node_name}: {error}; context={summary}")
        self.node_name = node_name
        self.context = context


class Workflow:
    def __init__(self, context: Context, nodes: list[Node]) -> None:
        self.context = context
        self.nodes = nodes

    def run(self) -> dict:
        for node in self.nodes:
            result = _execute_observed_node(self.context, node)
            if not result.success:
                raise WorkflowError(
                    result.node_name,
                    result.error or f"Node {result.node_name} failed",
                    self.context,
                )

        if self.context.final_output is None:
            raise WorkflowError(
                "workflow",
                "Workflow completed without final output",
                self.context,
            )
        return self.context.final_output


class ReflectiveWorkflow:
    """Small bounded feedback loop for execution, reflection, and SQL repair."""

    def __init__(
        self,
        context: Context,
        setup_nodes: list[Node],
        gen_sql_node: Node,
        execute_sql_node: Node,
        reflect_node: Node,
        fix_node: Node,
        output_node: Node,
        parallel_candidates_node: Node | None = None,
        tool_loop_node: Node | None = None,
        visualization_node: Node | None = None,
        plan_output_node: Node | None = None,
        plan_node: Node | None = None,
        max_retries: int = 2,
        analysis_hook: Callable[[Context], None] | None = None,
        candidate_hook: Callable[[Context], None] | None = None,
    ) -> None:
        if max_retries < 0:
            raise ValueError("max_retries must be zero or greater")
        self.context = context
        self.setup_nodes = setup_nodes
        self.gen_sql_node = gen_sql_node
        self.parallel_candidates_node = parallel_candidates_node
        self.tool_loop_node = tool_loop_node
        self.execute_sql_node = execute_sql_node
        self.reflect_node = reflect_node
        self.fix_node = fix_node
        self.output_node = output_node
        self.visualization_node = visualization_node
        self.plan_output_node = plan_output_node
        self.plan_node = plan_node
        self.max_retries = max_retries
        self.analysis_hook = analysis_hook
        self.candidate_hook = candidate_hook

    def run(self) -> dict:
        for node in self.setup_nodes:
            self._run_required(node)
        if self.analysis_hook is not None:
            self._run_hook("agent_analysis", self.analysis_hook)
        if getattr(self.context, "final_output", None) is not None:
            assert self.context.final_output is not None
            return self.context.final_output
        if self.tool_loop_node is not None and self.context.sql_context is None:
            self._run_required(self.tool_loop_node)
            if getattr(self.context, "final_output", None) is not None:
                assert self.context.final_output is not None
                return self.context.final_output
        if self.context.sql_context is None:
            if self.parallel_candidates_node is not None:
                self._run_required(self.parallel_candidates_node)
            else:
                self._run_required(self.gen_sql_node)

        while True:
            if self.candidate_hook is not None:
                self._run_hook("agent_candidate", self.candidate_hook)
                if getattr(self.context, "final_output", None) is not None:
                    assert self.context.final_output is not None
                    return self.context.final_output
            if self.plan_node is not None:
                self._run_required(self.plan_node)
                if self.plan_output_node is not None:
                    self._run_required(self.plan_output_node)
                    assert self.context.final_output is not None
                    return self.context.final_output

            execute_result = self._run(self.execute_sql_node)
            attempt = SqlAttempt(
                attempt_number=len(self.context.sql_attempt_history) + 1,
                sql=self.context.sql_context.sql if self.context.sql_context else "",
                status="success" if execute_result.success else "failed",
                row_count=(
                    self.context.execution_result.row_count
                    if execute_result.success and self.context.execution_result
                    else None
                ),
                error=execute_result.error if not execute_result.success else None,
                execution_duration_ms=self.context.sql_execution_duration_ms,
            )
            self.context.sql_attempt_history.append(attempt)

            if not execute_result.success:
                error = execute_result.error or "Unknown SQL execution error"
                self.context.last_execution_error = error
                self.context.execution_errors.append(error)
                if (
                    self.context.sql_policy_decisions
                    and not self.context.sql_policy_decisions[-1].allowed
                ):
                    raise WorkflowError("execute_sql", error, self.context)
                LOGGER.warning(
                    "sql_retry_requested trigger=execution_failure retry_count=%s error=%s",
                    self.context.retry_count,
                    error,
                )
                self._require_retry("execution failure", error)
                self.context.retry_count += 1
                self._run_required(self.fix_node)
                continue

            self.context.last_execution_error = None
            self._run_required(self.reflect_node)
            reflection = self.context.reflection_result
            if reflection is None:
                raise WorkflowError(
                    "reflect", "ReflectNode did not produce a result", self.context
                )
            attempt.reflection_strategy = reflection.strategy
            attempt.reflection_reason = reflection.reason
            LOGGER.info(
                "reflection strategy=%s success=%s reason=%s",
                reflection.strategy,
                reflection.success,
                reflection.reason,
            )

            if reflection.strategy == "SUCCESS":
                self._run_required(self.output_node)
                if self.visualization_node is not None:
                    visualization_result = self._run(self.visualization_node)
                    if not visualization_result.success:
                        assert self.context.final_output is not None
                        self.context.final_output["visualization"] = {
                            "chart_type": "table",
                            "chart_config": {
                                "format": "table",
                                "columns": (
                                    self.context.execution_result.columns
                                    if self.context.execution_result
                                    else []
                                ),
                                "rows": (
                                    self.context.execution_result.rows
                                    if self.context.execution_result
                                    else []
                                ),
                            },
                            "chart_path": None,
                            "reason": "Visualization failed; SQL output remains valid.",
                            "error": visualization_result.error,
                        }
                assert self.context.final_output is not None
                return self.context.final_output

            if reflection.strategy == "NEED_USER_REVIEW":
                raise WorkflowError(
                    "reflect",
                    f"Human review required: {reflection.reason}",
                    self.context,
                )

            self._require_retry(reflection.strategy, reflection.reason)
            self.context.retry_count += 1
            if reflection.strategy == "FIX_SQL":
                self.context.last_execution_error = (
                    "Reflection requested FIX_SQL: " + reflection.reason
                )
                if reflection.suggested_fix:
                    self.context.last_execution_error += (
                        " | Suggested fix: " + reflection.suggested_fix
                    )
                self._run_required(self.fix_node)
                continue

            if reflection.strategy == "REGENERATE":
                self.context.regeneration_feedback = reflection.reason
                if reflection.suggested_fix:
                    self.context.regeneration_feedback += (
                        " | Suggested direction: " + reflection.suggested_fix
                    )
                self.context.execution_result = None
                self.context.reflection_result = None
                self._run_required(self.gen_sql_node)
                continue

            raise WorkflowError(
                "reflect",
                f"Unsupported reflection strategy: {reflection.strategy}",
                self.context,
            )

    def _require_retry(self, trigger: str, detail: str) -> None:
        if self.context.retry_count < self.max_retries:
            emit_event(
                self.context.event_emitter,
                "retrying",
                self.context.run_id,
                node_name="reflect",
                status="retrying",
                message=f"Retry requested: {trigger}.",
                data={"retry_number": self.context.retry_count + 1},
            )
            return
        attempts = [
            {
                "attempt": attempt.attempt_number,
                "status": attempt.status,
                "error": attempt.error,
                "reflection": attempt.reflection_strategy,
            }
            for attempt in self.context.sql_attempt_history
        ]
        raise WorkflowError(
            "retry_limit",
            f"Maximum SQL retries ({self.max_retries}) exhausted after {trigger}. "
            f"Last detail: {detail}. Attempt history: {attempts}",
            self.context,
        )

    def _run_required(self, node: Node) -> NodeResult:
        result = self._run(node)
        if not result.success:
            raise WorkflowError(
                result.node_name,
                result.error or f"Node {result.node_name} failed",
                self.context,
            )
        return result

    def _run(self, node: Node) -> NodeResult:
        return _execute_observed_node(self.context, node)

    def _run_hook(self, name: str, hook: Callable[[Context], None]) -> None:
        try:
            hook(self.context)
        except WorkflowError:
            raise
        except Exception as exc:
            raise WorkflowError(name, str(exc), self.context) from exc
