"""Pydantic models that carry state between workflow nodes."""

from __future__ import annotations

from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field

from queryforge.domain.semantic import (
    MetricMatch,
    ResolvedJoinPath,
    SemanticModelContext,
    SubjectSelection,
)


class SqlTask(BaseModel):
    question: str = Field(min_length=1)
    database_path: str = Field(min_length=1)


class ReasoningJoin(BaseModel):
    left_table: str = ""
    right_table: str = ""
    join_type: str = "inner"
    left_key: str = ""
    right_key: str = ""
    reason: str = ""


class ReasoningMetric(BaseModel):
    name: str = ""
    expression: str = ""
    alias: str = ""
    source: Literal["metric_model", "ad_hoc", "unknown"] = "unknown"


class ReasoningFilter(BaseModel):
    column: str = ""
    operator: str = ""
    value: str = ""
    logic: Literal["AND", "OR", "UNKNOWN"] = "UNKNOWN"


class ReasoningSort(BaseModel):
    column: str = ""
    direction: Literal["ASC", "DESC", "UNKNOWN"] = "UNKNOWN"


class ReasoningResult(BaseModel):
    """Auditable decision summary, never a hidden chain-of-thought."""

    goal: str = ""
    grain: str = ""
    tables: list[str] = Field(default_factory=list)
    joins: list[ReasoningJoin] = Field(default_factory=list)
    metrics: list[ReasoningMetric] = Field(default_factory=list)
    dimensions: list[str] = Field(default_factory=list)
    filters: list[ReasoningFilter] = Field(default_factory=list)
    time_range: str | None = None
    sorting: list[ReasoningSort] = Field(default_factory=list)
    limit: int | None = None
    assumptions: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    strategy: str = ""


class TableColumn(BaseModel):
    name: str
    data_type: str = ""
    nullable: bool = True
    primary_key: bool = False


class ForeignKeyReference(BaseModel):
    column: str
    referenced_table: str
    referenced_column: str


class TableSchema(BaseModel):
    table_name: str
    columns: list[TableColumn] = Field(default_factory=list)
    foreign_keys: list[ForeignKeyReference] = Field(default_factory=list)


class ColumnValueHint(BaseModel):
    """Question-relevant values observed in one database column."""

    table_name: str
    column_name: str
    values: list[str] = Field(default_factory=list)


class ReferenceExample(BaseModel):
    """A lexically similar, locally bundled question-to-SQL example."""

    question: str
    sql: str
    similarity: float


class HistoryMatch(BaseModel):
    """One lexically similar persisted SQL history entry."""

    id: int
    question: str
    sql: str
    explanation: str = ""
    tables_used: list[str] = Field(default_factory=list)
    similarity: float
    row_count: int | None = None
    provider: str | None = None
    model: str | None = None
    created_at: str
    source: str = "query"


class VectorMatch(BaseModel):
    """One document retrieved from the optional vector knowledge base."""

    id: str
    text: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    source_type: str
    created_at: str
    score: float | None = None


class DateRange(BaseModel):
    """One inclusive calendar-date range resolved from the user question."""

    expression: str
    start_date: str
    end_date: str


class DateContext(BaseModel):
    """Explicit date semantics supplied to SQL generation."""

    reference_date: str
    source: Literal["none", "rule", "llm", "llm_fallback_failed"] = "none"
    ranges: list[DateRange] = Field(default_factory=list)
    note: str = "All start_date and end_date values are inclusive calendar dates."


class SQLContext(BaseModel):
    sql: str = Field(min_length=1)
    explanation: str = Field(min_length=1)
    tables_used: list[str] = Field(default_factory=list)
    reasoning_result: ReasoningResult | None = None
    reasoning_validation: dict[str, Any] | None = None


class ExecutionResult(BaseModel):
    columns: list[str] = Field(default_factory=list)
    rows: list[list[Any]] = Field(default_factory=list)
    row_count: int = 0
    #: True when the adapter's row bound cut the result short. Without this the
    #: bound was invisible: ``_enforce_row_bound`` overwrote ``row_count`` with the
    #: truncated length, so a caller could not tell a complete result from a
    #: capped one, and the original size was not recorded anywhere.
    truncated: bool = False
    #: Rows the engine actually produced, before the bound was applied. Equal to
    #: ``row_count`` when ``truncated`` is False.
    fetched_row_count: int = 0


class SqlPolicyDecision(BaseModel):
    """One auditable AST policy decision made before SQLite execution."""

    allowed: bool
    run_id: str
    policy_name: str
    rule: str
    reason: str
    tables: list[str] = Field(default_factory=list)
    columns: list[str] = Field(default_factory=list)
    functions: list[str] = Field(default_factory=list)
    limit: int | None = None


class VisualizationResult(BaseModel):
    """Rule-selected chart metadata and optional local Vega-Lite file."""

    chart_type: Literal["bar", "line", "pie", "table"]
    chart_config: dict[str, Any] = Field(default_factory=dict)
    chart_path: str | None = None
    reason: str
    error: str | None = None


class ExecutionPlan(BaseModel):
    """Read-only review artifact created before SQL execution."""

    question: str
    tables: list[str] = Field(default_factory=list)
    date_context: DateContext | None = None
    sql: str
    risks: list[str] = Field(default_factory=list)


ReflectionStrategy = Literal[
    "SUCCESS", "FIX_SQL", "REGENERATE", "NEED_USER_REVIEW"
]


class ReflectionResult(BaseModel):
    success: bool
    strategy: ReflectionStrategy
    reason: str = Field(min_length=1)
    suggested_fix: str | None = None


class FixAttempt(BaseModel):
    retry_number: int
    trigger: str
    original_sql: str
    fixed_sql: str
    explanation: str


class SqlAttempt(BaseModel):
    attempt_number: int
    sql: str
    status: Literal["success", "failed"]
    row_count: int | None = None
    error: str | None = None
    reflection_strategy: ReflectionStrategy | None = None
    reflection_reason: str | None = None
    execution_duration_ms: float | None = None


class NodeResult(BaseModel):
    node_name: str
    success: bool
    status: Literal["success", "failed"]
    message: str = ""
    error: str | None = None
    started_at: str | None = None
    ended_at: str | None = None
    duration_ms: float | None = None


class RunContext(BaseModel):
    """Run identity and the versions a run executed against.

    Both execution paths (the conversational workflow and the planner) populate
    this, so a run's identity and the semantic/data/policy versions it depended on
    travel with the payload instead of being reconstructed from whichever layer
    happens to be asking. Artifact provenance and recovery fingerprints need the
    same four versions, and before this each caller assembled them separately.
    """

    run_id: str
    task_id: str | None = None
    session_id: str | None = None
    domain_id: str | None = None
    #: Versions the answer is only valid for. ``None`` means "not declared", which
    #: is different from "unchanged" and is reported as such.
    data_version: str | None = None
    semantic_version: str | None = None
    policy_version: str | None = None
    entrypoint: str | None = None

    @classmethod
    def from_context(cls, context: "Context", **overrides: Any) -> "RunContext":
        """Build from anything already known, leaving unknown fields as None."""

        task_context = context.task_context if isinstance(context.task_context, dict) else {}
        scope = task_context.get("retrieval_scope")
        scope = scope if isinstance(scope, dict) else {}
        semantic = context.semantic_model
        values: dict[str, Any] = {
            "run_id": context.run_id,
            "task_id": task_context.get("task_id"),
            "session_id": task_context.get("session_id"),
            "domain_id": scope.get("domain_id"),
            "data_version": scope.get("data_version"),
            "semantic_version": getattr(getattr(semantic, "model", None), "version", None),
            "policy_version": (context.sql_policy or {}).get("version"),
            "entrypoint": task_context.get("entrypoint"),
        }
        values.update({key: value for key, value in overrides.items() if value is not None})
        return cls(**values)


class Context(BaseModel):
    task: SqlTask
    run_id: str = Field(default_factory=lambda: f"qf_{uuid4().hex}")
    selected_provider: str | None = None
    selected_model: str | None = None
    available_skills_context: str = ""
    loaded_skills_context: str = ""
    loaded_skill_names: list[str] = Field(default_factory=list)
    skill_selection_mode: str = "auto"
    skill_selection_reason: str = ""
    date_context: DateContext | None = None
    plan_mode: bool = False
    execution_plan: ExecutionPlan | None = None
    plan_approved: bool | None = None
    reflection_result: ReflectionResult | None = None
    #: Why a model-supplied ``reasoning`` payload was rejected, when it was. The
    #: rejection used to be silent: the payload was validated, failed, and dropped
    #: with nothing but a warning buried in ``reasoning_validation``, so a model
    #: that always emitted an incompatible shape looked identical to one that
    #: emitted nothing at all.
    reasoning_discarded: str | None = None
    fix_attempts: list[FixAttempt] = Field(default_factory=list)
    retry_count: int = 0
    execution_errors: list[str] = Field(default_factory=list)
    sql_attempt_history: list[SqlAttempt] = Field(default_factory=list)
    last_execution_error: str | None = None
    sql_execution_duration_ms: float | None = None
    regeneration_feedback: str = ""
    history_matches: list[HistoryMatch] = Field(default_factory=list)
    history_entry_id: int | None = None
    history_write_status: Literal["not_attempted", "inserted", "duplicate", "failed"] = (
        "not_attempted"
    )
    history_error: str | None = None
    vector_kb_enabled: bool = False
    vector_kb_status: Literal["disabled", "active", "degraded"] = "disabled"
    vector_kb_error: str | None = None
    vector_sql_matches: list[VectorMatch] = Field(default_factory=list)
    vector_schema_matches: list[VectorMatch] = Field(default_factory=list)
    vector_write_status: Literal[
        "not_attempted", "inserted", "failed", "disabled"
    ] = "disabled"
    semantic_model: SemanticModelContext | None = None
    subject_selection: SubjectSelection | None = None
    metric_matches: list[MetricMatch] = Field(default_factory=list)
    metric_requested_dimensions: list[str] = Field(default_factory=list)
    metric_join_paths: list[ResolvedJoinPath] = Field(default_factory=list)
    sql_policy: dict[str, Any] = Field(default_factory=dict)
    sql_policy_decisions: list[SqlPolicyDecision] = Field(default_factory=list)
    relevant_tables: list[TableSchema] = Field(default_factory=list)
    value_hints: list[ColumnValueHint] = Field(default_factory=list)
    reference_examples: list[ReferenceExample] = Field(default_factory=list)
    sql_context: SQLContext | None = None
    execution_result: ExecutionResult | None = None
    visualization_result: VisualizationResult | None = None
    final_output: dict[str, Any] | None = None
    event_emitter: Any | None = Field(default=None, exclude=True)
    report_requested: bool = False
    report_output_dir: str = ".queryforge/reports"
    report_max_rows: int = 50
    report_max_charts: int = 3
    tool_loop_history: list[dict[str, Any]] = Field(default_factory=list)
    tool_loop_status: Literal[
        "disabled", "completed", "max_rounds", "timeout", "error"
    ] = "disabled"
    tool_loop_exit_reason: str | None = None
    candidate_selection: dict[str, Any] | None = None
    #: Which SQL-producing strategy this attempt used: ``tool_loop`` when the
    #: exploration loop produced the answer, ``parallel_candidates`` when several
    #: candidates were generated and selected, ``single_generation`` otherwise.
    #: Recorded because "complex" enables both the tool loop and extra candidates,
    #: and the tool loop wins — so a run cannot be audited for candidate use without
    #: this field.
    candidate_strategy: str = "unknown"
    reasoning_result: ReasoningResult | None = None
    reasoning_validation: dict[str, Any] | None = None
    node_results: list[NodeResult] = Field(default_factory=list)
    # Shared structured context for step 04/05/07/08 workflows (schema
    # retrieval evidence, typed error categories, analysis patches, ...).
    task_context: dict[str, Any] = Field(default_factory=dict)
    #: Unified run identity and the versions this run depends on.
    run_context: RunContext | None = None
    #: Shared budget bounding every model call in this run, or None when the path
    #: does not charge model calls. Excluded from serialization: it is a live
    #: object graph, and its snapshot is reported instead.
    model_budget: Any | None = Field(default=None, exclude=True)
    #: Populated when a model call was refused by that budget, so a stopped run can
    #: say it stopped for budget rather than merely that it stopped.
    budget_refusal: dict[str, Any] = Field(default_factory=dict)
