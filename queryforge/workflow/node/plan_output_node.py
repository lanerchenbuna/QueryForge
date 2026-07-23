"""Build a plan-only response without executing model-generated SQL."""

from queryforge.workflow.node.base import Node
from queryforge.core.schemas.models import Context, NodeResult
from queryforge.infrastructure.tools.database_tool import DatabaseTool


class PlanOutputNode(Node):
    name = "plan_output"
    description = "Return a generated execution plan without running SQL"

    def __init__(self, database_tool: DatabaseTool) -> None:
        self.database_tool = database_tool

    def execute(self, context: Context) -> NodeResult:
        if context.execution_plan is None or context.sql_context is None:
            return self.failure("Execution plan or generated SQL is missing")
        try:
            decision = self.database_tool.policy_engine.evaluate(
                context.sql_context.sql
            )
        except Exception as exc:
            decision = self.database_tool.last_policy_decision
            policy_violation = getattr(exc, "decision", None)
            if policy_violation is not None:
                decision = policy_violation
                self.database_tool.last_policy_decision = policy_violation
            if decision is not None:
                context.sql_policy_decisions.append(decision)
            return self.failure(f"SQL policy preflight failed: {exc}")
        self.database_tool.last_policy_decision = decision
        context.sql_policy_decisions.append(decision)
        context.final_output = {
            "status": "planned",
            "run_id": context.run_id,
            "question": context.task.question,
            "model_provider": context.selected_provider,
            "model": context.selected_model,
            "skills_used": context.loaded_skill_names,
            "date_context": (
                context.date_context.model_dump(mode="json")
                if context.date_context
                else None
            ),
            "plan": {
                **context.execution_plan.model_dump(mode="json"),
                "approved": None,
                "executed": False,
            },
            "sql": context.sql_context.sql,
            "explanation": context.sql_context.explanation,
            "tables_used": context.sql_context.tables_used,
            "subject": (
                context.subject_selection.model_dump(mode="json")
                if context.subject_selection
                else None
            ),
            "reasoning": (
                context.reasoning_result.model_dump(mode="json")
                if context.reasoning_result
                else None
            ),
            "reasoning_validation": context.reasoning_validation,
            "sql_security": {
                **context.sql_policy,
                "decisions": [
                    item.model_dump() for item in context.sql_policy_decisions
                ],
            },
        }
        if context.semantic_model:
            context.final_output["semantic_model"] = {
                "status": "active",
                "name": context.semantic_model.model.name,
                "version": context.semantic_model.model.version,
                "source_path": context.semantic_model.source_path,
                "matches": [
                    match.model_dump() for match in context.semantic_model.matches
                ],
            }
            context.final_output["metric_search"] = {
                "status": "matched" if context.metric_matches else "no_match",
                "matches": [
                    match.model_dump() for match in context.metric_matches
                ],
                "requested_dimensions": context.metric_requested_dimensions,
                "join_paths": [
                    path.model_dump() for path in context.metric_join_paths
                ],
            }
        return self.success("Plan-only output assembled")
