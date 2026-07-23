"""Validated application options shared by CLI, REST, MCP, and Gateway."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from queryforge.core.config import Config
from queryforge.workflow.node.plan_mode_node import PlanApprover, PlanPresenter


@dataclass(slots=True)
class AgentOptions:
    """Transport-neutral request options with centralized budget validation."""

    database: str | None = None
    semantic_model_path: str | None = None
    allow_schema_only: bool = False
    subject_tree_enabled: bool = False
    subject_tree_path: str | None = None
    subject: str | None = None
    default_subject: str | None = None
    sql_policy_path: str | None = None
    provided_sql: str | None = None
    model_provider: str | None = None
    model: str | None = None
    skills: list[str] | None = None
    plan_mode: bool = False
    auto_approve_plan: bool = False
    visualize: bool = False
    chart_output_dir: str | None = None
    report: bool = False
    report_output_dir: str | None = None
    report_max_rows: int | None = None
    report_max_charts: int | None = None
    date_llm_fallback: bool = False
    max_retries: int = 2
    history_top_k: int = 3
    enable_vector_kb: bool = False
    vector_top_k: int = 3
    debug_prompts: bool = False
    show_run_summary: bool = False
    plan_approver: PlanApprover | None = None
    plan_presenter: PlanPresenter | None = None
    run_id: str | None = None
    entrypoint: str = "service"
    orchestration_state_root: str | None = None
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
    selector_weights: dict[str, float] | None = None
    complexity_mode: Literal["auto", "simple", "complex"] = "auto"

    def validate(self) -> None:
        """Reject invalid budgets before any database or model work begins."""
        self._between("max_retries", self.max_retries, 0, 10)
        self._between("history_top_k", self.history_top_k, 0, 20)
        self._between("vector_top_k", self.vector_top_k, 0, 20)
        self._between("tool_loop_max_rounds", self.tool_loop_max_rounds, 1, 20)
        self._between(
            "tool_loop_timeout_seconds",
            self.tool_loop_timeout_seconds,
            0,
            300,
            lower_inclusive=False,
        )
        self._between("tool_loop_preview_limit", self.tool_loop_preview_limit, 1, 100)
        self._between("parallel_candidates", self.parallel_candidates, 1, 3)
        self._between("parallel_max_preview", self.parallel_max_preview, 1, 3)
        self._between(
            "parallel_preview_limit", self.parallel_preview_limit, 1, 100
        )
        if self.parallel_preview_timeout_seconds <= 0:
            raise ValueError("parallel_preview_timeout_seconds must be positive")
        if self.report_max_rows is not None and self.report_max_rows < 1:
            raise ValueError("report_max_rows must be positive")
        if self.report_max_charts is not None and self.report_max_charts < 1:
            raise ValueError("report_max_charts must be positive")
        if self.reset_session and not self.session_id:
            raise ValueError("reset_session requires session_id")
        if self.new_session and self.reset_session:
            raise ValueError("new_session and reset_session cannot be used together")
        if self.complexity_mode not in {"auto", "simple", "complex"}:
            raise ValueError("complexity_mode must be auto, simple, or complex")

    def validate_for_config(self, config: Config) -> None:
        if self.subject and not (
            self.subject_tree_enabled or config.subject_tree_enabled
        ):
            raise ValueError("subject requires subject_tree_enabled")

    @staticmethod
    def _between(
        name: str,
        value: float,
        minimum: float,
        maximum: float,
        *,
        lower_inclusive: bool = True,
    ) -> None:
        lower_valid = value >= minimum if lower_inclusive else value > minimum
        if not lower_valid or value > maximum:
            comparator = "between" if lower_inclusive else "greater than"
            if lower_inclusive:
                raise ValueError(f"{name} must be between {minimum:g} and {maximum:g}")
            raise ValueError(
                f"{name} must be {comparator} {minimum:g} and at most {maximum:g}"
            )
