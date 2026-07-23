"""Transport request schemas kept separate from workflow state."""

from pydantic import BaseModel, Field

from queryforge.application import AgentOptions


class AskRequest(BaseModel):
    question: str = Field(min_length=1)
    database: str | None = None
    semantic_model_path: str | None = None
    allow_schema_only: bool = False
    subject_tree_enabled: bool = False
    subject_tree_path: str | None = None
    subject: str | None = None
    default_subject: str | None = None
    sql_policy_path: str | None = None
    model_provider: str | None = None
    model: str | None = None
    skills: list[str] | None = None
    plan_mode: bool = False
    auto_approve_plan: bool = False
    visualize: bool = False
    report: bool = False
    report_output_dir: str | None = None
    report_max_rows: int | None = None
    report_max_charts: int | None = None
    session_id: str | None = None
    new_session: bool = False
    reset_session: bool = False
    tool_loop_enabled: bool = False
    tool_loop_max_rounds: int = 5
    tool_loop_timeout_seconds: float = 30
    tool_loop_preview_limit: int = 20
    parallel_candidates: int = 1
    parallel_max_preview: int = 2
    parallel_preview_limit: int = 20
    parallel_preview_timeout_seconds: float = 10
    complexity_mode: str = "auto"

    def to_options(self, entrypoint: str = "api") -> AgentOptions:
        return AgentOptions(
            database=self.database,
            semantic_model_path=self.semantic_model_path,
            allow_schema_only=self.allow_schema_only,
            subject_tree_enabled=self.subject_tree_enabled,
            subject_tree_path=self.subject_tree_path,
            subject=self.subject,
            default_subject=self.default_subject,
            sql_policy_path=self.sql_policy_path,
            model_provider=self.model_provider,
            model=self.model,
            skills=self.skills,
            plan_mode=self.plan_mode,
            auto_approve_plan=self.auto_approve_plan,
            visualize=self.visualize,
            report=self.report,
            report_output_dir=self.report_output_dir,
            report_max_rows=self.report_max_rows,
            report_max_charts=self.report_max_charts,
            session_id=self.session_id,
            new_session=self.new_session,
            reset_session=self.reset_session,
            tool_loop_enabled=self.tool_loop_enabled,
            tool_loop_max_rounds=self.tool_loop_max_rounds,
            tool_loop_timeout_seconds=self.tool_loop_timeout_seconds,
            tool_loop_preview_limit=self.tool_loop_preview_limit,
            parallel_candidates=self.parallel_candidates,
            parallel_max_preview=self.parallel_max_preview,
            parallel_preview_limit=self.parallel_preview_limit,
            parallel_preview_timeout_seconds=self.parallel_preview_timeout_seconds,
            complexity_mode=self.complexity_mode,
            entrypoint=entrypoint,
        )


class GatewayWebhookRequest(BaseModel):
    user_id: str = Field(min_length=1)
    channel: str = Field(min_length=1)
    text: str = Field(min_length=1)
