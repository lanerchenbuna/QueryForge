"""Direct read-only metadata and SQL-review application use cases."""

from __future__ import annotations

from queryforge.application.options import AgentOptions
from queryforge.core.config import Config
from queryforge.core.schemas.models import Context, SQLContext, SqlTask
from queryforge.domain.security import load_sql_policy
from queryforge.infrastructure.db.sqlite_connector import SQLiteConnector
from queryforge.infrastructure.storage import SQLHistoryStore
from queryforge.infrastructure.tools.database_tool import DatabaseTool
from queryforge.infrastructure.tools.reference_sql_tool import ReferenceSqlTool
from queryforge.orchestration.orchestrator.orchestrator import OrchestratorAgent
from queryforge.orchestration.schemas import TaskState
from queryforge.workflow.node.schema_linking_node import SchemaLinkingNode
from queryforge.workflow.node.subject_selection_node import SubjectSelectionNode


class DirectTaskExecutor:
    """Execute non-generative task routes while preserving orchestration artifacts."""

    def metadata(
        self,
        state: TaskState,
        orchestrator: OrchestratorAgent,
        task: SqlTask,
        config: Config,
        options: AgentOptions,
        run_id: str,
    ) -> dict:
        context = self._new_context(task, config, run_id)
        with SQLiteConnector(task.database_path) as connector:
            database_tool = self._database_tool(connector, config, options)
            self._populate_context(context, database_tool, config, options)
            state.current_phase = "analysis"
            orchestrator.start_phase(state, "analysis")
            orchestrator.schema_architect.run(state, context)
            orchestrator.check_gate(state, "schema", context)
            if context.final_output is not None:
                return context.final_output
            orchestrator.knowledge.run(state, context)
            orchestrator.check_gate(state, "analysis", context)
            orchestrator.complete_phase(state, "analysis")
            if context.final_output is not None:
                return context.final_output
        return {
            "status": "success",
            "run_id": run_id,
            "question": task.question,
            "metadata": self._metadata_payload(context),
        }

    def sql_review(
        self,
        state: TaskState,
        orchestrator: OrchestratorAgent,
        task: SqlTask,
        config: Config,
        options: AgentOptions,
        run_id: str,
    ) -> dict:
        context = self._new_context(task, config, run_id)
        with SQLiteConnector(task.database_path) as connector:
            database_tool = self._database_tool(connector, config, options)
            self._populate_context(context, database_tool, config, options)
            self._run_review_analysis(state, orchestrator, context)
            if context.final_output is not None:
                return context.final_output
            sql = self._run_review_candidate(
                state, orchestrator, context, database_tool, task.question
            )
            review_ref = self._run_review_gate(
                state, orchestrator, context, database_tool, task.question
            )
            if context.final_output is not None:
                return context.final_output
            state.current_phase = "completion"
            orchestrator.start_phase(state, "completion")
            orchestrator.ops.run(state)
            orchestrator.complete_phase(state, "completion")
        return {
            "status": "success",
            "run_id": run_id,
            "question": task.question,
            "sql": sql,
            "review_artifact": review_ref.model_dump(mode="json"),
            "executed": False,
        }

    @staticmethod
    def _run_review_analysis(
        state: TaskState,
        orchestrator: OrchestratorAgent,
        context: Context,
    ) -> None:
        state.current_phase = "analysis"
        orchestrator.start_phase(state, "analysis")
        orchestrator.product_analyst.run(state, context)
        orchestrator.schema_architect.run(state, context)
        orchestrator.check_gate(state, "analysis", context)
        orchestrator.complete_phase(state, "analysis")

    @staticmethod
    def _run_review_candidate(
        state: TaskState,
        orchestrator: OrchestratorAgent,
        context: Context,
        database_tool: DatabaseTool,
        question: str,
    ) -> str | None:
        sql = orchestrator.sql_review.extract_sql(question)
        state.current_phase = "candidate"
        orchestrator.start_phase(state, "candidate")
        if sql:
            context.sql_context = SQLContext(
                sql=sql,
                explanation="User-provided SQL for static review.",
                tables_used=[],
            )
            orchestrator.sql_developer.run(state, context)
            try:
                orchestrator.governance.run(state, context, database_tool)
            except Exception:
                pass  # Governance already persisted the blocking decision.
        else:
            orchestrator.state_store.write_artifact(
                state,
                artifact_type="sql_candidate",
                producer="SQLDeveloperAgent",
                status="blocked",
                payload={
                    "sql": None,
                    "reason": "No SELECT or WITH statement was found in the request.",
                    "generated_by": "user_input_extraction",
                },
            )
        orchestrator.complete_phase(state, "candidate")
        return sql

    @staticmethod
    def _run_review_gate(
        state: TaskState,
        orchestrator: OrchestratorAgent,
        context: Context,
        database_tool: DatabaseTool,
        question: str,
    ):
        state.current_phase = "review"
        orchestrator.start_phase(state, "review")
        review_ref = orchestrator.sql_review.run(
            state,
            question,
            database_tool,
            context.semantic_model,
        )
        orchestrator.complete_phase(state, "review")
        orchestrator.check_gate(state, "review", context)
        if context.final_output is None:
            orchestrator.check_gate(state, "sql_candidate", context)
        return review_ref

    @staticmethod
    def _new_context(task: SqlTask, config: Config, run_id: str) -> Context:
        return Context(
            task=task,
            run_id=run_id,
            selected_provider=config.llm_provider,
            selected_model=config.llm_model,
            reference_examples=ReferenceSqlTool(task.database_path).find_similar(
                task.question
            ),
            vector_kb_enabled=False,
            vector_kb_status="disabled",
            vector_write_status="disabled",
        )

    @staticmethod
    def _database_tool(
        connector: SQLiteConnector,
        config: Config,
        options: AgentOptions,
    ) -> DatabaseTool:
        policy, source = load_sql_policy(
            options.sql_policy_path or config.sql_policy_path
        )
        return DatabaseTool(connector, policy, policy_source_path=source)

    @staticmethod
    def _populate_context(
        context: Context,
        database_tool: DatabaseTool,
        config: Config,
        options: AgentOptions,
    ) -> None:
        context.sql_policy = database_tool.policy_summary
        subject_result = SubjectSelectionNode(
            enabled=options.subject_tree_enabled or config.subject_tree_enabled,
            subject_tree_path=options.subject_tree_path or config.subject_tree_path,
            requested_subject=options.subject,
            default_subject=options.default_subject or config.default_subject,
        ).execute(context)
        context.node_results.append(subject_result)
        if not subject_result.success:
            raise ValueError(subject_result.error or "Could not select subject scope")
        schema_result = SchemaLinkingNode(
            database_tool,
            vector_store=None,
            vector_top_k=0,
            semantic_model_path=(
                options.semantic_model_path or config.semantic_model_path
            ),
        ).execute(context)
        context.node_results.append(schema_result)
        if not schema_result.success:
            raise ValueError(schema_result.error or "Could not inspect database schema")
        if options.history_top_k > 0:
            try:
                context.history_matches = SQLHistoryStore(
                    config.history_db_path
                ).search(context.task.question, top_k=options.history_top_k)
            except Exception as exc:
                context.history_error = str(exc)
                context.history_write_status = "failed"

    @staticmethod
    def _metadata_payload(context: Context) -> dict:
        return {
            "tables": [
                {
                    "name": schema.table_name,
                    "columns": [
                        column.model_dump(mode="json") for column in schema.columns
                    ],
                    "foreign_keys": [
                        foreign_key.model_dump(mode="json")
                        for foreign_key in schema.foreign_keys
                    ],
                }
                for schema in context.relevant_tables
            ],
            "semantic_model": (
                {
                    "name": context.semantic_model.model.name,
                    "entities": [
                        entity.model_dump(mode="json")
                        for entity in context.semantic_model.model.entities
                    ],
                    "metrics": [
                        metric.model_dump(mode="json")
                        for metric in context.semantic_model.model.metrics
                    ],
                }
                if context.semantic_model
                else None
            ),
            "history_examples": [
                match.model_dump(mode="json") for match in context.history_matches
            ],
            "reference_examples": [
                example.model_dump(mode="json")
                for example in context.reference_examples
            ],
        }
