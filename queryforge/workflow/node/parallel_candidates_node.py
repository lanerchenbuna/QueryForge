"""Generate and select a bounded set of SQL candidates."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import time

from queryforge.workflow.node.base import Node
from queryforge.workflow.node.gen_sql_node import GenSqlNode
from queryforge.workflow.sql_selector import SQLSelector
from queryforge.domain.semantic import QuerySpecCompiler
from queryforge.infrastructure.models.base import BaseModelProvider, ModelResponseError
from queryforge.core.schemas.models import (
    Context,
    NodeResult,
    ReasoningResult,
    SQLContext,
)
from queryforge.infrastructure.tools.database_tool import DatabaseTool


class ParallelCandidatesNode(Node):
    name = "parallel_candidates"
    description = "Generate bounded SQL candidates and select the best preview"

    #: One deterministic QuerySpec candidate is appended after the generated
    #: ones, and it must still fit the selector's preview ceiling to compete, so
    #: the generated-candidate bound is derived from that ceiling instead of
    #: duplicating the number.
    MAX_CANDIDATES = SQLSelector.MAX_PREVIEW - 1

    def __init__(
        self,
        llm: BaseModelProvider,
        database_tool: DatabaseTool,
        *,
        candidate_count: int = 2,
        max_preview: int = 2,
        preview_limit: int = 20,
        preview_timeout_seconds: float = 10,
        selector_weights: dict[str, float] | None = None,
    ) -> None:
        if candidate_count < 2 or candidate_count > self.MAX_CANDIDATES:
            raise ValueError(
                f"candidate_count must be between 2 and {self.MAX_CANDIDATES}"
            )
        self.llm = llm
        self.database_tool = database_tool
        self.candidate_count = candidate_count
        self.selector = SQLSelector(
            database_tool,
            max_preview=max_preview,
            preview_limit=preview_limit,
            timeout_seconds=preview_timeout_seconds,
            weights=selector_weights,
        )

    def execute(self, context: Context) -> NodeResult:
        prompt = GenSqlNode._build_prompt(context)
        candidates: list[dict | None] = [None] * self.candidate_count
        started = time.monotonic()
        with ThreadPoolExecutor(
            max_workers=self.candidate_count,
            thread_name_prefix="queryforge-candidate",
        ) as executor:
            futures = {
                executor.submit(self._generate_candidate, prompt, index): index
                for index in range(self.candidate_count)
            }
            for future in as_completed(futures):
                index = futures[future]
                try:
                    candidates[index] = future.result()
                except Exception as exc:
                    candidates[index] = self._generation_error(index, exc)
        resolved_candidates = [
            candidate if candidate is not None else self._generation_error(index, None)
            for index, candidate in enumerate(candidates)
        ]
        # Step 07: add ONE deterministic candidate compiled from the governed
        # metric request. It passes through exactly the same selector hard gates
        # (AST policy -> semantic validator -> preview).
        spec_candidate = self._query_spec_candidate(context, len(resolved_candidates))
        if spec_candidate is not None:
            resolved_candidates.append(spec_candidate)
        selection = self._selector_for(len(resolved_candidates)).select(
            resolved_candidates, context
        )
        selection["candidate_count"] = self.candidate_count
        selection["total_candidates"] = len(resolved_candidates)
        selection["query_spec_candidate"] = spec_candidate is not None
        selection["generation_mode"] = "concurrent"
        selection["generation_duration_ms"] = round(
            (time.monotonic() - started) * 1000,
            3,
        )
        selection["candidates"] = resolved_candidates
        context.candidate_selection = selection
        selected_index = selection.get("selected_index")
        if selected_index is None:
            return self.failure(
                "No generated SQL candidate passed selector validation: "
                + str(selection.get("reason"))
            )
        selected = resolved_candidates[selected_index]
        context.sql_context = SQLContext(
            sql=selected["sql"],
            explanation=selected["explanation"],
            tables_used=selected["tables_used"],
            reasoning_result=selected.get("reasoning_result"),
            reasoning_validation=selected.get("reasoning_validation"),
        )
        context.reasoning_result = context.sql_context.reasoning_result
        context.reasoning_validation = context.sql_context.reasoning_validation
        return self.success(
            f"Selected candidate {selected_index + 1}/{len(resolved_candidates)}"
        )

    def _query_spec_candidate(self, context: Context, index: int) -> dict | None:
        """Deterministic compiled candidate for a matched metric request."""
        spec = QuerySpecCompiler.for_context(
            context,
            limit=max(self.selector.preview_limit, 100),
        )
        if spec is None:
            return None
        return spec.to_candidate(index)

    def _selector_for(self, candidate_total: int) -> SQLSelector:
        """Selector whose preview budget also covers the deterministic candidate."""
        if candidate_total <= self.selector.max_preview:
            return self.selector
        return SQLSelector(
            self.database_tool,
            max_preview=candidate_total,
            preview_limit=self.selector.preview_limit,
            timeout_seconds=self.selector.timeout_seconds,
            weights=self.selector.weights,
        )

    def _generate_candidate(self, prompt: str, index: int) -> dict:
        started = time.monotonic()
        try:
            payload = self.llm.generate_json(
                prompt
                + f"\nGenerate candidate {index + 1} of {self.candidate_count}. "
                "Use a materially different valid approach when possible."
            )
            sql_context = SQLContext.model_validate(payload)
            reasoning_result = None
            reasoning_validation = None
            if payload.get("reasoning") is not None:
                try:
                    reasoning_result = ReasoningResult.model_validate(
                        payload["reasoning"]
                    )
                    reasoning_validation = GenSqlNode._validate_reasoning(
                        sql_context.sql,
                        reasoning_result,
                    )
                except ValueError as exc:
                    reasoning_validation = {
                        "status": "warning",
                        "warnings": [f"Invalid reasoning payload: {exc}"],
                    }
            return {
                "candidate_index": index,
                "sql": sql_context.sql,
                "explanation": sql_context.explanation,
                "tables_used": sql_context.tables_used,
                "reasoning_result": (
                    reasoning_result.model_dump(mode="json")
                    if reasoning_result
                    else None
                ),
                "reasoning_validation": reasoning_validation,
                "generation_duration_ms": round(
                    (time.monotonic() - started) * 1000,
                    3,
                ),
            }
        except (ModelResponseError, ValueError, TypeError) as exc:
            return self._generation_error(index, exc, started)

    @staticmethod
    def _generation_error(
        index: int,
        error: Exception | None,
        started: float | None = None,
    ) -> dict:
        payload = {
            "candidate_index": index,
            "sql": None,
            "explanation": None,
            "tables_used": [],
            "generation_error": str(error or "Candidate generation did not complete."),
        }
        if started is not None:
            payload["generation_duration_ms"] = round(
                (time.monotonic() - started) * 1000,
                3,
            )
        return payload
