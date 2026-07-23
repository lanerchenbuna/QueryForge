"""Project existing local retrieval results into a knowledge artifact."""

from __future__ import annotations

from queryforge.orchestration.agents.base import RoleAgent
from queryforge.orchestration.schemas import ArtifactRef, TaskState
from queryforge.core.schemas.models import Context


class KnowledgeAgent(RoleAgent):
    agent_name = "KnowledgeAgent"
    artifact_type = "knowledge_context"

    def run(self, state: TaskState, context: Context) -> ArtifactRef:
        try:
            history = self._rank_history(context.history_matches)
            reference_sql = self._rank_reference(context.reference_examples)
        except Exception as exc:
            return self._fallback(state, context, str(exc))
        warning = len(history) < 2 and not context.history_error
        return self.emit(
            state,
            {
                "question": context.task.question,
                "sql_history": history,
                "recommended_sql_history": [
                    item for item in history if item.get("recommended")
                ],
                "reference_sql": reference_sql,
                "recommended_reference_sql": [
                    item for item in reference_sql if item.get("recommended")
                ],
                "vector_sql_matches": [
                    match.model_dump(mode="json")
                    for match in context.vector_sql_matches
                ],
                "vector_schema_matches": [
                    match.model_dump(mode="json")
                    for match in context.vector_schema_matches
                ],
                "skills": {
                    "loaded": list(context.loaded_skill_names),
                    "selection_mode": context.skill_selection_mode,
                    "selection_reason": context.skill_selection_reason,
                },
                "retrieval_status": {
                    "history": "degraded" if context.history_error else "active",
                    "vector": context.vector_kb_status,
                    "history_error": context.history_error,
                    "vector_error": context.vector_kb_error,
                    "history_warning": (
                        "Fewer than two successful historical SQL examples matched."
                        if warning else None
                    ),
                },
                "note": "Retrieved examples are advisory and must pass current schema and policy checks.",
            },
            status=(
                "degraded"
                if context.history_error or context.vector_kb_status == "degraded"
                else "warning"
                if warning
                else "valid"
            ),
        )

    def _fallback(
        self,
        state: TaskState,
        context: Context,
        error: str,
    ) -> ArtifactRef:
        return self.emit(
            state,
            {
                "question": context.task.question,
                "sql_history": [],
                "recommended_sql_history": [],
                "reference_sql": [],
                "recommended_reference_sql": [],
                "vector_sql_matches": [],
                "vector_schema_matches": [],
                "skills": {
                    "loaded": list(context.loaded_skill_names),
                    "selection_mode": context.skill_selection_mode,
                    "selection_reason": context.skill_selection_reason,
                },
                "retrieval_status": {
                    "history": "degraded",
                    "vector": "degraded",
                    "history_error": error,
                    "vector_error": error,
                    "history_warning": "Knowledge retrieval fallback was used.",
                },
                "note": "Knowledge retrieval failed; SQL generation must rely on schema and policy checks.",
            },
            status="degraded",
        )

    @staticmethod
    def _rank_history(matches) -> list[dict]:
        seen: set[str] = set()
        ranked = sorted(
            matches,
            key=lambda item: (
                -(item.similarity or 0),
                -(item.row_count or 0),
                item.created_at,
            ),
        )
        output: list[dict] = []
        for match in ranked:
            normalized_sql = " ".join(match.sql.lower().split())
            if normalized_sql in seen:
                continue
            seen.add(normalized_sql)
            payload = match.model_dump(mode="json")
            payload["quality_score"] = round(
                (match.similarity or 0) + (0.1 if match.row_count is not None else 0),
                3,
            )
            payload["recommended"] = len(output) < 3 and payload["quality_score"] > 0
            output.append(payload)
        return output

    @staticmethod
    def _rank_reference(examples) -> list[dict]:
        seen: set[str] = set()
        ranked = sorted(examples, key=lambda item: -(item.similarity or 0))
        output: list[dict] = []
        for example in ranked:
            normalized_sql = " ".join(example.sql.lower().split())
            if normalized_sql in seen:
                continue
            seen.add(normalized_sql)
            payload = example.model_dump(mode="json")
            payload["recommended"] = len(output) < 3 and example.similarity > 0
            output.append(payload)
        return output
