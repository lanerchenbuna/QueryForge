"""Transport request schemas kept separate from workflow state."""

from typing import Any, Literal

from pydantic import BaseModel, Field

from queryforge.application import AgentOptions


class AskRequest(BaseModel):
    question: str = Field(min_length=1)
    database: str | None = None
    domain_id: str | None = None
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
            domain_id=self.domain_id,
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


class AnalyzeRequest(BaseModel):
    """One planned, evidence-gated analysis request.

    `limit` fields are optional; when omitted the planner uses its documented
    defaults (see `queryforge.orchestration.tools.budget.DEFAULT_LIMITS`). The
    durable-run fields (`run_id`, `resume`, `force_resume`) expose step 15's
    resumable execution over the network, which used to be reachable only from the
    local CLI (M5).
    """

    question: str = Field(min_length=1)
    database: str | None = None
    domain_id: str | None = None
    semantic_model_path: str | None = None
    sql_policy_path: str | None = None
    mode: Literal["execute", "plan_only"] = "execute"
    max_tool_calls: int | None = None
    max_sql_duration_ms: float | None = None
    model_deadline_ms: float | None = None
    max_output_rows: int | None = None
    max_output_bytes: int | None = None
    max_estimated_tokens: int | None = None
    max_replans: int = 2
    #: Durable run identity: supplying it persists the plan and step journal under
    #: the deployment's orchestration state root, which is what makes the run
    #: resumable and inspectable through ``/analyze/runs/{run_id}``. The id becomes
    #: a directory name under that root, so it is constrained to the vocabulary the
    #: run state store already enforces.
    run_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{1,64}$")
    resume: bool = False
    force_resume: bool = False

    def to_limits(self) -> dict[str, float] | None:
        limits = {
            "max_tool_calls": self.max_tool_calls,
            "max_sql_duration_ms": self.max_sql_duration_ms,
            "model_deadline_ms": self.model_deadline_ms,
            "max_output_rows": self.max_output_rows,
            "max_output_bytes": self.max_output_bytes,
            "max_estimated_tokens": self.max_estimated_tokens,
        }
        resolved = {key: value for key, value in limits.items() if value is not None}
        return resolved or None

    def to_kwargs(self, entrypoint: str | None = "api") -> dict:
        """Keyword arguments for ``AnalysisPlannerService.analyze``.

        This schema describes a *network* request, so it defaults to the ``api``
        entrypoint and the planner then applies the same path allowlist as
        ``/ask`` (H4). A local, in-process caller that passes ``entrypoint=None``
        keeps the unrestricted local behaviour.
        """

        return {
            "database": self.database,
            "domain_id": self.domain_id,
            "semantic_model_path": self.semantic_model_path,
            "sql_policy_path": self.sql_policy_path,
            "mode": self.mode,
            "limits": self.to_limits(),
            "max_replans": self.max_replans,
            "run_id": self.run_id,
            "resume": self.resume,
            "force_resume": self.force_resume,
            "entrypoint": entrypoint,
        }


class GatewayWebhookRequest(BaseModel):
    user_id: str = Field(min_length=1)
    channel: str = Field(min_length=1)
    text: str = Field(min_length=1)


class SessionExpireRequest(BaseModel):
    """Retention-driven expiry of one session, or of every stored session.

    Both fields are optional: with neither, every session is expired against its
    own retention window (stage 13's ``SessionStore.expire_all``).
    """

    session_id: str | None = None
    before: str | None = None


class SessionPreferenceRequest(BaseModel):
    """One user-scoped conversation preference.

    ``user_id`` is mandatory because preferences are never global: a session
    cannot carry an anonymous preference that a later user could inherit.
    """

    user_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    value: Any = None
    domain_id: str | None = None


class SessionVersionInvalidationRequest(BaseModel):
    """Invalidate the turns that relied on a superseded definition version."""

    version_ref: str = Field(min_length=1)
    session_id: str | None = None
    reason: str | None = None
